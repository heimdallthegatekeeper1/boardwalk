#!/usr/bin/env python3
"""paused.py — every deliberate pause, with an expiry, surfaced while it is paused.

★★★ WHY THIS EXISTS
    `khaoss-kb-capture.timer` was stopped and disabled on 2026-09-13 because one unattended run
    cost $0.78. Correct decision. Nobody recorded what would resume it, and nothing surfaced it
    while it was off. Five days later the operator asked why his captures had stopped arriving.

    **He was not missing captures. There had been none since 09-13, by our own hand.**

    ★★ THE CLASS: A DELIBERATE PAUSE WITH NO EXPIRY BECOMES A SILENT OUTAGE.
       It is the same shape as `capture_master.override` — a temporary measure that outlives its
       reason and quietly becomes the state nobody chose. And a stopped service that nothing
       surfaces is **indistinguishable from a broken one**, which is exactly how this read to him.

★ THE ASYMMETRY THAT MATTERS: pausing is cheap and reversing is expensive, because reversing
  requires somebody to REMEMBER. So the pause carries the memory: why, what would resume it, and a
  date after which it must be looked at again. An entry past its review date is a BOARD ITEM.

★ This registry never pauses or resumes anything itself. It records and it surfaces. The act stays
  a human decision; only the forgetting is engineered out.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

REGISTRY = Path(__file__).resolve().parent.parent / "operator" / "paused.json"


def _today() -> datetime.date:
    return datetime.date.today()


def load() -> dict:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"_doc": "Deliberate pauses. Each MUST carry why, resume_when and review_by. "
                        "An entry past review_by becomes a board item.", "entries": []}
    except Exception as exc:
        # ★ A corrupt registry is "could not look", never "nothing is paused".
        return {"_error": f"{exc.__class__.__name__}: {exc}", "entries": []}


def save(d: dict) -> None:
    REGISTRY.write_text(json.dumps(d, indent=2), encoding="utf-8")


def add(name: str, why: str, resume_when: str, review_days: int = 7,
        kind: str = "systemd-timer", costs: str = "") -> dict:
    """Record a pause. ★ All three of why / resume_when / review_by are REQUIRED —
    that is the whole mechanism, and an optional field would make it optional to remember."""
    if not why.strip() or not resume_when.strip():
        return {"ok": False, "error": "why and resume_when are both required — a pause without a "
                                      "resume condition is the defect this registry exists to stop"}
    d = load()
    d["entries"] = [e for e in d.get("entries", []) if e.get("name") != name]
    d["entries"].append({
        "name": name,
        "kind": kind,
        "why": why.strip(),
        "resume_when": resume_when.strip(),
        "costs_while_paused": costs.strip(),
        "paused_at": _today().isoformat(),
        "review_by": (_today() + datetime.timedelta(days=review_days)).isoformat(),
    })
    save(d)
    return {"ok": True, "name": name, "review_by": d["entries"][-1]["review_by"]}


def _live_state(e: dict) -> str:
    """Is the thing actually still paused? ★ The registry is a CLAIM; this measures it."""
    if e.get("kind") != "systemd-timer":
        return "unverified"
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", e["name"]],
                           capture_output=True, text=True, timeout=8)
        return r.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def status() -> dict:
    d = load()
    out = {"paused": [], "overdue": [], "resumed_without_record": []}
    for e in d.get("entries", []):
        live = _live_state(e)
        row = dict(e, live=live)
        overdue = False
        try:
            # ★ <= not <. "review by the 18th" means it is DUE ON the 18th. With a strict <,
            #   an item due today stays silent for one more day — a one-day silent outage
            #   engineered into the thing built to stop silent outages.
            overdue = datetime.date.fromisoformat(e["review_by"]) <= _today()
        except Exception:
            overdue = True            # unparseable review date = treat as overdue, never as fine
        row["overdue"] = overdue
        if live == "active":
            # ★ It is running again and the registry still says paused. Not an error — a stale
            #   record, which is its own small version of the same disease.
            out["resumed_without_record"].append(row)
            continue
        (out["overdue"] if overdue else out["paused"]).append(row)
    if "_error" in d:
        out["error"] = d["_error"]
    return out


def board_rows() -> list:
    """Rows for the operator board — ★ this is what makes a pause visible while it is paused."""
    st = status()
    rows = []
    for e in st["overdue"]:
        rows.append({
            "id": f"derived:paused:{e['name']}",
            "sev": "stop", "derived": True, "source": "paused",
            "title": f"{e['name']} has been paused past its review date ({e['review_by']})",
            "one": (f"Paused {e['paused_at']} because: {e['why']} "
                    f"Resume when: {e['resume_when']}. "
                    f"{('Costs while paused: ' + e['costs_while_paused']) if e.get('costs_while_paused') else ''}").strip(),
        })
    d = died_unannounced()
    if d:
        rows.append({
            "id": "derived:paused:capture-died-unannounced",
            "sev": "stop", "derived": True, "source": "paused",
            "title": "a capture run produced output today and never reported starting or ending",
            "one": (f"approved/ gained documents up to {d['newest_output']}, but the heartbeat's "
                    f"last line is {d['last_heartbeat']} — {d['gap_hours']}h earlier. The run wrote "
                    f"no start, no yield and no exit line. It is neither paused nor finished, and "
                    f"nothing announced it. ({d['runs_with_terminal_record']} earlier runs did "
                    f"record a terminal line, so the absence is the anomaly.)"),
        })
    for e in st["resumed_without_record"]:
        rows.append({
            "id": f"derived:paused-stale:{e['name']}",
            "sev": "note", "derived": True, "source": "paused",
            "title": f"{e['name']} is running again but is still recorded as paused",
            "one": "The registry is stale. Remove the entry so the record matches the box.",
        })
    return rows


HEARTBEAT = Path(__file__).resolve().parent.parent / "capture_work" / "capture_heartbeat.log"
APPROVED = Path(__file__).resolve().parent.parent / "capture_work" / "approved"


def died_unannounced() -> dict | None:
    """★★★ 'PAUSED' AND 'ATTEMPTED AND DIED' ARE DIFFERENT STATES. Only one was on his board.

    Captures were deliberately paused on 09-13 (recorded above). Then on 2026-09-18 something
    ATTEMPTED to resume: approved/ gained documents up to 08:07:13 and the run stopped at [16/104]
    with 89 KEEP documents unprocessed. It wrote NO heartbeat line and NO terminal record.

    ★ The signature, and it is exact: every completed run in this log pairs a `yield ...` line with
      an `ok exit=` line. A run whose output exists while its heartbeat does not is one that
      started and never reported an ending. That is neither paused nor done, and until now the
      board could only say "paused" — which reads as a decision when it was a failure.
    ★★ Same family as the pause registry: a state nobody chose, that nothing surfaces.
    """
    try:
        if not HEARTBEAT.exists():
            return None
        lines = [l for l in HEARTBEAT.read_text(errors="replace").splitlines() if l.strip()]
        hb_mtime = HEARTBEAT.stat().st_mtime
        newest_out = 0.0
        if APPROVED.is_dir():
            newest_out = max((f.stat().st_mtime for f in APPROVED.iterdir()), default=0.0)
        if not newest_out:
            return None
        # Output produced measurably AFTER the last heartbeat line = a run the log never saw.
        if newest_out - hb_mtime < 300:
            return None
        import datetime as _dt
        last_hb = lines[-1][:19] if lines else "never"
        closed = sum(1 for l in lines if "exit=" in l)
        return {
            "last_heartbeat": last_hb,
            "newest_output": _dt.datetime.fromtimestamp(newest_out).isoformat(timespec="seconds"),
            "gap_hours": round((newest_out - hb_mtime) / 3600, 1),
            "runs_with_terminal_record": closed,
        }
    except Exception:
        return None


def self_test() -> int:
    """★ Negative controls: the failure modes, not the happy path."""
    import tempfile, shutil
    global REGISTRY, HEARTBEAT, APPROVED
    original, orig_hb, orig_app = REGISTRY, HEARTBEAT, APPROVED
    tmp = Path(tempfile.mkdtemp()) / "paused.json"
    REGISTRY = tmp
    # ★★★ ISOLATE THE LIVE BOX OUT OF THE TEST. Adding died_unannounced() made this self-test read
    #   the operator's REAL capture heartbeat, so board_rows() returned 2 and the test failed —
    #   not because the code was wrong, but because a real anomaly existed on his machine.
    #   ★ A self-test that measures live state passes or fails on what he happens to have running.
    #     The other seat hit this exact shape today (nine controls reading his real capture policy).
    #     Point the detector at the temp dir; the detector's OWN behaviour is tested separately.
    HEARTBEAT = tmp.parent / "no_such_heartbeat.log"
    APPROVED = tmp.parent / "no_such_approved"
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += (not ok)
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        check("★ a pause with no resume condition is REFUSED",
              add("x.timer", "cost", "", 7).get("ok") is False)
        check("★ a pause with no reason is REFUSED",
              add("x.timer", "", "when cheap", 7).get("ok") is False)
        check("a complete pause is accepted",
              add("x.timer", "cost $0.78/run", "balance confirmed > $20", 7).get("ok") is True)

        d = load(); d["entries"][0]["review_by"] = "2020-01-01"; save(d)
        st = status()
        check("★ past its review date -> OVERDUE, not quietly paused", len(st["overdue"]) == 1)
        check("  and it produces a BOARD ROW (this is what makes it visible)",
              len(board_rows()) == 1 and "past its review date" in board_rows()[0]["title"])

        d = load(); d["entries"][0]["review_by"] = "not-a-date"; save(d)
        check("★ an unparseable review date is treated as OVERDUE, never as fine",
              len(status()["overdue"]) == 1)

        tmp.write_text("{not json", encoding="utf-8")
        st2 = status()
        check("★ a corrupt registry reports an ERROR, never 'nothing is paused'",
              "error" in st2 and not st2["paused"])
        # ★ and the detector's own behaviour, tested against SYNTHETIC state rather than his box
        hb = tmp.parent / "hb.log"; app = tmp.parent / "app"
        app.mkdir(exist_ok=True)
        hb.write_text("2026-09-17T23:58:20-07:00 yield seen=42 new=38 ingested=12\n")
        import os as _os, time as _t
        _os.utime(hb, (_t.time() - 40000, _t.time() - 40000))
        (app / "doc.json").write_text("{}")          # output NEWER than the heartbeat
        HEARTBEAT, APPROVED = hb, app
        check("★ output newer than the heartbeat -> DIED UNANNOUNCED", died_unannounced() is not None)
        _os.utime(hb, None)                          # heartbeat now newest again
        check("  heartbeat current -> no anomaly", died_unannounced() is None)
        HEARTBEAT = tmp.parent / "gone.log"
        check("★ missing heartbeat -> no claim either way (not a false alarm)",
              died_unannounced() is None)
    finally:
        REGISTRY, HEARTBEAT, APPROVED = original, orig_hb, orig_app
        shutil.rmtree(tmp.parent, ignore_errors=True)

    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILED'} — "
          f"a pause cannot be recorded without an expiry, and cannot expire silently.")
    return 1 if fails else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if args == ["--self-test"]:
        return self_test()
    if args and args[0] == "--add":
        if len(args) < 4:
            print("usage: paused.py --add <name> <why> <resume_when> [review_days] [costs]",
                  file=sys.stderr)
            return 2
        days = int(args[4]) if len(args) > 4 and args[4].isdigit() else 7
        costs = args[5] if len(args) > 5 else ""
        print(json.dumps(add(args[1], args[2], args[3], days, costs=costs), indent=2))
        return 0
    if args and args != ["--json"]:
        print(f"unknown argument(s): {' '.join(args)}\n"
              f"usage: paused.py [--json | --self-test | --add <name> <why> <resume_when>]",
              file=sys.stderr)
        return 2
    st = status()
    if args == ["--json"]:
        print(json.dumps(st, indent=2)); return 0
    if st.get("error"):
        print(f"  ★ registry unreadable: {st['error']}"); return 1
    if not (st["paused"] or st["overdue"] or st["resumed_without_record"]):
        print("  nothing recorded as paused."); return 0
    for e in st["overdue"]:
        print(f"  ★ OVERDUE  {e['name']}  (review_by {e['review_by']}, live={e['live']})")
        print(f"             why: {e['why']}")
        print(f"             resumes when: {e['resume_when']}")
        if e.get("costs_while_paused"):
            print(f"             costs while paused: {e['costs_while_paused']}")
    for e in st["paused"]:
        print(f"    paused   {e['name']}  (review_by {e['review_by']}, live={e['live']})")
    for e in st["resumed_without_record"]:
        print(f"    ★ stale  {e['name']} is ACTIVE again but still recorded as paused")
    return 0


if __name__ == "__main__":
    sys.exit(main())
