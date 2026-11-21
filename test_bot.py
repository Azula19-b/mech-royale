"""Focused regressions for survival controller mechanics."""
import copy
import json
import os
import unittest

import bot


ROOT = os.path.dirname(__file__)


def fixture():
    with __import__('gzip').open(os.path.join(ROOT, "check/stream.jsonl.gz"), "rt") as f:
        msgs = [json.loads(line) for line in f]
    return msgs[0], next(m for m in msgs if m.get("type") == "obs")


class ControllerRegression(unittest.TestCase):
    def setUp(self):
        self.hello, self.obs = fixture()
        self.obs = copy.deepcopy(self.obs)
        self.b = bot.Bot(self.hello)
        self.me = self.obs["self"]
        self.me["pos"] = [0, 0, 0]
        self.me["vel"] = [0, 0, 0]
        self.me["aim"] = [0, 0]
        self.obs["zone"] = {"center": [0, 0], "radius": 500, "next_center": [0,0], "next_radius": 500, "shrink_start": 999, "shrink_end": 1000}
        self.me["in_zone"] = True

    def enemy(self, x=30):
        return {"id": 99, "kind": "mech", "pos": [x, 0, 0], "vel": [0,0,0], "aim": [3.14,0], "hp": 1000, "weapons": ["gun"], "charging": False}

    def test_second_weapon_slot_fires_independently(self):
        self.me["weapons"][1] = {"kind":"lightning", "cooldown":0, "stats":self.hello["config"]["weapons"]["lightning"]}
        self.obs["mechs"] = [self.enemy(30)]
        _, fire, _ = self.b._aim_and_fire(self.obs, self.me, self.b._threats(self.obs,self.me))
        self.assertTrue(fire[1])

    def test_waypoint_does_not_stop_under_pressure(self):
        enemy = self.enemy(2)
        threats = [(enemy, 2.0, 1.0, 0.0, 1.0)]
        move, _ = self.b._move(self.obs, self.me, threats, [0,0])
        self.assertGreater(sum(v*v for v in move), 0.5)

    def test_cubes_and_weapon_pickups_can_be_navigation_goals(self):
        self.obs["cubes"] = [{"id":1,"pos":[15,0,0]}]
        target = self.b._target(self.obs,self.me,[],[0,0],450)
        self.assertEqual(target,[15,0])

    def test_recent_threat_is_remembered_and_cover_is_selected(self):
        self.b.prisms = [(20,-5,30,5,-5,20,[20,-5,30,-5,30,5,20,5],[])]
        self.obs["mechs"] = [self.enemy(50)]
        threats = self.b._threats(self.obs,self.me)
        self.assertEqual(len(threats),1)
        self.assertIsNotNone(self.b._cover_target(self.me,threats,[0,0],400))
        self.obs["mechs"] = []
        self.obs["t"] = 0.2
        self.assertEqual(len(self.b._threats(self.obs,self.me)),1)

    def test_nearby_npc_is_a_movement_threat_but_not_a_combat_target(self):
        npc = {"id":101,"kind":"npc","pos":[25,0,0],"vel":[0,0,0],"aim":[3.14,0],"hp":350}
        self.obs["mechs"]=[npc]
        threats=self.b._threats(self.obs,self.me)
        self.assertEqual(len(threats),1)
        self.assertEqual(self.b._aim_and_fire(self.obs,self.me,threats)[2],None)

    def test_obstacle_route_rejects_a_solid_prism(self):
        square = [4,-3, 8,-3, 8,3, 4,3]
        self.b.prisms = [(4,-3,8,3,-10,20,square,[])]
        self.assertTrue(self.b._path_blocked([0,0,0],10,0))
        self.assertFalse(self.b._path_blocked([0,10,0],10,10))

    def test_safe_navigation_target_persists_across_enemy_jitter(self):
        self.obs["t"] = 1.0
        self.obs["mechs"] = [self.enemy(30)]
        first = self.b._target(self.obs,self.me,self.obs["mechs"],[0,0],400)
        self.obs["t"] = 1.5
        self.obs["mechs"] = [self.enemy(30), {**self.enemy(-25), "id": 77}]
        second = self.b._target(self.obs,self.me,self.obs["mechs"],[0,0],400)
        self.assertEqual(first, second)

    def test_projectile_prediction_uses_velocity_units(self):
        self.obs["projectiles"] = [{"pos":[-15,0,4],"vel":[20,0,0]}]
        threats=[]
        move, _ = self.b._move(self.obs,self.me,threats,[100,0])
        # A projectile crossing our position should make straight-line motion
        # score poorly; the selected vector must substantially sidestep it.
        self.assertLess(abs(move[1]), 0.98)

    def test_laser_charge_releases_at_useful_charge(self):
        self.me["weapons"][0] = {"kind":"laser","charge":0.7,"cooldown":0,"stats":self.hello["config"]["weapons"]["laser"]}
        self.obs["mechs"]=[self.enemy(100)]
        _, fire, _ = self.b._aim_and_fire(self.obs,self.me,[])
        self.assertFalse(fire[0])


if __name__ == "__main__":
    unittest.main()
