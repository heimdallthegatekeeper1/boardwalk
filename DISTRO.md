# BoardWalk — distribution and launchers

**One app, a launcher chosen at install, a desktop icon with the BoardWalk mark.**

```bash
python3 install.py          # choose Linux / macOS / Windows
python3 install.py --dry-run
python3 launchers/boardwalk_launcher.py --check
python3 launchers/boardwalk_launcher.py        # start it directly
```

The installer never downloads packages, never uses sudo and never touches the network. It creates a
shortcut that runs `launchers/boardwalk_launcher.py`, which finds a free loopback port, starts
`panel_server.py`, waits for the board, and opens the default browser at
`http://127.0.0.1:<port>/?section=operator-needs`.

## What the launcher does

| OS | launcher file | what it creates |
|---|---|---|
| **Linux** | `launchers/BoardWalk.desktop` | `~/.local/share/applications/boardwalk.desktop` + `~/Desktop/BoardWalk.desktop` when a Desktop dir exists |
| **macOS** | `launchers/BoardWalk.command` | `~/Applications/BoardWalk.app` (Info.plist, executable, `.icns`) |
| **Windows** | `launchers/BoardWalk.bat` | Start Menu `BoardWalk.lnk` and a Desktop shortcut (via PowerShell) |
| **a foreign OS** | staged `dist/<os>/` | the launcher files to copy to that machine, then run its `install.py` |

The Python launcher is the same on all three: `launchers/boardwalk_launcher.py`.

## The honest OS matrix

| | panel / board / walk | grant wizard | click-through desktop overlay | per-window OCR pointing |
|---|---|---|---|---|
| **Linux/X11** | yes | yes | **yes** | **yes** |
| **Linux/Wayland** | yes | yes | XWayland windows only; a Wayland-native window has no XID | region/screen only |
| **macOS** | yes (browser) | yes | **no native** — XQuartz/X11 only | **no native** in this release |
| **Windows** | yes (browser) | yes | **no native** — an X server (WSL/X) is required, unsupported | **no native** in this release |

**Do not read "the panel launched" as "the overlay works here."** The overlay is an X11 window with
an XShape input region; that is a Linux/X11 feature in this release. The board, the walk, the grant
wizard and the privacy gate are portable.

## Dependencies (install these yourself)

**Linux (Debian/Ubuntu names)**
```bash
sudo apt install python3 python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 python3-cairo      tesseract-ocr imagemagick x11-utils
# optional helpers
sudo apt install xdotool wmctrl
# optional, for the machine-driven card test (no operator needed)
sudo apt install xvfb xfwm4 xdotool
```
Python: 3.11+. The panel itself is standard library only. The overlay needs PyGObject/GTK3 + pycairo.
`gir1.2-atspi-2.0` is what lets an accessibility client (and the included test suite) press the real
card buttons; it is not needed for the overlay to run by hand.

**macOS**
```bash
brew install python3 pygobject3 gtk+3 tesseract imagemagick
# overlay only:
brew install --cask xquartz
```
The panel runs in the browser; the overlay is not native.

**Windows**
- Python 3 from python.org (check *Add python.exe to PATH*).
- Tesseract OCR and ImageMagick installers for Windows (for `point-at`).
- GTK3/PyGObject via MSYS2 if you need the overlay — and an X server; **not supported natively**.

## Panel position: the one thing to know before the first overlay

The panel is a browser page. A Wayland-native browser has no X window id, so `xwininfo` cannot see
it. The page therefore **reports its own `screenX/screenY/outer/inner`** to
`POST /operator/window-geometry`, and the overlay reads that to place a window-relative ring.

- If the panel has not reported yet, the overlay still opens and says **“waiting for the panel to
  report its position”**. It is not broken: **hard-reload the panel page once (Ctrl+Shift+R)** and
  the overlay re-aims within a second, with no restart.
- A report older than 90 s is marked **stale** on the card. The ring stays on the last known
  position and the card tells you to reload if the panel moved.
- Move the panel: the 700 ms tracker follows it and the card says **“target moved — re-aimed”**.
  A step authored with `track: off` says **“anchored … (tracking off)”** instead.

## What the card can do

`✓ right spot` / `✗ wrong spot` record a verdict and forward it through the one guide-agent delivery
path; the mirror under the card says **queued** or **NOT delivered** with the reason. `✗ wrong spot`
re-aims through window → anchor → OCR → title and raises an X11 target. **`⤴ front`** raises an X11
window, or asks the panel page to focus/scroll/flash the item; a Wayland compositor may still keep
another window on top, and the page's toast says so. **`◧ agent / explain`** opens Explain · Ask ·
Diagram · Agent. A **live DSH chat is link-only** while it is open (its `session.lock` is held); the
Agent tab marks it so and offers **Open DSH**, and Ask/Diagram go to the selected answerer.

## The test item

The release ships a safe fake board row, **“Take the BoardWalk feature tour (fake test item)”**, and
`operator/walks/feature-tour.json`. It exercises the board, the authored walk, the overlay, the
agent-authored illustration and the cancel. Start the panel, open `Point me to there`, choose
`feature-tour.json`, and press **show it on my screen**.

## Machine-driving the card (no operator)

`jarvis/test_point_to_drive.py` starts its own Xvfb display + xfwm4, a fake panel-geometry server,
and the real overlay, then presses the real GTK buttons through **AT-SPI** (not XTEST, which does not
reach GTK on this box) and reads the real widget labels back. It uses a temporary disconnected guide
backend, so it never spends a model call and never touches the operator's state.

```bash
python3 jarvis/test_point_to_drive.py              # PASS / FAIL
python3 jarvis/test_point_to_drive.py --prove-fail # each injected defect must go RED
```

It exercises: geometry feed, tracker re-aim, right/wrong verdict labels + journal + refused
delivery, front on the panel and on a real X11 window, the four agent tabs, Ask/Diagram refusal
states, the live-chat link-only text, cancel, stale geometry, and first-run recovery.

## Launcher files

| path | purpose |
|---|---|
| `launchers/boardwalk_launcher.py` | cross-OS engine: free port, start panel, open browser, Ctrl-C stop |
| `launchers/BoardWalk.desktop` | Linux template (`__APP_DIR__` substituted by the installer) |
| `launchers/BoardWalk.command` | macOS double-clickable wrapper |
| `launchers/BoardWalk.bat` | Windows wrapper |
| `launchers/BoardWalk.sh` | Linux/Unix terminal wrapper |
| `launchers/icons/boardwalk.png/.ico/.icns` | the BoardWalk mark for each desktop |
| `install.py` | the installer/chooser |

## Uninstalling

Linux: delete `~/.local/share/applications/boardwalk.desktop` and, if present,
`~/Desktop/BoardWalk.desktop`; stop the server with Ctrl-C in the terminal that launched it.
macOS: delete `~/Applications/BoardWalk.app`. Windows: delete the two shortcuts. The release
directory itself is the app; nothing else is written except `launcher.log` and the release's own
runtime stores.
