#!/usr/bin/env python3
"""khaoss_dsh_card_drive.py — DRIVE the DSH launcher card in the operator's own browser and assert,
IN THE RENDERED DOM, that it exists, paints a real verdict, and carries working controls.

Why CDP and not a source-grep: "the control is in the HTML" cost this estate weeks. A card can be in
the file and absent from the window — the panel serves a page the operator reloads constantly, and
the whole point of this card is that he can reach his harness from it. So this asserts the DOM.

Prove-fail (AGENTS.md §2): `KHAOSS_DSH_CARD_PROVE_FAIL=1` blanks the badge text in the DOM and the
verdict assertion MUST go red. Without that, a passing run proves only that the page loaded.

Usage: python3 operator/khaoss_dsh_card_drive.py        (exit 0 = all held)
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = os.environ.get("KHAOSS_CDP", "http://127.0.0.1:9230")
PANEL_HINT = "127.0.0.1:7842"
SHOT = Path.home() / "boardwalk_dsh_card_shots"
PROVE_FAIL = os.environ.get("KHAOSS_DSH_CARD_PROVE_FAIL") == "1"

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        fails.append(name)


def pick_target() -> str:
    with urllib.request.urlopen(f"{CDP_HTTP}/json/list", timeout=8) as r:
        targets = json.load(r)
    for t in targets:
        if t.get("type") == "page" and PANEL_HINT in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit(f"khaoss_dsh_card_drive: no panel page on {PANEL_HINT} in CDP {CDP_HTTP}")


class CDP:
    def __init__(self, url: str) -> None:
        # ★ suppress_origin=True: Chromium 403s a CDP WebSocket carrying an Origin header
        #   ("Rejected an incoming WebSocket connection from the http://127.0.0.1:9230 origin").
        #   Measured here, and the same option the estate's other CDP drivers already pass.
        self.ws = websocket.create_connection(url, timeout=25, suppress_origin=True)
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    cdp = CDP(pick_target())
    try:
        print("=== DSH launcher card — driven in the operator's browser ===")

        # ★ A · THE CARD IS IN THE RENDERED DOM **AND VISIBLE**. 2026-09-23: the first version of this
        #       check asked only `querySelector(...) !== null`, and it PASSED while the card measured
        #       **0x0** — this panel is SECTION-SWITCHED, so a card can exist in the DOM inside a
        #       container with `display:none` and be completely absent from the operator's screen.
        #       ★ That is this estate's oldest defect wearing a new coat: "the control is in the HTML"
        #       is not "the operator can see it". A card added for the express purpose of being a way
        #       back in must be asserted VISIBLE, by measurement, not by presence.
        box = cdp.js("""(function(){var e=document.querySelector('[data-svc="dsh"]'); if(!e) return null;
            var r=e.getBoundingClientRect();
            return JSON.stringify({w:Math.round(r.width),h:Math.round(r.height)});})()""")
        dims = json.loads(box) if box else None
        check("A · the DSH card is present in the RENDERED DOM", dims is not None, "querySelector found nothing")
        check("A · the card is VISIBLE (non-zero box), not merely present",
              bool(dims) and dims.get("w", 0) > 0 and dims.get("h", 0) > 0,
              f"measured box {dims} — a 0x0 card is in the DOM and absent from the screen")

        # ★ B · IT PAINTS A REAL VERDICT. A card that says "checking…" forever is the failure this
        #       estate calls a control that renders but does not work.
        badge = cdp.js("(document.getElementById('dsh-badge')||{}).textContent||''")
        verdict = cdp.js("(document.getElementById('dsh-verdict')||{}).textContent||''")
        check("B · the badge shows a real state, not the placeholder",
              bool(badge) and "checking" not in badge.lower(), f"badge={badge!r}")
        check("B · the verdict row is populated", bool(verdict) and verdict != "—", f"verdict={verdict!r}")

        # ★ C · THE NUMBERS AGREE WITH THE SERVER. C3 is set equality between what the page shows and
        #       what /run/dsh-status returns — the check that catches a card painting stale state.
        state = json.loads(urllib.request.urlopen("http://127.0.0.1:7842/run/dsh-status", timeout=15).read())
        unit_dom = cdp.js("(document.getElementById('dsh-unitstate')||{}).textContent||''")
        http_dom = cdp.js("(document.getElementById('dsh-http')||{}).textContent||''")
        check("C · the DOM's unit state equals the server's",
              unit_dom == (state.get("unit_state") or ""), f"dom={unit_dom!r} server={state.get('unit_state')!r}")
        check("C · the DOM's http code equals the server's",
              (state.get("http") or "") in http_dom, f"dom={http_dom!r} server={state.get('http')!r}")

        # ★ D · THE TOKEN ROW DISTINGUISHES "serving" FROM "openable". This is the card's whole value:
        #       the operator's real lockout was a server that was UP and un-openable.
        tok_dom = cdp.js("(document.getElementById('dsh-token')||{}).textContent||''")
        want = ("recoverable" if state.get("has_token") is True
                else "none" if state.get("has_token") is False else "—")
        check("D · the token row reports openability, not just liveness",
              want in tok_dom, f"dom={tok_dom!r} expected~{want!r}")

        # ★ E · THE CONTROLS EXIST AND ARE BOUND to real handlers.
        bound = cdp.js("typeof window.dshLaunch==='function' && typeof window.dshOpen==='function'")
        check("E · Launch and Open are bound to real handlers", bound is True, f"typeof={bound!r}")

        # ★★ F · THE ENDPOINTS ACTUALLY ANSWER — AND THIS IS THE ASSERTION THAT WAS MISSING.
        #   2026-09-23: the card rendered, every control was bound, and `POST /run/dsh-launch`
        #   returned a DEAD SOCKET. The handler raised `UnboundLocalError` on a variable bound only
        #   inside another branch, so no response was sent, nothing was logged (`log_message` runs
        #   only after a reply), and the page saw "Failed to fetch". ★ Every assertion above passed
        #   throughout. A card is only a way back in if its button REACHES the server, so the shape
        #   asserted here is deliberately weak and therefore hard to fool: **any HTTP status at all
        #   proves the route is reachable** — 500 would still pass this, and that is intentional,
        #   because reachability and correctness are different claims and only reachability belongs
        #   in a plumbing check. A dead socket yields status 0 and fails.
        for ep in ("/run/dsh-launch", "/run/dsh-url"):
            status = cdp.js(
                "fetch(%r,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})"
                ".then(function(r){return r.status;}).catch(function(){return 0;})" % ep)
            check(f"F · POST {ep} answers the page (status != 0)", bool(status),
                  f"status={status!r} — a dead socket is what a crashing handler looks like from the page")

        # ★ PROVE-FAIL · blank the badge and demand the assertion notice.
        if PROVE_FAIL:
            cdp.js("var b=document.getElementById('dsh-badge'); if(b) b.textContent='';")
            after = cdp.js("(document.getElementById('dsh-badge')||{}).textContent||''")
            check("PROVE-FAIL · blanking the badge in the DOM MUST go red",
                  bool(after) and "checking" not in after.lower(), f"badge={after!r}")

        # ★ F · SCREENSHOT — the RENDERS rung, and the artifact a reader can check.
        SHOT.mkdir(parents=True, exist_ok=True)
        rect = cdp.js("""(function(){var e=document.querySelector('[data-svc="dsh"]');
            if(!e) return null; e.scrollIntoView({block:'center'});
            var r=e.getBoundingClientRect();
            return JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)});})()""")
        time.sleep(0.6)
        png = cdp.call("Page.captureScreenshot", format="png").get("data")
        out = SHOT / f"dsh_card_{int(time.time())}.png"
        if png:
            out.write_bytes(base64.b64decode(png))
            print(f"  [ ok ] screenshot: {out}")
        else:
            print("  [warn] screenshot returned no data")

        print()
        if fails:
            print(f"  {len(fails)} FAILED: {', '.join(fails)}")
            return 1
        print("  ALL HELD — the card renders, paints measured state, and carries bound controls.")
        return 0
    finally:
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
