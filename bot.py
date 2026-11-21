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
        self.memory = {}
        self.safe_target = None
        self.safe_target_time = -1e9
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
            dx, dy = a-p[0], b-p[1]
            length = math.hypot(dx,dy) or 1.0
            # Sweep the mech's radius as well as its centerline. Samples are
            # dense enough to reject narrow walls and corner clipping.
            nx, ny = -dy/length*2.35, dx/length*2.35
            for k in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
                cx, cy = p[0] + dx*k, p[1] + dy*k
                for ox, oy in ((0,0),(nx,ny),(-nx,-ny)):
                    x,y=cx+ox,cy+oy
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
        # Opportunistic pickups are goals only when safely reachable and useful.
        urgency = not me.get("in_zone", True) or radius < 100
        best, best_score = None, 0.0
        for item in obs.get("cubes", []) + obs.get("pickups", []):
            d = math.sqrt(dist2(item["pos"], p))
            if d > 90 or abs(item["pos"][2] - p[2]) > 12:
                continue
            outside = math.hypot(item["pos"][0]-center[0], item["pos"][1]-center[1]) > radius
            if outside and not urgency:
                continue
            if item in obs.get("pickups", []):
                kind = item.get("kind")
                held = {w.get("kind") for w in me.get("weapons", []) if w}
                if kind in held or len(held) >= 2 or kind == "mortar":
                    continue
                value = {"lightning": 1.1, "laser": 0.9, "mortar": 0.25}.get(kind, 0)
            else:
                value = 1.7 if me.get("cubes", 0) < 10 else 0.9
            danger = min((max(0.0, 70-math.sqrt(dist2(e["pos"], p))) * 0.025 for e in enemies), default=0)
            score = value * (1.0 - d / 140.0) - danger - (1.0 if outside else 0.0)
            if score > best_score:
                best, best_score = item["pos"][:2], score
        if best is not None:
            tx, ty = best
        # Hold a chosen navigation point briefly to avoid direction jitter as
        # the visible enemy set changes from observation to observation.
        if (self.safe_target is None or obs.get("t", 0)-self.safe_target_time > 3.0
                or math.hypot(self.safe_target[0]-p[0], self.safe_target[1]-p[1]) < 14
                or math.hypot(self.safe_target[0]-center[0], self.safe_target[1]-center[1]) > radius):
            self.safe_target = [tx, ty]
            self.safe_target_time = obs.get("t", 0)
        return self.safe_target

    def _threats(self, obs, me):
        p = me["pos"]
        threats = []
        for e in obs.get("mechs", []):
            kind = e.get("kind")
            if kind not in ("mech", "npc"):
                continue
            dx, dy = e["pos"][0] - p[0], e["pos"][1] - p[1]
            d = math.hypot(dx, dy) + 1e-6
            aim = e.get("aim", [0.0, 0.0])[0]
            facing = max(0.0, math.cos(wrap(math.atan2(p[1] - e["pos"][1], p[0] - e["pos"][0]) - aim)))
            weapons = e.get("weapons", [])
            danger = (0.3 + 0.7 * facing) if kind == "mech" else (0.55 if d < 60 else 0.18)
            if "lightning" in weapons and d < 65:
                danger *= 1.5
            if "mortar" in weapons and d < 180:
                danger *= 1.2
            if e.get("charging"):
                danger *= 1.6
            threats.append((e, d, dx / d, dy / d, danger))
            if kind == "mech":
                self.memory[e["id"]] = (e, obs.get("t", 0.0))
        # Retain recently seen pursuers after they disappear behind cover.
        for eid, (e, seen) in list(self.memory.items()):
            if obs.get("t", 0.0) - seen <= 5 and all(q[0].get("id") != eid for q in threats):
                dx, dy = e["pos"][0]-p[0], e["pos"][1]-p[1]
                d = math.hypot(dx, dy)+1e-6
                threats.append((e, d, dx/d, dy/d, 0.45))
            elif obs.get("t", 0.0)-seen > 5:
                del self.memory[eid]
        for ev in obs.get("events", []):
            if ev.get("type") == "damaged" and ev.get("bearing") is not None:
                self.damage_memory = 3.0
                self.last_damage_bearing = ev["bearing"]
        self.damage_memory = max(0.0, self.damage_memory - 0.1)
        return threats

    def _cover_target(self, me, threats, center, radius):
        if not threats or not self.prisms:
            return None
        p = me["pos"]
        # Test points behind nearby buildings relative to attacker positions.
        best, best_score = None, -1e9
        for xmin, ymin, xmax, ymax, base, top, ring, holes in self.prisms:
            if top < p[2] + 2 or base > p[2] + me.get("stats", {}).get("height", 9):
                continue
            cx, cy = (xmin+xmax)*0.5, (ymin+ymax)*0.5
            if (cx-p[0])**2 + (cy-p[1])**2 > 170**2:
                continue
            for e, d, ex, ey, danger in threats:
                if d > 170:
                    continue
                ax, ay = e["pos"][0], e["pos"][1]
                vx, vy = cx-ax, cy-ay
                n = math.hypot(vx, vy) or 1
                x, y = cx + vx/n*10, cy + vy/n*10
                if self._inside(x,y,ring) and not any(self._inside(x,y,h) for h in holes):
                    continue
                # Only call it cover when the attacker-to-waypoint ray crosses
                # a solid that blocks a mech at this elevation.
                blocked = False
                for k in (0.2,0.35,0.5,0.65,0.8):
                    lx=e["pos"][0]+(x-e["pos"][0])*k
                    ly=e["pos"][1]+(y-e["pos"][1])*k
                    if self._inside(lx,ly,ring) and not any(self._inside(lx,ly,h) for h in holes):
                        blocked = True
                        break
                if not blocked:
                    continue
                travel = math.hypot(x-p[0], y-p[1])
                safe = math.hypot(x-center[0], y-center[1]) <= max(0, radius-8)
                score = (180-d)*0.25 + (70 if safe else -120) - travel*0.8
                if score > best_score and travel < 100:
                    best, best_score = [x, y], score
        return best

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
                # Relative velocity is m/s for both actors; horizon above is
                # a spatial sample length and must never be subtracted from it.
                speed = me.get("stats", {}).get("walk_speed", 11.7)
                qx, qy = q["pos"][0] - p[0], q["pos"][1] - p[1]
                vx, vy = q["vel"][0] - ux * speed, q["vel"][1] - uy * speed
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
        far_target = math.hypot(dx, dy) > 3
        if not far_target and threats:
            closest = min(threats, key=lambda q: q[1])
            best = math.atan2(p[1]-closest[0]["pos"][1], p[0]-closest[0]["pos"][0]) + 0.65
            far_target = True
        move = [math.cos(best), math.sin(best)] if far_target else [0.0, 0.0]
        # Boost only to escape danger or reach the shrinking zone. Energy is
        # reserved for emergency movement and lightning defense.
        pressure = any(d < self.s.get("emergency_threat_range_m", 52.0) and danger > 0.55 for _, d, _, _, danger in threats)
        outside_or_late = not me["in_zone"] or (obs["t"] > obs["zone"]["shrink_start"] - 12 and far_target)
        reserve = self.s.get("boost_reserve_energy", 40.0) if not low_energy else self.s.get("low_energy_boost_reserve", 58.0)
        boost = energy > reserve and far_target and (pressure or outside_or_late or (self.damage_memory > 0 and energy > 55))
        return move, boost

    def _aim_and_fire(self, obs, me, threats):
        p, aim = me["pos"], me["aim"]
        enemies = [e for e in obs.get("mechs", []) if e.get("kind") == "mech"]
        enemies.sort(key=lambda e: (dist2(e["pos"], p) / max(0.35, 1.0 + (e.get("charging", False) * 0.6)), e.get("hp", 1e9)))
        target = enemies[0] if enemies else None
        weapons = me.get("weapons", [None, None])
        fire = [False, False]
        desired_aim = None
        if target:
            dx, dy = target["pos"][0] - p[0], target["pos"][1] - p[1]
            vx, vy, vz = target.get("vel", [0, 0, 0])[:3]
            for i, w in enumerate(weapons):
                if not w:
                    continue
                kind = w.get("kind", "gun")
                stats = w.get("stats", self.cfg["weapons"].get(kind, {}))
                tx, ty, tz = target["pos"][0], target["pos"][1], target["pos"][2] + 4.5
                rx, ry = tx-p[0], ty-p[1]
                d = math.hypot(rx, ry)
                pitch = 0.0
                if kind == "mortar":
                    # Select a low ballistic arc when reachable. Gravity and
                    # speed are taken from the live weapon stats.
                    speed, gravity = stats.get("speed", 72), stats.get("gravity", 22)
                    q = speed**4 - gravity*(gravity*d*d + 2*(tz-(p[2]+9.5))*speed*speed)
                    if q >= 0 and d > 1:
                        pitch = math.atan((speed*speed-math.sqrt(q))/(gravity*d))
                        flight = d / max(1, speed*math.cos(pitch))
                        tx, ty, tz = tx+vx*flight, ty+vy*flight, tz+vz*flight
                    else:
                        pitch = math.radians(35)
                else:
                    speed = stats.get("speed", 252) if kind == "gun" else 252
                    aa, bb, cc = vx*vx+vy*vy-speed*speed, 2*(rx*vx+ry*vy), rx*rx+ry*ry
                    disc = bb*bb-4*aa*cc
                    flight = d/max(1, speed)
                    if disc >= 0 and abs(aa) > 1e-6:
                        roots = [q for q in ((-bb-math.sqrt(disc))/(2*aa),(-bb+math.sqrt(disc))/(2*aa)) if q>0]
                        if roots: flight=min(roots)
                    tx, ty, tz = tx+vx*flight, ty+vy*flight, tz+vz*flight
                    pitch = math.atan2(tz-(p[2]+7), max(1, math.hypot(tx-p[0],ty-p[1])))
                yaw = math.atan2(ty-p[1], tx-p[0])
                if desired_aim is None or kind in ("gun", "laser"):
                    desired_aim = [yaw, clamp(pitch, self.cfg["mech"].get("min_pitch",-.7), self.cfg["mech"].get("max_pitch",1.3))]
                yawerr, pitcherr = abs(wrap(yaw-aim[0])), abs(pitch-aim[1])
                aligned = yawerr < 0.16 and pitcherr < 0.14
                rng = math.hypot(tx-p[0], ty-p[1])
                if kind == "gun":
                    fire[i] = aligned and rng < stats.get("range",230) and w.get("reload",0)<=0 and w.get("ammo",1)>0
                elif kind == "laser":
                    charge, full = w.get("charge",0), stats.get("charge",.75)
                    # Trigger held builds charge; false releases and fires.
                    if charge > 0:
                        fire[i] = aligned and charge < full * self.s.get("laser_charge_fraction",.78)
                    else:
                        fire[i] = aligned and rng < stats.get("range",280)
                elif kind == "lightning":
                    fire[i] = rng < stats.get("range",45) and yawerr < math.radians(stats.get("cone_deg",30)) and me.get("energy",0) > stats.get("energy",3)+5
                elif kind == "mortar":
                    fire[i] = d > 14 and d < 220 and aligned and w.get("cooldown",0)<=0
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
        elif self.damage_memory > 0 and hasattr(self, "last_damage_bearing"):
            desired_aim = [self.last_damage_bearing, 0.0]
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
        cover = self._cover_target(me, threats, center, radius)
        if cover and (self.damage_memory > 0 or len(threats) >= 2):
            target = cover
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


class RooftopBot(Bot):
    """Experimental placement-first controller: roof refuge, silence and timed evacuation."""
    name = "mech-roof-survivor-v3"

    def __init__(self, hello):
        super().__init__(hello)
        self.roofs = []
        self.roof = None
        self.roof_started = -1e9
        self.last_hp = None
        self.last_hp_hit = -1e9
        self.roof_fail_until = -1e9
        self.roof_last_pos = None
        self.roof_still = 0
        for xmin, ymin, xmax, ymax, base, top, ring, holes in self.prisms:
            width, height = xmax - xmin, ymax - ymin
            if width < 16 or height < 13 or top - base < 7:
                continue
            if width > 175 or height > 175:
                continue
            # Choose a real interior point, not merely the bounding-box centre.
            for fx, fy in ((.5, .5), (.35, .5), (.65, .5), (.5, .35), (.5, .65)):
                x, y = xmin + width * fx, ymin + height * fy
                if self._inside(x, y, ring) and not any(self._inside(x, y, h) for h in holes):
                    self.roofs.append((x, y, top, xmin, ymin, xmax, ymax, ring, holes))
                    break

    def _forecast(self, obs):
        z, t = obs["zone"], obs["t"]
        if t < z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        return z["center"], z["radius"]

    def _roof_valid(self, roof, obs):
        c, r = self._forecast(obs)
        x, y = roof[0], roof[1]
        # When the next safe circle is very small, leave rooftops for the zone.
        return r > 58 and math.hypot(x - c[0], y - c[1]) < r - 16

    def _choose_roof(self, obs, me, foes):
        p, t = me["pos"], obs["t"]
        if self.roof and self._roof_valid(self.roof, obs):
            # Keep a committed plan; switching rooftops every observation is fatal.
            if t - self.roof_started < 35:
                return self.roof
            if p[2] >= self.roof[2] - 1.5 and self._inside(p[0], p[1], self.roof[7]):
                return self.roof
        if t < self.roof_fail_until or me["energy"] < 30:
            return None
        chosen, best = None, 1e20
        for roof in self.roofs:
            x, y, top = roof[:3]
            distance = math.hypot(p[0] - x, p[1] - y)
            height = top - p[2]
            if distance > 115 or height < 5 or height > 42:
                continue
            if not self._roof_valid(roof, obs):
                continue
            # Prefer near, moderate-height roofs outside enemy clustering.
            enemy_risk = sum(max(0, 115 - math.hypot(e["pos"][0] - x, e["pos"][1] - y)) * .24 for e in foes)
            score = distance + max(0, height - 12) * .4 + enemy_risk
            if score < best:
                best, chosen = score, roof
        if chosen is not None:
            self.roof = chosen
            self.roof_started = t
        return chosen

    def _roof_action(self, obs, me, roof, close_threat):
        p = me["pos"]
        x, y, top, xmin, ymin, xmax, ymax, ring, holes = roof
        dx, dy = x - p[0], y - p[1]
        d = math.hypot(dx, dy)
        inside = self._inside(p[0], p[1], ring) and not any(self._inside(p[0], p[1], h) for h in holes)
        # After landing, stop making footsteps and avoid revealing our position.
        if inside and p[2] >= top - 1.5 and p[2] <= top + 2.5 and me.get("grounded", False):
            if not close_threat:
                return [0.0, 0.0], False, False, True
            # Retreat toward the roof's central area instead of jumping into enemies.
            return ([dx / d, dy / d] if d > 7 else [0.0, 0.0]), False, False, True
        # Distance to the closest point of the building footprint's bounding box.
        edge = math.hypot(p[0] - clamp(p[0], xmin, xmax), p[1] - clamp(p[1], ymin, ymax))
        if edge > 11:
            return [dx / d, dy / d] if d > .1 else [0.0, 0.0], False, False, True
        # The vertical component must be actively solved; horizontal wall-sliding
        # alone was the principal limitation of v1/v2 navigation.
        if p[2] < top - .6 and me["energy"] > 13:
            if me.get("grounded", False):
                return [0.0, 0.0], False, True, True
            return [dx / d, dy / d] if p[2] >= top - 4 and d > 2 else [0.0, 0.0], True, False, True
        if d > 3:
            # Keep jetting briefly if airborne beside the edge to avoid sliding
            # down the wall before we reach the roof footprint.
            jet = not inside and not me.get("grounded", False) and me["energy"] > 17
            return [dx / d, dy / d], jet, False, True
        return [0.0, 0.0], False, False, True

    def act(self, obs):
        # Retain the well-tested wrapper, schema, pickup and weapon handling,
        # replacing the original risky navigation/combat policy below.
        action = Bot.act(self, obs)
        me, p, t = obs["self"], obs["self"]["pos"], obs["t"]
        foes = [e for e in obs.get("mechs", []) if e.get("kind") == "mech"]
        nearest = min((math.hypot(e["pos"][0] - p[0], e["pos"][1] - p[1]) for e in foes), default=1e9)
        damaged = any(e.get("type") == "damaged" and e.get("weapon") != "zone" for e in obs.get("events", []))
        if self.last_hp is not None and me["hp"] < self.last_hp - 1:
            damaged = True
        self.last_hp = me["hp"]
        if damaged:
            self.last_hp_hit = t
        threatened = nearest < 75 or t - self.last_hp_hit < 2

        # Healing requires 5 seconds without dealing OR taking damage. Do not
        # fire at distant pursuers simply because they entered the vision cone.
        # Only fire when an actual close-range defensive engagement is necessary.
        if nearest > 32 or (not threatened and me["hp"] > me.get("max_hp", 1400) * .45):
            action["fire"] = [False, False]
        if foes and nearest < 100 and not any(action["fire"]):
            e = min(foes, key=lambda e: dist2(e["pos"], p))
            action["aim"] = [math.atan2(e["pos"][1] - p[1], e["pos"][0] - p[0]), 0.0]

        # Determine rooftop anchor from the fixed public map, not hidden spawns.
        roof = self._choose_roof(obs, me, foes)
        if roof:
            on_roof = (self._inside(p[0], p[1], roof[7]) and
                       p[2] >= roof[2] - 1.5 and me.get("grounded", False))
            if (self.roof_last_pos is not None and dist2(p, self.roof_last_pos) < .05
                    and abs(p[2] - self.roof_last_pos[2]) < .05 and not on_roof):
                self.roof_still += 1
            else:
                self.roof_still = 0
            self.roof_last_pos = p[:]
            if self.roof_still > 35 and t - self.roof_started > 3.5:
                self.roof = None
                self.roof_fail_until = t + 15
                roof = None
            else:
                move, boost, jump, uses_roof = self._roof_action(obs, me, roof, threatened)
                if uses_roof:
                    action["move"], action["boost"], action["jump"] = move, boost, jump
                    # Do not spend movement energy in safe cover; use it to escape later.
                    return action
        # Away from combat, avoid walking needlessly across the open map if
        # safely deep inside the destination zone: preserve HP regeneration.
        c, r = self._forecast(obs)
        if not threatened and r > 90 and math.hypot(p[0]-c[0], p[1]-c[1]) < r * .56:
            action["move"] = [0.0, 0.0]
            action["boost"] = False
        # If enemies are actively chasing, spend energy to create distance.
        if threatened and nearest < 110 and me["energy"] > 26 and any(abs(v) > .2 for v in action["move"]):
            action["boost"] = True
        return action


class V4Bot(RooftopBot):
    """Persistent tactical state machine; emergencies always outrank roof plans."""
    name = "mech-survivor-v4"
    STATES = ("EXPLORE", "REPOSITION", "CLIMB", "HIDE", "EVADE", "FIGHT", "ZONE_ESCAPE", "ENDGAME")

    def __init__(self, hello):
        super().__init__(hello)
        self.state = "EXPLORE"
        self.state_since = 0.0
        self.last_transition = -1e9
        self.climb_phase = "approach"
        self.climb_started = -1e9
        self.climb_phase_since = -1e9
        self.evade_until = self.hide_until = -1e9
        self.last_hp_v4 = None
        self.last_pos_v4 = None
        self.last_goal_v4 = None
        self.movement_failures = 0

    def _select_state(self, obs, me, threats, roof):
        t, p = obs["t"], me["pos"]
        center, radius = self._future_zone(obs)
        radial = math.hypot(p[0]-center[0], p[1]-center[1])
        if self.last_hp_v4 is not None and me["hp"] < self.last_hp_v4 - 1:
            self.last_hp_hit = t
        self.last_hp_v4 = me["hp"]
        damaged = any(e.get("type") == "damaged" and e.get("weapon") != "zone" for e in obs.get("events", []))
        if damaged:
            self.last_hp_hit = t
        mech_threats = [q for q in threats if q[0].get("kind") == "mech"]
        nearest = min((q[1] for q in mech_threats), default=1e9)
        attackers = [q for q in mech_threats if q[1] < 115 and q[4] > .35]
        if len(attackers) >= 2 or (nearest < 42 and len(mech_threats) >= 2) or (damaged and nearest < 70):
            self.evade_until = max(self.evade_until, t + 2.4)
        if damaged and nearest > 65 and me["hp"] < me.get("max_hp",1400)*.75:
            self.hide_until = max(self.hide_until, t + 5.0)
        if (not me.get("in_zone",True) or radial > radius-12 or
                (t > obs["zone"].get("shrink_start",1e9) and radial > radius*.88)):
            desired = "ZONE_ESCAPE"
        elif obs.get("alive",16) <= 4:
            desired = "ENDGAME"
        elif t < self.evade_until:
            desired = "EVADE"
        elif t < self.hide_until and self.damage_memory > 0:
            desired = "HIDE"
        elif nearest < 72 or (damaged and nearest < 105):
            desired = "FIGHT"
        elif self.climb_phase not in ("done","failed") and self.roof:
            desired = "CLIMB"
        elif roof and me.get("energy",0) > 48 and not attackers:
            desired = "CLIMB"
        elif len(mech_threats) >= 2 or nearest < 155:
            desired = "REPOSITION"
        elif obs.get("cubes") or obs.get("pickups"):
            desired = "EXPLORE"
        else:
            desired = "REPOSITION"
        # Short commitment prevents state chatter; emergencies bypass it.
        if desired != self.state and desired not in ("ZONE_ESCAPE","EVADE","ENDGAME") and t-self.last_transition < .8:
            desired = self.state
        if desired != self.state:
            self.state, self.state_since, self.last_transition = desired, t, t
        return self.state, center, radius, nearest, attackers

    def _climb(self, obs, me, roof):
        p, t = me["pos"], obs["t"]
        x,y,top,xmin,ymin,xmax,ymax,ring,holes = roof
        d = math.hypot(x-p[0],y-p[1])
        inside = self._inside(p[0],p[1],ring) and not any(self._inside(p[0],p[1],h) for h in holes)
        landed = inside and p[2] >= top-1.4 and me.get("grounded",False)
        if landed:
            self.climb_phase = "done"
            return [0,0],False,False
        if self.climb_started < 0:
            self.climb_started = self.climb_phase_since = t
        if t-self.climb_started > 12:
            self.climb_phase="failed"; self.roof=None; self.roof_fail_until=t+18
            return None
        if self.climb_phase == "approach":
            edge=math.hypot(p[0]-clamp(p[0],xmin-5,xmax+5),p[1]-clamp(p[1],ymin-5,ymax+5))
            if edge > 1.5:
                return [(x-p[0])/max(d,1e-6),(y-p[1])/max(d,1e-6)],False,False
            self.climb_phase="jump"; self.climb_phase_since=t
        if self.climb_phase == "jump":
            if me.get("grounded"):
                self.climb_phase="jet"; self.climb_phase_since=t
                return [0,0],False,True
            self.climb_phase="jet"; self.climb_phase_since=t
        if self.climb_phase == "jet":
            if p[2] >= top+1:
                self.climb_phase="land"; self.climb_phase_since=t
            elif me.get("energy",0)<8 or t-self.climb_phase_since>4.2:
                self.climb_phase="recover"; self.climb_phase_since=t
            else:
                return [clamp((x-p[0])/max(d,1),-1,1),clamp((y-p[1])/max(d,1),-1,1)],True,False
        if self.climb_phase == "land":
            if p[2]>top+.2:
                return [clamp((x-p[0])/max(d,1),-1,1),clamp((y-p[1])/max(d,1),-1,1)],False,False
            self.climb_phase="recover"; self.climb_phase_since=t
        if self.climb_phase == "recover":
            if me.get("grounded") and t-self.climb_phase_since>.8:
                self.climb_phase="approach"; self.climb_started=t
            return [clamp((x-p[0])/max(d,1),-1,1),clamp((y-p[1])/max(d,1),-1,1)],False,bool(me.get("grounded") and p[2]<top-5)
        return None

    def act(self, obs):
        # Run shared targeting, protocol, pickup and aim logic; replace V3's
        # final roof/standing-still movement arbitration below.
        action = super().act(obs)
        me,p,t=obs["self"],obs["self"]["pos"],obs["t"]
        threats=self._threats(obs,me)
        foes=[e for e in obs.get("mechs",[]) if e.get("kind")=="mech"]
        roof=self._choose_roof(obs,me,foes)
        state,center,radius,nearest,attackers=self._select_state(obs,me,threats,roof)
        target=self._target(obs,me,foes,center,radius)
        if state in ("ZONE_ESCAPE","EVADE"):
            if state == "ZONE_ESCAPE":
                dx,dy=center[0]-p[0],center[1]-p[1]; length=max(1,math.hypot(dx,dy))
                target=[center[0]-dx/length*radius*.48,center[1]-dy/length*radius*.48]
            else:
                sx=sy=0.0
                for e,d,ex,ey,danger in threats:
                    if e.get("kind")=="mech": sx+=ex*danger/max(d,12); sy+=ey*danger/max(d,12)
                angle=math.atan2(-sy,-sx) if abs(sx)+abs(sy)>1e-5 else t*1.7+self.slot
                target=[p[0]+math.cos(angle)*85,p[1]+math.sin(angle)*85]
            action["move"],action["boost"]=self._move(obs,me,threats,target)
            action["boost"]=me.get("energy",0)>22
            action["jump"]=bool(me.get("grounded") and state=="EVADE")
        elif state == "HIDE":
            cover=self._cover_target(me,threats,center,radius)
            if cover: action["move"],action["boost"]=self._move(obs,me,threats,cover)
            if nearest>75: action["fire"]=[False,False]
        elif state == "FIGHT":
            # Keep moving while defending; prioritize the weakest nearby attacker.
            if foes:
                target_enemy=min(foes,key=lambda e:(dist2(e["pos"],p),e.get("hp",1e9)))
                target=[p[0]-(target_enemy["pos"][0]-p[0]),p[1]-(target_enemy["pos"][1]-p[1])]
            action["move"],action["boost"]=self._move(obs,me,threats,target)
            if nearest<125:
                action["aim"],action["fire"],_=self._aim_and_fire(obs,me,threats)
        elif state == "CLIMB" and roof:
            if self.climb_phase in ("done","failed"):
                self.climb_phase="approach"; self.climb_started=t
            cmd=self._climb(obs,me,roof)
            if cmd is not None: action["move"],action["boost"],action["jump"]=cmd
            if nearest<80:
                action["move"],action["boost"]=self._move(obs,me,threats,target)
        elif state in ("EXPLORE","REPOSITION","ENDGAME"):
            action["move"],action["boost"]=self._move(obs,me,threats,target)
            if state=="ENDGAME" and foes and nearest<130:
                action["fire"]=self._aim_and_fire(obs,me,threats)[1]
        # Movement failure recovery detects blocked/stalled commands over time.
        if self.last_pos_v4 is not None:
            delta=math.hypot(p[0]-self.last_pos_v4[0],p[1]-self.last_pos_v4[1])
            if delta<.08 and self.last_goal_v4 and math.hypot(*me.get("vel",[0,0])[:2])<1 and math.hypot(self.last_goal_v4[0]-p[0],self.last_goal_v4[1]-p[1])>8:
                self.movement_failures+=1
            else: self.movement_failures=max(0,self.movement_failures-1)
        self.last_pos_v4=p[:]; self.last_goal_v4=target[:]
        if self.movement_failures>=5 and state not in ("ZONE_ESCAPE","EVADE"):
            angle=math.atan2(target[1]-p[1],target[0]-p[0])+math.pi/2*((obs["tick"]//20+self.slot)%2*2-1)
            action["move"],action["boost"],action["jump"]=[math.cos(angle),math.sin(angle)],me["energy"]>25,bool(me.get("grounded"))
            self.movement_failures=0
        action["tick"]=obs["tick"]
        return action


mechfight.run(V4Bot)
