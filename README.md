# Project Phoenix

Phoenix starts from the official Python example at the local `phoenix-baseline` tag. The development branch is `experiment/phoenix`. The subtree export is `export/phoenix`, with `Dockerfile` at the repository root.

The controller ranks escape headings using projected squad pressure, cover, walking clearance, and zone deadlines. It keeps torso aim independent of movement, uses defensive weapons when it expects a clear shot, and limits boosts to ground movement with room to accelerate. Map geometry and a ground graph are loaded during hello.

## Validation

Every row passed the official 300-observation conformance check under Docker on linux/amd64 with one CPU, 512 MB RAM and no swap, no network, UID/GID 65534, no capabilities, a read-only root, and a 64 MB writable `/tmp`. Every run answered all 300 observations within budget. The baseline entrypoint and wrapper were identical to the kit copies.

| Checkpoint | Commit | Regression tests | Docker conformance p50 / p99 / max (ms) | Synthetic p50 / p99 / max (ms) |
| --- | --- | ---: | ---: | ---: |
| Starter | `3cde049` | Official checker | 0.6 / 0.8 / 0.9 | 0.68 / 0.87 / 4.99 |
| A · Front armor and strafe | `283c073` | 3 | 0.5 / 0.7 / 0.8 | 0.51 / 0.68 / 3.15 |
| B · Squad escape routes | `82f8f5c` | 5 | 1.0 / 1.6 / 1.8 | 1.16 / 1.64 / 2.79 |
| C · Predictive gunfire | `ff35ee8` | 7 | 0.9 / 1.5 / 2.9 | 1.11 / 1.70 / 3.32 |
| D · Disengagement | `37009f1` | 9 | 1.1 / 2.2 / 3.1 | 1.15 / 1.72 / 8.01 |
| E · Navigation and zone | `c456925` | 12 | 1.7 / 2.3 / 2.4 | 1.70 / 2.42 / 4.18 |
| F · Weapons and upgrades | `946db8b` | 15 | 1.7 / 2.2 / 2.3 | 1.68 / 2.42 / 3.95 |
| Export candidate | Development tip | 18 | 1.8 / 2.3 / 2.7 | 1.78 / 2.52 / 3.08 |

All eight synthetic comparison runs processed 2,400 sequential actions each across pursuit, pincer, cover, evacuation, and map-edge scenarios, with zero late or invalid actions, crashes, or logged controller exceptions. Timings include protocol I/O on this host. Candidate hello took 1.58 s in the official Docker check, including startup.

| Synthetic candidate check | Result |
| --- | --- |
| Movement under fire | Moved on 638 of 640 exposed ticks. The two stationary ticks were in evacuation. |
| Front armor | 4 rear hits out of 108 weapon hits |
| Mobility | 5 collision ticks, zero stuck ticks |
| Energy | 419.6 boost energy spent, 15.3 spent on ticks slower than walking, zero jump commands |
| Zone | 10 ticks outside during transition, zero outside after shrink completion |
| Cover | 1,760 ticks behind solid cover |
| Damage proxies | 2,000 received, 4,680 dealt, one HP reset during the pincer |

The separate 4,800-action stress run with 15 opponents had p50 / p99 / max latency of 2.33 / 3.16 / 4.40 ms and zero late or invalid actions, crashes, or logged controller exceptions. It also logged eight HP resets and 157 ticks outside after shrink completion. That sustained squad scenario remains unresolved.

The corner clearance correction is the reason to select the export candidate over Stage F. In the same evacuation scenario it reduced collision ticks from 399 to 2 and ticks outside after shrink completion from 340 to zero. The protocol fallback also carries forward only previously serialized move and aim values, drops Boost and Fire on recoverable errors, and echoes the observation tick.

The recorded checker only measures protocol compliance and latency. The synthetic harness drives observations from actions, using simplified ground movement, solid cover and damage proxies. It omits aerial dynamics, the official squad AI, projectile physics, and enemy deaths. No documented local runner for the official squad was found. The sustained pincer still exhausts a health pool, and evading narrow geometry relies on sampled clearance. The ground graph assumes base mech dimensions and prioritizes walking routes. Only official 48-match practice scoring can establish a placement score or whether Phoenix beats the published starter or the 6.31 target.

## Reproduce from the development checkout

Run from the repository root. The checked-in official kit assets under `submission/` supply the checker, fixtures and map. On the export branch, build from `.` and use the checker and map from a kit checkout.

```bash
python3 -B -m unittest discover -s phoenix
docker build --platform linux/amd64 -t mech-phoenix:candidate phoenix

python3 submission/check/check.py --map-dir /arena -- docker run --rm -i \
  --platform linux/amd64 --network none --cpus 1 --memory 512m --memory-swap 512m \
  --pids-limit 128 --cap-drop ALL --read-only --tmpfs /tmp:rw,size=64m \
  --user 65534:65534 -v "$PWD/submission/arena:/arena:ro" mech-phoenix:candidate

python3 -B phoenix/diagnose.py -- docker run --rm -i \
  --platform linux/amd64 --network none --cpus 1 --memory 512m --memory-swap 512m \
  --pids-limit 128 --cap-drop ALL --read-only --tmpfs /tmp:rw,size=64m \
  --user 65534:65534 -v '{map}:/arena:ro' mech-phoenix:candidate
```

The diagnostic harness reports to stderr. Put `--scenario squad --steps 4800` before the Docker command separator to exercise all 15 opponent records for a full-length stream.
