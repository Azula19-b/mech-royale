"""Phoenix survival controller, developed from the official Python example."""
import math

import mechfight as mf
from arena import Arena
from combat import intercept


V6A_ENABLED = True
EARLY_PHASE_S = 120
ISOLATED_RANGE_M = 90
RECENT_ENEMY_RANGE_M = 150
RECENT_ENEMY_S = 3
ENGAGE_MIN_HP_FRAC = 0.6
UNKNOWN_ENEMY_MIN_HP_FRAC = 0.8
MIN_ENGAGE_S = 2
COMBAT_MAX_ZONE_URGENCY = 0.35
DISENGAGE_HP_FRAC = 0.4
DISENGAGE_ENERGY_FRAC = 0.25
ESCAPE_PROBE_M = 10
ESCAPE_SIDE_ANGLE = math.pi / 3
MIN_ESCAPE_ROUTES = 2
FINISH_HP_FRAC = 0.35
FINISH_MAX_S = 3
FINISH_MAX_M = 25


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
        if V6A_ENABLED:
            self.recent_enemies = {}
            self.engaged_id = None
            self.engaged_since = -math.inf
            self.finish_id = None
            self.finish_start = None
            self.finish_spent = False

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

    def engagement_target(self, obs, enemies, urgency):
        me, now = obs["self"], obs["t"]
        if now >= EARLY_PHASE_S:
            self.engaged_id = None
            return None

        nearby = []
        self.recent_enemies = {
            identifier: seen for identifier, seen in self.recent_enemies.items()
            if now - seen <= RECENT_ENEMY_S
        }
        for enemy in enemies:
            distance = math.dist(me["pos"], enemy["pos"])
            if distance <= RECENT_ENEMY_RANGE_M:
                # The protocol does not distinguish live sight from built-in memory.
                self.recent_enemies[enemy["id"]] = now
            if distance <= ISOLATED_RANGE_M:
                nearby.append((enemy, distance))

        if (len(nearby) != 1 or len(self.recent_enemies) != 1
                or not me["in_zone"] or urgency >= COMBAT_MAX_ZONE_URGENCY
                or me["hp"] < DISENGAGE_HP_FRAC * me["max_hp"]
                or me["energy"] < DISENGAGE_ENERGY_FRAC * me["max_energy"]):
            self.engaged_id = None
            return None

        enemy, distance = nearby[0]
        escapes = self.escape_routes(me, enemy)
        if len(escapes) < MIN_ESCAPE_ROUTES:
            self.engaged_id = None
            return None
        if self.engaged_id == enemy["id"] and now - self.engaged_since < MIN_ENGAGE_S:
            return enemy
        if not self.can_engage(me, enemy, distance):
            self.engaged_id = None
            return None
        if self.engaged_id != enemy["id"]:
            self.engaged_id = enemy["id"]
            self.engaged_since = now
            if self.finish_id != enemy["id"]:
                self.finish_id = enemy["id"]
                self.finish_start = None
                self.finish_spent = False
        return enemy

    def escape_routes(self, me, enemy):
        pos, stats = me["pos"], me["stats"]
        away = bearing(enemy["pos"], pos)
        routes = []
        for offset in (-ESCAPE_SIDE_ANGLE, 0, ESCAPE_SIDE_ANGLE):
            dx, dy = direction(away + offset)
            distance, height = self.arena.walk(pos, dx, dy, ESCAPE_PROBE_M, stats)
            end = (pos[0] + dx * distance, pos[1] + dy * distance, height)
            if distance >= ESCAPE_PROBE_M and self.arena.edge(*end[:2]) >= stats["radius"]:
                routes.append(end)
        return routes

    def can_engage(self, me, enemy, distance):
        hp = enemy.get("hp")
        minimum = UNKNOWN_ENEMY_MIN_HP_FRAC if hp is None else ENGAGE_MIN_HP_FRAC
        if me["hp"] < minimum * me["max_hp"] or (hp is not None and me["hp"] < hp):
            return False

        # Use boost speed as a conservative ground approach estimate.
        contact = max(0, distance - 2 * me["stats"]["radius"])
        reach_time = contact / max(self.config["mech"]["boost_speed"], math.hypot(*enemy["vel"]))
        return any(weapon and weapon["kind"] == "gun"
                   and (weapon.get("ammo", 0) > 0 or 0 < weapon.get("reload", 0) < reach_time)
                   for weapon in me["weapons"])

    def finish_move(self, obs, enemy):
        if self.finish_spent:
            return None
        me, now = obs["self"], obs["t"]
        pos, stats = me["pos"], me["stats"]
        hp, maximum = enemy.get("hp"), enemy.get("max_hp")
        distance = math.dist(pos, enemy["pos"])
        yaw = bearing(pos, enemy["pos"])
        move = direction(yaw)
        step = max(stats["walk_speed"], math.hypot(*me["vel"][:2])) * self.period
        started, origin = self.finish_start if self.finish_start else (now, pos[:2])
        if (hp is not None and maximum is not None and hp < FINISH_HP_FRAC * maximum
                and self.can_engage(me, enemy, distance) and me["grounded"]
                and distance > 2 * stats["radius"] + step
                and now + self.period - started <= FINISH_MAX_S
                and math.dist(origin, pos[:2]) + step <= FINISH_MAX_M
                and self.arena.walk(pos, *move, step, stats)[0] >= step
                and self.exposed(me, [enemy], pos)
                and any(not self.exposed(me, [enemy], end) for end in self.escape_routes(me, enemy))):
            self.finish_start = started, origin
            self.heading = yaw
            return list(move), False
        if self.finish_start:
            self.finish_spent = True
        return None

    def movement(self, obs, enemies, loot_goal=None, zone_goal=None):
        me = obs["self"]
        goal_yaw, urgency = self.zone_goal(obs) if zone_goal is None else zone_goal
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
        yaw = wrap(max(options, key=lambda option: self.route_score(obs, option, goal_yaw, threats, urgency)))
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

    def defense(self, obs, enemies, armor, act, engagement=None):
        me = obs["self"]
        if not enemies:
            return
        target = engagement if engagement is not None else enemies[0]
        distance = math.dist(me["pos"], target["pos"])
        wounded = me["hp"] < 0.85 * me["max_hp"]
        recovering = obs["t"] - self.last_damage >= me["stats"]["regen_delay"] * 0.55
        dx, dy = act["move"]
        x, y, z = me["pos"]
        behind_cover_soon = wounded and not self.exposed(me, enemies, (x + 10 * dx, y + 10 * dy, z))
        if engagement is None and (distance > 105 or (
                wounded and (recovering or behind_cover_soon) and distance > 32)):
            return
        choices = []
        for slot, weapon in enumerate(me["weapons"]):
            if weapon is None:
                continue
            kind, w = weapon["kind"], weapon["stats"]
            if kind in ("gun", "mortar"):
                tracking_gun = engagement is not None and kind == "gun"
                if not tracking_gun and (weapon["cooldown"] > 0.35 or weapon.get("reload", 0)):
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
        zone_goal = self.zone_goal(obs) if V6A_ENABLED and obs["t"] < EARLY_PHASE_S else None
        engagement = self.engagement_target(obs, enemies, zone_goal[1]) if zone_goal else None
        pursuit = self.finish_move(obs, engagement) if engagement is not None else None
        if V6A_ENABLED and engagement is None and self.finish_start is not None:
            self.finish_spent = True
        move, boost = pursuit or self.movement(obs, enemies, self.loot_goal(obs, enemies), zone_goal)
        armor_threats = [engagement] if engagement is not None else enemies
        yaw = self.armor_yaw(obs, armor_threats, math.atan2(move[1], move[0]))
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
        self.defense(obs, enemies, yaw, act, engagement)
        return act


if __name__ == "__main__":
    mf.run(Bot)
