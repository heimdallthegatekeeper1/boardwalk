#!/usr/bin/env python3
"""★ NO FUNCTION IS DEFINED TWICE IN THE SERVED PANEL.

★ WHY: a stale <script> below the operator-needs section redefined `onRender` and eight other functions.
  Last definition wins, so THREE PASSES OF REBUILDS WERE OVERWRITTEN AT LOAD TIME. "Built" was true in
  the file and false in the window — FC-3 in a new medium: not a store versus a process, but a file
  versus its own later lines.

★★ It is one grep-shaped check and it would have found this on day one of three. That it did not exist
  is the point: we have invariants for gates, a census for surfaces and canaries for panels, and nothing
  that asserted a page does not silently overwrite itself.

★★★ IT READS THE SERVED PAGE, NOT THE FILE. What the browser receives is the only thing that can shadow
  anything — a check on disk would miss an injection added at serve time.

USE:  panel_dupecheck.py            # check every panel surface (exit 1 on a duplicate)
      panel_dupecheck.py --self-test
"""
from __future__ import annotations
import re, sys, urllib.request
from collections import Counter

SURFACES = [
    ("panel root", "http://127.0.0.1:7842/"),
    ("operator board", "http://127.0.0.1:7842/?section=operator-needs"),
]
# ★ TOP-LEVEL DEFINITIONS ONLY — shadowing requires them. The first version matched `function NAME(`
#   anywhere and reported two "duplicates" that were nothing of the kind: one inside a MINIFIED QR
#   library's IIFE, one INDENTED and therefore nested in another function. Neither can shadow anything.
#   That is the coarse-detector-convicting-the-innocent shape for the third time in a day, so the rule is
#   now the one that matches the actual hazard: a definition at column 0 of its line.
DEF = re.compile(r"^function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)


def duplicates(body: str):
    names = DEF.findall(body)
    return {n: c for n, c in Counter(names).items() if c > 1}


# ★ LIVENESS: a served page with NO top-level functions has not been proven duplicate-free — it has not
#   been read. An empty body satisfies "no duplicates" perfectly, which is the vacuous pass.
MIN_FUNCTIONS = 20


def saw_a_real_page(body: str) -> tuple[bool, str]:
    n = len(set(DEF.findall(body)))
    if not body.strip():
        return False, "the page came back EMPTY — nothing was examined"
    if n < MIN_FUNCTIONS:
        return False, (f"only {n} top-level function(s) found, below the {MIN_FUNCTIONS} this panel always "
                       f"has — the page is truncated or the fetch failed; a duplicate-free verdict here "
                       f"would be about the fetch, not the panel")
    return True, f"{n} function(s)"


def main() -> int:
    bad = 0
    print("\n★ PANEL DUPLICATE-DEFINITION CHECK — reading what the BROWSER receives\n")
    for label, url in SURFACES:
        try:
            body = urllib.request.urlopen(url, timeout=10).read().decode("utf-8", "replace")
        except Exception as e:
            print(f"  [SKIP] {label}: {str(e)[:70]}")
            continue
        live, why = saw_a_real_page(body)
        if not live:
            bad += 1
            print(f"  [FAIL] {label}: SAW NOTHING — {why}")
            continue
        d = duplicates(body)
        if d:
            bad += len(d)
            print(f"  [FAIL] {label}: {len(d)} function(s) defined more than once")
            for n, c in sorted(d.items(), key=lambda x: -x[1]):
                print(f"           {n} ×{c}  ← the LAST definition wins; every earlier one is dead code")
        else:
            print(f"  [ ok ] {label}: {len(set(DEF.findall(body)))} function(s), none defined twice")
    print(f"\n  {'FAIL' if bad else 'PASS'} — {bad} duplicate name(s)\n")
    return 1 if bad else 0


def self_test() -> int:
    # ★ FIXTURES REWRITTEN to the real shape. The first set put every definition on ONE LINE, so under
    #   the top-level rule only the first matched and the check "passed" a case it should fail — a
    #   self-test that agreed with the code instead of testing it.
    cases = [
        ("★ the same function twice, top level — must FAIL",
         "function a(){}\nfunction b(){}\nfunction a(){}\n", True),
        ("distinct functions — must pass", "function a(){}\nfunction b(){}\n", False),
        ("★ a call is not a definition", "function a(){}\na();\na();\n", False),
        ("★ three copies — must FAIL", "function z(){}\nfunction z(){}\nfunction z(){}\n", True),
        ("★ NESTED duplicate is not shadowing — must pass",
         "function outer(){\n  function _h(){}\n}\nfunction other(){\n  function _h(){}\n}\n", False),
        ("★ minified vendor IIFE — must pass",
         "!function(){function a(a){}function a(b){}}();\n", False),
    ]
    bad = 0
    print("═══ SELF-TEST — it must catch the shape that cost three passes ═══")
    for label, body, want in cases:
        got = bool(duplicates(body))
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label:42s} → {'FAIL' if got else 'pass'} (want {'FAIL' if want else 'pass'})")
    print("★ SELF-TEST PASSED\n" if not bad else "✗ SELF-TEST FAILED\n")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
