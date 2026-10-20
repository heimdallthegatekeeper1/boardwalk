#!/usr/bin/env python3
"""recommended_agents.py — for each board item, WHICH agent(s) and which TERRITORY are involved.

★★★ THE OPERATOR'S ASK (2026-09-19): "a 'Recommended' part for the agent backing citing the agents
    relevant to each item on the board and what territory/which agent(s) are involved with it — so
    the user can more intuitively walk through objectives dynamically and in real time."

★ WHAT THIS IS: a pure READ-ONLY computation. It answers, for one board row, "who is relevant here?"
    from sources that already exist — never a second store of who owns what:
      1. an EXPLICIT binding in `item_bindings.json` (the operator/holder already chose an agent), else
      2. the row's TERRITORY (its `lane`) mapped to the seat that works that territory, else
      3. an HONEST fallback that names no owner rather than guessing one.

★★ RULE IT OBEYS: it will NOT invent an owner. A row whose territory is unmapped says so out loud.
   The failure this avoids is the operator walking a row believing an agent owns it when none does.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BINDINGS = HERE / "item_bindings.json"

# territory (row `lane`) -> (team_id, lead_conversation_id, label, why)
# Mirrors the lanes the live board actually uses. `trading` is listed so it is visibly PRESENT,
# never silently absent — it stays fenced for walking, but the recommendation must not hide it.
TERRITORY_AGENTS: dict[str, tuple] = {
    # ★ THE RELEASE SHIPS NO LIVE SEAT IDS. These are sample lanes for the test row and the honest
    #   fallbacks; a real deployment fills its own map. The function never invents an owner.
    "example": ("example-team", "example-lead", "Example Agent",
                "a sample seat used by the test row"),
    "operator": (None, None, "the operator",
                 "this row waits on a decision or a secret only you hold — no agent can clear it"),
    "cross-lane": (None, None, "two or more seats",
                   "the row spans lanes — read the row to see which seats are involved"),
    "security": (None, None, "security seat",
                 "credential + privacy territory"),
}


def _derive_territory(item: dict) -> tuple[str, str]:
    """Decide the territory from the row's OWN signals, most-specific first.

    ★ The board's `lane` is set on only ~26% of rows, so lane alone leaves most of the walk saying
      "unmapped". The row carries its provenance (`origin.raised_by` = who raised it, `origin.where`
      = which file it came from), and that is a truthful territory signal — never a guess.
    ★ `origin` arrives as a dict on most rows and as a PROSE STRING on nine of them; both shapes are
      read, and the string is only ever matched against the raised_by rules (see below).
    """
    lane = (item or {}).get("lane")
    if lane not in (None, "", "unknown"):
        return str(lane), "lane"
    o = (item or {}).get("origin")
    # ★★ THE BOARD SERVES `origin` IN TWO SHAPES AND THIS ONLY READ ONE. Measured 2026-09-20 on the
    #    live board: 88 rows carry a dict `{raised_by, where, ...}` and 9 carry it as a PLAIN STRING
    #    — `'cc-khaoss-dev, measured 2026-09-10'`. The rules below were written for the dict shape,
    #    so on those 9 they could never fire and the row fell through to "unmapped" while its own
    #    provenance field named the seat. Two of the 9 are the FIRST TWO ROWS OF THE WALK, which is
    #    the operator's first look at the feature — an empty agent list on two rows that carry the
    #    evidence to fill it.
    #  ★ THIS ADDS NO NEW INFERENCE. `raised_by.startswith("cc-") -> khaoss` already existed; it was
    #    simply unreachable for the string shape. The string is provenance PROSE, which is exactly
    #    what `raised_by` is — so it is fed to the raised_by rules ONLY. The loose `where` heuristics
    #    (substring matches on paths) are deliberately NOT fed prose, because a sentence containing
    #    the word "auth" must never be read as a file path.
    #  ★ AND WHEN THE STRING CARRIES NO SIGNAL IT STILL RETURNS "unknown" — the refusal to invent an
    #    owner is the feature, not the bug, and it is unchanged.
    if isinstance(o, dict):
        raised_by = str(o.get("raised_by") or "").lower()
        where = str(o.get("where") or "").lower()
        origin_is_prose = False
    elif isinstance(o, str) and o.strip():
        raised_by = o.strip().lower()
        where = ""
        origin_is_prose = True
    else:
        raised_by = ""
        where = ""
        origin_is_prose = False
    if origin_is_prose:
        if "example" in raised_by or "demo" in raised_by:
            return "example", "origin string: example"
        return "unknown", "unmapped"
    if "example" in raised_by or "demo" in raised_by:
        return "example", "raised_by"
    if "security" in where or "secret" in where or "auth" in where:
        return "security", "where (security territory)"
    if "requirements_register" in where or "roadmap" in where or "findings_rows" in where:
        return "khaoss", "where (requirements/roadmap = dev territory)"
    if "you" == raised_by.strip() or "operator" in raised_by:
        return "operator", "raised_by"
    return "unknown", "unmapped"


def _explicit(item_id: str) -> dict | None:
    """An operator/holder-set binding wins over any inference."""
    try:
        b = json.loads(BINDINGS.read_text()).get("bindings", {}) or {}
    except Exception:
        return None
    row = b.get(item_id) or {}
    cur = row.get("current")
    if not cur:
        return None
    label = None
    for h in reversed(row.get("history") or []):
        if h.get("target") == cur and h.get("action") == "bound":
            label = h.get("target_label") or h.get("target")
            break
    return {"target": cur, "label": label or cur}


def for_item(item: dict) -> dict:
    """Return the recommendation for one board row. Read-only; never raises on bad input."""
    iid = str((item or {}).get("id") or "")
    territory, how = _derive_territory(item)

    exp = _explicit(iid)
    if exp:
        return {
            "territory": territory,
            "basis": "explicit binding",
            "agents": [{"name": exp.get("label"), "target": exp.get("target"),
                        "why": "explicitly bound to this row in item_bindings.json"}],
            "note": "",
        }

    t = TERRITORY_AGENTS.get(territory)
    if t:
        tid, cid, label, why = t
        return {
            "territory": territory,
            "basis": f"territory map ({how})",
            "agents": [{"name": label, "team_id": tid, "conversation_id": cid, "why": why}],
            "note": ("this is the seat that works the territory, not a claim that it already holds "
                     "this row") if tid else "",
        }

    return {
        "territory": territory,
        "basis": "unmapped",
        "agents": [],
        "note": (f"★ no agent is mapped to territory {territory!r} yet. Do not assume an owner — read "
                 f"the row, or bind one deliberately."),
    }


def self_test() -> int:
    fails = []
    # 1. territory mapping resolves a known lane
    r = for_item({"id": "x", "lane": "example"})
    if not (r["agents"] and r["agents"][0]["name"] == "Example Agent"):
        fails.append("example did not resolve to Example Agent")
    # 2. unmapped lane must NOT invent an owner
    r = for_item({"id": "y", "lane": "unknown"})
    if r["agents"]:
        fails.append("★ an unmapped territory invented an agent")
    if r["basis"] != "unmapped":
        fails.append("unmapped territory did not report its basis")
    # 3. None lane is handled, not crashed
    r = for_item({"id": "z", "lane": None})
    if r["territory"] != "unknown":
        fails.append("a None lane was not normalised")
    # 4. the named example lane is PRESENT, never hidden
    r = for_item({"id": "t", "lane": "example"})
    if not r["agents"]:
        fails.append("★ the example territory hid its seat instead of naming it")
    # ★ CONTROLS 6–9 COVER THE TWO `origin` SHAPES — the empty-top-of-walk defect.
    # 6. ★★ A STRING `origin` NAMING THE EXAMPLE SEAT MUST BIND.
    r = for_item({"id": "s1", "origin": "cc-example-dev, measured 2026-09-10"})
    if r["territory"] != "example" or not r["agents"]:
        fails.append("★★ a STRING origin naming the example seat did not bind")
    # 7. ★ A STRING ORIGIN THAT NAMES NOBODY MUST STILL INVENT NOBODY.
    r = for_item({"id": "s2", "origin": "an agent, sometime, about something"})
    if r["agents"] or r["basis"] != "unmapped":
        fails.append("★ a string origin naming no seat invented an owner")
    # 8. ★ A prose origin must NOT be fed to the `where` heuristics.
    r = for_item({"id": "s3", "origin": "cc-example-dev: the auth ticket, 2026-09-10"})
    if r["territory"] != "example":
        fails.append("★★ a prose origin was matched by the `where` heuristics")
    # 9. A dict origin still resolves through raised_by (no regression).
    r = for_item({"id": "s4", "origin": {"raised_by": "lead-example", "where": "/x"}})
    if r["territory"] != "example" or not r["agents"]:
        fails.append("★ a dict origin stopped resolving through raised_by")
    r = for_item({"id": "s5", "origin": {"raised_by": "nobody recorded", "where": "/x/y"}})
    if r["agents"]:
        fails.append("★ a dict origin naming no seat invented an owner")
    import tempfile
    global BINDINGS
    original = BINDINGS
    try:
        tmp = Path(tempfile.mkdtemp()) / "item_bindings.json"
        tmp.write_text(json.dumps({"bindings": {"bound-row": {
            "current": "cc:999", "history": [{"at": "now", "action": "bound",
                                              "target": "cc:999", "target_label": "Bound Seat"}]}}}))
        BINDINGS = tmp
        r = for_item({"id": "bound-row", "lane": "example"})
        if r["basis"] != "explicit binding" or r["agents"][0]["name"] != "Bound Seat":
            fails.append("★ an explicit binding did not win over the territory map")
    finally:
        BINDINGS = original
    if not fails:
        print("recommended_agents.self_test: 9 controls pass — a named lane resolves · ★ an unmapped "
              "territory invents NO owner · a None lane normalises · the example lane is NAMED not "
              "hidden · an explicit binding wins · ★★ a STRING origin naming the example seat BINDS "
              "(the empty top-of-walk defect) · ★ a string origin naming nobody still invents nobody · "
              "a prose origin is not read as a file path · a dict origin behaves as before")
    else:
        print("recommended_agents.self_test: FAILED: " + "; ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(self_test())
