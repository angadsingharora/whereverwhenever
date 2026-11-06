"""link.py provider selection, with the pymobiledevice3 calls faked."""
import asyncio

import pytest
from pymobiledevice3.exceptions import NoDeviceConnectedError, RemotePairingCompletedError

from whereverwhenever import link


class FakeSvc:
    def __init__(self, ident, host="10.0.0.5"):
        self.remote_identifier, self.hostname, self.closed = ident, host, False

    async def close(self):
        self.closed = True


class FakeLockdown:
    udid = "ABC123"


@pytest.fixture
def fakes(monkeypatch):
    calls = {"usb": 0, "browse": 0, "pair": 0}
    paired = {"ids": []}
    monkeypatch.setattr(link, "wifi_paired", lambda udid=None: [i for i in paired["ids"] if udid in (None, i)])

    async def usb(serial, autopair, fallback=True):
        calls["usb"] += 1
        if not fakes.cable:
            raise NoDeviceConnectedError()
        return "usb-provider", FakeLockdown()

    async def browse(bonjour_timeout=0, udid=None, excluded_identifiers=()):
        calls["browse"] += 1
        return [FakeSvc(i) for i in fakes.on_wifi if udid in (None, i)]

    class PairSvc:
        @classmethod
        async def create(cls, lockdown):
            return cls()

        async def connect(self, autopair):
            calls["pair"] += 1
            paired["ids"].append("ABC123")
            raise RemotePairingCompletedError()

        async def close(self):
            pass

    monkeypatch.setattr(link, "_orig_provider", usb)
    monkeypatch.setattr(link._ts, "get_remote_pairing_tunnel_services", browse)
    monkeypatch.setattr(link._ts, "RemotePairingLockdownService", PairSvc)
    monkeypatch.setattr(link, "LAST", {"link": "", "udid": ""})
    monkeypatch.setattr(link, "PREF", {"mode": "auto"})
    fakes.cable, fakes.on_wifi, fakes.calls, fakes.paired = True, [], calls, paired
    return fakes


def run(c):
    return asyncio.run(c)


def test_usb_session_pairs_once(fakes):
    prov, ld = run(link._provider(None, True))
    assert prov == "usb-provider" and ld.udid == "ABC123"
    assert link.LAST == {"link": "usb", "udid": "ABC123"} and fakes.calls["pair"] == 1
    run(link._provider(None, True))
    assert fakes.calls["pair"] == 1  # record exists now, no second pair-setup


def test_auto_falls_back_to_wifi_only_when_paired(fakes):
    fakes.cable = False
    with pytest.raises(NoDeviceConnectedError, match="plug in once"):
        run(link._provider(None, True))
    assert fakes.calls["browse"] == 0  # no record, so no network scan
    fakes.paired["ids"].append("ABC123")
    with pytest.raises(NoDeviceConnectedError, match="answering on Wi-Fi"):
        run(link._provider(None, True))
    fakes.on_wifi = ["ABC123", "OTHER"]
    svc, ld = run(link._provider(None, True))
    assert ld is None and svc.remote_identifier == "ABC123" and link.LAST["link"] == "wifi"


def test_remembers_udid_for_wifi(fakes):
    run(link._provider(None, True))  # USB first: learns the udid
    fakes.cable, fakes.on_wifi = False, ["OTHER", "ABC123"]
    svc, _ = run(link._provider(None, True))
    assert svc.remote_identifier == "ABC123"


def test_usb_only_never_scans(fakes):
    link.PREF["mode"] = "usb"
    fakes.cable, fakes.paired["ids"], fakes.on_wifi = False, ["ABC123"], ["ABC123"]
    with pytest.raises(NoDeviceConnectedError):
        run(link._provider(None, True))
    assert fakes.calls["browse"] == 0


def test_wifi_only_ignores_cable(fakes):
    link.PREF["mode"] = "wifi"
    fakes.paired["ids"], fakes.on_wifi = ["ABC123"], ["ABC123"]
    svc, _ = run(link._provider(None, True))
    assert svc.remote_identifier == "ABC123" and fakes.calls["usb"] == 0


def test_pairing_failure_does_not_block_usb(fakes, monkeypatch):
    async def boom(lockdown):
        raise RuntimeError("phone said no")

    monkeypatch.setattr(link, "pair_for_wifi", boom)
    prov, _ = run(link._provider(None, True))
    assert prov == "usb-provider" and link.LAST["link"] == "usb"
