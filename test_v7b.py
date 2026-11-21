from copy import copy
import gzip
import json
import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from cover import Cover
from diagnose import make_map, Scenario
import example_bot
from test_phoenix import KIT, enemy, hello


class CoverTests(TestCase):
    @classmethod
    def setUpClass(cls):
        root = TemporaryDirectory()
        cls.addClassCleanup(root.cleanup)
        make_map(Path(root.name))
        cls.official = hello()
        cls.message = dict(cls.official, map={"dir": root.name})
        cls.arena = example_bot.Bot(cls.message).arena

    def setUp(self):
        with patch("example_bot.Arena", return_value=self.arena), patch("arena.Arena.build_navigation"):
            self.bot = example_bot.Bot(self.message)
        self.scenario = Scenario("pursuit", self.arena, self.message["config"])
        self.obs = self.scenario.obs
        self.me = self.obs["self"]
        self.me.update(hp=900, pos=[18, 45, 0])
        self.obs["zone"].update(center=[0, 0], next_center=[0, 0], radius=150,
                                next_radius=140, shrink_start=60, shrink_end=110)
        self.obs["mechs"] = [enemy(-45, 45, vx=4)]

    def step(self, count):
        for _ in range(count):
            self.scenario.advance(self.bot.act(self.obs))

    def test_approaching_hunter_and_sustained_hard_cover(self):
        hunter = self.bot.cover.hunters(self.obs["mechs"])[0]
        self.assertTrue(self.bot.cover.visible(self.me["pos"], self.me["stats"], hunter))
        self.step(100)
        hunter = self.bot.cover.hunters(self.obs["mechs"])[0]
        self.assertFalse(self.bot.cover.visible(self.me["pos"], self.me["stats"], hunter))
        self.assertGreater(self.scenario.metrics["covered"], 80)
        self.assertGreater(self.me["hp"], 1000)
        self.assertEqual(self.scenario.metrics["stationary_exposed"], 0)

    def test_converging_hunters_prevent_holding_exposed_side(self):
        self.step(25)
        original = self.bot.cover.path[-1]
        self.obs["mechs"].append(enemy(42, 33, vx=-10, identifier=2))
        action = self.bot.act(self.obs)
        self.assertNotEqual(action["move"], [0, 0])
        self.assertNotIn(original, self.bot.cover.path)

    def test_cover_path_rounds_solid_without_collisions(self):
        self.me["pos"] = [28, 55, 0]
        self.step(40)
        self.assertGreater(self.scenario.metrics["covered"], 20)
        self.assertEqual(self.scenario.metrics["collision"], 0)
        self.assertLess(self.me["pos"][1], 37)

    def test_dead_end_is_not_a_candidate(self):
        arena = copy(self.arena)
        arena.links = dict(arena.links)
        node = (2, 4)
        self.assertIn(node, self.bot.cover.sites)
        arena.links[node] = arena.links[node][:1]
        cover = Cover(arena, self.me["stats"], self.message["config"])
        self.assertNotIn(node, cover.sites)

    def test_flanking_interrupts_regeneration_hold(self):
        self.step(65)
        self.assertEqual(self.bot.act(self.obs)["move"], [0, 0])
        self.obs["mechs"][0].update(pos=[19, 20, 0], vel=[0, 8, 0])
        move = self.bot.act(self.obs)["move"]
        self.assertGreater(math.hypot(*move), 0.5)

    def test_emergency_enters_safe_circle_around_obstacle(self):
        self.scenario = Scenario("evacuation", self.arena, self.message["config"])
        self.obs = self.scenario.obs
        self.obs["self"]["in_zone"] = False
        self.step(180)
        self.assertTrue(self.obs["self"]["in_zone"])
        self.assertLess(self.scenario.metrics["zone_out"], 15)
        self.assertEqual(self.scenario.metrics["stuck"], 0)

    def test_flag_off_matches_scoring_source(self):
        namespace = {"__name__": "v5"}
        source = subprocess.check_output(["git", "show", "990f4a2:example_bot.py"])
        exec(compile(source, "v5", "exec"), namespace)
        reference = namespace["Bot"](self.official)
        with patch.object(example_bot, "V7B_ENABLED", False):
            candidate = example_bot.Bot(self.official)
        count = 0
        with gzip.open(KIT / "check/stream.jsonl.gz", "rt") as stream:
            for line in stream:
                obs = json.loads(line)
                if obs["type"] == "obs":
                    count += 1
                    self.assertEqual(candidate.act(obs), reference.act(obs), obs["tick"])
        self.assertEqual(count, 300)
