#!/usr/bin/env python3
"""Drive the DSH Activity popout in the operator's OWN Chrome over CDP (:9222).

WHY THIS EXISTS: the panel's rule is "a UI feature is not reported until it has been DRIVEN in the
live app". Rendering is not driving. This talks to the already-open page on the operator's Chrome
(port 9222, the CDP port `operator/voice_in.py` names as "panel") and either evaluates JS, clicks a
real element with a real mouse event at its real centre, watches a selector over time, or grabs a
PNG of the driven state into var/dsh-observability/uitest/.

It reads state; it never touches the harness, the plugin, or any store.

  python3 operator/dsh_obs_uitest.py eval  '<js expression>'
  python3 operator/dsh_obs_uitest.py click '<css selector>'
  python3 operator/dsh_obs_uitest.py shot  '<name>'            -> var/dsh-observability/uitest/<name>.png
  python3 operator/dsh_obs_uitest.py watch '<css selector>' <seconds> <interval>
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
UITEST_DIR = Path(__file__).resolve().parent.parent / "var" / "dsh-observability" / "uitest"


class Page:
    def __init__(self) -> None:
        self.ws = None
        self._id = 0

    def __enter__(self) -> "Page":
        targets = json.load(urllib.request.urlopen(
            f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5))
        page = next((t for t in targets
                     if t.get("type") == "page" and PAGE_MATCH in (t.get("url") or "")), None)
        if page is None:
            raise SystemExit(f"no open page matching {PAGE_MATCH} on CDP :{CDP_PORT}; "
                             f"open http://127.0.0.1:7842/operator/dsh-jobs in his Chrome first")
        # suppress_origin: Chrome 111+ rejects WS handshakes carrying an Origin header.
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=15,
                                              suppress_origin=True)
        self.title = page.get("title")
        return self

    def __exit__(self, *exc) -> None:
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
        r = self.call("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True, "awaitPromise": True})
        res = r.get("result", {})
        if res.get("exceptionDetails"):
            return "JS-ERROR: " + json.dumps(res["exceptionDetails"])[:400]
        return res.get("result", {}).get("value")

    def rect(self, selector: str):
        return self.ev(f"""(()=>{{const e=document.querySelector({json.dumps(selector)});
            if(!e) return null; const r=e.getBoundingClientRect();
            return {{x:r.left+r.width/2, y:r.top+r.height/2, w:r.width, h:r.height}};}})()""")

    def click(self, selector: str):
        """A REAL mouse press+release at the element's real screen point — not el.click()."""
        r = self.rect(selector)
        if not r:
            raise SystemExit(f"click: no element matches {selector}")
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent",
                      {"type": kind, "x": r["x"], "y": r["y"], "button": "left",
                       "clickCount": 1, "buttons": 1})
            time.sleep(0.06)
        return r

    def shot(self, name: str) -> Path:
        UITEST_DIR.mkdir(parents=True, exist_ok=True)
        out = UITEST_DIR / (name if name.endswith(".png") else name + ".png")
        r = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        data = r.get("result", {}).get("data")
        if not data:
            raise SystemExit(f"screenshot failed: {json.dumps(r)[:300]}")
        out.write_bytes(base64.b64decode(data))
        return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    with Page() as p:
        if cmd == "eval":
            print(p.ev(argv[2]))
        elif cmd == "click":
            print(json.dumps(p.click(argv[2])))
        elif cmd == "shot":
            print(p.shot(argv[2]))
        elif cmd == "watch":
            sel, secs, every = argv[2], float(argv[3]), float(argv[4]) if len(argv) > 4 else 0.5
            end = time.time() + secs
            while time.time() < end:
                print(f"{time.strftime('%H:%M:%S')} {p.ev(sel)}")
                time.sleep(every)
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
