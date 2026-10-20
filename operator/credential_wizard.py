#!/usr/bin/env python3
"""credential_wizard.py — a guided run through the logins, in bursts he chooses.

★★★ WHAT THIS IS AND WHY IT IS AN OPTION, NOT THE PATH. The board already shows every service at once
    with its own controls, and that is right for someone who wants to fix one thing. It is wrong for
    someone sitting down to get through a stack of logins, because a wall of eleven services with four
    buttons each is a wall. This offers the same actions in SMALL CHUNKS he sizes himself — a burst of
    one when he has two minutes, ten when he has an afternoon. Neither view is primary; they are two
    doors into the same store, and there is exactly one store, so they cannot disagree.

★★ THE AGENT'S CONTRIBUTION IS THE ADVICE, NOT THE SEQUENCING. Anyone can list eleven services. What an
    operator cannot get faster than we can is: which scope is the narrowest that works, which pitfall
    bites here, what "unverified app" means on that consent screen, that a Notion token sees nothing
    until pages are shared with it, that the npm package calling itself a Linear CLI is hijacked. That
    is what `credential_advice.json` holds, and it is the reason this is worth building rather than
    just printing a checklist.

★ NOTHING HERE DECIDES FOR HIM. The run can be abandoned at any step, resized mid-way, reordered, or
  ignored entirely in favour of the board. Every step offers the same three settlements the board does —
  the probe passed · he says it is done · not now — because a wizard that only accepts one kind of
  success is a wizard that tells him his judgement does not count.
"""
from __future__ import annotations
import json, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADVICE = HERE / "credential_advice.json"
RUN = HERE / "credential_run.json"

ORDERS = {
    "quickest": "quickest first — build momentum; the cheap wins are real wins",
    "unlocks": "biggest unlock first — most capability per login",
    "listed": "as listed — no reordering",
}


def _advice() -> dict:
    try:
        return json.loads(ADVICE.read_text()).get("advice", {})
    except Exception:
        return {}


def _states() -> dict:
    """Current probe state per service, plus anything he has already settled."""
    sys.path.insert(0, str(HERE))
    import service_auth_check as sac, service_actions as sa, importlib
    importlib.reload(sac); importlib.reload(sa)
    d = sac.check()
    settled = sa.decisions()
    out = {}
    for r in d["services"]:
        out[r["id"]] = {"label": r["label"], "state": r["state"], "detail": r.get("detail", ""),
                        "how": r.get("how", ""), "settled": settled.get(r["id"])}
    return out


def steps(order: str = "quickest") -> list:
    """Everything still worth doing, in the order he asked for.
    ★ A service already `works`, or already settled by him, is NOT a step. A wizard that walks you
      through things you have finished is how a guided flow becomes something people skip."""
    adv, st = _advice(), _states()
    rows = []
    for sid, a in adv.items():
        s = st.get(sid)
        if not s:
            continue
        if s["state"] == "works" or s.get("settled"):
            continue
        rows.append({"service": sid, "label": s["label"], "state": s["state"],
                     "detail": s["detail"], "how": s["how"], **a})
    if order == "quickest":
        rows.sort(key=lambda r: (r.get("minutes", 99), r["label"]))
    elif order == "unlocks":
        rows.sort(key=lambda r: (-len(r.get("unlocks", "")), r.get("minutes", 99)))
    return rows


def _run() -> dict:
    if RUN.exists():
        try:
            return json.loads(RUN.read_text())
        except Exception:
            pass
    return {}


def plan(batch: int = 3, order: str = "quickest") -> dict:
    """★ The BURST. `batch` is his, 1..N — one when he has two minutes, ten when he has an afternoon.
    The estimate is stated up front because the honest question before starting is 'what will this
    cost me', and a guided run that will not answer that is asking for blind commitment."""
    batch = max(1, min(int(batch or 1), 50))
    all_steps = steps(order)
    r = _run()
    done = set(r.get("done", []))
    remaining = [s for s in all_steps if s["service"] not in done]
    burst = remaining[:batch]
    return {
        "order": order, "order_says": ORDERS.get(order, ""),
        "batch": batch,
        "burst": burst,
        "remaining_after": max(0, len(remaining) - len(burst)),
        "total_remaining": len(remaining),
        "minutes_this_burst": sum(s.get("minutes", 0) for s in burst),
        "minutes_all": sum(s.get("minutes", 0) for s in remaining),
        "done_count": len(done),
        "note": ("Nothing here is committed. Close it, resize it, or do one and stop — the board has the "
                 "same actions if you would rather work from there."),
    }


def mark_done(service: str, how: str, note: str = "") -> dict:
    """★ Delegates the SETTLEMENT to service_actions so the wizard and the board write to ONE store.
    A wizard with its own completion record is a second source of truth about the same fact, and this
    house has twice paid for two surfaces disagreeing."""
    sys.path.insert(0, str(HERE))
    import service_actions as sa, importlib
    importlib.reload(sa)
    res = sa.settle(service, how, note)
    if not res.get("ok"):
        return res
    r = _run()
    r.setdefault("done", [])
    if service not in r["done"]:
        r["done"].append(service)
    r["last"] = {"service": service, "how": how,
                 "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}
    RUN.write_text(json.dumps(r, indent=2) + "\n")
    return {"ok": True, "service": service, "how": how, "done_count": len(r["done"])}


def reset() -> dict:
    """★ Abandoning a run must not un-settle anything. The run file remembers PROGRESS THROUGH A LIST;
    the settlements live in service_actions and survive. Clearing one must never clear the other."""
    if RUN.exists():
        RUN.unlink()
    return {"ok": True, "note": "run position cleared. Nothing you settled was undone — those live in "
                                "the services store, not here."}


def self_test() -> int:
    """★ NEGATIVE CONTROL. Three failures matter: walking him through something already finished,
    a burst that ignores the size he asked for, and a reset that silently un-settles his decisions."""
    fails = []
    p = plan(2)
    if len(p["burst"]) > 2:
        fails.append("the burst ignored the batch size he asked for")
    if any(s["state"] == "works" for s in p["burst"]):
        fails.append("a service that already WORKS was put in the burst — a guided run that walks you "
                     "through finished work is one people learn to skip")
    if p["minutes_this_burst"] > p["minutes_all"] and p["total_remaining"] > len(p["burst"]):
        fails.append("the burst estimate exceeds the total estimate")
    big = plan(50)
    if big["remaining_after"] != 0 and big["total_remaining"] > 50:
        fails.append("a large batch did not consume the list")
    # a reset must not touch settlements
    sys.path.insert(0, str(HERE))
    import service_actions as sa, importlib
    importlib.reload(sa)
    before = dict(sa.decisions())
    reset()
    importlib.reload(sa)
    after = dict(sa.decisions())
    if set(before) != set(after):
        fails.append("★ reset() changed what he had SETTLED — abandoning a run must never undo his "
                     "decisions")
    for f in fails:
        print(f"  ✗ {f}")
    print(f"  ✓ credential_wizard self_test: honours the batch size, skips finished services, and a "
          f"reset leaves settlements untouched" if not fails else "  ✗ credential_wizard self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--reset":
        print(json.dumps(reset(), indent=2)); return 0
    if a and a[0] == "--done" and len(a) >= 3:
        print(json.dumps(mark_done(a[1], a[2], " ".join(a[3:])), indent=2)); return 0
    batch = 3
    order = "quickest"
    for i, x in enumerate(a):
        if x == "--batch" and i + 1 < len(a):
            batch = int(a[i + 1])
        if x == "--order" and i + 1 < len(a):
            order = a[i + 1]
    p = plan(batch, order)
    if "--json" in a:
        print(json.dumps(p, indent=2)); return 0
    if not p["burst"]:
        print("\n  ✓ nothing left in the guided run — every service is either working or settled.")
        return 0
    print(f"\n  GUIDED RUN — {len(p['burst'])} of {p['total_remaining']} remaining, "
          f"about {p['minutes_this_burst']} minutes")
    print(f"  order: {p['order_says']}")
    for n, s in enumerate(p["burst"], 1):
        print(f"\n  ── {n}. {s['label']}   (~{s['minutes']} min, {s['kind']})")
        print(f"     UNLOCKS   {s['unlocks']}")
        print(f"     ADVICE    {s['advice']}")
        print(f"     SCOPE     {s['scope']}")
        print(f"     WATCH FOR {s['pitfalls']}")
        if s.get("prereq"):
            print(f"     FIRST     {s['prereq']}")
    print(f"\n  after this burst: {p['remaining_after']} left (~{p['minutes_all'] - p['minutes_this_burst']} min)")
    print(f"  {p['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
