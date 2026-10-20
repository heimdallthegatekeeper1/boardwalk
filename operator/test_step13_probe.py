#!/usr/bin/env python3
"""test_step13_probe.py -- STEP 13: THE PROBE MUST MEASURE, NOT REMEMBER.

MODEL/MODE: see the lane report. This suite guards the claim the 2026-10-12 step-13 lane made:

  G1  IT MEASURES THE GATE. `probe_value_substitution`'s own AST calls the round-trip driver and
      does NOT read `scrubber.py`. ★ THE DEFECT THIS EXISTS FOR: the shipped probe used to grep
      `scrubber.py` for the ABSENCE of substitution, so it could not notice the capability arriving
      in `jarvis/privacy_gate.py` -- and it kept reporting "not implemented" after it WAS.
  G2  THE ROUND TRIP IS REALLY DRIVEN. The gate's public API produces placeholders, keeps BOTH real
      values out of the sent text, and restores BOTH on the way back.
  G3  THE ROW IS DONE AND NAMES ITS RUNG. On this healthy box the probe answers DONE and its
      evidence says which rung it reached.
  G4  IT DOES NOT OVERCLAIM. The evidence carries the explicit not-measured clause and never says
      "end to end".
  G5  THE WIRING COUNT IS AN AST COUNT. ★ PROVE-FAIL TARGET FOR A BUG THIS LANE SHIPPED AND CAUGHT:
      the first version counted LINES containing the call and returned 7 where there are 6, because
      a COMMENT in `router.py` mentions it. A comment must not be able to vote.
  G6  GATE ABSENT -> STILL-BLOCKED-LATER, and the row says absent.
  G7  A LEAKING GATE -> BLOCKED. Driven end to end against a FAKE gate that substitutes AND leaks
      the real name: the probe must refuse to call that a pass.
  G8  NOTHING CALLS IT -> BLOCKED. A gate no egress path calls is a library, not a gate.
  G9  IT STAYS INSIDE ITS CHARTER. No `pgrep -f`/`pkill -f` anywhere in the probe code, and the
      driver's fabricated values are `example.invalid` -- the operator's real values are never in a
      measurement that gets printed.
  G10 THE ROW IS HONEST AND COMPLETE. `status` is in the vocabulary and no longer DESIGN-ONLY, the
      `why` no longer says NOT IMPLEMENTED, and both approval paths survive.
  G11 THE STT BACKEND IS READ FROM WHERE IT LIVES. `/state.asr_backend` is None on this box; the
      live value is at `/state.phrases.asr_backend`. Reading the wrong place does not raise -- it
      falls back to `'?'`, a shape that looks like an answer. Same class as the step-13 defect.

EVERY GUARD HAS A PROVE-FAIL IN `--prove-fail`, AND THE MUTATIONS ARE APPLIED TO THE SHIPPED FILE
rather than to copies, each one restored and md5-verified (AGENTS.md 2: a guard never tested against
its own bypass is a guard we are hoping about).

Usage:
    python3 operator/test_step13_probe.py --self-test      exit 0 green, 1 red
    python3 operator/test_step13_probe.py --prove-fail     the mutations, exit 0 iff all reddened
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import pathlib
import re
import signal
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
SHIPPED = HERE / "setup_blockers.py"
sys.path.insert(0, str(HERE))

GUARDS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11")

# ★ A GATE THAT SUBSTITUTES **AND LEAKS**. It is deliberately well-formed enough to pass every
#   check EXCEPT the one that matters, so the leak guard cannot be satisfied by accident.
_BROKEN_GATE = '''
NAME, MAIL = "Probe Person", "operator@example.invalid"
class _L:
    def __init__(self, sent, mp):
        self.sent_text = sent
        self._map = mp
        self.action = "delegate_sanitised"
        self.verdict = "obfuscate"
    def rehydrate(self, text):
        s = text
        for k, v in self._map.items():
            s = s.replace(k, v)
        return {"text": s, "restored": len(self._map), "unrestored": 0}
def gate(text, destination, local=None, **kw):
    mp = {"[[PERSON_a1b2c3d4]]": NAME, "[[EMAIL_e5f6a7b8]]": MAIL}
    sent = "Ask [[PERSON_a1b2c3d4]] at [[EMAIL_e5f6a7b8]] to call the clinic -- LEAK: " + NAME
    return _L(sent, mp)
'''


def _fresh(name="setup_blockers"):
    """Import the SHIPPED module fresh, so a mutation on disk is what gets measured."""
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


class _Swap:
    """Point a module global at a fixture for the duration of a block."""

    def __init__(self, sb, attr, value):
        self.sb, self.attr, self.value = sb, attr, value

    def __enter__(self):
        self.old = getattr(self.sb, self.attr)
        setattr(self.sb, self.attr, self.value)
        return self

    def __exit__(self, *a):
        setattr(self.sb, self.attr, self.old)
        return False


def _tmpfile(text, suffix=".py"):
    fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    fh.write(text)
    fh.close()
    return pathlib.Path(fh.name)


def _probe2(sb) -> tuple:
    """Run the shipped probe the way the WALK's runner does. ★ THIS HELPER EXISTS BECAUSE OF A REAL
    CRASH: the first `--prove-fail` mutated the probe back to the remembered-grep, the mutant built
    its evidence from an empty facts dict, and the KeyError escaped `run_guards` and killed the whole
    suite before it could report ANY guard. A suite that dies on the thing it is judging reports
    nothing. `setup_blockers._probe_state` already answers a raising probe with UNKNOWN (invariant
    I5); this is the same rule on the test's side of the seam."""
    try:
        r = sb.probe_value_substitution()
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"probe raised {type(e).__name__}: {e}"
    if isinstance(r, tuple) and len(r) >= 2:
        return r[0], r[1]
    return "UNKNOWN", str(r)


# ── the guards ───────────────────────────────────────────────────────────────────────────────────
def run_guards() -> list:
    fails = []
    sb = _fresh()
    src = SHIPPED.read_text(encoding="utf-8")

    # -- G1 --------------------------------------------------------------------------------------
    fn = next((n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.FunctionDef) and n.name == "probe_value_substitution"), None)
    if fn is None:
        fails.append("G1 probe_value_substitution is GONE from the shipped file")
    else:
        called = {n.func.id for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "_drive_gate_round_trip" not in called:
            fails.append("G1 the probe does NOT call `_drive_gate_round_trip` -- it is not driving "
                         "the gate, so it cannot notice the gate changing")
        names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        if "scrubber" in names:
            fails.append("G1 the probe still reaches for `scrubber.py` -- the remembered-grep is "
                         "back, and that is the exact shape that lied for hours")

    # -- G2 --------------------------------------------------------------------------------------
    ok, facts = sb._drive_gate_round_trip()
    if not ok:
        fails.append(f"G2 the round trip did NOT pass on this box: {facts}")
    else:
        for k, want in (("substitutions", None), ("name_left_in_sent", False),
                        ("mail_left_in_sent", False), ("placeholder_in_sent", True),
                        ("name_restored", True), ("mail_restored", True)):
            got = facts.get(k)
            if k == "substitutions":
                if not isinstance(got, int) or got < 2:
                    fails.append(f"G2 expected at least 2 substitutions, measured {got!r}")
            elif got != want:
                fails.append(f"G2 {k} measured {got!r}, expected {want!r}")

    # -- G3 / G4 ---------------------------------------------------------------------------------
    state, ev = _probe2(sb)
    if state != "DONE":
        fails.append(f"G3 the probe answers {state!r} on a box where the gate exists and its round "
                     f"trip passes -- the row would understate what is built")
    if "rung WORKS" not in ev:
        fails.append("G3 the evidence does not NAME the rung it reached")
    if "WHAT THIS DOES NOT MEASURE" not in ev:
        fails.append("G4 the evidence carries no not-measured clause -- an unqualified claim")
    if "USABLE is NOT reached" not in ev:
        fails.append("G4 the evidence does not say which rung was NOT reached")
    for bad in ("end to end", "end-to-end", "verified end"):
        if bad in ev.lower():
            fails.append(f"G4 the evidence claims {bad!r} -- the gate lane was explicit that no "
                         f"turn reached a real cloud agent and none went through the daemon HTTP")

    # -- G5 --------------------------------------------------------------------------------------
    comment_only = _tmpfile('x = 1\n# the shape every egress uses is _gate_egress(text, dest)\n')
    real_call = _tmpfile('def f():\n    return _gate_egress("t", "d")\n')
    try:
        with _Swap(sb, "ROUTER_PY", comment_only):
            got = sb._egress_call_sites()
        if got != 0:
            fails.append(f"G5 a COMMENT mentioning the call counted as {got} call site(s) -- a "
                         f"comment must not be able to vote")
        with _Swap(sb, "ROUTER_PY", real_call):
            got = sb._egress_call_sites()
        if got != 1:
            fails.append(f"G5 one real call counted as {got}, expected 1")
    finally:
        comment_only.unlink(missing_ok=True)
        real_call.unlink(missing_ok=True)

    # -- G6 --------------------------------------------------------------------------------------
    with _Swap(sb, "GATE_PY", pathlib.Path("/nonexistent/privacy_gate.py")):
        st6, ev6 = _probe2(sb)
    if st6 != "STILL-BLOCKED-LATER":
        fails.append(f"G6 with the gate ABSENT the probe answers {st6!r}, expected "
                     f"STILL-BLOCKED-LATER")
    if "absent" not in ev6.lower():
        fails.append("G6 the absent-gate evidence does not say the gate is absent")

    # -- G7 -- a gate that substitutes AND leaks -----------------------------------------------
    broken = _tmpfile(_BROKEN_GATE)
    try:
        with _Swap(sb, "GATE_PY", broken):
            ok7, why7 = sb._drive_gate_round_trip()
            st7, _ = _probe2(sb)
        if ok7:
            fails.append("G7 a gate that LEAKS the real name into the sent text was reported as a "
                         "passing round trip -- that is the leak this whole step exists to prevent")
        if st7 != "BLOCKED":
            fails.append(f"G7 a leaking gate produced {st7!r}, expected BLOCKED")
    finally:
        broken.unlink(missing_ok=True)

    # -- G8 --------------------------------------------------------------------------------------
    silent = _tmpfile('def f():\n    return "no gate call anywhere here"\n')
    try:
        with _Swap(sb, "ROUTER_PY", silent):
            st8, ev8 = _probe2(sb)
        if st8 != "BLOCKED":
            fails.append(f"G8 a gate that NOTHING CALLS produced {st8!r}, expected BLOCKED")
        if "NOTHING CALLS IT" not in ev8:
            fails.append("G8 the uncalled-gate evidence does not say nothing calls it")
    finally:
        silent.unlink(missing_ok=True)

    # -- G9 --------------------------------------------------------------------------------------
    head = src.split("def probe_secrets_keeper", 1)[0]
    # ★★★ A COMMENT MUST NOT BE ABLE TO FAIL THIS EITHER. The first version of this guard was a TEXT
    #   scan for the strings and it went RED on the probe's OWN docstring -- which explains that
    #   `pgrep -f` is banned on this box. A guard a comment can trip is the same defect as a guard a
    #   comment can satisfy, seen from the other side (the whole reason `privacy_gate.self_test`
    #   drops docstrings before its AST check). So this asks what the code EXECUTES: it looks for the
    #   words inside the argv literal handed to a subprocess runner, where a comment cannot reach.
    banned_calls = []
    for node in ast.walk(ast.parse(head)):
        if isinstance(node, ast.Call):
            fn = node.func
            nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if nm in ("_run", "run", "Popen", "check_output", "call"):
                for lit in [n.value for n in ast.walk(node)
                            if isinstance(n, ast.Constant) and isinstance(n.value, str)]:
                    if "pgrep" in lit or "pkill" in lit:
                        banned_calls.append(lit[:40])
    if banned_calls:
        fails.append(f"G9 the probe EXECUTES a banned process matcher: {banned_calls} -- banned on "
                     f"this box (five shells died to a pattern matching their own command line)")
    if "example.invalid" not in src:
        fails.append("G9 the round-trip driver does not use reserved example values -- a probe "
                     "that fabricates real-looking values can print one")
    if "import hashlib" not in src or "sha256" not in src:
        fails.append("G9 the fingerprint helper is gone; the row can no longer name WHICH build "
                     "it measured")

    # -- G10 -------------------------------------------------------------------------------------
    row = next((s for s in sb.STEPS if s.get("id") == "value_substitution"), None)
    if row is None:
        fails.append("G10 the value_substitution step is GONE from STEPS")
    else:
        if row.get("status") not in sb.STATUSES:
            fails.append(f"G10 status {row.get('status')!r} is outside the vocabulary")
        if row.get("status") == "DESIGN-ONLY":
            fails.append("G10 the row still says DESIGN-ONLY -- the vocabulary defines that as "
                         "'nothing here refuses yet', and the gate refuses on six egress paths")
        why = str(row.get("why") or "")
        if "NOT IMPLEMENTED" in why:
            fails.append("G10 the `why` still says NOT IMPLEMENTED -- that is the sentence that "
                         "became a lie when the gate landed")
        if "WORKS" not in why:
            fails.append("G10 the `why` does not name the rung")
        if not (row.get("button") or {}).get("action") or not str(row.get("typing") or "").strip():
            fails.append("G10 the row lost one of its two approval paths")
        if not str(row.get("next") or "").strip():
            fails.append("G10 the row has no `next`")

    # -- G11 -------------------------------------------------------------------------------------
    # ★★★ THE ROW MUST READ THE STT BACKEND FROM WHERE IT LIVES. `/state.asr_backend` -- the TOP
    #   level -- is **None** on this box; the live value is at `/state.phrases.asr_backend`
    #   (measured: `'auto'`). Reading the wrong place does not raise: it falls back to `'?'`, which
    #   is a shape that looks like an answer rather than a bug. That is the same defect class as the
    #   step-13 probe this lane fixed, found by the `voice_boundary` lane in THIS row.
    ok11, st11 = sb._http(sb.DAEMON + "/state")
    if not ok11:
        fails.append("G11 the daemon /state could not be read, so the STT backend row could NOT be "
                     "checked -- NOT-RUN, which is its own state and is not a pass")
    else:
        want11 = (st11.get("phrases") or {}).get("asr_backend")
        r11 = sb.probe_voice_boundary()
        ev11 = r11[1] if isinstance(r11, tuple) and len(r11) >= 2 else str(r11)
        if not want11:
            fails.append("G11 the box reports no `phrases.asr_backend` at all, so this guard cannot "
                         "judge the row -- it is NOT passing it")
        elif f"asr backend {want11!r}" not in ev11:
            fails.append(f"G11 the voice_boundary row does not report the LIVE STT backend "
                         f"({want11!r}); it is reading it from the wrong place and falling back")
    fails.extend(sb._fails_for(sb.STEPS))
    return fails


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  PROVE-FAIL -- ON THE SHIPPED FILE, RESTORED AND md5-VERIFIED
# ══════════════════════════════════════════════════════════════════════════════════════════════════
def _md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


# ★★★ MEASURED 2026-10-12: `--prove-fail` was killed by a timeout BETWEEN a mutation and its
#     restore. It left a mutation on disk. So: the original bytes are captured first, a signal
#     handler restores on SIGINT/SIGTERM, and the `.bak` is removed only after a byte-identical
#     restore is verified.
_ORIGINAL: bytes | None = None
_BAK: pathlib.Path | None = None


def _restore_all() -> None:
    global _ORIGINAL
    if _ORIGINAL is not None and SHIPPED.exists():
        SHIPPED.write_bytes(_ORIGINAL)
    if _BAK is not None and _BAK.exists():
        _BAK.unlink()


# (name, old, new, the guard this mutation is EXPECTED to redden)
MUTATIONS = (
    ("the remembered-grep comes back", "G1",
     "    ok, facts = _drive_gate_round_trip()",
     '    ok, facts = (REPO / "scrubber.py").is_file(), {}'),
    ("the wiring count goes back to counting LINES", "G5",
     "        tree = ast.parse(ROUTER_PY.read_text(errors=\"replace\"))",
     "        return len([l for l in ROUTER_PY.read_text(errors=\"replace\").splitlines()\n"
     "                    if \"_gate_egress(\" in l])"),
    ("the row goes back to DESIGN-ONLY", "G10",
     '        "status": "ENFORCING",\n        "owner": "system",\n'
     '        "probe": probe_value_substitution,',
     '        "status": "DESIGN-ONLY",\n        "owner": "system",\n'
     '        "probe": probe_value_substitution,'),
    ("the why goes back to NOT IMPLEMENTED", "G10",
     '        "why": ("★ THIS IS IMPLEMENTED NOW, AND THE ROW SAYS SO PLAINLY',
     '        "why": ("★ THIS IS NOT IMPLEMENTED AND THE ROW SAYS SO PLAINLY'),
    ("the not-measured clause is dropped", "G4",
     '        "★ WHAT THIS DOES NOT MEASURE: no turn has been sent to a real cloud agent and no gated "',
     '        "★ measured on this box "'),
    ("the leak check is neutered", "G7",
     '    if facts.get("name_left_in_sent") or facts.get("mail_left_in_sent"):',
     '    if False:  # MUTANT'),
    ("a banned process matcher is EXECUTED", "G9",
     '    rc, out, _ = _run(["ss", "-ltn"])',
     '    rc, out, _ = _run(["pgrep", "-f", "daemon.py"])'),
    ("the STT backend is read from the wrong place", "G11",
     '        asr = str((st.get("phrases") or {}).get("asr_backend") or "?")',
     '        asr = str(st.get("asr_backend") or "?")'),
)


def prove_fail() -> int:
    global _ORIGINAL, _BAK
    print("=== PROVE-FAIL -- mutate the SHIPPED file, require the named guard RED, restore ===")
    _ORIGINAL = SHIPPED.read_bytes()
    md5_before = hashlib.md5(_ORIGINAL).hexdigest()
    _BAK = SHIPPED.with_suffix(".py.bak.step13provefail")
    _BAK.write_bytes(_ORIGINAL)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_a: (_restore_all(), sys.exit(130)))
        except Exception:  # noqa: BLE001
            pass
    bad = 0
    try:
        for name, expect, old, new in MUTATIONS:
            text = _ORIGINAL.decode("utf-8")
            if text.count(old) != 1:
                print(f"  SKIP  {name}: anchor appears {text.count(old)}x, expected exactly 1")
                bad += 1
                continue
            SHIPPED.write_text(text.replace(old, new, 1), encoding="utf-8")
            fails = run_guards()
            red = any(f.startswith(expect) for f in fails)
            SHIPPED.write_bytes(_ORIGINAL)
            restored = _md5(SHIPPED) == md5_before
            print(f"  {'RED' if red else 'GREEN':<5} {name}  -> expected {expect} red: "
                  f"{'yes' if red else 'NO'}  [restored "
                  f"{'byte-identical' if restored else 'DIFFERENT'}]")
            if not red:
                print(f"        failures seen: {fails[:3] or 'none at all'}")
                bad += 1
            if not restored:
                print(f"        ** THE FILE WAS NOT RESTORED: {md5_before} -> {_md5(SHIPPED)}")
                bad += 1
    finally:
        _restore_all()
    print()
    print(f"  {'INVALID' if bad else 'VALID'} -- {len(MUTATIONS) - bad}/{len(MUTATIONS)} mutations "
          f"reddened their own guard and restored their file")
    return 1 if bad else 0


def main() -> int:
    a = sys.argv[1:]
    if "--prove-fail" in a:
        return prove_fail()
    if "--self-test" in a or not a:
        fails = run_guards()
        for f in fails:
            print(f"  x {f}")
        green = len(GUARDS) - len({f[:f.index(' ')] for f in fails if " " in f})
        print(f"\n  {'FAIL' if fails else 'PASS'} -- test_step13_probe: {green}/{len(GUARDS)} "
              f"guards green")
        return 1 if fails else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
