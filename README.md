# Mech Survivor

A standard-library-only `mechfight/1` bot focused on placement. The controller navigates toward a projected safe-zone sector, biases away from visible enemy groups, dodges visible projectile trajectories and recent weapon sounds, and keeps its torso facing an attacker when it is not taking a shot. It leads gun fire against moving targets and spends boost mainly to escape close threats or make the zone.

Tune policy thresholds and priorities in `strategy.json`. Weapon damage, speeds, ranges, movement stats, zone timings and upgrade availability are read from each match's `hello.config`.

The runtime requires the match map mount at `/arena` only when a bot elects to load map geometry. This policy uses the live zone and observation geometry and needs no map file at runtime.

## Build and conformance check

```sh
docker build --platform linux/amd64 -t mech-survivor .
python3 check/check.py -- python3 -u bot.py
python3 check/check.py --map-dir /arena -- docker run --rm -i --network none --cpus 1 --memory 512m --read-only --tmpfs /tmp:rw,size=64m --user 65534:65534 -v "$PWD/arena:/arena:ro" mech-survivor
```

The checker replays recorded observations and confirms protocol and timing behavior. It is not a match simulator and does not measure placement.

## Submit

Create a public GitHub repository from these files with `Dockerfile` at its root, push it, then submit the repository URL on the Mech Royale track page. Evaluate practice results through the track's scorer. Do not infer match performance from `check.py`.
