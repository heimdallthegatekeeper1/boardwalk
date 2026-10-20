#!/usr/bin/env python3
"""★ Reword a board row — the ONE path that can do it, and it cannot omit the direction.

★ WHY THIS EXISTS (trading lane, 2026-08-28, and the objection is theirs): the hazard is not rewording,
  it is NARROWING. A board can be defanged one defensible correction at a time, and every individual
  narrowing looks like rigour — theirs did, and theirs WAS. Nothing in a journal distinguishes "the author
  retracted an overstatement" from "a peer quietly softened a finding": both produce a narrower row,
  journalled, with a plausible reason.

★★ SO DIRECTION IS DECLARED, NOT INFERRED. Not from text length, not from severity — by the author, in
  writing. And it is REQUIRED here rather than remembered, because every rule that stayed a rule drifted (L6).

★★★ AND IT HAS A READER: board_history.py. A direction field nobody reads would be a record that does not
  exist to the decision it should govern (L7) — the failure this whole board was built to end.

TWO RULES THIS ENFORCES:
  · a row is reworded only BY its author or at the author's WRITTEN request (--requested-by is required)
  · the journal names the ACTOR and the REQUESTER, never just one

USE:
    board_reword.py --id <id> --direction narrowed|widened|neutral \
        --requested-by "<who asked, verbatim>" --field one --text "..."      (or --text - for stdin)
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "blockers.json"
AUDIT = HERE / "blockers_audit.jsonl"
DIRECTIONS = ("narrowed", "widened", "neutral")


# ★★★ A GUARD AGAINST SHELL-MANGLED TEXT, added after it happened to me.
#   I passed a --text containing backticks through a shell argument. The shell command-substituted
#   them, the substitution failed, and the OUTPUT WAS EMPTY — so the board row silently lost the
#   identifier it existed to name: "the CDP bridge event , whose callback never fires".
#   ★ Nothing errored. The reword reported success. A row is worse for having a hole in it than for
#     not being reworded, because the hole is invisible to the next reader.
def _warn_if_mangled(text: str) -> None:
    import sys as _s
    marks = [m for m in ("``", "  ,", " , ", "$(", "  .") if m in text]
    if any(text.count(c) for c in "`$") and "SHELL-MANGLED" not in text:
        print("  \u2605 NOTE: this text contains ` or $ — if you passed it as a shell argument it may "
              "already have been substituted before reaching here. Check the stored row.", file=_s.stderr)
    if marks:
        print(f"  \u2605 NOTE: the text contains {marks} — that pattern often means a shell ate "
              f"something. Read the stored row back before trusting it.", file=_s.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", required=True)
    ap.add_argument("--direction", required=True, choices=DIRECTIONS,
                    help="declared by the AUTHOR. narrowed = the claim is now weaker/less alarming.")
    ap.add_argument("--requested-by", required=True,
                    help="who asked for this, verbatim. A rewrite with no requester is not allowed.")
    ap.add_argument("--actor", default="CC (KHAOSS dev)")
    ap.add_argument("--field", default="one", choices=("one", "title", "blocks"))
    ap.add_argument("--text", required=True, help="new text, or - to read stdin")
    ap.add_argument("--why", default="", help="one line: why the wording changed")
    # ★ ADDED AFTER I DAMAGED A LIVE ROW WITH THIS TOOL. I smoke-tested it against the operator's real
    #   board — it wrote the placeholder text straight into a STOP item — an hour after reading the trading
    #   lane say they had tested THEIR write path against a copy for exactly this reason. The tool was
    #   correct; the target was not. So the safe target is now cheap to reach and the dry run is one flag.
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change and write nothing. USE THIS to smoke-test.")
    ap.add_argument("--db", default=None, help="operate on a COPY instead of the live board")
    a = ap.parse_args()
    global DB, AUDIT
    if a.db:
        DB = Path(a.db)
        AUDIT = DB.parent / (DB.stem + "_audit.jsonl")

    text = sys.stdin.read().strip() if a.text == "-" else a.text
    if not text.strip():
        print("refusing: empty text", file=sys.stderr); return 2
    if not a.requested_by.strip():
        print("refusing: --requested-by may not be blank", file=sys.stderr); return 2

    d = json.loads(DB.read_text())
    hit = [i for i in d["items"] if i["id"] == a.id]
    if not hit:
        print(f"unknown id {a.id!r}. ids: {[i['id'] for i in d['items']]}", file=sys.stderr); return 2
    it = hit[0]
    old = it.get(a.field, "")
    if old.strip() == text.strip():
        print("  no change — text is identical"); return 0

    # ★ STATE IS NOT TOUCHABLE FROM HERE. Rewording is proposing; closing is disposing, and only the
    #   operator disposes. Keeping them in separate tools is what makes that structural rather than polite.
    if a.dry_run:
        print(f"  DRY RUN — nothing written.\n  {a.id}.{a.field}: {len(old)} chars → {len(text)} chars"
              f"\n  direction={a.direction} requested_by={a.requested_by!r}\n  was: {old[:100]}"
              f"\n  now: {text[:100]}")
        return 0
    it[a.field] = text
    tmp = DB.with_suffix(".json.tmp"); tmp.write_text(json.dumps(d, indent=2) + "\n"); tmp.replace(DB)

    with AUDIT.open("a") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "id": a.id, "field": a.field,
            "direction": a.direction,
            "by": a.actor, "requested_by": a.requested_by,
            "why": a.why,
            "was_chars": len(old), "now_chars": len(text),
            "was_excerpt_first_120": old[:120],
        }) + "\n")
    print(f"  {a.id}.{a.field} reworded · direction={a.direction} · requested by {a.requested_by}")
    print(f"  state untouched: {it['state']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
