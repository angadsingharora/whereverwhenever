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
import logging
import time

from pymobiledevice3.exceptions import NoDeviceConnectedError, RemotePairingCompletedError
from pymobiledevice3.pair_records import iter_remote_pair_records_by_identifier
from pymobiledevice3.remote import tunnel_service as _ts
from pymobiledevice3.remote import userspace_tunnel as _ut

LAST = {"link": "", "udid": ""}  # how the most recent tunnel was reached
PREF = {"mode": "auto"}  # auto: USB if a cable is in, else Wi-Fi.  usb / wifi: only that.
MODES = ("auto", "usb", "wifi")
WIFI_BROWSE_S = 4


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
    if wifi_paired(lockdown.udid):
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


async def _rp_connect(identifier, address, port):
    """bonjour can answer with an address Python's hostname encoder rejects (a scoped IPv6,
    'label too long'). pymobiledevice3 only skips OSError, so turn that into one and the
    browse moves on to the device's next address instead of dropping the whole attempt."""
    try:
        return await _orig_rp_connect(identifier, address, port)
    except (UnicodeError, ValueError) as e:
        raise OSError(f"unusable address {address!r}: {e}") from e


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
