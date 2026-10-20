#!/usr/bin/env python3
"""BoardWalk launcher — one entry point, whichever OS you are on.

★★ WHAT THIS DOES: finds a free loopback port, starts `panel_server.py` from this release, waits
   until the board answers, and opens the default browser at the one board surface
   (`/?section=operator-needs`). Ctrl-C stops the server it started.

★★ WHAT IT DOES NOT DO: install packages, use sudo, touch the network, or copy your data. The
   installer (`install.py`) creates the desktop icon; this file is what that icon runs.

★★ THE HONEST OS MATRIX (also in DISTRO.md):
   · Linux/X11 — the full app: board, walk, grant wizard, click-through overlay, OCR pointing.
   · Linux/Wayland — the panel and board are full; the OS-wide overlay is an X11 window and is
     only reliable over XWayland windows. Screen-region pointing still works.
   · macOS / Windows — the panel and board run in the browser; the X11 desktop overlay and the
     per-window OCR pointing DO NOT work natively in this release. The launcher says so and still
     starts the panel. Do not read a launched panel as "the overlay works here".
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PANEL = APP / "panel_server.py"
ICON = APP / "assets" / "boardwalk" / "boardwalk-icon-64.png"


def free_port(start: int = 7842, end: int = 7900) -> int | None:
    for p in range(start, end + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def check() -> int:
    """Print what is present, what is missing, and what that means. No install, no network."""
    have = lambda m: importlib.util.find_spec(m) is not None
    print("BoardWalk — launcher check")
    print(f"  app            {APP}")
    print(f"  python         {sys.version.split()[0]}  ({sys.executable})")
    print(f"  os             {platform.system()} {platform.release()}")
    print(f"  panel_server   {'present' if PANEL.is_file() else 'MISSING'}")
    print(f"  icon           {'present' if ICON.is_file() else 'MISSING'}")
    print(f"  PyGObject/gi   {'present' if have('gi') else 'missing (the overlay needs it)'}")
    print(f"  pycairo        {'present' if have('cairo') else 'missing (the overlay needs it)'}")
    for tool, why in (("tesseract", "local OCR for point-at"),
                      ("convert", "ImageMagick: capture/measure"),
                      ("xwininfo", "X11 window map")):
        print(f"  {tool:<14} {'present' if shutil.which(tool) else 'missing — ' + why}")
    if platform.system() != "Linux":
        print("\n  NOTE: the OS-wide overlay and per-window OCR pointing are X11-only in this release.")
        print("        The panel and board work in the browser on this OS.")
    elif not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("\n  NOTE: no DISPLAY — the panel will start, but the desktop overlay cannot open.")
    return 0 if PANEL.is_file() else 1


def wait_for(port: int, proc: subprocess.Popen, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Start BoardWalk and open the one board.")
    ap.add_argument("--port", type=int, default=0, help="port (default: first free from 7842)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ap.add_argument("--check", action="store_true", help="report dependencies and exit")
    a = ap.parse_args(argv)
    if a.check:
        return check()
    if not PANEL.is_file():
        print(f"REFUSED: panel_server.py is not beside the launcher ({PANEL})", file=sys.stderr)
        return 1
    if a.port:
        port = a.port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            print(f"REFUSED: port {port} is already in use on loopback. "
                  f"Try --port 0 (pick a free one) or stop the other panel.", file=sys.stderr)
            return 1
        finally:
            s.close()
    else:
        port = free_port()
        if port is None:
            print("REFUSED: no free loopback port in 7842..7900", file=sys.stderr)
            return 1

    log = open(APP / "launcher.log", "ab", buffering=0)
    cmd = [sys.executable, str(PANEL), "--port", str(port), "--foreground"]
    print(f"starting BoardWalk on http://127.0.0.1:{port}/ …")
    proc = subprocess.Popen(cmd, cwd=str(APP), stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_for(port, proc):
            print(f"the panel did not answer on :{port}. Last lines of launcher.log:", file=sys.stderr)
            try:
                tail = (APP / "launcher.log").read_text(errors="replace").splitlines()[-12:]
                print("\n".join(tail), file=sys.stderr)
            except Exception:
                pass
            return 1
        url = f"http://127.0.0.1:{port}/?section=operator-needs"
        print(f"BoardWalk is up: {url}")
        print("  the board, the walk and the utilities are in the browser; Ctrl-C stops this server.")
        if not a.no_browser:
            try:
                webbrowser.open(url)
            except Exception as e:  # noqa: BLE001
                print(f"could not open a browser ({e}); open the URL above.", file=sys.stderr)
        return proc.wait()
    except KeyboardInterrupt:
        print("\nstopping BoardWalk …")
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        return 0
    finally:
        try:
            log.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
