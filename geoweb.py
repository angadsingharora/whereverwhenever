#!/usr/bin/env python3
"""
geoweb: click a spot on a map, the iPhone goes there. Or pick A and B and drive.

  python geoweb.py            then open http://127.0.0.1:8765

Same engine as geofake.py (pymobiledevice3 DVT location simulation, no admin).
Keeps the device session open and re-applies the chosen spot after any drop.
Ctrl+C clears the simulation and exits.
"""
import argparse
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from geofake import Session, haversine_m, log

STATE = {"target": None, "status": "starting", "device": "", "applied": None, "set_at": None, "trip": None}
WAKE = None  # asyncio.Event, created inside the loop
LOOP = None
TICK = 1.0  # seconds between position updates while on a trip

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()


def valid(p):
    return isinstance(p, (list, tuple)) and len(p) == 2 and -90 <= float(p[0]) <= 90 and -180 <= float(p[1]) <= 180


def current_target(now):
    """Resolve what the phone should show right now. Returns (target, seconds until next check)."""
    trip = STATE["trip"]
    if not trip:
        return STATE["target"], 15
    f = 1.0 if trip["dist"] <= 0 else min(1.0, (now - trip["t0"]) * trip["speed"] / trip["dist"])
    a, b = trip["from"], trip["to"]
    pos = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]
    trip["progress"] = f
    trip["remaining_s"] = max(0.0, trip["dist"] * (1 - f) / trip["speed"])
    if f >= 1.0:
        STATE["target"] = b
        STATE["trip"] = None
        log(f"arrived {b[0]:.6f}, {b[1]:.6f}")
        return b, 15
    STATE["target"] = pos
    return pos, TICK


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/status":
            return self._json(STATE)
        b = PAGE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/set":
            lat, lng = float(body["lat"]), float(body["lng"])
            if not valid([lat, lng]):
                return self._json({"error": "out of range"}, 400)
            STATE["trip"] = None
            STATE["target"] = [lat, lng]
        elif self.path == "/trip":
            a, b, speed = body.get("from"), body.get("to"), float(body.get("speed") or 0)
            if not (valid(a) and valid(b)) or not (0.1 <= speed <= 500):
                return self._json({"error": "bad trip"}, 400)
            a = [float(a[0]), float(a[1])]
            b = [float(b[0]), float(b[1])]
            dist = haversine_m(a, b)
            STATE["trip"] = {"from": a, "to": b, "speed": speed, "t0": time.time(), "dist": dist,
                             "progress": 0.0, "remaining_s": dist / speed}
            log(f"trip {a[0]:.5f},{a[1]:.5f} -> {b[0]:.5f},{b[1]:.5f}  {dist:.0f} m at {speed:.1f} m/s ({dist / speed:.0f} s)")
        elif self.path == "/stop":
            STATE["trip"] = None  # target stays at the last interpolated point
            log("trip stopped")
        elif self.path == "/clear":
            STATE["trip"] = None
            STATE["target"] = None
        else:
            return self._json({"error": "no"}, 404)
        LOOP.call_soon_threadsafe(WAKE.set)
        return self._json({"ok": True})


async def apply_step(sess):
    """One pass: push the current target to the phone if it changed. Returns wait seconds."""
    moving = STATE["trip"] is not None
    tgt, wait = current_target(time.time())
    if tgt != STATE["applied"]:
        if tgt is None:
            if sess:
                await sess.clear()
            log("cleared")
        else:
            if sess:
                await sess.set(*tgt)
            if not moving or STATE["trip"] is None:  # don't spam the log once a second mid-trip
                log(f"set {tgt[0]:.6f}, {tgt[1]:.6f}")
        STATE["applied"] = tgt
        STATE["set_at"] = time.time()
    STATE["status"] = "ok: moving" if STATE["trip"] else ("ok: holding" if tgt else "ok: connected")
    return wait


async def wait_wake(timeout):
    WAKE.clear()
    try:
        await asyncio.wait_for(WAKE.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def device_loop(rsd, udid):
    global WAKE, LOOP
    LOOP = asyncio.get_running_loop()
    WAKE = asyncio.Event()
    sess = Session(rsd, udid)
    backoff = 2
    try:
        while True:
            try:
                if sess.loc is None:
                    STATE["status"] = "connecting to phone (unlock it)"
                    await sess.open()
                    STATE["device"] = getattr(sess, "device", "")
                    STATE["status"] = "ok: connected"
                    backoff = 2
                    STATE["applied"] = None
                await wait_wake(await apply_step(sess))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATE["status"] = f"err: {type(e).__name__}: {e}"[:160]
                STATE["applied"] = None
                log(f"link lost: {e!r}; retry in {backoff}s")
                await sess.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
    finally:
        try:
            await sess.clear()
        except Exception:
            pass
        await sess.close()


async def fake_loop():
    global WAKE, LOOP
    LOOP = asyncio.get_running_loop()
    WAKE = asyncio.Event()
    while True:
        await wait_wake(await apply_step(None))
        STATE["status"] = STATE["status"].replace("ok: connected", "ok: no-device mode")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rsd", nargs=2, metavar=("ADDR", "PORT"), help="reuse an existing start-tunnel")
    p.add_argument("--udid", help="target device if more than one is plugged in")
    p.add_argument("--no-device", action="store_true", help="UI only, for testing without a phone")
    a = p.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"open http://127.0.0.1:{a.port}", flush=True)

    try:
        asyncio.run(fake_loop() if a.no_device else device_loop(a.rsd, a.udid))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
