#!/usr/bin/env python3
"""drain_handoffs.py — make `queued` mean something.

★★★ THE PROBLEM THIS CLOSES. The board's Send-to picker marks most destinations `queued`: "an agent
    delivers this on its next turn". That sentence was TRUE about intent and UNVERIFIED in practice —
    nothing had ever read the queue. A promise nobody has kept once is indistinguishable from a lie
    told politely, and it was sitting on a surface built to stop exactly that.

★★ WHY THE PANEL CANNOT JUST DELIVER. Reaching another Claude Code seat requires the cross-session
    message tool, which lives in an AGENT's turn, not in an HTTP handler. Spawning a Hermes profile or
    a new session likewise. So the honest architecture is: the panel QUEUES and says so; an agent
    DRAINS and records who delivered and when. This file is the drain half, and the session hook now
    tells every agent that work is waiting, so the queue is READ rather than merely written.

★ NOTHING HERE MARKS ITSELF DELIVERED. The agent marks a row delivered AFTER it has actually sent it —
  `--delivered <n> "<how>"`. A drainer that ticked its own boxes would recreate the defect it exists to
  close, one layer down.
"""
from __future__ import annotations
import json, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "handoff_queue.jsonl"


def _rows() -> list:
    if not QUEUE.exists():
        return []
    out = []
    for n, line in enumerate(QUEUE.read_text(errors="replace").splitlines()):
        if line.strip():
            try:
                r = json.loads(line); r["_n"] = n
                out.append(r)
            except Exception:
                pass
    return out


def pending() -> list:
    return [r for r in _rows() if not r.get("delivered")]


def mark(n: int, how: str, by: str = "an agent") -> dict:
    rows = _rows()
    hit = next((r for r in rows if r.get("_n") == n), None)
    if not hit:
        return {"ok": False, "error": f"no queue row {n}"}
    if hit.get("delivered"):
        return {"ok": False, "error": f"row {n} is already marked delivered — refusing to double-count"}
    hit["delivered"] = True
    hit["delivered_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    hit["delivered_by"] = by
    hit["delivered_how"] = how
    QUEUE.write_text("\n".join(json.dumps({k: v for k, v in r.items() if k != "_n"})
                               for r in rows) + "\n")
    return {"ok": True, "row": n, "target": hit.get("target")}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is a row that reads DELIVERED without anyone having
    delivered it — so: marking must refuse an unknown row, and refuse to re-mark one already delivered
    (which is how a single send becomes two confirmations)."""
    fails = []
    if mark(10 ** 9, "nowhere").get("ok"):
        fails.append("marked a queue row that does not exist as delivered")
    rows = _rows()
    done = next((r for r in rows if r.get("delivered")), None)
    if done and mark(done["_n"], "again").get("ok"):
        fails.append("re-marked an already-delivered row — one send would show as two confirmations")
    p = pending()
    for r in p:
        if r.get("delivered_at"):
            fails.append("a row counted as PENDING carries a delivery timestamp")
    for f in fails:
        print(f"  ✗ {f}")
    print(f"  ✓ drain_handoffs self_test: {len(p)} pending; refuses unknown rows and double-marking"
          if not fails else "  ✗ drain_handoffs self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--delivered" and len(a) >= 2:
        try:
            n = int(a[1])
        except ValueError:
            print("usage: drain_handoffs.py --delivered <row> \"how it was delivered\"", file=sys.stderr)
            return 2
        r = mark(n, " ".join(a[2:]) or "not stated", by="agent")
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 2
    p = pending()
    if not p:
        print("  ✓ no handoffs waiting. The queue is empty — which is different from unread: this "
              "command is how it gets read.")
        return 0
    print(f"\n  ★ {len(p)} HANDOFF(S) WAITING FOR AN AGENT TO DELIVER")
    print("    Deliver each one, THEN mark it: drain_handoffs.py --delivered <row> \"how\"")
    for r in p:
        print(f"\n    [row {r['_n']}]  → {r.get('target_label') or r.get('target')}   ({r.get('kind','note')})")
        print(f"        queued {str(r.get('ts'))[:16]}   about item: {r.get('item') or '(none)'}")
        print(f"        {str(r.get('text',''))[:300]}")
    return 1        # ★ non-zero while work is undelivered, so a caller can branch on it


if __name__ == "__main__":
    sys.exit(main())
