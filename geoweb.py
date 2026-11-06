#!/usr/bin/env python3
"""
geoweb: click a spot on a map, the iPhone goes there.

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

from geofake import Session, log

STATE = {"target": None, "status": "starting", "device": "", "applied": None, "set_at": None}
WAKE = None  # asyncio.Event, created inside the loop
LOOP = None

PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html"), encoding="utf-8").read()


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
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                return self._json({"error": "out of range"}, 400)
            STATE["target"] = [lat, lng]
        elif self.path == "/clear":
            STATE["target"] = None
        else:
            return self._json({"error": "no"}, 404)
        LOOP.call_soon_threadsafe(WAKE.set)
        return self._json({"ok": True})


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
                tgt = STATE["target"]
                if tgt != STATE["applied"]:
                    if tgt is None:
                        await sess.clear()
                        log("cleared")
                    else:
                        await sess.set(*tgt)
                        log(f"set {tgt[0]:.6f}, {tgt[1]:.6f}")
                    STATE["applied"] = tgt
                    STATE["set_at"] = time.time()
                    STATE["status"] = "ok: holding" if tgt else "ok: connected"
                WAKE.clear()
                try:
                    await asyncio.wait_for(WAKE.wait(), timeout=15)  # periodic keepalive check
                except asyncio.TimeoutError:
                    pass
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

    async def fake_loop():
        global WAKE, LOOP
        LOOP = asyncio.get_running_loop()
        WAKE = asyncio.Event()
        STATE["status"] = "ok: no-device mode"
        while True:
            await WAKE.wait()
            WAKE.clear()
            STATE["applied"] = STATE["target"]
            log(f"(fake) {STATE['target']}")

    try:
        asyncio.run(fake_loop() if a.no_device else device_loop(a.rsd, a.udid))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
