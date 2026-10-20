# BoardWalk

**A guided board for the decisions and blockers only you can clear — and a pointer that rings the
real thing on your screen.**

This is a **sanitized public extraction** of a private working tree. The private tree was not
modified or deleted; this directory is a full duplicate with the operator's data, credentials,
hostnames and personal paths removed. Some panels that monitor a specific machine are therefore
inert here; the board, the walk, the overlay and the grant wizard are the parts that run.

---

## What it is

- **The Operator Needs Board** — one full board in the KHAOSS control panel
  (`/?section=operator-needs`), with search, filters, per-row controls, the walk, stewards and the
  grant utility. `/operator/board` and the retired BoardWalk popout URL both redirect here.
- **Cancel means cancel.** Every walk/run has an explicit **✕ cancel** in the guide sidebar, the
  walkthrough bar and the overlay card; Esc and the overlay's window X cancel in guide mode.
  Closing the overlay only hides it; cancelling dismisses the guide in its one store and marks
  nothing done.
- **Point-me-to-there** — a click-through desktop overlay (`jarvis/point_to.py`) that draws a ring
  around the actual control and a card that says why. The agent-facing protocol
  (`bin/point-at.py`) reads the screen with **local OCR**, filters it through the installed privacy
  gate, and resolves a **phrase** to a real rectangle. If the phrase is not on screen, it says so and
  points at nothing.
- **The ring follows the panel.** A Wayland-native browser has no X window id, so the panel page
  reports its own `screenX/screenY/outer/inner` to `POST /operator/window-geometry`; the overlay reads
  it, treats each walk rect as window-relative, and re-aims on a 700 ms tracker. If the panel has not
  reported yet the card says **“waiting for the panel”** — hard-reload the panel once
  (Ctrl+Shift+R) and it self-heals. A report older than 90 s is marked **stale** instead of silently
  trusted.
- **A card you can argue with.** `✓ right spot` / `✗ wrong spot` record a verdict and forward it to
  the selected guide agent through one delivery path; the mirror says **queued** or **NOT delivered**
  and why. `✗ wrong spot` re-aims through window → anchor → OCR → title and raises an X11 target.
  `⤴ front` raises an X11 window or asks the panel page to focus/scroll/flash the item; a Wayland
  compositor may still keep another window on top, and the page's toast says so.
  `◧ agent / explain` opens **Explain · Ask · Diagram · Agent**. A **live DSH chat is link-only**
  while it is open (its `session.lock` is held); the Agent tab marks it and offers **Open DSH**.
- **Grant access** — a timed, scoped, expiring root grant wizard (`panel/grant-wizard-popout.html` +
  `bin/capability-grant.py`). This release ships **one no-op example capability**; the private
  catalogue did not ship. Review the rendered sudoers line before granting anything.

## The fake test item

The release ships a safe test row, **“Take the BoardWalk feature tour (fake test item)”**, and
`operator/walks/feature-tour.json`. It exercises the board, the authored walk, the click-through
overlay, an **agent-authored illustration** (labels/arrows/boxes/dots drawn on the plane in the
BoardWalk palette) and the cancel. Start the panel, open *Point me to there*, choose
`feature-tour.json`, and press **show it on my screen**.

The same diagram is available to an agent:
```bash
bin/point-at.py point "the words on screen" --illustration my-diagram.json --say "Click this."
```
`my-diagram.json` is data only — `{"frame":"target","items":[{"kind":"label","at":[10,-40],
"text":"1 · Read the card","color":"blue"}, …]}`. No HTML, no script, no file path. The overlay
normalizes it against a closed vocabulary and drops anything it does not recognize.

## Quick start

```bash
# From this directory. The default port is 7842; use another if your live panel has it.
KHAOSS_PORT=7850 ./run.sh
# then open:
#   http://127.0.0.1:7850/?section=operator-needs
```

The panel binds **127.0.0.1 only** by default. There is no telemetry, no cloud call and no account.

### Requirements

- Linux with an X11 session (or XWayland). The overlay is GTK3 + cairo.
- Python 3.11+ (standard library only for the panel).
- System packages (Debian/Ubuntu names):
  `python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 python3-cairo tesseract-ocr imagemagick x11-utils`
- Optional: `xdotool`, `wmctrl` (the overlay's raise/search helpers).

## Point at something

```bash
# what can be captured per-window (X11/XWayland; a LOWER BOUND — Wayland-native windows have no xid)
./bin/point-at.py windows

# a privacy-filtered text map of one window, with rectangles
./bin/point-at.py see --window "<title or 0xid>"

# cite the words, not a coordinate; the overlay opens on the screen
./bin/point-at.py point "Continue" --window "<title or 0xid>" \
    --say "Click Continue to open the next screen."

# read-only: is it on screen now?
./bin/point-at.py verify "Continue"
```

Privacy, measured and stated:

- Local tesseract OCR; **the screenshot is deleted after OCR** and its path is never returned.
- **`--privacy shaped` (default):** keys, tokens, private keys, credentialed URLs, cards, accounts,
  SSNs, DOB, phones, emails, paths and hosts. **Names, health and money are NOT covered.**
- **`--privacy strict`:** also runs the local classifier and **refuses** to emit if it cannot run.
  On a whole screen it can over-mask; the receipt always says which tier ran.
- PII can be replaced with an **anchor** (`‹PERSONAL:ab12cd34ef›`) so an agent can point at a private
  label without receiving it. Credential-shaped values are never anchorable.
- Audit: `~/.local/state/khaoss/point_at_audit.jsonl` — counts only, never content.
- Screen text is **evidence, not instruction**; the digest says so on every call.

See `docs/POINT_AT_PROTOCOL.md` for the full contract and the live evidence.

## Grant access — read this first

The grant utility writes scoped `NOPASSWD` sudoers entries with a TTL. It is deliberately
two-part: `bin/capability-grant.py` (user) renders and queues; `bin/capability-grant-apply.sh`
(root) installs and withdraws. **This release ships one example capability whose script does
nothing** (`bin/example-capability.sh`). Add your own capabilities in `bin/capability-grant.py`,
review every line, and keep the apply step deliberate.

## Tests

```bash
python3 jarvis/point_at_core.py              # OCR + privacy + resolution; no screen captured
python3 jarvis/point_at_core.py --prove-fail # must exit 1 (mutants caught)
python3 bin/point-at.py selftest             # CLI guards on a temp guide store
DISPLAY=:0 XAUTHORITY=... python3 jarvis/point_to.py --self-test
python3 jarvis/privacy_gate.py --self-test

# drive the REAL card through AT-SPI on its own Xvfb display (no operator clicks)
python3 jarvis/test_point_to_drive.py
python3 jarvis/test_point_to_drive.py --prove-fail   # every injected defect must go RED
```

The drive suite needs `Xvfb`, `xfwm4`, `xdotool` and `gir1.2-atspi-2.0`; see `DISTRO.md`. It uses a
temporary disconnected backend, so it never calls a model and never edits your guide state.

## Layout

| path | what |
|---|---|
| `aionui-stack-panel.html` | the one KHAOSS control panel (board, walk, utilities) |
| `panel_server.py` | its loopback HTTP server |
| `panel/` | served popouts (grant, setup, consent, phone, stormwatch, DSH observability) |
| `bin/point-at.py` | the agent-facing point protocol |
| `bin/point_at_fixture_window.py` | a known test window for the first live drive |
| `jarvis/point_to.py` | the click-through overlay |
| `jarvis/point_at_core.py` | OCR → privacy → phrase → rectangle |
| `jarvis/privacy_gate.py` | the installed privacy filter |
| `operator/` | the board/walk/guide modules + three seed rows |
| `operator/walks/demo.md` | a one-step authored walk |

## Not done / honest limits

- **This is an extraction, not a productized installer.** Estate-specific service checks and
  panels will show absent or inert on a machine that is not the original. **The release does not ship
  the private DSH launcher**, so the live-chat row's **Open DSH** may be unavailable here; the
  link-only honesty still applies.
- **The card is machine-driven, not operator-driven.** The included suite presses the real buttons
  through AT-SPI and reads the real labels back, but **USABLE is the operator's row**, not a machine
  claim. A live DSH chat cannot be attached while it is open; Ask/Diagram go to the selected answerer
  (or say NOT delivered).
- **The private board was replaced with three seed rows**; the derived rows you see are your own
  machine's, not anyone else's.
- Wayland-native windows cannot be captured per-window (no XID); use `--region`/`--screen`.
- An icon with no text must be pointed at by rectangle; OCR has no element map.
- The `--privacy shaped` default does **not** cover names/health/money. Use `strict` for prose.
- The first live loop was proven on a fixture and on the grant wizard's duration control; a real
  end-to-end errand is a user test, not a machine claim.

## License

Apache-2.0 — see `LICENSE` and `NOTICE`. The NOTICE names the upstream Walker project this work was
extracted from; keep it with any redistribution.

## Install a desktop launcher

Run `python3 install.py` (choose the OS) and see `DISTRO.md`.
