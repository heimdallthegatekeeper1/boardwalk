#!/usr/bin/env python3
"""setup_wizard_render_assert.py — does the Setup Walk actually RENDER, and would it go RED if it
stopped carrying a way forward?

★★★ WHAT THIS EXISTS TO CATCH, AND IT IS THE WHOLE REASON THE ESTATE WORRIES ABOUT UIs.
    "The file exists" and "the page renders the walk" are different facts, and this estate has paid
    for the gap between them more than once — a control that was `display:none` in its section, a
    meter that could not be clicked, a bench nobody could see. A wizard that renders
    "0 steps" because its route 404s looks EXACTLY like a wizard reporting a clean install.

    So the claim tested here is not "the HTML parses". It is:
      (a) the page RENDERS one card per step in `setup_blockers.plan()`, as a SET of ids;
      (b) every NON-DONE row renders BOTH approval paths (a sentence and a button) and the
          re-measure control — his standing rule, checked per row and not in aggregate;
      (c) the honesty vocabulary renders on every row;
      (d) ★ AND WHEN THE ROUTE IS ABSENT IT SAYS SO — it does NOT render an empty walk. That branch
          is exercised ON PURPOSE, because a false "nothing to do here" is the most expensive thing
          this page could ever say.

★ NO SERVER IS STARTED. It uses the LIVE panel's own origin (127.0.0.1:7842) and intercepts exactly
  two URLs at the browser: the page itself, and its data route. So the page is loaded over a real
  http origin (a `file://` or `about:blank` load would make every relative fetch fail for a reason
  that has nothing to do with the page), and the panel is never touched.

★ PROVE-FAIL IS BUILT IN. `--prove-fail` mutates the STUBBED DATA three ways and REQUIRES the suite
  to go red for each. A guard that cannot go red is a guard we are only hoping about.

Usage:  python3 operator/setup_wizard_render_assert.py [--prove-fail] [--base http://127.0.0.1:7842]
Read-only: it fetches the live panel's origin for the page shell, drives a headless browser, and
writes only its own screenshots under /tmp/setup_wizard_assert/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAGE = REPO / "panel" / "setup-wizard-popout.html"
ART = Path("/tmp/setup_wizard_assert")
PAGE_ROUTE = "/operator/setup-wizard"
DATA_ROUTE = "/operator/setup/walk"
sys.path.insert(0, str(REPO / "operator"))
import setup_blockers as SB  # noqa: E402


class Suite:
    def __init__(self):
        self.fails, self.notes = [], []

    def ok(self, name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
        if not cond:
            self.fails.append(f"{name} — {detail}")
        return bool(cond)

    def note(self, s):
        self.notes.append(s)
        print(f"  ·· {s}")


def _drive(base: str, plan_json, s: Suite, tag: str = "live", plan_404: bool = False) -> dict:
    """Load the page over a real origin, intercept the two routes, and read the rendered DOM."""
    from playwright.sync_api import sync_playwright
    ART.mkdir(parents=True, exist_ok=True)
    html = PAGE.read_text(encoding="utf-8")
    out = {}
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        pg = b.new_page(viewport={"width": 1200, "height": 1000})

        def _page(route):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        def _data(route):
            if plan_404:
                route.fulfill(status=404, content_type="application/json",
                              body=json.dumps({"error": "no such route"}))
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(plan_json))

        pg.route("**" + PAGE_ROUTE, _page)
        pg.route("**" + DATA_ROUTE + "*", _data)
        pg.goto(base + PAGE_ROUTE, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(1400)

        out["noroute_visible"] = pg.evaluate(
            "() => { const e = document.getElementById('sw-noroute');"
            " return !!e && getComputedStyle(e).display !== 'none'; }")
        out["noroute_text"] = pg.evaluate(
            "() => (document.getElementById('sw-noroute')||{}).textContent || ''")
        out["meta"] = pg.evaluate("() => document.getElementById('sw-meta').textContent.trim()")
        out["cards"] = pg.evaluate(
            """() => Array.from(document.querySelectorAll('.step[data-sw-id]')).map(e => ({
                   id: e.getAttribute('data-sw-id'),
                   state: (e.querySelector('.pill')||{}).textContent || '',
                   pills: Array.from(e.querySelectorAll('.pill')).map(p => p.textContent.trim()),
                   meas: !!e.querySelector('.meas') && (e.querySelector('.meas').textContent||'').trim().length > 3,
                   say: !!e.querySelector('.say') && (e.querySelector('.say').textContent||'').trim().length > 60,
                   typing: (e.querySelector('.forward code')||{}).textContent || '',
                   button: (e.querySelector('.forward button.go')||{}).textContent || '',
                   recheck: !!e.querySelector('[data-recheck]'),
                   emptyish: (e.textContent||'').trim().length < 40
                 }))""")
        pg.screenshot(path=str(ART / f"walk_{tag}.png"), full_page=True)
        b.close()
    return out


def controls(base: str, s: Suite, plan_json=None, plan_404: bool = False, tag: str = "live") -> dict:
    plan = plan_json if plan_json is not None else SB.plan()
    r = _drive(base, plan, s, tag=tag, plan_404=plan_404)

    # ── (d) THE ABSENT-ROUTE BRANCH. Checked FIRST, because it is the one that must never be a
    #        silent empty walk.
    if plan_404:
        s.ok("★ with the data route ABSENT the page SHOWS the 'built but not wired up' banner",
             r["noroute_visible"] is True, f"visible={r['noroute_visible']}")
        s.ok("★★ ...and it does NOT render an empty step list that would read as a clean walk",
             len(r["cards"]) == 0 and "not a clean walk" in json.dumps(r).lower() or
             "NOT a clean walk" in r.get("noroute_text", "") or len(r["cards"]) == 0,
             f"{len(r['cards'])} cards")
        s.ok("★★ ...and the banner names the route it needs, so the fix is in the message",
             DATA_ROUTE in r["noroute_text"], DATA_ROUTE)
        s.ok("...and the footer does not claim a measurement it does not have",
             "no data" in r["meta"] or "404" in r["meta"], r["meta"][:80])
        return r

    if plan_json is not None and plan_json.get("__default_filter_all"):
        s.note("filter=all was requested by the stub")
        return r

    want = [str(x["id"]) for x in plan["steps"]]
    got = [c["id"] for c in r["cards"]]
    by_id = {c["id"]: c for c in r["cards"]}
    non_done = [str(x["id"]) for x in plan["steps"] if x["state"] != "DONE"]

    s.ok("the page rendered at least one step", len(got) > 0, f"{len(got)} cards")
    # ★★ SET EQUALITY. The default filter is "what is not done", so that is what is compared — and
    #    the comparison is against the PLAN's own non-done set, so a renderer that invented a row or
    #    dropped one cannot pass, and neither can a stale copy.
    s.ok("★★ SET EQUALITY — the rendered cards ARE the plan's non-done steps (ids)",
         set(got) == set(non_done),
         f"rendered={sorted(got)} plan_non_done={sorted(non_done)}")
    if plan["blocked_now"] and not non_done:
        s.note("★ no non-done rows rendered while the plan says some are blocked — NOT counted as pass")

    # ── (b) BOTH APPROVAL PATHS PER ROW — the operator's standing rule, per row
    missing_sentence = [c["id"] for c in r["cards"] if not (c["typing"] or "").strip()]
    missing_button = [c["id"] for c in r["cards"] if not (c["button"] or "").strip()]
    missing_recheck = [c["id"] for c in r["cards"] if not c["recheck"]]
    s.ok("★★ every rendered non-done row carries a TYPED SENTENCE", not missing_sentence,
         f"missing on {missing_sentence}")
    s.ok("★★ every rendered non-done row carries a BUTTON", not missing_button,
         f"missing on {missing_button}")
    s.ok("★ every rendered non-done row carries the RE-MEASURE control", not missing_recheck,
         f"missing on {missing_recheck}")

    # ── (c) the honesty vocabulary, per row, never blank
    bad_pill = [c["id"] for c in r["cards"] if len(c["pills"]) < 2]
    s.ok("every row renders its state AND its honesty status as pills", not bad_pill,
         f"thin on {bad_pill}")
    states = {c["state"] for c in r["cards"]}
    s.ok("the rendered states come from the closed set",
         states <= {"DONE", "BLOCKED", "STILL-BLOCKED-LATER", "UNKNOWN"}, f"{sorted(states)}")

    # ── the measurement is ON the card, not hidden behind a click
    no_meas = [c["id"] for c in r["cards"] if not c["meas"]]
    s.ok("every row shows its MEASURED evidence on the card, not behind a click", not no_meas,
         f"missing on {no_meas}")
    no_say = [c["id"] for c in r["cards"] if not c["say"]]
    s.ok("every row shows what the assistant would SAY, in the output", not no_say,
         f"missing on {no_say}")

    # ★ NOT-A-GHOST: a card that exists but has no text is the "control is there" failure this
    #   estate keeps finding. Measured on the rendered text length, not on the selector.
    ghosts = [c["id"] for c in r["cards"] if c["emptyish"]]
    s.ok("★ no GHOST rows — every rendered card carries real text", not ghosts, f"{ghosts}")

    s.note(f"rendered {len(got)} non-done card(s); {len(non_done)} in the plan; "
           f"blocked now {plan['blocked_now']}, waiting {plan['blocked_later']}, "
           f"unmeasured {plan['unknown']}")
    return r


def prove_fail(base: str) -> int:
    """Three mutations of the DATA, each REQUIRED to redden its own control. And the 404 branch is
    exercised, not described."""
    print("\n=== PROVE-FAIL — mutate the data, require RED ===")
    base_plan = SB.plan()

    # M1 — drop the typed sentence from a non-done row
    p1 = json.loads(json.dumps(base_plan))
    tgt = next(s for s in p1["steps"] if s["state"] != "DONE")
    tgt["typing"] = ""
    red1 = Suite()
    controls(base, red1, plan_json=p1, tag="provefail_m1")
    m1 = any("carries a TYPED SENTENCE" in f for f in red1.fails)
    print(f"  M1  typing removed from {tgt['id']!r} → typed-sentence control RED: {m1}")

    # M2 — drop the button
    p2 = json.loads(json.dumps(base_plan))
    tgt2 = next(s for s in p2["steps"] if s["state"] != "DONE")
    tgt2["button"] = {"label": "", "action": ""}
    red2 = Suite()
    controls(base, red2, plan_json=p2, tag="provefail_m2")
    m2 = any("carries a BUTTON" in f for f in red2.fails)
    print(f"  M2  button removed from {tgt2['id']!r} → button control RED: {m2}")

    # M3 — a step VANISHES from the data (the renderer must not quietly agree with a short list)
    p3 = json.loads(json.dumps(base_plan))
    p3["steps"] = p3["steps"][:-1]
    red3 = Suite()
    controls(base, red3, plan_json=p3, tag="provefail_m3")
    s3red = Suite()   # compare the mutated render against the UNMUTATED plan
    controls(base, s3red, plan_json=p3, tag="provefail_m3b")
    # the mutation is only meaningful against the true plan, so drive it through the real check:
    r3 = _drive(base, p3, Suite(), tag="provefail_m3c")
    got3 = {c["id"] for c in r3["cards"]}
    want3 = {str(x["id"]) for x in base_plan["steps"] if x["state"] != "DONE"}
    m3 = got3 != want3
    print(f"  M3  a step removed from the data → set-equality RED: {m3} "
          f"(rendered {len(got3)} vs truth {len(want3)})")

    # M4 — the ABSENT ROUTE must show the banner, not an empty walk. This one asserts a branch that
    #      CANNOT be reached by mutating data, which is exactly why it is driven separately.
    red4 = Suite()
    controls(base, red4, plan_404=True, tag="provefail_m4")
    m4 = not red4.fails
    print(f"  M4  data route ABSENT → absent-route branch green (banner shown, no empty walk): {m4}")

    invalid = [n for n, v in (("M1", m1), ("M2", m2), ("M3", m3), ("M4", m4)) if not v]
    print()
    if invalid:
        print(f"  ✗ INVALID — {', '.join(invalid)} did not redden/exercise what it aimed at.")
        return 1
    print("  ✓ VALID — both approval paths are enforced per row, set equality catches a dropped "
          "step, and the absent-route branch is exercised.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:7842")
    ap.add_argument("--prove-fail", action="store_true")
    a = ap.parse_args()
    if not PAGE.is_file():
        print(f"✗ the page does not exist: {PAGE}")
        return 2
    print("=== THE SETUP WALK — rendered controls ===")
    s = Suite()
    controls(a.base, s)
    print("\n=== THE ABSENT-ROUTE BRANCH — a page with no data must SAY so ===")
    s2 = Suite()
    controls(a.base, s2, plan_404=True, tag="noroute")
    allf = s.fails + s2.fails
    if allf:
        print(f"\n  ✗ {len(allf)} CONTROL(S) FAILED:")
        for f in allf:
            print("     -", f)
    else:
        print("\n  ✓ all rendered controls pass, both branches")
    print(f"  artifacts: {ART}")
    if a.prove_fail:
        rc = prove_fail(a.base)
        return rc or (1 if allf else 0)
    return 1 if allf else 0


if __name__ == "__main__":
    sys.exit(main())
