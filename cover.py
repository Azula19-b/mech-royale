"""Ground routes to projectile-blocking cover using the arena's walking graph."""
from heapq import heappop, heappush
import math


class Cover:
    def __init__(self, arena, stats, config):
        self.arena = arena
        self.config = config
        self.sites = set()
        self.path = []
        self.replan_at = 0
        self.zone_waypoint = None
        self.zone_replan_at = 0
        self.stall = 0
        self.was_moving = False
        self.stall_origin = None
        self.recovery = None
        for node, links in arena.links.items():
            if len(links) < 3:
                continue
            x, y, z = self.point(node)
            chest = z + stats["height"] * 0.55
            if any(not arena.clear((x, y, chest), (x + dx, y + dy, chest), spacing=3)
                   for dx, dy in ((16, 0), (-16, 0), (0, 16), (0, -16))):
                self.sites.add(node)

    def point(self, node):
        i, j = node
        return i * self.arena.spacing, j * self.arena.spacing, self.arena.nodes[node]

    def walk(self, pos, point, stats):
        dx, dy = point[0] - pos[0], point[1] - pos[1]
        distance = math.hypot(dx, dy)
        return distance < 0.01 or self.arena.walk(pos, dx / distance, dy / distance, distance, stats)[0] >= distance

    def hunters(self, enemies):
        base = self.config["mech"]
        scale = self.config["cubes"]["size_per_cube"]
        return [
            (e["pos"], e["vel"], base["muzzle_height"] * (1 + scale * e.get("cubes", 0)))
            for e in enemies
        ]

    def visible(self, point, stats, hunter, seconds=0):
        pos, vel, muzzle = hunter
        dx, dy = vel[0] * seconds, vel[1] * seconds
        travel = math.hypot(dx, dy)
        if travel:
            traveled, z = self.arena.walk(pos, dx / travel, dy / travel, travel, self.config["mech"])
            fraction = traveled / travel
            pos = (pos[0] + dx * fraction, pos[1] + dy * fraction, z)
        origin = (*pos[:2], pos[2] + muzzle)
        return any(self.arena.clear(origin, (*point[:2], point[2] + stats["height"] * fraction), spacing=3)
                   for fraction in (0.45, 0.85))

    def sheltered(self, point, stats, hunters, seconds):
        for hunter in hunters:
            pos = hunter[0]
            gap = math.dist(point[:2], pos[:2])
            if gap < 30 or (gap < 230 and (self.visible(point, stats, hunter)
                                          or self.visible(point, stats, hunter, seconds))):
                return False
        return True

    @staticmethod
    def inside(point, zone):
        return (math.dist(point[:2], zone["center"]) <= zone["radius"] - 7
                and math.dist(point[:2], zone["next_center"]) <= zone["next_radius"] - 6)

    def search(self, obs, hunters, rotation):
        me, zone = obs["self"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        step = self.arena.spacing
        x, y = (round(v / step) for v in pos[:2])
        queue, costs, parents = [], {}, {}
        for i in range(x - 1, x + 2):
            for j in range(y - 1, y + 2):
                node = i, j
                if node in self.arena.nodes:
                    point = self.point(node)
                    if (math.dist(point[:2], zone["center"]) <= zone["radius"]
                            and self.walk(pos, point, stats) and abs(point[2] - pos[2]) < 5):
                        cost = math.dist(pos[:2], point[:2])
                        costs[node], parents[node] = cost, None
                        heappush(queue, (cost, node))
        ranked = []
        for _ in range(180):
            if not queue:
                break
            cost, node = heappop(queue)
            if cost != costs[node] or cost > 85:
                continue
            point = self.point(node)
            if node in self.sites and self.inside(point, zone):
                nearest = min((math.dist(point[:2], h[0][:2]) for h in hunters), default=160)
                if nearest >= 30 and (cost >= 5 or not rotation):
                    ranked.append((cost - 0.30 * min(nearest, 160), node))
            for nxt in self.arena.links[node]:
                next_point = self.point(nxt)
                if math.dist(next_point[:2], zone["center"]) > zone["radius"]:
                    continue
                nearest = min((math.dist(next_point[:2], h[0][:2]) for h in hunters), default=160)
                penalty = 0.24 * max(0, 60 - nearest)
                new_cost = cost + step + penalty
                if new_cost < costs.get(nxt, math.inf):
                    costs[nxt], parents[nxt] = new_cost, node
                    heappush(queue, (new_cost, nxt))

        for _, node in sorted(ranked)[:10]:
            point = self.point(node)
            route_cost = costs[node]
            eta = min(3, route_cost / stats["walk_speed"])
            if hunters and not self.sheltered(point, stats, hunters, max(1.4, eta)):
                continue
            route = [point]
            while parents[node] is not None:
                node = parents[node]
                route.append(self.point(node))
            route.reverse()
            if any(math.dist(p[:2], h[0][:2]) < 27 for p in route for h in hunters):
                continue
            if route and hunters and route_cost > 28:
                midpoint = route[len(route) // 2]
                if any(math.dist(midpoint[:2], h[0][:2]) < 75 and self.visible(midpoint, stats, h)
                       for h in hunters):
                    continue
            return route
        return []

    def command(self, obs, enemies, waypoint):
        me, zone = obs["self"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        if not me["grounded"]:
            self.path = []
            return None
        now = obs["t"]
        margin = zone["radius"] - math.dist(pos[:2], zone["center"])
        inset = min(10, zone["next_radius"] * 0.45)
        remaining = math.dist(pos[:2], zone["next_center"]) - max(0, zone["next_radius"] - inset)
        deadline = zone["shrink_end"] - now
        emergency = not me["in_zone"] or (
            remaining > 3 and (margin < 13 or deadline < remaining / stats["boost_speed"] + 2))
        rotation = remaining > 3 and (
            emergency or deadline < remaining / (stats["walk_speed"] * 0.68) + 15
            or now >= zone["shrink_start"] - 8)
        stalled = self.was_moving and self.stall_origin is not None and math.dist(pos[:2], self.stall_origin) < 0.12
        self.stall = self.stall + 1 if stalled else 0
        self.stall_origin = tuple(pos[:2])
        if self.stall >= 5:
            self.path = []
            self.zone_waypoint = None
            if rotation:
                target = zone["next_center"]
                yaw = math.atan2(target[1] - pos[1], target[0] - pos[0])
            elif enemies:
                target = enemies[0]["pos"]
                yaw = math.atan2(pos[1] - target[1], pos[0] - target[0])
            else:
                yaw = me["aim"][0] + math.pi
            choices = []
            for i in range(16):
                angle = yaw + i * math.tau / 16
                dx, dy = math.cos(angle), math.sin(angle)
                travel, _ = self.arena.walk(pos, dx, dy, 11, stats)
                if travel >= 8:
                    choices.append((math.cos(angle - yaw), dx, dy))
            if choices:
                _, dx, dy = max(choices)
                self.recovery = ((pos[0] + dx * 8, pos[1] + dy * 8), now + 0.7)
            self.stall = 0
        if self.recovery is not None:
            point, until = self.recovery
            if now < until and self.walk(pos, point, stats):
                return self.steer(me, point, False)
            self.recovery = None
        if emergency:
            self.path = []
            center = zone["next_center"]
            if not self.walk(pos, center, stats):
                if now >= self.zone_replan_at or self.zone_waypoint is None or math.dist(pos[:2], self.zone_waypoint) < 2:
                    self.zone_waypoint = waypoint(pos, center, max(0, zone["next_radius"] - inset), stats)
                    self.zone_replan_at = now + 0.5
                center = self.zone_waypoint
            return self.steer(me, center, True)
        self.zone_waypoint = None
        wounded = me["hp"] < 0.96 * me["max_hp"]
        if not rotation and not wounded:
            self.path = []
            return None
        actors = enemies + [m for m in obs["mechs"] if m["kind"] == "npc"]
        nearby = [e for e in actors if math.dist(pos[:2], e["pos"][:2]) < 245]
        if not nearby and not rotation:
            self.path = []
            return None
        hunters = self.hunters(nearby)
        if len(hunters) > 5:
            self.path = []
            return None
        if self.path:
            destination = self.path[-1]
            arrived = math.dist(pos[:2], destination[:2]) < 2.5
            if (not self.inside(destination, zone)
                    or not self.sheltered(pos if arrived else destination, stats, hunters, 1.4)
                    or any(math.dist(pos[:2], h[0][:2]) < 25 for h in hunters)):
                self.path = []
                self.replan_at = now + 0.3
            elif arrived:
                if wounded and hunters and not obs["projectiles"]:
                    return [0.0, 0.0], False
                self.path = []
            else:
                while len(self.path) > 1 and math.dist(pos[:2], self.path[0][:2]) < 2.5:
                    self.path.pop(0)
                front = self.path[0]
                if (math.dist(front[:2], zone["center"]) <= zone["radius"]
                        and all(math.dist(front[:2], h[0][:2]) >= 27 for h in hunters)
                        and self.walk(pos, front, stats)):
                    return self.steer(me, front[:2], False)
                self.path = []
        if not self.path and now >= self.replan_at:
            self.replan_at = now + 1.2
            self.path = self.search(obs, hunters, rotation)
        if self.path:
            return self.steer(me, self.path[0][:2], False)
        return None

    def steer(self, me, point, emergency):
        pos, stats = me["pos"], me["stats"]
        distance = math.dist(pos[:2], point)
        if distance < 0.7:
            return [0.0, 0.0], False
        yaw = math.atan2(point[1] - pos[1], point[0] - pos[0])
        options = []
        for offset in (0, 0.55, -0.55, 1.05, -1.05, 1.57, -1.57):
            dx, dy = math.cos(yaw + offset), math.sin(yaw + offset)
            probe = min(12, distance) if not emergency else 12
            travel, _ = self.arena.walk(pos, dx, dy, probe, stats)
            if travel >= min(1.8, probe):
                options.append((math.cos(offset) * min(5, travel), dx, dy, travel))
        if not options:
            return None
        _, dx, dy, travel = max(options)
        boost = emergency and me["energy"] > 2 and distance > 9 and travel >= 11
        speed = stats["boost_speed"] if boost else stats["walk_speed"]
        scale = min(1, distance / (speed * 0.24), travel / (speed * 0.3))
        return [dx * scale, dy * scale], boost
