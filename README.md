# geofake

Spoof an iPhone's GPS location from a Windows/Linux/Mac box. No jailbreak, no admin,
no paid app. A map UI you click on, plus a CLI. Built on
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

> **Status (Nov 2026):** works on iOS 17.4 through 26.x for Maps and most apps.
> On **iOS 27.2** the phone accepts the command and ignores it. **Find My** stopped
> honouring simulated location around iOS 26.6 regardless. See [The hack](#the-hack)
> and [Workarounds](#workarounds) before you spend time on this.

## How it works

Apple ships a developer feature that lets Xcode inject a fake CoreLocation fix into a
paired phone, so developers can test location-aware apps. It's reached over the
iOS 17+ developer tunnel through the DVT instruments channel
(`com.apple.instruments.server.services.LocationSimulation`). Every commercial
"location changer" app for iPhone is a wrapper around this same channel.

geofake does three things:

1. Opens a no-root, in-process tunnel to the phone over USB (pymobiledevice3's
   userspace tunnel).
2. Opens the LocationSimulation channel and sends `simulateLocationWithLatitude:longitude:`.
3. **Keeps that session open.** The phone only honours the fake location while the
   session lives. Drop the cable, sleep the PC, kill the script, and real GPS is back
   within seconds. geofake reconnects and re-applies the last pin on any drop.

## Setup

1. iPhone: Settings > Privacy & Security > Developer Mode > on. Reboot, confirm.
   If the toggle isn't there: plug in, then `pymobiledevice3 amfi reveal-developer-mode`.
2. Windows only: install the **Apple Devices** app (Microsoft Store) or iTunes for
   the USB driver.
3. PC, phone plugged in, unlocked, trusted:
   ```
   pip install -U pymobiledevice3
   pymobiledevice3 mounter auto-mount
   ```

## Use

Map UI:
```
python geoweb.py
```
Open http://127.0.0.1:8765. Click the map to drop a pin, hit **Teleport**. Drag to
fine-tune. Search a place name or paste `lat, lng`. Star a spot to save it. **Real GPS**
clears the spoof. Ctrl+C in the terminal also clears it.

CLI:
```
python geofake.py pin 37.8716 -122.2727
python geofake.py walk route.gpx --speed 1.4     # walk a GPX track at 1.4 m/s, back and forth
python geofake.py clear
```

Both take `--rsd ADDR PORT` to reuse a tunnel from `pymobiledevice3 lockdown start-tunnel`,
and `--udid` if more than one phone is attached. `geoweb.py --no-device` runs the UI
without a phone.

## Verify

1. Status box in the sidebar goes green with your iPhone model and iOS version.
2. Apple Maps on the phone jumps to the pin. If it doesn't, the phone is ignoring the spoof.
3. For Find My, check from the **outside**: iCloud.com > Find My, or a friend's phone.
   Watch for a full minute. Known behaviour on 26.6+: flashes the fake spot for half a
   second, then snaps back to real.

## The hack

What this exploits, and where it stops:

- **It's an official feature, not a bug.** Developer Mode and location simulation are
  Apple-sanctioned. Nothing here voids warranty or touches the OS. Fully reversible.
- **It's tethered.** The computer must be in the loop the whole time, over USB or the
  same Wi-Fi. There is no way to make the phone remember a fake location on its own
  without a jailbreak, and there is no jailbreak for current hardware.
- **It can't run on the phone.** iOS won't let an app send the phone its own developer
  commands, and there's no public API to override system location.
- **iOS 27.2 ignores it.** Tested on iPhone18,3 / 27.2 with pymobiledevice3 11.43: tunnel
  up, channel open, `set` accepted with no error, phone doesn't move. The developer
  image available is the iOS 27.0 beta build (27A5228h), the newest the tooling ships.
  Whether Apple closed the channel or the image is just too old for 27.2 is unknown.
  The legacy pre-17 service (`com.apple.dt.simulatelocation`) is gone from the phone.
- **Find My is a separate wall.** Even where Maps moves (iOS 26.x), Find My reverts to
  the real location within a second. Multiple independent reports since 26.6. Apple
  appears to have moved Find My's reporting off the path that simulation overrides.
- **Offline leak.** If the phone drops off the network, the Find My network reports its
  real position via nearby Apple devices regardless of any spoof.
- **Anti-spoof apps detect it.** Pokémon Go, some banks and dating apps check for
  simulated location and ban.

## Workarounds

For "my friend sees me somewhere else in Find My", in order of how well they actually work:

1. **Second iPhone.** Sign a spare phone into your Apple ID, leave it where you want to
   appear, set it as the sharing device: Settings > your name > Find My > My Location.
   No computer, no cable, no iOS version dependency. This is the only one that holds up.
2. **Pause sharing.** Find My > People > the person > Stop Sharing, or toggle Share My
   Location off. Shows "No location found" rather than a fake one.
3. **This tool, on iOS 26.x or earlier,** for apps other than Find My. Keep the PC awake
   and the cable in.
4. **Wait for tooling.** Check `pip install -U pymobiledevice3` occasionally. If
   `pymobiledevice3 mounter list` ever shows a developer image newer than 27A5228h, retry.

Things that don't work: GPS RF spoofing with an SDR (illegal to transmit nearly
everywhere, and the phone cross-checks Wi-Fi and cell positioning anyway), browser
geolocation extensions (fool websites only), any paid "location changer" app (same
channel, same limits).

## Files

- `geoweb.py` + `page.html`: local web server and map UI
- `geofake.py`: CLI, GPX walking, and the shared `Session` class
