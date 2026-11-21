"""Phoenix V10 integrates the tactical modules while retaining the V5 entry points."""
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
        for event in obs["events"]:
            if event["type"] == "damaged" and event["bearing"] is not None:
                self.lanes.append((event["bearing"], event["amount"] / 45 * penalty))
        actual = me["aim"][0]
        options = [actual]
        for yaw, _ in self.lanes:
            options.extend((yaw, yaw - self.front_arc + 0.05, yaw + self.front_arc - 0.05))
        return min(options, key=lambda yaw: self.armor_risk(yaw) + 0.10 * angle_error(yaw, actual))

    def threats(self, obs):
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
        return self.planner.choose(obs, self.tracks, self.state, target, loot_goal)

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
