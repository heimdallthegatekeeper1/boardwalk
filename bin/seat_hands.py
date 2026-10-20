#!/usr/bin/env python3
"""seat_hands.py — HANDS for the voice seat: drive and photograph it without a human at the keyboard.

★ WHY (operator, 2026-10-01): *"Make some hands to drive it if you need. You have full access."*

★ THE PROBLEM IT SOLVES, MEASURED. Two full rounds were spent unable to verify UI changes because
  `xdotool` clicks on the collapsed pill did **not** expand it — four clicks at four x-positions, all
  no-ops. The window stayed 338x44. Meanwhile an unverified UI change is exactly the thing this estate
  forbids reporting as done. The answer was already in the app: `bubble.py` handles
  **SIGUSR1 -> expand(True)** and **SIGUSR2 -> expand(False)**, the latter explicitly added for
  *"testable collapse, independent of click accuracy"*. So drive it by SIGNAL, and photograph it.

★ ONE TOOL, FOUR VERBS, so a change can be driven and seen in one line each:
    ./seat_hands.py open            expand the seat (SIGUSR1, falls back to the daemon route)
    ./seat_hands.py close           collapse it (SIGUSR2)
    ./seat_hands.py geom            print WxH+X+Y so click coordinates can be computed honestly
    ./seat_hands.py shot out.png    capture the seat window itself (-w = whole screen)
    ./seat_hands.py click X Y       click at absolute screen coordinates (xdotool)
    ./seat_hands.py clickrel X Y    click at coordinates RELATIVE to the seat's top-left
    ./seat_hands.py open+shot out.png   the common case, in one call

★ It REFUSES rather than guessing: if the seat window is not found it says so and exits 2, because a
  screenshot of the wrong window looks like success.
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import time

PIDFILE = pathlib.Path("/tmp/jarvis_bubble.pid")
DAEMON = os.environ.get("KHAOSS_JARVIS", "http://127.0.0.1:8771")
TITLE = "KHAOSS voice seat"
# ★★★ THE WINDOW IS FOUND BY ITS WM_CLASS FIRST, AND BY TITLE ONLY AS A FALLBACK — 2026-10-10.
#     A34 renamed the seat's window to `Walker — Voice Assistant Agent` (the operator asked for exactly
#     those words), and this file was matching the literal string `KHAOSS voice seat`. MEASURED: after
#     the rename `geom` printed "seat window NOT FOUND" while the seat was running and its window was on
#     screen — every screenshot and every click-driven verb would have refused, which is the same
#     "present but unreachable" failure this estate keeps paying for. A TITLE is a LABEL and labels
#     change; the class is the process. `("bubble.py" "Bubble.py")` is what the seat's window carries on
#     this box and what no other window does (the 10x10 helper window carries `("bubble.py" "")`).
TITLE_CLASS = '("bubble.py" "Bubble.py")'
TITLE_MATCH = (TITLE.lower(), "walker \u2014 voice assistant agent")


def _display_env():
    """The seat runs on the operator's X session; borrow ITS display and auth.

    ★★★ 2026-10-01 — THE FIRST VERSION GLOBBED FOR THE AUTH FILE AND GOT THE WRONG ONE. It built
        `XAUTHORITY=~/.Xauthority` (which EXISTS, so the check passed) while the real cookie is
        `/run/user/<uid>/.mutter-Xwaylandauth.<suffix>` — a name that changes EVERY LOGIN. `xwininfo` then
        failed with *"Authorization required, but no authorization protocol specified"* and the tool
        reported "seat window NOT FOUND" while the seat was running perfectly. A wrong-but-existing path
        is the worst kind: it passes every existence check and fails only at use.
    ★ SO ASK THE PROCESS, NOT THE FILESYSTEM. The running bubble already holds the correct DISPLAY and
      XAUTHORITY in its own environment, so read `/proc/<pid>/environ`. That cannot drift at the next
      login, cannot pick a stale sibling, and needs no guessing. The glob is kept only as a fallback for
      the case where the seat is not running yet.
    """
    env = dict(os.environ)
    # 1 · the seat's own environment — authoritative when it is running
    # ★★★ AND THE PID COMES FROM `_seat_pid()`, NOT FROM THE PIDFILE DIRECTLY. Measured 2026-10-09:
    #     this shell carries `XAUTHORITY=~/.Xauthority`, which EXISTS and is the WRONG cookie.
    #     While the pidfile named a live seat the block below overwrote it with the right one and every
    #     verb worked; the moment the pidfile went missing the read raised, the overwrite was skipped,
    #     and `geom` reported "seat window NOT FOUND" about a window that was on screen. Falling back to
    #     the systemd unit is what keeps the authoritative source reachable.
    try:
        pid = _seat_pid()
        if not pid:
            raise ValueError("no seat pid")
        with open(f"/proc/{pid}/environ", "rb") as f:
            for kv in f.read().split(b"\0"):
                if b"=" not in kv:
                    continue
                k, _, v = kv.partition(b"=")
                k = k.decode("utf-8", "replace")
                if k in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
                    env[k] = v.decode("utf-8", "replace")
    except (OSError, ValueError):
        pass
    # 2 · fallbacks, only if still missing
    env.setdefault("DISPLAY", ":0")
    if not env.get("XAUTHORITY"):
        cands = sorted(pathlib.Path(f"/run/user/{os.getuid()}").glob(".mutter-Xwaylandauth*"))
        if cands:
            env["XAUTHORITY"] = str(cands[-1])
    return env


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, env=_display_env(), **kw)


def _xwininfo():
    """The seat's WINDOW, scored — 2026-10-10.

    * THE WM_CLASS ALONE IS NOT AN IDENTITY ANY MORE. The seat now owns a second `Gtk.Window` of type
      POPUP (the tooltip overlay, `#seattip`), and it carries the SAME `("bubble.py" "Bubble.py")`
      class as the main window. MEASURED: after the class-only version of this function, `geom`
      reported a 330x169+1594+969 window — the TOOLTIP — as the seat, and every screenshot would have
      photographed a popup. So the score is: a window whose TITLE is one of the seat's titles AND
      whose class is the seat's class wins; failing that, a titled window; failing that, the class
      match with the largest area (the pill is 372x44, the popup is smaller, the expanded view is
      larger). The title is checked case-insensitively against every title the seat is known by.
    """
    r = _run(["xwininfo", "-root", "-tree"])
    best = None
    for line in r.stdout.splitlines():
        low = line.lower()
        by_class = TITLE_CLASS.lower() in low
        by_title = any(t in low for t in TITLE_MATCH)
        if not (by_class or by_title):
            continue
        parts = line.split()
        wid = next((p for p in parts if p.startswith("0x")), None)
        geom = next((p for p in parts if "x" in p and "+" in p and p[0].isdigit()), None)
        if not wid or not geom:
            continue
        try:
            _wh = geom.split("+")[0]
            w, h = (int(v) for v in _wh.split("x"))
        except Exception:  # noqa: BLE001
            continue
        rank = 2 if (by_class and by_title) else (1 if by_title else 0)
        score = (rank, w * h)
        if best is None or score > best[0]:
            best = (score, wid, geom)
    return (best[1], best[2]) if best else (None, None)


def _seat_pid():
    """The seat's pid, from the pidfile when it is there and from systemd when it is not.

    ★★★ THE PIDFILE GOES MISSING AND THAT MADE THIS TOOL LIE. Measured 2026-10-09: after a
        `systemctl --user restart`, `/tmp/jarvis_bubble.pid` was absent while the seat was running
        perfectly and its window was on screen -- so `geom` reported "seat window NOT FOUND" and every
        signal-driven verb refused. `bubble.py` writes the file only when it wins the singleton check
        and REMOVES it in `on_quit`, so a restart that races a dying predecessor can leave it gone even
        though the new process is alive.
    ★ The unit is the identity (this estate's own rule for the seat's processes), so ask it. The
        pidfile is tried first only because it is what the app's own signal handlers are keyed to.
    """
    try:
        pid = int(PIDFILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        pass
    try:
        r = subprocess.run(["systemctl", "--user", "show", "khaoss-jarvis-bubble", "-p", "MainPID",
                            "--value"], capture_output=True, text=True, timeout=10)
        pid = int((r.stdout or "0").strip() or 0)
        if pid > 1:
            try:                       # heal the hint for anything else that reads it
                PIDFILE.write_text(str(pid))
            except OSError:
                pass
            return pid
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def geom():
    wid, g = _xwininfo()
    if not wid:
        print("seat window NOT FOUND", file=sys.stderr)
        return None
    return wid, g


def _parse_geom(g):
    try:
        wh, x, y = g.split("+")
        w, h = wh.split("x")
        return int(w), int(h), int(x), int(y)
    except Exception:  # noqa: BLE001
        return None


def open_seat():
    """SIGUSR1 is the app's OWN raise path, so this is what a click would have done."""
    pid = _seat_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
            time.sleep(0.8)
            return f"raised pid={pid} by SIGUSR1"
        except OSError:
            pass
    try:
        import json
        import urllib.request
        req = urllib.request.Request(DAEMON + "/bubble", data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        time.sleep(1.2)
        return f"daemon {d.get('action')} pid={d.get('pid')}"
    except Exception as e:  # noqa: BLE001
        return f"FAILED: {type(e).__name__}: {e}"


def signal_page(name):
    """Open a PAGE or DRIVE A CONTROL by signal, never by clicking.

    ★ WHY A SIGNAL AND NOT A CLICK: `xdotool` input injection is converted by Mutter into an
      `org.freedesktop.portal.RemoteDesktop` consent dialog on this box, so a click costs the operator a
      permission prompt. `bubble.py` handles SIGRTMIN -> routing grid, +1 -> settings, +2 -> access,
      +3 -> the Destination dropdown, +4/+5/+6 -> zoom in/out/reset, +7 -> prime, +8 -> brain stop,
      +9 -> the lead-by-default toggle, +10 -> the COMPREHENSIVE model picker.
    ★ A CONTROL THAT CANNOT BE DRIVEN IS A CONTROL THAT CANNOT BE REPORTED. Each verb below exists so
      that the surface it names can be photographed in its POST-PRESS state rather than described.
    """
    SIG = {"grid": signal.SIGRTMIN, "settings": signal.SIGRTMIN + 1,
           "access": signal.SIGRTMIN + 2, "menu": signal.SIGRTMIN + 3,
           "zoomin": signal.SIGRTMIN + 4, "zoomout": signal.SIGRTMIN + 5,
           "zoomreset": signal.SIGRTMIN + 6, "prime": signal.SIGRTMIN + 7,
           "brainstop": signal.SIGRTMIN + 8, "leadtoggle": signal.SIGRTMIN + 9,
           # ★ the COMPREHENSIVE model picker (every installed model, sectioned by status)
           "models": signal.SIGRTMIN + 10,
           # ★★ THE POSTURE ROW'S THREE SURFACES, 2026-10-10 -- Agent Rules / Capture pages and the
           #    autonomy ladder's cycle. +26/+27/+28 are the plan's own free slots; +30 is NOT reused
           #    (that is the command file) and nothing above SIGRTMAX is touched.
           "agentrules": signal.SIGRTMIN + 26, "capture": signal.SIGRTMIN + 27,
           "autonomy": signal.SIGRTMIN + 28}.get(name)
    if SIG is None:
        return (f"unknown page {name!r}; have grid, settings, access, menu, zoomin, zoomout, "
                f"zoomreset, prime, brainstop, leadtoggle, models, agentrules, capture, autonomy")
    pid = _seat_pid()
    if not pid:
        return "FAILED: no seat pid (pidfile missing and the unit has no MainPID)"
    try:
        os.kill(pid, SIG)
        time.sleep(1.2)
        return f"drove {name} by signal {SIG} (pid {pid})"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {e}"


def close_seat():
    pid = _seat_pid()
    if not pid:
        return "FAILED: no seat pid"
    try:
        os.kill(pid, signal.SIGUSR2)
        time.sleep(0.8)
        return f"collapsed pid={pid} by SIGUSR2"
    except OSError as e:
        return f"FAILED: {type(e).__name__}: {e}"


def shot(out, whole=False):
    wid = None if whole else (geom() or (None,))[0]
    if not whole and not wid:
        return "REFUSED: seat window not found (use -w for the whole screen)"
    cmd = ["xwd", "-silent"] + (["-root"] if whole else ["-id", wid])
    # ★ BINARY, NOT TEXT. The first version ran this with text=True and then re-encoded the decoded
    #   string -- which corrupts an image stream. `xwd` writes PNG-shaped bytes to stdout; capture them
    #   as bytes and pipe them straight in, or the screenshot is silently garbage.
    p = subprocess.run(cmd, capture_output=True, env=_display_env())
    if p.returncode != 0 or not p.stdout:
        return f"xwd failed: {p.stderr[:120]!r}"
    c = subprocess.run(["convert", "xwd:-", out], input=p.stdout,
                       capture_output=True, env=_display_env())
    if c.returncode != 0:
        return f"convert failed: {c.stderr.decode(errors='replace')[:120]}"
    return f"wrote {out} ({os.path.getsize(out)} B)"


def click(x, y):
    _run(["xdotool", "mousemove", str(int(x)), str(int(y))])
    time.sleep(0.4)
    _run(["xdotool", "click", "--clearmodifiers", "1"])
    time.sleep(0.5)
    return f"clicked {x},{y}"


def clickrel(x, y):
    got = geom()
    if not got or not got[1]:
        return "REFUSED: no seat geometry"
    p = _parse_geom(got[1])
    if not p:
        return f"REFUSED: unparsable geometry {got[1]!r}"
    _, _, base_x, base_y = p
    return click(base_x + int(x), base_y + int(y))


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 2
    v = a[0]
    if v == "open":
        print(" ", open_seat())
    elif v == "close":
        print(" ", close_seat())
    elif v in ("grid", "settings", "access", "menu", "zoomin", "zoomout", "zoomreset",
               "prime", "brainstop", "leadtoggle", "models",
               # ★★ THE POSTURE ROW'S THREE SURFACES, 2026-10-10. The SIG map alone was NOT enough:
               #    this dispatch list is a SECOND gate, and a verb present in one and absent from the
               #    other prints the usage text and exits 2 -- which is exactly what happened when I
               #    added the slots and not the verbs here. A door with a handle means BOTH.
               "agentrules", "capture", "autonomy"):
        print(" ", signal_page(v))
    elif v == "geom":
        got = geom()
        if not got:
            return 2
        print(f"  {TITLE}: id={got[0]} geom={got[1]}")
    elif v == "shot":
        print(" ", shot(a[1] if len(a) > 1 else "/tmp/seat.png", whole="-w" in a))
    elif v == "click":
        print(" ", click(a[1], a[2]))
    elif v == "clickrel":
        print(" ", clickrel(a[1], a[2]))
    elif v == "open+shot":
        print(" ", open_seat())
        print(" ", shot(a[1] if len(a) > 1 else "/tmp/seat.png"))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
