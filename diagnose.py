"""Action-responsive diagnostics using simplified movement and damage proxies."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import queue
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
    "pursuit": ((-70, 55), [(0, -60)], (0, 0), 135, 135),
    "pincer": ((0, 65), [(-55, -15), (55, -15), (0, 100)], (0, 35), 105, 105),
    "cover": ((-22, 0), [(-50, 27), (-75, -12)], (0, 0), 120, 100),
    "wounded_cover": ((18, 45), [(-45, 45)], (0, 0), 150, 140),
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
        if name == "wounded_cover":
            self.obs["self"]["hp"] = 900
            self.obs["mechs"][0]["vel"] = [4, 0, 0]
            self.obs["zone"].update(shrink_start=60, shrink_end=110)
        self.large = large
        self.last_damage = self.last_spend = self.last_shot = -10
        self.stall = 0
        self.metrics = Counter()

    def advance(self, act):
        obs, m = self.obs, self.metrics
        me, zone, targets = obs["self"], obs["zone"], obs["mechs"]
        now = obs["t"]
        stats = me["stats"]
        dx, dy = act.get("move", (0, 0))
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
        for actor in exposed:
            if (obs["tick"] // 2 + actor["id"]) % 6 == 0:
                yaw = bearing(me["pos"], actor["pos"])
                rear = abs(wrap(yaw - me["aim"][0])) > math.radians(stats["front_arc_deg"]) / 2
                damage = 18 * (stats["rear_mult"] if rear else 1)
                obs["events"].append({"type": "damaged", "amount": damage, "bearing": yaw,
                                      "by": actor["id"], "weapon": "gun", "rear": rear})
                m["hits_received"] += 1
                m["rear_hits"] += rear
        gun = me["weapons"][0]["stats"]
        if act.get("fire", [False, False])[0] and now - self.last_shot >= gun["cooldown"]:
            self.last_shot = now
            for actor in exposed:
                distance = math.dist(me["pos"], actor["pos"])
                if abs(wrap(me["aim"][0] - bearing(me["pos"], actor["pos"]))) < math.atan2(3, distance):
                    obs["events"].append({"type": "hit", "target": actor["id"], "amount": gun["damage"], "weapon": "gun"})
                    m["damage_out"] += gun["damage"]
                    break
        incoming = sum(e["amount"] for e in obs["events"] if e["type"] == "damaged")
        m["damage_in"] += incoming
        me["hp"] -= incoming
        if obs["events"]:
            self.last_damage = now
        elif now - self.last_damage >= stats["regen_delay"] and me["in_zone"]:
            me["hp"] = min(me["max_hp"], me["hp"] + stats["regen_per_s"] / 10)
        if me["hp"] <= 0:
            # Keep the deterministic stream alive and record losses separately.
            if not m["hp_resets"]:
                m["first_loss_seconds"] = round(now - 0.9, 2)
            m["hp_resets"] += 1
            me["hp"] = me["max_hp"]
        obs["tick"] += 2
        obs["t"] = round(now + 0.1, 2)


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


def run(command, message, schema, scenario, count):
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
    return "p50/p99/max=" + "/".join(f"{ordered[math.ceil(len(ordered) * q) - 1]:.2f}" for q in (0.5, 0.99, 1)) + " ms"


def main():
    parser = argparse.ArgumentParser()
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
        command = command or [sys.executable, "-B", "-u", str(Path(__file__).with_name("example_bot.py"))]
        message["map"]["dir"] = "/arena" if any("{map}" in arg for arg in command) else directory
        command = [item.replace("{map}", directory) for item in command]
        arena = Arena(root)
        times = []
        for name in args.scenario or (name for name in SCENARIOS if name != "squad"):
            scenario = Scenario(name, arena, message["config"])
            sample = run(command, message, schema, scenario, args.steps)
            times.extend(sample)
            scenario.metrics["hp_remaining"] = scenario.obs["self"]["hp"]
            scenario.metrics["resources_gained"] = scenario.obs["self"]["cubes"]
            scenario.metrics.setdefault("first_loss_seconds", args.steps / 10)
            metrics = " ".join(f"{key}={value:.1f}" if isinstance(value, float) else f"{key}={value}"
                               for key, value in sorted(scenario.metrics.items()))
            print(f"{name}: {summary(sample)} {metrics}", file=sys.stderr)
        budget = message["config"]["bot"]["tick_budget_ms"]
        late = sum(t > budget for t in times)
        print(f"{len(times)} actions {summary(times)} late={late} invalid=0 crashes=0", file=sys.stderr)
        return bool(late)


if __name__ == "__main__":
    sys.exit(main())
