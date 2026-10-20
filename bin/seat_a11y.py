#!/usr/bin/env python3
"""seat_a11y.py - hands for the voice seat that use the ACCESSIBILITY bus, not input injection.

★ WHY THIS EXISTS ALONGSIDE seat_hands.py. seat_hands.py drives the seat by SIGNAL, which is exact but
  only reaches the handful of controls that have a signal handler. Some things genuinely have to be
  ACTIVATED as a user would: the model picker, the zoom buttons, the Send button. `xdotool` is not an
  option on this box -- Mutter converts synthetic input into an org.freedesktop.portal.RemoteDesktop
  consent dialog, so every click costs the operator a permission prompt.

★ AT-SPI has no such cost. It is the accessibility interface GTK publishes for screen readers: it
  enumerates the real widget tree, reports each widget's role and name, and can invoke a widget's
  declared `click` action directly. NO pointer events are synthesised, so nothing is intercepted by the
  portal. A previous agent on this estate proved the route; this is that route with verbs on it.

★ IT REFUSES RATHER THAN GUESSING, like seat_hands: if a name matches nothing it prints the candidates
  it DID see and exits 2, because "clicked the thing" and "clicked the wrong thing" look identical in a
  screenshot.

Verbs:
    list [substring]        roles/names in the seat window (optionally filtered)
    click <name>            invoke the widget's `click` action
    settext <name> <text>   put text into an editable widget (the composer)
    value <name> [0..1]     read/set a value interface (a SCROLL BAR - this is how a scroller is driven)
    extents <name>          the widget's screen rectangle, for measuring a hit area
    frame                   print the seat's frame name/role
"""
from __future__ import annotations

import sys
import time

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

# ★★★ MEASURED, 2026-10-14, AND THE CONSTANT ABOVE WAS WRONG IN A WAY THAT SILENTLY DISABLED THIS
#     WHOLE TOOL. It searched every accessible FRAME for the string `KHAOSS voice seat`, which is the
#     seat's **X11 window title** (`xwininfo` calls it exactly that). AT-SPI does not publish that:
#     the seat registers as an APPLICATION named after its own entry point, and the frame inside it
#     carries the seat's GTK title:
#
#         app 'bubble.py'  ->  frame 'Walker - Voice Assistant Agent'  (role: frame, 6 children)
#
#     Neither string contains `KHAOSS voice seat`, so `list`, `click`, `extents`, `settext` and
#     `value` ALL printed *"seat frame NOT FOUND ... is the window open?"* and exited 2 -- **while the
#     window was open and 16 other applications were publishing normally.** A driver that refuses
#     because it cannot find the thing is only honest if it would recognise the thing when it is
#     there; this one would not, and the refusal read as "the operator's seat is closed".
#   ★ SO THE MATCH IS ON THE APPLICATION, NOT ON THE TITLE. The app name is the script's own basename
#     and is stable across a retitle; the frame name is a human string the operator could change at
#     any time, and matching on it is what broke here in the first place.
#   ★ IT REFUSES IN BOTH DIRECTIONS: if MORE than one application matches, that is ambiguity and it
#     says so rather than picking the first -- two seats on screen is a state where clicking the
#     wrong one is worse than not clicking.
SEAT_APP = "bubble.py"
SEAT = "KHAOSS voice seat"          # kept: the X11 title, still used by `frame` for cross-checking


def _walk(node, depth=0):
    yield node, depth
    try:
        n = node.get_child_count()
    except Exception:  # noqa: BLE001
        return
    for i in range(n):
        try:
            c = node.get_child_at_index(i)
        except Exception:  # noqa: BLE001
            continue
        if c is not None:
            yield from _walk(c, depth + 1)


def _text(node):
    for getter in ("get_name", "get_description"):
        try:
            v = getattr(node, getter)()
            if v:
                return str(v)
        except Exception:  # noqa: BLE001
            continue
    return ""


def _role(node):
    try:
        return str(node.get_role_name())
    except Exception:  # noqa: BLE001
        return "?"


def seat_nodes():
    """Every accessible node inside the seat's frame, in tree order."""
    Atspi.init()
    desk = Atspi.get_desktop(0)
    # ── FIND THE APPLICATION FIRST (see `SEAT_APP`). The frame's own name is a title, not an identity.
    apps = []
    for i in range(desk.get_child_count()):
        try:
            a = desk.get_child_at_index(i)
        except Exception:
            continue
        try:
            if _role(a) == "application" and SEAT_APP.lower() in _text(a).lower():
                apps.append(a)
        except Exception:
            continue
    if len(apps) > 1:
        print(f"REFUSED: {len(apps)} applications match {SEAT_APP!r}; refusing to guess which seat "
              f"is the operator's", file=sys.stderr)
        return []
    frames = []
    if apps:
        # ── `_walk`, NOT `for f in apps[0]`: an `Atspi.Accessible` is not promised to be iterable, and
        #    the generator three functions up is the file's own one way of walking a node.
        for f, _d in _walk(apps[0]):
            if _role(f) == "frame":
                frames.append(f)
    if not frames:
        # ── SECOND CHANCE, AND ONLY AS A FALLBACK: the window TITLE, which is what this tool used to
        #    match alone. Kept so a seat running under a different entry point is still reachable, and
        #    it can only ever ADD a frame the app-name path missed.
        for node, _d in _walk(desk):
            if _role(node) == "frame" and SEAT.lower() in _text(node).lower():
                frames.append(node)
    if not frames:
        print(f"seat frame NOT FOUND (looked for application {SEAT_APP!r}, then a frame named like "
              f"{SEAT!r}); is the window open?", file=sys.stderr)
        return []
    out = []
    for f in frames:
        out.extend(n for n, _d in _walk(f))
    return out


def find(name: str):
    """Nodes matching `name` (case-insensitive) by NAME or by ROLE, most specific first.

    ★ ROLE MATTERS TOO, AND FOR A MEASURED REASON. A GtkScrollbar's accessible name is EMPTY -- it is
    identified only by its role. Matching on name alone therefore made the scroll bar unaddressable,
    which is exactly the control this tool needs to drive to prove the routing dropdown scrolls.
    """
    needle = name.lower()
    hits = [n for n in seat_nodes()
            if needle in _text(n).lower() or needle in _role(n).lower()]
    hits.sort(key=lambda n: (0 if _text(n).lower() == needle else 1, len(_text(n))))
    return hits


def do_click(node) -> tuple[bool, str]:
    try:
        action = node.get_action()
    except Exception as e:  # noqa: BLE001
        return False, f"no action interface: {e}"
    if action is None:
        return False, "no action interface"
    try:
        n = action.get_n_action()
    except Exception:  # noqa: BLE001
        n = 0
    names = []
    for i in range(n):
        try:
            names.append(str(action.get_action_name(i)))
        except Exception:  # noqa: BLE001
            names.append("?")
    idx = 0
    for i, nm in enumerate(names):
        if nm.lower() in ("click", "press", "activate", "toggle"):
            idx = i
            break
    try:
        ok = bool(action.do_action(idx))
    except Exception as e:  # noqa: BLE001
        return False, f"do_action({idx}) raised {e.__class__.__name__}: {e}"
    return ok, f"action[{idx}]={names[idx] if names else '?'} -> {ok}"


def do_settext(node, text: str) -> tuple[bool, str]:
    iface = None
    for getter in ("get_editable_text", "get_text"):
        try:
            cand = getattr(node, getter)()
            if cand is not None and hasattr(cand, "set_text_contents"):
                iface = cand
                break
        except Exception:  # noqa: BLE001
            continue
    if iface is None:
        return False, "no editable-text interface on this node"
    try:
        iface.set_text_contents(text)
        return True, f"set text ({len(text)} chars)"
    except Exception as e:  # noqa: BLE001
        return False, f"set_text_contents raised {e.__class__.__name__}: {e}"


def value_iface(node):
    """The Atspi.Value interface of a node, or None. This is what a SCROLL BAR exposes, so it is how
    a scroller is driven without a pointer: set the value, and the view moves."""
    try:
        return node.get_value()
    except Exception:  # noqa: BLE001
        return None


def do_setvalue(node, frac: float | None) -> tuple[bool, str]:
    v = value_iface(node)
    if v is None:
        return False, "no value interface (not a scroll bar / slider)"
    try:
        cur, mn, mx = v.get_current_value(), v.get_minimum_value(), v.get_maximum_value()
    except Exception as e:  # noqa: BLE001
        return False, f"reading the value raised {e.__class__.__name__}: {e}"
    if frac is None:
        return True, f"current={cur:.1f} min={mn:.1f} max={mx:.1f}"
    target = mn + (mx - mn) * max(0.0, min(1.0, frac))
    try:
        v.set_current_value(target)
    except Exception as e:  # noqa: BLE001
        return False, f"set_current_value raised {e.__class__.__name__}: {e}"
    time.sleep(0.3)
    return True, f"set {cur:.1f} -> {v.get_current_value():.1f} (min {mn:.1f} max {mx:.1f})"


def main() -> int:
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    v = a[0]
    if v == "frame":
        nodes = seat_nodes()
        if not nodes:
            return 2
        print("  seat frame found;", len(nodes), "accessible nodes")
        return 0
    if v == "list":
        needle = (a[1] if len(a) > 1 else "").lower()
        nodes = seat_nodes()
        if not nodes:
            return 2
        shown = 0
        for n in nodes:
            t = _text(n)
            if needle and needle not in t.lower():
                continue
            print(f"  {_role(n):<14} {t!r}")
            shown += 1
        print(f"  ({shown} of {len(nodes)} nodes" + (f" matching {needle!r}" if needle else "") + ")")
        return 0
    if v == "click":
        if len(a) < 2:
            print("usage: click <name-substring>", file=sys.stderr)
            return 2
        hits = find(a[1])
        if not hits:
            print(f"  REFUSED: nothing named {a[1]!r} in the seat", file=sys.stderr)
            for n in seat_nodes()[:60]:
                print(f"    saw {_role(n):<12} {_text(n)!r}", file=sys.stderr)
            return 2
        ok, why = do_click(hits[0])
        print(f"  click {_text(hits[0])!r} ({_role(hits[0])}): {why}")
        time.sleep(0.6)
        return 0 if ok else 1
    if v == "settext":
        if len(a) < 3:
            print("usage: settext <name-substring> <text>", file=sys.stderr)
            return 2
        hits = find(a[1])
        if not hits:
            print(f"  REFUSED: nothing named {a[1]!r} in the seat", file=sys.stderr)
            return 2
        ok, why = do_settext(hits[0], a[2])
        print(f"  settext {_text(hits[0])!r} ({_role(hits[0])}): {why}")
        return 0 if ok else 1
    if v == "value":
        if len(a) < 2:
            print("usage: value <role-or-name-substring> [fraction 0..1]", file=sys.stderr)
            return 2
        frac = float(a[2]) if len(a) > 2 else None
        hits = find(a[1])
        if not hits:
            print(f"  REFUSED: nothing named {a[1]!r} in the seat", file=sys.stderr)
            return 2
        rc = 0
        for n in hits[:8]:
            ok, why = do_setvalue(n, frac)
            print(f"  value {_role(n)} {_text(n)!r}: {why}")
            if not ok:
                rc = 1
        return rc
    if v == "extents":
        if len(a) < 2:
            print("usage: extents <name-substring>", file=sys.stderr)
            return 2
        hits = find(a[1])
        if not hits:
            print(f"  REFUSED: nothing named {a[1]!r} in the seat", file=sys.stderr)
            return 2
        for n in hits[:5]:
            try:
                e = n.get_extents(Atspi.CoordType.SCREEN)
                print(f"  {_role(n):<12} {_text(n)!r}: {e.width}x{e.height}+{e.x}+{e.y}")
            except Exception as ex:  # noqa: BLE001
                print(f"  {_role(n)} {_text(n)!r}: extents failed {ex}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
