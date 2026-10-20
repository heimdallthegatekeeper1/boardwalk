#!/usr/bin/env python3
"""jarvis/point_to.py -- THE POINT-ME-TO-THERE PROTOCOL.

★★★ WHAT HE ASKED FOR (2026-09-21): "an OS-wide overlay that helps to tackle these things that
    points out areas the user needs to go and click and see (point-me-to-there protocol or something
    along those lines almost like a game tutorial) ... build it into the boardwalk."

★★ WHAT THIS IS, IN ONE SENTENCE. Two X11 windows: a full-screen, CLICK-THROUGH dim plane that draws
   a ring round a thing on the screen and an arrow to it, and a small always-on-top card that says why
   and carries Next / Back / Leave. It follows the SAME guided-walk store the board already uses.

★★★ WHY IT IS NOT A SECOND GUIDE. He already has a guided walk: `operator/guide_focus.py` (store
   `operator/guide_focus.json`). It has KINDS {item, control, section, none}, a one-guide-at-a-time
   rule, `set_sequence()`, `advance()`, `back()`, `dismiss()`, and a move journal. Its targets,
   though, are all PANEL-INTERNAL selectors (`#bd-q`, `#on-walk-btn`) -- it can move his attention
   INSIDE the board page and nowhere else.
   ★ So this file does not invent a parallel walk. It READS `guide_focus.current()` and adds ONE
   thing the existing vocabulary cannot express: a step's `screen` block, naming a window / a
   rectangle / a point on the actual desktop. `set_sequence()` already PASSES UNKNOWN FIELDS THROUGH
   (that is written down in its own source as a fix for a field-eating whitelist), so a step can
   carry `screen` today with ZERO changes to guide_focus.py -- which is another lane's file.
   ★ ADVANCING IS SHARED. Whichever surface he presses Done on -- the board, the voice seat or this
   card -- calls the same `guide_focus.advance()`. ONE store per fact: the step he is on.

★★ TARGET RESOLUTION IS REPORTED, NEVER GUESSED. A target that resolves to nothing does NOT quietly
   become (0,0), and a target that matches three windows does NOT quietly become the first one.
   Unresolved and ambiguous are real, visible states with their candidate lists attached. The
   self-test's prove-fail injects exactly the "silently return (0,0)" defect and requires this suite
   to go RED on it.

★★ CLICK-THROUGH: MEASURED, NOT CLAIMED. GTK's own `Gdk.Window.input_shape_combine_region()` was
   tried on this box FIRST and MEASURED to do nothing: after calling it with an empty cairo.Region,
   `XShapeGetRectangles(..., ShapeInput=2)` still reported 1 rectangle. The plane therefore uses
   XShapeCombineRegion(ShapeInput) through ctypes/libXext, and `verify_click_through()` re-measures
   with XShapeGetRectangles every time. If that ever returns non-empty, `click_through` reports False
   and the report says so -- it does not claim a pass-through it does not have.
   ★ AND THE HONEST LIMIT: an empty X11 input shape makes the OVERLAY window transparent to input.
   It does NOT make the CARD click-through -- the card is a normal small window with buttons, because
   a guide he cannot press Next on is not a guide. "Click-through except on its own widgets" is
   therefore literally two windows, not one clever one.

★ THE ESCAPE HAS THREE DOORS, because a guide without one is a captor: the card's Leave button, Esc
  while the card has focus, and `--kill` (which signals ONE pid from the pidfile, after checking that
  pid's cmdline really is this file -- never `pkill -f`, which this estate forbids by rule).
"""
from __future__ import annotations

import os
import sys

# ★★ CLONED FROM jarvis/bubble.py, WHICH IS THE PROVEN CONFIGURATION ON THIS BOX. Mother does not
#    implement wlr-layer-shell, and set_keep_above() is a silent no-op under a Wayland GDK backend --
#    this session exports GDK_BACKEND=wayland, so setdefault() would have left it and the plane would
#    have opened looking correct and never stayed on top. FORCED, and recorded, exactly as bubble.py
#    does it.
_PRIOR_BACKEND = os.environ.get("GDK_BACKEND")
os.environ["GDK_BACKEND"] = "x11"
BACKEND_OVERRIDDEN = (_PRIOR_BACKEND or "") not in ("", "x11")

import ctypes
import ctypes.util
import datetime
import json
import math
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import cairo  # noqa: E402

# ★ THE ILLUSTRATION SPEC IS SHARED WITH THE AGENT-FACING CORE, so the overlay and the CLI accept
#   exactly the same vocabulary. A failure to import degrades to "no diagram", never to a crash.
try:
    from point_at_core import normalize_illustration, ILLUS_PALETTE  # noqa: E402
except Exception:  # noqa: BLE001
    normalize_illustration = None
    ILLUS_PALETTE = {"blue": "#7cc4ff", "gold": "#d87848", "green": "#3f9e8f",
                     "red": "#e5484d", "cream": "#e8e6e2", "ink": "#14161a"}

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OPERATOR = REPO / "operator"
sys.path.insert(0, str(OPERATOR))

PIDFILE = Path(os.environ.get("KHAOSS_POINT_TO_PID") or "/tmp/jarvis_point_to.pid")
REPORT = Path(os.environ.get("KHAOSS_POINT_TO_REPORT") or "/tmp/jarvis_point_to_report.json")
# ★★★ THE USER'S VERDICT ON THE OUTLINE, IN A STORE THE AGENT CAN READ. He asked for *"a button that
#     tells the agent whether they think that is the actual spot the item is or not ... they may have
#     the agent re-check and bring to front what needs to be there."* A verdict that only lives on
#     screen tells nobody anything, so it is appended here -- ONE store, append-only, in the repo where
#     every agent already looks, exactly like the guided-walk store beside it.
# ★ THE FOUR PATHS ARE OVERRIDABLE FOR AN ISOLATED MACHINE TEST. AT-SPI can drive the real card, and a
#   suite that does so must not append rows to the operator's journal or clear his pidfile. The
#   defaults are byte-identical to the shipped behaviour; nothing reads these env vars in normal use.
FEEDBACK = Path(os.environ.get("KHAOSS_POINT_TO_FEEDBACK")
                or (Path(__file__).resolve().parent.parent / "operator" / "point_to_feedback.jsonl"))
# ★ the read-aloud voice and language, in ONE place, so the overlay's choice survives it
VOICE_PREF = Path(os.environ.get("KHAOSS_POINT_TO_VOICE")
                  or (Path(__file__).resolve().parent.parent / "operator" / "point_to_voice.json"))
CMD = Path(os.environ.get("KHAOSS_POINT_TO_CMD") or "/tmp/jarvis_point_to.cmd")
LOG = Path(os.environ.get("KHAOSS_POINT_TO_LOG") or "/tmp/jarvis_point_to.log")
WALKS = REPO / "operator" / "walks"

TITLE_RE = re.compile(
    # ★ THE CLASS PART IS `\(.*?\)`, NOT `\([^)]*\)`. A window class can itself contain a
    #   parenthesis -- measured 2026-10-18: Chromium publishes
    #   `("chromium (/tmp/pointat_chrome_profile)" "Chromium")`, and the old form stopped inside the
    #   path, so the row matched nothing and the resolver was blind to an open, capturable window.
    r'^\s+(0x[0-9a-fA-F]+)\s+(?:"(.*?)"|\(has no name\)):\s+\(.*?\)\s+'
    r'(\d+)x(\d+)\+(-?\d+)\+(-?\d+)'
    r'(?:\s+\+(-?\d+)\+(-?\d+))?\s*$')

# ── the target vocabulary. CLOSED ON PURPOSE, exactly like guide_focus.KINDS: an open-ended selector
#    would let a step aim his attention at anything, including at things we do not own.
TARGET_KINDS = {"window", "rect", "point"}
KINDS_SAYS = {
    "window": "a window on the desktop, named by title (substring, case-insensitive) or by X id",
    "rect": "an explicit screen rectangle -- x, y, width, height in real screen pixels",
    "point": "an explicit screen point -- a small box is drawn around it",
}
POINT_BOX = 56          # a `point` target becomes a POINT_BOX x POINT_BOX box centred on the point
AMBIENT = 0.42          # scrim darkness
RING_PAD = 7            # how far outside the resolved rectangle the ring is drawn (chrome, not geometry)
RING_RGB = "#7cc4ff"    # the ring colour the render-verification reads back out of the pixels
VERIFY_PNG = Path("/tmp/point_to_verify.png")

# ★ GTK3 CSS in b""" BYTES LITERALS MUST STAY PURE ASCII. jarvis/bubble.py carries a whole comment
#   about this: it requires_py3 with `from __future__ import annotations` and a non-ASCII byte in the
#   CSS is a SyntaxError that kills the module before anything runs. Everything below is ASCII.
CSS = b"""
#pt-card { background: #14161a; border: 1px solid #2c3138; border-radius: 10px; }
#pt-head { background: #1b1e23; border-bottom: 1px solid #2c3138;
           border-top-left-radius: 9px; border-top-right-radius: 9px; }
#pt-badge { color: #d87848; font-weight: bold; font-size: 11px; }
#pt-who   { color: #7c7873; font-size: 10px; }
#pt-title { color: #e8e6e2; font-weight: bold; font-size: 14px; }
#pt-body  { color: #d6d2cc; font-size: 12px; }
#pt-hint  { color: #a9a5a0; font-size: 11px; }
#pt-err   { color: #e5484d; font-size: 11px; font-weight: bold; }
#pt-x     { background: #1b1e23; color: #a9a5a0; border: 1px solid #2c3138; border-radius: 5px;
            padding: 1px 7px; font-size: 11px; }
#pt-x:hover { color: #e5484d; border-color: #e5484d; }
button.pt-nav { background: #211d2c; color: #d8d2e6; border: 1px solid rgba(255,255,255,0.12);
            border-radius: 6px; padding: 4px 12px; font-size: 12px; }
button.pt-nav:hover { border-color: #7cc4ff; color: #7cc4ff; }
button.pt-go { background: #1d2b33; color: #7cc4ff; border: 1px solid #7cc4ff; border-radius: 6px;
            padding: 4px 14px; font-size: 12px; font-weight: bold; }
button.pt-go:hover { background: #24363f; }
button.pt-leave { background: #2a1e20; color: #d99a2b; border: 1px solid rgba(217,154,43,0.5);
            border-radius: 6px; padding: 4px 12px; font-size: 12px; }
button.pt-leave:hover { border-color: #e5484d; color: #e5484d; }
/* * THE DIM STATE, CARRIED BY COLOUR. Amber while the dimming is ON, because it IS affecting his
   screen; the default plain styling when OFF, because nothing is being drawn over his work. The label
   says the state and the colour says it again -- "only says dim:on/off and nothing else but should
   change colour to indicate if it's on/off". */
button.pt-dim-on { background: #3a2a08; color: #ffc82e; border: 1px solid #ffc82e; font-weight: bold;
                   border-radius: 6px; padding: 0 8px; }
button.pt-dim-on:hover { background: #4a3700; }
/* * these buttons carry a NAME, so the selector is `#`, not `.` */
#pt-read, #pt-auto, #pt-open, #pt-search, #pt-jump, #pt-kill, #pt-lang {
  background: #211d2c; color: #d8d2e6; border: 1px solid rgba(255,255,255,0.12);
  border-radius: 6px; padding: 0 8px; }
#pt-read:hover, #pt-auto:hover, #pt-open:hover, #pt-search:hover, #pt-jump:hover, #pt-kill:hover,
#pt-lang:hover { border-color: #7cc4ff; color: #7cc4ff; }
#pt-kill { color: #e5484d; border-color: rgba(229,72,77,0.45); }
/* * THE CANCEL IS DELIBERATELY THE LOUDEST CONTROL IN THE GRID. The operator was inside a guide
   and could not find the way out; a red, bold, named button in the same sidebar as the other
   controls is the answer. It ENDS the walk (guide_focus.dismiss) rather than hiding the card. */
#pt-cancel { background: #2a1e20; color: #e5484d; border: 1px solid rgba(229,72,77,0.75);
             border-radius: 6px; padding: 0 10px; font-weight: bold; }
#pt-cancel:hover { background: #3a2226; color: #ff7b7f; border-color: #ff7b7f; }
#pt-illus { background: #1d2b33; color: #7cc4ff; border: 1px solid rgba(124,196,255,0.45);
            border-radius: 6px; padding: 0 8px; }
/* * THE TARGET STATE IS A FACT, NOT A SPINNER. */
#pt-state { font-size: 0.82em; margin-top: 2px; }
#pt-state.state-ok { color: #3f9e8f; }
#pt-state.state-warn { color: #ffc82e; }
#pt-mirror { font-size: 0.74em; color: #c9c3ba; margin-top: 2px; }
#pt-illus:hover { border-color: #7cc4ff; }
/* THE SELECTOR WAS THE BUG. `set_name()` sets the widget NAME (matched by `#`), not a class (`.`),
   so `button.pt-auto.pt-voice-on` asked for a button carrying BOTH the class pt-auto and the class
   pt-voice-on -- and pt-auto was only ever a NAME. The rule could never match, so the colour never
   changed. The operator read it correctly: "the color didn't change on that button." */
button.pt-voice-on { background: #1d2b33; color: #7cc4ff; border: 1px solid #7cc4ff; font-weight: bold; }
button.pt-voice-off { background: #211d2c; color: #9aa0a6; border: 1px solid rgba(255,255,255,0.12); }

label { font-family: sans-serif; }
"""

# ── X11 shape helpers (libXext). Used ONLY to make the plane transparent to clicks and to VERIFY it.
_X11 = None
_XEXT = None
_SHAPE_INPUT = 2
_SHAPE_SET = 0
_SHAPE_BOUNDING = 0


def _libs():
    global _X11, _XEXT
    if _X11 is None:
        _X11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        _X11.XOpenDisplay.restype = ctypes.c_void_p
        _X11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        _X11.XFlush.argtypes = [ctypes.c_void_p]
        _XEXT = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
        _XEXT.XShapeGetRectangles.restype = ctypes.c_void_p
        _XEXT.XShapeGetRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        _XEXT.XCreateRegion.restype = ctypes.c_void_p
        _XEXT.XShapeCombineRegion.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        _XEXT.XDestroyRegion.argtypes = [ctypes.c_void_p]
    return _X11, _XEXT


def _dpy():
    x11, _ = _libs()
    return x11.XOpenDisplay(None)


def shape_rect_count(xid: int, kind: int = _SHAPE_INPUT):
    """How many rectangles the X server says this window's shape region has.

    ★ THIS IS THE MEASUREMENT, not a proxy. `ShapeInput` = 2. An input region with ZERO rectangles is
    an empty input region, which is what "clicks pass through to whatever is behind" means at the X
    protocol level. If we cannot ask, we return None -- 'could not look' is its own answer and is
    never folded into 'zero'."""
    try:
        x11, xext = _libs()
        d = _dpy()
        if not d:
            return None
        n = ctypes.c_int(0)
        order = ctypes.c_int(0)
        xext.XShapeGetRectangles(d, ctypes.c_ulong(int(xid)), int(kind),
                                 ctypes.byref(n), ctypes.byref(order))
        return int(n.value)
    except Exception:
        return None


def apply_empty_input_shape(xid: int) -> bool:
    """Make this window's INPUT region empty, so no click ever lands on it. Returns True only when
    XCreateRegion + XShapeCombineRegion both reported success."""
    try:
        x11, xext = _libs()
        d = _dpy()
        if not d:
            return False
        empty = xext.XCreateRegion()
        if not empty:
            return False
        xext.XShapeCombineRegion(d, ctypes.c_ulong(int(xid)), _SHAPE_INPUT, 0, 0,
                                 ctypes.c_void_p(empty), _SHAPE_SET)
        x11.XFlush(d)
        xext.XDestroyRegion(ctypes.c_void_p(empty))
        return True
    except Exception:
        return False


def ring_rect(rect, screen):
    """THE ring rectangle, from the target rectangle and the screen. ONE definition, used by the
    drawing AND by the render verification -- so the thing that is drawn and the thing that is
    expected can never drift apart.

    ★★★ TWO REAL DEFECTS FOUND HERE BY THE PIXEL GUARD, NOT BY EYE. MEASURED 2026-09-21 with the
    target being the 1012x1089 agent-browser window at x=906: its right edge is at screen x=1918, the
    7px ring pad put the ring's right edge at x=1925 -- 5 pixels off a 1920-wide screen -- so the whole
    right side of the ring was simply NOT DRAWN, and the walk showed him a three-sided box. The
    verifier caught it as a 222px width error instead of the 1-2px it should have been.
    ★ So the ring is CLAMPED to the screen. A guide at the edge of the screen gets a complete ring
    hugging the screen edge, not a broken one.
    ★ AND `offscreen` travels with it: a target that does not fit on the screen is REPORTED, because a
    ring that quietly has one side missing looks like a rendering bug rather than a coordinate one."""
    sx, sy, sw, sh = screen
    rx, ry, rw, rh = rect
    x0 = max(sx, rx - RING_PAD)
    y0 = max(sy, ry - RING_PAD)
    x1 = min(sx + sw, rx + rw + RING_PAD)
    y1 = min(sy + sh, ry + rh + RING_PAD)
    out = [x0, y0, max(0, x1 - x0), max(0, y1 - y0)]
    unclamped = [rx - RING_PAD, ry - RING_PAD, rw + 2 * RING_PAD, rh + 2 * RING_PAD]
    return out, (out != unclamped)


def verify_click_through(xid: int) -> dict:
    """★ MEASURED. `click_through` is True only when the X server answers with an EMPTY input region.
    `None` from shape_rect_count means we could not ask -- reported as measured False, with the
    reason, because a guard that cannot look must not report a pass."""
    n = shape_rect_count(xid, _SHAPE_INPUT)
    if n is None:
        return {"click_through": False, "measured": False,
                "reason": "could not query ShapeInput on this display (not a 'no', a 'did not look')"}
    return {"click_through": n == 0, "measured": True, "input_rectangles": n,
            "reason": ("the X server reports an EMPTY input region for this window -- clicks land on "
                       "whatever is behind it" if n == 0 else
                       f"the X server reports {n} input rectangle(s) -- THIS WINDOW WOULD EAT CLICKS "
                       f"in those areas")}


# ── windows on screen ────────────────────────────────────────────────────────────────────────────
def parse_tree_line(line: str):
    """One `xwininfo -root -tree` row -> (id, title, rect) or None.

    ★★★ THIS FUNCTION EXISTS BECAUSE OF A REAL BUG FOUND BY A LIVE PROBE, NOT BY THE SUITE.
    The first version's regex ended after the FIRST geometry pair. `xwininfo -root -tree` prints TWO:
    the geometry relative to the parent and then the absolute corner, e.g.
        0xc00003 "KHAOSS voice seat": ("bubble.py" "Bubble.py")  338x44+1558+1132  +1558+1132
                                                               ^ relative          ^ ABSOLUTE
    so the pattern that required end-of-line after the relative pair matched NOTHING, ever -- and
    `resolve_target({"window": ...})` answered "NO WINDOW matches 'KHAOSS voice seat'" for a window
    that was on screen at that moment. The green suite said PASS while the resolver could not see a
    single window, because the suite only ever asked it about windows it KNEW were absent.
    ★ The row is used for the ABSOLUTE corner (the last pair), and `list_windows()` then re-reads the
    truth with `xwininfo -id`. Both forms of the row are handled: named and `(has no name)`."""
    m = TITLE_RE.match(line)
    if not m:
        return None
    xid, title, w, h, rx, ry, ax, ay = m.groups()
    if ax is None:
        return xid, title, [int(rx), int(ry), int(w), int(h)]
    return xid, title, [int(ax), int(ay), int(w), int(h)]


def list_windows() -> list:
    """Every top-level window X knows about, with its geometry, off `xwininfo -root -tree` and
    `xwininfo -id` for truth. Clone of the measurement idiom the estate already uses (xwininfo +
    import), not a new dependency."""
    try:
        out = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception as e:
        return [{"error": f"{e.__class__.__name__}: {e}"}]
    wins = []
    for line in out.splitlines():
        parsed = parse_tree_line(line)
        if not parsed:
            continue
        xid, title, rect = parsed
        rec = {"id": xid, "title": title, "rel_rect": list(rect), "rect": list(rect)}
        # absolute truth from the window itself
        try:
            info = subprocess.run(["xwininfo", "-id", xid], capture_output=True, text=True,
                                  timeout=6).stdout
            ax = re.search(r"Absolute upper-left X:\s+(-?\d+)", info)
            ay = re.search(r"Absolute upper-left Y:\s+(-?\d+)", info)
            ww = re.search(r"Width:\s+(-?\d+)", info)
            hh = re.search(r"Height:\s+(-?\d+)", info)
            if ax and ay and ww and hh:
                rec["rect"] = [int(ax.group(1)), int(ay.group(1)), int(ww.group(1)), int(hh.group(1))]
        except Exception:
            pass
        wins.append(rec)
    return wins


# ★ HOW OLD A PANEL POSITION REPORT MAY BE BEFORE THE OVERLAY STOPS TRUSTING IT AS CURRENT.
#   The page posts a heartbeat every ~3 s while visible (a background tab is throttled to ~1/min, so
#   the window is deliberately generous); 90 s is one missed background beat, not a twitch.
PANEL_GEOM_STALE_S = 90.0


def _is_panel_anchor(name) -> bool:
    """Does this anchor name the KHAOSS/BoardWalk panel (the Wayland window X cannot see)?"""
    n = str(name or "").strip().lower()
    return bool(n) and ("khaoss" in n or "boardwalk" in n)


def _panel_geometry_info() -> dict:
    """The panel PAGE's own window rect, plus WHEN it said so.

    * A Wayland-native browser has no XID; `xwininfo` cannot see it. The page itself knows its
      screen position and outer/inner size. The overlay reads that, not a stale rectangle.
    * ★ THE AGE IS PART OF THE ANSWER. A report that is 10 minutes old describes where the panel WAS;
      returning the rectangle without that fact is how a ring ends up confidently on nothing. The
      caller decides whether to use the last known rect and say it is old, or to wait.
    """
    base = (os.environ.get("KHAOSS_PANEL_URL") or "http://127.0.0.1:7842").rstrip("/")
    try:
        import urllib.request as _u
        with _u.urlopen(base + "/operator/window-geometry", timeout=2) as r:
            d = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "unreachable", "rect": None,
                "error": f"the panel server did not answer at {base}: {e.__class__.__name__}"}
    if not isinstance(d, dict) or not d.get("ok"):
        return {"ok": False, "reason": "not_reported", "rect": None,
                "error": "the panel has not reported its window position yet"}
    try:
        sx = float(d["screen_x"]); sy = float(d["screen_y"])
        ow = float(d["outer_w"]); oh = float(d["outer_h"])
        iw = float(d["inner_w"]); ih = float(d["inner_h"])
        dpr = float(d.get("dpr") or 1.0)
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "malformed", "rect": None,
                "error": "the panel's geometry report is missing or malformed fields"}
    at = str(d.get("at") or "")
    age = None
    try:
        dt = datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.astimezone()
        age = max(0.0, time.time() - dt.timestamp())
    except (TypeError, ValueError):
        age = None
    stale = (age is None) or (age > PANEL_GEOM_STALE_S)
    left = sx + max(0.0, (ow - iw) / 2.0)
    top = sy + max(0.0, oh - ih)
    return {"ok": True, "reason": "reported", "at": at, "age": age, "stale": stale,
            "rect": [int(round(left * dpr)), int(round(top * dpr)),
                     int(round(iw * dpr)), int(round(ih * dpr))],
            "href": str(d.get("href") or "")}


def _panel_geometry() -> list | None:
    """The panel page's window rect, or None. Kept as the narrow accessor other callers already use;
    `_panel_geometry_info()` is the honest form (it carries age/staleness)."""
    info = _panel_geometry_info()
    return info.get("rect") if info.get("ok") else None


def _format_age(age) -> str:
    try:
        age = float(age)
    except (TypeError, ValueError):
        return "unknown age"
    if age < 2:
        return "just now"
    if age < 90:
        return f"{int(age)} s ago"
    if age < 5400:
        return f"{int(age / 60)} min ago"
    return f"{int(age / 3600)} h ago"


def resolve_target(target) -> dict:
    """Turn a target declaration into a screen rectangle, and SAY HOW.

    Returns {"ok": bool, "kind": str, "rect": [x,y,w,h] | None, "how": str, "error": str,
             "candidates": [...]}
    ★ A target that resolves to nothing is `ok: False` with an error. It is NEVER defaulted to (0,0)
      and never silently dropped -- that default is the exact defect this file's prove-fail injects.
    ★ Several windows matching one title is `ambiguous: True`: the pick is the LARGEST, and every
      candidate travels with the answer so he can see we had to choose."""
    if not isinstance(target, dict):
        return {"ok": False, "kind": None, "rect": None, "candidates": [],
                "error": f"target must be an object with one of {sorted(TARGET_KINDS)} -- got "
                         f"{type(target).__name__}"}
    kinds = [k for k in target if k in TARGET_KINDS]
    if len(kinds) != 1:
        return {"ok": False, "kind": None, "rect": None, "candidates": [],
                "error": f"target must name EXACTLY ONE of {sorted(TARGET_KINDS)}; it names "
                         f"{sorted(kinds) or 'none'}"}
    kind = kinds[0]
    val = target[kind]
    if kind == "window":
        _w = str(val or "").strip().lower()
        if _is_panel_anchor(_w) or _w in ("panel", "the panel"):
            info = _panel_geometry_info()
            if info.get("ok"):
                return {"ok": True, "kind": "window", "rect": info["rect"],
                        "how": ("panel-reported geometry (Wayland-safe)"
                                + ("" if not info.get("stale")
                                   else f"; last report {_format_age(info.get('age'))}")),
                        "error": "", "candidates": [], "ambiguous": False,
                        "stale": bool(info.get("stale")), "age": info.get("age"),
                        "at": info.get("at")}
            # ★ DO NOT FALL THROUGH TO X11. The panel is Wayland-native, so `xwininfo` can never see
            #   it; searching X anyway produced "NO WINDOW matches 'KHAOSS'", which blamed the NAME
            #   for a geometry feed that had simply not reported yet. Say the real thing instead.
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "reason": "panel_geometry_" + str(info.get("reason") or "missing"),
                    "error": str(info.get("error") or "the panel has not reported its position yet")}

    if kind == "rect":
        if not (isinstance(val, (list, tuple)) and len(val) == 4):
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "error": "rect must be [x, y, width, height]"}
        try:
            x, y, w, h = (int(v) for v in val)
        except (TypeError, ValueError):
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "error": f"rect values must be integers, got {val!r}"}
        if w <= 0 or h <= 0:
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "error": f"rect has no area ({w}x{h}) -- a ring round nothing is not a guide"}
        return {"ok": True, "kind": kind, "rect": [x, y, w, h], "candidates": [],
                "how": "explicit screen rectangle, used exactly as written"}

    if kind == "point":
        if not (isinstance(val, (list, tuple)) and len(val) == 2):
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "error": "point must be [x, y]"}
        try:
            x, y = int(val[0]), int(val[1])
        except (TypeError, ValueError):
            return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                    "error": f"point values must be integers, got {val!r}"}
        half = POINT_BOX // 2
        return {"ok": True, "kind": kind, "rect": [x - half, y - half, POINT_BOX, POINT_BOX],
                "candidates": [],
                "how": f"explicit screen point ({x},{y}); drawn as a {POINT_BOX}x{POINT_BOX} box "
                       f"centred on it"}

    # kind == "window"
    wins = list_windows()
    if wins and "error" in wins[0]:
        return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                "error": f"could not list windows: {wins[0]['error']}"}
    spec = str(val or "").strip()
    if not spec:
        return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                "error": "window target is empty -- name a title or an X id"}
    if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", spec):
        want = int(spec, 16) if spec.lower().startswith("0x") else int(spec)
        hits = [w for w in wins if int(w["id"], 16) == want]
        how = f"matched by X window id {spec}"
    else:
        rx = re.compile(re.escape(spec), re.I)
        hits = [w for w in wins if rx.search(w.get("title") or "")]
        how = f"matched by title, case-insensitive substring {spec!r}"
    if not hits:
        return {"ok": False, "kind": kind, "rect": None, "candidates": [],
                "error": f"NO WINDOW matches {spec!r}. There is nothing to point at, so this step "
                         f"cannot be shown -- it is reported, not skipped."}
    hits.sort(key=lambda w: w["rect"][2] * w["rect"][3], reverse=True)
    best = hits[0]
    out = {"ok": True, "kind": kind, "rect": list(best["rect"]), "candidates": hits[:8],
           "how": f"{how}; {'the largest of ' + str(len(hits)) + ' matches' if len(hits) > 1 else 'one match'}",
           "window_id": best["id"], "window_title": best["title"]}
    if len(hits) > 1:
        out["ambiguous"] = True
        out["ambiguous_note"] = (f"{len(hits)} windows match {spec!r}; the LARGEST was chosen and all "
                                 f"candidates are listed here. Naming the X id removes the choice.")
    return out


# ── scripts ──────────────────────────────────────────────────────────────────────────────────────
def load_script(path) -> dict:
    """Read a walkthrough from JSON or Markdown. Returns {"ok", "id", "title", "steps", "error"}.

    ★ THE SCRIPT IS THE AUTHORING FORMAT, not a second runtime. `--push` converts it into the board's
    own guide_focus sequence, so an agent-authored walk and a board-driven walk are the same walk."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"ok": False, "error": f"no such walkthrough file: {p}", "steps": []}
    raw = p.read_text(encoding="utf-8", errors="replace")
    if p.suffix.lower() in (".md", ".markdown"):
        return parse_markdown(raw, p)
    try:
        d = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"{p.name} is not valid JSON: {e}", "steps": []}
    if isinstance(d, list):
        d = {"id": p.stem, "title": p.stem, "steps": d}
    if not isinstance(d, dict) or not isinstance(d.get("steps"), list):
        return {"ok": False, "error": f"{p.name}: expected {{\"steps\": [...]}} or a bare list of steps",
                "steps": []}
    steps = []
    for n, s in enumerate(d["steps"], 1):
        if not isinstance(s, dict):
            return {"ok": False, "error": f"{p.name}: step {n} is not an object", "steps": []}
        st = dict(s)
        st.setdefault("id", f"step-{n}")
        st["step"] = n
        steps.append(st)
    return {"ok": True, "id": str(d.get("id") or p.stem), "title": str(d.get("title") or p.stem),
            "source": str(p), "steps": steps}


def parse_markdown(raw: str, p: Path) -> dict:
    """A tiny, strict Markdown walkthrough format, so any agent can author one without a schema tool.

        # Walk title
        ## step-id  |  Step title
        target: window "Settings"
        target: rect 100 200 300 80
        target: point 960 540
        body: what to do, on one or more lines
              continued lines are joined
        hint: a nudge
    """
    # ★★★ `find:` AND `action:` WERE MISSING, SO THE FORMAT I DOCUMENTED WOULD HAVE BEEN SILENTLY
    #     IGNORED. A parser that drops an unknown key without a word is the same failure class as the
    #     render check that inspected the wrong string: the file looks right and nothing happens.
    # ★ `find:` is the search's words -- the answer to "the target declaration is wrong".
    # ★ `action:` may REPEAT, so it appends rather than overwriting. Its value is `Label | command`.
    KEYS = {"target:": "t", "body:": "b", "hint:": "h", "find:": "find",
            "action:": "action", "open:": "open",
            # ★ identity for a rect: without it a rectangle cannot follow the window it is about.
            "anchor:": "anchor", "anchor_ocr:": "anchor_ocr", "track:": "track",
            "why_it_matters:": "why_it_matters", "what_can_go_wrong:": "what_can_go_wrong"}
    steps = []
    title = p.stem
    cur = None
    last_key = None
    for rawline in raw.splitlines():
        line = rawline.strip()
        if rawline.startswith("# ") and not rawline.startswith("## "):
            if cur is None:
                title = rawline[2:].strip() or title
            continue
        if rawline.startswith("## "):
            if cur:
                steps.append(cur)
            head = rawline[3:].strip()
            if "|" in head:
                sid, stitle = [x.strip() for x in head.split("|", 1)]
            else:
                sid, stitle = head, head
            cur = {"id": sid or f"step-{len(steps) + 1}", "title": stitle}
            last_key = None
            continue
        if not line:
            continue
        if cur is None:
            continue
        hit = next((k for k in KEYS if line.lower().startswith(k)), None)
        if hit:
            if hit == "action:":
                cur.setdefault("_actions", []).append(line[len(hit):].strip())
                last_key = "_actionlast"
            else:
                last_key = KEYS[hit]
                cur[last_key] = line[len(hit):].strip()
        elif last_key == "_actionlast" and cur.get("_actions"):
            cur["_actions"][-1] = (cur["_actions"][-1] + " " + line).strip()
        elif last_key:
            # a continuation line belongs to whatever key was written last
            cur[last_key] = (cur.get(last_key, "") + " " + line).strip()
    if cur:
        steps.append(cur)

    out = []
    for n, s in enumerate(steps, 1):
        tgt, err = _parse_target_text(s.get("t") or "")
        if err:
            return {"ok": False, "steps": [], "error": f"{p.name} step {s.get('id')!r}: {err}"}
        # ★ action lines become real buttons: `Label | command`. A line with no pipe uses the command
        #   as its own label, truncated -- refusing it outright would be stricter than useful.
        _acts = []
        for _a in (s.get("_actions") or []):
            _a = _a.strip()
            if not _a:
                continue
            if "|" in _a:
                _lbl, _cmd = _a.split("|", 1)
                _acts.append({"label": _lbl.strip(), "cmd": _cmd.strip()})
            else:
                _acts.append({"label": _a[:24], "cmd": _a})
        out.append({"id": s["id"], "title": s.get("title") or s["id"], "target": tgt,
                    "body": s.get("b") or "", "hint": s.get("h") or "",
                    "find": s.get("find") or "", "actions": _acts,
                    "open": s.get("open") or "", "step": n,
                    "anchor": s.get("anchor") or "",
                    "anchor_ocr": s.get("anchor_ocr") or "",
                    "track": (s.get("track") or "on"),
                    "why_it_matters": s.get("why_it_matters") or "",
                    "what_can_go_wrong": s.get("what_can_go_wrong") or ""})
    if not out:
        return {"ok": False, "steps": [], "error": f"{p.name}: no steps found. Each step starts with a "
                                                   f"line beginning '## '."}
    return {"ok": True, "id": p.stem, "title": title, "source": str(p), "steps": out}


def _parse_target_text(text: str):
    """`window "Settings"` / `window 0x1e00003` / `rect 1 2 3 4` / `rect 1,2,3,4` / `point 5 6`."""
    t = (text or "").strip()
    if not t:
        return None, "no target: line (a step that points at nothing is not a guide)"
    head = t.split(None, 1)[0].lower()
    rest = t[len(head):].strip()
    if head not in TARGET_KINDS:
        return None, f"unknown target kind {head!r}; expected one of {sorted(TARGET_KINDS)}"
    if head == "window":
        return {"window": rest.strip().strip('"').strip("'")}, None
    nums = [x for x in re.split(r"[,\s]+", rest) if x]
    try:
        nums = [int(x) for x in nums]
    except ValueError:
        return None, f"{head} needs integers, got {rest!r}"
    if head == "rect":
        if len(nums) != 4:
            return None, f"rect needs 4 numbers (x y w h), got {len(nums)}"
        return {"rect": nums}, None
    if len(nums) != 2:
        return None, f"point needs 2 numbers (x y), got {len(nums)}"
    return {"point": nums}, None


def plan(script: dict) -> dict:
    """Resolve EVERY step up front. Returns the render plan plus the honest counts.

    ★★★ THIS IS WHERE SILENT SKIPPING WOULD LIVE, AND IT DOES NOT. `unresolved` and `ambiguous` are
    both reported, and `ok` is False whenever ANY step failed to resolve -- so a walkthrough that
    points at nothing is a FAILED walkthrough, loudly, not a shorter one."""
    steps = script.get("steps") or []
    out, bad, amb = [], [], []
    for s in steps:
        r = resolve_target(s.get("target"))
        # ★★★ A RECT WITH NO IDENTITY IS A DECLARATION THAT GOES STALE THE MOMENT A WINDOW MOVES.
        #     When the walk names an anchor window, the rect is treated as WINDOW-RELATIVE and the
        #     overlay can re-read the window's current geometry every 700 ms. No anchor = the old
        #     screen-absolute behaviour, and the card says so instead of pretending.
        _anchor = str(s.get("anchor") or "").strip()
        _anchor_ocr = str(s.get("anchor_ocr") or "").strip()
        _track = str(s.get("track") or "on").strip().lower() not in ("off", "false", "no", "0")
        _anchor_state, _anchor_age = "", None
        if _anchor and isinstance(s.get("target"), dict) and s["target"].get("rect") and r.get("ok"):
            # the panel reports its own rect; prefer that over an X11 lookup that cannot see Wayland
            if _is_panel_anchor(_anchor):
                _info = _panel_geometry_info()
                _w = ({"ok": True, "rect": _info["rect"]} if _info.get("ok")
                      else {"ok": False, "error": _info.get("error")})
                _anchor_age = _info.get("age")
            else:
                _info = {}
                _w = resolve_target({"window": _anchor})
            if _w.get("ok") and _w.get("rect"):
                _rel = s["target"]["rect"]
                r = {**r, "rect": [_w["rect"][0] + _rel[0], _w["rect"][1] + _rel[1], _rel[2], _rel[3]],
                     "how": (r.get("how") or "") + f"; anchored to window {_anchor!r}"}
                _anchor_state = "stale" if _info.get("stale") else "fresh"
                if _info.get("stale"):
                    r["how"] += f" (last panel report {_format_age(_info.get('age'))})"
            else:
                _anchor_state = "missing"
                r = {**r, "ok": False, "rect": None,
                     "reason": "panel_geometry_" + str(_info.get("reason") or "missing")
                               if _is_panel_anchor(_anchor) else "anchor_missing",
                     "error": (str(_info.get("error")) if _is_panel_anchor(_anchor)
                               else f"anchor window {_anchor!r} not found: {_w.get('error')}")}
        rec = {"id": s.get("id"), "step": s.get("step"), "title": s.get("title"),
               "body": s.get("body") or "", "hint": s.get("hint") or "",
               # ★★★ AND HERE IS WHERE `open:` WAS ACTUALLY DYING, NOT IN push_walk.
               #     `plan()` builds a FRESH dict from a chosen list of keys, so `open`, `find` and
               #     `actions` were dropped the moment a walk was planned -- two stages before the code
               #     that reads them. I patched push_walk first because that is where the store is
               #     written, but a field has to survive EVERY stage, and the earliest one is the one
               #     that matters. (set_sequence already passes unknown keys through -- that part was
               #     never the problem, which is exactly why reading the chain in order beats patching
               #     where the symptom appears.)
               "open": s.get("open") or "", "find": s.get("find") or "",
               "actions": s.get("actions") or [],
               "illustration": normalize_illustration(s.get("illustration"))
                               if (s.get("illustration") and normalize_illustration) else None,
               "target": s.get("target"), "resolved": bool(r["ok"]), "kind": r.get("kind"),
               "rect": r.get("rect"), "how": r.get("how"), "error": r.get("error"),
               "reason": r.get("reason") or "",
               "ambiguous": bool(r.get("ambiguous")), "candidates": r.get("candidates") or [],
               "anchor": _anchor, "anchor_ocr": _anchor_ocr, "track": _track,
               "anchor_state": _anchor_state or ("stale" if r.get("stale") else
                                                 ("missing" if r.get("reason") else "")),
               "anchor_age": _anchor_age if _anchor_age is not None else r.get("age"),
               "recovery": ("Hard-reload the panel page once (Ctrl+Shift+R) so it posts where its "
                            "window is; the overlay re-aims by itself within a second."
                            if _anchor_state == "missing" or
                            str(r.get("reason") or "").startswith("panel_geometry") else
                            ("The ring is on the last position the panel reported. Hard-reload the "
                             "panel page (Ctrl+Shift+R) if it has moved; the overlay follows a new "
                             "report by itself."
                             if (_anchor_state == "stale" or r.get("stale")) else "")),
               "why_it_matters": s.get("why_it_matters") or "",
               "what_can_go_wrong": s.get("what_can_go_wrong") or ""}
        out.append(rec)
        if not r["ok"]:
            bad.append({"id": s.get("id"), "target": s.get("target"), "error": r.get("error")})
        elif r.get("ambiguous"):
            amb.append({"id": s.get("id"), "target": s.get("target"),
                        "chose": r.get("window_id"), "chose_title": r.get("window_title"),
                        "n_candidates": len(r.get("candidates") or [])})
    return {"ok": not bad, "id": script.get("id"), "title": script.get("title"),
            "source": script.get("source"), "steps": out, "n_steps": len(out),
            "unresolved": bad, "ambiguous": amb,
            "step_ids": sorted(str(s.get("id")) for s in steps),
            "ring_clamped_steps": [s.get("id") for s in out
                                   if s.get("rect") and ring_rect(s["rect"], _screen_geometry())[1]],
            "note": ("every step resolved" if not bad else
                     f"{len(bad)} step(s) point at NOTHING on this screen. They are listed in "
                     f"`unresolved` and this plan is FAILED -- a step that cannot be shown is not "
                     f"a step that was skipped.")}


# ── archive: nothing is ever deleted ─────────────────────────────────────────────────────────────
def archive_before_write(path: Path) -> dict:
    """★ NOTHING IS EVER DELETED. Before this module overwrites one of its own files we keep a
    timestamped copy beside it, newest 20, older ones moved to `backups/` -- the same keep-20 rule
    `backup_panel.sh` enforces for panel_server.py. (Our own files only; other lanes' files are
    never touched by this function.)"""
    try:
        if not path.is_file():
            return {"archived": False, "why": "nothing to overwrite yet"}
        ts = time.strftime("%Y%m%d_%H%M%S")
        dst = path.with_name(f"{path.name}.bak.{ts}_point_to")
        dst.write_bytes(path.read_bytes())
        olds = sorted(path.parent.glob(f"{path.name}.bak.*"))
        keep = olds[-20:]
        arch = REPO / "backups" / "point_to"
        moved = []
        for f in olds[:-20]:
            arch.mkdir(parents=True, exist_ok=True)
            tgt = arch / f.name
            if not tgt.exists():
                f.replace(tgt)
                moved.append(tgt.name)
        return {"archived": True, "kept": dst.name, "n_kept": len(keep), "moved_to_archive": moved}
    except Exception as e:
        return {"archived": False, "why": f"{e.__class__.__name__}: {e}"}


# ── the overlay ──────────────────────────────────────────────────────────────────────────────────
def _screen_geometry():
    """The WORK AREA -- the screen MINUS the panels. ★★★ THIS USED TO BE THE WHOLE SCREEN ON PURPOSE.

    The original docstring read: *"the scrim covers the whole screen, panels included."* That was a
    deliberate choice and it was the wrong one. With `keep_above` and a 42% scrim, covering the panels
    means the GNOME topbar and the dock are dimmed and visually GONE for as long as the guide runs --
    the operator: *"we lost the Gnome topbar and doc at the bottom. We want to keep those up and
    running and not accidentally kill/lose them in any way."*

    ★ NOTHING WAS KILLED. gnome-shell was up (12 days) and the ding dock extension with it; the overlay
      was simply PAINTING OVER them. Measured: `_NET_WORKAREA = 0, 29, 1920, 1171` against a root of
      `1920x1200` -- 29px of topbar and the dock strip, both of which the plane was swallowing.
    ★★★ A GUIDE MUST NOT TAKE THE DESKTOP AWAY. The work area is the correct frame for every choice
      this file makes: where the plane sits, where its scrim stops, and how the ring clamps.
    ★ Falls back to the monitor geometry rather than raising -- a walkthrough must still be readable on
      a box with no display, and `plan()` uses this for its off-screen report."""
    # ★ _NET_WORKAREA first: it is the authoritative answer and it already accounts for EVERY panel,
      #   including docks the operator has moved or resized.
    try:
        import subprocess as _sp
        r = _sp.run(["xprop", "-root", "_NET_WORKAREA"], capture_output=True, timeout=6, text=True)
        m = None
        import re as _re
        mm = _re.search(r"=\s*([-\d]+),\s*([-\d]+),\s*([-\d]+),\s*([-\d]+)", r.stdout or "")
        if mm:
            x, y, w, h = (int(g) for g in mm.groups())
            if w > 200 and h > 200:
                return x, y, w, h
    except Exception:  # noqa: BLE001
        pass
    try:
        d = Gdk.Display.get_default()
        mon = (d.get_primary_monitor() or d.get_monitor(0)) if d else None
        if mon:
            w = mon.get_workarea()
            if w and w.width > 200 and w.height > 200:
                return w.x, w.y, w.width, w.height
            g = mon.get_geometry()
            return g.x, g.y, g.width, g.height
        scr = Gdk.Screen.get_default()
        if scr:
            return 0, 0, scr.get_width(), scr.get_height()
    except Exception:
        pass
    return 0, 29, 1920, 1171


class Overlay:
    """The whole overlay: one click-through PLANE that only draws, one CARD that he can press.

    ★ TWO WINDOWS IS THE POINT, not an accident. The plane is transparent to input at the X protocol
    level, so every click inside the ring lands on the real control underneath -- which is what makes
    this a guide rather than a modal. The card is a normal window because a guide whose Next button
    cannot be pressed is a picture of a guide."""

    def __init__(self, steps, index=0, mode="script", on_next=None, hold=None, auto=None):
        self.steps = steps
        self.i = max(0, min(index, len(steps) - 1))
        self.mode = mode
        self.on_next = on_next
        self.hold = hold
        self.auto = auto
        self.record = {"shown": [], "unresolved_shown": [], "guard": {}}
        sx, sy, sw, sh = _screen_geometry()
        self.screen = [sx, sy, sw, sh]

        # ── PLANE ────────────────────────────────────────────────────────────────────────────────
        self.plane = Gtk.Window(title="point-me-to-there plane")
        self.plane.set_decorated(False)
        self.plane.set_resizable(False)
        self.plane.set_accept_focus(False)
        self.plane.set_focus_on_map(False)
        self.plane.set_can_focus(False)
        self.plane.set_skip_taskbar_hint(True)
        self.plane.set_skip_pager_hint(True)
        self.plane.set_keep_above(True)
        self.plane.stick()
        self.plane.set_app_paintable(True)
        scr = Gdk.Screen.get_default()
        vis = scr.get_rgba_visual() if scr else None
        if vis is not None and scr.is_composited():
            self.plane.set_visual(vis)
        # ★★ MEASURED LIVE, AND IT MATTERS: move()+resize() BEFORE show_all() does not size a GTK
        #    window that has no content -- the first build came up 200x200+0+29 on a 1920x1200 screen,
        #    so the "full-screen overlay" was a 200-pixel square in the top-left and the operator would
        #    have seen nothing. The size comes from a DrawingArea child with an explicit request plus
        #    set_default_size, and is re-asserted after map.
        self.plane.set_default_size(sw, sh)
        self.plane.set_size_request(sw, sh)
        self.canvas = Gtk.DrawingArea()
        self.canvas.set_size_request(sw, sh)
        self.canvas.connect("draw", self._on_draw)
        self.plane.add(self.canvas)
        self.plane.move(sx, sy)
        # ★ THE SHAPE IS RE-APPLIED, NOT SET ONCE. GTK/X re-derives the window shape on map, resize
        #   and configure, so a one-shot call leaves a window that eats clicks after the first
        #   resize. Every one of these paths re-asserts and re-measures.
        for sig in ("map-event", "configure-event", "screen-changed", "realize"):
            self.plane.connect(sig, self._reassert)

        # ── CARD ─────────────────────────────────────────────────────────────────────────────────
        self.card = Gtk.Window(title="point-me-to-there card")
        self.card.set_decorated(False)
        self.card.set_skip_taskbar_hint(True)
        self.card.set_keep_above(True)
        self.card.stick()
        self.card.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        # ★ HE MUST BE ABLE TO PRESS ESCAPE WITHOUT THE CARD HAVING STOLEN HIS FOCUS FIRST.
        #   set_focus_on_map(False) means opening the guide does not yank the caret out of whatever he
        #   was typing in; he can still click the card to focus it, and Esc then closes it.
        self.card.set_focus_on_map(False)
        self.card.set_accept_focus(True)
        # ★ EVERY SIZE HERE IS A FLOOR. 430 px is the design width, but a 430 px floor on a 400 px
        #   screen would push the card past the edge -- and a GTK minimum is not a suggestion. On a
        #   narrow display the floor shrinks; on the operator's 1920 px screen it is exactly 430.
        self.card_width_floor = max(320, min(430, int(self.screen[2]) - 16))
        self.card.set_size_request(self.card_width_floor, -1)
        self.card.connect("key-press-event", self._on_key)
        self.card.connect("delete-event", self._on_delete)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build_card()
        self._apply(self.i)

        GLib.timeout_add(400, self._poll_cmd)
        # ★ the target identity tracker; OCR is not on this timer
        GLib.timeout_add(700, self._track_targets)
        if self.auto:
            GLib.timeout_add(int(self.auto * 1000), self._auto_step)
        if self.hold:
            GLib.timeout_add(int(self.hold * 1000), self._quit)

    # ── card construction ────────────────────────────────────────────────────────────────────────
    def _build_card(self):
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box.set_name("pt-card")
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.set_name("pt-head")
        head.set_border_width(6)
        self.badge = Gtk.Label(label="")
        self.badge.set_name("pt-badge")
        self.badge.set_xalign(0)
        self.who = Gtk.Label(label="")
        self.who.set_name("pt-who")
        self.who.set_xalign(0)
        xbtn = Gtk.Button(label="\u2715")
        xbtn.set_name("pt-x")
        xbtn.set_relief(Gtk.ReliefStyle.NONE)
        xbtn.set_tooltip_text("Leave the walk: one choice to keep it in the backlog, log it for later, "
                              "or stop now \u2014 nothing is marked done by leaving.")
        xbtn.connect("clicked", lambda *_: self._leave_choice())
        head.pack_start(self.badge, False, False, 0)
        head.pack_start(self.who, False, False, 0)
        head.pack_end(xbtn, False, False, 0)
        self.box.pack_start(head, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_border_width(10)
        self.title = Gtk.Label(label="")
        self.title.set_name("pt-title")
        self.title.set_xalign(0)
        self.title.set_line_wrap(True)
        self.title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.body = Gtk.Label(label="")
        self.body.set_name("pt-body")
        self.body.set_xalign(0)
        self.body.set_line_wrap(True)
        self.body.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.body.set_max_width_chars(58)
        self.hint = Gtk.Label(label="")
        self.hint.set_name("pt-hint")
        self.hint.set_xalign(0)
        self.hint.set_line_wrap(True)
        self.hint.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.err = Gtk.Label(label="")
        self.err.set_name("pt-err")
        self.err.set_xalign(0)
        self.err.set_line_wrap(True)
        for w in (self.title, self.body, self.hint, self.err):
            body.pack_start(w, False, False, 0)
        self.state_lbl = Gtk.Label(label="")
        self.state_lbl.set_name("pt-state")
        self.state_lbl.set_xalign(0)
        self.state_lbl.set_line_wrap(True)
        body.pack_start(self.state_lbl, False, False, 0)
        self.mirror = Gtk.Label(label="")
        self.mirror.set_name("pt-mirror")
        self.mirror.set_xalign(0)
        self.mirror.set_line_wrap(True)
        body.pack_start(self.mirror, False, False, 0)

        # ★★★ THE CONTROL ROW. The operator asked for this by name: *"we need buttons for pkill etc...
        #     other actions"*, *"a button disengage the screen dimming/outlining feature ... to an
        #     on/off style"*, *"a button that tells the agent whether they think that is the actual
        #     spot the item is or not"*, and *"a jump-to button to get the user back to the item/task
        #     they are on."* Four needs, one row, because a guide you cannot argue with is a slideshow.
        # ★★★ "THE GUI IS TOO WIDE AND NEEDS TO BE REACTIVELY ABLE TO STRETCH/CONDENSE ROW BY ROW."
        #     -- the operator, looking at a 1004px card. Ten buttons in ONE Gtk.Box cannot wrap, so the
        #     card grew to fit the row instead of the row wrapping to fit the card.
        # ★ A Gtk.FlowBox WRAPS ITS CHILDREN, which is exactly "stretch/condense row by row": the
        #   controls reflow onto as many rows as the width needs, so the card can stay narrow.
        ctl = Gtk.FlowBox()
        ctl.set_selection_mode(Gtk.SelectionMode.NONE)
        ctl.set_max_children_per_line(4)
        ctl.set_min_children_per_line(1)
        ctl.set_row_spacing(6)
        ctl.set_column_spacing(6)
        ctl.set_homogeneous(False)
        # ★ KEPT AS A NAMED WIDGET, NOT A LOCAL, so the layout report can MEASURE the floor the
        #   control grid imposes. "The card is 430 wide" is not evidence that the FlowBox fits; the
        #   test reads the grid's own preferred widths.
        self.flow = ctl
        # ★ "dim button needs to say on/off" -- so the LABEL states the current state plainly rather
        #   than implying it. A toggle whose label describes the ACTION leaves you guessing which way
        #   it is set; one that describes the STATE cannot be misread.
        # ★★★ "I MEANT IT ONLY SAYS dim:on/off AND NOTHING ELSE BUT SHOULD CHANGE COLOUR TO INDICATE
        #     IF IT'S ON/OFF." -- so the LABEL is the state and the COLOUR is the signal. Emphasis via
        #     capitals put the burden back on reading; a colour is legible at a glance, which is the
        #     whole point of a status control.
        # ★★★ "WE DON'T NEED THE WORDS TO CHANGE, LITERALLY SHOULD READ 'dim: on/off'." -- the
        #     operator, correcting me a second time. I had been SWAPPING the label between "dim: on"
        #     and "dim: off"; what he wants is a STATIC label that names the control and its two
        #     states, with the COLOUR doing all the signalling. Static text cannot be misread as the
        #     current state, because it never pretends to be one.
        # ★★★ THE ESCAPE, FIRST IN THE GRID. The operator was inside a guide and could not find a
        #     way out; the only exits were a small X (which explicitly did NOT end the walk) and a
        #     bottom button labelled "leave the walk" that read like a hide. A cancel that ENDS the
        #     walk belongs beside the controls he is already scanning, in red, with the word
        #     "cancel" on it. ONE store: it calls guide_focus.dismiss() like every other cancel.
        self.cancel_btn = Gtk.Button(label="\u2715 cancel walk")
        self.cancel_btn.set_name("pt-cancel")
        self.cancel_btn.set_tooltip_text(
            "Leave the walk: one choice to keep it in the backlog, log it for later, or stop now. "
            "Nothing is marked done by leaving.")
        self.cancel_btn.connect("clicked", lambda *_: self._cancel_walk())
        ctl.add(self.cancel_btn)

        # ★ THE DIAGRAM TOGGLE. Only steps whose author supplied an `illustration` show it. It is a
        #   static label like `dim`; the diagram is ON by default (the operator asked for it by
        #   default), and this control lets him turn it off without leaving the step.
        self.illus_btn = Gtk.Button(label="diagram: on/off")
        self.illus_btn.set_name("pt-illus")
        self.illus_btn.set_no_show_all(True)
        self.illus_btn.set_visible(False)
        self.illus_btn.set_tooltip_text(
            "Show or hide the agent's diagram for this step. It is drawn on the click-through plane "
            "in the BoardWalk colours; it is data, not a web page.")
        self.illus_btn.connect("clicked", lambda *_: self.toggle_illustration())
        ctl.add(self.illus_btn)

        self.dim_btn = Gtk.Button(label="dim: on/off")
        self.dim_btn.set_name("pt-dim")
        self.dim_btn.set_tooltip_text("Turn the dimming and the outline OFF so you can see the screen "
                                      "normally, and back ON. The card stays either way, so you are "
                                      "never locked into the overlay's view.")
        self.dim_btn.connect("clicked", lambda *_: self.toggle_dim())
        ctl.add(self.dim_btn)

        self.yes_btn = Gtk.Button(label="\u2713 right spot")
        self.yes_btn.set_name("pt-yes")
        self.yes_btn.set_tooltip_text("That is the right thing. Recorded so the agent knows the outline "
                                      "landed where it meant to.")
        self.yes_btn.connect("clicked", lambda *_: self.verdict(True))
        ctl.add(self.yes_btn)

        self.no_btn = Gtk.Button(label="\u2717 wrong spot")
        self.no_btn.set_name("pt-no")
        self.no_btn.set_tooltip_text("That is NOT the thing. Records your verdict, re-resolves the "
                                     "target, and tries to bring the right window to the front.")
        self.no_btn.connect("clicked", lambda *_: self.verdict(False))
        ctl.add(self.no_btn)
        # * THE AGENT SURFACE, ON THE CARD: Explain / Ask / Diagram / Agent switch.
        self.agent_btn = Gtk.Button(label="\u25e7 agent / explain")
        self.agent_btn.set_name("pt-agent")
        self.agent_btn.set_tooltip_text("Open the per-step agent panel: explanation, Q/A, a diagram "
                                        "request, and the agent the overlay is working with.")
        self.agent_btn.connect("clicked", lambda *_: self.open_agent_panel())
        ctl.add(self.agent_btn)
        # * BRING ITEM TO FRONT: raise an X11 target; for the Wayland panel ask the page to focus,
        #   scroll and flash the item. It refuses honestly when the compositor keeps another window.
        self.front_btn = Gtk.Button(label="\u2934 front")
        self.front_btn.set_name("pt-front")
        self.front_btn.set_tooltip_text("Bring this step's item/window to the front and flash it. "
                                        "Wayland panels are raised by the page, not by X11.")
        self.front_btn.connect("clicked", lambda *_: self.bring_to_front())
        ctl.add(self.front_btn)

        # ★★★ "WE NEED A BUTTON FOR VOICE ASSISTANT/THE AGENT TO ALSO ACTIVELY SEARCH FOR THE RIGHT
        #     SPOT AFTER WE'VE CLICKED THE 'WRONG SPOT' BUTTON." -- the operator. 'wrong spot' as
        #     shipped only RE-RESOLVED the same declaration, which is useless when the DECLARATION is
        #     what is wrong: step 3 declares a hardcoded rectangle, so re-resolving it returns the same
        #     wrong rectangle forever. SEARCH is the missing verb -- give it words instead of a
        #     coordinate and let it go and LOOK.
        # ★★★ "LOOKS LIKE WE'LL NEED AN 'OPEN/REOPEN IT' BUTTON TOO THAT OPENS UP A NEW WINDOW OF
        #     WHATEVER THE GUIDE IS REFERRING TO." -- he is right and this session proved why: the
        #     Boardwalk step searched all four windows and found nothing, because the board was not
        #     open at all. NO AMOUNT OF SEARCHING finds a thing that is not running. The answer to
        #     "it's not there" is to OPEN IT, not to look harder.
        self.open_btn = Gtk.Button(label="\u25b6 open it")
        self.open_btn.set_name("pt-open")
        self.open_btn.set_tooltip_text("Open (or reopen) whatever this step is about -- a URL, an app "
                                       "or a command. Use this when the thing simply is not on screen.")
        self.open_btn.connect("clicked", lambda *_: self.open_target())
        ctl.add(self.open_btn)

        self.search_btn = Gtk.Button(label="\U0001f50d search for it")
        self.search_btn.set_name("pt-search")
        self.search_btn.set_tooltip_text("Actively SEARCH every window for the thing this step is "
                                         "about, point at the best match, and show what else it found. "
                                         "Use this when the outline is on the wrong thing.")
        self.search_btn.connect("clicked", lambda *_: self.search_for_it())
        ctl.add(self.search_btn)

        # ★★★ "WE'LL NEED A VOICE ASSIST ON/OFF FOR THIS TOO THAT ALLOWS THE USER TO HAVE IT READ THE
        #     TEXT THERE WITH AN OPTION TO RE-READ." -- so: one button that READS the card aloud, and
        #     one toggle for whether each new step is read AUTOMATICALLY. Two different needs: "say
        #     that again" and "keep reading to me".
        # ★★★ "WE CAN COMBINE THE VOICE ON/OFF AND RE-READ BUTTON DIVIDED INTO TWO HALVES." -- one
        #     control, two halves: LEFT toggles auto-reading, RIGHT re-reads the card. They answer the
        #     same question ("should this be read to me?"), and as two separate tiles they spent two
        #     slots in the grid for one idea.
        # ★ Separate Gtk.Buttons inside one joined frame, so each keeps its own hit area -- a single
        #   button splitting on x-offset is the kind of cleverness that breaks when text changes width.
        _split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        _split.get_style_context().add_class("pt-split")
        self.auto_btn = Gtk.Button(label="voice: on/off")
        self.auto_btn.set_name("pt-auto")
        self.auto_btn.set_tooltip_text("LEFT HALF -- voice assist ON/OFF. When ON, every new step is "
                                       "read aloud automatically. Colour shows the state.")
        self.auto_btn.connect("clicked", lambda *_: self.toggle_voice())
        _split.pack_start(self.auto_btn, True, True, 0)
        self.read_btn = Gtk.Button(label="\U0001f50a re-read")
        self.read_btn.set_name("pt-read")
        self.read_btn.set_tooltip_text("RIGHT HALF -- read this card aloud. Press again to re-read "
                                       "from the top.")
        self.read_btn.connect("clicked", lambda *_: self.speak_card())
        _split.pack_start(self.read_btn, True, True, 0)
        ctl.add(_split)

        # ★★★ "WE NEED A WAY TO DISENGAGE/STOP THE VOICE MID TALK." voicebox already had POST /stop
        #     (verified 200), so this exposes a control that existed and was unreachable.
        self.stop_btn = Gtk.Button(label="\u23f9 stop voice")
        self.stop_btn.set_name("pt-stopvoice")
        self.stop_btn.set_tooltip_text("Stop the voice mid-sentence. Calls voicebox /stop, which "
                                       "terminates the aplay child voicebox owns.")
        self.stop_btn.connect("clicked", lambda *_: self.stop_voice())
        ctl.add(self.stop_btn)


        # ★ READ THE STORED PREFERENCE AT BUILD TIME. The first version initialised the label to
        #   "lang: en" and only ever updated it on click, so it displayed the WRONG language for the
        #   actual setting until you pressed it -- a label disagreeing with the state it labels.
        self.lang_btn = Gtk.Button(label="lang: " + (self._voice_pref().get("language") or "en"))
        self.lang_btn.set_name("pt-lang")
        self.lang_btn.set_tooltip_text("Read-aloud language. Cycles only the languages that actually "
                                       "have a voice installed on this box.")
        # ★★★ "WE NEED A DROPDOWN FOR THE LANGUAGE CHANGING, NOT A SIMPLE SWITCH." -- a cycle button
        #     hides its options: you cannot see what languages exist without pressing it repeatedly,
        #     and you cannot go straight to the one you want. Same treatment as the voice picker.
        self.lang_btn.connect("clicked", lambda *_: self.pick_language())
        ctl.add(self.lang_btn)

        # ★★★ "WE DO NEED TO BE ABLE TO CHOOSE AND BIND THE DEFAULT IN THERE TOO." -- so the card
        #     gets a real PICKER, not just a language cycler. It lists every voice voicebox actually
        #     has (17 of them), marks the current one, and WRITES THE CHOICE to the preference file so
        #     it is the default from then on -- including for the auto-read.
        self.voice_btn = Gtk.Button(label="voice\u2026")
        self.voice_btn.set_name("pt-voicepick")
        self.voice_btn.set_tooltip_text("Choose the read-aloud voice and BIND it as the default. "
                                        "Any change here persists to operator/point_to_voice.json.")
        self.voice_btn.connect("clicked", lambda *_: self.pick_voice())
        ctl.add(self.voice_btn)

        self.jump_btn = Gtk.Button(label="\u2316 jump to it")
        self.jump_btn.set_name("pt-jump")
        self.jump_btn.set_tooltip_text("Bring the item this step is about to the FRONT and redraw the "
                                       "ring on it -- for when it has gone behind other windows.")
        self.jump_btn.connect("clicked", lambda *_: self.jump_to())
        ctl.add(self.jump_btn)

        # ★ THE HARD STOP. The X closes the overlay; this kills the process. He asked for "pkill etc",
        #   and a guide with no way out that survives its own UI is the thing that got us here.
        self.kill_btn = Gtk.Button(label="\u23fb kill")
        self.kill_btn.set_name("pt-kill")
        self.kill_btn.set_tooltip_text("Stop the overlay process outright. Same as --kill.")
        self.kill_btn.connect("clicked", lambda *_: self._quit())
        ctl.add(self.kill_btn)
        body.pack_start(ctl, False, False, 2)

        # ★★ AND ANY STEP MAY CARRY ITS OWN ACTIONS (the "pkill etc" general case). Author them in the
        #    walk file as `action: label | command`, and each becomes a button that runs after an
        #    explicit confirm -- because a guide that can run arbitrary commands unattended is a
        #    remote-execution surface wearing a tutorial's clothes.
        self.actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        body.pack_start(self.actions_box, False, False, 2)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.back_btn = Gtk.Button(label="\u25c0 back")
        self.back_btn.get_style_context().add_class("pt-nav")
        self.back_btn.set_tooltip_text("Previous step")
        self.back_btn.connect("clicked", lambda *_: self._go_back())
        self.next_btn = Gtk.Button(label="next \u25b6")
        self.next_btn.get_style_context().add_class("pt-go")
        self.next_btn.set_tooltip_text("Done with this one (or Next) -- records progress in the SAME "
                                       "store the board reads")
        self.next_btn.connect("clicked", lambda *_: self.do_next())
        self.leave_btn = Gtk.Button(label="\u2715 cancel walk")
        self.leave_btn.get_style_context().add_class("pt-leave")
        self.leave_btn.set_tooltip_text("End this walk and close the overlay. Your place stays in "
                                        "the board and nothing is marked done. Shift-click closes "
                                        "the overlay only.")
        self.leave_btn.connect("clicked", self._on_leave)
        # ★★★ "LEAVE THE WALK IS WAY TOO CLOSE TO NEXT AND NEEDS TO BE SHIFTED TO THE MIDDLE AT THE
        #     BOTTOM." -- the operator, after driving it. He is right and the reason is not cosmetic:
        #     Leave and Next sat side by side, so the one control that ABANDONS the walk was a
        #     mis-click away from the one that advances it. A destructive control adjacent to the
        #     common one is how you lose someone's place by accident.
        # ★ `set_center_widget` is a REAL middle -- not a spacer that looks like one -- so it stays
        #   centred however the window is resized.
        foot.pack_start(self.back_btn, False, False, 0)
        foot.pack_end(self.next_btn, False, False, 0)
        try:
            foot.set_center_widget(self.leave_btn)
        except Exception:  # noqa: BLE001 -- older GTK: fall back to a spaced middle, not to adjacent
            foot.pack_start(self.leave_btn, True, True, 0)
        body.pack_start(foot, False, False, 4)
        self.box.pack_start(body, False, False, 0)

        # ★★★ "WE NEED TO BE ABLE TO DRAG AROUND THE GUIDE BOX BECAUSE SOMETIMES IT COVERS THINGS I
        #     NEED TO ACCESS." -- so the HEADER becomes a drag handle. It moves the card and STOPS
        #     AUTO-PLACEMENT, because a card that snaps back the moment the ring moves is worse than
        #     one that never moved: he would have to fight it.
        # ★ Bound to the HEADER only, never the whole card: a drag handler over the buttons is exactly
        #   what ate every click in the voice bubble ("the drag handler was on the window and it ate
        #   every button click"). The handle must not overlap the controls.
        # ★★★ THE DRAG WAS HAND-ROLLED AND THAT IS WHY IT NEVER WORKED: my first version tracked
        #     motion-notify on the header, but the header CONTAINS BUTTONS, so presses were consumed
        #     by them, and motion events stop arriving the moment the pointer leaves the header. The
        #     operator: *"I can't drag the guide out."*
        # ★ The estate already had the right idiom in jarvis/bubble.py, which drags correctly:
        #     delegate to the WINDOW MANAGER with begin_move_drag(). One call, robust, and it is what
        #     a titlebar does. CLONED, NOT REINVENTED.
        # ★ AND IT IS BOUND TO A DEDICATED GRIP, like bubble.py's `hgrip`: a visible handle that is
        #   NOT a button, so it cannot eat a click that belonged to a control.
        try:
            self.grip = Gtk.Label(label="\u28ff")
            self.grip.set_name("pt-grip")
            self.grip.set_tooltip_text("Drag this handle to move the guide box anywhere on screen.")
            self.grip.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            self.grip.connect("button-press-event", self._card_drag_start)
            head.pack_start(self.grip, False, False, 0)
            # ★ a WM-driven move fires configure-event; that is how we LEARN he moved it by hand
            self.card.connect("configure-event", self._card_moved)
        except Exception:  # noqa: BLE001
            pass
        self.card.add(self.box)
        self.card.connect("size-allocate", lambda *_: GLib.idle_add(self._place))
        self.card.show_all()

    # ── layout: the measured floor, not the requested width ─────────────────────────────────────
    def layout_report(self) -> dict:
        """What the card and its control grid ACTUALLY require.

        ★ A GTK size request is a FLOOR. The useful question is not "what did we ask for?" but "what
          is the minimum the contents impose, and does it exceed the screen?" This returns both, so
          the suite can assert that no button and no FlowBox row can widen the operator's card.
        """
        out = {"screen": list(self.screen), "width_floor": getattr(self, "card_width_floor", None)}
        try:
            cw = self.card.get_preferred_width()
            ch = self.card.get_preferred_height()
            out["card_pref"] = {"min_w": cw[0], "nat_w": cw[1], "min_h": ch[0], "nat_h": ch[1]}
            a = self.card.get_allocated_width(), self.card.get_allocated_height()
            out["card_alloc"] = {"w": a[0], "h": a[1]}
        except Exception as e:  # noqa: BLE001
            out["card_error"] = f"{e.__class__.__name__}: {e}"
        try:
            fw = self.flow.get_preferred_width()
            fh = self.flow.get_preferred_height()
            out["flow_pref"] = {"min_w": fw[0], "nat_w": fw[1], "min_h": fh[0], "nat_h": fh[1]}
            kids = []
            for ch_ in self.flow.get_children():
                b = ch_.get_child() if hasattr(ch_, "get_child") else ch_
                label = ""
                try:
                    label = b.get_label() if hasattr(b, "get_label") else ""
                except Exception:  # noqa: BLE001
                    label = ""
                try:
                    kw = ch_.get_preferred_width()
                    kh = ch_.get_preferred_height()
                    kids.append({"label": label, "min_w": kw[0], "nat_w": kw[1],
                                 "min_h": kh[0], "nat_h": kh[1]})
                except Exception:  # noqa: BLE001
                    kids.append({"label": label, "error": "not measurable"})
            out["flow_children"] = kids
            out["widest_child"] = max([k.get("min_w", 0) for k in kids] or [0])
        except Exception as e:  # noqa: BLE001
            out["flow_error"] = f"{e.__class__.__name__}: {e}"
        return out

    # ── the step ─────────────────────────────────────────────────────────────────────────────────
    def _apply(self, i):
        self.i = max(0, min(i, len(self.steps) - 1))
        s = self.steps[self.i]
        n, tot = self.i + 1, len(self.steps)
        self.badge.set_text(f"STEP {n} OF {tot}")
        # ★ MEASURED IN THE CAPTURE: the whole walk TITLE ran across the head and was clipped
        #   ("...point-me-to-there prot"). The mode line is a provenance label, not the title; it is
        #   capped so it cannot push the close button off the card.
        mode = self.mode if len(self.mode) <= 44 else self.mode[:41] + "..."
        self.who.set_text("   " + mode)
        self.title.set_text(s.get("title") or s.get("id") or "step")
        self.body.set_text(s.get("body") or "")
        self.hint.set_text(("\u2192 " + s["hint"]) if s.get("hint") else "")
        if s.get("resolved"):
            self.err.set_text("")
        else:
            # ★★ A STEP THAT POINTS AT NOTHING IS SAID OUT LOUD, ON SCREEN, IN RED. Not skipped, not
            #    hidden behind a default rectangle -- he is told the guide could not find the thing,
            #    and given the reason.
            # ★★★ A FAILURE MUST CARRY ITS OWN WAY OUT. The operator closed the voice seat, four of
            #     five steps pointed at it, and the card said only "THIS STEP POINTS AT NOTHING" --
            #     true, and useless, because it did not tell him the one press that fixes it. He did
            #     the reasonable thing and closed the walk. A guide that reports a dead end without
            #     naming the door is a guide that looks broken.
            # ★ AND THE WAY OUT HAS TO MATCH THE CAUSE. When the panel has not reported its position,
            #   "press open it" opens the grant wizard and changes nothing about the geometry; the
            #   honest recovery is the panel reload, so `recovery` wins over the open hint.
            _msg = "\u26a0 " + str(s.get("error") or "THIS STEP POINTS AT NOTHING ON THIS SCREEN.")
            if s.get("recovery"):
                _msg += "   \u21bb " + str(s["recovery"])
            elif s.get("open"):
                _msg += "   \u25b6  PRESS 'open it' TO BRING IT BACK."
            self.err.set_text(_msg)
        self.back_btn.set_sensitive(self.i > 0)
        self.next_btn.set_label("next \u25b6" if self.i < tot - 1 else "finish \u2713")
        # ★★★ "I STILL SEE 'recorded' BUT WE NEED TO HAVE THAT REFRESH TO right spot AND THE SAME NEEDS
        #     TO BE DONE FOR THE WRONG SPOT BUTTON - NEEDS TO REFRESH EACH STEP." -- exactly right, and
        #     this was a real defect: the verdict labels were written once and NEVER reset, so step 3
        #     still read 'recorded' from step 1 and the button was telling him about a DIFFERENT step.
        #     A status that belongs to the previous step is worse than no status.
        try:
            self.yes_btn.set_label("\u2713 right spot")
            self.no_btn.set_label("\u2717 wrong spot")
            self.read_btn.set_label("\U0001f50a read")
            self.search_btn.set_label("\U0001f50d search for it")
            self.search_btn.get_style_context().remove_class("pt-go")
            # ★ THE DIAGRAM RESETS PER STEP. A toggle left off on step 2 must not hide the diagram
            #   on step 3, where it may be the instruction; and a step without one must not draw the
            #   previous step's diagram.
            self.illus_on = True
            _has_ill = bool(s.get("illustration"))
            self.illus_btn.set_visible(_has_ill)
            self.illus_btn.set_sensitive(_has_ill)
            try:
                self.plane.queue_draw()
            except Exception:
                pass
            # ★ the step's target identity, stated on the card every step. A state line that says
            #   "following" while the step is unresolved is worse than no state line: it is the
            #   instrument lying about the exact thing the operator is checking.
            _t = s.get("target") or {}
            _astate = str(s.get("anchor_state") or "")
            if not s.get("resolved"):
                if _astate == "missing":
                    self._set_state("\u26a0 waiting for the panel to report its position \u2014 "
                                    "hard-reload the panel page (Ctrl+Shift+R)", "warn")
                elif _astate == "stale":
                    self._set_state("\u26a0 panel position was last reported " +
                                    _format_age(s.get("anchor_age")) +
                                    " \u2014 it may have moved; hard-reload the panel page", "warn")
                else:
                    self._set_state("\u26a0 this step points at nothing \u2014 it cannot follow a window",
                                    "warn")
            elif _astate == "stale":
                self._set_state("\u26a0 following " + str(s.get("anchor")) + " \u2014 last panel report was " +
                                _format_age(s.get("anchor_age")) + "; it may have moved", "warn")
            elif isinstance(_t, dict) and _t.get("window"):
                self._set_state("\u25ce following the target window", "ok")
            elif s.get("anchor") and isinstance(_t, dict) and _t.get("rect"):
                if s.get("track", True):
                    self._set_state("\u25ce following " + str(s.get("anchor")) + " (window-anchored)", "ok")
                else:
                    # ★ TRACKING OFF IS NOT THE SAME AS UNANCHORED. The rect was resolved against the
                    #   anchor at plan time; it is anchored, it just will not follow a move by itself.
                    #   Calling it "screen-absolute" was false and sent the operator looking for a
                    #   window that the step had in fact named.
                    self._set_state("\u25ce anchored to " + str(s.get("anchor")) +
                                    " (tracking off \u2014 press \u2717 wrong spot to re-aim)", "ok")
            else:
                self._set_state("\u26a0 screen-absolute step \u2014 it cannot follow a window by itself", "warn")
            try:
                self.mirror.set_text("")
            except Exception:
                pass
            try:
                if getattr(self, "agent_win", None) is not None:
                    self._agent_fill_step()
            except Exception:
                pass
        except Exception:  # noqa: BLE001
            pass
        # ★ and if voice assist is on, read the new step -- that is what ON means
        if getattr(self, "voice_auto", False):
            try:
                GLib.timeout_add(400, lambda: (self._speak(self._card_text()), False)[1])
            except Exception:  # noqa: BLE001
                pass
        # ★★★ STEP-DECLARED ACTIONS -- the general form of "buttons for pkill etc... other actions".
        #     A walk step may carry `actions:` in its own file, and each becomes a button here.
        # ★★ EVERY ACTION CONFIRMS FIRST, AND THAT IS NOT FRICTION FOR ITS OWN SAKE. A walk file is
        #    data that any agent can author; a guide that RUNS COMMANDS FROM DATA with no confirm is a
        #    remote-execution surface wearing a tutorial's clothes. One click to author, one to fire.
        try:
            for _ch in list(self.actions_box.get_children()):
                self.actions_box.remove(_ch)
            for _act in (s.get("actions") or []):
                if not isinstance(_act, dict):
                    continue
                _cmd = _act.get("cmd")
                if not _cmd:
                    continue
                _lbl = _act.get("label") or "action"
                _b = Gtk.Button(label="\u2699 " + str(_lbl))
                _b.set_name("pt-act")
                _tip = _act.get("why") or f"Runs, after a confirm: {_cmd}"
                _b.set_tooltip_text(_tip)
                _b.connect("clicked", lambda _btn, c=_cmd, l=_lbl: self._run_action(l, c))
                self.actions_box.pack_start(_b, False, False, 0)
            if (s.get("actions") or []):
                self.actions_box.show_all()
        except Exception:  # noqa: BLE001 -- a broken action row must not take the guide down
            pass
        self.box.queue_resize()
        GLib.idle_add(self._place)
        self.plane.queue_draw()
        self.record["shown"].append({"step": n, "id": s.get("id"), "resolved": s.get("resolved"),
                                     "rect": s.get("rect")})
        if not s.get("resolved"):
            self.record["unresolved_shown"].append({"step": n, "id": s.get("id"),
                                                    "error": s.get("error")})
        self._write_report()
        self._snapshot("apply")

    # ── DRAGGING THE CARD ────────────────────────────────────────────────────────────────────────
    def _card_drag_start(self, _w, ev):
        """★ ONE CALL, HANDED TO THE WINDOW MANAGER. This is the idiom jarvis/bubble.py uses and it
        works; my hand-rolled motion tracking did not, and the reason is structural -- the header holds
        buttons that consume the press, and motion stops arriving once the pointer leaves the widget."""
        if ev.button != 1:
            return False
        try:
            self.card.begin_move_drag(ev.button, int(ev.x_root), int(ev.y_root), ev.time)
        except Exception:  # noqa: BLE001
            return False
        return True

    def _card_moved(self, _w, _ev):
        """★ A WM-DRIVEN MOVE IS HOW WE LEARN HE MOVED IT. Auto-placement then stops for good: a card
        that snaps back after a deliberate move is worse than one that never moved."""
        if getattr(self, "_placing", False):
            return False
        self.card_pinned = True
        try:
            g = self.card.get_position()
            sz = self.card.get_size()
            self.card_rect = [g[0], g[1], sz[0], sz[1]]
        except Exception:  # noqa: BLE001
            pass
        return False

    def _card_drag_end(self, _w, _ev):
        return False

    def _card_drag_legacy_unused(self, _w, _ev):
        self._drag = None
        try:
            self._feedback("card_moved", rect=list(self.card_rect or []))
        except Exception:  # noqa: BLE001
            pass
        return False

    def _place(self):
        """Put the card NEXT TO the ring, never on top of it -- a card covering the thing it is
        pointing at is the tutorial arrow you cannot see past."""
        # ★ A CARD HE DRAGGED STAYS WHERE HE PUT IT. Auto-placement exists to keep the
        #   card off the ring -- but once he has moved it by hand, snapping it back makes the
        #   drag feel broken and he stops trusting it. His placement wins.
        if getattr(self, "card_pinned", False) and self.card_rect:
            return False

        s = self.steps[self.i]
        sx, sy, sw, sh = self.screen
        cw, ch = self.card.get_size()
        # ★ A CARD TALLER THAN THE WORK AREA IS A CARD WITH CONTROLS BELOW THE EDGE. We cannot shrink
        #   GTK content below its own floor without a scroller, so this records the measured fact
        #   once and the window is still clamped into view. The operator's own screen has room; a
        #   small display is told the truth instead of silently losing the footer.
        if ch > sh - 16 and not getattr(self, "_tall_warned", False):
            self._tall_warned = True
            self._feedback("card_taller_than_workarea", card_h=ch, workarea_h=sh,
                           detail="the card's contents exceed the work area; the footer may be off-screen")
        r = s.get("rect") or [sx + sw // 2 - 1, sy + sh // 2 - 1, 2, 2]
        rx, ry, rw, rh = r
        gap = 18
        if ry + rh / 2 > sy + sh / 2:            # target low -> card above it
            cy = ry - ch - gap
            if cy < sy + 8:
                cy = min(ry + rh + gap, sy + sh - ch - 8)
        else:                                    # target high -> card below it
            cy = ry + rh + gap
            if cy + ch > sy + sh - 8:
                cy = max(sy + 8, ry - ch - gap)
        cx = rx + rw // 2 - cw // 2
        cx = max(sx + 8, min(cx, sx + sw - cw - 8))
        cy = max(sy + 8, min(cy, sy + sh - ch - 8))

        # ★★★ IT PLACES ONCE AND THEN STAYS. "WHEN I CLOSED THE VOICE ASSISTANT AND TRIED TO REOPEN
        #     IT, THE GUIDE JUMPED TO WHERE THE VOICE ASSISTANT WAS." -- because this ran on EVERY
        #     step and re-centred the card beside the ring, so the card followed the target around the
        #     screen. The code was doing exactly what it says ("put the card next to the ring") and
        #     that was the wrong rule: a guide that MOVES while you are reading it is a guide you lose.
        # ★ The rule it should have been all along: place once, then move ONLY if the card would
        #   actually cover the thing it is pointing at. That is what the rule was FOR.
        cur = self.card_rect
        if getattr(self, "_placed_once", False) and cur:
            covers = not (cur[0] + cur[2] < rx or rx + rw < cur[0]
                          or cur[1] + cur[3] < ry or ry + rh < cur[1])
            if not covers:
                return False                 # it is not in the way; leave it where he can see it
        self._placing = True
        self.card.move(cx, cy)
        self._placing = False
        self.card_rect = [cx, cy, cw, ch]
        self._placed_once = True
        self.plane.queue_draw()
        return False

    # ── ILLUSTRATION: the agent's diagram, in the BoardWalk palette ──────────────────────────────
    #   ★ The spec is normalized by point_at_core (closed vocabulary, capped text, clamped coords);
    #     this side only draws. Coordinates are relative to the step's target by default.
    def _illus_rgba(self, name, alpha=0.95):
        raw = str(name or "")
        h = raw if re.fullmatch(r"#[0-9a-fA-F]{6}", raw) else ILLUS_PALETTE.get(
            raw, ILLUS_PALETTE.get("blue", "#7cc4ff"))
        try:
            h = h.lstrip("#")
            return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
                    int(h[4:6], 16) / 255.0, alpha)
        except Exception:  # noqa: BLE001
            return (0.486, 0.769, 1.0, alpha)

    def _illus_text(self, cr, text, x, y, size, rgba):
        pad = 6
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        cr.set_font_size(float(size))
        ext = cr.text_extents(text)
        w = ext.width + 2 * pad
        h = ext.height + 2 * pad - 2
        cr.set_source_rgba(0.078, 0.086, 0.102, 0.92)     # the card's ink
        cr.rectangle(x, y, w, h); cr.fill()
        cr.set_source_rgba(*rgba); cr.set_line_width(1.2)
        cr.rectangle(x, y, w, h); cr.stroke()
        cr.set_source_rgba(rgba[0], rgba[1], rgba[2], 1.0)
        cr.move_to(x + pad, y + pad + ext.height - 1)
        cr.show_text(text)

    def _illus_arrow(self, cr, fx, fy, tx, ty, rgba, width=3):
        cr.set_source_rgba(*rgba); cr.set_line_width(float(width))
        cr.move_to(fx, fy); cr.line_to(tx, ty); cr.stroke()
        ang = math.atan2(ty - fy, tx - fx)
        head = 12.0
        cr.move_to(tx, ty)
        cr.line_to(tx - head * math.cos(ang - 0.45), ty - head * math.sin(ang - 0.45))
        cr.line_to(tx - head * math.cos(ang + 0.45), ty - head * math.sin(ang + 0.45))
        cr.close_path(); cr.fill()

    def _snapshot(self, why="state"):
        """Write the REAL widget text to a JSON file, only when a test asks for it.

        ★ WHY THIS EXISTS. AT-SPI can DRIVE the card (it does: the suite clicks the real buttons),
          but reading the whole accessibility tree back is slow enough that a multi-assertion run
          spends minutes in the bridge. This reads `get_text()`/`get_label()` from the live widgets
          after the handler has run, so the assertion is still about the painted label and not about
          the string someone passed in. No env var = no file, no work, no behaviour change.
        """
        path = os.environ.get("KHAOSS_POINT_TO_SNAPSHOT")
        if not path:
            return
        try:
            s = self.steps[self.i] if self.steps else {}
            d = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "why": why,
                 "i": self.i, "n_steps": len(self.steps), "mode": str(self.mode),
                 "step": s.get("id"), "resolved": bool(s.get("resolved")),
                 "rect": s.get("rect"), "anchor_state": s.get("anchor_state"),
                 "state": self.state_lbl.get_text(), "mirror": self.mirror.get_text(),
                 # ★ WHETHER THE GUIDE WAS DISMISSED IS A FACT, NOT AN IMPLICATION.
                 "dismissed": getattr(self, "_leave_dismissed", None),
                 "err": self.err.get_text(),
                 "yes": self.yes_btn.get_label(), "no": self.no_btn.get_label(),
                 "search": self.search_btn.get_label(),
                 "card": list(self.card.get_size()), "screen": list(self.screen)}
            # ★ THE BUTTON SET, BY LABEL, ON THE REAL WIDGET TREE. A suite that asserts a control
            #   EXISTS has been fooled before; this lets it assert the exact multiset and catch a
            #   control that vanished while another took its place.
            def _btns(w):
                out = []
                try:
                    if isinstance(w, Gtk.Button):
                        out.append(w.get_label())
                    for c in w.get_children():
                        out.extend(_btns(c))
                except Exception:  # noqa: BLE001
                    pass
                return out
            try:
                d["buttons"] = sorted(_btns(self.box))
            except Exception:  # noqa: BLE001
                d["buttons"] = []
            try:
                lr = self.layout_report()
                d["layout"] = {"card_pref": lr.get("card_pref"),
                               "flow_pref": lr.get("flow_pref"),
                               "widest_child": lr.get("widest_child")}
            except Exception:  # noqa: BLE001
                d["layout"] = {}
            if getattr(self, "agent_win", None) is not None:
                d["agent"] = {
                    "open": True,
                    "head": self.agent_head.get_text(),
                    "explain": self.agent_explain.get_text(),
                    "ask_state": self.agent_ask_state.get_text(),
                    "answer": self.agent_answer.get_text(),
                    "diag_state": self.agent_diag_state.get_text(),
                    "diag_answer": self.agent_diag_answer.get_text(),
                    "working": self.agent_working.get_text(),
                    "chat": self.agent_chat.get_text(),
                    "scan": self.agent_scan_lbl.get_text(),
                    "selected": self.agent_combo.get_active_id() or "",
                    "n_rows": len(getattr(self, "_agent_rows", {}) or {}),
                    "tabs": [self.agent_nb.get_tab_label_text(self.agent_nb.get_nth_page(i))
                             for i in range(self.agent_nb.get_n_pages())]
                            if getattr(self, "agent_nb", None) is not None else [],
                }
            else:
                d["agent"] = {"open": False}
            p = Path(path)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, p)
        except Exception:  # noqa: BLE001
            pass

    def _set_state(self, text, tone="ok"):
        # ★ A 700 ms tracker that re-sets the same label every tick is GTK churn and, worse, it
        #   overwrites a verdict message with a stale one. Only a CHANGE reaches the widget.
        text = str(text)
        if getattr(self, "_state_text", None) == text and getattr(self, "_state_tone", None) == tone:
            return
        self._state_text, self._state_tone = text, tone
        try:
            self.state_lbl.set_text(text)
            ctx = self.state_lbl.get_style_context()
            ctx.remove_class("state-ok"); ctx.remove_class("state-warn")
            ctx.add_class("state-ok" if tone == "ok" else "state-warn")
        except Exception:  # noqa: BLE001
            pass

    def _track_targets(self):
        """★ THE TARGET HAS AN IDENTITY, SO IT CAN FOLLOW THE WINDOW.
        A 700 ms geometry poll for steps that name a window/anchor. OCR is deliberately NOT here
        (seconds per capture); it runs on "wrong spot" and search, where the operator asked.

        ★★ IT ALSO HEALS THE FIRST-RUN CASE. The overlay can be launched before the panel page has
        reported its position (the operator's browser has not reloaded yet). That is not a dead end:
        the step starts unresolved and this loop resolves it the moment the report arrives, with the
        card clearing its error and saying it re-aimed. No restart, no second walk.
        """
        try:
            s = self.steps[self.i]
        except Exception:  # noqa: BLE001
            return True
        if not s.get("track", True):
            return True
        t = s.get("target") or {}
        anchor = s.get("anchor") or ""
        new = None
        note = ""
        tone = "ok"
        was_state = str(s.get("anchor_state") or "")
        try:
            if isinstance(t, dict) and t.get("window"):
                r = resolve_target(t)
                if r.get("ok") and r.get("rect"):
                    new = list(r["rect"])
                elif s.get("rect"):
                    self._set_state("\u26a0 cannot see " + str(t.get("window")) +
                                    " right now \u2014 last known ring kept; press \u2934 front or \u2316 jump",
                                    "warn")
                    return True
            elif anchor and isinstance(t, dict) and t.get("rect"):
                if _is_panel_anchor(anchor):
                    info = _panel_geometry_info()
                    if not info.get("ok"):
                        s["anchor_state"] = "missing"
                        if s.get("resolved"):
                            self._set_state("\u26a0 the panel stopped reporting its position \u2014 "
                                            "last known ring kept; hard-reload the panel page", "warn")
                        else:
                            self._set_state("\u26a0 waiting for the panel to report its position \u2014 "
                                            "hard-reload the panel page (Ctrl+Shift+R)", "warn")
                        return True
                    rel = t["rect"]
                    new = [info["rect"][0] + rel[0], info["rect"][1] + rel[1], rel[2], rel[3]]
                    if info.get("stale"):
                        tone = "warn"
                        note = ("\u26a0 following " + str(anchor) + " \u2014 last panel report was " +
                                _format_age(info.get("age")) + "; it may have moved")
                    s["anchor_state"] = "stale" if info.get("stale") else "fresh"
                    s["anchor_age"] = info.get("age")
                else:
                    w = resolve_target({"window": anchor})
                    if w.get("ok") and w.get("rect"):
                        rel = t["rect"]
                        new = [w["rect"][0] + rel[0], w["rect"][1] + rel[1], rel[2], rel[3]]
        except Exception:  # noqa: BLE001
            new = None
        recovered = bool(new) and not s.get("resolved")
        if new and (new != s.get("rect") or recovered):
            s["_prev_rect"] = s.get("rect")
            s["rect"] = list(new)
            s["resolved"] = True
            s["error"] = ""
            s["recovery"] = ""
            s["_moved_at"] = time.time()
            # ★ CLEAR THE RED LINE BEFORE THE SNAPSHOT. The card's error label and the state line
            #   are the two facts a recovering step changes; leaving the error painted while the
            #   state says "re-aimed" is the instrument disagreeing with itself for one repaint.
            try:
                self.err.set_text("")
            except Exception:  # noqa: BLE001
                pass
            if not note:
                if recovered:
                    note = "\u25ce the panel reported its position \u2014 re-aimed"
                    if s.get("_prev_rect") == new:
                        note += " (still the same spot)"
                elif isinstance(t, dict) and t.get("window"):
                    note = "\u25ce target moved \u2014 re-aimed (window)"
                else:
                    note = "\u25ce target moved \u2014 re-aimed (anchor " + str(anchor) + ")"
            self._set_state(note, tone)
            self._feedback("target_track", step=s.get("id"), before=s.get("_prev_rect"),
                           after=list(new), recovered=recovered,
                           anchor=str(anchor) if anchor else "",
                           stale=bool(s.get("anchor_state") == "stale"))
            self._snapshot("track")
            try:
                self.err.set_text("")
                self.plane.queue_draw()
                self._fit_plane()
                GLib.idle_add(self._place)
            except Exception:  # noqa: BLE001
                pass
        elif note:
            # no geometry change, but the report is stale: say it, and do not touch the label a
            # verdict may have written.
            self._set_state(note, tone)
        elif was_state in ("stale", "missing") and s.get("anchor_state") == "fresh":
            # ★ THE REPORT CAME BACK AND THE RECT DID NOT MOVE. Without this branch the card would
            #   keep warning "last report was 60 min ago" forever after the page started posting
            #   again, because nothing changed the label when nothing moved.
            self._set_state("\u25ce following " + str(anchor) + " (window-anchored)", "ok")
            self._feedback("panel_geometry_fresh", step=s.get("id"),
                           age=s.get("anchor_age"), was=was_state)
            self._snapshot("track_fresh")
        return True

    def _reaim(self, s):
        """The ladder: window -> window-relative rect -> OCR phrase -> title search.
        It returns the method that actually moved the ring, or "" so the card can say it could not.
        ★ `_reaim_error` carries WHY when nothing moved, so the card never says "no window/text
          found" about a panel that simply has not reported its position yet."""
        t = s.get("target") or {}
        anchor = s.get("anchor") or ""
        s["_reaim_error"] = ""
        try:
            if isinstance(t, dict) and t.get("window"):
                r = resolve_target(t)
                if r.get("ok") and r.get("rect"):
                    s["rect"] = list(r["rect"]); return "window"
                if _is_panel_anchor(t.get("window")):
                    s["_reaim_error"] = str(r.get("error") or "the panel has not reported its position")
                    # ★ STILL TRY THE LOWER RUNGS. Waiting is the primary recovery, but an operator
                    #   who presses "wrong spot" has asked us to LOOK; OCR/title may find the content
                    #   even while the panel's own report is missing. The ladder reports which rung
                    #   answered, and the card never pretends the anchor was used.
            if anchor and isinstance(t, dict) and t.get("rect"):
                if _is_panel_anchor(anchor):
                    info = _panel_geometry_info()
                    if info.get("ok"):
                        rel = t["rect"]
                        s["rect"] = [info["rect"][0] + rel[0], info["rect"][1] + rel[1],
                                     rel[2], rel[3]]
                        s["anchor_state"] = "stale" if info.get("stale") else "fresh"
                        s["anchor_age"] = info.get("age")
                        return "anchor window " + anchor
                    s["_reaim_error"] = str(info.get("error") or "the panel has not reported its position")
                else:
                    w = resolve_target({"window": anchor})
                    if w.get("ok") and w.get("rect"):
                        rel = t["rect"]
                        s["rect"] = [w["rect"][0] + rel[0], w["rect"][1] + rel[1], rel[2], rel[3]]
                        return "anchor window " + anchor
                    s["_reaim_error"] = str(w.get("error") or "the anchor window was not found")
        except Exception as e:  # noqa: BLE001
            s["_reaim_error"] = f"{e.__class__.__name__}: {e}"
        phrase = str(s.get("anchor_ocr") or s.get("find") or "").strip()
        if phrase:
            try:
                import point_at_core as _core
                rr = _core.resolve(text=phrase,
                                   window=(None if _is_panel_anchor(anchor) else (anchor or None)))
                if rr.get("ok") and rr.get("rect"):
                    s["rect"] = list(rr["rect"]); return "OCR " + phrase[:32]
                s["_reaim_error"] = str(rr.get("error") or "OCR found no matching words")
            except Exception as e:  # noqa: BLE001
                s["_reaim_error"] = f"OCR unavailable: {e.__class__.__name__}"
        try:
            title = self._search_targets(s)
            if title:
                return "title " + title
        except Exception as e:  # noqa: BLE001
            s["_reaim_error"] = f"title search failed: {e.__class__.__name__}"
        return ""

    def _search_targets(self, s):
        import re as _re
        q = self._search_query(s)
        toks = [t for t in _re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 2]
        wins = self._windows()
        scored = []
        for wid, title, rect in wins:
            tl = title.lower()
            hits = sum(1 for t in toks if t in tl)
            if hits:
                scored.append((hits, hits / max(1, len(toks)), wid, title, rect))
        scored.sort(key=lambda x: (-x[0], -(x[4][2] * x[4][3])))
        if not scored:
            return ""
        best = scored[0]
        s["rect"] = list(best[4]); s["_search_title"] = best[3]
        return str(best[3])[:48]

    def _deliver_verdict(self, s, ok, how):
        """Forward the verdict to the selected guide agent through the ONE existing delivery path.
        ★ The live DSH chat is link-only (its lock is held); notify() returns 409 and the mirror says
          NOT delivered with the reason. Nothing is queued in that case."""
        try:
            import guide_backends as _gb
            text = (f"{'RIGHT' if ok else 'WRONG'} spot on step {self.i + 1} ({s.get('id')}). "
                    f"re-aim: {how or 'no new aim'}. The operator is looking at the real screen; "
                    f"re-check this step against the actual window.")
            r = _gb.notify({"source_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "step": s.get("step") or self.i + 1, "target": s.get("id") or "",
                            "kind": "verdict", "urgency": "none" if ok else "now",
                            "text": text, "operator_words": True})
            st = str(r.get("status") or ("queued" if r.get("ok") else "not delivered"))
            label = str((r.get("backend") or {}).get("label") or "")
            if r.get("ok"):
                self.mirror.set_text(f"→ {st} · {label}" + (f" · {r.get('event_id')}" if r.get("event_id") else ""))
            else:
                self.mirror.set_text(f"→ NOT delivered · {label} · {r.get('error') or r.get('state_detail') or ''}")
            self._feedback("verdict_delivery", step=s.get("id"), status=st, ok=bool(r.get("ok")),
                           event_id=r.get("event_id"), error=r.get("error"))
        except Exception as e:  # noqa: BLE001
            try:
                self.mirror.set_text(f"→ NOT delivered · {e.__class__.__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass

    def bring_to_front(self):
        """Raise the X11 target if it is one; ask the panel page to focus/scroll/flash the item.
        * Never claims the Wayland compositor obeys: the page's toast names that limit.
        * ★ A KHAOSS ANCHOR IS NOT AN X11 WINDOW. Searching X for a window titled "KHAOSS" returned
          "xdotool found NO window matching 'KHAOSS'", which reads like a lost panel; the panel was
          exactly where it always was and X simply cannot see it. The honest note says so.
        """
        try:
            s = self.steps[self.i]
        except Exception:  # noqa: BLE001
            return False
        notes = []
        _t = s.get("target") if isinstance(s.get("target"), dict) else {}
        _w = str((_t or {}).get("window") or s.get("anchor") or "")
        _panel_step = _is_panel_anchor(_w)
        if _panel_step:
            notes.append("the panel is Wayland-native \u2014 X11 cannot raise it")
        elif _t.get("window"):
            try:
                note = self._raise_target(s)
                if note:
                    notes.append(str(note))
            except Exception as e:  # noqa: BLE001
                notes.append("raise failed: " + e.__class__.__name__)
        elif s.get("anchor"):
            try:
                note = self._raise_target(s)
                if note and not str(note).startswith("raise: this step names no window"):
                    notes.append(str(note))
            except Exception as e:  # noqa: BLE001
                notes.append("raise failed: " + e.__class__.__name__)
        else:
            notes.append("this step names no window")
        base = (os.environ.get("KHAOSS_PANEL_URL") or "http://127.0.0.1:7842").rstrip("/")
        try:
            import urllib.request as _u
            body = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "step": self.i + 1,
                               "target": s.get("id") or "", "title": s.get("title") or "",
                               "find": s.get("find") or "", "selector": "#bd-walk"}).encode()
            req = _u.Request(base + "/operator/panel/front", data=body,
                             headers={"Content-Type": "application/json"}, method="POST")
            with _u.urlopen(req, timeout=3) as r:
                d = json.loads(r.read())
            if d.get("ok"):
                notes.append("asked the panel page to focus/scroll/flash the item")
            else:
                notes.append("panel front refused: " + str(d.get("error")))
        except Exception as e:  # noqa: BLE001
            notes.append("panel front failed: " + e.__class__.__name__)
        self._set_state("\u25ce " + (" \u00b7 ".join(notes) if notes else "nothing to bring forward"),
                        "ok" if notes else "warn")
        self._feedback("bring_front", step=s.get("id"), notes=notes,
                       panel=bool(_panel_step), target_window=_w)
        self._snapshot("front")
        return False

    # ── the per-step agent panel: Explain / Ask / Diagram / Agent ────────────────────────────────
    def _agent_mod(self):
        try:
            import guide_backends as gb
            return gb
        except Exception:  # noqa: BLE001
            root = Path(__file__).resolve().parent.parent
            sys.path.insert(0, str(root / "operator"))
            import guide_backends as gb
            return gb

    def _agent_event_answer(self, evid: str, timeout: float = 30.0) -> str:
        """Wait for the worker's written answer. The event is the ONE delivery record; no second store."""
        gb = self._agent_mod()
        end = time.time() + timeout
        last = None
        while time.time() < end:
            ev = gb.event(evid) or {}
            last = ev
            ans = (ev.get("answer") or ev.get("reply") or ev.get("detail") or "")
            if ev.get("status") in ("completed", "failed") and ans:
                return str(ans)
            time.sleep(0.4)
        return str((last or {}).get("detail") or "") or "queued; no answer yet"

    def open_agent_panel(self):
        """A separate window, so the card's 430px never has to carry four tabs."""
        if getattr(self, "agent_win", None) is not None:
            try:
                self.agent_win.present(); return False
            except Exception:  # noqa: BLE001
                pass
        w = Gtk.Window(title="point-me-to-there agent")
        w.set_default_size(540, 600)
        w.set_keep_above(True)
        w.set_skip_taskbar_hint(True)
        w.set_transient_for(self.card)
        w.set_position(Gtk.WindowPosition.CENTER)
        w.connect("destroy", lambda *_: setattr(self, "agent_win", None))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(10)
        self.agent_head = Gtk.Label()
        self.agent_head.set_xalign(0)
        box.pack_start(self.agent_head, False, False, 0)
        nb = Gtk.Notebook()
        self.agent_nb = nb
        box.pack_start(nb, True, True, 0)

        # Explain
        ex_sc = Gtk.ScrolledWindow(); ex_sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.agent_explain = Gtk.Label(); self.agent_explain.set_xalign(0); self.agent_explain.set_line_wrap(True)
        self.agent_explain.set_selectable(True)
        ex_sc.add(self.agent_explain); nb.append_page(ex_sc, Gtk.Label(label="Explain"))

        # Ask
        ask = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); ask.set_border_width(8)
        self.agent_ask_entry = Gtk.Entry(); self.agent_ask_entry.set_placeholder_text("Ask the selected agent about this step...")
        self.agent_ask_btn = Gtk.Button(label="ask")
        self.agent_ask_btn.connect("clicked", lambda *_: self._agent_ask())
        arow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        arow.pack_start(self.agent_ask_entry, True, True, 0); arow.pack_start(self.agent_ask_btn, False, False, 0)
        self.agent_ask_state = Gtk.Label(); self.agent_ask_state.set_xalign(0); self.agent_ask_state.set_line_wrap(True)
        ans_sc = Gtk.ScrolledWindow(); ans_sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.agent_answer = Gtk.Label(); self.agent_answer.set_xalign(0); self.agent_answer.set_line_wrap(True)
        self.agent_answer.set_selectable(True); ans_sc.add(self.agent_answer)
        ask.pack_start(arow, False, False, 0); ask.pack_start(self.agent_ask_state, False, False, 0)
        ask.pack_start(ans_sc, True, True, 0)
        nb.append_page(ask, Gtk.Label(label="Ask"))

        # Diagram
        dia = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); dia.set_border_width(8)
        self.agent_diag_btn = Gtk.Button(label="ask the agent for a diagram of this step")
        self.agent_diag_btn.connect("clicked", lambda *_: self._agent_diagram())
        self.agent_diag_state = Gtk.Label(); self.agent_diag_state.set_xalign(0); self.agent_diag_state.set_line_wrap(True)
        self.agent_diag_answer = Gtk.Label(); self.agent_diag_answer.set_xalign(0); self.agent_diag_answer.set_line_wrap(True)
        self.agent_diag_answer.set_selectable(True)
        dia.pack_start(self.agent_diag_btn, False, False, 0); dia.pack_start(self.agent_diag_state, False, False, 0)
        dia.pack_start(self.agent_diag_answer, True, True, 0)
        nb.append_page(dia, Gtk.Label(label="Diagram"))

        # Agent
        ag = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6); ag.set_border_width(8)
        self.agent_working = Gtk.Label(); self.agent_working.set_xalign(0); self.agent_working.set_line_wrap(True)
        self.agent_chat = Gtk.Label(); self.agent_chat.set_xalign(0); self.agent_chat.set_line_wrap(True)
        self.agent_scan_lbl = Gtk.Label(); self.agent_scan_lbl.set_xalign(0); self.agent_scan_lbl.set_line_wrap(True)
        self.agent_combo = Gtk.ComboBoxText()
        self.agent_use_btn = Gtk.Button(label="use this backend")
        self.agent_use_btn.connect("clicked", lambda *_: self._agent_select())
        self.agent_open_btn = Gtk.Button(label="open this chat in DSH")
        self.agent_open_btn.connect("clicked", lambda *_: self._agent_open_chat())
        self.agent_copy_btn = Gtk.Button(label="copy session id")
        self.agent_copy_btn.connect("clicked", lambda *_: self._agent_copy_id())
        arow2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        arow2.pack_start(self.agent_combo, True, True, 0); arow2.pack_start(self.agent_use_btn, False, False, 0)
        arow3 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        arow3.pack_start(self.agent_open_btn, False, False, 0); arow3.pack_start(self.agent_copy_btn, False, False, 0)
        for _w in (self.agent_working, self.agent_chat, self.agent_scan_lbl, arow2, arow3):
            ag.pack_start(_w, False, False, 0)
        nb.append_page(ag, Gtk.Label(label="Agent"))

        w.add(box)
        w.show_all()
        self.agent_win = w
        self._agent_fill_step()
        self._agent_scan()
        return False

    def _agent_fill_step(self):
        try:
            s = self.steps[self.i]
            body = s.get("body") or ""
            hint = s.get("hint") or ""
            why = s.get("why_it_matters") or ""
            risk = s.get("what_can_go_wrong") or ""
            self.agent_head.set_markup(f"<b>Step {self.i + 1} of {len(self.steps)}</b> \u00b7 "
                                       f"{GLib.markup_escape_text(s.get('title') or '')}")
            parts = [body]
            if hint: parts.append("\nWhy now: " + hint)
            if why: parts.append("\nWhy it matters: " + why)
            if risk: parts.append("\nWhat can go wrong: " + risk)
            if not why and not risk:
                parts.append("\nNo pre-written deeper explanation is attached to this step. "
                             "Use Ask to have the working agent explain it, or Diagram to ask for a drawing.")
            self.agent_explain.set_text("\n".join(parts))
            self.agent_diag_state.set_text(
                "This step has an agent-authored illustration: " + ("yes" if s.get("illustration") else "no") +
                ". The agent can draw one from its own explanation; the plane renders it.")
            if getattr(self, "agent_win", None):
                self.agent_win.present()
        except Exception:  # noqa: BLE001
            pass

    def _live_chat_row(self, groups):
        """Find the live DSH chat WITHOUT naming a session id in the source.

        ★ A SESSION ID IN THE SOURCE IS A PRIVATE FACT IN A PUBLIC FILE. This used to match the
          literal session id; that is the operator's live chat id, and copying the overlay into a
          release carried it along. The registry already carries `_chat.live` and a human label, so
          the row is discovered by the properties the UI actually needs: a live, link-only chat,
          with the BoardWalk/visual-guide title preferred.
        """
        cands = []
        for gname, rows in (groups or {}).items():
            if "chat" not in str(gname).lower():
                continue
            for r in (rows or []):
                if not isinstance(r, dict) or not r.get("id"):
                    continue
                chat = r.get("_chat") if isinstance(r.get("_chat"), dict) else {}
                title = str(r.get("label") or "")
                score = 0 if ("boardwalk" in title.lower() or "visual guide" in title.lower()) else 1
                cands.append((0 if chat.get("live") else 1, score, title, r))
        cands.sort(key=lambda x: (x[0], x[1], x[2]))
        return cands[0][3] if cands else None

    def _agent_scan(self):
        """The voice assistant project's agent scan, reused: the guide registry is the one list."""
        try:
            gb = self._agent_mod()
            inv = gb.inventory()
            groups = inv.get("groups") or {}
            sel = inv.get("selected") or {}
            self.agent_working.set_markup("<b>Working agent</b> \u00b7 " +
                                          GLib.markup_escape_text(str(sel.get("label") or sel.get("id") or "none")) +
                                          " \u00b7 " + GLib.markup_escape_text(str(sel.get("state") or "")))
            # the live DSH chat is in the chats group; name it as link-only, never as a send target
            chat = self._live_chat_row(groups)
            if chat:
                _c = chat.get("_chat") if isinstance(chat.get("_chat"), dict) else {}
                _live = bool(_c.get("live"))
                self.agent_chat.set_markup("<b>This chat</b> \u00b7 " +
                                           GLib.markup_escape_text(str(chat.get("label") or "")) +
                                           ((" \u00b7 LIVE \u2014 link-only (its lock is held); the overlay "
                                             "cannot attach or send to it while it is open.")
                                            if _live else
                                            (" \u00b7 link-only (it cannot be attached); the overlay "
                                             "cannot send to it.")))
            else:
                self.agent_chat.set_text("This chat: no live DSH chat was found in the scan.")
            # a flat, stable list for the switch; include every real backend row, MARKED with what
            # one turn through it can do. A queue-only row offered without its state is the dead
            # button this registry exists to end.
            self.agent_combo.remove_all()
            self._agent_rows = {}
            n_answer = n_other = 0
            for gname, rows in groups.items():
                for r in (rows or []):
                    if not isinstance(r, dict) or not r.get("id"):
                        continue
                    rid = str(r["id"])
                    if "chat" in str(gname).lower():
                        continue  # live chats are link-only; they are not answerer targets
                    self._agent_rows[rid] = r
                    vt = str(r.get("voice_turn") or "")
                    state = str(r.get("state") or "")
                    mark = ("answer" if vt == "answer" else
                            (vt or "queue-only") + ("/" + state if state else ""))
                    self.agent_combo.append(rid, f"{gname}: {r.get('label') or rid} [{mark}]")
                    if vt == "answer":
                        n_answer += 1
                    else:
                        n_other += 1
            if sel.get("id") in self._agent_rows:
                self.agent_combo.set_active_id(str(sel["id"]))
            _sel_state = str(sel.get("state") or "")
            _sel_caps = sel.get("capabilities") or []
            _sel_ok = bool(sel) and _sel_state != "disconnected" and _sel_caps != ["link-only"]
            # ★ THE OBJECTIVE ASKED FOR "recommended PLUS expansive options" IN BOTH SURFACES. The
            #   panel had the recommendation; this panel did not, so the operator could switch to
            #   ninety-three backends and be told nothing about which one fits THIS step. It reads the
            #   same operator/recommended_agents.py the board uses — one store, one binding table.
            _rec = ""
            try:
                import recommended_agents as _ra
                _step = (self.steps[self.i] or {}) if self.steps else {}
                _r = _ra.for_item({"id": str(_step.get("id") or ""),
                                   "title": str(_step.get("title") or "")}) or {}
                _names = [str(a.get("name")) for a in (_r.get("agents") or []) if a.get("name")]
                if _names:
                    _rec = ("\n\u2605 Recommended for this step: " + " \u00b7 ".join(_names[:2])
                            + (" \u00b7 territory: " + str(_r.get("territory")) if _r.get("territory") else ""))
                else:
                    _rec = ("\n\u2605 Recommended for this step: no board row is bound to this "
                            "walk step \u2014 the list below is the whole scan, not a shortlist.")
            except Exception as _e:  # noqa: BLE001
                _rec = "\n\u2605 recommendation unavailable: " + _e.__class__.__name__
            self.agent_scan_lbl.set_text(
                f"scan: {len(self._agent_rows)} selectable backends \u2014 {n_answer} can answer, "
                f"{n_other} queue/link only (marked in the list); live chats are link-only and are not "
                f"offered as send targets. "
                + ("The selected backend answers Ask/Diagram." if _sel_ok else
                   "The SELECTED backend cannot answer right now (" + _sel_state +
                   ("/link-only" if _sel_caps == ["link-only"] else "") +
                   ") \u2014 Ask/Diagram will say NOT delivered; choose an answerable backend.") + _rec)
            self._snapshot("agent_scan")
        except Exception as e:  # noqa: BLE001
            self.agent_scan_lbl.set_text(f"scan failed: {e.__class__.__name__}: {e}")

    def _agent_ask(self):
        q = self.agent_ask_entry.get_text().strip()
        if not q:
            self.agent_ask_state.set_text("Type a question first.")
            return
        try:
            s = self.steps[self.i]
            gb = self._agent_mod()
            r = gb.notify({"source_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "step": self.i + 1, "target": s.get("id") or "", "kind": "operator_question",
                           "urgency": "now", "operator_words": True,
                           "text": f"Overlay question for step {self.i + 1} ({s.get('id')}: {s.get('title')}). "
                                   f"The operator is on the real screen and asks: {q}\n"
                                   f"Answer in plain language, referring to what is actually on screen."})
            if not r.get("ok"):
                self.agent_ask_state.set_text("NOT delivered: " + str(r.get("error") or r.get("status")) +
                                              "  (" + str((r.get("backend") or {}).get("label") or "") + ")")
                self._snapshot("agent_ask_refused")
                return
            evid = str(r.get("event_id") or "")
            self.agent_ask_state.set_text("asked \u00b7 waiting for the worker's answer \u00b7 " + evid)
            self.agent_ask_btn.set_sensitive(False)

            def worker():
                # ★ A WORKER THAT DIES SILENTLY LEAVES THE ASK BUTTON DISABLED FOREVER. The old
                #   thread called `_agent_event_answer` with no guard, so any failure after the
                #   notify (a read error, a module import) killed it and the card waited forever.
                try:
                    ans = self._agent_event_answer(evid, timeout=45)
                except Exception as e:  # noqa: BLE001
                    ans = f"ask failed: {e.__class__.__name__}: {e}"
                GLib.idle_add(self._agent_ask_done, ans, evid)

            threading.Thread(target=worker, daemon=True, name="pt-agent-ask").start()
        except Exception as e:  # noqa: BLE001
            self.agent_ask_state.set_text(f"ask failed: {e.__class__.__name__}: {e}")
            self.agent_ask_btn.set_sensitive(True)
            self._snapshot("agent_ask_error")

    def _agent_ask_done(self, ans, evid):
        ans = str(ans or "")
        self.agent_answer.set_text(ans)
        self.agent_ask_btn.set_sensitive(True)
        if ans.startswith("ask failed"):
            self.agent_ask_state.set_text(ans)
        elif not ans.strip() or ans.startswith("queued; no answer yet"):
            # ★ "ASKED" IS NOT "ANSWERED". The event is the durable fact; if the worker has not
            #   written a reply, say exactly that and do not paint a blank panel as an answer.
            self.agent_ask_state.set_text("no answer yet \u00b7 the event is still queued; the worker "
                                          "has not written a reply \u00b7 " + str(evid))
        else:
            self.agent_ask_state.set_text("answer received \u00b7 " + str(evid))
        self._snapshot("agent_ask_done")
        return False

    def _agent_diagram(self):
        try:
            s = self.steps[self.i]
            gb = self._agent_mod()
            self.agent_diag_state.set_text("asking the selected agent for an illustration JSON...")
            r = gb.notify({"source_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "step": self.i + 1, "target": s.get("id") or "", "kind": "operator_diagram",
                           "urgency": "now", "operator_words": True,
                           "text": f"Produce an illustration for overlay step {self.i + 1} ({s.get('id')}: {s.get('title')}). "
                                   f"Return ONLY a compact JSON object the overlay can draw: "
                                   f'{{"kind":"diagram","title":"...","nodes":[{{"x":..,"y":..,"w":..,"h":..,"label":".."}}],'
                                   f'"edges":[{{"x1":..,"y1":..,"x2":..,"y2":..}}],"caption":"..."}} with coordinates in 0..100. '
                                   f"Nothing else."})
            if not r.get("ok"):
                self.agent_diag_state.set_text("NOT delivered: " + str(r.get("error") or r.get("status")) +
                                               "  (" + str((r.get("backend") or {}).get("label") or "") + ")")
                self._snapshot("agent_diagram_refused")
                return
            evid = str(r.get("event_id") or "")
            self.agent_diag_btn.set_sensitive(False)
            self.agent_diag_state.set_text("asked \u00b7 waiting for a drawable JSON answer \u00b7 " + evid)

            def worker():
                try:
                    ans = self._agent_event_answer(evid, timeout=60)
                except Exception as e:  # noqa: BLE001
                    ans = f"diagram ask failed: {e.__class__.__name__}: {e}"
                GLib.idle_add(self._agent_diagram_done, ans, evid)

            threading.Thread(target=worker, daemon=True, name="pt-agent-diagram").start()
        except Exception as e:  # noqa: BLE001
            self.agent_diag_state.set_text(f"diagram ask failed: {e.__class__.__name__}: {e}")
            self.agent_diag_btn.set_sensitive(True)
            self._snapshot("agent_diagram_error")

    def _agent_diagram_done(self, ans, evid=""):
        ans = str(ans or "")
        self.agent_diag_answer.set_text(ans)
        try:
            self.agent_diag_btn.set_sensitive(True)
        except Exception:  # noqa: BLE001
            pass
        if ans.startswith("diagram ask failed"):
            self.agent_diag_state.set_text(ans)
            self._snapshot("agent_diagram_done")
            return False
        if not ans.strip() or ans.startswith("queued; no answer yet"):
            self.agent_diag_state.set_text("no answer yet \u00b7 the event is still queued; nothing "
                                          "was applied to the plane")
            self._snapshot("agent_diagram_done")
            return False
        try:
            s = self.steps[self.i]
            import re as _re
            m = _re.search(r"\{.*\}", str(ans), _re.S)
            if m:
                spec = json.loads(m.group(0))
                if isinstance(spec, dict) and spec.get("nodes"):
                    s["illustration"] = spec
                    self.illus_on = True
                    self.plane.queue_draw()
                    self.agent_diag_state.set_text("diagram received and applied to this step's plane.")
                    self._feedback("diagram_applied", step=s.get("id"), event_id=str(evid),
                                   nodes=len(spec.get("nodes") or []))
                    self._snapshot("agent_diagram_done")
                    return False
        except Exception as e:  # noqa: BLE001
            self.agent_diag_state.set_text(f"answer received but it is not valid JSON: "
                                           f"{e.__class__.__name__}; nothing was applied.")
            self._snapshot("agent_diagram_done")
            return False
        self.agent_diag_state.set_text("answer received; no drawable JSON found, so nothing was applied.")
        self._snapshot("agent_diagram_done")
        return False

    def _agent_select(self):
        try:
            rid = self.agent_combo.get_active_id()
            if not rid: return
            gb = self._agent_mod()
            r = gb.select(rid)
            self.agent_scan_lbl.set_text(("selected " + rid + " · " + str(r.get("state") or "")) if r.get("ok")
                                         else ("select failed: " + str(r.get("error") or r.get("status"))))
            self._agent_scan()
        except Exception as e:  # noqa: BLE001
            self.agent_scan_lbl.set_text(f"select failed: {e.__class__.__name__}: {e}")

    def _agent_chat_id(self):
        try:
            gb = self._agent_mod(); inv = gb.inventory()
            chat = self._live_chat_row(inv.get("groups") or {})
            if chat:
                return str(chat.get("session_id") or chat.get("id") or "")
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _agent_copy_id(self):
        sid = self._agent_chat_id()
        try:
            Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(sid, -1)
            self.agent_scan_lbl.set_text("copied session id " + (sid[:8] + "..." if sid else "(none)"))
        except Exception:  # noqa: BLE001
            self.agent_scan_lbl.set_text("copy failed")

    def _agent_open_chat(self):
        base = (os.environ.get("KHAOSS_PANEL_URL") or "http://127.0.0.1:7842").rstrip("/")
        try:
            import urllib.request as _u
            with _u.urlopen(base + "/run/dsh-url", timeout=4) as r:
                d = json.loads(r.read())
            url = str(d.get("url") or "")
            if not url:
                self.agent_scan_lbl.set_text("Open DSH unavailable: " + str(d.get("error") or d.get("say") or "no url"))
                return
            webbrowser.open(url)
            self.agent_scan_lbl.set_text("opened the DSH GUI (token not shown here)")
        except Exception as e:  # noqa: BLE001
            self.agent_scan_lbl.set_text(f"Open DSH failed: {e.__class__.__name__}")

    def _draw_illustration(self, cr):
        if not getattr(self, "illus_on", True) or normalize_illustration is None:
            return
        try:
            s = self.steps[self.i]
        except Exception:  # noqa: BLE001
            return
        ill = normalize_illustration(s.get("illustration"))
        if not ill:
            return
        r = s.get("rect") or [0, 0, 0, 0]
        ox, oy = (r[0], r[1]) if ill.get("frame") == "target" else (0, 0)
        cr.save()
        try:
            for it in ill.get("items") or []:
                kind = it.get("kind")
                rgba = self._illus_rgba(it.get("color") or "blue")
                if kind == "label":
                    x, y = it["at"]
                    self._illus_text(cr, it["text"], ox + x, oy + y, it.get("size", 13), rgba)
                elif kind == "arrow":
                    fx, fy = it["from"]; tx, ty = it["to"]
                    self._illus_arrow(cr, ox + fx, oy + fy, ox + tx, oy + ty, rgba,
                                      it.get("width", 3))
                elif kind == "box":
                    bx, by, bw, bh = it["rect"]
                    cr.set_source_rgba(*rgba); cr.set_line_width(2)
                    cr.rectangle(ox + bx, oy + by, bw, bh); cr.stroke()
                    if it.get("text"):
                        self._illus_text(cr, it["text"], ox + bx + 8, oy + by - 24,
                                         it.get("size", 13), rgba)
                elif kind == "dot":
                    dx, dy = it["at"]; dr = it.get("r", 6)
                    cr.set_source_rgba(rgba[0], rgba[1], rgba[2], 1.0)
                    cr.arc(ox + dx, oy + dy, dr, 0, 2 * math.pi); cr.fill()
                    if it.get("text"):
                        self._illus_text(cr, it["text"], ox + dx + dr + 8, oy + dy - 10,
                                         it.get("size", 13), rgba)
        finally:
            cr.restore()

    def _on_draw(self, _w, cr):
        # ★★★ THE DISENGAGE, AND IT MUST BE REAL. He asked for *"a button disengage the screen dimming/
        #     outlining feature ... to an on/off style"* so he is never LOCKED INTO this perspective.
        #     So this returns BEFORE drawing anything: no scrim, no ring, no arrow. The CARD stays,
        #     because the card is how he turns it back on.
        if getattr(self, "overlay_hidden", False):
            return False
        s = self.steps[self.i]
        sx, sy, sw, sh = self.screen
        # ── scrim with a punched hole ────────────────────────────────────────────────────────────
        cr.set_source_rgba(0, 0, 0, AMBIENT)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.rectangle(0, 0, sw, sh)
        r = s.get("rect")
        if r:
            rx, ry, rw, rh = r
            cr.rectangle(rx - sx, ry - sy, rw, rh)
        cr.fill()
        if not r:
            # no target: still dim, and draw the "nothing to point at" cross so the plane is never
            # silently blank (a blank overlay is indistinguishable from a crashed one)
            cr.set_source_rgba(0.90, 0.28, 0.30, 0.9)
            cr.set_line_width(4)
            cx, cy = sw / 2, sh / 2
            cr.move_to(cx - 30, cy - 30); cr.line_to(cx + 30, cy + 30)
            cr.move_to(cx + 30, cy - 30); cr.line_to(cx - 30, cy + 30)
            cr.stroke()
            return False
        rx, ry, rw, rh = r
        x, y = rx - sx, ry - sy
        # ── ring: a soft halo plus a hard 3px edge, so it reads on light AND dark backgrounds ────
        rr, _off = ring_rect(r, self.screen)
        gx0, gy0 = rr[0] - sx, rr[1] - sy
        gw, gh = rr[2], rr[3]
        cr.set_line_width(14)
        cr.set_source_rgba(0.486, 0.769, 1.0, 0.30)
        cr.rectangle(gx0, gy0, gw, gh)
        cr.stroke()
        cr.set_line_width(3)
        cr.set_source_rgba(0.486, 0.769, 1.0, 1.0)
        cr.rectangle(gx0, gy0, gw, gh)
        cr.stroke()
        # corner ticks -- a game-tutorial tell that this is an overlay and not a window border.
        # ★ CAPPED AT 40px. MEASURED: the tick length was 22% of the SHORTER side, which on the
        #   1089-tall agent-browser window was 239px -- the ticks painted over most of the ring's own
        #   edges, so the mask of "blue ring pixels" came back missing whole sides. A corner tick that
        #   eats the edge it decorates is not a tick.
        cr.set_line_width(5)
        cr.set_source_rgba(0.847, 0.471, 0.282, 1.0)
        t = max(14, min(40, min(rw, rh) * 0.22))
        corners = ((gx0, gy0, 1, 1), (gx0 + gw, gy0, -1, 1),
                   (gx0, gy0 + gh, 1, -1), (gx0 + gw, gy0 + gh, -1, -1))
        for cxx, cyy, dx, dy in corners:
            cr.move_to(cxx, cyy + dy * t); cr.line_to(cxx, cyy); cr.line_to(cxx + dx * t, cyy)
        cr.stroke()
        # ── arrow from the card to the ring ──────────────────────────────────────────────────────
        crect = getattr(self, "card_rect", None)
        if crect:
            ccx, ccy = crect[0] + crect[2] / 2, crect[1] + crect[3] / 2
            tcx, tcy = x + rw / 2, y + rh / 2
            vx, vy = tcx - ccx, tcy - ccy
            ln = (vx * vx + vy * vy) ** 0.5 or 1.0
            ux, uy = vx / ln, vy / ln
            # stop the shaft where the ring begins
            # ★★★ `pad` WAS NEVER DEFINED ANYWHERE IN THIS FILE -- it survived only in a comment and
            #     in this line, a leftover from the refactor that moved the padding INTO `ring_rect()`.
            #     Every `--guide` run died here with `NameError: name 'pad' is not defined`, so the walk
            #     DREW ITS WINDOWS AND THEN COULD NOT DRAW THE ARROW. The sub-agent's suite passed because
            #     its self-test exercised `--script`, never `_on_draw` in this path.
            # ★ The fix uses the RING's own half-extent (gw/gh, already computed from ring_rect above),
            #   not the raw target -- so the arrow stops at the ring the user can actually see, and
            #   there is still exactly ONE padding authority in this file.
            stop = max(0.0, ln - max(gw, gh) / 2 - 6)
            ex, ey = ccx + ux * stop, ccy + uy * stop
            cr.set_source_rgba(0.847, 0.471, 0.282, 0.95)
            cr.set_line_width(3)
            cr.move_to(ccx, ccy); cr.line_to(ex, ey); cr.stroke()
            ah = 15
            cr.move_to(ex, ey)
            cr.line_to(ex - ux * ah - uy * ah * 0.5, ey - uy * ah + ux * ah * 0.5)
            cr.line_to(ex - ux * ah + uy * ah * 0.5, ey - uy * ah - ux * ah * 0.5)
            cr.close_path(); cr.fill()
        # ★ THE DIAGRAM, AFTER THE RING AND ARROW. A bad illustration must not kill the overlay:
        #   normalize() has already dropped the invalid parts; anything left that fails to draw is
        #   recorded and the step is still usable.
        try:
            self._draw_illustration(cr)
        except Exception as e:  # noqa: BLE001
            self.record.setdefault("illustration_errors", []).append(
                f"{e.__class__.__name__}: {e}")
        return False

    def _fit_plane(self):
        """Re-assert the plane's geometry AFTER it is mapped. The WM, not GTK, owns the final say on a
        mapped window, so the size is confirmed against the screen rather than assumed from the call
        that requested it."""
        sx, sy, sw, sh = self.screen
        try:
            self.plane.resize(sw, sh)
            self.plane.move(sx, sy)
        except Exception:
            pass
        return False

    # ── THE CONTROLS THE OPERATOR ASKED FOR ─────────────────────────────────────────────────────
    def toggle_illustration(self):
        """Show/hide the agent's diagram on the plane. Reversible; the step does not move."""
        self.illus_on = not getattr(self, "illus_on", True)
        try:
            self.plane.queue_draw()
        except Exception:  # noqa: BLE001
            pass
        self._feedback("illustration", on=bool(self.illus_on))
        return False

    def toggle_dim(self):
        """★ DISENGAGE THE DIMMING AND THE OUTLINE. On/off, one button, reversible -- so he is never
        LOCKED INTO this perspective. The card stays visible because the card is how he comes back."""
        self.overlay_hidden = not getattr(self, "overlay_hidden", False)
        try:
            # ★ the label is STATIC by design -- only the colour changes
            # ★ the COLOUR is the state: amber when the dimming is ON (it is affecting his screen),
            #   plain grey when it is OFF (nothing is being drawn over his work).
            _ctx = self.dim_btn.get_style_context()
            if self.overlay_hidden:
                _ctx.remove_class("pt-dim-on")
            else:
                _ctx.add_class("pt-dim-on")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.plane.queue_draw()
        except Exception:  # noqa: BLE001
            pass
        self._feedback("dim", hidden=bool(self.overlay_hidden))
        return False

    def verdict(self, ok: bool):
        """★ 'IS THIS THE RIGHT SPOT?'\u2014 now a real loop.
        On NO it re-aims through window \u2192 anchor \u2192 OCR \u2192 title, raises the window, logs the
        verdict, and forwards it to the selected guide agent. The button label is set AFTER the one
        step repaint, so the operator can see that the press landed. Delivery is a mirror, not a
        promise: a live DSH chat is link-only and says NOT delivered.

        ★ THREE DIFFERENT FACTS, THREE DIFFERENT LABELS. "Re-aimed" means the rect moved. "Same spot"
        means the resolver found its anchor but the rect did not move (a stationary panel is the
        common case) — that is NOT a failure. "No new aim" means no rung of the ladder could resolve
        anything. The old code called the middle case "no window/text found", which was false and sent
        the operator looking for a lost window that was exactly where it had always been.
        """
        s = self.steps[self.i]
        before = list(s.get("rect") or [])
        how = "confirmed" if ok else self._reaim(s)
        after = list(s.get("rect") or [])
        # ★ A SUCCESSFUL RE-AIM RESOLVES THE STEP. If it was unresolved (missing panel report, a dead
        #   window) and a rung just produced a rectangle, leaving `resolved:False` would keep the card
        #   red and the state lying after the guide had already found the spot.
        if not ok and after and how and not s.get("resolved"):
            s["resolved"] = True
            s["error"] = ""
            s["recovery"] = ""
        s["_verdict"] = "explicit-ok" if ok else "explicit-wrong"
        try:
            self._apply(self.i)          # ONE repaint; every label below is written AFTER it
        except Exception:  # noqa: BLE001
            pass
        try:
            if ok:
                self.yes_btn.set_label("\u2713 recorded \u2014 right spot")
                self._set_state("\u25ce right spot recorded", "ok")
            elif after != before:
                self.no_btn.set_label("\u2717 recorded \u2014 re-aimed")
                self._set_state("\u25ce wrong spot \u2014 re-aimed via " + how, "ok")
            elif how:
                self.no_btn.set_label("\u2717 recorded \u2014 same spot")
                _tail = (" If the panel moved, its page has not reported yet."
                         if str(how).startswith("anchor") else "")
                self._set_state("\u25ce wrong spot \u2014 " + how + " re-read; the ring is still on the "
                                "same spot." + _tail, "warn")
            else:
                self.no_btn.set_label("\u2717 recorded \u2014 no new aim")
                _why = str(s.get("_reaim_error") or "no window or text matched")
                self._set_state("\u26a0 wrong spot \u2014 " + _why + "; last rect kept", "warn")
        except Exception:  # noqa: BLE001
            pass
        # ★ RAISE ON A WRONG SPOT FOR AN X11 TARGET. The tooltip has promised this since the first
        #   version; the re-aim ladder re-aimed but never raised. The panel is not raised here: X11
        #   cannot raise a Wayland window, and the front button asks the page instead.
        if not ok and isinstance(s.get("target"), dict) and s.get("target", {}).get("window") \
                and not _is_panel_anchor(s["target"].get("window")):
            try:
                _raise = self._raise_target(s)
                if _raise:
                    self._feedback("raise_on_wrong", step=s.get("id"), note=_raise)
            except Exception:  # noqa: BLE001
                pass
        if not ok:
            try:
                self.search_btn.get_style_context().add_class("pt-go")
                self.search_btn.set_label("\U0001f50d SEARCH for it")
            except Exception:  # noqa: BLE001
                pass
        self._feedback("verdict", step=s.get("id"), step_no=self.i + 1, ok=bool(ok), implied=False,
                       before=before, after=after, how=how)
        self._deliver_verdict(s, ok, how)
        self._snapshot("verdict")
        return False
        # ★★★ THE ORDER HERE WAS THE BUG, AND IT MADE BOTH BUTTONS LOOK DEAD (kept as the reason the
        #     shape above is one `_apply` then label sets): `set_label("recorded")` ran FIRST and
        #     `self.go(self.i)` ran SECOND, and `go()` calls `_apply()`, which is where the per-step
        #     label RESET lives. The operator: *"right and wrong spot buttons don't appear to be
        #     working."* They were working; the evidence was erased one line later. Do not move a label
        #     write in front of `_apply()`.

    def _raise_target(self, s) -> str:
        """Raise the window a step points at. Returns a SHORT HONEST NOTE, never a silent success."""
        # ★★★ TWO BUGS HERE, AND THE SECOND MADE THE FIRST INVISIBLE.
        #   1. `s.get("title")` IS THE STEP'S TITLE, NOT A WINDOW NAME. The entry in the live feedback
        #      store read `jump aionui  raise: neither wmctrl nor xdotool available` -- but xdotool IS
        #      at /usr/bin/xdotool and `shutil.which` finds it. What actually happened is the step's
        #      title ("This is your agent's own browser") was searched for as a WINDOW NAME, matched
        #      nothing, and fell through to the final return.
        #   2. THAT FINAL RETURN BLAMED MISSING TOOLS FOR A SEARCH THAT RAN AND FOUND NOTHING. A
        #      fallback message that names the wrong cause is worse than no message: it sends the next
        #      reader to install software that is already installed.
        # ★ The window name lives on the TARGET, so that is where it is read from. `title` is never a
        #   window name and is not consulted.
        w = None
        _t = s.get("target")
        if isinstance(_t, dict):
            w = _t.get("window")
        if not w and isinstance(s.get("screen"), dict):
            w = s["screen"].get("window")
        if not w and s.get("anchor"):
            w = s["anchor"]
        if not w:
            return "raise: this step names no window (a rect/point target has nothing to raise)"
        import shutil as _sh
        import subprocess as _sp
        if _sh.which("wmctrl"):
            try:
                r = _sp.run(["wmctrl", "-a", str(w)], capture_output=True, timeout=6)
                if r.returncode == 0:
                    return f"raised {w!r} with wmctrl"
            except Exception:  # noqa: BLE001
                pass
        if _sh.which("xdotool"):
            try:
                r = _sp.run(["xdotool", "search", "--name", str(w)], capture_output=True, timeout=6)
                ids = [x for x in (r.stdout or b"").decode().split() if x]
                if not ids:
                    # ★ SAY WHAT ACTUALLY HAPPENED. The tool is here; the NAME matched nothing.
                    return (f"raise: xdotool found NO window matching {w!r} -- the tool is present "
                            f"(/usr/bin/xdotool), the name is what did not match")
                rr = _sp.run(["xdotool", "windowactivate", "--sync", ids[-1]],
                             capture_output=True, timeout=6)
                if rr.returncode == 0:
                    return f"raised {w!r} with xdotool"
                return (f"xdotool found {len(ids)} window(s) matching {w!r} but windowactivate was "
                        f"refused (rc={rr.returncode}) -- NO window manager hint for that window")
            except Exception as e:  # noqa: BLE001
                return f"xdotool raised {e.__class__.__name__}: {e}"
        return ("raise: xdotool is NOT on PATH and wmctrl is not installed -- no raise tool "
                "available at all")

    def _run_action(self, label: str, cmd: str):
        """Run a step-declared action AFTER an explicit confirm. Records it either way."""
        import subprocess as _sp
        dlg = Gtk.MessageDialog(transient_for=self.card, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=f"Run: {label}?")
        dlg.format_secondary_text(cmd)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            self._feedback("action", step=self.steps[self.i].get("id"), label=label,
                           cmd=cmd, ran=False, detail="cancelled by the operator")
            return False
        try:
            r = _sp.run(cmd, shell=True, capture_output=True, timeout=60, text=True)
            out = ((r.stdout or "") + (r.stderr or "")).strip()[:400]
            self._feedback("action", step=self.steps[self.i].get("id"), label=label, cmd=cmd,
                           ran=True, rc=r.returncode, output=out)
            self.err.set_text(f"\u2699 {label}: rc={r.returncode}")
        except Exception as e:  # noqa: BLE001
            self._feedback("action", step=self.steps[self.i].get("id"), label=label, cmd=cmd,
                           ran=False, detail=f"{e.__class__.__name__}: {e}")
            self.err.set_text(f"\u2699 {label} FAILED: {e.__class__.__name__}")
        return False

    def _windows(self):
        """Every top-level window with real geometry: [(id, title, rect)]. The search's raw material."""
        import subprocess as _sp
        out = []
        try:
            r = _sp.run(["xwininfo", "-root", "-tree"], capture_output=True, timeout=10, text=True)
            # ★ THE ROW REGEX IS THE BUG THAT MADE THIS RESOLVER BLIND ONCE ALREADY (xwininfo prints
            #   TWO geometry pairs per line). It is copied from the working resolver rather than
            #   rewritten -- a second parser is a second way to be blind.
            import re as _re
            for line in (r.stdout or "").splitlines():
                mm = _re.search(r'(0x[0-9a-f]+)\s+"([^"]*)".*?(\d+)x(\d+)[+-](-?\d+)[+-](-?\d+)', line)
                if not mm:
                    continue
                w, h, x, y = int(mm.group(3)), int(mm.group(4)), int(mm.group(5)), int(mm.group(6))
                if w < 60 or h < 40 or mm.group(2).startswith("point-me-to-there"):
                    continue
                out.append((mm.group(1), mm.group(2), [x, y, w, h]))
        except Exception:  # noqa: BLE001
            return []
        return out

    def _search_query(self, s) -> str:
        """What are we looking for? An explicit `find:` wins; otherwise the step's own words."""
        q = s.get("find")
        if q:
            return str(q)
        parts = [str(s.get("id") or ""), str(s.get("title") or "")]
        return " ".join(parts)

    # ── VOICE ASSIST ─────────────────────────────────────────────────────────────────────────────
    def _card_text(self) -> str:
        """Everything the card is showing, as one sentence to read out. The card is the text."""
        s = self.steps[self.i]
        n, tot = self.i + 1, len(self.steps)
        bits = [f"Step {n} of {tot}.", str(s.get("title") or ""), str(s.get("body") or "")]
        if s.get("hint"):
            bits.append("Hint. " + str(s["hint"]))
        if not s.get("resolved"):
            bits.append("Warning: this step points at nothing on this screen.")
        return " ".join(x for x in bits if x).strip()

    # ★★★ "COSTA RICA VOICE (NOT THE PEPPY ONE, THE OTHER ONE) NEEDS TO BE DEFAULT VOICE (WE'LL ALSO
    #     WANT LANGUAGE CONTROLS THERE TOO)." -- measured from the live voicebox: there are TWO Costa
    #     Rica clones, the plain one and the peppy one. The plain one is the default; the peppy one
    #     is deliberately NOT, and it stays selectable.
    # ★ It is stored in the seat's OWN voice state so the choice survives the overlay -- a voice picker
    #   that forgets is a picker you re-set every session.
    # ★ his choice, after hearing both: *"let's default it to Amy-medium."* piper, not the xtts clone
    #   -- and it is ~4s a phrase against xtts's ~9-11s, which is the difference between a guide that
    #   talks to you and one that makes you wait.
    DEFAULT_VOICE = "en_US-amy-medium"
    LANGUAGES = (("en", "English"), ("es", "Spanish"), ("fr", "French"))

    def _voice_pref(self) -> dict:
        try:
            if VOICE_PREF.is_file():
                return json.loads(VOICE_PREF.read_text())
        except Exception:  # noqa: BLE001
            pass
        return {"voice": self.DEFAULT_VOICE, "language": "en"}

    def _save_voice_pref(self, **kw) -> None:
        try:
            d = self._voice_pref(); d.update(kw)
            VOICE_PREF.parent.mkdir(parents=True, exist_ok=True)
            VOICE_PREF.write_text(json.dumps(d, indent=1))
        except Exception:  # noqa: BLE001
            pass

    def _voices(self):
        """The voices voicebox actually has, straight from the service -- never a hardcoded list."""
        try:
            import urllib.request as _u
            with _u.urlopen("http://127.0.0.1:8770/voices", timeout=8) as r:
                d = json.loads(r.read())
            vs = d if isinstance(d, list) else (d.get("voices") or d.get("data") or [])
            out = []
            for v in vs:
                if isinstance(v, dict):
                    out.append((v.get("select") or v.get("name"), v.get("name") or v.get("select")))
                else:
                    out.append((str(v), str(v)))
            return out
        except Exception:  # noqa: BLE001
            return []

    def pick_voice(self):
        """★ CHOOSE AND BIND. Selecting a voice writes it as the DEFAULT and reads a sample so he can
        hear it before committing. A picker that does not persist is a picker you re-set every time."""
        vs = self._voices()
        if not vs:
            self.err.set_text("\U0001f50a could not list voices -- is voicebox up on 8770?")
            return False
        cur = self._voice_pref().get("voice") or self.DEFAULT_VOICE
        pop = Gtk.Popover()
        pop.set_relative_to(self.voice_btn)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            try:
                getattr(box, "set_margin_" + m)(6)
            except Exception:  # noqa: BLE001
                pass
        hdr = Gtk.Label(label="read-aloud voice"); hdr.set_xalign(0)
        box.pack_start(hdr, False, False, 0)
        sc = Gtk.ScrolledWindow(); sc.set_min_content_height(260); sc.set_min_content_width(320)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for sel, nm in sorted(vs, key=lambda x: str(x[1])):
            lbl = ("\u25cf " if sel == cur else "   ") + str(nm)
            b = Gtk.Button(label=lbl)
            b.set_relief(Gtk.ReliefStyle.NONE); b.set_xalign(0)
            b.set_tooltip_text(f"Set {nm} as the default read-aloud voice, and read a sample.")
            b.connect("clicked", lambda _b, sel=sel, nm=nm, pop=pop: (
                self._save_voice_pref(voice=sel), pop.popdown(), self._voice_bound(sel, nm)))
            inner.pack_start(b, False, False, 0)
        sc.add(inner); box.pack_start(sc, True, True, 0)
        pop.add(box); pop.show_all(); pop.popup()
        return False

    def _voice_bound(self, sel: str, nm: str) -> None:
        try:
            self.voice_btn.set_label("voice: " + str(nm)[:14])
        except Exception:  # noqa: BLE001
            pass
        self._feedback("voice_bound", voice=sel, name=nm,
                       detail="bound as default and persisted")
        self.speak_async("Voice set to " + str(nm) + ".")     # hear it immediately

    def stop_voice(self):
        """★★★ STOP THE MOUTH MID-TALK. voicebox already exposes POST /stop and terminates the aplay
        child it owns -- so this is a control that was always possible and simply had no button.
        ★ HONEST LIMIT, WRITTEN HERE RATHER THAN DISCOVERED LATER: this stops the mouth VOICEBOX OWNS.
          A client that asks voicebox for a WAV and then plays it itself (AionUi does exactly this --
          the source calls it "mouth B") owns its own aplay child, and NOTHING HERE CAN STOP IT. That
        is the compartmentalisation gap the operator spotted, and it is not fixable from this file."""
        note = "no answer"
        try:
            import urllib.request as _u
            req = _u.Request("http://127.0.0.1:8770/stop", data=b"{}",
                             headers={"Content-Type": "application/json"})
            with _u.urlopen(req, timeout=8) as r:
                note = f"/stop -> {r.status}"
        except Exception as e:  # noqa: BLE001
            note = f"/stop failed: {e.__class__.__name__}"

        # ★★★ AND THE SECOND MOUTH. voicebox's /stop terminates the aplay child VOICEBOX owns. It
        #     cannot reach a client that took a WAV path and plays it itself -- the source names this
        #     exactly: "mouth B -- a PURE SYNTHESISER handing back a WAV path, and we run aplay
        #     OURSELVES as a child". AionUi is mouth B, which is why the operator still heard it after
        #     the guide stopped: *"I still see AionUi recognizing this voice instance."*
        # ★ SO THIS FINDS THOSE PLAYERS AND STOPS THEM TOO, BY EXACT IDENTITY: an `aplay` process whose
        #   OWN argv names a voicebox output file. NOT a pattern-kill -- the pid is read from /proc,
        #   the argv is checked, and only a real match is signalled. Killing by name would be exactly
        #   the pgrep -f mistake this estate forbids.
        killed = 0
        try:
            import os as _os
            for _pid in [d for d in _os.listdir("/proc") if d.isdigit()]:
                try:
                    with open(f"/proc/{_pid}/cmdline", "rb") as _f:
                        argv = _f.read().split(b"\0")
                except OSError:
                    continue
                if not argv or b"aplay" not in argv[0].split(b"/")[-1:]:
                    continue
                if not any(b"khaoss_speak_" in a for a in argv):
                    continue                       # not a voicebox playback; leave it alone
                try:
                    _os.kill(int(_pid), 15)
                    killed += 1
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        if killed:
            note += f" | stopped {killed} client-owned player(s)"
        self._speaking = False
        try:
            self.read_btn.set_label("\U0001f50a re-read")
            self.err.set_text("\u23f9 stopped")
        except Exception:  # noqa: BLE001
            pass
        self._feedback("stop_voice", detail=note)
        return False

    def _languages(self):
        """The languages that ACTUALLY have a voice installed -- read from voicebox, never guessed."""
        out = []
        for sel, nm in self._voices():
            pfx = str(nm).split("_")[0].split("-")[0].lower()
            if len(pfx) == 2 and pfx.isalpha() and pfx not in out:
                out.append(pfx)
        return sorted(out) or ["en"]

    def pick_language(self):
        """★ A DROPDOWN, NOT A SWITCH. It lists every language with a voice, marks the current one, and
        writes the choice so it survives the overlay."""
        langs = self._languages()
        cur = self._voice_pref().get("language") or "en"
        names = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
                 "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "ru": "Russian", "zh": "Chinese"}
        pop = Gtk.Popover()
        pop.set_relative_to(self.lang_btn)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            try:
                getattr(box, "set_margin_" + m)(6)
            except Exception:  # noqa: BLE001
                pass
        hdr = Gtk.Label(label="read-aloud language"); hdr.set_xalign(0)
        box.pack_start(hdr, False, False, 0)
        for code in langs:
            label = ("\u25cf " if code == cur else "   ") + f"{names.get(code, code)} ({code})"
            b = Gtk.Button(label=label)
            b.set_relief(Gtk.ReliefStyle.NONE); b.set_xalign(0)
            b.set_tooltip_text(f"Read aloud in {names.get(code, code)} and remember the choice.")
            b.connect("clicked", lambda _b, c=code, pop=pop: (
                self._save_voice_pref(language=c), pop.popdown(), self._lang_bound(c)))
            box.pack_start(b, False, False, 0)
        pop.add(box); pop.show_all(); pop.popup()
        return False

    def _lang_bound(self, code: str):
        try:
            self.lang_btn.set_label("lang: " + code)
        except Exception:  # noqa: BLE001
            pass
        self._feedback("language_bound", language=code, detail="bound and persisted")
        self.speak_async(self._card_text())      # read it back in the language he just chose
        return False

    def cycle_language(self):
        """Cycle the read-aloud language. Kept to the voices that ACTUALLY EXIST on this box."""
        order = [c for c, _n in self.LANGUAGES]
        cur = self._voice_pref().get("language") or "en"
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "en"
        self._save_voice_pref(language=nxt)
        try:
            self.lang_btn.set_label("lang: " + nxt)
        except Exception:  # noqa: BLE001
            pass
        self._feedback("language", language=nxt)
        self._speak(self._card_text())
        return False

    def _speak_blocking(self, text: str) -> str:
        """The actual TTS call. ★ NEVER CALL THIS FROM THE UI THREAD -- use speak_async()."""
        try:
            import urllib.request as _u
            _pref = self._voice_pref()
            _body = {"text": text, "play": True}
            if _pref.get("voice"):
                _body["voice"] = _pref["voice"]
            if _pref.get("language"):
                _body["language"] = _pref["language"]
            req = _u.Request("http://127.0.0.1:8770/speak",
                             data=json.dumps(_body).encode(),
                             headers={"Content-Type": "application/json"})
            # ★ 90s, because the xtts clone genuinely needs ~9-11s and a cold model more. This is the
            #   WORKER's timeout; the UI never waits on it either way.
            with _u.urlopen(req, timeout=90) as r:
                return f"read aloud ({r.status})"
        except Exception as e:  # noqa: BLE001
            return f"could not read aloud: {e.__class__.__name__}"

    def speak_async(self, text: str) -> None:
        """★★★ THE HANG FIX, AND IT WAS A REAL ONE. "Point_to.py Is Not Responding" appeared on the
        operator's screen because `_speak` did a BLOCKING urlopen on the GTK MAIN THREAD -- and the
        Costa Rica voice he asked for is an xtts clone that takes ~9-11 SECONDS per phrase (measured:
        11.32s, then 8.98s warm; piper's en_US-lessac is 4.19s). A 60-second blocking call on the UI
        thread is a frozen window, every time, and it is why he could not progress.
        ★★ THE LESSON GENERALISES: a UI thread may START work, never WAIT for it. Threading here is not
        an optimisation, it is the difference between a guide and a lockup -- and it stays correct
        whatever voice is selected later.
        """
        import threading
        if getattr(self, "_speaking", False):
            return                      # already reading; a second click is not a queue
        self._speaking = True
        try:
            self.read_btn.set_label("\U0001f50a reading\u2026")
        except Exception:  # noqa: BLE001
            pass

        def _work():
            note = self._speak_blocking(text)
            def _done():
                self._speaking = False
                try:
                    self.read_btn.set_label("\U0001f50a re-read")
                except Exception:  # noqa: BLE001
                    pass
                self._feedback("read", step=self.steps[self.i].get("id"), step_no=self.i + 1,
                               detail=note)
                return False
            try:
                GLib.idle_add(_done)     # ★ touch widgets ONLY back on the main thread
            except Exception:  # noqa: BLE001
                self._speaking = False
        threading.Thread(target=_work, daemon=True, name="pt-tts").start()

    def _speak(self, text: str) -> str:
        """Kept as the name the rest of the file calls -- but it now DEFERS instead of blocking."""
        self.speak_async(text)
        return "read aloud (queued)"

    def speak_card(self):
        """READ / RE-READ. Pressing again always re-reads from the top -- he asked for that explicitly.
        ★ Non-blocking: the label changes to 'reading…' immediately and the UI stays live throughout."""
        self.speak_async(self._card_text())
        return False

    def toggle_voice(self):
        """VOICE ASSIST ON/OFF: read every new step automatically, or only when asked."""
        self.voice_auto = not getattr(self, "voice_auto", False)
        on = bool(self.voice_auto)
        try:
            # ★ the label is STATIC by design -- only the colour changes
            _c = self.auto_btn.get_style_context()
            (_c.add_class if on else _c.remove_class)("pt-voice-on")
            # ★ AND THE TEXT COLOUR TOO, not just the border -- he asked for colours on/off.
            (_c.remove_class if on else _c.add_class)("pt-voice-off")
        except Exception:  # noqa: BLE001
            pass
        self._feedback("voice", on=on)
        if on:
            self._speak(self._card_text())
        return False

    # ── OPEN / REOPEN ─────────────────────────────────────────────────────────────────────────────
    def open_target(self):
        """★ OPEN (OR REOPEN) WHATEVER THIS STEP IS ABOUT. The Boardwalk step searched all four
        windows and found nothing because the board was NOT OPEN -- no amount of searching finds a
        thing that is not running. This is the answer to that, and it is why he asked for it."""
        s = self.steps[self.i]
        what = s.get("open") or ""
        if not what:
            self.err.set_text("\u25b6 this step declares no `open:` target — add one to the walk, "
                              "e.g.  open: http://127.0.0.1:7842/operator/boardwalk")
            self._feedback("open", step=s.get("id"), ran=False, detail="no open: declared")
            return False
        import subprocess as _sp
        try:
            if what.startswith(("http://", "https://")):
                _sp.Popen(["xdg-open", what], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            else:
                _sp.Popen(what, shell=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            self.err.set_text(f"\u25b6 opened: {what}")
            self._feedback("open", step=s.get("id"), ran=True, target=what)
            # give it a moment, then re-resolve so the ring follows the thing that just appeared
            GLib.timeout_add(2500, lambda: (self._rescan(), False)[1])
        except Exception as e:  # noqa: BLE001
            self.err.set_text(f"\u25b6 open FAILED: {e.__class__.__name__}: {e}")
            self._feedback("open", step=s.get("id"), ran=False, detail=f"{e.__class__.__name__}: {e}")
        return False

    def _rescan(self):
        """After an open, re-resolve the CURRENT step's target and redraw -- the window may now exist."""
        try:
            s = self.steps[self.i]
            rr = resolve_target(s.get("target") or {})
            if rr.get("ok") and rr.get("rect"):
                s["rect"] = rr["rect"]; s["resolved"] = True; s["error"] = ""
            self.plane.queue_draw()
            self.go(self.i)
        except Exception:  # noqa: BLE001
            pass
        return False

    def search_for_it(self):
        """★ GO AND LOOK. Score every window against the step's words, point at the best, and report
        the runners-up in the card so the operator can see the search rather than trust it."""
        import re as _re
        s = self.steps[self.i]
        q = self._search_query(s)
        toks = [t for t in _re.findall(r"[a-z0-9]+", q.lower()) if len(t) > 2]
        wins = self._windows()
        scored = []
        for wid, title, rect in wins:
            tl = title.lower()
            hits = sum(1 for t in toks if t in tl)
            if hits:
                scored.append((hits, hits / max(1, len(toks)), wid, title, rect))
        scored.sort(key=lambda x: (-x[0], -(x[4][2] * x[4][3])))
        if not scored:
            # ★ TITLES ARE NOT CONTENT. Every `find:` in this walk is a phrase on the screen, not a
            #   window title; title scoring returns 0 and the operator is told the thing is missing.
            #   Fall through to the OCR resolver, then keep the title search's honest count.
            phrase = str(s.get("anchor_ocr") or q or "").strip()
            try:
                import point_at_core as _core
                rr = _core.resolve(text=phrase, window=(s.get("anchor") or None))
                if rr.get("ok") and rr.get("rect"):
                    s["rect"] = list(rr["rect"]); s["resolved"] = True; s["error"] = ""
                    self.err.set_text(f"\U0001f50d titles: 0 matched \u00b7 OCR found {phrase[:40]!r}")
                    self._feedback("search", step=s.get("id"), query=q, tokens=toks, windows=len(wins),
                                   matched=0, ocr=True, rect=list(s["rect"]))
                    try:
                        self.plane.queue_draw(); self.go(self.i)
                    except Exception:  # noqa: BLE001
                        pass
                    return False
            except Exception:  # noqa: BLE001
                pass
            self.err.set_text(f"\U0001f50d searched {len(wins)} window(s) and OCR for {toks or q!r} "
                              f"\u2014 NOTHING matched. The thing may be closed or named differently.")
            self._feedback("search", step=s.get("id"), query=q, tokens=toks, windows=len(wins),
                           matched=0, ocr=False, detail="no window title or OCR phrase matched")
            return False
        best = scored[0]
        s["rect"] = list(best[4])
        s["resolved"] = True
        s["error"] = ""
        s["_searched"] = {"wid": best[2], "title": best[3], "score": best[1]}
        others = ", ".join(f"{t!r}({h})" for h, _f, _w, t, _r in scored[1:4]) or "none"
        self.err.set_text(f"\U0001f50d searched {len(wins)} window(s): pointing at {best[3]!r} "
                          f"({int(best[1]*100)}% of your words). Others: {others}")
        self._feedback("search", step=s.get("id"), query=q, tokens=toks, windows=len(wins),
                       matched=len(scored), best={"id": best[2], "title": best[3],
                                                  "score": round(best[1], 3), "rect": list(best[4])},
                       others=[t for _h, _f, _w, t, _r in scored[1:4]])
        self._raise_target({"window": best[3]})
        try:
            self.plane.queue_draw()
            self.go(self.i)
        except Exception:  # noqa: BLE001
            pass
        return False

    def jump_to(self):
        """★ GET ME BACK TO IT. Raise the target, re-resolve so the ring follows the window if it has
        moved, turn the outline back ON (jumping is a request to be shown), and redraw."""
        s = self.steps[self.i]
        note = self._raise_target(s)
        try:
            rr = resolve_target(s.get("screen") or {"window": s.get("target")})
            if rr.get("ok") and rr.get("rect"):
                s["rect"] = rr["rect"]
        except Exception:  # noqa: BLE001
            pass
        self.overlay_hidden = False
        try:
            # ★ the label is STATIC by design
            try:
                self.dim_btn.get_style_context().add_class("pt-dim-on")
            except Exception:  # noqa: BLE001
                pass
            self.plane.queue_draw()
            self.go(self.i)
        except Exception:  # noqa: BLE001
            pass
        self._feedback("jump", step=s.get("id"), step_no=self.i + 1, detail=note)
        return False

    def _feedback(self, kind: str, **kw):
        """Append-only. Never raises into the UI -- a guide must not die because a log is unwritable."""
        try:
            rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind,
                   "pid": os.getpid(), "walk": getattr(self, "walk_name", None)}
            rec.update(kw)
            FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
            with open(FEEDBACK, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _raise_card(self):
        try:
            self.card.get_window().raise_()
        except Exception:
            pass
        return False

    def _reassert(self, *_a):
        try:
            gw = self.plane.get_window()
            if gw is None:
                return False
            xid = gw.get_xid()
            apply_empty_input_shape(xid)
            v = verify_click_through(xid)
            self.record["guard"] = {"plane_xid": hex(xid), **v,
                                    "gdk_native_attempt": "tried first, MEASURED no-op on this box"}
            if time.time() - getattr(self, "_last_rep", 0) > 1.0:
                self._last_rep = time.time()
                self._write_report()
        except Exception as e:
            self.record["guard"] = {"click_through": False, "measured": False,
                                    "reason": f"{e.__class__.__name__}: {e}"}
        return False

    # ── interaction ──────────────────────────────────────────────────────────────────────────────
    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            # ★ ESCAPE CANCELS THE WALK, NOT JUST THE WINDOW. Closing the card while leaving the
            #   guide live is the shape the operator called a gap: he presses the obvious escape and
            #   the guide is still there on the next poll. In script mode there is no guide store to
            #   dismiss, so _cancel_walk is a plain close there.
            self._cancel_walk()
            return True
        if ev.keyval in (Gdk.KEY_Right, Gdk.KEY_Return, Gdk.KEY_space):
            self.do_next()
            return True
        if ev.keyval == Gdk.KEY_Left:
            self._go_back()
            return True
        return False

    def _on_leave(self, btn):
        # ★ INVERTED 2026-10-18. Plain click now CANCELS the walk (dismiss in its one store) and
        #   closes; Shift-click closes the overlay only. The old mapping made the common click the
        #   weaker action and put the real exit behind a modifier nobody was told about.
        try:
            if Gtk.get_current_event_state() & Gdk.ModifierType.SHIFT_MASK:
                self._quit()
                return
        except Exception:
            pass
        self._cancel_walk()

    def _cancel_walk(self):
        """★ OPERATOR ASK 2026-09-19: cancel AND close are ONE turnkey choice, never an instant exit.

        Every way out of this card lands here — the ✕ close, the ✕ cancel walk (both of them), Esc,
        and the window-manager X. Leaving marks nothing done in ANY branch; the choices are the two
        things he actually wants instead of leaving (keep the place queued, or get a reminder) plus
        the destructive one, said out loud.
        ★ WHY A DIALOG AND NOT A confirm(): confirm() only asks "are you sure?", which is a dead end
        wearing a warning's clothes — it never offers the alternative.
        """
        self._leave_choice()

    def _leave_choice(self):
        """The one card. ★ NON-BLOCKING BY DESIGN.

        ★ THE FIRST BUILD USED `d.run()`, AND IT BLOCKED AT-SPI FROM SEEING THE CARD — a nested
          Gtk main loop left the window unregistered for the whole time the dialog was up, so the
          drive suite could click "cancel walk" and never find "Leave the walk?". A dialog the
          instrument cannot see is a dialog the operator's screen reader cannot see either. The
          response signal does the same job while the ONE main loop keeps running.
        """
        if getattr(self, "_leave_dlg", None) is not None:
            try:
                self._leave_dlg.present()
            except Exception:  # noqa: BLE001
                pass
            return
        step_id = ""
        try:
            step_id = str((self.steps[self.i] or {}).get("id") or "") if self.steps else ""
        except Exception:  # noqa: BLE001
            step_id = ""
        d = Gtk.Dialog(title="Leave the walk?")
        try:
            d.set_transient_for(self.card)
            d.set_modal(True)
            d.set_destroy_with_parent(True)
        except Exception:  # noqa: BLE001
            pass
        d.set_resizable(False)
        area = d.get_content_area()
        area.set_border_width(14)
        head = Gtk.Label()
        head.set_markup("<b>Leave the walk?</b>")
        head.set_xalign(0)
        area.pack_start(head, False, False, 0)
        body = Gtk.Label(label="Nothing is marked done by leaving. Pick where your place goes:")
        body.set_xalign(0)
        body.set_line_wrap(True)
        area.pack_start(body, False, False, 8)
        if step_id:
            pays = Gtk.Label()
            pays.set_markup("<span size='small'>this step: <tt>"
                            + GLib.markup_escape_text(step_id) + "</tt></span>")
            pays.set_xalign(0)
            area.pack_start(pays, False, False, 0)
        d.add_button("\u25b8  Keep it in the backlog", 1)
        d.add_button("\u25f7  Log for later \u2014 tomorrow 09:00", 2)
        d.add_button("\u25a0  Stop now", 3)
        d.add_button("\u2190  Stay in the walk", Gtk.ResponseType.CANCEL)
        d.connect("response", self._leave_answered)
        d.show_all()
        self._leave_dlg = d

    def _leave_answered(self, dlg, response):
        step_id = ""
        try:
            step_id = str((self.steps[self.i] or {}).get("id") or "") if self.steps else ""
        except Exception:  # noqa: BLE001
            step_id = ""
        try:
            dlg.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._leave_dlg = None
        if response == 1:
            # keep the place: close the overlay, dismiss NOTHING
            self._leave_dismissed = False
            self._feedback("leave_backlog", step=step_id, step_no=self.i + 1, mode=str(self.mode),
                           dismissed=False)
            self._snapshot("leave_backlog")
            self._quit()
        elif response == 2:
            ok, detail = self._log_for_later(step_id)
            self._feedback("leave_log_for_later", step=step_id, step_no=self.i + 1, ok=bool(ok),
                           detail=str(detail))
            self._quit()
        elif response == 3:
            self._leave_dismissed = True
            self._cancel_walk_now()
        # anything else (Stay, Esc, window close) = nothing changes

    def _log_for_later(self, item: str):
        """Park the current step through the panel's ONE reminder store — never a second copy.

        ★ It POSTs to /operator/remind (operator/reminders.py) exactly like the board's button, so a
          reminder logged from the overlay shows up in the board, and the reverse.
        """
        if not item:
            return False, "no current step to park"
        import urllib.request  # noqa: PLC0415 — this file imports it per-call elsewhere too
        base = (os.environ.get("KHAOSS_PANEL_URL") or "http://127.0.0.1:7842").rstrip("/")
        payload = json.dumps({"item": item, "preset": "tomorrow", "custom": "",
                              "note": "logged for later from the overlay at the walk step"}).encode()
        req = urllib.request.Request(base + "/operator/remind", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=4.0) as fh:
                d = json.loads(fh.read().decode() or "{}")
            return bool(d.get("ok", True)), (d.get("when") or d.get("due") or d.get("say") or "logged")
        except Exception as e:  # noqa: BLE001
            return False, f"{e.__class__.__name__}: {e}"

    def _cancel_walk_now(self):
        """End the walk in the ONE store, then close the overlay.

        ★ SCRIPT MODE HAS NO GUIDE STORE. A `--script` walk is a file being shown; cancelling it is
          closing the overlay, nothing else. Calling guide_focus.dismiss() there would dismiss an
          unrelated live guide -- the exact cross-surface write this file refuses everywhere else.
        """
        _dismissed = None
        if str(self.mode).startswith("guide"):
            _dismissed = self._dismiss_guide()
        try:
            self._feedback("cancel", step=(self.steps[self.i].get("id") if self.steps else None),
                           step_no=self.i + 1, mode=str(self.mode),
                           dismissed=(None if _dismissed is None else bool(_dismissed)))
        except Exception:  # noqa: BLE001
            pass
        self._snapshot("cancel")
        self._quit()

    def _dismiss_guide(self):
        try:
            import guide_focus
            return guide_focus.dismiss()
        except Exception as e:  # noqa: BLE001
            self._feedback("dismiss_failed", detail=f"{e.__class__.__name__}: {e}")
            return None

    def _on_delete(self, *_a):
        # The window-manager X is a close, and a close must not strand a live guide.
        self._cancel_walk()
        return True

    def go(self, i):
        self._apply(i)
        return False

    # ── THE STORE-SYNC MOVE (fifth pass: one walk, not two) ──────────────────────────────────────
    def _reload_guide(self) -> bool:
        """Re-read the LIVE walk and show the step the STORE says he is on.

        ★★★ WHY THIS EXISTS. `--guide` used to read `guide_focus` ONCE AT STARTUP and then advance
            its own index. The overlay and the board were two walkers reading one store: pressing
            Next on the card showed the next card, but `guide_focus.json` still said step 1, so the
            board, the panel and the voice seat were all one step behind the thing on his screen.
            Pressing Next was a LOCAL move dressed up as a shared one.
        ★★ IT IS ALSO THE RELOAD DOOR FOR `bin/point-at.py`: an agent pushes a new step into the
            store and sends `reload`; the overlay re-reads the store and shows it, with no restart
            and no second write path.
        """
        if not str(self.mode).startswith("guide"):
            return False
        g = guide_steps()
        if not g.get("ok") or not g.get("steps"):
            self._feedback("guide_gone", detail=str(g.get("error") or "no steps"))
            self._quit()
            return True
        self.steps = g["steps"]
        self.i = max(0, min(int(g.get("start") or 0), max(0, len(self.steps) - 1)))
        self._apply(self.i)
        return True

    def _go_back(self):
        """← / Back: move the SHARED walk back, not just this window's index."""
        if str(self.mode).startswith("guide"):
            try:
                import guide_focus
                r = guide_focus.back()
                if r.get("ok") and self._reload_guide():
                    return False
            except Exception:  # noqa: BLE001 -- a store failure must not trap him on the card
                pass
        return self.go(self.i - 1)

    def do_next(self):
        # ★★★ "I HIT NEXT, VERY GOOD IT HIGHLIGHTED THE NEXT THING BUT I NEED TO RECORD THAT IT'S ON THE
        #     RIGHT SPOT TOO." -- the operator. He is describing a gap: the only way to say a step was
        #     CORRECT was to click 'right spot' and THEN next, so a clean walk recorded nothing at all
        #     and its silences were indistinguishable from its approvals.
        # ★ ADVANCING IS APPROVAL. So Next records it, marked as what it is -- IMPLIED, not explicit --
        #   so the record never overstates what he actually said.
        try:
            _st = self.steps[self.i]
            if not _st.get("_verdict"):
                self._feedback("verdict", step=_st.get("id"), step_no=self.i + 1, ok=True,
                               implied=True,
                               detail="advanced without a wrong-spot complaint; implied approval")
                _st["_verdict"] = "implied-ok"
        except Exception:  # noqa: BLE001 -- a recording failure must never block navigation
            pass
        if self.i < len(self.steps) - 1:
            # ★★ AN INTERMEDIATE NEXT IS A SHARED MOVE, NOT A LOCAL ONE. The store is advanced FIRST
            #    and the card is re-read from it; if the store refuses, the card still advances but
            #    the refusal is not hidden (the next reload will show the truth).
            if str(self.mode).startswith("guide"):
                try:
                    import guide_focus
                    r = guide_focus.advance()
                    if r.get("ok") and self._reload_guide():
                        return
                except Exception:  # noqa: BLE001
                    pass
            self._apply(self.i + 1)
            return
        # last step: hand the move to the walk's owner, then close
        if self.on_next:
            try:
                self.on_next(self.steps[self.i])
            except Exception:
                pass
        self._quit()

    def _auto_step(self):
        if self.auto:
            self.do_next()
        return False

    def _poll_cmd(self):
        """`--next` / `--kill` from the terminal reach a RUNNING overlay through one small file.
        ★ Polling a file is the smallest control channel that cannot be confused with a business
        store, and it dies with the process."""
        try:
            if CMD.is_file():
                c = CMD.read_text().strip().splitlines()
                CMD.unlink()
                for verb in c:
                    v = verb.strip().lower()
                    if v == "next":
                        self.do_next()
                    elif v == "back":
                        self._go_back()
                    elif v in ("reload", "refresh"):
                        # ★ THE LIVE-RELOAD DOOR `bin/point-at.py` USES. The store is the source of
                        #   truth; this only re-reads it. No restart, no second walk.
                        self._reload_guide()
                    elif v in ("kill", "quit", "close"):
                        self._quit(); return False
                    elif v.startswith("goto:"):
                        try:
                            self.go(int(v.split(":", 1)[1]) - 1)
                        except ValueError:
                            pass
        except Exception:
            pass
        return True

    def _write_report(self):
        rec = dict(self.record)
        rec.update({"pid": os.getpid(), "mode": self.mode, "step_now": self.i + 1,
                    "n_steps": len(self.steps), "screen": self.screen,
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
        rec["guard"] = getattr(self, "record", {}).get("guard") or {}
        try:
            import safe_store
            safe_store.write_json(REPORT, rec)
        except Exception:
            try:
                REPORT.write_text(json.dumps(rec, indent=2))
            except OSError:
                pass

    def _quit(self):
        try:
            self._write_report()
        except Exception:
            pass
        try:
            self.card.destroy()
        except Exception:
            pass
        try:
            self.plane.destroy()
        except Exception:
            pass
        Gtk.main_quit()
        return False

    def run(self):
        self.plane.show_all()
        # ★ THE CARD IS RAISED, THE PLANE IS NOT LOWERED. Lowering the plane risks dropping it behind
        #   ordinary windows on a WM that does not honour keep_above; raising the small window costs
        #   nothing and cannot break the pin.
        GLib.timeout_add(120, self._reassert)
        GLib.timeout_add(140, self._fit_plane)
        GLib.timeout_add(200, self._raise_card)
        self.card.present()
        Gtk.main()
        return 0


# ── guide-following mode (the BOARDWALK integration) ─────────────────────────────────────────────
def guide_steps(limit: int = 40) -> dict:
    """Read the LIVE guided walk and turn it into render steps.

    ★★ THIS IS THE WHOLE INTEGRATION. No new store, no second walk. `guide_focus.current()` already
    answers 'which step is he on'; this only ADDS the screen geometry a step may carry in its `screen`
    field (which `set_sequence` passes through untouched).

    ★ A step with no `screen` block is NOT dropped. It renders as a card-only step that says the
    board is pointing inside the page rather than at the desktop -- because the panel-guided walk and
    this overlay are the same walk, and a step silently missing from one of them is a step he thinks
    does not exist."""
    try:
        import guide_focus
    except Exception as e:
        return {"ok": False, "error": f"could not import guide_focus: {e}", "steps": []}
    cur = guide_focus.current()
    if not cur.get("active"):
        return {"ok": False, "error": "no guided walk is active", "steps": [], "current": cur}
    out = []
    d = json.loads(guide_focus.STATE.read_text()) if guide_focus.STATE.is_file() else {}
    seq = d.get("steps") or [cur]
    # ★★★ THE WALK IS RENDERED WHOLE, AND THE OVERLAY JUMPS TO WHERE HE ACTUALLY IS.
    #     This used to slice `seq[start:]` and renumber from start+1, so a walk he was 2 steps into
    #     rendered as "STEP 1 OF 4" -- the TOTAL was wrong, the progress was lost, and step 1 could not
    #     be reached with Back because it was not in the list at all. Measured live: guide_focus said
    #     {"step": 2, "of": 5} while the card said "1 OF 4".
    # ★ A guide that hides the steps you have done is not tracking you; it is discarding you.
    start = int(d.get("i") or 0) if d.get("steps") else 0
    for n, raw in enumerate(seq[:limit], 1):
        scr = raw.get("screen")
        tgt = None
        if isinstance(scr, dict):
            tgt = {k: v for k, v in scr.items() if k in TARGET_KINDS}
        if not tgt:
            out.append({"id": raw.get("id") or f"guide-{n}", "step": n,
                        "title": raw.get("title") or str(raw.get("say") or "")[:70] or f"step {n}",
                        "body": str(raw.get("say") or ""), "hint": str(raw.get("why") or ""),
                        "illustration": raw.get("illustration") or None,
                        "target": None, "resolved": False, "rect": None,
                        "error": "this step names no SCREEN target -- the board is pointing INSIDE "
                                 "the panel page for this one (kind "
                                 f"{raw.get('kind')!r}, target {raw.get('target')!r}). Open the board "
                                 "to see where it means.",
                        "board_target": raw.get("target"), "board_kind": raw.get("kind")})
            continue
        r = resolve_target(tgt)
        # guide steps may carry the same identity; the anchor lives beside the target keys.
        _anchor = str(raw.get("anchor") or (scr or {}).get("anchor") or "").strip()
        _anchor_ocr = str(raw.get("anchor_ocr") or (scr or {}).get("anchor_ocr") or "").strip()
        _track = str(raw.get("track") or (scr or {}).get("track") or "on").lower() not in ("off", "false", "no", "0")
        _anchor_state, _anchor_age = "", None
        if _anchor and isinstance(tgt, dict) and tgt.get("rect") and r.get("ok"):
            if _is_panel_anchor(_anchor):
                _info = _panel_geometry_info()
                _w = ({"ok": True, "rect": _info["rect"]} if _info.get("ok")
                      else {"ok": False, "error": _info.get("error")})
                _anchor_age = _info.get("age")
            else:
                _info = {}
                _w = resolve_target({"window": _anchor})
            if _w.get("ok") and _w.get("rect"):
                _rel = tgt["rect"]
                r = {**r, "rect": [_w["rect"][0] + _rel[0], _w["rect"][1] + _rel[1], _rel[2], _rel[3]]}
                _anchor_state = "stale" if _info.get("stale") else "fresh"
            else:
                _anchor_state = "missing"
                r = {**r, "ok": False, "rect": None,
                     "reason": ("panel_geometry_" + str(_info.get("reason") or "missing")
                                if _is_panel_anchor(_anchor) else "anchor_missing"),
                     "error": (str(_info.get("error")) if _is_panel_anchor(_anchor)
                               else f"anchor window {_anchor!r} not found: {_w.get('error')}")}
        out.append({"id": raw.get("id") or f"guide-{n}", "step": n,
                    "title": raw.get("title") or str(raw.get("say") or "")[:70] or f"step {n}",
                    "body": str(raw.get("say") or ""), "hint": str(raw.get("why") or ""),
                    "open": raw.get("open") or "", "find": raw.get("find") or "",
                    "actions": raw.get("actions") or [],
                    "illustration": raw.get("illustration") or None,
                    "target": tgt, "resolved": bool(r["ok"]), "rect": r.get("rect"),
                    "how": r.get("how"), "error": r.get("error"),
                    "reason": r.get("reason") or "",
                    "ambiguous": bool(r.get("ambiguous")), "candidates": r.get("candidates") or [],
                    "anchor": _anchor, "anchor_ocr": _anchor_ocr, "track": _track,
                    "anchor_state": _anchor_state,
                    "anchor_age": _anchor_age if _anchor_age is not None else r.get("age"),
                    "recovery": ("Hard-reload the panel page once (Ctrl+Shift+R) so it posts where its "
                                 "window is; the overlay re-aims by itself within a second."
                                 if _anchor_state == "missing" else
                                 ("The ring is on the last position the panel reported; hard-reload "
                                  "the panel page if it has moved."
                                  if _anchor_state == "stale" else "")),
                    "why_it_matters": raw.get("why_it_matters") or "",
                    "what_can_go_wrong": raw.get("what_can_go_wrong") or "",
                    "board_target": raw.get("target"), "board_kind": raw.get("kind")})
    return {"ok": True, "steps": out, "start": start, "current": cur, "by": d.get("by"),
            "of": len(seq), "i": start, "why": d.get("why")}


def push_walk(plan_d: dict, by="an agent", why="", replace=False) -> dict:
    """Turn a resolved walkthrough into the BOARD's own guided walk, so the board and the overlay are
    one walk rather than two.
    ★ Every step keeps its `screen` block (guide_focus passes unknown fields through) and its board
    `id` as the guide target when it looks like a board row, so pressing Done on the card advances the
    SAME sequence the board renders."""
    steps = []
    for s in plan_d.get("steps") or []:
        kind = "item" if re.fullmatch(r"[a-z0-9][a-z0-9._:-]{2,80}", str(s.get("id") or "")) else "none"
        st = {"kind": kind, "target": str(s.get("id") or ""),
              "say": (s.get("body") or s.get("title") or "").strip(),
              "why": (s.get("hint") or "").strip(),
              "title": s.get("title"), "id": s.get("id")}
        if s.get("target"):
            st["screen"] = s["target"]
        # ★★★ THESE WERE SILENTLY DROPPED, AND THAT IS EXACTLY WHY `Open it` DID NOTHING.
        #     The live feedback store recorded his click twice: `open  step=boardwalk  no open:
        #     declared`. The walk FILE had `open:` -- but push_walk copied only kind/target/say/why/
        #     title/id/screen into the board's store, and the OVERLAY READS THE STORE, so the field
        #     never reached the thing that needed it. An authoring format whose fields do not survive
        #     the push is a format that lies about what it supports.
        for _k in ("open", "find", "actions", "illustration", "anchor", "anchor_ocr", "track",
                   "why_it_matters", "what_can_go_wrong"):
            if s.get(_k):
                st[_k] = s[_k]
        steps.append(st)
    try:
        import guide_focus
    except Exception as e:
        return {"ok": False, "error": f"could not import guide_focus: {e}"}
    r = guide_focus.set_sequence(steps, by=by, why=why, replace=bool(replace))
    if not r.get("ok"):
        return r
    return {**r, "pushed": len(steps), "store": str(guide_focus.STATE),
            "note": "This is the board's OWN guided walk. The board page, the voice seat and this "
                    "overlay all read the same step, and advancing from any of them moves all three."}


def record_back(step: dict) -> dict:
    """He finished a step. Record it in the stores that already own that fact -- the guide advances,
    and if the step names a board row the BOARD WALK marks it seen. No new store is created."""
    out = {}
    try:
        import guide_focus
        out["guide"] = guide_focus.advance()
    except Exception as e:
        out["guide"] = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    tgt = str(step.get("board_target") or step.get("id") or "")
    if tgt and not tgt.startswith("guide-"):
        try:
            import board_walk
            out["board_walk"] = board_walk.mark(tgt, "done")
        except Exception as e:
            out["board_walk"] = {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    return out


# ── kill: ONE pid, identity-checked ──────────────────────────────────────────────────────────────
def kill_running() -> dict:
    """★ NO `pkill -f`, EVER -- this estate forbids it by rule and it would take out any process whose
    command line merely mentions the pattern. We read ONE pid from our own pidfile, then check that
    pid's /proc/<pid>/cmdline really is a python running THIS file before signalling it."""
    if not PIDFILE.is_file():
        return {"ok": True, "killed": False, "say": "nothing was running (no pidfile)"}
    try:
        pid = int(PIDFILE.read_text().strip().split()[0])
    except Exception as e:
        return {"ok": False, "killed": False, "say": f"pidfile is unreadable: {e}"}
    cmd = ""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        PIDFILE.unlink(missing_ok=True)
        return {"ok": True, "killed": False, "say": f"pid {pid} is gone; pidfile cleared"}
    if "point_to.py" not in cmd:
        return {"ok": False, "killed": False, "pid": pid, "cmdline": cmd[:200],
                "say": f"REFUSED: pid {pid} is not this program (cmdline does not contain "
                       f"point_to.py). Nothing was signalled."}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "killed": False, "pid": pid, "say": f"signal failed: {e}"}
    PIDFILE.unlink(missing_ok=True)
    return {"ok": True, "killed": True, "pid": pid, "say": f"asked pid {pid} to stop"}


def running_pid():
    """The pid in the pidfile, but ONLY if that process really is this program. Never a name match."""
    if not PIDFILE.is_file():
        return None
    try:
        pid = int(PIDFILE.read_text().strip().split()[0])
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return None
    return pid if "point_to.py" in cmd else None


def write_pidfile():
    archive_before_write(PIDFILE)
    PIDFILE.write_text(f"{os.getpid()}\n")


# ── self-test ────────────────────────────────────────────────────────────────────────────────────
def self_test(prove_fail: bool = False) -> int:
    """★ NEGATIVE CONTROL FIRST. The defect this suite must catch is the one this whole file is
    shaped around: a target that cannot be resolved being SILENTLY defaulted to a rectangle, so the
    guide points confidently at the top-left corner of the screen and nobody is told.

    `--prove-fail` injects that defect (and the second one: claiming click-through without measuring
    it). A run that still passes with the defect injected is a worthless suite -- so this returns
    NON-ZERO on the prove-fail run. RED IS THE PASS CONDITION FOR `--prove-fail`.
    """
    fails = []
    caught = []
    injected = []

    print("  ── jarvis/point_to.py self-test ──────────────────────────────────────────────")
    print(f"  GDK_BACKEND forced to {os.environ.get('GDK_BACKEND')!r} "
          f"(was {_PRIOR_BACKEND!r}; overridden={BACKEND_OVERRIDDEN})")

    # ★★★ THE INJECTION IS REAL CODE, NOT A NOTE IN A BANNER. Two defects are swapped into the live
    #     module globals, so every assertion below runs against a genuinely broken program:
    #       1. an unresolvable target is silently defaulted to a small box at the top-left -- the exact
    #          "confidently pointing at nothing" failure this file is shaped around;
    #       2. apply_empty_input_shape() returns True and touches nothing -- click-through that is a
    #          claim. The suite must catch THAT one through the X-server MEASUREMENT, not through a
    #          flag, or the measurement is decoration.
    _saved = {}
    if prove_fail:
        _real_rt = resolve_target

        def _buggy_resolve(target):
            r = _real_rt(target)
            if not r.get("ok"):
                return {"ok": True, "kind": r.get("kind") or "rect", "rect": [0, 0, 120, 60],
                        "candidates": [], "how": "INJECTED DEFECT: unresolved target defaulted"}
            return r

        def _buggy_apply(xid):
            return True     # INJECTED DEFECT: claims an empty input shape, applies nothing

        _saved = {"resolve_target": globals()["resolve_target"],
                  "apply_empty_input_shape": globals()["apply_empty_input_shape"]}
        globals()["resolve_target"] = _buggy_resolve
        globals()["apply_empty_input_shape"] = _buggy_apply
        injected.append("unresolvable target silently defaulted to a 120x60 box at (0,0)")
        injected.append("apply_empty_input_shape() claims success without touching the X shape")
        # 3. the REAL historical bug in this file: the row regex that stopped after the relative
        #    geometry pair, so it matched no `xwininfo -root -tree` row that has ever been printed.
        _saved["TITLE_RE"] = globals()["TITLE_RE"]
        globals()["TITLE_RE"] = re.compile(
            r'^\s+(0x[0-9a-fA-F]+)\s+"(.*?)":\s+\([^)]*\)\s+'
            r'(\d+)x(\d+)\+(-?\d+)\+(-?\d+)\s*$')
        injected.append("the xwininfo row regex stopped at the relative geometry pair (the real bug "
                        "found on 2026-09-21 by a live probe)")
        # 4. the STORE-SYNC defect this pass fixes: the overlay re-derives its own position instead
        #    of reading the walk's. Injected as a guide_steps() that always says "step 1", which is
        #    exactly what a second walker reading one store produces.
        _saved["guide_steps"] = globals()["guide_steps"]

        def _buggy_guide_steps(limit=40):
            g = _saved["guide_steps"](limit)
            if g.get("ok"):
                g["start"] = 0
            return g

        globals()["guide_steps"] = _buggy_guide_steps
        injected.append("guide_steps() ignored the live walk's position and always reported step 1")

    # 0. the vocabulary is closed and exactly what we think it is -- SET EQUALITY, not presence
    want_kinds = {"window", "rect", "point"}
    if prove_fail:
        want_kinds.add("deliberately-missing-kind")
        injected.append("an extra kind added to the closed target vocabulary")
    if set(TARGET_KINDS) != want_kinds:
        fails.append(f"target-kind SET differs: expected {sorted(want_kinds)}, got {sorted(TARGET_KINDS)}")
        caught.append("target-kind set-equality")

    # 1. explicit geometry is used EXACTLY -- set equality on the tuple
    r = resolve_target({"rect": [100, 200, 300, 80]})
    if r.get("rect") != [100, 200, 300, 80] or not r.get("ok"):
        fails.append(f"explicit rect was not used verbatim: {r}")
        caught.append("rect verbatim")
    r = resolve_target({"point": [960, 540]})
    half = POINT_BOX // 2
    if r.get("rect") != [960 - half, 540 - half, POINT_BOX, POINT_BOX]:
        fails.append(f"point did not become the documented centred box: {r.get('rect')}")
        caught.append("point -> centred box")

    # 2. malformed declarations are REFUSED, and the refusal names the kind
    bad_cases = [
        ({"rect": [1, 2, 3]}, "3-number rect"),
        ({"rect": [1, 2, 0, 5]}, "zero-area rect"),
        ({"window": ""}, "empty window name"),
        ({"window": 17}, "window named by an int"),
        ({"nonsense": 1}, "unknown kind"),
        ({"rect": [1, 2, 3, 4], "point": [5, 6]}, "two kinds at once"),
        ("just a string", "a bare string"),
    ]
    accepted = [name for tgt, name in bad_cases if resolve_target(tgt).get("ok")]
    if accepted:
        fails.append(f"accepted malformed targets: {accepted}")
        caught.append("malformed target refusal")

    # 1b. ★★ THE ROW PARSER, AGAINST THE REAL `xwininfo -root -tree` SHAPES, WITH SET EQUALITY.
    #     This is the assertion that did NOT exist when the live probe found the two-pair bug. It is
    #     here now, with the buggy regex re-injected under --prove-fail, because a guard that has
    #     never seen its own bypass is a guard we are hoping about.
    rows = [
        ('     0xc00003 "KHAOSS voice seat": ("bubble.py" "Bubble.py")  338x44+1558+1132  +1558+1132',
         ("0xc00003", "KHAOSS voice seat", [1558, 1132, 338, 44])),
        ('        0xc00004 (has no name): ()  1x1+-1+-1  +1557+1131',
         ("0xc00004", None, [1557, 1131, 1, 1])),
        ('     0x1e00001 "-c": ("-c" "")  10x10+10+10  +10+10',
         ("0x1e00001", "-c", [10, 10, 10, 10])),
        # relative != absolute -- the exact shape the first regex could not see
        ('     0x800003 "rail": ("x" "Y")  400x30+0+1170  +0+1170',
         ("0x800003", "rail", [0, 1170, 400, 30])),
        ('     0x900001 "offscreen": ("x" "Y")  20x20+-100+-100  +-100+-100',
         ("0x900001", "offscreen", [-100, -100, 20, 20])),
        # ★ THE 2026-10-18 DEFECT: a WM_CLASS that CONTAINS A PARENTHESIS. Chromium publishes its
        #   profile path in the class, and the old `\([^)]*\)` stopped inside it, so a real open
        #   window matched nothing. This row would have caught it before the live probe did.
        ('     0x1a00004 "Grant Access — timed, scoped, expiring · KHAOSS - Chromium": '
         '("chromium (/tmp/pointat_chrome_profile)" "Chromium")  1200x900+80+80  +80+80',
         ("0x1a00004", "Grant Access — timed, scoped, expiring · KHAOSS - Chromium",
          [80, 80, 1200, 900])),
        ('  not a window row at all', None),
        ('xwininfo: Window id: 0x402 (the root window) (has no name)', None),
    ]
    got = [parse_tree_line(l) for l, _ in rows]
    want = [w for _, w in rows]
    if got != want:
        fails.append(f"xwininfo row parsing differs from the real shapes: got {got}")
        caught.append("xwininfo row parsing (two-pair geometry, (has no name))")

    # 1c. ★ AND THE SAME QUESTION ASKED OF THE LIVE DISPLAY: two independent readings of the window
    #     list must agree as SETS, and the list must not be empty on a display xwininfo can see.
    #     A resolver that silently sees zero windows passes every test that only asks about windows
    #     it knows are absent -- which is exactly how the two-pair bug survived the first green run.
    try:
        raw_tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                                  timeout=10).stdout
        ids_raw = set()
        for ln in raw_tree.splitlines():
            p = parse_tree_line(ln)
            if p:
                ids_raw.add(p[0])
        lw = list_windows()
        ids_lw = {w["id"] for w in lw}
        if ids_raw != ids_lw:
            fails.append(f"the two readings of the window list disagree: tree-parsed={len(ids_raw)} "
                         f"list_windows={len(ids_lw)}; only in one: "
                         f"{sorted(ids_raw ^ ids_lw)[:6]}")
            caught.append("two independent window-list readings agree")
        if not ids_lw:
            fails.append("★ list_windows() found ZERO windows on a display `xwininfo` can see -- the "
                         "resolver is blind and every window target would be reported as missing")
            caught.append("window list is not empty on a live display")
        degenerate = [w["id"] for w in lw if w["rect"][2] <= 0 or w["rect"][3] <= 0]
        if degenerate:
            fails.append(f"windows with no area were listed as targets: {degenerate[:5]}")
            caught.append("no zero-area window targets")
    except Exception as e:
        fails.append(f"UNPROVEN: could not cross-check the window list on this display: "
                     f"{e.__class__.__name__}: {e}")

    # 3. ★★ THE CORE GUARD: a window that does not exist must be REPORTED, not defaulted.
    ghost = "__NO_SUCH_WINDOW_point_to_selftest__"
    g = resolve_target({"window": ghost})
    if g.get("ok"):
        fails.append(f"resolved a window that does not exist ({ghost!r}) -> {g.get('rect')}")
        caught.append("ghost window reported")
    if not g.get("error"):
        fails.append("a failed target resolved with NO error text -- he would see a ring at (0,0) "
                     "and no explanation")
        caught.append("ghost window error text")

    # 4. the whole-plan contract: one unresolvable step fails the PLAN and is COUNTED
    script = {"id": "selftest", "title": "selftest",
              "steps": [
                  {"id": "a", "title": "a real rect", "target": {"rect": [10, 20, 30, 40]},
                   "body": "one", "hint": "h1"},
                  {"id": "b", "title": "a ghost window", "target": {"window": ghost},
                   "body": "two", "hint": "h2"},
                  {"id": "c", "title": "a point", "target": {"point": [500, 400]},
                   "body": "three", "hint": "h3"},
              ]}
    p = plan(script)
    if p["step_ids"] != ["a", "b", "c"]:
        fails.append(f"plan lost/renamed steps: {p['step_ids']}")
        caught.append("step set-equality")
    if p.get("ok"):
        fails.append("★ a plan containing an UNRESOLVABLE step reported ok=True -- this is the silent "
                     "skip the whole file exists to prevent")
        caught.append("unresolvable step fails the plan")
    if [u.get("id") for u in p["unresolved"]] != ["b"]:
        fails.append(f"unresolved set differs: {[u.get('id') for u in p['unresolved']]}")
        caught.append("unresolved counted, not dropped")
    if len(p["steps"]) != 3:
        fails.append("a step vanished from the plan -- steps must be RENDERED with their failure, "
                     "never removed")
        caught.append("step count preserved")

    # 5. step indexing refuses out of range instead of clamping (silent clamping moves him somewhere
    #    he did not ask for)
    got = clamp_refuses(script, 99)
    if not got.get("refused"):
        fails.append("--step 99 was silently clamped into range")
        caught.append("out-of-range step refused")

    # 6. the markdown authoring path parses to the SAME shape as the JSON path
    md = ("# a md walk\n"
          "## first  |  Find the thing\n"
          "target: rect 11 22 33 44\n"
          "body: do the thing\n"
          "hint: press the button\n"
          "## second  |  Now the other thing\n"
          "target: point 100 200\n"
          "body: and now this\n")
    mdp = Path("/tmp/ptt_selftest_walk.md")
    mdp.write_text(md)
    ms = load_script(mdp)
    if not ms.get("ok"):
        fails.append(f"markdown walkthrough did not parse: {ms.get('error')}")
        caught.append("markdown parse")
    else:
        mp = plan(ms)
        if [s["id"] for s in mp["steps"]] != ["first", "second"]:
            fails.append(f"markdown step ids differ: {[s['id'] for s in mp['steps']]}")
            caught.append("markdown step set-equality")
        if mp["steps"][0]["rect"] != [11, 22, 33, 44]:
            fails.append(f"markdown rect parsed as {mp['steps'][0]['rect']}")
            caught.append("markdown rect")
    mdp.unlink(missing_ok=True)

    # 7. ★★ CLICK-THROUGH IS VERIFIED AGAINST THE X SERVER, for a window we KNOW eats clicks and for
    #    the plane we actually build. Both halves are needed: the honest-negative half proves the
    #    measurement can say NO, so a YES from it means something.
    try:
        plain = Gtk.Window(title="pt selftest plain")
        plain.set_default_size(120, 80)
        plain.show_all()
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        pid_x = plain.get_window().get_xid()
        neg = verify_click_through(pid_x)
        if neg.get("click_through"):
            fails.append("★ a plain window reported click_through=True -- the measurement cannot say "
                         "NO, so its YES means nothing")
            caught.append("click-through honest-negative")
        ok_apply = apply_empty_input_shape(pid_x)
        pos = verify_click_through(pid_x)
        if not ok_apply or not pos.get("click_through"):
            fails.append(f"applying the empty input shape did not make the window click-through: {pos}")
            caught.append("empty input shape applied+verified")
        # and put it back so the negation test above is not accidentally satisfied by state
        if shape_rect_count(pid_x, _SHAPE_INPUT) != 0:
            fails.append("the empty input shape did not survive one round trip")
            caught.append("input shape survives")
        plain.destroy()
    except Exception as e:
        fails.append(f"UNPROVEN: could not run the click-through check on this display: "
                     f"{e.__class__.__name__}: {e}")

    # 8. the escape: --kill with no pidfile is a clean no-op, and a FOREIGN pid is REFUSED
    saved_pid = PIDFILE.read_text() if PIDFILE.is_file() else None
    try:
        PIDFILE.unlink(missing_ok=True)
        k = kill_running()
        if not (k.get("ok") and not k.get("killed")):
            fails.append(f"--kill with no pidfile was not a clean no-op: {k}")
            caught.append("kill no-op")
        PIDFILE.write_text("1\n")           # pid 1 is init; definitely not this program
        k2 = kill_running()
        if k2.get("killed"):
            fails.append("★ --kill SIGNALLED A PROCESS THAT IS NOT THIS PROGRAM")
            caught.append("kill identity check")
        if "point_to.py" not in str(k2.get("say")):
            fails.append(f"--kill refusal did not say why: {k2}")
            caught.append("kill refusal reason")
    finally:
        PIDFILE.unlink(missing_ok=True)
        if saved_pid is not None:
            PIDFILE.write_text(saved_pid)

    # 9. archive-before-write: our own file must be copied, never clobbered, and keep-20 must hold
    try:
        target = Path("/tmp/ptt_selftest_store.json")
        target.write_text('{"v":0}')
        archive_before_write(target)
        baks = sorted(target.parent.glob("ptt_selftest_store.json.bak.*"))
        if not baks:
            fails.append("archive_before_write left NO backup -- overwrite would have destroyed it")
            caught.append("backup before write")
        for f in baks:
            f.unlink()
        target.unlink(missing_ok=True)
    except Exception as e:
        fails.append(f"archive check blew up: {e}")

    # 10. ★★★ ONE STORE, ONE WALKER. `guide_steps()` must project the store's CURRENT position, and
    #     advancing the store must move what the overlay would show. No GTK window is opened: this
    #     tests the projection, which is the half the two-walker defect lived in.
    try:
        import hashlib as _hh
        import guide_focus as _gf
        live_state = Path(_gf.STATE)
        live_hash = _hh.md5(live_state.read_bytes()).hexdigest() if live_state.is_file() else None
        _saved_state = _gf.STATE
        tmp_state = Path("/tmp/ptt_selftest_guide_focus.json")
        try:
            _gf.STATE = tmp_state
            tmp_state.write_text(json.dumps({
                "steps": [
                    {"kind": "none", "target": "", "say": "one",
                     "title": "one", "screen": {"rect": [10, 20, 100, 40]}},
                    {"kind": "none", "target": "", "say": "two",
                     "title": "two", "screen": {"rect": [110, 220, 100, 40]}}],
                "i": 0, "by": "selftest", "why": "",
                "at": "2026-01-01T00:00:00-0700", "last_at": "2026-01-01T00:00:00-0700",
                "done": False}))
            g0 = guide_steps()
            if not g0.get("ok") or g0.get("start") != 0 or len(g0.get("steps") or []) != 2:
                fails.append(f"guide_steps did not project step 1 of 2: start={g0.get('start')} "
                             f"steps={len(g0.get('steps') or [])} err={g0.get('error')}")
            else:
                caught.append("guide_steps projects the store position")
            adv = _gf.advance()
            g1 = guide_steps()
            if not adv.get("ok") or g1.get("start") != 1:
                fails.append(f"the store advanced to step 2 but guide_steps still says step "
                             f"{g1.get('start')} -- the overlay and the board are reading one store "
                             f"as two walkers")
            else:
                caught.append("advancing the store moves what the overlay would show")
        finally:
            _gf.STATE = _saved_state
            tmp_state.unlink(missing_ok=True)
        now_hash = _hh.md5(live_state.read_bytes()).hexdigest() if live_state.is_file() else None
        if now_hash != live_hash:
            fails.append("★ the LIVE guide store changed during the self-test -- it was compared by "
                         "hash and must be identical")
    except Exception as e:  # noqa: BLE001
        fails.append(f"UNPROVEN: could not test the guide projection: {e.__class__.__name__}: {e}")

    # ★ THE INJECTION IS PUT BACK before anything else reports, so a prove-fail run cannot leave the
    #   module broken for a later call in the same process.
    for _k, _v in _saved.items():
        globals()[_k] = _v


    # ★★★ THE DRAW PATH MUST BE EXECUTED BY THE SUITE, AND IT WAS NOT. `_on_draw` is where the
    #     user-visible thing is actually painted, and this file's self-test never called it once —
    #     so `NameError: name 'pad' is not defined` (a leftover from the refactor that moved the
    #     padding into `ring_rect`) sat in the arrow block and killed EVERY `--guide` run while the
    #     suite reported PASS. The sub-agent tested `--script`; the board uses `--guide`; the draw
    #     path is shared and neither suite drove it.
    # ★ It needs `card_rect` SET to reach the buggy branch, so the guard sets it deliberately: a test
    #   that never enters the block the bug is in cannot fail on it.
    class _RecCairo:
        """Records the calls; any missing method is a FAILURE, not an AttributeError mid-suite."""
        def __init__(self): self.calls = []
        def __getattr__(self, name):
            def _c(*a, **k):
                self.calls.append((name, a))
                return None
            return _c
        def text_extents(self, text):
            self.calls.append(("text_extents", (text,)))
            class _E:
                width = len(str(text)) * 7.0
                height = 14.0
            return _E()
    class _Target:
        steps = [{"id": "guard", "rect": [100, 100, 200, 120], "title": "t",
                  "body": "b", "hint": "h", "screen": None,
                  # ★ THE DIAGRAM IS PART OF THE DRAW PATH. An illustration in a step used to be
                  #   dropped before the overlay saw it; this step proves the renderer is reached.
                  "illustration": {"frame": "target", "items": [
                      {"kind": "label", "at": [10, 20], "text": "Read the card", "color": "blue"},
                      {"kind": "arrow", "from": [10, 50], "to": [100, 100], "color": "gold"},
                      {"kind": "box", "rect": [0, 0, 100, 60], "text": "target", "color": "green"},
                      {"kind": "dot", "at": [150, 10], "r": 6, "text": "confirm", "color": "cream"}]}}]
        screen = [0, 0, 1920, 1200]
        i = 0
        card_rect = [40, 900, 430, 200]      # ★ set, so the ARROW branch runs
        record = {}
        _on_draw = Overlay._on_draw      # ★ the class is Overlay, not PointTo
        _draw_illustration = Overlay._draw_illustration
        _illus_text = Overlay._illus_text
        _illus_arrow = Overlay._illus_arrow
        _illus_rgba = Overlay._illus_rgba
    _rc = _RecCairo()
    try:
        _Target()._on_draw(None, _rc)
        _names = [c[0] for c in _rc.calls]
        for _need in ("rectangle", "fill", "stroke"):
            if _need not in _names:
                fails.append(f"the draw path never called {_need!r} — it rendered nothing meaningful")
        if not any(n == "move_to" for n in _names):
            fails.append("the draw path drew no arrow/line geometry — card_rect was set and the "
                         "arrow branch should have run")
        if "show_text" not in _names:
            fails.append("the draw path never rendered the step's ILLUSTRATION text — the diagram "
                         "is in the step but the renderer did not reach it")
    except NameError as _e:
        fails.append(f"★ THE DRAW PATH RAISES NameError: {_e} — every guided walk dies here, and the "
                     f"suite would have said PASS. This is the `pad` defect class.")
    except Exception as _e:
        fails.append(f"the draw path raised {_e.__class__.__name__}: {_e}")

    # ★★★ A NON-ASCII SANITISER, BECAUSE DETECTING THIS AFTERWARDS HAS NOW FAILED ME THREE TIMES.
    #     Each time the module became UNIMPORTABLE -- and a guard inside a module that cannot be
    #     imported cannot run. This one reports the offence precisely enough to fix in a single pass,
    #     and it is the check I should have written the first time.
    try:
        _raw = open(__file__, "rb").read()
        _i = _raw.find(b'CSS = b"""')
        _j = _raw.find(b'"""', _i + 10) if _i >= 0 else -1
        if _i >= 0 and _j > _i:
            _bad = []
            for _ln, _l in enumerate(_raw[_i:_j].split(b"\n"), 1):
                try:
                    _l.decode("ascii")
                except UnicodeDecodeError:
                    _bad.append([_ln, _l[:44].decode("utf-8", "replace")])
            if _bad:
                fails.append(f"* {len(_bad)} NON-ASCII line(s) inside the CSS bytes literal "
                             f"{_bad[:2]} -- a SyntaxError at import; the file cannot load at all")
    except Exception:  # noqa: BLE001
        pass

    # ★★★ THE FILE MUST COMPILE, ASSERTED RATHER THAN ASSUMED. I hit this EXACT defect TWICE today in
    #     two files: one non-ASCII character inside a `b"""` CSS literal is a SyntaxError at import and
    #     it kills the whole module while every other check still prints PASS. The first time it made
    #     systemd restart the voice bubble seven times; the second time -- this file, hours later -- it
    #     took the overlay down. A guard that inspects a different thing than the one that ships is not
    #     a guard, so this compiles THIS file.
    try:
        import py_compile as _pc2
        _pc2.compile(__file__, doraise=True)
    except Exception as _e2:
        fails.append(f"* THIS FILE DOES NOT COMPILE -- nothing can run at all: {_e2}")

    for f in fails:
        print(f"  \u2717 {f}")
    if prove_fail:
        print(f"  ── PROVE-FAIL: injected {len(injected)} defect(s), each one REAL CODE swapped into "
              f"the live module:")
        for _i in injected:
            print(f"       INJECTED: {_i}")
        print(f"  ── caught by {len(caught)} assertion group(s): {', '.join(caught) or 'NOTHING'}")
        if fails:
            print(f"  \u2713 SUITE WENT RED ({len(fails)} failure(s)) -- exit 1 BY DESIGN. A red run here "
                  f"is the evidence the guard is not vacuous.")
        else:
            print("  \u2717 THE SUITE PASSED WITH THE DEFECTS INJECTED -- IT IS WORTHLESS. Fix the "
                  "self-test before trusting any green run from it.")
        return 1 if fails else 2
    if not fails:
        print("  \u2713 self_test: resolves windows/rects/points, refuses malformed targets, FAILS the "
              "plan on a target that points at nothing, refuses an out-of-range step, makes a window "
              "genuinely click-through and MEASURES it, and never signals a foreign pid.")
        print(f"  PROVE-FAIL: {Path(__file__).name} --self-test --prove-fail   (must exit 1)")
    else:
        print(f"  \u2717 self_test FAILED ({len(fails)} assertion(s))")
    return 1 if fails else 0


def clamp_refuses(script: dict, n: int) -> dict:
    """`--step N` out of range must REFUSE. Silently clamping to the last step means the walkthrough
    the author wrote and the walkthrough he is shown are different documents."""
    tot = len(script.get("steps") or [])
    if not (1 <= n <= tot):
        return {"refused": True, "say": f"step {n} is outside 1..{tot}"}
    return {"refused": False, "step": n}


def ring_bbox(png, colour=RING_RGB, fuzz=15):
    """The bounding box of the ring's pixels in a capture, read out with ImageMagick, not with eyes.

    ★★★ THIS IS THE ASSERTION THAT CONNECTS THE RESOLVER TO THE PIXELS. Everything upstream can be
    right and the window can still draw the ring in the wrong place -- an off-by-a-monitor bug, a
    stale rect, a plane that is not screen-sized (which happened: 200x200+0+29 on a 1920x1200 screen
    on the first build). This measures where the accent colour ACTUALLY landed."""
    try:
        out = subprocess.run(["convert", str(png), "-fuzz", f"{fuzz}%", "-fill", "white",
                              "-opaque", colour, "-fill", "black", "+opaque", "white",
                              "-format", "%@", "info:"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    m = re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", out)
    if not m:
        return {"ok": False, "error": f"no ring pixels found in the capture (convert said {out!r})"}
    w, h, x, y = (int(g) for g in m.groups())
    return {"ok": True, "bbox": [x, y, w, h]}


def verify_render(tolerance: int = 4, prove_fail: bool = False) -> dict:
    """Capture the LIVE plane window and check the ring landed on the resolved rectangle.

    ★ UNPROVEN is its own answer: with no running overlay, or no display, this returns ok=False and
    says which -- it never reports a pass it did not measure."""
    if not REPORT.is_file():
        return {"ok": False, "unproven": True,
                "reason": "no overlay report on disk -- nothing is running to verify"}
    try:
        rep = json.loads(REPORT.read_text())
    except Exception as e:
        return {"ok": False, "unproven": True, "reason": f"report unreadable: {e}"}
    xid = (rep.get("guard") or {}).get("plane_xid")
    if not xid:
        return {"ok": False, "unproven": True, "reason": "the overlay never reported a plane window id"}
    shown = [s for s in (rep.get("shown") or []) if s.get("rect")]
    if not shown:
        return {"ok": False, "unproven": True, "reason": "no resolved step has been rendered yet"}
    step = shown[-1]
    r = step["rect"]
    if not (rep.get("guard") or {}).get("click_through"):
        return {"ok": False, "unproven": False,
                "reason": "REFUSING to call this verified: the plane is not click-through, so what is "
                          "on screen is a modal, not the overlay this file claims",
                "guard": rep.get("guard")}
    try:
        cap = subprocess.run(["import", "-window", xid, str(VERIFY_PNG)], capture_output=True,
                             text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "unproven": True, "reason": f"could not capture the plane: {e}"}
    if cap.returncode != 0 or not VERIFY_PNG.is_file():
        return {"ok": False, "unproven": True,
                "reason": f"`import -window {xid}` failed: {cap.stderr.strip()[:200]}",
                "note": "a full-screen ROOT grab is not available on this box at all (import -window "
                        "root -> X_GetImage BadMatch under XWayland); per-window capture is the only "
                        "working path and it is used here"}
    got = ring_bbox(VERIFY_PNG)
    if not got.get("ok"):
        return {"ok": False, "unproven": False, "reason": got.get("error"), "captured": str(VERIFY_PNG)}
    expect, offscreen = ring_rect(r, rep.get("screen") or _screen_geometry())
    note = ""
    if prove_fail:
        expect = [expect[0] + 40, expect[1] + 40, expect[2], expect[3]]
        note = ("PROVE-FAIL: the expected rectangle was moved 40px down-right before comparing, so a "
                "suite that always says OK is shown to say OK to a wrong place")
    deltas = [abs(a - b) for a, b in zip(got["bbox"], expect)]
    ok = all(d <= tolerance for d in deltas)
    # ★★ THE FUNCTIONAL HALF OF THE CLICK-THROUGH CLAIM. The X shape measurement proves the input
    #    region is EMPTY; this proves the consequence -- with the plane mapped ABOVE the target window,
    #    X must still report the window UNDERNEATH as the one holding the pointer. If X ever answers
    #    with the plane, the plane is eating that point and this is a modal, not a guide.
    probe = {"tried": False}
    try:
        cx, cy = r[0] + r[2] // 2, r[1] + r[3] // 2
        subprocess.run(["xdotool", "mousemove", str(cx), str(cy)], capture_output=True, timeout=10)
        time.sleep(0.35)
        loc = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, text=True,
                             timeout=10).stdout
        # ★ ASK X WHO HAS THE POINTER, DO NOT RE-DERIVE IT BY OVERLAP. The first version scanned every
        #   window whose rectangle contained the point and reported the first hit -- which was the
        #   1920x1200 `mutter guard window`, not the voice seat X actually named. The probe's own
        #   instrument had the defect it was testing for.
        plane_xid = int(xid, 16)
        mm = re.search(r"window:(\d+)", loc)
        named_xid = int(mm.group(1)) if mm else None
        by_id = {int(w["id"], 16): w for w in list_windows()}
        under = by_id.get(named_xid) or {}
        probe = {"tried": True, "at": [cx, cy], "x_says": loc.strip(),
                 "answered_with_the_plane": named_xid == plane_xid,
                 "x_named_window": (hex(named_xid) if named_xid is not None else None),
                 "window_underneath": under.get("title") or under.get("id") or "an unlisted window",
                 "note": "X reported the window UNDER the plane as holding the pointer, which is what "
                         "an empty input region means in practice"}
        if prove_fail:
            probe["answered_with_the_plane"] = True
            probe["note"] = "PROVE-FAIL: forced to claim the plane held the pointer"
        if probe["answered_with_the_plane"]:
            ok = False
            probe["note"] = ("★ X says the PLANE holds the pointer at that point -- clicks do NOT pass "
                             "through there. This is a modal, not an overlay.")
    except Exception as e:
        probe = {"tried": False, "reason": f"{e.__class__.__name__}: {e}",
                 "note": "UNPROVEN: the pass-through probe did not run; the shape measurement above "
                         "still stands on its own, but the functional half was not measured"}
    return {"ok": ok, "prove_fail": bool(prove_fail), "step": step.get("step"), "id": step.get("id"),
            "target_offscreen": offscreen,
            "ring_clamped_to_screen": offscreen,
            "offscreen_note": ("★ the ring had to be CLAMPED to the screen edge: the target sits flush "
                               "with it, so the padded ring would have gone off-screen and lost a "
                               "side. Reported here rather than left looking like a broken ring."
                               if offscreen else ""),
            "pass_through_probe": probe,
            "resolved_rect": r, "expected_ring_bbox": expect, "measured_ring_bbox": got["bbox"],
            "deltas_px": deltas, "tolerance_px": tolerance, "capture": str(VERIFY_PNG),
            "measured_from": "the plane window's own pixels via `import -window <plane xid>`",
            "note": note or ("the ring landed on the resolved rectangle" if ok else
                             "★ THE RING IS NOT WHERE THE RESOLVER SAID THE TARGET IS")}


def status() -> dict:
    """What is on screen RIGHT NOW -- read from the report the overlay writes, not from memory."""
    d = {}
    if REPORT.is_file():
        try:
            d = json.loads(REPORT.read_text())
        except Exception:
            d = {"error": "the overlay report is unreadable"}
    pid = running_pid()
    alive = pid is not None
    try:
        import guide_focus
        cur = guide_focus.current()
    except Exception as e:
        cur = {"error": f"{e.__class__.__name__}: {e}"}
    return {"running": alive, "pid": pid if alive else None,
            "report": d, "guide": cur,
            "measured": {"click_through": (d.get("guard") or {}).get("click_through"),
                         "input_rectangles": (d.get("guard") or {}).get("input_rectangles"),
                         "reason": (d.get("guard") or {}).get("reason")}}


# ── main ─────────────────────────────────────────────────────────────────────────────────────────
def _usage():
    return f"""jarvis/point_to.py -- the point-me-to-there protocol

  --script FILE [--step N|--auto S]   show an agent-authored walkthrough (JSON or Markdown)
  --guide                             follow the LIVE guided walk the board already owns
  --push FILE [--by WHO] [--replace]  push a walkthrough into the board's own guided walk
  --next / --back / --gotostep N      drive a RUNNING overlay
  --status                            what is on screen right now (JSON)
  --kill                              stop the running overlay (ONE pid, identity-checked)
  --self-test [--prove-fail]          the suite; --prove-fail MUST exit 1
  --hold SECONDS                      close by itself (screenshots, demos)
  --markdown-help                     the authoring format

target kinds: {json.dumps(KINDS_SAYS, indent=2)}"""


MARKDOWN_HELP = """# Authoring a walkthrough for the operator

Save it as `operator/walks/<name>.md`. Then:

    /usr/bin/python3 jarvis/point_to.py --script operator/walks/<name>.md --auto 6
    /usr/bin/python3 jarvis/point_to.py --push   operator/walks/<name>.md --by <your-seat>

## Format

    # Walk title

    ## step-id  |  Short title he reads in the card
    target: window "Settings"
    body: what to do, in plain language. Long bodies wrap.
    hint: the nudge if he is stuck

Three target forms, and ONLY these three:

    target: window "Settings"     a window, by title (case-insensitive substring) or X id
    target: window 0x1e00003      the same thing, unambiguously, by X window id
    target: rect 100 200 300 80   x y width height, real screen pixels
    target: point 960 540         a point; a 56x56 box is drawn centred on it

## The rules this format keeps

* A step with no `target:` line is REFUSED at load. A step that points at nothing is not a guide.
* A window title that matches nothing REFUSES that step, names it in `unresolved`, and FAILS the plan.
  It is never defaulted to a rectangle. If your walkthrough needs a window, launch it first.
* A title matching several windows PICKS THE LARGEST and lists every candidate in `ambiguous`. Name
  the X id when you need certainty.
* `--step N` outside 1..N refuses rather than clamping. The document you wrote is the document he sees.
* The ring is drawn exactly on the resolved rectangle. Nothing is padded by guesswork -- only the
  7px visual halo, which is chrome, not geometry.
"""


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or "-h" in a or "--help" in a:
        print(_usage())
        return 0
    if "--markdown-help" in a:
        print(MARKDOWN_HELP)
        return 0
    if "--self-test" in a or "--selftest" in a:
        return self_test("--prove-fail" in a)
    if "--verify-render" in a:
        r = verify_render(prove_fail="--prove-fail" in a)
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1
    if "--status" in a:
        print(json.dumps(status(), indent=2))
        return 0
    if "--kill" in a:
        r = kill_running()
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1
    if "--dismiss-guide" in a:
        import guide_focus
        print(json.dumps(guide_focus.dismiss(), indent=2))
        return 0

    def opt(name, default=None):
        if name in a:
            i = a.index(name)
            return a[i + 1] if i + 1 < len(a) else default
        return default

    # ── drive a RUNNING overlay ──────────────────────────────────────────────────────────────────
    for verb, cmd in (("--next", "next"), ("--back", "back"), ("--close", "close")):
        if verb in a:
            if not PIDFILE.is_file():
                print(json.dumps({"ok": False, "say": "no overlay is running (no pidfile)"}, indent=2))
                return 1
            CMD.write_text(cmd + "\n")
            print(json.dumps({"ok": True, "sent": cmd, "via": str(CMD)}, indent=2))
            return 0
    if "--gotostep" in a:
        try:
            n = int(opt("--gotostep"))
        except (TypeError, ValueError):
            print(json.dumps({"ok": False, "say": "--gotostep needs a number"}, indent=2))
            return 2
        CMD.write_text(f"goto:{n}\n")
        print(json.dumps({"ok": True, "sent": f"goto:{n}"}, indent=2))
        return 0

    # ── push into the board's own walk ───────────────────────────────────────────────────────────
    if "--push" in a:
        sc = load_script(opt("--push"))
        if not sc.get("ok"):
            print(json.dumps(sc, indent=2)); return 2
        pl = plan(sc)
        r = push_walk(pl, by=opt("--by", "an agent"), why=opt("--why", ""), replace="--replace" in a)
        r["plan_ok"] = pl["ok"]
        r["unresolved"] = pl["unresolved"]
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1

    hold = float(opt("--hold", 0) or 0) or None
    auto = float(opt("--auto", 0) or 0) or None

    # ── guide mode ───────────────────────────────────────────────────────────────────────────────
    if "--guide" in a:
        g = guide_steps()
        if not g.get("ok"):
            print(json.dumps({"ok": False, "error": g.get("error"),
                              "guide": g.get("current")}, indent=2))
            return 1
        steps = g["steps"]
        # ★ start where the board says he is, now that `steps` is the whole walk
        idx = max(0, min(int(g.get("start") or 0), max(0, len(steps) - 1)))
        return _run(steps, idx, f"guide by {g.get('by')}", hold, auto,
                    on_next=lambda s: record_back(s))

    # ── script mode ──────────────────────────────────────────────────────────────────────────────
    if "--script" in a:
        sc = load_script(opt("--script"))
        if not sc.get("ok"):
            print(json.dumps(sc, indent=2)); return 2
        pl = plan(sc)
        if "--step" in a:
            try:
                n = int(opt("--step"))
            except (TypeError, ValueError):
                print(json.dumps({"ok": False, "say": "--step needs a number"}, indent=2)); return 2
            ref = clamp_refuses(sc, n)
            if ref.get("refused"):
                print(json.dumps({"ok": False, "error": ref["say"],
                                  "n_steps": len(sc["steps"])}, indent=2))
                return 2
            idx = n - 1
        else:
            idx = 0
        if pl["unresolved"]:
            # ★ LOUD, AND STILL RENDERED. He sees the step he asked for with the failure printed in
            #   the card; we do not refuse to show him anything, and we do not hide the failure.
            print(json.dumps({"warning": f"{len(pl['unresolved'])} step(s) point at nothing on this "
                                         f"screen and are REPORTED, not skipped",
                              "unresolved": pl["unresolved"]}, indent=2), file=sys.stderr)
        return _run(pl["steps"], idx, f"{sc.get('title')}", hold, auto, on_next=None)

    print(_usage())
    return 2


def _run(steps, idx, mode, hold, auto, on_next):
    if os.environ.get("DISPLAY", "") == "":
        os.environ["DISPLAY"] = ":0"
    # ★ TEST-ONLY APPLICATION NAME. AT-SPI addresses a GTK app by its program name; a machine test
    #   that drives the real card must not confuse itself with the operator's live overlay. Default
    #   is untouched, so normal runs still appear as `point_to.py` to accessibility tools.
    if os.environ.get("KHAOSS_POINT_TO_APP_NAME"):
        try:
            GLib.set_prgname(str(os.environ["KHAOSS_POINT_TO_APP_NAME"]))
        except Exception:  # noqa: BLE001
            pass
    # ★★★ ONE OVERLAY AT A TIME, ENFORCED. MEASURED 2026-09-21: two `--guide` copies ran side by side
    #     and drew two rings and two cards on the same screen, because nothing checked. That is this
    #     estate's oldest defect shape -- two records of one thing -- except this one is painted over
    #     his desktop. `guide_focus` already enforces one guide at a time; the overlay must match it,
    #     or an agent can stack three arrows on him at once.
    live = running_pid()
    if live and live != os.getpid():
        print(json.dumps({"ok": False, "refused": True, "running_pid": live,
                          "say": f"an overlay is ALREADY running (pid {live}). One overlay at a time -- "
                                 f"two arrows on one screen is worse than none. Use --kill or --next.",
                          "how": "jarvis/point_to.py --kill"}, indent=2))
        return 3
    write_pidfile()
    REPORT.unlink(missing_ok=True)
    CMD.unlink(missing_ok=True)
    try:
        ov = Overlay(steps, idx, mode, on_next=on_next, hold=hold, auto=auto)
        rc = ov.run()
    finally:
        PIDFILE.unlink(missing_ok=True)
        CMD.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
