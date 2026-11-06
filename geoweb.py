#!/usr/bin/env python3
"""
geoweb: a map UI that moves the iPhone. Teleport to a pin, or run an itinerary
that follows real roads with realistic motion (see motion.py).

  python geoweb.py            then open http://127.0.0.1:8765

Same engine as geofake.py (pymobiledevice3 DVT location simulation, no admin).
Keeps the device session open and re-applies the chosen spot after any drop.
State (pin or running itinerary) is saved to state.json and resumed on restart,
so a crash or reboot puts the phone back where it should be at the current time.
Ctrl+C clears the simulation and exits.
"""
import argparse
import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import motion
from geofake import Session, log

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
PAGE = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()

STATE = {"target": None, "status": "starting", "device": "", "applied": None, "set_at": None, "trip": None}
TRAJ = None      # motion.Trajectory for the running itinerary
LOCK = threading.Lock()
WAKE = None      # asyncio.Event, created inside the loop
LOOP = None
TICK = 1.0


# ---------------------------------------------------------------- persistence
def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"target": STATE["target"],
                       "trip": {"t0": STATE["trip"]["t0"], "traj": TRAJ.to_json()} if TRAJ else None}, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        log(f"could not save state: {e}")


def load_state():
    global TRAJ
    if not os.path.exists(STATE_FILE):
        return
    try:
        j = json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception as e:
        log(f"ignoring unreadable state.json: {e}")
        return
    STATE["target"] = j.get("target")
    tr = j.get("trip")
    if tr:
        TRAJ = motion.Trajectory.from_json(tr["traj"])
        STATE["trip"] = {"t0": tr["t0"], **public(TRAJ)}
        el = time.time() - tr["t0"]
        if not TRAJ.loop and el >= TRAJ.total_s:
            STATE["target"] = [TRAJ.stops[-1].lat, TRAJ.stops[-1].lng]
            STATE["trip"], TRAJ = None, None
            log("resumed: itinerary already finished, holding at its last stop")
        else:
            log(f"resumed itinerary, {el:.0f}s in")
    elif STATE["target"]:
        log(f"resumed pin {STATE['target']}")


def public(traj):
    d = traj.describe()
    d.pop("route")  # big; served separately at /route
    return d


# ---------------------------------------------------------------- target resolution
def current_target(now):
    """What the phone should show right now. Returns (target, seconds until next check)."""
    trip = STATE["trip"]
    if not trip or TRAJ is None:
        return STATE["target"], 15
    el = now - trip["t0"]
    if not TRAJ.loop and el >= TRAJ.total_s:
        end = [TRAJ.stops[-1].lat, TRAJ.stops[-1].lng]
        finish_trip(end)
        log(f"arrived {end[0]:.6f}, {end[1]:.6f}")
        return end, 15
    p = TRAJ.pos_at(el)
    trip.update({"elapsed": el, "progress": p["progress"], "leg": p["leg"], "speed": p["speed"],
                 "heading": p["heading"], "s": p["s"],
                 "remaining_s": (TRAJ.total_s - (el % TRAJ.total_s if TRAJ.loop else el))})
    STATE["target"] = [p["lat"], p["lng"]]
    return STATE["target"], TICK


def finish_trip(hold_at):
    global TRAJ
    STATE["target"] = hold_at
    STATE["trip"], TRAJ = None, None
    save_state()


# ---------------------------------------------------------------- http
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
            with LOCK:
                return self._json(STATE)
        if self.path == "/route":
            with LOCK:
                return self._json(TRAJ.describe() if TRAJ else {})
        b = PAGE.encode()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        global TRAJ
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            if self.path == "/set":
                lat, lng = float(body["lat"]), float(body["lng"])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    return self._json({"error": "out of range"}, 400)
                with LOCK:
                    STATE["trip"], TRAJ = None, None
                    STATE["target"] = [lat, lng]
                reply = {"ok": True}
            elif self.path == "/trip":
                stops = body.get("stops") or [body.get("from"), body.get("to")]
                st = []
                for s in stops:
                    if isinstance(s, dict):
                        st.append(motion.Stop(float(s["lat"]), float(s["lng"]), str(s.get("name", ""))[:80],
                                              max(0.0, float(s.get("dwell_s") or 0))))
                    else:
                        st.append(motion.Stop(float(s[0]), float(s[1])))
                if len(st) < 2 or any(not (-90 <= s.lat <= 90 and -180 <= s.lng <= 180) for s in st):
                    return self._json({"error": "need 2+ valid stops"}, 400)
                mode = body.get("mode") or "drive"
                if mode not in motion.PROFILES:
                    return self._json({"error": "mode"}, 400)
                cruise = body.get("cruise") or body.get("speed")
                cruise = float(cruise) if cruise else None
                if cruise is not None and not (0.1 <= cruise <= 150):
                    return self._json({"error": "cruise out of range"}, 400)
                traj = motion.build(st, mode, body.get("seed"), cruise, bool(body.get("loop")),
                                    online=not body.get("straight"), log=log)
                with LOCK:
                    TRAJ = traj
                    STATE["trip"] = {"t0": time.time(), **public(traj)}
                log(f"itinerary: {len(st)} stops, {traj.total_m:.0f} m, {traj.total_s / 60:.1f} min, "
                    f"{mode}{' loop' if traj.loop else ''}, seed {traj.seed}")
                reply = {**traj.describe(), "t0": STATE["trip"]["t0"]}
            elif self.path == "/stop":
                with LOCK:
                    if TRAJ:
                        tgt, _ = current_target(time.time())
                        finish_trip(tgt)
                        log("itinerary stopped; holding here")
                reply = {"ok": True}
            elif self.path == "/clear":
                with LOCK:
                    STATE["trip"], TRAJ = None, None
                    STATE["target"] = None
                reply = {"ok": True}
            else:
                return self._json({"error": "no"}, 404)
        except (KeyError, ValueError, TypeError) as e:
            return self._json({"error": f"bad request: {e}"}, 400)
        save_state()
        LOOP.call_soon_threadsafe(WAKE.set)
        return self._json(reply)


# ---------------------------------------------------------------- device loop
async def apply_step(sess):
    """Push the current target to the phone if it changed. Returns seconds to wait."""
    with LOCK:
        tgt, wait = current_target(time.time())
        moving = STATE["trip"] is not None
    if tgt != STATE["applied"]:
        if tgt is None:
            if sess:
                await sess.clear()
            log("cleared")
        else:
            if sess:
                await sess.set(*tgt)
            if not moving:
                log(f"set {tgt[0]:.6f}, {tgt[1]:.6f}")
        STATE["applied"] = tgt
        STATE["set_at"] = time.time()
    STATE["status"] = "ok: moving" if moving else ("ok: holding" if tgt else "ok: connected")
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
    p.add_argument("--fresh", action="store_true", help="ignore state.json, start with no spoof")
    a = p.parse_args()

    if not a.fresh:
        load_state()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"open http://127.0.0.1:{a.port}", flush=True)
    try:
        asyncio.run(fake_loop() if a.no_device else device_loop(a.rsd, a.udid))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
