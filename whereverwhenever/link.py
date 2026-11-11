"""USB or Wi-Fi, whichever reaches the phone.

pymobiledevice3's no-root userspace tunnel only looks on USB. This module wraps its provider
selection so that:

* with a cable, the normal USB (CoreDeviceProxy) tunnel is used, and the first time a phone
  is seen we also run a promptless RemotePairing pair-setup over that trusted USB channel. That
  writes the pair record a Wi-Fi tunnel needs, with no Trust dialog on the phone;
* with no cable, paired phones on the same Wi-Fi are found over bonjour and the tunnel runs
  over the network instead.

So: plug in once, and from then on the laptop and the phone just need to share a Wi-Fi.
"""
import asyncio
import logging
import socket
import time

import os

from pymobiledevice3.exceptions import ConnectionTerminatedError, NoDeviceConnectedError, RemotePairingCompletedError
from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
from pymobiledevice3.remote import tunnel_service as _ts
from pymobiledevice3.remote import userspace_tunnel as _ut

LAST = {"link": "", "udid": ""}  # how the most recent tunnel was reached
PREF = {"mode": "auto"}  # auto: USB if a cable is in, else Wi-Fi.  usb / wifi: only that.
MODES = ("auto", "usb", "wifi")
WIFI_BROWSE_S = 4
STALE = set()  # UDIDs whose Wi-Fi pair record the phone refused; re-paired on the next cable session
RP_CONNECT_S = 4  # per address; a wrong interface just hangs


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def wifi_paired(udid=None):
    """UDIDs with a RemotePairing record good enough to be matched over bonjour."""
    out = []
    for ident, _path, rec in iter_remote_pair_records_by_identifier():
        if rec.get(_ts.PEER_ALT_IRK_KEY) is not None and (udid is None or ident == udid):
            out.append(ident)
    return out


async def pair_for_wifi(lockdown):
    """Promptless RemotePairing pair-setup over the USB lockdown channel. Idempotent."""
    if lockdown.udid in STALE:  # the phone refused this record over Wi-Fi: throw it away, pair afresh
        for ident, path, _rec in list(iter_remote_pair_records_by_identifier()):
            if ident == lockdown.udid:
                try:
                    os.remove(path)
                except OSError:
                    pass
        STALE.discard(lockdown.udid)
    elif wifi_paired(lockdown.udid):
        return False
    svc = await _ts.RemotePairingLockdownService.create(lockdown)
    try:
        try:
            await svc.connect(autopair=True)
        except RemotePairingCompletedError:
            pass  # the phone hangs up once pairing is written; that's success
    finally:
        await svc.close()
    return True


_orig_provider = _ut._create_no_root_tunnel_provider
_orig_rp_connect = _ts.create_core_device_tunnel_service_using_remotepairing


def _lan_ifaces():
    """Interfaces a phone can be on: Wi-Fi / Ethernet / hotspot bridges, not loopback or tunnels."""
    names = [n for _i, n in socket.if_nameindex()]
    keep = [n for n in names if n.startswith(("en", "bridge", "wl", "eth", "Wi-Fi", "Ethernet"))]
    return keep or [n for n in names if not n.startswith(("lo", "utun", "awdl", "llw", "gif", "stf", "anpi", "ap"))]


async def _rp_connect(identifier, address, port):
    """Two things go wrong with the addresses bonjour hands back, both on IPv6-only networks
    (a T-Mobile iPhone hotspot is one), where the link-local address is the only one that works:
    it can be scoped to the wrong interface (lo0), and some forms make Python's hostname
    encoder raise UnicodeError, which pymobiledevice3 doesn't skip. So a link-local address
    is tried on each real network interface, and anything unusable becomes an OSError, which
    makes the browse move on to the next address."""
    ip, _, scope = address.partition("%")
    tries = [address]
    if ip.lower().startswith("fe80"):
        tries = [f"{ip}%{n}" for n in _lan_ifaces() if n != scope] + ([address] if scope and not scope.startswith("lo") else [])
    # all candidates at once: a wrong interface hangs rather than failing, the right one answers
    tasks = [asyncio.ensure_future(asyncio.wait_for(_orig_rp_connect(identifier, a, port), RP_CONNECT_S))
             for a in tries]
    won, last, refused = None, None, False
    for fut in asyncio.as_completed(tasks):
        try:
            svc = await fut
        except ConnectionTerminatedError as e:  # reached the phone; it rejected our pair record
            last, refused = e, True
            continue
        except (Exception, asyncio.CancelledError) as e:
            last = e
            continue
        if won is None:
            won = svc
            for t in tasks:
                t.cancel()
        else:  # a second interface also answered; one tunnel is enough
            try:
                await svc.close()
            except Exception:
                pass
    if won is None:
        if refused:
            STALE.add(identifier)
        raise OSError(f"unusable address {address!r}: {last!r}") from last
    STALE.discard(identifier)
    return won


_ts.create_core_device_tunnel_service_using_remotepairing = _rp_connect


class _BlockedWatch(logging.Handler):
    """pymobiledevice3 only logs when the OS refuses its mDNS queries, then returns nothing.
    Catch that line so 'macOS is blocking this' isn't reported as 'phone not found'."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.blocked = False

    def emit(self, record):
        if "discovery is blocked" in record.getMessage():
            self.blocked = True


async def _wifi(serial):
    want = serial or LAST["udid"] or None
    if not wifi_paired(want):
        raise NoDeviceConnectedError(
            "no phone on USB, and none paired for Wi-Fi yet (plug in once to pair)" if PREF["mode"] != "wifi" else "not paired for Wi-Fi yet (plug in once to pair)"
        ) from None
    log(f"no cable; looking for the phone on Wi-Fi{' (' + want[:8] + '…)' if want else ''}")
    watch, blog = _BlockedWatch(), logging.getLogger("pymobiledevice3.bonjour")
    blog.addHandler(watch)
    try:
        services = await _ts.get_remote_pairing_tunnel_services(bonjour_timeout=WIFI_BROWSE_S, udid=want)
    finally:
        blog.removeHandler(watch)
    if not services and watch.blocked:
        raise NoDeviceConnectedError(
            "Wi-Fi blocked by macOS: System Settings > Privacy & Security > Local Network, "
            "turn on your terminal app, then restart the bridge (or use the cable)"
        ) from None
    if not services and (want in STALE if want else STALE):
        raise NoDeviceConnectedError(
            "phone found on Wi-Fi but it refused this computer's pairing: plug the cable in once to pair again"
        ) from None
    if not services:
        raise NoDeviceConnectedError(
            "no phone answering on Wi-Fi (same network? unlocked since reboot?)"
        ) from None
    for extra in services[1:]:
        try:
            await extra.close()
        except Exception:
            pass
    svc = services[0]
    LAST["link"], LAST["udid"] = "wifi", svc.remote_identifier
    log(f"phone found on Wi-Fi at {svc.hostname}")
    return svc, None


async def _provider(serial, autopair, remotepairing_fallback=True):
    if PREF["mode"] == "wifi":
        return await _wifi(serial)
    try:
        provider, lockdown = await _orig_provider(serial, autopair, remotepairing_fallback)
    except NoDeviceConnectedError:
        if PREF["mode"] == "usb":
            raise
        return await _wifi(serial)
    LAST["link"] = "usb"
    if lockdown is not None:
        LAST["udid"] = lockdown.udid
        try:
            if await pair_for_wifi(lockdown):
                log("paired for Wi-Fi: from now on the cable is optional, same network is enough")
        except Exception as e:  # pairing is a bonus, never block the USB session on it
            log(f"Wi-Fi pairing skipped: {type(e).__name__}: {e}")
    return provider, lockdown


_ut._create_no_root_tunnel_provider = _provider
