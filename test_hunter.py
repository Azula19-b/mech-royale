"""Deterministic tactical fixtures and exact V5 replay parity."""
from copy import copy
import gzip
import json
import math
import unittest
from unittest.mock import patch

from example_bot import Bot as V5
from hunter import Bot
from test_phoenix import KIT, enemy, hello, observation


class HunterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.message = hello()
        cls.arena = V5(cls.message).arena

    def setUp(self):
        with patch("example_bot.Arena", return_value=copy(self.arena)), patch("arena.Arena.build_navigation"):
            self.bot = Bot(self.message)
        self.arena_local = self.bot.arena
        self.arena_local.walk = lambda start, dx, dy, distance, stats: (distance, start[2])
        self.arena_local.clear = lambda *args, **kwargs: True
        self.arena_local.edge = lambda *args: 200
        self.arena_local.solid = lambda *args: False
        self.obs = observation(self.message["config"])
        self.me = self.obs["self"]

    def step(self, *enemies):
        self.obs["t"] += 0.1
        self.obs["tick"] += 2
        self.obs["mechs"] = list(enemies)
        return self.bot.act(self.obs)

    def weapon(self, kind, **fields):
        self.me["weapons"][1] = dict(kind=kind, stats=self.bot.config["weapons"][kind], cooldown=0, **fields)

    def test_isolated_approaching_hunter(self):
        act = self.step(enemy(70, 0, -10))
        self.assertEqual(self.bot.state, "ENGAGE")
        self.assertTrue(act["fire"][0])
        self.assertGreater(math.hypot(*act["move"]), 0.9)

    def test_low_hp_finish(self):
        weak = enemy(75, -2, identifier=2)
        weak["hp"] = 30
        self.step(enemy(65, 0), weak)
        self.assertEqual(self.bot.fire_control.target, 2)

    def test_two_shootable_targets_prioritize_threat(self):
        dangerous = enemy(55, 1, -15, identifier=2)
        dangerous["aim"][0] = math.pi
        self.step(enemy(55, -1), dangerous)
        self.assertEqual(self.bot.fire_control.target, 2)

    def test_blocked_highest_ranked_target(self):
        self.arena_local.clear = lambda origin, target, **kwargs: not (target[0] > 12 and target[1] > 4)
        exposed = enemy(85, 0, identifier=2)
        act = self.step(enemy(22, 12, -5), exposed)
        self.assertEqual(self.bot.fire_control.target, 2)
        self.assertTrue(act["fire"][0])

    def test_same_sector_squad(self):
        act = self.step(enemy(30, 5, -10), enemy(35, 0, -10, identifier=2),
                        enemy(38, -5, -10, identifier=3))
        self.assertEqual(self.bot.state, "EVADE")
        self.assertLess(act["move"][0], 0.4)

    def test_opposite_directions_and_armor(self):
        self.me["weapons"] = [None, None]
        east = enemy(35, 0)
        west = enemy(-35, 0, identifier=2)
        self.obs["events"] = [{"type": "damaged", "bearing": math.pi, "amount": 80}]
        act = self.step(east, west)
        self.assertEqual(self.bot.state, "EVADE")
        self.assertLess(math.cos(act["aim"][0]), 0)

    def test_tracked_outside_fov(self):
        east = enemy(75, 0)
        west = enemy(-75, 0, identifier=2)
        self.step(east, west)
        first, second = self.bot.tracks
        self.assertTrue(first.sight_likely)
        self.assertFalse(second.sight_likely)
        self.assertIsNone(second.reliable_at)
        self.assertGreater(second.uncertainty(0.7), first.uncertainty(0.7))

    def test_lateral_projectile_lead(self):
        act = self.step(enemy(100, 0, 0, 12))
        self.assertGreater(act["aim"][0], 0.035)

    def test_closing_prediction_and_acceleration_bound(self):
        target = enemy(95, 0, -5)
        self.step(target)
        target = enemy(94, 0, -16)
        self.step(target)
        track = self.bot.tracks[0]
        self.assertLess(track.predict(0.7)[0], 87)
        self.assertLessEqual(math.hypot(*track.acceleration), self.me["stats"]["ground_accel"] * 0.46)

    def test_range_edge(self):
        reach = self.me["weapons"][0]["stats"]["range"]
        self.assertTrue(self.step(enemy(reach - 2, 0))["fire"][0])
        self.assertFalse(self.step(enemy(reach + 12, 0)).get("fire", [False])[0])

    def test_cooldown_and_reload_keep_aiming(self):
        gun = self.me["weapons"][0]
        for cooldown, reload in [(0.5, 0), (0, 1.1)]:
            gun.update(cooldown=cooldown, reload=reload)
            act = self.step(enemy(60, 14))
            self.assertGreater(act["aim"][0], 0.20)
            self.assertFalse(act["fire"][0])

    def test_laser_charging_and_release(self):
        self.me["weapons"][0] = None
        self.me["aim"][1] = -0.06
        self.weapon("laser", charge=0)
        target = enemy(40, 0)
        target["hp"] = 60
        self.assertTrue(self.step(target)["fire"][1])
        self.me["weapons"][1]["charge"] = 0.08
        self.assertTrue(self.step(target)["fire"][1])
        self.me["weapons"][1]["charge"] = 0.20
        self.assertFalse(self.step(target)["fire"][1])

    def test_mortar_splash_and_obstacle(self):
        self.me["weapons"][0] = None
        self.weapon("mortar")
        self.step(enemy(8, 0))
        self.assertIsNone(self.bot.fire_control.selected)
        self.step(enemy(65, 0))
        self.assertIsNotNone(self.bot.fire_control.selected)
        self.arena_local.clear = lambda origin, target, **kwargs: target[0] < 28
        self.step(enemy(65, 0))
        self.assertIsNone(self.bot.fire_control.selected)

    def test_lightning_reserves_energy(self):
        self.me["weapons"][0] = None
        self.weapon("lightning")
        self.me["energy"] = 4
        self.assertFalse(self.step(enemy(22, 0)).get("fire", [False, False])[1])
        self.me["energy"] = 100
        self.assertTrue(self.step(enemy(22, 0))["fire"][1])

    def risk(self, x, y):
        self.obs["projectiles"] = [{"id": 1, "kind": "gun", "pos": [x, y, 4], "vel": [252, 0, 0]}]
        paths = self.bot.planner.projectiles(self.obs)
        return self.bot.planner.impact(self.me, 0, 0, paths)

    def test_projectile_collision_course(self):
        self.assertGreater(self.risk(-55, 0), 1)

    def test_harmless_projectile(self):
        self.assertEqual(self.risk(-55, 9), 0)
        self.assertEqual(self.risk(5, 0), 0)

    def test_low_hp_safe_recovery(self):
        self.me["hp"] = 550
        self.arena_local.clear = lambda *args, **kwargs: False
        act = self.step(enemy(75, 0))
        self.assertEqual(self.bot.state, "RECOVER")
        self.assertFalse(any(act.get("fire", [])))
        self.assertGreater(math.hypot(*act["move"]), 0.9)

    def test_low_hp_without_cover_still_fires(self):
        self.me["hp"] = 340
        act = self.step(enemy(60, 0))
        self.assertEqual(self.bot.state, "ENGAGE")
        self.assertTrue(act["fire"][0])

    def test_boost_reserve(self):
        self.me["energy"] = 7
        self.assertFalse(self.step(enemy(25, 0), enemy(30, 8, identifier=2))["boost"])

    def test_emergency_evacuation(self):
        self.me.update(in_zone=False, pos=[80, 0, 0])
        self.obs["zone"].update(radius=65, next_radius=35, shrink_start=0, shrink_end=5)
        act = self.step(enemy(32, 0))
        self.assertEqual(self.bot.state, "EMERGENCY_ZONE")
        self.assertLess(act["move"][0], -0.75)

    def test_narrow_passage_and_blocked_heading(self):
        self.arena_local.walk = lambda start, dx, dy, distance, stats: (distance if dx < -0.7 else 0, start[2])
        self.bot.heading = 0
        act = self.step(enemy(50, 0))
        self.assertLess(act["move"][0], -0.7)

    def test_no_chase_into_crossfire(self):
        weak = enemy(47, 0)
        weak["hp"] = 10
        act = self.step(weak, enemy(25, 28, identifier=2), enemy(25, -28, identifier=3))
        self.assertEqual(self.bot.state, "EVADE")
        self.assertLess(act["move"][0], 0.3)

    def test_target_retention_then_switch_when_blocked(self):
        first, second = enemy(60, 5), enemy(61, -5, identifier=2)
        self.step(first, second)
        self.assertEqual(self.bot.fire_control.target, 1)
        second["pos"][0] -= 2
        self.step(first, second)
        self.assertEqual(self.bot.fire_control.target, 1)
        self.arena_local.clear = lambda origin, target, **kwargs: target[0] < 15 or target[1] < 0
        self.step(first, second)
        self.assertEqual(self.bot.fire_control.target, 2)

    def test_v5_parity_all_flags_off(self):
        flags = {f"V10_{flag}_ENABLED": "0" for flag in ("TRACKING", "COMBAT", "MOVEMENT", "TACTICS")}
        with patch("example_bot.Arena", return_value=self.arena), patch("arena.Arena.build_navigation"):
            baseline = V5(self.message)
            with patch.dict("os.environ", flags):
                candidate = Bot(self.message)
        with gzip.open(KIT / "check/stream.jsonl.gz", "rt") as stream:
            count = 0
            for line in stream:
                message = json.loads(line)
                if message["type"] == "obs":
                    self.assertEqual(baseline.act(message), candidate.act(message), message["tick"])
                    count += 1
        self.assertEqual(count, 300)


if __name__ == "__main__":
    unittest.main()
