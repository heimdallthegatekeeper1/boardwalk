#!/usr/bin/env python3
"""reminders.py — "log for later" means a date, not a feeling.

★★★ WHY THE RENAME MATTERS. The button said "not now", and "not now" is a mood: it removes a row from
    view and promises nothing. Every one of those rows came back as a surprise, because nothing was
    holding the date. "Log for later" takes a WHEN — to the second — and the row carries it.

★★ CALENDAR INTEGRATION IS NOT BUILT AND IS NOT PRETENDED. He asked for online/offline calendar
    integration eventually; this is the proprietary store that comes first. It exports iCal (.ics) so
    the data is not trapped here when that day comes, but nothing syncs, nothing subscribes, and no
    calendar on this machine or any other knows these exist. Said plainly rather than implied.

★ AND A REMINDER THAT NOTHING READS IS A NOTE. `due()` is the read side, the board renders it on the
    row, and the session hook surfaces overdue ones — so the date has a reader before it has a promise.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "reminders.json"


def _ss():
    import importlib
    sys.path.insert(0, str(HERE))
    return importlib.import_module("safe_store")


# ★ Offered presets, plus a free datetime. Presets are what he will use nine times in ten; the free
#   field is why the tenth is not a fight.
PRESETS = {
    "1h":       ("In an hour",            3600),
    "3h":       ("This afternoon (3h)",   3 * 3600),
    "tonight":  ("Tonight (18:00)",       None),
    "tomorrow": ("Tomorrow morning (09:00)", None),
    "2d":       ("In two days",           2 * 86400),
    "1w":       ("Next week",             7 * 86400),
    "2w":       ("In two weeks",          14 * 86400),
    "1m":       ("In a month",            30 * 86400),
    "custom":   ("Pick a date and time",  None),
}


def resolve(preset: str, custom_iso: str = "", now: float | None = None) -> dict:
    """Turn a preset or an ISO string into an exact instant. ★ Refuses the past: a reminder set for a
    time already gone is a row that vanishes and never comes back, which is what 'not now' did."""
    n = now if now is not None else time.time()
    if preset == "custom":
        if not custom_iso.strip():
            return {"ok": False, "error": "pick a date and time, or choose a preset"}
        try:
            dt = datetime.fromisoformat(custom_iso.strip())
        except Exception as e:
            return {"ok": False, "error": f"could not read that date/time ({e}). "
                                          f"Use YYYY-MM-DDTHH:MM:SS"}
        ts = dt.timestamp()
    elif preset == "tonight":
        d = datetime.fromtimestamp(n).replace(hour=18, minute=0, second=0, microsecond=0)
        if d.timestamp() <= n:
            d += timedelta(days=1)
        ts = d.timestamp()
    elif preset == "tomorrow":
        d = (datetime.fromtimestamp(n) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        ts = d.timestamp()
    elif preset in PRESETS and PRESETS[preset][1]:
        ts = n + PRESETS[preset][1]
    else:
        return {"ok": False, "error": f"unknown preset {preset!r}; choose from {list(PRESETS)}"}
    if ts <= n:
        return {"ok": False, "error": "that time has already passed — a reminder in the past is a row "
                                      "that disappears and never returns"}
    return {"ok": True, "ts": ts,
            "iso": datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S"),
            "human": datetime.fromtimestamp(ts).strftime("%a %d %b, %H:%M:%S"),
            "in_seconds": int(ts - n)}


def set_reminder(item_id: str, preset: str, custom_iso: str = "", note: str = "",
                 by: str = "operator") -> dict:
    r = resolve(preset, custom_iso)
    if not r.get("ok"):
        return r
    d = _ss().read_json(STORE, {"reminders": []}) or {"reminders": []}
    d["reminders"] = [x for x in d["reminders"] if x.get("item") != item_id]
    d["reminders"].append({"item": item_id, "at": r["ts"], "iso": r["iso"], "human": r["human"],
                           "note": note[:400], "set_by": by,
                           "set_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _ss().write_json(STORE, d)
    return {"ok": True, **r, "item": item_id,
            "note_to_operator": ("This is a KHAOSS reminder only. No calendar on this machine or any "
                                 "other knows about it — calendar sync is not built. Export .ics if "
                                 "you want it somewhere that will actually nag you.")}


def all_reminders() -> list:
    d = _ss().read_json(STORE, {"reminders": []}) or {"reminders": []}
    return sorted(d.get("reminders", []), key=lambda x: x.get("at", 0))


def due(now: float | None = None) -> list:
    n = now if now is not None else time.time()
    return [r for r in all_reminders() if r.get("at", 0) <= n]


def to_ics() -> str:
    """★ Export so the data is not trapped here when calendar integration is built."""
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//KHAOSS//operator board//EN"]
    for r in all_reminders():
        st = datetime.fromtimestamp(r["at"]).strftime("%Y%m%dT%H%M%S")
        L += ["BEGIN:VEVENT", f"UID:khaoss-{r['item']}@boardwalk.local",
              f"DTSTART:{st}", f"DTEND:{st}",
              f"SUMMARY:Operator board — {r['item']}",
              f"DESCRIPTION:{(r.get('note') or 'log for later').replace(chr(10),' ')}",
              "END:VEVENT"]
    L.append("END:VCALENDAR")
    return "\n".join(L) + "\n"


def self_test() -> int:
    fails = []
    now = 1_700_000_000.0
    r = resolve("1h", now=now)
    if not r["ok"] or r["in_seconds"] != 3600:
        fails.append(f"1h preset wrong: {r}")
    # ★ the control that matters: a past time must be refused
    p = resolve("custom", "2001-01-01T00:00:00", now=now)
    if p.get("ok"):
        fails.append("a reminder in the PAST was accepted — that is the vanishing row 'not now' created")
    if resolve("nonsense", now=now).get("ok"):
        fails.append("unknown preset accepted")
    if resolve("custom", "not a date", now=now).get("ok"):
        fails.append("unparseable date accepted")
    t = resolve("tonight", now=now)
    if not t["ok"] or t["ts"] <= now:
        fails.append("tonight resolved to the past")
    # second-level precision must survive
    c = resolve("custom", "2099-03-04T05:06:07", now=now)
    if not c["ok"] or not c["iso"].endswith(":07"):
        fails.append(f"second-level precision lost: {c.get('iso')}")
    ics = to_ics()
    if "BEGIN:VCALENDAR" not in ics:
        fails.append("ics export malformed")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: presets resolve · A PAST TIME IS REFUSED · unknown preset refused · "
              "unparseable date refused · tonight rolls forward · seconds survive · ics exports")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if "--ics" in a:
        print(to_ics()); return 0
    rs = all_reminders()
    print(f"\n  REMINDERS · {len(rs)} set · {len(due())} due now")
    for r in rs:
        mark = "DUE" if r["at"] <= time.time() else "   "
        print(f"    {mark} {r['human']:<24} {r['item']}")
    print("\n  ★ KHAOSS-only. No calendar knows about these; sync is not built.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
