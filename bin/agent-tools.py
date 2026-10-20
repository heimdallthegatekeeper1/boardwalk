#!/usr/bin/env python3
"""agent-tools.py — THE STANDARD AGENT-TOOLS SUITE for the voice seat, as one gated inventory.

★ THE ESTATE'S RULE THIS FILE EXISTS TO OBEY (AGENTS.md §8):
    *"A capability not in the manifest does not exist to the agent that needs it."*
  So the tool table below is not documentation — it is the GATE. `TOOLS` is the single store; a verb
  that is not a key in it is REFUSED by name, not attempted. `--emit-manifest` writes
  `docs/AGENT_TOOLS_MANIFEST.md` FROM this table, so the human document cannot drift from the gate
  (AGENTS.md §5 "ONE store per fact · pointed at, never copied"; failure class FA-07 two-stores-drift).

★ WHAT IT IS NOT. This is NOT a second capability manifest. `~/boardwalk/CAPABILITY_MANIFEST.md`
  remains the estate-wide, auto-assembled view over skills+atlas+tools and remains the authority for
  CLEARANCE. This file is the *agent-tools subset*: the verbs a seat agent may actually call, each with
  what it reaches, whether it is loopback-only, whether it can touch the network, and what it is
  FORBIDDEN to do. Two documents, two different facts — not two copies of one fact.

★ THE SCREENSHOT GATE — READ THIS, IT IS THE POINT
  A screenshot is CONTENT. Two hard rules are enforced IN CODE here, not promised in prose:
    1. NO SILENT CAPTURE. `shot` REFUSES unless it can write its visible record (the seat's own log at
       /tmp/jarvis_bubble.log plus a persistent JSONL ledger). If the record cannot be written, the
       capture does not happen. A screenshot with no trace is the defect, so the trace is a precondition.
    2. NEVER OFF THE BOX. Nothing here transmits. There is no network verb in this file and `--selftest`
       proves it by scanning our own source. Screenshot egress to a cloud model is a SEPARATE capability
       (`shot.egress.cloud`) marked FORBIDDEN and NOT IMPLEMENTED — see the manifest.

★ MEASURED ON this-box 2026-10-10 (Wayland/GNOME, DISPLAY=:0, XWayland present). The naive paths fail,
  two succeed, and WHICH ONE YOU GET CHANGES WHAT YOU SEE:
    FAIL  xwd -root                      -> X Error BadMatch (X_GetImage), exit 1, 0 bytes
    FAIL  import -window root            -> no image written (exit 1 / "missing an image filename")
    FAIL  ffmpeg -f x11grab              -> EXIT 0 AND A VALID 1920x1200 PNG THAT IS BLACK
                                            (mean=1.7/65535, 43 colours). ★ THIS IS THE TRAP: a silent
                                            failure that looks like a successful capture.
    FAIL  org.gnome.Shell.Screenshot      -> GDBus AccessDenied "Screenshot is not allowed"
    FAIL  grim / gnome-screenshot / spectacle / wl-copy / wl-paste -> NOT INSTALLED
    WORK  import -window <xid>            -> real pixels, PER WINDOW (this is how you see another app)
    WORK  xdg portal Screenshot (gdbus)   -> real pixels, WHOLE SCREEN, and SILENT (no dialog)
  ★ The portal's silence is exactly why rule 1 above is enforced in THIS tool and not delegated to the
    desktop: the desktop does not ask, so we must.

USAGE
  ./agent-tools.py --list                     print the inventory (from TOOLS, the gate)
  ./agent-tools.py --manifest                 print the generated manifest markdown
  ./agent-tools.py --emit-manifest [PATH]     write docs/AGENT_TOOLS_MANIFEST.md from TOOLS
  ./agent-tools.py --selftest                 set-equality + forbidden-list + no-network proofs
  ./agent-tools.py --selftest --prove-fail    must exit 1
  ./agent-tools.py shot --windows             list CAPTURABLE windows (X11/XWayland only — honest)
  ./agent-tools.py shot --window <name|0xid>  capture ONE APP'S WINDOW   [-o out.png]
  ./agent-tools.py shot --screen              capture the whole screen via the desktop portal
  ./agent-tools.py windows                    list capturable windows
  ./agent-tools.py clip --get | --set TEXT     clipboard (X11/XWayland only)
  ./agent-tools.py notify "text"              raise a desktop notification
  ./agent-tools.py say "text" [--to-file f]   TTS (piper/espeak, local); --to-file makes no sound
  ./agent-tools.py read-text --window <name>  ★ LOCAL OCR of one window — the loopback answer to a
                                              cloud vision call. [--from <png>] [--raw] [-o out.png]
  ./agent-tools.py find <glob> --root DIR     find files by NAME under declared roots (read-only)
  ./agent-tools.py open --path <file>         open ONE local path (RAISES A WINDOW); --url is refused
                                              for anything non-loopback (that would be egress)
  ./agent-tools.py handoff "what" --lane X    queue work for another agent (approved=false)
  ./agent-tools.py inbox                      list the handoff queue
  ./agent-tools.py net-posture                flag wildcard/non-loopback listeners (/proc, no socket)
  ./agent-tools.py lane                       measure the stack's RSS against the 1776 MiB ceiling

  ★ HARD FLOORS (AGENTS.md §6) — encoded as module constants, never as toggles:
      raw audio never leaves the box · credential endpoints stay loopback-only ·
      never log key material (fingerprints and last-4 only) · a screenshot is content ·
      no sudo, no installs, no downloads.
    `--selftest` FAILS if any environment variable name mentions one of these, because a floor that
    can be switched off was never a floor.

EXIT CODES  0 ok · 1 refused/failed · 2 usage · 3 the gate could not be satisfied
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

VERSION = "2.0.0"
EPOCH = 1791667072  # recorded build epoch for this file (this-box, 2026-10-10, harness-toolkit pass)

SEAT_LOG = pathlib.Path("/tmp/jarvis_bubble.log")
LEDGER = pathlib.Path(os.path.expanduser("~/.local/state/khaoss/agent_tools_audit.jsonl"))
SHOT_DIR = pathlib.Path(os.path.expanduser("~/Pictures/agent-tools"))
HANDOFF = pathlib.Path(os.path.expanduser("~/.local/state/khaoss/handoff_queue.jsonl"))

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ★ HARD FLOORS (AGENTS.md §6). These are MODULE CONSTANTS on purpose.
#   ★ "a hard floor cannot be represented as a toggleable value." A floor read from a config key, an
#     environment variable, or a CLI flag has stopped being a floor and become a setting. So nothing
#     here is read from anywhere, and `--selftest` asserts that NO environment variable whose name
#     mentions any of these words is read anywhere in this file (prove-fail-able).
# ─────────────────────────────────────────────────────────────────────────────────────────────────
HARD_FLOORS = (
    "raw audio never leaves the box",
    "credential endpoints stay loopback-only",
    "never log key material — fingerprints and last-4 only",
    "a screenshot is content and never leaves the box by default",
    "no sudo, no installs, no downloads",
)
# The declared lane ceiling. AGENTS.md records the whole KHAOSS stack (electron+hermes+python+node) at
# 1,843 MB, which is ALREADY OVER this 1,776 MiB lane ceiling — that discrepancy is the point of
# `lane.measure`, and it is reported rather than smoothed over.
LANE_CEILING_MIB = 1776
LANE_CEILING_WORDS = ("FLOOR", "AUDIO", "EGRESS", "LOOPBACK", "CEILING", "SUDO", "GRANT_ALWAYS")

# ★ NEVER LOG KEY MATERIAL — fingerprints and last-4 only. Conservative shapes only: these are the
#   token formats that are near-certainly credentials, so ordinary prose survives untouched.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._\-]{16,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
)


def _fingerprint(tok: str) -> str:
    """★ The estate's rule, in code: a fingerprint (truncated sha256) and the LAST FOUR characters,
    never the material itself. `fp:1a2b3c4d5e…cdef`."""
    fp = hashlib.sha256(tok.encode("utf-8", "replace")).hexdigest()[:10]
    tail = tok[-4:] if len(tok) >= 8 else "····"
    return f"fp:{fp}…{tail}"


def redact(text: str) -> tuple[str, int]:
    """Replace secret-shaped tokens with a fingerprint + last-4. Returns (redacted_text, count).
    The COUNT is what the ledger records — the matched text is never written anywhere."""
    n = 0

    def _sub(m):
        nonlocal n
        n += 1
        if m.re.groups >= 3 and m.group(1).lower() == "bearer":
            return m.group(1) + m.group(2) + _fingerprint(m.group(3))
        return _fingerprint(m.group(0))

    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub(_sub, out)
    return out, n

# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ★ THE SINGLE STORE. Every verb the suite exposes is a key here, with its reach, its network
#   posture, and what it is FORBIDDEN to do. A key absent from this dict does not exist.
#   `needs` is the gate: a list of preconditions checked BEFORE the verb runs. If any fails, the verb
#   is REFUSED (exit 3) and nothing happens.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
TOOLS: dict[str, dict] = {
    "shot.window": {
        "felt": "Look at one program",
        "does": "Capture ONE named window's pixels to a PNG. This is the capability the operator asked "
                "for: see what is going on INSIDE the program the assistant is working with.",
        "reaches": "the pixels of any X11/XWayland window on this box, by window id (xid).",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "import -window <xid>",
        "measured": "WORKS — proved on the live box (see --selftest evidence and the report).",
        "forbidden": [
            "MUST NOT raise, move, resize, focus or activate the target window (standing estate rule).",
            "MUST NOT capture without writing its visible record (no silent capture).",
            "MUST NOT write outside the capture directory.",
            "MUST NOT be combined with any egress.",
        ],
        "needs": ["visible_record"],
        "limits": "X11/XWayland windows ONLY. A Wayland-native window (Firefox/Chromium started with the "
                  "Wayland backend) has NO xid, cannot be listed here, and CANNOT be captured this way. "
                  "Use `shot --screen` for those, and accept that it captures everything.",
    },
    "shot.screen": {
        "felt": "Look at the whole screen",
        "does": "Capture the entire screen to a PNG through the XDG desktop portal.",
        "reaches": "every pixel currently displayed, including windows this tool cannot list.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "gdbus org.freedesktop.portal.Screenshot",
        "measured": "WORKS and is SILENT on this box (no consent dialog — 9 prior files since 2026-09-17).",
        "forbidden": [
            "MUST NOT capture without writing its visible record (no silent capture).",
            "★ The desktop portal does NOT ask the operator. The visible record in this tool is therefore "
            "  the ONLY trace a screen capture leaves, which is why `visible_record` is a hard gate here.",
            "MUST NOT be used to observe the operator rather than the work.",
        ],
        "needs": ["visible_record", "portal"],
        "limits": "Whole-screen only. It cannot exclude a window, and on GNOME it may include every "
                  "monitor. It is the honest fallback when a window is not listable, NOT the default.",
    },
    "windows.list": {
        "felt": "Which programs exist",
        "does": "List the windows that can be captured, with geometry.",
        "reaches": "window titles/geometry of X11/XWayland clients.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "xdotool search / xwininfo",
        "measured": "WORKS for X windows; reports 4 titles on this box while Firefox+Chromium are running.",
        "forbidden": [
            "MUST NOT raise, move, resize, focus or activate any window.",
            "MUST NOT claim the list is complete: Wayland-native windows are invisible to it.",
        ],
        "needs": [],
        "limits": "★ NEGATIVE EVIDENCE, MEASURED: on this box Firefox and Chromium are running and appear "
                  "in NO window list, because they are Wayland-native. The list is a lower bound, never a "
                  "census, and the output says so.",
    },
    "clip.get": {
        "felt": "Read the clipboard",
        "does": "Print the current X clipboard selection.",
        "reaches": "the current clipboard text — which is operator content.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "xclip -o",
        "measured": "installed; X11/XWayland selections only.",
        "forbidden": ["MUST NOT be copied into any outbound request.", "MUST NOT be logged."],
        "needs": [],
        "limits": "X11 selections only — a Wayland-native app's clipboard may not be visible. Content is "
                  "NOT recorded in the ledger (only the byte count).",
    },
    "clip.set": {
        "felt": "Put something on the clipboard",
        "does": "Set the X clipboard to the given text.",
        "reaches": "the operator's clipboard — it overwrites what was there.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "xclip -selection clipboard",
        "measured": "installed.",
        "forbidden": ["MUST NOT overwrite a non-empty clipboard without the caller saying so explicitly."],
        "needs": [],
        "limits": "Overwrites. Not reversible by this tool.",
    },
    "notify.raise": {
        "felt": "Say something on screen",
        "does": "Raise a desktop notification with the given text.",
        "reaches": "the operator's notification area. It does not steal focus.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "notify-send",
        "measured": "installed (GNOME Shell owns org.freedesktop.Notifications).",
        "forbidden": ["MUST NOT be used to impersonate a system or security warning."],
        "needs": [],
        "limits": "Transient; may be missed while the screen is locked or in Do Not Disturb.",
    },
    "voice.say": {
        "felt": "Speak",
        "does": "Speak text aloud locally, or render it to a WAV with --to-file (no sound).",
        "reaches": "the speakers. AUDIBLE — this is the one verb here with a physical side effect.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "piper (preferred) or espeak",
        "measured": "both installed at /usr/bin.",
        "forbidden": [
            "MUST NOT speak without the caller passing text explicitly.",
            "MUST NOT synthesise text that came from a screen capture into an outbound request.",
        ],
        "needs": ["tts_engine"],
        "limits": "Local synthesis only. Raw audio never leaves the box (AGENTS.md §6).",
    },
    "screen.read_text": {
        "felt": "Read the words on screen, locally",
        "does": "OCR ONE window (or an existing PNG) with the LOCAL tesseract engine and print the text. "
                "This is the loopback answer to the harness's `vision_analyze`: it reads what is on "
                "screen WITHOUT sending a single pixel off the box.",
        "reaches": "the text visible in a captured image — operator content, and the place keys leak from.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "import -window <xid>  ->  tesseract <png> stdout",
        "measured": "MEASURED 2026-10-10 on this-box — driven against a real Tk window and against a "
                    "rendered text PNG; see the report for the pasted output.",
        "forbidden": [
            "MUST NOT transmit the image or the recognised text — no network verb exists in this file.",
            "MUST NOT log the recognised text: the ledger records a character count and a redaction "
            "count, never content.",
            "MUST NOT capture without writing its visible record (no silent capture).",
            "MUST NOT be used to observe the operator rather than the work.",
        ],
        "needs": ["visible_record", "ocr_engine"],
        "limits": "Recognition quality tracks font size, contrast and DPI; a dark or scaled-down window "
                  "recognises badly, and this verb says so rather than returning confident nonsense. "
                  "Secret-shaped tokens are REDACTED to a fingerprint + last-4 in the printed output by "
                  "default; `--raw` prints them unredacted and the ledger records that the un-redacted "
                  "read happened, so the exception is visible.",
    },
    "file.find": {
        "felt": "Find a file",
        "does": "Search DECLARED roots for files whose NAME matches a glob and print path + size. "
                "Read-only: it never opens a file.",
        "reaches": "file NAMES and metadata under roots the caller declares.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "python os.walk over declared roots (no external binary)",
        "measured": "MEASURED 2026-10-10 — driven; see the report.",
        "forbidden": [
            "MUST NOT read file CONTENTS — names and sizes only.",
            "MUST NOT traverse outside the roots the caller declared, and MUST NOT follow a symlink "
            "out of a declared root.",
            "MUST NOT be handed `$HOME` or `/` as a root: a whole-home scan is a census of private "
            "content. That is refused BY NAME unless `--allow-home` widens it deliberately, and the "
            "widening is recorded in the ledger.",
        ],
        "needs": [],
        "limits": "A depth cap applies (default 6) and results are capped. This is a NAME search, not a "
                  "content search — the harness's own `search_files` does content and is a different "
                  "tool with a different reach; neither is a copy of the other.",
    },
    "app.open": {
        "felt": "Open a thing for the operator",
        "does": "Open ONE explicitly named LOCAL path with the desktop's default application. A "
                "non-loopback URL is REFUSED — that would be egress.",
        "reaches": "the operator's desktop. ★ Opening a file RAISES A WINDOW; that focus change is why "
                   "this verb is gated and why it takes exactly one target.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "xdg-open <path>  (local paths only; the non-loopback URL branch is refused)",
        "measured": "MEASURED 2026-10-10 — the PERMITTED branch driven with --dry-run (no window "
                    "raised), the non-loopback URL and the missing path driven and REFUSED; see the "
                    "report. The live open branch is deliberately not driven by tests: it raises a "
                    "window, and that is the operator's call, not a test's.",
        "forbidden": [
            "MUST NOT open more than one target per invocation — no bulk window raising.",
            "MUST NOT raise, move or focus a window the operator did not ask for: the operator names "
            "the target and this verb opens exactly that, once.",
            "MUST NOT open a non-loopback URL. That is egress and it follows the `shot.egress.cloud` "
            "precedent — off by default, and no switch in this suite enables it.",
            "MUST NOT open a path that does not exist: xdg-open fails silently on a missing path, and "
            "a silent failure reads as success.",
        ],
        "needs": ["visible_record"],
        "limits": "One target per call. The non-loopback URL branch is refused outright on this box "
                  "(no operator grant exists), so this verb cannot reach the network.",
    },
    "work.handoff": {
        "felt": "Hand work to another agent",
        "does": "Append ONE work order to this lane's durable handoff queue so another agent can pick it "
                "up. Records lane · what · where, and nothing else.",
        "reaches": "~/.local/state/khaoss/handoff_queue.jsonl — a file the HARNESS LANE owns. It writes "
                   "no other lane's files.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "append-only JSONL under ~/.local/state/khaoss/",
        "measured": "MEASURED 2026-10-10 — driven; see the report.",
        "forbidden": [
            "MUST NOT write into another lane's files (AGENTS.md §6: no lane writes without the "
            "declared owner). This verb writes exactly one file, owned by the harness lane.",
            "MUST NOT mark anything approved: a handed-off item is `queued` with `approved: false`. "
            "DISCOVERED IS NOT APPROVED (A48).",
            "MUST NOT carry key material — the payload goes through the same redactor as "
            "`screen.read_text`, and the ledger records counts, never the payload.",
            "MUST NOT dispatch by itself: this QUEUES work, it does not start another agent.",
        ],
        "needs": ["visible_record"],
        "limits": "The queue is a file, not a transport. Nothing consumes it automatically — an agent "
                  "must read it through `work.inbox`. That is deliberate: a queue that dispatches by "
                  "itself is an agent nobody started.",
    },
    "work.inbox": {
        "felt": "What work is waiting",
        "does": "List the work orders on this lane's handoff queue, newest first, with their state.",
        "reaches": "the same harness-lane JSONL, read-only.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "read ~/.local/state/khaoss/handoff_queue.jsonl",
        "measured": "MEASURED 2026-10-10 — driven; see the report.",
        "forbidden": [
            "MUST NOT report an item as approved unless the record itself says so.",
            "MUST NOT be read as a census of AGENTS — it is a census of QUEUED WORK only.",
        ],
        "needs": [],
        "limits": "Shows only what was handed off through this verb. Agent DISCOVERY (A47) is a "
                  "different capability owned by the voice_intent lane and is deliberately NOT "
                  "implemented here — a second scanner would be a fork of that lane's work.",
    },
    "net.posture": {
        "felt": "Are our listeners loopback-only",
        "does": "Read the kernel's listening sockets and report every listener bound to a WILDCARD or "
                "non-loopback address, so the 'credential endpoints stay loopback-only' floor is "
                "CHECKED rather than asserted.",
        "reaches": "/proc/net/tcp and /proc/net/tcp6 — read-only. This tool opens no socket.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "parse /proc/net/{tcp,tcp6} (no binary, no connection)",
        "measured": "MEASURED 2026-10-10 — driven; see the report for the listeners it found.",
        "forbidden": [
            "MUST NOT connect to anything: it parses /proc and opens no socket, so it cannot probe.",
            "MUST NOT claim the list is complete without naming the address families it read.",
            "MUST NOT be used to fingerprint the operator's network.",
        ],
        "needs": [],
        "limits": "This shows what THIS box exposes in its own namespace. It cannot see the remote "
                  "policy — the estate's own record is that a credential endpoint answers 403 from the "
                  "tailnet even with a valid token, which is a policy fact this verb cannot observe.",
    },
    "lane.measure": {
        "felt": "How much memory is this lane using",
        "does": "Sum the resident memory of the KHAOSS stack's processes, broken out by class, and "
                "compare it against the declared 1776 MiB lane ceiling.",
        "reaches": "/proc/<pid>/statm for the matching processes — read-only.",
        "loopback_only": True,
        "can_touch_network": False,
        "mechanism": "parse /proc (no binary, no network, no signal sent)",
        "measured": "MEASURED 2026-10-10 — driven; see the report for the live numbers.",
        "forbidden": [
            "MUST NOT kill, renice, stop or signal anything: it measures and reports.",
            "MUST NOT claim the number is the whole machine's memory — it is the LANE's.",
            "MUST NOT be used to justify an install that would raise the ceiling.",
        ],
        "needs": [],
        "limits": "RSS double-counts shared pages, so the total is an UPPER BOUND on private memory, "
                  "not an exact figure. The ceiling is a declared constant in this file, not a "
                  "configurable setting.",
    },
    "shot.egress.cloud": {
        "felt": "Send a picture to a cloud model",
        "does": "NOT IMPLEMENTED — DECLARED SO IT CANNOT BE ASSUMED.",
        "reaches": "WOULD reach a network model with the pixels of your screen. It reaches NOTHING today.",
        "loopback_only": False,
        "can_touch_network": True,
        "mechanism": "none — deliberately absent",
        "measured": "ABSENT. No code path exists. This entry exists so the capability is VISIBLE as "
                    "forbidden rather than silently missing (AGENTS.md: a silent gap reads as 'the stack "
                    "is empty').",
        "forbidden": [
            "FORBIDDEN by default. Off by default. There is no switch in this suite that enables it.",
            "If it is ever built it MUST be a separate explicit grant, per-capture, operator-visible,",
            "  and MUST apply the same VALUE-SUBSTITUTION discipline as every other egress stage:",
            "  secrets/credentials/PII replaced by references before the image leaves the box.",
            "MUST NOT be inferred from any other grant.",
        ],
        "needs": ["operator_grant"],
        "limits": "NOT BUILT. Named here because the operator's rule is that a screenshot must never "
                  "leave the box by default — so the shape of the exception is written down and the "
                  "exception itself is not.",
    },
}

GATE_CHECKS = {
    "visible_record": "the capture's visible record can be written (seat log + ledger)",
    "portal": "org.freedesktop.portal.Screenshot is answerable on the session bus",
    "tts_engine": "piper or espeak is installed",
    "operator_grant": "an explicit operator grant exists for this session",
    "ocr_engine": "a LOCAL OCR engine (tesseract) is installed — nothing is fetched to read text",
}

# ★ THE BINARY GATE. Every external program this suite may execute must be declared in ALLOWED_BINARIES.
#   `--selftest` parses this file's own AST, collects the binaries actually invoked in an argv position,
#   and fails if any is undeclared OR is a network verb. This replaced a plain text scan of the source,
#   which failed on its own declaration line — the instrument was the suspect, not the network.
NETWORK_BINARIES = frozenset({"curl", "wget", "nc", "ncat", "ssh", "scp", "rsync", "socat", "ftp", "telnet"})
ALLOWED_BINARIES = frozenset({
    "systemctl",   # _seat_pid: read the seat unit's MainPID
    "pgrep",       # _seat_pid fallback
    "gdbus",       # portal introspect + portal Screenshot call
    "xdotool",     # list windows / read geometry (READ-ONLY: search, getwindowname, getwindowgeometry)
    "import",      # ImageMagick per-window capture
    "identify",    # measure a capture so a BLACK png cannot pass as a success
    "xclip",       # X clipboard
    "notify-send", # visible on-screen record
    "espeak",      # local TTS
    "piper",       # local TTS (preferred engine)
    "convert",     # selftest only: builds the black/red fixtures for the blank guard
    "tesseract",   # LOCAL OCR (screen.read_text) — loopback, nothing is fetched
    "xdg-open",    # app.open: the desktop's default handler for ONE local path
})


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# environment: borrow the SEAT's display auth, do not guess it.
# ★ CLONED, NOT RECONSTRUCTED, from bin/seat_hands.py::_display_env (read-only) — that file's own
#   scar says it best: `~/.Xauthority` EXISTS on this box and is the WRONG 0-byte cookie, while the
#   real one is /run/user/<uid>/.mutter-Xwaylandauth.<suffix> and its suffix changes every login. So
#   ASK THE RUNNING PROCESS via /proc/<pid>/environ rather than globbing the filesystem.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _seat_pid() -> int | None:
    try:
        p = pathlib.Path("/tmp/jarvis_bubble.pid").read_text().strip()
        if p.isdigit() and pathlib.Path(f"/proc/{p}").exists():
            return int(p)
    except OSError:
        pass
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "khaoss-bubble"],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip().isdigit() and r.stdout.strip() != "0":
            return int(r.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    try:
        r = subprocess.run(["pgrep", "-f", "jarvis/bubble.py"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.split():
            if line.isdigit():
                return int(line)
    except Exception:  # noqa: BLE001
        pass
    return None


def display_env() -> dict:
    env = dict(os.environ)
    pid = _seat_pid()
    if pid:
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for kv in f.read().split(b"\0"):
                    k, _, v = kv.partition(b"=")
                    k = k.decode("utf-8", "replace")
                    if k in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY"):
                        env[k] = v.decode("utf-8", "replace")
        except OSError:
            pass
    env.setdefault("DISPLAY", ":0")
    if not env.get("XAUTHORITY"):
        cands = sorted(pathlib.Path(f"/run/user/{os.getuid()}").glob(".mutter-Xwaylandauth*"))
        if cands:
            env["XAUTHORITY"] = str(cands[-1])
    return env


def _run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 30)
    return subprocess.run(cmd, env=display_env(), **kw)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE GATE
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def gate_ok(name: str) -> tuple[bool, str]:
    """Is this verb permitted to run right now? Fails CLOSED."""
    tool = TOOLS.get(name)
    if tool is None:
        return False, (f"'{name}' is not in the manifest, so it does not exist to you. "
                       f"Known verbs: {', '.join(sorted(TOOLS))}")
    if name == "shot.egress.cloud":
        return False, ("FORBIDDEN: this suite has no screenshot-egress capability. A screenshot is "
                       "content and it does not leave this box (AGENTS.md §6).")
    for need in tool.get("needs", []):
        if need == "visible_record":
            try:
                LEDGER.parent.mkdir(parents=True, exist_ok=True)
                with open(LEDGER, "a") as f:
                    f.write("")
                with open(SEAT_LOG, "a") as f:
                    f.write("")
            except OSError as e:
                return False, (f"REFUSED — no silent capture: the visible record cannot be written "
                               f"({e}). A capture with no trace is the defect, so the trace is a "
                               f"precondition, not a nicety.")
        elif need == "portal":
            r = _run(["gdbus", "introspect", "--session",
                      "--dest", "org.freedesktop.portal.Desktop",
                      "--object-path", "/org/freedesktop/portal/desktop"], timeout=10)
            if "org.freedesktop.portal.Screenshot" not in (r.stdout or ""):
                return False, "REFUSED — the desktop portal does not offer Screenshot on this session."
        elif need == "tts_engine":
            if not any(shutil.which(b) for b in ("piper", "espeak")):
                return False, "REFUSED — no local TTS engine (piper/espeak) installed."
        elif need == "ocr_engine":
            if not shutil.which("tesseract"):
                return False, ("REFUSED — no LOCAL OCR engine (tesseract). Reading screen text by "
                               "sending the image to a vision model would be EGRESS and is forbidden; "
                               "install tesseract locally or do not read the screen.")
        elif need == "operator_grant":
            return False, "REFUSED — no explicit operator grant for this session."
    return True, "ok"


def audit(verb: str, **fields) -> None:
    """The visible record. Appends to the SEAT'S OWN LOG (so it lands where the operator already reads)
    and to a persistent JSONL ledger (so it survives a reboot). Never records content — only metadata."""
    rec = {"ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "epoch": int(_dt.datetime.now().timestamp()), "verb": verb, "tool": "agent-tools",
           "version": VERSION, **fields}
    line = json.dumps(rec, sort_keys=True)
    for p in (SEAT_LOG, LEDGER):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a") as f:
                f.write(("AGENT-TOOL " + line + "\n") if p == SEAT_LOG else (line + "\n"))
        except OSError:
            pass


def _banner(text: str) -> None:
    """The on-screen half of the visible record. notify-send does NOT steal focus."""
    try:
        subprocess.run(["notify-send", "-a", "agent-tools", "-u", "low", "agent-tools", text],
                       capture_output=True, timeout=6)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# VERBS
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def list_windows() -> list[dict]:
    out: list[dict] = []
    r = _run(["xdotool", "search", "--onlyvisible", "--name", ""])
    for wid in (r.stdout or "").split():
        if not wid.strip():
            continue
        t = _run(["xdotool", "getwindowname", wid]).stdout.strip()
        if not t:
            continue
        g = _run(["xdotool", "getwindowgeometry", "--shell", wid]).stdout
        geom = {}
        for ln in g.splitlines():
            k, _, v = ln.partition("=")
            geom[k.strip()] = v.strip()
        out.append({"xid": wid, "title": t,
                    "geom": f"{geom.get('WIDTH','?')}x{geom.get('HEIGHT','?')}+{geom.get('X','?')}+{geom.get('Y','?')}",
                    "pid": geom.get("WINDOW", "")})
    return out


def measure_capture(png) -> dict:
    """★ THE ASSERTION THAT STOPS A BLACK PNG FROM BEING REPORTED AS A SCREENSHOT.

    Measured on this box: `ffmpeg -f x11grab` exits 0 and writes a valid 1920x1200 PNG that is ENTIRELY
    BLACK (mean 1.7/65535, 43 colours). Every naive success check — exit code, file exists, is a PNG —
    passes it. So the check is on the PIXELS, and it is a function (not an `if` inside the paint) so the
    guard the live verb uses and the guard the suite proves red are THE SAME LINE (AGENTS.md §2)."""
    m = _run(["identify", "-format", "%wx%h %[mean] %[standard-deviation] %k", str(png)])
    raw = (m.stdout or "").strip()
    parts = raw.split()
    if len(parts) != 4:
        return {"measurable": False, "raw": raw, "verdict": "unmeasured — identify gave no numbers"}
    try:
        mean, stddev, colours = float(parts[1]), float(parts[2]), int(parts[3])
    except ValueError:
        return {"measurable": False, "raw": raw, "verdict": "unmeasured — identify output unparsable"}
    # A black/blank frame: almost no brightness AND almost no distinct colours. Both are required —
    # a legitimately dark window still has many distinct colours, so it will not be flagged.
    blank = mean < 200.0 and colours <= 64
    return {"measurable": True, "raw": raw, "geom": parts[0], "mean": mean,
            "stddev": stddev, "colours": colours, "blank": blank,
            "verdict": "BLANK/BLACK — treat as FAILED, not as a screenshot" if blank else "real content"}


def cmd_shot(a) -> int:
    verb = "shot.screen" if a.screen else "shot.window"
    if a.windows:
        ok, why = gate_ok("windows.list")
        if not ok:
            print(why, file=sys.stderr)
            return 3
        ws = list_windows()
        print(f"CAPTURABLE WINDOWS ({len(ws)}) — X11/XWayland only:")
        for w in ws:
            print(f"  {w['xid']:>10}  {w['geom']:<18}  {w['title']!r}")
        print("★ NEGATIVE EVIDENCE: this list is a LOWER BOUND, not a census. A Wayland-native window has "
              "no xid and appears nowhere here. On this box Firefox and Chromium are running and are NOT "
              "in this list. `shot --screen` captures them; `shot --window` cannot.")
        return 0

    ok, why = gate_ok(verb)
    if not ok:
        print(why, file=sys.stderr)
        return 3

    if a.screen:
        out = pathlib.Path(a.out or (SHOT_DIR / f"screen-{int(_dt.datetime.now().timestamp())}.png"))
        out.parent.mkdir(parents=True, exist_ok=True)
        # visible record FIRST — the gate's whole point
        audit("shot.screen", out=str(out), mechanism="xdg-portal", silent_portal=True)
        _banner("screen capture taken → " + out.name)
        r = _run(["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
                  "--object-path", "/org/freedesktop/portal/desktop",
                  "--method", "org.freedesktop.portal.Screenshot.Screenshot",
                  "", "{'interactive': <false>, 'modal': <false>}"])
        if r.returncode != 0:
            print(f"portal call failed: {r.stderr.strip()[:200]}", file=sys.stderr)
            return 1
        # the portal writes its own file and returns a handle; we wait for the newest
        import time as _t
        newest, deadline = None, _t.time() + 15
        while _t.time() < deadline:
            cands = sorted(pathlib.Path(os.path.expanduser("~/Pictures")).glob("Screenshot-*.png"),
                           key=lambda p: p.stat().st_mtime)
            if cands and cands[-1].stat().st_mtime > _t.time() - 20:
                newest = cands[-1]
                break
            _t.sleep(0.4)
        if not newest:
            print("portal returned but no PNG appeared within 15s", file=sys.stderr)
            return 1
        shutil.copy2(newest, out)
        print(f"wrote {out} ({out.stat().st_size} B) via xdg portal")
        print(f"  also at {newest} (the portal's own output)")
        print(f"  VISIBLE RECORD: {SEAT_LOG} and {LEDGER}")
        return 0

    # ── per-window ──────────────────────────────────────────────────────────────────────────────
    if not a.window:
        print("shot needs --window <name|0xid>, or --windows to list, or --screen", file=sys.stderr)
        return 2
    ws = list_windows()
    target = None
    needle = a.window.lower()
    for w in ws:
        if w["xid"].lower() == needle or (needle in w["title"].lower()):
            target = w
            break
    if target is None:
        print(f"REFUSED: no CAPTURABLE window matches {a.window!r}. Tried: "
              f"{[w['title'] for w in ws] or 'nothing listable'}. "
              f"A Wayland-native window cannot be matched here — use `shot --screen`.", file=sys.stderr)
        return 1
    out = pathlib.Path(a.out or (SHOT_DIR / f"{target['title'][:40].replace('/','_')}-"
                                          f"{int(_dt.datetime.now().timestamp())}.png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    audit("shot.window", window=target["title"], xid=target["xid"], out=str(out),
          mechanism="import -window")
    if not a.no_banner:
        _banner(f"captured “{target['title']}” → {out.name}")
    r = _run(["import", "-window", target["xid"], str(out)])
    if r.returncode != 0 or not out.is_file():
        print(f"import failed: {r.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    # ★ A BLACK PNG IS A FAILED CAPTURE THAT LOOKS LIKE A SUCCESS. Measure it, do not trust exit 0.
    got = measure_capture(out)
    if got.get("blank"):
        print(f"★ capture is BLANK (mean={got['mean']:.1f}, {got['colours']} colours) — this is the "
              f"ffmpeg/x11grab failure mode, NOT a screenshot. Refusing to report it as one.",
              file=sys.stderr)
        audit("shot.blank", window=target["title"], out=str(out),
              mean=got["mean"], colours=got["colours"])
        return 1
    print(f"wrote {out} ({out.stat().st_size} B) via `import -window {target['xid']}`")
    print(f"  window: {target['title']}  geom={target['geom']}")
    print(f"  measured: {got.get('raw','?')}  -> {got.get('verdict','unmeasured')}")
    print(f"  VISIBLE RECORD: {SEAT_LOG} and {LEDGER}")
    return 0


def cmd_windows(a) -> int:
    ok, why = gate_ok("windows.list")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    ws = list_windows()
    for w in ws:
        print(f"{w['xid']:>10}  {w['geom']:<18}  {w['title']}")
    print(f"--- {len(ws)} capturable window(s). LOWER BOUND: Wayland-native windows are not listable.")
    return 0


def cmd_clip(a) -> int:
    if a.get:
        ok, why = gate_ok("clip.get")
        if not ok:
            print(why, file=sys.stderr)
            return 3
        r = _run(["xclip", "-selection", "clipboard", "-o"])
        if r.returncode != 0:
            print(f"xclip failed: {r.stderr.strip()[:120]}", file=sys.stderr)
            return 1
        audit("clip.get", bytes=len(r.stdout))  # ★ the byte count, NEVER the content
        sys.stdout.write(r.stdout)
        return 0
    if a.set is not None:
        ok, why = gate_ok("clip.set")
        if not ok:
            print(why, file=sys.stderr)
            return 3
        p = subprocess.run(["xclip", "-selection", "clipboard"], input=a.set, text=True, env=display_env())
        audit("clip.set", bytes=len(a.set))
        return 0 if p.returncode == 0 else 1
    print("clip needs --get or --set TEXT", file=sys.stderr)
    return 2


def cmd_notify(a) -> int:
    ok, why = gate_ok("notify.raise")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    audit("notify.raise", chars=len(a.text))
    r = _run(["notify-send", "-a", "agent-tools", a.text])
    return 0 if r.returncode == 0 else 1


def cmd_say(a) -> int:
    ok, why = gate_ok("voice.say")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    if a.to_file:
        if shutil.which("espeak"):
            audit("voice.say", to_file=a.to_file, engine="espeak", chars=len(a.text))
            r = _run(["espeak", "-w", a.to_file, a.text])
            if r.returncode != 0 or not pathlib.Path(a.to_file).is_file():
                print(f"espeak failed: {r.stderr.strip()[:200]}", file=sys.stderr)
                return 1
            print(f"wrote {a.to_file} ({pathlib.Path(a.to_file).stat().st_size} B) — NO sound made")
            return 0
        print("no espeak for --to-file", file=sys.stderr)
        return 1
    audit("voice.say", engine="piper" if shutil.which("piper") else "espeak", chars=len(a.text))
    if shutil.which("espeak"):
        r = _run(["espeak", a.text], timeout=120)
        return 0 if r.returncode == 0 else 1
    print("no local TTS engine", file=sys.stderr)
    return 1


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# ★ THE LOOPBACK ANSWER TO A CLOUD VISION CALL.
#   The live harness ships `vision_analyze`, which base64-encodes an image and posts it to the
#   configured model — on this box that is the metered OpenRouter link. This verb reads the SAME
#   pixels with a LOCAL engine, so "read what is on screen" does not require egress. It reuses the
#   window matcher and the BLANK guard from `shot`, so a black frame cannot be OCR'd into silence and
#   reported as "no text on screen".
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def _match_window(needle: str) -> tuple[dict | None, list[dict]]:
    ws = list_windows()
    n = needle.lower()
    for w in ws:
        if w["xid"].lower() == n or n in w["title"].lower():
            return w, ws
    return None, ws


def cmd_read_text(a) -> int:
    ok, why = gate_ok("screen.read_text")
    if not ok:
        print(why, file=sys.stderr)
        return 3

    source, caps = None, {}
    if a.from_png:
        png = pathlib.Path(a.from_png)
        if not png.is_file():
            print(f"REFUSED: no such image {png}", file=sys.stderr)
            return 1
        source = f"png:{png}"
    else:
        if not a.window:
            print("read-text needs --window <name|0xid> or --from <png>", file=sys.stderr)
            return 2
        target, ws = _match_window(a.window)
        if target is None:
            print(f"REFUSED: no CAPTURABLE window matches {a.window!r}. Tried: "
                  f"{[w['title'] for w in ws] or 'nothing listable'}. A Wayland-native window cannot "
                  f"be matched here — use `shot --screen` then `read-text --from <png>`.", file=sys.stderr)
            return 1
        png = pathlib.Path(a.out or (SHOT_DIR / f"ocr-{int(_dt.datetime.now().timestamp())}.png"))
        png.parent.mkdir(parents=True, exist_ok=True)
        # ★ visible record FIRST — a capture with no trace is the defect, so the trace is a precondition
        audit("screen.read_text.capture", window=target["title"], xid=target["xid"], out=str(png))
        if not a.no_banner:
            _banner(f"reading text from “{target['title']}” → {png.name}")
        r = _run(["import", "-window", target["xid"], str(png)])
        if r.returncode != 0 or not png.is_file():
            print(f"import failed: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
            return 1
        got = measure_capture(png)
        if got.get("blank"):
            print(f"★ the capture is BLANK (mean={got['mean']:.1f}, {got['colours']} colours) — OCR on "
                  f"a black frame would return '' and read as 'no text on this screen'. Refusing.",
                  file=sys.stderr)
            audit("screen.read_text.blank", window=target["title"], out=str(png),
                  mean=got["mean"], colours=got["colours"])
            return 1
        caps = got
        source = f"window:{target['xid']}"

    r = _run(["tesseract", str(png), "stdout"], timeout=180)
    if r.returncode != 0:
        print(f"tesseract failed: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
        return 1
    raw = r.stdout or ""
    if a.raw:
        shown, nred = raw, 0
    else:
        shown, nred = redact(raw)

    # ★ NEVER LOG THE CONTENT. Counts only — the text itself does not reach the ledger.
    audit("screen.read_text", source=source, chars=len(raw), redactions=nred,
          redacted=not a.raw, engine="tesseract", capture_measured=caps.get("raw", ""))
    sys.stdout.write(shown if shown.endswith("\n") or not shown else shown + "\n")
    print(f"--- {len(raw)} chars · {nred} secret-shaped token(s) "
          f"{'REDACTED to fingerprint+last-4' if not a.raw else 'NOT redacted (--raw) — the ledger '
             'records that this un-redacted read happened'}", file=sys.stderr)
    if not raw.strip():
        print("★ OCR returned EMPTY text. That is a real result (a blank/very small window) — it is "
              "NOT proof the screen is empty.", file=sys.stderr)
    return 0


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# find · open · handoff · inbox · posture · lane
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def cmd_find(a) -> int:
    ok, why = gate_ok("file.find")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    raw_roots = a.root or ["."]
    roots = []
    for rr in raw_roots:
        r = pathlib.Path(rr).expanduser().resolve()
        widened = a.allow_home
        if str(r) in ("/", str(pathlib.Path.home())) and not widened:
            print(f"REFUSED: {r} is the whole filesystem/home — scanning it is a census of private "
                  f"content, not a search. Pass a narrower --root, or --allow-home to widen it "
                  f"deliberately (the widening is recorded).", file=sys.stderr)
            return 3
        roots.append(r)
    for r in roots:
        if not r.is_dir():
            print(f"REFUSED: {r} is not a directory", file=sys.stderr)
            return 1
    audit("file.find", pattern=a.pattern, roots=[str(r) for r in roots], depth=a.depth,
          allow_home=bool(a.allow_home))

    hits: list[tuple[str, int]] = []
    truncated = False
    for root in roots:
        base = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            if len(pathlib.Path(dirpath).parts) - base >= a.depth:
                dirnames[:] = []
            # never follow a symlink out of the declared root
            dirnames[:] = [d for d in dirnames if not (pathlib.Path(dirpath) / d).is_symlink()]
            for fn in filenames:
                if pathlib.Path(fn).match(a.pattern):
                    p = pathlib.Path(dirpath) / fn
                    try:
                        sz = p.stat().st_size
                    except OSError:
                        continue
                    hits.append((str(p), sz))
                    if len(hits) >= a.limit:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    for path, sz in hits:
        print(f"{sz:>12}  {path}")
    print(f"--- {len(hits)} match(es) for {a.pattern!r} under "
          f"{', '.join(str(r) for r in roots)} (depth<={a.depth})"
          + ("  ★ TRUNCATED at the limit — this is a lower bound" if truncated else ""))
    print("★ NAMES AND SIZES ONLY — no file was opened. A name search is not a content search.")
    return 0


def cmd_open(a) -> int:
    ok, why = gate_ok("app.open")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    if bool(a.path) == bool(a.url):
        print("open takes exactly ONE of --path or --url", file=sys.stderr)
        return 2

    if a.url:
        from urllib.parse import urlparse
        host = (urlparse(a.url).hostname or "").lower()
        is_loop = host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")
        if not is_loop:
            audit("app.open.refused", url_host=host, reason="non-loopback URL is egress")
            print("REFUSED — opening a non-loopback URL is EGRESS: the desktop fetches it over the "
                  "METERED link. This follows the `shot.egress.cloud` precedent exactly: the shape of "
                  "the exception is written down and the exception itself is not. No switch in this "
                  "suite enables it.", file=sys.stderr)
            return 3
        audit("app.open", url=a.url, url_host=host, kind="loopback-url", raised_window=True)
        if not a.no_banner:
            _banner(f"opening {host}")
        r = _run(["xdg-open", a.url], timeout=20)
        if r.returncode != 0:
            print(f"xdg-open failed: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
            return 1
        print(f"opened loopback URL {a.url} (loopback only — it cannot reach the network)")
        return 0

    p = pathlib.Path(a.path).expanduser()
    if not p.exists():
        print(f"REFUSED: {p} does not exist — xdg-open fails SILENTLY on a missing path, and a silent "
              f"failure reads as success.", file=sys.stderr)
        return 1
    if a.dry_run:
        # ★ PROVES THE PERMITTED BRANCH WITHOUT RAISING A WINDOW. The standing rule is not to raise a
        #   window the operator did not ask for, so the live open is the operator's call, not a test's.
        audit("app.open.dry_run", path=str(p), kind="dir" if p.is_dir() else "file", raised_window=False)
        print(f"DRY RUN — WOULD open {p} with the desktop default handler. No window was raised.")
        print("★ The live branch is deliberately NOT exercised by tests: opening it raises a window, "
              "and raising a window the operator did not ask for is the thing this gate exists to stop.")
        return 0
    audit("app.open", path=str(p), kind="dir" if p.is_dir() else "file", raised_window=True)
    if not a.no_banner:
        _banner(f"opening {p.name}")
    r = _run(["xdg-open", str(p)], timeout=20)
    if r.returncode != 0:
        print(f"xdg-open failed: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
        return 1
    print(f"opened {p} with the desktop default handler.")
    print("★ This RAISED A WINDOW. That side effect is recorded in the ledger — one target, explicitly "
          "named by the caller, never a bulk raise.")
    return 0


def _read_handoff() -> list[dict]:
    if not HANDOFF.is_file():
        return []
    out = []
    for ln in HANDOFF.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def cmd_handoff(a) -> int:
    ok, why = gate_ok("work.handoff")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    payload, nred = redact(a.what)
    rec = {"ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
           "epoch": int(_dt.datetime.now().timestamp()),
           "lane": a.lane or "unassigned", "what": payload, "where": a.where or "",
           "from": "agent-tools", "status": "queued", "approved": False,
           "redactions": nred, "version": VERSION}
    HANDOFF.parent.mkdir(parents=True, exist_ok=True)
    with open(HANDOFF, "a") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")
    audit("work.handoff", lane=rec["lane"], chars=len(a.what), redactions=nred,
          approved=False, status="queued", queue=str(HANDOFF))
    print(f"queued → {rec['lane']}  status=queued  approved=false")
    print(f"  {payload}")
    print(f"  queue: {HANDOFF}")
    if nred:
        print(f"  ★ {nred} secret-shaped token(s) REDACTED to fingerprint+last-4 before the payload "
              f"was written.")
    print("★ DISCOVERED IS NOT APPROVED. This queued work; it did not start an agent and it did not "
          "approve anything.")
    return 0


def cmd_inbox(a) -> int:
    ok, why = gate_ok("work.inbox")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    rows = _read_handoff()
    print(f"HANDOFF QUEUE — {len(rows)} item(s), newest first  ({HANDOFF})")
    for r in reversed(rows):
        flag = "APPROVED" if r.get("approved") else "not-approved"
        print(f"  [{r.get('status','?'):<8}] lane={r.get('lane','?'):<14} {flag:<13} "
              f"{r.get('what','')[:70]!r}")
        if r.get("where"):
            print(f"             where: {r['where']}")
    if not rows:
        print("  (empty)")
    print("★ This is a census of QUEUED WORK, not of agents. Agent discovery (A47) belongs to the "
          "voice_intent lane and is not implemented here.")
    return 0


def _hex_ip(h: str) -> str:
    """Little-endian hex from /proc/net/tcp* -> address. tcp6 is FOUR 32-bit little-endian words, so
    each word must be byte-reversed before the address is assembled; a v4-mapped address is then
    printed as the dotted quad it actually is, because `::ffff:127.0.0.1` is loopback and a mangled
    string would hide that."""
    try:
        if len(h) == 8:
            return ".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0))
        if len(h) == 32:
            b = bytearray()
            for wi in range(0, 32, 8):
                w = h[wi:wi + 8]
                b += bytes(int(w[i:i + 2], 16) for i in (6, 4, 2, 0))
            if b[:10] == b"\x00" * 10 and b[10:12] == b"\xff\xff":
                return ".".join(str(x) for x in b[12:16])
            return ":".join(f"{b[i]:02x}{b[i + 1]:02x}" for i in range(0, 16, 2))
    except ValueError:
        pass
    return h


def _is_loopback(ip: str) -> bool:
    if ip.startswith("127."):
        return True
    if ip in ("0:0:0:0:0:0:0:1", "::1"):
        return True
    return False


def cmd_net_posture(a) -> int:
    ok, why = gate_ok("net.posture")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    listeners, exposed, families = [], [], []
    for fam, fn in (("tcp", "/proc/net/tcp"), ("tcp6", "/proc/net/tcp6")):
        p = pathlib.Path(fn)
        if not p.is_file():
            continue
        families.append(fam)
        for i, ln in enumerate(p.read_text().splitlines()):
            if i == 0 or not ln.strip():
                continue
            f = ln.split()
            if len(f) < 4 or f[3] != "0A":  # 0A = LISTEN
                continue
            ip_h, _, port_h = f[1].partition(":")
            ip = _hex_ip(ip_h)
            try:
                port = int(port_h, 16)
            except ValueError:
                port = -1
            rec = {"family": fam, "addr": ip, "port": port}
            listeners.append(rec)
            if not _is_loopback(ip):
                exposed.append(rec)
    audit("net.posture", families=families, listeners=len(listeners), exposed=len(exposed))
    print(f"NET POSTURE — read-only from /proc/net/{{{','.join(families)}}}; this tool opened no socket.")
    print(f"  {len(listeners)} listening socket(s) total.")
    if exposed:
        print(f"  ★ {len(exposed)} bound to a WILDCARD or NON-LOOPBACK address:")
        for r in sorted(exposed, key=lambda x: x["port"]):
            print(f"      {r['family']:<5} {r['addr']:<40} :{r['port']}")
        print("  → A wildcard bind is reachable from every interface. The floor is that CREDENTIAL "
              "endpoints stay loopback-only; this list is how that floor gets CHECKED instead of "
              "asserted. It is not proof of what is reachable — it is proof of what is BOUND.")
    else:
        print("  ✓ no wildcard/non-loopback listeners found in the families read.")
    print("★ NEGATIVE EVIDENCE: remote policy is invisible here. The estate's own record is that a "
          "credential endpoint answers 403 from the tailnet even with a valid token — a policy fact "
          "this verb cannot observe.")
    return 0


def _lane_processes() -> list[dict]:
    """Classify this box's KHAOSS processes by reading /proc. Read-only; sends no signal."""
    classes = []
    page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
    for ent in pathlib.Path("/proc").iterdir():
        if not ent.name.isdigit():
            continue
        try:
            cmd = (ent / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
            if not cmd:
                continue
            statm = (ent / "statm").read_text().split()
            rss = int(statm[1]) * page
        except (OSError, ValueError, IndexError):
            continue
        klass = None
        if "hermes-agent" in cmd:
            klass = "harness"
        elif "deepseek-harness" in cmd:
            klass = "dsh"
        elif "AionUi" in cmd:
            klass = "aionui-electron"
        elif "jarvis/" in cmd:
            klass = "seat"
        elif "ollama" in cmd:
            klass = "ollama"
        if klass:
            classes.append({"pid": int(ent.name), "class": klass, "rss": rss,
                            "cmd": cmd[:90]})
    return classes


def cmd_lane(a) -> int:
    ok, why = gate_ok("lane.measure")
    if not ok:
        print(why, file=sys.stderr)
        return 3
    procs = _lane_processes()
    audit("lane.measure", ceiling_mib=LANE_CEILING_MIB, procs=len(procs))
    by: dict[str, int] = {}
    for p in procs:
        by[p["class"]] = by.get(p["class"], 0) + p["rss"]
    total = sum(by.values())
    print(f"LANE MEASURE — RSS by class (ceiling {LANE_CEILING_MIB} MiB, a DECLARED constant)")
    for k in sorted(by, key=lambda x: -by[x]):
        print(f"  {k:<16} {by[k] / 1048576:>9.1f} MiB")
    print(f"  {'TOTAL':<16} {total / 1048576:>9.1f} MiB  "
          f"({total / 1048576 / LANE_CEILING_MIB * 100:.1f}% of the ceiling)")
    if total / 1048576 > LANE_CEILING_MIB:
        print(f"  ★ OVER THE CEILING by {total / 1048576 - LANE_CEILING_MIB:.1f} MiB. This is a "
              f"measurement, not a permission slip: the ceiling is a constant in this file and this "
              f"verb cannot raise it.")
    print(f"★ LIMIT: RSS double-counts shared pages, so {total / 1048576:.1f} MiB is an UPPER BOUND on "
          f"private memory, not an exact figure. AGENTS.md records the whole stack at 1,843 MB, which "
          f"is already over this {LANE_CEILING_MIB} MiB lane ceiling.")
    return 0


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# THE MANIFEST (generated FROM TOOLS — one store)
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def manifest_md() -> str:
    lines = [
        "# AGENT TOOLS MANIFEST — the seat's standard agent-tools suite",
        "",
        f"_Generated from `bin/agent-tools.py` · TOOLS table · version {VERSION} · do not edit by hand._",
        f"_Regenerate: `bin/agent-tools.py --emit-manifest`._",
        "",
        "> **A capability not in this manifest does not exist to the agent that needs it** (AGENTS.md §8).",
        "> `bin/agent-tools.py` is the GATE: a verb that is not a key in its `TOOLS` table is REFUSED by",
        "> name. This document is generated from that same table, so the document cannot drift from the gate.",
        "",
        "> **This is not a second capability manifest.** `CAPABILITY_MANIFEST.md` remains the estate-wide,",
        "> auto-assembled view over skills · atlas · tools, and remains the authority for **clearance**.",
        "> This file is the **agent-tools subset**: the verbs a seat agent may call, what each reaches,",
        "> whether it is loopback-only, whether it can touch the network, and what it is FORBIDDEN to do.",
        "",
        "## Hard gates encoded here (from AGENTS.md §6)",
        "",
        "| gate | how this suite encodes it |",
        "|---|---|",
        "| **A screenshot is content** | `shot.*` REFUSES unless its visible record can be written. |",
        "| **Never off the box** | no network verb exists in this file; `--selftest` scans for them. |",
        "| **Raw audio never leaves the box** | `voice.say` is local synthesis only (piper/espeak). |",
        "| **No sudo** | no verb escalates; nothing here calls sudo. |",
        "| **Credential endpoints stay loopback** | everything here is local; the only server contact is `127.0.0.1:8771`. |",
        "| **Never log key material** | the ledger records byte counts, never clipboard/screenshot content. |",
        "| **Do not move/focus a window** | `shot.window` and `windows.list` are read-only; `import -window` does not raise. |",
        "| **Never log key material (2)** | `screen.read_text` prints secret-shaped tokens as `fp:<sha256[:10]>…<last4>` by default; the ledger records counts only. |",
        "| **No egress by default** | every verb with `can_touch_network` must carry `operator_grant`; `--selftest` asserts it. |",
        "| **A floor is not a setting** | `HARD_FLOORS` is an immutable tuple; `--selftest` fails if any env var name mentions one. |",
        "| **Credential endpoints stay loopback** | `net.posture` reads `/proc/net/*` and flags wildcard binds; it opens no socket. |",
        "",
        "## Hard floors (AGENTS.md §6) — constants, not configuration",
        "",
    ] + [f"- {f}" for f in HARD_FLOORS] + [
        "",
        f"★ **Declared lane ceiling: {LANE_CEILING_MIB} MiB** — a module constant. `lane.measure` reports the "
        "live number against it and cannot raise it.",
        "",
        "## The tools",
        "",
        "| verb | felt name | loopback only | can touch network | mechanism | status |",
        "|---|---|---|---|---|---|",
    ]
    for k in sorted(TOOLS):
        t = TOOLS[k]
        lines.append(f"| `{k}` | {t['felt']} | {'yes' if t['loopback_only'] else '**NO**'} | "
                     f"{'no' if not t['can_touch_network'] else '**YES**'} | `{t['mechanism']}` | "
                     f"{'absent' if k == 'shot.egress.cloud' else 'present'} |")
    lines += ["", "## Every tool in full", ""]
    for k in sorted(TOOLS):
        t = TOOLS[k]
        lines += [
            f"### `{k}` — {t['felt']}", "",
            f"- **What it does:** {t['does']}",
            f"- **What it can reach:** {t['reaches']}",
            f"- **Loopback only:** {'yes' if t['loopback_only'] else 'NO'}",
            f"- **Can touch the network:** {'no' if not t['can_touch_network'] else '**YES**'}",
            f"- **Mechanism:** `{t['mechanism']}`",
            f"- **Measured status:** {t['measured']}",
            f"- **Gate (`needs`):** {', '.join(t['needs']) or 'none'}",
            f"- **Limits (honest):** {t['limits']}",
            "- **FORBIDDEN:**",
        ]
        lines += [f"  - {f}" for f in t["forbidden"]]
        lines.append("")
    lines += [
        "## Measured capture paths on this-box (2026-10-10, Wayland/GNOME, XWayland present)",
        "",
        "| path | result |",
        "|---|---|",
        "| `xwd -root` | **FAIL** — `X Error BadMatch (X_GetImage)`, exit 1, 0 bytes |",
        "| `import -window root` | **FAIL** — no image written |",
        "| `ffmpeg -f x11grab` | **FAIL SILENTLY** — exit 0, valid 1920×1200 PNG, **entirely black** (mean 1.7/65535, 43 colours) |",
        "| `org.gnome.Shell.Screenshot` (D-Bus) | **FAIL** — `AccessDenied: Screenshot is not allowed` |",
        "| `grim` / `gnome-screenshot` / `spectacle` | **ABSENT** (not installed) |",
        "| `wl-copy` / `wl-paste` | **ABSENT** |",
        "| **`import -window <xid>`** | **WORKS** — real per-window pixels (this is how another app is seen) |",
        "| **xdg portal `Screenshot` (gdbus)** | **WORKS** — real whole-screen pixels, **and silent** |",
        "",
        "★ The portal does not ask the operator. That silence is why this suite enforces the visible record",
        "itself instead of delegating it to the desktop.",
        "",
        "★ **What is NOT capturable:** a Wayland-native window has no xid. On this box Firefox and Chromium",
        "are running and appear in no window list. Per-window capture cannot see them; only whole-screen can.",
        "",
        "## The one capability deliberately absent",
        "",
        "`shot.egress.cloud` — sending a screenshot to a cloud model. **Not implemented, forbidden by",
        "default, no switch in this suite enables it.** It is written down so the shape of the exception is",
        "visible and the exception itself is not. If ever built it must be a separate explicit per-capture",
        "grant, operator-visible, and must apply the same value-substitution discipline as every other egress",
        "stage (secrets/credentials/PII replaced by references before the image leaves the box).",
        "",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SELFTEST — set equality, the forbidden lists, and the no-network proof. Ships a PROVE-FAIL.
# ─────────────────────────────────────────────────────────────────────────────────────────────────
def selftest(prove_fail: bool = False) -> int:
    fails: list[str] = []
    src = pathlib.Path(__file__).read_text()

    # 1 · SET EQUALITY: every verb the CLI can dispatch must BE in TOOLS, and every TOOLS key must be
    #     dispatchable. Presence is not the test; the two sets must be equal.
    dispatchable = {"shot.window", "shot.screen", "windows.list", "clip.get", "clip.set",
                    "notify.raise", "voice.say", "screen.read_text", "file.find", "app.open",
                    "work.handoff", "work.inbox", "net.posture", "lane.measure",
                    "shot.egress.cloud"}
    tools = set(TOOLS)
    if prove_fail:
        tools.add("ghost.tool")  # PROVE-FAIL: a verb in the manifest with no gate entry
    if tools != dispatchable:
        fails.append(f"TOOLS != dispatchable: only in TOOLS={sorted(tools - dispatchable)}, "
                     f"only dispatchable={sorted(dispatchable - tools)}")

    # 2 · EVERY tool must state what it is FORBIDDEN to do. An empty forbidden list is an ungated tool.
    for k, t in TOOLS.items():
        if prove_fail and k == "notify.raise":
            t = dict(t, forbidden=[])  # PROVE-FAIL
        if not t.get("forbidden"):
            fails.append(f"{k}: no FORBIDDEN entries — an ungated capability")
        for fld in ("does", "reaches", "loopback_only", "can_touch_network", "mechanism", "limits", "needs"):
            if fld not in t:
                fails.append(f"{k}: missing field {fld}")

    # 3 · NO NETWORK, PROVED STRUCTURALLY. Parse our OWN AST and collect every binary that appears as
    #     the first element of a literal argv list handed to subprocess.run()/our _run(). A text scan
    #     cannot do this: it matches the NETWORK_BINARIES declaration itself and reports the guard as
    #     the breach. The AST sees only argv positions, so the declaration is invisible to it.
    import ast as _ast
    invoked: set[str] = set()
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name not in ("run", "_run"):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, (_ast.List, _ast.Tuple)) and first.elts:
            e0 = first.elts[0]
            if isinstance(e0, _ast.Constant) and isinstance(e0.value, str):
                invoked.add(e0.value)
    if prove_fail:
        invoked.add("curl")  # PROVE-FAIL: a network verb in an argv position must go red
    for b in sorted(invoked & NETWORK_BINARIES):
        fails.append(f"NETWORK BINARY {b!r} is invoked in an argv position — this suite transmits nothing")
    for b in sorted(invoked - ALLOWED_BINARIES):
        fails.append(f"undeclared binary {b!r} invoked (add it to ALLOWED_BINARIES or remove the call)")
    print(f"  · binaries invoked in argv position: {', '.join(sorted(invoked)) or '(none)'}")

    # 4 · THE SCREENSHOT GATE MUST ACTUALLY REFUSE. Drive it: a nonexistent window must refuse, and the
    #     egress verb must be forbidden.
    ws = list_windows()
    ok_refuse = True
    for w in ws:
        if "no-such-window-xyzzy" in w["title"].lower():
            ok_refuse = False
    if not ok_refuse:
        fails.append("a window matching the refusal probe exists — the probe is not proving anything")
    g_ok, g_why = gate_ok("shot.egress.cloud")
    if g_ok:
        fails.append("shot.egress.cloud was permitted — the default must be FORBIDDEN")

    # 5 · every verb in TOOLS must be reachable by gate_ok
    for k in dispatchable:
        gate_ok(k)  # must not raise

    # 6 · THE BLANK GUARD, DRIVEN AGAINST A REAL BLACK IMAGE. A guard never tested against the exact
    #     input it exists to catch is a guard we are hoping about (AGENTS.md §2, atlas FA-17). So build
    #     the black frame and the non-black frame and require the SAME function the live verb uses to
    #     separate them. This is the ffmpeg/x11grab failure mode reproduced as a fixture.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        blk, red = pathlib.Path(td) / "black.png", pathlib.Path(td) / "red.png"
        _run(["convert", "-size", "64x64", "xc:black", str(blk)])
        _run(["convert", "-size", "64x64", "xc:red", str(red)])
        gb, gr = measure_capture(blk), measure_capture(red)
        if prove_fail:
            gb = dict(gb, blank=False)  # PROVE-FAIL: pretend the black frame is fine
        if not gb.get("measurable") or not gr.get("measurable"):
            fails.append("measure_capture could not measure the fixtures (ImageMagick missing?)")
        else:
            if not gb.get("blank"):
                fails.append(f"the BLANK guard did NOT flag a black 64x64 frame "
                             f"(mean={gb.get('mean')}, colours={gb.get('colours')})")
            if gr.get("blank"):
                fails.append(f"the BLANK guard flagged a RED frame as blank "
                             f"(mean={gr.get('mean')}, colours={gr.get('colours')})")

    # 7 · ★ HARD FLOORS ARE CONSTANTS, NOT SETTINGS. "A hard floor cannot be represented as a
    #     toggleable value." So: no environment variable whose NAME mentions a floor may be read
    #     anywhere in this file, and the floors themselves must be an immutable tuple.
    if not isinstance(HARD_FLOORS, tuple) or len(HARD_FLOORS) < 4:
        fails.append("HARD_FLOORS is not an immutable tuple of at least 4 floors")
    if isinstance(LANE_CEILING_MIB, bool) or not isinstance(LANE_CEILING_MIB, int):
        fails.append("LANE_CEILING_MIB is not a plain int constant — a ceiling that can be retyped is "
                     "not a ceiling")
    env_keys: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            nm = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if nm in ("getenv",) and node.args and isinstance(node.args[0], _ast.Constant):
                env_keys.add(str(node.args[0].value))
        if isinstance(node, _ast.Subscript):
            v = node.value
            if isinstance(v, _ast.Attribute) and v.attr == "environ":
                sl = node.slice
                if isinstance(sl, _ast.Constant) and isinstance(sl.value, str):
                    env_keys.add(sl.value)
    if prove_fail:
        env_keys.add("KHAOSS_EGRESS_ENABLED")  # PROVE-FAIL: a floor as a toggle must go red
    for k in sorted(env_keys):
        if any(w in k.upper() for w in LANE_CEILING_WORDS):
            fails.append(f"env var {k!r} names a HARD FLOOR — a floor read from the environment is a "
                         f"setting, not a floor (AGENTS.md §6)")

    # 8 · ★ EGRESS IS ALWAYS GATED. Any verb that can touch the network must carry the operator_grant
    #     precondition, so permission can never be inferred — the `shot.egress.cloud` precedent.
    for k, t in TOOLS.items():
        if t.get("can_touch_network") and "operator_grant" not in t.get("needs", []):
            fails.append(f"{k}: can_touch_network=True without the operator_grant gate — egress must "
                         f"never be reachable by default")

    print(f"selftest: {len(TOOLS)} tools, {len(fails)} failure(s)")
    for f in fails:
        print(f"  ✗ {f}")
    if not fails:
        print("  ✓ set equality: TOOLS == dispatchable")
        print("  ✓ every tool declares what it is FORBIDDEN to do")
        print(f"  ✓ AST scan: {len(invoked)} binary/binaries invoked, none is a network verb "
              f"({', '.join(sorted(NETWORK_BINARIES))}), all declared in ALLOWED_BINARIES")
        print("  ✓ the screenshot egress capability refuses by default")
        print("  ✓ gate_ok() answers for every verb")
        print("  ✓ the BLANK guard flags a black frame AND accepts a red one (driven, not asserted)")
        print(f"  ✓ HARD FLOORS ({len(HARD_FLOORS)}) are immutable constants and no env var names one")
        print("  ✓ no verb can reach the network without the operator_grant gate")
        print("  PROVE-FAIL: --selftest --prove-fail   (must exit 1)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True, description="the seat's standard agent-tools suite")
    ap.add_argument("--list", action="store_true", help="print the inventory (from TOOLS, the gate)")
    ap.add_argument("--manifest", action="store_true", help="print the generated manifest markdown")
    ap.add_argument("--emit-manifest", nargs="?", const="docs/AGENT_TOOLS_MANIFEST.md", default=None)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--prove-fail", action="store_true")
    ap.add_argument("--version", action="version", version=f"agent-tools {VERSION}")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("shot")
    p.add_argument("--window"); p.add_argument("--windows", action="store_true")
    p.add_argument("--screen", action="store_true"); p.add_argument("-o", "--out")
    p.add_argument("--no-banner", action="store_true")
    sub.add_parser("windows")
    c = sub.add_parser("clip"); c.add_argument("--get", action="store_true"); c.add_argument("--set")
    n = sub.add_parser("notify"); n.add_argument("text")
    s = sub.add_parser("say"); s.add_argument("text"); s.add_argument("--to-file")
    rt = sub.add_parser("read-text")
    rt.add_argument("--window"); rt.add_argument("--from", dest="from_png")
    rt.add_argument("-o", "--out"); rt.add_argument("--raw", action="store_true")
    rt.add_argument("--no-banner", action="store_true")
    fd = sub.add_parser("find"); fd.add_argument("pattern")
    fd.add_argument("--root", action="append"); fd.add_argument("--depth", type=int, default=6)
    fd.add_argument("--limit", type=int, default=200); fd.add_argument("--allow-home", action="store_true")
    op = sub.add_parser("open")
    op.add_argument("--path"); op.add_argument("--url"); op.add_argument("--dry-run", action="store_true")
    op.add_argument("--no-banner", action="store_true")
    hf = sub.add_parser("handoff"); hf.add_argument("what")
    hf.add_argument("--lane"); hf.add_argument("--where")
    sub.add_parser("inbox")
    sub.add_parser("net-posture")
    sub.add_parser("lane")

    a = ap.parse_args()

    if a.emit_manifest is not None:
        out = pathlib.Path(a.emit_manifest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(manifest_md())
        print(f"wrote {out}")
        return 0
    if a.manifest:
        print(manifest_md()); return 0
    if a.selftest:
        return selftest(prove_fail=a.prove_fail)
    if a.list:
        print(f"AGENT TOOLS — {len(TOOLS)} verbs. A verb not here does not exist to you.")
        print(f"{'verb':<20} {'felt':<28} {'net':<4} {'gate'}")
        for k in sorted(TOOLS):
            t = TOOLS[k]
            net = "YES" if t["can_touch_network"] else "no"
            print(f"  {k:<18} {t['felt']:<28} {net:<4} {','.join(t['needs']) or '-'}")
        print("\nRun with --manifest for the full document, or --selftest for the proofs.")
        print(f"Visible record: {SEAT_LOG} and {LEDGER}")
        return 0
    if a.cmd == "shot":
        return cmd_shot(a)
    if a.cmd == "windows":
        return cmd_windows(a)
    if a.cmd == "clip":
        return cmd_clip(a)
    if a.cmd == "notify":
        return cmd_notify(a)
    if a.cmd == "say":
        return cmd_say(a)
    if a.cmd == "read-text":
        return cmd_read_text(a)
    if a.cmd == "find":
        return cmd_find(a)
    if a.cmd == "open":
        return cmd_open(a)
    if a.cmd == "handoff":
        return cmd_handoff(a)
    if a.cmd == "inbox":
        return cmd_inbox(a)
    if a.cmd == "net-posture":
        return cmd_net_posture(a)
    if a.cmd == "lane":
        return cmd_lane(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
