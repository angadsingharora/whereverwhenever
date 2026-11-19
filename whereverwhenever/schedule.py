"""
schedule: when a scheduled spoof starts and stops.

An entry is in local wall-clock time, on the bridge's computer:

  start  "HH:MM"              when it begins
  end    "HH:MM" or None      when it hands the phone back to real GPS; earlier than start
                              means the next day (22:00-07:00 is overnight). None: it stays
                              until something else moves the phone.
  days   [0..6] (0 = Monday)  repeat on these weekdays, or [] with
  date   "YYYY-MM-DD"         for a one-off

Pure time logic, no I/O: the bridge calls step() every couple of seconds and acts on what
it returns. `fired` (start of the last occurrence handled) and `until` (end of the one in
progress) live on the entry and are saved with it, so a restart neither repeats a start
nor forgets an end.
"""
import datetime as dt
import re
import secrets

GRACE_S = 15 * 60  # an open-ended start missed by less than this (bridge was down) still fires
DAY = dt.timedelta(days=1)


def hm(s):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(s or "").strip())
    if not m:
        raise ValueError(f"time {s!r}: want HH:MM, 24-hour")
    return int(m[1]), int(m[2])


def _at(day, t):
    return dt.datetime.combine(day, dt.time(*t)).timestamp()  # naive local: follows DST


def validate(e, what):
    """Normalise a request into a stored entry. `what` is already validated by the caller."""
    start = hm(e.get("start"))
    end = e.get("end") or None
    if end is not None and hm(end) == start:
        raise ValueError("end is the same as start")
    days = sorted({int(d) for d in (e.get("days") or [])})
    if any(not 0 <= d <= 6 for d in days):
        raise ValueError("days are 0 (Monday) to 6 (Sunday)")
    date = None
    if not days:
        date = dt.date.fromisoformat(str(e.get("date") or "")).isoformat()
    return {"id": secrets.token_hex(4), "name": str(e.get("name") or "").strip()[:60] or "Scheduled",
            "what": what, "start": "%02d:%02d" % start, "end": end and "%02d:%02d" % hm(end),
            "days": days, "date": date, "enabled": True, "fired": None, "until": None, "next": None}


def starts(e, lo, hi):
    """Occurrence start times (epoch seconds) in [lo, hi], oldest first."""
    t = hm(e["start"])
    if not e["days"]:
        s = _at(dt.date.fromisoformat(e["date"]), t)
        return [s] if lo <= s <= hi else []
    out, d = [], dt.date.fromtimestamp(lo) - DAY
    while d <= dt.date.fromtimestamp(hi):
        if d.weekday() in e["days"]:
            s = _at(d, t)
            if lo <= s <= hi:
                out.append(s)
        d += DAY
    return out


def last_start(e, now):
    s = starts(e, now - 8 * 86400, now)
    return s[-1] if s else None


def next_start(e, now):
    s = [x for x in starts(e, now, now + 8 * 86400) if x > now]
    return s[0] if s else None


def end_of(e, start_t):
    if not e["end"]:
        return None
    d = dt.datetime.fromtimestamp(start_t).date()
    t = _at(d, hm(e["end"]))
    return t if t > start_t else _at(d + DAY, hm(e["end"]))


def arm(e, now):
    """Make step() start a window already under way, and skip any occurrence that is over
    (or, open-ended, already past): nothing fires for times before the entry was switched on."""
    t0 = last_start(e, now)
    inside = t0 is not None and e["end"] is not None and now < end_of(e, t0)
    e["fired"] = None if inside else t0
    e["until"] = None
    e["next"] = next_start(e, now)


def created(e, now):
    """Set up a fresh entry. Raises if a one-off has nothing left to do."""
    arm(e, now)
    if finished(e):
        raise ValueError("that time has already passed")
    return e


def step(e, now):
    """Advance one entry to `now`. Returns the events to act on, in order:
    "end" (its window closed), "start" (an occurrence begins now), "missed" (one came and went
    while nobody was watching)."""
    ev = []
    if not e["enabled"]:
        return ev
    if e.get("until") and now >= e["until"]:
        e["until"] = None
        ev.append("end")
    t0 = last_start(e, now)
    if t0 is not None and t0 > (e.get("fired") or 0):
        e["fired"] = t0
        until = end_of(e, t0)
        if (until and now < until) or (not until and now - t0 <= GRACE_S):
            e["until"] = until
            ev.append("start")
        else:
            ev.append("missed")
    e["next"] = next_start(e, now)
    return ev


def finished(e):
    """A one-off that has started and, if it had an end, ended."""
    return not e["days"] and e.get("fired") is not None and not e.get("until")
