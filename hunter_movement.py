"""Short-horizon route comparison using the existing arena geometry."""
import math

from intelligence import angle_error, bounded, pressure_weight


class Movement:
    horizons = (0.3, 0.7, 1.5)

    def __init__(self, bot):
        self.bot = bot
        self.escape_committed = False

    def projectiles(self, obs):
        me = obs["self"]
        stats = me["stats"]
        nearby = []
        for shot in obs["projectiles"]:
            speed = math.hypot(*shot["vel"])
            distance = math.dist(shot["pos"], me["pos"])
            if distance <= (speed + stats["boost_speed"]) * 0.65 + stats["height"]:
                nearby.append((distance, shot))
        paths = []
        for _, shot in sorted(nearby, key=lambda item: item[0])[:10]:
            kind = shot["kind"]
            radius = stats["radius"] + self.bot.config["projectile"].get(kind + "_radius", 0)
            position, velocity = shot["pos"], shot["vel"]
            gravity = self.bot.config["weapons"]["mortar"]["gravity"] if kind == "mortar" else 0
            reach = (stats["boost_speed"] + math.hypot(*me["vel"][:2])) * 0.6 + radius + 1
            dx, dy = (a - b for a, b in zip(position[:2], me["pos"][:2]))
            vx, vy = velocity[:2]
            closest = max(0, min(0.6, -(dx * vx + dy * vy) / max(1e-9, vx * vx + vy * vy)))
            if not gravity and math.hypot(dx + vx * closest, dy + vy * closest) > reach:
                continue
            previous = position
            for i in range(1, 7):
                t = i * 0.1
                x, y, z = (p + v * t for p, v in zip(position, velocity))
                point = x, y, z - gravity * t * t / 2
                if not self.bot.arena.clear(previous, point, spacing=2):
                    if gravity:
                        splash = self.bot.config["weapons"]["mortar"]["splash_radius"]
                        paths.append((t, previous, previous, radius + splash, splash))
                    break
                paths.append((t, previous, point, radius, 0))
                previous = point
        return paths

    @staticmethod
    def impact(me, yaw, speed, paths):
        px, py, pz = me["pos"]
        vx, vy = me["vel"][:2]
        stats = me["stats"]
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        tx, ty = math.cos(yaw) * speed, math.sin(yaw) * speed
        risk = 0
        for time, start, end, radius, splash in paths:
            mid = time - 0.05
            ax, ay = bounded((tx - vx, ty - vy), accel * mid)
            x, y = px + (vx + ax / 2) * mid, py + (vy + ay / 2) * mid
            dx, dy = end[0] - start[0], end[1] - start[1]
            fraction = max(0, min(1, ((x - start[0]) * dx + (y - start[1]) * dy)
                                      / max(1e-9, dx * dx + dy * dy)))
            z = start[2] + fraction * (end[2] - start[2])
            if pz - splash <= z <= pz + stats["height"] + splash:
                distance = math.hypot(x - start[0] - fraction * dx, y - start[1] - fraction * dy)
                risk = max(risk, max(0, 1 - distance / (radius + 0.6)) * (3 - 2 * time))
        return risk

    def score(self, obs, yaw, speed, goal, urgency, predictions, target, state, paths):
        bot, me = self.bot, obs["self"]
        arena, stats, zone = bot.arena, me["stats"], obs["zone"]
        x, y, z = me["pos"]
        vx, vy = me["vel"][:2]
        tx, ty = math.cos(yaw) * speed, math.sin(yaw) * speed
        dvx, dvy = tx - vx, ty - vy
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        settle = math.hypot(dvx, dvy) / max(1, accel)
        score = (0.24 + urgency * 4.8) * math.cos(yaw - goal)
        if bot.heading is not None:
            score -= 0.58 * angle_error(yaw, bot.heading)
        if arena.walk((x, y, z), math.cos(yaw), math.sin(yaw), 2.5, stats)[0] < 2.5:
            score -= 2.5
        step_vx, step_vy = bounded((dvx, dvy), accel * bot.period)
        step_x, step_y = (vx + step_vx) * bot.period, (vy + step_vy) * bot.period
        step = math.hypot(step_x, step_y)
        if step > 0.01 and arena.walk((x, y, z), step_x / step, step_y / step, step, stats)[0] < step:
            score -= 4
        for index, t in enumerate(self.horizons):
            ramp = min(t, settle)
            factor = (t - ramp / 2) * ramp / max(1e-6, settle)
            dx, dy = vx * t + dvx * factor, vy * t + dvy * factor
            wanted = math.hypot(dx, dy)
            if wanted > 0.01:
                distance, height = arena.walk((x, y, z), dx / wanted, dy / wanted, wanted, stats)
                point = (x + dx * distance / wanted, y + dy * distance / wanted, height)
                score -= (wanted - distance) * (0.55 if index == 0 else 0.26)
            else:
                point = (x, y, z)
            if zone["shrink_end"] > zone["shrink_start"]:
                fraction = (min(zone["shrink_end"], max(obs["t"] + t, zone["shrink_start"]))
                            - min(zone["shrink_end"], max(obs["t"], zone["shrink_start"])))
                fraction /= max(0.001, zone["shrink_end"] - max(obs["t"], zone["shrink_start"]))
            else:
                fraction = 0
            center = tuple(a + (b - a) * fraction for a, b in zip(zone["center"], zone["next_center"]))
            radius = zone["radius"] + (zone["next_radius"] - zone["radius"]) * fraction
            outside = math.dist(point[:2], center) - radius
            score -= 0.23 * max(0, outside + 3) + 0.065 * max(0, 13 - arena.edge(*point[:2]))
            if state == "EMERGENCY_ZONE":
                score -= 0.08 * max(0, math.dist(point[:2], zone["next_center"]) - zone["next_radius"])
            exposure = strongest = 0
            risks = [(pressure_weight(track, math.dist(point, samples[index])), track, samples[index])
                     for track, samples in predictions]
            if index == 2:
                risks.sort(key=lambda item: item[0], reverse=True)
            for rank, (risk, track, enemy) in enumerate(risks):
                if risk > 0.12:
                    muzzle = (*enemy[:2], enemy[2] + bot.config["mech"]["muzzle_height"])
                    body = (*point[:2], point[2] + stats["height"] / 2)
                    clear = (arena.clear(muzzle, body, spacing=5) if index == 2 and rank < 4 else not track.covered)
                    risk *= 1 if clear else 0.23
                    if angle_error(math.atan2(enemy[1] - point[1], enemy[0] - point[0]), bot.planned_armor) > math.radians(stats["front_arc_deg"]) / 2:
                        risk *= stats["rear_mult"]
                    exposure += risk
                    strongest = max(strongest, risk)
                if track is target:
                    gun_range = bot.config["weapons"]["gun"]["range"]
                    preferred = min(gun_range * 0.30, 62)
                    score -= 0.018 * abs(math.dist(point, enemy) - preferred)
                    if index == 2 and not track.covered and not clear_if_risk(arena, point, enemy, stats):
                        score -= 0.55
            score -= (exposure + 0.45 * max(0, exposure - strongest)) * (0.35 + index * 0.2)
        if target:
            yaw_to_target = math.atan2(target.actor["pos"][1] - y, target.actor["pos"][0] - x)
            score += 0.6 * abs(math.sin(yaw - yaw_to_target))
        return score - self.impact(me, yaw, speed, paths) * 4

    def escape_lanes(self, me, tracks, state):
        if state != "EVADE" or not self.bot.v13["ESCAPE"]:
            self.escape_committed = False
            return ()
        immediate = sum(not t.covered and t.distance < 78 for t in tracks)
        if immediate >= 3:
            self.escape_committed = True
        if not self.escape_committed:
            return ()
        candidates = [t for t in tracks if t.distance < min(120, t.reach + 10)]
        if len(candidates) < 2:
            return ()
        if len(candidates) == 2 and me["hp"] > me["max_hp"] * 0.7:
            return ()
        candidates.sort(key=lambda t: pressure_weight(t, t.distance) * (0.6 if t.covered else 1),
                        reverse=True)
        return tuple((t, t.predict(2.45)) for t in candidates[:5])

    def escape_cost(self, me, zone, yaw, speed, lanes):
        """Combat pressure at a reachable escape endpoint, in movement-score units."""
        arena, stats = self.bot.arena, me["stats"]
        dx, dy = math.cos(yaw), math.sin(yaw)
        vx, vy = me["vel"][:2]
        accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
        duration = 2.45
        ramp = min(duration, math.hypot(dx * speed - vx, dy * speed - vy) / max(accel, 1))
        vx = vx * ramp / 2 + dx * speed * (duration - ramp / 2)
        vy = vy * ramp / 2 + dy * speed * (duration - ramp / 2)
        wanted = math.hypot(vx, vy)
        if wanted < 0.01:
            return 2
        travel, z = arena.walk(me["pos"], vx / wanted, vy / wanted, wanted, stats)
        x = me["pos"][0] + vx * travel / wanted
        y = me["pos"][1] + vy * travel / wanted
        body = (x, y, z + stats["height"] / 2)
        muzzle = self.bot.config["mech"]["muzzle_height"]
        incoming = intercept = 0.0
        for track, (ex, ey, ez) in lanes:
            dist = math.hypot(ex - x, ey - y)
            pressure = pressure_weight(track, dist)
            if pressure > 0.08 and arena.clear((ex, ey, ez + muzzle), body, spacing=6):
                incoming += min(4, pressure)
                # A hunter near the exit can intercept the retreat before it opens distance.
                intercept += max(0, 1 - dist / 34)
        edge = arena.edge(x, y)
        exit_distance = arena.walk((x, y, z), dx, dy, 8, stats)[0]
        zone_gap = max(0, math.dist((x, y), zone["next_center"]) - zone["next_radius"])
        return (0.69 * incoming + 0.61 * intercept + 0.075 * (wanted - travel)
                + 0.12 * (8 - exit_distance) + 0.085 * max(0, 10 - edge) + 0.05 * zone_gap)

    def choose(self, obs, tracks, state, target, loot):
        bot, me = self.bot, obs["self"]
        stats = me["stats"]
        goal, urgency = bot.zone_goal(obs)
        paths = self.projectiles(obs)
        lanes = self.escape_lanes(me, tracks, state)
        if state in ("POSITION", "RECOVER") and not paths:
            relevant = any((not t.covered and t.distance < t.reach) or t.distance < 70 for t in tracks)
            if loot is not None or not relevant:
                return bot.baseline_movement(obs, [t.actor for t in tracks], loot)
        reference = goal if bot.heading is None else bot.heading
        options = [reference + i * math.tau / 12 for i in range(12)] + [goal]
        if target:
            yaw = math.atan2(target.actor["pos"][1] - me["pos"][1], target.actor["pos"][0] - me["pos"][0])
            options += [yaw - math.pi / 2, yaw + math.pi / 2]
        predictions = [(track, [track.predict(t) for t in self.horizons]) for track in tracks]
        charge = any(w and w.get("charge", 0) for w in me["weapons"])
        multiplier = stats["charge_move_mult"] if charge else 1
        walk = stats["walk_speed"] * multiplier
        run = stats["boost_speed"] * multiplier

        def score(yaw, speed):
            return self.score(obs, yaw, speed, goal, urgency, predictions, target, state, paths)

        scored = sorted(((score(yaw, walk), yaw) for yaw in options), reverse=True)
        if lanes:
            # Restrict the additional horizon to competitive directions.
            scored = sorted(((value - self.escape_cost(me, obs["zone"], yaw, walk, lanes), yaw)
                             for value, yaw in scored[:6]), reverse=True)
        best, yaw = scored[0]
        boosted = False
        reserve = stats["boost_drain"] * (0.08 if state == "EMERGENCY_ZONE" else 1.1)
        if me["grounded"] and me["energy"] > reserve and (state in ("EVADE", "EMERGENCY_ZONE") or paths):
            cost = 0.16 if state == "EMERGENCY_ZONE" else 0.7
            if lanes and sum(not track.covered and track.distance < 85 for track, _ in lanes) >= 3:
                cost = 0.35
            for option in dict.fromkeys((yaw, goal, scored[1][1], scored[2][1])):
                value = score(option, run) + (0.45 * math.cos(option - goal) if state == "EMERGENCY_ZONE" else 0)
                if lanes:
                    value -= self.escape_cost(me, obs["zone"], option, run, lanes)
                if value - cost > best:
                    best, yaw, boosted = value - cost, option, True
        bot.heading = math.atan2(math.sin(yaw), math.cos(yaw))
        return [math.cos(yaw), math.sin(yaw)], boosted


def clear_if_risk(arena, point, enemy, stats):
    return arena.clear((*point[:2], point[2] + stats["muzzle_height"]),
                       (*enemy[:2], enemy[2] + stats["height"] / 2), spacing=5)
