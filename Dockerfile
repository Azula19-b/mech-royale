# Build from this folder: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY arena.py combat.py example_bot.py mechfight.py hunter.py hunter_combat.py hunter_movement.py intelligence.py ./
ENTRYPOINT ["python3", "-u", "/bot/hunter.py"]
