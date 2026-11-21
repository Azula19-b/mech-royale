"""Evaluate feasible firing solutions before choosing which hunter to fight."""
from dataclasses import dataclass
import math

from combat import intercept
from intelligence import angle_error, pressure_weight


@dataclass
class Shot:
    track: object
    slot: int
    aim: tuple
    trigger: bool
    score: float
    finish: bool


class FireControl:
    def __init__(self, bot):
        self.bot = bot
        self.target = None
        self.retain_until = 0
        self.selected = None

    def evaluate(self, me, track, weapon, slot, state, boost, armor_risk):
        bot = self.bot
        kind, w = weapon["kind"], weapon["stats"]
        stats, base = me["stats"], bot.config["mech"]
        gravity = w.get("gravity", 0)
        offset = stats["radius"] + 0.5
        origin = (*me["pos"][:2], me["pos"][2] + (stats["height"] + 0.5 if gravity else stats["muzzle_height"]))
        cubes = bot.config["cubes"]
        size = 1 + cubes["size_per_cube"] * min(track.actor.get("cubes", 0), cubes["stat_cap"])
        radius = base["radius"] * size
        delay = 1 / bot.config["match"]["tick_hz"]
        x, y, z = track.predict(delay)
        point = x, y, z + base["height"] * size * 0.52
        flight = 0
        if kind in ("gun", "mortar"):
            solution = intercept(origin, point, track.velocity, w["speed"], offset, gravity)
            if solution is None:
                return None
            _, flight, _ = solution
            if any(track.acceleration):
                corrected = track.predict(delay + flight)
                linear = tuple(p + v * flight for p, v in zip(track.predict(delay), track.velocity))
                point = tuple(p + c - l for p, c, l in zip(point, corrected, linear))
                solution = intercept(origin, point, track.velocity, w["speed"], offset, gravity)
                if solution is None:
                    return None
            aim, flight, point = solution
            if flight > (w["range"] / w["speed"] if "range" in w else w["ttl"]):
                return None
        else:
            dx, dy, dz = (p - o for p, o in zip(point, origin))
            aim = math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))
            if math.dist(origin, point) > w["range"]:
                return None
        yaw, pitch = aim
        if not stats["min_pitch"] <= pitch <= stats["max_pitch"]:
            return None
        ray = math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)
        muzzle = tuple(o + offset * r for o, r in zip(origin, ray))
        if not bot.arena.clear(origin, muzzle, spacing=0.75):
            return None
        distance = math.dist(origin, point)
        if gravity:
            # Conservative bound on self-splash even when running towards impact.
            if distance < w["splash_radius"] + stats["radius"] + stats["boost_speed"] * flight:
                return None
            count = max(2, math.ceil(flight * 9))
            previous = muzzle
            for i in range(1, count + 1):
                t = flight * i / count
                x, y, z = (m + r * w["speed"] * t for m, r in zip(muzzle, ray))
                sample = x, y, z - gravity * t * t / 2
                if not bot.arena.clear(previous, sample, spacing=2):
                    return None
                previous = sample
        elif not bot.arena.clear(muzzle, point, spacing=2):
            return None
        error = angle_error(yaw, me["aim"][0])
        pitch_error = abs(pitch - me["aim"][1])
        spread = math.tan(math.radians(w.get("spread_deg", 0))) * distance
        tolerance = math.atan2(radius * 0.75, max(1, distance - offset))
        ready = weapon["cooldown"] <= bot.period and not weapon.get("reload", 0) and weapon.get("ammo", 1) > 0
        aligned = (error <= tolerance + stats["turn_rate"] * delay * 0.75
                   and pitch_error <= tolerance + stats["pitch_rate"] * delay * 0.75)
        damage = w["damage"] * (1 + cubes["damage_per_cube"] * min(me["cubes"], cubes["stat_cap"]))
        if gravity:
            damage += w["splash"]
        charge = weapon.get("charge", 0)
        if kind == "laser" and "mortar" in track.actor.get("weapons", ()):
            damage *= 1 + w["cookoff"]
        hp = track.actor.get("hp", math.inf)
        finish = hp <= damage * (w["min_frac"] if kind == "laser" else 1)
        trigger = ready and aligned
        if kind == "lightning":
            reserve = stats["boost_drain"] * (1.3 if state in ("EVADE", "EMERGENCY_ZONE") else 0.55)
            if me["energy"] < w["energy"] + reserve:
                return None
            trigger = ready and error <= math.radians(w["cone_deg"]) * 0.45
        if kind == "laser":
            aligned = error <= tolerance and pitch_error <= tolerance
            if charge:
                trigger = charge < w["min_charge"] or (state == "ENGAGE" and not finish and charge < w["charge"])
            else:
                trigger = ready and aligned and not boost and (state == "ENGAGE" or finish)
        uncertainty = track.uncertainty(flight + delay)
        probability = radius / (radius + spread + uncertainty * (0.3 + flight))
        interval = w["cooldown"] + w.get("reload", 0) / w.get("mag", 1) + w.get("charge", 0)
        dps = damage * probability / interval
        slew = max(error / stats["turn_rate"], pitch_error / stats["pitch_rate"])
        wait = max(weapon["cooldown"], weapon.get("reload", 0))
        eta = min(30, hp / max(1, dps)) + wait + slew + flight
        score = (dps / 90 + 1.6 / (1 + eta) + 0.85 * finish
                 + 0.34 * min(3, pressure_weight(track, track.distance))
                 + 0.42 * trigger - 1.25 * slew - 0.65 * min(2, wait)
                 - 0.62 * armor_risk(yaw))
        if kind == "laser":
            score += 5 if charge else (-0.9 if not trigger else 0.15)
        return Shot(track, slot, aim, trigger, score, finish)

    def apply(self, obs, tracks, state, act, armor_risk):
        self.selected = None
        me = obs["self"]
        now = obs["t"]
        if state == "RECOVER":
            for weapon in me["weapons"]:
                if weapon and weapon["kind"] == "gun" and not weapon.get("reload", 0):
                    if weapon["ammo"] < weapon["stats"]["mag"] * 0.6:
                        act["reload"] = True
            return
        ordered = sorted(tracks, key=lambda t: (t.actor["id"] != self.target, t.covered, t.distance))
        best, best_score = None, -math.inf
        for track in ordered[:15]:
            for slot, weapon in enumerate(me["weapons"]):
                if weapon is None:
                    continue
                shot = self.evaluate(me, track, weapon, slot, state, act["boost"], armor_risk)
                if shot is None:
                    continue
                score = shot.score + (0.7 if track.actor["id"] == self.target and now < self.retain_until else 0)
                if score > best_score:
                    best, best_score = shot, score
        if best is None:
            self.target = None
            return
        if best.track.actor["id"] != self.target:
            self.target = best.track.actor["id"]
            self.retain_until = now + 0.75
        self.selected = best
        act["aim"] = list(best.aim)
        act["fire"] = [False, False]
        act["fire"][best.slot] = best.trigger
