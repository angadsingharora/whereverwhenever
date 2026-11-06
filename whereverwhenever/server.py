#!/usr/bin/env python3
"""
whereverwhenever bridge: the one local process that talks to the phone.

  whereverwhenever            (or: python geoweb.py)

Serves the UI at http://127.0.0.1:8765 and accepts requests from the hosted site,
keeps the phone's location simulation alive, runs itineraries from motion.py,
saves state to ~/.whereverwhenever/state.json so a restart resumes mid-route,
and, if ANTHROPIC_API_KEY is set, plans itineraries from plain English.
Ctrl+C clears the simulation and exits.
"""
import argparse
import asyncio
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import motion
from . import link
from .cli import Session, log

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(os.path.expanduser("~"), ".whereverwhenever")
STATE_FILE = os.path.join(HOME, "state.json")
PAGE = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
SITE = "https://whereverwhenever.vercel.app"
ALLOWED_ORIGINS = {SITE, "https://geofake-pi.vercel.app", "https://whereverwhenever.app"}
ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$")

STATE = {"target": None, "status": "starting", "device": "", "link": "", "link_pref": "auto", "applied": None, "set_at": None, "trip": None,
         "ai": False, "version": "0.2.0"}
TRAJ = None      # motion.Trajectory for the running itinerary
LOCK = threading.Lock()
WAKE = None      # asyncio.Event, created inside the loop
LOOP = None
TICK = 1.0


# ---------------------------------------------------------------- persistence
def save_state():
    try:
        os.makedirs(HOME, exist_ok=True)
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
                 "accel": p["accel"], "heading": p["heading"], "s": p["s"],
                 "remaining_s": (TRAJ.total_s - (el % TRAJ.total_s if TRAJ.loop else el))})
    STATE["target"] = [p["lat"], p["lng"]]
    return STATE["target"], TICK


def finish_trip(hold_at):
    global TRAJ
    STATE["target"] = hold_at
    STATE["trip"], TRAJ = None, None
    save_state()


# ---------------------------------------------------------------- geocoding
def geocode(q, near=None):
    """Nominatim search with progressively simpler forms of the query. Biased towards `near`.
    Returns (lat, lng, short name) or None."""
    base = re.sub(r"\s*\([^)]*\)", "", q).strip()  # drop "(RSF)"-style asides
    parts = [x.strip() for x in base.split(",") if x.strip()]
    variants = [q, base]
    if len(parts) > 2:
        variants += [", ".join(parts[:1] + parts[-1:]), ", ".join(parts[1:]), ", ".join(parts[:2])]
    if parts:
        variants.append(parts[0])
    seen = set()
    for v in variants:
        if v in seen or not v:
            continue
        seen.add(v)
        r = _geocode_once(v, near)
        if r:
            return r
        time.sleep(1.0)  # Nominatim rate limit
    return None


def _geocode_once(q, near):
    params = {"format": "json", "limit": 1, "q": q}
    if near:
        d = 0.25
        params["viewbox"] = f"{near[1]-d},{near[0]+d},{near[1]+d},{near[0]-d}"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "whereverwhenever/0.2 (location simulation testing)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.load(r)
    if not j:
        return None
    return float(j[0]["lat"]), float(j[0]["lon"]), j[0]["display_name"].split(",")[0]


# ---------------------------------------------------------------- AI planner
PLAN_SYSTEM = """You turn a casual description of someone's movements into an itinerary for a location
simulator. Output only JSON, no prose, matching:

{"mode": "walk"|"bike"|"drive",
 "loop": bool,
 "cruise_mph": number|null,
 "stops": [{"query": string, "name": string, "dwell_min": number, "use_current": bool}]}

Rules:
- "query" is what you'd type into OpenStreetMap search: the place's common name plus the city,
  e.g. "Blue Bottle Coffee, Berkeley" or "Rockridge BART, Oakland". No abbreviations in
  parentheses, no descriptions, at most one comma. "name" is a short label (2-4 words).
- The first stop is where the journey starts. If the user doesn't say where they start, or says
  "from here"/"from where I am", make the first stop use_current=true with query "" and name "Here".
- dwell_min is how long they linger at that stop; 0 for the start unless looping. Infer sensible
  values ("grab a coffee" = 15, "gym" = 60, "work" = 480) when not stated.
- loop=true only if they describe going round repeatedly or returning to start and repeating.
- cruise_mph only if they state a speed; otherwise null.
- mode: infer from verbs/context; default drive for multi-km trips, walk for short ones."""


KEY_FILE = os.path.join(HOME, "key")


def ai_available():
    """ANTHROPIC_API_KEY from the environment, else ~/.whereverwhenever/key (one line)."""
    if not os.environ.get("ANTHROPIC_API_KEY") and os.path.exists(KEY_FILE):
        k = open(KEY_FILE, encoding="utf-8").read().strip()
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def plan_with_ai(text, near, now_local):
    import anthropic

    client = anthropic.Anthropic()
    ctx = f"Context: it is {now_local}. "
    ctx += f"The phone is currently near {near[0]:.4f}, {near[1]:.4f}. " if near else ""
    resp = client.messages.create(
        model="claude-opus-5-5",
        max_tokens=2000,
        system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": ctx + "\n\nPlan: " + text}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("the model declined to plan that")
    out = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("no plan in the model's reply")
    plan = json.loads(m.group(0))
    stops = []
    for s in plan.get("stops", []):
        if s.get("use_current"):
            if not near:
                raise RuntimeError("plan starts 'here' but the phone has no location yet; teleport first")
            stops.append({"lat": near[0], "lng": near[1], "name": s.get("name") or "Here",
                          "dwell_s": 60 * float(s.get("dwell_min") or 0)})
            continue
        g = geocode(s.get("query", ""), near)
        if not g:
            raise RuntimeError(f"couldn't find '{s.get('query')}'")
        stops.append({"lat": g[0], "lng": g[1], "name": s.get("name") or g[2],
                      "dwell_s": 60 * float(s.get("dwell_min") or 0), "query": s.get("query")})
        time.sleep(1.0)  # Nominatim rate limit
    if len(stops) < 2:
        raise RuntimeError("need at least two places")
    mode = plan.get("mode") if plan.get("mode") in motion.PROFILES else "drive"
    cruise = plan.get("cruise_mph")
    return {"stops": stops, "mode": mode, "loop": bool(plan.get("loop")),
            "cruise": float(cruise) * 0.44704 if cruise else None,
            "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}}


# ---------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        o = self.headers.get("Origin", "")
        if o in ALLOWED_ORIGINS or ORIGIN_RE.match(o):
            self.send_header("Access-Control-Allow-Origin", o)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

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
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(min(n, 1 << 20)) or b"{}")
            if not isinstance(body, dict):
                return self._json({"error": "bad request: body must be a JSON object"}, 400)
            if self.path == "/set":
                lat, lng = float(body["lat"]), float(body["lng"])
                if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                    return self._json({"error": "out of range"}, 400)
                with LOCK:
                    STATE["trip"], TRAJ = None, None
                    STATE["target"] = [lat, lng]
                reply = {"ok": True}
            elif self.path in ("/trip", "/preview"):
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
                if self.path == "/preview":  # plan only: same seed on /trip reproduces it exactly
                    return self._json(traj.describe())
                with LOCK:
                    TRAJ = traj
                    STATE["trip"] = {"t0": time.time(), **public(traj)}
                log(f"itinerary: {len(st)} stops, {traj.total_m:.0f} m, {traj.total_s / 60:.1f} min, "
                    f"{mode}{' loop' if traj.loop else ''}, seed {traj.seed}")
                reply = {**traj.describe(), "t0": STATE["trip"]["t0"]}
            elif self.path == "/plan":
                if not STATE["ai"]:
                    return self._json({"error": "AI planning is off: set ANTHROPIC_API_KEY and pip install anthropic"}, 400)
                text = str(body.get("text", "")).strip()[:2000]
                if not text:
                    return self._json({"error": "say where you're going"}, 400)
                near = STATE["applied"] or body.get("near")
                try:
                    reply = plan_with_ai(text, near, time.strftime("%A %H:%M"))
                except Exception as e:
                    log(f"plan failed: {e!r}")
                    return self._json({"error": str(e)[:300]}, 400)
                log(f"planned {len(reply['stops'])} stops from text ({reply['usage']['in']}+{reply['usage']['out']} tok)")
                return self._json(reply)
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
            elif self.path == "/link":
                m = body.get("mode")
                if m not in link.MODES:
                    return self._json({"error": "mode"}, 400)
                if m == "wifi" and not link.wifi_paired():
                    return self._json({"error": "Not paired for Wi-Fi yet. Plug the cable in once, it pairs on its own."}, 409)
                with LOCK:
                    link.PREF["mode"] = STATE["link_pref"] = m
                reply = {"ok": True}
            else:
                return self._json({"error": "no"}, 404)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError) as e:
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
                    STATE["status"] = "connecting to phone (plug in, unlock)"
                    await sess.open()
                    STATE["device"] = getattr(sess, "device", "")
                    STATE["link"] = getattr(sess, "link", "")
                    STATE["status"] = "ok: connected"
                    backoff = 2
                    STATE["applied"] = None
                await wait_wake(await apply_step(sess))
                pref = link.PREF["mode"]
                if sess.loc is not None and pref != "auto" and sess.link and sess.link != pref:
                    log(f"switching link to {pref}")
                    await sess.close()
                    STATE["status"] = f"switching to {pref}"
                    STATE["applied"] = None
                    STATE["link"] = ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                STATE["status"] = f"err: {type(e).__name__}: {e}"[:160]
                STATE["applied"] = None
                STATE["link"] = ""
                name = type(e).__name__
                if name == "NoDeviceConnectedError":
                    msg = str(e) if "Wi-Fi" in str(e) else "no phone plugged in"
                elif "Passcode" in name or "Lockdown" in name:
                    msg = "phone is locked, unlock it"
                else:
                    msg = f"link lost: {e!r}"
                if msg != getattr(sess, "_last_msg", None) or backoff >= 30 and time.time() - getattr(sess, "_last_t", 0) > 300:
                    log(f"{msg}; retrying")
                    sess._last_msg, sess._last_t = msg, time.time()
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
        wait = await apply_step(None)
        STATE["status"] = STATE["status"].replace("ok: connected", "ok: no-device mode")
        await wait_wake(wait)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--rsd", nargs=2, metavar=("ADDR", "PORT"), help="reuse an existing start-tunnel")
    p.add_argument("--udid", help="target device if more than one is plugged in")
    p.add_argument("--link", choices=link.MODES, default="auto",
                   help="usb: cable only. wifi: same network only (plug in once first to pair). auto: cable if in, else Wi-Fi")
    p.add_argument("--no-device", action="store_true", help="UI only, for testing without a phone")
    p.add_argument("--fresh", action="store_true", help="ignore saved state, start with no spoof")
    p.add_argument("--no-browser", action="store_true", help="don't open the UI automatically")
    a = p.parse_args()
    link.PREF["mode"] = STATE["link_pref"] = a.link

    try:  # another bridge already here? Windows lets two processes share the port, so ask first
        with urllib.request.urlopen(f"http://127.0.0.1:{a.port}/status", timeout=1) as r:
            if b'"version"' in r.read():
                print(f"a bridge is already running at http://127.0.0.1:{a.port}; use that one, or pass --port", flush=True)
                return
    except Exception:
        pass
    if not a.fresh:
        load_state()
    STATE["ai"] = ai_available()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{a.port}"
    print(f"bridge up at {url}  (AI planning: {'on' if STATE['ai'] else 'off: set ANTHROPIC_API_KEY or put the key in ~/.whereverwhenever/key'})", flush=True)
    if not a.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        asyncio.run(fake_loop() if a.no_device else device_loop(a.rsd, a.udid))
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
