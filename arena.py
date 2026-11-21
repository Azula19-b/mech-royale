"""Spatial index for the official arena's solid prisms and terrain."""
from array import array
from collections import defaultdict
import json
import math
from pathlib import Path
import sys


def edges(ring):
    points = list(zip(ring[::2], ring[1::2]))
    return tuple(zip(points, points[1:] + points[:1]))


def contains(x, y, segments):
    inside = False
    for (ax, ay), (bx, by) in segments:
        if (ay > y) != (by > y) and x < ax + (y - ay) * (bx - ax) / (by - ay):
            inside = not inside
    return inside


class Arena:
    cell = 24

    def __init__(self, map_dir):
        root = Path(map_dir) / "v2"
        world = json.loads((root / "world.json").read_text())
        bounds = world["bounds"]
        self.bounds = tuple(bounds[k] for k in ("minX", "minY", "maxX", "maxY"))
        self.terrain = world["terrain"]
        self.heights = array("h", (root / self.terrain["file"]).read_bytes())
        if sys.byteorder != "little":
            self.heights.byteswap()
        self.bins = defaultdict(list)
        for solid in world["prisms"]:
            ring = solid["ring"]
            if len(ring) < 6:
                continue
            left, right = min(ring[::2]), max(ring[::2])
            bottom, top = min(ring[1::2]), max(ring[1::2])
            item = (left, bottom, right, top, solid["base"], solid["top"],
                    edges(ring), tuple(edges(hole) for hole in solid["holes"]))
            for i in range(math.floor(left / self.cell), math.floor(right / self.cell) + 1):
                for j in range(math.floor(bottom / self.cell), math.floor(top / self.cell) + 1):
                    self.bins[i, j].append(item)

    def ground(self, x, y):
        t = self.terrain
        u = min(t["nx"] - 1.000001, max(0, (x - t["minX"]) / t["step"]))
        v = min(t["ny"] - 1.000001, max(0, (y - t["minY"]) / t["step"]))
        i, j = int(u), int(v)
        u, v = u - i, v - j
        n = t["nx"]
        k = j * n + i
        h = self.heights
        return ((h[k] * (1 - u) + h[k + 1] * u) * (1 - v)
                + (h[k + n] * (1 - u) + h[k + n + 1] * u) * v) * t["scale"]

    def solid(self, x, y, low, high):
        for left, bottom, right, top, base, roof, ring, holes in self.bins.get(
            (math.floor(x / self.cell), math.floor(y / self.cell)), ()
        ):
            if (left <= x <= right and bottom <= y <= top and low < roof and high > base
                    and contains(x, y, ring) and not any(contains(x, y, hole) for hole in holes)):
                return True
        return False

    def clear(self, origin, target, spacing=5):
        dx, dy, dz = (b - a for a, b in zip(origin, target))
        steps = max(1, math.ceil(math.sqrt(dx * dx + dy * dy + dz * dz) / spacing))
        ox, oy, oz = origin
        for i in range(1, steps + 1):
            fraction = i / steps
            x, y, z = ox + dx * fraction, oy + dy * fraction, oz + dz * fraction
            if z < self.ground(x, y) or self.solid(x, y, z - 0.01, z + 0.01):
                return False
        return True

    def edge(self, x, y):
        left, bottom, right, top = self.bounds
        return min(x - left, y - bottom, right - x, top - y)

    def walk(self, start, dx, dy, distance, stats):
        """Return travel clear of solids and the last sampled walking height."""
        x, y, z = start
        radius = stats["radius"]
        step = stats["step_up"]
        slope = math.tan(math.radians(stats["max_slope_deg"]))
        previous_z = z
        count = max(1, math.ceil(distance / 3))
        interval = distance / count
        for i in range(1, count + 1):
            px, py = x + dx * interval * i, y + dy * interval * i
            if self.edge(px, py) < radius:
                return interval * (i - 1), previous_z
            ground = self.ground(px, py)
            if ground - previous_z > max(step, slope * interval):
                return interval * (i - 1), previous_z
            if previous_z - ground > step and self.solid(px, py, previous_z - 0.4, previous_z - 0.05):
                ground = previous_z
            for sx, sy in ((px, py), (px - dy * radius, py + dx * radius),
                           (px + dy * radius, py - dx * radius),
                           (px + dx * radius, py + dy * radius)):
                if self.solid(sx, sy, ground + step, ground + stats["height"]):
                    return interval * (i - 1), previous_z
            previous_z = ground
        return distance, previous_z

    def build_navigation(self, stats):
        """Build a sparse ground graph once, including clearance under bridges."""
        self.spacing = 8
        left, bottom, right, top = self.bounds
        self.nodes = {}
        for i in range(math.ceil(left / self.spacing), math.floor(right / self.spacing) + 1):
            for j in range(math.ceil(bottom / self.spacing), math.floor(top / self.spacing) + 1):
                x, y = i * self.spacing, j * self.spacing
                z = self.ground(x, y)
                if self.edge(x, y) > stats["radius"] and not self.solid(x, y, z + stats["step_up"], z + stats["height"]):
                    self.nodes[i, j] = z
        self.links = {}
        for (i, j), z in self.nodes.items():
            x, y = i * self.spacing, j * self.spacing
            links = []
            for a, b in ((1, 0), (0, 1), (-1, 0), (0, -1)):
                node = i + a, j + b
                if node in self.nodes and self.walk((x, y, z), a, b, self.spacing, stats)[0] == self.spacing:
                    links.append(node)
            self.links[i, j] = links

    def waypoint(self, pos, center, radius, stats):
        """Bounded A* toward the safe circle, with an inward partial route if needed."""
        from heapq import heappop, heappush

        spacing = self.spacing
        i, j = round(pos[0] / spacing), round(pos[1] / spacing)
        starts = []
        for a in range(i - 1, i + 2):
            for b in range(j - 1, j + 2):
                if (a, b) in self.nodes:
                    dx, dy = a * spacing - pos[0], b * spacing - pos[1]
                    distance = math.hypot(dx, dy)
                    if distance < 0.1 or self.walk(pos, dx / distance, dy / distance, distance, stats)[0] == distance:
                        starts.append((distance, (a, b)))
        if not starts:
            return center

        def remaining(node):
            x, y = node
            return max(0, math.hypot(x * spacing - center[0], y * spacing - center[1]) - radius)

        queue, costs, parents = [], {}, {}
        for cost, node in starts:
            heappush(queue, (cost + remaining(node), cost, node))
            costs[node] = cost
            parents[node] = None
        best = min((node for _, node in starts), key=remaining)
        for _ in range(350):
            if not queue:
                break
            _, cost, node = heappop(queue)
            if cost != costs[node]:
                continue
            if remaining(node) < remaining(best):
                best = node
            if remaining(node) == 0:
                break
            for nxt in self.links[node]:
                new_cost = cost + spacing
                if new_cost < costs.get(nxt, math.inf):
                    costs[nxt] = new_cost
                    parents[nxt] = node
                    heappush(queue, (new_cost + remaining(nxt), new_cost, nxt))
        trail = [best]
        while parents[trail[-1]] is not None:
            trail.append(parents[trail[-1]])
        for a, b in reversed(trail):
            point = a * spacing, b * spacing
            if math.dist(pos[:2], point) > 4:
                return point
        return center
