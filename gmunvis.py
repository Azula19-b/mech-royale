"""Independent Python adaptations of GMUNVIS tactical ideas for Phoenix V18.

This module contains no copied GMUNVIS source. It reuses Phoenix's observation
tracks and arena queries, and keeps the expensive ideas narrowly bounded.
"""
from collections import deque
import math
import os

from intelligence import angle_error, pressure_weight

FEATURES = ("GEAR", "LASER", "SQUAD", "RECOVERY", "SPOT", "FFA")
WEAPON_VALUE = {"gun": 1.0, "mortar": 0.0, "lightning": 3.0, "laser": 12.0}


def enabled(name, default="1"):
    return os.getenv(f"V18_{name}_ENABLED", default).lower() not in ("0", "false", "no", "off")


class Gmunvis:
    """Bounded match memory for safe gear, squad-pressure and FFA decisions."""

    def __init__(self, bot):
        self.bot = bot
        self.features = {name: enabled(name) for name in FEATURES}
        self.hp_seen = {}
        self.own_hits = deque(maxlen=64)
        self.damage_from = deque(maxlen=32)
        self.ffa_signals = 0
        self.passive = False
        self.initial_alive = None
        self.our_kills = 0
        self.spot_point = None
        self.spot_time = -math.inf
        self.gear = None
        self.gear_slot = None

    @staticmethod
    def _distance(a, b):
        return math.dist(a[:2], b[:2])

    def update(self, obs, tracks):
        now = obs["t"]
        self.initial_alive = self.initial_alive or obs.get("alive", 0)
        for event in obs.get("events", ()):
            if event.get("type") == "hit":
                self.own_hits.append((event.get("target"), now))
                if event.get("weapon") == "kill":
                    self.our_kills += 1
            elif event.get("type") == "kill":
                self.our_kills += 1
            elif event.get("type") == "damaged" and event.get("by") is not None:
                self.damage_from.append((event["by"], now))
        while self.own_hits and now - self.own_hits[0][1] > 1.5:
            self.own_hits.popleft()
        while self.damage_from and now - self.damage_from[0][1] > 5.0:
            self.damage_from.popleft()

        if self.features["FFA"] and not self.passive:
            deaths = max(0, self.initial_alive - obs.get("alive", self.initial_alive))
            if now < 75 and deaths >= self.our_kills + 2:
                self.ffa_signals += 1
            me, zone = obs["self"], obs["zone"]
            npcs = [m["pos"] for m in obs.get("mechs", ()) if m.get("kind") == "npc"]
            for actor in obs.get("mechs", ()):
                if actor.get("kind") != "mech" or actor.get("hp") is None:
                    continue
                ident = actor["id"]
                hp = actor["hp"]
                previous = self.hp_seen.get(ident)
                if previous:
                    old_hp, old_t = previous
                    drop = old_hp - hp
                    ours = any(target == ident and now - hit_t < 1.2 for target, hit_t in self.own_hits)
                    in_zone = self._distance(actor["pos"], zone["center"]) < zone["radius"] - 2
                    npc_near = any(math.dist(q, actor["pos"]) < 150 for q in npcs)
                    if 0 < now - old_t < 0.6 and drop >= 45 and not ours and in_zone and not npc_near:
                        self.ffa_signals += 1
                self.hp_seen[ident] = (hp, now)
            self.hp_seen = {k: v for k, v in self.hp_seen.items() if now - v[1] <= 10}
            for beam in obs.get("beams", ()):
                path = beam.get("path") or ()
                if not path or beam.get("by") == me.get("id"):
                    continue
                endpoint = path[-1]
                if math.dist(endpoint, me["pos"]) < 9:
                    continue
                if any(m.get("kind") == "mech" and m["id"] not in (beam.get("by"), me.get("id"))
                       and math.dist(m["pos"], endpoint) < 5.5 for m in obs.get("mechs", ())):
                    self.ffa_signals += 1
            self.passive = self.ffa_signals >= 3

        self.gear = None
        self.gear_slot = None
        if self.features["GEAR"]:
            self.gear = self.choose_gear(obs, tracks)
            if self.gear is not None:
                self.gear_slot = self.replacement_slot(obs["self"], self.gear["kind"])

    def recent_attackers(self, now):
        return {ident for ident, at in self.damage_from if now - at < 3.0}

    def passive_allow(self, obs, track):
        now = obs["t"]
        d = track.distance
        actor = track.actor
        if actor.get("id") in self.recent_attackers(now) or d < 60:
            return True
        if not self.passive:
            return now >= 75 or d < 260
        me = obs["self"]
        weak = actor.get("hp", math.inf) <= 0.30 * actor.get("max_hp", math.inf)
        others = any(m.get("kind") == "mech" and m["id"] != actor.get("id")
                     and math.dist(m["pos"], actor["pos"]) < 150 for m in obs.get("mechs", ()))
        return weak and me["hp"] >= 0.7 * me["max_hp"] and not others and d < 150

    def squad_retreat(self, obs, tracks):
        if not self.features["SQUAD"]:
            return False
        me = obs["self"]
        if me["hp"] >= 0.65 * me["max_hp"]:
            return False
        attackers = self.recent_attackers(obs["t"])
        shooter_tracks = [t for t in tracks if t.actor["id"] in attackers]
        if len(shooter_tracks) >= 2:
            return True
        # A visible, charging laser aimed at us counts as a current shooter even
        # before its damage event arrives. Existing V13 escape remains in control.
        for track in tracks:
            if (track.actor.get("charging") and track.actor["id"] in attackers
                    and track.distance < 220):
                return True
        return False

    def choose_gear(self, obs, tracks):
        me, zone, now = obs["self"], obs["zone"], obs["t"]
        if (now > 120 or not me.get("grounded") or not me.get("in_zone")
                or zone.get("shrink_end", now) - now < 5
                or now - self.bot.last_damage < 3
                or obs.get("projectiles")
                or any(s.get("dist") == "near" and s.get("kind") != "drop" for s in obs.get("sounds", ()))):
            return None
        if any(t.distance < 150 for t in tracks):
            return None
        held = [w for w in me.get("weapons", ()) if w]
        if any(w["kind"] == "laser" for w in held):
            return None
        priorities = {"laser": 12.0, "lightning": 3.0}
        candidates = []
        speed = max(1, me["stats"]["walk_speed"])
        for item in obs.get("pickups", ()):
            kind = item.get("kind")
            if kind not in priorities:
                continue
            slot = self.replacement_slot(me, kind)
            if slot is False:
                continue
            point = item["pos"]
            d = math.dist(me["pos"], point)
            if d > 300 or abs(point[2] - me["pos"][2]) > 8:
                continue
            current_margin = zone["radius"] - math.dist(point[:2], zone["center"])
            if current_margin < 8:
                continue
            arrival = d / speed * 1.45 + 1.0
            future_margin = self._arrival_margin(zone, point, now, arrival)
            if future_margin < 6:
                continue
            # The preference difference is a modest tie-break, so a nearby
            # lightning pickup can beat a distant laser without making laser moot.
            score = d - 5 * priorities[kind]
            if kind == "laser" and not any(w["kind"] == "laser" for w in held):
                score -= 35
            candidates.append((score, item, slot))
        if not candidates:
            return None
        _, item, slot = min(candidates, key=lambda x: x[0])
        return item

    @staticmethod
    def _arrival_margin(zone, point, now, seconds):
        start, end = zone.get("shrink_start", now), zone.get("shrink_end", now)
        if end <= start or now + seconds <= start:
            return zone["next_radius"] - math.dist(point[:2], zone["next_center"])
        f = min(1.0, max(0.0, (now + seconds - start) / max(0.001, end - start)))
        center = [a + (b - a) * f for a, b in zip(zone["center"], zone["next_center"])]
        radius = zone["radius"] + f * (zone["next_radius"] - zone["radius"])
        return radius - math.dist(point[:2], center)

    @staticmethod
    def replacement_slot(me, kind):
        held = me.get("weapons", ())
        if any(w and w["kind"] == kind for w in held):
            return False
        for i, weapon in enumerate(held):
            if weapon is None:
                return i
        best = None
        for i, weapon in enumerate(held):
            if weapon["kind"] == "laser" and weapon.get("charge", 0) > 0:
                continue
            value = WEAPON_VALUE.get(weapon["kind"], -1)
            if best is None or value < best[0]:
                best = value, i
        if best is not None and best[0] + 0.5 < WEAPON_VALUE.get(kind, -1):
            return best[1]
        return False

    def recover_mode(self, obs, state, proposed, tracks):
        if not self.features["RECOVERY"]:
            return state, proposed
        me, now = obs["self"], obs["t"]
        threatened = any(not t.covered and (t.distance < 75 or t.closing > 6) for t in tracks)
        safe = me.get("in_zone") and not threatened and not obs.get("projectiles")
        if state == "RECOVER" and me["hp"] < 0.90 * me["max_hp"]:
            return ("RECOVER", "RECOVER") if safe else (proposed, proposed)
        if (safe and me["hp"] < 0.90 * me["max_hp"]
                and now - self.bot.last_damage >= me["stats"]["regen_delay"] * 0.45):
            return "RECOVER", "RECOVER"
        return state, proposed

    def spot_goal(self, obs, tracks, target):
        if not self.features["SPOT"] or not tracks or target is None:
            self.spot_point = None
            return None
        me, zone = obs["self"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        arena = self.bot.arena
        foes = sorted((t for t in tracks if t.distance < 260),
                      key=lambda t: pressure_weight(t, t.distance), reverse=True)[:4]
        if not foes:
            return None
        candidates = [(tuple(pos), 0.0)]
        rings = ((6, 8), (12, 8), (19, 8))
        for ri, (radius, count) in enumerate(rings):
            for k in range(count):
                angle = 2 * math.pi * k / count + ri * 0.31
                x, y = pos[0] + radius * math.cos(angle), pos[1] + radius * math.sin(angle)
                z = arena.ground(x, y)
                distance = math.hypot(x - pos[0], y - pos[1])
                if abs(z - pos[2]) > 5 or arena.edge(x, y) < stats["radius"] + 2:
                    continue
                walked, _ = arena.walk(pos, (x - pos[0]) / distance, (y - pos[1]) / distance, distance, stats)
                if walked + 0.2 < distance:
                    continue
                if zone["next_radius"] - math.dist((x, y), zone["next_center"]) < 8:
                    continue
                candidates.append(((x, y, z), distance))
        best_score, best = -math.inf, None
        target_point = target.actor["pos"]
        for q, travel in candidates:
            eye = (q[0], q[1], q[2] + stats["height"] * 0.5)
            exposure = 0.0
            hidden = 0
            laser_danger = 0
            for track in foes:
                actor = track.actor
                enemy_eye = (actor["pos"][0], actor["pos"][1], actor["pos"][2] + self.bot.config["mech"]["height"] * 0.8)
                visible = arena.clear(enemy_eye, eye, spacing=5)
                weight = pressure_weight(track, math.dist(q, actor["pos"]))
                if visible:
                    exposure += weight
                    laser_danger += bool(actor.get("charging") and "laser" in actor.get("weapons", ()))
                else:
                    hidden += 1
            target_center = (target_point[0], target_point[1], target_point[2] + stats["height"] * 0.5)
            attack_lane = arena.clear((q[0], q[1], q[2] + stats["muzzle_height"]), target_center, spacing=5)
            shot_bonus = 0.8 if attack_lane and math.dist(q, target_point) < 120 else 0
            score = 1.2 * hidden - 3.2 * exposure - 5.0 * laser_danger + shot_bonus - 0.09 * travel
            if self.spot_point and math.dist(q, self.spot_point) < 3 and obs["t"] - self.spot_time < 1.2:
                score += 0.55
            if score > best_score:
                best_score, best = score, q
        if best is None or math.dist(best, pos) < 2 or best_score < 0.15:
            self.spot_point = tuple(pos)
            self.spot_time = obs["t"]
            return None
        self.spot_point = best
        self.spot_time = obs["t"]
        return best
