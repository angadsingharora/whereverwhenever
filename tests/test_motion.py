"""Offline checks on the motion engine. Run: python -m pytest -q"""
import math
import random

import pytest

from whereverwhenever import motion
from whereverwhenever.motion import PROFILES, Leg, Stop, Trajectory, build, haversine_m

BERK = (37.8716, -122.2727)
OAK = (37.8044, -122.2712)


def square_legs(side_m=400.0, loop=False):
    """Four stops on a square so every corner is a sharp 90 degree turn."""
    k = side_m / 111_000
    s = [Stop(37.87, -122.27, "A", 30), Stop(37.87 + k, -122.27, "B", 60),
         Stop(37.87 + k, -122.27 + k * 1.27, "C", 0), Stop(37.87, -122.27 + k * 1.27, "D", 10)]
    pairs = list(zip(s, s[1:])) + ([(s[-1], s[0])] if loop else [])
    legs = [Leg(motion.densify([(a.lat, a.lng), (b.lat, b.lng)], 20), [], True) for a, b in pairs]
    return s, legs


def test_straight_trip_arrives_exactly_and_speed_is_bounded():
    tr = build([BERK, OAK], "drive", seed=1, online=False)
    assert tr.total_m == pytest.approx(haversine_m(BERK, OAK), rel=1e-3)
    start, end = tr.pos_at(0, noisy=False), tr.pos_at(tr.total_s, noisy=False)
    assert (start["lat"], start["lng"]) == pytest.approx(BERK, abs=1e-9)
    assert (end["lat"], end["lng"]) == pytest.approx(OAK, abs=1e-9)
    p = PROFILES["drive"]
    vs = [tr.state_at(t)[1] for t in range(int(tr.total_s) + 1)]
    assert max(vs) <= p.cruise * (1 + p.jitter) + 1e-6
    assert vs[0] == 0 and tr.state_at(tr.total_s)[1] == 0
    # it actually gets up to speed on an 8 km straight
    assert max(vs) > p.cruise * 0.8


def test_time_and_distance_are_monotone():
    tr = build([BERK, OAK], "bike", seed=7, online=False)
    last = -1
    for t in range(0, int(tr.total_s) + 1):
        s, _ = tr.state_at(t)
        assert s >= last - 1e-9
        last = s
    assert all(b >= a for a, b in zip(tr.T, tr.T[1:]))


def test_accel_limits_hold_between_samples():
    tr = build([BERK, OAK], "drive", seed=3, online=False)
    p = PROFILES["drive"]
    prev = tr.state_at(0)[1]
    for t in range(1, int(tr.total_s) + 1):
        v = tr.state_at(t)[1]
        assert v - prev <= p.accel + 1e-6
        assert prev - v <= p.decel + 1e-6
        prev = v


def test_corners_slow_the_car_down():
    stops, legs = square_legs()
    tr = Trajectory(stops, legs, PROFILES["drive"], seed=5)
    # at each interior stop vertex speed is zero; just before a stop we must be braking
    for idx in tr.leg_start_s[1:]:
        assert tr.KV[tr.KS.index(tr.S[idx])] == 0.0
    # on the first leg, right at the sharp turn into leg 2, speed is capped
    turn_s = tr.leg_start_m[1]
    t_turn = tr.T[tr.KS.index(turn_s)]
    assert tr.state_at(t_turn)[1] == 0.0  # it is also a stop
    # somewhere mid-leg it is clearly faster than turn_speed
    mid = tr.state_at(tr.T[tr.KS.index(turn_s)] / 2)[1]
    assert mid > PROFILES["drive"].turn_speed


def test_dwell_holds_position_for_the_requested_time():
    stops, legs = square_legs()
    tr = Trajectory(stops, legs, PROFILES["walk"], seed=2)
    # arrival at B (dwell 60 s): find keyframe pair with equal s and 60 s apart
    sB = tr.leg_start_m[1]
    ks = [i for i, s in enumerate(tr.KS) if s == sB]
    assert len(ks) == 2 and tr.T[ks[1]] - tr.T[ks[0]] == pytest.approx(60.0)
    t_mid = (tr.T[ks[0]] + tr.T[ks[1]]) / 2
    pos = tr.pos_at(t_mid, noisy=False)
    assert (pos["lat"], pos["lng"]) == pytest.approx((stops[1].lat, stops[1].lng), abs=1e-9)
    assert pos["speed"] == 0.0
    assert pos["leg"] == 1  # once we've reached B we are on the leg that leaves B


def test_loop_wraps_and_dwells_at_first_stop():
    stops, legs = square_legs(loop=True)
    tr = Trajectory(stops, legs, PROFILES["bike"], seed=9, loop=True)
    assert len(tr.legs) == 4
    a = tr.pos_at(5.0, noisy=False)
    b = tr.pos_at(5.0 + tr.total_s, noisy=False)
    assert (a["lat"], a["lng"]) == pytest.approx((b["lat"], b["lng"]), abs=1e-9)
    # dwell 30 s at A at the start of each cycle
    assert tr.state_at(10.0)[1] == 0.0 and tr.state_at(10.0)[0] == 0.0


def test_deterministic_for_seed_and_round_trips_through_json():
    a = build([BERK, OAK], "drive", seed=42, online=False)
    b = Trajectory.from_json(a.to_json())
    for t in (0, 10.5, 100, 777.25, a.total_s):
        pa, pb = a.pos_at(t), b.pos_at(t)
        assert (pa["lat"], pa["lng"]) == pytest.approx((pb["lat"], pb["lng"]), abs=1e-12)
    c = build([BERK, OAK], "drive", seed=43, online=False)
    assert c.pos_at(100)["lng"] != a.pos_at(100)["lng"]


def test_noise_is_smooth_and_about_sigma():
    rng = random.Random(0)
    n = motion._SmoothNoise(rng, 3.0)
    xs = [n.at(t)[0] for t in range(0, 3000)]
    rms = math.sqrt(sum(x * x for x in xs) / len(xs))
    assert 1.5 < rms < 5.0
    # consecutive samples never jump more than a couple of metres
    assert max(abs(b - a) for a, b in zip(xs, xs[1:])) < 2.5
    assert n.at(12.0) == n.at(12.0)  # stateless


def test_lights_sometimes_stop_the_car():
    pts = motion.densify([BERK, OAK], 20)
    lights = list(range(10, len(pts) - 10, 40))
    stops_with = Trajectory([Stop(*BERK), Stop(*OAK)], [Leg(pts, lights)], PROFILES["drive"], seed=4)
    stops_without = Trajectory([Stop(*BERK), Stop(*OAK)], [Leg(pts, [])], PROFILES["drive"], seed=4)
    assert stops_with.total_s > stops_without.total_s + 10
    zeros = sum(1 for v in stops_with.KV[1:-1] if v == 0.0)
    assert zeros >= 1


def test_leg_index_tracks_progress():
    stops, legs = square_legs()
    tr = Trajectory(stops, legs, PROFILES["walk"], seed=1)
    seen = [tr.pos_at(t, noisy=False)["leg"] for t in range(0, int(tr.total_s) + 1, 5)]
    assert seen[0] == 0 and seen[-1] == 2
    assert seen == sorted(seen)


def test_road_speeds_lift_the_freeway_and_hold_side_streets():
    """A leg whose router speeds say residential -> freeway -> residential: the car should
    get well past the flat cruise on the fast part, stay slow on the slow parts, and the
    speeds must survive the JSON round trip (a resumed trip replays the same profile)."""
    a, b = Stop(37.87, -122.27, "A"), Stop(37.87, -122.27 + 6000 / 88_000, "B")
    pts = motion.densify([(a.lat, a.lng), (b.lat, b.lng)], 20)
    n = len(pts)
    slow, fast = 25 / 3.6, 90 / 3.6  # OSRM car: residential, motorway
    vmax = [slow if i < n // 5 or i > 4 * n // 5 else fast for i in range(n)]
    tr = Trajectory([a, b], [Leg(pts, [], True, vmax)], PROFILES["drive"], seed=3)
    f, lo, hi = PROFILES["drive"].road
    assert tr.v_max > PROFILES["drive"].cruise * 1.5 and tr.v_max <= hi * (1 + PROFILES["drive"].jitter) + 1e-6
    # first fifth is residential: never faster than the residential ceiling (plus jitter)
    s_slow = tr.S[n // 5 - 2]
    vs = [tr.state_at(t)[1] for t in range(0, int(tr.total_s)) if tr.state_at(t)[0] < s_slow]
    assert max(vs) <= max(lo, slow * f) * (1 + PROFILES["drive"].jitter) + 1e-6
    again = Trajectory.from_json(tr.to_json())
    assert again.total_s == pytest.approx(tr.total_s) and again.v_max == pytest.approx(tr.v_max)
    # a user-set cruise overrides the road speeds, and that choice also survives the round trip
    capped = Trajectory([a, b], [Leg(pts, [], True, vmax)], PROFILES["drive"], seed=3, cruise=10.0)
    assert capped.v_max <= 10.0 * (1 + PROFILES["drive"].jitter) + 1e-6
    assert Trajectory.from_json(capped.to_json()).user_cruise == 10.0


def test_routing_retries_then_falls_back_to_next_router(monkeypatch):
    """First router times out, second answers: the leg is still on roads. The answer is
    cached, so planning and then pressing Go does not ask the router twice."""
    motion._route_cache.clear()
    monkeypatch.setattr(motion, "ROUTERS", ["https://dead.example/{osrm}", "https://alive.example"])
    monkeypatch.setattr(motion.time, "sleep", lambda s: None)
    calls = []
    ok = {"code": "Ok", "routes": [{"geometry": {"coordinates": [[-122.27, 37.87], [-122.26, 37.875], [-122.25, 37.88]]},
                                    "legs": [{"steps": [{"maneuver": {"location": [-122.27, 37.87]}},
                                                        {"maneuver": {"location": [-122.26, 37.875]}},
                                                        {"maneuver": {"location": [-122.25, 37.88]}}],
                                              "annotation": {"speed": [10.0, 25.0]}}]}]}

    def fake(base, a, b, profile, timeout):
        calls.append(base)
        if "dead" in base:
            raise TimeoutError("timed out")
        return ok

    monkeypatch.setattr(motion, "_one_router", fake)
    pts, man, sp = motion.fetch_route((37.87, -122.27), (37.88, -122.25), PROFILES["drive"])
    assert calls == ["https://dead.example/{osrm}", "https://alive.example"]
    assert len(pts) == 3 and man == [(37.875, -122.26)] and sp == [10.0, 25.0]
    motion.fetch_route((37.87, -122.27), (37.88, -122.25), PROFILES["drive"])
    assert len(calls) == 2  # second call served from the cache
    # walking has no public fallback, so a dead router means a straight line, after retries
    monkeypatch.setattr(motion, "ROUTERS", ["https://dead.example/{osrm}", "https://alive.example"])
    with pytest.raises(RuntimeError, match="timed out"):
        motion.fetch_route((37.0, -122.0), (37.1, -122.1), PROFILES["walk"])
    tr = build([(37.0, -122.0), (37.1, -122.1)], "walk", seed=1, online=True)
    assert all(l.straight for l in tr.legs) and tr.describe()["straight_legs"] == 1
