#!/usr/bin/env python3
"""★ SessionStart hook — every Claude Code session opens knowing what is waiting on the operator.

★ WHY THIS FILE EXISTS AT ALL: `session_start.sh` was written for exactly this and NOTHING CALLED IT —
  the same built-but-unwired shape as `ctx_meter.py` and `pretool_guard.py`. A surface that depends on
  someone remembering to run it is not a surface (L6/L7). The hook is the caller.

★★ IT SPEAKS TO BOTH PARTIES, because they need different things:
  · `systemMessage`     → the OPERATOR sees the count and the top blocking item, in his terminal.
  · `additionalContext` → CLAUDE gets the list and the protocol, so it can SAY SO before doing other
                          work rather than needing to be asked.
  A hook that only prints to the terminal leaves the model ignorant; one that only injects context
  leaves the operator ignorant. Both, or the board reaches nobody at the moment it matters.

★★★ SILENT WHEN THE BOARD IS CLEAR. Emitting nothing is the point — a banner that always fires is a
  banner people learn to skip, which is the whole reason blockers.py stays quiet on an empty board.
"""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = "/usr/bin/python3"   # ★ never bare python3 — that resolves to pyenv, which lacks the deps (L11)


def _taint_surface():
    """★ THE TAINT GATE, ON THE ONE SCREEN THE OPERATOR ACTUALLY READS.

    THE RULE THIS EXISTS TO KEEP: *the system must not know something the operator does not.*
    `jarvis/taint_gate.py` (the Doorman) is a pre-tool gate that is ASLEEP until a fetch-shaped call
    brings untrusted content into a run, and from then on holds shell / file-write / network /
    destructive calls behind an exact-action approval. While it is armed it is the reason a lane
    cannot write a file, and if the ONLY place that fact lived were the refusal the model reads,
    then the operator would be watching agents stop working for a reason nothing on his screen
    explained -- the exact shape of `FA-11-over-claiming-badges`, inverted: a control whose state
    nobody can see is indistinguishable from a control that is not there.

    ★ SILENT WHEN THE GATE IS ASLEEP, which is this file's own rule ("a banner that always fires is a
      banner people learn to skip") and also the whole point of the gate: a clean run costs ZERO
      prompts. So it speaks on exactly two states -- ARMED, and UNKNOWN (a state file that could not
      be read is NOT the same as a clean one, and must never be painted as such).

    Returns None on every path that is not worth a line, and swallows every error: a broken hook must
    never block a session from starting (the contract this file already states at the top).
    """
    try:
        import importlib
        mod_path = HERE.parent / "jarvis" / "taint_gate.py"
        if not mod_path.exists():
            return None
        sys.path.insert(0, str(mod_path.parent))
        tg = importlib.import_module("taint_gate")
        st = tg.status_state()
    except Exception:
        return None
    state = st.get("state")
    if state == "asleep":
        return None
    if state == "disabled":
        # Reported ONCE per session and never as a warning: taking the gate out of the way is a
        # legitimate operator act, and a line that nags about it is a line he will learn to skip.
        return {"head": " · TAINT GATE OFF",
                "body": [f"★ THE TAINT GATE IS SWITCHED OFF ({st.get('disabled_why')}). Untrusted content "
                         f"can enter a run and nothing will hold the tool calls that follow it."]}
    if state == "unknown":
        return {"head": " · TAINT GATE UNKNOWN",
                "body": [f"★★★ THE TAINT GATE COULD NOT READ ITS OWN STATE FILE "
                         f"({st.get('store_error') or 'unreadable'}). It has failed OPEN "
                         f"{st.get('unreadable_reads')} time(s): calls are going through UNCHECKED. "
                         f"This is NOT a clean run. Check: /usr/bin/python3 "
                         f"~/boardwalk/jarvis/taint_gate.py status"]}
    armed = st.get("armed") or []
    if not armed:
        return None
    who = "; ".join(f"{a['session']} (via {a['via']}, since {a['at']})" for a in armed[:3])
    body = [f"★★★ THE TAINT GATE (the Doorman) IS ARMED FOR {len(armed)} SESSION(S): {who}.",
            "    Untrusted content entered, so shell / file-write / network / destructive calls in "
            "that session need an approval naming the EXACT action. Nothing is wrong; the gate is "
            "doing what it was built to do, and it says so here rather than only inside a refusal "
            "the operator cannot see.",
            "    State: /usr/bin/python3 ~/boardwalk/jarvis/taint_gate.py status"]
    for a in armed[:3]:
        body.append(f"    · {a['session']}: {a['source']} -- {a['source_why']}; disarms {a['expires']}")
    return {"head": f" · TAINT GATE ARMED ({len(armed)})", "body": body}


def _rosetta_surface():
    """★ THE DOCUMENT'S OWN STALENESS GATE, ON THE ONE SURFACE EVERY SESSION READS FIRST.

    THE DEFECT THIS EXISTS FOR, MEASURED 2026-10-14: `docs/rosetta_gate.py` went RED with 17 stale
    citations and stayed red for **1h22m**. Nothing ran it -- no timer, no hook, no suite. It was found
    by a human who happened to type its path. `docs/ROSETTA_STONE.md` is the document every agent is
    told to read before touching the GUI, and it cites `jarvis/bubble.py` BY LINE NUMBER; that file was
    rewritten six times in one night, so the document rots on a schedule set by other lanes' work.

    ★ SILENT WHEN THE GATE IS GREEN, which is this file's own rule ("a banner that always fires is a
      banner people learn to skip") and the reason it can afford to be loud when it is not.
    ★ IT SPEAKS THE DENOMINATOR AND THE ONE COMMAND. "The gate is red" is a mood; "the symbol is not on
      the line the document names, and here is the one command that re-takes it" is a repair. This
      lane's own rule: a green is only as wide as its scope, so a RED says its width too.
    ★ IT NEVER WRITES. Re-taking the citations is a deliberate act (`docs/rosetta_recite.py --apply`);
      a hook that silently repaired the document would be a gate nobody could see failing.
    """
    try:
        gate = HERE.parent / "docs" / "rosetta_gate.py"
        if not gate.is_file():
            return None
        p = subprocess.run([PY, str(gate)], capture_output=True, text=True, timeout=25,
                           cwd=str(HERE.parent))
        if p.returncode == 0:
            return None
        out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        first = out[0] if out else "no output"
        tag = first.split("]")[0].replace("RED [", "") if first.startswith("RED [") else "?"
        return {
            "head": "  ·  ROSETTA GATE RED",
            "body": [
                f"★★ THE ROSETTA GATE IS RED [{tag}] — docs/ROSETTA_STONE.md is NOT current, and until "
                f"it is re-taken any line number you follow from it may land on the wrong line.",
                f"  {first[:220]}",
                "  ONE COMMAND re-takes the symbol-anchored citations from the symbols they name:",
                "      python3 docs/rosetta_recite.py --apply",
                "  It does NOT re-stamp the fingerprints (F2) on purpose — F2 exists so a changed file "
                "forces a re-read, and a tool that re-stamped by itself would be a guard that cannot fail.",
            ],
        }
    except Exception:  # noqa: BLE001 -- a broken hook must never block a session from starting
        return None


def main() -> int:
    try:
        # keep the generated copy the team chats read from lagging its store
        subprocess.run([PY, str(HERE / "blockers.py"), "--board"], capture_output=True, timeout=15)
        board = json.loads((HERE / "blockers.json").read_text())
    except Exception:
        return 0                       # a broken hook must never block a session from starting

    # ★ READ THE MERGED SET, NOT THE HAND-WRITTEN FILE.
    #   This line used to be `board["items"]` alone, which is the 21 hand-written rows — while the CLI
    #   and the panel both showed 67, because they merge in items DERIVED from the findings ledger,
    #   invariant FAILs, approval requests and the requirements register. So the hook that every session
    #   reads FIRST was teaching every agent that the board was a third its real size, and an agent that
    #   dutifully reported "21 outstanding" contradicted an operator looking at 67 — with no bug visible
    #   anywhere in the exchange. The trading lane found this same defect in its own board_lane.py
    #   (14 reported, 60 true) and fixed it the same day; this is the identical shape on my surface.
    #   Provenance is now printed, so no count from here can be quoted without its source.
    src = "hand-written + derived (complete)"
    try:
        sys.path.insert(0, str(HERE))
        import blockers as _b                       # same module the CLI renders from
        merged = _b.items_of(board)
    except Exception:
        merged = board.get("items", [])
        src = "hand-written ONLY — derived sources unavailable, THIS IS A SUBSET"

    items = [i for i in merged if i.get("state") == "open"]
    if not items:
        return 0                       # clear board, no noise

    order = {"stop": 0, "hold": 1, "info": 2}
    items.sort(key=lambda i: order.get(i.get("sev"), 9))
    stop = [i for i in items if i.get("sev") == "stop"]

    head = f"🛎  {len(items)} item(s) waiting on you"
    if stop:
        head += f" · {len(stop)} blocking · {stop[0]['title']}"
    head += "\n   http://127.0.0.1:7842/operator/board   ·   operator/blockers.py"

    # ★★★ THE QUEUE MUST BE READ, NOT ONLY WRITTEN. The board's picker tells him a queued handoff will
    #   be "delivered by an agent's next turn". That was true about intent and unverified in practice —
    #   nothing had ever read the queue. Surfacing it HERE is what makes the sentence true, because
    #   this is the one thing every agent session reads before doing anything else.
    handoffs = []
    try:
        sys.path.insert(0, str(HERE))
        import drain_handoffs
        handoffs = drain_handoffs.pending()
    except Exception:
        handoffs = []

    lines = [f"THE OPERATOR BOARD HAS {len(items)} OPEN ITEM(S) [{src}]. Nothing here can be unblocked by an agent.",
             "Protocol: say so before doing other work — a short list, then carry on with what you were "
             "asked. Never mark one done on his behalf; you may ADD one with `blockers.py --add`.", ""]
    for i in items:
        sev = {"stop": "BLOCKING", "hold": "WAITING ON YOU", "info": "YOUR CALL"}.get(i.get("sev"), "ITEM")
        if i.get("latent"):
            sev += " (latent)"
        lane = f" [{i['lane']}]" if i.get("lane") and i["lane"] != "unknown" else ""
        lines.append(f"  · [{sev}]{lane} {i.get('title','')} — {i.get('one','')[:180]}")
    lines += ["", "Live: /usr/bin/python3 ~/boardwalk/operator/blockers.py",
              "Local docket: http://127.0.0.1:7842/operator/board"]

    if handoffs:
        lines.append("")
        lines.append(f"★ {len(handoffs)} HANDOFF(S) ARE WAITING FOR AN AGENT TO DELIVER — he sent these "
                     f"from the board and they have NOT arrived yet. If any is addressed to you, deliver "
                     f"it and then mark it: operator/drain_handoffs.py --delivered <row> \"how\"")
        for h in handoffs[:6]:
            lines.append(f"  · [row {h.get('_n')}] → {h.get('target_label') or h.get('target')}: "
                         f"{str(h.get('text',''))[:120]}")

    # ★★ AND THE TAINT GATE SPEAKS HERE, because it is the one thing an agent may be blocked by that
    #    nothing else on this surface mentions. Silent when the gate is asleep; see _taint_surface().
    _tg = _taint_surface()
    if _tg:
        head += _tg["head"]
        lines.append("")
        lines += _tg["body"]

    # ★★ AND THE DOCUMENT'S STALENESS GATE SPEAKS HERE — 2026-10-14. It went red for 1h22m with
    #    nothing running it; this is the runner. Silent when green (see _rosetta_surface()).
    _rg = _rosetta_surface()
    if _rg:
        head += _rg["head"]
        lines.append("")
        lines += _rg["body"]

    print(json.dumps({
        "systemMessage": head,
        "suppressOutput": True,        # the systemMessage is the operator-facing surface; no raw stdout
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)},
    }))
    return 0


def self_test() -> int:
    """★ NEGATIVE CONTROL for the defect found 2026-08-29: this hook read `board["items"]` — the
    hand-written rows only — and announced that total to EVERY Claude session on this machine. It said
    21 while the CLI and the panel said 67. Every agent in the house began every session believing the
    board was a third of its real size, and a lead quoting it contradicted the operator with no bug
    visible in the exchange.

    The test therefore is not 'does it print a number'. It is: is the number the MERGED one, and is it
    strictly larger than the hand-written file alone? If someone reverts the line, hand-only == merged
    and this fails. Fails also if derived sources have vanished, which is itself worth failing on.
    """
    import json as _j
    sys.path.insert(0, str(HERE))
    try:
        import blockers as _b
    except Exception as e:
        print(f"  ✗ cannot import blockers ({e}) — the merged path is unavailable, so this hook would")
        print("    silently fall back to the hand-written SUBSET. That is the failure, not a skip.")
        return 1

    board = _j.loads((HERE / "blockers.json").read_text())
    hand = len([i for i in board.get("items", []) if i.get("state") == "open"])
    merged = len([i for i in _b.items_of(board) if i.get("state") == "open"])

    fail = []
    if merged <= hand:
        fail.append(f"merged count ({merged}) is not greater than hand-written ({hand}) — either the "
                    f"hook has been reverted to the subset, or every derived source has gone silent")
    if hand == 0:
        fail.append("hand-written set is EMPTY — the comparison is vacuous and proves nothing")

    for f in fail:
        print(f"  ✗ {f}")
    if fail:
        print("  ✗ cc_session_hook self_test FAILED")
        return 1
    print(f"  ✓ cc_session_hook self_test: reports the MERGED set ({merged}), not the "
          f"hand-written subset ({hand})")
    return 0


if __name__ == "__main__":
    if "--prove-fail" in sys.argv:
        sys.exit(self_test())
    sys.exit(main())
