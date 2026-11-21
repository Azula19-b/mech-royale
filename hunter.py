"""Phoenix adaptive engagement and damage-aware escape experiments."""
from collections import deque
import math
import os

from example_bot import Bot as Baseline, bearing
from hunter_combat import FireControl
from hunter_movement import Movement
from intelligence import Intelligence, angle_error, pressure_weight


class Bot(Baseline):
    name = "phoenix-v10-hunter"

    def __init__(self, hello):
        super().__init__(hello)
        self.flags = {key: os.getenv("V10_" + key + "_ENABLED", "1").lower() not in ("0", "false")
                      for key in ("TRACKING", "COMBAT", "MOVEMENT", "TACTICS")}
        self.v13 = {key: os.getenv("V13_" + key + "_ENABLED", default).lower() not in ("0", "false")
                    for key, default in (("ENGAGEMENT", "0"), ("ESCAPE", "1"))}
        if any(self.v13.values()):
            self.name = "phoenix-v13-survival"
        self.engagement = None
        self.trade = deque(maxlen=30)
        self.disengage_until = -math.inf
        self.intelligence = Intelligence(self.config, self.arena)
        self.fire_control = FireControl(self)
        self.planner = Movement(self)
        self.state = "POSITION"
        self.state_until = -1
        self.baseline_movement = super().movement
        self.planned_armor = 0
        self.lanes = []

    def tactics(self, obs, tracks):
        me, zone = obs["self"], obs["zone"]
        now = obs["t"]
        active = [t for t in tracks if not t.covered and t.distance < t.reach * 0.76]
        nearby = [t for t in tracks if t.distance < 55 and (not t.covered or t.closing > 4)]
        crossfire = any(angle_error(bearing(me["pos"], a.actor["pos"]), bearing(me["pos"], b.actor["pos"])) > 1.45
                        for i, a in enumerate(active) for b in active[i + 1:])
        next_gap = math.dist(me["pos"][:2], zone["next_center"]) - zone["next_radius"]
        margin = zone["radius"] - math.dist(me["pos"][:2], zone["center"])
        urgent = not me["in_zone"] or (next_gap > 0 and (margin < 9 or
                 zone["shrink_end"] - now < next_gap / max(1, me["stats"]["walk_speed"] * 0.65) + 4))
        if urgent:
            proposed = "EMERGENCY_ZONE"
        elif len(active) >= 2 or len(nearby) >= 2 or crossfire:
            proposed = "EVADE"
        elif active:
            proposed = "ENGAGE"
        elif me["hp"] < me["max_hp"] * 0.90 and not nearby and (
                tracks or now - self.last_damage < me["stats"]["regen_delay"] + 1):
            proposed = "RECOVER"
        else:
            proposed = "POSITION"
        immediate = proposed in ("EMERGENCY_ZONE", "EVADE") or (self.state == "RECOVER" and active)
        if proposed != self.state and (immediate or now >= self.state_until or self.state == "EMERGENCY_ZONE"):
            self.state = proposed
            self.state_until = now + (0.65 if proposed == "EVADE" else 0.4)
        return active

    def armor_risk(self, yaw):
        return sum(weight for direction, weight in self.lanes if angle_error(direction, yaw) > self.front_arc)

    def setup_armor(self, obs, tracks):
        me = obs["self"]
        stats = me["stats"]
        self.front_arc = math.radians(stats["front_arc_deg"]) / 2
        penalty = max(0, stats["rear_mult"] - 1) * (1 - stats["armor"])
        self.lanes = [(bearing(me["pos"], t.actor["pos"]), pressure_weight(t, t.distance) * penalty)
                      for t in tracks if not t.covered]
        reference_damage = self.config["weapons"]["gun"]["damage"]
        for event in obs["events"]:
            if event["type"] == "damaged" and event["bearing"] is not None:
                self.lanes.append((event["bearing"], event["amount"] / reference_damage * penalty))
        actual = me["aim"][0]
        options = [actual]
        for yaw, _ in self.lanes:
            options.extend((yaw, yaw - self.front_arc + 0.05, yaw + self.front_arc - 0.05))
        return min(options, key=lambda yaw: self.armor_risk(yaw) + 0.10 * angle_error(yaw, actual))

    def engagement_target(self, obs, target):
        """Keep firing while declining a losing pursuit or an opening flank."""
        me = obs["self"]
        now = obs["t"]
        if target is None:
            self.engagement = self.state
            return self.state, None
        stats = me["stats"]
        weapons = [weapon for weapon in me["weapons"] if weapon is not None]
        finish_threshold = self.config["weapons"]["gun"]["damage"] * (
            1 + self.config["cubes"]["damage_per_cube"] * min(me["cubes"], self.config["cubes"]["stat_cap"]))
        finish = target.actor.get("hp", math.inf) <= finish_threshold and target.distance < 85
        gun_ready = any(w["kind"] == "gun" and w["cooldown"] <= 0.25
                        and not w["reload"] and w["ammo"] for w in weapons)
        if finish and gun_ready:
            self.engagement = "FINISH"
            return self.state, target
        recent_in = sum(amount_in for _, amount_in, _ in self.trade)
        recent_out = sum(amount_out for _, _, amount_out in self.trade)
        reinforcement = False
        if len(self.tracks) > 1 and me["hp"] < me["max_hp"] * 0.80:
            body = (*me["pos"][:2], me["pos"][2] + stats["height"] / 2)
            muzzle = self.config["mech"]["muzzle_height"]
            for track in self.tracks:
                if track is target or track.distance > 100 or track.closing < 2 or track.age > 2:
                    continue
                x, y, z = track.predict(1.1)
                if self.arena.clear((x, y, z + muzzle), body, spacing=5):
                    reinforcement = True
                    break
        losing_trade = (me["hp"] < 0.55 * me["max_hp"] and recent_in > 95
                        and recent_in > 1.5 * recent_out + 45)
        # Even a favorable exchange is unsafe when the health budget runs out first.
        critical_exposure = (me["hp"] < 0.38 * me["max_hp"] and recent_in >= 50
                             and me["hp"] < 2.6 * recent_in)
        if losing_trade or critical_exposure or reinforcement:
            self.disengage_until = now + 0.55
        if now < self.disengage_until:
            self.engagement = "DISENGAGE"
            return "EVADE", None
        self.engagement = self.state
        return self.state, target

    def threats(self, obs):
        if self.v13["ENGAGEMENT"]:
            now = obs["t"]
            while self.trade and now - self.trade[0][0] > 1.6:
                self.trade.popleft()
            damage_in = sum(e["amount"] for e in obs["events"] if e["type"] == "damaged" and e.get("weapon") != "zone")
            damage_out = sum(e["amount"] for e in obs["events"] if e["type"] == "hit")
            if damage_in or damage_out:
                self.trade.append((now, damage_in, damage_out))
        enemies = super().threats(obs)
        if any(self.flags.values()):
            self.tracks = self.intelligence.update(obs, self.flags["TRACKING"])
            self.active = self.tactics(obs, self.tracks) if self.flags["TACTICS"] else []
            self.planned_armor = self.setup_armor(obs, self.tracks)
        return enemies

    def loot_goal(self, obs, enemies):
        if self.flags["TACTICS"] and self.state != "POSITION":
            return None
        return super().loot_goal(obs, enemies)

    def movement(self, obs, enemies, loot_goal=None):
        if not self.flags["MOVEMENT"]:
            return super().movement(obs, enemies, loot_goal)
        target = self.active[0] if self.state == "ENGAGE" and len(self.active) == 1 else None
        state = self.state
        if self.v13["ENGAGEMENT"]:
            state, target = self.engagement_target(obs, target)
        return self.planner.choose(obs, self.tracks, state, target, loot_goal)

    def armor_yaw(self, obs, enemies, default):
        if self.flags["TACTICS"] and self.lanes:
            return (self.planned_armor + math.pi) % math.tau - math.pi
        return super().armor_yaw(obs, enemies, default)

    def defense(self, obs, enemies, armor, act):
        if not self.flags["COMBAT"]:
            return super().defense(obs, enemies, armor, act)
        self.fire_control.apply(obs, self.tracks, self.state, act, self.armor_risk)


if __name__ == "__main__":
    import mechfight as mf

    mf.run(Bot)
