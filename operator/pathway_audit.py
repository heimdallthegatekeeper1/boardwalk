#!/usr/bin/env python3
"""pathway_audit.py — for every capability, WHICH WAYS can it be reached?

★★★ THE OPERATOR'S CRITERION (2026-09-01): "Anything the AI can do, the user should also be able to do
    with ease" — and the project is still leaning agent-heavy, where a capability exists as a script an
    LLM knows to run and nowhere else. This file makes that measurable instead of remembered.

★★ FOUR PATHWAYS, and a capability is only really BUILT when the ones it needs are present:
      agent    — an agent can invoke it (an importable module, an endpoint, a documented command)
      cli      — the operator can run it himself in a terminal (it has a main() and prints something)
      gui      — there is a control in the panel that reaches it, WITHOUT typing a command
      launcher — a desktop entry or deep link, so it is reachable without knowing the panel exists
    ★ Not every capability needs all four. A negative control does not need a desktop entry. But the
      ABSENCE should be a decision someone made, not an accident nobody noticed — which is the whole
      difference this file is trying to create.

★ HOW IT MEASURES, AND WHERE THAT IS WEAK — stated because a grep-based audit can flatter itself:
    cli      = the module defines main() and is executable directly. Strong.
    agent    = a panel route reaches it, or it is importable. Strong.
    gui      = the panel HTML mentions the route. ★ WEAK: HTML mentioning a route is not proof that a
               control is visible, reachable, or wired to anything. It can only say the wiring EXISTS,
               never that a person can find it. Treated as `wired`, never as `usable`.
    launcher = a .desktop file or a documented deep link points at it. Strong.
  The gui weakness is the reason this reports `wired` rather than `works`, and why a human still has to
  press the thing — this audit narrows where to look; it does not replace looking.
"""
from __future__ import annotations
import json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PANEL_PY = ROOT / "panel_server.py"
PANEL_HTML = ROOT / "aionui-stack-panel.html"
DESKTOP = Path.home() / ".local/share/applications"

# One row per capability we have actually built. `route` is the panel endpoint, if any.
CAPABILITIES = [
    {"id": "board-read", "label": "See what is waiting on you", "module": "blockers.py",
     "route": "/operator/board.json", "needs": ["agent", "cli", "gui", "launcher"]},
    {"id": "board-rule", "label": "Rule on an item (done / later / reopen)", "module": "blockers.py",
     "route": "/operator/set", "needs": ["agent", "cli", "gui"]},
    {"id": "board-add", "label": "Add an item to the board", "module": "blockers.py",
     "route": "/operator/board/add", "needs": ["agent", "cli", "gui"]},
    {"id": "board-note", "label": "Write a note on an item", "module": "blockers.py",
     "route": "/operator/set", "needs": ["cli", "gui"]},
    {"id": "board-history", "label": "See what has happened to an item", "module": "board_history.py",
     "route": "/operator/item-history", "needs": ["agent", "cli", "gui"]},
    {"id": "board-delta", "label": "See what is new since you last looked", "module": "board_delta.py",
     "route": "/operator/delta", "needs": ["cli", "gui"]},
    {"id": "item-bind", "label": "Hand an item to an agent", "module": "item_binding.py",
     "route": "/operator/bind", "needs": ["agent", "cli", "gui"]},
    {"id": "item-send", "label": "Send an item somewhere", "module": "dispatch_targets.py",
     "route": "/operator/dispatch/send", "needs": ["agent", "cli", "gui"]},
    {"id": "handoff-drain", "label": "See and deliver queued handoffs", "module": "drain_handoffs.py",
     "route": "/operator/handoffs", "needs": ["agent", "cli", "gui"]},
    {"id": "service-check", "label": "Check whether a service is reachable", "module": "service_auth_check.py",
     "route": "/operator/services", "needs": ["agent", "cli", "gui"]},
    {"id": "service-install", "label": "Install a missing tool", "module": "service_actions.py",
     "route": "/operator/service/action", "needs": ["agent", "cli", "gui"]},
    {"id": "service-credential", "label": "Store a credential", "module": "service_actions.py",
     "route": "/operator/service/action", "needs": ["agent", "cli", "gui"]},
    {"id": "agent-browser", "label": "See where the agent browser's login lives", "module": "agent_browser.py",
     "route": "/operator/browser", "needs": ["agent", "cli", "gui"]},
    {"id": "verdict-capture", "label": "Record your verdict on a test path", "module": "him_verify.py",
     "route": "/operator/verify", "needs": ["cli", "gui"]},
    {"id": "guided-run", "label": "Work through the logins in bursts", "module": "credential_wizard.py",
     "route": "/operator/wizard", "needs": ["agent", "cli", "gui"]},
    {"id": "credential-advice", "label": "Read the agent's advice on a credential", "module": "credential_wizard.py",
     "route": "/operator/wizard", "needs": ["agent", "cli", "gui"]},
    {"id": "speak", "label": "Have an item read aloud", "module": "voice_log.py",
     "route": "/voice/speak", "needs": ["agent", "gui"]},
]


def _text(p: Path) -> str:
    try:
        return p.read_text(errors="replace")
    except Exception:
        return ""


def audit() -> dict:
    html = _text(PANEL_HTML)
    pys = _text(PANEL_PY)
    desktop_blob = ""
    try:
        for f in DESKTOP.glob("*.desktop"):
            desktop_blob += _text(f)
    except Exception:
        pass

    rows = []
    for c in CAPABILITIES:
        have = {}
        mod = HERE / c["module"] if c.get("module") else None
        src = _text(mod) if mod and mod.exists() else ""
        have["cli"] = bool(src) and "def main(" in src and "__main__" in src
        have["agent"] = bool(src) and (bool(c["route"]) and c["route"] in pys or "def " in src)
        have["gui"] = bool(c["route"]) and c["route"] in html
        # ★ MY FIRST LAUNCHER CHECK CONVICTED THE INNOCENT. It looked for the ROUTE inside .desktop
        #   files — but a launcher runs a SCRIPT, not a URL, so two working desktop entries ("Operator
        #   Board", "Operator Needs Me") were reported as a missing pathway. The detector was wrong,
        #   not the launchers. It now counts a launcher when a desktop entry reaches the capability by
        #   ANY of the routes it actually could: the route, the module, or a launcher script that
        #   invokes either.
        launch_hits = [c["route"] or "\x00", c.get("module") or "\x00"]
        have["launcher"] = any(x in desktop_blob for x in launch_hits if x != "\x00")
        if not have["launcher"] and desktop_blob:
            # a wrapper script counts if the script itself reaches the module
            for line in desktop_blob.splitlines():
                if not line.startswith("Exec="):
                    continue
                for tok in line[5:].split():
                    q = Path(tok)
                    if q.exists() and q.is_file():
                        body = _text(q)
                        if (c.get("module") and c["module"] in body) or (c["route"] and c["route"] in body):
                            have["launcher"] = True
                            break
                if have["launcher"]:
                    break
        missing = [p for p in c["needs"] if not have.get(p)]
        rows.append({**c, "have": have, "missing": missing,
                     "complete": not missing})
    counts = {"complete": sum(1 for r in rows if r["complete"]),
              "incomplete": sum(1 for r in rows if not r["complete"])}
    gaps = {}
    for r in rows:
        for m in r["missing"]:
            gaps[m] = gaps.get(m, 0) + 1
    return {"capabilities": rows, "counts": counts, "gaps_by_pathway": gaps,
            "caveat": ("`gui` means the panel HTML mentions the route — the wiring exists. It is NOT "
                       "proof a control is visible or that a person can find it. Only pressing it "
                       "proves that.")}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is an audit that says everything is fine — so it
    must be able to report a gap, and must not credit a pathway for a capability whose module is
    absent."""
    fails = []
    d = audit()
    if not d["capabilities"]:
        fails.append("no capabilities audited — an empty population proves nothing")
    saved = CAPABILITIES[:]
    try:
        CAPABILITIES.clear()
        CAPABILITIES.append({"id": "ghost", "label": "module does not exist",
                             "module": "no_such_module_xyz.py", "route": "/no/such/route",
                             "needs": ["agent", "cli", "gui", "launcher"]})
        g = audit()["capabilities"][0]
        if g["complete"]:
            fails.append("a capability with NO module and NO route was reported complete — the audit "
                         "cannot say no, so its yes means nothing")
        if any(g["have"].values()):
            fails.append("a pathway was credited for a capability whose module does not exist")
    finally:
        CAPABILITIES.clear(); CAPABILITIES.extend(saved)
    if "NOT proof" not in d["caveat"]:
        fails.append("the audit does not state the weakness of its own gui check")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ pathway_audit self_test: reports a gap for a capability that does not exist, credits no "
          "pathway without evidence, and states its own gui weakness"
          if not fails else "  ✗ pathway_audit self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    d = audit()
    if "--json" in sys.argv:
        print(json.dumps(d, indent=2)); return 0
    print("\n  PATHWAY COVERAGE — how many ways can each capability be reached?")
    print("     agent · cli · gui · launcher          (✓ present   — missing but needed   · not needed)")
    for r in d["capabilities"]:
        cells = ""
        for p in ("agent", "cli", "gui", "launcher"):
            if p not in r["needs"]:
                cells += " · "
            elif r["have"].get(p):
                cells += " ✓ "
            else:
                cells += " — "
        print(f"    {cells}  {r['label']}")
        if r["missing"]:
            print(f"{'':>18}★ missing: {', '.join(r['missing'])}")
    c = d["counts"]
    print(f"\n  {c['complete']} complete · {c['incomplete']} with a gap")
    print(f"  gaps by pathway: {d['gaps_by_pathway']}")
    print(f"  ★ {d['caveat']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
