#!/usr/bin/env python3
"""nbm.py — the Attention OS, executable.

Doctrine: ~/boardwalk/docs/DOCTRINE_NBM_ATTENTION_OS_v1.md
Skill:    ~/.claude/skills/nbm/SKILL.md

WHAT THIS ADDS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
The board already derives ONE priority (P0..P4) in blockers.normalize(), with an explicit warning
against a second definition — that is how it once had a display of 70 and an exit code of 22.
So this tool is a CONSUMER of `priority`. It does not re-rank by a rival notion of importance.

What it adds is the axis the board has never had: **does this need HIM, and for how many of his
minutes.** Priority answers "how blocked are we". James NBM answers "is he the only one who can
move it". Those are different questions, and conflating them is why 130 items point at one person.

★ IT PUBLISHES NO COUNT. The board already disagrees with itself (130 on the CLI, 140 in
board.json, flagged on its own banner). A ninth counting surface would make that worse. This tool
rules BY ID, never by number — and says so when asked for a total.

★ EVERY SCORE NAMES ITS EVIDENCE. The verdict may be asserted; the reason must be measured. Each
term below is derived from something literally present in the row, and `--why <id>` prints it.
Where a term cannot be measured it is reported as unknown, never defaulted to a flattering value.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import blockers  # the SAME collector the operator's board renders from
except Exception as e:  # pragma: no cover
    print(f"nbm: cannot import blockers.py ({type(e).__name__}: {e})", file=sys.stderr)
    raise SystemExit(2)

ATTENTION_DOLLARS_PER_MIN = 83  # ~$5,000/hour

# ── JamesMinutes — MEASURED, and only when the row attributes the time to HIM ────
# ★ v1 of this function was WRONG and its own output exposed it: it read "16 seconds" (a relay
#   latency in a narrative) and "02s" (from a timestamp) as the operator's time cost. A tool that
#   mismeasures his minutes is worse than one that says it does not know — it would rank a 40-minute
#   job above a real 30-second one. So a duration now counts ONLY when it is either written in the
#   board's bracket convention ([10 s] / [5 min]) or sits within a short window of a second-person
#   cue. Everything else is UNKNOWN and says so.
_BRACKET = re.compile(r"\[\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)\s*\]", re.I)
_DURATION = re.compile(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)\b", re.I)
_SHORT = re.compile(r"\b(?:under|less than)\s+a\s+minute\b", re.I)
# second-person / ownership cues that make a duration HIS
_CUE = re.compile(r"\b(you|your|yours|operator'?s|his hands|takes? you|costs? you|of your time)\b", re.I)


def _to_minutes(n: str, unit: str) -> float:
    return float(n) if unit.lower().startswith("m") else max(int(n) / 60.0, 0.1)


def james_minutes(row: dict) -> tuple[float, str]:
    """Return (minutes, evidence). An unattributed duration is NOT his time."""
    text = " ".join(str(row.get(k) or "") for k in ("title", "one", "note", "blocks"))

    m = _BRACKET.search(text)                      # strongest: the board's own convention
    if m:
        return (_to_minutes(m.group(1), m.group(2)), f'row states "{m.group(0)}" in bracket form')

    m = _SHORT.search(text)
    if m and _CUE.search(text[max(0, m.start() - 120): m.end() + 120]):
        return (0.75, f'row says "{m.group(0)}" attributed to you')

    for m in _DURATION.finditer(text):             # only if a cue sits beside it
        window = text[max(0, m.start() - 60): m.end() + 60]
        if _CUE.search(window):
            return (_to_minutes(m.group(1), m.group(2)),
                    f'row says "{m.group(0)}" beside a second-person cue')

    return (10.0, "UNKNOWN — no time attributed to you; assumed 10 min, and the row should state it")


# ── Uniqueness — which irreplaceable faculty does this actually need? ─────────────
FACULTIES = [
    ("capital allocation", 1.0, re.compile(r"\b(you pay|pay for|buy |purchase|fund |budget|subscribe|upgrade to|costs? you \$|fee\b|\$\d+)", re.I)),
    ("authority / ruling", 1.0, re.compile(r"\b(your call|operator'?s call|yours to|his call|ruling|approve|authoris|authoriz|decide|decision is yours|not an agent'?s)", re.I)),
    ("relationship", 1.0, re.compile(r"\b(you send|send (?:them|him|her|it) |write to|letter|ask .*in writing|support letter|reply to (?:them|him|her))\b", re.I)),
    ("taste / acceptance", 0.8, re.compile(r"\b(accept|sign ?off|final|looks right|his eyes|acceptance)\b", re.I)),
    ("credential / hands-on-key", 1.0, re.compile(r"\b(chmod|password|private key|rotate|credential|sudo|login|2fa)\b", re.I)),
    ("values / policy", 0.9, re.compile(r"\b(policy|approved|consent|permission|nobody approved|should we)\b", re.I)),
]


def uniqueness(row: dict) -> tuple[float, list[str]]:
    text = " ".join(str(row.get(k) or "") for k in ("title", "one", "note", "blocks"))
    hits = [(name, w) for name, w, pat in FACULTIES if pat.search(text)]
    if not hits:
        return (0.15, [])  # nothing irreplaceable found — probably not his
    return (max(w for _, w in hits), [n for n, _ in hits])


# ── Decision readiness — the compression contract, audited ───────────────────────
CONTRACT = {
    "a decision to make": re.compile(r"\b(approve|reject|decide|choose|yes/no|your call|option|either|or\b.*\?)", re.I),
    "a recommendation": re.compile(r"\b(recommend|suggest|propose|advise|i'd|we should|prefer)", re.I),
    "a stated time cost": re.compile(r"\b(\d+\s*(?:s|sec|min|minute)|under a minute)\b", re.I),
    "what would change it": re.compile(r"\b(unless|if .*then|would change|flips?|depends on|blocked on)\b", re.I),
}


def contract_gaps(row: dict) -> list[str]:
    text = " ".join(str(row.get(k) or "") for k in ("title", "one", "note", "blocks"))
    return [name for name, pat in CONTRACT.items() if not pat.search(text)]


# ── James NBM ────────────────────────────────────────────────────────────────────
def score(row: dict) -> dict:
    mins, mins_why = james_minutes(row)
    uniq, faculties = uniqueness(row)
    pri = row.get("priority", 3)
    # Leverage: consume the board's OWN priority. P0=strongest. Do not invent a rival scale.
    leverage = {0: 1.0, 1: 0.85, 2: 0.6, 3: 0.4, 4: 0.0}.get(pri, 0.4)
    # Impact: how much the row says it blocks — measured from the text, not assumed.
    blocks = str(row.get("blocks") or "")
    if re.search(r"nothing (technically|mechanically)", blocks, re.I):
        impact, impact_why = 0.5, "row says it blocks nothing mechanically (cost is exposure/clock)"
    elif blocks.strip():
        impact, impact_why = 1.0, f'row names what it blocks: "{blocks[:60]}…"'
    else:
        impact, impact_why = 0.6, "row does not say what it blocks"
    urgency = 1.0 if row.get("deadline") else (0.6 if row.get("latent") else 0.85)
    nbm = (leverage * uniq * impact * urgency) / max(mins, 0.1)
    return {
        "id": row.get("id"), "title": row.get("title"), "priority_label": row.get("priority_label"),
        "nbm": nbm, "minutes": mins, "minutes_why": mins_why, "uniq": uniq, "faculties": faculties,
        "leverage": leverage, "impact": impact, "impact_why": impact_why, "urgency": urgency,
        "gaps": contract_gaps(row), "must_follow": row.get("must_follow"),
        "must_follow_why": row.get("must_follow_why"), "cost": mins * ATTENTION_DOLLARS_PER_MIN,
        "source": row.get("source", "hand"), "row": row,
    }


def open_rows() -> list[dict]:
    rows = blockers.items_of(blockers.load())
    return [r for r in rows if r.get("state") in (None, "open")]


def ranked() -> list[dict]:
    s = [score(r) for r in open_rows()]
    s.sort(key=lambda x: x["nbm"], reverse=True)
    # ★ ordering constraints outrank the score: an item that must follow another cannot be surfaced
    #   above it, however well it scores. The board already records this; honour it.
    ids = {x["id"]: i for i, x in enumerate(s)}
    for x in s:
        dep = x.get("must_follow")
        if dep and dep in ids and ids[dep] > ids[x["id"]]:
            x["nbm"] = 0.0
            x["deferred_by_order"] = dep
    s.sort(key=lambda x: x["nbm"], reverse=True)
    return s


def show(x: dict, n: int | None = None) -> None:
    head = f"#{n}  " if n else "    "
    print(f"\n{head}{x['title']}")
    print(f"      id: {x['id']}   ·   {x['priority_label']}   ·   source: {x['source']}")
    one = str(x["row"].get("one") or "").strip().replace("\n", " ")
    if one:
        print(f"      {one[:300]}{'…' if len(one) > 300 else ''}")
    fac = ", ".join(x["faculties"]) if x["faculties"] else "none found — likely NOT his"
    print(f"      needs him for: {fac}")
    print(f"      your time: ~{x['minutes']:.0f} min  (≈${x['cost']:.0f})   [{x['minutes_why']}]")
    if x.get("must_follow"):
        print(f"      ⚠ MUST FOLLOW «{x['must_follow']}» — {str(x['must_follow_why'])[:160]}")
    if x["gaps"]:
        print(f"      ⚠ compression contract missing: {', '.join(x['gaps'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="NBM — rank the board by what needs HIM, not by what is loudest.")
    ap.add_argument("--all", action="store_true", help="full ranked table")
    ap.add_argument("--system", action="store_true", help="rank by the board's own priority (what should happen)")
    ap.add_argument("--audit", action="store_true", help="rows that violate the compression contract")
    ap.add_argument("--why", metavar="ID", help="show the measured evidence behind one row's score")
    ap.add_argument("-n", type=int, default=3, help="how many decisions to surface (default 3)")
    a = ap.parse_args()

    rows = ranked()
    if not rows:
        print("nbm: nothing open. (This is 'I looked and there is nothing' — not 'I could not look'.)")
        return 0

    if a.why:
        m = [x for x in rows if x["id"] == a.why]
        if not m:
            print(f"nbm: no OPEN row with id '{a.why}'. Rule by id — try --all.")
            return 1
        x = m[0]
        print(f"\nWHY  {x['id']} scores {x['nbm']:.3f}\n" + "─" * 64)
        print(f"  leverage  {x['leverage']:.2f}   consumed from the board's own {x['priority_label']}")
        print(f"  uniqueness{x['uniq']:.2f}   {', '.join(x['faculties']) or 'no irreplaceable faculty found'}")
        print(f"  impact    {x['impact']:.2f}   {x['impact_why']}")
        print(f"  urgency   {x['urgency']:.2f}   {'deadline' if x['row'].get('deadline') else ('latent' if x['row'].get('latent') else 'standard')}")
        print(f"  minutes   {x['minutes']:.1f}    {x['minutes_why']}")
        print(f"\n  nbm = leverage × uniqueness × impact × urgency / minutes")
        return 0

    if a.audit:
        bad = [x for x in rows if x["gaps"]]
        print("\n⚠ COMPRESSION-CONTRACT AUDIT — rows that hand over work instead of a decision\n" + "─" * 72)
        for x in bad:
            print(f"  {x['id']:<34} missing: {', '.join(x['gaps'])}")
        print(f"\n  Rule by id, not by number. Each row above should state its decision, its\n"
              f"  recommendation, its time cost, and what would change the answer.")
        return 0

    if a.system:
        rows.sort(key=lambda x: (x["row"].get("priority", 3), -x["nbm"]))
        print("\n🧭 SYSTEM NBM — what should happen (regardless of who acts)\n" + "─" * 72)
        for x in rows[: (len(rows) if a.all else 10)]:
            print(f"  {x['priority_label']:<20} {x['id']:<32} {x['title'][:52]}")
        return 0

    if a.all:
        print("\n🧭 JAMES NBM — full ranking\n" + "─" * 72)
        for i, x in enumerate(rows, 1):
            flag = "  ⚠order" if x.get("deferred_by_order") else ""
            print(f"  {i:>3}. {x['nbm']:>7.3f}  {x['id']:<34} ~{x['minutes']:>4.0f}m{flag}")
        return 0

    top = rows[: a.n]
    print("\n🧭 JAMES NBM — the decisions that actually need you\n" + "═" * 72)
    for i, x in enumerate(top, 1):
        show(x, i)
    below = [x for x in rows[a.n:]]
    mins = sum(x["minutes"] for x in below)
    print("\n" + "─" * 72)
    print(f"  Below the attention line: everything else on the board.")
    print(f"  If you worked it all yourself it would cost ~{mins:.0f} min ≈ ${mins * ATTENTION_DOLLARS_PER_MIN:,.0f}.")
    print(f"  That is the point: it should be owned elsewhere, not read by you.")
    print(f"\n  ★ No total is printed on purpose — the board already disagrees with itself")
    print(f"    (130 on the CLI, 140 in board.json). Rule by id, never by number.")
    print(f"  nbm.py --why <id> · --audit · --system · --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
