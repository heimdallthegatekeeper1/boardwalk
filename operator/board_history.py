#!/usr/bin/env python3
"""★ Read the board's own history for ONE question: has anything been systematically NARROWED?

★ WHY (trading lane, 2026-08-28): a board can be defanged one defensible correction at a time. Every
  individual narrowing looks like rigour — theirs did, and theirs WAS correct. A single narrowing is
  housekeeping; a PATTERN of them is a board being disarmed, and nothing in a journal of reasons
  distinguishes the two, because both produce a narrower row with a plausible explanation.

★★ THE FIELD WITHOUT THIS READER WOULD BE DECORATION. Their proposal was the `direction` field; a record
  nothing reads does not exist to the decision it should govern (L7), which is the failure the board itself
  was built to end. So the field ships with the thing that asks the question.

★★★ IT REPORTS, IT DOES NOT JUDGE. No threshold is asserted here — "3 narrowings is too many" would be an
  invented number wearing the authority of a check. It shows the shape and names who did what; a human
  decides whether that shape is disarmament or housekeeping.

USE:  board_history.py            # the shape
      board_history.py --full     # every row, with reasons
"""
from __future__ import annotations
import json, sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE / "blockers_audit.jsonl"


def main() -> int:
    if not AUDIT.exists():
        print("  no board history yet — nothing has been changed through a journalled path.")
        return 0
    rows = []
    for line in AUDIT.read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    full = "--full" in sys.argv

    text_edits = [r for r in rows if r.get("field") in ("one", "title", "blocks")]
    state_edits = [r for r in rows if r.get("field") == "state"]
    # ★ ACCOUNT FOR EVERY ROW YOU COUNT. The header printed "19 journalled change(s)" over a breakdown
    #   summing to 15, because `--add` now writes field="item" and nothing classified it. A summary that
    #   silently drops rows it has already counted is the same false-complete as every subset count on
    #   this board today — and this file IS the register that makes narrowing visible, so a gap here is
    #   load-bearing rather than cosmetic.
    added = [r for r in rows if r.get("field") == "item"]
    other = [r for r in rows
             if r.get("field") not in ("one", "title", "blocks", "state", "item")]
    undeclared = [r for r in text_edits if not r.get("direction")]
    dirs = Counter(r.get("direction", "(undeclared)") for r in text_edits)

    print(f"\n★ BOARD HISTORY — {len(rows)} journalled change(s)")
    parts = [f"{len(state_edits)} state", f"{len(text_edits)} wording", f"{len(added)} added"]
    if other:
        parts.append(f"{len(other)} other")
    print("  " + " · ".join(p + " change(s)" if i == 0 else p for i, p in enumerate(parts)))
    accounted = len(state_edits) + len(text_edits) + len(added) + len(other)
    if accounted != len(rows):
        print(f"  ⚠ {len(rows) - accounted} row(s) counted but NOT classified — this breakdown is "
              f"incomplete and should not be read as the whole register")
    print("\n  WORDING CHANGES BY DECLARED DIRECTION")
    for k in ("narrowed", "widened", "neutral", "(undeclared)"):
        if dirs.get(k):
            print(f"    {k:14s} {dirs[k]}")
    if undeclared:
        print(f"  ⚠ {len(undeclared)} wording change(s) carry NO declared direction — they predate the field "
              f"or bypassed board_reword.py; they cannot be read for shape.")

    narrowed = [r for r in text_edits if r.get("direction") == "narrowed"]
    if narrowed:
        print(f"\n  ★ THE NARROWINGS ({len(narrowed)}) — the ones worth a human eye")
        per_item = defaultdict(list)
        for r in narrowed:
            per_item[r.get("id", "?")].append(r)
        for item_id, rs in sorted(per_item.items(), key=lambda kv: -len(kv[1])):
            flag = "  ← repeatedly narrowed" if len(rs) > 1 else ""
            print(f"    {item_id}  ×{len(rs)}{flag}")
            for r in rs:
                print(f"       {r.get('ts','?')}  by {r.get('by','?')}  ·  at the request of "
                      f"{r.get('requested_by','(nobody named)')}")
                if not r.get("requested_by"):
                    print("       ⚠ NO REQUESTER NAMED — a rewording nobody asked for")
    else:
        print("\n  no narrowings recorded.")

    if full:
        print("\n  EVERY ROW")
        for r in rows:
            print(f"    {r.get('ts','?')}  {r.get('id','?'):22s} {r.get('field','?'):6s} "
                  f"{r.get('direction','-'):10s} by {r.get('by','?')[:40]}")
            if r.get("why"):
                print(f"        why: {r['why'][:160]}")

    print("\n  ★ This reports the shape; it does not judge it. One narrowing is housekeeping. A pattern on")
    print("    one item, or any narrowing with no requester named, is the thing to look at.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
