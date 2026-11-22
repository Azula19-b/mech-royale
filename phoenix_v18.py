"""Phoenix V18: selected GMUNVIS ideas adapted to the Phoenix Python stack."""
import math

import mechfight as mf
from gmunvis import Gmunvis
from hunter import Bot as PhoenixV17


class Bot(PhoenixV17):
    name = "phoenix-v18-gmunvis"

    def __init__(self, hello):
        super().__init__(hello)
        self.name = type(self).name
        self.gmunvis = Gmunvis(self)
        self.v18 = self.gmunvis.features
        self._gmunvis_obs = None

    def tactics(self, obs, tracks):
        self._gmunvis_obs = obs
        self.gmunvis.update(obs, tracks)
        active = super().tactics(obs, tracks)
        me, now = obs["self"], obs["t"]
        if self.state == "EMERGENCY_ZONE":
            return active

        # A third party fight signal turns the opening into a positioning phase.
        # A close attacker or a direct hit always overrides passive behavior.
        attackers = self.gmunvis.recent_attackers(now)
        immediate = any(t.actor["id"] in attackers or t.distance < 60 for t in tracks)
        if self.gmunvis.passive and not immediate:
            safe = me.get("in_zone") and not obs.get("projectiles")
            self.state = "RECOVER" if safe and me["hp"] < 0.90 * me["max_hp"] else "POSITION"
            self.state_until = max(self.state_until, now + 0.8)
            return []

        if self.gmunvis.squad_retreat(obs, tracks):
            self.state = "EVADE"
            self.state_until = max(self.state_until, now + 1.1)
            return active

        if self.gmunvis.features["RECOVERY"]:
            proposed = "ENGAGE" if active else "POSITION"
            old_state, proposed = self.gmunvis.recover_mode(obs, self.state, proposed, tracks)
            if old_state == "RECOVER":
                self.state = "RECOVER"
                self.state_until = max(self.state_until, now + 0.55)
            elif self.state == "RECOVER":
                self.state = proposed
                self.state_until = now + 0.4
        return active

    def loot_goal(self, obs, enemies):
        ordinary = super().loot_goal(obs, enemies)
        if not self.gmunvis.features["GEAR"] or self.state != "POSITION":
            return ordinary
        item = self.gmunvis.gear
        if item is None:
            return ordinary
        if ordinary is not None and math.dist(obs["self"]["pos"][:2], ordinary) < 12:
            return ordinary
        return item["pos"][:2]

    def act(self, obs):
        self._gmunvis_obs = obs
        action = super().act(obs)
        item = self.gmunvis.gear
        if item is None:
            return action
        me = obs["self"]
        if (math.dist(me["pos"], item["pos"]) > self.config["loot"]["pickup_radius"]
                or abs(me["pos"][2] - item["pos"][2]) > 4):
            return action
        slot = self.gmunvis.gear_slot
        if slot is False or slot is None:
            return action
        action["take"] = item["id"]
        if me["weapons"][slot] is not None:
            action["drop"] = slot
            if len(action.get("fire", ())) > slot:
                action["fire"][slot] = False
        return action


if __name__ == "__main__":
    mf.run(Bot)
