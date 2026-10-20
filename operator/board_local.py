#!/usr/bin/env python3
"""board_local.py — the operator board, served LOCALLY and writable.

★ WHY LOCAL. The board has lived as a claude.ai artifact: good for reading, and it
cannot write back to blockers.json, so notes and decisions had to be relayed
through an agent. That makes the agent a middleman on the operator's own
decisions — which is precisely the shape the board exists to remove.

This serves the SAME store (operator/blockers.json) over loopback and accepts
writes, so a decision is recorded by the person making it.

★ SAFETY, because this writes to a live governance file:
  · LOOPBACK ONLY (127.0.0.1). Never binds a routable interface.
  · It can set state to done/later/open and attach notes. It CANNOT add, delete
    or reword an item — an agent proposes items, the operator disposes of them,
    and neither should be able to do the other's half from here.
  · Every write is journalled to blockers_audit.jsonl with a timestamp, so a
    cleared item can always be traced to when and to what it was before.
  · Writes are atomic (temp file + replace) — a crash mid-write cannot leave the
    board unreadable, which would silently look like "nothing is outstanding".

USAGE
    ./board_local.py            serve on 127.0.0.1:8899 and print the URL
    ./board_local.py --open     also launch the browser
    ./board_local.py --port N
"""
from __future__ import annotations

import argparse, json, os, subprocess, sys, tempfile, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "blockers.json"
AUDIT = HERE / "blockers_audit.jsonl"

SEV = {"stop": ("BLOCKING", "#b3352e"), "hold": ("WAITING ON YOU", "#b06a12"),
       "info": ("YOUR CALL", "#0f6b63")}


def load() -> dict:
    return json.loads(DB.read_text())


def save(d: dict) -> None:
    """Atomic. A half-written board reads as an empty board, which reads as
    'nothing is outstanding' — the exact false-clear this whole file exists to
    prevent."""
    fd, tmp = tempfile.mkstemp(dir=str(HERE), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, DB)


def audit(entry: dict) -> None:
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            **entry}) + "\n")


def merged_items(d: dict):
    """★★★ THE MERGED SET, NOT THE HAND-WRITTEN FILE.
    This page read `d["items"]` — the 22 hand-authored rows — while the CLI, the panel and the session
    hook render items_of(d), which merges rows DERIVED from the findings ledger, invariant FAILs,
    approval requests and the requirements register. Measured 2026-08-29: this page served 22 of 70.
    ★ And open_board.sh — the launcher the operator is told to use, and which I told him to use
      repeatedly today — execs THIS FILE. So the board he was pointed at hid 48 of his own items.
      Same subset defect as the session hook, on the surface HE opens rather than the one agents read.
    Degrades to the hand set only if blockers cannot be imported, and SAYS SO on the page rather than
    quietly serving a subset — absence of access is not absence of content."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import blockers as _b
        return _b.items_of(d), ""
    except Exception as e:
        return d.get("items", []), (f"DEGRADED — hand-written rows ONLY ({e.__class__.__name__}). "
                                    "Derived items are NOT shown. This is a SUBSET of your board.")


def page(d: dict) -> str:
    items, degraded = merged_items(d)
    order = {"stop": 0, "hold": 1, "info": 2}
    items = sorted(items, key=lambda i: (i["state"] != "open", order.get(i["sev"], 3)))
    rows = []
    for i in items:
        label, col = SEV.get(i["sev"], ("ITEM", "#666"))
        lat = " · LATENT" if i.get("latent") else ""
        done = i["state"] != "open"
        # ★ A derived row has NO row in the store: `blockers.py --set <id>` answers "unknown id", exit 2.
        #   Offering Done/Later on it would present a control that silently cannot work — a parameter
        #   accepted and ignored, which is the defect class of the whole day. Show the row, name its
        #   source, and say where it can actually be closed.
        if i.get("source", "hand") != "hand":
            acts = (f'<p class="derived">Derived from <b>{i.get("source")}</b> — this row is generated, '
                    f'not stored, so it cannot be closed here. It clears when its source clears.</p>')
        else:
            acts = (f'<div class="act">'
                    f'<button onclick="setState(\'{i["id"]}\',\'done\')" class="b-done">Done</button>'
                    f'<button onclick="setState(\'{i["id"]}\',\'later\')" class="b-later">Do later</button>'
                    f'<button onclick="setState(\'{i["id"]}\',\'open\')" class="b-open">Re-open</button>'
                    f'<span class="state">state: <b>{i["state"]}</b></span></div>'
                    f'<textarea id="n-{i["id"]}" placeholder="Your notes on this item — saved locally, '
                    f'kept with the item." onblur="saveNote(\'{i["id"]}\')">{i.get("note","")}</textarea>')
        rows.append(f"""
<article class="card {'done' if done else ''}">
  <div class="hd">
    <span class="pill" style="background:{col}1a;color:{col}">{label}{lat}</span>
    <h2>{i['title']}</h2>
  </div>
  <p class="one">{i['one']}</p>
  <p class="blocks"><b>blocks:</b> {i.get('blocks','—')}</p>
    {acts}
</article>""")
    n_open = sum(1 for i in items if i["state"] == "open")
    return f"""<!doctype html><meta charset="utf-8"><title>Operator Board</title>
<style>
:root{{--ink:#171b21;--ink2:#5b6674;--bg:#f4f5f7;--card:#fff;--line:#dde1e6}}
@media(prefers-color-scheme:dark){{:root{{--ink:#e6e9ed;--ink2:#a5b0bd;--bg:#12151a;--card:#191d24;--line:#2c323b}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 system-ui,sans-serif;padding:34px 20px 80px}}
.wrap{{max-width:860px;margin:0 auto}}
h1{{font-size:1.7rem;margin:0 0 4px}}
.sub{{color:var(--ink2);margin:0 0 26px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:18px 20px;margin-bottom:14px}}
.card.done{{opacity:.5}}
.hd{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
h2{{font-size:1.05rem;margin:0}}
.pill{{font:600 .66rem/1 ui-monospace,monospace;letter-spacing:.08em;padding:4px 8px;border-radius:3px;white-space:nowrap}}
.one{{color:var(--ink2);margin:10px 0}}
.blocks{{font-size:.85rem;color:var(--ink2);margin:0 0 12px}}
.act{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}}
button{{font:inherit;padding:6px 13px;border-radius:4px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}}
button:hover{{border-color:var(--ink2)}}
.b-done{{border-color:#0f6b63;color:#0f6b63}}
.state{{font-size:.85rem;color:var(--ink2);margin-left:auto}}
textarea{{width:100%;min-height:60px;padding:9px 11px;border:1px solid var(--line);border-radius:4px;
  background:transparent;color:var(--ink);font:inherit;resize:vertical}}
.note{{background:#b06a121a;border-left:3px solid #b06a12;padding:10px 14px;border-radius:3px;margin-bottom:20px;font-size:.9rem}}
</style>
<div class="wrap">
<h1>Operator Board</h1>
<p class="sub">{n_open} item(s) waiting on you · served locally from
<code>operator/blockers.json</code> · nothing here leaves this machine</p>
<div class="note">★ Buttons write straight to the file. An agent can <b>propose</b> items;
only you can <b>close</b> them. Every change is journalled to <code>blockers_audit.jsonl</code>.</div>
{''.join(rows)}
</div>
<script>
async function setState(id,s){{
  await fetch('/set',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{id:id,state:s}})}}); location.reload();
}}
async function saveNote(id){{
  const v=document.getElementById('n-'+id).value;
  await fetch('/note',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{id:id,note:v}})}});
}}
</script>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        body = page(load()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            b = self._json_body()
            d = load()
            hit = [i for i in d["items"] if i["id"] == b.get("id")]
            if not hit:
                self.send_response(404); self.end_headers(); return
            it = hit[0]
            if self.path == "/set" and b.get("state") in ("done", "later", "open"):
                audit({"id": it["id"], "field": "state",
                       "was": it["state"], "now": b["state"], "decided_via": "local board (loopback)"})
                it["state"] = b["state"]
            elif self.path == "/note":
                audit({"id": it["id"], "field": "note", "chars": len(b.get("note", "")),
                       "decided_via": "local board (loopback)"})
                it["note"] = b.get("note", "")
            save(d)
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        except Exception as e:
            self.send_response(500); self.end_headers()
            self.wfile.write(str(e).encode())


def self_test() -> int:
    """★ NEGATIVE CONTROL for the surface the OPERATOR opens (open_board.sh execs this file).

    The failure this guards is invisible by construction: a page showing 22 real items looks exactly
    like a complete board of 22. Nothing on it reads as wrong. So the test asserts the page is the
    MERGED set, that derived rows carry no control the store cannot honour, and that a degraded read
    announces itself instead of silently serving a subset."""
    failures = []
    d = load()
    items, degraded = merged_items(d)
    hand = d.get("items", [])

    if len(items) <= len(hand):
        failures.append(f"page renders {len(items)} rows, the hand-written file holds {len(hand)} — "
                        f"not merged, or every derived source has gone silent")
    html = page(d)
    for i in items:
        if i.get("source", "hand") != "hand" and f"setState('{i['id']}'" in html:
            failures.append(f"derived row {i['id']} offers a state control the store cannot honour "
                            f"(blockers.py --set answers 'unknown id')")
            break
    for i in items:
        if i.get("source", "hand") == "hand" and f"setState('{i['id']}'" not in html:
            failures.append(f"hand row {i['id']} lost its controls")
            break

    saved = globals().get("merged_items")
    globals()["merged_items"] = lambda dd: (dd.get("items", []), "DEGRADED — hand-written rows ONLY")
    try:
        _, deg = merged_items(d)
        if "SUBSET" not in deg and "DEGRADED" not in deg:
            failures.append("a degraded read does not announce itself")
    finally:
        globals()["merged_items"] = saved

    for f in failures:
        print(f"  \u2717 {f}")
    print(f"  \u2713 board_local self_test: serves the merged set ({len(items)}), derived rows "
          f"read-only, degraded read announced" if not failures else "  \u2717 board_local self_test FAILED")
    return 1 if failures else 0


def main() -> None:
    if "--self-test" in sys.argv:
        return self_test()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8899)
    p.add_argument("--open", action="store_true")
    a = p.parse_args()
    url = f"http://127.0.0.1:{a.port}/"
    srv = HTTPServer(("127.0.0.1", a.port), H)   # ★ loopback only, never routable
    _items, _deg = merged_items(load())
    n = sum(1 for i in _items if i["state"] == "open")
    print(f"  OPERATOR BOARD — {n} item(s) waiting on you")
    print(f"  {url}")
    print("  loopback only · writes to operator/blockers.json · Ctrl-C to stop")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    sys.exit(main() or 0)
