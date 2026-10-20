#!/usr/bin/env python3
"""alternation.py — every other pass is a roadmap pass, and displacement twice running is a finding.

★★★ THE NUMBER THAT FORCED THIS. Board items over the fortnight: 21 → 48 → 67 → 110 → 117 → 115.
    `verified-by-him`: 0 yes. **+34 added / −0 closed in the last seven days.**
    A board that grows every turn and closes nothing is not a queue, it is an ACCUMULATOR — and its
    founding claim was that emptying it would finally mean something.

★★ THE WORK WAS GOOD AND MOSTLY FORCED — atomic stores, the attribution finding, the seat map. That
    is exactly why a displacement rule that only RECORDS is useless: every displacement had a good
    reason, and good reasons are what displaced the roadmap fourteen turns running.

★ SO THE RULE HAS TEETH: two displacements in a row is itself a FINDING and goes on his board as one.
    Not a reminder to us — a row he can see, because we are the ones who keep deciding it was
    justified.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "alternation_log.json"


def _ss():
    import importlib
    sys.path.insert(0, str(HERE))
    return importlib.import_module("safe_store")


def load() -> dict:
    return _ss().read_json(LOG, {"passes": []}) or {"passes": []}


def record(kind: str, what: str, by: str = "cc-khaoss-dev") -> dict:
    """kind: 'roadmap' (advanced the roadmap) or 'displaced' (did something else instead)."""
    if kind not in ("roadmap", "displaced"):
        raise ValueError("kind must be 'roadmap' or 'displaced'")
    d = load()
    d["passes"].append({"at": time.strftime("%Y-%m-%d %H:%M"), "kind": kind,
                        "what": what[:220], "by": by})
    _ss().write_json(LOG, d)
    return status()


def status() -> dict:
    d = load()
    ps = d["passes"]
    run = 0
    for p in reversed(ps):
        if p["kind"] == "displaced":
            run += 1
        else:
            break
    recent = ps[-6:]
    owed = run >= 2
    return {
        "passes_recorded": len(ps),
        "consecutive_displacements": run,
        "next_pass_must_be_roadmap": owed,
        "recent": recent,
        "finding": ({
            "id": "roadmap-displaced-twice",
            "title": f"The roadmap has been displaced {run} passes running",
            "why": ("Every displacement had a good reason. That is the problem — good reasons are "
                    "what displaced it every turn for a fortnight, while the board went from 21 "
                    "items to 115 and closed none."),
        } if owed else None),
        "note": ("A pass is 'roadmap' only if it advanced a roadmap item. Fixing something found "
                 "while doing that is still a roadmap pass; abandoning it to fix something else "
                 "is a displacement, however justified."),
    }


def self_test() -> int:
    fails = []
    import tempfile
    global LOG
    real = LOG
    LOG = Path(tempfile.mkdtemp()) / "alt.json"
    try:
        try:
            record("nonsense", "x"); fails.append("accepted an unknown kind")
        except ValueError:
            pass
        record("roadmap", "a")
        if status()["consecutive_displacements"] != 0:
            fails.append("a roadmap pass did not reset the run")
        record("displaced", "b")
        if status()["next_pass_must_be_roadmap"]:
            fails.append("ONE displacement already demanded a roadmap pass — the rule is two")
        record("displaced", "c")
        st = status()
        if not st["next_pass_must_be_roadmap"] or not st["finding"]:
            fails.append("two displacements in a row did NOT produce a finding — the rule has no teeth")
        record("roadmap", "d")
        if status()["finding"] is not None:
            fails.append("a roadmap pass did not clear the finding")
    finally:
        LOG = real
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  4 controls pass: unknown kind refused · one displacement is not yet a finding · "
              "two in a row IS a finding · a roadmap pass clears it")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if a and a[0] in ("roadmap", "displaced"):
        print(json.dumps(record(a[0], " ".join(a[1:]) or "(unnamed)"), indent=2)); return 0
    s = status()
    print(f"\n  ALTERNATION · {s['passes_recorded']} pass(es) recorded")
    print(f"  consecutive displacements: {s['consecutive_displacements']}")
    print(f"  next pass must be a roadmap pass: {s['next_pass_must_be_roadmap']}")
    for p in s["recent"]:
        print(f"    {p['at']}  {p['kind']:<10} {p['what'][:64]}")
    if s["finding"]:
        print(f"\n  ★ FINDING: {s['finding']['title']}")
        print(f"    {s['finding']['why']}")
    print(f"\n  {s['note']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
