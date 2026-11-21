import gzip
import json
import subprocess
from unittest import TestCase
from unittest.mock import patch

import example_bot
from test_phoenix import KIT, enemy, hello, observation


class OpeningTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.message = hello()
        cls.arena = example_bot.Bot(cls.message).arena

    def setUp(self):
        from copy import copy
        with patch("example_bot.Arena", return_value=copy(self.arena)), patch("arena.Arena.build_navigation"):
            self.bot = example_bot.Bot(self.message)
        self.bot.arena.clear = lambda *args, **kwargs: True
        self.bot.arena.walk = lambda start, dx, dy, distance, stats: (distance, 0)
        self.obs = observation(self.message["config"])
        self.obs["crates"] = [{"id": 1001, "pos": [12, 0, 0], "hp": 300}]
        self.obs["self"]["aim"] = [0, -0.5]

    def test_safe_crate_and_rewards(self):
        action = self.bot.act(self.obs)
        self.assertEqual(action["fire"], [True, False])
        self.assertFalse(action["boost"])
        self.obs["t"] += 0.3
        self.obs["events"] = [{"type": "hit", "target": 1001}]
        self.obs["crates"][0]["hp"] -= 45
        self.assertTrue(self.bot.act(self.obs)["fire"][0])
        self.obs["crates"] = []
        self.obs["cubes"] = [{"id": 5001, "pos": [7, 0, 0]}]
        self.assertGreater(self.bot.act(self.obs)["move"][0], 0.9)
        self.obs["cubes"] = []
        self.obs["pickups"] = [{"id": 8001, "kind": "lightning", "pos": [3, 0, 0]}]
        self.assertEqual(self.bot.act(self.obs)["take"], 8001)

    def test_contested_resource(self):
        self.obs["mechs"] = [enemy(75, 0), enemy(-80, 0, identifier=2)]
        self.assertIsNone(self.bot.opening_resource(self.obs, self.obs["mechs"]))

    def test_outside_next_circle(self):
        self.obs["zone"]["next_center"] = [-150, 0]
        self.assertIsNone(self.bot.opening_resource(self.obs, []))

    def test_unreachable_resource(self):
        self.bot.arena.clear = lambda *args, **kwargs: False
        self.assertIsNone(self.bot.opening_resource(self.obs, []))

    def test_disappears(self):
        self.bot.act(self.obs)
        self.obs["crates"] = []
        self.bot.act(self.obs)
        self.assertIsNone(self.bot.crate_attempt)

    def test_interrupted_by_closing_hunter(self):
        self.bot.act(self.obs)
        self.obs["mechs"] = [enemy(168, 0, vx=-19)]
        self.assertNotIn("fire", self.bot.act(self.obs))
        self.assertIsNone(self.bot.crate_attempt)

    def test_unsafe_pickup(self):
        self.obs["crates"] = []
        self.obs["pickups"] = [{"id": 8001, "kind": "lightning", "pos": [3, 0, 0]}]
        self.obs["projectiles"] = [{"id": 99}]
        self.assertNotIn("take", self.bot.act(self.obs))

    def test_stops_shooting_after_misses(self):
        self.bot.act(self.obs)
        self.obs["t"] += 4.1
        self.assertNotIn("fire", self.bot.act(self.obs))
        self.assertIn(1001, self.bot.failed_crates)

    def test_flag_off_matches_scoring_source(self):
        namespace = {"__name__": "v5"}
        source = subprocess.check_output(["git", "show", "990f4a2:example_bot.py"])
        exec(compile(source, "v5", "exec"), namespace)
        reference, candidate = namespace["Bot"](self.message), example_bot.Bot(self.message)
        count = 0
        with patch.object(example_bot, "V7A_ENABLED", False), gzip.open(KIT / "check/stream.jsonl.gz", "rt") as stream:
            for line in stream:
                obs = json.loads(line)
                if obs["type"] == "obs":
                    count += 1
                    self.assertEqual(candidate.act(obs), reference.act(obs), obs["tick"])
        self.assertEqual(count, 300)
