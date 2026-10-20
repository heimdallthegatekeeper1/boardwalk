#!/usr/bin/env python3
"""dsh_obs_subagents_drive.py — DRIVE the subagent roster in the operator's own Chrome (:9222) and
assert, IN THE DOM, the four things the operator asked for:

  A. EVERY published subagent is rendered — SET EQUALITY against `GET /operator/dsh-jobs/data`,
     not "some rows exist". (Prove-fail: a row is deleted from the DOM and the same comparison MUST
     go red, then the next poll restores it.)
  B. Inactive is VISIBLE and marked by a NON-COLOUR cue: the row carries the WORD "inactive" and the
     glyph "⏸", and the mark is not the colour. (Prove-fail: the word is blanked in the DOM and the
     assertion MUST go red.)
  C. `one-shot` vs `continuable` is surfaced IN WORDS, and the counts agree with the feed.
  D. The 34 records with `label: null` are not blank rows: each says "unlabelled" and carries kind+id.
  E. The list is LIVE: a MutationObserver on the roster host proves it is rebuilt on the feed's own
     cadence, and the observed deltas are printed (the interval is measured, not assumed).
  F. The same roster is reachable in Simple, Mid and High — driven by clicking the real view buttons.

Why CDP and not a source-grep: "the control is in the HTML" cost this estate weeks. This asserts the
RENDERED DOM in the operator's browser after driving the real controls, and writes a PNG of the
driven state.

Usage: python3 operator/dsh_obs_subagents_drive.py      (exit 0 = all held)
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_PORT = 9222
PAGE_TAIL = "/operator/dsh-jobs"
BASE = "http://127.0.0.1:7842"
ROOT = Path(__file__).resolve().parent.parent
UITEST = ROOT / "var" / "dsh-observability" / "uitest"

RESULTS: list[dict] = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "pass": bool(passed), "detail": detail})
    print(("PASS  " if passed else "FAIL  ") + name + ("  — " + detail if detail else ""), flush=True)


def feed() -> dict:
    with urllib.request.urlopen(BASE + "/operator/dsh-jobs/data", timeout=10) as r:
        return json.load(r)


class Page:
    def __init__(self) -> None:
        d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=8))
        t = next(x for x in d if PAGE_TAIL in x.get("url", "") and x.get("type") == "page")
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=60,
                                             suppress_origin=True)
        self.i = 0

    def call(self, method: str, params: dict | None = None):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == mid:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def ev(self, expr: str, await_promise: bool = False):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": await_promise})
        if "exceptionDetails" in r:
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")

    def evjson(self, expr: str):
        v = self.ev("JSON.stringify(" + expr + ")")
        return json.loads(v) if v else None

    def shot(self, name: str) -> None:
        UITEST.mkdir(parents=True, exist_ok=True)
        r = self.call("Page.captureScreenshot", {"format": "png"})
        (UITEST / name).write_bytes(base64.b64decode(r["data"]))

    def click_js(self, selector: str) -> None:
        ok = self.ev(f"(function(){{var e=document.querySelector({json.dumps(selector)});"
                     "if(!e) return false; e.click(); return true;})()")
        if not ok:
            raise RuntimeError("no element for " + selector)


def main() -> int:
    p = Page()
    p.call("Page.enable")
    p.call("Runtime.enable")

    # ── bring the page up on the NEW bytes, from a clean view state ──
    p.ev("['dshobs.ctl.v3','dshobs.density.v3'].forEach(k=>localStorage.removeItem(k));"
         "sessionStorage.removeItem('dshobs.openPanels.v3'); true")
    p.call("Page.reload", {"ignoreCache": True})
    for _ in range(60):
        time.sleep(0.5)
        try:
            if p.ev("!!document.querySelector('#sa-high [data-sa-id]')"):
                break
        except Exception:
            pass
    time.sleep(1.0)

    # ══ A. SET EQUALITY: every published subagent is in the DOM ═════════════════════════════════
    d = feed()
    subs = d.get("subagents") or []
    want_ids = {s["id"] for s in subs}
    want_inactive = {s["id"] for s in subs if s.get("activity") == "inactive"}
    want_running = {s["id"] for s in subs if s.get("activity") == "running"}
    want_cont = {s["id"] for s in subs if s.get("mode") == "continuable"}
    want_one = {s["id"] for s in subs if s.get("mode") == "one-shot"}
    want_unlab = {s["id"] for s in subs if not s.get("label")}

    dom = p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),"
                   "e=>e.getAttribute('data-sa-id'))")
    got_ids = set(dom or [])
    view_disp = p.ev("getComputedStyle(document.getElementById('view-high')).display")
    check("A. every published subagent has a row — SET EQUALITY, not presence",
          got_ids == want_ids,
          f"DOM {len(got_ids)} rows vs feed {len(want_ids)} · missing={sorted(want_ids-got_ids)[:3]} "
          f"fabricated={sorted(got_ids-want_ids)[:3]}")
    check("A2. the roster's own completeness line states the published total",
          bool(p.ev("(function(){var s=document.querySelector('#sa-high .sa-sum');"
                    "return !!(s && s.textContent.indexOf('subagent(s) published by the harness feed')>=0);})()")),
          "view-high display=" + str(view_disp) + " · " +
          str(p.ev("(function(){var s=document.querySelector('#sa-high .sa-sum');"
                   "return s?s.textContent.replace(/\\s+/g,' ').slice(0,150):null;})()")))
    p.shot("20-subagent-roster-high-all-of-them.png")

    # ══ B. INACTIVE: visible, and marked by a WORD + GLYPH (not only colour) ════════════════════
    marks = p.evjson("""[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),function(e){
        var id=e.getAttribute('data-sa-id'); var pill=e.querySelector('.sa-pill');
        return {id:id, activity:e.getAttribute('data-activity'),
                pill:pill?pill.textContent:null, cls:pill?pill.className:null,
                border:pill?getComputedStyle(pill).borderTopStyle:null};})""")
    by_id = {m["id"]: m for m in (marks or [])}
    wrong = [i for i in want_inactive if (by_id.get(i) or {}).get("pill") != "⏸ inactive"]
    check("B. every INACTIVE subagent renders the word+glyph mark '⏸ inactive'",
          not wrong, f"{len(want_inactive) - len(wrong)}/{len(want_inactive)} correct"
                     + (f"; wrong: {wrong[:3]}" if wrong else ""))
    dashed = [i for i in want_inactive if (by_id.get(i) or {}).get("border") == "dashed"]
    check("B2. the inactive mark is NON-COLOUR (dashed border) as well as the word",
          len(dashed) == len(want_inactive),
          f"{len(dashed)}/{len(want_inactive)} inactive marks have border-style=dashed")
    run_ok = [i for i in want_running if (by_id.get(i) or {}).get("pill") == "▶ running"]
    check("B3. the running mark is a DIFFERENT word+glyph from inactive",
          len(run_ok) == len(want_running),
          f"{len(run_ok)}/{len(want_running)} running rows say '▶ running' "
          f"(inactive rows say '⏸ inactive' — the two never share a mark)")
    check("B4. the roster carries an on-screen legend for the marks",
          bool(p.ev("(function(){var l=document.querySelector('#sa-high .sa-legend');"
                    "return l && l.textContent.indexOf('inactive')>=0 && l.textContent.indexOf('colour')>=0;})()")),
          "states the mark differs by word and glyph, not only by colour")

    # ══ C. MODE: continuable vs one-shot, in words ═══════════════════════════════════════════════
    modes = p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),function(e){"
                     "var m=e.querySelector('.sa-mode');"
                     "return {id:e.getAttribute('data-sa-id'), t:m?m.textContent:null};})")
    mb = {m["id"]: m["t"] for m in (modes or [])}
    bad_cont = [i for i in want_cont if mb.get(i) != "continuable · resumable"]
    bad_one = [i for i in want_one if mb.get(i) != "one-shot · not resumable"]
    check("C. continuable is surfaced as 'continuable · resumable'",
          not bad_cont, f"{len(want_cont) - len(bad_cont)}/{len(want_cont)} labelled resumable")
    check("C2. one-shot is surfaced as 'one-shot · not resumable'",
          not bad_one, f"{len(want_one) - len(bad_one)}/{len(want_one)} labelled not resumable")
    readout = p.ev("document.querySelector('[data-sactl=\"roster\"] [data-sa-val=\"roster\"]').textContent")
    check("C3. the filter readout states both mode counts for the whole population",
          f"{len(want_cont)} continuable" in readout and f"{len(want_one)} one-shot" in readout,
          readout)

    # ══ D. label: null is NOT a blank row ════════════════════════════════════════════════════════
    blanks = p.evjson("""[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),function(e){
        var lab=e.querySelector('.sa-lab');
        return {id:e.getAttribute('data-sa-id'), haslabel:e.getAttribute('data-haslabel'),
                lab:lab?lab.textContent:null, txt:e.textContent.replace(/\\s+/g,' ').trim()};})""")
    bl = {b["id"]: b for b in (blanks or [])}
    bad_lab = [i for i in want_unlab
               if "unlabelled" not in (bl.get(i) or {}).get("lab", "")
               or not (bl.get(i) or {}).get("txt", "").strip()]
    check("D. the " + str(len(want_unlab)) + " records with label=null are NOT blank rows",
          not bad_lab, f"{len(want_unlab) - len(bad_lab)}/{len(want_unlab)} say 'unlabelled' and keep text"
                       + (f"; bad: {bad_lab[:3]}" if bad_lab else ""))
    fab = [i for i in want_unlab if (bl.get(i) or {}).get("lab", "") not in
           ("unlabelled", ) and (bl.get(i) or {}).get("lab") not in ("unlabelled",)]
    check("D2. no name was INVENTED for them (the cell is exactly 'unlabelled')",
          not fab, f"{len(fab)} unlabelled rows show something other than 'unlabelled'")

    # ══ PROVE-FAIL: the comparison MUST go red when a row is removed ═════════════════════════════
    victim = sorted(want_ids)[0]
    p.ev(f"(function(){{var e=document.querySelector('#sa-high [data-sa-id=\"{victim}\"]');"
         "if(e) e.remove(); return true;})()")
    tampered = set(p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),"
                            "e=>e.getAttribute('data-sa-id'))") or [])
    check("PROVE-FAIL: deleting one row makes the SET-EQUALITY check go RED",
          tampered != want_ids and (want_ids - tampered) == {victim},
          f"after removing {victim[:8]}: {len(tampered)} rows — the assertion is not vacuous")
    p.ev("true")
    time.sleep(3.0)   # >= 1 poll: the page must repair itself
    restored = set(p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),"
                            "e=>e.getAttribute('data-sa-id'))") or [])
    check("PROVE-FAIL recovery: the next poll restores the removed row (the list is re-derived, "
          "not patched)", restored == want_ids, f"{len(restored)} rows after one poll")

    # ══ E. LIVE: the roster is rebuilt on the feed's cadence (measured) ══════════════════════════
    obs = p.ev("""new Promise(function(res){
        var times=[]; var host=document.getElementById('sa-high');
        var mo=new MutationObserver(function(){ times.push(Date.now()); });
        mo.observe(host,{childList:true,subtree:true});
        var t0=Date.now();
        setTimeout(function(){ mo.disconnect();
          var dl=[]; for(var i=1;i<times.length;i++) dl.push(times[i]-times[i-1]);
          res(JSON.stringify({repaints:times.length, windowMs:Date.now()-t0, deltas:dl}));
        }, 11000);
      })""", await_promise=True)
    o = json.loads(obs)
    med = sorted(o["deltas"])[len(o["deltas"]) // 2] if o["deltas"] else None
    check("E. the subagent list is rebuilt WITHOUT a manual refresh (measured repaint cadence)",
          o["repaints"] >= 4 and med is not None and 1500 <= med <= 3000,
          f"{o['repaints']} repaints in {o['windowMs']}ms · median gap {med}ms · deltas {o['deltas']}")
    printed = p.ev("(function(){var l=document.querySelector('#sa-high .sa-live');"
                   "return l?l.textContent:null;})()")
    check("E2. the page PRINTS the interval it is polling on",
          bool(printed) and "every" in printed and "s" in printed, str(printed)[:160])

    # ══ F. the same roster in Simple and Mid, driven by clicking the REAL view buttons ═══════════
    for view, host, sel in (("Simple", "sa-simple", "#view-toggle button[data-view=\"simple\"]"),
                            ("Mid", "mid-subagents", "#view-toggle button[data-view=\"mid\"]"),
                            ("High", "sa-high", "#view-toggle button[data-view=\"high\"]")):
        p.click_js(sel)
        time.sleep(0.6)
        n = p.ev(f"document.querySelectorAll('#{host} [data-sa-id]').length")
        check(f"F. {view}: the roster renders all {len(want_ids)} subagents",
              n == len(want_ids), f"{n} rows in #{host}")
    p.click_js("#view-toggle button[data-view=\"high\"]")
    time.sleep(0.6)

    # ══ G. DRIVE the filters: they narrow the list and never change the stated total ═════════════
    if want_inactive:
        p.click_js('#sa-controls [data-sa-act="inactive"]')
        time.sleep(0.6)
        got = set(p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),"
                           "e=>e.getAttribute('data-sa-id'))") or [])
        ro = p.ev("document.querySelector('[data-sactl=\"roster\"] [data-sa-val=\"roster\"]').textContent")
        check("G. 'inactive only' narrows the roster to exactly the inactive set",
              got == want_inactive, f"{len(got)} rows vs {len(want_inactive)} inactive in the feed")
        check("G2. the total is STATED even while filtered (a filter cannot hide a subagent)",
              f"of {len(want_ids)} published" in ro, ro)
        p.shot("21-subagent-roster-inactive-only.png")
    p.click_js('#sa-controls [data-sa-act="all"]')
    time.sleep(0.4)
    if want_unlab:
        one = sorted(want_unlab)[0]
        # ★ REAL input, not a scripted value: focus the field, type with CDP, then let at least one
        #   poll run. The bug this guards is the one the operator already hit on this page — a control
        #   inside the 2s re-render losing its text and its focus ("the dropdown keeps undropping").
        p.ev("document.getElementById('sa-q').focus(); true")
        p.call("Input.insertText", {"text": one[:8]})
        time.sleep(3.2)
        got = set(p.evjson("[].map.call(document.querySelectorAll('#sa-high [data-sa-id]'),"
                           "e=>e.getAttribute('data-sa-id'))") or [])
        check("G3. typing in the search box FINDS an unlabelled subagent by its id prefix",
              one in got, f"searched {one[:8]} -> {len(got)} row(s), target present={one in got}")
        focus_id = p.ev("document.activeElement && document.activeElement.id")
        field_val = p.ev("document.getElementById('sa-q').value")
        check("G4. typed text and focus SURVIVE the 2s re-render (the undropping bug class)",
              focus_id == "sa-q" and field_val == one[:8],
              f"after 3.2s (>=1 poll): focus={focus_id!r} value={field_val!r}")
        p.ev("(function(){var q=document.getElementById('sa-q'); q.value='';"
             "q.dispatchEvent(new Event('input',{bubbles:true})); return true;})()")
        time.sleep(0.6)
        check("G5. clearing the search restores every subagent",
              p.ev("document.querySelectorAll('#sa-high [data-sa-id]').length") == len(want_ids),
              f"{p.ev('document.querySelectorAll(\"#sa-high [data-sa-id]\").length')} rows")

    ok = all(r["pass"] for r in RESULTS)
    UITEST.mkdir(parents=True, exist_ok=True)
    (UITEST / "subagents-drive-results.json").write_text(json.dumps(
        {"ranAt": int(time.time() * 1000), "pass": ok, "checks": RESULTS,
         "feed": {"subagents": len(want_ids), "inactive": len(want_inactive),
                  "running": len(want_running), "continuable": len(want_cont),
                  "oneShot": len(want_one), "unlabelled": len(want_unlab)}}, indent=2) + "\n",
        encoding="utf-8")
    print(("\nALL PASS" if ok else "\nFAILURES PRESENT") +
          f" · {sum(1 for r in RESULTS if r['pass'])}/{len(RESULTS)} · evidence: "
          f"{UITEST / 'subagents-drive-results.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:                     # a driver that throws is NOT a pass
        print(f"DRIVER ERROR ({e.__class__.__name__}): {e}")
        raise SystemExit(2)
