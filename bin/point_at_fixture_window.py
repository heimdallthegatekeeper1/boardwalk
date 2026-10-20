#!/usr/bin/env python3
"""point_at_fixture_window.py -- a KNOWN surface for driving the point-at loop.

★ WHY THIS EXISTS. The first live drive of `bin/point-at.py` must not photograph the operator's
  browser, chat, or mail to prove that OCR -> privacy -> rectangle works. This window contains only
  fixture text: a heading, a Continue button, a reserved-domain email, and a SYNTHETIC key that is
  deliberately not a real credential. It is X11 (GDK_BACKEND forced) so per-window capture is
  possible, and it has no network, no inputs, and no side effects.

★ IT IS NOT PART OF THE PRODUCT. It is the test surface; `--hold` closes it on its own.
"""
from __future__ import annotations

import os
import sys

os.environ["GDK_BACKEND"] = "x11"

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

TITLE = "BoardWalk point-at test window"
FAKE_KEY = "sk_" + "live_" + "TESTONLY1234567890"      # synthetic; not a provider key, never leaves the box
FAKE_EMAIL = "operator@example.invalid"             # example.com is reserved for documentation


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    hold = 300.0
    if "--hold" in argv:
        try:
            hold = float(argv[argv.index("--hold") + 1])
        except (IndexError, ValueError):
            pass
    win = Gtk.Window(title=TITLE)
    win.set_default_size(760, 440)
    win.set_position(Gtk.WindowPosition.CENTER)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
    for m in ("top", "bottom", "start", "end"):
        getattr(box, "set_margin_" + m)(26)
    win.add(box)

    def label(text, size=None, colour=None):
        lb = Gtk.Label(label=text)
        lb.set_xalign(0)
        lb.set_line_wrap(True)
        if size:
            lb.override_font(Pango.FontDescription(f"Sans {size}"))
        box.pack_start(lb, False, False, 0)
        return lb

    label("Grant access - timed, scoped, expiring", size=20)
    label("This window is a fixture. Nothing here is the operator's data.", size=13)
    btn = Gtk.Button(label="Continue to the next step")
    btn.set_size_request(300, 44)
    box.pack_start(btn, False, False, 6)
    label("You are choosing among known things.", size=13)
    label(f"Contact: {FAKE_EMAIL}", size=13)
    label(f"API key: {FAKE_KEY}", size=13)
    label("Close me with the window button or let --hold expire.", size=11)
    win.connect("destroy", lambda *_: Gtk.main_quit())
    win.show_all()

    def _close():
        try:
            win.destroy()
        except Exception:  # noqa: BLE001
            Gtk.main_quit()
        return False

    GLib.timeout_add(int(max(1.0, hold) * 1000), _close)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
