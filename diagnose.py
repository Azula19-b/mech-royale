"""Action-responsive diagnostics using simplified movement and damage proxies."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import queue
import random
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Thread
from time import perf_counter

from arena import Arena
from example_bot import bearing, wrap
from test_phoenix import KIT, enemy, hello, observation

sys.path.insert(0, str(KIT / "check"))
from check import Bot as ProtocolBot, Schema


class DiagnosticBot(ProtocolBot):
    def __init__(self, command, stderr):
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, bufsize=0)
        self.lines = queue.Queue()
        Thread(target=self._read, daemon=True).start()

SCENARIOS = {
    "duel": ((-32, 60), [(38, 60)], (0, 60), 120, 105),
    "pursuit": ((-70, 55), [(0, -60)], (0, 0), 135, 135),
    "pincer": ((0, 65), [(-55, -15), (55, -15), (0, 100)], (0, 35), 105, 105),
    "cover": ((-22, 0), [(-50, 27), (-75, -12)], (0, 0), 120, 100),
    "evacuation": ((-90, 0), [(-145, -24)], (45, 0), 150, 24),
    "edge": ((-135, 105), [(-95, 0), (25, -50)], (0, 0), 155, 95),
    "squad": ((0, 66), [(-70, -25), (-80, 0), (-83, 26), (-85, 52), (-77, 72),
                        (-68, 100), (-41, 120), (-22, 125), (7, 124), (33, 116),
                        (60, 103), (80, 83), (76, 57), (79, 29), (77, 3)], (0, 25), 140, 95),
}


def make_map(root):
    v2 = root / "v2"
    v2.mkdir()
    minimum, maximum = -160, 160
    grid = maximum - minimum + 1
    terrain = {"minX": minimum, "minY": minimum, "nx": grid, "ny": grid,
               "scale": 0.01, "step": 1, "file": "terrain.i16"}
    walls = [(-11, -40, 11, 40), (-65, 4, -45, 48), (43, -50, 61, -3),
             (-118, 71, -82, 93)]
    prisms = [{"kind": "building", "base": 0, "top": 22, "holes": [],
               "ring": [x, y, xx, y, xx, yy, x, yy]} for x, y, xx, yy in walls]
    bounds = {"minX": minimum, "maxX": maximum, "minY": minimum, "maxY": maximum}
    (v2 / "world.json").write_text(json.dumps({"bounds": bounds, "terrain": terrain, "prisms": prisms}))
    (v2 / "terrain.i16").write_bytes(bytes(2 * grid * grid))


def point3(position, height):
    return (*position[:2], position[2] + height)


class Scenario:
    def __init__(self, name, arena, config):
        start, attackers, center, large, small = SCENARIOS[name]
        self.arena, self.config = arena, config
        self.obs = observation(config)
        self.obs.update(type="obs", alive=16, beams=[], crates=[])
        self.obs["self"].update(id=0, pos=[*start, 0], upgrades=[])
        self.obs["mechs"] = [enemy(x, y, identifier=i) for i, (x, y) in enumerate(attackers, 1)]
        self.obs["zone"].update(center=list(center), next_center=list(center), radius=large,
                                next_radius=small, shrink_start=4, shrink_end=15)
        self.initial_attackers = tuple(attackers)
        self.large = large
        self.last_damage = self.last_spend = -10
        self.stall = 0
        self.heading = None
        self.shots = []
        self.random = {owner: random.Random(737 + owner) for owner in range(16)}
        self.dead = False
        self.metrics = Counter()

    def advance(self, act):
        obs, m = self.obs, self.metrics
        me, zone, targets = obs["self"], obs["zone"], obs["mechs"]
        now = obs["t"]
        stats = me["stats"]
        dx, dy = act.get("move", (0, 0))
        if math.hypot(dx, dy) > 0.1:
            yaw = math.atan2(dy, dx)
            m["heading_reversals"] += self.heading is not None and abs(wrap(yaw - self.heading)) > math.pi / 2
            self.heading = yaw
        scale = max(1, math.hypot(dx, dy))
        dx, dy = dx / scale, dy / scale
        m["jump_commands"] += bool(act.get("jump"))
        boost = act.get("boost") and me["energy"] > 0
        speed = stats["boost_speed"] if boost else stats["walk_speed"]
        vx, vy = me["vel"][:2]
        ax, ay = dx * speed - vx, dy * speed - vy
        scale = max(1, math.hypot(ax, ay) / (stats["ground_accel"] * 0.1))
        vx, vy = vx + ax / scale, vy + ay / scale
        distance = math.hypot(vx, vy) / 10
        x, y, z = me["pos"]
        traveled = distance
        if distance:
            traveled, z = self.arena.walk(me["pos"], vx / (distance * 10), vy / (distance * 10), distance, stats)
            ratio = traveled / distance
            me["pos"] = [x + vx * 0.1 * ratio, y + vy * 0.1 * ratio, z]
            me["vel"] = [vx * ratio, vy * ratio, 0]
        m["distance_traveled"] += traveled
        clearance = self.arena.edge(*me["pos"][:2]) - stats["radius"]
        m["minimum_boundary_clearance"] = min(m.get("minimum_boundary_clearance", math.inf), clearance)
        m["collision"] += traveled + 0.001 < distance
        self.stall = self.stall + 1 if traveled < 0.08 and math.hypot(dx, dy) > 0.5 else 0
        m["stuck"] += self.stall >= 10
        if boost:
            drained = min(me["energy"], stats["boost_drain"] / 10)
            me["energy"] -= drained
            self.last_spend = now
            m["boost_energy"] += drained
            if traveled < stats["walk_speed"] / 10:
                m["wasted_boost_energy"] += drained
        elif now - self.last_spend >= stats["energy_regen_delay"]:
            me["energy"] = min(me["max_energy"], me["energy"] + stats["energy_regen"] / 10)

        if "aim" in act:
            for i, (rate, delta) in enumerate(zip(("turn_rate", "pitch_rate"),
                    (wrap(act["aim"][0] - me["aim"][0]), act["aim"][1] - me["aim"][1]))):
                limit = stats[rate] / 10
                me["aim"][i] += max(-limit, min(limit, delta))
            me["aim"][0] = wrap(me["aim"][0])

        fraction = min(1, max(0, (now - zone["shrink_start"]) / (zone["shrink_end"] - zone["shrink_start"])))
        zone["radius"] = self.large + fraction * (zone["next_radius"] - self.large)
        me["in_zone"] = math.dist(me["pos"][:2], zone["center"]) <= zone["radius"]
        obs["events"] = []
        if not me["in_zone"]:
            damage = zone["dps"] / 10
            obs["events"].append({"type": "damaged", "amount": damage, "weapon": "zone", "bearing": None, "by": None})
            m["zone_out"] += 1
            m["zone_damage"] += damage
            m["evacuation_failure"] += fraction == 1

        exposed = []
        for actor in targets:
            distance = math.dist(me["pos"], actor["pos"])
            yaw = bearing(actor["pos"], me["pos"])
            forward = math.cos(yaw), math.sin(yaw)
            # Maintain a loose firing ring. Slide at obstructions using perpendicular alternatives.
            if distance > 40:
                for offset in (0, -1.15, 1.15):
                    vx, vy = 8 * math.cos(yaw + offset), 8 * math.sin(yaw + offset)
                    if self.arena.walk(actor["pos"], vx / 8, vy / 8, 0.8, stats)[0] >= 0.8:
                        actor["pos"][0] += vx / 10
                        actor["pos"][1] += vy / 10
                        actor["vel"] = [vx, vy, 0]
                        break
                else:
                    actor["vel"] = [0, 0, 0]
            else:
                actor["vel"] = [0, 0, 0]
            actor["aim"] = [math.atan2(forward[1], forward[0]), 0]
            if distance < 100 and self.arena.clear(point3(actor["pos"], 7), point3(me["pos"], 4.5)):
                exposed.append(actor)
        m["covered"] += bool(targets) and not exposed
        m["threatened"] += bool(exposed)
        m["moved_threatened"] += bool(exposed) and traveled > 0.15
        m["stationary_exposed"] += bool(exposed) and traveled <= 0.15
        gun = me["weapons"][0]
        w = gun["stats"]
        gun["cooldown"] = max(0, gun["cooldown"] - 0.1)
        if gun["reload"]:
            gun["reload"] = max(0, gun["reload"] - 0.1)
            if gun["reload"] < 1e-8:
                gun.update(reload=0, ammo=w["mag"])
        origin = point3(me["pos"], stats["muzzle_height"])
        opportunity = any(math.dist(origin, point3(actor["pos"], stats["height"] / 2)) < w["range"]
                          and self.arena.clear(origin, point3(actor["pos"], stats["height"] / 2), spacing=2)
                          for actor in targets)
        fire = act.get("fire", [False, False])[0]
        m["fire_commands"] += fire
        m["opportunity_ticks"] += opportunity
        m["unfired_ready_ticks"] += opportunity and not gun["cooldown"] and not gun["reload"] and not fire
        if act.get("reload") and gun["ammo"] < w["mag"] and not gun["reload"]:
            gun["reload"] = w["reload"]
        if fire and not gun["cooldown"] and not gun["reload"] and gun["ammo"]:
            self.launch(origin, me["aim"], w, None, w["damage"])
            m["shots"] += 1
            gun["ammo"] -= 1
            gun["cooldown"] = w["cooldown"]
            if not gun["ammo"]:
                gun["reload"] = w["reload"]
        for actor in exposed:
            if (obs["tick"] // 2 + actor["id"]) % 6 == 0:
                origin = point3(actor["pos"], stats["muzzle_height"])
                flight = math.dist(actor["pos"], me["pos"]) / w["speed"]
                point = tuple(p + v * flight for p, v in zip(point3(me["pos"], stats["height"] / 2), me["vel"]))
                pitch = math.atan2(point[2] - origin[2], math.dist(point[:2], origin[:2]))
                self.launch(origin, (bearing(origin, point), pitch), w, actor["id"], w["damage"] * 0.40)
        self.advance_projectiles(me, targets, obs["events"])
        incoming = sum(e["amount"] for e in obs["events"] if e["type"] == "damaged")
        m["damage_in"] += incoming
        me["hp"] -= incoming
        if any(e["type"] in ("damaged", "hit") for e in obs["events"]):
            m["regen_interruptions"] += (me["hp"] < me["max_hp"]
                                          and now - self.last_damage >= stats["regen_delay"] - 0.5)
            self.last_damage = now
        elif now - self.last_damage >= stats["regen_delay"] and me["in_zone"]:
            me["hp"] = min(me["max_hp"], me["hp"] + stats["regen_per_s"] / 10)
        m["survival_s"] = now - 1 + 0.1
        self.dead = me["hp"] <= 0
        m["deaths"] += self.dead
        obs["tick"] += 2
        obs["t"] = round(now + 0.1, 2)

    def launch(self, origin, aim, weapon, owner, damage):
        spread = math.radians(weapon["spread_deg"])
        yaw, pitch = (axis + self.random[owner or 0].gauss(0, spread) for axis in aim)
        ray = math.cos(yaw) * math.cos(pitch), math.sin(yaw) * math.cos(pitch), math.sin(pitch)
        offset = self.config["mech"]["radius"] + 0.5
        muzzle = tuple(p + offset * d for p, d in zip(origin, ray))
        if self.arena.clear(origin, muzzle, spacing=1):
            velocity = tuple(d * weapon["speed"] for d in ray)
            self.shots.append((muzzle, velocity, weapon["range"], owner, damage))

    def advance_projectiles(self, me, targets, events):
        stats = me["stats"]
        m = self.metrics
        survivors = []
        for position, velocity, remaining, owner, damage in self.shots:
            speed = math.hypot(*velocity)
            duration = min(0.1, remaining / speed)
            end = tuple(p + v * duration for p, v in zip(position, velocity))
            contact = None
            for actor in ([me] if owner is not None else targets):
                if actor["hp"] <= 0:
                    continue
                x, y, z = actor["pos"]
                vx, vy, vz = velocity
                time = ((x - position[0]) * vx + (y - position[1]) * vy) / max(1e-6, vx * vx + vy * vy)
                time = max(0, min(duration, time))
                point = tuple(p + v * time for p, v in zip(position, velocity))
                if (math.hypot(point[0] - x, point[1] - y) <= stats["radius"] + self.config["projectile"]["gun_radius"]
                        and z <= point[2] <= z + stats["height"]
                        and (contact is None or time < contact[0])):
                    contact = time, point, actor
            if contact and self.arena.clear(position, contact[1], spacing=1):
                actor = contact[2]
                direction = math.atan2(-velocity[1], -velocity[0])
                rear = abs(wrap(direction - actor["aim"][0])) > math.radians(stats["front_arc_deg"]) / 2
                damage *= stats["rear_mult"] if rear else 1
                if owner is not None:
                    m["hits_received"] += 1
                    m["rear_hits"] += rear
                    m["rear_damage"] += damage if rear else 0
                    events.append({"type": "damaged", "by": owner, "amount": damage,
                                   "weapon": "gun", "bearing": direction, "rear": rear})
                else:
                    m["hits"] += 1
                    m["damage_out"] += min(actor["hp"], damage)
                    actor["hp"] -= damage
                    events.append({"type": "hit", "target": actor["id"], "amount": damage, "weapon": "gun"})
                    if actor["hp"] <= 0:
                        m["kills"] += 1
                        if m["kills"] == 1:
                            m["first_kill_s"] = self.obs["t"] - 1 + 0.1
                        events.append({"type": "kill", "target": actor["id"]})
            elif remaining > speed * duration and self.arena.clear(position, end, spacing=1):
                survivors.append((end, velocity, remaining - speed * duration, owner, damage))
        targets[:] = [actor for actor in targets if actor["hp"] > 0]
        self.obs["alive"] = len(targets) + 1
        self.shots = survivors
        self.obs["projectiles"] = [{"id": i, "kind": "gun", "pos": list(p), "vel": list(v)}
                                   for i, (p, v, _, owner, _) in enumerate(survivors) if owner is not None]


def read(bot, seconds, limit):
    result = bot.next(perf_counter() + seconds)
    if result is None or result[1] is None:
        raise RuntimeError("bot timed out or closed stdout")
    at, raw = result
    if len(raw) > limit:
        raise RuntimeError("oversized protocol line")
    return at, json.loads(raw, parse_constant=lambda value: float(value))


def valid_numbers(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(map(valid_numbers, value))
    if isinstance(value, dict):
        return all(map(valid_numbers, value.values()))
    return True


def run(command, message, schema, scenario, count, stress=False):
    stderr = TemporaryFile()
    bot = DiagnosticBot(command, stderr)
    times = []
    try:
        config = message["config"]["bot"]
        limit = config["max_line_bytes"]
        bot.send(message)
        _, ready = read(bot, config["hello_budget_ms"] / 1000, limit)
        if schema.errors(ready, "Ready"):
            raise RuntimeError(f"invalid ready: {ready!r}")
        for _ in range(count):
            start = perf_counter()
            bot.send(scenario.obs)
            at, act = read(bot, 2, limit)
            errors = schema.errors(act, "Action")
            if errors or act.get("tick") != scenario.obs["tick"] or not valid_numbers(act):
                raise RuntimeError(f"invalid action for tick {scenario.obs['tick']}: {act!r} {errors}")
            times.append((at - start) * 1000)
            scenario.advance(act)
            if scenario.dead and not stress:
                break
            if stress:
                # Replenish attackers for sustained observation load. Timing only.
                actors = scenario.obs["mechs"]
                present = {actor["id"] for actor in actors}
                for i, (x, y) in enumerate(scenario.initial_attackers, 1):
                    if i not in present:
                        actors.append(enemy(x, y, identifier=i))
                scenario.obs["alive"] = len(actors) + 1
                if scenario.dead:
                    scenario.obs["self"]["hp"] = scenario.obs["self"]["max_hp"]
                    scenario.dead = False
        bot.send({"type": "end", "place": 1, "kills": 0})
        bot.proc.stdin.close()
        if bot.proc.wait(timeout=5):
            raise RuntimeError("bot exited with nonzero status")
    finally:
        if bot.proc.poll() is None:
            bot.proc.kill()
            bot.proc.wait()
        stderr.seek(0)
        logs = stderr.read().decode(errors="replace")
        stderr.close()
        if logs:
            print(logs, file=sys.stderr, end="")
    if any(word in logs for word in ("Traceback", "act() failed", "unserializable action")):
        raise RuntimeError("recoverable bot exception in diagnostic stream")
    return times


def summary(times):
    ordered = sorted(times)
    return "p50/p90/p99/max=" + "/".join(f"{ordered[math.ceil(len(ordered) * q) - 1]:.2f}" for q in (0.5, 0.9, 0.99, 1)) + " ms"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stress", action="store_true", help="Reset HP after death for timing-only stress")
    parser.add_argument("--steps", default=480, type=int)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.steps < 1:
        parser.error("--steps must be positive")
    message = hello()
    schema = Schema(KIT / "mechfight.schema.json")
    with TemporaryDirectory(prefix="phoenix-diagnostic-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        make_map(root)
        command = args.command
        if command[:1] == ["--"]:
            command = command[1:]
        command = command or [sys.executable, "-B", "-u", str(Path(__file__).with_name("hunter.py"))]
        message["map"]["dir"] = "/arena" if any("{map}" in arg for arg in command) else directory
        command = [item.replace("{map}", directory) for item in command]
        arena = Arena(root)
        times = []
        for name in args.scenario or (name for name in SCENARIOS if name != "squad"):
            scenario = Scenario(name, arena, message["config"])
            sample = run(command, message, schema, scenario, args.steps, args.stress)
            times.extend(sample)
            metrics = " ".join(f"{key}={value:.1f}" if isinstance(value, float) else f"{key}={value}"
                               for key, value in sorted(scenario.metrics.items()))
            details = "HP reset and attackers replenished (timing only)" if args.stress else metrics
            print(f"{name}: {summary(sample)} {details}", file=sys.stderr)
        budget = message["config"]["bot"]["tick_budget_ms"]
        late = sum(t > budget for t in times)
        print(f"{len(times)} actions {summary(times)} late={late} invalid=0 crashes=0", file=sys.stderr)
        return bool(late)


if __name__ == "__main__":
    sys.exit(main())
