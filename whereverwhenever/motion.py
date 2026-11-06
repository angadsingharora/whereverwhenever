"""
motion: turn a list of stops into a believable, replayable trajectory.

A real phone being carried around does not move in a straight line at constant
speed. It follows roads, slows into corners, waits at lights, lingers at each
stop, and its GPS fix wanders a few metres even when it is sitting still. This
module reproduces all of that, deterministically from a seed, so a trajectory
can be resumed after a restart by simply asking "where was I supposed to be at
time t".

Pipeline
  stops ──(OSRM, per travel mode)──> road polyline per leg
        ──(curvature + accel limits)──> speed profile along the polyline
        ──(lights, dwell at stops)───> keyframes (t, s, v)
        ──(pos_at(t))────────────────> lat, lng, heading, speed
        ──(+ smooth pseudo-random drift)──> what the phone reports

No state is kept between pos_at() calls, so the same Trajectory gives the same
answer for the same t on any machine, any time.
"""
from __future__ import annotations

import bisect
import json
import math
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

R_EARTH = 6371000.0

# ---------------------------------------------------------------- profiles
@dataclass(frozen=True)
class Profile:
    name: str
    osrm: str          # routing.openstreetmap.de profile
    cruise: float      # m/s typical top speed on an open stretch
    accel: float       # m/s^2
    decel: float       # m/s^2
    lat_acc: float     # m/s^2 max lateral acceleration in bends
    turn_speed: float  # m/s hard cap through a sharp (>70 deg) turn
    stop_p: float      # chance of stopping at a maneuver point (lights, stop signs)
    stop_s: tuple      # dwell range at such a stop, seconds
    jitter: float      # +- fraction of cruise that varies slowly along the way
    gps_sigma: float   # metres, 1-sigma horizontal drift of the reported fix


PROFILES = {
    "walk":  Profile("walk",  "foot", 1.4,  0.6, 0.8, 9.9, 1.0, 0.10, (2, 10),  0.15, 5.0),
    "bike":  Profile("bike",  "bike", 5.0,  0.8, 1.5, 1.5, 2.5, 0.25, (3, 25),  0.15, 4.0),
    "drive": Profile("drive", "car",  13.4, 2.0, 3.0, 2.2, 4.0, 0.35, (4, 40),  0.12, 3.0),
}


# ---------------------------------------------------------------- geometry
def haversine_m(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(h))


class Frame:
    """Local east/north metres around a reference point. Good to a few cm over a city."""

    def __init__(self, lat0, lng0):
        self.lat0, self.lng0 = lat0, lng0
        self.kx = math.radians(1) * R_EARTH * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * R_EARTH

    def to_xy(self, lat, lng):
        return (lng - self.lng0) * self.kx, (lat - self.lat0) * self.ky

    def to_ll(self, x, y):
        return self.lat0 + y / self.ky, self.lng0 + x / self.kx


# ---------------------------------------------------------------- routing
def fetch_route(a, b, profile: Profile, timeout=20):
    """Road polyline from a to b as [(lat,lng), ...] plus maneuver vertex indices.
    Raises on network/HTTP failure; caller decides whether to fall back."""
    url = (f"https://routing.openstreetmap.de/routed-{profile.osrm}/route/v1/driving/"
           f"{a[1]:.6f},{a[0]:.6f};{b[1]:.6f},{b[0]:.6f}"
           "?overview=full&geometries=geojson&steps=true")
    req = urllib.request.Request(url, headers={"User-Agent": "whereverwhenever/0.2 (location simulation testing)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.load(r)
    if j.get("code") != "Ok" or not j.get("routes"):
        raise RuntimeError(f"routing failed: {j.get('code')} {j.get('message', '')}")
    rt = j["routes"][0]
    pts = [(c[1], c[0]) for c in rt["geometry"]["coordinates"]]
    # maneuver points: turns, lights, roundabouts live here. First/last are depart/arrive.
    man = [(st["maneuver"]["location"][1], st["maneuver"]["location"][0])
           for leg in rt["legs"] for st in leg["steps"]]
    return pts, man[1:-1]


def nearest_vertices(pts, coords):
    """Index of the closest vertex in pts for each (lat,lng) in coords."""
    out = set()
    for m in coords:
        out.add(min(range(len(pts)), key=lambda i: (pts[i][0] - m[0]) ** 2 + (pts[i][1] - m[1]) ** 2))
    return sorted(out)


def densify(pts, max_seg=25.0):
    """Insert vertices so no segment is longer than max_seg metres (smoother sampling)."""
    out = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = haversine_m(a, b)
        n = max(1, int(d // max_seg))
        for i in range(1, n + 1):
            f = i / n
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


# ---------------------------------------------------------------- trajectory
@dataclass
class Stop:
    lat: float
    lng: float
    name: str = ""
    dwell_s: float = 0.0


@dataclass
class Leg:
    """One stop to the next. `pts` is the road polyline (lat,lng), `lights` vertex indices."""
    pts: list
    lights: list = field(default_factory=list)
    straight: bool = False


class Trajectory:
    """Deterministic position-as-a-function-of-time for a multi-stop itinerary."""

    def __init__(self, stops, legs, profile: Profile, seed=0, cruise=None, loop=False):
        assert len(stops) >= 2 and len(legs) == len(stops) - (0 if loop else 1)
        self.stops, self.legs, self.profile, self.seed, self.loop = stops, legs, profile, seed, loop
        self.cruise = cruise or profile.cruise
        rng = random.Random(seed)
        self.frame = Frame(stops[0].lat, stops[0].lng)
        self._build(rng)
        self._noise = _SmoothNoise(rng, profile.gps_sigma)

    # -- construction ------------------------------------------------------
    def _build(self, rng):
        p = self.profile
        ll, dwell_at, light_at, self.leg_start_s = [], {}, set(), []
        for li, leg in enumerate(self.legs):
            if ll:  # legs share their junction vertex
                start = len(ll) - 1
                ll.extend(leg.pts[1:])
            else:
                start = 0
                ll.extend(leg.pts)
            self.leg_start_s.append(start)
            for i in leg.lights:
                if 0 < i < len(leg.pts) - 1:
                    light_at.add(start + i)
            end = len(ll) - 1
            nxt = self.stops[(li + 1) % len(self.stops)]
            dwell_at[end] = max(dwell_at.get(end, 0.0), nxt.dwell_s)
        dwell_at[0] = max(dwell_at.get(0, 0.0), self.stops[0].dwell_s if self.loop else 0.0)
        self.ll = ll
        xy = [self.frame.to_xy(*q) for q in ll]
        n = len(xy)
        d = [math.hypot(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]) for i in range(n - 1)]
        S = [0.0]
        for x in d:
            S.append(S[-1] + x)
        self.S, self.d = S, d
        self.heading = [math.degrees(math.atan2(xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1])) % 360
                        if d[i] > 0 else None for i in range(n - 1)]
        for i in range(n - 1):
            if self.heading[i] is None:
                self.heading[i] = self.heading[i - 1] if i else 0.0

        # 1) speed ceiling per vertex: slow varying cruise, then bend limits
        v = [self.cruise] * n
        jit, kjit = 0.0, 0.0
        for i in range(n):
            if S[i] >= kjit:  # new random cruise factor every ~150-400 m
                jit = rng.uniform(-p.jitter, p.jitter)
                kjit = S[i] + rng.uniform(150, 400)
            v[i] = self.cruise * (1 + jit)
        corners = {}  # vertex -> (turn angle deg, capped speed) where the bend limit actually bit
        for i in range(1, n - 1):
            if d[i - 1] <= 0 or d[i] <= 0:
                continue
            dth = abs((self.heading[i] - self.heading[i - 1] + 180) % 360 - 180)
            if dth < 2:
                continue
            kappa = math.radians(dth) / max(1.0, (d[i - 1] + d[i]) / 2)
            cap = math.sqrt(p.lat_acc / kappa)
            if dth > 70:
                cap = min(cap, p.turn_speed)
            if cap < v[i] * 0.7:
                corners[i] = (dth, cap)
            v[i] = min(v[i], cap)

        # 2) forced stops: itinerary stops always, lights sometimes
        stop_dwell = dict(dwell_at)
        lights_hit = {}
        for i in sorted(light_at):
            if rng.random() < p.stop_p:
                lights_hit[i] = rng.uniform(*p.stop_s)
                stop_dwell[i] = max(stop_dwell.get(i, 0.0), lights_hit[i])
        self.n_maneuvers = len(light_at)
        v[0] = 0.0
        v[-1] = 0.0
        for i in stop_dwell:
            v[i] = 0.0
        # 3) accel / decel feasibility (forward then backward pass)
        for i in range(1, n):
            v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * p.accel * d[i - 1]))
        for i in range(n - 2, -1, -1):
            v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * p.decel * d[i]))
        # 4) keyframes (t, s, v); a dwell is a zero-length keyframe pair
        T, KS, KV = [0.0], [0.0], [v[0]]
        if stop_dwell.get(0):
            T.append(stop_dwell[0]); KS.append(0.0); KV.append(0.0)
        for i in range(n - 1):
            vs = v[i] + v[i + 1]
            dt = 2 * d[i] / vs if vs > 1e-6 else d[i] / 0.5
            T.append(T[-1] + dt); KS.append(S[i + 1]); KV.append(v[i + 1])
            if i + 1 in stop_dwell and i + 1 != 0 and stop_dwell[i + 1] > 0:
                T.append(T[-1] + stop_dwell[i + 1]); KS.append(S[i + 1]); KV.append(0.0)
        self.T, self.KS, self.KV = T, KS, KV
        self.total_s = T[-1]
        self.total_m = S[-1]
        self.leg_start_m = [S[i] for i in self.leg_start_s]
        # 5) things worth showing: where and why the speed profile dips
        t_at = {}
        for t, s in zip(T, KS):
            t_at.setdefault(s, t)
        ev = []
        stop_v = {self.leg_start_s[li]: li for li in range(len(self.leg_start_s))}
        ends = {len(ll) - 1: len(self.legs)}
        for i, dw in dwell_at.items():
            li = stop_v.get(i, ends.get(i))
            if li is None:
                continue
            st = self.stops[li % len(self.stops)]
            ev.append({"s": S[i], "t": t_at.get(S[i], 0.0), "kind": "stop", "dwell_s": dw,
                       "label": st.name or chr(65 + li % 26)})
        for i, dw in lights_hit.items():
            ev.append({"s": S[i], "t": t_at.get(S[i], 0.0), "kind": "light", "dwell_s": dw})
        for i, (dth, cap) in corners.items():
            ev.append({"s": S[i], "t": t_at.get(S[i], 0.0), "kind": "corner", "deg": round(dth), "cap": cap})
        self.events = sorted(ev, key=lambda e: e["s"])
        self.dwell_total = sum(stop_dwell.values())
        self.v_max = max(v)

    # -- queries -----------------------------------------------------------
    def state_at(self, t):
        """Noise-free kinematic state at trajectory time t (seconds since start)."""
        if self.loop and self.total_s > 0:
            t = t % self.total_s
        t = min(max(0.0, t), self.total_s)
        if len(self.T) < 2:
            return self.KS[0], 0.0
        k = min(bisect.bisect_right(self.T, t) - 1, len(self.T) - 2)
        t0, t1 = self.T[k], self.T[k + 1]
        s0, s1, v0, v1 = self.KS[k], self.KS[k + 1], self.KV[k], self.KV[k + 1]
        dt = t1 - t0
        if dt <= 0:
            s, v, a = s1, v1, 0.0
        else:
            tau = t - t0
            a = (v1 - v0) / dt
            s = s0 + v0 * tau + 0.5 * a * tau * tau
            v = v0 + a * tau
            s = min(max(s0, s), s1)
        self._last_accel = a
        return s, max(0.0, v)

    def point_at_m(self, s):
        """(lat, lng, heading) at arc length s along the whole polyline."""
        i = bisect.bisect_right(self.S, s) - 1
        i = min(max(i, 0), len(self.ll) - 2) if len(self.ll) > 1 else 0
        if len(self.ll) == 1 or self.d[i] <= 0:
            return self.ll[i][0], self.ll[i][1], self.heading[i] if self.heading else 0.0
        f = (s - self.S[i]) / self.d[i]
        f = min(max(f, 0.0), 1.0)
        a, b = self.ll[i], self.ll[i + 1]
        return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, self.heading[i]

    def pos_at(self, t, noisy=True):
        """What the phone should report at trajectory time t."""
        s, v = self.state_at(t)
        lat, lng, hdg = self.point_at_m(s)
        if noisy:
            nx, ny = self._noise.at(t)
            x, y = self.frame.to_xy(lat, lng)
            lat, lng = self.frame.to_ll(x + nx, y + ny)
        leg = max(0, bisect.bisect_right(self.leg_start_m, s + 1e-6) - 1)
        leg = min(leg, len(self.legs) - 1)
        return {"lat": lat, "lng": lng, "heading": hdg, "speed": v, "accel": self._last_accel, "s": s,
                "leg": leg, "progress": (s / self.total_m) if self.total_m else 1.0}

    def profile_points(self, max_pts=700):
        """Keyframes thinned for plotting: [[s, v, t], ...]. Keeps every zero-speed point so
        stops and lights stay visible, decimates the rest."""
        n = len(self.KS)
        step = max(1, n // max_pts)
        out = []
        for i in range(n):
            if i % step == 0 or self.KV[i] == 0.0 or i == n - 1 or (i and self.KV[i - 1] == 0.0):
                out.append([round(self.KS[i], 1), round(self.KV[i], 2), round(self.T[i], 1)])
        return out

    def describe(self):
        p = self.profile
        return {"total_m": self.total_m, "total_s": self.total_s, "loop": self.loop,
                "mode": p.name, "cruise": self.cruise, "seed": self.seed,
                "route": [[round(a, 6), round(b, 6)] for a, b in self.ll],
                "straight_legs": sum(1 for l in self.legs if l.straight),
                "leg_start_m": self.leg_start_m,
                "stops": [{"lat": s.lat, "lng": s.lng, "name": s.name, "dwell_s": s.dwell_s} for s in self.stops],
                "profile": self.profile_points(),
                "events": self.events,
                "stats": {"v_max": self.v_max, "dwell_s": self.dwell_total, "moving_s": self.total_s - self.dwell_total,
                          "maneuvers": self.n_maneuvers, "lights": sum(1 for e in self.events if e["kind"] == "light"),
                          "corners": sum(1 for e in self.events if e["kind"] == "corner"),
                          "accel": p.accel, "decel": p.decel, "lat_acc": p.lat_acc, "gps_sigma": p.gps_sigma,
                          "jitter": p.jitter, "stop_p": p.stop_p}}

    # -- (de)serialisation so a restart does not need the network -------------
    def to_json(self):
        return {"stops": [s.__dict__ for s in self.stops],
                "legs": [{"pts": l.pts, "lights": l.lights, "straight": l.straight} for l in self.legs],
                "mode": self.profile.name, "seed": self.seed, "cruise": self.cruise, "loop": self.loop}

    @classmethod
    def from_json(cls, j):
        return cls([Stop(**s) for s in j["stops"]],
                   [Leg([tuple(p) for p in l["pts"]], l.get("lights", []), l.get("straight", False)) for l in j["legs"]],
                   PROFILES[j["mode"]], j.get("seed", 0), j.get("cruise"), j.get("loop", False))


class _SmoothNoise:
    """Stateless smooth 2-D drift: a few seeded sinusoids per axis, normalised to sigma metres.
    Real GPS error is autocorrelated over tens of seconds, not white; this looks like that
    without needing any memory between calls."""

    def __init__(self, rng, sigma, k=6):
        self.sigma = sigma
        self.terms = []
        for _ in range(2):
            ws = [2 * math.pi / rng.uniform(12, 90) for _ in range(k)]  # periods 12..90 s
            ph = [rng.uniform(0, 2 * math.pi) for _ in range(k)]
            am = [rng.uniform(0.5, 1.0) for _ in range(k)]
            norm = math.sqrt(sum(a * a / 2 for a in am))  # rms of sum of sinusoids
            self.terms.append([(w, p, a / norm) for w, p, a in zip(ws, ph, am)])

    def at(self, t):
        return tuple(self.sigma * sum(a * math.sin(w * t + p) for w, p, a in ax) for ax in self.terms)


# ---------------------------------------------------------------- builders
def build(stops, mode="drive", seed=None, cruise=None, loop=False, online=True, log=None):
    """Plan a trajectory through `stops` (list of Stop or (lat,lng[,name,dwell]) tuples).
    Routes on roads when online; falls back to straight lines per leg otherwise."""
    st = [s if isinstance(s, Stop) else Stop(*s) for s in stops]
    prof = PROFILES[mode]
    seed = random.randrange(1 << 30) if seed is None else seed
    pairs = list(zip(st, st[1:])) + ([(st[-1], st[0])] if loop else [])
    legs = []
    for a, b in pairs:
        pts, man, straight = None, [], True
        if online:
            try:
                pts, man = fetch_route((a.lat, a.lng), (b.lat, b.lng), prof)
                straight = False
            except Exception as e:  # offline, rate-limited, no road nearby: straight line
                if log:
                    log(f"routing failed ({e}); straight line for this leg")
        if pts is None:
            pts = [(a.lat, a.lng), (b.lat, b.lng)]
        # snap the ends to the requested stops so arrival is exact, then densify
        pts = densify([(a.lat, a.lng)] + list(pts[1:-1]) + [(b.lat, b.lng)])
        legs.append(Leg(pts, nearest_vertices(pts, man), straight))
    return Trajectory(st, legs, prof, seed, cruise, loop)
