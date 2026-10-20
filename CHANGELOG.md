# BoardWalk — changelog

## v1.0.3 — 2026-10-20 · THE AUDIT'S FOUR FINDINGS, CLOSED

An independent read-only audit of the shipped v1.0.1 artifact found four real gaps. All four are fixed
here, and each names what was wrong rather than only what changed.

- **The coach was still a second dismiss path.** Its "leave the walk" button and Esc called
  `bdGuideStop()`, which POSTed `dismiss` on the click — bypassing the one card. `bdGuideStop()` now
  opens the same four-choice card; the immediate dismiss it used to perform is `bdGuideStopNow()`,
  and that is what the card's own **Stop now** calls for the coach.
- **The last "long for later" on the box was invisible to grep.** It sat inside a comment wrapped
  across two lines (`"long for` ⏎ `later" is meant to stop`), so a line-based scan returned zero. The
  comment now says "log for later"; a whitespace-insensitive scan finds none in either tree.
- **The overlay's agent panel had no recommendation.** It listed every backend and nothing about which
  one fits the step. It now reads `operator/recommended_agents.py` — the same module the board uses —
  and prints a recommended line above the list. For walk-file steps, which are not board rows, it says
  exactly that instead of inventing an owner.
- **Two smaller honesty fixes.** The overlay's cancel tooltip still described the old instant cancel;
  it now describes the four choices. The `#on-walk-btn` tooltip called a local *view* toggle
  "cancel" — it never cancelled anything server-side, and it no longer claims to. And a comment in
  `operator/dsh_sessions.py` carried the operator's real username in an example path; it is now
  `USER`.

Found by: the independent read-only audit report "v101_audit" (kept with the operator's records, not
shipped; the artifact's 145
tracked files were byte-identical to tag v1.0.1 — 145/145 sha256).

## v1.0.2 — 2026-10-19 · TELL HIM WHICH WALK "SHOW IT" WILL FOLLOW

- **The bar's walk dropdown no longer looks empty while a live guide is running.** The status line now
  names the target outright: `▶ show it follows: the LIVE guided walk (step 1 of 11)` or
  `▶ show it follows: the file grant-root-access.md`. The placeholder also says what it means —
  follow the live guide, or pick a file below to follow that instead.
- Found by driving the panel exactly as the operator was asked to: the dropdown read "(the live guided
  walk, if one is running)" while the status beside it said "live guided walk active", so the one
  question that matters — *which walk am I about to start?* — had no answer on screen.

## v1.0.1 — 2026-10-19 · THE OVERLAY'S EXIT IS THE SAME ONE CARD

- **The overlay card's exits are now the turnkey choice**, not an instant dismissal. "✕ close",
  both "✕ cancel walk" buttons, Esc and the window-manager X all open one card offering
  **Keep it in the backlog · Log for later (tomorrow 09:00) · Stop now · Stay in the walk**.
  Nothing is marked done by any of them, and "keep it in the backlog" provably dismisses nothing:
  the snapshot now carries `dismissed: false` instead of leaving a reader to infer it.
- **"Log for later" from the overlay writes the board's one reminder store** (`POST /operator/remind`),
  so a reminder logged at the overlay appears on the board and the reverse. No second copy.
- **Fixed: the leave card used a nested `Gtk.Dialog.run()` loop, which made the window invisible to
  AT-SPI for as long as it was open.** A dialog the accessibility tree cannot see is a dialog a
  screen reader cannot see. It is driven by the `response` signal now, on the one main loop.
- **Proven by driving, not by reading:** the isolated AT-SPI suite drives Stay (closes, overlay keeps
  running, no exit snapshot), Keep it in the backlog (snapshot `dismissed: false`, journal row,
  overlay closes), and Stop now (cancel snapshot, journal with the mode, overlay closes). The new
  guard has a prove-fail mutant (`instant-cancel`) that restores the old instant exit and must — and
  does — turn the suite red.

## v1.0.0 — 2026-10-19 · FIRST SHIP

The board, the walk, the overlay and the grant utility run as one app with a launcher per OS.

**The board**
- One Operator Needs Board with search, filters, per-row controls, stewards and the grant utility.
- The walk walks the board rows one at a time, each with a plain-English framing (or an honest
  "no framing written" rather than an invented one).

**The walk you can drive**
- One **turnkey exit card**: keep it in the backlog · log for later · stop now · stay. The walk
  header, the panel bar and the coach all open that one card, and it asks once.
- A numbered forward path on the bar — **1 choose → 2 show it on my screen → 3 next step** — with
  leaving in its own secondary band.
- A **working-agent selector on every walk step**, fed by the same guide registry scan as the agent
  panel; the recommendation is a suggestion, not a fence. Switch at any step.

**The pointer**
- `jarvis/point_to.py` draws a ring on the real control and a card that says why; click-through is
  measured, not assumed.
- The ring follows the panel: the panel page reports its own geometry, the overlay treats each walk
  rect as window-relative and re-aims on a 700 ms tracker, with a visible state line.
- "Log for later" means a date and a time to the second — and it is spelled that way everywhere.

**What this release does not claim**
- A live DSH chat is link-only by its own lock; the card says so instead of pretending.
- Some monitoring panels that watch a specific private machine are inert in this sanitized copy.
- No package downloads, no sudo, no network at install time.

**Known gaps**, recorded rather than smoothed: the walk's `find:` text is matched against window
titles while the authored values are screen content; a walk rect with no anchor is screen-absolute
and cannot follow a moved window; the panel is Wayland-native and has no X window id, so it reports
its own geometry instead.
