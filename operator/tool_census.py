#!/usr/bin/env python3
"""★ THE TOOL CENSUS — every guard, gate, checker and probe, and whether anything actually CALLS it.

★ WHY IT EXISTS: this session named "a checker nothing calls" FOUR TIMES — session_start.sh, ctx_meter.py,
  pretool_guard.py, and very nearly the report identity check — while the detector for that exact class sat
  specified and unbuilt. Diagnosing a disease repeatedly is not the same as owning the test for it.

★★ IT FLAGS TWO DIFFERENT DISEASES, and conflating them would hide the second:
  · NO CALLER      — the tool exists and nothing invokes it. It cannot fire, so its silence means nothing.
  · NO PATH TO GO  — the tool can only ever REFUSE. A preflight that has no way to say yes is one nobody
                     wires in, so it becomes a no-caller tool for a reason the first check cannot see.

★★★ IT REPORTS; IT DOES NOT CONVICT. A tool may be legitimately manual, and this cannot tell that apart
  from abandoned — so it names what it found and leaves the judgement, rather than inventing a threshold.

USE:  tool_census.py            # the census
      tool_census.py --json
"""
from __future__ import annotations
import json, pathlib, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLISH = re.compile(r"(guard|gate|check|assert|probe|sentinel|verify|audit|census|doctor|lens)", re.I)
# ★ venv-ml et al. added after the first run reported 107 "uncalled tools", most of them numpy and
#   protobuf. A census that counts third-party library files as OUR abandoned guards is the coarse
#   instrument convicting the innocent — the same shape twice in one day.
SKIP_DIRS = {"node_modules", ".git", "agent_archive", "reports", "__pycache__", "STALE_", "knowledge",
             "venv-ml", "venv", ".venv", "site-packages", "dist", "build", "android_app"}
GO_WORDS = re.compile(r"\b(PASS|ALLOW|GREEN|OK\b|ok.?:.?True|return 0|✓)", re.I)
NO_WORDS = re.compile(r"\b(FAIL|REFUSE|DENY|RED|BLOCK|✗)", re.I)


def candidates():
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith("STALE_") for part in rel.parts):
            continue
        if TOOLISH.search(p.stem):
            out.append(p)
    return out


def build_reference_index(names):
    """★ ONE PASS, NOT ONE PASS PER TOOL. The first version ran a tree-wide grep for every tool and did
    not finish — O(tools x tree). This reads each plausible CALLER once and asks which tool names it
    mentions, which is the same question asked the cheap way."""
    idx = {n: [] for n in names}
    caller_files = []
    for pat in ("*.py", "*.sh", "*.json", "*.md"):
        caller_files.extend(ROOT.rglob(pat))
    extra = pathlib.Path.home() / ".claude" / "settings.json"
    if extra.exists():
        caller_files.append(extra)
    for f in caller_files:
        rel = str(f)
        if any(s in rel for s in ("/reports/", "/agent_archive/", "__pycache__", "/node_modules/",
                                  ".bak", "/knowledge/", "/STALE_")):
            continue
        try:
            body = f.read_text(errors="replace")
        except Exception:
            continue
        for n in names:
            if n in body and f.name != n:
                idx[n].append(rel.replace(str(pathlib.Path.home()), "~"))
    return idx


def main() -> int:
    cands = candidates()
    idx = build_reference_index([c.name for c in cands])
    rows = []
    for p in cands:
        body = p.read_text(errors="replace")
        c = idx.get(p.name, [])
        can_go = bool(GO_WORDS.search(body))
        can_no = bool(NO_WORDS.search(body))
        rows.append({
            "tool": str(p.relative_to(ROOT)),
            "callers": c,
            "no_caller": len(c) == 0,
            "no_path_to_go": can_no and not can_go,
        })

    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1)); return 0

    nc = [r for r in rows if r["no_caller"]]
    ng = [r for r in rows if r["no_path_to_go"]]
    print(f"\n★ TOOL CENSUS — {len(rows)} guard/gate/checker/probe file(s)\n")
    print(f"  ★ NO CALLER — nothing in the tree invokes these, so their silence means nothing ({len(nc)}):")
    for r in nc:
        print(f"      {r['tool']}")
    if not nc:
        print("      (none)")
    print(f"\n  ★ NO PATH TO GO — can refuse, never approve; a preflight nobody wires in ({len(ng)}):")
    for r in ng:
        print(f"      {r['tool']}")
    if not ng:
        print("      (none)")
    wired = [r for r in rows if not r["no_caller"]]
    print(f"\n  wired: {len(wired)}/{len(rows)}")
    print("\n  ★ This REPORTS; it does not convict. A tool may be legitimately manual, and this cannot")
    print("    tell manual from abandoned — that judgement is a human's.\n")
    return 1 if nc else 0


if __name__ == "__main__":
    sys.exit(main())
