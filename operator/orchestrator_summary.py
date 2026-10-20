#!/usr/bin/env python3
"""orchestrator_summary.py — a checkbox .md for the orchestrator, cut the way he asks for it.

★★★ THE CAVEAT THAT MATTERS MOST, and it is his: a row on this board can be in THREE states that this
    file cannot tell apart —
      · genuinely still needing doing,
      · already done and never closed (this board has closed ZERO rows in seven days while adding 34),
      · IN ACTIVE LIVE WORK right now by another seat.
    A checkbox list that does not say so invites two seats to do the same job, or the operator to rule
    on something that was finished ten days ago. **Board item 1 was exactly that** — "the payload was
    never applied" sat at the top for ten days after the payload was built, deployed and firing.
    ★ So every cut carries the caveat, and it is not a footnote — it is the first thing after the title.

★★ THE CUTS ARE ENUMERATED, NOT FREE TEXT. "Summarise the board" produces a summary of whatever the
    summariser happened to notice. Each cut below names exactly which rows it selects and which it
    leaves out, so a short list means something.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "summaries"
PANEL = "http://127.0.0.1:7842"


def _board() -> dict:
    with urlopen(f"{PANEL}/operator/board.json", timeout=180) as r:
        return json.load(r)


def _age(i):
    fs = i.get("first_seen")
    if not fs:
        return None
    try:
        return int((time.time() - time.mktime(time.strptime(fs, "%Y-%m-%d"))) / 86400)
    except Exception:
        return None


def cuts(d: dict) -> dict:
    """Every cut this can produce, built from the live board so the menu can never offer an empty
    one that looks like a clean result."""
    items = d.get("items", [])
    openi = [i for i in items if i.get("state") == "open"]
    out = {
        "all_open": {"label": "Everything still needing doing",
                     "rows": openi,
                     "selects": "every OPEN row, hand-written and derived"},
        "waiting_on_him": {"label": "Only what is waiting on HIM",
                           "rows": [i for i in openi if (i.get("source") or "hand") == "hand"],
                           "selects": "hand-written rows — the ones only he can rule on"},
        "waiting_on_us": {"label": "Only what an agent can act on",
                          "rows": [i for i in openi if (i.get("source") or "hand") != "hand"],
                          "selects": "derived rows — these clear when their source stops reporting them"},
        "blocking": {"label": "Blocking only",
                     "rows": [i for i in openi if i.get("sev") == "stop"],
                     "selects": "severity = stop"},
        "aged": {"label": "Open more than 7 days",
                 "rows": [i for i in openi if (_age(i) or 0) >= 7],
                 "selects": "first_seen at least 7 days ago; rows with no date are EXCLUDED and counted"},
        "ordering": {"label": "Rows with an ordering constraint",
                     "rows": [i for i in openi if i.get("must_follow")],
                     "selects": "rows that must not be done before another — doing these out of order "
                                "closes a door permanently"},
        "unverified": {"label": "Asks recorded but never verified by him",
                       "rows": [i for i in openi if i.get("source") == "requirements"],
                       "selects": "the register — 19 asks, and a machine has pressed nearly all of them"},
        "done": {"label": "Already done",
                 "rows": [i for i in items if i.get("state") == "done"],
                 "selects": "state = done in the store"},
        "deferred": {"label": "Deferred — he said not now",
                     "rows": [i for i in items if i.get("state") == "later"],
                     "selects": "state = later"},
    }
    # per-lane and per-source cuts, only where they would select something
    for lane in sorted({i.get("lane") for i in openi if i.get("lane") and i["lane"] != "unknown"}):
        out[f"lane:{lane}"] = {"label": f"Lane — {lane}",
                               "rows": [i for i in openi if i.get("lane") == lane],
                               "selects": f"open rows in the {lane} lane"}
    for src in sorted({(i.get("source") or "hand") for i in openi}):
        out[f"source:{src}"] = {"label": f"Source — {src}",
                                "rows": [i for i in openi if (i.get("source") or "hand") == src],
                                "selects": f"open rows raised by {src}"}
    return out


CAVEAT = """> ### ★ READ THIS BEFORE TICKING ANYTHING
>
> **A row here can be in three states this file cannot tell apart:**
>
> 1. **Still needing doing** — the row is true and the work is outstanding.
> 2. **Already done and never closed.** This board has closed **zero** rows in the last seven days
>    while adding **34**. Board item 1 said *"the payload was never applied"* and sat at the top for
>    **ten days** after the payload had been built, deployed, and was firing at runtime.
> 3. **In active live work right now** by another seat. Six lanes relay through one operator and
>    nothing here reserves a row.
>
> **So: recon before acting.** If a row looks done, verify it against the running system rather than
> the row. If it looks unclaimed, say so in the lane before starting — two seats doing one job is the
> cheapest mistake on this list to avoid and the most annoying to discover afterwards."""


def render(cut_keys: list, d: dict | None = None) -> str:
    d = d or _board()
    c = cuts(d)
    bad = [k for k in cut_keys if k not in c]
    if bad:
        raise ValueError(f"unknown cut(s): {bad}. Available: {sorted(c)}")
    stamp = time.strftime("%Y-%m-%d %H:%M")
    openi = [i for i in d.get("items", []) if i.get("state") == "open"]
    L = [f"# OPERATOR BOARD — summary for the orchestrator",
         f"**Generated {stamp}** · board **{len(openi)} open** of {len(d.get('items', []))} · "
         f"cuts: {', '.join(cut_keys)}", "", CAVEAT, ""]
    for k in cut_keys:
        cut = c[k]
        rows = cut["rows"]
        L += [f"## {cut['label']}  ({len(rows)})", "",
              f"*Selects: {cut['selects']}.*", ""]
        if not rows:
            L += ["*Nothing in this cut. That is a real answer — the selection ran and matched "
                  "nothing, which is different from the cut being unavailable.*", ""]
            continue
        for i in sorted(rows, key=lambda x: (x.get("priority", 3), -(_age(x) or 0))):
            a = _age(i)
            age = f"{a}d" if a is not None else "undated"
            lane = f" · {i['lane']}" if i.get("lane") and i["lane"] != "unknown" else ""
            L.append(f"- [ ] **{i.get('title','')}**  ")
            L.append(f"      `{i['id']}` · P{i.get('priority', '?')} · {i.get('source', 'hand')}"
                     f"{lane} · {age}  ")
            one = (i.get("one") or "").replace("\n", " ")[:230]
            if one:
                L.append(f"      {one}  ")
            if i.get("must_follow"):
                L.append(f"      ⚠ **must follow `{i['must_follow']}`** — doing this first closes a "
                         f"door permanently  ")
        L.append("")
    L += ["---", "",
          "**Ticking a box here changes nothing on the board.** This is a working list; rulings are "
          "made on the board itself, and only the operator can close a row."]
    return "\n".join(L) + "\n"


def write(cut_keys: list, name: str = "") -> dict:
    txt = render(cut_keys)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    slug = name or ("-".join(k.replace(":", "_") for k in cut_keys)[:60] or "summary")
    f = OUTDIR / f"BOARD_SUMMARY_{slug}_{time.strftime('%Y%m%d_%H%M')}.md"
    f.write_text(txt)
    return {"ok": True, "file": str(f), "bytes": len(txt), "cuts": cut_keys}


def menu() -> dict:
    d = _board()
    c = cuts(d)
    return {"cuts": [{"key": k, "label": v["label"], "count": len(v["rows"]),
                      "selects": v["selects"]} for k, v in c.items()],
            "caveat": CAVEAT,
            "note": "Every cut shows its live count, so a cut that would select nothing says so "
                    "before you pick it rather than after."}


def self_test() -> int:
    fails = []
    fake = {"items": [
        {"id": "a", "state": "open", "source": "hand", "sev": "stop", "priority": 1,
         "title": "A", "one": "x", "first_seen": "2026-08-01", "lane": "trading"},
        {"id": "b", "state": "open", "source": "invariants", "sev": "hold", "priority": 2,
         "title": "B", "one": "y"},
        {"id": "c", "state": "done", "source": "hand", "sev": "info", "priority": 3, "title": "C"},
        {"id": "d", "state": "open", "source": "hand", "sev": "hold", "priority": 2, "title": "D",
         "must_follow": "a"},
    ]}
    c = cuts(fake)
    if len(c["all_open"]["rows"]) != 3:
        fails.append(f"all_open selected {len(c['all_open']['rows'])}, expected 3")
    if len(c["waiting_on_him"]["rows"]) != 2:
        fails.append("waiting_on_him did not select only hand rows")
    if len(c["done"]["rows"]) != 1:
        fails.append("done did not select the done row")
    if "lane:trading" not in c:
        fails.append("a lane present on the board produced no cut")
    if len(c["ordering"]["rows"]) != 1:
        fails.append("the ordering cut missed a must_follow row")
    txt = render(["all_open", "done"], fake)
    # ★ the controls that matter: the caveat must be present and must name all three states
    for phrase in ("Still needing doing", "Already done and never closed", "In active live work"):
        if phrase not in txt:
            fails.append(f"the three-state caveat is missing '{phrase}'")
    if "- [ ]" not in txt:
        fails.append("no checkboxes rendered")
    if "Ticking a box here changes nothing" not in txt:
        fails.append("the file does not say that ticking changes nothing on the board")
    try:
        render(["nonsense"], fake); fails.append("accepted an unknown cut")
    except ValueError:
        pass
    # an empty cut must SAY it is empty rather than render as a clean short list
    t2 = render(["deferred"], fake)
    if "That is a real answer" not in t2:
        fails.append("an empty cut did not announce itself as empty")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  7 controls pass: cuts select correctly · lane cuts appear from live data · the "
              "three-state caveat is present and complete · checkboxes render · it says ticking "
              "changes nothing · unknown cut refused · an empty cut announces itself")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if "--menu" in a:
        print(json.dumps(menu(), indent=2)); return 0
    keys = [x for x in a if not x.startswith("-")] or ["all_open"]
    r = write(keys)
    print(f"  wrote {r['file']}  ({r['bytes']:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
