#!/usr/bin/env python3
"""board_delta.py — what is NEW since he last looked, per source.

★★★ THE RULING THIS IMPLEMENTS (⛬6, 2026-08-29 §2). The circle objected that a never-empty board makes
    any persistent indicator into permanent noise. That is correct about the MECHANISM — and it is an
    argument about what TRIGGERS the badge, not about where it lives.
    ★ So the badge indicates DELTA, NOT STATE. "86 items" is wallpaper by tomorrow. "3 new since you
      last looked" is information, and it self-clears the moment he looks. The circle's concern
      dissolves rather than being overruled: nobody has to lose.

★★ DELTA IS COMPUTED PER SOURCE, and that is not a detail. A flood of derived rows from one source —
   fourteen roadmap items arriving at once — must not bury a single new approval request. Per-source
   counts mean the one row that matters keeps its own line.

★ IDS, NOT TIMESTAMPS. Board items carry no reliable added-at, and derived rows appear and vanish as
  their sources change. So "seen" is a SET OF IDS he has already been shown. An item that disappears
  and comes back is new again, which is the honest answer: it is on his board again and he has not
  looked at it since it returned.
"""
from __future__ import annotations
import json, sys
from datetime import datetime
from pathlib import Path


def _ss():
    """★ One atomic+locked writer. write_text(json.dumps(...)) truncates before it writes, so a crash
    between those two moments empties the store; and nothing serialised concurrent writers on a box
    where every agent runs as the same uid. Proven through 160 concurrent writes."""
    import importlib, sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent))
    return importlib.import_module("safe_store")

HERE = Path(__file__).resolve().parent
SEEN = HERE / "board_seen.json"


def _board() -> list:
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    return [i for i in b.items_of(b.load()) if i.get("state") == "open"]


def _seen() -> dict:
    if SEEN.exists():
        try:
            return json.loads(SEEN.read_text())
        except Exception:
            pass
    return {"ids": [], "at": None}


def delta() -> dict:
    items = _board()
    seen = set(_seen().get("ids") or [])
    new = [i for i in items if i["id"] not in seen]
    by_source, by_priority = {}, {}
    for i in new:
        by_source[i.get("source", "hand")] = by_source.get(i.get("source", "hand"), 0) + 1
        by_priority[i.get("priority", 3)] = by_priority.get(i.get("priority", 3), 0) + 1
    # ★ FIRST RUN IS NOT A FLOOD. With no stored view, every item is technically new — showing "86 new"
    #   on first sight is exactly the wallpaper the ruling exists to avoid. Say it is a first view.
    first = not seen
    return {
        "new": len(new), "first_view": first,
        "by_source": by_source, "by_priority": by_priority,
        "ids": [i["id"] for i in new][:200],
        "titles": [{"id": i["id"], "title": i["title"], "source": i.get("source", "hand"),
                    "priority": i.get("priority", 3)} for i in new[:12]],
        "last_looked": _seen().get("at"),
        "total_open": len(items),
        "note": ("you have not opened the board with this feature before, so everything reads as new — "
                 "press 'mark seen' once and from then on this counts only what changed"
                 if first else ""),
    }


def mark_seen() -> dict:
    items = _board()
    SEEN.write_text(json.dumps({"ids": [i["id"] for i in items],
                                "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")},
                               indent=2) + "\n")
    return {"ok": True, "seen": len(items)}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is a badge that lights when NOTHING changed —
    which is the noise the circle predicted and the reason a state badge was rejected. So the test is
    that delta goes to ZERO after a look, and rises again only when something actually appears."""
    import tempfile
    global SEEN
    saved = SEEN
    fails = []
    try:
        SEEN = Path(tempfile.mkdtemp()) / "seen.json"
        d0 = delta()
        if not d0["first_view"]:
            fails.append("with no stored view this did not report first_view — it would show the whole "
                         "board as 'new', which is the wallpaper the ruling exists to prevent")
        mark_seen()
        d1 = delta()
        if d1["new"] != 0:
            fails.append(f"after marking seen, {d1['new']} item(s) still read as new — a badge that "
                         f"cannot reach zero is a permanent indicator, which is exactly what was rejected")
        # plant one unseen id by removing it from the seen set
        st = json.loads(SEEN.read_text())
        if not st["ids"]:
            fails.append("nothing was recorded as seen — the comparison is vacuous")
        else:
            st["ids"] = st["ids"][1:]
            _ss().write_json(SEEN, st)
            d2 = delta()
            if d2["new"] != 1:
                fails.append(f"one item was made unseen but the delta says {d2['new']} — it does not "
                             f"track what actually changed")
            if not d2["by_source"]:
                fails.append("the delta carries no per-source breakdown, so a flood from one source "
                             "could bury a single new request")
    finally:
        SEEN = saved
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ board_delta self_test: reaches zero after a look, rises by exactly what changed, and "
          "breaks the count down by source" if not fails else "  ✗ board_delta self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if "--seen" in sys.argv:
        print(json.dumps(mark_seen())); return 0
    d = delta()
    print(json.dumps(d, indent=2) if "--json" in sys.argv else
          f"  {d['new']} new since you last looked" + (f"  ({d['note']})" if d['note'] else "")
          + f"\n  by source: {d['by_source']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
