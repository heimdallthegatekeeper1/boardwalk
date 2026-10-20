#!/usr/bin/env python3
"""★ OPERATOR BLOCKERS — what is waiting on the human, surfaced BEFORE work starts.

★ WHY THIS IS A STRUCTURE AND NOT A REMINDER: every rule that stayed a rule has drifted here (L6). Items
  needing the operator have repeatedly been raised once, buried in a report, and then silently carried for
  days — the rebuild sat undeployed for two days across two reports that never mentioned it. A blocker that
  is only mentioned when someone remembers to mention it is a blocker nobody is tracking.

★ THE RULE THIS ENCODES: a session with outstanding operator items must SAY SO before doing other work.
  Not refuse — say so. The operator decides what to do; the machine's job is to make sure the choice is in
  front of him rather than buried.

USE:
    blockers.py                # the banner — run at session start
    blockers.py --json         # machine-readable
    blockers.py --board        # regenerate OPERATOR_BOARD.md (the copy the team chats read)
    blockers.py --set <id> done|later|open
"""
from __future__ import annotations
import json, pathlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "blockers.json")
SEV = {"stop": ("BLOCKING", "\033[31m"), "hold": ("WAITING ON YOU", "\033[33m"), "info": ("YOUR CALL", "\033[36m")}
R = "\033[0m"; B = "\033[1m"; D = "\033[2m"


def load():
    with open(DB) as f:
        return json.load(f)


AUDIT = os.path.join(HERE, "blockers_audit.jsonl")


def audit(entry: dict) -> None:
    """★★★ THE CHANGE REGISTER DID NOT COVER THE CLI.
    blockers_audit.jsonl is journalled by board_reword.py and by board_local.py's writes, and
    board_history.py renders it as THE history of the board. `blockers.py --add` and `--set` wrote
    nothing. Measured 2026-08-29: three items were added to the operator's board by an agent that
    afternoon and the register's newest entry was from 08:21 — the additions were invisible.
    ★ A history that silently omits a CLASS of change is a false-complete: it reads as the record and
      is a subset of it, which is the same defect as every count on this board today.
    ★★ It matters more here than a count, because the trading lane relied on this register as the
      protection that makes narrowing an operator's item visible. That protection was notional for
      any change made through the CLI.
    Best-effort: failing to journal must never lose the write it accompanies."""
    try:
        from datetime import datetime, timezone
        with open(AUDIT, "a") as f:
            f.write(json.dumps({"ts": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                                **entry}) + "\n")
    except Exception:
        pass


def save(d):
    """★★★ ATOMIC, AND LOCKED. Two separate problems, both real on this machine:

    1. `open(DB, "w")` TRUNCATES BEFORE IT WRITES. A crash, a kill, or a full disk between those two
       moments leaves the operator board as an empty or half-written file. Everything on it — every
       blocker, every ruling — would be gone, and the surfaces would agree with each other about it
       because they all read the same file.
    2. NOTHING SERIALISED CONCURRENT WRITERS. Measured 2026-09-06: every agent on this box runs as
       the same uid, no code used flock anywhere, and git had no commits for six weeks. So two seats
       writing at once was possible, undetectable AND unattributable — three properties that make a
       corruption impossible to investigate afterwards.

    ★ Write to a sibling temp file, fsync it, then rename. rename() within a filesystem is atomic:
      a reader sees the old file or the new one, never a partial one.
    ★★ The lock is held across read-modify-write by the CALLER where it matters; here it at least
      makes two saves serialise instead of interleaving.
    """
    import fcntl, os as _os, tempfile
    lock = DB.parent / (DB.name + ".lock") if hasattr(DB, "parent") else pathlib.Path(str(DB) + ".lock")
    lf = open(lock, "a+")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(dir=str(pathlib.Path(str(DB)).parent), prefix=".blockers-", suffix=".tmp")
        try:
            with _os.fdopen(fd, "w") as f:
                json.dump(d, f, indent=2)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(tmp, str(DB))          # atomic within the filesystem
        except Exception:
            try:
                _os.unlink(tmp)
            except Exception:
                pass
            raise
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()
    # ★★★ THE DEGRADED PATH MUST NOT BE THE STALE ONE.
    #   OPERATOR_BOARD.md is what a lead reads when it cannot run commands — by definition the
    #   least-capable reader in the worst conditions. It regenerated only on an explicit `--board`,
    #   so every --add and --set left it behind: measured 2026-08-29 at four items stale, including
    #   an item added FOR the operator minutes earlier. The fallback being the stale copy is exactly
    #   the wrong way round. Raised by the trading lane, whose own AGENTS.md block names this file.
    #   ★ Best-effort: a failure to render the convenience copy must never lose the write above.
    try:
        with open(BOARD_MD, "w") as f:
            f.write(board_md(d))
    except Exception:
        pass


# ── KHAOSS-PATCH: a malformed row must never take the board down (2026-08-28) ──────────────────────
# ★ WHAT HAPPENED: one item arrived without a `one` field and `banner()` did `i['one']` — so the WHOLE
#   BOARD raised KeyError. Every agent that ran blockers.py got a traceback instead of 17 valid items,
#   and the PA lead found it before I did, in its own thinking.
# ★★ WHY THIS IS THE WORST SHAPE: a board that dies reads as a board with NOTHING ON IT. The trading
#   lane named this exact failure for their own write path — "a half-written board reads as an EMPTY
#   board which reads as nothing is outstanding" — and a FALSE CLEAR is the one thing this surface must
#   never produce. Failing closed here means silently telling him he is free.
# ★★★ SO: the reader TOLERATES and SHOWS the damage; the writer REFUSES to create it. Both halves, or
#   the next malformed row does this again from the other direction.
REQUIRED = ("id", "title", "one", "sev", "state")


def normalize(item: dict) -> dict:
    """Return a renderable row, marking what is missing rather than dying on it."""
    it = dict(item)
    missing = [k for k in REQUIRED if not it.get(k)]
    if missing:
        it["_malformed"] = missing
        it.setdefault("id", "(no id)")
        it.setdefault("sev", "hold")
        it.setdefault("state", "open")
        it.setdefault("title", it.get("id", "(untitled item)"))
        if not it.get("one"):
            it["one"] = ("⚠ THIS ITEM IS INCOMPLETE — it was added without: " + ", ".join(missing) +
                         ". It is shown because hiding it would be worse; its content has NOT been "
                         "invented. Whoever added it should supply the missing field(s).")
    it.setdefault("blocks", "")
    it.setdefault("source", "hand")

    # ★★★ ONE DEFINITION OF PRIORITY, COMPUTED HERE, SHARED BY EVERY SURFACE.
    #   The panel could have ranked rows in JavaScript and the CLI in Python. That is exactly how this
    #   board ended up with a display of 70 and an exit code of 22 — two surfaces, two definitions,
    #   nobody able to see the disagreement. So priority is a FIELD, derived once, and every renderer
    #   is a consumer of it.
    #   P0 blocking and time-bound · P1 blocking · P2 waiting on him · P3 his call · P4 settled
    #   ★ `deadline` and `latent` are the two modifiers that actually change urgency rather than
    #     severity: a latent blocker is real but not yet biting, and a deadline turns a decision into
    #     a countdown. Both were previously invisible to any sort.
    sev = it.get("sev", "hold")
    if it.get("state") not in (None, "open"):
        it["priority"] = 4
    elif sev == "stop":
        it["priority"] = 0 if it.get("deadline") else (2 if it.get("latent") else 1)
    elif sev == "hold":
        it["priority"] = 0 if it.get("deadline") else 2
    else:
        it["priority"] = 3
    it["priority_label"] = ["P0 · ACT NOW", "P1 · BLOCKING", "P2 · WAITING ON YOU",
                           "P3 · YOUR CALL", "P4 · SETTLED"][it["priority"]]

    # ★★★ ORDERING CONSTRAINTS — some items close a door permanently if done in the wrong order, and
    #   priority cannot express that. Requested by the trading seat, 2026-09-04: a fresh dial ack
    #   BEFORE the survival column is decided writes the first card under the old rule, and the
    #   shadow-verdict comparison becomes unrecoverable. It would look exactly like progress.
    #   ★ This is not advice in a text field. It is a field the renderer and the walkthrough both read,
    #     so a surface cannot present the item without the constraint attached.
    ORDER_AFTER = {
        "fresh-dial": ("survival-column-before-first-card",
                       "Acking a fresh dial lets the first card be written. If the survival column has "
                       "not been decided by then, that comparison is lost for good — the door closes "
                       "while looking like progress. Decide the survival column FIRST."),
    }
    dep = ORDER_AFTER.get(it["id"])
    if dep:
        it["must_follow"] = dep[0]
        it["must_follow_why"] = dep[1]
    return it


def derived_items() -> tuple[list, list]:
    """★ DERIVED AT RENDER TIME, NEVER WRITTEN INTO THE STORE. A derived row is a VIEW of its source: if
    it were persisted it would become a second copy that outlives the finding it describes — the
    two-stores defect, on the surface built to end it."""
    try:
        import importlib.util, sys as _s
        spec = importlib.util.spec_from_file_location(
            "khaoss_derive_items", os.path.join(HERE, "derive_items.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        return m.derive()
    except Exception as e:
        return ([], [{"source": "derivation", "status": "error", "reason": str(e)[:120], "count": 0}])


def items_of(d: dict) -> list:
    """Hand-authored rows are now ONE SOURCE OF FOUR. While the board was hand-authored, clearing it
    meant nothing: a new operator-blocking finding never reached it, so he could empty it and still be
    blocked."""
    hand = [normalize(i) for i in d.get("items", [])]
    # ★ WHERE A HAND-WRITTEN ROW CAME FROM IS IN THE CHANGE REGISTER, and it was not being shown.
    #   "YOU WROTE THIS" is only half true — some rows were added by an agent that discovered something,
    #   and he could not tell which. The journal knows: it records who added each row and when. Reading
    #   it here means the row can say "added by the trading lane, 2026-08-28" instead of implying he
    #   wrote it himself.
    added_by = {}
    try:
        for line in (HERE and open(AUDIT)):
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("field") == "item" and r.get("id"):
                added_by[r["id"]] = {"by": r.get("by"), "at": r.get("ts"), "direction": r.get("direction")}
    except Exception:
        added_by = {}
    for h in hand:
        h.setdefault("source", "hand")
        rec = added_by.get(h["id"])
        h.setdefault("origin", {
            "what": "a row on your board",
            "where": str(DB), "kind": "hand-written", "row": h["id"],
            "raised_by": (rec or {}).get("by") or "not recorded — it predates the change register",
            "as_of": (rec or {}).get("at") or "",
            "how_to_clear": "only you close it: Done / Do later on the row, or blockers.py --set",
        })
    derived, _health = derived_items()
    known = {i["id"] for i in hand}
    return hand + [normalize(i) for i in derived if i["id"] not in known]


def banner(d) -> str:
    tty = sys.stdout.isatty()
    c = (lambda s, k: f"{k}{s}{R}") if tty else (lambda s, k: s)
    out = [i for i in items_of(d) if i["state"] == "open"]
    if not out:
        return c("✓ OPERATOR BOARD CLEAR — nothing is waiting on you.", "\033[32m")
    L = []
    L.append("")
    L.append(c("━" * 76, "\033[2m"))
    L.append(c(f"  {len(out)} ITEM(S) WAITING ON YOU — nothing here can be unblocked by an agent", B))
    # ★ SAY WHERE THE NUMBER CAME FROM, ON THE SURFACE THAT PRODUCES IT.
    #   A count with no provenance is how two surfaces disagree without anyone noticing: this CLI, the
    #   panel and the session hook all print a total, and for a while the hook's was 21 against 67 here
    #   because it read only the hand-written file. Nobody could see that from either number alone. The
    #   trading lane hit the identical shape in board_lane.py (14 printed, 60 true) and fixed it the same
    #   way — always print the source. Suggested by that lane; taken.
    by_src = {}
    for i in out:
        by_src[i.get("source", "hand")] = by_src.get(i.get("source", "hand"), 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in sorted(by_src.items(), key=lambda kv: -kv[1]))
    # ★★ MEASURED, NOT ASSERTED. This line used to end "same set as /operator/board.json and the session
    #    hook" as STATIC TEXT — true the day it was written, and able to go false silently the next time a
    #    source is added to one surface and not the other. That is the 21-vs-67 defect re-created inside
    #    the fix for it. The trading lane took the same flag on board_lane.py the same hour; this is the
    #    same repair. Compare at print time, and never claim agreement you did not just observe.
    agree = ""
    try:
        import urllib.request
        n = len(json.loads(urllib.request.urlopen(
            "http://127.0.0.1:7842/operator/board.json", timeout=3).read().decode()).get("items", []))
        if n == len(out):
            agree = f"   ·   /operator/board.json agrees ({n}, counted now)"
        else:
            agree = (f"   ·   ★★ DISAGREEMENT: /operator/board.json reports {n}, this surface has "
                     f"{len(out)} — do not trust either count until this is explained")
    except Exception:
        agree = "   ·   /operator/board.json unreachable — NOT compared, so no agreement is claimed"
    L.append(c(f"  sources: {parts}{agree}", "\033[2m"))
    L.append(c("━" * 76, "\033[2m"))
    for i in sorted(out, key=lambda x: ["stop", "hold", "info"].index(x["sev"])):
        label, col = SEV[i["sev"]]
        # ★ A BLOCKER AND AN URGENCY ARE DIFFERENT FACTS. The trading lane raised two items that genuinely
        #   block work but are not urgent — nothing is trading, and one is latent (a gate that only fails at
        #   a venue we do not use). Forcing those to share a colour with "the rebuild was never applied"
        #   is how a board teaches people to skip its top severity. `latent: true` keeps the honesty of
        #   STOP without borrowing the urgency of it.
        if i.get("latent"):
            label += " · LATENT"
        if i.get("_malformed"):
            label += " · ⚠ INCOMPLETE"
        lane = f" ({i['lane']})" if i.get("lane") and i["lane"] != "unknown" else ""
        L.append(f"  {c('['+label+']', col)} {c(i['title'], B)}{c(lane, D)}")
        L.append(f"      {i['one']}")
        if i.get("blocks") and i["blocks"] != "nothing":
            L.append(c(f"      blocks: {i['blocks']}", D))
    L.append("")
    L.append(c(f"  Open the board to decide, add notes, or defer:", D))
    L.append(f"  {d['board_url']}")
    L.append(c("━" * 76, "\033[2m"))
    L.append("")
    return "\n".join(L)


BOARD_MD = os.path.join(HERE, "OPERATOR_BOARD.md")


def board_md(d) -> str:
    """★ The SAME items, rendered for a reader that is not a terminal.

    A second renderer is a drift risk (two views of one store), so it lives in this file, beside
    `banner()`, and is generated — never hand-edited. The file it writes carries that warning in its
    first line, because the failure mode here is somebody editing the copy instead of the board.
    """
    out = [i for i in items_of(d) if i["state"] == "open"]
    L = ["<!-- GENERATED by operator/blockers.py --board — do not edit; edit the board, then regenerate. -->",
         "# OPERATOR BOARD — what is waiting on the human", ""]
    if not out:
        L += ["**CLEAR** — nothing is waiting on the operator.", "", f"Board: {d['board_url']}", ""]
        return "\n".join(L)
    L.append(f"**{len(out)} item(s) outstanding.** Nothing here can be unblocked by an agent.")
    L.append("")
    for i in sorted(out, key=lambda x: ["stop", "hold", "info"].index(x["sev"])):
        label = SEV[i["sev"]][0]
        if i.get("latent"):
            label += " · LATENT"
        lane = f" ({i['lane']})" if i.get("lane") and i["lane"] != "unknown" else ""
        L.append(f"- **[{label}]** {i['title']}{lane}")
        L.append(f"  - {i['one']}")
        if i.get("blocks") and i["blocks"] != "nothing":
            L.append(f"  - *blocks:* {i['blocks']}")
    L += ["", f"Decide, note, or defer on the board: {d['board_url']}", ""]
    return "\n".join(L)


def self_test() -> int:
    """★ NEGATIVE CONTROL for the verdict a decision actually rests on.

    This surface exists to refuse to look clear when it is not. Every lead is told to branch on its
    exit code without parsing its text, so a FALSE CLEAR is the one failure that matters, and it is
    invisible: exit 0 and a green line look exactly like a genuinely empty board.

    The test therefore proves the verdict CAN FLIP, and flips for the right reason:
      · items open           -> exit 1        (must not report clear)
      · nothing open at all  -> exit 0        (clear is REACHABLE — otherwise "not clear" proves nothing)
      · hand-written rows all closed, derived rows still open -> STILL exit 1
        (the historical bug: the exit code read d["items"] while the display read the merged set)
    """
    failures = []

    def verdict(board):
        return 1 if any(i["state"] == "open" for i in items_of(board)) else 0

    real = load()

    if verdict(real) != 1 or not [i for i in items_of(real) if i["state"] == "open"]:
        failures.append("the live board has open items but the verdict is not 1")

    empty = {**real, "items": []}
    saved = globals().get("derived_items")
    globals()["derived_items"] = lambda: ([], {})
    try:
        if verdict(empty) != 0:
            failures.append("with NOTHING open the verdict is still 1 — 'clear' is unreachable, so "
                            "'not clear' proves nothing; a verdict that cannot flip is not a verdict")
    finally:
        globals()["derived_items"] = saved

    closed_hand = {**real, "items": [{**i, "state": "done"} for i in real["items"]]}
    if [i for i in items_of(closed_hand) if i["state"] == "open"] and verdict(closed_hand) != 1:
        failures.append("hand-written rows closed but DERIVED rows open -> reported CLEAR. "
                        "This is the false clear: the exit code reading a subset of what is displayed")

    for f in failures:
        print(f"  \u2717 {f}")
    print("  \u2713 blockers self_test: verdict can flip, and cannot go clear while anything is open"
          if not failures else "  \u2717 blockers self_test FAILED")
    return 1 if failures else 0


def main():
    if "--self-test" in sys.argv:
        return self_test()
    a = sys.argv[1:]
    d = load()
    if a and a[0] == "--json":
        print(json.dumps(d, indent=1)); return 0
    if a and a[0] == "--board":
        # ★ ONE STORE, TWO VIEWS. The teams' context files point at this generated file; they never
        #   carry a copy of the items, because a pasted digest is stale the next time he defers one.
        with open(BOARD_MD, "w") as f:
            f.write(board_md(d))
        # ★ This counted d["items"] — the hand-written rows — while board_md(d) renders the MERGED
        #   set, so the confirmation said "22 open" about a file containing 71. A confirmation line
        #   that describes a different population than the thing it confirms is its own small lie.
        n = len([i for i in items_of(d) if i["state"] == "open"])
        print(f"  wrote {BOARD_MD} ({n} open)")
        return 0
    if a and a[0] == "--add":
        # ★ 2026-08-27 — ADDED BECAUSE THE BOARD WAS SINGLE-LANE BY CONSTRUCTION. The trading lane had four
        #   operator-blocking items, correctly refused to write into another tree, and had nowhere to put
        #   them. A board only one lane can write to guarantees an item can be outstanding in one lane and
        #   invisible in another — which is the two-stores failure the board exists to end.
        #   Reads a JSON object or array on stdin so any lane can contribute without importing this module.
        try:
            payload = json.load(sys.stdin)
        except Exception as e:
            print(f"--add expects JSON on stdin: {e}", file=sys.stderr); return 2
        new_items = payload if isinstance(payload, list) else [payload]
        have = {i["id"] for i in d["items"]}
        added = skipped = 0
        for it in new_items:
            missing = [k for k in REQUIRED if k not in ("state",) and not it.get(k)]
            if missing:
                # ★ REFUSED AT THE DOOR. The reader can survive a malformed row, but it should never
                #   have to: an item with no `one` is an item nobody can act on, which is the whole
                #   point of a board. Naming the missing field is what makes this fixable in one edit.
                print(f"REFUSED {it.get('id') or '(no id)'}: missing required field(s) {missing}. "
                      f"An item with no {missing[0]!r} cannot be acted on.", file=sys.stderr)
                skipped += 1; continue
            if it["id"] in have:
                skipped += 1; continue
            # ★★ VALIDATE sev AT THE DOOR — added 2026-09-06 after the trading lane wrote
            #   sev="blocking" and CRASHED THIS BOARD for every reader. The renderer does
            #   ["stop","hold","info"].index(x["sev"]) and a value outside that set raises
            #   ValueError, so ONE malformed row from ONE lane took down the operator's only
            #   view of 111 items. The door already refuses a missing `one` on exactly this
            #   reasoning — an item nobody can act on should never get in — but it accepted
            #   any string at all for the field the SORT KEY reads.
            #   ★ Why the mistake was easy, which is why the message names the mapping: the
            #   value is "stop" but its RENDERED LABEL is "BLOCKING". A writer reading the
            #   board sees BLOCKING and writes "blocking". Refusing without showing the
            #   mapping would just produce the same wrong guess again.
            if it["sev"] not in SEV:
                print(f"REFUSED {it['id']}: sev={it['sev']!r} is not one of {sorted(SEV)}. "
                      f"Note the labels differ from the values — "
                      + ", ".join(f"{k!r} renders as {v[0]!r}" for k, v in SEV.items())
                      + ". A bad sev crashes the renderer for every reader, so it is "
                        "refused here rather than written.", file=sys.stderr)
                skipped += 1; continue
            it.setdefault("state", "open")
            it.setdefault("lane", "unknown")
            d["items"].append(it); have.add(it["id"]); added += 1
            # ★ An addition is a change to the operator's board and belongs in the register that
            #   board_history.py renders. Without this, an agent could add items all afternoon and
            #   the history would show the morning's last reword as the newest event.
            audit({"id": it["id"], "field": "item", "direction": "added",
                   "by": os.environ.get("KHAOSS_ACTOR", "agent (blockers.py --add)"),
                   "sev": it.get("sev"), "title": str(it.get("title"))[:160]})
        save(d)
        print(f"  added {added}, skipped {skipped} already present")
        return 0
    if a and a[0] == "--set":
        if len(a) < 3 or a[2] not in ("done", "later", "open"):
            print("usage: blockers.py --set <id> done|later|open", file=sys.stderr); return 2
        hit = [i for i in d["items"] if i["id"] == a[1]]
        if not hit:
            print(f"unknown id {a[1]!r}. ids: {[i['id'] for i in d['items']]}", file=sys.stderr); return 2
        was = hit[0].get("state")
        hit[0]["state"] = a[2]; save(d)
        audit({"id": a[1], "field": "state", "direction": f"{was} -> {a[2]}",
               "by": os.environ.get("KHAOSS_ACTOR", "agent (blockers.py --set)"),
               "note": "state changed via CLI"})
        print(f"  {a[1]} → {a[2]}")
        return 0
    print(banner(d))
    # ★ exit 1 when something is outstanding, so a caller can branch on it without parsing text.
    # ★★★ THE EXIT CODE IS THE CONTRACT, AND IT READ THE WRONG SET.
    #   This was `d["items"]` — the HAND-WRITTEN rows only — while banner() renders items_of(d), the
    #   merged set. Measured 2026-08-29: display 70 open, exit-code population 22. Close every
    #   hand-written row and this returned 0 ("board clear") with 48 derived items open ON SCREEN.
    #   ★ And the block installed in eight AGENTS.md files THIS SAME TURN tells every lead: "It exits 1
    #     while anything is open, and stays quiet on a clear board" — offered as the thing to branch on
    #     WITHOUT parsing text. So the documented contract was false in the direction that matters: a
    #     FALSE CLEAR, on the one surface whose whole job is to refuse to look clear when it isn't.
    #   Found only because INV-114 ranked unproven detectors by what depends on them and this one is
    #   read by the invariant runner, the panel, the board and the session hook.
    return 1 if any(i["state"] == "open" for i in items_of(d)) else 0


if __name__ == "__main__":
    sys.exit(main())
