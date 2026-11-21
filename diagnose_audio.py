"""Paired, action-responsive audio diagnostics using V5's simplified damage proxy."""
import json
import math
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter

from arena import Arena
from diagnose import SCENARIOS, Scenario, make_map, point3
from example_bot import Bot, bearing, wrap
from test_audio import baseline_module, sound
from test_phoenix import hello

# Start, hunter starts, next zone center, current radius, next radius.
CASES = {
    "east_hunter": ((-90, -70), [(-36, -70)], (-60, -70), 142, 142),
    "concentrated": ((-90, -70), [(-37, -68), (-38, -73)], (-60, -70), 142, 142),
    "opposing": ((-75, -70), [(-23, -70), (-127, -70)], (-45, -70), 142, 142),
    "far_gun": ((-125, -70), [(80, -70)], (-80, -70), 142, 142),
    "sudden": ((-90, -70), [(-42, -48), (-45, -47)], (-60, -70), 142, 142),
    "zone": SCENARIOS["evacuation"],
    "edge": ((-144, -80), [(-92, -80), (-93, -83)], (-115, -80), 135, 135),
    "passage": ((-18, 7), [(-18, -46), (-20, -49)], (-18, -65), 140, 140),
    "route_closes": ((-90, -70), [(-16, -70), (-18, -73)], (-60, -70), 142, 142),
}


class MeasuredBot(Bot):
    diversions = 0

    def audio_heading(self, obs, routes, baseline):
        chosen = super().audio_heading(obs, routes, baseline)
        self.diversions += chosen != baseline[0]
        return chosen


def cues(name, scenario, hunters, step):
    pos = scenario.obs["self"]["pos"]
    if step % 6 != 0 or (name in ("sudden", "route_closes") and step < 12):
        return []
    heard = []
    for index, hunter in enumerate(hunters):
        target = hunter["pos"]
        distance = math.dist(pos, target)
        yaw = bearing(pos, target)
        band = "near" if distance < 60 else "mid" if distance < 160 else "far"
        if distance < 320:
            heard.append(sound("gun", yaw, band))
        if distance < 90 and name not in ("east_hunter", "far_gun"):
            heard.append(sound("boost", yaw, band))
        if distance < 380 and index == 1:
            heard.append(sound("laser", yaw, band))
    return heard


def run(bot, name, arena, config):
    scenario = Scenario(name, arena, config)
    obs, metrics = scenario.obs, scenario.metrics
    hunters = obs["mechs"]
    if name != "zone":
        obs["zone"].update(shrink_start=60, shrink_end=110)
    headings, distances, audio_alignment, latencies = [], [], [], []
    last_turn = 0
    for step in range(240):
        pos = obs["self"]["pos"]
        if name == "route_closes" and step == 12:
            for hunter in hunters:
                hunter["pos"][0] -= 19  # A timed arrival into the previous route.
        obs["sounds"] = cues(name, scenario, hunters, step)
        # Constrain sight for this diagnostic to exercise the pre-contact interval.
        obs["mechs"] = [
            hunter for hunter in hunters if math.dist(pos, hunter["pos"]) < 38
            and arena.clear(point3(pos, 8), point3(hunter["pos"], 7))
        ]
        if obs["mechs"]:
            distances.append(min(math.dist(pos, hunter["pos"]) for hunter in obs["mechs"]))
        start = perf_counter()
        act = bot.act(obs)
        latencies.append((perf_counter() - start) * 1000)
        if math.hypot(*act["move"]) > 0.5:
            yaw = math.atan2(act["move"][1], act["move"][0])
            if headings:
                turn = wrap(yaw - headings[-1])
                sign = (1 if turn > 0 else -1) if abs(turn) > 0.05 else 0
                metrics["turns"] += bool(sign)
                metrics["oscillations"] += sign * last_turn < 0
                last_turn = sign
            headings.append(yaw)
            if obs["sounds"]:
                audio_alignment.append(mean(math.cos(yaw - cue["bearing"]) for cue in obs["sounds"]))
        obs["mechs"] = hunters
        scenario.advance(act)
        if metrics["hp_resets"]:
            break
    result = {key: round(metrics[key], 2) for key in (
        "turns", "oscillations", "collision", "stuck", "zone_out", "evacuation_failure",
        "damage_in", "damage_out", "rear_hits", "boost_energy",
    )}
    result.update(
        direct_audio_diversions=getattr(bot, "diversions", 0),
        alive_s=round((step + 1) / 10, 1),
        mean_confirmed_range=round(mean(distances), 2) if distances else None,
        mean_cue_alignment=round(mean(audio_alignment), 3) if audio_alignment else None,
        decision_p99_ms=round(sorted(latencies)[math.ceil(len(latencies) * .99) - 1], 3),
    )
    return result


def add_passage(directory):
    world_path = Path(directory) / "v2/world.json"
    world = json.loads(world_path.read_text())
    for ring in ([-36, -40, -25, -40, -25, 40, -36, 40],
                 [-25, 16, -11, 16, -11, 30, -25, 30]):
        world["prisms"].append(dict(kind="building", base=0, top=22, holes=[], ring=ring))
    world_path.write_text(json.dumps(world))


def main():
    message = hello()
    baseline = baseline_module()
    SCENARIOS.update(CASES)
    with TemporaryDirectory() as directory:
        for name in CASES:
            with TemporaryDirectory(dir=directory) as map_dir:
                make_map(Path(map_dir))
                if name == "passage":
                    add_passage(map_dir)
                message["map"]["dir"] = map_dir
                arena = Arena(map_dir)
                print(json.dumps({
                    "scenario": name,
                    "V5": run(baseline.Bot(message), name, arena, message["config"]),
                    "V9-A": run(MeasuredBot(message), name, arena, message["config"]),
                }))


if __name__ == "__main__":
    main()
