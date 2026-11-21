"""Bounded estimates from server-supplied actors and combat events."""
from dataclasses import dataclass
import math


def angle_error(a, b):
    return abs((a - b + math.pi) % math.tau - math.pi)


def bounded(vector, limit):
    scale = min(1, limit / max(math.hypot(*vector), 1e-9))
    return tuple(value * scale for value in vector)


@dataclass
class Track:
    actor: dict
    velocity: tuple
    acceleration: tuple
    distance: float
    closing: float
    reach: float
    damage_rate: float
    facing: float
    covered: bool
    sight_likely: bool
    reliable_at: float | None
    age: float

    @property
    def pressure(self):
        return self.damage_rate * self.facing * (1 + max(0, self.closing) / 35)

    def predict(self, horizon):
        # Damp acceleration quickly. Unobserved inputs are not extrapolated across ticks.
        correction = 0.5 * min(horizon, 0.3) ** 2
        return tuple(p + v * horizon + a * correction
                     for p, v, a in zip(self.actor["pos"], self.velocity, self.acceleration))

    def uncertainty(self, horizon):
        return 0.4 + 2 * horizon * horizon + self.age * 1.5 + (0 if self.sight_likely else 1.5)


class Intelligence:
    def __init__(self, config, arena):
        self.config = config
        self.arena = arena
        self.previous = {}

    def update(self, obs, tracking):
        me = obs["self"]
        stats, base = me["stats"], self.config["mech"]
        now = obs["t"]
        eye = (*me["pos"][:2], me["pos"][2] + stats["eye_height"])
        vision = self.config["vision"]
        next_previous = {}
        tracks = []
        for actor in obs["mechs"]:
            if actor["kind"] != "mech":
                continue
            identifier = actor["id"]
            x, y, z = actor["pos"]
            dx, dy = x - eye[0], y - eye[1]
            yaw = math.atan2(dy, dx)
            distance = math.hypot(dx, dy)
            center = (x, y, z + base["height"] / 2)
            covered = not self.arena.clear(eye, center, spacing=3)
            near = distance <= vision["near_range"] + stats.get("near_range_bonus", 0)
            in_view = (distance <= vision["range"] + stats.get("vision_bonus", 0)
                       and angle_error(yaw, me["aim"][0]) <= math.radians(vision["fov_deg"]) / 2)
            # Protocol has no visibility bit. Outside proximity this omits canopy occlusion.
            likely = not covered and (near or in_view)
            v = bounded(actor["vel"], base["boost_speed"] * 1.5 + base["max_rise_speed"])
            prior = self.previous.get(identifier)
            a = (0, 0, 0)
            reliable_at = now if likely else (prior[2] if prior else None)
            if tracking and likely and prior and prior[2] == prior[0] and 0 < now - prior[0] < 0.35:
                a = bounded(tuple((u - old) / (now - prior[0]) for u, old in zip(v, prior[1])),
                            base["ground_accel"] * 0.45)
            if tracking:
                next_previous[identifier] = (now, v, reliable_at)
            age = min(vision["memory_s"], now - reliable_at) if reliable_at is not None else vision["memory_s"]
            if not tracking:
                age = 0
            weapons = self.config["weapons"]
            held = [weapons[k] for k in actor.get("weapons", ())] or [weapons["gun"]]
            reach = max(w.get("range", min(w.get("speed", 0) * w.get("ttl", 0),
                                           w.get("speed", 0) ** 2 / max(1, w.get("gravity", 1)))) for w in held)
            damage = max(w["damage"] / (w["cooldown"] + w.get("reload", 0) / w.get("mag", 1)
                                         + w.get("charge", 0)) for w in held)
            facing = 1 if angle_error(actor["aim"][0], yaw + math.pi) < 0.55 else 0.48
            closing = ((me["vel"][0] - v[0]) * dx + (me["vel"][1] - v[1]) * dy) / max(1, distance)
            tracks.append(Track(actor, v, a, distance, closing, reach, damage, facing,
                                covered, likely, reliable_at, age))
        self.previous = next_previous
        return tracks


def pressure_weight(track, distance):
    return (track.pressure / 100 * (80 / (distance + 22)) ** 2
            * max(0, 1 - distance / (track.reach + 25)) / (1 + 0.12 * track.age))
