#!/usr/bin/env python3
"""him_verify.py — the ONE place an operator verdict lands, and the only source of `verified-by-him`.

★★★ WHY THIS EXISTS AND WHY IT HAD TO EXIST FIRST.
    The requirements register reads `verified-by-machine 10 · verified-by-him 0`. He is about to press
    these things for the first time, and he is the only source of the second number that will ever
    exist. A verdict delivered in chat and not recorded is a false clear waiting to happen — the exact
    class this entire day has been about. So the capture path is built BEFORE the test, not after it.

★★ `partial` IS FIRST-CLASS AND CARRIES A REQUIRED NOTE.
    He said the board "seems to kind of work". A partial verdict is more informative than either pole
    and there was nowhere to put it — so it would have been rounded to yes (optimistic, and a false
    clear) or to no (pessimistic, and it discards what does work). `partial` without a note is REFUSED,
    because "kind of" with no detail is the one answer that helps nobody.

★ EVERY VERDICT RECORDS WHICH BUILD IT WAS TAKEN AGAINST (L40).
    CC's first run of the voice controls passed against a panel started five hours before the edits
    under test. A verdict against stale code is worse than no verdict: it closes an ask that was never
    actually exercised. So each row stamps the serving process's start time and the file mtime, and
    says plainly whether the build was current when he ruled.
"""
from __future__ import annotations
import json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "operator_verdicts.json"
PANEL = HERE.parent / "panel_server.py"
HTML = HERE.parent / "aionui-stack-panel.html"

PATHS = [
    ("P1", "Open the board",
     "Launch it the way you normally would — desktop entry or launcher — and see the board appear.",
     "It opens, and the number at the top matches what you expect."),
    ("P2", "Expand an item and read it",
     "Click any row. Read WHAT THIS IS, then open 'Read more' if the row has one.",
     "The row explains itself without you needing to ask an agent what it means."),
    ("P3", "Have an item read aloud",
     "Press Speak on an item. Try the speed control, and More detail / Less detail.",
     "You hear it, in a voice you chose, at a speed you can follow."),
    ("P4", "Write a note and send it",
     "Type into a row's note box. Then try Save, and Save & tell the agent.",
     "The note sticks, and the agent version actually reaches someone."),
    ("P5", "Guided walkthrough",
     "Press Start guided walkthrough, and separately try 'Walk from here' on one row.",
     "It moves you through items one at a time and you can leave whenever."),
    ("P6", "Ask for a picture of one",
     "Press Make a visual on an item that confuses you.",
     "You get something that makes the item clearer than the words did."),
    ("P7", "Sort, search and filter",
     "Use the search box, the priority and source controls, and Show settled.",
     "You can get to the one item you care about without scrolling past the others."),
]
VERDICTS = ("yes", "partial", "no", "untested")


def build_stamp() -> dict:
    """★ MEASURED, NOT ASSUMED. `pgrep -f panel_server` matches the shell wrapper's own command line,
    so the port's owner is the only reliable identification (L40)."""
    st = {"checked_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        out = subprocess.run(["ss", "-ltnpH", "sport = :7842"], capture_output=True, timeout=5).stdout.decode()
        pid = next((w.split("=")[1] for w in out.replace(",", " ").split() if w.startswith("pid=")), None)
        st["panel_pid"] = pid
        if pid:
            started = subprocess.run(["ps", "-o", "lstart=", "-p", pid],
                                     capture_output=True, timeout=5).stdout.decode().strip()
            st["panel_started"] = started
            st["panel_started_epoch"] = int(subprocess.run(["date", "+%s", "-d", started],
                                            capture_output=True, timeout=5).stdout.decode().strip() or 0)
    except Exception as e:
        st["panel_error"] = f"{e.__class__.__name__}"
    for label, f in (("panel_py", PANEL), ("panel_html", HTML)):
        try:
            st[label + "_mtime_epoch"] = int(f.stat().st_mtime)
            st[label + "_mtime"] = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            st[label + "_mtime_epoch"] = 0
    newest = max(st.get("panel_py_mtime_epoch", 0), st.get("panel_html_mtime_epoch", 0))
    started = st.get("panel_started_epoch", 0)
    if not started or not newest:
        st["build_current"] = None
        st["build_note"] = "could not determine whether the running panel includes the latest edits"
    elif started >= newest:
        st["build_current"] = True
        st["build_note"] = "the running panel was started AFTER the last edit — this verdict is against current code"
    else:
        st["build_current"] = False
        st["build_note"] = ("★ THE RUNNING PANEL PREDATES THE LAST EDIT. A verdict taken now measures "
                            "STALE CODE and must not be treated as closing the ask. Restart the panel "
                            "(POST /services/restart-panel) and rule again.")
    return st


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"doc": "Operator verdicts. The ONLY source of `verified-by-him`. Written by him, never for him.",
            "verdicts": {}}


def save(d: dict) -> None:
    STORE.write_text(json.dumps(d, indent=2) + "\n")


def record(path_id: str, verdict: str, note: str = "", actor: str = "operator") -> dict:
    ids = [p[0] for p in PATHS]
    if path_id not in ids:
        return {"ok": False, "error": f"unknown path {path_id!r}; expected one of {ids}"}
    if verdict not in VERDICTS:
        return {"ok": False, "error": f"verdict must be one of {list(VERDICTS)}"}
    # ★ THE ONE REFUSAL. "Kind of works" with no detail is the least useful answer there is, and it is
    #   the one most likely to be given, because it is the honest first reaction. Requiring the note is
    #   what turns it from a shrug into a finding.
    if verdict == "partial" and not note.strip():
        return {"ok": False, "error": "a PARTIAL verdict needs one line about what did not go as expected — "
                                      "that sentence is the whole value of 'partial' over 'yes' or 'no'"}
    d = load()
    d["verdicts"][path_id] = {
        "verdict": verdict, "note": note.strip(), "by": actor,
        "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "build": build_stamp(),
    }
    save(d)
    return {"ok": True, "path": path_id, "verdict": verdict,
            "build_current": d["verdicts"][path_id]["build"].get("build_current")}


def status() -> dict:
    d = load()
    v = d.get("verdicts", {})
    rows = []
    for pid, title, how, looks_like in PATHS:
        r = v.get(pid) or {}
        rows.append({"id": pid, "title": title, "how": how, "looks_like": looks_like,
                     "verdict": r.get("verdict", "untested"), "note": r.get("note", ""),
                     "at": r.get("at"), "build_current": (r.get("build") or {}).get("build_current"),
                     "build_note": (r.get("build") or {}).get("build_note", "")})
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in VERDICTS}
    # ★ A verdict taken against stale code is NOT a verdict. Counted separately rather than folded in.
    counts["against_stale_build"] = sum(1 for r in rows if r["build_current"] is False)
    return {"paths": rows, "counts": counts, "build": build_stamp()}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The capture path's job is to refuse the answers that would become false
    clears. So the test is what it REFUSES, not what it stores."""
    fails = []
    if record("P1", "partial", "").get("ok") is not False:
        fails.append("a PARTIAL verdict with no note was accepted — 'kind of works' with no detail is "
                     "exactly the answer this field exists to capture, and storing it bare loses it")
    if record("P9", "yes").get("ok") is not False:
        fails.append("an unknown path id was accepted")
    if record("P1", "probably").get("ok") is not False:
        fails.append("an unknown verdict was accepted")
    st = build_stamp()
    if "build_current" not in st:
        fails.append("the build stamp does not say whether the running code is current")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ him_verify self_test: refuses a bare partial, unknown paths and unknown verdicts; "
          "stamps the build" if not fails else "  ✗ him_verify self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--json":
        print(json.dumps(status(), indent=2)); return 0
    if a and a[0] == "--set" and len(a) >= 3:
        r = record(a[1], a[2], " ".join(a[3:]))
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 2
    s = status()
    b = s["build"]
    print("\n  YOUR TEST PASS — the only source of `verified-by-him`")
    print(f"  build: {b.get('build_note','')}")
    for r in s["paths"]:
        mark = {"yes": "✓", "partial": "~", "no": "✗", "untested": "·"}[r["verdict"]]
        print(f"    {mark} {r['id']}  {r['title']:32s} {r['verdict']}")
        if r["note"]:
            print(f"        “{r['note']}”")
    c = s["counts"]
    print(f"\n  yes {c['yes']} · partial {c['partial']} · no {c['no']} · untested {c['untested']}"
          + (f"  ★ {c['against_stale_build']} taken against a STALE build" if c["against_stale_build"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
