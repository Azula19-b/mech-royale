"""mechfight/1 line protocol wrapper (standard library only)."""
import json
import queue
import sys
import threading
import traceback


def _send(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()


def run(factory):
    q = queue.Queue()

    def reader():
        for line in sys.stdin:
            if line.strip():
                q.put(line)
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()
    bot = None
    last = {}
    while True:
        raw = q.get()
        batch = [raw]
        while True:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        latest = None
        for line in batch:
            if line is None:
                return
            try:
                msg = json.loads(line)
            except (ValueError, TypeError):
                continue
            if msg.get("type") == "hello":
                try:
                    bot = factory(msg)
                except Exception:
                    traceback.print_exc(file=sys.stderr)
                _send({"type": "ready", "name": getattr(bot, "name", "mech-survivor")[:32]})
            elif msg.get("type") == "obs":
                latest = msg
            elif msg.get("type") == "end":
                return
        if latest is None:
            continue
        try:
            act = dict(bot.act(latest) if bot else {})
            last = {k: act[k] for k in ("move", "aim", "boost") if k in act}
        except Exception:
            traceback.print_exc(file=sys.stderr)
            act = dict(last)
        act["tick"] = latest["tick"]
        try:
            _send(act)
        except (TypeError, ValueError):
            _send({"tick": latest["tick"], **last})
