"""Short-horizon aim solutions for defensive fire."""
import math


def intercept(origin, pos, velocity, speed, offset, gravity=0):
    """Return aim angles, travel time and the projected hit point."""
    delta = tuple(p - o for p, o in zip(pos, origin))
    time = max(0, (math.hypot(*delta) - offset) / speed)
    for _ in range(10):
        dx, dy, dz = (d + v * time for d, v in zip(delta, velocity))
        dz += gravity * time * time / 2
        next_time = max(0, (math.hypot(dx, dy, dz) - offset) / speed)
        if abs(next_time - time) < 0.000001:
            return (math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))), time, (
                origin[0] + dx, origin[1] + dy, origin[2] + dz - gravity * time * time / 2)
        time = next_time
    return None
