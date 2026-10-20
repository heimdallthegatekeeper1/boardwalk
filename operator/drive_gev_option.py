#!/usr/bin/env python3
"""drive_gev_option.py — DRIVE the recommended option in a real browser and PHOTOGRAPH each state.

★ AGENTS.md §1: "It renders" is not "it works". A UI feature is not reported until it has been
DRIVEN in the live app. So this does not screenshot a static page — it loads the SERVED surface,
presses the actual controls, waits for the round trip, and captures what the operator would see
AFTER the click.

Three states, in the order the brief names them:
  1. OFFERED     — the recommended option as it first appears, with its costs on the card
  2. DECLINED    — after pressing "No thanks", driven through POST /operator/setup/choice
  3. RECONSIDERED— after pressing "Reconsider", proving the refusal is reversible

★ READ-ONLY ON THE REPO. It writes only PNGs into the lane's own shots directory.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PANEL = "http://127.0.0.1:7842"
PAGE = f"{PANEL}/operator/setup-wizard"
OUT = Path.home() / "gev-option-shots"
OUT.mkdir(parents=True, exist_ok=True)


def api(path, **kw):
    import urllib.request
    req = urllib.request.Request(f"{PANEL}{path}", **kw)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def choice(decision):
    import urllib.request
    body = json.dumps({"decision": decision}).encode()
    req = urllib.request.Request(f"{PANEL}/operator/setup/choice", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def main() -> int:
    log = []

    def say(s):
        print(s, flush=True)
        log.append(s)

    # ── 0. START FROM NO DECISION, so "offered" is a real first-run state ────────────────────────
    choice("clear")
    d = api("/operator/setup/walk")
    g = [r for r in d["steps"] if r["id"] == "gev_option"][0]
    say(f"[start] option_state={g['option_state']} walk_state={g['state']}")

    with sync_playwright() as p:
        br = p.chromium.launch(headless=True)
        pg = br.new_page(viewport={"width": 1180, "height": 1000}, device_scale_factor=1)

        # ── 1. THE OFFERED STATE ────────────────────────────────────────────────────────────────
        pg.goto(PAGE, wait_until="networkidle")
        pg.select_option("#sw-filter", "all")
        pg.wait_for_timeout(700)
        row = pg.locator('[data-sw-id="gev_option"]')
        row.scroll_into_view_if_needed()
        pg.wait_for_timeout(400)
        row.screenshot(path=str(OUT / "1_offered_card.png"))
        say(f"[1] offered card -> 1_offered_card.png  (heading: "
            f"{row.locator('h2').inner_text().strip()!r})")

        # the card is tall; the terms are the deliverable, so capture them as their own frame
        pg.screenshot(path=str(OUT / "1b_offered_fullpage.png"), full_page=True)
        say("[1b] full page -> 1b_offered_fullpage.png")

        # ★ ASSERT THE DISCLOSURE IS ACTUALLY ON SCREEN, not merely in the JSON.
        txt = row.inner_text()
        for needle, label in (("What it gives you", "gives"),
                              ("What it requires", "requires"),
                              ("BILLING", "billing"), ("phones out", "egress"),
                              ("TeleGeography", "licence-NC"),
                              ("ATTRIBUTION", "attribution"),
                              ("voice agent", "voice-conflict"),
                              ("No key is ever typed here", "no-key-field")):
            ok = needle.lower() in txt.lower()
            say(f"    {'✓' if ok else '✗'} on the card: {label} ({needle!r})")
        # and the two ways out must both be present
        for sel, label in (('[data-act="gev:accept"]', "YES button"),
                           ('[data-act="gev:decline"]', "NO button")):
            say(f"    {'✓' if row.locator(sel).count() else '✗'} on the card: {label}")
        say(f"    {'✓' if pg.locator('input[type=password]').count() == 0 else '✗'} "
            f"no password input anywhere on the page")

        # ── 2. DRIVE THE DECLINE ────────────────────────────────────────────────────────────────
        say("[2] pressing 'No thanks — do not offer this again'…")
        row.locator('[data-act="gev:decline"]').click()
        pg.wait_for_timeout(2600)          # the POST + full re-measure round trip
        said = pg.locator("#sw-said").inner_text().strip()
        say(f"    page said: {said!r}")
        pg.select_option("#sw-filter", "all")
        pg.wait_for_timeout(800)
        row = pg.locator('[data-sw-id="gev_option"]')
        row.scroll_into_view_if_needed()
        pg.wait_for_timeout(400)
        row.screenshot(path=str(OUT / "2_declined_card.png"))
        say(f"[2] declined card -> 2_declined_card.png (heading: "
            f"{row.locator('h2').inner_text().strip()!r})")
        dtxt = row.inner_text()
        for needle in ("You declined this on", "real answer", "Reconsider"):
            say(f"    {'✓' if needle.lower() in dtxt.lower() else '✗'} on the declined card: {needle!r}")

        # ★ AND THE DEFAULT VIEW MUST NOT NAG: with the default filter, a declined offer is gone.
        pg.select_option("#sw-filter", "open")
        pg.wait_for_timeout(600)
        n_open = pg.locator('[data-sw-id="gev_option"]').count()
        say(f"    {'✓' if n_open == 0 else '✗'} with the default filter ('what is not done') the "
            f"declined option is NOT shown (rows found: {n_open})")
        pg.screenshot(path=str(OUT / "2b_declined_default_filter.png"))

        # ── 3. DRIVE THE RECONSIDER ─────────────────────────────────────────────────────────────
        say("[3] pressing 'Reconsider'…")
        pg.select_option("#sw-filter", "all")
        pg.wait_for_timeout(600)
        pg.locator('[data-sw-id="gev_option"] [data-act="gev:reconsider"]').click()
        pg.wait_for_timeout(2600)
        say(f"    page said: {pg.locator('#sw-said').inner_text().strip()!r}")
        pg.select_option("#sw-filter", "all")
        pg.wait_for_timeout(800)
        row = pg.locator('[data-sw-id="gev_option"]')
        row.scroll_into_view_if_needed()
        pg.wait_for_timeout(400)
        row.screenshot(path=str(OUT / "3_reconsidered_card.png"))
        say(f"[3] reconsidered card -> 3_reconsidered_card.png (heading: "
            f"{row.locator('h2').inner_text().strip()!r})")
        say(f"    {'✓' if row.locator('[data-act=\"gev:accept\"]').count() else '✗'} "
            f"the YES button is back after reconsidering")

        # ── 4. THE RAW REFUSAL, PHOTOGRAPHED ────────────────────────────────────────────────────
        pg.screenshot(path=str(OUT / "4_full_page_reconsidered.png"), full_page=True)
        say("[4] full page after reconsider -> 4_full_page_reconsidered.png")

        br.close()

    # ── 5. FINAL STATE, READ BACK FROM THE ROUTE ────────────────────────────────────────────────
    d = api("/operator/setup/walk")
    g = [r for r in d["steps"] if r["id"] == "gev_option"][0]
    say(f"[final] option_state={g['option_state']} walk_state={g['state']} "
        f"declined={d['declined']} next_actionable={d['next_actionable']}")

    (OUT / "drive_log.txt").write_text("\n".join(log) + "\n", encoding="utf-8")
    print(f"\nshots in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
