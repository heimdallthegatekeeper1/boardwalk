#!/usr/bin/env python3
"""★ Build the operator's docket page FROM blockers.json — the board renders the store, it is not a copy.

★ WHY GENERATED AND NOT HAND-WRITTEN: `blockers.json` and the published board were TWO STORES and could
  already drift (handoff §5). A hand-maintained page guarantees that drift; a generated one cannot have it,
  because the only way to change what the page says is to change the store and rebuild.

★ WHAT THE PAGE MAY AND MAY NOT CLAIM: it cannot write to this filesystem, so a ruling made in the page is
  a PENDING DECISION and is labelled one. The page prints the exact `--set` commands and says the store
  still reads `open` until they run. It never shows a decision as applied.

USE:  build_board_panel.py            # write panel/operator_needs_me.html
      build_board_panel.py --stdout
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PANEL = HERE / "panel"
OUT = PANEL / "operator_needs_me.html"
FONTS = ("https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600"
         "&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap")


def build(local: bool = False) -> str:
    board = json.loads((HERE / "blockers.json").read_text())
    css = (PANEL / "style.css").read_text()
    js = (PANEL / "app.js").read_text()

    items = []
    for i in board["items"]:
        items.append({
            "id": i["id"], "sev": i.get("sev", "hold"), "state": i.get("state", "open"),
            "title": i.get("title", ""), "one": i.get("one", ""),
            "blocks": i.get("blocks", ""), "lane": i.get("lane", ""), "latent": bool(i.get("latent")),
        })
    # ★ severity order is the store's own ordering, not a display preference
    order = {"stop": 0, "hold": 1, "info": 2}
    items.sort(key=lambda x: (order.get(x["sev"], 9), x["state"] != "open"))

    state = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "source": "operator/blockers.json",
        "board_url": board.get("board_url", ""),
        "items": items,
        # Regenerating means CC has just re-read the store, so nothing is pending against it yet.
        "decisions": {},
        # ★ LOCAL means the page is served by panel_server on this machine and can write to the store
        #   directly — so it has no pending state, because it has no gap to be honest about.
        "local": bool(local),
    }

    # NOTE: the PUBLISHED form carries no <!doctype>/<html>/<head>/<body> — the Artifact tool supplies
    # that shell on first publish, and the page's own renderDoc() emits the full document when it
    # republishes itself. The LOCAL form is served by panel_server and must be a complete document.
    head = [
        f'<link rel="stylesheet" href="{FONTS}">',
        f'<style id="board-style">{css}</style>',
    ]
    body = [
        '<div class="wrap"><div id="app"></div></div>',
        '<script type="application/json" id="board-state">' + json.dumps(state) + "</script>",
        f'<script id="board-script">{js}</script>',
    ]
    if local:
        return "\n".join([
            "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>Operator Needs Me</title>",
            '<link rel="preconnect" href="https://fonts.googleapis.com">',
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
            *head, "</head>", "<body>", *body, "</body>", "</html>", "",
        ])
    return "\n".join([
        "<title>Operator Needs Me</title>",
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        f'<link rel="stylesheet" href="{FONTS}">',
        f'<style id="board-style">{css}</style>',
        '<div class="wrap"><div id="app"></div></div>',
        '<script type="application/json" id="board-state">' + json.dumps(state) + "</script>",
        f'<script id="board-script">{js}</script>',
        "",
    ])


if __name__ == "__main__":
    html = build(local="--local" in sys.argv)
    if "--stdout" in sys.argv:
        print(html)
    else:
        OUT.write_text(html)
        n = len(json.loads((HERE / "blockers.json").read_text())["items"])
        print(f"  wrote {OUT} ({len(html):,} bytes, {n} items)")
