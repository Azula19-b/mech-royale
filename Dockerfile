# Build from this folder: docker build -t phoenix-v18 .
# The scorer supplies linux/amd64, 1 CPU, 512 MB, a read-only root, and /arena:ro.
FROM --platform=linux/amd64 python:3.13-slim
WORKDIR /bot
COPY arena.py combat.py example_bot.py mechfight.py hunter.py hunter_combat.py hunter_movement.py intelligence.py gmunvis.py phoenix_v18.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
ENTRYPOINT ["python3", "-u", "/bot/phoenix_v18.py"]
