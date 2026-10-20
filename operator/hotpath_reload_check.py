#!/usr/bin/env python3
"""hotpath_reload_check.py — INV-119's instrument: no module RE-EXECUTION on a request hot path.

★★★ THIS EXISTS BECAUSE THE SHELL ONE-LINER IT REPLACES PASSED FOR THE WRONG REASON.
    The check was `awk '/def _op_mod/,/^    def [a-z_]+\\(self/'`. Both patterns match the SAME
    line, so the range opened and closed on `def _op_mod(...)` and captured exactly one line —
    the body was never inspected. A deliberately planted `importlib.reload` inside _op_mod was
    NOT detected, while the invariant reported PASS.
    ★ It was caught only by a negative control that verified its own plant had landed first.
      A control that plants nothing reports a false clean, which is the same conflation as
      "could not look" vs "it is fine" — the error this house has now paid for repeatedly.
    Parsing by indentation is exact; a line-range regex over code is not.
"""
from __future__ import annotations
import re, sys
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent / "panel_server.py"
DERIVE = Path(__file__).resolve().parent.parent / "operator" / "derive_items.py"
HOT_FUNCS = ("_op_mod", "_board_payload")
BAD = re.compile(r"importlib\.reload|exec_module")


def _strip(line: str) -> str:
    return line.split("#", 1)[0]


def body(src: str, name: str) -> list[str]:
    """The function's body, by INDENTATION — not by a line range."""
    lines = src.split("\n")
    out, indent = [], None
    for i, ln in enumerate(lines):
        if indent is None:
            m = re.match(r"^(\s*)def\s+" + re.escape(name) + r"\s*\(", ln)
            if m:
                indent = len(m.group(1))
            continue
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            break                      # dedented to the next sibling -> body ended
        out.append(ln)
    return out


def main() -> int:
    findings = []
    psrc = PANEL.read_text(encoding="utf-8", errors="replace")
    for fn in HOT_FUNCS:
        b = body(psrc, fn)
        if not b:
            findings.append(f"{fn}: NOT FOUND in panel_server.py (checker is stale)")
            continue
        hits = [l.strip()[:70] for l in b if BAD.search(_strip(l))]
        findings += [f"panel_server.{fn}: {h}" for h in hits]

    dsrc = DERIVE.read_text(encoding="utf-8", errors="replace")
    findings += [f"derive_items: {l.strip()[:70]}"
                 for l in dsrc.split("\n") if BAD.search(_strip(l))]

    cold = sum(1 for l in psrc.split("\n") if BAD.search(_strip(l)))
    if findings:
        print("FAIL: " + "; ".join(findings[:3]))
        return 1
    print(f"OK: hot paths clean ({', '.join(HOT_FUNCS)}, derive_items); "
          f"{cold} re-exec site(s) remain on cold paths")
    return 0


if __name__ == "__main__":
    # ★ Same rule: this checker takes NO arguments, so any argument means the caller believes it is
    #   doing something it is not. Silence there is how a suite reports a pass it never ran.
    if sys.argv[1:]:
        print(f"unknown argument(s): {' '.join(sys.argv[1:])}\n"
              f"usage: hotpath_reload_check.py   (no arguments)", file=sys.stderr)
        sys.exit(2)
    sys.exit(main())
