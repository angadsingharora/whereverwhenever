"""Bridge HTTP contract, run against a real `--no-device` bridge on a spare port.

Offline: every trip here uses straight:true so no OSRM call is made.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    port = _free_port()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONWARNINGS": "ignore"}
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.Popen([sys.executable, "-m", "whereverwhenever.server", "--no-device", "--no-browser",
                             "--fresh", "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "/status", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("bridge did not start:\n" + proc.stdout.read())
    yield url, home
    proc.kill()


def call(url, path, body=None, raw=None, origin=None, method=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url + path, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("content-type", "application/json")
    if origin:
        req.add_header("origin", origin)
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), dict(e.headers)


SEA = {"lat": 47.6062, "lng": -122.3321, "name": "Seattle"}
BEL = {"lat": 47.6101, "lng": -122.2015, "name": "Bellevue"}


def test_status_shape(bridge):
    url, _ = bridge
    st, j, _ = call(url, "/status")
    assert st == 200
    for k in ("target", "status", "device", "link", "link_pref", "applied", "trip", "ai", "version"):
        assert k in j
    assert j["ai"] is False  # no key in the temp home


def test_page_served(bridge):
    url, _ = bridge
    r = urllib.request.urlopen(url + "/", timeout=5)
    assert r.status == 200 and b"whereverwhenever" in r.read()


def test_cors(bridge):
    url, _ = bridge
    for o, ok in (("https://whereverwhenever.vercel.app", True), ("http://localhost:5173", True),
                  ("https://evil.example", False)):
        _, _, h = call(url, "/status", origin=o)
        assert (h.get("Access-Control-Allow-Origin") == o) is ok, o
    req = urllib.request.Request(url + "/set", method="OPTIONS")
    req.add_header("origin", "https://whereverwhenever.vercel.app")
    req.add_header("access-control-request-private-network", "true")
    r = urllib.request.urlopen(req, timeout=5)
    assert r.status == 204 and r.headers.get("Access-Control-Allow-Private-Network") == "true"


@pytest.mark.parametrize("path,body,code", [
    ("/set", {"lat": 91, "lng": 0}, 400),
    ("/set", {"lat": "x", "lng": 0}, 400),
    ("/set", {"lng": 0}, 400),
    ("/set", {"lat": float("nan"), "lng": 0}, 400),
    ("/set", [], 400),
    ("/trip", {"stops": [SEA]}, 400),
    ("/trip", {"stops": [SEA, {"lat": 200, "lng": 0}]}, 400),
    ("/trip", {"stops": [SEA, BEL], "mode": "fly", "straight": True}, 400),
    ("/trip", {"stops": [SEA, BEL], "cruise": 9999, "straight": True}, 400),
    ("/link", {"mode": "lte"}, 400),
    ("/link", {"mode": "wifi"}, 409),
    ("/plan", {"text": "coffee"}, 400),
    ("/nope", {}, 404),
])
def test_bad_requests(bridge, path, body, code):
    url, _ = bridge
    st, j, _ = call(url, path, body)
    assert st == code and "error" in j, (path, body, st, j)


def test_malformed_json(bridge):
    url, _ = bridge
    st, j, _ = call(url, "/set", raw=b"{not json")
    assert st == 400 and "error" in j
    st, j, _ = call(url, "/set", raw=b"")
    assert st == 400


def test_set_hold_clear(bridge):
    url, home = bridge
    assert call(url, "/set", {"lat": 47.6, "lng": -122.3})[0] == 200
    for _ in range(30):
        j = call(url, "/status")[1]
        if j["applied"] == [47.6, -122.3]:
            break
        time.sleep(0.1)
    assert j["applied"] == [47.6, -122.3] and j["status"] == "ok: holding"
    saved = json.load(open(home / ".whereverwhenever" / "state.json"))
    assert saved["target"] == [47.6, -122.3]
    assert call(url, "/clear", {})[0] == 200
    for _ in range(30):
        j = call(url, "/status")[1]
        if j["applied"] is None:
            break
        time.sleep(0.1)
    assert j["applied"] is None and j["target"] is None


def test_preview_then_trip_same_seed(bridge):
    url, _ = bridge
    st, p, _ = call(url, "/preview", {"stops": [SEA, BEL], "mode": "drive", "straight": True})
    assert st == 200 and p["total_m"] > 5000 and p["seed"]
    st, t, _ = call(url, "/trip", {"stops": [SEA, BEL], "mode": "drive", "straight": True, "seed": p["seed"]})
    assert st == 200 and t["seed"] == p["seed"] and abs(t["total_s"] - p["total_s"]) < 1e-6
    j = call(url, "/status")[1]
    assert j["trip"] and j["trip"]["t0"] == t["t0"]
    for _ in range(40):
        j = call(url, "/status")[1]
        if j["status"] == "ok: moving" and j["applied"]:
            break
        time.sleep(0.1)
    assert j["status"] == "ok: moving"
    assert call(url, "/route")[1]["seed"] == p["seed"]
    # stop: holds wherever it is now
    assert call(url, "/stop", {})[0] == 200
    j = call(url, "/status")[1]
    assert j["trip"] is None and j["target"] is not None


def test_dwell_and_loop_and_duplicate_stops(bridge):
    url, _ = bridge
    st, p, _ = call(url, "/preview", {"stops": [SEA, {**BEL, "dwell_s": 120}, SEA], "mode": "walk",
                                        "straight": True, "loop": True})
    assert st == 200 and p["loop"] and p["stats"]["dwell_s"] >= 120
    st, p, _ = call(url, "/preview", {"stops": [SEA, SEA], "mode": "bike", "straight": True})
    assert st in (200, 400), p  # never a crash
    if st == 200:
        assert p["total_s"] >= 0


def test_link_switch(bridge):
    url, _ = bridge
    assert call(url, "/link", {"mode": "usb"})[0] == 200
    assert call(url, "/status")[1]["link_pref"] == "usb"
    assert call(url, "/link", {"mode": "auto"})[0] == 200
    assert call(url, "/status")[1]["link_pref"] == "auto"
