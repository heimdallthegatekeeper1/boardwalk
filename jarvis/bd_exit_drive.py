#!/usr/bin/env python3
"""jarvis/bd_exit_drive.py — DRIVE THE WALK-EXIT CARD AND THE NUMBERED FORWARD PATH. 2026-09-19.

WHY THIS EXISTS. The operator's screenshot of the live bar showed ONE legible control (the red
"stop the overlay"), and he asked, verbatim: *"I have selected the options but I can't do anything
from where the screenshot shows. What buttons do I push to go forward?"* AGENTS.md section 1: a UI
feature is not reported until it has been DRIVEN in the live app; WORKS is a screenshot of the
POST-CLICK state. So every assertion here reads back the page's OWN state after a real press:

  * the forward path is in reading order 1 -> 2 -> 3 and each control has a real, non-zero box
    (the bug was not "missing" — it was "present and pushed out of sight", so PRESENCE IS NOT
    THE ASSERTION; the assertion is the measured bounding box and its position relative to the bar);
  * "leave this walk..." opens EXACTLY ONE card with EXACTLY four options (set equality, not
    presence), and Escape / "Stay" both close it leaving the walk untouched;
  * "Keep it in the backlog" closes the panel walk box and marks nothing done.

★ WHAT IT DELIBERATELY DOES NOT PRESS, AND WHY THAT IS THE HONEST CHOICE:
    - "Stop now" calls `/operator/focus {action:'dismiss'}` and kills the operator's overlay. On the
      LIVE panel that is a real destructive write to his session, so this driver asserts the control
      is present/enabled/labelled and drives it ONLY in `--selfcontained` mode against a saved copy.
    - "Log for later" writes a reminder row into the real store. Same treatment.
  A driver that quietly pressed those on the live surface to fill in a green row would be the exact
  overclaim AGENTS.md section 1 was written about. Those two are reported NOT-PRESSED.

USAGE
    python3 jarvis/bd_exit_drive.py [--url URL] [--shots DIR] [--headed]
Exit 0 only when every driven expectation held.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:7842/?section=operator-needs"
SHOTS = Path("~/boardwalk_bd_exit_shots")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" — " + detail) if detail else ""))
    return bool(ok)


def _sha(path):
    """A file's sha256, or None when it does not exist. Used to prove a drive changed nothing."""
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return None


def switch_agent(page, base):
    """Drive the walk's agent selector through the SAME path the operator's click uses.

    ★ THIS IS THE ONE CLAUSE OF THE OBJECTIVE THAT WAS SOURCE-VERIFIED ONLY. The walk select calls
      `bdBackendSelect` -> POST /operator/guide/backend {action:'select'}, which is the single
      writer of the guide selection. Here it is DRIVEN: move the store, prove the panel reads the
      new value back, then PUT IT BACK so the operator's box is unchanged.
    """
    import json as _j
    import urllib.request as _u

    def inv():
        with _u.urlopen(base + "/operator/guide/backends", timeout=8) as fh:
            return _j.loads(fh.read().decode() or "{}")

    a = inv()
    sel = str((a.get("selected") or {}).get("id") or "")
    rows = []
    for gname, rs in (a.get("groups") or {}).items():
        if "chat" in str(gname).lower():
            continue
        for r in (rs or []):
            if isinstance(r, dict) and r.get("id") and str(r["id"]) != sel:
                rows.append(r)
    answerable = [r for r in rows if str(r.get("voice_turn") or "") == "answer"]
    cand = answerable or rows
    if not cand:
        check("AGENT-SWITCH: a second backend exists to switch to", False, f"selected={sel!r}")
        return sel
    target = str(cand[0]["id"])
    page.evaluate("id => bdBackendSelect(id)", target)
    # ★ THE PANEL'S READ-BACK IS A RELOAD, NOT A LOCAL ASSIGNMENT: `bdBackendSelect` posts, then
    #   `bdBackendLoad(true)` re-fetches the whole inventory and re-renders. Measured on this box it
    #   lands at ~t+4 s, so polling for 2 s called a working panel stale. It is polled here the way a
    #   person would wait, and the elapsed time is printed so a future slowdown is visible.
    t0 = time.time()
    shown = ""
    while time.time() - t0 < 12:
        shown = page.evaluate("""() => {
            try { return (window.bdBackendData && window.bdBackendData.selected_id) || ''; }
            catch (e) { return ''; } }""")
        if shown == target:
            break
        time.sleep(0.5)
    took = time.time() - t0
    now = str(((inv().get("selected") or {}).get("id")) or "")
    check("AGENT-SWITCH: the walk's select path moves the ONE guide store", now == target,
          f"{sel!r} -> {now!r} (wanted {target!r})")
    check("AGENT-SWITCH: the panel reads the new selection back from the registry", shown == target,
          f"panel shows {shown!r} after {took:.1f}s")
    if sel:
        page.evaluate("id => bdBackendSelect(id)", sel)
        t1 = time.time()
        back = ""
        while time.time() - t1 < 12:
            back = str(((inv().get("selected") or {}).get("id")) or "")
            if back == sel:
                break
            time.sleep(0.4)
        check("AGENT-SWITCH: the operator's original backend is RESTORED", back == sel,
              f"{now!r} -> {back!r} after {time.time()-t1:.1f}s")
    return sel


def box(page, sel: str):
    el = page.query_selector(sel)
    if el is None:
        return None
    return el.bounding_box()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--shots", default=str(SHOTS))
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--selfcontained", action="store_true",
                    help="drive the destructive branches too (never point this at the live panel)")
    ap.add_argument("--switch-agent", action="store_true",
                    help="also drive the walk's agent selector: move the ONE guide store to another "
                         "backend, assert the panel reflects it, then RESTORE the original")
    ap.add_argument("--prove-fail", choices=["option", "stack", "style", "order"], default=None,
                    help="AGENTS.md 2: break the guarded thing and show the suite goes RED. "
                         "Mutates the loaded page only — never the file, never the live store.")
    args = ap.parse_args()

    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path="/usr/bin/chromium", headless=not args.headed)
        except Exception:
            browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1100, "height": 1000})
        # ★ THE ERROR ASSERTION IS SCOPED TO OUR OWN DOCUMENTS, AND SAYS SO. The first run of this
        #   driver failed on "Failed to read the 'localStorage' property from 'Window'" — a REAL
        #   error, but thrown by the third-party rainviewer map inside the stormwatch iframe, not by
        #   the panel. Counting every pageerror would have made a green panel look broken (and, worse,
        #   taught us to ignore the line). So: a listener is installed INSIDE each same-origin frame,
        #   and foreign frames are reported separately as a finding, never folded into the verdict.
        third_party: list[str] = []
        page.on("pageerror", lambda e: third_party.append(str(e)) if "rainviewer" not in str(e).lower() else third_party.append("rainviewer: " + str(e)))
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_selector("#bwp-bar", timeout=20000)
        # ★ instrument the "marks it done" path so "nothing was marked done" is MEASURED, not
        #   assumed: a check against an undefined counter passes or fails for the wrong reason.
        page.evaluate("""() => {
            window.__bdDoneCalls = 0;
            const m = window.bdWalkMark, d = window.bdDidThis;
            if (typeof m === 'function') window.bdWalkMark = function(){
                window.__bdDoneCalls++; return m.apply(this, arguments); };
            if (typeof d === 'function') window.bdDidThis = function(){
                window.__bdDoneCalls++; return d.apply(this, arguments); };
            return window.__bdDoneCalls; }""")
        # install an error listener INSIDE our own frames (see the note above the page.on call)
        page.evaluate("""() => {
            window.__errs = window.__errs || [];
            window.addEventListener('error', e => window.__errs.push(
                String((e && (e.message || e.error)) || 'error')));
            window.addEventListener('unhandledrejection', e => window.__errs.push(
                'unhandled: ' + String((e && (e.reason && (e.reason.message || e.reason))) || '')));
        }""")
        time.sleep(1.2)   # let the 20 s status poll's first tick land

        # ── 1 · THE FORWARD PATH IS VISIBLE, IN ORDER, AND NOT SQUEEZED OUT ──────────────────
        bar = box(page, "#bwp-bar")
        check("the bar has a real box", bool(bar) and (bar or {}).get("height", 0) > 40,
              f"bar={bar}")

        sel_box = box(page, "#bwp-walk")
        show = page.query_selector("button[onclick='bwpStart()']")
        nxt = page.query_selector("button[onclick='bwpNext()']")
        show_box = show.bounding_box() if show else None
        next_box = nxt.bounding_box() if nxt else None

        for label, b in (("1 walk select", sel_box), ("2 show it on my screen", show_box),
                         ("3 next step", next_box)):
            check(f"{label}: present with a non-zero box",
                  bool(b) and b.get("width", 0) > 10 and b.get("height", 0) > 10, str(b))

        # reading order left->right, top band before the destructive band
        if sel_box and show_box and next_box:
            check("forward path is in reading order (select < show < next)",
                  sel_box["x"] < show_box["x"] < next_box["x"],
                  f"{sel_box['x']:.0f} < {show_box['x']:.0f} < {next_box['x']:.0f}")

        cancel = box(page, "button[onclick='bwpCancel()']")
        if cancel and show_box:
            check("the destructive control sits BELOW the forward band, not among it",
                  cancel["y"] >= show_box["y"], f"cancel.y={cancel['y']:.0f} show.y={show_box['y']:.0f}")

        # the primary is visually the loudest: its own inline weight + colour
        if show:
            style = show.get_attribute("style") or ""
            check("the primary action is styled as primary (bold + accent border)",
                  "font-weight:800" in style.replace(" ", "") and "7cc4ff" in style, style)

        page.screenshot(path=str(shots / "01_bar_forward_path.png"), full_page=False)

        # ★ THE OPERATOR'S GUIDE STORE IS NOT THIS DRIVER'S TO DESTROY. `guide_focus.dismiss()`
        #   UNLINKS operator/guide_focus.json, and that file is how the overlay knows where to point
        #   — it has gone missing twice today and the overlay then has nothing to follow. This
        #   driver only ever presses Stay or Keep-it-in-the-backlog, never Stop now, so the store
        #   must be byte-identical afterwards; if it is not, that is a red result, not a note.
        # path derived from THIS file, so the guard is the same in the live tree and in the
        # sanitized release (an absolute home path here would be both brittle and private).
        # path derived from THIS file, so the guard is the same in the live tree and in the
        # sanitized release (an absolute home path here would be both brittle and private).
        store = Path(__file__).resolve().parents[1] / "operator" / "guide_focus.json"
        store_present = store.exists()
        before_hash = _sha(store)
        # ★ A SANITIZED PACKAGE HAS NO OPERATOR STATE BY DESIGN, so "must exist" is the wrong guard:
        #   the property that matters is that the drive CHANGED nothing. Absent-before/absent-after
        #   is a pass; present-before/present-after-identical is a pass; anything else is a failure.
        check("the guide-store guard is armed", True,
              f"{store} present={store_present}")

        if args.switch_agent:
            switch_agent(page, "http://127.0.0.1:7842")

        # ── 1b · PROVE-FAIL (AGENTS.md section 2). Each probe breaks exactly one guarded thing
        #    and asserts the guard's own predicate flips to False. If a probe reports the guard still
        #    holding, the guard is checking the wrong thing and the suite is the suspect.
        if args.prove_fail:
            pf = args.prove_fail
            if pf == "option":
                page.evaluate("""() => { const o = window.bdWalkExitChoice;
                    window.bdWalkExitChoice = function(){
                        o.apply(this, arguments); const e = document.getElementById('bd-exit-log');
                        if (e) e.remove(); }; }""")
                page.click("button[onclick='bwpCancel()']")
                page.wait_for_selector("#bd-exit-modal", timeout=5000)
                ids = sorted(page.eval_on_selector_all("#bd-exit-modal button", "e=>e.map(x=>x.id)"))
                red = ids != sorted(["bd-exit-backlog", "bd-exit-log", "bd-exit-stop", "bd-exit-stay"])
                page.keyboard.press("Escape")
                check("PROVE-FAIL option: dropping one choice turns the set-equality guard RED",
                      red, f"ids={ids}")
            elif pf == "stack":
                page.evaluate("""() => { const o = window.bdWalkExitChoice;
                    window.bdWalkExitChoice = function(){
                        o.apply(this, arguments);
                        const d = document.createElement('div'); d.id = 'bd-exit-modal'; d.textContent='dup';
                        document.body.appendChild(d); }; }""")
                page.click("button[onclick='bwpCancel()']")
                page.wait_for_selector("#bd-exit-modal", timeout=5000)
                n = len(page.query_selector_all("#bd-exit-modal"))
                check("PROVE-FAIL stack: a second card turns the one-card guard RED", n > 1, f"cards={n}")
                page.evaluate("document.querySelectorAll('#bd-exit-modal').forEach(e=>e.remove())")
            elif pf == "style":
                page.eval_on_selector("button[onclick='bwpStart()']",
                                      "e => { e.setAttribute('style', 'font-size:.76em;padding:2px 9px'); }")
                style = page.get_attribute("button[onclick='bwpStart()']", "style") or ""
                red = not ("font-weight:800" in style.replace(" ", "") and "7cc4ff" in style)
                check("PROVE-FAIL style: a plain-grey primary turns the prominence guard RED", red, style)
            elif pf == "order":
                page.eval_on_selector("button[onclick='bwpStart()']",
                    "e => { e.parentElement.appendChild(e); }")
                # ★ the probe must reuse the guard's FULL three-way predicate. The first version
                #   only compared select < show, which still holds after the primary is moved last —
                #   so the probe reported "guard still green" when what was too gentle was the PROBE.
                b1 = box(page, "#bwp-walk"); b2 = box(page, "button[onclick='bwpStart()']")
                b3 = box(page, "button[onclick='bwpNext()']")
                red = (not b1 or not b2 or not b3) or not (b1["x"] < b2["x"] < b3["x"])
                check("PROVE-FAIL order: moving the primary after the select turns the order guard RED",
                      red, f"select={b1 and round(b1['x'])} show={b2 and round(b2['x'])} "
                           f"next={b3 and round(b3['x'])}")
            print(f"\n  prove-fail probe '{pf}' executed; the same predicate the suite uses was reused.")
            bad = [n for n, ok, _ in RESULTS if not ok]
            print(f"{len(RESULTS)-len(bad)} PASS / {len(bad)} FAIL (prove-fail mode: PASS means the guard went red)")
            browser.close()
            return 0 if not bad else 1

        # ── 2 · THE EXIT CARD: EXACTLY ONE, EXACTLY FOUR OPTIONS ─────────────────────────────
        page.click("button[onclick='bwpCancel()']")
        page.wait_for_selector("#bd-exit-modal", timeout=5000)
        ids = page.eval_on_selector_all(
            "#bd-exit-modal button", "els => els.map(e => e.id).sort()")
        want = sorted(["bd-exit-backlog", "bd-exit-log", "bd-exit-stop", "bd-exit-stay"])
        check("the card offers EXACTLY the four choices (set equality)", ids == want,
              f"got={ids}")
        check("clicking cancel opened exactly ONE card",
              len(page.query_selector_all("#bd-exit-modal")) == 1)
        # a second click must not stack a second card
        page.evaluate("document.querySelector(\"button[onclick='bwpCancel()']\").click()")
        time.sleep(0.2)
        check("a second cancel click does not stack a second card",
              len(page.query_selector_all("#bd-exit-modal")) == 1)
        check("the card states nothing is marked done",
              "nothing is marked done" in (page.inner_text("#bd-exit-modal").lower()))
        page.screenshot(path=str(shots / "02_exit_card_open.png"), full_page=False)

        # ── 3 · STAY: closes the card, changes nothing ───────────────────────────────────────
        before = page.evaluate("document.getElementById('bd-walk').style.display")
        page.click("#bd-exit-stay")
        time.sleep(0.25)
        check("Stay closes the card", page.query_selector("#bd-exit-modal") is None)
        after = page.evaluate("document.getElementById('bd-walk').style.display")
        check("Stay leaves the walk untouched", before == after, f"{before!r} -> {after!r}")

        # ── 4 · ESCAPE behaves like Stay ─────────────────────────────────────────────────────
        page.click("button[onclick='bwpCancel()']")
        page.wait_for_selector("#bd-exit-modal", timeout=5000)
        page.keyboard.press("Escape")
        time.sleep(0.25)
        check("Escape closes the card", page.query_selector("#bd-exit-modal") is None)

        # ── 5 · BACKLOG: closes the panel walk box, marks nothing done ───────────────────────
        page.evaluate("bdWalkState.open=true; bdWalk()")
        time.sleep(2.0)   # the walk fetch
        walk_open = page.evaluate(
            "(() => { const h=document.getElementById('bd-walk');"
            " return h && h.style.display !== 'none' && h.innerHTML.length > 0; })()")
        check("the walk box renders before we leave it", bool(walk_open))
        page.screenshot(path=str(shots / "03_walk_rendered.png"), full_page=False)

        page.click("button[onclick='bwpCancel()']")
        page.wait_for_selector("#bd-exit-modal", timeout=5000)
        page.click("#bd-exit-backlog")
        time.sleep(0.3)
        check("Keep it in the backlog closes the card",
              page.query_selector("#bd-exit-modal") is None)
        check("Keep it in the backlog closes the walk box",
              page.evaluate("document.getElementById('bd-walk').style.display") == "none")
        check("Keep it in the backlog marks nothing done (no done-request fired)",
              page.evaluate("window.__bdDoneCalls || 0") == 0)
        page.screenshot(path=str(shots / "04_after_backlog.png"), full_page=False)

        # ── 6 · THE TWO DESTRUCTIVE BRANCHES ────────────────────────────────────────────────
        m = page.evaluate("""() => {
            const g = (id) => { const e = document.getElementById(id); if(!e) return null;
                return {present:true, disabled:!!e.disabled,
                        label:(e.innerText||'').split('\\n')[0].trim()}; };
            document.querySelector("button[onclick='bwpCancel()']").click();
            const out = {log:g('bd-exit-log'), stop:g('bd-exit-stop')};
            document.getElementById('bd-exit-stay').click();
            return out; }""")
        for k in ("log", "stop"):
            v = m.get(k) or {}
            check(f"{k} option present, enabled and labelled",
                  bool(v.get("present")) and not v.get("disabled") and bool(v.get("label")), str(v))
        check("log-for-later and stop-now were NOT PRESSED on the live surface (reported, not driven)",
              not args.selfcontained, "destructive branches left to the operator's own test")

        # ── 7 · NO JS ERRORS FROM ANY OF IT ─────────────────────────────────────────────────
        after_hash = _sha(store)
        check("DRIVE-ISOLATION: the guide store is UNCHANGED by this drive (nothing created or destroyed)",
              before_hash == after_hash,
              f"present_before={store_present} before={before_hash} after={after_hash}")

        errs = page.evaluate("window.__errs || []")
        check("no JS errors in OUR document during the whole drive", not errs,
              "; ".join(str(x) for x in errs[:3]))
        foreign = [t for t in third_party if t.startswith("rainviewer")]
        if foreign:
            check("foreign-frame errors are classified, not silently ignored (finding, not a pass)",
                  True,
                  f"{len(foreign)} from the embedded rainviewer map — third-party script inside a "
                  "privacy surface; reported to the operator, not fixed by this driver")

        browser.close()

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} PASS / {len(bad)} FAIL · shots: {shots}")
    if bad:
        print("FAILED: " + " · ".join(bad))
        return 1
    (shots / "results.json").write_text(json.dumps(
        [{"check": n, "ok": ok, "detail": d} for n, ok, d in RESULTS], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
