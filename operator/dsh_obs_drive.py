#!/usr/bin/env python3
"""DRIVE the DSH Activity popout in the operator's own Chrome (:9222) and assert what happened.

"A UI feature is not reported until it has been DRIVEN in the live app." This drives it with REAL
mouse and keyboard events through CDP, asserts a state CHANGE after every click, re-asserts the
open/shut state ACROSS the 2s poll (the undropping regression), drives the count/density/text
controls, drives the enlarged-image viewer (open / next / prev / contact sheet / Escape), and
measures the panel grid's column count at a NARROW and a WIDE viewport.

Every step writes a PNG of the driven state into var/dsh-observability/uitest/ and a PASS/FAIL
line. Exit 0 only if every assertion passed. Read-only toward the harness; the only store it
writes is /operator/dsh-jobs/screenshot-mode, whose operator value it restores at the end, and it
finishes by clearing the view preferences this run set so the page is left at its defaults.
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
PAGE_MATCH = "/operator/dsh-jobs"
ROOT = Path(__file__).resolve().parent.parent
UITEST = ROOT / "var" / "dsh-observability" / "uitest"
BASE = "http://127.0.0.1:7842"

RESULTS: list[dict] = []
SHOTS: list[str] = []


def jsbool(v) -> bool:
    """JSON.stringify(true) crosses CDP as the STRING "true" — compare as data, not identity."""
    return v is True or v == "true"


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"check": name, "pass": bool(passed), "detail": detail})
    print(("PASS  " if passed else "FAIL  ") + name + ("  — " + detail if detail else ""), flush=True)


def store_enabled() -> bool:
    d = json.loads(urllib.request.urlopen(BASE + "/operator/dsh-jobs/data", timeout=6).read())
    return bool(d["screenshotMode"]["enabled"])


class Page:
    def __init__(self) -> None:
        self._id = 0
        self._ov_w = 900
        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5))
        page = next((t for t in targets if t.get("type") == "page" and PAGE_MATCH in (t.get("url") or "")), None)
        if page is None:
            raise SystemExit("no DSH Activity page open in the operator's Chrome (:9222)")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=25, suppress_origin=True)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            r = json.loads(self.ws.recv())
            if r.get("id") == self._id:
                return r

    def ev(self, expr: str):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            raise RuntimeError("JS error: " + json.dumps(res["exceptionDetails"])[:300])
        return res.get("result", {}).get("value")

    def rect(self, selector: str):
        return self.ev(f"""(()=>{{const e=document.querySelector({json.dumps(selector)});
            if(!e) return null; const r=e.getBoundingClientRect();
            if(r.width===0||r.height===0) return null;
            return {{x:r.left+r.width/2, y:r.top+r.height/2}};}})()""")

    def click(self, selector: str, verify: str | None = None, expect=True, attempts: int = 5) -> bool:
        """A REAL mouse click at the element's real centre.

        ★ THE PAGE RE-RENDERS EVERY 2s, and a re-render moves the element between the moment its
        rect is measured and the moment the event lands — measured: a stale rect put a click on a
        stray DIV instead of the control. So: scroll, measure and dispatch back to back with no
        sleep in between, and when `verify` is supplied, retry with a FRESH measurement until the
        expected state actually holds. A retry happens ONLY while the state is wrong, so it can
        never double-toggle a control.
        """
        hit = False
        for a in range(1, attempts + 1):
            self.ev(f"""(function(){{var e=document.querySelector({json.dumps(selector)});
                if(e&&e.scrollIntoView) e.scrollIntoView({{block:'center'}}); return true;}})()""")
            r = self.rect(selector)
            if r:
                for kind in ("mousePressed", "mouseReleased"):
                    self.call("Input.dispatchMouseEvent", {"type": kind, "x": r["x"], "y": r["y"],
                                                           "button": "left", "clickCount": 1, "buttons": 1})
                hit = True
            else:
                print(f"      (click target not visible: {selector})", flush=True)
            time.sleep(0.55)
            if verify is None:
                return hit
            if jsbool(self.ev(verify)) == expect:
                if a > 1:
                    print(f"      (needed {a} attempts — the 2s poll had moved the target)", flush=True)
                return True
        print(f"      ({attempts} attempts never satisfied: {verify})", flush=True)
        return False

    def key(self, text: str, code: str = "", key: str = "") -> None:
        for kind in ("keyDown", "keyUp"):
            p = {"type": kind, "key": key or text, "code": code or ("Key" + text.upper()),
                 "windowsVirtualKeyCode": 0, "nativeVirtualKeyCode": 0}
            if kind == "keyDown":
                p["text"] = text
            self.call("Input.dispatchKeyEvent", p)
        time.sleep(0.08)

    def shot(self, name: str) -> str:
        UITEST.mkdir(parents=True, exist_ok=True)
        out = UITEST / (name + ".png")
        r = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        out.write_bytes(base64.b64decode(r["result"]["data"]))
        SHOTS.append(str(out))
        return str(out)

    def viewport(self, w: int, h: int = 1000) -> None:
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
        time.sleep(0.7)

    def css_width(self, target: int) -> int:
        """Set the override until the page's CSS layout width is ~= target.

        The operator's Chrome carries a page zoom, so an override width is NOT the CSS width.
        Measuring beats assuming — the first run asked for 760px and got 993px of CSS width,
        which is why its "narrow" case came out with 2 columns. This closes the loop instead.
        """
        for _ in range(5):
            self.viewport(self._ov_w)
            w = int(self.ev("document.documentElement.clientWidth") or 0)
            if abs(w - target) <= 40:
                return w
            self._ov_w = max(320, int(self._ov_w * target / max(1, w)))
        return w

    def clear_viewport(self) -> None:
        self.call("Emulation.clearDeviceMetricsOverride")
        time.sleep(0.4)


COLS = """(()=>{const h=document.getElementById('agent-panels');
  const cs=getComputedStyle(h).gridTemplateColumns;
  return JSON.stringify({cols:cs.split(' ').filter(x=>x&&x!=='none').length,
    raw:cs, docW:document.documentElement.clientWidth,
    hostW:Math.round(h.getBoundingClientRect().width),
    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    note: (document.getElementById('layout-note')||{}).textContent||''});})()"""

LB = """(function(){var el=document.getElementById('lb'),img=document.getElementById('lb-img'),
  g=document.getElementById('lb-grid');
  return JSON.stringify({hidden:el.hidden, src:img.getAttribute('src'),
    imgW:Math.round(img.getBoundingClientRect().width),
    imgLoaded:!!(img.complete&&img.naturalWidth>0),
    gridHidden:g.hidden, gridCells:g.querySelectorAll('.lb-gridcell').length,
    title:document.getElementById('lb-title').textContent,
    bodyOverflow:document.body.style.overflow});})()"""


def main() -> int:
    p = Page()
    try:
        # ── 0. clear stored view prefs (so the DEFAULTS are asserted, not the last run's), reload
        #       so the page is the SERVED build, then wait for the controls ──
        try:
            p.ev("['dshobs.ctl.v3','dshobs.density.v3'].forEach(k=>localStorage.removeItem(k));"
                 "sessionStorage.removeItem('dshobs.openPanels.v3'); true")
        except Exception:
            pass
        p.call("Page.reload", {"ignoreCache": True})
        for _ in range(50):
            time.sleep(0.5)
            try:
                if p.ev("!!document.querySelector('#controls [data-ctlbox]')"):
                    break
            except Exception:
                pass
        time.sleep(2.0)
        check("page reloaded and the new controls exist",
              bool(p.ev("!!document.querySelector('#controls [data-ctlbox]')")),
              str(p.ev("document.querySelectorAll('#controls [data-ctlbox]').length")) + " control boxes")

        # ── 1. controls carry current value + default + range (criterion 1) ──
        rows = json.loads(p.ev("""JSON.stringify([].map.call(document.querySelectorAll('#controls [data-ctlbox]'),function(b){
            const i=b.querySelector('input'); return {box:b.getAttribute('data-ctlbox'),
              num:i?i.value:null, min:i?i.min:null, max:i?i.max:null,
              read:b.querySelector('[data-val-for]').textContent,
              presets:[].map.call(b.querySelectorAll('.chip'),c=>c.getAttribute('data-val')).join(',')};}))"""))
        by = {r["box"]: r for r in rows}
        check("four numeric controls + density control rendered", len(rows) == 5, ", ".join(r["box"] for r in rows))
        exp = {"agents": ("10", "1", "60", "1,5,10,20,40,all"),
               "events": ("5", "1", "100", "1,5,10,20,40,all"),
               "shots": ("5", "1", "40", "1,5,10,20,40,all"),
               "chars": ("240", "40", "2000", "80,140,240,500,all")}
        for k, (v, mn, mx, ps) in exp.items():
            r = by.get(k, {})
            check(f"control {k}: value={r.get('num')} min={r.get('min')} max={r.get('max')} presets={r.get('presets')}",
                  (r.get("num"), r.get("min"), r.get("max"), r.get("presets")) == (v, mn, mx, ps),
                  "readout: " + str(r.get("read")))
        check("density control offers compact + full and states the default",
              by.get("density", {}).get("presets") == "compact,full" and "default compact" in str(by.get("density", {}).get("read")),
              str(by.get("density", {}).get("read")))

        # ── 2. baseline ──
        check("default view is High", p.ev("getComputedStyle(document.getElementById('view-high')).display") == "block")
        n_panels = p.ev("document.querySelectorAll('#agent-panels details[data-key]').length")
        check("agent panels + captures panel rendered", (n_panels or 0) >= 2, f"{n_panels} panels")
        check("captures panel is OPEN by default (screenshots visible without a click)",
              jsbool(p.ev("(function(){var d=document.querySelector('details[data-key=\"shots:captures\"]');return d?d.open:false;})()")))
        p.shot("01-high-default-collapsed-agent-panels")

        # ── 3. THE UNDROPPING REGRESSION, both directions, across 3 poll cycles ──
        CP = 'document.querySelector(\'details[data-key="shots:captures"]\')'
        was_open = jsbool(p.ev(f"{CP}.open"))
        closed_ok = p.click('details[data-key="shots:captures"] > summary', verify=f"{CP}.open", expect=False)
        now_closed = p.ev(f"{CP}.open")
        time.sleep(6.4)
        still_closed = p.ev(f"{CP}.open")
        check("captures panel: the click SHUT it (state actually changed)", was_open and now_closed is False and closed_ok,
              f"open {was_open} -> {now_closed}")
        check("captures panel: it STAYS shut across 3 polls", still_closed is False,
              f"after 6.4s (>=3 polls) open={still_closed}")
        p.click('details[data-key="shots:captures"] > summary', verify=f"{CP}.open", expect=True)
        time.sleep(6.4)
        still_open = p.ev(f"{CP}.open")
        check("captures panel: a click OPENS it and it STAYS open across 3 polls (the bug he reported)",
              still_open is True, f"after 6.4s (>=3 polls) open={still_open}")
        p.ev("""(function(){var d=document.querySelector('details[data-key="shots:captures"]');
            var i=d.querySelector('img'); if(i&&i.scrollIntoView) i.scrollIntoView({block:'center'}); return true;})()""")
        time.sleep(3.0)
        imgs = json.loads(p.ev("""(function(){var d=document.querySelector('details[data-key="shots:captures"]');
            return JSON.stringify({imgs:d.querySelectorAll('img').length,
              complete:[].filter.call(d.querySelectorAll('img'),i=>i.complete&&i.naturalWidth>0).length,
              broken:[].filter.call(d.querySelectorAll('img'),i=>i.complete&&i.naturalWidth===0).length});})()"""))
        check("captures panel serves real screenshot bytes (not broken tiles)", imgs["complete"] > 0, json.dumps(imgs))
        p.shot("02-captures-panel-open-and-still-open-after-3-polls")

        # ── 4. PROVE-FAIL for that mechanism: a panel with no stable key cannot survive a poll ──
        p.ev("""(function(){var h=document.getElementById('agent-panels');
            var d=document.createElement('details'); d.setAttribute('data-nokey','probe');
            d.open=true; d.innerHTML='<summary>prove-fail probe: no data-key</summary><div>body</div>';
            h.appendChild(d); return true;})()""")
        time.sleep(2.6)
        remain = json.loads(p.ev("""JSON.stringify({exists:!!document.querySelector('details[data-nokey="probe"]'),
            open:(function(){var d=document.querySelector('details[data-nokey="probe"]');return d?d.open:null;})()})"""))
        check("PROVE-FAIL: a panel with no stable key does NOT survive a poll (the old markup shape)",
              remain["exists"] is False or remain["open"] is False, f"probe after one poll: {remain}")

        # ── 5. per-agent density: compact -> full -> compact, asserted on the CLASS and row count ──
        first = p.ev("[].map.call(document.querySelectorAll('#agent-panels details[data-agent]'),d=>d.getAttribute('data-agent'))[0]")
        sel = f'details[data-agent="{first}"]'
        p.click(sel + ' > summary', verify=f"document.querySelector('{sel}').open", expect=True)
        base_rows = p.ev(f"document.querySelectorAll('{sel} .stream > *').length")
        full_ok = p.click(sel + ' [data-act="density"][data-val="full"]',
                          verify=f"document.querySelector('{sel} .stream').classList.contains('full')", expect=True)
        cls_full = p.ev(f"document.querySelector('{sel} .stream').className")
        rows_full = p.ev(f"document.querySelectorAll('{sel} .stream > *').length")
        check("per-agent density chip switches the stream to FULL and raises the row count",
              full_ok and cls_full == "stream full" and rows_full >= base_rows,
              f"class={cls_full} rows {base_rows}->{rows_full}")
        p.shot("03-per-agent-FULL-density")
        compact_ok = p.click(sel + ' [data-act="density"][data-val="compact"]',
                             verify=f"document.querySelector('{sel} .stream').classList.contains('compact')", expect=True)
        cls_compact = p.ev(f"document.querySelector('{sel} .stream').className")
        rows_compact = p.ev(f"document.querySelectorAll('{sel} .stream > *').length")
        check("per-agent density chip switches back to COMPACT with fewer rows",
              compact_ok and cls_compact == "stream compact" and rows_compact <= rows_full,
              f"class={cls_compact} rows {rows_full}->{rows_compact}")
        check("compact really is 'the last N lines' (bounded by the events+shots controls)",
              rows_compact <= int(p.ev("document.getElementById('num-events').value")) + int(p.ev("document.getElementById('num-shots').value")),
              f"{rows_compact} rows")

        # ── 6. count control: preset, then TYPED, then out-of-range clamped ──
        p.click('#controls [data-ctl="events"][data-val="20"]')
        time.sleep(0.7)
        check("preset chip 20 on 'events / agent' takes effect (field + readout + rows)",
              p.ev("document.getElementById('num-events').value") == "20" and
              "showing 20 of" in p.ev("document.querySelector('[data-val-for=\"events\"]').textContent") and
              p.ev(f"document.querySelectorAll('{sel} .stream > *').length") >= 20,
              p.ev("document.querySelector('[data-val-for=\"events\"]').textContent"))
        p.click('#controls input[data-num="events"]')
        p.ev("document.getElementById('num-events').select()")
        p.key("3")
        time.sleep(0.9)
        typed = p.ev("document.getElementById('num-events').value")
        check("TYPING in the editable field takes effect (3) and the readout follows",
              typed == "3" and "showing 3 of" in p.ev("document.querySelector('[data-val-for=\"events\"]').textContent"),
              f"value={typed} · " + p.ev("document.querySelector('[data-val-for=\"events\"]').textContent"))
        p.ev("document.getElementById('num-events').value='9999'; document.getElementById('num-events').dispatchEvent(new Event('change',{bubbles:true}))")
        time.sleep(0.6)
        check("out-of-range input is CLAMPED and the FIELD shows the clamped value (9999 -> 100)",
              p.ev("document.getElementById('num-events').value") == "100",
              "field=" + str(p.ev("document.getElementById('num-events').value")) + " max=" + str(p.ev("document.getElementById('num-events').max")))
        p.click('#controls [data-ctl="events"][data-val="all"]')
        time.sleep(0.6)
        check("preset 'all' is the HARD cap, not the preset list max",
              p.ev("document.getElementById('num-events').value") == "100")

        # ── 7. text-amount control (measure the LONGEST rendered line, not the first) ──
        longest = f"""(function(){{var m=0; document.querySelectorAll('{sel} .stream .ev .what').forEach(function(e){{m=Math.max(m,e.textContent.length);}}); return m;}})()"""
        p.click('#controls [data-ctl="chars"][data-val="80"]')
        time.sleep(0.7)
        len80 = p.ev(longest)
        p.click('#controls [data-ctl="chars"][data-val="all"]')
        time.sleep(0.7)
        lenall = p.ev(longest)
        check("text/line control changes how much of a line is shown (80 -> all)",
              len80 > 0 and lenall > len80, f"longest rendered line {len80} -> {lenall} chars")
        p.click('#controls [data-ctl="chars"][data-val="240"]')
        p.click('#controls [data-ctl="events"][data-val="5"]')
        time.sleep(0.6)
        p.shot("04-count-and-text-controls-driven")

        # ── 8. shots control + bulk actions + legend ──
        p.click('#controls [data-ctl="shots"][data-val="all"]')
        time.sleep(0.7)
        check("'shots / agent' preset all takes effect",
              p.ev("document.getElementById('num-shots').value") == "40" and
              "showing 40 of" in p.ev("document.querySelector('[data-val-for=\"shots\"]').textContent"),
              p.ev("document.querySelector('[data-val-for=\"shots\"]').textContent"))
        p.click('[data-bulk="expand"]',
                verify="[].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.open)", expect=True)
        allopen = p.ev("JSON.stringify([].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.open))")
        check("bulk 'expand all' opens every panel (one action, not twelve clicks)", jsbool(allopen), f"all open={allopen}")
        p.click('[data-bulk="collapse"]',
                verify="[].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>!d.open)", expect=True)
        allclosed = p.ev("JSON.stringify([].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>!d.open))")
        check("bulk 'collapse all' closes every panel", jsbool(allclosed), f"all closed={allclosed}")

        lr = json.loads(p.ev("""JSON.stringify([].map.call(document.querySelectorAll('#legend .legend-row'),r=>({
            jump:r.getAttribute('data-jump'),
            text:r.textContent.replace(/\\s+/g,' ').trim().slice(0,110)})))"""))
        check("legend lists every agent that carries activity, with what it did",
              len(lr) >= 1 and all(r["jump"] for r in lr), f"{len(lr)} rows; first: {lr[0]['text'] if lr else '-'}")
        p.click('#legend .legend-row', verify="""(function(){var r=document.querySelector('#legend .legend-row');
            var d=document.querySelector('#agent-panels details[data-key="agent:'+r.getAttribute('data-jump')+'"]');
            return d?d.open:false;})()""", expect=True)
        jumped = p.ev("""(function(){var r=document.querySelector('#legend .legend-row');
            var d=document.querySelector('#agent-panels details[data-key="agent:'+r.getAttribute('data-jump')+'"]');
            return d?d.open:false;})()""")
        check("clicking a legend row opens that agent's panel (the cross-link works)", jsbool(jumped))

        # ── 9. keyboard path (no mouse) ──
        v = []
        for k, code, view in (("1", "Digit1", "simple"), ("2", "Digit2", "mid"), ("3", "Digit3", "high")):
            p.key(k, code=code, key=k)
            time.sleep(0.5)
            v.append(p.ev(f"getComputedStyle(document.getElementById('view-{view}')).display"))
        check("keyboard 1/2/3 switch Simple/Mid/High with no mouse", v == ["block"] * 3, str(v))
        p.key("x", code="KeyX", key="x")
        time.sleep(0.9)
        kx = p.ev("JSON.stringify([].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.open))")
        p.key("z", code="KeyZ", key="z")
        time.sleep(0.7)
        kz = p.ev("JSON.stringify([].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>!d.open))")
        check("keyboard x/z expand/collapse every panel", jsbool(kx) and jsbool(kz), f"x={kx} z={kz}")

        # ── 10. THUMBNAILS at 25% + the enlarged viewer ──
        p.click('[data-bulk="expand"]')
        time.sleep(0.9)
        tw = json.loads(p.ev("""(function(){var t=document.querySelector('.shot-tile'),i=t.querySelector('img');
            return JSON.stringify({tag:t.tagName, tabIndex:t.tabIndex,
              tileW:Math.round(t.getBoundingClientRect().width),
              imgW:Math.round(i.getBoundingClientRect().width), src:i.getAttribute('src'),
              natW:i.naturalWidth||0});})()"""))
        check("inline thumbnail IMAGE renders at 59px = EXACTLY 25% of the previous 236px",
              tw["imgW"] == 59 and 59 <= tw["tileW"] <= 63, json.dumps(tw))
        check("the thumbnail is a real focusable control, not a mouse-only click target",
              tw["tag"] == "BUTTON" and tw["tabIndex"] >= 0, f"{tw['tag']} tabIndex={tw['tabIndex']}")
        lb0 = json.loads(p.ev(LB))
        check("the viewer costs ONE hidden element until asked for (no full-size images in the DOM)",
              lb0["hidden"] is True and lb0["gridCells"] == 0 and not lb0["src"],
              f"hidden={lb0['hidden']} gridCells={lb0['gridCells']} src={lb0['src']}")
        p.shot("05-thumbnails-25pct")

        opened_ok = p.click('.shot-tile', verify="!document.getElementById('lb').hidden", expect=True)
        time.sleep(1.6)
        lb1 = json.loads(p.ev(LB))
        check("clicking a thumbnail opens the enlarged viewer",
              opened_ok and lb1["hidden"] is False and lb1["imgW"] > 59 and lb1["imgLoaded"],
              f"img {lb1['imgW']}px (thumb was 59px), src=…{str(lb1['src'])[-24:]}, title={lb1['title'][:44]}")
        check("enlarging does NOT change the page layout (panels unchanged, body scroll locked temporarily)",
              p.ev("document.querySelectorAll('#agent-panels details[data-key]').length") == n_panels and lb1["bodyOverflow"] == "hidden",
              f"panels={n_panels}, bodyOverflow={lb1['bodyOverflow']}")
        p.shot("06-lightbox-enlarged")
        src1 = lb1["src"]
        # ★ compare the TITLE's index, not the file: the writer adds a shot every 8s, so the ring
        #   shifts and a file that was #1 becomes #2. The INDEX is the stable observable.
        idx1 = int(lb1["title"].split(" of ")[0])

        p.key("", code="ArrowRight", key="ArrowRight")
        time.sleep(1.3)
        lb2 = json.loads(p.ev(LB))
        idx2 = int(lb2["title"].split(" of ")[0])
        check("ARROW-KEY right steps to the next image (index advances, src changes)",
              idx2 == idx1 + 1 and lb2["src"] != src1 and lb2["imgLoaded"],
              f"index {idx1} -> {idx2}; …{str(src1)[-20:]} -> …{str(lb2['src'])[-20:]}")
        p.key("", code="ArrowLeft", key="ArrowLeft")
        time.sleep(1.3)
        lb3 = json.loads(p.ev(LB))
        idx3 = int(lb3["title"].split(" of ")[0])
        check("ARROW-KEY left steps back (index returns)", idx3 == idx1,
              f"index {idx2} -> {idx3} (expected {idx1}); src …{str(lb3['src'])[-20:]}")
        p.click('#lb [data-lb="next"]', verify="!document.getElementById('lb').hidden", expect=True)
        time.sleep(0.9)
        lb4 = json.loads(p.ev(LB))
        check("the on-screen 'next' BUTTON also steps (mouse and keyboard both work)",
              int(lb4["title"].split(" of ")[0]) == idx1 + 1 and lb4["src"] != src1,
              f"title={lb4['title'][:44]}")
        p.shot("07-lightbox-stepped")

        p.key("g", code="KeyG", key="g")
        time.sleep(1.8)
        lb5 = json.loads(p.ev(LB))
        check("'▦ all shots' opens a bounded contact sheet of the whole ring",
              0 < lb5["gridCells"] <= 40 and lb5["gridHidden"] is False,
              f"{lb5['gridCells']} cells (ring cap 40)")
        p.shot("08-lightbox-contact-sheet")
        p.click('#lb [data-lb-cell="0"]',
                verify="document.getElementById('lb-title').textContent.indexOf('1 of ')===0", expect=True)
        time.sleep(1.0)
        lb6 = json.loads(p.ev(LB))
        check("clicking a contact-sheet cell selects THAT shot (title jumps to 1 of N)",
              lb6["title"].startswith("1 of "), f"title={lb6['title'][:44]}")

        before_esc = json.loads(p.ev("""JSON.stringify({panels:document.querySelectorAll('#agent-panels details[data-key]').length,
            open:document.querySelectorAll('#agent-panels details[data-key][open]').length,
            scrollY:Math.round(window.scrollY), hash:location.hash})"""))
        p.key("", code="Escape", key="Escape")
        time.sleep(1.3)
        lb7 = json.loads(p.ev(LB))
        after_esc = json.loads(p.ev("""JSON.stringify({panels:document.querySelectorAll('#agent-panels details[data-key]').length,
            open:document.querySelectorAll('#agent-panels details[data-key][open]').length,
            scrollY:Math.round(window.scrollY), hash:location.hash})"""))
        check("ESCAPE dismisses the viewer", lb7["hidden"] is True, f"hidden={lb7['hidden']}")
        check("dismissal leaves NOTHING behind (no enlarged src, no contact sheet, scroll lock released)",
              not lb7["src"] and lb7["gridCells"] == 0 and lb7["bodyOverflow"] == "",
              f"src={lb7['src']} cells={lb7['gridCells']} bodyOverflow='{lb7['bodyOverflow']}'")
        check("the underlying layout is UNCHANGED after dismissal (same panels, open count, scroll)",
              before_esc["panels"] == after_esc["panels"] and before_esc["open"] == after_esc["open"]
              and abs(before_esc["scrollY"] - after_esc["scrollY"]) <= 2,
              f"{before_esc} -> {after_esc}")
        p.shot("09-after-escape-layout-unchanged")

        p.click('.shot-tile', verify="!document.getElementById('lb').hidden", expect=True)
        time.sleep(0.8)
        p.ev("document.getElementById('lb').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        time.sleep(0.7)
        check("a backdrop click also dismisses the viewer (no dead end)",
              jsbool(p.ev("document.getElementById('lb').hidden")))

        # ── 11. RESPONSIVE: measured at a genuinely NARROW and a WIDE CSS width ──
        p.click('[data-bulk="expand"]')
        time.sleep(0.9)
        order_before = p.ev("JSON.stringify([].map.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.getAttribute('data-key')))")
        w_narrow = p.css_width(560)
        narrow = json.loads(p.ev(COLS))
        p.shot("10-responsive-NARROW")
        w_wide = p.css_width(1900)
        wide = json.loads(p.ev(COLS))
        p.shot("11-responsive-WIDE")
        order_after = p.ev("JSON.stringify([].map.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.getAttribute('data-key')))")
        check("grid REFLOWS: one column when narrow, several when wide (auto, no toggle)",
              narrow["cols"] == 1 and wide["cols"] >= 3,
              f"{w_narrow}px CSS -> {narrow['cols']} col(s); {w_wide}px CSS -> {wide['cols']} col(s)")
        check("the column count follows the CONTAINER width",
              narrow["hostW"] < 840 and wide["hostW"] >= 840,
              f"container {narrow['hostW']}px -> {narrow['cols']} cols · {wide['hostW']}px -> {wide['cols']} cols")
        check("no horizontal overflow at EITHER width",
              narrow["overflowX"] <= 0 and wide["overflowX"] <= 0,
              f"narrow overflowX={narrow['overflowX']}px, wide overflowX={wide['overflowX']}px")
        check("no panel is wider than its column (nothing clipped or spilling)",
              jsbool(p.ev("""[].every.call(document.querySelectorAll('#agent-panels>.agent-panel'),
                d=>d.getBoundingClientRect().width <= d.parentElement.getBoundingClientRect().width+1)""")),
              f"host {narrow['hostW']}px/{wide['hostW']}px")
        check("the page tells him the current column count", "column(s) at this width" in (wide.get("note") or ""),
              wide.get("note"))
        check("expansion SURVIVES the 1-col -> many-col reflow",
              jsbool(p.ev("JSON.stringify([].every.call(document.querySelectorAll('#agent-panels details[data-key]'),d=>d.open))")))
        check("panel ORDER is unchanged by the reflow (same logical position)",
              order_before == order_after,
              f"{len(json.loads(order_after or '[]'))} panels in identical order")
        p.click('#controls [data-ctl="events"][data-val="10"]')
        time.sleep(0.8)
        wide_rows = p.ev("document.querySelectorAll('#agent-panels details[data-agent] .stream > *').length")
        check("the count control still drives the panels in the WIDE layout (no layout-only control)",
              p.ev("document.getElementById('num-events').value") == "10" and wide_rows > 0,
              f"events=10, rows={wide_rows}")
        p.click('.shot-tile', verify="!document.getElementById('lb').hidden", expect=True)
        time.sleep(1.0)
        check("the enlarged viewer still opens at the WIDE width",
              jsbool(p.ev("!document.getElementById('lb').hidden")))
        p.key("", code="Escape", key="Escape")
        time.sleep(0.7)
        p.click('#controls [data-ctl="events"][data-val="5"]')
        p.clear_viewport()
        time.sleep(0.9)

        # ── 12. the screenshot flag: still honest, still not enforced ──
        p.click('#shot-mode button[data-shots="off"]')
        # the server calls the writer "not honouring it" only after a shot lands past a 12s grace
        # window — asserting sooner would assert "unknown", not the claim.
        time.sleep(15.0)
        d = json.loads(p.ev("""JSON.stringify({on:document.querySelector('#shot-mode button[data-shots="on"]').className,
            off:document.querySelector('#shot-mode button[data-shots="off"]').className,
            note:document.getElementById('shot-mode-note').textContent,
            warn:document.getElementById('mode-warn').textContent})"""))
        check("clicking Off flips the STORE (not just the paint)", store_enabled() is False)
        check("the Off state is distinct (unhonored) and the page says capture did NOT stop",
              "unhonored" in d["off"] and "Nothing was stopped." in d["warn"] and "NOT honouring" in d["note"],
              d["note"] + " | " + d["warn"][:90])
        p.shot("12-screenshot-off-unhonored-honest")
        p.click('#shot-mode button[data-shots="on"]')
        time.sleep(2.5)
        check("clicking On flips the store back", store_enabled() is True)

        # ── 13. the three states must LOOK different: loading vs empty vs error (criterion 8),
        #        and the viewer must be unreachable with no shots. Driven by patching window.fetch
        #        INSIDE the page for one poll — the operator's real state file is never touched. ──
        p.ev("""window.__origFetch = window.fetch; window.fetch = function(u, o){
            if(String(u).indexOf('/operator/dsh-jobs/data') >= 0){
              return window.__origFetch(u, o).then(function(r){ return r.json(); }).then(function(j){
                j.screenshots = []; j.shotAttribution = {byAgentId:{}, unattributed:[], totalShots:0,
                  attributedCount:0, ambiguousCount:0, health:'no-shots', joinKey:'(none — no shots)'};
                return new Response(JSON.stringify(j), {status:200, headers:{'Content-Type':'application/json'}});
              });
            }
            return window.__origFetch(u, o); }; true""")
        time.sleep(3.0)
        empty = json.loads(p.ev("""JSON.stringify({thumbs:document.querySelectorAll('.shot-tile').length,
            captures:(!!document.querySelector('details[data-key="shots:captures"]')),
            lbHidden:document.getElementById('lb').hidden,
            health:document.getElementById('shot-health').textContent.slice(0,90),
            lbCells:document.getElementById('lb-grid').children.length})"""))
        p.key("g", code="KeyG", key="g")
        time.sleep(0.8)
        empty_lb = jsbool(p.ev("document.getElementById('lb').hidden"))
        check("EMPTY state (0 shots): no thumbnails, no captures panel, viewer unreachable and still hidden",
              empty["thumbs"] == 0 and empty["captures"] is False and empty["lbHidden"] and empty_lb,
              f"{empty['thumbs']} thumbs, captures={empty['captures']}, lbHidden={empty_lb}, health='{empty['health']}'")
        check("EMPTY state TEACHES (says what will appear here)", "No screenshots on disk" in empty["health"],
              empty["health"])
        p.shot("13-empty-state-no-shots")

        p.ev("""window.fetch = function(u, o){
            if(String(u).indexOf('/operator/dsh-jobs/data') >= 0){
              return Promise.resolve(new Response('{"error":"driven failure"}', {status:500}));
            }
            return window.__origFetch(u, o); }; true""")
        time.sleep(3.0)
        err = json.loads(p.ev("""JSON.stringify({big:document.querySelectorAll('#load-state .big-error').length,
            retry:!!document.getElementById('retry-btn'),
            badge:document.getElementById('source').textContent,
            text:document.getElementById('load-state').textContent.slice(0,120)})"""))
        check("ERROR state is distinct from empty, and names cause + remedy + a retry control in place",
              err["big"] == 1 and err["retry"] and "HTTP 500" in err["badge"],
              f"big-error={err['big']} retry={err['retry']} badge='{err['badge']}'")
        p.shot("14-error-state-with-remedy")
        p.ev("window.fetch = window.__origFetch; true")
        p.call("Page.reload", {"ignoreCache": True})
        time.sleep(4.5)
        check("fetch restored and the page recovered to live data",
              p.ev("document.getElementById('source').className").find("stub") < 0 and
              p.ev("document.getElementById('meta').textContent").find("shots") >= 0,
              str(p.ev("document.getElementById('meta').textContent"))[:70])

        # ── 14. bounds ──
        rowsn = p.ev("document.querySelectorAll('#agent-panels .stream > *').length")
        detailsn = p.ev("document.querySelectorAll('#agent-panels details[data-key]').length")
        check("DOM is bounded: panels <= 60 and total stream rows <= 3000",
              detailsn <= 61 and rowsn <= 3000, f"{detailsn} panels, {rowsn} stream rows")

        # ── 15. leave the page as we found it: defaults, nothing open, viewer hidden ──
        p.ev("localStorage.removeItem('dshobs.ctl.v3'); localStorage.removeItem('dshobs.density.v3'); sessionStorage.removeItem('dshobs.openPanels.v3'); true")
        p.call("Page.reload", {"ignoreCache": True})
        time.sleep(4.5)
        check("page restored to defaults after the run (no view state left behind)",
              p.ev("document.getElementById('num-events').value") == "5" and
              p.ev("document.getElementById('num-shots').value") == "5" and
              jsbool(p.ev("document.getElementById('lb').hidden")),
              "events=5 shots=5 viewer hidden")
    finally:
        try:
            p.clear_viewport()
        except Exception:
            pass
        p.close()

    ok = all(r["pass"] for r in RESULTS)
    UITEST.mkdir(parents=True, exist_ok=True)
    out = UITEST / "drive-results.json"
    out.write_text(json.dumps({"pass": ok, "checks": RESULTS, "screenshots": SHOTS}, indent=2) + "\n", encoding="utf-8")
    print(f"\n{'ALL PASS' if ok else 'FAILURES PRESENT'} · {sum(1 for r in RESULTS if r['pass'])}/{len(RESULTS)} · evidence: {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
