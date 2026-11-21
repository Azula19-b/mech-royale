"""Survival-focused mechfight/1 controller."""
import math
import json
import os
import mechfight

TAU = 2.0 * math.pi


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def dist2(a, b):
    x, y = a[0] - b[0], a[1] - b[1]
    return x * x + y * y


def wrap(a):
    return (a + math.pi) % TAU - math.pi


class Bot:
    name = "mech-survivor"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.slot = hello.get("slot", 0)
        try:
            with open(os.path.join(os.path.dirname(__file__), "strategy.json"), "r", encoding="utf-8") as f:
                self.s = json.load(f)
        except (OSError, ValueError):
            self.s = {}
        self.damage_memory = 0.0
        self.last_pos = None
        self.stuck = 0
        self.last_target = None
        self.prisms = []
        try:
            path = os.path.join(hello.get("map", {}).get("dir", "/arena"), "v2", "world.json")
            with open(path, "r", encoding="utf-8") as f:
                for prism in json.load(f).get("prisms", []):
                    ring = prism.get("ring", [])
                    if len(ring) >= 6:
                        xs, ys = ring[0::2], ring[1::2]
                        self.prisms.append((min(xs), min(ys), max(xs), max(ys),
                                            prism.get("base", -1e9), prism.get("top", 1e9),
                                            ring, prism.get("holes", [])))
        except (OSError, ValueError, TypeError):
            self.prisms = []

    @staticmethod
    def _inside(x, y, flat):
        inside = False
        n = len(flat) // 2
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi, xj, yj = flat[2*i], flat[2*i+1], flat[2*j], flat[2*j+1]
            if (yi > y) != (yj > y) and x < (xj-xi) * (y-yi) / ((yj-yi) or 1e-9) + xi:
                inside = not inside
            j = i
        return inside

    def _path_blocked(self, p, a, b):
        if not self.prisms:
            return False
        h = self.cfg["mech"].get("height", 9.0)
        # Sample a short steering segment against nearby solid footprints. This
        # avoids aiming directly into buildings while leaving engine collision
        # and wall sliding as the final authority.
        for xmin, ymin, xmax, ymax, base, top, ring, holes in self.prisms:
            if xmax < min(p[0], a) - 2 or xmin > max(p[0], a) + 2 or ymax < min(p[1], b) - 2 or ymin > max(p[1], b) + 2:
                continue
            if top <= p[2] or base >= p[2] + h:
                continue
            for k in (0.25, 0.5, 0.75, 1.0):
                x, y = p[0] + (a-p[0])*k, p[1] + (b-p[1])*k
                if self._inside(x, y, ring) and not any(self._inside(x, y, hole) for hole in holes):
                    return True
        return False

    def _future_zone(self, obs):
        z = obs["zone"]
        t = obs["t"]
        c = z["center"][:]
        r = z["radius"]
        # Project through the active shrink. Start moving early if the next zone
        # would otherwise require more travel than walking can cover.
        start, end = z["shrink_start"], z["shrink_end"]
        look = max(t, min(end, max(start, t + self.s.get("zone_projection_s", 12.0))))
        if end > start and look > start:
            f = clamp((look - start) / (end - start), 0.0, 1.0)
            n = z["next_center"]
            c = [c[0] + (n[0] - c[0]) * f, c[1] + (n[1] - c[1]) * f]
            r += (z["next_radius"] - r) * f
        elif t >= end:
            c, r = z["next_center"][:], z["next_radius"]
        return c, max(0.0, r - self.s.get("zone_margin_m", 18.0))

    def _target(self, obs, me, enemies, center, radius):
        p = me["pos"]
        # Select a quiet sector of the safe zone, biased away from the enemy
        # centroid. The deterministic phase prevents oscillation between sectors.
        ex = ey = weight = 0.0
        for e in enemies:
            d2 = dist2(e["pos"], p)
            influence = self.s.get("enemy_influence_range_m", 134.0)
            w = 1.0 / (1.0 + d2 / (influence * influence))
            ex += (e["pos"][0] - p[0]) * w
            ey += (e["pos"][1] - p[1]) * w
            weight += w
        if weight:
            away = math.atan2(-ey, -ex)
        else:
            away = ((self.slot * 2.399963229728653) + obs["t"] * 0.012) % TAU
        # Keep some room to respond to the circle while avoiding its crowded core.
        ring = radius * (self.s.get("zone_ring_fraction", 0.56) if radius > 100 else self.s.get("small_zone_ring_fraction", 0.43))
        tx, ty = center[0] + math.cos(away) * ring, center[1] + math.sin(away) * ring
        # Outside target safe circle, prioritize returning to the closest safe point.
        dx, dy = p[0] - center[0], p[1] - center[1]
        radial = math.hypot(dx, dy)
        if radial > radius * self.s.get("outer_zone_threshold", 0.78) and radial > 1e-5:
            f = self.s.get("zone_return_fraction", 0.62)
            tx, ty = center[0] + dx / radial * radius * f, center[1] + dy / radial * radius * f
        return [tx, ty]

    def _threats(self, obs, me):
        p = me["pos"]
        threats = []
        for e in obs.get("mechs", []):
            if e.get("kind") != "mech":
                continue
            dx, dy = e["pos"][0] - p[0], e["pos"][1] - p[1]
            d = math.hypot(dx, dy) + 1e-6
            aim = e.get("aim", [0.0, 0.0])[0]
            facing = max(0.0, math.cos(wrap(math.atan2(p[1] - e["pos"][1], p[0] - e["pos"][0]) - aim)))
            weapons = e.get("weapons", [])
            danger = 0.3 + 0.7 * facing
            if "lightning" in weapons and d < 65:
                danger *= 1.5
            if "mortar" in weapons and d < 180:
                danger *= 1.2
            if e.get("charging"):
                danger *= 1.6
            threats.append((e, d, dx / d, dy / d, danger))
        return threats

    def _move(self, obs, me, threats, target):
        p = me["pos"]
        vel = me["vel"]
        energy = me["energy"]
        low_energy = energy < max(18.0, self.cfg["mech"].get("energy", 100) * self.s.get("low_energy_fraction", 0.24))
        desired = math.atan2(target[1] - p[1], target[0] - p[0])
        # A compact angular search scores destination progress, separation,
        # telegraphed aim lanes, and projectiles over a short horizon.
        candidates = [desired + j * (TAU / 16.0) for j in range(16)]
        candidates.extend([desired, desired + math.pi / 8, desired - math.pi / 8])
        best, best_score = desired, -1e30
        goal_dist = math.sqrt(dist2(target, p)) + 1e-6
        for a in candidates:
            ux, uy = math.cos(a), math.sin(a)
            horizon = self.s.get("move_horizon_m", 8.0)
            nx, ny = p[0] + ux * horizon, p[1] + uy * horizon
            # progress to the safe waypoint
            score = (goal_dist - math.hypot(target[0] - nx, target[1] - ny)) * 0.48
            if self._path_blocked(p, nx, ny):
                score -= 18.0
            # Maintain standoff from the closest threats; do not chase their aim.
            for e, d, ex, ey, danger in threats:
                # predicted separation after acceleration has begun
                nd = math.hypot(e["pos"][0] + e["vel"][0] * 0.35 - nx,
                                e["pos"][1] + e["vel"][1] * 0.35 - ny)
                score += min(16.0, max(-12.0, nd - d)) * (0.25 + 0.23 * danger)
                if d < 150:
                    # Moving across an enemy's firing lane is safer than running
                    # directly toward or away from the muzzle.
                    radial = ux * ex + uy * ey
                    score -= max(0.0, radial) * (150.0 - d) * 0.055 * danger
            for b in obs.get("sounds", []):
                if b.get("kind") not in ("gun", "mortar", "explosion", "laser", "lightning", "npc_gun"):
                    continue
                age = b.get("age", 0.0)
                weight = math.exp(-age * 1.4)
                bearing = b.get("bearing", 0.0)
                toward = ux * math.cos(bearing) + uy * math.sin(bearing)
                score -= max(0.0, toward) * weight * (9.0 if b.get("dist") == "near" else 4.0)
            for q in obs.get("projectiles", []):
                qx, qy = q["pos"][0] - p[0], q["pos"][1] - p[1]
                vx, vy = q["vel"][0] - ux * horizon, q["vel"][1] - uy * horizon
                vv = vx * vx + vy * vy + 1e-5
                tm = clamp(-(qx * vx + qy * vy) / vv, 0.0, self.s.get("projectile_horizon_s", 0.9))
                miss = math.hypot(qx + vx * tm, qy + vy * tm)
                clearance = self.s.get("projectile_clearance_m", 12.0)
                if miss < clearance:
                    score -= (clearance - miss) * (1.0 - tm / 1.2) * 2.0
            # Preserve velocity through moderate turns; hard turns cost time.
            speed = math.hypot(vel[0], vel[1])
            if speed > 2:
                score += (ux * vel[0] + uy * vel[1]) / speed * 0.8
            if score > best_score:
                best_score, best = score, a
        dx, dy = target[0] - p[0], target[1] - p[1]
        far_target = math.hypot(dx, dy) > 16
        move = [math.cos(best), math.sin(best)] if far_target else [0.0, 0.0]
        # Boost only to escape danger or reach the shrinking zone. Energy is
        # reserved for emergency movement and lightning defense.
        pressure = any(d < self.s.get("emergency_threat_range_m", 52.0) and danger > 0.55 for _, d, _, _, danger in threats)
        outside_or_late = not me["in_zone"] or (obs["t"] > obs["zone"]["shrink_start"] - 12 and far_target)
        reserve = self.s.get("boost_reserve_energy", 40.0) if not low_energy else self.s.get("low_energy_boost_reserve", 58.0)
        boost = energy > reserve and far_target and (pressure or outside_or_late)
        return move, boost

    def _aim_and_fire(self, obs, me, threats):
        p, aim = me["pos"], me["aim"]
        enemies = [e for e in obs.get("mechs", []) if e.get("kind") == "mech"]
        enemies.sort(key=lambda e: (dist2(e["pos"], p), e.get("hp", 1e9)))
        target = enemies[0] if enemies else None
        weapons = me.get("weapons", [None, None])
        fire = [False, False]
        desired_aim = None
        if target:
            w = weapons[0] or {}
            stats = w.get("stats", self.cfg["weapons"]["gun"])
            dx, dy = target["pos"][0] - p[0], target["pos"][1] - p[1]
            vx, vy = target.get("vel", [0, 0, 0])[:2]
            speed = max(1.0, stats.get("speed", 252))
            # Intercept in the horizontal plane, then correct for elevation.
            aa = vx * vx + vy * vy - speed * speed
            bb = 2 * (dx * vx + dy * vy)
            cc = dx * dx + dy * dy
            disc = bb * bb - 4 * aa * cc
            flight = math.sqrt(cc) / speed
            if disc >= 0 and abs(aa) > 1e-6:
                roots = [x for x in ((-bb - math.sqrt(disc)) / (2 * aa), (-bb + math.sqrt(disc)) / (2 * aa)) if x > 0]
                if roots:
                    flight = min(roots)
            tx = target["pos"][0] + vx * flight
            ty = target["pos"][1] + vy * flight
            tz = target["pos"][2] + target.get("vel", [0, 0, 0])[2] * flight + 4.5
            mx = p[0] + math.cos(aim[0]) * 3.0
            my = p[1] + math.sin(aim[0]) * 3.0
            mz = p[2] + 7.0
            dx, dy, dz = tx - mx, ty - my, tz - mz
            dxy = math.hypot(dx, dy)
            yaw = math.atan2(dy, dx)
            pitch = math.atan2(dz, max(1.0, dxy))
            desired_aim = [yaw, clamp(pitch, self.cfg["mech"].get("min_pitch", -0.7), self.cfg["mech"].get("max_pitch", 1.3))]
            yawerr = abs(wrap(yaw - aim[0]))
            pitcherr = abs(pitch - aim[1])
            rng = math.sqrt(dist2([tx, ty], p))
            kind = (weapons[0] or {}).get("kind", "gun")
            # Fire the gun only when its actual, slewed aim is near the lead.
            if kind == "gun":
                fire[0] = (yawerr < self.s.get("gun_yaw_tolerance_rad", 0.19) and pitcherr < self.s.get("gun_pitch_tolerance_rad", 0.13) and rng < stats.get("range", 230)
                           and (w.get("reload", 0) <= 0) and w.get("ammo", 1) > 0)
            elif kind == "laser":
                charge = w.get("charge", 0.0)
                aligned = yawerr < 0.14 and pitcherr < 0.12
                # Controlled charge, release only after a useful charge.
                charge_frac = self.s.get("laser_charge_fraction", 0.78)
                fire[0] = aligned and (charge < stats.get("charge", 0.75) * charge_frac)
                if charge >= stats.get("min_charge", 0.15) and (not aligned or charge >= stats.get("charge", 0.75) * charge_frac):
                    fire[0] = False
            elif kind == "lightning":
                fire[0] = (rng < stats.get("range", 45) and yawerr < math.radians(stats.get("cone_deg", 30))
                           and me["energy"] > stats.get("energy", 3) + 8)
        if desired_aim is None:
            # Face the strongest recent or visible threat to keep the front armor
            # toward incoming shots. Facing does not slow movement.
            if threats:
                e = min(threats, key=lambda q: q[1] / max(0.1, q[4]))[0]
                desired_aim = [math.atan2(e["pos"][1] - p[1], e["pos"][0] - p[0]), 0.0]
            elif obs.get("events"):
                damaged = [e for e in obs["events"] if e.get("type") == "damaged" and e.get("bearing") is not None]
                if damaged:
                    desired_aim = [damaged[-1]["bearing"], 0.0]
            if desired_aim is None:
                desired_aim = [aim[0], 0.0]
        return desired_aim, fire, target

    def act(self, obs):
        me = obs["self"]
        p = me["pos"]
        if self.last_pos and dist2(p, self.last_pos) < 0.12 and math.hypot(*me["vel"][:2]) < 0.8:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 2)
        self.last_pos = p[:]
        enemies = [e for e in obs.get("mechs", []) if e.get("kind") == "mech"]
        center, radius = self._future_zone(obs)
        target = self._target(obs, me, enemies, center, radius)
        threats = self._threats(obs, me)
        move, boost = self._move(obs, me, threats, target)
        if self.stuck > 8:
            a = (obs["tick"] * 0.37 + self.slot) % TAU
            move, boost = [math.cos(a), math.sin(a)], me["energy"] > 35
        aim, fire, enemy = self._aim_and_fire(obs, me, threats)

        action = {"tick": obs["tick"], "move": move, "boost": boost, "aim": aim,
                  "fire": fire, "jump": bool(me.get("grounded") and self.stuck > 15), "reload": False}
        # Reload between fights; reload early before the magazine is empty.
        gun = next((w for w in me.get("weapons", []) if w and w.get("kind") == "gun"), None)
        if gun and gun.get("reload", 0) <= 0 and gun.get("ammo", 0) <= self.s.get("reload_ammo_threshold", 4) and not any(d < self.s.get("reload_safe_range_m", 55) for _, d, _, _, _ in threats):
            action["reload"] = True

        offer = obs.get("offer")
        if offer:
            rank = self.s.get("upgrade_rank", {})
            choices = offer.get("choices", [])
            action["pick"] = min(range(len(choices)), key=lambda i: rank.get(choices[i]["id"], 50)) if choices else 0

        # Favor lightning as a close-range defensive tool. Avoid slow laser
        # charging and self-damaging mortar unless unarmed in slot 1.
        held = {w["kind"] for w in me.get("weapons", []) if w}
        candidates = []
        for item in obs.get("pickups", []):
            if item["kind"] in held:
                continue
            d2 = dist2(item["pos"], p)
            if d2 <= self.cfg["loot"].get("pickup_radius", 4) ** 2 and abs(item["pos"][2] - p[2]) <= 4:
                value = self.s.get("pickup_rank", {}).get(item["kind"], 0)
                if item["kind"] == "lightning" and any(d < 100 for _, d, _, _, _ in threats):
                    value += 2
                candidates.append((value, -d2, item))
        empty = [i for i, w in enumerate(me["weapons"]) if w is None]
        if candidates and empty:
            action["take"] = max(candidates, key=lambda x: (x[0], x[1]))[2]["id"]
        return action


mechfight.run(Bot)
