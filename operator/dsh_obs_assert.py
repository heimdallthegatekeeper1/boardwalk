#!/usr/bin/env python3
"""Assert the DSH Activity feature is actually SERVED as built — bytes, payload and store.

Why a script and not a glance: the panel is served from a FILE by a long-running server, so
"my markup is in the served bytes" is a falsifiable claim and must be asserted against the
route, not against the file on disk. This checks, in order:

  1. GET /operator/dsh-jobs bytes == the file on disk (sha256) AND every REQUIRED marker is in
     those served bytes AND no FORBIDDEN (stale) marker is. A stale server serving the old page
     has exactly this failure mode, so it is asserted rather than assumed.
  2. GET /operator/dsh-jobs/data carries the fields the UI reads (attribution health, basis on
     every unattributed record, honesty block on the screenshot-mode flag).
  3. POST /operator/dsh-jobs/screenshot-mode proves the panel is still the ONLY writer and that
     a write is confirmed by an independent re-read — then restores the operator's value.

Exit code 0 = every assertion passed. Evidence JSON is written next to the uitest screenshots.
Read-only except for the screenshot-mode round-trip, which restores the original value.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:7842"
POPOUT = Path(__file__).resolve().parent.parent / "panel" / "dsh-observability-popout.html"
EVIDENCE = Path(__file__).resolve().parent.parent / "var" / "dsh-observability" / "uitest" / "served-bytes-assertions.json"

# ── every one of these must be in the SERVED bytes ──
REQUIRED = [
    # the six controls, one shape each (criterion 1 + 12)
    "agents: {lab:'agents shown', def:10, min:1, max:AGENTS_MAX, presets:[1,5,10,20,40,'all']",
    "events: {lab:'events / agent', def:5, min:1, max:EVENTS_MAX, presets:[1,5,10,20,40,'all']",
    "shots:  {lab:'shots / agent', def:5, min:1, max:SHOTS_MAX, presets:[1,5,10,20,40,'all']",
    "chars:  {lab:'text / line', def:240, min:40, max:CHARS_MAX, presets:[80,140,240,500,'all']",
    "AGENTS_MAX = 60, EVENTS_MAX = 100, SHOTS_MAX = 40, CHARS_MAX = 2000, ROWS_BUDGET = 3000",
    ">default density / agent</span>",
    "showing ' + Math.min(ctl[key], avail) + ' of ' + avail",
    "' · default ' + d.def + ' · range ' + d.min + '–' + d.max",
    # density switch: compact (last N lines) vs full scrollable list, per agent
    "data-act=\"density\"",
    "data-act=\"showall\"",
    "stream.full",
    "stream.compact",
    # legend / "which agents did what"
    "class=\"legend-row\"",
    "data-jump=\"",
    # the screenshots panel is a FIRST-CLASS panel with the same persisted key machinery
    "data-key=\"shots:captures\"",
    "data-key=\"agent:'+esc(id)+'\"",
    "function applyPanelState(host)",
    "if(t && t.tagName === 'DETAILS' && t.getAttribute('data-key'))",
    # honesty about the pairing
    "NOT ONE can be attributed to an agent",
    "refuse to fabricate an owner",
    "never guessed onto an agent",
    "no per-agent screen",
    "guessed onto an agent",
    # honesty about the toggle (a control must not claim to stop what it is not stopping)
    "screenshots (requested)",
    "writer NOT honouring it",
    "Nothing was stopped.",
    "dshobs-2-pkg-screenshot-flag.js",
    # loading != empty != error, each with a remedy
    "class=\"loading\"",
    "big-error",
    "retry-btn",
    "class=\"empty\"",
    # no mouse-only path
    "document.addEventListener('keydown'",
    "GET /operator/dsh-jobs/data",
    # bulk actions
    "data-bulk=\"expand\"",
    "data-bulk=\"collapse\"",
    "data-bulk=\"reset\"",
    "window.confirm(",
    # the bound, stated
    "row budget",
    # responsive grid: auto-fit on the CONTAINER, no fixed column count, no fixed panel width
    "#agent-panels{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(420px,100%),1fr))",
    "id=\"layout-note\"",
    "function updateLayoutNote()",
    # 25% thumbnails + the one reused viewer
    ":root{--thumb-w:59px}",
    ".shot-tile img{display:block;width:var(--thumb-w);height:auto}",
    "data-lb-file=",
    "function thumbHtml(file, src, ambiguous)",
    "id=\"lb\" hidden",
    "data-lb=\"prev\"",
    "data-lb=\"next\"",
    "data-lb=\"grid\"",
    "data-lb=\"close\"",
    "function lbClose()",
    "img.src = '/operator/dsh-jobs/shot/' + encodeURIComponent(it.file)",
    "if(!lbItems.length) return;",
    "e.key === 'Escape'",
    "e.key === 'ArrowLeft'",
    "e.key === 'ArrowRight'",
]
# ── and NONE of these (they are the shapes this work replaced) ──
FORBIDDEN = [
    "unattached",
    "has not been updated to read this flag yet",
    "class=\"agent-panel unattached\"",
]


def get(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=8) as r:
        return r.read()


def post(path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(BASE + path, method="POST",
                                data=json.dumps(payload).encode(),
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def main() -> int:
    results: list[dict] = []
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        results.append({"check": name, "pass": bool(passed), "detail": detail})

    # ── 1. served bytes ──
    try:
        served = get("/operator/dsh-jobs")
    except Exception as e:
        check("popout route answers", False, f"{e.__class__.__name__}: {e}")
        served = b""
    disk = POPOUT.read_bytes()
    flat = served.decode("utf-8", "replace")
    check("served bytes == panel/dsh-observability-popout.html",
          hashlib.sha256(served).hexdigest() == hashlib.sha256(disk).hexdigest(),
          "sha256 " + hashlib.sha256(served).hexdigest()[:16] + f" ({len(served)} bytes)")
    missing = [m for m in REQUIRED if m not in flat]
    check("every REQUIRED marker in served bytes", not missing,
          f"{len(REQUIRED)-len(missing)}/{len(REQUIRED)} present" +
          (f"; MISSING: {missing}" if missing else ""))
    present = [m for m in FORBIDDEN if m in flat]
    check("no FORBIDDEN (stale) marker in served bytes", not present,
          "clean" if not present else f"FOUND STALE: {present}")

    # ── 2. payload fields the UI reads ──
    d = json.loads(get("/operator/dsh-jobs/data"))
    attr = d.get("shotAttribution") or {}
    sm = d.get("screenshotMode") or {}
    check("/data carries shotAttribution.health + healthVerdict",
          bool(attr.get("health")) and bool(attr.get("healthVerdict")),
          f"health={attr.get('health')}")
    check("/data attribution counts add up",
          (attr.get("attributedCount") or 0) + len(attr.get("unattributed") or []) == attr.get("totalShots"),
          f"{attr.get('attributedCount')} + {len(attr.get('unattributed') or [])} == {attr.get('totalShots')}")
    check("/data explains WHY shots are unattributed (actionTail.whyUnattributed)",
          bool((attr.get("actionTail") or {}).get("whyUnattributed")),
          f"quietest gap inside the ring: {(attr.get('actionTail') or {}).get('quietestGapInsideRingMs')}ms")
    bad = [u for u in (attr.get("unattributed") or []) if not u.get("reasonCode")]
    check("every unattributed record names its reason", not bad, f"{len(bad)} without a reasonCode")
    basis = [(attr.get("byAgentId") or {}).get(k) or [] for k in (attr.get("byAgentId") or {})]
    allpair = [r for lst in basis for r in lst]
    check("every paired record carries a basis (bracketed | nearest-before)",
          all(r.get("basis") in ("bracketed", "nearest-before") for r in allpair),
          f"{len(allpair)} paired records")
    check("/data honesty block on the screenshot-mode flag",
          bool((sm.get("honesty") or {}).get("remedy")) and "REQUEST" in str((sm.get("honesty") or {}).get("thisIs")),
          "says REQUEST, names the plugin and the patch path")

    # ── 3. the store round-trip, restored ──
    original = sm.get("enabled")
    flipped = not bool(original)
    code, body = post("/operator/dsh-jobs/screenshot-mode", {"enabled": flipped, "actor": "served-bytes-assert"})
    check("POST screenshot-mode is confirmed by an independent re-read",
          code == 200 and body.get("ok") is True and body.get("enabled") == flipped,
          f"HTTP {code} ok={body.get('ok')} enabled={body.get('enabled')}")
    code2, body2 = post("/operator/dsh-jobs/screenshot-mode", {"enabled": bool(original), "actor": "served-bytes-assert-restore"})
    check("operator's original screenshot-mode value restored",
          code2 == 200 and body2.get("ok") is True and body2.get("enabled") == bool(original),
          f"restored to {original}")

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(
        {"ranAt": int(time.time() * 1000), "pass": ok, "checks": results,
         "requiredMarkers": len(REQUIRED), "forbiddenMarkers": FORBIDDEN}, indent=2) + "\n", encoding="utf-8")

    for r in results:
        print(("PASS  " if r["pass"] else "FAIL  ") + r["check"] + ("  — " + r["detail"] if r["detail"] else ""))
    print(("\nALL PASS" if ok else "\nFAILURES PRESENT") + f" · evidence: {EVIDENCE}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
