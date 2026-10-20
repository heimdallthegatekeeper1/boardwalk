#!/usr/bin/env python3
"""Machine-drive the point-me-to-there card through AT-SPI, on an isolated Xvfb display.

WHY THIS SUITE EXISTS
---------------------
The operator's central remaining gap was: *the card's agent/verdict/front buttons have not been
machine-clicked*. XTEST synthetic clicks do not reach GTK on this Xwayland box, and an earlier
attempt concluded the UI could not be driven. It can: **AT-SPI is present** (`gir1.2-atspi-2.0`),
and `Atspi.Action.do_action()` is a real accessibility activation that runs the widget's own
`clicked` handler. This suite drives the REAL buttons, then reads the REAL widget labels back
(`point_to._snapshot`) and the append-only journal.

ISOLATION (nothing of the operator's is touched)
------------------------------------------------
* its own Xvfb display + xfwm4, so no window appears on his screen
* its own fake panel-geometry server, so the live panel is not required
* its own `point_to` pid/report/feedback/cmd/snapshot paths
* its own guide state + event trail, selecting a DISCONNECTED backend, so no model is called and
  the operator's selected agent never changes
* a copy of the source only for `--prove-fail` mutants; the live file is never written

USAGE
-----
    DISPLAY may be unset; the suite starts Xvfb itself.
    python3 jarvis/test_point_to_drive.py            # the drive, exit 0 on PASS
    python3 jarvis/test_point_to_drive.py --prove-fail  # each injected defect must go RED

Not run = exit 2 with a named SKIP. A red mutant run is the evidence the guards are not vacuous.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "jarvis" / "point_to.py"
JARVIS = ROOT / "jarvis"
OPERATOR = ROOT / "operator"

CARD = "point-me-to-there card"
AGENT = "point-me-to-there agent"

# The exact labels the card carries on step 1 of a three-step walk. A control that vanishes while
# another takes its place is caught by comparing the whole sorted list, not by "the button exists".
EXPECTED_BUTTONS = [
    "diagram: on/off", "dim: on/off", "lang: en", "next ▶",
    "voice: on/off", "voice…", "⌖ jump to it", "⏹ stop voice", "⏻ kill",
    "▶ open it", "◀ back", "◧ agent / explain", "✓ right spot", "✕",
    "✕ cancel walk", "✕ cancel walk", "✗ wrong spot", "⤴ front", "🔊 read",
    "🔍 search for it",
]


# ── the fake panel: exactly the two routes the overlay reads ─────────────────────────────────────
class Panel:
    def __init__(self):
        self.geom = None
        self.fronts = []
        self.lock = __import__("threading").Lock()

    def set_geom(self, x, y, age_s=0.0, outer=(1280, 900), inner=(1280, 813)):
        at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_s)
        with self.lock:
            self.geom = {"at": at.isoformat().replace("+00:00", "Z"),
                         "screen_x": float(x), "screen_y": float(y),
                         "outer_w": float(outer[0]), "outer_h": float(outer[1]),
                         "inner_w": float(inner[0]), "inner_h": float(inner[1]),
                         "dpr": 1.0, "href": "http://127.0.0.1/fake-panel/?section=operator-needs"}

    def clear(self):
        with self.lock:
            self.geom = None

    def get(self):
        with self.lock:
            return dict(self.geom) if self.geom else None


PANEL = Panel()


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/operator/window-geometry":
            g = PANEL.get()
            return self._send(200, {"ok": bool(g), **(g or {})})
        if self.path == "/operator/panel/front":
            with PANEL.lock:
                d = PANEL.fronts[-1] if PANEL.fronts else {}
            return self._send(200, {"ok": bool(d), **d})
        return self._send(404, {"ok": False, "error": "not in this fake panel"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        if self.path == "/operator/window-geometry":
            with PANEL.lock:
                PANEL.geom = body
            return self._send(200, {"ok": True})
        if self.path == "/operator/panel/front":
            with PANEL.lock:
                PANEL.fronts.append(body)
            return self._send(200, {"ok": True, "seq": time.time_ns(),
                                    "step": body.get("step"),
                                    "title": body.get("title")})
        return self._send(404, {"ok": False, "error": "not in this fake panel"})


# ── AT-SPI: the real activation path ─────────────────────────────────────────────────────────────
def atspi():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi
    Atspi.init()
    return Atspi


class UI:
    def __init__(self, app_name):
        self.A = atspi()
        self.app_name = app_name
        self._app = None
        self._cache = {}

    def _kids(self, n):
        try:
            return [n.get_child_at_index(i) for i in range(n.get_child_count())]
        except Exception:
            return []

    def _info(self, n):
        try:
            name = n.get_name() or ""
        except Exception:
            name = ""
        try:
            role = n.get_role_name() or ""
        except Exception:
            role = ""
        return name, role

    def app(self):
        if self._app is not None:
            try:
                if self._info(self._app)[0] == self.app_name:
                    return self._app
            except Exception:
                self._app = None
        d = self.A.get_desktop(0)
        for i in range(d.get_child_count()):
            a = d.get_child_at_index(i)
            if self._info(a)[0] == self.app_name:
                self._app = a
                return a
        return None

    def wait_app(self, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            a = self.app()
            if a is not None:
                return a
            time.sleep(0.2)
        return None

    def window(self, title):
        a = self.app()
        if a is None:
            return None
        q = [(a, 0)]
        while q:
            n, d = q.pop(0)
            name, role = self._info(n)
            if role in ("window", "frame", "dialog") and title.lower() in name.lower():
                return n
            if d < 4:
                q.extend((c, d + 1) for c in self._kids(n))
        return None

    def wait_window(self, title, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            w = self.window(title)
            if w is not None:
                return w
            time.sleep(0.2)
        return None

    def nodes(self, window_title):
        """One AT-SPI walk per window, cached. The bridge is slow; a click must not re-walk 50 nodes."""
        if window_title in self._cache:
            return self._cache[window_title]
        w = self.window(window_title)
        if w is None:
            return (None, [])
        out = []
        q = [(w, 0)]
        while q:
            n, d = q.pop(0)
            out.append(n)
            if d < 8:
                q.extend((c, d + 1) for c in self._kids(n))
        self._cache[window_title] = (w, out)
        return (w, out)

    def select_tab(self, window_title, label):
        w, nodes = self.nodes(window_title)
        for n in nodes:
            name, role = self._info(n)
            if role in ("page tab list", "notebook"):
                for i in range(n.get_child_count()):
                    c = n.get_child_at_index(i)
                    if (self._info(c)[0] or "").lower() == label.lower():
                        try:
                            return bool(self.A.Selection.select_child(n, i))
                        except Exception:
                            return False
        return False

    def find(self, root, want, roles=None, depth=8):
        """BFS, exact label preferred, substring as a fallback. Stops at the first exact hit."""
        want_l = str(want).lower()
        fallback = None
        q = [(root, 0)]
        while q:
            n, d = q.pop(0)
            name, role = self._info(n)
            if roles and role not in roles:
                pass
            elif name:
                if name.lower() == want_l:
                    return n, name, role
                if want_l in name.lower() and fallback is None:
                    fallback = (n, name, role)
            if d < depth:
                q.extend((c, d + 1) for c in self._kids(n))
        return fallback if fallback else (None, None, None)

    def click(self, window_title, label, role="button"):
        w, nodes = self.nodes(window_title)
        if w is None:
            raise AssertionError(f"window {window_title!r} did not appear")
        want = label.lower()
        hits = []
        for n in nodes:
            name, got_role = self._info(n)
            if got_role == role and name:
                if name.lower() == want:
                    hits.insert(0, (n, name))
                elif want in name.lower():
                    hits.append((n, name))
        if not hits:
            raise AssertionError(f"no {role} {label!r} in {window_title!r}")
        n, name = hits[0]
        # ★ AT-SPI ACTION CALLS CAN BLOCK WHEN THE HANDLER DESTROYS THE WINDOW. The manual cancel
        #   returns, but a mutant or a shutdown race can leave the D-Bus call waiting forever; that
        #   is an INSTRUMENT hang, not a product failure. Run it on a thread with a deadline; a
        #   delivered action is confirmed by the snapshot/journal that follows, not by the return.
        import threading as _th
        res = {"ok": False, "done": False}

        def _do():
            try:
                res["ok"] = bool(self.A.Action.do_action(n, 0))
            except Exception:
                res["ok"] = False
            finally:
                res["done"] = True

        t = _th.Thread(target=_do, daemon=True)
        t.start()
        t.join(12)
        if not res["done"]:
            print(f"  NOTE: AT-SPI action on {name!r} did not return within 12 s; "
                  f"the snapshot/journal will confirm whether it landed")
            return name
        if not res["ok"]:
            raise AssertionError(f"AT-SPI refused the activation of {name!r}")
        return name

    def set_entry(self, window_title, text):
        w, nodes = self.nodes(window_title)
        # ★ GTK exposes the Entry as role `text` with an empty accessible name; some labels also
        #   have empty text children. Try every editable candidate instead of trusting the first.
        seen = 0
        for n in nodes:
            name, role = self._info(n)
            if role in ("entry", "text"):
                seen += 1
                try:
                    if self.A.EditableText.set_text_contents(n, text):
                        return True
                except Exception:
                    pass
        raise AssertionError(f"no editable entry in {window_title!r} ({seen} text/entry nodes tried)")

    def size(self, window_title):
        w = self.wait_window(window_title)
        if w is None:
            return None
        try:
            p = self.A.Component.get_size(w)
            return (int(p.x), int(p.y)) if p is not None else None
        except Exception:
            try:
                r = self.A.Component.get_extents(w, 0)
                return (int(r.width), int(r.height)) if r is not None else None
            except Exception:
                return None


# ── small waits ─────────────────────────────────────────────────────────────────────────────────
def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def read_rows(path):
    try:
        return [json.loads(x) for x in Path(path).read_text(errors="replace").splitlines() if x.strip()]
    except Exception:
        return []


def wait_snap(path, pred, timeout=25.0, after_seq=0):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        d = read_json(path)
        if isinstance(d, dict):
            last = d
            if int(d.get("seq") or 0) > after_seq and pred(d):
                return d
        time.sleep(0.15)
    return last if isinstance(last, dict) and pred(last) else None


def wait_rows(path, pred, timeout=20.0, n_before=0):
    end = time.time() + timeout
    while time.time() < end:
        rows = read_rows(path)
        for r in rows[n_before:]:
            if pred(r):
                return r
        time.sleep(0.2)
    return None


def wait_exit(proc, timeout=12.0):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return proc.poll() is not None


# ── the drive ───────────────────────────────────────────────────────────────────────────────────
class Drive:
    def __init__(self, tmp: Path, display: str, panel_port: int, overlay_src: Path):
        self.tmp = tmp
        self.display = display
        self.panel_port = panel_port
        self.overlay_src = overlay_src
        self.app_name = f"bdpt-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.snap = tmp / "snapshot.json"
        self.fb = tmp / "feedback.jsonl"
        self.pid = tmp / "overlay.pid"
        self.report = tmp / "overlay_report.json"
        self.cmd = tmp / "overlay.cmd"
        self.state = tmp / "guide_state.json"
        self.events = tmp / "guide_events.jsonl"
        self.overlay_log = tmp / "overlay.log"
        self.ref_title = f"BD REFERENCE {os.getpid()}"
        self.walk = tmp / "walk.json"
        self.failures = []
        self.ui = None
        self.proc = None
        self.ref = None

    def check(self, cond, msg):
        if cond:
            print(f"  PASS: {msg}")
        else:
            self.failures.append(msg)
            print(f"  FAIL: {msg}")
        return bool(cond)

    def env(self, extra=None):
        e = dict(os.environ)
        e.pop("XAUTHORITY", None)
        e.pop("WAYLAND_DISPLAY", None)
        e["DISPLAY"] = self.display
        e["GDK_BACKEND"] = "x11"
        e["PYTHONPATH"] = os.pathsep.join([str(JARVIS), str(OPERATOR), e.get("PYTHONPATH", "")]).strip(":")
        e.update({
            "KHAOSS_PANEL_URL": f"http://127.0.0.1:{self.panel_port}",
            "KHAOSS_POINT_TO_APP_NAME": self.app_name,
            "KHAOSS_POINT_TO_PID": str(self.pid),
            "KHAOSS_POINT_TO_REPORT": str(self.report),
            "KHAOSS_POINT_TO_FEEDBACK": str(self.fb),
            "KHAOSS_POINT_TO_CMD": str(self.cmd),
            "KHAOSS_POINT_TO_SNAPSHOT": str(self.snap),
            "KHAOSS_GUIDE_STATE": str(self.state),
            "KHAOSS_GUIDE_EVENTS": str(self.events),
        })
        if extra:
            e.update(extra)
        return e

    def write_fixture(self):
        self.state.write_text(json.dumps({
            "version": 1, "selected": "dispatch:prime", "voice_selected": "dispatch:prime",
            "selected_at": "2026-01-01T00:00:00-0700"}))
        self.walk.write_text(json.dumps({
            "id": "drive", "title": "machine drive", "steps": [
                {"id": "panel-1", "title": "Panel step",
                 "body": "This is the only door to root on this box, and it is deliberately narrow. "
                         "The machine test reads this body in the Explain tab.",
                 "hint": "press front and the verdicts",
                 "target": {"rect": [0, 0, 1080, 620]}, "anchor": "KHAOSS"},
                {"id": "x11-1", "title": "X11 step",
                 "body": "A real X11 window: front must raise it with xdotool.",
                 "target": {"window": self.ref_title}},
                {"id": "reaim-1", "title": "Re-aim step",
                 "body": "Tracking is OFF here on purpose, so 'wrong spot' has to re-aim.",
                 "target": {"rect": [0, 0, 900, 430]}, "anchor": "KHAOSS", "track": "off"},
            ]}, indent=1))

    def start_reference(self):
        py = self.tmp / "refwin.py"
        py.write_text(
            "import gi\n"
            "gi.require_version('Gtk','3.0')\n"
            "from gi.repository import Gtk\n"
            f"w=Gtk.Window(title={self.ref_title!r})\n"
            "w.set_default_size(640,420); w.move(300,338)\n"
            "w.add(Gtk.Label(label='REFERENCE PANEL')); w.show_all(); w.present()\n"
            "Gtk.main()\n")
        log = open(self.tmp / "refwin.log", "ab")
        self.ref = subprocess.Popen([sys.executable, str(py)], env=self.env(),
                                    stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + 15
        while time.time() < end:
            r = subprocess.run(["xdotool", "search", "--name", self.ref_title],
                               env=self.env(), capture_output=True, text=True)
            if r.stdout.strip():
                return True
            time.sleep(0.2)
        return False

    def launch(self):
        # ★ A NEW APPLICATION NAME PER RUN. AT-SPI can keep a dead app in its registry for a moment;
        #   reusing the name made the second launch find the FIRST run's card.
        self.app_name = f"bdpt-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        old_seq = (read_json(self.snap) or {}).get("seq") or 0
        log = open(self.overlay_log, "ab")
        self.proc = subprocess.Popen([sys.executable, str(self.overlay_src),
                                      "--script", str(self.walk)],
                                     env=self.env(), stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.ui = UI(self.app_name)
        if self.ui.wait_app(45) is None:
            raise AssertionError(f"the overlay app {self.app_name!r} never appeared in AT-SPI")
        if self.ui.wait_window(CARD, 30) is None:
            raise AssertionError("the card window never appeared")
        d = wait_snap(self.snap, lambda x: x.get("why") == "apply", timeout=25, after_seq=old_seq)
        if d is None:
            raise AssertionError("the overlay wrote no initial snapshot")
        return d

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
        if self.ref is not None and self.ref.poll() is None:
            self.ref.terminate()
            try:
                self.ref.wait(timeout=5)
            except Exception:
                self.ref.kill()

    def dlg_buttons(self, title="Leave the walk?"):
        """The leave card's buttons, glyph and spacing stripped, sorted — for SET EQUALITY."""
        w, nodes = self.ui.nodes(title)
        out = []
        for n in nodes:
            name, role = self.ui._info(n)
            if role == "button" and name:
                out.append(" ".join(str(name).replace("\u25b8", "").replace("\u25f7", "")
                                    .replace("\u25a0", "").replace("\u2190", "").split()))
        return sorted(out)

    def press_leave(self, label, tries=4):
        """Press a button on the leave card, retrying a stale AT-SPI handle.

        ★ THE FIRST VERSION CLICKED IMMEDIATELY AFTER wait_window AND MISSED: the second time the
          card opens, AT-SPI can still return the PREVIOUS card's nodes, so "no button ..." was an
          instrument race, not a missing control. It retries and, when it still fails, prints the
          card's real button names so a genuine absence is distinguishable from a race.
        """
        last = ""
        for n in range(tries):
            try:
                return self.ui.click("Leave the walk?", label)
            except AssertionError as e:
                last = str(e)
                time.sleep(0.4 * (n + 1))
        print(f"  NOTE: buttons present were {self.dlg_buttons()}")
        raise AssertionError(last)

    def click(self, label, window=CARD):
        before = (read_json(self.snap) or {}).get("seq") or 0
        name = self.ui.click(window, label)
        print(f"  CLICKED: {name!r} in {window!r}")
        return before


    def check_threaded_ask(self):
        """Drive the REAL `_agent_ask` worker with a stub backend: no model, no GUI loop.

        ★ THE REFUSAL PATH RETURNS BEFORE `threading.Thread` IS REACHED. A prove-fail that removes
          `import threading` therefore stayed GREEN while the Ask button was still dead — the exact
          too-gentle probe this estate warns about. This runs the success branch of the same method
          with a stub that completes immediately, and asserts the worker's answer lands in the real
          widget. It is the one test that can falsify the import.
        """
        code = r"""
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("pt_under_test", sys.argv[1])
pt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pt)
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib
class GB:
    @staticmethod
    def notify(src):
        return {"ok": True, "event_id": "ev-test", "status": "queued"}
    @staticmethod
    def event(evid):
        return {"status": "completed", "answer": "fake answer"}
o = pt.Overlay.__new__(pt.Overlay)
o.steps = [{"id": "s", "title": "stub"}]
o.i = 0
o.mode = "stub"
o.agent_ask_entry = Gtk.Entry()
o.agent_ask_entry.set_text("q")
o.agent_ask_state = Gtk.Label()
o.agent_answer = Gtk.Label()
o.agent_ask_btn = Gtk.Button()
o._agent_mod = lambda: GB
o._agent_ask()
end = time.time() + 6
while time.time() < end and o.agent_answer.get_text() != "fake answer":
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    time.sleep(0.03)
print("ANSWER", o.agent_answer.get_text())
print("STATE", o.agent_ask_state.get_text())
sys.exit(0 if o.agent_answer.get_text() == "fake answer" else 1)
"""
        r = subprocess.run([sys.executable, "-c", code, str(self.overlay_src)],
                           env=self.env(), capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0 and "ANSWER fake answer" in (r.stdout or "")
        self.check(ok, f"the threaded Ask path answers without a model (got {(r.stdout or '')[-160:]!r} "
                       f"{(r.stderr or '')[-160:]!r})")

    def run(self):
        d0 = self.launch()
        self.check(d0.get("resolved") is True, "first step resolved from the panel's reported geometry")
        self.check(d0.get("rect") == [200, 187, 1080, 620],
                   f"window-relative rect is the panel content origin + the walk's rect (got {d0.get('rect')})")
        self.check("following KHAOSS" in str(d0.get("state")),
                   "the card says it is following KHAOSS, not merely that it resolved")

        # ── the control set, by exact multiset ──────────────────────────────────────────────────
        got = [re.sub(r"^lang: .*$", "lang: *", b) for b in (d0.get("buttons") or [])]
        want = [re.sub(r"^lang: .*$", "lang: *", b) for b in EXPECTED_BUTTONS]
        self.check(got == sorted(want),
                   f"the card's button set is EXACTLY the expected one (got {len(got)}, want {len(want)})")
        if got != sorted(want):
            print("    extra:", sorted(set(got) - set(want)))
            print("    missing:", sorted(set(want) - set(got)))

        # ── width: contents must not widen the card ─────────────────────────────────────────────
        lay = d0.get("layout") or {}
        pref = lay.get("card_pref") or {}
        flow = lay.get("flow_pref") or {}
        sz = self.ui.size(CARD)
        floor = min(430, max(320, 1600 - 16))
        self.check(sz is not None and sz[0] <= floor,
                   f"the real card is not wider than its floor (window {sz}, floor {floor})")
        self.check(pref.get("min_w", 10 ** 9) <= floor,
                   f"the card's own minimum width does not exceed the floor ({pref.get('min_w')} <= {floor})")
        self.check(flow.get("min_w", 10 ** 9) <= floor,
                   f"the FlowBox's minimum width fits the card ({flow.get('min_w')} <= {floor})")
        self.check((lay.get("widest_child") or 10 ** 9) <= floor,
                   f"the widest control fits the card ({lay.get('widest_child')} <= {floor})")

        # ── geometry move ───────────────────────────────────────────────────────────────────────
        rows0 = len(read_rows(self.fb))
        seq0 = (read_json(self.snap) or {}).get("seq") or 0
        PANEL.set_geom(600, 300)
        tr = wait_rows(self.fb, lambda r: r.get("kind") == "target_track", n_before=rows0, timeout=15)
        self.check(bool(tr) and tr.get("after") == [600, 387, 1080, 620],
                   f"the tracker re-aimed after the panel moved (journal {tr and tr.get('after')})")
        d = wait_snap(self.snap, lambda x: x.get("why") == "track" and x.get("rect") == [600, 387, 1080, 620],
                      timeout=10, after_seq=seq0)
        self.check(bool(d) and "target moved" in str(d.get("state")),
                   f"the card shows 'target moved — re-aimed' (got {d and d.get('state')!r})")

        # ── right spot ──────────────────────────────────────────────────────────────────────────
        before = self.click("✓ right spot")
        d = wait_snap(self.snap, lambda x: x.get("why") == "verdict", after_seq=before, timeout=20)
        self.check(bool(d) and d.get("yes") == "✓ recorded — right spot",
                   f"the right-spot label survives the repaint (got {d and d.get('yes')!r})")
        self.check(bool(d) and "right spot recorded" in str(d.get("state")),
                   f"the state says the right spot is recorded (got {d and d.get('state')!r})")
        self.check(bool(d) and "NOT delivered" in str(d.get("mirror")),
                   f"a refused delivery is shown as NOT delivered, not as queued (got {d and d.get('mirror')!r})")
        vrow = wait_rows(self.fb, lambda r: r.get("kind") == "verdict" and r.get("ok") is True, timeout=10)
        self.check(bool(vrow) and vrow.get("implied") is False,
                   "the explicit right-spot verdict is in the journal and is not marked implied")

        # ── wrong spot: anchor re-read, same rect ──────────────────────────────────────────────
        before = self.click("✗ wrong spot")
        d = wait_snap(self.snap, lambda x: x.get("why") == "verdict", after_seq=before, timeout=20)
        self.check(bool(d) and d.get("no") == "✗ recorded — same spot",
                   f"a re-read anchor that did not move says 'same spot' (got {d and d.get('no')!r})")
        self.check(bool(d) and "anchor window KHAOSS re-read" in str(d.get("state")),
                   f"the state names the rung that answered (got {d and d.get('state')!r})")
        self.check(bool(d) and "no window/text found" not in str(d.get("state")),
                   "the state does NOT falsely say no window or text was found")

        # ── front: the panel path ──────────────────────────────────────────────────────────────
        nfront = len(PANEL.fronts)
        before = self.click("⤴ front")
        d = wait_snap(self.snap, lambda x: x.get("why") == "front", after_seq=before, timeout=15)
        self.check(bool(d) and "Wayland-native" in str(d.get("state")),
                   f"front says why X11 cannot raise the panel (got {d and d.get('state')!r})")
        self.check(bool(d) and "asked the panel page" in str(d.get("state")),
                   f"front records that the page was asked (got {d and d.get('state')!r})")
        self.check(len(PANEL.fronts) == nfront + 1,
                   f"the panel actually received the front request ({len(PANEL.fronts) - nfront} new)")
        fr = wait_rows(self.fb, lambda r: r.get("kind") == "bring_front", timeout=10)
        self.check(bool(fr) and fr.get("panel") is True,
                   "the journal records the front as a panel request")
        self.check(not any("xdotool found NO window" in str(n) for n in (fr or {}).get("notes") or []),
                   "front does not blame xdotool for a Wayland panel it cannot see")

        # ── agent panel: explain, tabs, live-chat honesty ─────────────────────────────────────
        before = self.click("◧ agent / explain")
        d = wait_snap(self.snap, lambda x: x.get("why") == "agent_scan", after_seq=before, timeout=25)
        ag = (d or {}).get("agent") or {}
        self.check(ag.get("open") is True, "the agent panel opened")
        self.check(set(ag.get("tabs") or []) == {"Explain", "Ask", "Diagram", "Agent"},
                   f"the agent panel has exactly the four tabs (got {ag.get('tabs')})")
        self.check("only door to root" in str(ag.get("explain")),
                   "Explain renders the step body")
        _chat_txt = str(ag.get("chat") or "")
        if "not found" in _chat_txt.lower():
            # A fresh box has no live DSH chat; the card must SAY that rather than imply a send target.
            self.check(True, "no live DSH chat on this box; the card says it was not found")
        else:
            self.check("link-only" in _chat_txt.lower() and "lock is held" in _chat_txt,
                       "the live DSH chat is stated as link-only with the measured lock reason")
        self.check(bool(re.search(r"scan: \d+ selectable backends — \d+ can answer, \d+ queue/link only",
                                  str(ag.get("scan")))),
                   f"the agent scan states how many rows can actually answer (got {ag.get('scan')!r})")
        self.check(ag.get("selected") == "dispatch:prime",
                   f"the selected backend is the isolated disconnected one (got {ag.get('selected')!r})")

        # Ask: a disconnected backend must say NOT delivered, not paint an answer.
        try:
            if not self.ui.select_tab(AGENT, "Ask"):
                print("  NOTE: the notebook refused the Ask tab selection; the entry is still tried")
            self.ui.set_entry(AGENT, "What should I check on this screen?")
            before = self.click("ask", window=AGENT)
            d = wait_snap(self.snap, lambda x: x.get("why") == "agent_ask_refused",
                          after_seq=before, timeout=25)
            a = ((d or {}).get("agent") or {}).get("ask_state") or ""
            self.check(a.startswith("NOT delivered") and "Prime Agent" in a,
                       f"Ask on a disconnected backend says NOT delivered and names it (got {a!r})")
        except AssertionError as e:
            self.check(False, f"Ask refusal state ({e})")

        try:
            before = self.click("ask the agent for a diagram of this step", window=AGENT)
            d = wait_snap(self.snap, lambda x: x.get("why") == "agent_diagram_refused",
                          after_seq=before, timeout=25)
            a = ((d or {}).get("agent") or {}).get("diag_state") or ""
            self.check(a.startswith("NOT delivered"),
                       f"Diagram on a disconnected backend says NOT delivered (got {a!r})")
        except AssertionError as e:
            self.check(False, f"Diagram refusal state ({e})")

        # ── the threading guard: the success branch of the SAME handler, no model, no GUI loop ──
        self.check_threaded_ask()

        # ── step 2: a real X11 window, front raises it ────────────────────────────────────────
        before = self.click("next ▶")
        d = wait_snap(self.snap, lambda x: x.get("why") == "apply" and x.get("i") == 1,
                      after_seq=before, timeout=15)
        self.check(bool(d) and "following the target window" in str(d.get("state")),
                   f"step 2 follows the X11 window (got {d and d.get('state')!r})")
        before = self.click("⤴ front")
        d = wait_snap(self.snap, lambda x: x.get("why") == "front", after_seq=before, timeout=15)
        self.check(bool(d) and "raised" in str(d.get("state")) and self.ref_title in str(d.get("state")),
                   f"front raised the X11 window by name (got {d and d.get('state')!r})")
        act = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                             env=self.env(), capture_output=True, text=True)
        self.check(self.ref_title in (act.stdout or ""),
                   f"the X11 window is actually active after front (got {act.stdout.strip()!r})")

        # ── step 3: tracking off, wrong spot must move the ring ────────────────────────────────
        before = self.click("next ▶")
        d = wait_snap(self.snap, lambda x: x.get("why") == "apply" and x.get("i") == 2,
                      after_seq=before, timeout=15)
        self.check(bool(d) and "anchored to KHAOSS" in str(d.get("state"))
                   and "tracking off" in str(d.get("state")),
                   f"step 3 says it is anchored with tracking off, not screen-absolute (got {d and d.get('state')!r})")
        PANEL.set_geom(500, 250)
        time.sleep(1.6)                      # the tracker runs, but this step has track: off
        before = self.click("✗ wrong spot")
        d = wait_snap(self.snap, lambda x: x.get("why") == "verdict", after_seq=before, timeout=20)
        self.check(bool(d) and d.get("no") == "✗ recorded — re-aimed",
                   f"a wrong spot with tracking off reports that it re-aimed (got {d and d.get('no')!r})")
        self.check(bool(d) and "re-aimed via anchor window KHAOSS" in str(d.get("state")),
                   f"the re-aim names the anchor rung (got {d and d.get('state')!r})")
        v = [r for r in read_rows(self.fb) if r.get("kind") == "verdict" and r.get("step") == "reaim-1"]
        self.check(bool(v) and v[-1].get("before") != v[-1].get("after"),
                   f"the journal carries a changed before/after for the re-aim ({v[-1].get('before') if v else None} -> {v[-1].get('after') if v else None})")

        # ── THE ONE TURNKEY EXIT: cancel AND close ────────────────────────────────────────────
        # ★ CANCEL MUST NOT EXIT ON THE CLICK. Every exit opens ONE card with exactly four choices;
        #   the three real outcomes are driven here in order — Stay (nothing changes), Keep it in the
        #   backlog (closes, dismisses nothing), Stop now (the old cancel, driven at the next site).
        before = self.click("✕ cancel walk")
        appeared = bool(self.ui.wait_window("Leave the walk?", 10))
        self.check(appeared, "LEAVE-CHOICE: cancel opens the one turnkey card")
        got = self.dlg_buttons()
        want_dlg = ["Keep it in the backlog", "Log for later — tomorrow 09:00",
                    "Stop now", "Stay in the walk"]   # the label names its default time
        self.check(got == sorted(want_dlg),
                   f"LEAVE-CHOICE: the card offers EXACTLY the four choices (got {got})")
        if appeared:
            self.press_leave("Stay in the walk")
            time.sleep(0.8)
            self.check(self.ui.wait_window("Leave the walk?", 1) is None,
                       "LEAVE-CHOICE: Stay closes the card")
            self.check(self.proc.poll() is None, "LEAVE-CHOICE: Stay leaves the overlay running")
            still = read_json(self.snap) or {}
            self.check((still.get("why") or "") != "cancel",
                       f"LEAVE-CHOICE: Stay writes no exit snapshot (got {still.get('why')!r})")
            # ★ STAY LEAVES THE OVERLAY RUNNING — that is the assertion above. The NEXT site launches
            #   a fresh overlay, so the running one is torn down here; without this the second launch
            #   raced a live process and AT-SPI never registered the new app.
            self.check(self.proc.poll() is None, "LEAVE-CHOICE: the overlay was still running to tear down")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                self.proc.kill()
            # ★ THE BACKLOG AND STOP-NOW OUTCOMES ARE DRIVEN ON FRESH LAUNCHES, NOT BY RE-OPENING
            #   THE CARD IN THE SAME PROCESS: AT-SPI kept returning the destroyed card's empty node
            #   list for the second open ("buttons present were []"), which is an instrument race,
            #   not a missing control. Each outcome now gets its own launch — which is also how the
            #   operator meets it. Backlog is driven on the stale run; Stop now on the first-run one.

        # ── stale geometry, then fresh again ──────────────────────────────────────────────────
        PANEL.set_geom(200, 100, age_s=3600)
        d0 = self.launch()
        self.check(d0.get("resolved") is True, "a stale report still resolves the last known rect")
        self.check("last panel report was" in str(d0.get("state")) and "may have moved" in str(d0.get("state")),
                   f"a stale report is stated on the card (got {d0.get('state')!r})")
        self.check(d0.get("anchor_state") == "stale", "the step carries anchor_state=stale")
        PANEL.set_geom(200, 100, age_s=0)
        d = wait_snap(self.snap, lambda x: x.get("why") == "track_fresh", timeout=15)
        self.check(bool(d) and "following KHAOSS" in str(d.get("state")),
                   f"a fresh report clears the stale warning (got {d and d.get('state')!r})")
        before = self.click("✕ cancel walk")
        self.ui.wait_window("Leave the walk?", 10)
        self.press_leave("Keep it in the backlog")
        d = wait_snap(self.snap, lambda x: x.get("why") == "leave_backlog", after_seq=before, timeout=15)
        self.check(bool(d) and d.get("dismissed") is False,
                   f"LEAVE-CHOICE: backlog dismisses NOTHING (got {d and d.get('dismissed')!r})")
        self.check(wait_exit(self.proc), "LEAVE-CHOICE: backlog closed the overlay")
        brow = wait_rows(self.fb, lambda r: r.get("kind") == "leave_backlog", timeout=8)
        self.check(bool(brow), "LEAVE-CHOICE: the journal records the backlog exit")

        # ── first run: no geometry, then it arrives ───────────────────────────────────────────
        PANEL.clear()
        d0 = self.launch()
        self.check(d0.get("resolved") is False, "with no panel report the anchored step is unresolved")
        self.check("waiting for the panel" in str(d0.get("state")),
                   f"the card waits instead of claiming to follow (got {d0.get('state')!r})")
        self.check("has not reported" in str(d0.get("err")),
                   "the card says the panel has not reported, not that a window is missing")
        self.check("PRES' open it" not in str(d0.get("err")) and "PRESS 'open it'" not in str(d0.get("err")),
                   "the missing-geometry error does not send him to the wrong recovery")
        PANEL.set_geom(200, 100)
        rec = wait_rows(self.fb, lambda r: r.get("kind") == "target_track" and r.get("recovered") is True,
                        timeout=15)
        self.check(bool(rec), "the tracker records the recovery when the report arrives")
        d = wait_snap(self.snap, lambda x: x.get("resolved") is True and "reported its position" in str(x.get("state")),
                      timeout=15)
        self.check(bool(d), f"the card clears the error and says it re-aimed (got {d and d.get('state')!r})")
        self.check(bool(d) and str(d.get("err")) == "",
                   "the red 'points at nothing' line is cleared on recovery")
        before = self.click("✕ cancel walk")
        self.ui.wait_window("Leave the walk?", 10)
        self.press_leave("Stop now")
        d = wait_snap(self.snap, lambda x: x.get("why") == "cancel", after_seq=before, timeout=15)
        self.check(bool(d), "LEAVE-CHOICE: Stop now wrote its own snapshot before the overlay closed")
        self.check(wait_exit(self.proc), "LEAVE-CHOICE: Stop now ended the overlay")
        crow = wait_rows(self.fb, lambda r: r.get("kind") == "cancel", timeout=8)
        self.check(bool(crow) and bool(crow.get("mode")),
                   f"the journal records the cancel and the mode (got {crow and crow.get('mode')!r})")

        return not self.failures


# ── prove-fail: each mutation must turn the drive red ───────────────────────────────────────────
MUTANTS = [
    ("instant-cancel", "        self._leave_choice()", "        self._cancel_walk_now()  # MUTANT",
     "LEAVE-CHOICE: cancel opens the one turnkey card"),
    ("no-tracker", "GLib.timeout_add(700, self._track_targets)", "pass  # MUTANT: no tracker",
     "the tracker re-aimed after the panel moved"),
    ("label-reset", 'self.yes_btn.set_label("\\u2713 recorded \\u2014 right spot")',
     'self.yes_btn.set_label("\\u2713 right spot")  # MUTANT: label erased',
     "right-spot label survives"),
    ("no-delivery", "self._deliver_verdict(s, ok, how)", "pass  # MUTANT: no delivery",
     "NOT delivered"),
    ("no-threading", "import threading\n", "# MUTANT: threading removed\n",
     "the threaded Ask path answers"),
    ("no-recovery", '            s["resolved"] = True\n            s["error"] = ""\n            s["recovery"] = ""',
     '            pass  # MUTANT: never mark resolved', "the card clears the error"),
    ("wide-button", 'self.front_btn = Gtk.Button(label="\\u2934 front")',
     'self.front_btn = Gtk.Button(label="\\u2934 front" + "X" * 300)  # MUTANT: wide',
     "FlowBox"),
]


def prove_fail(tmp: Path, display: str, port: int, only: str = "") -> int:
    src = OVERLAY.read_text()
    ok = True
    picked = [m for m in MUTANTS if not only or m[0] == only]
    print(f"prove-fail: {len(picked)} injected defect(s), each must turn the drive RED"
          + (f" (only {only})" if only else ""))
    for name, old, new, marker in picked:
        d = tmp / "mutants" / name
        d.mkdir(parents=True, exist_ok=True)
        p = d / "point_to.py"
        if old not in src:
            print(f"  MUTANT {name}: the anchor string is no longer in the source — NOT APPLIED")
            ok = False
            continue
        p.write_text(src.replace(old, new, 1))
        log = d / "run.log"
        env = dict(os.environ)
        env.pop("XAUTHORITY", None)
        env.pop("WAYLAND_DISPLAY", None)
        env["DISPLAY"] = display
        env["PYTHONPATH"] = os.pathsep.join([str(JARVIS), str(OPERATOR)])
        try:
            r = subprocess.run([sys.executable, __file__, "--overlay-src", str(p)],
                               env=env, capture_output=True, text=True, timeout=420)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            rc, out, err = 124, (e.stdout or ""), (e.stderr or "")
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
        blob = (out or "") + (err or "")
        log.write_text(blob)
        red = rc != 0 and marker in blob
        print(f"  MUTANT {name}: exit={rc} expected_marker={marker!r} "
              f"{'RED (caught)' if red else 'GREEN (NOT CAUGHT)'}")
        if not red:
            ok = False
            print("    last lines:", "\n    ".join(blob.splitlines()[-8:]))
    print(f"prove-fail: {'PASS — every injected defect was caught' if ok else 'FAIL — a guard is vacuous'}")
    return 0 if ok else 1


# ── main ────────────────────────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overlay-src", default=str(OVERLAY),
                    help="the point_to.py to drive (used by --prove-fail mutants)")
    ap.add_argument("--prove-fail", action="store_true")
    ap.add_argument("--only", default="", help="run one named prove-fail mutant")
    ap.add_argument("--keep-display", action="store_true")
    a = ap.parse_args(argv)

    if not shutil.which("Xvfb") or not shutil.which("xdotool"):
        print("SKIP: Xvfb and xdotool are required for an isolated drive; not found")
        return 2
    try:
        atspi()
    except Exception as e:  # noqa: BLE001
        print(f"SKIP: AT-SPI is not available ({e.__class__.__name__}: {e})")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="bd-point-to-drive-"))
    print(f"test dir: {tmp}")

    # choose a free display, start Xvfb and a window manager (the overlay needs a WM for raise)
    disp = None
    for n in range(86, 99):
        if not Path(f"/tmp/.X11-unix/X{n}").exists():
            disp = f":{n}"
            break
    if disp is None:
        print("SKIP: no free X display in :86..:98")
        return 2
    xvfb = subprocess.Popen(["Xvfb", disp, "-screen", "0", "1600x1000x24", "-nolisten", "tcp"],
                            stdout=open(tmp / "xvfb.log", "ab"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + 10
    while time.time() < end and not Path(f"/tmp/.X11-unix/X{disp[1:]}").exists():
        time.sleep(0.1)
    if xvfb.poll() is not None:
        print("SKIP: Xvfb did not start")
        return 2
    wm = None
    if shutil.which("xfwm4"):
        wm = subprocess.Popen(["xfwm4", "--replace"], env={**os.environ, "DISPLAY": disp},
                              stdout=open(tmp / "xfwm4.log", "ab"), stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(1.0)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler)
    port = srv.server_address[1]
    import threading as _t
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    PANEL.set_geom(200, 100)

    if a.prove_fail:
        try:
            return prove_fail(tmp, disp, port, a.only)
        finally:
            srv.shutdown()
            for p in (wm, xvfb):
                if p is not None:
                    p.terminate()
            if not a.keep_display:
                pass

    drv = Drive(tmp, disp, port, Path(a.overlay_src))
    try:
        drv.write_fixture()
        # ★ PREFLIGHT: the fixture must select a DISCONNECTED backend before a single click, or a
        #   verdict would spend a real model call. If the box's dispatch rows changed, stop here.
        probe = subprocess.run(
            [sys.executable, "-c",
             "import guide_backends as g, json; d=g.inventory(); s=d.get('selected') or {}; "
             "print(json.dumps({'id': s.get('id'), 'state': s.get('state')}))"],
            env=drv.env(), capture_output=True, text=True)
        try:
            sel = json.loads((probe.stdout or "").strip().splitlines()[-1])
        except Exception:
            sel = {}
        if sel.get("id") != "dispatch:prime" or sel.get("state") != "disconnected":
            print(f"SKIP: the isolated backend is not disconnected as required: {sel!r} "
                  f"({(probe.stderr or '')[-200:]})")
            return 2
        print(f"preflight: selected {sel.get('id')} is {sel.get('state')} — a verdict cannot spend")
        if not drv.start_reference():
            print("SKIP: the X11 reference window did not appear")
            return 2
        print("drive: launching the real overlay and clicking the real card via AT-SPI")
        passed = drv.run()
    except AssertionError as e:
        drv.failures.append(str(e))
        print(f"  FAIL: {e}")
        passed = False
    except Exception as e:  # noqa: BLE001
        drv.failures.append(f"{e.__class__.__name__}: {e}")
        print(f"  FAIL: {e.__class__.__name__}: {e}")
        passed = False
    finally:
        drv.stop()
        srv.shutdown()
        for p in (wm, xvfb):
            if p is not None and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except Exception:
                    p.kill()
        print(f"artefacts kept under {tmp}")

    if drv.failures:
        print(f"FAILED ({len(drv.failures)}):")
        for f in drv.failures:
            print("  -", f)
        return 1
    print("PASS: geometry feed, tracking, verdict labels/journal/delivery, front (panel + X11), "
          "agent tabs/Ask/Diagram refusal states, live-chat link-only, the ONE turnkey leave card "
          "(four choices, Stay/backlog/Stop-now driven), stale and first-run recovery — all driven "
          "through AT-SPI on the real widgets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
