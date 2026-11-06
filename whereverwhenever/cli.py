#!/usr/bin/env python3
"""
whereverwhenever CLI: keep a simulated location pinned on a USB-connected iPhone (iOS 17.4+).

  whereverwhenever-cli pin 37.8716 -122.2727          hold one coordinate until Ctrl+C
  whereverwhenever-cli go 37.87 -122.27 37.80 -122.41 --mode drive   A to B on real roads, realistic motion
  whereverwhenever-cli plan day.json --loop           multi-stop itinerary with dwell times, round and round
  whereverwhenever-cli walk route.gpx --speed 1.4     walk a GPX track at 1.4 m/s, back and forth
  whereverwhenever-cli clear                          drop the simulation

Needs: pip install -U pymobiledevice3 ; Developer Mode on ; phone unlocked at start.
No admin needed: uses pymobiledevice3's in-process userspace tunnel.
Pass --rsd ADDR PORT to reuse a tunnel from `pymobiledevice3 lockdown start-tunnel` instead.

The device only honours the fake location while the DVT session is open, so this
process must stay running. It reconnects and re-applies the last fix on any drop.
"""
import argparse
import asyncio
import json
import math
import time
import xml.etree.ElementTree as ET
from contextlib import AsyncExitStack

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------- geometry ----------
def haversine_m(a, b):
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def load_gpx(path):
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    root = ET.parse(path).getroot()
    pts = root.findall(".//g:trkpt", ns) or root.findall(".//g:rtept", ns) or root.findall(".//trkpt")
    out = [(float(p.get("lat")), float(p.get("lon"))) for p in pts]
    if len(out) < 2:
        raise SystemExit("GPX needs at least 2 points")
    return out


def walk_points(route, speed, tick, loop):
    """Yield (lat, lng) once per tick, moving along route at speed m/s."""
    while True:
        for a, b in zip(route, route[1:]):
            steps = max(1, int(haversine_m(a, b) / (speed * tick)))
            for i in range(steps):
                f = i / steps
                yield a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        yield route[-1]
        if not loop:
            return
        route = route[::-1]  # walk back so we never teleport to the start


def pin_points(lat, lng):
    while True:
        yield lat, lng


def traj_points(traj):
    """Follow a motion.Trajectory against the wall clock; ends when it arrives (never, if looping)."""
    t0 = time.time()
    while True:
        el = time.time() - t0
        p = traj.pos_at(el)
        yield p["lat"], p["lng"]
        if not traj.loop and el >= traj.total_s:
            return


# ---------- device session ----------
class Session:
    """One tunnel + one DVT LocationSimulation channel. Rebuilt from scratch on any failure."""

    def __init__(self, rsd=None, udid=None):
        self.rsd_arg = rsd
        self.udid = udid
        self.stack = None
        self.loc = None
        self.device = ""

    async def open(self):
        await self.close()
        self.stack = AsyncExitStack()
        if self.rsd_arg:
            addr, port = self.rsd_arg[0], int(self.rsd_arg[1])
            rsd = await self.stack.enter_async_context(RemoteServiceDiscoveryService((addr, port)))
            log(f"using external tunnel {addr} {port}")
        else:
            from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel

            log("opening userspace tunnel (phone must be unlocked)...")
            rsd = await self.stack.enter_async_context(UserspaceRsdTunnel(serial=self.udid))
            log(f"tunnel up: {rsd.product_type} iOS {rsd.product_version}")
        self.device = f"{rsd.product_type} iOS {rsd.product_version}"
        dvt = await self.stack.enter_async_context(DvtProvider(rsd))
        self.loc = await self.stack.enter_async_context(LocationSimulation(dvt))
        log("location channel open")

    async def set(self, lat, lng):
        await self.loc.set(lat, lng)

    async def clear(self):
        if self.loc:
            await self.loc.clear()

    async def close(self):
        if self.stack:
            try:
                await self.stack.aclose()
            except Exception:
                pass
        self.stack = self.loc = None


# ---------- main loop ----------
async def run(args):
    sess = Session(args.rsd, args.udid)

    if args.cmd == "clear":
        await sess.open()
        await sess.clear()
        log("cleared")
        await sess.close()
        return

    if args.cmd == "pin":
        gen = pin_points(args.lat, args.lng)
    elif args.cmd in ("go", "plan"):
        from . import motion

        if args.cmd == "go":
            stops = [(args.lat1, args.lng1), (args.lat2, args.lng2)]
        else:
            stops = [motion.Stop(float(s["lat"]), float(s["lng"]), s.get("name", ""), float(s.get("dwell_s", 0)))
                     for s in json.load(open(args.file, encoding="utf-8"))]
        traj = motion.build(stops, args.mode, args.seed, args.cruise, args.loop, online=not args.straight, log=log)
        log(f"{len(traj.stops)} stops, {traj.total_m:.0f} m on roads, {traj.total_s / 60:.1f} min, "
            f"{args.mode}{' loop' if traj.loop else ''}, seed {traj.seed}")
        gen = traj_points(traj)
    else:
        gen = walk_points(load_gpx(args.gpx), args.speed, args.tick, not args.once)
    backoff = 2
    current = None
    try:
        while True:
            try:
                if sess.loc is None:
                    await sess.open()
                    backoff = 2
                    if current:
                        await sess.set(*current)
                        log(f"re-applied {current}")
                try:
                    nxt = next(gen)
                except StopIteration:
                    log("route finished; holding last point until Ctrl+C")
                    await asyncio.Event().wait()
                if nxt != current:
                    await sess.set(*nxt)
                    current = nxt
                    log(f"{nxt[0]:.6f}, {nxt[1]:.6f}")
                await asyncio.sleep(args.tick)
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                log(f"link lost: {e!r}; retry in {backoff}s")
                await sess.close()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log("stopping; clearing simulation")
        try:
            await sess.clear()
        except Exception:
            pass
    finally:
        await sess.close()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rsd", nargs=2, metavar=("ADDR", "PORT"), help="reuse an existing start-tunnel")
    p.add_argument("--udid", help="target device if more than one is plugged in")
    p.add_argument("--tick", type=float, default=1.0, help="seconds between updates (default 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pin", help="hold one coordinate")
    s.add_argument("lat", type=float)
    s.add_argument("lng", type=float)

    def motion_args(s):
        s.add_argument("--mode", choices=("walk", "bike", "drive"), default="drive")
        s.add_argument("--cruise", type=float, help="override cruise speed, m/s")
        s.add_argument("--seed", type=int, help="replay the exact same lights, pauses and GPS drift")
        s.add_argument("--loop", action="store_true", help="go round the stops forever")
        s.add_argument("--straight", action="store_true", help="skip road routing (offline)")

    s = sub.add_parser("go", help="drive/bike/walk from A to B on real roads, then hold at B")
    s.add_argument("lat1", type=float)
    s.add_argument("lng1", type=float)
    s.add_argument("lat2", type=float)
    s.add_argument("lng2", type=float)
    motion_args(s)

    s = sub.add_parser("plan", help="run an itinerary: JSON list of {lat,lng,name,dwell_s}")
    s.add_argument("file")
    motion_args(s)

    s = sub.add_parser("walk", help="walk a GPX track")
    s.add_argument("gpx")
    s.add_argument("--speed", type=float, default=1.4, help="m/s (1.4 walk, 4 jog, 12 car)")
    s.add_argument("--once", action="store_true", help="stop at the end instead of bouncing")

    sub.add_parser("clear", help="drop the simulated location")

    try:
        asyncio.run(run(p.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
