"""Deterministic audio route tests and full-action replay parity against V5."""
import gzip
import json
import math
from pathlib import Path
import subprocess
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import example_bot as phoenix
from test_phoenix import KIT, enemy, hello, observation

V5 = "990f4a2181b25adc653291d0620cd755cb01301a"


def baseline_module():
    source = subprocess.check_output(
        ["git", "show", f"{V5}:example_bot.py"], cwd=Path(__file__).resolve().parent
    )
    module = ModuleType("phoenix_v5")
    exec(compile(source, f"{V5}:example_bot.py", "exec"), module.__dict__)
    return module


def sound(kind="gun", yaw=0, band="near", age=0):
    return {"kind": kind, "bearing": yaw, "dist": band, "age": age}


def concentrated(yaw=0):
    return [sound(kind, yaw) for kind in ("gun", "laser", "mortar", "boost")]


class AudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.message = hello()
        cls.baseline = baseline_module()

    def setUp(self):
        self.obs = observation(self.message["config"])
        self.obs["zone"]["next_center"] = [20, 0]
        self.arena = SimpleNamespace(
            build_navigation=lambda stats: None, solid=lambda *args: False,
            walk=lambda pos, dx, dy, distance, stats: (distance, pos[2]),
            edge=lambda *args: 100, clear=lambda *args, **kwargs: True,
        )
        with patch.object(phoenix, "Arena", return_value=self.arena):
            self.bot = phoenix.Bot(self.message)
        with patch.object(self.baseline, "Arena", return_value=self.arena):
            self.v5 = self.baseline.Bot(self.message)

    def assert_baseline(self):
        self.assertEqual(self.bot.act(self.obs), self.v5.act(self.obs))

    def test_a_no_relevant_audio(self):
        self.assert_baseline()
        self.obs["sounds"] = [sound("npc_gun"), sound("drop"), sound("explosion")]
        self.assert_baseline()

    def test_b_single_weak_sound(self):
        self.obs["sounds"] = [sound("gun", band="far")]
        self.assert_baseline()

    def test_c_concentrated_sound_changes_heading_without_stopping(self):
        self.obs["sounds"] = concentrated()
        move = self.bot.act(self.obs)["move"]
        self.assertEqual(self.v5.act(self.obs)["move"], [1, 0])
        self.assertAlmostEqual(abs(math.atan2(move[1], move[0])), math.pi / 6)
        self.assertAlmostEqual(math.hypot(*move), 1)

    def test_d_confirmed_enemy_and_matching_audio(self):
        self.obs["mechs"] = [enemy(28, 0, -12)]
        self.obs["sounds"] = concentrated() + concentrated(math.pi)
        self.assert_baseline()
        self.assertTrue(self.bot.audio)
        self.assertTrue(all(abs(yaw) == math.pi for _, yaw, _, _ in self.bot.audio))

    def test_e_zone_emergency(self):
        self.obs["zone"].update(radius=10, shrink_end=2)
        self.obs["sounds"] = concentrated()
        self.assert_baseline()

    def test_e_next_zone_veto_applies_before_urgency(self):
        self.obs["zone"].update(next_center=[-35, 0], next_radius=22)
        self.obs["sounds"] = concentrated(math.pi)
        # The westward route fits the next circle's margin and both alternatives miss it.
        self.assertEqual(self.bot.zone_goal(self.obs)[1], 0)
        self.assert_baseline()

    def test_f_only_clear_corridor_and_map_edge(self):
        self.obs["sounds"] = concentrated()
        self.arena.walk = lambda pos, dx, dy, distance, stats: (
            distance if dx > 0 and abs(dy) < 0.1 else min(distance, 2), pos[2])
        self.assert_baseline()
        self.arena.walk = lambda pos, dx, dy, distance, stats: (distance, pos[2])
        self.arena.edge = lambda x, y: 0 if abs(y) > 5 else 100
        self.assert_baseline()

    def test_g_opposing_sounds_are_stable(self):
        for tick in range(12):
            self.obs.update(t=1 + 0.1 * tick, sounds=concentrated() + concentrated(math.pi))
            self.bot.act(self.obs)
            if tick == 0:
                first = self.bot.heading
            self.assertAlmostEqual(self.bot.heading, first)
        self.assertLessEqual(abs(first), math.pi / 6 + 1e-9)

    def test_h_repeated_reports_and_history_bound(self):
        for tick in range(10):
            age = 0.1 * tick
            self.obs.update(t=1 + age, sounds=[sound(age=age)])
            self.bot.remember_audio(self.obs, [])
        self.assertEqual(len(self.bot.audio), 1)
        self.obs["sounds"] = [sound(yaw=i * math.tau / 40, age=i * 0.055 % 1.1) for i in range(40)]
        self.bot.remember_audio(self.obs, [])
        self.assertLessEqual(len(self.bot.audio), phoenix.AUDIO_HISTORY_LIMIT)
        self.assertLessEqual(self.bot.audio_penalty(0, self.obs["t"]), phoenix.AUDIO_MAX_PENALTY)

    def test_i_age_decays_then_expires(self):
        self.obs["sounds"] = concentrated()
        self.bot.remember_audio(self.obs, [])
        self.assertLess(self.bot.audio_penalty(0, 1.6), self.bot.audio_penalty(0, 1))
        self.obs.update(t=2.3, sounds=[])
        self.bot.remember_audio(self.obs, [])
        self.assertEqual(self.bot.audio_penalty(0, 2.3), 0)
        self.assert_baseline()

    def test_j_defensive_combat_full_action_parity(self):
        self.obs["mechs"] = [enemy(26, 0)]
        self.obs["sounds"] = concentrated(math.pi)
        expected = self.v5.act(self.obs)
        self.assertTrue(any(expected["fire"]))
        self.assertEqual(self.bot.act(self.obs), expected)

    def test_k_flag_off_full_action_replay(self):
        with gzip.open(KIT / "check/stream.jsonl.gz", "rt") as stream:
            next(stream)
            rows = [row for row in map(json.loads, stream) if row["type"] == "obs"]
        candidate, baseline = phoenix.Bot(self.message), self.baseline.Bot(self.message)
        with patch.object(phoenix, "AUDIO_THREAT_ENABLED", False):
            for obs in rows:
                with self.subTest(tick=obs["tick"]):
                    self.assertEqual(json.dumps(candidate.act(obs)), json.dumps(baseline.act(obs)))
        self.assertEqual(len(rows), 300)

    def test_l_wraparound_and_missing_optional_fields(self):
        self.obs["sounds"] = [sound(yaw=math.pi, age=0), sound(yaw=-math.pi + 0.01, age=0)]
        action = self.bot.act(self.obs)
        self.assertEqual(len(self.bot.audio), 1)
        self.assertAlmostEqual(self.bot.audio_penalty(math.pi, 1), self.bot.audio_penalty(-math.pi, 1))
        self.assertTrue(all(map(math.isfinite, action["move"] + action["aim"])))


if __name__ == "__main__":
    unittest.main()
