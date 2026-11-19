"""Schedule timing (pure), then the bridge's /schedule endpoints against a --no-device bridge."""
import datetime as dt
import time

import pytest

from whereverwhenever import schedule as sc

PIN = {"kind": "pin", "lat": 47.6, "lng": -122.3}


def at(y, mo, d, h=0, mi=0, s=0):
    return dt.datetime(y, mo, d, h, mi, s).timestamp()


def entry(**kw):
    base = {"start": "09:00", "end": "17:00", "days": [0, 1, 2, 3, 4]}
    base.update(kw)
    return sc.validate(base, PIN)


# 2026-11-16 is a Monday
MON, SAT = (2026, 11, 16), (2026, 11, 21)


def test_validate_rejects_nonsense():
    for bad in ({"start": "9am"}, {"start": "25:00"}, {"start": "09:00", "end": "09:00", "days": [1]},
                {"start": "09:00", "days": [7]}, {"start": "09:00"}, {"start": "09:00", "date": "tomorrow"}):
        with pytest.raises(ValueError):
            sc.validate(bad, PIN)
    e = sc.validate({"start": "7:05", "date": "2026-11-20", "name": " x " * 40}, PIN)
    assert e["start"] == "07:05" and e["days"] == [] and len(e["name"]) <= 60


def test_weekday_window_starts_ends_and_skips_weekend():
    e = sc.created(entry(), at(*MON, 8, 0))
    assert e["next"] == at(*MON, 9, 0)
    assert sc.step(e, at(*MON, 8, 59, 59)) == []
    assert sc.step(e, at(*MON, 9, 0, 1)) == ["start"] and e["until"] == at(*MON, 17, 0)
    assert sc.step(e, at(*MON, 12, 0)) == []
    assert sc.step(e, at(*MON, 17, 0, 1)) == ["end"] and e["until"] is None
    assert e["next"] == at(2026, 11, 17, 9, 0)
    # Friday's end, then nothing until Monday
    e["fired"] = at(2026, 11, 20, 9, 0)
    assert sc.step(e, at(*SAT, 10, 0)) == []
    assert e["next"] == at(2026, 11, 23, 9, 0)


def test_overnight_window_ends_next_morning():
    e = sc.created(entry(start="22:00", end="07:00", days=list(range(7))), at(*MON, 21, 0))
    assert sc.step(e, at(*MON, 22, 0, 2)) == ["start"]
    assert e["until"] == at(2026, 11, 17, 7, 0)
    assert sc.step(e, at(2026, 11, 17, 6, 59)) == []
    assert sc.step(e, at(2026, 11, 17, 7, 0, 1)) == ["end"]


def test_created_inside_window_starts_now_but_open_ended_past_waits():
    e = sc.created(entry(), at(*MON, 12, 0))
    assert sc.step(e, at(*MON, 12, 0, 2)) == ["start"] and e["until"] == at(*MON, 17, 0)
    e = sc.created(entry(end=None), at(*MON, 12, 0))
    assert sc.step(e, at(*MON, 12, 0, 2)) == []
    assert e["next"] == at(2026, 11, 17, 9, 0)


def test_once_in_the_past_is_refused_and_once_finishes():
    with pytest.raises(ValueError):
        sc.created(entry(days=[], date="2026-11-16", end=None), at(*MON, 10, 0))
    with pytest.raises(ValueError):
        sc.created(entry(days=[], date="2026-11-16"), at(*MON, 18, 0))
    e = sc.created(entry(days=[], date="2026-11-16"), at(*MON, 8, 0))
    assert not sc.finished(e)
    assert sc.step(e, at(*MON, 9, 0, 1)) == ["start"] and not sc.finished(e)
    assert sc.step(e, at(*MON, 17, 0, 1)) == ["end"] and sc.finished(e)


def test_bridge_down_catch_up():
    # window still open when the bridge comes back: starts late
    e = sc.created(entry(), at(*MON, 8, 0))
    assert sc.step(e, at(*MON, 15, 0)) == ["start"]
    # open-ended start missed by more than the grace: logged as missed, not applied hours late
    e = sc.created(entry(end=None), at(*MON, 8, 0))
    assert sc.step(e, at(*MON, 9, 0) + sc.GRACE_S + 1) == ["missed"]
    e = sc.created(entry(end=None), at(*MON, 8, 0))
    assert sc.step(e, at(*MON, 9, 5)) == ["start"]
    # the whole window passed while down: an end that was pending still fires
    e = sc.created(entry(), at(*MON, 8, 0))
    sc.step(e, at(*MON, 9, 0, 1))
    assert sc.step(e, at(*MON, 20, 0)) == ["end"]


def test_disabled_does_nothing_and_rearm_skips_the_past():
    e = sc.created(entry(), at(*MON, 8, 0))
    e["enabled"] = False
    assert sc.step(e, at(*MON, 9, 0, 1)) == []
    e["enabled"] = True
    sc.arm(e, at(*MON, 18, 0))
    assert sc.step(e, at(*MON, 18, 0, 2)) == []  # today's window is over: no late start, no "missed"


# ---------------------------------------------------------------- bridge endpoints
from test_server import bridge, call  # noqa: E402,F401  (same --no-device bridge fixture)


def wait_for(url, pred, n=60):
    for _ in range(n):
        j = call(url, "/status")[1]
        if pred(j):
            return j
        time.sleep(0.1)
    return j


def hhmm(t):
    return time.strftime("%H:%M", time.localtime(t))


def test_schedule_endpoints(bridge):
    url, home = bridge
    call(url, "/clear", {})
    now = time.time()
    every = list(range(7))
    # refused
    assert call(url, "/schedule", {"what": {"kind": "teleport"}, "start": "09:00", "days": every})[0] == 400
    assert call(url, "/schedule", {"what": {**PIN, "lat": 95}, "start": "09:00", "days": every})[0] == 400
    assert call(url, "/schedule", {"what": PIN, "start": "nine", "days": every})[0] == 400
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    st, j, _ = call(url, "/schedule", {"what": PIN, "start": "09:00", "date": yesterday})
    assert st == 400 and "passed" in j["error"]
    # a window open right now takes the phone within a tick
    st, e, _ = call(url, "/schedule", {"what": PIN, "name": "Office", "start": hhmm(now - 120),
                                       "end": hhmm(now + 600), "days": every})
    assert st == 200 and e["id"]
    j = wait_for(url, lambda j: j["applied"] == [47.6, -122.3])
    assert j["applied"] == [47.6, -122.3] and j["by"] == e["id"]
    assert [x["name"] for x in j["schedule"]] == ["Office"] and j["schedule"][0]["until"]
    # turning it off hands the phone back to real GPS
    assert call(url, "/schedule/enable", {"id": e["id"], "enabled": False})[0] == 200
    j = wait_for(url, lambda j: j["applied"] is None)
    assert j["applied"] is None and j["by"] is None
    # back on mid-window: resumes
    call(url, "/schedule/enable", {"id": e["id"], "enabled": True})
    j = wait_for(url, lambda j: j["applied"] == [47.6, -122.3])
    assert j["by"] == e["id"]
    # moving the phone by hand takes it from the schedule, so deleting the schedule leaves it be
    call(url, "/set", {"lat": 10, "lng": 20})
    assert wait_for(url, lambda j: j["applied"] == [10.0, 20.0])["by"] is None
    assert call(url, "/schedule/delete", {"id": e["id"]})[0] == 200
    assert call(url, "/schedule/delete", {"id": e["id"]})[0] == 404
    time.sleep(2.5)
    j = call(url, "/status")[1]
    assert j["applied"] == [10.0, 20.0] and j["schedule"] == []
    call(url, "/clear", {})


def test_schedule_survives_fresh_restart(bridge):
    import json
    import os
    import subprocess
    import sys
    from test_server import ROOT, _free_port

    url, home = bridge
    st, e, _ = call(url, "/schedule", {"what": PIN, "start": "03:00", "days": [6], "name": "Sunday"})
    assert st == 200
    saved = json.load(open(home / ".whereverwhenever" / "state.json"))
    assert [x["id"] for x in saved["schedule"]] == [e["id"]]
    # a second bridge on the same home with --fresh: no pin, but the schedule is still there
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    p = subprocess.Popen([sys.executable, "-m", "whereverwhenever.server", "--no-device", "--no-browser", "--fresh",
                          "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        j = None
        for _ in range(100):
            try:
                j = call(f"http://127.0.0.1:{port}", "/status")[1]
                break
            except Exception:
                time.sleep(0.1)
        assert j and j["target"] is None and [x["id"] for x in j["schedule"]] == [e["id"]]
    finally:
        p.kill()
        p.wait()
    call(url, "/schedule/delete", {"id": e["id"]})
