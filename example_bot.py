"""Phoenix survival controller, developed from the official Python example."""
import math

import mechfight as mf
from arena import Arena
from combat import intercept


AUDIO_THREAT_ENABLED = True
AUDIO_LIFETIME = 1.2  # Match seconds, long enough to hear successive footsteps.
AUDIO_HISTORY_LIMIT = 12
AUDIO_MAX_PENALTY = 0.24  # Route-score units, smaller than a single cover-side penalty.
AUDIO_MAX_DIVERSION = 0.55  # Radians, matching the existing per-decision turn limit.
AUDIO_SWITCH_MARGIN = 0.02  # Require a clear improvement beyond V5's turn cost.
AUDIO_KIND_WEIGHT = {
    "gun": 0.09, "laser": 0.09, "lightning": 0.09, "mortar": 0.07,
    "laser_charge": 0.055, "boost": 0.065, "jet": 0.05, "step": 0.045, "reload": 0.04,
}
AUDIO_BAND_WEIGHT = {"near": 1.0, "mid": 0.40, "far": 0.10}


def wrap(yaw):
    return (yaw + math.pi) % math.tau - math.pi


def bearing(origin, target):
    return math.atan2(target[1] - origin[1], target[0] - origin[0])


def direction(yaw):
    return math.cos(yaw), math.sin(yaw)


class Bot:
    name = "phoenix"

    def __init__(self, hello: mf.Hello):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"])
        self.arena.build_navigation(self.config["mech"])
        self.waypoint = None
        self.waypoint_until = -1
        self.reach = self.config["loot"]["pickup_radius"]
        self.period = self.config["match"]["decide_every"] / self.config["match"]["tick_hz"]
        self.heading = None
        self.attack_bearing = None
        self.attack_until = -1
        self.last_damage = -math.inf
        self.audio = []

    def threat_values(self, me, enemy):
        dx, dy = (b - a for a, b in zip(me["pos"][:2], enemy["pos"][:2]))
        dvx, dvy = (b - a for a, b in zip(me["vel"][:2], enemy["vel"][:2]))
        distance = max(1, math.hypot(dx, dy))
        closing = max(0, -(dx * dvx + dy * dvy) / distance)
        weapons = [self.config["weapons"][kind] for kind in enemy.get("weapons", ())] or [self.config["weapons"]["gun"]]
        reach = max(w.get("range", w.get("speed", 0) ** 2 / max(1, w.get("gravity", 1))) for w in weapons)
        damage = max(w["damage"] / max(0.25, w["cooldown"]) for w in weapons)
        facing = abs(wrap(enemy["aim"][0] - math.atan2(-dy, -dx)))
        weight = (damage / 185) * (1 + closing / 25) * (1 if facing < 0.7 else 0.7)
        return reach, weight

    def threats(self, obs):
        me = obs["self"]
        actors = (m for m in obs["mechs"] if m["kind"] == "mech")
        ranked = []
        for actor in actors:
            reach, weight = self.threat_values(me, actor)
            distance = math.dist(me["pos"], actor["pos"])
            ranked.append((weight / (20 + distance) * min(1, reach / max(1, distance)), actor))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [actor for _, actor in ranked]

    def armor_yaw(self, obs, enemies, default):
        hits = [e for e in obs["events"] if e["type"] == "damaged" and e["bearing"] is not None]
        if hits:
            self.attack_bearing = max(hits, key=lambda e: e["amount"])["bearing"]
            self.attack_until = obs["t"] + 0.5
        if obs["t"] <= self.attack_until:
            return self.attack_bearing
        if enemies:
            return bearing(obs["self"]["pos"], enemies[0]["pos"])
        return default

    def exposed(self, me, enemies, point):
        eye = (*point[:2], point[2] + me["stats"]["eye_height"])
        return any(
            math.dist(point, enemy["pos"]) < 230
            and self.arena.clear(eye, (*enemy["pos"][:2], enemy["pos"][2] + 7))
            for enemy in enemies[:5]
        )

    def zone_goal(self, obs):
        me, zone = obs["self"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        center = zone["next_center"]
        distance = math.dist(pos[:2], center)
        target_radius = max(0, zone["next_radius"] - min(15, zone["next_radius"] * 0.4))
        remaining = max(0, distance - target_radius)
        eta = remaining / max(1, stats["walk_speed"] * 0.62) + 3
        deadline = zone["shrink_end"] - obs["t"]
        margin = zone["radius"] - math.dist(pos[:2], zone["center"])
        urgency = max(0, min(1, (eta + 12 - deadline) / 12), min(1, (24 - margin) / 24))
        yaw = bearing(pos, center)
        if urgency > 0 and remaining > 4:
            check = min(22, remaining)
            if self.arena.walk(pos, *direction(yaw), check, stats)[0] < check:
                if (self.waypoint is None or obs["t"] >= self.waypoint_until
                        or math.dist(pos[:2], self.waypoint) < 4):
                    self.waypoint = self.arena.waypoint(pos, center, target_radius, stats)
                    self.waypoint_until = obs["t"] + 0.8
                yaw = bearing(pos, self.waypoint)
        return yaw, urgency

    def route_score(self, obs, yaw, goal_yaw, threats, urgency):
        me = obs["self"]
        x, y = me["pos"][:2]
        dx, dy = direction(yaw)
        immediate, _ = self.arena.walk(me["pos"], dx, dy, 1.5, me["stats"])
        if immediate == 0:
            return -1000
        travel, height = self.arena.walk(me["pos"], dx, dy, 24, me["stats"])
        px, py = x + travel * dx, y + travel * dy
        zone = obs["zone"]
        outside = math.dist((px, py), zone["center"]) - zone["radius"]
        future = math.dist((px, py), zone["next_center"]) - zone["next_radius"]
        turn = 0 if self.heading is None else abs(wrap(yaw - self.heading))
        edge = self.arena.edge(px, py)
        score = ((0.35 + urgency * 4) * math.cos(yaw - goal_yaw) - 0.22 * turn
                 - max(0, 16 - edge) * 0.15 - max(0, outside + 5) * 0.30
                 - urgency * max(0, future) * 0.035 - max(0, 21 - travel) * 0.47)
        for side in (-1, 1):
            if self.arena.solid(px - side * dy * 8, py + side * dx * 8,
                                height + 1.5, height + me["stats"]["height"]):
                score -= 0.35
        pressure = 0
        nearest = 0
        cover_checks = 0
        for enemy, reach, weight in threats:
            ex, ey, ez = enemy["pos"]
            vx, vy, vz = enemy["vel"]
            distance = math.dist((ex + vx * 1.5, ey + vy * 1.5, ez + vz * 1.5), (px, py, height))
            risk = weight * (95 / (distance + 20)) ** 2 * max(0.08, 1 - distance / (reach + 25))
            if distance < reach and cover_checks < 3 and risk > 0.15:
                cover_checks += 1
                if not self.arena.clear((px, py, height + me["stats"]["eye_height"]), (ex, ey, ez + 7), spacing=7):
                    risk *= 0.16 if me["hp"] < 0.85 * me["max_hp"] else 0.32
            pressure += risk
            nearest = max(nearest, risk)
        return score - 0.8 * pressure - 0.65 * nearest

    def remember_audio(self, obs, enemies):
        now = obs["t"]
        self.audio = [cue for cue in self.audio if now - cue[0] < AUDIO_LIFETIME]
        if not obs["sounds"]:
            return
        config = self.config["sound"]
        # Two standard deviations, with room for movement since the sound.
        tolerance = max(math.pi / 12, math.radians(2 * config["bearing_noise_deg"]))
        pos = obs["self"]["pos"]
        known = []
        for enemy in enemies:
            distance = math.dist(pos, enemy["pos"])
            band = "near" if distance < config["near"] else "mid" if distance < config["mid"] else "far"
            known.append((bearing(pos, enemy["pos"]), band))
        for sound in obs["sounds"]:
            kind, band = sound["kind"], sound["dist"]
            if kind not in AUDIO_KIND_WEIGHT or sound["age"] >= AUDIO_LIFETIME:
                continue
            occurred = now - sound["age"]
            yaw = wrap(sound["bearing"])
            if any(band == known_band and abs(wrap(yaw - angle)) <= tolerance
                   for angle, known_band in known):
                continue
            # Reports carry no event ID. Prefer merging ambiguous near-simultaneous cues.
            if any(kind == old_kind and abs(occurred - when) < self.period / 2
                   and abs(wrap(yaw - angle)) <= tolerance
                   for when, angle, old_kind, _ in self.audio):
                continue
            cue = (occurred, yaw, kind, band)
            if len(self.audio) < AUDIO_HISTORY_LIMIT:
                self.audio.append(cue)
            else:
                oldest = min(range(len(self.audio)), key=lambda index: self.audio[index][0])
                if occurred > self.audio[oldest][0]:
                    self.audio[oldest] = cue

    def audio_penalty(self, yaw, now):
        # Linear decay in time and across a 60-degree sector on either side.
        risk = sum(AUDIO_KIND_WEIGHT[kind] * AUDIO_BAND_WEIGHT[band]
                   * max(0, 1 - (now - occurred) / AUDIO_LIFETIME)
                   * max(0, 1 - abs(wrap(yaw - angle)) / (math.pi / 3))
                   for occurred, angle, kind, band in self.audio)
        return min(AUDIO_MAX_PENALTY, risk)

    def audio_heading(self, obs, routes, baseline):
        now = obs["t"]
        yaw, score = baseline
        best = score - self.audio_penalty(yaw, now) + AUDIO_SWITCH_MARGIN
        chosen = yaw
        me, zone = obs["self"], obs["zone"]
        for option, base_score in routes:
            if base_score <= best or option == yaw or abs(wrap(option - yaw)) > AUDIO_MAX_DIVERSION:
                continue
            # Let V5 smooth its original choice. Audio alternatives must already
            # fit within that turn limit so their evaluated path is the one used.
            if self.heading is not None and abs(wrap(option - self.heading)) > AUDIO_MAX_DIVERSION:
                continue
            adjusted = base_score - self.audio_penalty(option, now)
            if adjusted <= best:
                continue
            dx, dy = direction(option)
            x, y = me["pos"][:2]
            endpoint = x + 24 * dx, y + 24 * dy
            if (self.arena.edge(*endpoint) < 16
                    or math.dist(endpoint, zone["center"]) > zone["radius"] - 5
                    or math.dist(endpoint, zone["next_center"]) > zone["next_radius"] - 5
                    or self.arena.walk(me["pos"], dx, dy, 1.5, me["stats"])[0] == 0
                    or self.arena.walk(me["pos"], dx, dy, 24, me["stats"])[0] < 24):
                continue
            best, chosen = adjusted, option
        return chosen

    def movement(self, obs, enemies, loot_goal=None):
        me = obs["self"]
        goal_yaw, urgency = self.zone_goal(obs)
        if loot_goal is not None and urgency < 0.35:
            if math.dist(me["pos"][:2], loot_goal) < 2:
                return [0.0, 0.0], False
            goal_yaw, urgency = bearing(me["pos"], loot_goal), 0.55
        threats = [(enemy, *self.threat_values(me, enemy)) for enemy in enemies]
        reference = goal_yaw if self.heading is None else self.heading
        options = [reference + i * math.pi / 6 for i in range(12)]
        options.append(goal_yaw)
        if enemies:
            away = wrap(bearing(me["pos"], enemies[0]["pos"]) + math.pi)
            options.extend((away - 0.55, away + 0.55))
        routes = [(option, self.route_score(obs, option, goal_yaw, threats, urgency))
                  for option in options]
        baseline = max(routes, key=lambda route: route[1])
        yaw = baseline[0]
        if (AUDIO_THREAT_ENABLED and self.audio and not enemies and loot_goal is None
                and me["in_zone"] and urgency == 0 and not obs.get("projectiles")
                and not obs.get("beams") and obs["t"] - self.last_damage >= AUDIO_LIFETIME):
            yaw = self.audio_heading(obs, routes, baseline)
        yaw = wrap(yaw)
        if self.heading is not None:
            delta = wrap(yaw - self.heading)
            smooth = wrap(self.heading + max(-0.55, min(0.55, delta)))
            if self.arena.walk(me["pos"], *direction(smooth), 1.5, me["stats"])[0] > 0 and self.arena.walk(me["pos"], *direction(smooth), 6, me["stats"])[0] >= 3:
                yaw = smooth
        self.heading = yaw
        move = direction(yaw)
        pos = me["pos"]
        threatened = self.exposed(me, enemies, pos)
        nearest = min((math.dist(pos, enemy["pos"]) for enemy in enemies), default=math.inf)
        zone = obs["zone"]
        deep_inside = math.dist(pos[:2], zone["next_center"]) < zone["next_radius"] - 12
        healing = me["hp"] < 0.96 * me["max_hp"]
        if deep_inside and healing and not threatened and nearest > 35 and enemies:
            # Stay in established cover while moving would restore an opponent's line of fire.
            ahead = (pos[0] + move[0] * 7, pos[1] + move[1] * 7, pos[2])
            if self.exposed(me, enemies, ahead):
                return [0.0, 0.0], False
        escape = threatened and nearest < 58 and me["energy"] > 0.47 * me["max_energy"]
        emergency = not me["in_zone"] or (urgency > 0.9 and not deep_inside)
        viable = self.arena.walk(pos, *move, 14, me["stats"])[0] >= 12
        return list(move), me["grounded"] and viable and me["energy"] > 1 and (emergency or escape)

    def projectile_shot(self, me, target, weapon, armor, gravity=0):
        stats = me["stats"]
        offset = stats["radius"] + 0.5
        height = stats["height"] + 0.5 if gravity else stats["muzzle_height"]
        origin = (*me["pos"][:2], me["pos"][2] + height)
        target_height = self.config["mech"]["height"] * (1 + self.config["cubes"]["size_per_cube"] * target.get("cubes", 0))
        position = (*target["pos"][:2], target["pos"][2] + target_height * 0.53)
        w = weapon["stats"]
        solution = intercept(origin, position, target["vel"], w["speed"], offset, gravity)
        if solution is None:
            return None
        aim, flight, point = solution
        yaw, pitch = aim
        if (flight * w["speed"] > w.get("range", w.get("ttl", 0) * w["speed"])
                or not stats["min_pitch"] <= pitch <= stats["max_pitch"]
                or abs(wrap(yaw - armor)) > math.radians(stats["front_arc_deg"]) * 0.35):
            return None
        tolerance = min(0.16, math.atan2(self.config["mech"]["radius"] * 0.65, max(1, flight * w["speed"])))
        yaw_error = abs(wrap(yaw - me["aim"][0]))
        pitch_error = abs(pitch - me["aim"][1])
        # A ready weapon can fire on the first sim step, before the full decision's slew.
        first_step = 1 / self.config["match"]["tick_hz"]
        aligned = (yaw_error < stats["turn_rate"] * first_step * 0.75 + tolerance
                   and pitch_error < stats["pitch_rate"] * first_step * 0.75 + tolerance)
        ready = weapon["cooldown"] < self.period and not weapon.get("reload", 0) and weapon.get("ammo", 1) > 0
        if gravity:
            gap = math.dist(origin, point)
            if gap < w["splash_radius"] + 12:
                return None
            cp = math.cos(pitch)
            ray = (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))
            previous = origin
            count = max(3, math.ceil(flight * 10))
            for i in range(1, count + 1):
                t = flight * i / count
                distance = offset + w["speed"] * t
                sample = tuple(o + r * distance for o, r in zip(origin, ray))
                sample = (*sample[:2], sample[2] - gravity * t * t / 2)
                if not self.arena.clear(previous, sample, spacing=2):
                    return None
                previous = sample
        elif not self.arena.clear(origin, point, spacing=2):
            return None
        return aim, aligned and ready

    def hitscan(self, me, target, weapon, armor):
        w = weapon["stats"]
        stats = me["stats"]
        origin = (*me["pos"][:2], me["pos"][2] + stats["muzzle_height"])
        height = self.config["mech"]["height"] * (1 + target.get("cubes", 0) * self.config["cubes"]["size_per_cube"])
        point = (*target["pos"][:2], target["pos"][2] + height / 2)
        dx, dy, dz = (b - a for a, b in zip(origin, point))
        distance = math.hypot(dx, dy, dz)
        aim = math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))
        if (distance > w["range"] or abs(wrap(aim[0] - armor)) > math.radians(stats["front_arc_deg"]) * 0.35
                or not stats["min_pitch"] <= aim[1] <= stats["max_pitch"]
                or not self.arena.clear(origin, point, spacing=2)):
            return None
        error = abs(wrap(aim[0] - me["aim"][0]))
        if weapon["kind"] == "lightning":
            return aim, error <= math.radians(w["cone_deg"]) * 0.40 and me["energy"] >= w["energy"] + 15
        tolerance = math.atan2(self.config["mech"]["radius"] * 0.7, max(1, distance))
        return aim, error < tolerance and abs(aim[1] - me["aim"][1]) < tolerance

    def defense(self, obs, enemies, armor, act):
        me = obs["self"]
        if not enemies:
            return
        target = enemies[0]
        distance = math.dist(me["pos"], target["pos"])
        wounded = me["hp"] < 0.85 * me["max_hp"]
        recovering = obs["t"] - self.last_damage >= me["stats"]["regen_delay"] * 0.55
        dx, dy = act["move"]
        x, y, z = me["pos"]
        behind_cover_soon = wounded and not self.exposed(me, enemies, (x + 10 * dx, y + 10 * dy, z))
        if distance > 105 or (wounded and (recovering or behind_cover_soon) and distance > 32):
            return
        choices = []
        for slot, weapon in enumerate(me["weapons"]):
            if weapon is None:
                continue
            kind, w = weapon["kind"], weapon["stats"]
            if kind in ("gun", "mortar"):
                if weapon["cooldown"] > 0.35 or weapon.get("reload", 0):
                    continue
                shot = self.projectile_shot(me, target, weapon, armor, w.get("gravity", 0))
                if shot:
                    aim, trigger = shot
                    choices.append((2 + 4 * trigger if kind == "gun" else 1 + 7 * trigger, slot, aim, trigger))
                continue
            if weapon["cooldown"] >= self.period:
                continue
            shot = self.hitscan(me, target, weapon, armor)
            if not shot:
                continue
            aim, aligned = shot
            if kind == "lightning":
                choices.append((3 + 6 * aligned, slot, aim, aligned))
            elif weapon.get("charge", 0) > 0:
                charge = weapon["charge"]
                # Limit the period of half-speed movement under fire.
                hold = charge < w["min_charge"] or (not aligned and charge < min(w["charge"], 0.35))
                choices.append((12, slot, aim, hold))
            elif aligned and not act["boost"] and (distance < 36 or target.get("hp", math.inf) <= w["damage"] * w["min_frac"]):
                choices.append((4, slot, aim, True))
        if choices:
            _, slot, aim, trigger = max(choices)
            act["aim"] = list(aim)
            act["fire"] = [False, False]
            act["fire"][slot] = trigger

    def loot_goal(self, obs, enemies):
        me, zone = obs["self"], obs["zone"]
        if (not me["grounded"] or not me["in_zone"]
                or any(math.dist(me["pos"], e["pos"]) < 105 for e in enemies)
                or any(s["dist"] == "near" and s["kind"] != "drop" for s in obs["sounds"])
                or obs["t"] - self.last_damage < 2):
            return None
        candidates = [
            (d["pos"], 18) for d in obs["drops"] if d["lands_in"] == 0 and me["hp"] < 0.8 * me["max_hp"]
            and d["holder"] in (None, me.get("id")) and abs(d["pos"][2] - me["pos"][2]) <= 2
        ]
        if me["cubes"] < 3:
            candidates.extend((c["pos"], 9) for c in obs["cubes"])
        for point, limit in sorted(candidates, key=lambda item: math.dist(me["pos"], item[0])):
            distance = math.dist(me["pos"][:2], point[:2])
            if (distance <= limit and abs(point[2] - me["pos"][2]) <= 2
                    and math.dist(point[:2], zone["next_center"]) < zone["next_radius"] - 8
                    and self.arena.walk(me["pos"], *direction(bearing(me["pos"], point)), distance, me["stats"])[0] == distance):
                return point[:2]
        return None

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = me["pos"]
        if any(event["type"] in ("damaged", "hit") for event in obs["events"]):
            self.last_damage = obs["t"]
        enemies = self.threats(obs)
        if AUDIO_THREAT_ENABLED:
            self.remember_audio(obs, enemies)
        move, boost = self.movement(obs, enemies, self.loot_goal(obs, enemies))
        yaw = self.armor_yaw(obs, enemies, math.atan2(move[1], move[0]))
        act = {"tick": obs["tick"], "move": move, "boost": boost, "aim": [yaw, 0.0]}
        if obs["offer"]:
            priority = ("servos", "nanites", "armor", "plating", "reactor", "jets", "capacitor", "gyros", "sensors")
            ranks = {name: rank for rank, name in enumerate(priority)}
            choices = obs["offer"]["choices"]
            act["pick"] = min(range(len(choices)), key=lambda i: ranks.get(choices[i]["id"], len(priority)))
        if None in me["weapons"]:
            held = {weapon["kind"] for weapon in me["weapons"] if weapon}
            preference = {"lightning": 0, "mortar": 1, "laser": 2, "gun": 3}
            for item in sorted(obs["pickups"], key=lambda p: preference[p["kind"]]):
                px, py, pz = item["pos"]
                if math.hypot(px - x, py - y) <= self.reach and abs(pz - z) <= 4 and item["kind"] not in held:
                    act["take"] = item["id"]
                    break
        self.defense(obs, enemies, yaw, act)
        return act


if __name__ == "__main__":
    mf.run(Bot)
