import math
import gzip
import json
from pathlib import Path
import unittest

from example_bot import Bot
from combat import intercept

KIT = Path(__file__).resolve().parents[1] / "submission"


def hello():
    with gzip.open(KIT / "check/stream.jsonl.gz", "rt") as stream:
        message = json.loads(next(stream))
    message["map"]["dir"] = str(KIT / "arena")
    return message


def enemy(x, y, vx=0, vy=0, identifier=1):
    return {"kind": "mech", "id": identifier, "pos": [x, y, 0], "vel": [vx, vy, 0],
            "hp": 1400, "max_hp": 1400, "aim": [0, 0], "weapons": ["gun"], "cubes": 0}


def observation(config):
    stats = config["mech"]
    gun = {"kind": "gun", "stats": config["weapons"]["gun"], "ammo": 12, "reload": 0, "cooldown": 0}
    return {
        "tick": 20, "t": 1, "mechs": [], "events": [], "sounds": [], "projectiles": [],
        "pickups": [], "drops": [], "cubes": [], "offer": None,
        "zone": {"center": [0, 0], "radius": 200, "next_center": [0, 0], "next_radius": 150,
                 "shrink_start": 60, "shrink_end": 110, "dps": 20},
        "self": {"pos": [0, 0, 0], "vel": [0, 0, 0], "aim": [0, 0], "energy": 100,
                 "max_energy": 100, "hp": 1400, "max_hp": 1400, "in_zone": True,
                 "grounded": True, "stats": stats, "weapons": [gun, None], "cubes": 0},
    }


class PhoenixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.message = hello()
        cls.arena = Bot(cls.message).arena

    def setUp(self):
        from copy import copy
        from unittest.mock import patch

        with patch("example_bot.Arena", return_value=copy(self.arena)):
            with patch("arena.Arena.build_navigation"):
                self.bot = Bot(self.message)
        self.obs = observation(self.message["config"])
        self.bot.arena.walk = lambda start, dx, dy, distance, stats: (distance, start[2])
        self.bot.arena.solid = lambda *args: False


    def test_strafe_with_front_armor_toward_attacker(self):
        self.obs["mechs"] = [enemy(20, 0)]
        act = self.bot.act(self.obs)
        self.assertEqual(act["tick"], self.obs["tick"])
        self.assertAlmostEqual(act["aim"][0], 0)
        self.assertLess(act["move"][0], -0.35)

    def test_recent_damage_overrides_nearest_target(self):
        self.obs["mechs"] = [enemy(20, 0)]
        self.obs["events"] = [{"type": "damaged", "bearing": 2.0, "amount": 90}]
        self.assertAlmostEqual(self.bot.act(self.obs)["aim"][0], 2.0)

    def test_motion_does_not_reverse_when_target_switches_side(self):
        self.obs["mechs"] = [enemy(20, 0)]
        first = self.bot.act(self.obs)["move"]
        self.obs["mechs"] = [enemy(-20, 0)]
        second = self.bot.act(self.obs)["move"]
        self.assertGreater(sum(a * b for a, b in zip(first, second)), 0.5)

    def test_squad_does_not_cancel_lateral_escape(self):
        self.obs["mechs"] = [enemy(20, 0, -8, 0), enemy(-20, 0, 8, 0, 2)]
        self.assertGreater(abs(self.bot.act(self.obs)["move"][1]), 0.7)

    def test_closing_attacker_with_heavier_weapon_wins_priority(self):
        attacker = enemy(28, 0, -15, 0, 2)
        attacker["weapons"] = ["lightning"]
        self.obs["mechs"] = [enemy(-23, 0, -9, 0), attacker]
        self.assertEqual(self.bot.threats(self.obs)[0]["id"], 2)

    def test_intercept_leads_and_accounts_for_muzzle(self):
        aim, flight, hit = intercept((0, 0, 7), (100, 0, 4.5), (0, 12, 0), 252, 3)
        self.assertGreater(aim[0], 0.04)
        self.assertLess(aim[1], 0)
        self.assertAlmostEqual(flight * 252 + 3, math.dist((0, 0, 7), hit), places=2)

    def test_gun_respects_slew_cooldown_and_cover(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.obs["mechs"] = [enemy(0, 30)]
        act = self.bot.act(self.obs)
        self.assertFalse(any(act["fire"]))
        self.obs["self"]["aim"] = act["aim"]
        self.assertEqual(self.bot.act(self.obs)["fire"], [True, False])
        self.obs["self"]["weapons"][0]["cooldown"] = 0.2
        self.assertFalse(any(self.bot.act(self.obs)["fire"]))
        self.bot.arena.clear = lambda *args, **kwargs: False
        self.assertNotIn("fire", self.bot.act(self.obs))

    def test_exposed_retreat_uses_ground_boost_without_exhausting_energy(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.obs["mechs"] = [enemy(30, 0)]
        self.assertTrue(self.bot.act(self.obs)["boost"])
        self.obs["self"]["energy"] = 30
        self.assertFalse(self.bot.act(self.obs)["boost"])
        self.obs["self"]["energy"] = 100
        self.obs["self"]["grounded"] = False
        self.assertFalse(self.bot.act(self.obs)["boost"])

    def test_regeneration_suppresses_fire_when_safe_to_disengage(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.obs["mechs"] = [enemy(45, 0)]
        self.obs["self"]["hp"] = 650
        self.bot.last_damage = self.obs["t"] - 4
        self.assertFalse(any(self.bot.act(self.obs).get("fire", [])))
        self.obs["mechs"] = [enemy(18, 0)]
        self.assertTrue(self.bot.act(self.obs)["fire"][0])

    def test_early_evacuation_when_walking_eta_exceeds_deadline(self):
        self.obs["zone"].update(next_center=[100, 0], next_radius=10, shrink_end=9)
        yaw, urgency = self.bot.zone_goal(self.obs)
        self.assertEqual(yaw, 0)
        self.assertEqual(urgency, 1)

    def test_path_rejects_wall_and_uses_graph_waypoint(self):
        self.obs["zone"].update(next_center=[100, 0], next_radius=10, shrink_end=9)
        self.bot.arena.walk = lambda start, dx, dy, distance, stats: (0 if dx > 0.75 else distance, 0)
        self.bot.arena.waypoint = lambda *args: (0, 12)
        yaw, _ = self.bot.zone_goal(self.obs)
        self.assertAlmostEqual(yaw, math.pi / 2)
        self.assertGreater(self.bot.act(self.obs)["move"][1], 0.8)

    def test_real_map_ground_graph_and_boundary(self):
        self.assertGreater(len(self.arena.links), 5000)
        left, bottom, _, _ = self.arena.bounds
        x, y = left + 4, bottom + 20
        z = self.arena.ground(x, y)
        self.assertEqual(self.arena.walk((x, y, z), -1, 0, 10, self.obs["self"]["stats"])[0], 0)

    def test_slot_one_lightning_requires_energy_and_cone(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.obs["mechs"] = [enemy(25, 0)]
        me = self.obs["self"]
        w = self.message["config"]["weapons"]["lightning"]
        me["weapons"][0] = None
        me["weapons"][1] = {"kind": "lightning", "stats": w, "cooldown": 0}
        self.assertEqual(self.bot.act(self.obs)["fire"], [False, True])
        me["energy"] = w["energy"]
        self.assertFalse(any(self.bot.act(self.obs)["fire"]))
        me["energy"] = 100
        me["aim"][0] = math.pi
        self.assertFalse(any(self.bot.act(self.obs)["fire"]))

    def test_laser_releases_and_mortar_avoids_self_splash(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        me = self.obs["self"]
        config = self.message["config"]["weapons"]
        attacker = enemy(25, 0)
        self.obs["mechs"] = [attacker]
        me["weapons"] = [{"kind": "laser", "stats": config["laser"], "cooldown": 0, "charge": 0.1}, None]
        me["aim"][1] = math.atan2(-2.5, 25)
        self.assertEqual(self.bot.act(self.obs)["fire"], [True, False])
        me["weapons"][0]["charge"] = 0.2
        self.assertEqual(self.bot.act(self.obs)["fire"], [False, False])
        mortar = {"kind": "mortar", "stats": config["mortar"], "cooldown": 0}
        self.assertIsNotNone(self.bot.projectile_shot(me, attacker, mortar, 0, config["mortar"]["gravity"]))
        attacker["pos"][0] = 12
        self.assertIsNone(self.bot.projectile_shot(me, attacker, mortar, 0, config["mortar"]["gravity"]))

    def test_upgrades_and_loot_are_risk_gated(self):
        self.obs["offer"] = {"choices": [{"id": "gun_damage"}, {"id": "armor"}, {"id": "sensors"}]}
        self.obs["pickups"] = [{"id": 8001, "kind": "mortar", "pos": [1, 0, 5]},
                               {"id": 8002, "kind": "lightning", "pos": [1, 0, 0]}]
        self.obs["cubes"] = [{"pos": [5, 0, 0]}]
        act = self.bot.act(self.obs)
        self.assertEqual(act["pick"], 1)
        self.assertEqual(act["take"], 8002)
        self.assertEqual(self.bot.loot_goal(self.obs, []), [5, 0])
        self.assertIsNone(self.bot.loot_goal(self.obs, [enemy(50, 0)]))

    def test_optional_enemy_intel_may_be_absent(self):
        actor = enemy(30, 0)
        for field in ("hp", "max_hp", "weapons", "cubes"):
            del actor[field]
        self.obs["mechs"] = [actor]
        self.assertEqual(self.bot.act(self.obs)["tick"], self.obs["tick"])

    def test_near_wall_overrides_a_long_probe_that_skips_the_corner(self):
        self.bot.heading = 0.0
        self.obs["zone"].update(next_center=[100, 0], next_radius=10, shrink_end=9)
        self.bot.arena.walk = lambda start, dx, dy, distance, stats: (
            0 if distance == 1.5 and dx > 0.55 else distance, 0
        )
        move = self.bot.act(self.obs)["move"]
        self.assertLessEqual(move[0], 0.55)

    def test_protocol_fallback_echoes_tick_without_boosting_or_firing(self):
        import subprocess
        import sys

        code = """
import mechfight as mf
class Pilot:
    def __init__(self, hello): pass
    def act(self, obs):
        if obs["tick"] == 4: raise ValueError("recoverable")
        if obs["tick"] == 6: return {"move": [float("nan"), 0]}
        return {"move": [1, 0], "boost": True, "fire": [True, False]}
mf.run(Pilot)
"""
        with subprocess.Popen([sys.executable, "-B", "-u", "-c", code], cwd=Path(__file__).parent,
                              text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            def exchange(message):
                process.stdin.write(json.dumps(message) + "\n")
                process.stdin.flush()
                return json.loads(process.stdout.readline())

            self.assertEqual(exchange({"type": "hello"})["type"], "ready")
            self.assertTrue(exchange({"type": "obs", "tick": 2})["boost"])
            for tick in (4, 6):
                self.assertEqual(exchange({"type": "obs", "tick": tick}), {"move": [1, 0], "tick": tick})
            self.assertTrue(exchange({"type": "obs", "tick": 8})["boost"])
            process.stdin.write('{"type":"end"}\n')
            process.stdin.close()
            self.assertEqual(process.wait(timeout=3), 0)

    def test_isolated_hunter_engagement_and_recent_backup(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        hunter = enemy(55, 0)
        hunter["hp"] = 900
        self.obs["self"]["hp"] = 1100
        self.obs["mechs"] = [hunter]
        self.assertIn("fire", self.bot.act(self.obs))
        hunter["hp"] = 1200
        self.obs["t"] = 2
        self.assertIn("fire", self.bot.act(self.obs))
        hunter["hp"] = 900
        self.obs["mechs"].append(enemy(125, 0, identifier=2))
        self.assertNotIn("fire", self.bot.act(self.obs))
        self.obs["mechs"].pop()
        self.obs["t"] = 4
        self.assertNotIn("fire", self.bot.act(self.obs))
        self.obs["t"] = 6
        self.assertIn("fire", self.bot.act(self.obs))

    def test_isolated_hunter_requires_health_and_reload_time(self):
        hunter = enemy(45, 0, -25)
        del hunter["hp"]
        self.obs["self"]["hp"] = 1100
        self.assertIsNone(self.bot.engagement_target(self.obs, [hunter], 0))
        self.obs["self"]["hp"] = 1200
        gun = self.obs["self"]["weapons"][0]
        del gun["ammo"], gun["reload"]
        self.assertIsNone(self.bot.engagement_target(self.obs, [hunter], 0))
        gun.update(ammo=0, reload=2)
        self.assertIsNone(self.bot.engagement_target(self.obs, [hunter], 0))
        gun["reload"] = 1
        self.assertIs(self.bot.engagement_target(self.obs, [hunter], 0), hunter)

    def test_engaged_gun_leads_through_reload_and_respects_cover(self):
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.obs["mechs"] = [enemy(65, 0, 0, 10)]
        self.obs["self"]["weapons"][0].update(ammo=0, reload=1)
        act = self.bot.act(self.obs)
        self.assertGreater(act["aim"][0], 0.025)
        self.assertEqual(act["fire"], [False, False])
        self.assertGreater(math.hypot(*act["move"]), 0.9)
        self.bot.arena.clear = lambda *args, **kwargs: False
        self.assertNotIn("fire", self.bot.act(self.obs))

    def test_flag_off_replays_v5_action_bytes(self):
        import subprocess
        import sys

        module_dir = Path(__file__).resolve().parent
        original = subprocess.check_output(
            ["git", "show", "990f4a2181b25adc653291d0620cd755cb01301a:example_bot.py"],
            cwd=module_dir,
        ).decode()
        with gzip.open(KIT / "check/stream.jsonl.gz", "rb") as stream:
            stream.readline()
            inputs = [json.dumps(hello()).encode() + b"\n", *stream]

        def replay(code):
            with subprocess.Popen([sys.executable, "-B", "-u", "-c", code], cwd=module_dir,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE) as process:
                actions = []
                for line in inputs:
                    process.stdin.write(line)
                    process.stdin.flush()
                    kind = json.loads(line)["type"]
                    if kind == "hello":
                        self.assertEqual(json.loads(process.stdout.readline())["type"], "ready")
                    elif kind == "obs":
                        actions.append(process.stdout.readline())
                process.stdin.close()
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(process.stderr.read(), b"")
            self.assertEqual(len(actions), 300)
            self.assertTrue(all(actions))
            return actions

        v5 = replay(original)
        disabled = replay("import example_bot as bot; bot.V6A_ENABLED = False; bot.mf.run(bot.Bot)")
        self.assertEqual(v5, disabled)

    def test_finish_requires_cover_and_respects_time_and_distance_caps(self):
        hunter = enemy(55, 0)
        hunter["hp"] = 350
        self.obs["mechs"] = [hunter]
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.bot.act(self.obs)
        self.assertIsNone(self.bot.finish_start)

        self.bot.exposed = lambda me, enemies, pos: abs(pos[1]) < 5
        act = self.bot.act(self.obs)
        self.assertEqual(act["move"], [1, 0])
        self.assertFalse(act["boost"])
        self.obs["self"]["pos"][0] = 24
        self.assertIsNone(self.bot.finish_move(self.obs, hunter))
        self.assertTrue(self.bot.finish_spent)

        self.bot.finish_spent = False
        self.obs["self"]["pos"][0] = 0
        self.obs["t"] += 3
        self.assertIsNone(self.bot.finish_move(self.obs, hunter))
        self.assertTrue(self.bot.finish_spent)

    def test_zone_or_second_attacker_interrupts_finish(self):
        hunter = enemy(55, 0)
        hunter["hp"] = 350
        self.obs["mechs"] = [hunter]
        self.bot.exposed = lambda me, enemies, pos: abs(pos[1]) < 5
        self.assertEqual(self.bot.act(self.obs)["move"], [1, 0])
        self.obs["t"] += self.bot.period
        self.obs["zone"].update(next_center=[0, 140], next_radius=12, shrink_end=5)
        from copy import copy
        from unittest.mock import patch

        v5 = copy(self.bot)
        with patch("example_bot.V6A_ENABLED", False):
            expected = v5.act(self.obs)
        self.assertEqual(self.bot.act(self.obs), expected)
        self.assertIsNone(self.bot.engaged_id)
        self.assertTrue(self.bot.finish_spent)

        self.obs["zone"] = observation(self.message["config"])["zone"]
        self.obs["mechs"].append(enemy(55, 15, identifier=2))
        self.bot.act(self.obs)
        self.assertIsNone(self.bot.engaged_id)

    def test_disengage_during_minimum_hold_when_hurt_drained_or_cornered(self):
        hunter = enemy(55, 0)
        hunter["hp"] = 350
        self.obs["mechs"] = [hunter]
        me = self.obs["self"]
        for field, low in (("hp", 500), ("energy", 20)):
            with self.subTest(field=field):
                self.assertIs(self.bot.engagement_target(self.obs, [hunter], 0), hunter)
                original = me[field]
                me[field] = low
                self.assertIsNone(self.bot.engagement_target(self.obs, [hunter], 0))
                me[field] = original
        self.assertIs(self.bot.engagement_target(self.obs, [hunter], 0), hunter)
        self.bot.arena.walk = lambda start, dx, dy, distance, stats: (0, start[2])
        self.assertIsNone(self.bot.engagement_target(self.obs, [hunter], 0))
