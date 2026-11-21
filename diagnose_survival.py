"""Paired survival trials and bounded pre-death traces for Phoenix."""
import argparse
from collections import Counter, deque
from dataclasses import dataclass
import json
import math
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from time import perf_counter

from diagnose import Arena, KIT, SCENARIOS, Scenario, Schema, enemy, hello, make_map, summary, valid_numbers
from example_bot import bearing, wrap


@dataclass(frozen=True)
class Fight:
    name: str
    start: tuple
    enemies: tuple
    hp: int = 1400
    weak: bool = False
    zone: tuple = ((0, 32), 142, 112)


FIGHTS = (
    Fight("duel", (-32, 60), ((38, 60),), zone=((0, 60), 120, 105)),
    Fight("pair", (-40, 62), ((24, 55), (34, 83))),
    Fight("pincer", (0, 65), ((-55, -15), (67, -15), (0, 100)), zone=((0, 35), 105, 105)),
    Fight("reinforcements", (-22, 0), ((-55, -7), (-75, 43), (26, -31)), 900, True),
    Fight("cover_flank", (-22, 0), ((-72, 29), (-76, -12)), 600),
    Fight("weak_threat", (-20, 60), ((23, 60),), 950, True),
    Fight("recovery", (-22, 20), ((-75, 26), (-72, 46)), 460),
    Fight("edge_collapse", (-150, 22), ((-130, 36), (-112, 17), (-120, -10)), 600,
          zone=((-84, 22), 92, 82)),
    Fight("evacuation", (-90, 0), ((-145, -24), (-123, 58), (-26, 64)), 820,
          zone=((45, 0), 150, 24)),
    Fight("squad", (0, 66), tuple(SCENARIOS["squad"][1]), zone=((0, 25), 140, 95)),
)
DEVELOPMENT = (0, 17, 43, 71)
HOLDOUT = (109, 137, 179, 223, 277)


def configure_map(root, phase):
    make_map(root)
    if phase == "holdout":
        path = root / "v2/world.json"
        world = json.loads(path.read_text())
        # Shift the two peripheral buildings. Keep the central cover and valid spawn points.
        for prism in world["prisms"][2:]:
            prism["ring"] = [value + (4 if i % 2 else 0) for i, value in enumerate(prism["ring"])]
        path.write_text(json.dumps(world))


def build_fight(fight, arena, config, seed):
    sim = Scenario("duel", arena, config)
    obs, rng = sim.obs, Random(seed)
    me = obs["self"]

    def position(pos):
        x, y = pos
        if seed:
            x, y = x + rng.uniform(-1.4, 1.4), y + rng.uniform(-1.4, 1.4)
        return [x, y, 0]

    me.update(pos=position(fight.start), hp=fight.hp)
    if fight.name == "recovery":
        sim.last_damage = -2.8
    obs["mechs"] = [enemy(*position(p)[:2], identifier=i) for i, p in enumerate(fight.enemies, 1)]
    obs["alive"] = len(obs["mechs"]) + 1
    for actor in [me, *obs["mechs"]]:
        x, y, z = actor["pos"]
        if arena.edge(x, y) < me["stats"]["radius"] or arena.solid(x, y, z + 1.2, z + 9):
            raise ValueError(f"illegal spawn: {fight.name}, seed {seed}, {actor['pos']}")
    if fight.weak:
        obs["mechs"][0]["hp"] = 40
        me["aim"][0] = bearing(me["pos"], obs["mechs"][0]["pos"])
    if fight.name not in ("duel", "pincer", "squad"):
        for actor in obs["mechs"]:
            actor["aim"][0] = bearing(actor["pos"], me["pos"])
    center, large, small = fight.zone
    obs["zone"].update(center=list(center), next_center=list(center), radius=large,
                       next_radius=small, shrink_start=4, shrink_end=15)
    me["in_zone"] = math.dist(me["pos"][:2], center) <= large
    sim.large = large
    sim.random = {i: Random(seed * 31 + i + 737) for i in range(16)}
    return sim


def snapshot(sim, bot, action):
    obs, arena = sim.obs, sim.arena
    me, zone, stats = obs["self"], obs["zone"], obs["self"]["stats"]
    x, y, z = me["pos"]
    binned = sorted(obs["mechs"], key=lambda actor: actor["id"])
    eye = (x, y, z + stats["height"] / 2)
    lanes = [actor["id"] for actor in binned if math.dist(actor["pos"], me["pos"]) < 100 and
             arena.clear((*actor["pos"][:2], actor["pos"][2] + 7), eye)]
    corridors = []
    for i in range(12):
        yaw = i * math.tau / 12
        length, _ = arena.walk(me["pos"], math.cos(yaw), math.sin(yaw), 12, stats)
        corridors.append(round(length, 2))
    selected = bot.fire_control.selected
    return dict(t=obs["t"], hp=me["hp"], pos=me["pos"][:], velocity=me["vel"][:],
                state=bot.state, engagement=getattr(bot, "engagement", None),
                target=selected.track.actor["id"] if selected else None,
                enemies=[dict(id=a["id"], distance=round(math.dist(a["pos"], me["pos"]), 2),
                              weapons=a["weapons"], hp=a["hp"], bearing=bearing(me["pos"], a["pos"]))
                         for a in binned],
                live_lanes=lanes, available_exits=sum(length >= 12 for length in corridors),
                corridor_distances=corridors, boundary=arena.edge(x, y), in_zone=me["in_zone"],
                zone_margin=zone["radius"] - math.dist((x, y), zone["center"]),
                energy=me["energy"], move=action["move"], boost=action["boost"],
                fire=action.get("fire", [False, False]), armor_yaw=me["aim"][0],
                aim_command=action["aim"], heading=bot.heading)


def classify(trace):
    damage = Counter()
    rear = 0
    for row in trace:
        for event in row["events"]:
            if event["type"] == "damaged":
                damage[event["weapon"]] += event["amount"]
                rear += event["amount"] if event.get("rear") else 0
    total = sum(damage.values())
    share_crossfire = sum(len(row["live_lanes"]) >= 2 for row in trace) / len(trace)
    share_exposure = sum(bool(row["live_lanes"]) for row in trace) / len(trace)
    late = list(trace)[-15:]
    blocked = sum(row["collision"] for row in trace)
    exhausted = sum(row["energy"] < 8 and row["state"] in ("EVADE", "EMERGENCY_ZONE") for row in late)
    factors = []
    if share_crossfire >= 0.50:
        factors.append("crossfire")
    if share_exposure >= 0.70:
        factors.append("sustained exposure")
    if (sum(row["state"] == "EVADE" for row in late) >= 10 and
            sum(len(row["live_lanes"]) >= 2 for row in late) >= 10):
        factors.append("disengagement did not break contact")
    if rear >= 0.25 * total:
        factors.append("rear exposure")
    if blocked >= 8 or max(row["available_exits"] for row in late) <= 2:
        factors.append("geometry constraint")
    if exhausted >= 10:
        factors.append("low energy late")
    if damage["zone"] > 0.25 * total:
        factors.append("zone damage")
    return dict(factors=factors, damage_by_weapon=damage, rear_damage=rear,
                crossfire_fraction=share_crossfire, exposed_fraction=share_exposure, collisions=blocked,
                final_damage_events=[e for e in trace[-1]["events"] if e["type"] == "damaged"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("development", "holdout"), default="development")
    parser.add_argument("--mode", choices=("v10", "a", "b", "ab"), required=True)
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--traces", type=Path)
    parser.add_argument("--fights", nargs="+", choices=tuple(f.name for f in FIGHTS))
    args = parser.parse_args()
    if args.runtime:
        import sys
        from importlib.util import module_from_spec, spec_from_file_location
        for name in ("arena", "combat", "example_bot", "intelligence", "hunter_combat", "hunter_movement", "hunter"):
            spec = spec_from_file_location(name, args.runtime / (name + ".py"))
            module = module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
    from hunter import Bot
    message = hello()
    schema = Schema(KIT / "mechfight.schema.json")
    seeds = DEVELOPMENT if args.phase == "development" else HOLDOUT
    rows, times, deaths = [], [], []
    with TemporaryDirectory() as directory:
        configure_map(Path(directory), args.phase)
        message["map"]["dir"] = directory
        template = Bot(message)
        from unittest.mock import patch
        for fight in FIGHTS:
            if args.fights and fight.name not in args.fights:
                continue
            for seed in seeds:
                with patch("example_bot.Arena", return_value=template.arena), patch.object(
                        template.arena, "build_navigation"):
                    bot = Bot(message)
                if hasattr(bot, "v13"):
                    bot.v13 = {key: flag in args.mode for key, flag in (("ENGAGEMENT", "a"), ("ESCAPE", "b"))}
                sim = build_fight(fight, template.arena, message["config"], seed)
                trace = deque(maxlen=100)
                for _ in range(320):
                    obs = sim.obs
                    errors = schema.errors(obs, "Observation")
                    if errors:
                        raise ValueError(errors)
                    start = perf_counter()
                    action = bot.act(obs)
                    times.append((perf_counter() - start) * 1000)
                    errors = schema.errors(action, "Action")
                    if errors or action["tick"] != obs["tick"] or not valid_numbers(action):
                        raise ValueError((action, errors))
                    row = snapshot(sim, bot, action) if args.traces else None
                    collisions = sim.metrics["collision"]
                    sim.advance(action)
                    if row is not None:
                        row.update(events=obs["events"], hp_after=obs["self"]["hp"],
                                   collision=sim.metrics["collision"] > collisions)
                        trace.append(row)
                    if sim.dead:
                        if row is not None:
                            deaths.append(dict(fight=fight.name, seed=seed, **classify(trace), trace=list(trace)))
                        break
                row = dict(fight=fight.name, seed=seed, dead=sim.dead, **sim.metrics)
                row["hp_remaining"] = max(0, sim.obs["self"]["hp"])
                row["damage_per_s"] = row.get("damage_in", 0) / row["survival_s"]
                rows.append(row)
    args.output.write_text(json.dumps(rows, indent=2) + "\n")
    if args.traces:
        args.traces.write_text("".join(json.dumps(row) + "\n" for row in deaths))
    print(f"{args.mode} {args.phase}: {len(rows)} trials, {sum(r['dead'] for r in rows)} deaths, "
          f"{len(times)} actions {summary(times)}, late={sum(t > 50 for t in times)}")


if __name__ == "__main__":
    main()
