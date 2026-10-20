#!/usr/bin/env python3
"""INV-114 · No new detector without a negative control.

★★★ THE PRECONDITION THIS ENCODES, in the trading seat's words, which are better than mine:
    BEFORE REPORTING ABSENCE OR SUCCESS, PROVE THE QUERY COULD HAVE RETURNED THE OTHER ANSWER.

Three instances in one day between two seats, three different suppression mechanisms:
  · a grep for "UNREADABLE, never" against a file containing "UNREADABLE — never"  → 0, read as absence
  · an unquoted $(grep -rl ...) word-splitting on paths with spaces               → 3 of 8 files "clean"
  · a POST to an endpoint that 404s, with >/dev/null 2>&1                          → old code tested as new
Each returned a number that was indistinguishable from the right answer. None could have failed loudly.

★★ WHY A RATCHET AND NOT A FLAT INVARIANT. Twelve of fifteen detectors here have no negative control.
   A check that goes red on its first run and stays red is wallpaper inside a week — it teaches the
   reader to skip it, which is worse than not having it. So this records a BASELINE and fails only when
   the count of unproven detectors GROWS. Existing debt stays visible in the output; new debt is refused.
   Paying the baseline down is the operator's call on priorities, not something a gate should force.

★ A detector "has a negative control" when it can be made to say the OTHER thing on demand — a
  self_test()/--prove-fail that constructs input it MUST reject, and fails if the verdict comes back clean.
"""
from __future__ import annotations
import ast, json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASELINE = HERE / "detector_selftest_baseline.json"
# ★★★ MATCH A DEFINITION, NOT PROSE. The first version of this line was a regex over the file text,
#   and it counted any module whose COMMENTS discussed negative controls as HAVING one. Its own negative
#   control — a stub detector whose docstring said "no negative control" — was scored as PROVEN and the
#   gate returned green. A detector fooled by writing about detectors is the coarse-detector class again,
#   and it failed on the very first thing it was pointed at. Parse the module and look for a real
#   self-test function, or a real --prove-fail/--selftest flag compared against argv.
PROOF_NAMES = {"self_test", "selftest", "prove_fail", "prove_fails", "negative_control"}
PROOF_FLAGS = {"--prove-fail", "--selftest", "--self-test", "--negative-control"}


def _failure_path(fn) -> str:
    """Can this control return anything other than 0/None, or raise? A self_test that can ONLY pass
    asserts nothing — it is the SHAPE of a test, not a test, and satisfying a gate with the shape of
    the thing is the class this gate exists to catch.
    ★ The first version of this checked only ast.Constant returns, so `return 1 if failures else 0`
      — a ternary — read as 'can only pass'. It convicted four detectors, two of which had been
      watched failing on purpose an hour earlier. Coarse detector; caught by checking its verdict
      against ground truth already in hand rather than by reading the code again."""
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Raise):
            return "raise"
        if isinstance(sub, ast.Return) and sub.value is not None:
            v = sub.value
            if isinstance(v, ast.Constant):
                if v.value not in (0, None):
                    return f"return {v.value!r}"
            else:
                return f"return <{type(v).__name__}>"
    return ""


def has_proof(src: str) -> bool:
    """★★★ WHAT THIS CAN AND CANNOT ESTABLISH — stated here because the limit is the important part.
    IT CAN establish that a control EXISTS and that it CAN fail.
    IT CANNOT establish that the control was chosen ADVERSARIALLY, and that is where the value is.
    Demonstrated the same day: a peer's audibility check had a negative control using Firefox and
    speech-dispatcher. It passed, and told them nothing — the live bug was that `paplay` contains the
    substring `aplay`, so a different binary counted as theirs. A control built from plainly-wrong
    inputs certifies nothing about inputs that are NEARLY right.
    So "I proved it fails" is a weaker claim than it sounds, always. This gate raises the floor; it
    does not reach the ceiling, and no static check can."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROOF_NAMES:
            if _failure_path(node):
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PROOF_FLAGS:
            return True
    return False
# A file that renders a verdict about the world. Kept deliberately broad: over-including a helper costs
# one line of output, under-including a detector is how the gate goes quiet.
VERDICT = re.compile(r"def (check|detect|assert|scan|audit|verify)|FAIL|VIOLATION", re.I)
# ★★★ voice_log.py WAS EXEMPT AND SHOULD NEVER HAVE BEEN. It was waved through as a "helper", but it
#   holds TWO detectors: SinkWatcher (which decides whether an utterance was audible, and had inverted)
#   and read()'s CLEARANCE FILTER, which decides what a viewer is allowed to see. The orchestrator
#   ranked that filter SECOND by consequence — a false clear there leaks cross-lane content into one
#   view, the thing this house has closed twice. An exemption list is a place where things become
#   invisible to the very check that was built to find them.
EXEMPT = {"detector_selftest_ratchet.py"}


def survey() -> tuple[list, list]:
    with_p, without = [], []
    for f in sorted(HERE.glob("*.py")):
        if f.name in EXEMPT or ".bak." in f.name:
            continue
        try:
            src = f.read_text(errors="replace")
        except Exception:
            continue
        if not VERDICT.search(src):
            continue
        (with_p if has_proof(src) else without).append(f.name)
    return with_p, without


def main() -> int:
    with_p, without = survey()
    if "--record" in sys.argv:
        BASELINE.write_text(json.dumps({"unproven": sorted(without), "count": len(without),
                                        "note": "baseline recorded 2026-08-29; may shrink, never grow"},
                                       indent=2) + "\n")
        print(f"  baseline recorded: {len(without)} unproven detector(s)")
        return 0

    if not BASELINE.exists():
        print("  ✗ INV-114 no baseline recorded — run --record once")
        return 1

    base = json.loads(BASELINE.read_text())
    prev = set(base.get("unproven", []))
    now = set(without)
    new = sorted(now - prev)
    paid = sorted(prev - now)

    print(f"  detectors: {len(with_p)} proven, {len(without)} unproven (baseline {base['count']})")
    # ★★★ RANK THE DEBT BY WHAT RESTS ON IT. A flat list invites paying down the easy ones. Ranked, the
    #   top of this list was blockers.py — read by the invariant runner, the panel, the board and the
    #   session hook — and giving THAT one a control immediately exposed a live FALSE CLEAR: its exit
    #   code counted hand-written rows while its display rendered the merged set, so with the hand rows
    #   closed it returned 0 ("board clear") with 48 items open on screen. The ordering was the finding.
    if without:
        readers = {"invariant runner": HERE.parent / "check_invariants.sh",
                   "panel": HERE.parent / "panel_server.py",
                   "board": HERE / "blockers.py",
                   "session hook": HERE / "cc_session_hook.py"}
        texts = {}
        for label, path in readers.items():
            try:
                texts[label] = path.read_text(errors="replace")
            except Exception:
                texts[label] = ""
        ranked = []
        for n in without:
            deps = [lbl for lbl, t in texts.items() if n[:-3] in t and n != "blockers.py" or
                    (n == "blockers.py" and n[:-3] in t and lbl != "board")]
            ranked.append((len(deps), n, deps))
        ranked.sort(key=lambda r: (-r[0], r[1]))
        print("  standing debt, ranked by what reads it:")
        for cnt, n, deps in ranked:
            print(f"      {cnt}  {n:24s} {', '.join(deps) if deps else '— nothing reads it'}")
    if paid:
        print(f"  ✓ debt paid down: {', '.join(paid)}")
    if new:
        print(f"  ✗ INV-114 FAIL — {len(new)} detector(s) added with NO negative control:")
        for n in new:
            print(f"        {n} — add a self_test() that constructs input it MUST reject")
        print("    A detector that has only ever returned PASS has not been tested.")
        return 1
    if not with_p:
        # ★ Liveness: if the proof pattern matches nothing at all, the survey is broken, not clean.
        print("  ✗ INV-114 saw ZERO proven detectors — the pattern matches nothing, so this run")
        print("    examined an empty population. That is a broken survey, not a pass.")
        return 1
    print("  ✓ INV-114 no new detector without a negative control")
    return 0


if __name__ == "__main__":
    sys.exit(main())
