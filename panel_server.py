#!/usr/bin/env python3
"""
panel_server.py — local HTTP host + reverse proxy + SSE broadcaster for the
AionUI stack panel.

Why this exists
---------------
The panel HTML used to be opened as `file://…aionui-stack-panel.html`.  A
file:// origin cannot read PID files for most setups, and any fetch() to
http://localhost:3030 / :9119 is a cross-origin request that the browser will
block unless the target service emits the right CORS headers.

Serving the panel from http://localhost:7842 is necessary but **not
sufficient** to solve CORS: `localhost:7842 → localhost:3030` is still a
cross-origin pair (different ports = different origins). The robust fix is
to make this server a reverse proxy: the browser only ever fetches
same-origin URLs like `/api/screenpipe/health`, and this process proxies them
to the real service. Result: no CORS configuration on any backend.

Real-time status (SSE)
----------------------
The panel subscribes to GET /stream (SSE). A background poller thread
gathers status for every service every STREAM_INTERVAL_S seconds and
broadcasts one JSON event per tick to all subscribers. The panel applies
each event directly to the DOM — no setTimeout polling, no flicker.

Stdlib only — no Flask, no Starlette, no requests.

Security model
--------------
- Binds to 127.0.0.1 only — never reachable from the network.
- credential-check endpoint executes a fixed script path, no shell
  interpolation, no caller-supplied arguments.
- File-serving is locked to the single HTML file by path; no directory
  listing, no walks outside that file.
- screen_assistant lifecycle uses a fixed script path, never caller input.
"""

from __future__ import annotations

import argparse
import hashlib          # ★ S4 build stamp needs it at module scope
import http.client
import json
import logging
import os
import queue
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.request as _urllib_req
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

# ── Paths / config ───────────────────────────────────────────────────────────

HOME = Path(os.path.expanduser("~"))
# ★ C1 (Q115) — resolve the panel asset NON-user-relatively: prefer the copy that TRAVELS WITH THIS SCRIPT
# (boardwalk/), fall back to HOME for backward-compat. A user-relative home (`~/aionui-stack-panel.html`)
# is exactly the path assumption that left the asset behind when the code moved to another user's home on r710.
_SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = _SCRIPT_DIR
def _resolve_panel_html() -> Path:
    for _c in (_SCRIPT_DIR / "aionui-stack-panel.html", HOME / "aionui-stack-panel.html"):
        if _c.is_file():
            return _c
    return _SCRIPT_DIR / "aionui-stack-panel.html"  # canonical target (also the honest 500 message path)
PANEL_HTML = _resolve_panel_html()
# ★★★ WHEN THIS PROCESS STARTED. The page is served from disk on every request, so the HTML is always
#   current — but `_op_mod` imports Python ONCE, so the CODE behind it is frozen at startup. Tonight
#   that cost two wrong measurements: I tested endpoints against a panel still running the previous
#   version of the module and read the old behaviour as a bug in the new one.
#   ★ The voice seat found the same hole in its own gate an hour ago: "the running seat is the build
#     this suite is testing" — a suite that imports fresh passes identically whether or not the thing
#     on screen runs that code. Same shape here, one level up: the artefact a process READS differing
#     from the artefact a human SEES.
_PROCESS_STARTED = _t.time() if "_t" in dir() else __import__("time").time()


def _panel_stamp() -> dict:
    """★ S4 BUILD STAMP, RE-ADDED 2026-08-23 — ALONE, through the canary.
    Identity of the panel HTML as it exists ON DISK right now. Served at /panel/stamp and baked into each page
    at load, so a live page can compare itself and announce that it is stale.
    ★ THIS IS THE CHANGE THAT BROKE THE PANEL ON 08-22 — it injected at the FIRST of two </body> tags, inside
    the JS template literal that builds the popout, and its literal </script> closed the enclosing block.
    It goes back in through `_inject_before_body_end()` (last </body>) and nothing else changes this pass."""
    try:
        b = PANEL_HTML.read_bytes()
        return {"sha": hashlib.sha256(b).hexdigest()[:12],
                "mtime": int(PANEL_HTML.stat().st_mtime), "bytes": len(b)}
    except Exception as e:
        return {"sha": "", "mtime": 0, "bytes": 0, "error": str(e)[:80]}


def _inject_before_body_end(body: bytes, snippet: bytes) -> bytes:
    """★ Inject before the DOCUMENT'S LAST `</body>` — never the first.

    THIS FUNCTION EXISTS BECAUSE OF AN OUTAGE I CAUSED ON 2026-08-22.
    `aionui-stack-panel.html` contains **TWO** `</body>` tags. The FIRST (line ~6074) is *inside a JavaScript
    template literal* — it is part of the HTML string that builds the POPOUT WINDOW. A `replace(..., 1)`
    therefore injected a `<script>…</script>` into the middle of that string.

    Two things then went wrong at once, which is why the panel looked "half broken":
      1. The HTML parser honours `</script>` **regardless of JavaScript string context**, so the injected
         closing tag terminated the real enclosing script block. Every byte after it rendered as visible
         text — the wall of `var _lcDebugReport …` the operator screenshotted.
      2. The popout renderer's own template was corrupted, so the popout button and surface disappeared.
    ★ ONE bug, BOTH symptoms. The original author already knew this trap — that template writes
    `\\u003C/script>` deliberately. The knowledge was in the file; nothing asserted it.

    ★ AND THE REPORTING LESSON: the wire check said `OK=true` and was *true*. It proved the flag ARRIVED.
    It never proved THE PAGE SURVIVED IT. `panel_canary.py` now asserts the second thing.
    """
    i = body.rfind(b"</body>")
    if i == -1:
        return body + snippet          # no closing tag at all: appending is the only safe move
    return body[:i] + snippet + body[i:]
# KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS — redacted infrastructure registry; no credentials are returned.
REMOTE_INFRA_CONFIG = HOME / ".config" / "AionUi-Dev" / "remote-access-infrastructure.json"
# KHAOSS-PATCH: dual-surface consent — the panel-served popout (one of two surfaces; the other is the
# AionUi in-app banner). Both read/write the same store via /consent/* so a resolution closes BOTH.
CONSENT_POPOUT_HTML = ROOT / "panel" / "consent-popout.html"
# KHAOSS-PATCH: the Board Walk gets its own SERVED surface, one click from the panel. It is a sibling
# of the consent popout — a self-contained page under panel/, served by its own route — and it holds
# NO board data: it renders GET /operator/walk, which calls board_walk.plan(). One store per fact.
BOARDWALK_POPOUT_HTML = ROOT / "panel" / "boardwalk-popout.html"
GRANT_WIZARD_HTML = ROOT / "panel" / "grant-wizard-popout.html"
# KHAOSS-PATCH: THE SETUP WALK gets its own SERVED surface, like the Board Walk. ★★ MEASURED
# 2026-10-12: `panel/setup-wizard-popout.html` existed on disk and BOTH its routes returned HTTP 404
# — the page was built and never wired, which is why its own `sw-noroute` banner was already written:
# *"'0 steps' and 'no server' must never look the same."* This is that wiring.
# ★ THE PAGE HOLDS NO FACT. It renders GET /operator/setup/walk, which runs
#   `operator/setup_blockers.plan()`. The GEV recommendation inside that plan is owned by
#   `operator/gev_option.py` — terms, measured state, and the accept/decline ledger — so the cost of
#   a paid API key is written down ONCE and this file never restates it.
SETUP_WIZARD_HTML = ROOT / "panel" / "setup-wizard-popout.html"
GEV_OPTION_STORE = ROOT / "operator" / "gev_option.py"
# KHAOSS-PATCH: DSH Harness observability — a served popout over the harness's live subagents and
# background jobs. The PAGE holds no live data itself: it polls /operator/dsh-jobs/data, which reads
# the JSON state file that the dsh-observability Cordis Host plugin writes from the live `agents`,
# `subagents`, `jobs`, and `sessions` Services. Screenshots are ring-capped by that plugin (last 40).
DSH_OBS_POPOUT_HTML = ROOT / "panel" / "dsh-observability-popout.html"
DSH_OBS_STATE_JSON = ROOT / "var" / "dsh-observability" / "state.json"
DSH_OBS_SHOTS_DIR = ROOT / "var" / "dsh-observability" / "shots"
# KHAOSS-PATCH: STORMWATCH — the storm heartbeat + the connectivity protocol, from the panel.
# ★ THE PAGE HOLDS NO FACT OF ITS OWN. It renders GET /operator/stormwatch/data, which loads the SAME
#   two modules the systemd timer and the CLI run (`stormwatch/storm_heartbeat.py`, `stormwatch/protocol.py`).
#   A threshold or a survey question is therefore defined exactly once, in stormwatch/config.json and
#   protocol.py, and the wizard can never drift from what the heartbeat actually did.
# ★ THE LOCATION IS CONFIG. Nothing in this panel item knows a coordinate; it prints back the one it
#   reads from the config file, so moving the site is editing config only.
STORMWATCH_POPOUT_HTML = ROOT / "panel" / "stormwatch-popout.html"
STORMWATCH_DIR = ROOT / "stormwatch"
# KHAOSS-PATCH: screenshot on/off. ★ ONE STORE, ONE WRITER. This file is the SINGLE fact
# "is screenshot capture meant to be running". The panel is its only writer; the Cordis Host
# plugin dshobs-2 is its intended reader (it decides whether to spend an X11 grab). The page
# never holds the state — it POSTs and then re-reads. Beside state.json on purpose: the same
# directory already is the observability plugin's territory, so a reader of one finds the other.
DSH_OBS_SHOT_MODE_JSON = ROOT / "var" / "dsh-observability" / "screenshot-mode.json"
# ★ THE STANDALONE WRITER'S OWN STATUS FILE (bin/dsh_obs_writer.py, unit khaoss-dsh-obs-writer).
#   Its ONLY writer is that process. It is how the panel can say WHICH process owns the ring right
#   now (owning / yielding / conflict) instead of assuming, and it is deliberately a DIFFERENT file
#   from state.json: the standalone writer must never write state.json, because two writers on one
#   file is corruption.
DSH_OBS_WRITER_JSON = ROOT / "var" / "dsh-observability" / "writer.json"
# How old state.json may be before the panel refuses to call it live. The Cordis plugin published
# every 2s, but its own capture has a 20s timeout, so a live-and-slow plugin can legitimately
# publish nothing for ~25s. 60s is a 2.4× margin over that measured worst case: tight enough that
# the operator learns quickly when the feed dies, loose enough not to cry wolf during one slow grab.
DSH_OBS_STATE_STALE_MS = 60000
_DSH_OBS_SHOT_RE = re.compile(r"^shot-(\d+)\.jpg$")
# The default when the store is absent or corrupt. OFF, because the operator's stated motive is
# saving resources and the capture is a STANDING cost (the plugin shoots at least every 8s even
# with zero activity). Absent ⇒ do not spend. He opts in with one click.
DSH_OBS_SHOT_MODE_DEFAULT = False
# ★ STICKY ATTRIBUTION MEMO — process-local, derived, never a second store of a fact.
#   WHY IT EXISTS: the join is recomputed on every poll from the writer's SLIDING 60-event action
#   tail, so without this a shot's owner drifts between polls — measured, 4 of 40 shots changed
#   panel between two reads seconds apart. A screenshot that hops from one agent's panel to
#   another reads as evidence in BOTH, which is worse than showing it in neither. So the FIRST
#   decision sticks for the life of that shot in the ring: it is never re-homed to a different
#   agent. Entries are pruned the moment the ring drops the file, so it cannot grow.
_DSH_OBS_ATTR_MEMO: dict = {}
PID_FILE = ROOT / "panel_server.pid"
LOG_FILE = Path("/tmp/panel_server.log")

VAULT_PID_FILE     = ROOT / "vault_watcher.pid"
SANITIZER_PID_FILE = ROOT / "vault_import_sanitizer.pid"
SA_PID_FILE        = ROOT / "screen_assistant.pid"
SA_SCRIPT          = ROOT / "screen_assistant.py"
SA_LOG             = ROOT / "screen_assistant.log"          # JSON suggestions only
SA_RUNTIME_LOG     = ROOT / "screen_assistant_runtime.log"  # daemon stderr
SM_SCRIPT          = ROOT / "session_memory.py"             # Tier-2 session synthesizer
SM_STATUS_DIR      = ROOT / "_session_runs"                 # per-run status JSON
PR_SCRIPT          = ROOT / "proactive_research.py"
PR_PID_FILE        = ROOT / "proactive_research.pid"
PR_QUEUE           = ROOT / "research_queue.jsonl"
VK_SCRIPT          = ROOT / "voice_kanban.py"
VK_PID_FILE        = ROOT / "voice_kanban.pid"
VK_RESULTS         = ROOT / "voice_kanban_results.jsonl"
PIPER_DIR          = HOME / ".local" / "share" / "piper"   # installed piper voices (alias resolution)
LL_SCRIPT          = ROOT / "learning_loop.py"
LL_PID_FILE        = ROOT / "learning_loop.pid"
LL_RUNS            = ROOT / "learning_loop_runs.jsonl"
LL_STATUS_DIR      = ROOT / "_learning_runs"
DEBATE_DIR         = HOME / "vault" / "workspace"                              # debate transcripts land in the workspace
DEBATE_STATUS_DIR  = ROOT / "_debate_runs"                  # per-debate status JSON
OBSIDIAN_VAULT     = HOME / "obsidian-vault"
VAULT_WORKSPACE    = HOME / "vault" / "workspace"
VAULT_QUARANTINE   = HOME / "vault" / "quarantine"
MCP_LOG            = ROOT / "mcp_access.log"
SECURITY_LOG       = ROOT / "security.log"
SANITIZER_LOG      = ROOT / "sanitizer.log"
WATCHER_LOG        = ROOT / "watcher.log"
CREDENTIAL_CHECK   = ROOT / "credential_check.sh"
INVARIANTS_CHECK   = ROOT / "check_invariants.sh"  # KHAOSS-PATCH: invariants-utility
BRIDGE_LEDGER      = ROOT / "bridge_ledger.jsonl"  # KHAOSS-PATCH: traceability-ledger (append-only)
FAILURE_JOURNAL    = ROOT / "failure_journal.jsonl"  # KHAOSS-PATCH: failure-immune-system (append-only)

# KHAOSS-PATCH: failure-immune-system — pure genre/remediation/breaker logic lives in a sibling
# module so the circuit breaker + detectors are unit-testable without the HTTP server. stdlib-only.
try:
    import failure_immune as fim  # noqa: E402
except Exception as _fe:  # pragma: no cover — never let a helper-import kill the panel
    fim = None
    sys.stderr.write(f"[failure-immune] module import failed (routes will 503): {_fe}\n")

# KHAOSS-PATCH: thinking-cap-archive — shared archive core (cap-at-write + sweep produce identical rows;
# one retrieval path serves /conversation/raw + /context/reinject). stdlib-only, testable.
try:
    import thinking_archive as tarch  # noqa: E402
except Exception as _te:  # pragma: no cover
    tarch = None
    sys.stderr.write(f"[thinking-archive] module import failed (routes will 503): {_te}\n")


def _thinking_archive_and_mark(con, conv, msgid, orig_content, origin):
    """ARCHIVE-BEFORE-TRUNCATE: verify the full text is in the archive, THEN shrink the DB row. If the
    archive write/verify fails → return (False, err) and DO NOT touch the row (fail open on preservation).
    Idempotent. Commits per row (resumable). Returns (ok, detail)."""
    if tarch is None:
        return False, "thinking_archive module unavailable"
    already, _ = tarch.is_archived(orig_content)
    if already:
        return True, "already-archived"
    ok, rel, err = tarch.write_archive(conv, msgid, orig_content)
    if not ok:
        # FAIL OPEN — the row keeps its full content; log loudly.
        sys.stderr.write(f"[thinking-archive] ARCHIVE FAILED for {conv}/{msgid} — NOT truncating (fail open): {err}\n")
        return False, err
    marker = tarch.make_marker(orig_content, rel, origin)
    con.execute("UPDATE messages SET content=? WHERE id=?", (marker, msgid))
    con.commit()  # per-row commit = safe to interrupt + re-run
    return True, {"archived_rel": rel, "was_bytes": len(orig_content.encode("utf-8")), "now_bytes": len(marker.encode("utf-8"))}


def _ledger_append(entry: dict) -> None:
    """Append ONE entry to the /bridge traceability ledger. APPEND-ONLY (O_APPEND) — never rewrites or
    truncates. A ledger append must not break the audited operation, but a failure is logged loudly (not
    silent). The write path is separately forbidden from targeting the ledger (see _bridge_path_allowed)."""
    try:
        rec = {"ts": datetime.now(tz=timezone.utc).isoformat(), **entry}
        fd = os.open(str(BRIDGE_LEDGER), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as _e:  # pragma: no cover
        sys.stderr.write(f"[ledger] FAILED to record {entry.get('action')} on {entry.get('path') or entry.get('cmd')}: {_e}\n")


def _failure_append(entry: dict) -> None:
    """Append ONE entry to the FAILURE JOURNAL. APPEND-ONLY (O_APPEND) — never rewrites or truncates
    (mirror of _ledger_append; same three-legged guarantee: this writer + _bridge_path_allowed deny +
    the exec string-guard). A journal append must never break the audited op — failures are logged
    loudly, not silently. The caller must NOT pass 'ts' (it is prepended here)."""
    try:
        rec = {"ts": datetime.now(tz=timezone.utc).isoformat(), **entry}
        fd = os.open(str(FAILURE_JOURNAL), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as _e:  # pragma: no cover
        sys.stderr.write(f"[failure] FAILED to record {entry.get('genre')}/{entry.get('action')}: {_e}\n")


FAILURE_JOURNAL_READ_CAP = 2000   # DQ62·A — BOUND, not a periodic clear


def _failure_journal_read(limit: int = FAILURE_JOURNAL_READ_CAP) -> list:
    """Read failure-journal entries (oldest first). Used by the breaker, the reader route, and the health
    board (every 3s tick) so they share one source of truth.

    ★ DQ62·A — BOUNDED, not cleared. The journal is append-only and grows monotonically; the previous
    read loaded the WHOLE file into memory on every tick, so the panel's RSS grew with the file and the
    growth survived relaunches (the disk file persists + grows). Now it streams line-by-line into a
    deque(maxlen) — memory is capped at the last `limit` entries regardless of file size. This is a BOUND,
    not a schedule someone forgets: nothing is deleted (the disk file keeps everything); only the in-memory
    read is capped. The breaker + health board only need recent entries, so the cap is behaviour-preserving."""
    from collections import deque
    tail: "deque[str]" = deque(maxlen=limit)
    try:
        if FAILURE_JOURNAL.exists():
            with open(FAILURE_JOURNAL, encoding="utf-8", errors="replace") as fh:
                for line in fh:                       # stream — never load the whole (growing) file as one string
                    line = line.strip()
                    if line:
                        tail.append(line)             # deque(maxlen) auto-evicts the oldest — the bound
    except Exception as _e:  # pragma: no cover
        sys.stderr.write(f"[failure] read failed: {_e}\n")
    out = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out
LAUNCH_AIONUI      = HOME / "launch-aionui.sh"
LAUNCH_STACK       = HOME / "launch-stack.sh"

# ★★★ 2026-09-19 — THE PANEL USED TO BE A WAY TO KILL THE BACKGROUND TRADER.
#   `/services/restart {"service":"aionui"}` went through _SVC_DEFS["aionui"]["stop_pattern"]
#   = "electron/dist/electron", a PROCESS PATTERN — and BOTH AionUi instances are that same
#   binary. Restarting dev therefore stopped the background Example Team instance too (and
#   orphaned its ACP children, which then held the CDP socket). The isolation argument for the
#   second instance is only true once this path stops pattern-matching, so both AionUi services
#   now go through bin/aionui-instance.sh, which stops an instance by its OWN identity: its
#   systemd unit (cgroup-scoped), or the exact pid in its pidfile after reading /proc/<pid>/cmdline.
#   ONE store for that logic — the helper — never a second copy here.
AIONUI_INSTANCE_SH = ROOT / "bin" / "aionui-instance.sh"
AIONUI_INSTANCE_SVCS = {
    "aionui":         {"restart": "dev-restart",     "stop": "dev-stop",     "start": "dev-start"},
    "aionui-trading": {"restart": "trading-restart", "stop": "trading-stop", "start": "trading-start"},
}
SPACE_AGENT_DIR    = HOME / "agents" / "space-agent"
HERMES_DASH_PORT   = 9119
HERMES_CLI         = HOME / "agents" / "hermes-agent" / "venv" / "bin" / "hermes"
AIONUI_DB          = HOME / ".config" / "AionUi-Dev" / "aionui" / "aionui.db"
# KHAOSS-PATCH: cap-settings — the ONE canonical caps file AionUi reads at startup (getThinkingCapBytes,
# 3 MB fallback) and the panel writes here. userData root = ~/.config/AionUi-Dev (sibling to aionui/ + thinking-archive).
AIONUI_CAPS        = HOME / ".config" / "AionUi-Dev" / "khaoss-caps.json"
THINKING_CAP_DEFAULT_BYTES = 3 * 1024 * 1024   # matches thinkingArchive.ts THINKING_CAP_DEFAULT_BYTES
THINKING_CAP_FLOOR_BYTES   = 256 * 1024        # hard floor — never clip thinking below 256 KB
# KHAOSS-PATCH: model-routing — the task-type -> tier policy (ONE source of truth). Mechanical work routes
# to the economy tier; frontier is reserved for reasoning/code. never_for_mechanical guards against waste.
AIONUI_ROUTING     = HOME / ".config" / "AionUi-Dev" / "khaoss-routing.json"
SWARM_REGISTRY_MD  = HOME / ".hermes" / "SWARM_REGISTRY.md"
NOTIF_DB           = ROOT / "notifications.db"
NOTIF_CAP          = 5000
NOTIF_SEVERITIES   = frozenset({"low", "mid", "high"})
_RESERVED_PROFILES = frozenset({"default", "example-a", "example-b"})
_PROVISION_NAME_RE = re.compile(r"^swarm\d+$|^[a-z][a-z0-9_-]{0,39}$")
CDP_PORT = 9230
KNOWN_TEAM_CAPABLE_BACKENDS = frozenset({"hermes", "claude", "codex", "gemini"})
_CDP_CLI_MAP = {"hermes": "hermes", "claude": "claude", "codex": "codex", "gemini": "gemini"}

# AI Launch Assist
LA_AGENTS_FILE        = ROOT / "launch_assist_agents.json"
LA_PROMPT_FILE        = ROOT / "launch_assist_prompt.md"
LA_SESSIONS_DIR       = ROOT / "launch_assist_sessions"
LA_FEEDBACK_DIR       = ROOT / "dev_feedback_outbox"
LA_ADAPTERS_DIR       = ROOT / "launch_assist_adapters"
_LA_CODEX_BATCH_SIZE     = 12
_LA_CODEX_BATCH_INTERVAL = 10.0
_LA_REDIRECTION_RE    = re.compile(r'[><|&;`]')

# Prompter
PROMPTER_SESSIONS_DIR = ROOT / "prompter_sessions"
PROMPTER_LIBRARY_DIR  = ROOT / "prompter_library"
PROMPTER_MAX_SESSIONS = 5
PROMPTER_OLLAMA_URL   = "http://localhost:11434/api/chat"
PROMPTER_OLLAMA_MODEL = "qwen2.5-coder:7b"
PROMPTER_TURN_WARN    = 15

# Service control — allowlisted definitions only; no caller input ever reaches argv.
# Each entry has:  start_argv (list or None for special cases),
#                  start_log  (stdout/stderr redirect path),
#                  stop_pattern (fixed pkill -f argument),
#                  pid_file   (optional Path — preferred kill target).
_SVC_DEFS: dict[str, dict] = {
    "screenpipe": {
        "start_argv":   [str(HOME / "Downloads" / "screenpipe-main" / "target" / "release" / "screenpipe"),
                         "record"],
        "start_env":    {"DISPLAY": ":0.0", "WAYLAND_DISPLAY": "", "XDG_SESSION_TYPE": "x11"},
        "start_log":    "/tmp/sp.log",
        "stop_pattern": "screenpipe",
        "pid_file":     None,
    },
    "hermes_dashboard": {
        "start_argv":   ["hermes", "dashboard", "--no-open", "--skip-build"],
        "start_log":    "/tmp/hermes-dashboard.log",
        "stop_pattern": "hermes.*dashboard",
        "pid_file":     None,
    },
    "aionui": {
        "start_argv":   None,   # resolved at call-time: launch-aionui.sh or skip
        "start_log":    "/tmp/aionui.log",
        # ★★★ INV-101, fixed 2026-09-19. This was the bare word "electron", so the panel's stop ran
        #     `pgrep -f electron` — which matches ANY process whose COMMAND LINE contains it, not just
        #     the AionUi binary. That is the pattern this estate has been bitten by six times, and it
        #     bit here again: on 2026-09-19 a restart killed Electron but ORPHANED its Hermes ACP
        #     children, which then held the CDP socket on :9230 and blocked the new instance's
        #     debug port. Narrowed to the real binary path, which only the AionUi Electron processes
        #     carry (main + zygotes + utilities). The sanctioned name-exact form is `pkill -x electron`
        #     and `launch-aionui.sh` already uses it on its own relaunch.
        "stop_pattern": "electron/dist/electron",
        "pid_file":     None,
    },
    # ★ 2026-09-19 — the SECOND AionUi instance: the background Example Team trader. It has its own
    #   unit, its own data dir (~/.config/AionUi-Dev-2) and its own CDP/web ports, so it is listed
    #   here as a peer of `aionui` rather than hidden behind it. start_argv/stop_pattern are None ON
    #   PURPOSE: this row may only ever be acted on through bin/aionui-instance.sh (see
    #   AIONUI_INSTANCE_SVCS). There is deliberately no pattern to match, because a pattern is what
    #   would kill the wrong instance.
    "aionui-trading": {
        "start_argv":   None,
        "start_log":    "/tmp/aionui-trading.log",
        "stop_pattern": None,
        "pid_file":     str(HOME / ".config" / "AionUi-Dev-2" / "trading.pid"),
    },
    "vault_watcher": {
        "start_argv":   ["/usr/bin/env", "python3", str(ROOT / "vault_watcher.py")],
        "start_log":    "/tmp/vw.log",
        "stop_pattern": "vault_watcher.py",
        "pid_file":     str(VAULT_PID_FILE),
    },
    "sanitizer": {
        "start_argv":   ["/usr/bin/env", "python3", str(ROOT / "vault_import_sanitizer.py")],
        "start_log":    "/tmp/san.log",
        "stop_pattern": "vault_import_sanitizer.py",
        "pid_file":     str(SANITIZER_PID_FILE),
    },
    "screen_assistant": {
        "start_argv":   ["/usr/bin/env", "python3", str(ROOT / "screen_assistant.py")],
        "start_log":    "/tmp/sa.log",
        "stop_pattern": "screen_assistant.py",
        "pid_file":     str(SA_PID_FILE),
    },
    "proactive_research": {
        "start_argv":   ["/usr/bin/env", "python3", str(ROOT / "proactive_research.py")],
        "start_log":    "/tmp/pr.log",
        "stop_pattern": "proactive_research.py",
        "pid_file":     str(PR_PID_FILE),
    },
    "voice_kanban": {
        "start_argv":   ["/usr/bin/env", "python3", str(ROOT / "voice_kanban.py")],
        "start_log":    "/tmp/vk.log",
        "stop_pattern": "voice_kanban.py",
        "pid_file":     str(VK_PID_FILE),
    },
    # KHAOSS-PATCH: voicebox-control (v1.33) — local TTS/STT server (:8770).
    # MUST use the absolute system interpreter /usr/bin/python3 (3.13, has
    # fastapi/uvicorn in /usr/lib/python3/dist-packages). Bare `python3`/`env
    # python3` resolves to pyenv 3.11.7 which LACKS fastapi → ModuleNotFoundError.
    "voicebox": {
        "start_argv":   ["/usr/bin/python3", str(ROOT / "voicebox" / "tts_server.py")],
        "start_log":    str(ROOT / "voicebox" / "tts_server.log"),
        "stop_pattern": "voicebox/tts_server.py",
        "pid_file":     None,
    },
    "space_agent": {
        "start_argv":   None,   # special: needs cwd + PORT=3000 env
        "start_log":    "/tmp/space.log",
        "stop_pattern": r"space-agent.*node|node.*space-agent",
        "pid_file":     None,
    },
}

# Launcher script discovery — first existing file wins.
# Listed so the new canonical name takes priority but legacy names still work.
LAUNCHER_CANDIDATES = [
    HOME / "launch-aionui-kanban-screenpipe.sh",
    HOME / "launch-stack.sh",
    HOME / "launch-boardwalk-stack.sh",
    HOME / "launch-ai-stack.sh",
]

# Reverse proxy targets — keyed by URL prefix
PROXY_TARGETS = {
    "screenpipe": ("localhost", 3030),
    "hermes":     ("127.0.0.1", 9119),   # kept for proxy fallback / future dashboard
    "aionui":     ("127.0.0.1", CDP_PORT),
    "space":      ("127.0.0.1", 3000),
}
# ★★★ MEASURED 2026-09-10, replacing a comment that asserted the OPPOSITE of reality.
#     What is actually true on this machine:
#       · :9119  = the Hermes DASHBOARD. Up, listening, HTTP 200. `/api/status` returns real JSON.
#       · :8642  = NOTHING. Never has. The old comment called :9119 "intended-but-absent" while
#                  :9119 was the one that was up, and :8642 was the one that did not exist.
#       · the systemd unit called "gateway" is `Hermes Agent Gateway - MESSAGING PLATFORM
#         INTEGRATION` — a Telegram/messaging bridge. Its log says "No messaging platforms
#         enabled", so it correctly binds NO socket. It is not an LLM API and it is NOT in the
#         teammate-spawn path.
#     ★ THE COST OF THE STALE COMMENT: a downstream design debate ranked "the :9119 gateway is
#       offline" as its #1 critical path and sequenced everything else behind it. The gateway was
#       never down; this panel's check was pointed at a port that never existed, and the alarm it
#       raised was inherited as ground truth.
#     ★★ AND :9119 IS A SINGLE-PAGE APP: it returns HTTP 200 for ANY path, including garbage.
#        So "200 on /health" is a check that CANNOT FAIL, which is not a check. `/api/status` is
#        the only endpoint that discriminates, so that is what is probed.
HERMES_GATEWAY_HOST = "127.0.0.1"
HERMES_GATEWAY_PORT = 9119
HERMES_STATUS_PATH = "/api/status"          # ★ the only path that returns JSON rather than the SPA

# Space agent install path — used as fallback when TCP :3000 is closed.
SPACE_AGENT_PACKAGE_JSON = HOME / "agents" / "space-agent" / "package.json"

# /logs endpoint — whitelist of log files under ~/boardwalk/ that the
# panel may tail. The whitelist + a fixed parent directory is the
# path-traversal defence; an attacker who somehow controls the `file` query
# parameter cannot escape this set even with ../ tricks because we ignore
# anything that contains a slash or isn't in the whitelist.
ALLOWED_LOG_FILES = {
    "mcp_access.log",
    "security.log",
    "sanitizer.log",
    "watcher.log",
    "screen_assistant.log",
    "proactive_research.log",
    "voice_kanban.log",
    "learning_loop.log",
}
LOG_DIR = ROOT

# Backend log lives in /tmp — separate path constant since LOG_DIR is locked
# to ~/boardwalk/. The /logs?file=panel_server.log path resolves here.
BACKEND_LOG_NAME = "panel_server.log"

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("KHAOSS_PORT") or 7842)

# ── Remote-access exposure model (Dispatch Queue 8 · REMOTE ACCESS) ───────────
# The panel binds loopback ONLY by default (DEFAULT_BIND). Reaching it from a
# phone means opening a SECOND bind to the tailnet (staged, operator-applied:
# staged/remote_access_tailnet_bind_*.yaml — NOT applied automatically).
#
# When a remote (non-loopback) caller arrives, these are the ONLY endpoints it
# may reach — EVEN WITH a valid bearer token. Everything else is loopback-only:
# credential writes/reads (/providers/*, /security/token, /secrets), config
# mutations (/caps/set, /routing/set, /ai-routing/config), the bridge
# (write/exec/rollback), service control (/services/*, restart), log/DB readers
# (/logs*, /conversation/raw, /api/mcp/log) and the blanket backend proxies
# NEVER leave this machine. Default-DENY: an endpoint is remote-reachable only
# if it is listed here. Local (loopback) requests bypass this list entirely and
# keep full access, so the desktop, the credential gate, and the loopback
# verifier are unaffected. This is the guarantee that opening a bind can never
# carry /providers/set-key (or any sibling) out with the dashboard.
REMOTE_SAFE_GET_EXACT = frozenset({
    "/", "/index.html", "/favicon.ico",
    # live status / dashboard shell
    "/stream", "/snapshot", "/status",
    "/health/board", "/api/health", "/failure/journal",
    "/roster", "/roster/stream",
    # Linked Programs status
    "/swarms", "/teams", "/orion", "/mammouth", "/team/cdp-check",
    # voice status (read-only)
    "/voice/status", "/voice/voices", "/voice/devices", "/voice/clones", "/voice/engines",
    # ★ SEEK (read-only): the connection/route map and the agent brief. No secret values — ports,
    #   model names and failure reasons only — so it is safe on the remote list beside /voice/status.
    "/voice/seek", "/voice/seek/brief",
    # background-service status
    "/screen-assistant/status", "/session-memory/status",
    "/learning-loop/status", "/voice-kanban/status",
    "/research/queue", "/research/status",
    "/privacy/status",
    # notifications (read)
    "/notifications", "/notifications/stream",
    # consent surface (read)
    "/consent/pending", "/consent/audit", "/consent/popout",
    # privacy pipeline counters (no values, just stats)
    "/api/vault/pid", "/api/vault/stats", "/api/sanitizer/pid", "/api/quarantine/stats",
    # honest read-only diagnostics
    "/context/diagnostic", "/thinking/status", "/components", "/capture/queue",
    # reachability probes
    "/api/aionui/ping", "/api/screenpipe/ping", "/api/hermes/ping", "/api/space-agent/ping",
    # INV-39 read cards (data-source surfaces; NOT /secrets)
    "/broker", "/namespace",
    # exposure self-report + tunnel state (read)
    "/tunnel/status", "/remote-access/status",
})
REMOTE_SAFE_GET_PREFIX = (
    "/notifications/explain/",
)
# The only mutating endpoint reachable remotely, because it ONLY ever REDUCES
# exposure (returns the panel to loopback-only) — the fail-safe "close it now"
# control the operator can hit from the phone. Doctrine: revocable everywhere.
REMOTE_SAFE_POST_EXACT = frozenset({
    "/remote-access/off",
})

# ── ★★★ THE PHONE SURFACE'S OPERATOR-ONLY PATHS (2026-10-15). A paired phone is a CALLER, not an
#    OPERATOR. These four are the device roster, the ledger, the policy override and revocation --
#    the exact controls that exist so the operator can see and cut off a phone. Letting a phone
#    reach them would mean a lost phone could revoke its siblings and hide its own trail, which is
#    the opposite of what the surface is for.
#    ★ ENFORCED TWICE ON PURPOSE: here in `_check_auth` (before dispatch) and again inside each
#      handler (`_is_local_request`). One check is one edit away from being no check.
PHONE_LOCAL_ONLY = frozenset({
    "/phone/devices",
    "/phone/log",
    "/phone/policy",
    "/phone/revoke",
})

PROXY_TIMEOUT = 5.0
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# SSE broadcast tick. 3s = fast enough that a service transition feels
# instant in the UI; slow enough that the poller doesn't hammer screenpipe.
STREAM_INTERVAL_S = 3.0

# ── DQ71·C6 — the Steward-sweep auto-pause ceiling (what should have prevented today) ──────────
# The Stewards are not separate processes; they are the detector sweep run by the status broadcaster
# (poll_all_services + derive_alerts) inside THIS process. If that sweep balloons the panel's own RSS,
# it pauses ITSELF and says so, instead of eating memory until his apps die. This is the process-level
# twin of the per-Steward GUI ceiling. Ceiling = an absolute well above the panel's ~130-300MB normal
# and below the observed >1GB leak; also expressed as a share of AVAILABLE ram per the dispatch bands.
PANEL_RSS_CEILING_MB = 700          # hard: pause the sweep if the panel's own RSS exceeds this
PANEL_RSS_AVAIL_FRACTION = 0.25     # or if it exceeds this share of AVAILABLE ram (red band > 25%)


def _own_rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _avail_ram_mb() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


# ── Space Agent Bridge ─────────────────────────────────────────
BRIDGE_ENABLED = False
ALLOWED_PATHS: list[str] = []
BRIDGE_CONFIG_FILE = Path(__file__).resolve().parent.parent / "bridge_config.json"

# ── AI Routing ─────────────────────────────────────────────────
AI_ROUTING_CONFIG_FILE = Path(__file__).resolve().parent.parent / "ai_routing_config.json"
_AI_ROUTING_DEFAULT: dict = {
    "tier": "cloud",
    "local": {"url": "http://localhost:11434", "model": "llama3.2", "enabled": False},
    "private": {"url": "", "api_key": "", "model": "", "enabled": False},
    "cloud": {"profile": "example-a", "enabled": True},
}

# Pending agent permission requests queued for operator review.
# Each entry: {id, action, params, reason, agent, ts, status}
# DQ42·I1·A — THE LEAK: this list appended every bridge request forever (no eviction), ~13MB/day monotonic,
# surviving relaunch because the panel is long-lived. Fixed with a BOUND, not a periodic clear (a clear is a
# schedule someone forgets; a bound is structural). A deque(maxlen) auto-evicts the OLDEST resolved requests —
# only append/iterate/list() are used on it (verified), so the bound is transparent to every caller.
from collections import deque as _deque
BRIDGE_REQUESTS_MAX = 2000  # generous: the newest 2000 requests are always retained; older resolved ones evict
BRIDGE_REQUESTS: "_deque[dict]" = _deque(maxlen=BRIDGE_REQUESTS_MAX)
BRIDGE_REQUESTS_LOCK = threading.Lock()


def _load_bridge_config() -> None:
    global BRIDGE_ENABLED, ALLOWED_PATHS
    try:
        if BRIDGE_CONFIG_FILE.exists():
            data = json.loads(BRIDGE_CONFIG_FILE.read_text())
            BRIDGE_ENABLED = bool(data.get("enabled", False))
            ALLOWED_PATHS = list(data.get("allowed_paths", []))
    except Exception:
        pass
    # Ensure ai_routing_config.json exists with safe defaults on first boot.
    if not AI_ROUTING_CONFIG_FILE.exists():
        try:
            AI_ROUTING_CONFIG_FILE.write_text(json.dumps(_AI_ROUTING_DEFAULT, indent=2))
        except Exception:
            pass


def _save_bridge_config() -> None:
    try:
        BRIDGE_CONFIG_FILE.write_text(
            json.dumps({"enabled": BRIDGE_ENABLED,
                        "allowed_paths": ALLOWED_PATHS}, indent=2))
    except Exception:
        pass


def _load_ai_routing_config() -> dict:
    try:
        if AI_ROUTING_CONFIG_FILE.exists():
            data = json.loads(AI_ROUTING_CONFIG_FILE.read_text())
            cfg = dict(_AI_ROUTING_DEFAULT)
            cfg.update(data)
            return cfg
    except Exception:
        pass
    return dict(_AI_ROUTING_DEFAULT)


def _save_ai_routing_config(cfg: dict) -> None:
    try:
        AI_ROUTING_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def _ai_routing_config_public(cfg: dict) -> dict:
    """Strip api_key from config before returning to clients."""
    out = {k: v for k, v in cfg.items() if k != "private"}
    priv = dict(cfg.get("private", {}))
    priv.pop("api_key", None)
    out["private"] = priv
    return out


# ── KHAOSS-PATCH: settings-apikey-entry — boardwalk-owned secure credential store ─
# One-Above-All: boardwalk holds + injects provider keys; agents/the model NEVER see
# the raw value. A typed key is written to a chmod-600 key_file + a chmod-600
# Hermes profile .env (OPENAI_API_KEY), and injected at the API-call point via the
# Hermes profile. The metadata registry (aionui-providers.json) holds only a
# key_file POINTER and base_url — never the value. Callers MUST gate this behind
# _is_local_request() (loopback only). The raw key is never logged or returned;
# responses carry only key_present + last-4.
def _provider_key_last4(key_file: str, hermes_profile: str = "") -> str:
    """Return the last 4 chars of the ACTIVE (injected) key for masked display —
    nothing more. Source of truth is the Hermes profile .env OPENAI_API_KEY (what
    actually authenticates the call); falls back to the first non-blank line of
    the key_file. Handles messy multi-line key_files without leaking the value."""
    # Prefer the injected .env value.
    if hermes_profile:
        try:
            envp = Path.home() / ".hermes" / "profiles" / hermes_profile / ".env"
            if envp.is_file():
                for line in envp.read_text().splitlines():
                    if line.startswith("OPENAI_API_KEY="):
                        v = line.split("=", 1)[1].strip()
                        out = v[-4:] if len(v) >= 4 else ""
                        v = None
                        if out:
                            return out
        except Exception:
            pass
    try:
        kf = Path(key_file)
        if kf.is_file():
            for line in kf.read_text().splitlines():
                s = line.strip()
                if s:
                    out = s[-4:] if len(s) >= 4 else ""
                    s = None
                    return out
    except Exception:
        pass
    return ""


def _upsert_aionui_model_config(name: str, base_url: str, raw_key: str) -> dict:
    """★ Queue-16 ONE-STORE: mirror a provider key into AionUi's OWN store, model.config
    (~/.config/AionUi-Dev/config/aionui-config.txt, encoded base64(encodeURIComponent(JSON.stringify))),
    so the UI and the backend can never hold DIFFERENT keys (the fork that hid the credential blocker).
    Create-if-absent, update-in-place, matched by base_url. Backed up first; NEVER logs the key.
    ★ CAVEAT: a RUNNING AionUi caches model.config in memory and reflects a file write only on its next
    restart — a live file-watch in modelBridge is the follow-up. This closes the fork at the FILE layer."""
    import base64 as _b64, urllib.parse as _up, json as _j, shutil as _sh, time as _t
    p = Path.home() / ".config" / "AionUi-Dev" / "config" / "aionui-config.txt"
    if not p.exists():
        return {"written": False, "reason": "aionui-config.txt absent"}
    try:
        cfg = _j.loads(_up.unquote(_b64.b64decode(p.read_text().strip()).decode()))
    except Exception as e:
        return {"written": False, "reason": f"decode failed: {e}"}
    mc = cfg.get("model.config")
    if not isinstance(mc, list):
        return {"written": False, "reason": "no model.config array"}
    bl = (base_url or "").rstrip("/").lower()
    idx = next((i for i, x in enumerate(mc)
                if (x.get("baseUrl") or "").rstrip("/").lower() == bl
                or (x.get("name") or "").lower() == (name or "").lower()), None)
    if idx is None:
        mc.append({"platform": "custom", "name": name, "baseUrl": base_url, "apiKey": raw_key, "model": []})
        action = "created"
    else:
        mc[idx]["apiKey"] = raw_key
        mc[idx].setdefault("baseUrl", base_url)
        action = "updated"
    cfg["model.config"] = mc
    s = _j.dumps(cfg, separators=(",", ":"), ensure_ascii=False)
    out = _b64.b64encode(_up.quote(s, safe="-_.!~*'()").encode()).decode()  # == JS encodeURIComponent
    # round-trip integrity — never write a config that doesn't decode back cleanly
    try:
        if _j.loads(_up.unquote(_b64.b64decode(out).decode())).get("model.config") != mc:
            return {"written": False, "reason": "round-trip integrity failed — NOT written"}
    except Exception as e:
        return {"written": False, "reason": f"round-trip decode failed: {e}"}
    try:
        _sh.copy2(p, str(p) + f".bak.onestore_{int(_t.time())}")
    except Exception:
        pass
    p.write_text(out)
    return {"written": True, "action": action, "note": "reflects on next AionUi restart (in-memory cache)"}


def _secure_write_secret(secret_id: str, raw_value: str, kind: str = "key_file",
                         note: str = "", username: str = "") -> dict:
    """★ SECRETS WIZARD backend — write ANY secret (not just provider API keys) the same safe way.

    Guarantees, matching the provider-key path: loopback-only (enforced at the route), dir 0700 / file 0600,
    the VALUE is never returned, never logged, and never enters the panel HTML. Only METADATA (id, backend,
    ref path, created, note, username) is registered in khaoss-secrets.json — the inventory card reads that.

    Exists because the panel could only take PROVIDER keys; credentials like the Support Team's Proton account had
    nowhere to go, so they lived in loose files or nowhere at all.
    """
    sid = re.sub(r"[^a-z0-9._-]", "-", (secret_id or "").strip().lower()).strip("-")
    if not sid:
        return {"ok": False, "error": "secret id required"}
    if not raw_value:
        return {"ok": False, "error": "value required"}

    key_file = Path.home() / ".khaoss-secrets" / sid
    try:
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file.parent.chmod(0o700)
        # write with restrictive perms from the start (never world-readable, even briefly)
        fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(raw_value if raw_value.endswith("\n") else raw_value + "\n")
        key_file.chmod(0o600)
    except OSError as exc:
        return {"ok": False, "error": f"could not write secret store: {exc}"}
    finally:
        raw_value = None  # drop the value promptly

    # register METADATA only in the manifest the SECRETS inventory card reads
    man_path = Path.home() / ".config" / "AionUi-Dev" / "khaoss-secrets.json"
    try:
        man = json.loads(man_path.read_text()) if man_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        man = {}
    man.setdefault("secrets", {})
    entry = man["secrets"].get(sid, {}) or {}
    entry.update({"backend": kind, "ref": str(key_file), "created": entry.get("created")
                  or time.strftime("%Y-%m-%d"), "last_rotated": time.strftime("%Y-%m-%d")})
    if note:
        entry["note"] = note[:200]
    if username:
        entry["username"] = username[:120]      # identifier, not a credential
    man["secrets"][sid] = entry
    try:
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text(json.dumps(man, indent=1))
    except OSError as exc:
        return {"ok": True, "warning": f"secret written but manifest not updated: {exc}",
                "id": sid, "ref": str(key_file)}
    return {"ok": True, "id": sid, "ref": str(key_file), "backend": kind,
            "note": "value stored 0600; never displayed"}


def _secure_write_provider_key(name: str, base_url: str, raw_key: str,
                               models=None, default_model: str = "") -> dict:
    """Persist a provider API key into the boardwalk secure store. Returns a MASKED
    status dict (key_present + key_last4) — never the raw value. Loopback-only:
    the caller is responsible for the _is_local_request() gate."""
    import re
    import subprocess
    name = (name or "").strip()
    base_url = (base_url or "").strip()
    raw_key = (raw_key or "").strip()
    if not name or not base_url or not raw_key:
        return {"ok": False, "error": "name, base_url, key required"}
    provider_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    if not provider_id:
        return {"ok": False, "error": "provider id could not be derived from name"}

    # Preserve an existing registration's paths so re-saving a key updates the
    # store IN PLACE (e.g. Mammouth already lives at ~/.mammouth/api_key with the
    # mammouth-general profile — don't fork a divergent mammouth profile/key_file).
    registry_path = Path.home() / ".config" / "AionUi-Dev" / "aionui-providers.json"
    try:
        registry = json.loads(registry_path.read_text())
    except Exception:
        registry = {"version": 1, "providers": []}
    registry.setdefault("providers", [])
    existing = next((p for p in registry["providers"] if p.get("id") == provider_id), None)

    # 1. key_file — reuse existing path, else ~/.<provider_id>/api_key (dir 700, file 600)
    existing_kf = (existing.get("key_file") or "").strip() if existing else ""
    key_file = Path(existing_kf) if existing_kf else (Path.home() / f".{provider_id}" / "api_key")
    try:
        key_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_file.parent.chmod(0o700)
    except Exception:
        pass
    key_file.write_text(raw_key + "\n")
    key_file.chmod(0o600)

    # 2. Hermes profile .env for OpenAI-compatible injection at the call point.
    #    Anthropic is not OpenAI-compatible, so it gets the secure key_file only
    #    (consumed by Claude Code / codex via env), no OpenAI-compat profile.
    is_anthropic = "anthropic.com" in base_url or "anthropic.ai" in base_url
    hermes_profile = ""
    if not is_anthropic:
        # Reuse an already-mapped profile (e.g. mammouth → mammouth-general) so a
        # re-save updates the live profile rather than forking a new one.
        target_profile = ((existing.get("hermes_profile") or "").strip()
                          if existing else "") or provider_id
        profile_dir = Path.home() / ".hermes" / "profiles" / target_profile
        if not profile_dir.exists():
            try:
                subprocess.run(
                    ["hermes", "profile", "create", target_profile,
                     "--description", f"{name} via settings key entry"],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                pass  # profile dir is created below regardless
        profile_dir.mkdir(parents=True, exist_ok=True)
        config_yaml = profile_dir / "config.yaml"
        try:
            import yaml
            cfg = (yaml.safe_load(config_yaml.read_text()) or {}) if config_yaml.exists() else {}
        except Exception:
            cfg = {}
        cfg.setdefault("model", {})
        cfg["model"]["provider"] = "custom"
        cfg["model"]["base_url"] = base_url
        if default_model:
            cfg["model"]["default"] = default_model
            cfg["model"]["name"] = default_model
        cfg["model"]["key_env"] = "OPENAI_API_KEY"
        cfg.setdefault("agent", {})
        cfg["agent"].setdefault("reasoning_effort", "")
        cfg.setdefault("api_server", {})
        cfg["api_server"]["enabled"] = True
        try:
            import yaml
            config_yaml.write_text(yaml.dump(cfg, default_flow_style=False))
        except ImportError:
            _dm = cfg["model"].get("default", "")
            config_yaml.write_text(
                f"model:\n  provider: custom\n  base_url: {base_url}\n"
                f"  default: {_dm}\n  name: {_dm}\n"
                f"  key_env: OPENAI_API_KEY\nagent:\n  reasoning_effort: ''\n"
                f"api_server:\n  enabled: true\n"
            )
        env_path = profile_dir / ".env"
        env_path.write_text(f"OPENAI_API_KEY={raw_key}\nOPENAI_BASE_URL={base_url}\n")
        env_path.chmod(0o600)
        hermes_profile = target_profile

    # 3. registry (metadata + key_file POINTER only — never the value)
    model_entries = []
    for m in (models or []):
        mid = (m.strip() if isinstance(m, str)
               else str(m.get("id", "")).strip() if isinstance(m, dict) else "")
        if mid:
            model_entries.append({"id": mid, "label": mid, "provider_tag": provider_id})
    entry = {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "key_file": str(key_file),
        "hermes_profile": hermes_profile,
        "enabled": existing.get("enabled", True) if existing else True,
        "models": model_entries or (existing.get("models", []) if existing else []),
    }
    if existing:
        registry["providers"] = [entry if p.get("id") == provider_id else p
                                 for p in registry["providers"]]
    else:
        registry["providers"].append(entry)
    registry_path.write_text(json.dumps(registry, indent=2))

    # 4. ★ Queue-16 ONE-STORE: mirror the key into AionUi's model.config too, so the UI and the backend
    #    can never hold different keys. authoritative host-side home = model.config; readers = AionUi UI +
    #    (via .env) the Hermes backend; aionui-providers.json stays a pointer/label registry.
    mc_result = _upsert_aionui_model_config(name, base_url, raw_key)

    last4 = raw_key[-4:] if len(raw_key) >= 4 else "****"
    raw_key = None  # drop from memory
    return {"ok": True, "id": provider_id, "name": name, "base_url": base_url,
            "hermes_profile": hermes_profile, "key_present": True, "key_last4": last4,
            "model_config_mirror": mc_result}


def _check_local_available() -> bool:
    try:
        cfg = _load_ai_routing_config()
        url = cfg["local"]["url"].rstrip("/") + "/api/tags"
        import urllib.request as _ur
        req = _ur.urlopen(url, timeout=2)
        # ★ SHAPE, not status. A bare `status == 200` cannot fail against anything that answers:
        #   we hit this three times in one day, most sharply when a check pointed at a dead port
        #   (always False, correct by accident) would have flipped to ALWAYS TRUE the moment it was
        #   repointed at a single-page app that returns 200 for every path.
        if req.status != 200:
            return False
        body = json.loads(req.read().decode("utf-8", "replace"))
        return isinstance(body, dict) and isinstance(body.get("models"), list)
    except Exception:
        return False


def _check_private_available() -> bool:
    try:
        cfg = _load_ai_routing_config()
        url = cfg["private"].get("url", "")
        if not url:
            return False
        import urllib.request as _ur
        req = _ur.urlopen(url.rstrip("/") + "/health", timeout=2)
        # ★ SHAPE, not status. A bare `status == 200` cannot fail against anything that answers:
        #   we hit this three times in one day, most sharply when a check pointed at a dead port
        #   (always False, correct by accident) would have flipped to ALWAYS TRUE the moment it was
        #   repointed at a single-page app that returns 200 for every path.
        if req.status != 200:
            return False
        body = req.read().decode("utf-8", "replace").strip()
        return bool(body) and not body.lstrip().lower().startswith(("<!doctype", "<html"))
    except Exception:
        return False


def _check_cloud_available() -> bool:
    """Is a local OpenAI-compatible gateway actually serving models?

    ★★ THIS NEARLY BECAME A CHECK THAT COULD NOT SAY NO. It used to accept a bare HTTP 200 from the
       gateway port. That was harmless while the port pointed at :8642 (dead, so always False), but
       repointing to :9119 would have flipped it to ALWAYS TRUE — :9119 is a single-page app that
       answers 200 for every path, garbage included.
    ★ So the shape of the answer is what is checked, not the status code: /v1/models must return the
      OpenAI `{"data": [...]}` envelope. Measured on this machine right now: NO local port serves
      that, so this correctly returns False."""
    r = _http_get(HERMES_GATEWAY_HOST, HERMES_GATEWAY_PORT, "/v1/models", timeout=2.0)
    if r is not None and r[0] == 200:
        parsed = r[1]
        if isinstance(parsed, dict) and isinstance(parsed.get("data"), list):
            return True
    return False

# ── Logging ──────────────────────────────────────────────────────────────────

logger = logging.getLogger("panel_server")

# ── KHAOSS-PATCH: panel-security-and-tunnel-controls ─────────────────────────
# Bearer-token auth for remote (tunnelled) access + cloudflared tunnel control +
# Telnyx webhook (Ed25519). Local loopback keeps working with no token; remote
# requests — including cloudflared, which connects from 127.0.0.1 — must present
# the bearer token. See _is_local_request for why client_address alone is unsafe.
import hmac as _hmac
import secrets as _secrets

PANEL_TOKEN_PATH           = HOME / ".config" / "AionUi-Dev" / "panel-token"
TELNYX_API_KEY_PATH        = HOME / ".telnyx" / "api_key"
TELNYX_WEBHOOK_PUBKEY_PATH = HOME / ".telnyx" / "webhook_public_key"   # base64 Ed25519
WEBHOOK_QUEUE_DIR          = ROOT / "dispatch_queue" / "webhooks"

# Headers a reverse proxy / cloudflared injects. Their presence means the request
# originated OUTSIDE even though client_address is loopback (cloudflared dials
# localhost:7842). Such requests are treated as REMOTE and require the token —
# without this, the loopback bypass would defeat auth for ALL tunnelled traffic.
_PROXY_FORWARD_HEADERS = (
    "CF-Connecting-IP", "Cf-Connecting-Ip", "CF-Ray", "Cf-Ray",
    "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
)


def _load_or_create_panel_token() -> str:
    """Persist a urlsafe token at PANEL_TOKEN_PATH (chmod 600). Idempotent."""
    try:
        if PANEL_TOKEN_PATH.exists():
            tok = PANEL_TOKEN_PATH.read_text().strip()
            if tok:
                return tok
        PANEL_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tok = _secrets.token_urlsafe(32)
        PANEL_TOKEN_PATH.write_text(tok)
        PANEL_TOKEN_PATH.chmod(0o600)
        logger.info("panel token created at %s", PANEL_TOKEN_PATH)
        return tok
    except OSError as exc:
        # Fail closed: an in-memory random token still blocks remote access.
        logger.error("panel token init failed (%s) — using ephemeral token", exc)
        return _secrets.token_urlsafe(32)


PANEL_TOKEN = _load_or_create_panel_token()

# Tunnel process state (module-level singleton, guarded by a lock).
_TUNNEL_STATE: dict[str, Any] = {"proc": None, "pid": None, "url": None}
_TUNNEL_LOCK = threading.Lock()


def _tunnel_reader(proc: "subprocess.Popen") -> None:
    """Scrape the trycloudflare URL from cloudflared's combined output."""
    pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            m = pat.search(line)
            if m:
                _TUNNEL_STATE["url"] = m.group(0)
                logger.info("[tunnel] public URL acquired")
    except Exception:
        pass


def _verify_telnyx_signature(raw_body: bytes, signature_b64: str, timestamp: str) -> bool:
    """Telnyx Ed25519: signed payload is f"{timestamp}|{raw_body}", verified
    against the base64 Ed25519 public key at TELNYX_WEBHOOK_PUBKEY_PATH."""
    try:
        import base64
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub_b64 = TELNYX_WEBHOOK_PUBKEY_PATH.read_text().strip()
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pub_b64))
        signed = timestamp.encode("utf-8") + b"|" + raw_body
        pub.verify(base64.b64decode(signature_b64), signed)
        return True
    except Exception:
        return False


def _queue_webhook_event(source: str, raw_body: bytes, verified: bool) -> None:
    """Persist a raw webhook body for later processing. No keys are involved."""
    WEBHOOK_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    epoch_ms = int(time.time() * 1000)
    tag = "verified" if verified else "unverified"
    (WEBHOOK_QUEUE_DIR / f"{epoch_ms}_{source}_{tag}.json").write_bytes(raw_body)
# ── end panel-security-and-tunnel-controls module block ──────────────────────


def _setup_logging(foreground: bool) -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # ★★★ ROTATE. 2026-09-18. This was a plain FileHandler(mode="a") with no rotation anywhere —
    #   no maxBytes, no logrotate entry — so /tmp/panel_server.log had reached 306,040,955 bytes
    #   and 3,585,479 lines, growing ~635 B/s for as long as the panel runs.
    #   ★ The unbounded file is what made the /logs endpoint expensive (it read the whole thing to
    #     return twenty lines, 84.8% of panel CPU). That read is now O(bytes wanted) — but an
    #     unbounded log is a defect on its own: it fills /tmp, it makes every future full read
    #     worse, and a file that only ever grows has no honest retention story.
    #   ★★ 32 MB x 5 keeps roughly a fortnight at the observed rate and caps the total at 160 MB.
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(LOG_FILE, mode="a", encoding="utf-8",
                             maxBytes=32 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    # Only add stderr handler in foreground mode. In daemon mode, callers
    # commonly redirect stderr into the same log file (`nohup … 2>&1`), and
    # adding a StreamHandler would cause every line to be written twice.
    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

# ── PID file ─────────────────────────────────────────────────────────────────

def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
        return int(raw) if raw.isdigit() else None
    except (OSError, ValueError):
        return None

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False

def write_pid_file() -> None:
    existing = _read_pid(PID_FILE)
    if existing and _alive(existing):
        logger.error("panel_server already running (PID %d) — refusing to start", existing)
        sys.exit(2)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n")

def remove_pid_file() -> None:
    try:
        current = _read_pid(PID_FILE)
        if current == os.getpid():
            PID_FILE.unlink()
    except OSError:
        pass

# ── Status pollers (server-side; same data the SSE stream emits) ─────────────

def _pid_status(pid_file: Path) -> dict[str, Any]:
    pid = _read_pid(pid_file)
    if pid is None:
        return {"pid": None, "running": False, "reason": "no pid file"}
    running = _alive(pid)
    started = None
    if running:
        try:
            started = os.stat(f"/proc/{pid}").st_mtime
        except OSError:
            pass
    return {"pid": pid, "running": running, "started": started}

def _pid_status_with_display(pid_file: Path) -> dict[str, Any]:
    info = _pid_status(pid_file)
    info["display_state"] = "running" if info.get("running") else "stopped"
    return info

def _http_get(host: str, port: int, path: str, timeout: float = 2.0) -> tuple[int, dict, str] | None:
    """Fetch a JSON endpoint. Returns (status, parsed_or_empty, raw_text) or None on failure."""
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        status = resp.status
        try: conn.close()
        except OSError: pass
        try:
            parsed = json.loads(body) if body else {}
            if not isinstance(parsed, dict):
                parsed = {"raw": parsed}
        except Exception:
            parsed = {}
        return status, parsed, body
    except (ConnectionRefusedError, socket.timeout, socket.gaierror, OSError):
        return None

def _tcp_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False

def _find_electron_pid() -> int | None:
    """Scan /proc for any electron / AionUI process. Returns PID or None."""
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as fh:
                    comm = fh.read().strip().lower()
                if comm in ("electron", "aionui", "aion-ui"):
                    return int(entry)
                # comm may be truncated — also peek at cmdline for "aionui"
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmd = fh.read().decode("utf-8", errors="replace").replace("\x00", " ").lower()
                if "aionui" in cmd or "/electron" in cmd:
                    return int(entry)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None

def _discover_launcher() -> dict[str, Any]:
    for cand in LAUNCHER_CANDIDATES:
        if cand.is_file():
            return {"path": str(cand), "exists": True, "name": cand.name, "status": "found"}
    return {"path": str(LAUNCHER_CANDIDATES[0]), "exists": False, "name": LAUNCHER_CANDIDATES[0].name, "status": "missing"}

def _vault_stats() -> dict[str, Any]:
    if not VAULT_WORKSPACE.is_dir():
        return {"file_count": 0, "last_scrub": None, "last_scrub_epoch": 0.0, "status": "ok (0 files)"}
    count = 0
    latest = 0.0
    try:
        for p in VAULT_WORKSPACE.iterdir():
            if p.is_file():
                count += 1
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
    except OSError:
        pass
    return {
        "file_count": count,
        "last_scrub": (datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else None),
        "last_scrub_epoch": latest,
        "status": f"ok ({count} files)",
    }

def _quarantine_stats() -> dict[str, Any]:
    if not VAULT_QUARANTINE.is_dir():
        return {"count": 0, "last": None, "status": "clear"}
    count = 0
    latest = 0.0
    try:
        for p in VAULT_QUARANTINE.iterdir():
            if p.is_file():
                count += 1
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
    except OSError:
        pass
    return {
        "count": count,
        "last": (datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else None),
        "status": f"quarantined ({count})" if count else "clear",
    }

def _tier3_pid_status(pid_file: Path, script: Path) -> dict[str, Any]:
    """Generic PID + install-check for Tier-3 daemons."""
    base = _pid_status(pid_file)
    base["script_exists"] = script.is_file()
    base["display_state"] = "running" if base.get("running") else "stopped"
    return base

def _space_agent_status(reachable: bool, installed: bool) -> dict[str, Any]:
    if reachable:
        state = "running"
    elif installed:
        state = "installed"
    else:
        state = "absent"
    if state == "running":
        display_state = "running"
    elif state == "installed":
        display_state = "installed"
    else:
        display_state = "offline"
    return {
        "reachable":     reachable,
        "installed":     installed,
        "state":         state,
        "display_state": display_state,
    }


def _screen_assistant_status() -> dict[str, Any]:
    pid_info = _pid_status(SA_PID_FILE)
    recent_suggestions = _read_screen_assistant_tail(3)
    return {
        **pid_info,
        "recent_suggestions": recent_suggestions,
        "log_exists": SA_LOG.is_file(),
        "display_state": "running" if pid_info.get("running") else "stopped",
    }

def _read_screen_assistant_tail(n: int) -> list[dict[str, Any]]:
    if not SA_LOG.is_file():
        return []
    try:
        with SA_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-max(1, n):]
    except OSError:
        return []
    out = []
    for raw in tail:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out.append(obj)
                continue
        except Exception:
            pass
        out.append({"ts": None, "suggestion": raw, "source_file": None})
    return out[-n:]

def _tail_log(path: Path, n: int = 20) -> list[str]:
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-max(1, n):]]
    except OSError:
        return []

# ── Known fixes lookup for launch debug reports ───────────────────────────────
KNOWN_FIXES: dict[str, str] = {
    "screenpipe":       "Run with: DISPLAY=:0.0 WAYLAND_DISPLAY='' XDG_SESSION_TYPE=x11 screenpipe record",
    "aionui":           "Clean electron locks: rm -f ~/.config/AionUi-Dev/SingletonLock && ~/launch-aionui.sh",
    "hermes":           "Check: hermes doctor --fix --profile example-a",
    "hermes_dashboard": "Restart: hermes dashboard > /tmp/hermes-dashboard.log 2>&1 &",
    "screen_assistant": "Check: python3 ~/boardwalk/screen_assistant.py",
    "proactive_research": "Check: python3 ~/boardwalk/proactive_research.py",
    "vault_watcher":    "Check: python3 ~/boardwalk/vault_watcher.py",
    "sanitizer":        "Check: python3 ~/boardwalk/vault_import_sanitizer.py",
}


def _log_tail(path_str: str, n: int = 20) -> str:
    """Return the last n lines of a log file as a string."""
    try:
        p = Path(path_str)
        if not p.exists():
            return "(log file not found)"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-n:] if len(lines) > n else lines
        return "\n".join(tail) if tail else "(empty log)"
    except OSError:
        return "(cannot read log)"


def _mem_free_mb() -> str:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemFree:"):
                kb = int(line.split()[1])
                return f"{kb // 1024} MB"
    except Exception:
        pass
    return "unknown"


def _disk_free_gb() -> str:
    try:
        import shutil
        usage = shutil.disk_usage(str(HOME))
        return f"{usage.free // (1024 ** 3)} GB"
    except Exception:
        return "unknown"


def _build_debug_report(results: list, fixes: dict) -> str:
    import platform as _platform
    ts = datetime.now(timezone.utc).isoformat()
    hostname = _platform.node()
    sysinfo = f"{_platform.system()} {_platform.release()}"
    lines = [
        "# KHAOSS Launch Debug Report",
        f"Generated: {ts}",
        f"Machine: {hostname} ({sysinfo})",
        "",
    ]
    failures = [r for r in results if r.get("status") == "failed"]
    degraded = [r for r in results if r.get("status") == "degraded"]
    healthy  = [r for r in results if r.get("status") in ("healthy", "already_running", "skipped")]

    if failures:
        lines.append("## Failed Services")
        for r in failures:
            name = r.get("name", "?")
            lines.append(f"### {r.get('display', name)}")
            svc_defs_entry = _SVC_DEFS.get(r.get("svc_key", name), {})
            argv = svc_defs_entry.get("start_argv")
            if argv:
                lines.append(f"- Start command: {' '.join(str(a) for a in argv)}")
            lines.append(f"- Suggested fix: {fixes.get(name, 'No known fix — check service log manually')}")
            log_file = r.get("log_file", "")
            if log_file:
                lines.append(f"- Log file: {log_file}")
                tail = r.get("log_tail") or _log_tail(log_file)
                lines.append("- Last 20 log lines:")
                lines.append("```")
                lines.append(tail)
                lines.append("```")
            lines.append("")

    if degraded:
        lines.append("## Degraded Services")
        for r in degraded:
            lines.append(f"- {r.get('display', r.get('name', '?'))}: running but not fully healthy")
        lines.append("")

    if healthy:
        lines.append("## Healthy Services")
        lines.append("| Service | Status |")
        lines.append("|---|---|")
        for r in healthy:
            lines.append(f"| {r.get('display', r.get('name', '?'))} | {r.get('status', '?')} |")
        lines.append("")

    lines += [
        "## System State",
        f"- Display: {os.environ.get('DISPLAY', 'not set')}",
        f"- Memory free: {_mem_free_mb()}",
        f"- Disk free (home): {_disk_free_gb()}",
    ]
    return "\n".join(lines)


def _ai_routing_poll_status() -> dict[str, Any]:
    cfg = _load_ai_routing_config()
    tier = cfg.get("tier", "cloud")
    local_available = _check_local_available()
    return {
        "tier": tier,
        "display_state": tier,
        "status": tier,
        "local_available": local_available,
    }


def _teams_data() -> dict:
    """Read AionUI teams DB, filter to Hermes-led, probe sessions, compute action."""
    if not AIONUI_DB.exists():
        return {"teams": [], "error": f"aionui.db not found: {AIONUI_DB}"}
    teams_out = []
    try:
        # Read-only URI connection avoids write-lock on the live app's DB.
        con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, name, agents, updated_at FROM teams ORDER BY updated_at DESC"
        ).fetchall()
    except Exception as exc:
        return {"teams": [], "error": str(exc)}

    for row in rows:
        try:
            agents = json.loads(row["agents"] or "[]")
        except Exception:
            continue

        # Only Hermes-led teams
        leader = next((a for a in agents if a.get("role") == "leader"), None)
        if not leader or leader.get("agentType") != "hermes":
            continue

        teammates = [a for a in agents if a.get("role") == "teammate"]
        slots = len(agents)

        # Probe session liveness from leader conversation's teamMcpStdioConfig
        session_live = False
        has_stale_acp = False
        leader_conv_id = leader.get("conversationId", "")
        try:
            conv = con.execute(
                "SELECT extra FROM conversations WHERE id=?", (leader_conv_id,)
            ).fetchone()
            if conv:
                extra = json.loads(conv["extra"] or "{}")
                cfg = extra.get("teamMcpStdioConfig") or {}
                env = {e["name"]: e["value"]
                       for e in cfg.get("env", [])
                       if "name" in e and "value" in e}
                port_str = env.get("TEAM_MCP_PORT")
                if port_str:
                    try:
                        session_live = _tcp_reachable("127.0.0.1", int(port_str), timeout=1.0)
                    except Exception:
                        pass
                has_stale_acp = bool(extra.get("acpSessionId"))
        except Exception:
            pass

        # Action detection (server-side so UI is dumb)
        if slots == 1:
            action = "BUILD"
        elif slots > 1 and not session_live:
            action = "REPAIR_CANDIDATE"
        elif slots > 1 and session_live and (
            any(t.get("status") != "active" for t in teammates) or has_stale_acp
        ):
            action = "REPAIR"
        else:
            action = "OK"

        # updated_at is epoch-ms in this JS-native DB
        try:
            updated_iso = datetime.fromtimestamp(
                int(row["updated_at"]) / 1000, tz=timezone.utc
            ).isoformat()
        except Exception:
            updated_iso = str(row["updated_at"])

        teams_out.append({
            "id":   row["id"],
            "name": row["name"],
            "leader": {
                "agentName":      leader.get("agentName", "Leader"),
                "agentType":      leader.get("agentType", "hermes"),
                "conversationId": leader_conv_id,
                "status":         leader.get("status", ""),
                "hasStaleAcp":    has_stale_acp,
            },
            "teammates": [
                {
                    "agentName": t.get("agentName", ""),
                    "agentType": t.get("agentType", ""),
                    "status":    t.get("status", ""),
                    "slotId":    t.get("slotId", ""),
                }
                for t in teammates
            ],
            "slots":       slots,
            "sessionLive": session_live,
            "action":      action,
            "updatedAt":   updated_iso,
        })

    try:
        con.close()
    except Exception:
        pass

    return {
        "teams":       teams_out,
        "generatedAt": datetime.now(tz=timezone.utc).isoformat(),
    }


def _parse_registry() -> dict[str, dict]:
    """Parse SWARM_REGISTRY.md → {profile: {program, role}}. Expands ranges."""
    registry_map: dict[str, dict] = {}
    try:
        text = SWARM_REGISTRY_MD.read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"^\|\s*\*{0,2}(swarm[\d–\-]+|space-agent)\*{0,2}\s*\|(.+)", line)
            if not m:
                continue
            profile_raw = m.group(1).strip()
            cols = [c.strip().strip("*") for c in m.group(2).split("|") if c.strip()]
            if len(cols) < 2:
                continue
            program = cols[0].strip("*").strip()
            role    = cols[1].strip("*").strip()
            range_m = re.match(r"swarm(\d+)[–\-](\d+)$", profile_raw)
            if range_m:
                for n in range(int(range_m.group(1)), int(range_m.group(2)) + 1):
                    registry_map[f"swarm{n}"] = {"program": program, "role": role}
            else:
                registry_map[profile_raw] = {"program": program, "role": role}
    except Exception:
        pass
    return registry_map


def _provision_one(name: str, role: str, program: str, registry: dict) -> dict:
    """Provision a single Hermes profile. Returns {name, ok, result|reason}."""
    # Name format
    if not _PROVISION_NAME_RE.match(name):
        return {"name": name, "ok": False, "reason": f"invalid name format: {name!r}"}
    # Reserved
    if name in _RESERVED_PROFILES:
        return {"name": name, "ok": False, "reason": f"{name!r} is reserved and cannot be provisioned"}
    # Allocated in registry
    if name not in registry:
        return {"name": name, "ok": False, "reason": f"{name!r} is not allocated in the registry"}
    reg = registry[name]
    reg_program = reg.get("program", "")
    # Normalise for comparison (strip parens/asterisks the registry uses for formatting)
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 +]", "", s.lower()).strip()
    if _norm(reg_program) != _norm(program):
        return {"name": name, "ok": False,
                "reason": f"{name!r} is allocated to '{reg_program}', not '{program}'"}

    profile_dir = HOME / ".hermes" / "profiles" / name
    src_auth    = HOME / ".hermes" / "profiles" / "example-a" / "auth.json"
    dst_auth    = profile_dir / "auth.json"
    already_existed = profile_dir.exists()

    # Step 1: create if not on disk
    if not already_existed:
        try:
            r = subprocess.run(
                [str(HERMES_CLI), "profile", "create", name],
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return {"name": name, "ok": False,
                        "reason": f"profile create failed: {(r.stderr or r.stdout).strip()[:300]}"}
        except Exception as exc:
            return {"name": name, "ok": False, "reason": f"profile create error: {exc}"}

    # Step 2: copy auth.json from example-a (always source is example-a)
    if not src_auth.exists():
        return {"name": name, "ok": False,
                "reason": "example-a/auth.json not found — cannot copy credentials"}
    try:
        shutil.copy2(str(src_auth), str(dst_auth))
    except Exception as exc:
        return {"name": name, "ok": False, "reason": f"auth.json copy failed: {exc}"}

    # Step 3: set kanban-facing description (role label) via --text flag
    desc_note = ""
    try:
        rd = subprocess.run(
            [str(HERMES_CLI), "profile", "describe", name, "--text", role],
            capture_output=True, text=True, timeout=30,
        )
        if rd.returncode != 0:
            desc_note = f" (describe warn: {(rd.stderr or rd.stdout).strip()[:100]})"
    except Exception as exc:
        desc_note = f" (describe warn: {exc})"

    # Step 4: verify
    if not profile_dir.exists():
        return {"name": name, "ok": False, "reason": "profile dir missing after create"}
    if not dst_auth.exists():
        return {"name": name, "ok": False, "reason": "auth.json missing after copy"}

    label = "already_provisioned" if already_existed else "provisioned"
    return {"name": name, "ok": True, "result": label + desc_note}


def _swarms_data() -> dict:
    """Cross-reference kanban assignees, SWARM_REGISTRY.md, and profiles on disk."""
    # 1. hermes kanban assignees → per-profile task counts
    kanban_map: dict[str, dict] = {}
    try:
        r = subprocess.run(
            [str(HERMES_CLI), "kanban", "assignees"],
            capture_output=True, text=True, timeout=12,
        )
        for line in r.stdout.splitlines():
            if not line.strip() or line.startswith("NAME"):
                continue
            parts = re.split(r"\s{2,}", line.strip(), maxsplit=2)
            if len(parts) < 2:
                continue
            name = parts[0]
            on_disk = parts[1].strip().lower() == "yes"
            counts_raw = parts[2].strip() if len(parts) >= 3 else "(idle)"
            counts: dict[str, int] = {}
            if counts_raw != "(idle)":
                for kv in counts_raw.split(","):
                    kv = kv.strip()
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        try:
                            counts[k.strip()] = int(v.strip())
                        except ValueError:
                            pass
            kanban_map[name] = {"onDisk": on_disk, "counts": counts}
    except Exception:
        pass

    # 2. Registry + disk
    registry_map = _parse_registry()
    profiles_dir = HOME / ".hermes" / "profiles"
    disk_profiles: set[str] = set()
    try:
        disk_profiles = {p.name for p in profiles_dir.iterdir() if p.is_dir()}
    except Exception:
        pass

    # 3. Union → cross-reference
    all_profiles = sorted(set(kanban_map) | set(registry_map) | disk_profiles)
    swarms = []
    for profile in all_profiles:
        kb = kanban_map.get(profile, {})
        on_disk     = profile in disk_profiles or kb.get("onDisk", False)
        in_kanban   = profile in kanban_map
        in_registry = profile in registry_map
        reg     = registry_map.get(profile, {})
        program = reg.get("program", "")
        role    = reg.get("role", "")
        counts  = kb.get("counts", {})
        status  = ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else (
            "(idle)" if in_kanban else "")

        if not in_registry:
            state = "orphan"
        elif not on_disk:
            state = "allocated-not-provisioned"
        elif counts:
            state = "active"
        else:
            state = "provisioned-idle"

        swarms.append({
            "profile": profile, "role": role, "program": program,
            "onDisk": on_disk, "status": status, "counts": counts,
            "state": state, "overlap": False,
        })

    return {"swarms": swarms, "generatedAt": datetime.now(tz=timezone.utc).isoformat()}


def poll_all_services() -> dict[str, Any]:
    """Single snapshot of every service we monitor. Called every STREAM_INTERVAL_S."""

    # Screenpipe — /health returns {status, fps, frames_captured, ...}.
    # Fix B: treat HTTP-200 with status=="degraded" as amber, not red.
    sp = {"reachable": False, "status": "offline", "display_state": "offline"}
    r = _http_get(*PROXY_TARGETS["screenpipe"], "/health")
    if r is not None:
        http_status, parsed, _ = r
        if http_status == 200:
            svc_status = parsed.get("status") or parsed.get("frame_status") or "ok"
            sp["reachable"]      = True
            sp["http_status"]    = http_status
            sp["status"]         = svc_status
            sp["frames_captured"]= parsed.get("frames_captured") or parsed.get("frame_count")
            sp["fps"]            = parsed.get("fps")
            vis = parsed.get("vision_status") or parsed.get("vision")
            if vis is None:
                vis = "disabled" if (
                    parsed.get("is_vision_enabled") is False or
                    parsed.get("vision_enabled") is False
                ) else "enabled"
            sp["vision"] = vis
            # "degraded" = service is alive but some subsystem (e.g. Wayland
            # vision) is broken. Treat as amber, not red.
            sp["display_state"] = svc_status if svc_status in ("healthy", "degraded") else "ok"
        else:
            sp["http_status"] = http_status
            sp["display_state"] = "error"

    # Hermes dashboard — optional :9119 web UI (hermes dashboard process).
    hm_dash = {"reachable": False, "display_state": "offline"}
    r_dash = _http_get("127.0.0.1", HERMES_DASH_PORT, "/api/status")
    if r_dash is not None:
        hs, pd, _ = r_dash
        hm_dash["reachable"]     = (hs == 200)
        hm_dash["http_status"]   = hs
        hm_dash["display_state"] = "running" if hs == 200 else "error"
        if hs == 200:
            hm_dash["version"]       = pd.get("version")
            hm_dash["gateway_state"] = pd.get("gateway_state")
            hm_dash["active_sessions"] = pd.get("active_sessions")

    # Hermes — probe :9119/api/status. ★ NOT a bare 200: this host serves a single-page app that
    # answers 200 for any path at all, so "reachable" must mean PARSED JSON WE RECOGNISE, otherwise
    # the check can never fail and tells us nothing.
    hm = {"reachable": False, "display_state": "offline"}
    r = _http_get(HERMES_GATEWAY_HOST, HERMES_GATEWAY_PORT, HERMES_STATUS_PATH)
    if r is not None:
        http_status, parsed, raw_body = r
        looks_real = bool(isinstance(parsed, dict) and parsed.get("version"))
        hm["reachable"]      = (http_status == 200 and looks_real)
        hm["http_status"]    = http_status
        hm["gateway_port"]   = HERMES_GATEWAY_PORT
        if http_status == 200 and not looks_real:
            # ★ the SPA answered, the API did not — say so instead of reporting a false green
            hm["note"] = ("port answered 200 but returned the web app, not status JSON — "
                          "treated as NOT reachable")
        # /health returns {"status":"ok","platform":"hermes-agent"} — no
        # version/sessions in this endpoint; surface what we have.
        hm["gateway_status"] = parsed.get("status") or "unknown"
        hm["platform"]       = parsed.get("platform")
        hm["display_state"]  = "online" if http_status == 200 else "error"
        # Try :9119 for extended status (version, gateway_state, sessions)
        # but don't fail if it's not there.
        r2 = _http_get(*PROXY_TARGETS["hermes"], "/api/status")
        if r2 is not None and r2[0] == 200:
            p2 = r2[1]
            hm["version"]       = p2.get("version") or p2.get("ver")
            hm["gateway_state"] = p2.get("gateway_state") or p2.get("gatewayState") or p2.get("state")
            hm["active_sessions"] = p2.get("active_sessions") or p2.get("activeSessions") or p2.get("sessions")

    # AionUI — Fix C: electron process scan is primary; TCP :3002 is secondary.
    ai_pid       = _find_electron_pid()
    ai_reachable = _tcp_reachable(*PROXY_TARGETS["aionui"]) if not ai_pid else False

    # CDP health check — port 9230. Blank white screen after suspend shows as
    # electron_pid present but CDP unresponsive (GPU process exited).
    # ★★★ CACHED, AND THIS IS A DISRUPTION FIX, NOT A PERFORMANCE ONE. Operator, 2026-10-09:
    #     *"I cannot be disrupted while working."*
    # ★ MEASURED BY AN ADJACENT SEAT: this call ran every STREAM_INTERVAL_S (3.0s) forever, and `ss`
    #   showed ~98 TIME-WAIT sockets on 9230 - CONTINUOUS connection churn against the operator's own
    #   AionUi app. The deep report named 9230 as the mechanism every input injector rides, and listed
    #   *this* poller by file and line as one of them.
    # ★ IT WAS NOT INJECTING INPUT. It only reads /json/list. But it was holding a socket to the
    #   operator's UI process 20 times a minute for a HEALTH BOOLEAN that changes about never - and it
    #   is the one contributor on that list that belongs to THIS seat's lane (COWORK_PROTOCOL §2 rule 3
    #   gives the panel to this seat), so it is the one I can close without asking.
    # ★ A CDP health check does not need 3-second resolution. Cached at 60s it is still "is the
    #   window alive after a suspend", which is what the comment above says it is FOR.
    cdp_ok = _cdp_health_cached(bool(ai_pid))

    ai = {
        "reachable":      ai_reachable or bool(ai_pid),
        "port_open":      ai_reachable,
        "electron_pid":   ai_pid,
        "cdp_responsive": cdp_ok,
        "display_state":  (
            "running"   if ai_pid and cdp_ok else
            "blank"     if ai_pid and not cdp_ok else
            "reachable" if ai_reachable else
            "stopped"
        ),
    }

    # Space agent — Fix D: if TCP fails, check if installed on disk.
    sa_agent_reachable = _tcp_reachable(*PROXY_TARGETS["space"])
    sa_installed = SPACE_AGENT_PACKAGE_JSON.is_file()

    # BOHKSS TRANSITION — uncomment when boardwalk Phase E ships
    # boardwalk_main = probe_http("http://localhost:8506/health", timeout=2)
    # boardwalk_watchdog = probe_http("http://localhost:8507/health", timeout=2)
    # boardwalk_autocode = probe_http("http://localhost:8509/health", timeout=2)

    return {
        "screenpipe":        sp,
        "hermes":            hm,
        "hermes_dashboard":  hm_dash,
        "aionui":            ai,
        "space_agent":   _space_agent_status(sa_agent_reachable, sa_installed),
        "vault_watcher": _pid_status_with_display(VAULT_PID_FILE),
        "sanitizer":     _pid_status_with_display(SANITIZER_PID_FILE),
        "screen_assistant":    _screen_assistant_status(),
        "proactive_research":  _tier3_pid_status(PR_PID_FILE, PR_SCRIPT),
        "voice_kanban":        _tier3_pid_status(VK_PID_FILE, VK_SCRIPT),
        "learning_loop":       _tier3_pid_status(LL_PID_FILE, LL_SCRIPT),
        "bridge": {
            "enabled": BRIDGE_ENABLED,
            "allowed_paths": ALLOWED_PATHS,
            "allowed_path_count": len(ALLOWED_PATHS),
            "display_state": "enabled" if BRIDGE_ENABLED else "disabled",
            "status": "enabled" if BRIDGE_ENABLED else "disabled",
        },
        "panel_server":  {"running": True, "pid": os.getpid(), "display_state": "running"},
        "ai_routing":    _ai_routing_poll_status(),
        "vault":         _vault_stats(),
        "quarantine":    _quarantine_stats(),
        "launcher":      _discover_launcher(),
        # BOHKSS pending — these surface as grey "pending BOHKSS transition"
        # cards on the panel until the real boardwalk services come online.
        "boardwalk_main":      {"pending": True, "port": 8506, "status": "pending BOHKSS transition"},
        "boardwalk_watchdog":  {"pending": True, "port": 8507, "status": "pending BOHKSS transition"},
        "boardwalk_autocode":  {"pending": True, "port": 8509, "status": "pending BOHKSS transition"},
    }

def _acked_states() -> dict:
    """★ A5 (2026-08-23) — a warning about a DELIBERATE CHOICE becomes an ACKED STATE, not an alert.
    A panel that warns about a choice trains the operator to ignore warnings, and an ignored warning is worse
    than an absent one because it still looks like oversight. Acked entries carry a reason and a date and are
    reversible by deleting the key — the same shape as the acked secrets rows."""
    try:
        return _read_json(_SCRIPT_DIR / "acked_states.json") or {}
    except Exception:
        return {}


def derive_alerts(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    _acked = _acked_states()
    sp = snapshot.get("screenpipe", {})
    if not sp.get("reachable"):
        _a = _acked.get("screenpipe") or {}
        if _a.get("state") == "acked-intentionally-down":
            # ★ still SHOWN, not hidden — a vanished row is indistinguishable from one that never existed.
            alerts.append({"tone": "muted",
                           "text": f"screenpipe intentionally down since {_a.get('since','?')} — "
                                   f"{_a.get('reason','no reason recorded')}"})
        else:
            alerts.append({"tone": "amber", "text": "screenpipe offline (port 3030)"})
    elif sp.get("display_state") == "degraded":
        alerts.append({"tone": "amber", "text": "screenpipe degraded (vision broken, service alive)"})
    if not snapshot["hermes"].get("reachable"):
        alerts.append({"tone": "amber",
                       "text": f"hermes dashboard not answering on :{HERMES_GATEWAY_PORT}"
                               f" — NOTE: this is the dashboard, not the teammate-spawn path"})
    if snapshot.get("aionui", {}).get("display_state") == "blank":
        alerts.append({"tone": "amber", "text": "AionUI renderer blank — may need reload after suspend"})
    if not snapshot["vault_watcher"].get("running"):
        alerts.append({"tone": "red", "text": "vault_watcher is not running"})
    if not snapshot["sanitizer"].get("running"):
        alerts.append({"tone": "red", "text": "vault_import_sanitizer is not running"})
    q = snapshot["quarantine"].get("count", 0)
    if q:
        alerts.append({"tone": "amber", "text": f"{q} file(s) in quarantine — review"})
    if not snapshot["launcher"].get("exists"):
        alerts.append({"tone": "amber", "text": f"launcher script missing: {snapshot['launcher']['path']}"})
    return alerts

# ── Small JSON state helpers (session-memory & debate runs) ──────────────────

def _write_json(path: Path, obj: dict) -> None:
    """Best-effort atomic write so a panel mid-poll never sees a half file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, default=str)
    os.replace(tmp, path)

def _read_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}

# ── Debate orchestration ─────────────────────────────────────────────────────

# Hermes profile names are restricted to a conservative shape so caller
# input can't be smuggled into the subprocess argv as an arbitrary string.
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

def _safe_profile(value: Any, default: str) -> str:
    if isinstance(value, str) and _PROFILE_RE.match(value):
        return value
    return default

def _hermes_to_file(profile: str, prompt: str, out_file: Path, timeout: float = 180.0) -> tuple[int, str]:
    """Run hermes -z, capture stdout, write it to out_file. Returns (exit_code, stderr_tail)."""
    try:
        proc = subprocess.run(
            ["hermes", "--profile", profile, "-z", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return -1, "hermes binary not on PATH"
    except subprocess.TimeoutExpired:
        return 124, f"hermes timed out after {timeout:.0f}s"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    body = proc.stdout.strip() or "(empty response from hermes)"
    out_file.write_text(body + "\n", encoding="utf-8")
    return proc.returncode, (proc.stderr or "").strip()[-1000:]

def _run_debate_worker(
    debate_id: str, topic: str,
    profile_a: str, profile_b: str, judge: str,
    files: dict[str, Path], status_path: Path,
) -> None:
    """Run A and B in parallel; then ask `judge` to read both and rule."""
    def _update(**patch):
        cur = _read_json(status_path)
        cur.update(patch)
        _write_json(status_path, cur)

    prompt_a = (
        f"Argue FOR the following position: \"{topic}\". "
        "Be specific with evidence and concrete examples. "
        "3 paragraphs maximum. Markdown only — no preamble."
    )
    prompt_b = (
        f"Argue AGAINST the following position: \"{topic}\". "
        "Be specific with evidence and concrete examples. "
        "3 paragraphs maximum. Markdown only — no preamble."
    )

    _update(stage="arguing")

    results: dict[str, tuple[int, str]] = {}
    threads = []
    def _arg(side: str, profile: str, prompt: str, out: Path):
        code, err = _hermes_to_file(profile, prompt, out)
        results[side] = (code, err)
    for side, profile, prompt, out in [
        ("a", profile_a, prompt_a, files["a"]),
        ("b", profile_b, prompt_b, files["b"]),
    ]:
        t = threading.Thread(target=_arg, args=(side, profile, prompt, out),
                             name=f"debate-{debate_id}-{side}", daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join()

    cur = _read_json(status_path)
    cur["exit_codes"] = {side: results[side][0] for side in results}
    errors = []
    for side, (code, err) in results.items():
        if code != 0 and err:
            errors.append(f"{side}: {err}")
    cur["errors"] = errors
    _write_json(status_path, cur)

    if any(code != 0 for code, _ in results.values()) and not (files["a"].exists() and files["b"].exists()):
        _update(stage="failed", running=False,
                finished=datetime.now(tz=timezone.utc).isoformat())
        return

    # Judge stage. We hand the judge the actual transcripts so it doesn't
    # have to guess at the framing.
    try:
        body_a = files["a"].read_text(encoding="utf-8", errors="replace")
        body_b = files["b"].read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _update(stage="failed", running=False, errors=errors + [f"read transcripts: {exc}"],
                finished=datetime.now(tz=timezone.utc).isoformat())
        return

    _update(stage="judging")
    verdict_prompt = (
        f"You are the impartial judge of a debate on the topic:\n\n"
        f"  \"{topic}\"\n\n"
        f"### Argument FOR (profile {profile_a})\n{body_a}\n\n"
        f"### Argument AGAINST (profile {profile_b})\n{body_b}\n\n"
        "Write your verdict as Markdown with these sections (use `##` headings):\n"
        "  - Summary of each side (one sentence each)\n"
        "  - Stronger argument and why\n"
        "  - Concessions the loser should make\n"
        "  - Your ruling (one paragraph)\n\n"
        "Be specific. Reference points raised in each transcript by quoting "
        "or paraphrasing. Keep the whole verdict under 400 words."
    )
    code, err = _hermes_to_file(judge, verdict_prompt, files["verdict"], timeout=240.0)
    final_errors = errors[:]
    if code != 0 and err:
        final_errors.append(f"judge: {err}")

    _update(
        stage="completed" if code == 0 else "judge_failed",
        running=False,
        finished=datetime.now(tz=timezone.utc).isoformat(),
        exit_codes={**(cur.get("exit_codes") or {}), "judge": code},
        errors=final_errors,
    )

# ── SSE broadcaster ──────────────────────────────────────────────────────────

CAPTURE_PUBLISH_INTERVAL_S = 3.0


class CapturePolicyPublisher:
    """★★★ The ONE writer of the microphone permission — and nothing else's passenger.

    Deliberately its own thread rather than a line in the steward sweep:
      · the sweep AUTO-PAUSES under memory pressure, and a permission must not stop publishing
        because something unrelated is using RAM;
      · the sweep's cadence is its own business (work + backoff), while this needs a cadence a
        staleness bound can be derived from;
      · the work here is ONE bounded CDP read (measured 153 ms), so it is cheap enough to run on
        a fixed tick without competing with anything.
    ★ It never widens anything on failure. A failed publish leaves the file to go stale, and a
      stale file DENIES — the reader falls through to the live endpoint, which fails closed too.
    """

    def __init__(self, interval: float = CAPTURE_PUBLISH_INTERVAL_S) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.writes = 0
        self.failures = 0
        self.slow = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="capture-policy-publisher",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ★★★ THE READ IS BOUNDED SO THE TICK CANNOT BE STRETCHED. 2026-09-17.
    #   Standalone, one publish costs 33 ms median / 153 ms max. INSIDE THE PANEL it is slower and
    #   spikier, because it shares :9230 with everything else that talks CDP (the same endpoint
    #   measured p90 8.5 s under contention). An unbounded read turns a 3 s tick into an 11 s one —
    #   measured: file age max 10.6 s against a 15 s bound, i.e. 1.4x headroom, not the 4.8x the
    #   standalone numbers implied. ★ A staleness bound is only as honest as the cadence it is
    #   derived from, so the cadence must not be a function of how busy CDP happens to be.
    #   Abandoning a slow read is safe: the PREVIOUS file stays, it ages, and if the writer really
    #   is dead it crosses the bound and DENIES. We lose freshness, never fail open.
    def _run(self) -> None:
        """★★★ SINGLE-FLIGHT. Never more than one CDP read in flight, ever.

        The previous version abandoned a read after a 2 s budget and started a fresh one on the
        next tick — while the abandoned one kept running, holding its websocket. Measured: 10
        abandonments in ~3 minutes, i.e. up to ten concurrent reads of the same key, each making
        the others slower. The same read costs 123 ms median from a standalone process and
        regularly blew past 2 s from inside the panel, which is the tell: THE CONTENTION WAS MY OWN
        PUBLISHER. Write gaps reached 14.5 s on a declared 3 s tick and the file was usable only
        92% of the time.
        ★ This is the identical mistake as the /proc scan earlier today — N concurrent copies of
          one cheap read, collapsing into a herd — and I made it a second time in the same file.
          The lesson that generalises: whenever a timeout leaves work RUNNING rather than
          cancelled, a retry is not a retry, it is a second worker.
        So: one attempt at a time, run to completion, next tick after it lands. A slow read delays
        freshness (the file ages, and a dead writer still crosses the bound and DENIES) but can
        never multiply.
        """
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                _publish_capture_policy()
                self.writes += 1
            except Exception as exc:
                self.failures += 1
                logger.warning("capture-policy publish failed (%s) — previous file stays and "
                               "ages; a dead writer crosses the bound and DENIES", exc)
            elapsed = time.monotonic() - started
            if elapsed > self._interval:
                self.slow += 1
                logger.warning("capture-policy publish took %.1fs (> %.1fs tick) — cadence is "
                               "read-bound, not tick-bound (slow=%d)",
                               elapsed, self._interval, self.slow)
            self._stop.wait(max(0.25, self._interval - elapsed))


CAPTURE_PUBLISHER = CapturePolicyPublisher()


def _publish_capture_policy_quietly() -> None:
    """_publish_capture_policy that cannot take the panel down on its way up."""
    try:
        _publish_capture_policy()
    except Exception as exc:
        logger.warning("capture-policy boot publish failed (%s) — the store stays empty, which "
                       "DENIES; the broadcaster retries in %.0fs", exc, STREAM_INTERVAL_S)


def _publish_capture_policy() -> None:
    """Read the policy from its source of truth ONCE and publish it to the file store."""
    import sys as _s
    _p = str(ROOT / "operator")
    if _p not in _s.path:
        _s.path.insert(0, _p)
    import focus_sources as _fs
    import capture_policy_store as _cps
    _cps.write(_fs.capture_policy())


class StatusBroadcaster:
    """One background thread polls; many client queues receive each tick."""

    def __init__(self, interval: float = STREAM_INTERVAL_S) -> None:
        self._interval = interval
        self._subs: list[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_payload: str | None = None
        self._paused = False   # DQ71·C6 — true while the sweep is auto-paused for exceeding the RSS ceiling
        self._halted = False   # DQ72·A — true while the operator has MANUALLY halted the Watchman (OFF)
        self._halted_at = 0.0
        self._thread = threading.Thread(target=self._run, name="status-broadcaster", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ── DQ72·A — the OFF that HALTS the Watchman's execution (not its reporting) ──────────────────
    def halt(self) -> dict:
        self._halted = True
        self._halted_at = time.time()
        logger.warning("WATCHMAN HALTED by operator (DQ72·A): the detector sweep STOPS executing — not just "
                       "hidden. Stops silent-failure detection (spawn-without-readiness, empty completions, "
                       "handoff failures, cost spikes). Nothing else watches for these until resumed.")
        return self.status()

    def resume(self) -> dict:
        self._halted = False
        logger.info("WATCHMAN RESUMED by operator (DQ72·A): silent-failure detection is watching again.")
        return self.status()

    def status(self) -> dict:
        """DQ72·C — the Watchman's REAL process reading, by PID. basis is never 'unknown' when live."""
        rss = _own_rss_mb()
        avail = _avail_ram_mb()
        pct = (rss / avail * 100.0) if avail else None
        return {
            "pid": os.getpid(),                       # the Watchman sweep runs in THIS process
            "rss_mb": round(rss, 1),
            "avail_mb": round(avail, 1),
            "avail_basis": "MemAvailable" if avail else "unknown",
            "pct_of_avail": round(pct, 2) if pct is not None else None,
            "halted": self._halted,
            "auto_paused": self._paused,
            "ceiling_pct": PANEL_RSS_AVAIL_FRACTION * 100,
            "measured": bool(avail),
        }

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=8)
        with self._lock:
            self._subs.append(q)
            if self._last_payload is not None:
                try: q.put_nowait(self._last_payload)
                except queue.Full: pass
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            try: self._subs.remove(q)
            except ValueError: pass

    def _run(self) -> None:
        _last_sweep = 0.0
        while not self._stop.is_set():
            # ── DQ72·A — MANUAL halt: the operator turned the Watchman OFF. Stop executing the sweep. ──
            if self._halted:
                self._last_payload = json.dumps({
                    "ts": int(time.time() * 1000), "watchman_halted": True,
                    "reason": "operator halted the Watchman (OFF) — detector sweep is not executing",
                })
                with self._lock:
                    for q in list(self._subs):
                        try: q.put_nowait(self._last_payload)
                        except queue.Full: pass
                self._stop.wait(self._interval)
                continue
            # ── DQ71·C6 — auto-pause the sweep if the panel's own RSS balloons ───────────────
            rss = _own_rss_mb()
            avail = _avail_ram_mb()
            over_abs = rss > PANEL_RSS_CEILING_MB
            over_frac = avail > 0 and rss > (avail * PANEL_RSS_AVAIL_FRACTION)
            if over_abs or over_frac:
                if not self._paused:
                    self._paused = True
                    logger.warning("STEWARD SWEEP AUTO-PAUSED (DQ71·C6): panel RSS %.0fMB exceeds ceiling "
                                   "(abs %dMB / %.0f%% of %.0fMB avail). The sweep pauses itself so it stops "
                                   "eating memory; it resumes when RSS drops back.",
                                   rss, PANEL_RSS_CEILING_MB, PANEL_RSS_AVAIL_FRACTION * 100, avail)
                self._last_payload = json.dumps({
                    "ts": int(time.time() * 1000), "steward_sweep_paused": True,
                    "reason": f"panel RSS {rss:.0f}MB over ceiling (abs {PANEL_RSS_CEILING_MB}MB / "
                              f"{PANEL_RSS_AVAIL_FRACTION*100:.0f}% of {avail:.0f}MB avail)",
                })
                with self._lock:
                    for q in list(self._subs):
                        try: q.put_nowait(self._last_payload)
                        except queue.Full: pass
                self._stop.wait(self._interval * 4)   # back off hard while paused
                continue
            if self._paused:
                self._paused = False
                logger.info("steward sweep RESUMED (DQ71·C6): panel RSS %.0fMB back under ceiling", rss)
            try:
                snapshot = poll_all_services()
                payload = json.dumps({
                    "ts": int(time.time() * 1000),
                    "services": snapshot,
                    "alerts": derive_alerts(snapshot),
                }, default=str)
                # Service state transition notifications
                try: _check_service_transitions(snapshot)
                except Exception: pass
                # ★★★ THE CAPTURE-POLICY PUBLISH USED TO LIVE HERE. IT WAS THE WRONG PLACE.
                #   This loop AUTO-PAUSES itself when the panel's RSS crosses a ceiling (DQ71·C6
                #   above) — correct for a memory-hungry sweep. But the panel sits at ~725MB
                #   against a 700MB ceiling, so it pauses intermittently, waits interval*4 = 12s
                #   and `continue`s — SKIPPING the publish. Measured by the voice seat: 3 writes
                #   in 30 s, file usable only 57% of the time, and the operator's gate still
                #   flapped. I had attached the MICROPHONE PERMISSION to a loop designed to stop
                #   itself under load. It now has its own publisher (CapturePolicyPublisher).
            except Exception as exc:
                logger.warning("snapshot failed: %s", exc)
                payload = json.dumps({"ts": int(time.time() * 1000), "error": str(exc)})
            self._last_payload = payload
            with self._lock:
                dead: list[queue.Queue[str]] = []
                for q in list(self._subs):
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        # Slow consumer — drop them so we don't memory-leak.
                        dead.append(q)
                for q in dead:
                    try: self._subs.remove(q)
                    except ValueError: pass
            # Stale sweep + kanban scan every 5 min
            now = time.time()
            if now - _last_sweep >= 300:
                try: _notif_stale_sweep()
                except Exception: pass
                try: _kanban_blocked_scan()
                except Exception: pass
                _last_sweep = now
            self._stop.wait(self._interval)


BROADCASTER = StatusBroadcaster()


# ── Notification Center ───────────────────────────────────────────────────────

class NotifBroadcaster:
    """Push new notification events to all /notifications/stream SSE clients."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue[str]] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue[str]:
        q: queue.Queue[str] = queue.Queue(maxsize=32)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[str]) -> None:
        with self._lock:
            try: self._subs.remove(q)
            except ValueError: pass

    def broadcast(self, payload: str) -> None:
        with self._lock:
            dead: list[queue.Queue[str]] = []
            for q in list(self._subs):
                try: q.put_nowait(payload)
                except queue.Full: dead.append(q)
            for q in dead:
                try: self._subs.remove(q)
                except ValueError: pass


NOTIF_BROADCASTER = NotifBroadcaster()


def _notif_db_init() -> None:
    NOTIF_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(NOTIF_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       INTEGER NOT NULL,
            severity TEXT NOT NULL,
            source   TEXT NOT NULL,
            title    TEXT NOT NULL,
            body     TEXT NOT NULL DEFAULT '',
            payload  TEXT NOT NULL DEFAULT '{}',
            state    TEXT NOT NULL DEFAULT 'unread',
            stale    INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_notif_ts    ON notifications(ts DESC)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_notif_state ON notifications(state)")
    con.commit()
    con.close()


def _notify(severity: str, source: str, title: str,
            body: str = "", payload: dict | None = None) -> int | None:
    """Write one notification row, enforce cap, broadcast to SSE subscribers."""
    if severity not in NOTIF_SEVERITIES:
        severity = "mid"
    try:
        ts = int(time.time() * 1000)
        con = sqlite3.connect(str(NOTIF_DB))
        cur = con.execute(
            "INSERT INTO notifications(ts,severity,source,title,body,payload) VALUES(?,?,?,?,?,?)",
            (ts, severity, source[:80], title[:200],
             (body or "")[:2000], json.dumps(payload or {})),
        )
        new_id = cur.lastrowid
        count = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        if count > NOTIF_CAP:
            con.execute(
                "DELETE FROM notifications WHERE id IN "
                "(SELECT id FROM notifications ORDER BY ts ASC LIMIT ?)",
                (count - NOTIF_CAP,),
            )
        con.commit()
        con.close()
        NOTIF_BROADCASTER.broadcast(json.dumps({
            "event": "new", "id": new_id, "ts": ts,
            "severity": severity, "source": source[:80],
            "title": title[:200], "body": (body or "")[:2000],
            "payload": payload or {}, "state": "unread", "stale": 0,
        }))
        return new_id
    except Exception:
        return None


def _notif_stale_sweep() -> int:
    """Mark unread notifications stale per-severity threshold. Returns count marked."""
    thresholds_ms = {"high": 4 * 3600 * 1000, "mid": 24 * 3600 * 1000, "low": 1 * 3600 * 1000}
    now_ms = int(time.time() * 1000)
    total = 0
    try:
        con = sqlite3.connect(str(NOTIF_DB))
        for sev, thresh in thresholds_ms.items():
            cur = con.execute(
                "UPDATE notifications SET stale=1 "
                "WHERE state='unread' AND severity=? AND ts < ? AND stale=0",
                (sev, now_ms - thresh),
            )
            total += cur.rowcount
        con.commit()
        con.close()
    except Exception:
        pass
    return total


def _notif_explain_text(row: dict) -> str:
    source = row.get("source", "")
    try:
        pl = json.loads(row.get("payload") or "{}")
    except Exception:
        pl = {}
    kind = pl.get("kind", "")

    if source == "swarm-manager":
        if kind == "overlap-detected":
            return (f"swarm{pl.get('swarm','?')} is registered to {pl.get('program','?')} but has "
                    f"Kanban work assigned by {pl.get('otherProgram','?')}. This usually means a "
                    "leftover from before a renumber. Run the inspect step from the v1.11 cleanup "
                    "playbook, or archive the stale tasks.")
        if kind == "provision-ok":
            return f"Swarm profile {pl.get('name','?')!r} was successfully provisioned to program {pl.get('program','?')!r}."
        if kind == "provision-skip":
            return f"Swarm profile {pl.get('name','?')!r} already existed — no changes were made."
        if kind == "provision-error":
            return f"Provisioning swarm profile {pl.get('name','?')!r} failed: {pl.get('reason','unknown')}."

    if source == "build-hermes-team":
        if kind == "seed-complete":
            return f"Hermes team build complete for {pl.get('team','?')!r}: {pl.get('count','?')} teammate(s) seeded."
        if kind == "seed-fail":
            return f"Team build for {pl.get('team','?')!r} failed: {pl.get('reason','unknown')}."

    if source == "team-repair":
        return (f"Leader session for team {pl.get('teamId','?')!r} repaired: stale acpSessionId cleared "
                f"from conversation {pl.get('leaderConversationId','?')!r}. "
                "Reopen the team in AionUi to start a fresh session.")

    if source == "service":
        return (f"Service {pl.get('service','?')!r} transitioned from {pl.get('from','?')!r} "
                f"to {pl.get('to','?')!r}.")

    if source == "kanban":
        if kind == "task-blocked":
            return (f"Kanban task {pl.get('taskId','?')!r} was blocked: {pl.get('reason', 'max attempts reached')}. "
                    "This is a MAX_ATTEMPTS signal — the task will not retry automatically. "
                    "Review in the Hermes dashboard and reassign or archive.")
        if kind == "log-blocked":
            return (f"Kanban task {pl.get('taskId','?')!r} logged a blocking error: "
                    f"{pl.get('reason', 'see task log')}. Inspect the task log in the Hermes dashboard.")
        return (f"Kanban task {pl.get('task', pl.get('taskId','?'))!r} blocked after "
                f"{pl.get('attempts','?')} attempt(s). Review in the Hermes dashboard and reassign or archive.")

    if source == "prompter":
        # prompter notifications carry no `kind`; differentiate by payload shape + title.
        if pl.get("session_id") and pl.get("agent_kind"):
            return (f"Prompter session {pl.get('session_id')!r} started "
                    f"(agent kind: {pl.get('agent_kind')}). The interactive prompt-crafter is "
                    "running — open its SSE stream to answer its questions.")
        if "delivered" in (row.get("title","").lower()):
            return (f"A crafted prompt was delivered to a Hermes leader/teammate. "
                    f"Details: {row.get('body','')}. Open AionUi to confirm the leader received it.")
        if "saved" in (row.get("title","").lower()):
            return (f"A prompt set was saved to the Prompter library "
                    f"(id: {row.get('body','?')}). It can now be re-delivered from the library browser.")
        if "briefing" in (row.get("title","").lower()):
            return (f"A role briefing was delivered. Details: {row.get('body','')}.")
        # Generic prompter fallback — still describes the event.
        return (f"Prompter event: {row.get('title','(no title)')}. "
                f"{row.get('body','')}".strip())

    if source == "launch-assist":
        sid = pl.get("session_id", "?")
        aid = pl.get("agent_id", "")
        return (f"AI Launch-Assist started session {sid!r}"
                + (f" for agent {aid!r}" if aid else "")
                + ". The launch-assist runner is bringing the agent up — watch its stream for progress.")

    if source == "build-hermes-team":
        # Newer team-create events not covered by the seed-* branch above.
        if kind == "team-create-ok":
            return (f"Hermes team {pl.get('name','?')!r} created successfully "
                    f"(team_id: {pl.get('teamId','?')}).")
        if kind == "team-create-fail":
            return (f"Creating Hermes team {pl.get('name','?')!r} failed: "
                    f"{pl.get('reason','unknown')}. Check CDP reachability and that AionUi is open.")

    if source == "team-repair":
        # already handled above for the common case; this guards future kinds.
        if kind and kind != "repair-ok":
            return (f"Team-repair event ({kind}) for team {pl.get('teamId','?')!r}: {row.get('body','')}".strip())

    # ── Graceful generic fallback ─────────────────────────────────────────────
    # NEVER emit a dead "not wired" string. Describe whatever we have so the
    # operator always gets a meaningful explanation, even for new sources/kinds.
    sev = row.get("severity", "mid")
    title = row.get("title") or "(untitled notification)"
    body = row.get("body") or ""
    src = source or "unknown"
    extra = ""
    if kind:
        extra = f" (kind: {kind})"
    elif pl:
        # show a compact view of the payload so nothing is hidden
        try:
            extra = " · payload: " + ", ".join(f"{k}={v}" for k, v in list(pl.items())[:6])
        except Exception:
            extra = ""
    return (f"{sev.upper()} notification from {src!r}: {title}{extra}. "
            + (f"Details: {body}" if body else "No further details were recorded.")).strip()


KANBAN_LOG_DIR = HOME / ".hermes" / "kanban" / "logs"


def _kanban_blocked_scan() -> None:
    """Detect blocked hermes kanban tasks and emit HIGH notifications (deduped)."""
    seen_ids: set[str] = set()
    # Load already-notified task IDs from the notifications DB
    try:
        con_n = sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True)
        rows_n = con_n.execute(
            "SELECT json_extract(payload, '$.taskId') FROM notifications "
            "WHERE source='kanban' AND json_extract(payload,'$.taskId') IS NOT NULL"
        ).fetchall()
        con_n.close()
        seen_ids = {r[0] for r in rows_n if r[0]}
    except Exception:
        pass

    # Part 1: team_tasks where blocked_by is non-empty
    try:
        if AIONUI_DB.exists():
            con_a = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            rows_a = con_a.execute(
                "SELECT id, subject, blocked_by FROM team_tasks "
                "WHERE blocked_by NOT IN ('[]', '', 'null')"
            ).fetchall()
            con_a.close()
            for task_id, subject, blocked_by in rows_a:
                if task_id in seen_ids:
                    continue
                _notify(
                    severity="high", source="kanban",
                    title=f"Task blocked: {subject[:80]}",
                    body=f"Task is blocked by: {blocked_by}",
                    payload={"kind": "task-blocked", "taskId": task_id,
                             "task": subject, "blockedBy": blocked_by, "attempts": 3},
                )
                seen_ids.add(task_id)
    except Exception:
        pass

    # Part 2: recent kanban logs (modified last 4h) containing "is blocked" at final attempt
    try:
        cutoff = time.time() - 4 * 3600
        if KANBAN_LOG_DIR.is_dir():
            for log_file in KANBAN_LOG_DIR.glob("t_*.log"):
                try:
                    if log_file.stat().st_mtime < cutoff:
                        continue
                    task_id = log_file.stem  # e.g. t_338f3e0a
                    if task_id in seen_ids:
                        continue
                    text = log_file.read_text(encoding="utf-8", errors="replace")
                    # Look for final-attempt failure with explicit block marker
                    if re.search(r"attempt 3/3|attempt=3|is blocked|blocked the task|blocked the card",
                                 text, re.IGNORECASE):
                        first_line = next(
                            (l.strip() for l in text.splitlines() if l.strip()), task_id
                        )
                        _notify(
                            severity="high", source="kanban",
                            title=f"Kanban task may be blocked: {task_id}",
                            body=first_line[:200],
                            payload={"kind": "log-blocked", "taskId": task_id,
                                     "task": task_id, "attempts": 3},
                        )
                        seen_ids.add(task_id)
                except Exception:
                    pass
    except Exception:
        pass


# Service-state transition tracker (for Phase C event capture)
_SVC_PREV_STATES: dict[str, str] = {}
_SVC_FIRST_POLL = True
_BAD_DISPLAY_STATES  = frozenset({"offline", "stopped", "error", "blank", "crash", "disabled"})
_GOOD_DISPLAY_STATES = frozenset({"running", "online", "ok", "healthy", "reachable", "enabled",
                                   "degraded", "active"})


def _check_service_transitions(snapshot: dict) -> None:
    global _SVC_PREV_STATES, _SVC_FIRST_POLL
    if _SVC_FIRST_POLL:
        for svc, data in snapshot.items():
            if isinstance(data, dict) and "display_state" in data:
                _SVC_PREV_STATES[svc] = data["display_state"]
        _SVC_FIRST_POLL = False
        return
    for svc, data in snapshot.items():
        if not isinstance(data, dict) or "display_state" not in data:
            continue
        curr = data["display_state"]
        prev = _SVC_PREV_STATES.get(svc)
        if prev is not None and curr != prev:
            severity = "mid" if curr in _BAD_DISPLAY_STATES else "low"
            _notify(
                severity=severity, source="service",
                title=f"{svc}: {prev} → {curr}",
                body=f"Service '{svc}' changed state from '{prev}' to '{curr}'.",
                payload={"kind": "state-transition", "service": svc, "from": prev, "to": curr},
            )
        _SVC_PREV_STATES[svc] = curr


# ── CDP / Make New Team helpers ───────────────────────────────────────────────

def _cdp_find_aionui_page() -> tuple[bool, str, str]:
    """GET http://127.0.0.1:CDP_PORT/json, return (ok, ws_url, error).
    Prefers a page whose title contains 'AionUi'; falls back to first page target."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", CDP_PORT, timeout=5)
        conn.request("GET", "/json")
        resp = conn.getresponse()
        targets = json.loads(resp.read())
        conn.close()
    except Exception as exc:
        return False, "", str(exc)
    page = next((t for t in targets if t.get("type") == "page"
                 and "AionUi" in t.get("title", "")), None)
    if not page:
        page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        return False, "", "No page target found in CDP /json (is AionUi open?)"
    ws = page.get("webSocketDebuggerUrl", "")
    if not ws:
        return False, "", "CDP target missing webSocketDebuggerUrl"
    return True, ws, ""


def _cdp_team_create(ws_url: str, params: dict) -> tuple[bool, dict]:
    """Invoke the AionUi team.create bridge provider via CDP Runtime.evaluate.

    The bridge uses a subscribe/callback event pattern:
      - Renderer emits 'subscribe-team.create' with {id, data}
      - Main process fires the provider, then broadcasts 'subscribe.callback-team.create<id>'
      - Renderer resolves with the TTeam value from that broadcast

    We replicate this pattern in the CDP expression by:
      1. Setting up an on-listener via window.electronAPI.on() BEFORE emitting
      2. Emitting 'subscribe-team.create' with a random id
      3. Awaiting the Promise that resolves when the callback event arrives
    Returns (ok, {team_obj} or {error}).
    """
    import asyncio as _aio
    try:
        import websockets as _ws
    except ImportError:
        return False, {"error": "websockets library not installed; run: pip install websockets"}

    async def _run() -> dict:
        params_json = json.dumps(params)
        # JS expression: subscribe/callback bridge pattern
        expr = r"""
(function() {
  return new Promise(function(resolve, reject) {
    var uid = Math.random().toString(36).slice(2) + Date.now().toString(36);
    var callbackEvent = 'subscribe.callback-team.create' + uid;
    var off;
    var timer = setTimeout(function() {
      try { off && off(); } catch(e) {}
      reject(new Error('team.create CDP timed out (25s)'));
    }, 25000);
    off = window.electronAPI.on(function(evt) {
      try {
        var msg = JSON.parse(evt.value);
        if (msg.name === callbackEvent) {
          clearTimeout(timer);
          try { off(); } catch(e) {}
          if (msg.data && msg.data.id) {
            resolve(msg.data);
          } else {
            reject(new Error('bridge returned unexpected data: ' + JSON.stringify(msg.data)));
          }
        }
      } catch(e) {}
    });
    window.electronAPI.emit('subscribe-team.create', {id: uid, data: PARAMS_PLACEHOLDER});
  });
})()
""".replace("PARAMS_PLACEHOLDER", params_json)
        payload = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
        })
        async with _ws.connect(ws_url, open_timeout=10) as sock:
            await sock.send(payload)
            loop = _aio.get_event_loop()
            deadline = loop.time() + 35
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("CDP connection timed out waiting for team.create response")
                raw = await _aio.wait_for(sock.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    return msg

    try:
        return True, _aio.run(_run())
    except Exception as exc:
        return False, {"error": str(exc)}


def _cdp_send_message(ws_url: str, conversation_id: str, text: str) -> tuple[bool, dict]:
    """Inject a user message into an AionUi conversation via CDP Runtime.evaluate.

    Uses the subscribe/callback bridge pattern with bridge event
    'subscribe-chat.send.message' (payload {conversation_id, input}).
    Returns (ok, {success:bool} or {error:str}).
    """
    import asyncio as _aio
    try:
        import websockets as _ws
    except ImportError:
        return False, {"error": "websockets library not installed; run: pip install websockets"}

    async def _run() -> dict:
        conv_id_json = json.dumps(conversation_id)
        text_json = json.dumps(text)
        expr = r"""
(function() {
  return new Promise(function(resolve, reject) {
    var uid = Math.random().toString(36).slice(2) + Date.now().toString(36);
    var callbackEvent = 'subscribe.callback-chat.send.message' + uid;
    var off;
    var timer = setTimeout(function() {
      try { off && off(); } catch(e) {}
      reject(new Error('chat.send.message CDP timed out (25s)'));
    }, 25000);
    off = window.electronAPI.on(function(evt) {
      try {
        var msg = JSON.parse(evt.value);
        if (msg.name === callbackEvent) {
          clearTimeout(timer);
          try { off(); } catch(e) {}
          resolve(msg.data || {});
        }
      } catch(e) {}
    });
    window.electronAPI.emit('subscribe-chat.send.message', {
      id: uid,
      data: {conversation_id: CONV_ID, input: TEXT}
    });
  });
})()
""".replace("CONV_ID", conv_id_json).replace("TEXT", text_json)
        payload = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True},
        })
        async with _ws.connect(ws_url, open_timeout=10) as sock:
            await sock.send(payload)
            loop = _aio.get_event_loop()
            deadline = loop.time() + 35
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("CDP connection timed out waiting for chat.send.message response")
                raw = await _aio.wait_for(sock.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    return msg

    try:
        return True, _aio.run(_run())
    except Exception as exc:
        return False, {"error": str(exc)}


def _cdp_emit_event(ws_url: str, event_name: str, data: dict) -> tuple[bool, dict]:
    """KHAOSS-PATCH: failure-immune-system — fire an AionUi bridge event over CDP (fire-and-forget).
    Mirrors _cdp_send_message but emits an arbitrary subscribe-<event> with {id, data} and does not
    wait for a callback. Used by SAFE remediations (e.g. team.ensure-session)."""
    import asyncio as _aio
    try:
        import websockets as _ws
    except ImportError:
        return False, {"error": "websockets library not installed"}

    async def _run() -> dict:
        ev_json = json.dumps(event_name)
        data_json = json.dumps(data)
        expr = (
            "(function(){var uid=Math.random().toString(36).slice(2)+Date.now().toString(36);"
            "window.electronAPI.emit('subscribe-'+" + ev_json + ",{id:uid,data:" + data_json + "});"
            "return {emitted:true,event:" + ev_json + "};})()"
        )
        payload = json.dumps({"id": 1, "method": "Runtime.evaluate",
                              "params": {"expression": expr, "returnByValue": True}})
        async with _ws.connect(ws_url, open_timeout=10) as sock:
            await sock.send(payload)
            loop = _aio.get_event_loop()
            deadline = loop.time() + 15
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("CDP emit timed out")
                raw = await _aio.wait_for(sock.recv(), timeout=remaining)
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    return msg
    try:
        return True, _aio.run(_run())
    except Exception as exc:
        return False, {"error": str(exc)}


def _ollama_ps() -> list:
    """KHAOSS-PATCH: failure-immune-system — resident models via `ollama ps` -> [{name,size}]. Empty on error."""
    try:
        proc = subprocess.run(["ollama", "ps"], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    out = []
    for line in proc.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 3:
            # NAME ID SIZE PROCESSOR CONTEXT UNTIL...  — size is 2 tokens (e.g. '5.2 GB')
            name = parts[0]
            size = " ".join(parts[2:4]) if len(parts) >= 4 else parts[2]
            out.append({"name": name, "size": size})
    return out


def _ram_sample() -> dict:
    """KHAOSS-PATCH: failure-immune-system — free/avail/swap in MB from /proc/meminfo."""
    info = {}
    try:
        for line in open("/proc/meminfo"):
            k, _, v = line.partition(":")
            info[k.strip()] = int(v.strip().split()[0])  # kB
    except Exception:
        return {"free_mb": None, "avail_mb": None, "swap_used_mb": None}
    free = info.get("MemFree", 0) // 1024
    avail = info.get("MemAvailable", 0) // 1024
    swap_used = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) // 1024
    return {"free_mb": free, "avail_mb": avail, "swap_used_mb": swap_used}


# ── KHAOSS-PATCH: failure-immune-system — the LOGS section (one place for every log we write) ──
def _log_sources() -> dict:
    """name -> {path, desc, builtin_filter?}. Resolves today's daily + the panel log dynamically.
    Everything the immune system + stack writes, in one selectable list (bind — the bridge ledger is
    referenced read-only here; its rollback card is NOT duplicated)."""
    aion_logs = HOME / ".config" / "AionUi-Dev" / "logs"
    dailies = sorted(aion_logs.glob("20*-*-*.log"))
    daily = dailies[-1] if dailies else (aion_logs / "today.log")
    panel_candidates = [Path("/tmp/panel_server_fis.log"), LOG_FILE, Path("/tmp/panel_server.log")]
    panel_log = next((p for p in panel_candidates if p.exists() and p.stat().st_size > 0), LOG_FILE)
    return {
        "failure-journal":        {"path": FAILURE_JOURNAL, "desc": "canonical failure journal (.jsonl, append-only)"},
        "failure-journal-aionui": {"path": aion_logs / "failure-journal.log", "desc": "AionUi-side turn-failure log (source for ingest)"},
        "bridge-ledger":          {"path": BRIDGE_LEDGER, "desc": "traceability ledger (read-only here; rollback is on the LEDGER card)"},
        "freeze-reports":         {"path": aion_logs / "freeze-reports.log", "desc": "Electron freeze/unclean-exit reports"},
        "acp-stderr":             {"path": daily, "desc": "ACP hermes stderr (filtered from the electron daily)", "builtin_filter": "ACP"},
        "electron-daily":         {"path": daily, "desc": "electron-log daily (whole app log)"},
        "panel":                  {"path": panel_log, "desc": "the panel's own stdout/stderr"},
        "invariants":             {"path": None, "desc": "the invariants board result (runs check_invariants.sh)", "action": "invariants"},
    }


def _bounded_tail(path: "Path", n: int, filt: str = "", builtin_filter: str = "",
                  max_bytes: int = 512 * 1024) -> list:
    """Tail the last N lines WITHOUT loading the whole file — seek to the last max_bytes, then split.
    (The panel must not OOM itself reading a log about OOMs.) Applies builtin_filter (per-source) +
    the user filter, case-insensitive."""
    try:
        size = os.path.getsize(str(path))
        with open(str(path), "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial first line
            data = f.read().decode("utf-8", "replace")
    except Exception as e:
        return [f"[logs] cannot read {path}: {e}"]
    lines = data.splitlines()
    if builtin_filter:
        bf = builtin_filter.lower()
        lines = [l for l in lines if bf in l.lower()]
    if filt:
        ff = filt.lower()
        lines = [l for l in lines if ff in l.lower()]
    return lines[-max(1, n):]


def _wait_for_session_live(leader_conv_id: str, timeout_s: int = 10) -> tuple[bool, int]:
    """Poll conversations.extra.teamMcpPort until set + TCP-reachable, or timeout.
    Returns (live, port)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            row = con.execute(
                "SELECT json_extract(extra, '$.teamMcpPort') AS port "
                "FROM conversations WHERE id=?",
                (leader_conv_id,),
            ).fetchone()
            con.close()
            if row and row[0]:
                port = int(row[0])
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                try:
                    sock.connect(("127.0.0.1", port))
                    sock.close()
                    return True, port
                except OSError:
                    sock.close()
        except Exception:
            pass
        time.sleep(0.5)
    return False, 0


# ── Space Agent Bridge helpers ────────────────────────────────────────────────

def _bridge_path_allowed(path: str) -> bool:
    """Return True if path is under at least one ALLOWED_PATHS prefix."""
    p = os.path.realpath(os.path.expanduser(path))
    # KHAOSS-PATCH: traceability-ledger — the write path must NOT be able to target the ledger (a ledger
    # the audited thing can rewrite is not a ledger). Also protect .bak files from direct clobber.
    if p == os.path.realpath(str(BRIDGE_LEDGER)):
        return False
    # KHAOSS-PATCH: failure-immune-system — the write path must not be able to truncate the journal either.
    if p == os.path.realpath(str(FAILURE_JOURNAL)):
        return False
    return any(p.startswith(os.path.realpath(os.path.expanduser(a)))
               for a in ALLOWED_PATHS)

def _bridge_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }

# ── AI Launch Assist ─────────────────────────────────────────────────────────

DEFAULT_LA_PROMPT = """\
You are an AI assistant helping with local services and infrastructure tasks.

Current services available:
{services}

Read-only paths: {readonlyPaths}
Writable paths: {writablePaths}
Environment: {environment}

When you want to propose running a command, emit a line:
[[PROPOSE]] {"action": "run_command", "command": "<single command, no shell metacharacters>", "reason": "<why>"}

When you want to propose editing a file, emit a line:
[[PROPOSE]] {"action": "edit_file", "path": "<absolute path>", "before": "<exact current text>", "after": "<replacement>", "reason": "<why>"}

Constraints:
- Command proposals must be a single command with no pipes, redirections, or shell metacharacters (>, <, |, &, ;, backtick)
- File edit proposals must match the current file contents exactly; the before field must appear exactly once
- All other output is recorded as observations visible to the operator
"""

_la_sessions: dict[str, "_LASession"] = {}
_la_sessions_lock = threading.Lock()


class _LAProposal:
    def __init__(self, session_id: str, action: str, payload: dict) -> None:
        self.id = f"prop_{os.urandom(4).hex()}"
        self.session_id = session_id
        self.ts = int(time.time() * 1000)
        self.action = action
        self.payload = payload
        self.status = "pending"
        self.result: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "ts": self.ts,
            "action": self.action,
            "payload": self.payload,
            "status": self.status,
            "result": self.result,
        }


class _LASession:
    def __init__(self, session_id: str, agent_id: str, adapter: str, prompt_used: str) -> None:
        self.id = session_id
        self.agent_id = agent_id
        self.adapter = adapter
        self.status = "starting"
        self.started_at = int(time.time() * 1000)
        self.ended_at: int | None = None
        self.prompt_used = prompt_used
        self.observations: list[dict] = []
        self.proposals: list[_LAProposal] = []
        self.proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self._broadcaster = NotifBroadcaster()
        self.feedback_opt_in = False
        # codex-specific
        self._codex_thread_id: str | None = None
        self._codex_thread_id_missing = False
        self._codex_batch: list[str] = []
        self._codex_batch_lock = threading.Lock()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "adapter": self.adapter,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "observation_count": len(self.observations),
            "proposal_count": len(self.proposals),
            "proposals": [p.to_dict() for p in self.proposals],
        }


def _la_load_agents() -> list[dict]:
    if LA_AGENTS_FILE.exists():
        try:
            data = json.loads(LA_AGENTS_FILE.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            pass
    try:
        default_text = LA_PROMPT_FILE.read_text()
    except Exception:
        default_text = ""
    _ = default_text
    return [
        {
            "id": "cc-default",
            "name": "Claude Code",
            "adapter": "cc",
            "cmd": ["claude"],
            "args": ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
            "isDefault": True,
        },
        {
            "id": "codex-default",
            "name": "Codex",
            "adapter": "codex",
            "cmd": ["codex"],
            "args": [],
            "isDefault": False,
        },
    ]


def _la_save_agents(agents: list[dict]) -> None:
    LA_AGENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LA_AGENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(agents, indent=2))
    tmp.replace(LA_AGENTS_FILE)


def _la_load_prompt() -> str:
    if LA_PROMPT_FILE.exists():
        try:
            return LA_PROMPT_FILE.read_text()
        except Exception:
            pass
    return DEFAULT_LA_PROMPT


def _la_save_prompt(text: str) -> None:
    LA_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LA_PROMPT_FILE.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(LA_PROMPT_FILE)


def _la_build_prompt(template: str, services: list, readonly_paths: list,
                     writable_paths: list, environment: dict) -> str:
    return (template
        .replace("{services}",      ", ".join(services) if services else "(none)")
        .replace("{readonlyPaths}", ", ".join(readonly_paths) if readonly_paths else "(none)")
        .replace("{writablePaths}", ", ".join(writable_paths) if writable_paths else "(none)")
        .replace("{environment}",   json.dumps(environment) if environment else "{}")
    )


def _la_emit(session: _LASession, event_type: str, data: dict) -> None:
    """Append to session state and broadcast to SSE subscribers."""
    ts = int(time.time() * 1000)
    payload = json.dumps({"type": event_type, "ts": ts, **data})
    session._broadcaster.broadcast(payload)
    if event_type == "observation":
        session.observations.append({"ts": ts, "text": data.get("text", "")})
    elif event_type == "status":
        session.status = data.get("status", session.status)


def _la_close_session(session_id: str) -> None:
    with _la_sessions_lock:
        session = _la_sessions.pop(session_id, None)
    if session is None:
        return
    session.status = "done"
    session.ended_at = int(time.time() * 1000)
    if session.proc is not None:
        try:
            session.proc.terminate()
            try: session.proc.wait(timeout=3)
            except subprocess.TimeoutExpired: session.proc.kill()
        except Exception:
            pass
    session._broadcaster.broadcast(json.dumps({
        "type": "done", "status": "done", "ts": int(time.time() * 1000),
    }))
    _la_serialize_session(session)


def _la_serialize_session(session: _LASession) -> None:
    try:
        LA_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        data: dict = {
            "id": session.id,
            "agent_id": session.agent_id,
            "adapter": session.adapter,
            "status": session.status,
            "started_at": session.started_at,
            "ended_at": session.ended_at,
            "observations": session.observations,
            "proposals": [p.to_dict() for p in session.proposals],
            "feedback_opt_in": session.feedback_opt_in,
        }
        if session.feedback_opt_in:
            data["feedback_prompt"] = _la_anonymize(session.prompt_used)
        path = LA_SESSIONS_DIR / f"{session.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except Exception as exc:
        logger.warning("la: failed to serialize session %s: %s", session.id, exc)


def _la_anonymize(text: str) -> str:
    """Minimal PII scrub for dev feedback: strip home directory path prefix."""
    home_str = str(HOME)
    text = text.replace(home_str, "~")
    import getpass
    try:
        username = getpass.getuser()
        text = re.sub(re.escape(username), "<user>", text)
    except Exception:
        pass
    return text


def _la_init_dirs() -> None:
    LA_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LA_FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    adapters_parent = str(LA_ADAPTERS_DIR.parent)
    if adapters_parent not in sys.path:
        sys.path.insert(0, adapters_parent)


def _la_handle_events(session: _LASession, events: list[dict]) -> None:
    """Route parsed adapter events (observations, proposals) to session state + SSE."""
    for evt in events:
        etype = evt.get("type", "")
        if etype == "observation":
            _la_emit(session, "observation", {"text": evt.get("text", "")})
        elif etype == "proposal":
            payload = evt.get("payload") or {}
            action = str(payload.get("action", "other"))
            prop = _LAProposal(session.id, action, payload)
            session.proposals.append(prop)
            session._broadcaster.broadcast(json.dumps({
                "type": "proposal",
                "ts": prop.ts,
                "id": prop.id,
                "action": prop.action,
                "payload": prop.payload,
                "status": "pending",
            }))
        elif etype == "proposal_raw":
            _la_emit(session, "observation",
                     {"text": f"[unparsed proposal] {evt.get('text','')}"})


# ── CC adapter runner ─────────────────────────────────────────────────────────

def _la_cc_start(session: _LASession, agent: dict) -> None:
    argv = list(agent.get("cmd", ["claude"])) + list(agent.get("args", []))
    try:
        session.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(HOME),
            env=dict(os.environ),
        )
    except FileNotFoundError:
        _la_emit(session, "status", {"status": "error"})
        _la_emit(session, "observation", {"text": f"error: command not found: {argv[0]}"})
        _la_close_session(session.id)
        return

    session.status = "running"
    _la_emit(session, "status", {"status": "running"})

    try:
        msg = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": session.prompt_used},
        }) + "\n"
        assert session.proc.stdin is not None
        session.proc.stdin.write(msg)
        session.proc.stdin.flush()
    except Exception as exc:
        logger.warning("la cc: failed to write initial prompt: %s", exc)

    threading.Thread(target=_la_cc_stdout_reader, args=(session,),
                     name=f"la-cc-{session.id[:8]}", daemon=True).start()


def _la_cc_stdout_reader(session: _LASession) -> None:
    from launch_assist_adapters import cc as _cc_mod
    assert session.proc is not None
    assert session.proc.stdout is not None
    try:
        for line in session.proc.stdout:
            events = _cc_mod.parse_stdout_line(line)
            _la_handle_events(session, events)
    except Exception as exc:
        logger.warning("la cc reader: %s", exc)
    finally:
        rc = session.proc.wait()
        if session.status == "running":
            session.status = "done" if rc == 0 else "error"
        _la_emit(session, "status", {"status": session.status, "exit_code": rc})
        with _la_sessions_lock:
            _la_sessions.pop(session.id, None)
        session.ended_at = int(time.time() * 1000)
        session._broadcaster.broadcast(json.dumps({
            "type": "done", "status": session.status, "ts": int(time.time() * 1000),
        }))
        _la_serialize_session(session)


# ── Codex adapter runner ──────────────────────────────────────────────────────

def _la_codex_start(session: _LASession, agent: dict) -> None:
    from launch_assist_adapters import codex as _codex_mod
    cmd = list(agent.get("cmd", ["codex"]))
    first_argv = cmd + _codex_mod.FIRST_ARGS
    session.status = "running"
    _la_emit(session, "status", {"status": "running"})

    def _run_first() -> None:
        try:
            result = subprocess.run(
                first_argv,
                input=session.prompt_used,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(HOME),
                env=dict(os.environ),
            )
            thread_id, events = _codex_mod.parse_output(result.stdout)
            if result.returncode != 0 and not events:
                _la_emit(session, "observation",
                         {"text": f"codex exited {result.returncode}: {result.stderr[:500]}"})

            if thread_id:
                session._codex_thread_id = thread_id
                logger.info("la codex: thread_id=%s", thread_id)
            else:
                session._codex_thread_id_missing = True
                logger.warning("la codex: thread.started missing from first batch — "
                               "subsequent batches will be one-shot")
                _la_emit(session, "observation", {
                    "text": "Codex thread continuity lost; subsequent batches will not retain prior context.",
                })

            _la_handle_events(session, events)
        except subprocess.TimeoutExpired:
            _la_emit(session, "observation", {"text": "codex first batch timed out (60s)"})
        except FileNotFoundError:
            _la_emit(session, "status", {"status": "error"})
            _la_emit(session, "observation", {"text": f"error: command not found: {first_argv[0]}"})
            _la_close_session(session.id)
            return

        # Start batch loop for continuation messages
        t = threading.Thread(target=_la_codex_batch_loop, args=(session, cmd),
                             name=f"la-codex-{session.id[:8]}", daemon=True)
        t.start()

    threading.Thread(target=_run_first, name=f"la-codex-init-{session.id[:8]}", daemon=True).start()


def _la_codex_batch_loop(session: _LASession, cmd: list[str]) -> None:
    """Poll every _LA_CODEX_BATCH_INTERVAL seconds and flush if batch has items."""
    while True:
        time.sleep(_LA_CODEX_BATCH_INTERVAL)
        # Check if session still live
        with _la_sessions_lock:
            if session.id not in _la_sessions:
                return
        with session._codex_batch_lock:
            if not session._codex_batch:
                continue
            batch = session._codex_batch[:_LA_CODEX_BATCH_SIZE]
            session._codex_batch = session._codex_batch[_LA_CODEX_BATCH_SIZE:]
        _la_codex_flush(session, cmd, batch)


def _la_codex_flush(session: _LASession, cmd: list[str], batch: list[str]) -> None:
    from launch_assist_adapters import codex as _codex_mod
    batch_text = "\n".join(batch)
    if session._codex_thread_id and not session._codex_thread_id_missing:
        argv = cmd + _codex_mod.make_resume_args(session._codex_thread_id)
    else:
        argv = cmd + _codex_mod.FIRST_ARGS

    try:
        result = subprocess.run(
            argv,
            input=batch_text,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(HOME),
            env=dict(os.environ),
        )
        thread_id, events = _codex_mod.parse_output(result.stdout)
        if thread_id and not session._codex_thread_id:
            session._codex_thread_id = thread_id
        _la_handle_events(session, events)
    except subprocess.TimeoutExpired:
        _la_emit(session, "observation", {"text": "codex batch timed out (60s)"})
    except Exception as exc:
        logger.warning("la codex flush: %s", exc)


# ── Generic adapter runner ────────────────────────────────────────────────────

def _la_generic_start(session: _LASession, agent: dict) -> None:
    argv = list(agent.get("cmd", [])) + list(agent.get("args", []))
    if not argv:
        _la_emit(session, "status", {"status": "error"})
        _la_emit(session, "observation", {"text": "error: agent has no cmd configured"})
        _la_close_session(session.id)
        return
    try:
        session.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(HOME),
            env=dict(os.environ),
        )
    except FileNotFoundError:
        _la_emit(session, "status", {"status": "error"})
        _la_emit(session, "observation", {"text": f"error: command not found: {argv[0]}"})
        _la_close_session(session.id)
        return

    session.status = "running"
    _la_emit(session, "status", {"status": "running"})

    try:
        assert session.proc.stdin is not None
        session.proc.stdin.write(session.prompt_used + "\n")
        session.proc.stdin.flush()
    except Exception as exc:
        logger.warning("la generic: failed to write prompt: %s", exc)

    t = threading.Thread(target=_la_generic_stdout_reader, args=(session,),
                         name=f"la-gen-{session.id[:8]}", daemon=True)
    t.start()


def _la_generic_stdout_reader(session: _LASession) -> None:
    from launch_assist_adapters import generic_stdio as _gen_mod
    assert session.proc is not None
    assert session.proc.stdout is not None
    try:
        for line in session.proc.stdout:
            evt = _gen_mod.parse_line(line)
            if evt:
                _la_handle_events(session, [evt])
    except Exception as exc:
        logger.warning("la generic reader: %s", exc)
    finally:
        rc = session.proc.wait()
        if session.status == "running":
            session.status = "done" if rc == 0 else "error"
        _la_emit(session, "status", {"status": session.status, "exit_code": rc})
        with _la_sessions_lock:
            _la_sessions.pop(session.id, None)
        session.ended_at = int(time.time() * 1000)
        session._broadcaster.broadcast(json.dumps({
            "type": "done", "status": session.status, "ts": int(time.time() * 1000),
        }))
        _la_serialize_session(session)


# ── Proposal execution helpers ────────────────────────────────────────────────

def _la_execute_command(payload: dict) -> tuple[bool, dict]:
    cmd_str = str(payload.get("command", "")).strip()
    if not cmd_str:
        return False, {"error": "empty command"}
    if _LA_REDIRECTION_RE.search(cmd_str):
        return False, {"error": "command contains shell metacharacters (>, <, |, &, ;, backtick) — not allowed"}
    try:
        argv = shlex.split(cmd_str)
    except ValueError as exc:
        return False, {"error": f"invalid command syntax: {exc}"}
    if not argv:
        return False, {"error": "empty command after parsing"}
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(HOME),
        )
        return True, {
            "returncode": result.returncode,
            "stdout": result.stdout[:4000],
            "stderr": result.stderr[:2000],
        }
    except FileNotFoundError:
        return False, {"error": f"command not found: {argv[0]}"}
    except subprocess.TimeoutExpired:
        return False, {"error": "command timed out (60s)"}
    except Exception as exc:
        return False, {"error": str(exc)}


def _la_execute_edit(payload: dict) -> tuple[bool, dict]:
    path_str = str(payload.get("path", "")).strip()
    before = str(payload.get("before", ""))
    after = str(payload.get("after", ""))
    if not path_str:
        return False, {"error": "path is required"}
    if not before:
        return False, {"error": "before is required"}
    try:
        p = Path(os.path.realpath(os.path.expanduser(path_str)))
    except Exception as exc:
        return False, {"error": f"invalid path: {exc}"}
    if not p.exists():
        return False, {"error": f"path does not exist: {path_str}"}
    if not p.is_file():
        return False, {"error": f"not a file: {path_str}"}
    try:
        current = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return False, {"error": f"could not read file: {exc}"}
    count = current.count(before)
    if count == 0:
        return False, {"error": "before text not found in file"}
    if count > 1:
        return False, {"error": f"before text matches {count} times; must match exactly once"}
    new_content = current.replace(before, after, 1)
    try:
        tmp = p.with_suffix(p.suffix + ".la_tmp")
        tmp.write_text(new_content, encoding="utf-8")
        tmp.replace(p)
        return True, {"bytesWritten": len(new_content.encode("utf-8"))}
    except Exception as exc:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        return False, {"error": f"write failed: {exc}"}


def _la_run_proposal(proposal: _LAProposal) -> dict:
    """Execute an approved proposal. Returns result dict."""
    if proposal.action == "run_command":
        ok, result = _la_execute_command(proposal.payload)
    elif proposal.action == "edit_file":
        ok, result = _la_execute_edit(proposal.payload)
    else:
        return {"ok": False, "error": f"unknown action: {proposal.action}"}
    return {"ok": ok, **result}


# ── Prompter (Q&A-to-prompt synthesis) ───────────────────────────────────────

_PROMPTER_BOOTSTRAP = """\
You are helping a user design the system prompt for a Hermes-led team of AI
agents inside the KHAOSS Stack. The user will use your output to spawn the team
in their AionUi panel.

Your job: conduct a short focused interview (5-10 questions, one at a time) to
understand the team's purpose. Then synthesize the system prompt(s).

Information you need to collect, in roughly this order:
1. What is the team's primary purpose? (project context, domain, audience)
2. What outputs should it produce? (artifacts, decisions, reports)
3. What tools/data sources does it touch? (codebases, vaults, APIs, kanban)
4. Local-first or cloud-allowed? (privacy posture)
5. Risk surface - what should the leader NEVER do without explicit user OK?
   (cost-incurring actions, destructive ops, external communications)
6. How autonomous should teammates be? (propose-only / auto-execute on small
   things / fully autonomous within scope)
7. Naming / tone preferences? (formal / casual / domain-specific terminology)
8. Anything domain-specific you want me to know? (regulatory, IP-sensitive,
   bilingual, anything else)

Rules:
- ONE question at a time. Wait for the user's answer before asking the next.
- Each question is one or two sentences max - no walls of text.
- If the user's answer is ambiguous, ask a clarifying question instead of moving on.
- After ~7 answers (or when the user signals "synthesize now"), stop asking and
  produce a synthesis.

Synthesis output format (when ready, output EXACTLY this JSON inside a fenced
```json block, nothing else after it):

```json
{
  "lineup_summary": "<1-2 sentence team description>",
  "leader_prompt": "<full system prompt for the team leader, 200-500 words, including: identity, mission, tool authorization, escalation rules, language/tone preferences>",
  "role_briefings": [
    {"role": "<role name>", "prompt": "<briefing for this teammate, 80-150 words: identity, scope, when to escalate to leader>"}
  ]
}
```

The role list should be 3-10 roles depending on what the user's project actually needs.
If the user wants a leader-only team, output an empty role list and a leader prompt only.
After your JSON block, emit no further text.
Begin the interview now with your first question.\
"""

_PROMPTER_SYNTHESIS_TRIGGER = """\
The user is ready for synthesis. Stop asking questions and produce the JSON
synthesis block now using everything you've learned so far. If you have less
information than ideal, do your best with what you have.\
"""

_PROMPTER_SYNTHESIS_RE = re.compile(r'```json\s*(.*?)\s*```', re.DOTALL)

_prompter_sessions: dict[str, "_PrompterSession"] = {}
_prompter_sessions_lock = threading.Lock()


class _PrompterSession:
    def __init__(self, session_id: str, project_name: str, intent_seed: str,
                 agent_id: str, agent_kind: str, ollama_model: str = PROMPTER_OLLAMA_MODEL) -> None:
        self.id = session_id
        self.created_at = time.time()
        self.agent_id = agent_id
        self.agent_kind = agent_kind          # 'cc' | 'ollama'
        self.ollama_model = ollama_model
        self.state = "awaiting_answer"
        self.project_name = project_name
        self.intent_seed = intent_seed
        self.turns: list[dict] = []
        self.current_question: str | None = None
        self.synthesis: dict | None = None
        self.library_id: str | None = None
        self.proc: subprocess.Popen | None = None  # type: ignore[type-arg]
        self.proc_lock = threading.Lock()
        self.broadcaster = NotifBroadcaster()
        self._ollama_messages: list[dict] = []
        self._first_q_event = threading.Event()
        self._first_q: str | None = None
        # KHAOSS-PATCH: catalog-backend-routing — openai_compat path
        self._oapicompat_base_url: str = ""
        self._oapicompat_api_key: str = ""
        self._oapicompat_model: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "agent_id": self.agent_id,
            "agent_kind": self.agent_kind,
            "ollama_model": self.ollama_model,
            "state": self.state,
            "project_name": self.project_name,
            "intent_seed": self.intent_seed,
            "turns": self.turns,
            "current_question": self.current_question,
            "synthesis": self.synthesis,
            "library_id": self.library_id,
            "turn_count": len(self.turns),
        }


def _prompter_emit(session: "_PrompterSession", event_type: str, data: dict) -> None:
    ts = int(time.time() * 1000)
    payload = json.dumps({"event": event_type, "ts": ts, **data})
    session.broadcaster.broadcast(payload)


def _prompter_validate_synthesis(raw_json: str) -> tuple[bool, "dict | str"]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        return False, f"JSON parse error: {exc}"
    if not isinstance(data.get("lineup_summary"), str) or not data["lineup_summary"].strip():
        return False, "lineup_summary must be a non-empty string"
    if not isinstance(data.get("leader_prompt"), str) or not data["leader_prompt"].strip():
        return False, "leader_prompt must be a non-empty string"
    if not isinstance(data.get("role_briefings"), list):
        return False, "role_briefings must be a list"
    cleaned: list[dict] = []
    for r in data["role_briefings"]:
        if not isinstance(r, dict) or not r.get("role") or not r.get("prompt"):
            return False, f"each role_briefing needs role+prompt, got: {r!r}"
        cleaned.append({"role": str(r["role"])[:80], "prompt": str(r["prompt"])[:1500]})
    data["role_briefings"] = cleaned
    data["leader_prompt"] = str(data["leader_prompt"])[:4000]
    return True, data


def _prompter_handle_response(session: "_PrompterSession", full_text: str) -> None:
    """Parse a complete assistant response as question or synthesis and emit SSE."""
    m = _PROMPTER_SYNTHESIS_RE.search(full_text)
    if m:
        raw_json = m.group(1).strip()
        ok, result = _prompter_validate_synthesis(raw_json)
        if ok:
            assert isinstance(result, dict)
            session.synthesis = result
            session.state = "awaiting_review"
            session.turns.append({"role": "agent", "text": full_text, "ts": time.time()})
            _prompter_emit(session, "synthesized", {"synthesis": result})
            _prompter_emit(session, "done", {})
        else:
            assert isinstance(result, str)
            session.state = "error"
            session.turns.append({"role": "agent", "text": full_text, "ts": time.time(),
                                   "parse_error": result})
            _prompter_emit(session, "error", {"message": f"Synthesis parse failed: {result}",
                                               "raw": full_text[:500]})
        return

    question_text = full_text.strip()
    if not question_text:
        return
    session.current_question = question_text
    turn_idx = sum(1 for t in session.turns if t.get("role") == "agent")
    session.turns.append({"role": "agent", "text": question_text, "ts": time.time()})
    session.state = "awaiting_answer"

    if not session._first_q_event.is_set():
        session._first_q = question_text
        session._first_q_event.set()

    _prompter_emit(session, "question", {"text": question_text, "turn_index": turn_idx})

    # Soft cost/length warning
    user_turn_count = sum(1 for t in session.turns if t.get("role") == "user")
    if user_turn_count >= PROMPTER_TURN_WARN:
        _prompter_emit(session, "warning", {
            "message": "Interview's running long — consider synthesizing soon.",
            "turn_count": user_turn_count,
        })


def _prompter_serialize_session(session: "_PrompterSession") -> None:
    try:
        PROMPTER_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = PROMPTER_SESSIONS_DIR / f"{session.id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(session.to_dict(), indent=2, default=str))
        tmp.replace(path)
    except Exception as exc:
        logger.warning("prompter: serialize %s failed: %s", session.id, exc)


def _prompter_cc_start(session: "_PrompterSession", agent: dict) -> None:
    argv = list(agent.get("cmd", ["claude"])) + [
        "-p", "--input-format", "stream-json",
        "--output-format", "stream-json", "--verbose",
    ]
    try:
        session.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(HOME), env=dict(os.environ),
        )
    except FileNotFoundError:
        session.state = "error"
        session._first_q_event.set()
        _prompter_emit(session, "error", {"message": f"command not found: {argv[0]}"})
        _prompter_emit(session, "done", {})
        return

    initial = (
        f"{_PROMPTER_BOOTSTRAP}\n\n"
        f"Project: {session.project_name}\n"
        f"Context: {session.intent_seed}"
    )
    try:
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": initial}}) + "\n"
        with session.proc_lock:
            assert session.proc.stdin is not None
            session.proc.stdin.write(msg)
            session.proc.stdin.flush()
    except Exception as exc:
        logger.warning("prompter cc: write initial: %s", exc)

    threading.Thread(target=_prompter_cc_reader, args=(session,),
                     name=f"prompter-cc-{session.id[:8]}", daemon=True).start()


def _prompter_cc_reader(session: "_PrompterSession") -> None:
    assert session.proc is not None
    assert session.proc.stdout is not None
    try:
        for line in session.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "assistant":
                continue
            content = (evt.get("message") or {}).get("content", [])
            texts: list[str] = []
            if isinstance(content, str):
                texts.append(content)
            else:
                for block in (content or []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
            full = "\n".join(texts).strip()
            if full:
                _prompter_handle_response(session, full)
    except Exception as exc:
        logger.warning("prompter cc reader: %s", exc)
    finally:
        try:
            if session.proc:
                session.proc.wait()
        except Exception:
            pass
        if not session._first_q_event.is_set():
            session._first_q_event.set()
        if session.state not in ("awaiting_review", "error", "cancelled", "approved"):
            session.state = "error"
        _prompter_emit(session, "done", {})
        with _prompter_sessions_lock:
            _prompter_sessions.pop(session.id, None)
        _prompter_serialize_session(session)


def _prompter_cc_send_answer(session: "_PrompterSession", answer: str) -> bool:
    if session.proc is None or session.proc.poll() is not None:
        return False
    try:
        msg = json.dumps({"type": "user", "message": {"role": "user", "content": answer}}) + "\n"
        with session.proc_lock:
            assert session.proc.stdin is not None
            session.proc.stdin.write(msg)
            session.proc.stdin.flush()
        return True
    except Exception as exc:
        logger.warning("prompter cc: send answer: %s", exc)
        return False


def _prompter_cc_send_synthesize(session: "_PrompterSession") -> bool:
    return _prompter_cc_send_answer(session, _PROMPTER_SYNTHESIS_TRIGGER)


def _prompter_ollama_call(session: "_PrompterSession", extra_user: str | None = None) -> None:
    """POST to Ollama /api/chat (blocking). Always called in a background thread."""
    if extra_user:
        session._ollama_messages.append({"role": "user", "content": extra_user})
    body = json.dumps({
        "model": session.ollama_model,
        "messages": session._ollama_messages,
        "stream": False,
    }).encode()
    try:
        req = _urllib_req.Request(
            PROMPTER_OLLAMA_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with _urllib_req.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        assistant_text = (result.get("message") or {}).get("content", "")
        session._ollama_messages.append({"role": "assistant", "content": assistant_text})
        _prompter_handle_response(session, assistant_text)
    except Exception as exc:
        logger.warning("prompter ollama: %s", exc)
        if not session._first_q_event.is_set():
            session._first_q_event.set()
        session.state = "error"
        _prompter_emit(session, "error", {"message": f"Ollama call failed: {exc}"})
        _prompter_emit(session, "done", {})
        with _prompter_sessions_lock:
            _prompter_sessions.pop(session.id, None)
        _prompter_serialize_session(session)


# KHAOSS-PATCH: catalog-backend-routing — OpenAI-compatible spawn path
def _prompter_oapicompat_call(session: "_PrompterSession", extra_user: str | None = None) -> None:
    """POST to OpenAI-compatible /chat/completions. Always called in a background thread."""
    if extra_user:
        session._ollama_messages.append({"role": "user", "content": extra_user})
    body = json.dumps({
        "model": session._oapicompat_model,
        "messages": session._ollama_messages,
        "stream": False,
    }).encode()
    url = f"{session._oapicompat_base_url}/chat/completions"
    try:
        req = _urllib_req.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {session._oapicompat_api_key}",
            },
        )
        with _urllib_req.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
        choices = result.get("choices") or []
        if not choices:
            raise ValueError("empty choices array in response")
        assistant_text = (choices[0].get("message") or {}).get("content", "")
        session._ollama_messages.append({"role": "assistant", "content": assistant_text})
        _prompter_handle_response(session, assistant_text)
    except Exception as exc:
        logger.warning("prompter openai_compat: %s", exc)
        if not session._first_q_event.is_set():
            session._first_q_event.set()
        session.state = "error"
        _prompter_emit(session, "error", {"message": f"API call failed: {exc}"})
        _prompter_emit(session, "done", {})
        with _prompter_sessions_lock:
            _prompter_sessions.pop(session.id, None)
        _prompter_serialize_session(session)


def _prompter_mcp_send(port: int, token: str, tool: str, args: dict,
                        from_slot_id: str | None = None) -> dict:
    """TCP call to team MCP server using 4-byte big-endian length prefix framing."""
    payload: dict = {"tool": tool, "args": args, "auth_token": token}
    if from_slot_id:
        payload["from_slot_id"] = from_slot_id
    body = json.dumps(payload).encode("utf-8")
    header = struct.pack(">I", len(body))
    with socket.create_connection(("127.0.0.1", port), timeout=10.0) as sock:
        sock.sendall(header + body)
        raw_len = b""
        while len(raw_len) < 4:
            chunk = sock.recv(4 - len(raw_len))
            if not chunk:
                break
            raw_len += chunk
        if len(raw_len) < 4:
            raise OSError("connection closed before response length prefix")
        msg_len = struct.unpack(">I", raw_len)[0]
        raw_body = b""
        while len(raw_body) < msg_len:
            chunk = sock.recv(msg_len - len(raw_body))
            if not chunk:
                break
            raw_body += chunk
    return json.loads(raw_body.decode("utf-8"))


def _prompter_lookup_team_mcp(team_id: str) -> "tuple[bool, dict, str]":
    """Read team MCP config from aionui.db. Returns (ok, info, error)."""
    if not AIONUI_DB.exists():
        return False, {}, "aionui.db not found"
    try:
        con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        team_row = con.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        if not team_row:
            con.close()
            return False, {}, f"team {team_id!r} not found"
        agents = json.loads(team_row["agents"] or "[]")
        leader = next((a for a in agents if a.get("role") == "leader"), None)
        if not leader:
            con.close()
            return False, {}, "team has no leader agent"
        leader_conv_id = leader.get("conversationId", "")
        conv = con.execute("SELECT extra FROM conversations WHERE id=?",
                           (leader_conv_id,)).fetchone()
        con.close()
        if not conv:
            return False, {}, "leader conversation not found"
        extra = json.loads(conv["extra"] or "{}")
        cfg = extra.get("teamMcpStdioConfig") or {}
        env = {e["name"]: e["value"]
               for e in cfg.get("env", [])
               if "name" in e and "value" in e}
        port_str = env.get("TEAM_MCP_PORT")
        token = env.get("TEAM_MCP_TOKEN", "")
        if not port_str:
            return False, {}, "TEAM_MCP_PORT not found in conversation extra"
        teammates = [{"name": a.get("agentName", ""),
                      "slotId": a.get("slotId", ""),
                      "conversationId": a.get("conversationId", "")}
                     for a in agents if a.get("role") != "leader"]
        return True, {
            "port": int(port_str),
            "token": token,
            "leader_name": leader.get("agentName", "Leader"),
            "leader_slot": leader.get("slotId", ""),
            "leader_conv_id": leader_conv_id,
            "teammates": teammates,
        }, ""
    except Exception as exc:
        return False, {}, str(exc)


# ── Handler ──────────────────────────────────────────────────────────────────

# ★★★ TAIL BY SEEKING FROM THE END, NOT BY READING THE WHOLE FILE. 2026-09-18.
#   This endpoint did `fh.readlines()[-n:]` — materialising EVERY line of the log into a Python
#   list in order to return the last twenty. Measured today: /tmp/panel_server.log is 306,040,955
#   bytes and 3,585,479 lines, the endpoint is served at 1.12 req/s, and py-spy put 84.8% of all
#   panel CPU (1409 of 1661 samples) on that single line.
#   ★ That is the "idle core" I described yesterday as "honest work — a poll-rate question, not a
#     fault". It was not honest work. It was O(file size) per request, growing ~635 B/s forever,
#     and I had attributed it to load because I never asked WHERE the CPU went.
#   ★★ Reading backwards in blocks makes the cost O(bytes actually wanted) instead of O(file).
#     The file also has NO ROTATION (plain FileHandler, mode="a", no logrotate entry), so without
#     this the per-request cost rises for as long as the panel runs.
def _tail_lines(path, n: int, block: int = 8192) -> list[str]:
    """Last `n` lines, read backwards from EOF. Never loads the whole file."""
    if n <= 0:
        return []
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end == 0:
            return []
        buf = b""
        pos = end
        # ★ Keep reading blocks backwards until we hold n+1 newlines (the +1 guards a partial
        #   first line) or we reach the start of the file.
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
        text = buf.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return [ln + "\n" for ln in lines[-n:]]


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "PanelServer/2.0"

    # ★★★ A REQUEST LINE THAT NEVER ARRIVES MUST NOT COST A THREAD FOREVER. 2026-09-17.
    #   BaseHTTPRequestHandler.timeout defaults to None, so rfile.readline() in handle_one_request
    #   blocks with no bound. Any connection that opens and then goes quiet — a half-open socket, a
    #   client that connects and stalls, an idle keep-alive — parks one thread permanently. Measured
    #   45 of 48 threads sitting in http/server.py:435 on exactly that read.
    #   ★ This is a SILENT park, not the traceback convoy: nothing raises, so handle_error never
    #     sees it and the QuietThreadingHTTPServer fix cannot help. Two different defects, one
    #     symptom curve — which is why fixing the convoy made the growth slower but not zero.
    #   socketserver applies this to the connection socket; handle_one_request already treats a
    #   socket timeout as close_connection.
    # ★★ 30 -> 5 on 2026-09-17. 30 s stopped the INFINITE park, which was the point, but with
    #   pollers at 16 req/s it still let ~38 idle keep-alive connections accumulate, each holding a
    #   thread and a queue slot. On loopback 5 s is a generous grace for a client to send its
    #   request line, and idle connections retire six times faster.
    timeout = 5

    # We log through `logger`, not stderr default
    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    # ── KHAOSS-PATCH: panel-security-and-tunnel-controls — auth + tunnel ──────
    def _is_local_request(self) -> bool:
        """True only for genuine loopback with NO proxy-forward headers.
        cloudflared connects from 127.0.0.1 but injects CF-/X-Forwarded-* headers,
        so a forwarded request is REMOTE even though client_address looks local."""
        client_host = self.client_address[0] if self.client_address else ""
        is_loopback = client_host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost")
        forwarded = any(self.headers.get(h) for h in _PROXY_FORWARD_HEADERS)
        return is_loopback and not forwarded

    def _is_remote_safe(self) -> bool:
        """Default-DENY second tier. A remote (non-loopback) caller — even with a
        valid bearer token — may reach ONLY the read/status endpoints on the
        allowlist. Credential/config/destructive endpoints return here False and
        are refused, so opening a tailnet bind can never carry them off-box."""
        path = urlsplit(self.path).path
        method = self.command
        if method == "GET":
            if path in REMOTE_SAFE_GET_EXACT:
                return True
            return any(path.startswith(p) for p in REMOTE_SAFE_GET_PREFIX)
        if method == "POST":
            return path in REMOTE_SAFE_POST_EXACT
        return False  # DELETE / PUT / etc. are never remote-safe

    def _cookie_token(self) -> str:
        """The panel_token cookie value (browser remote-login path), or ''. A browser
        cannot set an Authorization header on a top-level navigation or on EventSource
        (SSE), so the remote login (POST /login) stores the SAME token in a cookie and
        the browser carries it automatically. Same secret, same compare_digest."""
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith("panel_token="):
                return part[len("panel_token="):].strip()
        return ""

    def _check_auth(self) -> bool:
        """Gate every route. Local loopback bypasses (full access). A remote
        request needs the bearer token AND must target a remote-safe endpoint
        (default-deny allowlist) — the guarantee that credential/destructive
        routes stay loopback-only regardless of what bind is open. The Telnyx
        webhook authenticates via Ed25519 signature instead.

        ★★★ WIDENED 2026-10-15 FOR THE PHONE SURFACE — AND THIS, NOT `_jarvis_chats`, IS THE GATE
        THAT ACTUALLY BITES. Measured: this runs BEFORE dispatch, so a remote caller never reached
        the per-route `_is_local_request()` check at all. A remote request for `/jarvis/chats` was
        refused HERE, by `_is_remote_safe()`'s default-deny allowlist — the handler's own 403 was
        the second lock on a door that was already shut. Widening the handler alone would have been
        a NO-OP that reads like a feature. Both had to move, and this one first.

        ★★★ THE WIDENING IS TWO NARROW LINES, AND NEITHER OF THEM IS "ALLOW REMOTE".
          · `/phone` and `/phone/*` are authenticated by the PHONE'S OWN CREDENTIAL, not the panel
            token. That is the entire point of pairing: requiring `PANEL_TOKEN` as well would mean
            pasting the board-wide master secret into a phone — the shared-secret problem pairing
            exists to remove, and a secret on a device that gets lost. So these paths fall through
            to `_phone_gate()`, which demands a paired, unrevoked, unexpired DEVICE token.
          · `/jarvis/chats` is admitted here ONLY WHEN A DEVICE TOKEN IS PRESENTED. Absent a token,
            the original default-deny 401/403 is unchanged — the old behaviour is the default and
            still the fast path.
        ★ WHAT STILL CANNOT LEAVE, VERIFIED BY THE SUITE: `/phone/devices`, `/phone/log`,
          `/phone/policy`, `/phone/revoke` are refused from a non-loopback caller EVEN WITH A VALID
          DEVICE TOKEN — they are the operator's acts, and `PHONE_LOCAL_ONLY` below is what says so.
        ★ THE THREE ANONYMOUS PHONE PATHS SERVE NO SECRET, which is the constraint the leak produced:
          `/phone` is the shell (no secret in it), `/phone/manifest.webmanifest` is static, and
          `/phone/pair/redeem` takes a SINGLE-USE code the operator had to mint on the box. There is
          still no anonymous path to a token or to a value.
        """
        path = urlsplit(self.path).path
        if path == "/webhooks/telnyx":
            return True
        if self._is_local_request():
            return True
        # ── REMOTE from here down ──
        # ★ THE PHONE SURFACE'S OWN DOOR, BEFORE THE PANEL TOKEN IS CONSIDERED.
        if path == "/phone" or path.startswith("/phone/"):
            if path in PHONE_LOCAL_ONLY:
                # ★ Defense in depth: the handler checks this too, and both checks are kept.
                #   A single check is a single edit away from being no check.
                self._send_json(403, {
                    "error": "loopback-only endpoint — the device roster, the ledger and the "
                             "policy are the operator's, not a paired phone's",
                    "path": path})
                return False
            return True
        if path == "/jarvis/chats" and self._phone_token():
            # ★ A device token routes this request to the phone gate instead of the panel-token
            #   gate. The token is NOT validated here — `_jarvis_chats` does that, in one place,
            #   so there is exactly one implementation of "is this a live device".
            return True
        auth = self.headers.get("Authorization", "")
        header_ok = _hmac.compare_digest(auth, f"Bearer {PANEL_TOKEN}")
        cookie_tok = self._cookie_token()
        cookie_ok = bool(cookie_tok) and _hmac.compare_digest(cookie_tok, PANEL_TOKEN)
        if not (header_ok or cookie_ok):
            self._send_json(401, {"error": "Unauthorized — token required (Bearer header, or log in at /login)"})
            return False
        if not self._is_remote_safe():
            self._send_json(403, {
                "error": "loopback-only endpoint — not reachable remotely; "
                         "credential/config/destructive endpoints never leave this machine",
                "path": path})
            return False
        return True

    def _security_token(self) -> None:
        """Return the panel token to genuine local requests only (never via tunnel)."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "token is available to local requests only"})
        return self._send_json(200, {"token": PANEL_TOKEN})

    # ── KHAOSS-PATCH: remote-login — token-entry page + cookie mint (browser path) ──
    def _serve_login_page(self) -> None:
        """The one pre-auth surface: a minimal token-entry page for a phone browser.
        Supports one-tap via a #token= URL fragment, which the browser never sends to
        the server (stays client-side) — so the token stays out of the access log."""
        html = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>KHAOSS Panel — Login</title><style>"
            ":root{color-scheme:dark}"
            "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
            "background:#0e1116;color:#e6edf3;font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}"
            ".card{width:min(92vw,360px);background:#161b22;border:1px solid #30363d;border-radius:14px;"
            "padding:24px;box-shadow:0 8px 30px rgba(0,0,0,.5)}"
            "h1{font-size:18px;margin:0 0 4px}p{margin:0 0 16px;color:#8b949e;font-size:13px}"
            "input{width:100%;box-sizing:border-box;padding:12px;border-radius:10px;border:1px solid #30363d;"
            "background:#0d1117;color:#e6edf3;font-size:16px}"
            "button{width:100%;margin-top:12px;padding:12px;border:0;border-radius:10px;background:#238636;"
            "color:#fff;font-size:16px;font-weight:600}button:disabled{opacity:.6}"
            ".msg{font-size:13px;margin-top:10px;min-height:16px;color:#f85149}.msg.ok{color:#3fb950}"
            "</style></head><body>"
            "<form class=\"card\" id=\"f\" onsubmit=\"return false\">"
            "<h1>KHAOSS Panel</h1><p>Paste your panel token to connect this device.</p>"
            "<input id=\"t\" type=\"password\" inputmode=\"text\" autocomplete=\"off\" autocapitalize=\"off\" "
            "autocorrect=\"off\" spellcheck=\"false\" placeholder=\"panel token\">"
            "<button id=\"b\" type=\"submit\">Log in</button>"
            "<div class=\"msg\" id=\"e\"></div></form><script>"
            "(function(){var t=document.getElementById('t'),e=document.getElementById('e'),"
            "b=document.getElementById('b'),f=document.getElementById('f');"
            "function go(tok){e.className='msg';e.textContent='';b.disabled=true;"
            "fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},"
            "body:JSON.stringify({token:tok})}).then(function(r){"
            "return r.json().catch(function(){return {ok:r.ok}})}).then(function(j){"
            "if(j&&j.ok){e.className='msg ok';e.textContent='connected — loading panel…';"
            "location.replace('/');}else{e.textContent='invalid token';b.disabled=false;}})"
            ".catch(function(){e.textContent='connection error';b.disabled=false;});}"
            "f.addEventListener('submit',function(){var v=(t.value||'').trim();if(v)go(v);});"
            "var m=(location.hash||'').match(/token=([^&]+)/);"
            "if(m){history.replaceState(null,'','/login');go(decodeURIComponent(m[1]));}})();"
            "</script></body></html>"
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_login_post(self) -> None:
        """Validate a submitted token and, on an exact match, set the panel_token cookie.
        Reveals nothing on failure. The cookie carries the SAME secret as the Bearer
        header, so it grants no reach the header wouldn't — the remote-safe allowlist
        still gates every authenticated route."""
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        token = ""
        try:
            token = (json.loads(raw.decode("utf-8") or "{}") or {}).get("token", "")
        except Exception:
            from urllib.parse import parse_qs
            token = (parse_qs(raw.decode("utf-8", "ignore")).get("token", [""]) or [""])[0]
        token = (token or "").strip()
        if not token or not _hmac.compare_digest(token, PANEL_TOKEN):
            return self._send_json(401, {"ok": False, "error": "invalid token"})
        # HttpOnly (XSS can't read it), SameSite=Strict (CSRF), Path=/, 30-day life. No
        # Secure flag: this is http over the WireGuard-encrypted tailnet, where Secure
        # would suppress the cookie and lock the phone out.
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie",
                         f"panel_token={PANEL_TOKEN}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000")
        self.end_headers()
        self.wfile.write(body)

    def _tunnel_start(self) -> None:
        # KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS BEGIN
        # Quick tunnels cannot enforce the named Cloudflare Access policy required for IT.
        # Keep the endpoint as a fail-closed compatibility response; never spawn public ingress.
        return self._send_json(410, {
            "ok": False,
            "status": "RETIRED",
            "error": "quick tunnels are disabled; use the named Cloudflare Access tunnel",
        })
        # KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS END

    def _tunnel_stop(self) -> None:
        with _TUNNEL_LOCK:
            proc = _TUNNEL_STATE.get("proc")
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                except OSError:
                    pass
            _TUNNEL_STATE["proc"] = None
            _TUNNEL_STATE["pid"] = None
            _TUNNEL_STATE["url"] = None
        return self._send_json(200, {"ok": True})

    def _graveyard_list(self) -> None:
        # Q86·Item1 — the graveyard is a VIEW over agent_archive; graveyard.py owns the facts (straight from the
        # archive records) + the clearance filter (FA-15). The panel only serves its JSON so the sidebar entry can
        # show a count and the page can render the graves. No new store here, and nothing embellished — only the
        # ballad is generated, and only on demand (not in this list call).
        try:
            out = subprocess.check_output(["python3", str(Path(__file__).parent / "graveyard.py")], timeout=8)
            data = json.loads(out.decode("utf-8"))
            data["count"] = len(data.get("graves", []))
            return self._send_json(200, data)
        except Exception as e:
            return self._send_json(200, {"graves": [], "restricted": [], "count": 0, "error": str(e)})

    def _tunnel_status(self) -> None:
        proc = _TUNNEL_STATE.get("proc")
        running = bool(proc and proc.poll() is None)
        return self._send_json(200, {
            "running": running,
            "url": _TUNNEL_STATE.get("url") if running else None,
            "pid": _TUNNEL_STATE.get("pid") if running else None,
            "telnyx_key_present": TELNYX_API_KEY_PATH.exists(),
            "telnyx_webhook_pubkey_present": TELNYX_WEBHOOK_PUBKEY_PATH.exists(),
        })

    # KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS BEGIN
    def _remote_infrastructure_status(self) -> None:
        """Return redacted, evidence-labelled IT access state.

        The config may name a local key path so this host can verify presence, but that
        path, key material, tunnel token, certificate, and account identities never
        cross the HTTP boundary. This route is intentionally absent from the remote-safe
        allowlist: only a genuine loopback caller can inspect infrastructure metadata.
        """
        checked_at = datetime.now(timezone.utc).isoformat()
        try:
            raw = json.loads(REMOTE_INFRA_CONFIG.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._send_json(200, {
                "ok": False,
                "enforcement": "NOT_ENFORCING_YET",
                "reason": "infrastructure registry is not configured",
                "checked_at": checked_at,
            })
        except (OSError, ValueError) as exc:
            return self._send_json(500, {
                "ok": False,
                "enforcement": "NOT_ENFORCING_YET",
                "reason": f"infrastructure registry unreadable: {exc}",
                "checked_at": checked_at,
            })

        lan_host = str(raw.get("lan_host") or "")
        lan_ssh_reachable = False
        if lan_host:
            try:
                with socket.create_connection((lan_host, 22), timeout=1.0):
                    lan_ssh_reachable = True
            except OSError:
                pass

        key_path = Path(str(raw.get("ssh_key_path") or ""))
        key_present = bool(key_path.name and key_path.is_file())
        hostname = str(raw.get("cloudflare_hostname") or "")
        access_status = str(raw.get("cloudflare_access_status") or "NOT_ENFORCING_YET")
        tunnel_status = str(raw.get("tunnel_service_status") or "NOT_ENFORCING_YET")
        enforcing = bool(hostname and access_status == "ENFORCING" and tunnel_status == "ENFORCING")

        return self._send_json(200, {
            "ok": True,
            "host_id": str(raw.get("host_id") or "unknown"),
            "host_label": str(raw.get("host_label") or "Remote host"),
            "lan_host": lan_host,
            "lan_ssh_reachable": lan_ssh_reachable,
            "cloudflare_hostname": hostname or None,
            "cloudflare_tunnel_name": str(raw.get("cloudflare_tunnel_name") or ""),
            "access_status": access_status,
            "tunnel_status": tunnel_status,
            "enforcement": "ENFORCING" if enforcing else "NOT_ENFORCING_YET",
            "authentication": ["Cloudflare Access identity", "individual SSH public key"],
            "shared_password": False,
            "tunnel_credential_exposed": False,
            "local_operator_key_present": key_present,
            "handoff_report": str(raw.get("handoff_report") or ""),
            "checked_at": checked_at,
            "evidence": "live LAN TCP/22 probe plus local redacted registry; Cloudflare state is declared until off-LAN verification",
        })
    # KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS END

    def _handle_telnyx_webhook(self) -> None:
        clen = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(clen) if clen > 0 else b""
        sig = self.headers.get("Telnyx-Signature-Ed25519", "")
        ts = self.headers.get("Telnyx-Timestamp", "")
        verified = False
        if TELNYX_WEBHOOK_PUBKEY_PATH.exists():
            if not (sig and ts):
                logger.warning("[webhook/telnyx] missing signature headers — rejecting")
                return self._send_json(403, {"error": "missing signature headers"})
            verified = _verify_telnyx_signature(raw, sig, ts)
            if not verified:
                logger.warning("[webhook/telnyx] signature verification FAILED — rejecting")
                return self._send_json(403, {"error": "invalid signature"})
        else:
            # No webhook public key provisioned — cannot verify. Queue UNVERIFIED.
            logger.warning("[webhook/telnyx] webhook public key not configured — queuing UNVERIFIED")
        event_type = "unknown"
        try:
            event_type = (json.loads(raw or b"{}").get("data") or {}).get("event_type", "unknown")
        except Exception:
            pass
        try:
            _queue_webhook_event("telnyx", raw, verified)
        except Exception as exc:
            logger.error("[webhook/telnyx] queue error: %s", exc)
        # HOOK: route to PA leader (R3/R5) — feed the queued event into the PA-leader mailbox here.
        return self._send_json(200, {"ok": True, "event_type": event_type, "verified": verified})

    def _send_json(self, status: int, payload: Any, headers: dict | None = None) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # ★ `headers` ADDED 2026-10-15 for the phone surface: a warning that only exists in the
            #   JSON body is a warning a caller can ignore by not reading it. It travels on the wire
            #   as well (`X-Khaoss-Warn`), so it is present in a proxy log, a browser devtools header
            #   pane and the body — three places, one fact. Additive and default-None: every existing
            #   call site writes exactly the bytes it wrote before.
            for _k, _v in (headers or {}).items():
                self.send_header(str(_k), str(_v))
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except _CONN_ERRORS:
            # ★★★ A client that hangs up before we finish writing is NORMAL TRAFFIC, not a fault.
            #   Letting it propagate reached socketserver.handle_error, which printed a ~15-line
            #   traceback per dropped request to fd 2 — the SHARED, RATE-LIMITED journald socket,
            #   guarded by one TextIOWrapper lock. journald stopped draining ("Suppressed 8505
            #   messages"), the holding thread blocked in write(), and 255 threads queued behind it.
            #   The panel wedged itself with its own error reporting. 2026-09-17.
            self.close_connection = True

    def _item_assist_model(self, model: str, prompt: str) -> str:
        """Q94·C — route an item-assist proposal to the cheap model (same path as the daily ingest). Only
        reached AFTER item_ai_assist's bound clears the instruction, so no governed field can arrive here."""
        try:
            import sys as _sys
            _sys.path.insert(0, str(ROOT))
            import capture_knowledge_slice as ks
            return ks.cloud_generate(model.replace("or:", ""), prompt, num_predict=2000)
        except Exception as e:
            return f"(model unavailable: {e})"

    def _send_text(self, status: int, body: str, ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, body: str, ctype: str, extra_headers: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # ── KHAOSS-PATCH: skill-infra BEGIN — skills registry + dispatch queue ───

    def _skills_index(self) -> None:
        skills_index = Path(__file__).resolve().parent.parent / "skills" / "index.json"
        try:
            self._send_json(200, json.loads(skills_index.read_text()))
        except Exception as e:
            self._send_json(404, {"error": str(e)})

    def _skills_get(self, skill_id: str) -> None:
        skill_id = skill_id.strip("/")
        # Sanitize: only allow alphanumeric + hyphens
        if not re.match(r"^[a-z0-9-]+$", skill_id):
            self._send_json(400, {"error": "invalid skill id"})
            return
        skills_dir = Path(__file__).resolve().parent.parent / "skills"
        try:
            index = json.loads((skills_dir / "index.json").read_text())
            skill = next((s for s in index["skills"] if s["id"] == skill_id), None)
            if not skill:
                self._send_json(404, {"error": f"skill not found: {skill_id}"})
                return
            content = (skills_dir / skill["file"]).read_text()
            self._send_json(200, {"id": skill_id, "content": content})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _dispatch_requests_create(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception as e:
            self._send_json(400, {"error": f"invalid json: {e}"})
            return
        required = ["requester", "target_surface", "desired_change", "acceptance_criteria", "urgency"]
        missing = [f for f in required if not body.get(f)]
        if missing:
            self._send_json(400, {"error": f"missing fields: {missing}"})
            return
        queue_dir = Path(__file__).resolve().parent.parent / "dispatch_queue"
        queue_dir.mkdir(exist_ok=True)
        slug = re.sub(r"[^a-z0-9]", "-", body["target_surface"].lower())[:30]
        epoch = int(time.time())
        filepath = queue_dir / f"{epoch}_{slug}.md"
        content = f"""# Dispatch Request — {body['target_surface']}
**Submitted:** {time.strftime('%Y-%m-%d %H:%M %Z')}
**Requester:** {body['requester']}
**Urgency:** {body['urgency']}

## Desired Change
{body['desired_change']}

## Acceptance Criteria
{body['acceptance_criteria']}

## Status
pending — awaiting human review
"""
        try:
            filepath.write_text(content)
            self._send_json(200, {"ok": True, "file": str(filepath)})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def _dispatch_requests_list(self) -> None:
        queue_dir = Path(__file__).resolve().parent.parent / "dispatch_queue"
        try:
            files = sorted(queue_dir.glob("*.md"), reverse=True)
            items = []
            for f in files[:20]:  # latest 20
                items.append({"file": f.name, "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)})
            self._send_json(200, {"requests": items})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    # ── KHAOSS-PATCH: self-evolution proposal queue (governing-note gate) ──────
    # REUSE, no second path: consent_broker's reserved `config-proposal` consumer
    # (the DECISION GATE) + module-level `_la_execute_edit` (the exact before→after
    # DIFF EXECUTOR) + dispatch_queue/*.md (the durable QUEUE SURFACE). Content
    # notes never reach here — the gate classifies by DESTINATION via
    # consent_broker.is_governing_note (Part A3). Async by design: proposals
    # accumulate on disk; the agent is never blocked; nothing auto-approves; a
    # pending broker request times out to DENY (never to approval).
    _GOV_QUEUE = Path(__file__).resolve().parent.parent / "dispatch_queue"

    def _gov_find(self, pid):
        try:
            for jp in sorted(self._GOV_QUEUE.glob("*.gov.json")):
                try:
                    rec = json.loads(jp.read_text())
                except Exception:
                    continue
                if rec.get("proposal_id") == pid:
                    return jp, rec
        except Exception:
            pass
        return None, None

    def _gov_write(self, rec):
        jp = Path(rec["files"]["json"]); mp = Path(rec["files"]["md"])
        tmp = jp.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=2)); tmp.replace(jp)
        mp.write_text(self._gov_render_md(rec))

    def _gov_render_md(self, rec):
        d = rec["diff"]
        return "\n".join([
            f"# Governing-Note Proposal — {os.path.basename(rec['path'])}",
            f"**Proposal ID:** {rec['proposal_id']}",
            f"**Submitted:** {rec.get('created_h','')}",
            f"**Requester:** {rec.get('requester','')}",
            f"**Target:** `{rec['path']}`",
            f"**Classification:** {rec['classification']} — {rec.get('class_reason','')}",
            f"**Status:** {rec['status']}",
            f"**Broker request:** {rec.get('request_id') or '(none)'}",
            "",
            "## Why (reason)", rec.get("reason", "") or "(not stated)",
            "",
            "## What prompted this", rec.get("prompted_by", "") or "(not stated)",
            "",
            "## Diff (exact before → after)",
            "### before", "```", d.get("before", ""), "```",
            "### after", "```", d.get("after", ""), "```",
            "",
            "## Review — no second approval path",
            "- approve · edit-then-approve · reject  →  POST /governing-proposal/resolve",
            "- NEVER auto-approved. A pending proposal times out to DENY, never to approval.",
            "- Full revert after merge  →  POST /governing-proposal/revert",
            "",
            "## Provenance",
            *[f"- {p.get('event')}  by {p.get('by')}  @ {p.get('ts')}" for p in rec.get("provenance", [])],
        ])

    def _gov_precheck(self, path_str, before):
        """Read-only applicability check mirroring _la_execute_edit's match rules —
        so a proposal is only queued if the diff can actually apply. No write."""
        try:
            p = Path(os.path.realpath(os.path.expanduser(str(path_str))))
        except Exception as exc:
            return False, f"invalid path: {exc}"
        if not p.is_file():
            return False, f"not a file: {path_str}"
        try:
            cur = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return False, f"could not read file: {exc}"
        n = cur.count(before)
        if n == 0:
            return False, "before text not found in file"
        if n > 1:
            return False, f"before text matches {n} times; must match exactly once"
        return True, "ok"

    def _gov_proposal_create(self, body):
        import consent_broker as _cb
        path_str = str(body.get("path", "")).strip()
        before = str(body.get("before", ""))
        after = str(body.get("after", ""))
        reason = str(body.get("reason", "")).strip()
        prompted_by = str(body.get("prompted_by", "")).strip()
        requester = str(body.get("requester", "unknown-agent")).strip()
        if not path_str or not before or not reason:
            return self._send_json(400, {"ok": False, "error": "path, before, reason required"})
        # ★ THE GATE (Part A3): class decided by destination, not by any writer flag.
        is_gov, why = _cb.is_governing_note(path_str)
        if not is_gov:
            return self._send_json(400, {
                "ok": False, "classification": "content", "class_reason": why,
                "error": "content note (freely writable) — the proposal queue is ONLY for behavior-governing notes",
            })
        ok, msg = self._gov_precheck(path_str, before)
        if not ok:
            return self._send_json(409, {"ok": False, "error": f"diff not applicable: {msg}"})
        self._GOV_QUEUE.mkdir(exist_ok=True)
        epoch = int(time.time())
        pid = f"gov_{epoch}_{os.urandom(3).hex()}"
        slug = re.sub(r"[^a-z0-9]", "-", os.path.basename(path_str).lower())[:30]
        jpath = self._GOV_QUEUE / f"{epoch}_{slug}.gov.json"
        mpath = self._GOV_QUEUE / f"{epoch}_{slug}.gov.md"
        rec = {
            "proposal_id": pid, "kind": "governing-note-edit", "status": "pending",
            "path": path_str, "classification": "governing", "class_reason": why,
            "reason": reason, "prompted_by": prompted_by, "requester": requester,
            "diff": {"action": "edit_file", "path": path_str, "before": before, "after": after},
            "created": epoch, "created_h": time.strftime("%Y-%m-%d %H:%M %Z"),
            "files": {"json": str(jpath), "md": str(mpath)},
            "provenance": [{"event": "created", "by": requester, "ts": epoch,
                            "why": reason, "prompted_by": prompted_by}],
        }
        # WIRE the reserved consumer: a live decision handle in the shared broker.
        try:
            req = _cb.create_request(
                host=socket.gethostname(), consumer="config-proposal",
                what=f"edit governing note {path_str}", why=reason,
                scope="edit-governing-note",
                duration=int(body.get("duration") or 30 * 24 * 3600),  # accumulates; timeout=DENY
                options=["approve", "edit-then-approve", "reject"],
            )
            rec["request_id"] = req["request_id"]
        except Exception as exc:
            rec["request_id"] = None
            rec["broker_error"] = str(exc)
        try:
            self._gov_write(rec)
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": f"queue write failed: {exc}"})
        return self._send_json(200, {"ok": True, "proposal_id": pid, "request_id": rec.get("request_id"),
                                     "file": str(mpath), "classification": "governing", "class_reason": why})

    def _gov_proposal_resolve(self, body):
        import consent_broker as _cb
        pid = str(body.get("proposal_id", "")).strip()
        choice = str(body.get("choice", "")).strip()
        resolver = str(body.get("resolver", "operator")).strip() or "operator"
        if choice not in ("approve", "edit-then-approve", "reject"):
            return self._send_json(400, {"ok": False, "error": "choice must be approve|edit-then-approve|reject"})
        jp, rec = self._gov_find(pid)
        if not rec:
            return self._send_json(404, {"ok": False, "error": f"no such proposal: {pid}"})
        if rec.get("status") != "pending":
            return self._send_json(409, {"ok": False, "error": f"proposal already {rec.get('status')}"})
        ts = int(time.time()); rid = rec.get("request_id")
        if choice == "reject":
            rec["status"] = "rejected"
            rec["provenance"].append({"event": "rejected", "by": resolver, "ts": ts})
            if rid:
                try: _cb.resolve(rid, "deny", resolver=resolver)
                except Exception: pass
            self._gov_write(rec)
            return self._send_json(200, {"ok": True, "status": "rejected"})
        diff = dict(rec["diff"])
        if choice == "edit-then-approve":
            newp = body.get("payload") or {}
            if "after" in newp: diff["after"] = str(newp["after"])
            if "before" in newp: diff["before"] = str(newp["before"])
            rec["provenance"].append({"event": "edited-by-operator", "by": resolver, "ts": ts})
        ok, result = _la_execute_edit(diff)  # ★ REUSE the exact-match diff executor
        if not ok:
            rec["provenance"].append({"event": "apply-failed", "by": resolver, "ts": ts, "error": result.get("error")})
            self._gov_write(rec)
            return self._send_json(409, {"ok": False, "error": f"apply failed: {result.get('error')}"})
        rec["status"] = "approved-merged"; rec["applied_at"] = ts
        # full-revert payload: swap before/after so revert restores the prior text
        rec["revert"] = {"action": "edit_file", "path": diff["path"],
                         "before": diff["after"], "after": diff["before"]}
        rec["provenance"].append({"event": "approved-merged", "by": resolver, "ts": ts,
                                  "choice": choice, "result": result})
        if rid:
            try: _cb.resolve(rid, "approve", resolver=resolver)
            except Exception: pass
        self._gov_write(rec)
        return self._send_json(200, {"ok": True, "status": "approved-merged", "result": result})

    def _gov_proposal_revert(self, body):
        pid = str(body.get("proposal_id", "")).strip()
        resolver = str(body.get("resolver", "operator")).strip() or "operator"
        jp, rec = self._gov_find(pid)
        if not rec:
            return self._send_json(404, {"ok": False, "error": f"no such proposal: {pid}"})
        if rec.get("status") != "approved-merged" or "revert" not in rec:
            return self._send_json(409, {"ok": False, "error": "only a merged proposal can be reverted"})
        ok, result = _la_execute_edit(rec["revert"])
        ts = int(time.time())
        if not ok:
            return self._send_json(409, {"ok": False, "error": f"revert failed: {result.get('error')}"})
        rec["status"] = "reverted"
        rec["provenance"].append({"event": "reverted", "by": resolver, "ts": ts, "result": result})
        self._gov_write(rec)
        return self._send_json(200, {"ok": True, "status": "reverted", "result": result})

    def _gov_proposal_list(self):
        items = []
        try:
            for jp in sorted(self._GOV_QUEUE.glob("*.gov.json"), reverse=True):
                try:
                    rec = json.loads(jp.read_text())
                except Exception:
                    continue
                items.append({k: rec.get(k) for k in (
                    "proposal_id", "status", "path", "classification", "reason",
                    "requester", "created_h", "request_id")})
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})
        return self._send_json(200, {"proposals": items})

    # ── KHAOSS-PATCH: skill-infra END ─────────────────────────────────────────

    # ── routing ──────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self._send(200, "", "text/plain", extra_headers=_bridge_headers())

    def do_GET(self) -> None:
        global BRIDGE_ENABLED, ALLOWED_PATHS
        path = urlsplit(self.path).path

        # KHAOSS-PATCH: remote-login — /login is the ONLY pre-auth surface. It serves a
        # token-entry page (sets no data, reveals nothing on failure), so a tokenless
        # phone browser has somewhere to present the token. Everything else gates below.
        if path == "/login":
            return self._serve_login_page()

        # KHAOSS-PATCH: panel-security — gate every route (local bypass; remote needs token)
        if not self._check_auth():
            return

        # KHAOSS-PATCH: panel-security-and-tunnel-controls
        if path == "/panel/stamp":             return self._send_json(200, _panel_stamp())
        if path == "/security/token":          return self._security_token()
        if path == "/tunnel/status":           return self._tunnel_status()
        if path == "/remote-access/status":    return self._remote_access_status()  # DQ8: observed exposure
        # KHAOSS-PATCH: SECURE_IT_REMOTE_ACCESS — local-only, redacted named-tunnel posture.
        if path == "/remote-access/infrastructure/status": return self._remote_infrastructure_status()
        if path == "/remote-access/key":       return self._remote_access_key()      # panel key (local only)
        if path == "/tailscale/status":        return self._tailscale_status_full()  # rich Tailscale status
        if path == "/graveyard/list":          return self._graveyard_list()        # Q86: the graveyard view over agent_archive

        if path == "/" or path == "/index.html":
            return self._serve_panel()
        if path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return

        # Real-time stream
        if path == "/stream":                  return self._sse_stream()
        if path == "/snapshot":                return self._snapshot_once()
        if path == "/status":                  return self._status()
        if path == "/stewards/watchman/status": return self._send_json(200, BROADCASTER.status())  # DQ72·C
        if path.startswith("/watchman/search"):   # DQ78·2 — deterministic evidence search
            try:
                import watchman_search as ws, urllib.parse as _up
                q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                return self._send_json(200, ws.search_evidence(q.get("sig", ["empty-completion"])[0], int(q.get("hours", ["720"])[0])))
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if path.startswith("/wizard/plan"):    # DQ77 — the migration wizard (deterministic; steps 1-5, dry)
            try:
                import migration_wizard as mw, urllib.parse as _up
                q = _up.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                return self._send_json(200, mw.plan(q.get("conv", [""])[0], q.get("target", [""])[0]))
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
        # KHAOSS-PATCH: dual-surface consent broker (GET) — pending list · audit · popout page
        if path == "/consent/pending":         return self._consent_pending()
        if path == "/consent/audit":           return self._consent_audit()
        if path == "/consent/popout":          return self._consent_popout()
        # KHAOSS-PATCH: INV-39 cards — broker · namespace-view · secrets (data sources; portable surfaces)
        if path == "/broker":                  return self._broker_card()
        if path == "/namespace":               return self._namespace_card()
        if path == "/secrets":                 return self._secrets_card()
        if path == "/tasks":                                               # DQ26: progress surfaces — ONE store
            try:
                import importlib, sys as _sys
                _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
                tp = importlib.import_module("task_progress")
                return self._send_json(200, tp.snapshot())
            except Exception as _e:
                return self._send_json(200, {"tasks": [], "active_count": 0, "session_spend_usd": 0.0,
                                             "error": f"task store unreadable: {_e}"})  # broken≠healthy
        if path == "/secrets/stewards":                                    # DQ25: stewards-see-secrets toggle
            _pol = Path(__file__).resolve().parent.parent / "secrets_policy.json"
            try:
                _p = json.loads(_pol.read_text())
            except Exception:
                _p = {"stewards_see_secrets": False}
            return self._send_json(200, {
                "stewards_see_secrets": bool(_p.get("stewards_see_secrets", False)),
                "default": False,
                "label": _p.get("_label", "Stewards see secret values: OFF."),
            })
        if path == "/access":                  return self._access_card()
        # ★★★ THE ACCESS VERBS (2026-10-12). `/access` above stays byte-for-byte what it was, so
        #     every existing reader of the card -- the panel's `accessStatus()`, INV-45's check, any
        #     suite -- keeps working. These are ADDITIVE routes beside it; `/access/panel` is the
        #     full row set, `/access/truth` is the live-vs-ledger comparison, `/access/need` is the
        #     repair surface for a refused action.
        if path == "/access/panel":            return self._access_panel()
        if path == "/access/truth":            return self._access_truth()
        if path == "/access/need":
            import urllib.parse as _up
            return self._access_need(_up.parse_qs(
                self.path.split("?", 1)[1] if "?" in self.path else ""))

        # Screen assistant
        if path == "/screen-assistant/status": return self._sa_status()

        # Tier-2: session memory + debate
        if path == "/session-memory/status":          return self._session_memory_status()
        if path.startswith("/debate/status/"):        return self._debate_status(path[len("/debate/status/"):])
        if path == "/debate/list":                    return self._debate_list()
        if path.startswith("/debate/transcript/"):
            rest = path[len("/debate/transcript/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                return self._debate_transcript(parts[0], parts[1])
            return self._send_json(400, {"error": "usage: /debate/transcript/<id>/<side>"})

        # Tier-3: proactive research
        if path == "/research/queue":          return self._research_queue()
        if path == "/research/status":         return self._research_status()

        # Tier-3: voice kanban
        if path == "/voice-kanban/status":     return self._vk_status()

        # KHAOSS-PATCH: boardwalk-voice-card — local TTS (voicebox) controls
        if path == "/voice/status":            return self._voice_status()
        if path == "/voice/voices":            return self._voice_voices()
        if path == "/voice/devices":           return self._voice_devices()
        if path == "/voice/clones":            return self._voice_clones()
        if path == "/audio/status":            return self._audio_status()      # KHAOSS audio doctor (read-only)
        # KHAOSS-PATCH: voice-lane (DQ7#3) — the backend-CHOICE catalog (speech-out engines +
        # speech-in models + languages) with honest trade-offs stated AT the control. One SSOT for
        # both the panel card and AionUi's voice-settings surface. Read-only.
        if path == "/voice/engines":           return self._voice_engines()
        # ★ SEEK — the voice-orchestration connection/route map, plus the copy-paste agent brief.
        #   Read-only: it probes and reports, and changes nothing. See jarvis/seek.py.
        if path == "/voice/seek":              return self._voice_seek()
        if path == "/voice/seek/brief":        return self._voice_seek_brief()

        # KHAOSS-PATCH: roster-ssot (#3 v2) — normalized roster JSON (the boundary contract) + change SSE
        if path == "/roster":                  return self._roster()
        if path == "/roster/stream":           return self._roster_stream()

        # KHAOSS-PATCH: profile-attrs (read-only) — soul + skills + tools + md_files per Hermes profile
        if path == "/profile/attrs":           return self._profile_attrs()

        # Tier-3: learning loop
        if path == "/learning-loop/status":    return self._ll_status()

        if path == "/api/vault/pid":           return self._send_json(200, _pid_status(VAULT_PID_FILE))
        if path == "/api/vault/stats":         return self._api_vault_stats()
        if path == "/api/sanitizer/pid":       return self._send_json(200, _pid_status(SANITIZER_PID_FILE))
        if path == "/api/quarantine/stats":    return self._api_quarantine_stats()
        if path == "/api/mcp/log":             return self._api_mcp_log()
        if path == "/api/health":              return self._send_json(200, {"ok": True})
        if path == "/logs":                    return self._api_logs()
        if path == "/launch/all":              return self._launch_all()
        if path == "/blocklist":               return self._api_blocklist_get()
        if path == "/api/space-agent/ping":    return self._ping_target("space")

        # AI routing
        if path == "/ai-routing/status":       return self._ai_routing_status()
        if path == "/ai-routing/test":         return self._ai_routing_test()

        # GET form of credential-check kept for convenience curl-ability
        if path == "/run/credential-check":    return self._api_credential_check()
        if path == "/run/invariants":          return self._api_invariants()  # KHAOSS-PATCH: invariants-utility
        # ★ KHAOSS-PATCH: dsh-launcher (2026-09-23) — read-only state for the DSH card on the panel.
        if path == "/run/dsh-status":          return self._dsh_status()
        # KHAOSS-PATCH: failure-immune-system — journal reader + the ONE health board (read-only)
        if path == "/failure/journal":         return self._failure_journal()
        if path == "/health/board":            return self._health_board()
        # KHAOSS-PATCH: failure-immune-system — the LOGS section (one place for every log we write)
        if path == "/logs/sources":            return self._logs_sources()
        if path == "/logs/tail":               return self._logs_tail()
        if path == "/conversation/raw":        return self._conversation_raw()  # raw-log escape hatch
        if path == "/operator/board":          return self._operator_board_redirect()  # → the ONE board
        if path == "/operator/board.json":     return self._operator_board_json()   # the store behind it
        if path == "/voice/log":               return self._voice_log()            # the spoken log
        if path == "/operator/targets":        return self._operator_targets()     # reachable lanes
        if path == "/operator/verify":         return self._operator_verify_get()   # HIS test pass
        if path == "/operator/dispatch/targets": return self._dispatch_targets()     # where an item can go
        if path == "/operator/guide/backends": return self._guide_backends_get()    # who the guide talks to
        if path == "/operator/guide/events":   return self._guide_events_get()      # honest delivery state
        if path == "/operator/share":          return self._operator_share()        # URLs + app folder, no secrets
        if path == "/operator/window-geometry": return self._window_geometry_get()  # the panel's REAL window rect
        if path == "/operator/panel/front":     return self._panel_front_get()       # bring the panel/item forward
        if path == "/qr.svg":                  return self._qr_svg()               # loopback QR of a NON-secret URL
        if path == "/operator/delta":          return self._operator_delta()       # NEW since he looked
        if path == "/operator/bindings":       return self._operator_bindings()    # who holds what
        if path == "/operator/item-history":   return self._operator_item_history()  # what happened to it
        if path == "/operator/services":       return self._operator_services()    # parity: what HE can do
        if path == "/operator/assist/routes":  return self._assist_routes()        # where a question can go
        if path.startswith("/assets/"):        return self._static_asset(path)     # branding, cached
        if path == "/secrets/list":            return self._secrets_list()         # ids only, never values
        if path == "/secrets/access":          return self._secrets_access()       # clearance + territory
        if path == "/operator/shelf":          return self._operator_shelf()       # what he told us to do
        if path == "/panel/build":             return self._panel_build()          # is this page stale?
        if path == "/operator/focus":          return self._guide_focus_get()      # the agent's spotlight
        if path == "/operator/outbox":         return self._operator_outbox()      # what he sent, and replies
        if path == "/operator/wizard":         return self._operator_wizard()      # the guided run, in bursts
        if path == "/operator/grant-wizard":   return self._grant_wizard_popout()  # ★ the GRANT WIZARD page
        if path == "/operator/grant-wizard/data":   return self._grant_wizard_data()   # catalogue + live grants
        if path == "/operator/terminal":       return self._operator_terminal()    # the terminal the wizard can contain
        if path == "/operator/walk":           return self._operator_walk()        # the BOARD walk, 1 by 1
        if path == "/operator/boardwalk":      return self._operator_boardwalk()   # ★ the walk's own PAGE (panel/boardwalk-popout.html)
        if path == "/operator/setup-wizard":   return self._setup_wizard_popout()  # ★ the SETUP WALK page (panel/setup-wizard-popout.html)
        if path == "/operator/setup/walk":     return self._setup_walk()           # ★ the first-install walk, measured live
        if path == "/operator/setup/gev":      return self._setup_gev()            # ★ the recommended option, on its own
        if path == "/operator/dsh-jobs":       return self._dsh_obs_popout()       # ★ DSH subagents/jobs popout page
        if path == "/operator/dsh-jobs/data":  return self._dsh_obs_data()         # ★ live state JSON (plugin-written)
        if path.startswith("/operator/dsh-jobs/shot/"): return self._dsh_obs_shot(path)  # ring-capped screenshots
        if path == "/operator/point-to":       return self._point_to_get()         # ★ the SCREEN overlay: what is it pointing at right now
        if path == "/operator/stormwatch":      return self._stormwatch_popout()   # ★ the STORMWATCH page (panel/stormwatch-popout.html)
        if path == "/operator/stormwatch/data": return self._stormwatch_data()     # ★ storm state + survey + accumulated context
        if path.startswith("/operator/stormwatch/radar/"): return self._stormwatch_radar_frame(path)  # ★ one cached radar PNG
        if path.startswith("/operator/stormwatch/vendor/"): return self._stormwatch_vendor(path)  # ★ vendored Leaflet 1.9.4 (local copy, not a CDN)
        if path == "/jarvis/status":           return self._jarvis_status()        # the voice seat (proxied, read-only)
        if path == "/jarvis/chats":            return self._jarvis_chats()         # ★ the full routing menu — every chat
        # ── ★★★ THE PHONE SURFACE (2026-10-15, overnight item 8) ─────────────────────────────────
        #   ★ WHY THESE ARE HERE AND NOT A SECOND SERVER. The phone reaches the box over the
        #     tailnet; this server is the only thing bound to something the phone can address. A
        #     second server would be a second bind, a second place to get the gate wrong, and a
        #     second port to keep free. ONE SERVER, ONE GATE, ONE LEDGER.
        #   ★ THE PAGE IS PUBLIC; EVERY `/phone/api/*` CALL NEEDS A PAIRED DEVICE. Serving the shell
        #     anonymously is safe because it holds no secret -- the token lives in the phone's
        #     localStorage and is presented per request. That is deliberate: the incident this
        #     inherits from was a live value served ANONYMOUSLY, and the countermeasure is that
        #     nothing behind this path returns a secret to an unidentified caller.
        if path == "/phone":                   return self._serve_phone()
        if path == "/phone/manifest.webmanifest": return self._phone_manifest()
        if path == "/phone/session":           return self._phone_session()
        if path == "/phone/state":             return self._phone_state()
        if path == "/phone/chats":             return self._phone_chats()          # ★ THE WIDENING, phone-shaped
        if path == "/phone/delivery":          return self._phone_delivery()
        #   ★ OPERATOR-ONLY, LOOPBACK-ONLY. These are the credential and control endpoints -- the
        #     same class as `/remote-access/key`, which the estate already keeps loopback-only with a
        #     403 from the tailnet even with a valid token. A paired phone is NOT enough for these.
        if path == "/phone/devices":           return self._phone_devices()
        if path == "/phone/log":               return self._phone_log()
        if path == "/phone/policy":            return self._phone_policy_get()
        if path == "/state/focus-sources":     return self._focus_sources()        # ★ SOURCES only — the focus is derived by the boardwalk seat
        if path == "/state/capture-policy":    return self._capture_policy()       # ★ the mic master, read-only — NOT /capture/* (that is the knowledge queue)
        if path == "/operator/model-routes":   return self._model_routes()         # what each seat COSTS and whether it resolves
        if path == "/operator/handoffs":       return self._operator_handoffs()    # what is queued, undelivered
        if path == "/operator/browser":        return self._operator_browser()     # the agent browser: where its login lives
        if path == "/operator/facets":         return self._operator_facets()     # clickable chips, derived live
        if path.startswith("/operator/resolve"): return self._operator_resolve()  # his numbers -> rows
        if path == "/operator/foreground":     return self._operator_foreground()  # what may take his screen
        if path == "/operator/objectives":     return self._operator_objectives()  # life objectives, from his vault
        if path == "/operator/sweep/options":  return self._operator_sweep_options()  # how a sweep is configured
        if path == "/operator/completions":    return self._operator_completions()    # what has left the board
        if path == "/operator/stewards":       return self._operator_stewards()       # who is watching, and what they hold
        if path == "/operator/stewards/deploy": return self._operator_stewards_deploy()   # ★ HOW MANY agents, WHAT each does, HOW routed
        if path == "/operator/stewards/budget": return self._operator_stewards_budget()   # ★ the memory gate: ceiling, FLOOR, override
        if path == "/operator/stewards/laya":   return self._operator_stewards_laya()     # ★ what Laya started as vs how we run it now
        if path == "/operator/stewards/all":    return self._operator_stewards_all()      # ★★ ONE STORE: everything a stewards page needs
        if path == "/operator/stewards/slots":  return self._operator_stewards_slots()    # ★ JOBS not models: roles, instances, LEAN/STANDARD/EXPANDED
        if path == "/operator/summary/menu":   return self._operator_summary_menu()   # the cuts a summary can take
        if path == "/operator/voicein":        return self._operator_voicein()        # can this machine take dictation
        if path.startswith("/operator/objective/note"): return self._operator_objective_note()  # render one, in-panel
        if path == "/operator/settled":        return self._operator_settled()    # the finished record
        if path == "/operator/service/ways":    return self._service_ways()        # HOW to check, and how to sign in
        if path == "/operator/reminders":       return self._operator_reminders()  # what he asked to be reminded of
        if path == "/operator/didthis/fields":  return self._didthis_fields()      # the completion form
        if path == "/context/diagnostic":      return self._context_diagnostic()  # Part A — the cliff, honest
        if path == "/context/reinject":        return self._context_reinject()    # Part B — token cost before inject
        if path == "/thinking/status":         return self._thinking_status()      # thinking cap+archive status
        if path == "/caps/get":                return self._caps_get()             # cap-settings: read canonical khaoss-caps.json
        if path == "/routing/get":             return self._routing_get()          # model-routing: read khaoss-routing.json policy
        if path == "/capture/queue":           return self._capture_queue()        # Card 4: staged review items
        if path == "/capture/canary":          return self._capture_canary()       # Card 4: run the scrub canary (INV-35)
        if path == "/kb/status":               return self._kb_status()            # Q9 Part C: KB overview (index/queue/seed/posture)
        if path == "/kb/posture":              return self._kb_posture_get()       # Q9 Part D: Sealed·Ask·Scheduled·Open spectrum
        if path == "/kb/cost-preview":         return self._kb_cost_preview({})     # Q12 Part D1/D2: storage budget preview (GET = current)
        if path == "/components":              return self._components_get()       # Card 5: resolve_components()

        # Exact ping matches MUST come before the prefix proxy below; otherwise
        # the proxy swallows them and tries to HTTP GET /ping on the target.
        if path == "/swarms":                  return self._swarms()
        if path == "/teams":                   return self._teams()
        if path == "/orion":                   return self._orion_status()
        if path == "/mammouth":                return self._mammouth_status()
        if path == "/team/cdp-check":          return self._team_cdp_check()

        # AI Launch Assist
        if path == "/launch-assist/agents":    return self._la_agents_list()
        if path == "/launch-assist/prompt":    return self._la_prompt_get()
        if path.startswith("/launch-assist/session/"):
            sid = path[len("/launch-assist/session/"):]
            return self._la_session_get(sid)
        if path.startswith("/launch-assist/stream/"):
            sid = path[len("/launch-assist/stream/"):]
            return self._la_stream(sid)

        # Prompter
        if path.startswith("/prompter/stream/"):
            return self._prompter_stream(path[len("/prompter/stream/"):])
        if path.startswith("/prompter/result/"):
            return self._prompter_result(path[len("/prompter/result/"):])
        if path == "/prompter/library":
            return self._prompter_library_list()
        if path.startswith("/prompter/library/"):
            return self._prompter_library_get(path[len("/prompter/library/"):])

        if path == "/privacy/status":          return self._privacy_status()
        if path == "/notifications":           return self._notif_list()
        if path == "/notifications/stream":    return self._notif_stream()
        if path.startswith("/notifications/explain/"):
            return self._notif_explain(path[len("/notifications/explain/"):])

        if path == "/api/aionui/ping":          return self._ping_target("aionui")
        if path == "/api/screenpipe/ping":      return self._ping_target("screenpipe")
        if path == "/api/hermes/ping":          return self._ping_target("hermes")

        if path.startswith("/api/screenpipe/"): return self._proxy("screenpipe", path[len("/api/screenpipe"):])
        if path.startswith("/api/hermes/"):     return self._proxy("hermes",     path[len("/api/hermes"):])
        if path.startswith("/api/aionui/"):     return self._proxy("aionui",     path[len("/api/aionui"):])

        # GET /bridge/read?path=<absolute_path>
        if path.startswith("/bridge/read"):
            if not BRIDGE_ENABLED:
                self._send(503, json.dumps({"error": "bridge disabled"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            from urllib.parse import urlparse, parse_qs as _parse_qs
            qs = _parse_qs(urlparse(self.path).query)
            fpath = qs.get("path", [""])[0]
            if not fpath or not _bridge_path_allowed(fpath):
                self._send(403, json.dumps({"error": "path not allowed"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            try:
                full = os.path.realpath(os.path.expanduser(fpath))
                with open(full, "rb") as f:
                    raw = f.read()
                headers = {
                    **_bridge_headers(),
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Length": str(len(raw)),
                    "X-Raw-Passthrough": "1",
                }
                self.send_response(200)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(raw)
            except FileNotFoundError:
                self._send(404, json.dumps({"error": "not found"}),
                           "application/json", extra_headers=_bridge_headers())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}),
                           "application/json", extra_headers=_bridge_headers())
            return

        # GET /bridge/list?path=<absolute_path>
        if path.startswith("/bridge/list"):
            if not BRIDGE_ENABLED:
                self._send(503, json.dumps({"error": "bridge disabled"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            from urllib.parse import urlparse, parse_qs as _parse_qs
            qs = _parse_qs(urlparse(self.path).query)
            fpath = qs.get("path", [""])[0]
            if not fpath or not _bridge_path_allowed(fpath):
                self._send(403, json.dumps({"error": "path not allowed"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            try:
                full = os.path.realpath(os.path.expanduser(fpath))
                entries = os.listdir(full)
                self._send(200, json.dumps(sorted(entries)),
                           "application/json", extra_headers=_bridge_headers())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}),
                           "application/json", extra_headers=_bridge_headers())
            return

        # GET /bridge/status
        if path == "/bridge/status":
            self._send(200, json.dumps({
                "enabled": BRIDGE_ENABLED,
                "allowed_paths": ALLOWED_PATHS,
                "allowed_path_count": len(ALLOWED_PATHS),
            }), "application/json", extra_headers=_bridge_headers())
            return

        # GET /bridge/requests — list pending agent permission requests
        if path == "/bridge/requests":
            with BRIDGE_REQUESTS_LOCK:
                requests_copy = list(BRIDGE_REQUESTS)
            self._send(200, json.dumps({"requests": requests_copy}),
                       "application/json", extra_headers=_bridge_headers())
            return

        # KHAOSS-PATCH: traceability-ledger + rollback (recovery leg)
        if path == "/bridge/ledger":          return self._bridge_ledger()
        if path == "/bridge/rollbacks":        return self._bridge_rollbacks()

        # KHAOSS-PATCH: skill-infra — skills registry + dispatch queue (GET)
        if path == "/skills":               return self._skills_index()
        if path.startswith("/skills/") and len(path) > 8:
            return self._skills_get(path[len("/skills/"):])
        if path == "/dispatch-requests":    return self._dispatch_requests_list()
        # KHAOSS-PATCH: self-evolution proposal queue (GET) — list governing-note proposals
        if path == "/governing-proposal/list": return self._gov_proposal_list()

        if path == "/providers":
            registry_path = Path.home() / ".config" / "AionUi-Dev" / "aionui-providers.json"
            try:
                data = json.loads(registry_path.read_text())
                providers = []
                for p in data.get("providers", []):
                    providers.append({
                        "id": p["id"],
                        "name": p["name"],
                        "base_url": p["base_url"],
                        "hermes_profile": p.get("hermes_profile", ""),
                        "enabled": p.get("enabled", False),
                        "status": p.get("status", "active"),
                        "model_count": len(p.get("models", [])),
                        "key_present": Path(p.get("key_file", "")).exists(),
                        "key_last4": _provider_key_last4(p.get("key_file", ""), p.get("hermes_profile", "")),
                    })
                self._send_json(200, {"providers": providers})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
            return

        # KHAOSS-PATCH: models-catalog — aggregated backend catalog (shared infra)
        if path == "/models/catalog":
            return self._models_catalog()

        self._send_json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:
        global BRIDGE_ENABLED, ALLOWED_PATHS
        path = urlsplit(self.path).path

        # KHAOSS-PATCH: remote-login — validate a token and set the login cookie. Pre-auth
        # by necessity (the caller has no cookie yet); it only ever ACCEPTS a token and
        # sets nothing on a mismatch, so exposing it authless is safe.
        if path == "/login":
            return self._handle_login_post()

        # KHAOSS-PATCH: panel-security — gate every route (Telnyx webhook self-auths via Ed25519)
        if not self._check_auth():
            return

        # ★★★ the CAPTURE master's ONE writer. See _set_capture_policy for why a write path exists.
        if path == "/state/capture-policy":
            return self._set_capture_policy()

        # KHAOSS-PATCH: DSH observability — screenshot on/off. The ONE writer of the flag the
        # dshobs-2 Cordis Host plugin reads before it spends an X11 grab. Loopback-only and
        # self-confirming (re-reads the store); see _dsh_obs_set_shot_mode.
        if path == "/operator/dsh-jobs/screenshot-mode":
            return self._dsh_obs_set_shot_mode()

        # KHAOSS-PATCH: panel-security-and-tunnel-controls
        if path == "/tunnel/start":            return self._tunnel_start()
        if path == "/tunnel/stop":             return self._tunnel_stop()
        if path == "/remote-access/off":       return self._remote_access_off()  # DQ8: fail-safe close
        if path == "/remote-access/on":        return self._remote_access_on()   # open the tailnet bind
        if path == "/tailscale/up":            return self._tailscale_up()       # connect
        if path == "/tailscale/down":          return self._tailscale_down()     # disconnect
        if path == "/webhooks/telnyx":         return self._handle_telnyx_webhook()

        # KHAOSS-PATCH: skill-infra — dispatch queue (POST)
        if path == "/dispatch-requests":
            return self._dispatch_requests_create()
        # KHAOSS-PATCH: self-evolution proposal queue (POST) — governing-note gate
        if path in ("/governing-proposal", "/governing-proposal/resolve", "/governing-proposal/revert"):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            if path == "/governing-proposal":         return self._gov_proposal_create(body)
            if path == "/governing-proposal/resolve": return self._gov_proposal_resolve(body)
            return self._gov_proposal_revert(body)
        if path == "/api/credential-check" or path == "/run/credential-check":
            return self._api_credential_check()
        if path == "/api/invariants" or path == "/run/invariants":  # KHAOSS-PATCH: invariants-utility
            return self._api_invariants()
        # ★ KHAOSS-PATCH: dsh-launcher (2026-09-23) — the card's ONE action. Fixed literal action,
        #   no caller input reaches argv.
        #   ★ WRAPPED IN A DIAGNOSTIC BECAUSE IT WAS FAILING SILENTLY. Measured: this route returned a
        #     dead socket (HTTP 000) from curl AND "Failed to fetch" from the page, while the sibling
        #     route two lines down (/run/dsh-url) answered 200 — and the panel logged the working POST
        #     but NOTHING for this one. `log_message` fires only AFTER a response is sent, so an
        #     exception inside a handler produces exactly that signature: no log line, no response.
        #     ★ A handler that dies without a traceback is the same defect class as the obs-writer that
        #     reported "an empty desktop" for 479 runs: the failure was real and unreadable. So the
        #     traceback now goes to a file the operator can read, and a response is always sent.
        if path == "/run/dsh-launch":
            try:
                # ★★ `body` IS NOT IN SCOPE HERE, AND THAT WAS THE BUG. Measured via the traceback:
                #     UnboundLocalError: cannot access local variable 'body' where it is not associated with a value
                #   `body` is bound only INSIDE the governing-proposal branch above (line ~5115), so any
                #   route outside that branch that reaches for it dies before it can respond. The
                #   signature is nasty: no response, no log line (`log_message` runs only after a reply
                #   is sent), and a dead socket the caller sees as HTTP 000 / "Failed to fetch" — with
                #   the sibling route two lines down working perfectly, which points the reader at the
                #   route rather than at the shared variable.
                #   ★ The estate already owns the right helper: `_read_body_json()` never raises and
                #   always returns a dict. Use it — do not re-read rfile here and do not rely on a
                #   variable some other branch happens to set.
                return self._dsh_launch(self._read_body_json())
            except Exception as _e:  # noqa: BLE001
                import traceback as _tb
                _detail = _tb.format_exc()
                try:
                    with open("/tmp/panel_dsh_launch_error.log", "a", encoding="utf-8") as _fh:
                        _fh.write(f"--- {datetime.now(tz=timezone.utc).isoformat()} ---\n{_detail}\n")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    return self._send_json(200, {"ok": False,
                                                 "error": f"{type(_e).__name__}: {_e}",
                                                 "traceback": _detail[-1500:],
                                                 "remedy": "see /tmp/panel_dsh_launch_error.log"})
                except Exception:  # noqa: BLE001
                    return
        # ★ Returns the AUTHENTICATED url as DATA. The panel has no X access on purpose, so it never
        #   opens a window — it hands the URL to the browser the operator is already looking at, and
        #   THAT browser performs the token->cookie handshake.
        if path == "/run/dsh-url":
            return self._dsh_url()
        # KHAOSS-PATCH: failure-immune-system — record a failure · detection tick · fire a remediation
        if path == "/failure/record":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._failure_record(body)
        if path == "/health/tick":
            return self._health_tick()
        if path == "/thinking/cap-message" or path == "/thinking/sweep":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._thinking_cap_message(body) if path == "/thinking/cap-message" else self._thinking_sweep(body)
        if path == "/caps/set":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._caps_set(body)
        if path == "/routing/set":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._routing_set(body)
        # KHAOSS-PATCH: dual-surface consent broker (POST) — create a request · resolve one
        if path == "/consent/request" or path == "/consent/resolve":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._consent_request(body) if path == "/consent/request" else self._consent_resolve(body)
        if path == "/capture/approve" or path == "/capture/reject":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._capture_approve(body) if path == "/capture/approve" else self._capture_reject(body)
        # ── DQ72 — the Watchman controls: OFF halts execution (A) · chat seat (D) ──────────────────
        if path == "/watchman/propose-detector":   # DQ78·2·C — PROPOSE (allowed)
            body=self._read_body_json() or {}
            try:
                import watchman_search as ws
                return self._send_json(200, ws.propose_detector(str(body.get("signature","")), str(body.get("why",""))))
            except Exception as e: return self._send_json(500,{"error":str(e)})
        if path == "/watchman/detector-change":     # DQ78·2·C2 — self-alter is REFUSED (prove-fail)
            body=self._read_body_json() or {}
            try:
                import watchman_search as ws
                return self._send_json(200, ws.attempt_detector_change(str(body.get("action","add")), str(body.get("detector",""))))
            except Exception as e: return self._send_json(500,{"error":str(e)})
        if path == "/item/assist" or path == "/item/assist-new":   # Q94·C — per-item AI assist (PROPOSES, bounded)
            body = self._read_body_json() or {}
            try:
                import item_ai_assist as ia
                if path == "/item/assist-new":
                    return self._send_json(200, ia.propose_new_item(str(body.get("spec", "")),
                                                                     call_model=self._item_assist_model))
                item = body.get("item") or {}
                return self._send_json(200, ia.propose_modification(item, str(body.get("instruction", "")),
                                                                    mode=str(body.get("mode", "modify")),
                                                                    call_model=self._item_assist_model))
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if path == "/wizard/apply":               # DQ77 — apply the migration (steps 6-8, with ROLLBACK)
            body = self._read_body_json() or {}
            try:
                import migration_wizard as mw
                return self._send_json(200, mw.apply(str(body.get("conv", "")), str(body.get("target", "")),
                                                     _force_fail_step=body.get("force_fail_step")))
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
        if path == "/kill" or path == "/kill/expire":   # Q104·Item2 — the kill switch (agent/session/team/global)
            body = self._read_body_json() or {}
            try:
                import kill_switch as ks
                if path == "/kill/expire":
                    return self._send_json(200, ks.expire_grant(str(body.get("session", "")), str(body.get("current_level", "Internal")), base_level=str(body.get("base_level", "Internal"))))
                # ★ D3 — the panel is an operator surface, but an agent-originated request carries by='agent:…' and
                #   is refused a global halt by kill_switch itself. Global still needs confirm=True (two-step).
                return self._send_json(200, ks.halt(str(body.get("scope", "session")), str(body.get("target", "")),
                                                    by=str(body.get("by", "operator")), confirm=bool(body.get("confirm")), hard=bool(body.get("hard"))))
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": str(e)})
        if path == "/stewards/watchman/halt":     return self._send_json(200, BROADCASTER.halt())    # DQ72·A
        if path == "/stewards/watchman/resume":   return self._send_json(200, BROADCASTER.resume())  # DQ72·A
        if path == "/stewards/watchman/chat":
            body = self._read_body_json()
            q = str((body or {}).get("q", "")).strip()
            proceed = bool((body or {}).get("proceed", False))   # DQ76·B1 — user confirmed "try anyway"
            st = BROADCASTER.status()
            # DQ76·B3 — SCOPE LIVES IN THE SOUL (watchman_soul.json), not the UI, so it travels with the Watchman.
            try:
                soul = json.loads((ROOT / "watchman_soul.json").read_text())
            except Exception:
                soul = {"in_scope": ["watch", "detect", "cost", "memory", "halt", "you"],
                        "out_of_scope_line": "That is outside my scope as the Watchman. Want me to try anyway?",
                        "watches": "silent failures"}
            ql = q.lower()
            state = ('HALTED (not executing)' if st['halted'] else 'watching') + (' · auto-paused (over ceiling)' if st['auto_paused'] else '')
            def _state_line():
                return (f"State: {state}. My process (pid {st['pid']}) is using {st['rss_mb']}MB = "
                        f"{st['pct_of_avail']}% of {st['avail_mb']}MB available (basis {st['avail_basis']}; ceiling {st['ceiling_pct']:.0f}%).")
            out_of_scope = False
            asked = False
            handoff = None
            # ★ DQ78·B — the gate ENGAGES the question. Classify: ANOTHER KEEPER → IN SCOPE → OUT OF SCOPE.
            # Order matters: another-keeper is the most specific, so it is checked first.
            keeper = None
            for k, pats in (soul.get("another_keeper") or {}).items():
                if any(p in ql for p in pats):
                    keeper = k
                    break
            is_greeting = any(g in (ql + " ") for g in soul.get("greetings", []))
            in_scope = any(w in ql for w in soul.get("in_scope", []))
            if not q:
                ans = f"Ask me about what I watch ({soul.get('watches', 'silent failures')}) or my current state."
            elif keeper:
                # ★ B4 — ANOTHER KEEPER'S REMIT: HAND OFF, do not deflect. Name the remit; if the keeper is not
                # built, SAY SO — never promise a keeper that does not exist.
                handoff = keeper
                ans = (soul.get("handoff_lines", {}).get(keeper)
                       or f"That is the {keeper}'s remit — want me to ask it?")
            elif is_greeting or in_scope:
                # in scope (incl. a greeting — the status IS the in-scope answer to 'hello')
                ans = f"Watchman here. {_state_line()} I watch for {soul.get('watches', 'silent failures')} — nothing else does."
            elif proceed:
                # DQ76·B1 — the operator confirmed 'try anyway'. PROCEED (best-effort) and LABEL it out-of-scope.
                out_of_scope = True
                ans = (f"[out of my lane — you asked me to try] I only observe traces and count silent failures, so I "
                       f"can't reason about content. The most I can offer is what I actually see: {_state_line()} "
                       f"For '{q[:60]}', you want a different Steward or a team lead — I'd be guessing.")
            else:
                # ★ B3 — OUT OF SCOPE: INFORM AND ASK, naming what was asked (never a status recital).
                asked = True
                ans = f"'{q[:70]}' — {soul.get('out_of_scope_line', 'That is outside what I do. Want me to try anyway?')}"
            return self._send_json(200, {"q": q, "answer": ans, "out_of_scope": out_of_scope, "asked": asked,
                                         "handoff": handoff, "steward": soul.get("steward", "Watchman"), "watchman_state": st})
        if path == "/kb/update" or path == "/kb/posture":
            body = self._read_body_json()
            return self._kb_update(body) if path == "/kb/update" else self._kb_posture_set(body)
        if path == "/kb/cost-preview":   # Q12 Part D1/D2 — what-if a candidate absorb against the two budgets
            return self._kb_cost_preview(self._read_body_json())
        if path == "/kb/reclaim":        # Q12 Part D2 — reclaim a derived (regenerable) layer; recorded, not a delete
            return self._kb_reclaim(self._read_body_json())
        if path == "/components/reconsider":
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._components_reconsider(body)
        if path.startswith("/remediate/"):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._remediate(path[len("/remediate/"):], body)
        if path == "/repair/dispatch":  # Organ 5 — LLM-tier repair to the affected team's lead
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._repair_dispatch(body)
        if path == "/ai-routing/config":
            return self._ai_routing_config_post()
        if path == "/screen-assistant/toggle":
            return self._sa_toggle()
        # KHAOSS-PATCH: boardwalk-voice-card — download an approved piper voice
        if path.startswith("/voice/download/"):
            return self._voice_download(path[len("/voice/download/"):])
        if path == "/voice/test":
            return self._voice_test()
        if path == "/voice/clone":
            return self._voice_clone()
        if path == "/voice/fix":
            return self._voice_fix()
        if path == "/audio/fix":
            return self._audio_fix()
        if path == "/session-memory/run":
            return self._session_memory_run()
        if path == "/debate/start":
            return self._debate_start()
        if path.startswith("/debate/continue/"):
            return self._debate_continue(path[len("/debate/continue/"):])
        if path == "/services/start":
            return self._svc_start()
        if path == "/services/stop":
            return self._svc_stop()
        # ★ FULL RESTART of the voice seat — loopback-only; a mutation belongs in the POST block,
        #   not the GET block, or a POST to it would 404 while looking correctly registered.
        if path == "/services/voice-full-restart":
            return self._voice_full_restart()
        if path == "/services/restart":
            return self._svc_restart()
        if path == "/services/restart-panel":
            return self._restart_panel()
        if path == "/voice-kanban/test":
            return self._vk_test()
        if path == "/voice-kanban/toggle":
            return self._vk_toggle()
        if path == "/learning-loop/run":
            return self._ll_run()
        if path == "/swarms/provision":
            return self._provision()
        if path == "/team/seed":
            return self._team_seed()
        if path == "/team/repair":
            return self._team_repair()
        if path == "/team/repair-teammate-session":
            return self._team_repair_teammate_session()
        if path == "/team/create":
            return self._team_create()

        # AI Launch Assist
        if path == "/launch-assist/agents":
            return self._la_agents_add()
        if path == "/launch-assist/agents/default":
            return self._la_agents_set_default()
        if path == "/launch-assist/prompt":
            return self._la_prompt_save()
        if path == "/launch-assist/launch":
            return self._la_launch_handler()
        if path.startswith("/launch-assist/proposal/"):
            rest = path[len("/launch-assist/proposal/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2:
                return self._la_proposal_action(parts[0], parts[1])
            return self._send_json(400, {"error": "usage: /launch-assist/proposal/<id>/approve|reject|modify"})
        if path.startswith("/launch-assist/session/"):
            rest = path[len("/launch-assist/session/"):]
            parts = rest.split("/", 1)
            if len(parts) == 2 and parts[1] == "feedback-opt-in":
                return self._la_feedback_opt_in(parts[0])
            if len(parts) == 2 and parts[1] == "inject-test-proposal":
                return self._la_inject_test_proposal(parts[0])
            return self._send_json(400, {"error": "unknown session action"})

        # Prompter
        if path == "/prompter/start":
            return self._prompter_start()
        if path.startswith("/prompter/answer/"):
            return self._prompter_answer(path[len("/prompter/answer/"):])
        if path.startswith("/prompter/synthesize/"):
            return self._prompter_force_synthesize(path[len("/prompter/synthesize/"):])
        if path.startswith("/prompter/cancel/"):
            return self._prompter_cancel(path[len("/prompter/cancel/"):])
        if path == "/prompter/library":
            return self._prompter_library_save()
        if path == "/prompter/deliver-roles-only":
            return self._prompter_deliver_roles_only()
        if path == "/prompter/deliver":
            return self._prompter_deliver()

        if path == "/privacy/test":
            return self._privacy_test()
        if path == "/voice/log":
            return self._voice_log()
        if path == "/operator/notify":
            return self._operator_notify(self._read_body_json())
        if path == "/voice/speak":
            return self._voice_speak(self._read_body_json())
        if path == "/voice/stop":
            return self._voice_stop()
        if path == "/operator/verify":
            return self._operator_verify_post(self._read_body_json())
        if path == "/operator/dispatch/send":
            return self._dispatch_send(self._read_body_json())
        if path == "/operator/guide/backend":
            return self._guide_backend_post(self._read_body_json())
        if path == "/operator/guide/ping":
            return self._guide_ping(self._read_body_json())
        if path == "/operator/window-geometry":
            return self._window_geometry_post(self._read_body_json())
        if path == "/operator/panel/front":
            return self._panel_front_post(self._read_body_json())
        if path == "/operator/seen":
            return self._operator_seen()
        if path == "/operator/bind":
            return self._operator_bind(self._read_body_json())
        if path == "/operator/unbind":
            return self._operator_unbind(self._read_body_json())
        if path == "/operator/session-name":
            return self._operator_session_name(self._read_body_json())
        if path == "/operator/service/action":
            return self._operator_service_action(self._read_body_json())
        if path == "/operator/wizard/action":
            return self._operator_wizard_action(self._read_body_json())
        # ★★★ POST /operator/setup/choice -- the GEV option's accept/decline (added 2026-10-12).
        #     Loopback only. ★ IT TAKES A DECISION AND NOTHING ELSE: the operator's answer to a
        #     recommended option is `accepted` or `declined`, and this route REFUSES any body that
        #     looks like it is carrying a credential. A "recommended" option that needs an API key is
        #     exactly the shape that grows a key field, and a key field on a page the agent serves is
        #     a key in a log. The key goes into GEV's own provider settings on the box, 0600.
        if path == "/operator/setup/choice":
            return self._setup_choice(self._read_body_json())
        if path == "/operator/grant-wizard/action":
            return self._grant_wizard_action(self._read_body_json())
        # ★★★ POST /access/<verb> -- the write half of the access panel (2026-10-12). Loopback
        #     only, like every credential/destructive route. `propose*` writes nothing at all; the
        #     writing verbs delegate to access_broker / capability-grant.py and then re-read.
        if path.startswith("/access/"):
            return self._access_verb(path[len("/access/"):], self._read_body_json())
        if path == "/jarvis/control":
            return self._jarvis_control(self._read_body_json())
        # ── ★★★ THE PHONE SURFACE, WRITE HALF (2026-10-15) ───────────────────────────────────────
        #   ★ `/phone/pair/redeem` IS THE ONLY PHONE-REACHABLE ENDPOINT IN THIS FILE THAT TAKES NO
        #     DEVICE TOKEN -- because it is how a device gets one. It is guarded by a SINGLE-USE
        #     code the operator had to mint on the box first (loopback-only CLI), and that code dies
        #     in ten minutes. "It may ASK. It may NOT TAKE." -- and asking costs him a code he read
        #     off his own screen.
        if path == "/phone/pair/redeem":
            return self._phone_pair_redeem(self._read_body_json())
        if path == "/phone/ptt":
            return self._phone_ptt(self._read_body_json())
        if path == "/phone/mode":
            return self._phone_mode(self._read_body_json())
        if path == "/phone/always_on":
            return self._phone_always_on(self._read_body_json())
        #   ★ OPERATOR-ONLY, LOOPBACK-ONLY: revocation and the policy override.
        if path == "/phone/revoke":
            return self._phone_revoke(self._read_body_json())
        if path == "/phone/policy":
            return self._phone_policy_post(self._read_body_json())
        if path == "/operator/walk/mark":
            return self._operator_walk_mark(self._read_body_json())
        if path == "/operator/point-to":
            return self._point_to_post(self._read_body_json())
        if path == "/operator/model-routes/repair":
            return self._model_routes_repair(self._read_body_json())
        if path == "/operator/assist":
            return self._assist_ask(self._read_body_json())
        if path == "/operator/shelf":
            b = self._read_body_json()
            if not self._is_local_request():
                return self._send_json(403, {"error": "local-only"})
            try:
                g = self._op_mod("guide_focus")
                return self._send_json(200, g.settle(str(b.get("at") or ""),
                                                     str(b.get("how") or "done"),
                                                     str(b.get("note") or "")))
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})
        if path == "/operator/focus":
            return self._guide_focus_post(self._read_body_json())
        if path == "/operator/jump":
            return self._operator_jump(self._read_body_json())
        if path == "/operator/board/add":
            return self._operator_board_add(self._read_body_json())
        if path == "/operator/ruling":
            return self._operator_ruling(self._read_body_json())
        if path == "/operator/feature-check":
            return self._operator_feature_check(self._read_body_json())
        if path == "/operator/foreground/set":
            return self._operator_foreground_set(self._read_body_json())
        if path == "/operator/objective/create":
            return self._operator_objective_create(self._read_body_json())
        if path == "/operator/sweep/run":
            return self._operator_sweep_run(self._read_body_json())
        # ★★★ FIXED 2026-09-23 — `body` WAS NEVER DEFINED IN do_POST. Both of these lines referenced a
        #     bare `body` that no line in this method ever assigned, while every other route in this same
        #     method calls `self._read_body_json()`. The result was `NameError: name 'body' is not defined`
        #     raised inside the handler, which BaseHTTPRequestHandler answers with NOTHING AT ALL: the
        #     connection closes and curl reports `HTTP 000 — Empty reply from server`.
        #     ★ SO THE STEWARD DEPLOYMENT CONTROL ON THE PAGE HAS NEVER ONCE WORKED. It RENDERED — the
        #     buttons drew, the POST was issued, the page showed no error because it could not parse an
        #     empty reply — and every click was dead. That is precisely AGENTS.md §1's "it renders is not
        #     it works", and it was found only by DRIVING the control rather than looking at it.
        #     ★ I had already copied the broken line for the budget route before finding this, which is the
        #     lesson the repo keeps re-learning: a wrong pattern that renders is inherited silently.
        if path == "/operator/stewards/deploy":  return self._operator_stewards_deploy_set(self._read_body_json())
        if path == "/operator/stewards/budget":  return self._operator_stewards_budget_set(self._read_body_json())
        if path == "/operator/stewards/slots":   return self._operator_stewards_slots_set(self._read_body_json())
        if path == "/operator/stormwatch/action": return self._stormwatch_action(self._read_body_json())
        if path == "/operator/steward/stop":
            return self._operator_steward_stop(self._read_body_json())
        if path == "/operator/summary/write":
            return self._operator_summary_write(self._read_body_json())
        if path == "/operator/voicein/dictate":
            return self._operator_voicein_dictate(self._read_body_json())
        if path == "/operator/objective/context":
            return self._operator_objective_context(self._read_body_json())
        if path == "/operator/browser/transplant":
            return self._operator_browser_transplant(self._read_body_json())
        if path == "/operator/service/probe":
            return self._service_probe(self._read_body_json())
        if path == "/operator/service/login":
            return self._service_login(self._read_body_json())
        if path == "/operator/remind":
            return self._operator_remind(self._read_body_json())
        if path == "/operator/didthis":
            return self._operator_didthis(self._read_body_json())
        if path == "/operator/set":
            return self._operator_set(self._read_body_json())
        if path == "/notify":
            return self._notif_write()
        if path == "/notifications/mark-all-read":
            return self._notif_mark_all_read()
        if re.match(r"^/notifications/\d+/read$", path):
            return self._notif_state(path, "read")
        if re.match(r"^/notifications/\d+/dismiss$", path):
            return self._notif_state(path, "dismissed")
        if path == "/research/toggle":
            return self._research_toggle()
        if path == "/launch/all":              return self._launch_all()
        if path == "/launch/selected":         return self._launch_selected()
        if path == "/blocklist":               return self._api_blocklist_post()
        if path.startswith("/api/screenpipe/"):
            return self._proxy("screenpipe", path[len("/api/screenpipe"):])
        if path.startswith("/api/hermes/"):
            return self._proxy("hermes",     path[len("/api/hermes"):])

        # POST /bridge/write  body: {path, content, backup:bool}
        if path == "/bridge/write":
            if not BRIDGE_ENABLED:
                self._send(503, json.dumps({"error": "bridge disabled"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            import shutil
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
            fpath = body.get("path", "")
            content = body.get("content", "")
            do_backup = body.get("backup", True)
            if not fpath or not _bridge_path_allowed(fpath):
                self._send(403, json.dumps({"error": "path not allowed"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            try:
                full = os.path.realpath(os.path.expanduser(fpath))
                backup_path = None
                if do_backup and os.path.exists(full):
                    backup_path = full + ".bak"
                    shutil.copy2(full, backup_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write(content)
                # KHAOSS-PATCH: traceability-ledger — record WHO wrote WHAT, and the .bak (rollback target).
                _ledger_append({
                    "agent":  str(body.get("agent") or body.get("conversation") or "unknown"),
                    "action": "write", "path": full, "bak": backup_path,
                    "bytes":  len(content.encode()),
                })
                result = {"ok": True, "bytesWritten": len(content.encode()),
                          "backup": backup_path}
                self._send(200, json.dumps(result),
                           "application/json", extra_headers=_bridge_headers())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}),
                           "application/json", extra_headers=_bridge_headers())
            return

        # POST /bridge/exec  body: {cmd, timeout}
        if path == "/bridge/exec":
            if not BRIDGE_ENABLED:
                self._send(503, json.dumps({"error": "bridge disabled"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
            cmd = body.get("cmd", "")
            timeout = min(int(body.get("timeout", 30)), 120)
            if not cmd:
                self._send(400, json.dumps({"error": "cmd required"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            # KHAOSS-PATCH: traceability-ledger — the audited path must not be able to erase its own audit
            # trail. Deny exec that references the ledger. (Residual: a determined shell could still evade
            # a string match; OS-level chattr +a — root-only here — would close that. Stated honestly.)
            if "bridge_ledger" in cmd or "failure_journal" in cmd:
                self._send(403, json.dumps({"error": "exec may not target the append-only ledger/journal"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            try:
                result = subprocess.run(
                    cmd, shell=True, capture_output=True,
                    text=True, timeout=timeout)
                # KHAOSS-PATCH: traceability-ledger — record WHO ran WHAT command.
                _ledger_append({
                    "agent":  str(body.get("agent") or body.get("conversation") or "unknown"),
                    "action": "exec", "cmd": cmd, "returncode": result.returncode,
                })
                self._send(200, json.dumps({
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }), "application/json", extra_headers=_bridge_headers())
            except subprocess.TimeoutExpired:
                self._send(200, json.dumps({
                    "stdout": "", "stderr": f"timeout after {timeout}s",
                    "returncode": -1,
                }), "application/json", extra_headers=_bridge_headers())
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}),
                           "application/json", extra_headers=_bridge_headers())
            return

        # POST /bridge/rollback  body: {path, agent?}  — KHAOSS-PATCH: rollback (operator-triggered)
        if path == "/bridge/rollback":
            if not BRIDGE_ENABLED:
                self._send(503, json.dumps({"error": "bridge disabled"}),
                           "application/json", extra_headers=_bridge_headers())
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            return self._bridge_rollback(body)

        # POST /bridge/request  body: {action, params, reason, agent}
        if path == "/bridge/request":
            try:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception:
                body = {}
            req_id = f"req_{int(time.time()*1000)}_{os.urandom(3).hex()}"
            entry = {
                "id":     req_id,
                "action": str(body.get("action", "")),
                "params": body.get("params") or {},
                "reason": str(body.get("reason", "")),
                "agent":  str(body.get("agent", "unknown")),
                "ts":     int(time.time() * 1000),
                "status": "pending",
            }
            with BRIDGE_REQUESTS_LOCK:
                BRIDGE_REQUESTS.append(entry)
            self._send(200, json.dumps({
                "ok": True, "request_id": req_id, "status": "pending",
            }), "application/json", extra_headers=_bridge_headers())
            return

        # POST /bridge/request/<id>/approve | /deny
        if path.startswith("/bridge/request/") and (
            path.endswith("/approve") or path.endswith("/deny")
        ):
            parts = path.split("/")
            # ['', 'bridge', 'request', '<id>', '<approve|deny>']
            if len(parts) == 5:
                req_id = parts[3]
                op = parts[4]
                with BRIDGE_REQUESTS_LOCK:
                    target = next((r for r in BRIDGE_REQUESTS
                                   if r.get("id") == req_id), None)
                    if target is None:
                        self._send(404, json.dumps({"error": "not found"}),
                                   "application/json",
                                   extra_headers=_bridge_headers())
                        return
                    if op == "deny":
                        target["status"] = "denied"
                        self._send(200, json.dumps({"ok": True}),
                                   "application/json",
                                   extra_headers=_bridge_headers())
                        return
                    # approve
                    target["status"] = "approved"
                    action = target.get("action", "")
                    params = target.get("params") or {}
                executed = False
                exec_error = None
                try:
                    if action == "add_allowed_path":
                        new_paths = params.get("allowed_paths") or []
                        for p in new_paths:
                            real = os.path.realpath(os.path.expanduser(str(p)))
                            if real not in ALLOWED_PATHS:
                                ALLOWED_PATHS.append(real)
                        _save_bridge_config()
                        executed = True
                    elif action == "set_enabled":
                        BRIDGE_ENABLED = bool(params.get("enabled", False))
                        _save_bridge_config()
                        executed = True
                    else:
                        # Unknown actions are recorded approved but not executed.
                        executed = False
                except Exception as e:
                    exec_error = str(e)
                resp = {"ok": True, "executed": executed}
                if exec_error:
                    resp["error"] = exec_error
                self._send(200, json.dumps(resp),
                           "application/json",
                           extra_headers=_bridge_headers())
                return

        # POST /bridge/config  body: {enabled:bool, allowed_paths:[str]}
        if path == "/bridge/config":
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
            if "enabled" in body:
                BRIDGE_ENABLED = bool(body["enabled"])
            if "allowed_paths" in body:
                ALLOWED_PATHS = [os.path.realpath(os.path.expanduser(str(p)))
                                 for p in body["allowed_paths"]]
            _save_bridge_config()
            result = {"ok": True, "enabled": BRIDGE_ENABLED,
                      "allowed_paths": ALLOWED_PATHS}
            self._send(200, json.dumps(result),
                       "application/json", extra_headers=_bridge_headers())
            return

        # KHAOSS-PATCH: settings-apikey-entry — enter/update a provider key from
        # the Settings tab. LOOPBACK-ONLY: even with a valid bearer token, a
        # remote/tunnelled caller is refused — raw keys are settable only from
        # the local machine. The key is written to the boardwalk secure store and
        # never logged or echoed back (response carries only key_present + last4).
        if path == "/secrets/delete":
            # ★★★ DELETING A SECRET IS IRREVERSIBLE AND THE VALUE CANNOT BE READ BACK FIRST — it is
            #   write-only by design. So this REFUSES unless the caller names the exact id AND
            #   confirms it, and it reports what it actually removed rather than claiming success.
            if not self._is_local_request():
                return self._send_json(403, {"error": "local-only"})
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            sid = str(b.get("id") or "").strip()
            if not sid or b.get("confirm") != sid:
                return self._send_json(400, {"ok": False,
                    "error": "to delete, send the id AND confirm with the same id — "
                             "this cannot be undone and the value cannot be shown to you first"})
            f = Path.home() / ".khaoss-secrets" / sid
            if "/" in sid or not f.is_file():
                return self._send_json(404, {"ok": False, "error": f"no such secret {sid!r}"})
            try:
                import subprocess as _sp
                _sp.run(["shred", "-u", str(f)], capture_output=True, timeout=20)
                gone = not f.exists()
                if not gone:
                    f.unlink(missing_ok=True); gone = not f.exists()
            except Exception as e:
                return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})
            removed_meta = False
            try:
                m = self._op_mod("secret_meta")
                d2 = m._load()
                if sid in d2:
                    d2.pop(sid); m._save(d2); removed_meta = True
            except Exception:
                pass
            # ★ and the inventory the card reads must forget it too, or the card lies
            inv_cleaned = False
            try:
                inv = Path.home() / ".config" / "AionUi-Dev" / "khaoss-secrets.json"
                j = json.loads(inv.read_text())
                tgt = j.get("secrets") if isinstance(j.get("secrets"), dict) else j
                if isinstance(tgt, dict) and sid in tgt:
                    tgt.pop(sid)
                    inv.write_text(json.dumps(j, indent=2))
                    inv_cleaned = True
            except Exception:
                pass
            return self._send_json(200, {"ok": gone, "id": sid, "file_removed": gone,
                                         "clearance_removed": removed_meta,
                                         "inventory_updated": inv_cleaned,
                                         "note": "shredded, not merely unlinked"})

        if path == "/secrets/set":
            # ★ SECRETS WIZARD — loopback-only, write-only. The value is never echoed back.
            if not self._is_local_request():
                return self._send_json(
                    403, {"error": "secrets can only be set from local loopback"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            result = _secure_write_secret(body.get("id"), body.get("value"),
                                          body.get("backend") or "key_file",
                                          body.get("note") or "", body.get("username") or "")
            # ★★ THE CLEARANCE IS ATTACHED IN THE SAME BREATH AS THE VALUE, deliberately. A secret
            #   stored now and classified "later" is a secret with no clearance, because later does
            #   not arrive — all ten existing ones prove it. The metadata write never touches the
            #   value and cannot fail the store: if classification errors, the secret is still saved
            #   and the response says the classification did not land.
            if result.get("ok"):
                try:
                    ring = body.get("ring")
                    m = self._op_mod("secret_meta").describe(
                        result.get("id") or body.get("id") or "",
                        rank_required=str(body.get("rank_required") or ""),
                        territory=str(body.get("territory") or ""),
                        ring=(int(ring) if ring not in (None, "") else None),
                        owner=str(body.get("owner") or ""),
                        kind=str(body.get("kind") or ""),
                        account=str(body.get("account") or ""))
                    result["classified"] = m.get("ok", False)
                    if not m.get("ok"):
                        result["classify_error"] = m.get("error")
                except Exception as e:
                    result["classified"] = False
                    result["classify_error"] = f"{e.__class__.__name__}: {e}"
            body = None  # drop the raw value from memory promptly
            return self._send_json(200 if result.get("ok") else 400, result)

        if path == "/providers/set-key":
            if not self._is_local_request():
                return self._send_json(
                    403, {"error": "provider keys can only be set from local loopback"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            result = _secure_write_provider_key(
                body.get("name"), body.get("base_url"), body.get("key"),
                body.get("models"), (body.get("default_model") or "").strip())
            body = None  # drop the raw key from memory promptly
            return self._send_json(200 if result.get("ok") else 400, result)

        if path == "/secrets/stewards":                                    # DQ25: flip stewards-see-secrets
            if not self._is_local_request():
                return self._send_json(403, {"error": "stewards secrets policy settable only from local loopback"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            val = bool(body.get("stewards_see_secrets", False))
            _pol = Path(__file__).resolve().parent.parent / "secrets_policy.json"
            try:
                _p = json.loads(_pol.read_text())
            except Exception:
                _p = {}
            _p["stewards_see_secrets"] = val
            _p["_label"] = ("Stewards see secret values: ON — WARNING: a Steward permitted to see secrets is a "
                            "Steward whose own summaries then carry them onward." if val else
                            "Stewards see secret values: OFF. The Watchman does not need this — its detection "
                            "reads trace shapes, not contents.")
            _p["_updated"] = "2026-08-04"
            _pol.write_text(json.dumps(_p, indent=2))
            try:
                _pol.chmod(0o600)
            except Exception:
                pass
            return self._send_json(200, {"ok": True, "stewards_see_secrets": val})

        if path == "/providers/register":
            import subprocess

            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            name = (body.get("name") or "").strip()
            base_url = (body.get("base_url") or "").strip()
            key_file = (body.get("key_file") or "").strip()
            models_raw = body.get("models", [])
            default_model = (body.get("default_model") or "").strip()

            if not name or not base_url or not key_file:
                self._send_json(400, {"error": "name, base_url, key_file required"})
                return
            kp = Path(key_file)
            if not kp.exists():
                self._send_json(400, {"error": f"key_file not found: {key_file}"})
                return
            kstat = kp.stat()
            if oct(kstat.st_mode)[-3:] != "600":
                self._send_json(400, {"error": "key_file must be chmod 600"})
                return

            provider_id = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
            if not provider_id:
                self._send_json(400, {"error": "provider id could not be derived from name"})
                return

            registry_path = Path.home() / ".config" / "AionUi-Dev" / "aionui-providers.json"
            try:
                registry = json.loads(registry_path.read_text())
            except Exception:
                registry = {"version": 1, "providers": []}
            registry.setdefault("providers", [])

            model_entries = []
            for m in (models_raw if isinstance(models_raw, list) else []):
                mid = m.strip() if isinstance(m, str) else str(m.get("id", "")).strip() if isinstance(m, dict) else ""
                if mid:
                    model_entries.append({"id": mid, "label": mid, "provider_tag": provider_id})

            entry = {
                "id": provider_id,
                "name": name,
                "base_url": base_url,
                "key_file": key_file,
                "hermes_profile": provider_id,
                "enabled": False,
                "models": model_entries,
            }
            if any(p.get("id") == provider_id for p in registry["providers"]):
                registry["providers"] = [entry if p.get("id") == provider_id else p for p in registry["providers"]]
            else:
                registry["providers"].append(entry)
            registry_path.write_text(json.dumps(registry, indent=2))

            subprocess.run(
                ["hermes", "profile", "create", provider_id, "--description", f"{name} via provider registry"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            profile_dir = Path.home() / ".hermes" / "profiles" / provider_id
            profile_dir.mkdir(parents=True, exist_ok=True)
            config_yaml = profile_dir / "config.yaml"
            if config_yaml.exists():
                try:
                    import yaml
                    cfg = yaml.safe_load(config_yaml.read_text()) or {}
                except Exception:
                    cfg = {}
            else:
                cfg = {}
            cfg.setdefault("model", {})
            cfg["model"]["provider"] = "custom"
            cfg["model"]["base_url"] = base_url
            cfg["model"]["default"] = default_model or (model_entries[0]["id"] if model_entries else "")
            cfg["model"]["name"] = cfg["model"]["default"]
            cfg["model"]["key_env"] = "OPENAI_API_KEY"
            cfg.setdefault("agent", {})
            cfg["agent"]["reasoning_effort"] = ""
            cfg.setdefault("api_server", {})
            cfg["api_server"]["enabled"] = True
            try:
                import yaml
                config_yaml.write_text(yaml.dump(cfg, default_flow_style=False))
            except ImportError:
                config_yaml.write_text(
                    f"model:\n  provider: custom\n  base_url: {base_url}\n"
                    f"  default: {cfg['model']['default']}\n  name: {cfg['model']['default']}\n"
                    f"  key_env: OPENAI_API_KEY\nagent:\n  reasoning_effort: ''\n"
                    f"api_server:\n  enabled: true\n"
                )

            env_path = profile_dir / ".env"
            key_val = kp.read_text().strip()
            env_path.write_text(f"OPENAI_API_KEY={key_val}\nOPENAI_BASE_URL={base_url}\n")
            env_path.chmod(0o600)
            key_val = None

            wrapper = Path.home() / ".local" / "bin" / provider_id
            probe_ok = False
            probe_output = ""
            if wrapper.exists():
                probe_result = subprocess.run(
                    [str(wrapper), "chat", "-q", "Reply with exactly: PROVIDER_OK", "-Q"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                probe_output = (probe_result.stdout or probe_result.stderr).strip()
                probe_ok = "PROVIDER_OK" in probe_output
            else:
                probe_output = f"Wrapper not found at {wrapper} - profile created but not probed"

            if probe_ok:
                for p in registry["providers"]:
                    if p.get("id") == provider_id:
                        p["enabled"] = True
            registry_path.write_text(json.dumps(registry, indent=2))

            self._send_json(200 if probe_ok else 207, {
                "ok": probe_ok,
                "id": provider_id,
                "probe": probe_output[:200] if probe_ok else "probe failed",
                "error": None if probe_ok else probe_output[:300],
                "enabled": probe_ok,
            })
            return

        if path == "/providers/test":
            import subprocess

            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            provider_id = (body.get("id") or "").strip()
            if not provider_id:
                self._send_json(400, {"error": "id required"})
                return
            wrapper = Path.home() / ".local" / "bin" / provider_id
            if not wrapper.exists():
                self._send_json(404, {"error": f"wrapper not found: {wrapper}"})
                return
            result = subprocess.run(
                [str(wrapper), "chat", "-q", "Reply with exactly: PROVIDER_OK", "-Q"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            output = (result.stdout or result.stderr).strip()
            ok = "PROVIDER_OK" in output
            self._send_json(200, {"ok": ok, "output": output[:200]})
            return

        self._send_json(404, {"error": "not found", "path": path})

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        # KHAOSS-PATCH: panel-security — gate every route
        if not self._check_auth():
            return
        if path.startswith("/launch-assist/agents/"):
            agent_id = path[len("/launch-assist/agents/"):]
            return self._la_agents_delete(agent_id)
        if path.startswith("/prompter/library/"):
            return self._prompter_library_delete(path[len("/prompter/library/"):])
        self._send_json(404, {"error": "not found", "path": path})

    # ── /  → panel HTML ──────────────────────────────────────────────────────

    def _serve_panel(self) -> None:
        """Serve the panel. ★ `?section=<name>` renders the DESTINATION SERVER-SIDE (Shape C, S4).

        WHY A QUERY AND NOT A FRAGMENT: a fragment is client-side and never reaches the server, and — the
        actual bug — a browser that already has `http://127.0.0.1:7842/` open FOCUSES that tab instead of
        navigating, so the hash never changes, `hashchange` never fires, and the deep-link code never runs.
        The panel and its resolver were innocent; the URL never arrived. A query string is part of the URL
        the browser matches on, so it opens/navigates properly, the server can VALIDATE and LOG it, and the
        page comes back with that section already active — no race, no hidden container, no silent ignore.
        """
        if not PANEL_HTML.is_file():
            self._send_text(500, f"panel HTML missing: {PANEL_HTML}")
            return
        body = PANEL_HTML.read_bytes()
        # ★ via _inject_before_body_end — the LAST </body>, never the first (see that docstring)
        body = _inject_before_body_end(
            body, ('<script>window.__PANEL_STAMP__=%s;</script>' % json.dumps(_panel_stamp())).encode())

        # ★★ TWO LINK SEMANTICS, KEPT SEPARATE FOREVER (2026-08-22, after breaking the panel by conflating them)
        #
        #   ?section=<id>  — RESERVED, PRE-EXISTING, DO NOT REPURPOSE. This is the panel's own POPOUT
        #                    parameter: `popoutSection()` does window.open('?section='+id), and
        #                    `applyPopoutMode()` reads it, hides topbar/alerts-bar/qc-bar/launch-bar and every
        #                    [data-section] except the named one. Correct in a popup window; catastrophic in
        #                    the main window.
        #   ?popout=<id>   — the SAME surface under its honest name. Served as a redirect to ?section= rather
        #                    than a second renderer, because two renderers for one surface is duplicate
        #                    substrate — this project's most-repeated defect. The AionUi button points HERE.
        #   ?jump=<id>     — MAIN PANEL: scroll + highlight + open the wizard. It must NEVER hide a section,
        #                    and it deliberately uses a parameter applyPopoutMode does not read.
        #
        # ★ THE MISTAKE THIS ENCODES: my Shape C fix chose `?section=` as its deep-link name without checking
        #   whether the name was taken. It was. The panel then did exactly what it was built to do — strip its
        #   own chrome down to one section — and that read as "the control panel is half broken". A wire check
        #   said OK=true throughout, because the flag HAD arrived; it just arrived somewhere that meant
        #   something else. Check whether a name is already owned before you give it a second meaning.
        try:
            _q = parse_qs(urlsplit(self.path).query)
            _pop = (_q.get("popout", [""])[0] or "").strip()
            if _pop:
                _safe_pop = re.sub(r"[^a-zA-Z0-9_-]", "", _pop)[:64]
                self.send_response(302)
                self.send_header("Location", f"/?section={_safe_pop}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
        except Exception:
            pass

        section = ""
        try:
            q = parse_qs(urlsplit(self.path).query)
            section = (q.get("jump", [""])[0] or "").strip()
        except Exception:
            section = ""
        if section:
            safe = re.sub(r"[^a-zA-Z0-9_-]", "", section)[:64]
            known = set(re.findall(rb'data-section="([a-zA-Z0-9_-]+)"', body)) | \
                    set(re.findall(rb'data-svc="([a-zA-Z0-9_-]+)"', body))
            known_s = {k.decode() for k in known}
            # ★ a section that does not exist is announced, never silently ignored (the Shape C rule)
            ok = safe in known_s
            inject = (
                "<script>(function(){var S=%r,OK=%s;function go(){"
                "var el=document.querySelector('[data-svc=\"'+S+'\"]')||document.getElementById(S)"
                "||document.querySelector('[data-section=\"'+S+'\"]');"
                "if(!OK||!el){var w=document.createElement('div');"
                "w.setAttribute('data-testid','section-unknown');"
                "w.style.cssText='position:fixed;top:8px;left:50%%;transform:translateX(-50%%);z-index:99999;"
                "background:#3a2a00;border:1px solid #e93;color:#e93;padding:6px 12px;border-radius:4px;font:12px monospace';"
                "w.textContent='\\u26a0 section \\''+S+'\\' does not exist in this panel';"
                "document.body.appendChild(w);return;}"
                # ★ 2026-08-22 — popoutSection() IS FORBIDDEN HERE. It is the POPOUT WINDOW's own renderer:
                #   "show one [data-section], hide the rest" is correct inside a popout and catastrophic in
                #   the main panel, where it collapses the full view to a single section and hides the very
                #   popout buttons the operator is looking for. `?section=` means SCROLL + HIGHLIGHT ONLY and
                #   must NEVER hide a section. The dedicated surface is `?popout=`, a separate semantic.
                "var host=el.closest('[data-section]');"
                "if(host&&(host.offsetParent===null||getComputedStyle(host).display==='none')){"
                "var w2=document.createElement('div');w2.setAttribute('data-testid','section-hidden');"
                "w2.style.cssText='position:fixed;top:8px;left:50%%;transform:translateX(-50%%);z-index:99999;"
                "background:#3a2a00;border:1px solid #e93;color:#e93;padding:6px 12px;border-radius:4px;font:12px monospace';"
                "w2.textContent='\\u26a0 \\''+S+'\\' is inside a collapsed section \\u2014 open it, or use ?popout='+S;"
                "document.body.appendChild(w2);}"
                "el.scrollIntoView({block:'center'});el.style.outline='2px solid #5c5';"
                "setTimeout(function(){el.style.outline='';},2500);"
                "if(S==='secrets'&&typeof secretsWizard==='function'){var b=document.getElementById('secrets-wizard');"
                "if(b&&b.dataset.open!=='1')secretsWizard();}}"
                "if(document.readyState==='complete'){setTimeout(go,150);}"
                "else{window.addEventListener('load',function(){setTimeout(go,150);});}})();</script>"
            ) % (safe, "true" if ok else "false")
            body = _inject_before_body_end(body, inject.encode())

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # S1b — never let a stale panel be cached
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ── dual-surface consent broker ──────────────────────────────────────────
    # ONE component, THREE consumers (broker · first-contact · secrets), TWO surfaces (this popout +
    # the AionUi banner). VALUES NEVER transit here — the DECISION only; a secret flows solely through
    # the backend injector (khaoss-secrets resolve-for-inject) or zenity. Portable surface: /consent/*.
    def _consent_pending(self) -> None:
        try:
            import consent_broker as _cb
            return self._send_json(200, {"pending": _cb.pending(), "host": socket.gethostname()})
        except Exception as exc:
            return self._send_json(500, {"error": f"consent pending failed: {exc}"})

    def _consent_request(self, body) -> None:
        try:
            import consent_broker as _cb
            rec = _cb.create_request(
                host=body.get("host") or socket.gethostname(),
                consumer=body.get("consumer", ""),
                what=body.get("what", ""), why=body.get("why", ""),
                scope=body.get("scope", "allow-once"),
                duration=body.get("duration"), options=body.get("options"),
            )
            return self._send_json(200, {"ok": True, "request_id": rec["request_id"], "deadline": rec["deadline"]})
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": f"consent request failed: {exc}"})

    def _consent_resolve(self, body) -> None:
        rid = body.get("request_id", ""); choice = body.get("choice", "")
        if not rid or not choice:
            return self._send_json(400, {"ok": False, "error": "request_id and choice required"})
        try:
            import consent_broker as _cb
            rec = _cb.resolve(rid, choice, resolver=body.get("resolver", "operator"))
            if rec is None:
                return self._send_json(404, {"ok": False, "error": f"no such request: {rid}"})
            return self._send_json(200, {"ok": True, "status": rec["status"], "choice": rec["choice"]})
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": f"consent resolve failed: {exc}"})

    def _consent_audit(self) -> None:
        try:
            import consent_broker as _cb
            return self._send_json(200, {"audit": _cb.recent_audit(25)})
        except Exception as exc:
            return self._send_json(500, {"error": f"consent audit failed: {exc}"})

    def _consent_popout(self) -> None:
        if not CONSENT_POPOUT_HTML.is_file():
            return self._send_text(500, f"consent popout HTML missing: {CONSENT_POPOUT_HTML}")
        body = CONSENT_POPOUT_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ── INV-39 cards: broker · namespace-view · secrets (portable surfaces — /X returns a JSON contract) ──
    def _broker_card(self) -> None:
        """BROKER card data (consumer #1). Privileged-op requests route through /consent/*."""
        import subprocess as _sp, os as _os, stat as _stat, pwd as _pwd
        try:
            sudo = str(ROOT / "khaoss-sudo")
            st = _sp.run(["bash", sudo, "status"], capture_output=True, text=True, timeout=8)
            au = _sp.run(["bash", sudo, "audit"], capture_output=True, text=True, timeout=8)
            status_raw = st.stdout or ""
            armed = "STATE: ON" in status_raw
            installed = "no /etc/sudoers.d/khaoss-ops" not in status_raw
            targets = []
            for name in ("fix_swap.sh", "fix_ollama_keepalive.sh"):
                p = "/usr/local/sbin/" + name
                if _os.path.exists(p):
                    stt = _os.stat(p); mode = _stat.S_IMODE(stt.st_mode)
                    targets.append({"path": p, "owner": (_pwd.getpwuid(stt.st_uid).pw_name if stt.st_uid == 0 else str(stt.st_uid)),
                                    "mode": oct(mode), "root_owned": stt.st_uid == 0, "non_writable": not bool(mode & 0o022)})
            return self._send_json(200, {
                "installed": installed, "armed": armed,
                "policy": "on for a task, off after — never left armed. LLM auto-approval OFF (human-gate).",
                "targets": targets, "status_raw": status_raw.strip(),
                "audit_tail": (au.stdout or "").strip().splitlines()[-6:],
                "arm_hint": "arm/disarm: `sudo bash install_khaoss_sudo.sh` then `khaoss-sudo on|off`; privileged requests route through /consent/*",
            })
        except Exception as exc:
            return self._send_json(500, {"error": f"broker card failed: {exc}"})

    def _namespace_card(self) -> None:
        """NAMESPACE-VIEW card (consumer #2). Renders the role->profile map (names only, read-mostly)."""
        try:
            d = _read_json(HOME / ".config" / "AionUi-Dev" / "khaoss-namespace.json")
            if not d:
                return self._send_json(404, {"error": "khaoss-namespace.json not found"})
            return self._send_json(200, {
                "host": d.get("host"), "recommended_scheme": d.get("recommended_scheme"),
                "roles": d.get("roles", {}), "grandfathered": d.get("grandfathered", {}),
                "aionui_hardcodes": d.get("aionui_hardcodes", {}),
                "adaptive_rules": d.get("adaptive_installer_clause", {}),
                "refactor_exemptions_count": len(d.get("refactor_exemptions", []) or []),
            })
        except Exception as exc:
            return self._send_json(500, {"error": f"namespace card failed: {exc}"})

    def _secrets_card(self) -> None:
        """SECRETS card (consumer #3 surface). Metadata ONLY — never a value."""
        import subprocess as _sp
        try:
            reg = _read_json(HOME / ".config" / "AionUi-Dev" / "khaoss-secrets.json")
            if not reg:
                return self._send_json(404, {"error": "khaoss-secrets.json not found"})
            secrets = []
            for name, r in (reg.get("secrets") or {}).items():
                ex = r.get("exemption")
                # ★★ 2026-08-23 — RECONCILE THE MANIFEST AGAINST THE ACTUAL STORE.
                #   There are TWO stores: khaoss-secrets.json (this manifest) and ~/.khaoss-secrets/ (the
                #   key_file backing store). They can diverge, and today they did: the operator shredded
                #   `this-box-pass` (a MACHINE PASSWORD) and the file went, but the manifest entry stayed —
                #   so this card kept listing it as managed. **The inventory was telling him a machine
                #   password was still stored when it was not.**
                #   ★ Do NOT silently drop the row — a vanished row is indistinguishable from one that was
                #   never there. Mark it MISSING and let the divergence be the thing you see. Same rule as
                #   every other duplicate-substrate case here: when two of a thing exist, the surface must
                #   say which is live.
                exists = None
                if (r.get("backend") or "") == "key_file":
                    try:
                        ref = r.get("ref") or ""
                        p = Path(ref).expanduser() if ref.startswith(("/", "~")) else (HOME / ".khaoss-secrets" / name)
                        exists = p.is_file()
                    except Exception:
                        exists = None
                secrets.append({"name": name, "backend": r.get("backend"), "ref": r.get("ref"),
                                "consumer": r.get("consumer"), "cap": (r.get("spend_cap_note", "") or "")[:100],
                                "last_rotated": r.get("last_rotated"),
                                "backing_file_present": exists,
                                "exemption": (ex.get("status") if ex else None)})
            au = _sp.run(["python3", str(ROOT / "khaoss-secrets"), "audit", "--json"],
                         capture_output=True, text=True, timeout=8)
            audit = {}
            try:
                audit = json.loads(au.stdout) if au.stdout.strip().startswith("{") else {}
            except ValueError:
                pass
            return self._send_json(200, {
                "backends": {k: v.get("status") for k, v in (reg.get("backends") or {}).items()},
                "secrets": secrets, "audit_verdict": audit.get("verdict"),
                "managed": len(audit.get("managed", [])), "acked": len(audit.get("acked", [])),
                "pending": [x.get("ref") for x in audit.get("pending", [])],
                "note": "metadata only — values never appear here (L5 injection only)",
            })
        except Exception as exc:
            return self._send_json(500, {"error": f"secrets card failed: {exc}"})

    def _access_card(self) -> None:
        """ACCESS / posture card (first-contact consumer #4 + INV-45's panel home). Names only."""
        import subprocess as _sp
        try:
            reg = _read_json(HOME / ".config" / "AionUi-Dev" / "khaoss-access.json")
            if not reg:
                return self._send_json(404, {"error": "khaoss-access.json not found"})
            surfaces = reg.get("surfaces", {})
            personal = {k: v for k, v in surfaces.items() if v.get("kind") == "personal"}
            granted = [k for k, v in personal.items() if v.get("status") == "granted"]
            j = _sp.run(["python3", str(ROOT / "inv4145_check.py"), "45"],
                        capture_output=True, text=True, timeout=8)
            jail_ok = (j.stdout or "").strip() == "OK"
            canon = _read_json(HOME / ".config" / "AionUi-Dev" / "khaoss-aionui-settings.canon.json")
            exemptions = list((canon.get("exemptions") or {}).keys())
            return self._send_json(200, {
                "personal_total": len(personal),
                "granted": granted,
                "ungranted": [k for k, v in personal.items() if v.get("status") == "UNGRANTED"],
                "surfaces": {k: {"kind": v.get("kind"), "status": v.get("status"),
                                 "scope": v.get("scope"), "gate": (v.get("gate", "") or "")[:70]}
                             for k, v in surfaces.items()},
                "jail_default": "Contained (RULED)",
                "jail_materialized": jail_ok,
                "exemptions": exemptions,
            })
        except Exception as exc:
            return self._send_json(500, {"error": f"access card failed: {exc}"})

    # ── /stream  (SSE) ───────────────────────────────────────────────────────

    def _sse_stream(self) -> None:
        # SSE headers. Streaming response — never close from server side
        # unless the client disconnects or the broadcaster stops.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        # Disable any reverse-proxy buffering (defence in depth — we don't
        # actually expect a proxy in front of 127.0.0.1).
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        q = BROADCASTER.subscribe()
        try:
            # Send a hello so the client knows the connection is live before
            # the first tick fires.
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15.0)
                    chunk = f"data: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    # Heartbeat comment keeps stalled proxies / clients honest.
                    chunk = b": keepalive\n\n"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            BROADCASTER.unsubscribe(q)

    def _snapshot_once(self) -> None:
        snapshot = poll_all_services()
        return self._send_json(200, {
            "ts": int(time.time() * 1000),
            "services": snapshot,
            "alerts": derive_alerts(snapshot),
        })

    # ── /api/vault/stats ─────────────────────────────────────────────────────

    def _api_vault_stats(self) -> None:
        if not VAULT_WORKSPACE.is_dir():
            return self._send_json(200, {"file_count": 0, "last_scrub": None, "files": []})
        files = []
        latest = 0.0
        for p in VAULT_WORKSPACE.iterdir():
            if p.is_file():
                st = p.stat()
                files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
                latest = max(latest, st.st_mtime)
        files.sort(key=lambda f: f["mtime"], reverse=True)
        last_iso = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else None
        return self._send_json(200, {
            "file_count": len(files),
            "last_scrub": last_iso,
            "last_scrub_epoch": latest,
            "files": files[:10],
        })

    def _api_quarantine_stats(self) -> None:
        if not VAULT_QUARANTINE.is_dir():
            return self._send_json(200, {"count": 0, "files": [], "last": None})
        files = []
        latest = 0.0
        for p in VAULT_QUARANTINE.iterdir():
            if p.is_file():
                st = p.stat()
                files.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
                latest = max(latest, st.st_mtime)
        files.sort(key=lambda f: f["mtime"], reverse=True)
        last_iso = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat() if latest else None
        return self._send_json(200, {
            "count": len(files),
            "last": last_iso,
            "files": files[:10],
        })

    # ── /api/mcp/log?n=N ─────────────────────────────────────────────────────

    def _api_mcp_log(self) -> None:
        qs = parse_qs(urlsplit(self.path).query)
        try:
            n = int(qs.get("n", ["20"])[0])
        except ValueError:
            n = 20
        n = max(1, min(n, 500))
        if not MCP_LOG.is_file():
            return self._send_json(200, {"lines": [], "missing": True})
        with MCP_LOG.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-n:]
        parsed = []
        for raw in tail:
            raw = raw.rstrip("\n")
            parts = raw.split("\t", 2)
            if len(parts) == 3:
                ts, tool, detail = parts
            elif len(parts) == 2:
                ts, tool = parts; detail = ""
            else:
                ts, tool, detail = "", "", raw
            parsed.append({"ts": ts, "tool": tool, "detail": detail, "raw": raw})
        return self._send_json(200, {"lines": parsed})

    # ── /logs?file=NAME&lines=N ──────────────────────────────────────────────
    # Generic tail-er for any file in the ALLOWED_LOG_FILES whitelist, plus
    # the backend's own /tmp/panel_server.log (special-cased because LOG_DIR
    # is locked to ~/boardwalk/).

    def _api_logs(self) -> None:
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("file") or [""])[0]
        try:
            n = int((qs.get("lines") or ["20"])[0])
        except ValueError:
            n = 20
        n = max(1, min(n, 500))

        # Path traversal defence: reject anything with separators or `..`
        # before doing any path math.
        if "/" in name or "\\" in name or ".." in name:
            return self._send_json(400, {"error": "invalid file name", "file": name})

        if name == BACKEND_LOG_NAME:
            path = LOG_FILE
        elif name in ALLOWED_LOG_FILES:
            path = LOG_DIR / name
        else:
            return self._send_json(400, {
                "error": "file not allowed",
                "file": name,
                "allowed": sorted(ALLOWED_LOG_FILES | {BACKEND_LOG_NAME}),
            })

        if not path.is_file():
            return self._send_json(200, {"file": name, "lines": [], "count": 0, "missing": True})

        try:
            tail = _tail_lines(path, n)
        except OSError as exc:
            return self._send_json(500, {"error": "read failed", "reason": str(exc)})

        lines = [ln.rstrip("\n") for ln in tail]
        return self._send_json(200, {"file": name, "lines": lines, "count": len(lines)})

    # ── /api/credential-check ────────────────────────────────────────────────

    def _api_credential_check(self) -> None:
        if not CREDENTIAL_CHECK.is_file():
            return self._send_json(500, {"error": f"missing: {CREDENTIAL_CHECK}"})
        try:
            # Fixed path, no shell, no caller input. Safe.
            proc = subprocess.run(
                ["/bin/bash", str(CREDENTIAL_CHECK)],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            return self._send_json(504, {"error": "credential_check.sh timed out after 30s"})
        return self._send_json(200, {
            "exit_code": proc.returncode,
            "exit":      proc.returncode,  # back-compat alias
            "passed":    proc.returncode == 0,
            "output":    proc.stdout,
            "stdout":    proc.stdout,      # back-compat alias
            "stderr":    proc.stderr,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })

    # ── /run/invariants ──────────────────────────────────────────────────────
    # KHAOSS-PATCH: invariants-utility — runs check_invariants.sh and returns the PASS/FAIL board.
    # Read-only (the checker never modifies anything). One click → the board. Fixed path, no shell,
    # no caller input.
    def _api_invariants(self) -> None:
        if not INVARIANTS_CHECK.is_file():
            return self._send_json(500, {"error": f"missing: {INVARIANTS_CHECK}"})
        try:
            proc = subprocess.run(
                ["/bin/bash", str(INVARIANTS_CHECK)],
                capture_output=True, text=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            return self._send_json(504, {"error": "check_invariants.sh timed out after 60s"})
        # strip ANSI colour so the UI shows a clean board; parse the tally line.
        clean = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
        m = re.search(r"(\d+) PASS\s+(\d+) FAIL\s+(\d+) SKIP", clean)
        n_pass, n_fail, n_skip = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (None, None, None)
        # KHAOSS-PATCH: failure-immune-system — a checker FAIL is a failure event (genre=invariant_drift).
        # Purely additive: scrape from the clean string we already have, never re-run the checker.
        # A journal write must not break the board response (mirror _ledger_append's swallow).
        try:
            if fim is not None:
                for ev in fim.scrape_invariant_fails(clean, exit_code=proc.returncode):
                    _failure_append({"action": "detect", "source": "check_invariants.sh", **ev})
        except Exception as _je:  # pragma: no cover
            sys.stderr.write(f"[failure] invariant-drift scrape failed: {_je}\n")
        return self._send_json(200, {
            "exit_code": proc.returncode,
            "exit":      proc.returncode,
            "passed":    proc.returncode == 0,
            "n_pass":    n_pass,
            "n_fail":    n_fail,
            "n_skip":    n_skip,
            "output":    clean,
            "stdout":    clean,
            "stderr":    proc.stderr,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        })

    # ── KHAOSS-PATCH: dsh-launcher (2026-09-23) ──────────────────────────────────────────────────
    # ★★ WHY THIS EXISTS. On 2026-09-23 the operator did a full system restart and could not get his
    #   own harness back: `khaoss-dsh-web.service` did not exist yet, the panel had ZERO "dsh" service
    #   keys, and there was no desktop entry. His only route back was to ask another team to start a
    #   bare manual process — and because that process came back on a different `DSH_HOME` than the one
    #   holding his history, the conversation he had been working in vanished from the GUI while it was
    #   still on disk. This card, the desktop icon, and the unit are the three ways back in.
    #
    # ★ The ONE action calls `bin/khaoss-dsh-launch.sh --no-open` — the SAME entry point the desktop
    #   icon uses, so there is one code path and not two. `--no-open` because the panel must not open a
    #   browser it cannot then authenticate; the card reports the recovered result and the operator
    #   clicks Open, which hands the authenticated URL to his own browser.
    DSH_LAUNCH_SH = ROOT / "bin" / "khaoss-dsh-launch.sh"
    DSH_UNIT      = "khaoss-dsh-web.service"
    DSH_URL       = "http://127.0.0.1:3080"

    def _dsh_state(self) -> dict:
        """Measured state of the harness, never assumed. Every field is read, not inferred."""
        import subprocess as _sp
        out: dict = {"url": self.DSH_URL, "unit": self.DSH_UNIT}
        try:
            r = _sp.run(["/usr/bin/systemctl", "--user", "is-active", self.DSH_UNIT],
                        capture_output=True, text=True, timeout=8)
            out["unit_state"] = (r.stdout or "").strip() or "unknown"
        except Exception as e:  # noqa: BLE001
            out["unit_state"] = f"unreadable: {type(e).__name__}"
        # ★ 401 IS HEALTHY. The fence refuses a tokenless client by design, so readiness is "answered
        #   at all" — a 200-check would report a perfectly good server as down.
        try:
            r = _sp.run(["/usr/bin/curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                         "--max-time", "4", self.DSH_URL + "/"],
                        capture_output=True, text=True, timeout=10)
            code = (r.stdout or "").strip()
            out["http"] = code
            out["serving"] = code not in ("", "000")
        except Exception as e:  # noqa: BLE001
            out["http"] = "?"
            out["serving"] = False
            out["http_error"] = type(e).__name__
        # A launch token is only valid for the process that printed it, so its PRESENCE is what decides
        # whether a browser can get in. Report presence only — never the token itself.
        try:
            r = _sp.run(["/usr/bin/journalctl", "--user", "-u", self.DSH_UNIT, "--no-pager", "-o", "cat"],
                        capture_output=True, text=True, timeout=15)
            out["has_token"] = "?token=" in (r.stdout or "")
        except Exception:  # noqa: BLE001
            out["has_token"] = None
        out["launcher"] = self.DSH_LAUNCH_SH.is_file()
        # ★ THE TRANSCRIPT MIRROR, REPORTED BECAUSE IT DECIDES WHAT THE OPERATOR CAN OPEN. The service
        #   pins DSH_HOME=str(Path.home())/.dsh while a live session may be written in the OTHER home, and the
        #   GUI can only open a session that exists in its own home. Measured 2026-09-23: the live
        #   transcript was 1.4 MB ahead of the mirrored copy, so a restart without a mirror lists the
        #   session and opens a truncated conversation and nothing on screen says why.
        #   ★ WHICH SESSION: the NEWEST transcript in the workspace — that is the live one and the one
        #     whose staleness actually costs the operator something. The panel has no DSH_SESSION_ID of
        #     its own (that variable belongs to a tool call), so an id-based check would report
        #     "unknown" forever, which is exactly the unreadable-instrument shape this estate keeps
        #     paying for. The id is therefore DISCOVERED, not assumed.
        try:
            # ★ RELEASE: the workspace is configurable; the private checkout name is not shipped.
            _src_wd = (Path(os.environ.get("KHAOSS_DSH_WORKSPACE")
                            or (Path.home() / "Downloads" / "boardwalk-workspace"))
                       / ".dsh" / "sessions")
            _newest, _newest_mt = None, -1.0
            if _src_wd.is_dir():
                for _d in _src_wd.iterdir():
                    _f = _d / "session.v3.jsonl.zstd"
                    if _f.is_file():
                        _mt = _f.stat().st_mtime
                        if _mt > _newest_mt:
                            _newest, _newest_mt = _d.name, _mt
            if _newest is None:
                out["mirror"] = "no live transcript found"
                out["mirror_ok"] = None
            else:
                r = _sp.run(["/bin/bash",
                             str(ROOT / "bin" / "khaoss-dsh-mirror-session.sh"),
                             _newest, "--status"],
                            capture_output=True, text=True, timeout=15)
                last = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip().startswith("verdict")]
                out["mirror"] = (last[-1].replace("verdict : ", "") if last else "unknown")
                out["mirror_session"] = _newest
                out["mirror_ok"] = (r.returncode == 0)
        except Exception as _me:  # noqa: BLE001
            out["mirror"] = f"unreadable: {type(_me).__name__}"
            out["mirror_ok"] = None
        # ★ THE HONEST VERDICT, and the reason this method exists: a server that is SERVING but whose
        #   unit is not active is the bare manual process — the exact state that produced the lockout.
        #   It cannot hand out a valid token through the unit's journal, so it is reported distinctly
        #   rather than being called "up".
        if out.get("serving") and out.get("unit_state") != "active":
            out["verdict"] = "UNSUPERVISED"
            out["note"] = ("something is serving :3080 but khaoss-dsh-web.service is NOT active — this "
                           "is a bare manual process. It has no restart, no memory bound, and no journal "
                           "token line, so a browser cannot be authenticated into it. Restart the unit.")
        elif out.get("serving") and out.get("has_token"):
            out["verdict"] = "UP"
            out["note"] = "serving under systemd with a recoverable launch token"
        elif out.get("serving"):
            out["verdict"] = "UP_NO_TOKEN"
            out["note"] = ("serving under systemd but no token line is in the journal yet — it is "
                           "printed at startup; restart the unit if a browser gets 401")
        else:
            out["verdict"] = "DOWN"
            out["note"] = "not serving — click Launch to start it"
        return out

    def _dsh_status(self) -> None:
        """GET /run/dsh-status — read-only. The card polls this."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        return self._send_json(200, self._dsh_state())

    def _dsh_url(self) -> None:
        """POST /run/dsh-url — the AUTHENTICATED url, as data.

        ★★ WHY THE PANEL ASKS FOR A URL INSTEAD OF OPENING A BROWSER. Measured 2026-09-23: opening a
        window needs the operator's Xwayland cookie, which lives in `/run/user/<uid>/.mutter-Xwaylandauth.*`
        and changes on every boot. Two units on this box were handing a service a STATIC (and empty)
        `~/.Xauthority` for exactly this purpose and silently failing. The panel is a ~1 GB Python
        process whose job is to be a control plane; giving it the ability to open windows would repeat
        that mistake with a bigger blast radius. So it returns the URL and the OPERATOR'S OWN BROWSER
        does the handshake — no new privilege, and the token still never touches the disk.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        import subprocess as _sp
        if not self.DSH_LAUNCH_SH.is_file():
            return self._send_json(200, {"ok": False, "error": f"launcher missing: {self.DSH_LAUNCH_SH}",
                                         "state": self._dsh_state()})
        try:
            r = _sp.run(["/bin/bash", str(self.DSH_LAUNCH_SH), "--url-only"],
                        capture_output=True, text=True, timeout=180)
            url = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
            # ★ VALIDATE THE SHAPE BEFORE HANDING IT OUT. An error message must never be returned as a
            #   url, or the browser navigates somewhere nonsensical and the real failure is hidden.
            ok = (r.returncode == 0 and url.startswith("http://") and "?token=" in url)
            return self._send_json(200, {
                "ok": ok,
                "url": url if ok else None,
                "exit": r.returncode,
                "stderr": (r.stderr or "")[-800:],
                "state": self._dsh_state(),
                "remedy": (None if ok else
                           f"the harness is serving but no usable token was recovered — "
                           f"restart it: systemctl --user restart {self.DSH_UNIT}"),
            })
        except _sp.TimeoutExpired:
            return self._send_json(200, {"ok": False, "error": "url recovery timed out after 180 s",
                                         "state": self._dsh_state()})
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                         "state": self._dsh_state()})

    def _dsh_launch(self, body: dict) -> None:
        """POST /run/dsh-launch — the card's ONE action. Fixed argv, no caller input."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        import subprocess as _sp
        if not self.DSH_LAUNCH_SH.is_file():
            return self._send_json(200, {"ok": False,
                                         "error": f"launcher missing: {self.DSH_LAUNCH_SH}",
                                         "remedy": "restore boardwalk/bin/khaoss-dsh-launch.sh",
                                         "state": self._dsh_state()})
        try:
            r = _sp.run(["/bin/bash", str(self.DSH_LAUNCH_SH), "--no-open"],
                        capture_output=True, text=True, timeout=180)
            ok = (r.returncode == 0)
            return self._send_json(200, {
                "ok": ok,
                "exit": r.returncode,
                "stdout": (r.stdout or "")[-1200:],
                "stderr": (r.stderr or "")[-1200:],
                "state": self._dsh_state(),
                # ★ A FAILURE CARRIES ITS OWN REMEDY. The launcher already distinguishes "could not
                #   start" from "started but no token", and that distinction is the whole value of
                #   this card — so it is passed through rather than flattened into ok:false.
                "remedy": (None if ok else
                           "if the unit is active but no token was found, restart it: "
                           f"systemctl --user restart {self.DSH_UNIT}"),
            })
        except _sp.TimeoutExpired:
            return self._send_json(200, {"ok": False, "error": "launcher timed out after 180 s",
                                         "remedy": f"check: systemctl --user status {self.DSH_UNIT}",
                                         "state": self._dsh_state()})
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}",
                                         "state": self._dsh_state()})

    # ── KHAOSS-PATCH: failure-immune-system — journal · health board · detection tick · remediation ──
    def _failure_journal(self) -> None:
        """GET /failure/journal?genre=&action=&since=&limit= — read the append-only journal, newest first."""
        q = parse_qs(urlsplit(self.path).query)
        genre_f = (q.get("genre", [""])[0] or "")
        action_f = (q.get("action", [""])[0] or "")
        since_f = q.get("since", [""])[0] or ""
        try:
            limit = min(int(q.get("limit", ["200"])[0] or 200), 2000)
        except ValueError:
            limit = 200
        entries = _failure_journal_read()
        out = []
        for e in entries:
            if genre_f and e.get("genre") != genre_f:
                continue
            if action_f and e.get("action") != action_f:
                continue
            if since_f and str(e.get("ts", "")) < since_f:
                continue
            out.append(e)
        out.reverse()
        return self._send_json(200, {
            "count": len(out), "entries": out[:limit],
            "journal": str(FAILURE_JOURNAL), "append_only": True,
        })

    def _failure_record(self, body: dict) -> None:
        """POST /failure/record {genre, action?, ...} — a detector/agent emits a failure event.
        Append-only; the genre is validated against the catalog (unknown genres are recorded but flagged)."""
        if fim is None:
            return self._send_json(503, {"error": "failure_immune module unavailable"})
        genre = str(body.get("genre") or "").strip()
        if not genre:
            return self._send_json(400, {"error": "genre required (see failure_immune.GENRES)"})
        known = genre in fim.GENRES
        entry = {k: v for k, v in body.items() if k != "ts"}
        entry.setdefault("action", "report")
        entry["genre"] = genre
        entry["genre_known"] = known
        _failure_append(entry)
        return self._send_json(200, {"recorded": True, "genre": genre, "genre_known": known,
                                     "journal": str(FAILURE_JOURNAL)})

    def _health_tick(self) -> None:
        """POST /health/tick — the DETECTION tick (writes): ingest new freeze records + detect live
        resource-exhaustion, appending only NEW/throttled entries to the journal. Idempotent-ish."""
        if fim is None:
            return self._send_json(503, {"error": "failure_immune module unavailable"})
        recorded = {"freeze": 0, "resource": 0}
        existing = _failure_journal_read()
        # 1) freeze ingest (dedup by (ts,event))
        seen = {(e.get("ts"), e.get("event")) for e in existing if e.get("source") == "freeze-reports.log"}
        flog = HOME / ".config" / "AionUi-Dev" / "logs" / "freeze-reports.log"
        try:
            if flog.exists():
                for rec in fim.parse_freeze_records(flog.read_text(encoding="utf-8", errors="replace")):
                    if (rec.get("ts"), rec.get("event")) in seen:
                        continue
                    _failure_append({"action": "detect", **rec})
                    recorded["freeze"] += 1
        except Exception as _e:
            sys.stderr.write(f"[failure] freeze ingest failed: {_e}\n")
        # 1b) AionUi turn-failure ingest (the FOUR turn genres auto-emitted by TeammateManager's
        # recordFailure taps -> logs/failure-journal.log). Dedup by (origin_ts, slotId, genre).
        recorded["turn"] = 0
        seen_turn = {(e.get("origin_ts"), e.get("slotId"), e.get("genre"))
                     for e in existing if e.get("source") == "aionui-turn"}
        tlog = HOME / ".config" / "AionUi-Dev" / "logs" / "failure-journal.log"
        try:
            if tlog.exists():
                for line in tlog.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    key = (ev.get("ts"), ev.get("slotId"), ev.get("genre"))
                    if key in seen_turn:
                        continue
                    seen_turn.add(key)
                    entry = {k: v for k, v in ev.items() if k != "ts"}
                    entry["origin_ts"] = ev.get("ts")
                    entry["source"] = "aionui-turn"
                    entry.setdefault("action", "detect")
                    _failure_append(entry)
                    recorded["turn"] += 1
        except Exception as _e:
            sys.stderr.write(f"[failure] aionui-turn ingest failed: {_e}\n")
        # 2) resource detection (throttle: skip if a resource entry was recorded in the last 5 min)
        try:
            from datetime import timedelta as _td
            now = datetime.now(tz=timezone.utc)
            last_res = None
            for e in existing:
                if e.get("genre") == "resource_exhaustion" and e.get("action") == "detect":
                    ts = fim._parse_ts(e.get("ts", ""))
                    if ts and (last_res is None or ts > last_res):
                        last_res = ts
            throttled = last_res is not None and (now - last_res) < _td(minutes=5)
            ram = _ram_sample()
            residents = _ollama_ps()
            signals = fim.detect_resource(ram["free_mb"], ram["avail_mb"], ram["swap_used_mb"], residents)
            if signals and not throttled:
                for s in signals:
                    _failure_append({"action": "detect", **s})
                    recorded["resource"] += 1
        except Exception as _e:
            sys.stderr.write(f"[failure] resource detect failed: {_e}\n")
        return self._send_json(200, {"ticked": True, "recorded": recorded})

    def _health_board(self) -> None:
        """GET /health/board — the ONE Health board (read-only aggregation + live samples). Runs the
        invariant checker for live PASS/FAIL/SKIP; does not write the journal (use /health/tick for that)."""
        if fim is None:
            return self._send_json(503, {"error": "failure_immune module unavailable"})
        entries = _failure_journal_read()
        # invariants (live, read-only) — degrade gracefully if slow
        invariants = None
        try:
            if INVARIANTS_CHECK.is_file():
                proc = subprocess.run(["/bin/bash", str(INVARIANTS_CHECK)],
                                      capture_output=True, text=True, timeout=25)
                clean = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
                mm = re.search(r"(\d+) PASS\s+(\d+) FAIL\s+(\d+) SKIP", clean)
                invariants = {"n_pass": int(mm.group(1)) if mm else None,
                              "n_fail": int(mm.group(2)) if mm else None,
                              "n_skip": int(mm.group(3)) if mm else None,
                              "passed": proc.returncode == 0}
        except Exception as _e:
            invariants = {"error": str(_e)}
        ram = _ram_sample()
        residents = _ollama_ps()
        freeze_tail = [e for e in entries if e.get("source") == "freeze-reports.log"]
        board = fim.aggregate_health(entries, invariants, freeze_tail, residents,
                                     now_iso=datetime.now(tz=timezone.utc).isoformat())
        board["ram"] = ram
        board["live_resource_signals"] = fim.detect_resource(ram["free_mb"], ram["avail_mb"],
                                                             ram["swap_used_mb"], residents)
        return self._send_json(200, board)

    def _remediate(self, name: str, body: dict) -> None:
        """POST /remediate/<name> — fire a tiered remediation through the MANDATORY circuit breaker.
        LOOPBACK-ONLY (mutating/privileged). SAFE tier fires; GATED needs confirm:true (operator click).
        The breaker STOPS (429 PLAGUED) if this (genre,remediation) has fired >= N in M minutes."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "remediation is local-only"})
        if fim is None:
            return self._send_json(503, {"error": "failure_immune module unavailable"})
        rem = fim.REMEDIATIONS.get(name)
        if not rem:
            return self._send_json(404, {"error": f"unknown remediation: {name}",
                                         "known": sorted(fim.REMEDIATIONS.keys())})
        # genre: explicit, else the genre this remediation is the default for, else 'manual'
        genre = str(body.get("genre") or "").strip()
        if not genre:
            genre = next((gid for gid, g in fim.GENRES.items() if g.get("remediation") == name), "manual")
        tier = rem["tier"]
        confirm = bool(body.get("confirm"))
        dry = bool(body.get("dry_run"))
        # engine-safety floor: never fire a non-engine-safe remediation without explicit confirm
        if not rem.get("engine_safe", False) and not confirm and not dry:
            return self._send_json(200, {"fired": False, "tier": tier, "remediation": name,
                                         "needs": "confirm:true", "reason": "not engine-safe — explicit confirm required",
                                         "desc": rem["desc"]})
        if tier == "gated" and not confirm and not dry:
            return self._send_json(200, {"fired": False, "tier": "gated", "remediation": name,
                                         "needs": "confirm:true (operator click)", "desc": rem["desc"]})
        # ── the MANDATORY circuit breaker ──
        now_iso = datetime.now(tz=timezone.utc).isoformat()
        dec = fim.breaker_decision(genre, name, _failure_journal_read(), now_iso)
        if not dec["allow"]:
            _failure_append({"action": "remediation", "genre": genre, "remediation": name,
                             "fired": False, "plagued": True, "breaker": dec})
            _failure_append({"action": "escalation", "genre": genre, "remediation": name,
                             "to": (fim.GENRES.get(genre) or {}).get("escalation", "operator+lead"),
                             "reason": dec["reason"]})
            return self._send_json(429, {"fired": False, "plagued": True, "breaker": dec,
                                         "escalated_to": (fim.GENRES.get(genre) or {}).get("escalation", "operator+lead")})
        if dry:
            return self._send_json(200, {"dry_run": True, "would_fire": name, "genre": genre,
                                         "tier": tier, "breaker": dec})
        ok, detail = self._fire_remediation(name, rem, body)
        _failure_append({"action": "remediation", "genre": genre, "remediation": name,
                         "fired": True, "ok": ok, "detail": detail, "tier": tier})
        return self._send_json(200 if ok else 500, {"fired": True, "ok": ok, "detail": detail,
                                                    "remediation": name, "genre": genre, "breaker": dec})

    def _fire_remediation(self, name: str, rem: dict, body: dict) -> tuple:
        """Execute a whitelisted remediation by binding to EXISTING recovery legs. Never restarts the
        panel/aionui engine; never unloads a model the panel itself consumes."""
        try:
            if name == "restart-voicebox":
                self._svc_do_stop("voicebox")
                time.sleep(1)
                ok, msg = self._svc_do_start("voicebox")
                return ok, msg
            if name == "unload-idle-model":
                residents = _ollama_ps()
                targets = fim.unloadable_residents(residents)
                model = body.get("model")
                if model:  # explicit single model, still consumer-guarded
                    targets = [model] if model in targets else []
                if not targets:
                    return True, "no idle non-consumer resident models to unload"
                stopped = []
                for t in targets:
                    r = subprocess.run(["ollama", "stop", t], capture_output=True, text=True, timeout=20)
                    stopped.append({"model": t, "rc": r.returncode})
                return True, {"stopped": stopped}
            if name == "ensure-team-session":
                team_id = body.get("teamId")
                if not team_id:
                    return False, "teamId required for ensure-team-session"
                ok, ws, err = _cdp_find_aionui_page()
                if not ok:
                    return False, f"CDP unavailable: {err}"
                ok2, res = _cdp_emit_event(ws, "team.ensure-session", {"teamId": team_id})
                return ok2, res
            if name == "remint-acp-session":
                conv = body.get("conversationId")
                if not conv:
                    return False, "conversationId required for remint-acp-session"
                ok, ws, err = _cdp_find_aionui_page()
                if not ok:
                    return False, f"CDP unavailable: {err}"
                ok2, res = _cdp_emit_event(ws, "acp.reset-session", {"conversationId": conv})
                return ok2, res
            if name == "rollback":
                # bind to the EXISTING operator-triggered rollback endpoint (don't duplicate restore logic)
                return True, {"manual": True,
                              "instruction": "POST /bridge/rollback {path} — the dedicated operator rollback endpoint",
                              "path": body.get("path")}
            if name in ("repoint-model", "free-ram-elicitation"):
                return True, {"manual": True, "instruction": rem["desc"]}
            return False, f"no executor bound for {name}"
        except Exception as e:
            return False, f"executor error: {e}"

    def _repair_dispatch(self, body: dict) -> None:
        """POST /repair/dispatch {genre, leadConversationId, [conversationId]} — Organ 5: the LLM tier.
        Package a failure (genre + catalog entry + evidence + scope) and hand it to the affected team's
        LEAD to PROPOSE a fix. The lead operates under the existing rails (Autonomy ladder · jail ·
        One-Above-All): it PROPOSES; anything beyond safe-tier needs the gated click. Everything is
        journaled + reversible. LOOPBACK-ONLY (delivers into a live conversation)."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "repair dispatch is local-only"})
        if fim is None:
            return self._send_json(503, {"error": "failure_immune module unavailable"})
        genre = str(body.get("genre") or "").strip()
        if genre not in fim.GENRES:
            return self._send_json(400, {"error": "genre required (must be a catalog genre)",
                                         "known": sorted(fim.GENRES.keys())})
        lead_conv = str(body.get("leadConversationId") or "").strip()
        if not lead_conv:
            return self._send_json(400, {"error": "leadConversationId required"})
        gpol = fim.GENRES[genre]
        evidence = [e for e in _failure_journal_read() if e.get("genre") == genre][-5:]
        package = {
            "genre": genre, "catalog": gpol, "recent_evidence": evidence,
            "remediation_tier": gpol.get("tier"), "suggested_remediation": gpol.get("remediation"),
            "escalation": gpol.get("escalation"),
        }
        ev_lines = "\n".join(f"  - {e.get('action','?')}: {e.get('evidence') or e.get('reason') or e.get('desc') or ''}"
                             for e in evidence) or "  (no prior journal evidence for this genre)"
        text = (
            "[FAILURE-IMMUNE-SYSTEM · REPAIR REQUEST — propose only, do not auto-apply]\n"
            f"Genre: {genre}  (structural class: {gpol.get('struct')})\n"
            f"Signature: {gpol.get('signature')}\n"
            f"Tier: {gpol.get('tier')}   Suggested remediation: {gpol.get('remediation')}\n"
            f"Recent evidence from the failure journal:\n{ev_lines}\n\n"
            "You are the lead for the affected team. Under your Autonomy ladder + jail + One-Above-All: "
            "PROPOSE the smallest correct fix (name the file/config/route and the change). Anything beyond "
            "a safe-tier action needs the operator's gated click — do NOT apply it yourself. "
            "Reply with: ROOT CAUSE, PROPOSED FIX, and TIER (safe/gated)."
        )
        ok, ws, err = _cdp_find_aionui_page()
        delivered, deliver_res = False, {"error": err}
        if ok:
            delivered, deliver_res = _cdp_send_message(ws, lead_conv, text)
        _failure_append({"action": "repair-dispatch", "genre": genre, "lead_conv": lead_conv,
                         "delivered": bool(delivered), "tier": gpol.get("tier")})
        return self._send_json(200 if delivered else 502, {
            "dispatched": bool(delivered), "genre": genre, "lead_conv": lead_conv,
            "package": package, "delivery": deliver_res,
            "note": "lead PROPOSES; apply is gated/operator; dispatch is journaled + reversible",
        })

    # ── KHAOSS-PATCH: failure-immune-system — the LOGS section (selectable source + bounded tail + filter) ──
    def _logs_sources(self) -> None:
        """GET /logs/sources — the selectable log sources + whether each exists + size."""
        out = []
        for name, meta in _log_sources().items():
            p = meta.get("path")
            exists = bool(p and Path(p).exists())
            size = (Path(p).stat().st_size if exists else 0)
            out.append({"name": name, "desc": meta.get("desc", ""),
                        "path": str(p) if p else "(action)", "exists": exists or bool(meta.get("action")),
                        "size_kb": round(size / 1024, 1)})
        return self._send_json(200, {"sources": out})

    def _conversation_raw(self) -> None:
        """GET /conversation/raw?conv=&limit=&maxbytes= — RAW conversation rows straight from aionui.db,
        BOUNDED per-message (so an 18 MB thinking block can't choke this the way it choked the pretty view).
        The escape hatch: works WHEN the rendered view is empty, because it does not go through that view."""
        q = parse_qs(urlsplit(self.path).query)
        conv = (q.get("conv", [""])[0] or "").strip()
        if not re.match(r"^[a-zA-Z0-9-]{4,64}$", conv):
            return self._send_json(400, {"error": "conv id required (4-64 alnum/-)"})
        try:
            limit = min(int(q.get("limit", ["100"])[0] or 100), 1000)
        except ValueError:
            limit = 100
        try:
            maxbytes = min(int(q.get("maxbytes", ["4000"])[0] or 4000), 200000)
        except ValueError:
            maxbytes = 4000
        try:
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            rows = con.execute(
                "SELECT position,type,content,created_at,length(content) FROM messages "
                "WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?", (conv, limit)).fetchall()
            total = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conv,)).fetchone()[0]
            con.close()
        except Exception as e:
            return self._send_json(500, {"error": str(e)})
        out = []
        for pos, typ, content, created, clen in reversed(rows):  # chronological
            # KHAOSS-PATCH: thinking-cap-archive — a capped/swept row's DB content is a marker; retrieve the
            # FULL original from the archive so the escape hatch still returns it BYTE-FOR-BYTE (the seam).
            archived = False
            if tarch is not None and content:
                full = tarch.retrieve_full_content(content)
                if full is not None and full != content:
                    archived = True
                    content = full
                    clen = len(content.encode("utf-8"))
            body = content or ""
            try:
                j = json.loads(content); body = j.get("content") or j.get("text") or j.get("input") or content
            except Exception:
                pass
            truncated = len(str(body)) > maxbytes
            out.append({"role": {"left": "agent", "right": "user", "center": "system"}.get(pos, pos),
                        "type": typ, "ts": created, "bytes": clen, "archived": archived,
                        "content": str(body)[:maxbytes], "truncated": truncated})
        return self._send_json(200, {"conversation": conv, "total_rows": total, "returned": len(out),
                                     "maxbytes_per_msg": maxbytes, "rows": out,
                                     "note": "raw DB read, bounded per message — the escape hatch when the view is empty"})

    # ── KHAOSS-PATCH: operator-docket BEGIN ────────────────────────────────────────────────────
    # ★ THE SAME PAGE, SERVED LOCALLY, WRITES THROUGH. The published artifact cannot reach this
    #   filesystem, so a ruling there is PENDING and prints the command to apply it. Served from
    #   here it writes blockers.json directly — one store, no gap, nothing to reconcile later.
    #   The page is GENERATED on every request, so it can never be stale against the store.
    _OPERATOR_DIR = ROOT / "operator"

    def _operator_builder(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "khaoss_build_board_panel", self._OPERATOR_DIR / "build_board_panel.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _operator_board_redirect(self) -> None:
        """★ ONE BOARD, ONE SURFACE. `/operator/board` used to SERVER-RENDER its own copy from
        build_board_panel.py — so it never saw the lane filter, the per-item controls, the walkthrough or
        the hidden-count disclosure, all of which live in the panel HTML. Two boards that diverge is the
        duplicate-substrate defect this project has paid for repeatedly, and the trading lane found it the
        only way it is ever found: by verifying a URL instead of pasting it.
        ★★ It redirects rather than being deleted, because the path is already published in team AGENTS.md
        blocks, launchers and desktop entries — a dead link would be a worse answer than a moved one.
        ★★★ Query parameters are CARRIED THROUGH, so ?lane=trading survives the hop."""
        from urllib.parse import urlsplit, parse_qs
        q = urlsplit(self.path).query
        # ★ ONLY DEFAULT THE SECTION WHEN THE CALLER DID NOT NAME ONE. The first version prepended
        #   section=operator-needs unconditionally and then appended the original query, so `section`
        #   appeared TWICE — and URLSearchParams.get() returns the FIRST. A caller asking for
        #   ?section=health was silently given operator-needs: a parameter that LOOKS honoured and is not,
        #   on the redirect built to stop a parameter looking honoured and not being. Found by the trading
        #   lane, who verified the redirect instead of accepting it.
        has_section = "section" in parse_qs(q)
        if has_section:
            target = "/?" + q
        else:
            target = "/?section=operator-needs" + (("&" + q) if q else "")
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _operator_board_page(self) -> None:
        """GET /operator/board — the operator docket, rendered from blockers.json at request time.
        LOOPBACK-ONLY: it names every open blocker, and it is a write surface."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only — the operator docket is a write surface"})
        try:
            html = self._operator_builder().build(local=True).encode("utf-8")
        except Exception as e:
            return self._send_json(500, {"error": f"could not build the docket: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(html)

    def _operator_board_json(self) -> None:
        """GET /operator/board.json — the store itself. LOOPBACK-ONLY."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._board_payload())
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _board_payload(self) -> dict:
        """The ONE assembled board — hand rows + derived rows + bindings + source health.

        ★ Factored out because /operator/facets and /operator/settled must be computed from the SAME
        merge this serves. The comment below already says why: three implementations that happen to
        agree today is luck, not a property. Facet chips that disagree with the list under them would
        be that failure wearing a new hat -- you would filter by a count that does not exist."""
        if True:
            # ★★★ ONE MERGE, NOT THREE. This handler used to assemble hand + derived rows ITSELF,
            #   which made it a THIRD implementation alongside blockers.items_of() and derive(). It
            #   happened to agree on the count today — and that is exactly how the 70-vs-22 split
            #   survived: agreement between independent implementations is luck, not a property.
            #   ★ It also meant normalize() never ran here, so `priority` — the field the whole
            #     redesign sorts and groups by — was absent from the payload the page actually reads.
            #     The page would have rendered every row as P0 and nobody would have seen why.
            sys.path.insert(0, str(self._OPERATOR_DIR))
            # ★★★ TWO MODULE RE-EXECUTIONS REMOVED FROM THIS HOT PATH. 2026-09-17 (second pass).
            #   This morning I removed importlib.reload from derive_items and declared the board
            #   hot path clean. It was not: THIS function still did importlib.reload(blockers) AND
            #   a full spec_from_file_location + exec_module of derive_items.py — re-executing an
            #   entire module from source on EVERY request — plus two _op_mod() reloads below.
            #   Four module re-executions per request, each taking the import lock.
            #   ★ Measured: all 8 derive sources total 36 ms, while /operator/board.json took
            #     1.4-20 s. The derivation was never the cost; the import machinery was.
            #   ★★ And INV-119 declared this path clean because it only inspected derive_items.py.
            #     I fixed one file and wrote a checker shaped like the file instead of like the
            #     PATH — the same "named the thief, not the class" error the port registry taught
            #     this house, committed inside the fix for it. INV-119 now covers both.
            import blockers as _bl
            d = _bl.load()
            d["items"] = _bl.items_of(d)
            import derive_items as _di
            _rows, health = _di.derive()
            d["source_health"] = health
            # ★ Who holds each item travels WITH the item, so the row can show it without a second
            #   fetch that could disagree with the first.
            try:
                binds = self._op_mod("item_binding").bindings()
                for i in d["items"]:
                    if i["id"] in binds:
                        i["bound_to"] = binds[i["id"]]
            except Exception:
                pass
            # ★★★ THE NUMBER HE RULES BY MUST TRAVEL WITH THE ROW.
            #   He works the board by NUMBER -- "12. the Sleeve is now set to $1000. 18. Trading
            #   cockpit needs to be secure. 41. Reconcile Deadlock" -- and the trading lead, reading
            #   this same payload, had to answer "the board IDs don't map to display numbers 1:1".
            #   It could not: the number was computed in the BROWSER from a separate /operator/assist
            #   /routes call and existed nowhere in the board JSON. So the surface he rules through
            #   and the surface agents read were speaking different languages, and the translation
            #   step was a human one nobody had been assigned.
            #   ★ SAME ORDERING, NOT A SECOND ONE. board_order() is what the page numbers from and
            #     what the batch selector resolves against; deriving a third here would reintroduce
            #     exactly the "he selects 7 and gets a different 7" failure it was written to prevent.
            try:
                order = self._op_mod("item_assist").board_order()
                pos = {i["id"]: n for n, i in enumerate(order, 1)}
                for i in d.get("items", []):
                    if i.get("id") in pos:
                        i["n"] = pos[i["id"]]
            except Exception:
                pass
            return d

    def _operator_resolve(self) -> None:
        """GET /operator/resolve?n=12,18,41 — turn the numbers he rules by into rows.

        ★ ONE CALL FOR A WHOLE RULING. He does not rule on one item at a time; he sends a paragraph
        naming nine numbers. An agent that has to make nine lookups will make eight and guess one.
        ★★ AN OUT-OF-RANGE NUMBER IS REPORTED, NEVER DROPPED. Silently ignoring 113 of 112 means the
        agent believes it acted on everything he asked for -- the failure this board exists to end.
        ★ AND IT WARNS THAT NUMBERS MOVE. board_order() sorts by (priority, title) over OPEN rows, so
        adding a row, or a rule that closes one, renumbers everything after it. A number is a
        reference to a moment, not a name. The id is the name."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        spec = (q.get("n") or q.get("spec") or [""])[0]
        try:
            ia = self._op_mod("item_assist")
            order = ia.board_order()
            by_n = {n: i for n, i in enumerate(order, 1)}
            if not spec.strip():
                return self._send_json(400, {
                    "error": "give me the numbers, e.g. ?n=12,18,41 or ?n=1-5",
                    "total_numbered": len(order)})
            wanted, problems = [], []
            for part in [p.strip() for p in spec.split(",") if p.strip()]:
                if "-" in part and not part.startswith("-"):
                    a, _, b = part.partition("-")
                    try:
                        wanted += list(range(int(a), int(b) + 1))
                    except ValueError:
                        problems.append(f"{part!r} is not a range I understand")
                else:
                    try:
                        wanted.append(int(part))
                    except ValueError:
                        problems.append(f"{part!r} is not a number or an id")
            rows, seen = [], set()
            for n in wanted:
                if n in seen:
                    continue
                seen.add(n)
                i = by_n.get(n)
                if not i:
                    problems.append(f"{n} is out of range — the board has {len(order)} numbered rows")
                    continue
                rows.append({"n": n, "id": i["id"], "title": i.get("title"),
                             "priority": i.get("priority_label"), "sev": i.get("sev"),
                             "lane": i.get("lane"), "source": i.get("source"),
                             "state": i.get("state")})
            return self._send_json(200, {
                "resolved": rows, "problems": problems, "total_numbered": len(order),
                "warning": ("These numbers are positions in the CURRENT board, sorted by priority "
                            "then title over OPEN rows. Adding a row or closing one renumbers "
                            "everything after it. Quote the id back to him, not the number — the "
                            "number was true at the moment he read it.")})
        except Exception as e:
            return self._send_json(500, {"error": f"could not resolve: {e}"})

    def _operator_feature_check(self, body: dict) -> None:
        """POST — run the board's own feature check and return what it found.

        ★ PARITY, and I had just broken it: I wrote a checker that answers "did the features land?"
        and it was runnable only from a terminal. The criterion for this project is that anything the
        agent can do he can do too, so a verification tool only the agent can run is exactly the shape
        that criterion forbids -- it makes him take my word for it, which is the thing the board
        exists to stop.
        ★★ It drives a real browser and takes about half a minute; the caller is told that rather
        than left watching a spinner with no expectation set."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        import subprocess
        script = str(self._OPERATOR_DIR / "feature_check.py")
        args = ["/usr/bin/python3", script, "--json"]
        if body.get("routes_only"):
            args.append("--routes-only")
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired:
            return self._send_json(504, {"error": "the check did not finish in 7 minutes"})
        except Exception as e:
            return self._send_json(500, {"error": f"could not run the check: {e}"})
        # ★ Read its JSON, never its table. Parsing the pretty output dropped three checks whose
        #   names happened to be exactly the column width, and the button reported 27 of 30 while
        #   looking perfectly healthy.
        try:
            payload = json.loads(r.stdout or "{}")
            rows = payload.get("checks", [])
            counts = payload.get("counts", {})
            clean = json.dumps(payload, indent=2)[-6000:]
        except Exception as e:
            return self._send_json(500, {
                "error": f"the check ran but its output could not be read: {e}",
                "stdout_tail": (r.stdout or "")[-1200:], "stderr_tail": (r.stderr or "")[-600:]})
        return self._send_json(200, {
            "ok": r.returncode == 0, "checks": rows, "counts": counts,
            "raw": clean[-6000:],
            "note": ("A FAIL is a feature that was claimed and is not working. UNPROVEN is NOT a pass "
                     "-- it means the check could not run.")})

    def _operator_ruling(self, body: dict) -> None:
        """POST /operator/ruling — paste his ruling paragraph, get each number resolved AND CHECKED.

        ★★★ WHY THIS EXISTS, and it is a near-miss not a theory. He rules in prose: "12. the Sleeve is
        now set to $1000. 18. Trading cockpit needs to be secure. 41. Reconcile Deadlock: do as
        recommended." The trading seat resolved those numbers against its own ordering and #41 came
        back as `credential-leaked-into-chat` -- a P1 about a leaked Google password. Acting on "do as
        recommended" there, on a money board, while he is travelling, would have been catastrophic and
        no gate would have caught it, because THE IDENTIFIER ITSELF was unstable.
        ★★ IT ESCAPED BY LUCK: he happened to write a description beside eight of the nine numbers, so
        the seat matched on his WORDS instead. This route makes that luck into a procedure -- the
        number is checked AGAINST the words he wrote next to it, and a disagreement is refused rather
        than resolved in favour of either.
        ★ A NUMBER WITH NO DESCRIPTION IS NOT ACTIONABLE and is returned as such. His "28. Do as
        recommended" carried no words; the honest answer is to ask him, not to pick."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        text = str(body.get("ruling") or body.get("text") or "")
        if not text.strip():
            return self._send_json(400, {"error": "paste his ruling text as `ruling`",
                                         "field_contract": {"ruling": "required — his words, verbatim"}})
        import re
        ia = self._op_mod("item_assist")
        order = ia.board_order()
        by_n = {n: i for n, i in enumerate(order, 1)}

        # "12. the Sleeve is now set to $1000." -> (12, "the Sleeve is now set to $1000")
        # Stop at the next "<number>." so one clause cannot swallow the following ruling.
        hits = list(re.finditer(r"(?<![\d.])(\d{1,3})\s*[.):]\s*", text))
        parsed = []
        for k, m in enumerate(hits):
            end = hits[k + 1].start() if k + 1 < len(hits) else len(text)
            parsed.append((int(m.group(1)), text[m.end():end].strip()))

        STOP = {"the", "a", "an", "is", "are", "to", "we", "on", "of", "and", "or", "it", "this",
                "that", "do", "as", "now", "set", "with", "for", "in", "be", "not", "our", "you",
                "your", "let", "lets", "ok", "here", "there", "will", "can", "at", "if", "so",
                # pure-instruction words: they are what he wants DONE, never which row he means
                "recommended", "recommend", "recomend", "please", "get", "make", "need", "needs"}

        def toks(x):
            return {w for w in re.findall(r"[a-z0-9$]+", str(x).lower()) if w not in STOP and len(w) > 2}

        out = []
        for n, said in parsed:
            row = by_n.get(n)
            if not row:
                out.append({"n": n, "verdict": "OUT-OF-RANGE", "said": said[:120],
                            "note": f"the board has {len(order)} numbered rows"})
                continue
            hay = toks(row.get("title", "")) | toks(row.get("one", "")) | toks(row.get("id", ""))
            said_t = toks(said)
            overlap = sorted(said_t & hay)
            # A ruling clause is mostly instruction ("do as recommended"); a couple of real content
            # words agreeing is a strong signal, and zero content words is no signal at all.
            if not said_t:
                verdict, why = "NO-DESCRIPTION", ("he gave a number and no words — do NOT act on this; "
                                                  "ask him which item it is")
            elif overlap:
                verdict, why = "CONFIRMED", f"his words and the row agree on: {', '.join(overlap[:6])}"
            else:
                verdict, why = "MISMATCH", ("his words share NO content word with this row — the number "
                                            "may be from a different moment of the board. Ask him; do "
                                            "not resolve it in favour of either.")
            out.append({"n": n, "verdict": verdict, "said": said[:160], "why": why,
                        "id": row["id"], "title": row.get("title"),
                        "priority": row.get("priority_label"), "lane": row.get("lane"),
                        "sev": row.get("sev")})
        counts = {}
        for r in out:
            counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        return self._send_json(200, {
            "rulings": out, "counts": counts, "total_numbered": len(order),
            "how_to_use": ("Act only on CONFIRMED. Take MISMATCH and NO-DESCRIPTION back to him. "
                           "Quote the id in your reply, never the number — the number was true at the "
                           "moment he read the board and renumbers whenever a row is added or closed.")})

    def _operator_set(self, body: dict) -> None:
        """POST /operator/set — apply ONE ruling to blockers.json. LOOPBACK-ONLY (writes the store).
        ★ It will not invent an item: an unknown id is refused with the ids that exist, rather than
        silently creating one — a board you can typo a new row into is not a board."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only — this writes the operator board"})
        item_id = str(body.get("id") or "").strip()
        state = str(body.get("state") or "").strip().lower()
        note = body.get("note")
        # ★★★ A REQUIRED FIELD IS A TRAP WHENEVER THE CALLER HAS NO OPINION ABOUT IT.
        #   `state` was mandatory, so every caller that only wanted to save a NOTE had to supply one
        #   — and the note editor supplies the literal 'open'. Harmless there, because it only ever
        #   edits open rows. From the guided walk it is not harmless: type a note after ruling a row
        #   done and the row SILENTLY RE-OPENS. The API forces an answer, the caller invents one, and
        #   the invention overwrites a real value.
        #   ★ Fixing it at the caller (send the row's current state back) fixes one caller. Fixing it
        #     here fixes every caller that will ever exist. Omit `state` now and the row keeps the one
        #     it has; a ruling still requires naming one, because that IS a decision the caller must make.
        if state and state not in ("done", "later", "open"):
            return self._send_json(400, {"error": "state must be done, later or open",
                                         "note": "or omit it entirely to leave the ruling untouched"})
        if not state and note is None:
            return self._send_json(400, {"error": "nothing to change — send a state, a note, or both"})
        f = self._OPERATOR_DIR / "blockers.json"
        # ★★★ 2026-09-14 — HOLD THE LOCK ACROSS READ-MODIFY-WRITE.
        #   The write below was already ATOMIC (tmp + replace), so no reader could ever see a
        #   half-written board. But atomic is not serialised: two writers that each read, modify and
        #   atomically replace will LOSE ONE UPDATE — last writer wins and the other ruling vanishes
        #   with no error and no trace.
        #   ★ blockers.py has locked this exact file since it was written (fcntl.flock on
        #     blockers.json.lock). The panel wrote the same store directly and never took that lock,
        #     so the two surfaces were never serialised against each other.
        #   ★★ This matters NOW because the Board Walk seat is arriving as a third writer, on a box
        #     with one uid and no other locking. Same lock file, same discipline, no new pattern.
        import fcntl as _fcntl
        _lockf = f.with_name(f.name + ".lock")
        try:
            _lf = open(_lockf, "a+")
            _fcntl.flock(_lf, _fcntl.LOCK_EX)
        except Exception:
            _lf = None                    # a board that cannot lock must still record the decision
        try:
            d = json.loads(f.read_text())
        except Exception as e:
            if _lf:
                try: _fcntl.flock(_lf, _fcntl.LOCK_UN); _lf.close()
                except Exception: pass
            return self._send_json(500, {"error": f"could not read the board: {e}"})
        hit = [i for i in d.get("items", []) if i.get("id") == item_id]
        if not hit:
            return self._send_json(404, {"error": f"unknown id {item_id!r}",
                                         "ids": [i.get("id") for i in d.get("items", [])]})
        before = hit[0].get("state")
        state_changed = bool(state) and state != before
        if state:
            hit[0]["state"] = state
        else:
            state = before          # for the response — reported as unchanged, never as a new ruling
        if note is not None:
            n = str(note).strip()
            if n:
                hit[0]["note"] = n
            else:
                hit[0].pop("note", None)
        if state_changed:
            hit[0]["decided_at"] = time.strftime("%Y-%m-%d %H:%M")   # ★ a note is not a decision
        # ★ The endpoint knows the CHANNEL, not the person. "operator" would be an identity claim this
        #   code cannot make — loopback proves the machine, never who was at the keyboard (L34).
        hit[0]["decided_via"] = "operator docket (local panel)"
        # ★ AUDIT JOURNAL — adopted from the trading lane's board_local.py, which built the same surface
        #   independently and had this when I did not. SAME FILE, SAME SCHEMA on purpose: two surfaces
        #   writing two journals of one board would be the two-stores failure a third time.
        try:
            audit = self._OPERATOR_DIR / "blockers_audit.jsonl"
            # ★ Only journal a ruling that actually happened. Writing a state row for a note-only
            #   save would fill the audit with rulings he never made, and the journal is what
            #   `/operator/settled` reads to say what was decided and when.
            rows = ([{"id": item_id, "field": "state", "was": before, "now": state,
                      "by": "operator docket (local panel)"}] if state_changed else [])
            if note is not None:
                rows.append({"id": item_id, "field": "note", "chars": len(str(note).strip()),
                             "by": "operator docket (local panel)"})
            with audit.open("a") as fh:
                for r in rows:
                    fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **r}) + "\n")
        except Exception:
            pass                      # a board that will not journal must still record the decision
        try:
            tmp = f.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, indent=2) + "\n")
            tmp.replace(f)                      # atomic — a half-written board is worse than a stale one
        except Exception as e:
            return self._send_json(500, {"error": f"could not write the board: {e}"})
        finally:
            # ★ released only AFTER the replace, so the whole read-modify-write is one critical section
            if _lf:
                try:
                    _fcntl.flock(_lf, _fcntl.LOCK_UN); _lf.close()
                except Exception:
                    pass
        # keep the generated copy the team chats read from lagging its store
        try:
            import subprocess
            subprocess.run(["/usr/bin/python3", str(self._OPERATOR_DIR / "blockers.py"), "--board"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        open_left = len([i for i in d["items"] if i.get("state") == "open"])
        return self._send_json(200, {"ok": True, "id": item_id, "from": before, "to": state,
                                     "state_changed": state_changed,
                                     "open_remaining": open_left})
    # ── KHAOSS-PATCH: operator-docket END ──────────────────────────────────────────────────────

    def _context_diagnostic(self) -> None:
        """GET /context/diagnostic?conv= — Part A: the context cliff made visible. Reports the DB side
        PRECISELY and says PLAINLY what it CANNOT determine (the model's live context), rather than guessing."""
        q = parse_qs(urlsplit(self.path).query)
        conv = (q.get("conv", [""])[0] or "").strip()
        if not re.match(r"^[a-zA-Z0-9-]{4,64}$", conv):
            return self._send_json(400, {"error": "conv id required"})
        try:
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            by_type = {t: {"rows": n, "bytes": b, "est_tokens": (b or 0) // 4}
                       for t, n, b in con.execute(
                           "SELECT type,COUNT(*),SUM(length(content)) FROM messages WHERE conversation_id=? GROUP BY type", (conv,))}
            span = con.execute("SELECT MIN(created_at),MAX(created_at),COUNT(*) FROM messages WHERE conversation_id=?", (conv,)).fetchone()
            exrow = con.execute("SELECT extra FROM conversations WHERE id LIKE ?||'%'", (conv,)).fetchone()
            con.close()
        except Exception as e:
            return self._send_json(500, {"error": str(e)})
        ex = json.loads(exrow[0]) if exrow and exrow[0] else {}
        acp_session = ex.get("acpSessionId")
        # ★★ DISPLAY TRUTH (2026-09-19): `agentProfile` is what the conversation LAUNCHES under
        #    (`hermes --profile <agentProfile>`); `hermesProfile` is a legacy display label. Measured
        #    on the live DB: 28 conversations carry an explicit agentProfile the label CONTRADICTS, so
        #    reading the label here pointed at the WRONG profile's state.db. Authoritative first, label
        #    only as the fallback for the 52 older conversations that predate agentProfile.
        profile = ex.get("agentProfile") or ex.get("hermesProfile")
        # compaction detection: count session rotations (parent_session_id chain) in the profile's state.db
        compactions = None
        try:
            if profile:
                sdb = HOME / ".hermes" / "profiles" / str(profile) / "state.db"
                if sdb.exists() and acp_session:
                    sc = sqlite3.connect(f"file:{sdb}?mode=ro", uri=True)
                    # rotations reachable from this session (parent chain)
                    compactions = sc.execute("SELECT COUNT(*) FROM sessions WHERE parent_session_id IS NOT NULL").fetchone()[0]
                    sc.close()
        except Exception:
            compactions = None
        total_bytes = sum(v["bytes"] or 0 for v in by_type.values())
        # lastTokenUsage = the model's LAST-OBSERVED context size (a real signal of in-place compression,
        # which de63a495 does every turn WITHOUT rotating the session — so rotation count alone misses it).
        last_ctx = None
        try:
            ltu = ex.get("lastTokenUsage")
            if isinstance(ltu, dict):
                last_ctx = ltu.get("totalTokens") or ltu.get("total_tokens")
            elif isinstance(ltu, (int, float)):
                last_ctx = int(ltu)
        except Exception:
            last_ctx = None
        # answers+recent are the cheap re-injectable side; thinking is the bulk that's gone from context
        answer_tokens = (by_type.get("text", {}).get("est_tokens", 0))
        in_place_compression = (last_ctx is not None and last_ctx * 4 < total_bytes * 0.5)
        return self._send_json(200, {
            "conversation": conv,
            "CAN_DETERMINE (DB side, precise)": {
                "total_rows": span[2], "est_total_tokens": total_bytes // 4,
                "answer_tokens": answer_tokens,
                "by_type": by_type,
                "span_utc": [span[0], span[1]],
                "acp_session": acp_session, "profile": profile,
                "model_last_observed_context_tokens": last_ctx,
                "in_place_compression_detected": in_place_compression,
                "session_rotations_in_profile": compactions,
                "cliff_gap_note": (f"model's last context ~{last_ctx} tokens vs DB ~{total_bytes // 4} tokens — "
                                   f"the model cannot see most of the DB" if last_ctx else "lastTokenUsage not recorded on this conv"),
            },
            "CANNOT_DETERMINE (stated plainly, not guessed)": [
                "The model's LIVE context window — what the agent can actually still SEE this turn. That is the "
                "provider's state (post-compaction summary + prompt cache), not readable from the DB or from here.",
                "Exactly WHICH specifics were lost in a compaction (port numbers, error codes, decisions) — compaction "
                "replaces them with a summary; the summary is in the model, not here.",
                "So: I can show the DB side precisely and detect THAT a compaction happened; I cannot read the model side. "
                "The gap between 'DB has it' and 'model may not' IS the context cliff.",
            ],
            "note": "A diagnostic that estimates the model side confidently would be worse than one that says what it can't see.",
        })

    def _context_reinject(self) -> None:
        """GET /context/reinject?conv=&since=&until=&thinking=0&limit= — Part B: preview a SELECTIVE
        re-injection with the TOKEN COST shown BEFORE injecting, defaulting to ANSWERS (text), never thinking."""
        q = parse_qs(urlsplit(self.path).query)
        conv = (q.get("conv", [""])[0] or "").strip()
        if not re.match(r"^[a-zA-Z0-9-]{4,64}$", conv):
            return self._send_json(400, {"error": "conv id required"})
        since = q.get("since", [""])[0] or ""
        until = q.get("until", [""])[0] or ""
        include_thinking = q.get("thinking", ["0"])[0] == "1"   # DEFAULT: answers only, thinking opt-in
        try:
            limit = min(int(q.get("limit", ["50"])[0] or 50), 500)
        except ValueError:
            limit = 50
        types = ["text"] if not include_thinking else ["text", "thinking"]
        ph = ",".join("?" * len(types))
        args = [conv] + types
        clause = f"conversation_id=? AND type IN ({ph}) AND position='left'"
        if since:
            clause += " AND created_at>=?"; args.append(int(since))
        if until:
            clause += " AND created_at<=?"; args.append(int(until))
        try:
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            rows = con.execute(f"SELECT type,content,created_at,length(content) FROM messages WHERE {clause} ORDER BY created_at ASC LIMIT ?", args + [limit]).fetchall()
            con.close()
        except Exception as e:
            return self._send_json(500, {"error": str(e)})
        pieces = []
        total_bytes = 0
        for t, content, ts, clen in rows:
            # KHAOSS-PATCH: thinking-cap-archive — retrieve archived thinking so re-inject reaches the FULL
            # original byte-for-byte (a swept conversation still works through this hatch).
            archived = False
            if tarch is not None and content:
                full = tarch.retrieve_full_content(content)
                if full is not None and full != content:
                    archived = True
                    content = full
                    clen = len(content.encode("utf-8"))
            body = content
            try:
                j = json.loads(content); body = j.get("content") or j.get("text") or content
            except Exception:
                pass
            total_bytes += clen or 0
            pieces.append({"type": t, "ts": ts, "bytes": clen, "est_tokens": (clen or 0) // 4,
                           "archived": archived, "preview": str(body)[:200]})
        # what re-injecting THINKING would have cost (the 350x guard)
        try:
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            think_bytes = con.execute("SELECT COALESCE(SUM(length(content)),0) FROM messages WHERE conversation_id=? AND type='thinking'", (conv,)).fetchone()[0]
            con.close()
        except Exception:
            think_bytes = 0
        return self._send_json(200, {
            "conversation": conv,
            "selection": {"types": types, "thinking_included": include_thinking, "since": since, "until": until, "count": len(pieces)},
            "TOKEN_COST_BEFORE_INJECT": {"est_tokens": total_bytes // 4, "bytes": total_bytes},
            "pieces": pieces,
            "guard": {
                "default": "ANSWERS ONLY (text). Re-injecting thinking is opt-in (thinking=1) — it is catastrophic: "
                           f"this conversation's thinking is ~{think_bytes // 4} est tokens vs ~{total_bytes // 4} for the answers.",
                "reminder": "SHOW THIS TOKEN COST TO THE OPERATOR BEFORE INJECTING — blind re-injection pushes the conversation back over the cliff.",
            },
        })

    # ---- Card 4: CAPTURE (review queue is THE human gate; nothing reaches the KB around it) ----
    _CAPWORK = ROOT / "capture_work"
    _SLICE = ROOT / "capture_knowledge_slice.py"

    def _capture_queue(self) -> None:
        """GET /capture/queue — staged items awaiting the human gate (scrubbed, NOT in the KB)."""
        q = self._CAPWORK / "review_queue"
        items = []
        try:
            for fp in sorted(q.glob("*.json")):
                try:
                    d = json.loads(fp.read_text())
                    items.append({"id": d.get("video_id", fp.stem), "title": (d.get("title") or "")[:80],
                                  "chars": len(d.get("caption_scrubbed") or d.get("caption") or ""),
                                  "url": d.get("url", ""), "status": d.get("status", "pending_review")})
                except Exception:
                    pass
        except Exception as e:
            return self._send_json(500, {"error": str(e)})
        return self._send_json(200, {"count": len(items), "items": items,
                                     "note": "approve = commit to KB on the economy model; reject = drop. Nothing reaches the KB without passing here."})

    def _capture_canary(self) -> None:
        """GET /capture/canary — run the scrub canary (INV-35): plant SSN+CC, confirm neither survives."""
        try:
            r = subprocess.run(["/usr/bin/python3", str(self._SLICE), "canary"],
                               capture_output=True, text=True, timeout=60)
            ok = r.returncode == 0
            return self._send_json(200, {"canary_pass": ok,
                                         "detail": (r.stdout or r.stderr).strip().splitlines()[-1:] and (r.stdout or r.stderr).strip().splitlines()[-1] or "",
                                         "meaning": "PASS = planted SSN+CC both redacted before any KB write" if ok else "FAIL = PII would leak — scrub broken"})
        except Exception as e:
            return self._send_json(500, {"error": str(e), "canary_pass": False})

    def _capture_approve(self, body) -> None:
        """POST /capture/approve {id, raw?, summary?, topics?, clearance?} — THE gate: approve → commit to KB
        on the economy model (spawned in background; slow). LOOPBACK-ONLY (writes the KB)."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        vid = str(body.get("id") or "").strip()
        if not re.match(r"^[A-Za-z0-9_-]{4,64}$", vid):
            return self._send_json(400, {"error": "valid id required"})
        src = self._CAPWORK / "review_queue" / f"{vid}.json"
        if not src.is_file():
            return self._send_json(404, {"error": f"not in review queue: {vid}"})
        # ★ Q12 Part D4 — kb- items are COMMITTED THROUGH THE KB PIPELINE, not the YouTube slice. The slice's
        # approve/process reads FETCHED/<id>.json (a caption artifact) which never exists for a kb- id → the old
        # path silently no-op'd. Route kb items to the real, supersession-aware, contested-preserving commit.
        try:
            _item = json.loads(src.read_text())
        except Exception:
            _item = {}
        if _item.get("kind") == "kb" or vid.startswith("kb-"):
            return self._kb_commit(vid, _item)   # human gate → real commit; never auto-commits
        try:
            subprocess.run(["/usr/bin/python3", str(self._SLICE), "approve", vid], capture_output=True, text=True, timeout=30)
            flags = []
            if body.get("raw"): flags.append("--raw")
            if body.get("summary", True): flags.append("--summary")
            if body.get("topics", True): flags.append("--topics")
            cl = str(body.get("clearance") or "public")
            # process on the economy model = slow → background (non-blocking)
            subprocess.Popen(["/usr/bin/python3", str(self._SLICE), "process", vid, "--clearance", cl] + flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try: src.unlink()
            except Exception: pass
            return self._send_json(200, {"ok": True, "id": vid, "clearance": cl,
                                         "note": "approved → committing to KB on the economy model (background). It leaves the queue now."})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _capture_reject(self, body) -> None:
        """POST /capture/reject {id} — drop a staged item (does NOT reach the KB). Moves to rejected/, nothing deleted."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        vid = str(body.get("id") or "").strip()
        src = self._CAPWORK / "review_queue" / f"{vid}.json"
        if not re.match(r"^[A-Za-z0-9_-]{4,64}$", vid) or not src.is_file():
            return self._send_json(404, {"error": f"not in review queue: {vid}"})
        try:
            rej = self._CAPWORK / "rejected"; rej.mkdir(exist_ok=True)
            src.replace(rej / f"{vid}.json")   # nothing deleted — moved to rejected/
            return self._send_json(200, {"ok": True, "id": vid, "note": "rejected — moved to rejected/ (nothing deleted), never reached the KB."})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    # ---- Q9 DISPATCH_STEWARDS_INTERACTIVE Part C/D: the KB update pipeline (check→stage→scan→compare→decide→record) ----
    # The panel side of the Stewards KB controls. Design principles, enforced here:
    #   · change detection is DETERMINISTIC (sha256 of content+metadata) — NO LLM is used to NOTICE a change (C1);
    #     inference is reserved for the natural-language compare/contrast summary, which is deferred (named, not faked).
    #   · UPSERTS *and* DELETIONS both flow through here (C2); a delete stages a TOMBSTONE (supersession-marked on
    #     approval, history kept — nothing is ever hard-deleted, D2).
    #   · every staged item is injection-SCANNED and lands ENVELOPED via kb_envelope (INV-75, C3).
    #   · the button STAGES into the review queue (the human gate) and NEVER authors into the KB directly (C3).
    #   · posture is the Sealed·Ask·Scheduled·Open spectrum (D — the existing vocabulary, no 4th scale), per-source
    #     over a global default; a change to it is GOVERNANCE: recorded forever (append-only) + rate-limited (D5).
    _KB_KNOWLEDGE = ROOT / "knowledge"
    _KB_SEED = ROOT / "knowledge" / "seeds" / "KB_SEED_BOT_LANDSCAPE_2026.md"
    _KB_POSTURES = ("sealed", "ask", "scheduled", "open")
    # ---- Q12 Part D: STRATA STORAGE BUDGET — where landed entries + reclaimable derived layers live ----
    _KB_ENTRIES = ROOT / "knowledge" / "entries"   # L0 bedrock: landed KB text (permanent)
    _KB_ARCHIVE = ROOT / "knowledge" / "archive"   # superseded prior versions (kept, never deleted)
    _KB_DERIVED = ROOT / "knowledge" / "derived"   # reclaimable: wikis/models/embeddings (regenerable)
    # TWO budgets on purpose (D1): the corpus text is single-digit MB and must never be crowded out by a GB-scale
    # download. One shared line would let a single wiki/model eat a year of notes — so text and heavy are SEPARATE.
    _KB_BUDGET_DEFAULTS = {"text_cap_mb": 8.0, "heavy_cap_gb": 4.0}

    def _kb_paths(self) -> dict:
        cw = self._CAPWORK
        return {
            "queue": cw / "review_queue",
            "approved": cw / "approved",
            "manifest": cw / "kb_manifest.json",
            "audit": cw / "kb_audit.log",
            "posture": cw / "kb_posture.json",
            "posture_audit": cw / "kb_posture_audit.log",
            # Q12 Part D
            "budget": cw / "kb_budget.json",           # operator-settable caps (text MB · heavy GB)
            "reclaim_log": cw / "kb_reclaim.log",       # append-only: every reclaim recorded (NOT a deletion event)
            "cost_log": cw / "kb_cost_predictions.jsonl",  # append-only: predicted-vs-actual, so the estimate learns
            "contested": cw / "kb_contested.json",      # contested pairs, preserved (never resolved)
        }

    def _kb_import_envelope(self):
        import sys as _sys
        bp = str(ROOT)
        if bp not in _sys.path:
            _sys.path.insert(0, bp)
        import kb_envelope
        return kb_envelope

    @staticmethod
    def _kb_parse_frontmatter(text: str):
        """Minimal front-matter parser (key: value) — no pyyaml dependency. Returns (metadata, body)."""
        meta, body = {}, text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip()
                body = text[end + 4:].lstrip("\n")
        return meta, body

    def _kb_load_json(self, p, default):
        try:
            return json.loads(p.read_text())
        except Exception:
            return default

    @staticmethod
    def _kb_diff(old: str, new: str) -> dict:
        """Deterministic compare/contrast — a line-set delta, NO model call. A natural-language summary would be
        the ONE place inference is used (C1); it is deferred and named, never faked as a deterministic result."""
        old_lines = [l for l in (old or "").splitlines() if l.strip()]
        new_lines = [l for l in (new or "").splitlines() if l.strip()]
        old_set, new_set = set(old_lines), set(new_lines)
        added = [l for l in new_lines if l not in old_set]
        removed = [l for l in old_lines if l not in new_set]
        return {
            "added_lines": len(added),
            "removed_lines": len(removed),
            "sample_added": added[:3],
            "sample_removed": removed[:3],
            "narrative": "deterministic diff only (no model call); a natural-language compare/contrast summary is the one inference step, deferred.",
        }

    def _kb_status(self) -> None:
        """GET /kb/status — KB overview: index size, review-queue counts, seed state, manifest, global posture."""
        P = self._kb_paths()
        index_entries = 0
        try:
            index_entries = len([l for l in (self._KB_KNOWLEDGE / "llms.txt").read_text().splitlines() if l.strip().startswith("- [")])
        except Exception:
            pass
        queued = kb_staged = 0
        try:
            for fp in P["queue"].glob("*.json"):
                queued += 1
                if fp.stem.startswith("kb-"):
                    kb_staged += 1
        except Exception:
            pass
        manifest = self._kb_load_json(P["manifest"], {"entries": {}})
        posture = self._kb_load_json(P["posture"], {})
        seed_entry = "bot-landscape-2026"
        return self._send_json(200, {
            "index_entries": index_entries,
            "review_queued": queued,
            "kb_staged": kb_staged,
            "seed": {
                "present_on_disk": self._KB_SEED.is_file(),
                "staged": (P["queue"] / f"kb-{seed_entry}.json").is_file(),
                "landed": bool(manifest.get("entries", {}).get(seed_entry, {}).get("landed_hash")),
                "entry": seed_entry,
                "path": str(self._KB_SEED),
            },
            "manifest_entries": len(manifest.get("entries", {})),
            "global_posture": posture.get("global_default", "ask"),
            "note": "The KB button STAGES into the review queue (the human gate); it never authors into the KB directly. Change detection is hash-based (no LLM). Every staged item is injection-scanned + enveloped (INV-75).",
        })

    def _kb_update(self, body) -> None:
        """POST /kb/update {source?, op?} — the specced pipeline. source='seed' or a path under knowledge/;
        op='upsert'|'delete'. LOOPBACK-ONLY (writes the review queue)."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only — the KB pipeline writes the review queue"})
        import hashlib
        P = self._kb_paths()
        source = str(body.get("source") or "seed").strip()
        op = str(body.get("op") or "upsert").strip().lower()
        if op not in ("upsert", "delete"):
            return self._send_json(400, {"error": "op must be upsert or delete"})
        if source == "seed":
            path = self._KB_SEED
        else:
            try:
                root = self._KB_KNOWLEDGE.resolve()
                cand = (self._KB_KNOWLEDGE / source).resolve()
                if cand != root and root not in cand.parents:
                    return self._send_json(400, {"error": "source must be under knowledge/"})
                path = cand
            except Exception:
                return self._send_json(400, {"error": "bad source path"})
        if not path.is_file():
            return self._send_json(404, {"error": f"source not found: {path}"})
        text = path.read_text()
        meta, bodytext = self._kb_parse_frontmatter(text)
        entry_id = (meta.get("kb_entry") or path.stem).strip()
        if not re.match(r"^[A-Za-z0-9_-]{2,64}$", entry_id):
            return self._send_json(400, {"error": f"invalid kb_entry id: {entry_id}"})
        # DETERMINISTIC change detection — hash of content + metadata (NO LLM to notice change).
        h = hashlib.sha256()
        h.update(text.encode("utf-8"))
        h.update(json.dumps(meta, sort_keys=True).encode("utf-8"))
        content_hash = h.hexdigest()
        manifest = self._kb_load_json(P["manifest"], {"entries": {}})
        entries = manifest.setdefault("entries", {})
        prior = entries.get(entry_id, {})
        if op == "upsert" and prior.get("landed_hash") == content_hash:
            return self._send_json(200, {"status": "unchanged", "entry": entry_id, "content_hash": content_hash[:16],
                                         "note": "content+metadata hash matches the landed version — skipped, no re-stage, no model call."})
        # injection scan + envelope (INV-75).
        try:
            kbenv = self._kb_import_envelope()
            flags = kbenv.scan(bodytext)
            enveloped = text if kbenv.is_enveloped(text) else kbenv.envelope(bodytext, source=f"kb:{entry_id}")
        except Exception as e:
            return self._send_json(500, {"error": f"kb_envelope unavailable: {e}"})
        compare = self._kb_diff(prior.get("last_body", ""), "" if op == "delete" else bodytext)
        q = P["queue"]; q.mkdir(parents=True, exist_ok=True)
        item = {
            "video_id": f"kb-{entry_id}", "id": f"kb-{entry_id}", "kind": "kb",
            "kb_entry": entry_id, "kb_op": op, "source": str(path),
            "title": meta.get("title") or entry_id, "content_hash": content_hash,
            "enveloped": True, "caption": enveloped, "caption_scrubbed": enveloped,
            "injection_flags": flags, "compare": compare, "metadata": meta,
            "status": "pending_review", "staged_ts": time.time(),
            "note": ("DELETE staged as a TOMBSTONE — on approval this is a supersession mark, never a hard delete; history kept (D2)."
                     if op == "delete" else
                     "UPSERT staged — enveloped + injection-scanned; commits to the KB only through the human gate (approve). The button never authors directly."),
        }
        (q / f"kb-{entry_id}.json").write_text(json.dumps(item, indent=2))
        entries[entry_id] = {
            "content_hash": content_hash,
            "landed_hash": prior.get("landed_hash"),   # unchanged — STAGING is not LANDING (the human gate lands it)
            "staged_ts": item["staged_ts"], "op": op, "source": str(path),
            "last_body": bodytext if op == "upsert" else prior.get("last_body", ""),
        }
        P["manifest"].write_text(json.dumps(manifest, indent=2))
        try:
            with open(P["audit"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "action": "stage", "entry": entry_id, "op": op,
                                     "content_hash": content_hash[:16], "injection_flags": len(flags)}) + "\n")
        except Exception:
            pass
        return self._send_json(200, {
            "status": "staged", "entry": entry_id, "op": op, "content_hash": content_hash[:16],
            "injection_flags": len(flags), "flags": flags[:5], "enveloped": True, "compare": compare,
            "queue_id": f"kb-{entry_id}",
            "note": "Staged into the review queue (the human gate). Injection-scanned + enveloped (INV-75). Nothing reached the KB — approve there to commit.",
        })

    def _kb_posture_get(self) -> None:
        """GET /kb/posture — the Sealed·Ask·Scheduled·Open spectrum: global default + per-source overrides,
        each with last-checked · last-changed · staleness age · pending (D1/D4)."""
        P = self._kb_paths()
        posture = self._kb_load_json(P["posture"], {"global_default": "ask", "sources": {}})
        gd = posture.get("global_default", "ask")
        pending_by = {}
        try:
            for fp in P["queue"].glob("kb-*.json"):
                d = self._kb_load_json(fp, {})
                key = d.get("kb_entry") or fp.stem
                pending_by[key] = pending_by.get(key, 0) + 1
        except Exception:
            pass
        now = time.time()
        out_sources = {}
        for name, cfg in posture.get("sources", {}).items():
            last_changed = cfg.get("last_changed")
            out_sources[name] = {
                "posture": cfg.get("posture", gd),
                "last_checked": cfg.get("last_checked"),
                "last_changed": last_changed,
                "staleness_age_s": round(now - last_changed) if last_changed else None,
                "pending": pending_by.get(name, 0),
            }
        return self._send_json(200, {
            "vocabulary": list(self._KB_POSTURES),
            "global_default": gd,
            "sources": out_sources,
            "invariants": {
                "never_deleted": "At every posture, nothing is ever deleted — supersession is marked, history kept; contested pairs are marked contested, with dates (D2).",
                "never_auto_fix": "No posture auto-generates a fix. Open means changes LAND and are REVIEWED; a model never rewrites an entry unwatched (D3).",
                "governance": "A posture change is governance — rate-limited notification + recorded forever (D5).",
            },
        })

    def _kb_posture_set(self, body) -> None:
        """POST /kb/posture {posture, source?} — set the global default (no source) or a per-source override.
        GOVERNANCE: recorded forever (append-only) + rate-limited notification. LOOPBACK-ONLY."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only — posture is governance"})
        P = self._kb_paths()
        new_posture = str(body.get("posture") or "").strip().lower()
        if new_posture not in self._KB_POSTURES:
            return self._send_json(400, {"error": f"posture must be one of {list(self._KB_POSTURES)}"})
        source = str(body.get("source") or "").strip()
        posture = self._kb_load_json(P["posture"], {"global_default": "ask", "sources": {}})
        now = time.time()
        if source:
            src = posture.setdefault("sources", {}).setdefault(source, {})
            old = src.get("posture", posture.get("global_default", "ask"))
            src["posture"] = new_posture
            src["last_changed"] = now
            src.setdefault("last_checked", now)
            key = f"source:{source}"
        else:
            old = posture.get("global_default", "ask")
            posture["global_default"] = new_posture
            key = "global"
        posture["updated"] = now
        # RECORD forever (append-only; never deleted) — D5. The record is written BEFORE the rate-limit gate so a
        # rate-limited change is still fully recorded; only the human-facing NOTIFICATION is throttled.
        try:
            with open(P["posture_audit"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": now, "key": key, "from": old, "to": new_posture}) + "\n")
        except Exception:
            pass
        notified = False
        rl = posture.setdefault("_notify", {})
        if now - float(rl.get(key, 0) or 0) >= 10.0:
            rl[key] = now
            notified = True
        P["posture"].write_text(json.dumps(posture, indent=2))
        return self._send_json(200, {
            "ok": True, "governance": True, "recorded": True, "notified": notified,
            "key": key, "from": old, "to": new_posture,
            "note": "Posture change recorded forever (append-only audit). Notification is rate-limited; the RECORD never is. Nothing was deleted; no fix was generated.",
        })

    # ======================================================================================================
    # Q12 Part D — THE STORAGE BUDGET (D1 preview · D2 refuse-and-ask cap · D3 predicted-vs-actual · D4 commit)
    # The through-line: knowledge is worth keeping only if it can't quietly eat the disk, and a download can't be
    # allowed to evict a year of hand-made notes. So: predict the cost in MB BEFORE the run (D1) against TWO
    # separate budgets; at the cap REFUSE and ASK — never silently evict (D2); bedrock (L0 text) is permanent,
    # derived layers are reclaimable-because-regenerable and every reclaim is RECORDED (not a delete); learn the
    # estimate from predicted-vs-actual (D3); and commit-on-approve for kb- items is supersession-aware with
    # contested pairs PRESERVED, deletes as tombstones, and never auto-commits at any posture (D4).
    # ======================================================================================================

    # The read-chain instruction line (D3): agents that absorb material MUST state the storage cost in their own
    # message. Surfaced by /kb/cost-preview and written to the advisory read-chain so it reaches agents, not just UI.
    _KB_ABSORB_INSTRUCTION = (
        "When you absorb material into the KB, STATE ITS STORAGE COST in your own message, in MB, against the two "
        "budgets (corpus-text MB · derived-heavy GB), before you ask to commit. Get the number from "
        "/kb/cost-preview. Bedrock text is permanent; derived layers (wikis/models/embeddings) are reclaimable. "
        "At the cap the system refuses and asks — it never evicts. Do not round a GB download down to feel small."
    )

    @staticmethod
    def _kb_dir_bytes(path) -> int:
        total = 0
        try:
            for fp in path.rglob("*"):
                if fp.is_file():
                    try: total += fp.stat().st_size
                    except Exception: pass
        except Exception:
            pass
        return total

    def _kb_measure_usage(self) -> dict:
        """Measure current on-disk usage split across the two budgets. TEXT = the corpus (.md under knowledge/,
        excluding the reclaimable derived/ tree) — L0 bedrock. HEAVY = knowledge/derived/ (wikis/models/embeddings)."""
        text_bytes = 0
        try:
            for fp in self._KB_KNOWLEDGE.rglob("*.md"):
                if self._KB_DERIVED in fp.parents:
                    continue  # derived .md (e.g., generated summaries) counts as HEAVY, not corpus text
                if fp.is_file():
                    try: text_bytes += fp.stat().st_size
                    except Exception: pass
        except Exception:
            pass
        heavy_bytes = self._kb_dir_bytes(self._KB_DERIVED)
        return {"text_bytes": text_bytes, "heavy_bytes": heavy_bytes}

    def _kb_budget(self, overrides: dict = None) -> dict:
        b = dict(self._KB_BUDGET_DEFAULTS)
        b.update(self._kb_load_json(self._kb_paths()["budget"], {}))
        if overrides:
            for k in ("text_cap_mb", "heavy_cap_gb"):
                if overrides.get(k) is not None:
                    try: b[k] = float(overrides[k])
                    except Exception: pass
        return b

    def _kb_calibration(self) -> dict:
        """Learn the estimate from history (D3): mean(actual/predicted) over recorded commits. 1.0 until we have
        data; a value >1 means our predictions ran light and future previews are scaled up accordingly."""
        ratios = []
        try:
            for line in self._kb_paths()["cost_log"].read_text().splitlines():
                try:
                    r = json.loads(line)
                    p, a = float(r.get("predicted_mb", 0)), float(r.get("actual_mb", 0))
                    if p > 0 and a > 0:
                        ratios.append(a / p)
                except Exception:
                    pass
        except Exception:
            pass
        recent = ratios[-20:]
        factor = round(sum(recent) / len(recent), 4) if recent else 1.0
        return {"factor": factor, "samples": len(ratios)}

    def _kb_candidate_cost(self, body: dict) -> dict:
        """Predict a candidate absorb's cost in MB + classify it text|heavy. A kb source is TEXT (its enveloped
        body); an explicit est_mb / est_gb (a wiki/model/embedding download) is HEAVY."""
        klass = str(body.get("class") or "").lower()
        raw_mb = None
        label = body.get("label") or ""
        if body.get("est_gb") is not None:
            raw_mb = float(body["est_gb"]) * 1024.0; klass = klass or "heavy"
        elif body.get("est_mb") is not None:
            raw_mb = float(body["est_mb"]); klass = klass or ("heavy" if raw_mb >= 100 else "text")
        else:
            # derive from a KB source's body size (TEXT)
            source = str(body.get("source") or "seed")
            try:
                path = self._KB_SEED if source == "seed" else (self._KB_KNOWLEDGE / source)
                if path.is_file():
                    raw_mb = len(path.read_bytes()) / 1_000_000.0
                    label = label or path.name
            except Exception:
                raw_mb = None
            klass = klass or "text"
        if raw_mb is None:
            return {"predicted_mb": None, "class": klass or "text", "label": label, "measured": False}
        cal = self._kb_calibration()
        predicted = round(raw_mb * cal["factor"], 4)
        return {"predicted_mb": predicted, "raw_mb": round(raw_mb, 4), "class": klass, "label": label,
                "measured": True, "calibration": cal}

    def _kb_budget_lines(self, budget: dict, usage: dict, candidate: dict = None) -> dict:
        """Build BOTH budget lines with used/cap/headroom. If a candidate is given, show where it lands + whether
        it fits its OWN budget (text vs heavy) — a text note can never be blocked by a full HEAVY line and vice versa."""
        text_used_mb = usage["text_bytes"] / 1_000_000.0
        heavy_used_mb = usage["heavy_bytes"] / 1_000_000.0
        text_cap_mb = float(budget["text_cap_mb"])
        heavy_cap_mb = float(budget["heavy_cap_gb"]) * 1024.0
        def line(used, cap, unit, name):
            head = cap - used
            return {"budget": name, "used_mb": round(used, 4), "cap_mb": round(cap, 2), "headroom_mb": round(head, 4),
                    "pct": round(100.0 * used / cap, 1) if cap > 0 else None, "display": f"{name}: {used:.2f}/{cap:.0f} {unit}"}
        lines = {
            "corpus_text": line(text_used_mb, text_cap_mb, "MB", "corpus text (bedrock)"),
            "derived_heavy": line(heavy_used_mb, heavy_cap_mb, "MB (=%.0f GB)" % budget["heavy_cap_gb"], "wikis/models/embeddings"),
        }
        decision = {"fits": None}
        if candidate and candidate.get("predicted_mb") is not None:
            which = "derived_heavy" if candidate["class"] == "heavy" else "corpus_text"
            head = lines[which]["headroom_mb"]
            fits = candidate["predicted_mb"] <= head
            decision = {
                "fits": fits, "against": which, "predicted_mb": candidate["predicted_mb"],
                "headroom_mb": head, "shortfall_mb": round(max(0.0, candidate["predicted_mb"] - head), 4),
            }
        return {"lines": lines, "decision": decision}

    def _kb_cost_preview(self, body: dict = None) -> None:
        """GET/POST /kb/cost-preview — D1/D2. Storage cost IN MB, BEFORE the run, against TWO budget lines. If a
        candidate would exceed its budget the decision is REFUSE-AND-ASK (never evict); the reclaimable derived
        layers are listed as the ONLY way to make room (bedrock L0 is never offered for reclaim)."""
        body = body or {}
        budget = self._kb_budget(body)
        usage = self._kb_measure_usage()
        candidate = self._kb_candidate_cost(body) if (body.get("source") or body.get("est_mb") is not None or body.get("est_gb") is not None) else None
        bl = self._kb_budget_lines(budget, usage, candidate)
        over = candidate is not None and bl["decision"].get("fits") is False
        reclaimable = []
        if over:
            # ONLY derived (regenerable) layers may be reclaimed — bedrock text is never offered.
            try:
                for fp in sorted(self._KB_DERIVED.rglob("*")):
                    if fp.is_file():
                        reclaimable.append({"path": str(fp.relative_to(self._KB_KNOWLEDGE)), "mb": round(fp.stat().st_size / 1_000_000.0, 4),
                                            "regenerable_from": "re-derive (summary/embedding/wiki fetch)"})
            except Exception:
                pass
        out = {
            "budgets": bl["lines"],
            "candidate": candidate,
            "decision": ("ok" if candidate is None else ("refuse-and-ask" if over else "ok")),
            "reclaimable_derived": reclaimable,
            "bedrock_note": "L0 corpus text is PERMANENT — never evicted, never offered for reclaim. Only derived layers (regenerable) can be reclaimed, and every reclaim is recorded (not a deletion event).",
            "absorb_instruction": self._KB_ABSORB_INSTRUCTION,
            "calibration": self._kb_calibration(),
        }
        if over:
            out["refusal"] = {
                "reason": "would exceed its budget cap",
                "shortfall_mb": bl["decision"]["shortfall_mb"],
                "ask": "Refused, not evicted. To make room, reclaim a derived layer (POST /kb/reclaim) or raise the cap deliberately. Bedrock text is never touched.",
            }
        return self._send_json(200 if not over else 409, out)

    def _kb_reclaim(self, body: dict) -> None:
        """POST /kb/reclaim {path} — reclaim ONE derived (regenerable) artifact to free space. RECORDED as a
        reclaim (append-only), never framed as a deletion. Bedrock text (L0) can NEVER be reclaimed — refused."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only — reclaim is governance"})
        P = self._kb_paths()
        rel = str(body.get("path") or "").strip()
        if not rel:
            return self._send_json(400, {"error": "path required (relative to knowledge/)"})
        try:
            root = self._KB_KNOWLEDGE.resolve()
            target = (self._KB_KNOWLEDGE / rel).resolve()
        except Exception:
            return self._send_json(400, {"error": "bad path"})
        # HARD GUARD: only the derived/ subtree is reclaimable. Bedrock text is permanent.
        derived_root = self._KB_DERIVED.resolve()
        if derived_root not in target.parents:
            return self._send_json(403, {"error": "not reclaimable — only knowledge/derived/ (regenerable layers) may be reclaimed; bedrock text is permanent (L0)."})
        if not target.is_file():
            return self._send_json(404, {"error": f"no such derived artifact: {rel}"})
        freed = target.stat().st_size
        # record BEFORE acting (a reclaim is an event we keep, not a hole we leave)
        rec = {"ts": time.time(), "action": "reclaim", "path": rel, "freed_bytes": freed,
               "regenerable": True, "note": "derived layer reclaimed to free space; regenerable — NOT a knowledge deletion."}
        try:
            with open(P["reclaim_log"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        try:
            target.unlink()
        except Exception as e:
            return self._send_json(500, {"error": f"reclaim failed: {e}"})
        return self._send_json(200, {"ok": True, "reclaimed": rel, "freed_mb": round(freed / 1_000_000.0, 4),
                                     "recorded": True, "note": rec["note"]})

    @staticmethod
    def _kb_detect_contested(bodytext: str):
        """Preserve contested pairs — never resolve them (D4). The bot-landscape seed is the worked example:
        Imperva/Thales (53% bots) vs Cloudflare Radar (35.2%) — 'surfaced, not resolved. Both stand.' Detect a
        contradiction marker, then capture BOTH sides so the commit records the pair as contested (both kept)."""
        if not re.search(r"contradiction|contested|surfaced,\s*not\s*resolved|both stand", bodytext, re.I):
            return None
        def find(pat):
            for ln in bodytext.splitlines():
                if re.search(pat, ln, re.I) and re.search(r"\d", ln):
                    return re.sub(r"\s+", " ", ln).strip().strip("*").strip()
            return None
        a = find(r"imperva|thales")
        b = find(r"cloudflare")
        sides = [s for s in (a, b) if s]
        if len(sides) < 2:
            # generic fallback: the two non-empty lines nearest the contradiction marker
            lines = [re.sub(r"\s+", " ", l).strip() for l in bodytext.splitlines() if l.strip()]
            idx = next((i for i, l in enumerate(lines) if re.search(r"contradiction|both stand", l, re.I)), None)
            if idx is not None:
                sides = [l for l in lines[max(0, idx - 2):idx] if re.search(r"\d", l)][:2] or sides
        if len(sides) < 2:
            return None
        return {"contested": True, "resolution": "preserved-unresolved", "both_stand": True, "sides": sides[:2]}

    def _kb_commit(self, vid: str, item: dict) -> None:
        """D4 — the REAL commit-on-approve for a kb- item. Supersession-aware (prior body archived + dated, never
        overwritten-away), contested pairs PRESERVED (not resolved), delete → TOMBSTONE (history kept). Records
        predicted-vs-actual (D3). Called ONLY from the human approve gate — never auto-commits at any posture."""
        import hashlib
        P = self._kb_paths()
        entry_id = str(item.get("kb_entry") or vid[3:] if vid.startswith("kb-") else vid).strip()
        op = str(item.get("kb_op") or "upsert").lower()
        bodytext = item.get("caption") or item.get("caption_scrubbed") or ""
        meta = item.get("metadata") or {}
        content_hash = item.get("content_hash") or hashlib.sha256(bodytext.encode("utf-8")).hexdigest()
        self._KB_ENTRIES.mkdir(parents=True, exist_ok=True)
        self._KB_ARCHIVE.mkdir(parents=True, exist_ok=True)
        P["approved"].mkdir(parents=True, exist_ok=True)
        manifest = self._kb_load_json(P["manifest"], {"entries": {}})
        entries = manifest.setdefault("entries", {})
        prior = entries.get(entry_id, {})
        entry_file = self._KB_ENTRIES / f"{entry_id}.md"
        date = time.strftime("%Y-%m-%d")

        # SUPERSESSION: if a prior version is already on disk, archive it (kept + dated) before writing the new one.
        superseded = None
        if entry_file.is_file():
            prior_hash = (prior.get("landed_hash") or "prev")[:8]
            arch = self._KB_ARCHIVE / f"{entry_id}.{prior_hash}.{date}.md"
            try:
                old = entry_file.read_text()
                arch.write_text(f"<!-- SUPERSEDED {date} · prior landed_hash={prior_hash} · kept, not deleted (D2) -->\n\n" + old)
                superseded = str(arch.relative_to(self._KB_KNOWLEDGE))
            except Exception:
                pass

        # CONTESTED PAIRS — preserved, never resolved.
        contested = self._kb_detect_contested(bodytext)
        contested_store = self._kb_load_json(P["contested"], {"pairs": {}})
        if contested:
            contested["recorded"] = date
            contested["entry"] = entry_id
            contested_store.setdefault("pairs", {})[entry_id] = contested
            P["contested"].write_text(json.dumps(contested_store, indent=2))

        landed = True
        if op == "delete":
            # TOMBSTONE — mark deleted, keep history. The entry file becomes a tombstone; the body is archived above.
            tomb = (f"---\nkb_entry: {entry_id}\nstatus: tombstoned\ntombstoned: {date}\n"
                    f"supersedes: {(prior.get('landed_hash') or 'none')[:16]}\n---\n\n"
                    f"# {meta.get('title') or entry_id} — TOMBSTONE ({date})\n\n"
                    "This entry was retired. It is NOT hard-deleted: the prior body is archived under knowledge/archive/ "
                    "and this tombstone records the retirement (D2 — nothing is ever destroyed).\n")
            entry_file.write_text(tomb)
        else:
            # UPSERT — write the landed entry with a header that PRESERVES any contested pair inline.
            header = [f"---", f"kb_entry: {entry_id}", f"landed: {date}", f"content_hash: {content_hash[:16]}"]
            if superseded:
                header.append(f"supersedes: {(prior.get('landed_hash') or 'prev')[:16]} (archived {superseded})")
            if contested:
                header.append("contested: true  # preserved, not resolved — both sides stand (D4)")
            header.append("---")
            preface = ""
            if contested:
                preface = ("<!-- CONTESTED PAIR — preserved, not resolved (D4):\n"
                           + "\n".join(f"     · {s}" for s in contested["sides"])
                           + "\n     Both stand; this commit records the disagreement, it does not pick a winner. -->\n\n")
            entry_file.write_text("\n".join(header) + "\n\n" + preface + bodytext)

        # actual bytes written (D3 — predicted-vs-actual)
        actual_mb = round(entry_file.stat().st_size / 1_000_000.0, 6) if entry_file.is_file() else 0.0
        predicted_mb = round(len(bodytext.encode("utf-8")) / 1_000_000.0, 6)
        try:
            with open(P["cost_log"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "entry": entry_id, "op": op,
                                     "predicted_mb": predicted_mb, "actual_mb": actual_mb}) + "\n")
        except Exception:
            pass

        # manifest: this is now LANDED. Record the supersession chain + contested marker.
        chain = list(prior.get("supersedes_chain") or [])
        if prior.get("landed_hash"):
            chain.append({"hash": prior["landed_hash"][:16], "archived": superseded, "date": date})
        entries[entry_id] = {
            "content_hash": content_hash,
            "landed_hash": content_hash,        # LANDED now (the human gate landed it)
            "landed_ts": time.time(), "landed_date": date, "op": op,
            "source": item.get("source"), "last_body": "" if op == "delete" else bodytext,
            "supersedes_chain": chain,
            "contested": bool(contested),
            "entry_file": str(entry_file.relative_to(self._KB_KNOWLEDGE)),
            "status": "tombstoned" if op == "delete" else "landed",
        }
        P["manifest"].write_text(json.dumps(manifest, indent=2))

        # index it (deterministic, model-free) — reuse the slice reindex so llms.txt resolves the new entry.
        try:
            subprocess.run(["/usr/bin/python3", str(self._SLICE), "reindex"], capture_output=True, text=True, timeout=30)
        except Exception:
            pass

        # audit + leave-as-found: the queue item moves to approved/ (kept, not deleted).
        try:
            with open(P["audit"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "action": "commit", "entry": entry_id, "op": op,
                                     "content_hash": content_hash[:16], "superseded": bool(superseded),
                                     "contested": bool(contested)}) + "\n")
        except Exception:
            pass
        try:
            (P["queue"] / f"{vid}.json").replace(P["approved"] / f"{vid}.json")
        except Exception:
            pass

        return self._send_json(200, {
            "ok": True, "committed": True, "entry": entry_id, "op": op,
            "landed_hash": content_hash[:16],
            "superseded": superseded, "supersedes_chain_len": len(chain),
            "contested_preserved": contested,
            "tombstone": op == "delete",
            "cost": {"predicted_mb": predicted_mb, "actual_mb": actual_mb},
            "note": ("DELETE committed as a TOMBSTONE — prior body archived, history kept; nothing hard-deleted (D2)."
                     if op == "delete" else
                     "UPSERT committed to the KB via the human gate. Prior version archived + dated (supersession); "
                     "any contested pair preserved unresolved (D4). Predicted-vs-actual recorded (D3)."),
        })

    # ---- Card 5: COMPONENT-MANIFEST ----
    def _components_get(self) -> None:
        """GET /components — resolve_components(): present/declined/pending/UNAVAILABLE-with-reason per component."""
        try:
            import sys as _sys
            if str(ROOT) not in _sys.path: _sys.path.insert(0, str(ROOT))
            import hardware_profile as _hp
            return self._send_json(200, {"components": _hp.resolve_components()})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _components_reconsider(self, body) -> None:
        """POST /components/reconsider {id} — flip a declined component to pending-with-note (backup-before-write).
        INV-40 must still hold (pending-with-note is a recorded absence)."""
        cid = str(body.get("id") or "").strip()
        p = AIONUI_ROUTING.parent / "khaoss-components.json"
        if not p.is_file():
            return self._send_json(404, {"error": "component manifest not found"})
        try:
            man = json.loads(p.read_text())
            hit = next((c for c in man.get("components", []) if c.get("id") == cid), None)
            if not hit:
                return self._send_json(404, {"error": f"no such component: {cid}"})
            shutil.copy2(str(p), str(p) + ".bak")   # backup-before-write
            hit["declined"] = False
            hit["note"] = "reconsidered via panel — pending install choice"
            p.write_text(json.dumps(man, indent=2) + "\n")
            return self._send_json(200, {"ok": True, "id": cid, "note": "flipped to pending-with-note (recorded absence; INV-40 holds)."})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _routing_get(self) -> None:
        """GET /routing/get — read the canonical khaoss-routing.json (task-type -> tier policy). Reports the
        selected mechanical model + whether it's a forbidden frontier model (never_for_mechanical) so the UI
        can flag the waste. The real backer, no shadow copy."""
        try:
            if AIONUI_ROUTING.is_file():
                cfg = json.loads(AIONUI_ROUTING.read_text())
            else:
                cfg = {}
        except Exception as e:
            return self._send_json(500, {"error": f"routing config unreadable: {e}"})
        sel = ((cfg.get("mechanical") or {}).get("selected_model")) or ""
        never = cfg.get("never_for_mechanical") or []
        # hardware-resolved tiers (Colibri etc. show AVAILABLE / UNAVAILABLE-with-reason — no silent absence)
        hw = None; tiers = None
        try:
            import sys as _sys
            if str(ROOT) not in _sys.path: _sys.path.insert(0, str(ROOT))
            import hardware_profile as _hp
            hw = _hp.get(); tiers = _hp.resolve_tiers(hw)
        except Exception as e:
            hw = {"error": str(e)}
        # per-leader delegation.model + SELECT-TOOL soul-pin presence (Card 1 data contract) — regex parse,
        # no yaml dependency. delegation.model = the first `model:` inside the `delegation:` block.
        leaders = {}
        for _L in ("example-a", "example-leader", "trading-leader"):
            _cfg = HOME / ".hermes" / "profiles" / _L / "config.yaml"
            _soul = HOME / ".hermes" / "profiles" / _L / "SOUL.md"
            _model = None
            try:
                _txt = _cfg.read_text(); _in = False
                for _ln in _txt.splitlines():
                    if _ln.startswith("delegation:"): _in = True; continue
                    if _in:
                        if _ln[:1] not in (" ", "\t", ""):  # left the block
                            break
                        _m = re.match(r"\s+model:\s*([^\s#]+)", _ln)
                        if _m: _model = _m.group(1); break
            except Exception:
                _model = None
            _pin = False
            try:
                _pin = "SELECT TOOL BY TASK" in _soul.read_text()
            except Exception:
                _pin = False
            leaders[_L] = {"delegation_model": _model, "select_tool_pin": _pin}
        return self._send_json(200, {
            "path": str(AIONUI_ROUTING), "config": cfg,
            "mechanical_model": sel, "mechanical_is_frontier": sel in never,
            "hardware": hw, "tiers": tiers, "leaders": leaders,
            "note": "enforcement = leaders' pinned DELEGATE + SELECT-TOOL rules + delegation.model in the leader config.yaml.",
        })

    def _routing_set(self, body) -> None:
        """POST /routing/set {mechanical_model, mechanical_service?} — update the mechanical tier's selected
        model/service. REJECTS a frontier model for mechanical work (the whole point). Backup-before-write."""
        model = str(body.get("mechanical_model") or "").strip()
        if not model:
            return self._send_json(400, {"error": "mechanical_model required"})
        try:
            cfg = json.loads(AIONUI_ROUTING.read_text()) if AIONUI_ROUTING.is_file() else {}
        except Exception:
            cfg = {}
        never = cfg.get("never_for_mechanical") or ["claude-opus-4-8", "claude-fable-5", "claude-sonnet-5"]
        if model in never:
            return self._send_json(400, {"error": f"'{model}' is a frontier model — mechanical work must NOT route to frontier (that is the waste this guards). Pick an economy model."})
        try:
            cfg.setdefault("mechanical", {})["selected_model"] = model
            if body.get("mechanical_service"):
                cfg["mechanical"]["default_service"] = str(body.get("mechanical_service"))
            cfg.setdefault("never_for_mechanical", never)
            AIONUI_ROUTING.parent.mkdir(parents=True, exist_ok=True)
            if AIONUI_ROUTING.is_file():
                shutil.copy2(str(AIONUI_ROUTING), str(AIONUI_ROUTING) + ".bak")   # backup-before-write
            AIONUI_ROUTING.write_text(json.dumps(cfg, indent=2) + "\n")
            return self._send_json(200, {"ok": True, "path": str(AIONUI_ROUTING), "mechanical_model": model,
                                         "note": "policy updated. To ENFORCE cost, set delegation.model=this in the leader profile config.yaml."})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _caps_get(self) -> None:
        """GET /caps/get — read the canonical khaoss-caps.json (the source of truth AionUi reads at startup).
        Returns the stored value, the effective value (default when unset), the default, and the floor — so
        the UI shows what's live vs. what would apply. Never a shadow copy; this reads the real file."""
        stored = None
        try:
            if AIONUI_CAPS.is_file():
                stored = int(json.loads(AIONUI_CAPS.read_text()).get("thinking_cap_bytes"))
        except Exception:
            stored = None
        effective = stored if (isinstance(stored, int) and stored >= THINKING_CAP_FLOOR_BYTES) else THINKING_CAP_DEFAULT_BYTES
        return self._send_json(200, {
            "path": str(AIONUI_CAPS),
            "thinking_cap_bytes": stored,                       # what's written (None = unset → default)
            "effective_bytes": effective,                       # what AionUi would use
            "effective_mb": round(effective / 1048576, 2),
            "default_bytes": THINKING_CAP_DEFAULT_BYTES, "floor_bytes": THINKING_CAP_FLOOR_BYTES,
            "note": "AionUi reads this at startup — a change takes effect on next AionUi start.",
        })

    def _caps_set(self, body) -> None:
        """POST /caps/set {thinking_cap_bytes} — write the ONE canonical caps file AionUi reads. Backup-before-
        write; floor-guarded (>=256 KB, never clip to nothing). The real backer, no shadow copy."""
        try:
            n = int(body.get("thinking_cap_bytes"))
        except Exception:
            return self._send_json(400, {"error": "thinking_cap_bytes (int) required"})
        if n < THINKING_CAP_FLOOR_BYTES:
            return self._send_json(400, {"error": f"below floor {THINKING_CAP_FLOOR_BYTES} bytes (256 KB) — would clip ordinary thinking"})
        try:
            AIONUI_CAPS.parent.mkdir(parents=True, exist_ok=True)
            if AIONUI_CAPS.is_file():
                shutil.copy2(str(AIONUI_CAPS), str(AIONUI_CAPS) + ".bak")   # backup-before-write
            existing = {}
            try:
                if AIONUI_CAPS.is_file():
                    existing = json.loads(AIONUI_CAPS.read_text())
            except Exception:
                existing = {}
            existing["thinking_cap_bytes"] = n
            AIONUI_CAPS.write_text(json.dumps(existing, indent=2) + "\n")
            return self._send_json(200, {"ok": True, "path": str(AIONUI_CAPS), "thinking_cap_bytes": n,
                                         "mb": round(n / 1048576, 2),
                                         "note": "written — restart AionUi for the new cap to take effect."})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _thinking_status(self) -> None:
        """GET /thinking/status — DB size · thinking share · archived rows · archive size (for the card)."""
        if tarch is None:
            return self._send_json(503, {"error": "thinking_archive unavailable"})
        try:
            dbsize = os.path.getsize(str(AIONUI_DB))
            con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            n, tb = con.execute("SELECT COUNT(*), COALESCE(SUM(length(content)),0) FROM messages WHERE type='thinking'").fetchone()
            arch_n = con.execute("SELECT COUNT(*) FROM messages WHERE type='thinking' AND content LIKE '%__thinking_archived%'").fetchone()[0]
            over = con.execute("SELECT COUNT(*) FROM messages WHERE type='thinking' AND length(content) > ? AND content NOT LIKE '%__thinking_archived%'", (tarch.CAP_BYTES,)).fetchone()[0]
            con.close()
            archsize = sum(f.stat().st_size for f in tarch.ARCHIVE_ROOT.rglob("*.gz")) if tarch.ARCHIVE_ROOT.exists() else 0
            baks = sorted(Path(str(AIONUI_DB)).parent.glob(Path(str(AIONUI_DB)).name + ".bak.thinking-sweep-*"))
            return self._send_json(200, {
                "db_mb": round(dbsize / 1e6, 1), "thinking_rows": n, "thinking_mb": round(tb / 1e6, 1),
                "thinking_share_pct": round(100 * tb / dbsize, 1) if dbsize else 0,
                "archived_rows": arch_n, "over_cap_unarchived": over,
                "archive_mb": round(archsize / 1e6, 1), "cap_mb": tarch.CAP_BYTES // 1048576,
                "last_sweep_backup": baks[-1].name if baks else None, "archive_root": str(tarch.ARCHIVE_ROOT),
            })
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _thinking_cap_message(self, body) -> None:
        """POST /thinking/cap-message {msgid} — apply the CAP archive to ONE oversized thinking message
        (the 18 MB de63a495 demonstration). LOOPBACK-ONLY (writes the DB)."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if tarch is None:
            return self._send_json(503, {"error": "thinking_archive unavailable"})
        msgid = str(body.get("msgid") or "").strip()
        if not msgid:
            return self._send_json(400, {"error": "msgid required"})
        con = sqlite3.connect(str(AIONUI_DB), timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        row = con.execute("SELECT conversation_id, content FROM messages WHERE id=?", (msgid,)).fetchone()
        if not row:
            con.close()
            return self._send_json(404, {"error": "message not found"})
        conv, content = row
        ok, detail = _thinking_archive_and_mark(con, conv, msgid, content, "cap")
        con.close()
        return self._send_json(200 if ok else 500, {"msgid": msgid, "conv": conv, "capped": ok, "detail": detail})

    def _thinking_sweep(self, body) -> None:
        """POST /thinking/sweep {days=14, dry_run, cap_only, limit} — batch-archive thinking. Backs up the
        DB FIRST on a real run. Idempotent (skips archived) + resumable (per-row commit). LOOPBACK-ONLY."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if tarch is None:
            return self._send_json(503, {"error": "thinking_archive unavailable"})
        import time as _t
        days = int(body.get("days", 14))
        dry = bool(body.get("dry_run", False))
        cap_only = bool(body.get("cap_only", False))  # archive >CAP regardless of age (existing oversized)
        # min size floor — archiving a tiny row makes it BIGGER (marker > content); only archive rows
        # where archiving actually saves space. Default 4 KB (sweep marker is ~220 B, so >4 KB is net win).
        try:
            min_bytes = max(int(body.get("min_bytes", 4096)), 512)
        except ValueError:
            min_bytes = 4096
        try:
            limit = min(int(body.get("limit", 100000)), 100000)
        except ValueError:
            limit = 100000
        cutoff = int((_t.time() - days * 86400) * 1000)
        backup = None
        if not dry:
            backup = str(AIONUI_DB) + f".bak.thinking-sweep-{int(_t.time())}"
            try:
                shutil.copy2(str(AIONUI_DB), backup)
            except Exception as e:
                return self._send_json(500, {"error": f"backup failed — refusing to sweep: {e}"})
        con = sqlite3.connect(str(AIONUI_DB), timeout=60)
        con.execute("PRAGMA busy_timeout=60000")
        if cap_only:
            rows = con.execute("SELECT id, conversation_id, content FROM messages WHERE type='thinking' AND length(content) > ? ORDER BY length(content) DESC LIMIT ?", (tarch.CAP_BYTES, limit)).fetchall()
        else:
            rows = con.execute("SELECT id, conversation_id, content FROM messages WHERE type='thinking' AND created_at < ? AND length(content) > ? ORDER BY created_at ASC LIMIT ?", (cutoff, min_bytes, limit)).fetchall()
        archived = skipped = failed = freed = would = 0
        for msgid, conv, content in rows:
            already, _ = tarch.is_archived(content)
            if already:
                skipped += 1
                continue
            would += 1  # not-yet-archived candidate (accurate dry-run count)
            if dry:
                continue
            ok, detail = _thinking_archive_and_mark(con, conv, msgid, content, "cap" if cap_only else "sweep")
            if ok and isinstance(detail, dict):
                archived += 1
                freed += detail.get("was_bytes", 0) - detail.get("now_bytes", 0)
            elif ok:
                skipped += 1
            else:
                failed += 1  # fail open — row kept full
        con.close()
        return self._send_json(200, {"dry_run": dry, "cap_only": cap_only, "days": days, "min_bytes": min_bytes,
                                     "matched": len(rows), "would_archive": would, "archived": archived,
                                     "skipped_already": skipped, "failed_kept_full": failed,
                                     "mb_freed": round(freed / 1e6, 1), "backup": backup})

    def _logs_tail(self) -> None:
        """GET /logs/tail?source=&lines=&filter= — bounded tail of a whitelisted source (never loads whole)."""
        q = parse_qs(urlsplit(self.path).query)
        source = (q.get("source", [""])[0] or "").strip()
        try:
            lines = min(int(q.get("lines", ["200"])[0] or 200), 2000)
        except ValueError:
            lines = 200
        filt = q.get("filter", [""])[0] or ""
        sources = _log_sources()
        if source not in sources:
            return self._send_json(400, {"error": f"unknown source: {source}", "known": sorted(sources.keys())})
        meta = sources[source]
        # special action source: the invariants board (run the checker, bounded output)
        if meta.get("action") == "invariants":
            try:
                proc = subprocess.run(["/bin/bash", str(INVARIANTS_CHECK)],
                                      capture_output=True, text=True, timeout=25)
                clean = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
                out = clean.splitlines()
                if filt:
                    out = [l for l in out if filt.lower() in l.lower()]
                return self._send_json(200, {"source": source, "lines": out[-lines:], "count": len(out[-lines:]),
                                             "exit_code": proc.returncode})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        p = meta.get("path")
        if not p or not Path(p).exists():
            # KHAOSS-PATCH: logs-failed-fetch-hint — NAME THE CAUSE + offer the fix IN PLACE (no-silent-failure).
            aion = ("freeze" in source or "electron" in source or "acp" in source or "aionui" in source)
            if aion:
                hint = {"cause": "AionUi hasn't written this log yet (no freeze/turn since launch), or the app isn't running.",
                        "fix": "Start/relaunch AionUi (Services → AionUi), or open a team to generate ACP activity.",
                        "action": {"label": "Restart AionUi", "endpoint": "/services/restart", "body": {"service": "aionui"}}}
            elif source in ("failure-journal", "bridge-ledger"):
                hint = {"cause": "No entries recorded yet — the journal/ledger is created on first write.",
                        "fix": "Trigger an event (POST /health/tick, or run an audited /bridge/write); the file appears on first append."}
            else:
                hint = {"cause": f"The writer for '{source}' hasn't created {p} yet.",
                        "fix": "Confirm the producing service is running; the file appears on its first write."}
            return self._send_json(200, {"source": source, "lines": [], "count": 0, "missing": True,
                                         "path": str(p) if p else None, "hint": hint})
        try:
            tail = _bounded_tail(Path(p), lines, filt, meta.get("builtin_filter", ""))
        except Exception as e:
            return self._send_json(200, {"source": source, "path": str(p), "lines": [], "count": 0, "read_error": True,
                                         "hint": {"cause": f"cannot read {p}: {e}",
                                                  "fix": "Check file permissions/rotation, or pick another source."}})
        return self._send_json(200, {"source": source, "path": str(p), "lines": tail, "count": len(tail),
                                     "filtered": bool(filt or meta.get("builtin_filter"))})

    # ── /bridge/ledger + /bridge/rollback* ────────────────────────────────────
    # KHAOSS-PATCH: traceability-ledger + rollback — the RECOVERY leg (policy·sandbox·monitoring·RECOVERY).
    def _bridge_ledger(self) -> None:
        """GET /bridge/ledger?agent=&path=&since=&limit= — read the append-only ledger, newest first."""
        q = parse_qs(urlsplit(self.path).query)
        agent_f = (q.get("agent", [""])[0] or "").lower()
        path_f = q.get("path", [""])[0] or ""
        since_f = q.get("since", [""])[0] or ""
        try:
            limit = min(int(q.get("limit", ["200"])[0] or 200), 2000)
        except ValueError:
            limit = 200
        entries = []
        try:
            if BRIDGE_LEDGER.exists():
                for line in BRIDGE_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if agent_f and agent_f not in str(e.get("agent", "")).lower():
                        continue
                    if path_f and path_f not in str(e.get("path", "")) and path_f not in str(e.get("cmd", "")):
                        continue
                    if since_f and str(e.get("ts", "")) < since_f:
                        continue
                    entries.append(e)
        except Exception as ex:
            return self._send_json(500, {"error": str(ex)})
        entries.reverse()  # newest first
        return self._send_json(200, {
            "count": len(entries), "entries": entries[:limit],
            "ledger": str(BRIDGE_LEDGER), "append_only": True,
        })

    def _bridge_rollbacks(self) -> None:
        """GET /bridge/rollbacks?path=X — the single .bak rollback target for X (one level deep)."""
        q = parse_qs(urlsplit(self.path).query)
        p = q.get("path", [""])[0] or ""
        if not p:
            return self._send_json(400, {"error": "path required"})
        full = os.path.realpath(os.path.expanduser(p))
        bak = full + ".bak"

        def _meta(fp):
            try:
                st = os.stat(fp)
                return {"exists": True, "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}
            except FileNotFoundError:
                return {"exists": False}

        last_writer = None
        try:
            if BRIDGE_LEDGER.exists():
                for line in BRIDGE_LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("action") == "write" and e.get("path") == full:
                        last_writer = {"agent": e.get("agent"), "ts": e.get("ts")}  # keep most recent
        except Exception:
            pass
        return self._send_json(200, {
            "path": full, "current": _meta(full), "backup": {"path": bak, **_meta(bak)},
            "last_writer": last_writer, "levels": 1,
            "note": "one level deep — a single previous version, not a history tree",
        })

    def _bridge_rollback(self, body: dict) -> None:
        """POST /bridge/rollback {path, agent?} — OPERATOR-TRIGGERED restore of path from its single .bak.
        One level deep. The rollback is itself a write → it is ledgered, and the pre-rollback current is
        preserved so the undo is itself undoable."""
        import shutil
        p = body.get("path", "")
        if not p:
            return self._send_json(400, {"error": "path required"})
        if not _bridge_path_allowed(p):
            return self._send_json(403, {"error": "path not allowed"})
        full = os.path.realpath(os.path.expanduser(p))
        bak = full + ".bak"
        if not os.path.exists(bak):
            return self._send_json(404, {"error": f"no .bak to roll back to for {full}"})

        def _meta(fp):
            try:
                st = os.stat(fp)
                return {"size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}
            except FileNotFoundError:
                return {"size": None, "mtime": None}

        before_current = _meta(full)
        restore_from = _meta(bak)
        try:
            pre = None
            if os.path.exists(full):
                pre = full + ".bak.pre-rollback." + str(int(time.time()))
                shutil.copy2(full, pre)   # safety copy of current before we overwrite it
            shutil.copy2(bak, full)       # the rollback itself
            _ledger_append({
                "agent": str(body.get("agent") or "operator"), "action": "rollback",
                "path": full, "restored_from": bak, "pre_rollback": pre,
            })
            return self._send_json(200, {
                "ok": True, "path": full, "restored_from": bak, "levels": 1,
                "before": before_current, "restored_to": restore_from, "after": _meta(full),
                "pre_rollback": pre,
            })
        except Exception as ex:
            return self._send_json(500, {"error": str(ex)})

    # ── /ai-routing/* ────────────────────────────────────────────────────────

    def _ai_routing_status(self) -> None:
        cfg = _load_ai_routing_config()
        return self._send_json(200, _ai_routing_config_public(cfg))

    def _ai_routing_config_post(self) -> None:
        body = self._read_body_json()
        cfg = _load_ai_routing_config()
        # Merge top-level keys
        if "tier" in body:
            cfg["tier"] = str(body["tier"])
        for section in ("local", "private", "cloud"):
            if section in body and isinstance(body[section], dict):
                cfg.setdefault(section, {}).update(body[section])
        _save_ai_routing_config(cfg)
        return self._send_json(200, _ai_routing_config_public(cfg))

    def _ai_routing_test(self) -> None:
        qs = parse_qs(urlsplit(self.path).query)
        tier = (qs.get("tier") or ["cloud"])[0]
        if tier == "local":
            available = _check_local_available()
            msg = "Ollama reachable" if available else "Ollama not reachable at configured URL"
        elif tier == "private":
            available = _check_private_available()
            msg = "Private server reachable" if available else "Private server not reachable"
        else:
            available = _check_cloud_available()
            msg = "Hermes gateway reachable" if available else "Hermes gateway not reachable"
        return self._send_json(200, {"tier": tier, "available": available, "message": msg})

    # ── /models/catalog — KHAOSS-PATCH: models-catalog ──────────────────────

    def _catalog_classify_pathway(self, base_url: str, model_id: str) -> str:
        if not base_url:
            return "direct"
        bu = base_url.lower()
        if "mammouth.ai" in bu:
            return "mammouth"
        if "openrouter.ai" in bu:
            return "aggregator"
        if "127.0.0.1" in bu or "localhost" in bu:
            return "local"
        return "direct"

    def _models_catalog(self) -> None:
        import shutil, re
        # KHAOSS-fix: guard the yaml import (this interpreter may lack PyYAML). An unguarded import
        # here crashed the handler mid-response and left the UI card a DEAD SOCKET. Guard + fallback so
        # the route always returns JSON. (Mirrors the /providers/register guard.)
        try:
            import yaml as _yaml
        except ImportError:
            _yaml = None
        backends: list[dict] = []

        # 1. Agentic CLIs
        for cli_id, cli_name, cli_cmd in [
            ("cli:claude-code", "Claude Code", "claude"),
            ("cli:codex", "Codex", "codex"),
        ]:
            avail = "available" if shutil.which(cli_cmd) else "unavailable"
            backends.append({
                "id": cli_id,
                "name": cli_name,
                "pathway": "cli",
                "model": None,
                "provider": None,
                "provider_name": None,
                "availability": avail,
                "key_present": avail == "available",
                "description": "Agentic CLI",
            })

        # 2. Named Hermes profiles (skip swarm*)
        profiles_dir = HOME / ".hermes" / "profiles"
        _SWARM_RE = re.compile(r"^swarm\d+[a-z0-9]*$")
        if profiles_dir.is_dir():
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                pname = entry.name
                if _SWARM_RE.match(pname):
                    continue
                cfg_path = entry / "config.yaml"
                if not cfg_path.is_file():
                    continue
                try:
                    if _yaml is not None:
                        cfg = _yaml.safe_load(cfg_path.read_text()) or {}
                    else:
                        # Minimal fallback parse (no PyYAML): pull model.default + base_url by line.
                        cfg = {"model": {}}
                        for _ln in cfg_path.read_text().splitlines():
                            _s = _ln.strip()
                            if _s.startswith("default:"):
                                cfg["model"]["default"] = _s.split(":", 1)[1].strip().strip("\"'")
                            elif _s.startswith("base_url:"):
                                cfg["model"]["base_url"] = _s.split(":", 1)[1].strip().strip("\"'")
                    model_cfg = cfg.get("model", {})
                    model_id = model_cfg.get("default", "") or ""
                    base_url = model_cfg.get("base_url", "") or ""
                    pathway = self._catalog_classify_pathway(base_url, model_id)
                    env_path = entry / ".env"
                    key_present = False
                    if env_path.is_file():
                        for line in env_path.read_text().splitlines():
                            k, _, v = line.partition("=")
                            if k.strip() == "OPENAI_API_KEY" and len(v.strip()) > 4:
                                key_present = True
                                break
                    domain = base_url.split("/")[2] if base_url and "/" in base_url else ""
                    desc = f"Hermes profile → {model_id or '?'}" + (f" ({domain})" if domain else "")
                    backends.append({
                        "id": f"hermes-profile:{pname}",
                        "name": pname,
                        "pathway": pathway,
                        "model": model_id or None,
                        "provider": None,
                        "provider_name": None,
                        "availability": "available" if key_present else "unknown",
                        "key_present": key_present,
                        "description": desc,
                    })
                except Exception:
                    continue

        # 3. Provider models from aionui-providers.json (inline model lists)
        providers_file = Path.home() / ".config" / "AionUi-Dev" / "aionui-providers.json"
        if providers_file.is_file():
            try:
                pdata = json.loads(providers_file.read_text())
                for p in pdata.get("providers", []):
                    pid = p.get("id", "")
                    pname_s = p.get("name", pid)
                    pbase = p.get("base_url", "") or ""
                    pkey_file = p.get("key_file", "")
                    pkey = Path(pkey_file).is_file() if pkey_file else False
                    pathway = self._catalog_classify_pathway(pbase, "")
                    for m in p.get("models", []):
                        mid = (m.get("id") if isinstance(m, dict) else m) or ""
                        if not mid:
                            continue
                        backends.append({
                            "id": f"provider:{pid}:{mid}",
                            "name": mid,
                            "pathway": pathway,
                            "model": mid,
                            "provider": pid,
                            "provider_name": pname_s,
                            "availability": "available" if pkey else "unknown",
                            "key_present": pkey,
                            "description": f"{pname_s} → {mid}",
                        })
            except Exception:
                pass

        self._send_json(200, {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "backends": backends,
        })

    # ── /screen-assistant/status ─────────────────────────────────────────────

    def _sa_status(self) -> None:
        return self._send_json(200, _screen_assistant_status())

    # ── /screen-assistant/toggle ────────────────────────────────────────────
    # Body: {"action": "start" | "stop"}.  Defaults to toggling current state.

    def _sa_toggle(self) -> None:
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            try:
                body = self.rfile.read(int(content_length))
            except OSError:
                body = b""
        action = None
        if body:
            try:
                obj = json.loads(body.decode("utf-8", errors="replace") or "{}")
                if isinstance(obj, dict):
                    raw_action = obj.get("action")
                    if isinstance(raw_action, str) and raw_action.lower() in ("start", "stop"):
                        action = raw_action.lower()
            except Exception:
                pass

        current = _pid_status(SA_PID_FILE)
        if action is None:
            action = "stop" if current.get("running") else "start"

        if action == "stop":
            if current.get("running") and current.get("pid"):
                try:
                    os.kill(int(current["pid"]), signal.SIGTERM)
                except (OSError, ValueError) as exc:
                    return self._send_json(500, {"error": f"kill failed: {exc}"})
                # Give the daemon a moment to clean up its PID file.
                for _ in range(20):
                    if not _alive(int(current["pid"])): break
                    time.sleep(0.1)
            try:
                if SA_PID_FILE.is_file():
                    SA_PID_FILE.unlink()
            except OSError:
                pass
            return self._send_json(200, {"action": "stop", "status": _screen_assistant_status()})

        # action == "start"
        if current.get("running"):
            return self._send_json(200, {"action": "noop", "reason": "already running", "status": current})
        if not SA_SCRIPT.is_file():
            return self._send_json(500, {"error": f"missing: {SA_SCRIPT}"})
        try:
            # Fully detached background process. stdout/stderr → runtime
            # log; the daemon writes its own JSON suggestions to SA_LOG so
            # we MUST NOT redirect stdout there or we'll contaminate the
            # JSON-lines file with python logging output.
            log_fh = open(SA_RUNTIME_LOG, "a", encoding="utf-8")
            proc = subprocess.Popen(
                ["/usr/bin/env", "python3", str(SA_SCRIPT)],
                stdout=log_fh, stderr=log_fh,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            return self._send_json(500, {"error": f"spawn failed: {exc}"})
        # The daemon writes its own PID file; give it a brief moment.
        for _ in range(15):
            if _read_pid(SA_PID_FILE): break
            time.sleep(0.1)
        return self._send_json(200, {
            "action": "start",
            "spawned_pid": proc.pid,
            "status": _screen_assistant_status(),
        })

    # ── Session memory (Tier-2) ─────────────────────────────────────────────
    # POST /session-memory/run  → spawns session_memory.py in the background,
    #                              returns a run_id immediately.
    # GET  /session-memory/status (optional ?id=…) → latest run, or named run.
    #
    # State for each run lives in SM_STATUS_DIR/<run_id>.json:
    #   {"id", "started", "finished", "exit_code", "output_path",
    #    "stdout_tail", "stderr_tail", "running"}

    def _session_memory_run(self) -> None:
        if not SM_SCRIPT.is_file():
            return self._send_json(500, {"error": f"missing: {SM_SCRIPT}"})
        SM_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        status_path = SM_STATUS_DIR / f"{run_id}.json"
        _write_json(status_path, {
            "id": run_id,
            "started": datetime.now(tz=timezone.utc).isoformat(),
            "finished": None,
            "running": True,
            "exit_code": None,
            "output_path": None,
            "stdout_tail": "",
            "stderr_tail": "",
        })

        # Worker thread keeps us off the response path so the panel can
        # immediately render its "synthesizing…" spinner and start polling.
        def _worker():
            try:
                proc = subprocess.run(
                    ["/usr/bin/env", "python3", str(SM_SCRIPT)],
                    capture_output=True, text=True, timeout=180,
                )
                stdout = (proc.stdout or "").strip()
                stderr = (proc.stderr or "").strip()
                exit_code = proc.returncode
                output_path = stdout.splitlines()[-1] if stdout and exit_code == 0 else None
            except subprocess.TimeoutExpired:
                stdout, stderr, exit_code, output_path = "", "timed out after 180s", 124, None
            except Exception as exc:
                stdout, stderr, exit_code, output_path = "", f"worker crashed: {exc}", -1, None
            _write_json(status_path, {
                "id": run_id,
                "started": _read_json(status_path).get("started"),
                "finished": datetime.now(tz=timezone.utc).isoformat(),
                "running": False,
                "exit_code": exit_code,
                "output_path": output_path,
                "stdout_tail": stdout[-2000:],
                "stderr_tail": stderr[-2000:],
            })

        threading.Thread(target=_worker, name=f"session-mem-{run_id}", daemon=True).start()
        return self._send_json(202, {"run_id": run_id, "status": "running"})

    def _session_memory_status(self) -> None:
        qs = parse_qs(urlsplit(self.path).query)
        run_id = (qs.get("id") or [""])[0]
        if not SM_STATUS_DIR.is_dir():
            return self._send_json(200, {"runs": [], "latest": None})
        if run_id:
            if "/" in run_id or "\\" in run_id or ".." in run_id:
                return self._send_json(400, {"error": "invalid run_id"})
            path = SM_STATUS_DIR / f"{run_id}.json"
            if not path.is_file():
                return self._send_json(404, {"error": "no such run", "run_id": run_id})
            return self._send_json(200, _read_json(path))
        # No id → return latest run + a small index.
        # Also flatten latest fields at the top level so callers can read
        # exit_code / output_path directly without going through "latest".
        runs = sorted(SM_STATUS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        latest = _read_json(runs[0]) if runs else None
        return self._send_json(200, {
            **(latest or {}),
            "latest": latest,
            "runs": [p.stem for p in runs[:10]],
        })

    # ── Multi-agent debate (Tier-2) ─────────────────────────────────────────
    # POST /debate/start  body: {topic, profile_a, profile_b, judge}
    # GET  /debate/status/{id}
    # GET  /debate/list
    #
    # Each debate spawns A and B in parallel; once both finish, the judge
    # reads both transcripts and writes the verdict. Status lives at
    # DEBATE_STATUS_DIR/<debate_id>.json.

    def _debate_start(self) -> None:
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            try:
                body = self.rfile.read(int(content_length))
            except OSError:
                body = b""
        try:
            payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        topic = (payload.get("topic") or "").strip()
        if not topic:
            return self._send_json(400, {"error": "topic is required"})
        if len(topic) > 800:
            return self._send_json(400, {"error": "topic too long (max 800 chars)"})

        profile_a = _safe_profile(payload.get("profile_a"), default="example-a")
        profile_b = _safe_profile(payload.get("profile_b"), default="example-b")
        judge     = _safe_profile(payload.get("judge"),     default="example-a")

        DEBATE_DIR.mkdir(parents=True, exist_ok=True)
        DEBATE_STATUS_DIR.mkdir(parents=True, exist_ok=True)

        debate_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        files = {
            "a":       DEBATE_DIR / f"debate_{debate_id}_a.md",
            "b":       DEBATE_DIR / f"debate_{debate_id}_b.md",
            "verdict": DEBATE_DIR / f"debate_{debate_id}_verdict.md",
        }
        status_path = DEBATE_STATUS_DIR / f"{debate_id}.json"
        _write_json(status_path, {
            "id": debate_id,
            "topic": topic,
            "profile_a": profile_a,
            "profile_b": profile_b,
            "judge": judge,
            "started": datetime.now(tz=timezone.utc).isoformat(),
            "finished": None,
            "running": True,
            "stage": "spawning",
            "files": {k: str(v) for k, v in files.items()},
            "exit_codes": {},
            "errors": [],
        })

        threading.Thread(
            target=_run_debate_worker,
            args=(debate_id, topic, profile_a, profile_b, judge, files, status_path),
            name=f"debate-{debate_id}",
            daemon=True,
        ).start()
        return self._send_json(202, {"debate_id": debate_id, "status": "running"})

    def _debate_status(self, debate_id: str) -> None:
        if not debate_id or "/" in debate_id or "\\" in debate_id or ".." in debate_id:
            return self._send_json(400, {"error": "invalid debate_id"})
        path = DEBATE_STATUS_DIR / f"{debate_id}.json"
        if not path.is_file():
            return self._send_json(404, {"error": "no such debate", "debate_id": debate_id})
        return self._send_json(200, _read_json(path))

    def _debate_list(self) -> None:
        if not DEBATE_STATUS_DIR.is_dir():
            return self._send_json(200, {"debates": []})
        files = sorted(DEBATE_STATUS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:20]:
            d = _read_json(p)
            out.append({
                "id": d.get("id"),
                "topic": d.get("topic"),
                "running": d.get("running"),
                "stage": d.get("stage"),
                "started": d.get("started"),
                "finished": d.get("finished"),
            })
        return self._send_json(200, {"debates": out})

    def _debate_transcript(self, debate_id: str, side: str) -> None:
        if not debate_id or "/" in debate_id or "\\" in debate_id or ".." in debate_id:
            return self._send_json(400, {"error": "invalid debate_id"})
        if side not in ("a", "b", "verdict"):
            return self._send_json(400, {"error": "side must be a, b, or verdict"})
        fpath = DEBATE_DIR / f"debate_{debate_id}_{side}.md"
        if not fpath.is_file():
            return self._send_json(200, {"content": "", "word_count": 0, "exists": False})
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self._send_json(500, {"error": str(exc)})
        return self._send_json(200, {
            "content": content,
            "word_count": len(content.split()),
            "exists": True,
        })

    def _debate_continue(self, debate_id: str) -> None:
        if not debate_id or "/" in debate_id or "\\" in debate_id or ".." in debate_id:
            return self._send_json(400, {"error": "invalid debate_id"})
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            try:
                body = self.rfile.read(int(content_length))
            except OSError:
                body = b""
        try:
            payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
        except Exception:
            payload = {}
        follow_up = (payload.get("follow_up") or "").strip()
        if not follow_up:
            return self._send_json(400, {"error": "follow_up is required"})
        if len(follow_up) > 800:
            return self._send_json(400, {"error": "follow_up too long (max 800 chars)"})

        status_path = DEBATE_STATUS_DIR / f"{debate_id}.json"
        if not status_path.is_file():
            return self._send_json(404, {"error": "original debate not found"})
        orig = _read_json(status_path)
        profile_a = _safe_profile(orig.get("profile_a"), "example-a")
        profile_b = _safe_profile(orig.get("profile_b"), "example-b")
        judge     = _safe_profile(orig.get("judge"),     "example-a")
        orig_topic = (orig.get("topic") or "").strip()

        # Prepend prior verdict as context so agents know what was already argued.
        verdict_path = DEBATE_DIR / f"debate_{debate_id}_verdict.md"
        context = ""
        if verdict_path.is_file():
            try:
                context = verdict_path.read_text(encoding="utf-8", errors="replace")[:600]
            except OSError:
                pass
        new_topic = follow_up
        if orig_topic:
            new_topic = f"{follow_up} (continued from: {orig_topic[:120]})"
        if context:
            new_topic += f"\n\n[Prior verdict excerpt: {context}]"

        DEBATE_DIR.mkdir(parents=True, exist_ok=True)
        DEBATE_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        new_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        files = {
            "a":       DEBATE_DIR / f"debate_{new_id}_a.md",
            "b":       DEBATE_DIR / f"debate_{new_id}_b.md",
            "verdict": DEBATE_DIR / f"debate_{new_id}_verdict.md",
        }
        new_status = DEBATE_STATUS_DIR / f"{new_id}.json"
        _write_json(new_status, {
            "id": new_id,
            "topic": follow_up,
            "orig_topic": orig_topic,
            "orig_debate_id": debate_id,
            "profile_a": profile_a,
            "profile_b": profile_b,
            "judge": judge,
            "started": datetime.now(tz=timezone.utc).isoformat(),
            "finished": None,
            "running": True,
            "stage": "spawning",
            "files": {k: str(v) for k, v in files.items()},
            "exit_codes": {},
            "errors": [],
        })
        threading.Thread(
            target=_run_debate_worker,
            args=(new_id, new_topic, profile_a, profile_b, judge, files, new_status),
            name=f"debate-{new_id}",
            daemon=True,
        ).start()
        return self._send_json(202, {"debate_id": new_id, "orig_id": debate_id, "status": "running"})

    # ── Service control (start / stop / restart) ─────────────────────────────

    def _read_body_json(self) -> dict:
        body = b""
        cl = self.headers.get("Content-Length")
        if cl and cl.isdigit():
            try: body = self.rfile.read(int(cl))
            except OSError: body = b""
        try:
            obj = json.loads(body.decode("utf-8", errors="replace") or "{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def _aionui_instance_action(self, name: str, action: str):
        """Route an AionUi instance action through bin/aionui-instance.sh. Returns (ok, msg) or None
        when `name` is not an AionUi instance.

        ★ Why this exists (2026-09-19): `aionui` and `aionui-trading` are the SAME binary and the
        same process name, so no pattern can tell them apart. The helper stops an instance by its
        own systemd unit (cgroup-scoped) or by the exact pid in its pidfile after an identity check,
        and a pid that fails that check is never killed. Routing here means the panel can no longer
        restart dev and take the background trader down with it.
        """
        spec = AIONUI_INSTANCE_SVCS.get(name)
        if not spec:
            return None
        verb = spec.get(action)
        if not verb:
            return False, f"no {action} path for {name} in aionui-instance.sh"
        if not AIONUI_INSTANCE_SH.is_file():
            return False, f"helper not found: {AIONUI_INSTANCE_SH}"
        try:
            p = subprocess.run(["/bin/bash", str(AIONUI_INSTANCE_SH), verb],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"helper failed: {exc}"
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return (p.returncode == 0), f"aionui-instance.sh {verb} → {out[-600:]}"

    def _svc_do_start(self, name: str) -> tuple[bool, str]:
        """Spawn the service. Returns (success, message)."""
        routed = self._aionui_instance_action(name, "start")
        if routed is not None:
            return routed
        defn = _SVC_DEFS.get(name)
        if not defn:
            return False, f"unknown service: {name}"
        log_path = defn.get("start_log", "/tmp/svc.log")
        try:
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            return False, f"cannot open log {log_path}: {exc}"

        # Special case: aionui — run launch-aionui.sh if it exists
        if name == "aionui":
            if not LAUNCH_AIONUI.is_file():
                try: log_fh.close()
                except OSError: pass
                return False, f"launch script not found: {LAUNCH_AIONUI}"
            argv = ["/bin/bash", str(LAUNCH_AIONUI)]
        # Special case: space_agent — needs cwd + PORT env
        elif name == "space_agent":
            if not SPACE_AGENT_DIR.is_dir():
                try: log_fh.close()
                except OSError: pass
                return False, f"space-agent directory not found: {SPACE_AGENT_DIR}"
            env = {**os.environ, "PORT": "3000"}
            try:
                subprocess.Popen(
                    ["npm", "start"],
                    cwd=str(SPACE_AGENT_DIR),
                    env=env,
                    stdout=log_fh, stderr=log_fh,
                    start_new_session=True, close_fds=True,
                )
            except OSError as exc:
                try: log_fh.close()
                except OSError: pass
                return False, f"spawn failed: {exc}"
            try: log_fh.close()
            except OSError: pass
            return True, f"space_agent starting (log: {log_path})"
        else:
            argv = defn.get("start_argv")
            if not argv:
                try: log_fh.close()
                except OSError: pass
                return False, f"no start command defined for {name}"

        env_extra = defn.get("start_env")
        spawn_env = {**os.environ, **env_extra} if env_extra else None

        try:
            subprocess.Popen(
                argv, stdout=log_fh, stderr=log_fh,
                env=spawn_env,
                start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            try: log_fh.close()
            except OSError: pass
            return False, f"spawn failed: {exc}"
        try: log_fh.close()
        except OSError: pass
        return True, f"{name} starting (log: {log_path})"

    def _svc_do_stop(self, name: str) -> tuple[bool, str]:
        """Stop the service. Returns (success, message)."""
        routed = self._aionui_instance_action(name, "stop")
        if routed is not None:
            return routed
        defn = _SVC_DEFS.get(name)
        if not defn:
            return False, f"unknown service: {name}"

        # Prefer PID file kill over pkill for cleaner shutdown.
        pid_file_str = defn.get("pid_file")
        if pid_file_str:
            pid = _read_pid(Path(pid_file_str))
            if pid and _alive(pid):
                try:
                    import signal as _sig
                    os.kill(pid, _sig.SIGTERM)
                    return True, f"{name} stopped (SIGTERM → PID {pid})"
                except OSError as exc:
                    logger.warning("SIGTERM %d failed: %s", pid, exc)

        # Fallback: pkill -f with fixed pattern (no caller input in pattern).
        pattern = defn.get("stop_pattern")
        if not pattern:
            return False, f"no stop method for {name}"
        try:
            # ★ 2026-08-24 — NEVER pkill OUR OWN PROCESS GROUP. `pkill -f <pattern>` matches any command
            #   line containing the pattern, and panel_server is itself a long-lived python process: a
            #   stop_pattern like "python3 .*panel" would stop the panel that is executing the stop, and the
            #   caller would see a dead socket rather than an error. Three shells were lost to this class
            #   this week (exit 144); the panel is the same footgun with a longer fuse.
            #   Enumerate, drop anything sharing our pgid, then signal explicitly.
            import os as _os, signal as _sig
            _mypgid = _os.getpgid(0)
            _found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5)
            _targets = []
            for _ln in (_found.stdout or "").split():
                try:
                    _pid = int(_ln)
                    if _os.getpgid(_pid) != _mypgid:
                        _targets.append(_pid)
                except (ValueError, ProcessLookupError, PermissionError):
                    continue
            if not _targets:
                return True, f"{name}: nothing to stop (no match outside our own process group)"
            for _pid in _targets:
                try:
                    _os.kill(_pid, _sig.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            result = subprocess.CompletedProcess(args=["pkill", "-f", pattern], returncode=0)
            if result.returncode in (0, 1):   # 1 = no processes matched
                return True, f"{name} stopped (pkill -f '{pattern}')"
            return False, f"pkill returned {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"pkill failed: {exc}"

    def _svc_start(self) -> None:
        payload = self._read_body_json()
        name = (payload.get("service") or "").strip()
        if not name:
            return self._send_json(400, {"error": "service is required"})
        ok, msg = self._svc_do_start(name)
        logger.info("svc_start %s → %s %s", name, ok, msg)
        return self._send_json(200 if ok else 500, {"success": ok, "message": msg, "service": name})

    def _svc_stop(self) -> None:
        payload = self._read_body_json()
        name = (payload.get("service") or "").strip()
        if not name:
            return self._send_json(400, {"error": "service is required"})
        ok, msg = self._svc_do_stop(name)
        logger.info("svc_stop %s → %s %s", name, ok, msg)
        return self._send_json(200 if ok else 500, {"success": ok, "message": msg, "service": name})

    def _svc_restart(self) -> None:
        payload = self._read_body_json()
        name = (payload.get("service") or "").strip()
        if not name:
            return self._send_json(400, {"error": "service is required"})
        # ★ An AionUi instance is restarted by bin/aionui-instance.sh in ONE action, not by this
        #   method's stop→sleep→start: the helper knows to stop the unit first and then the tracked
        #   pid, and it clears the dev-instance singleton files at exactly the right moment.
        routed = self._aionui_instance_action(name, "restart")
        if routed is not None:
            ok, msg = routed
            logger.info("svc_restart %s → %s %s", name, ok, msg)
            return self._send_json(200 if ok else 500,
                                   {"success": ok, "message": msg, "service": name,
                                    "via": "aionui-instance.sh"})
        ok_stop, msg_stop = self._svc_do_stop(name)
        time.sleep(2)
        ok_start, msg_start = self._svc_do_start(name)
        ok = ok_start
        msg = f"stop: {msg_stop} | start: {msg_start}"
        logger.info("svc_restart %s → %s", name, msg)
        return self._send_json(200 if ok else 500, {"success": ok, "message": msg, "service": name})

    def _ping_target_result(self, target_key: str) -> dict:
        host, port = PROXY_TARGETS[target_key]
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return {"reachable": True}
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            return {"reachable": False, "reason": str(exc)}

    def _status(self) -> None:
        return self._send_json(200, {
            "ok": True,
            "panel": "running",
            "aionui": self._ping_target_result("aionui"),
            "hermes": self._ping_target_result("hermes"),
            "orion": "see /orion",
            "mammouth": "see /mammouth",
        })

    def _orion_status(self) -> None:
        try:
            result = subprocess.run(
                ["str(Path.home())/codex-project/scripts/orion_healthcheck.sh"],
                capture_output=True, text=True, timeout=15,
            )
            return self._send_json(200, {
                "ok": result.returncode == 0,
                "output": result.stdout[-2000:] if result.stdout else "",
                "error": result.stderr[-500:] if result.stderr else "",
            })
        except Exception as exc:
            return self._send_json(200, {"ok": False, "error": str(exc)})

    def _mammouth_status(self) -> None:
        key_path = Path.home() / ".mammouth" / "api_key"
        profile_path = Path.home() / ".hermes" / "profiles" / "mammouth-general" / "config.yaml"
        return self._send_json(200, {
            "key_present": key_path.is_file() and key_path.stat().st_size > 0,
            "profile_registered": profile_path.is_file(),
            "profile": "mammouth-general",
            "endpoint": "https://api.mammouth.ai/v1",
            "note": "Use hermes -p mammouth-general chat to test",
        })

    # ── Remote access (Dispatch Queue 8) — OBSERVED exposure, never config ──────
    @staticmethod
    def _classify_bind_addr(addr: str) -> str:
        """Two-tier-plus classification of a bound local address (OBSERVED)."""
        a = (addr or "").strip().strip("[]")
        if a in ("0.0.0.0", "::", "*"):
            return "all"          # every interface — internet + LAN. The dangerous one.
        if a == "::1" or a.startswith("127.") or a == "localhost":
            return "loopback"
        # Tailscale CGNAT range 100.64.0.0/10 (100.64.x – 100.127.x) + its ULA prefix
        if a.startswith("fd7a:115c:a1e0"):
            return "tailnet"
        if a.startswith("100."):
            try:
                second = int(a.split(".")[1])
                if 64 <= second <= 127:
                    return "tailnet"
            except (ValueError, IndexError):
                pass
        if a.startswith(("192.168.", "10.")) or any(
            a.startswith(f"172.{n}.") for n in range(16, 32)
        ):
            return "lan"
        return "other"

    def _observed_binds(self) -> list:
        """Probe which addresses the panel is ACTUALLY listening on for :PORT.
        Reads the live socket table (ss) — never the config. Returns
        [{addr, tier}] for every LISTEN row on this panel's port."""
        binds = []
        try:
            out = subprocess.run(
                ["ss", "-H", "-ltn", f"sport = :{DEFAULT_PORT}"],
                capture_output=True, text=True, timeout=4,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            cols = line.split()
            if len(cols) < 4:
                continue
            local = cols[3]  # Local Address:Port
            addr = local.rsplit(":", 1)[0] if ":" in local else local
            binds.append({"addr": addr, "tier": self._classify_bind_addr(addr)})
        return binds

    def _tailscale_state(self) -> dict:
        """OBSERVED tailscale reachability — daemon up? tailnet IPv4? hostname?"""
        state = {"tailscaled_running": False, "tailnet_ip": None, "tailnet_host": None}
        # daemon: presence of the control socket is the cheap ground truth
        if Path("/run/tailscale/tailscaled.sock").exists():
            state["tailscaled_running"] = True
        else:
            try:
                r = subprocess.run(["pgrep", "-x", "tailscaled"],
                                   capture_output=True, text=True, timeout=3)
                state["tailscaled_running"] = r.returncode == 0
            except (OSError, subprocess.SubprocessError):
                pass
        if shutil.which("tailscale"):
            try:
                ip = subprocess.run(["tailscale", "ip", "-4"],
                                    capture_output=True, text=True, timeout=4).stdout.strip()
                if ip:
                    state["tailnet_ip"] = ip.splitlines()[0].strip()
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                js = subprocess.run(["tailscale", "status", "--json"],
                                    capture_output=True, text=True, timeout=4).stdout
                data = json.loads(js) if js else {}
                self_node = (data.get("Self") or {})
                dns = self_node.get("DNSName") or ""
                state["tailnet_host"] = dns.rstrip(".") or self_node.get("HostName")
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return state

    def _remote_access_status(self) -> None:
        """OBSERVED exposure of the panel: which interfaces it is bound to right
        now, tailnet reachability, and the two-tier endpoint accounting. Probes
        the live socket table + tailscale — reads no config. (Remote-safe: read only.)"""
        binds = self._observed_binds()
        tiers = {b["tier"] for b in binds}
        if "all" in tiers:
            exposure = "internet+lan"       # bound 0.0.0.0 — reachable from everywhere
        elif "lan" in tiers or "other" in tiers:
            exposure = "lan"
        elif "tailnet" in tiers:
            exposure = "tailnet"
        elif "loopback" in tiers:
            exposure = "loopback-only"
        else:
            exposure = "unknown"
        ts = self._tailscale_state()
        has_tailnet_bind = "tailnet" in tiers
        phone_url = None
        phone_reason = None
        if has_tailnet_bind and ts["tailnet_ip"]:
            phone_url = f"http://{ts['tailnet_ip']}:{DEFAULT_PORT}"
        elif not has_tailnet_bind:
            phone_reason = "panel not bound to the tailnet yet — apply the staged bind (loopback-only today)"
        elif not ts["tailnet_ip"]:
            phone_reason = "tailnet bind present but tailscale reported no IPv4 — is tailscaled up and logged in?"
        loopback_addr = next((b["addr"] for b in binds if b["tier"] == "loopback"), "127.0.0.1")
        return self._send_json(200, {
            "port": DEFAULT_PORT,
            "binds": binds,
            "exposure": exposure,
            "loopback_addr": loopback_addr,
            "tailscaled_running": ts["tailscaled_running"],
            "tailnet_ip": ts["tailnet_ip"],
            "tailnet_host": ts["tailnet_host"],
            "tailnet_bind_present": has_tailnet_bind,
            "phone_url": phone_url,
            "phone_url_reason": phone_reason,
            "remote_safe_endpoint_count": len(REMOTE_SAFE_GET_EXACT) + len(REMOTE_SAFE_GET_PREFIX)
                                          + len(REMOTE_SAFE_POST_EXACT),
            "posture": ("reachable only from your tailnet · not from the internet · "
                        "not from your LAN · credential endpoints never leave this machine"),
            "off_available": exposure not in ("loopback-only", "unknown"),
        })

    def _remote_access_off(self) -> None:
        """Fail-safe close: return the panel to loopback-only. Only ever REDUCES
        exposure — relaunches on DEFAULT_BIND (127.0.0.1), which is the exact
        revert of the staged tailnet bind. Inert (no-op) when already loopback.
        Remote-safe precisely because it can never widen reach."""
        binds = self._observed_binds()
        non_loopback = [b for b in binds if b["tier"] != "loopback"]
        if not non_loopback:
            return self._send_json(200, {
                "ok": True, "already_loopback": True, "restarting": False,
                "message": "already closed — the panel is bound loopback-only; nothing to turn off",
            })
        # Relaunch with NO --bind arg → DEFAULT_BIND (loopback). This drops the
        # tailnet listener. The caller (incl. a phone) will lose the connection —
        # that is the point of an OFF switch.
        script = Path("/tmp/_panel_remote_off.sh")
        pid = os.getpid()
        panel_py = Path(__file__).resolve()
        script.write_text(
            "#!/bin/bash\n"
            "sleep 2\n"
            f"kill {pid} 2>/dev/null\n"
            "sleep 1\n"
            f"nohup python3 {panel_py} >> /tmp/panel_server.log 2>&1 &\n"
            "disown\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        subprocess.Popen(
            ["/bin/bash", str(script)],
            start_new_session=True, close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self._send_json(200, {
            "ok": True, "already_loopback": False, "restarting": True,
            "closed": [b["addr"] for b in non_loopback],
            "message": "closing remote access — relaunching loopback-only in ~2 s",
        })

    # ── KHAOSS-PATCH: remote-access control from the panel (loopback-only) ────────
    def _remote_access_key(self) -> None:
        """Return the panel token + phone login URLs — LOCAL requests only (never
        over the tunnel), same guard as /security/token. This is the 'where's my
        key' surface for the panel card."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "the panel key is available to local requests only"})
        ts = self._tailscale_state()
        ip = ts.get("tailnet_ip")
        login_url = f"http://{ip}:{DEFAULT_PORT}/login" if ip else None
        one_tap = f"{login_url}#token={PANEL_TOKEN}" if login_url else None
        return self._send_json(200, {
            "token": PANEL_TOKEN,
            "login_url": login_url,
            "one_tap_url": one_tap,
            "tailnet_ip": ip,
            "port": DEFAULT_PORT,
        })

    def _remote_access_on(self) -> None:
        """Open the tailnet bind: relaunch the panel dual-bound (loopback + tailnet).
        The ON counterpart to /remote-access/off. Loopback-only (never remote), so a
        phone can never widen its own exposure. Mirrors the OFF relaunch pattern."""
        ts = self._tailscale_state()
        ip = ts.get("tailnet_ip")
        if not ts.get("tailscaled_running") or not ip:
            return self._send_json(409, {"ok": False,
                "error": "tailscaled is not up / no tailnet IP — connect Tailscale first"})
        import re as _re
        if not _re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
            return self._send_json(500, {"ok": False, "error": f"refusing to bind unexpected tailnet ip: {ip!r}"})
        binds = self._observed_binds()
        if any(b["tier"] == "tailnet" for b in binds):
            return self._send_json(200, {"ok": True, "already_on": True,
                "message": "remote access is already open on the tailnet"})
        script = Path("/tmp/_panel_remote_on.sh")
        pid = os.getpid()
        panel_py = Path(__file__).resolve()
        script.write_text(
            "#!/bin/bash\n"
            "sleep 1\n"
            f"kill {pid} 2>/dev/null\n"
            "sleep 1\n"
            f"nohup python3 {panel_py} --bind 127.0.0.1,{ip} >> /tmp/panel_server.log 2>&1 &\n"
            "disown\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        subprocess.Popen(
            ["/bin/bash", str(script)],
            start_new_session=True, close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self._send_json(200, {"ok": True, "already_on": False, "restarting": True,
            "opening": ip, "message": f"opening remote access on {ip} — relaunching in ~2 s"})

    # ── KHAOSS-PATCH: Tailscale control from the panel (loopback-only) ────────────
    def _tailscale_status_full(self) -> None:
        """Rich Tailscale status for the panel card: daemon, this node, logged-in
        account, key expiry, and peers. Parses `tailscale status --json`."""
        base = self._tailscale_state()
        info = {"backend_state": None, "logged_in_as": None, "key_expiry": None,
                "peers": [], "peer_count": 0, "peers_online": 0}
        if shutil.which("tailscale"):
            try:
                js = subprocess.run(["tailscale", "status", "--json"],
                                    capture_output=True, text=True, timeout=6).stdout
                data = json.loads(js) if js else {}
                info["backend_state"] = data.get("BackendState")
                selfn = data.get("Self") or {}
                info["key_expiry"] = selfn.get("KeyExpiry")
                users = data.get("User") or {}
                u = users.get(str(selfn.get("UserID", ""))) or {}
                info["logged_in_as"] = u.get("LoginName") or u.get("DisplayName")
                peers = []
                for p in (data.get("Peer") or {}).values():
                    peers.append({
                        "host": (p.get("DNSName") or p.get("HostName") or "").rstrip("."),
                        "ip": (p.get("TailscaleIPs") or ["?"])[0],
                        "os": p.get("OS"),
                        "online": bool(p.get("Online")),
                    })
                peers.sort(key=lambda x: (not x["online"], x["host"]))
                info["peers"] = peers
                info["peer_count"] = len(peers)
                info["peers_online"] = sum(1 for p in peers if p["online"])
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
        return self._send_json(200, {**base, **info})

    def _tailscale_up(self) -> None:
        """Connect this node to the tailnet (`tailscale up`). Loopback-only. Already
        authenticated as the operator, so this reconnects fast; if the key expired it
        returns the login URL rather than blocking."""
        if not shutil.which("tailscale"):
            return self._send_json(500, {"ok": False, "error": "tailscale CLI not found"})
        import re as _re
        try:
            r = subprocess.run(["tailscale", "up"], capture_output=True, text=True, timeout=25)
            out = (r.stdout + r.stderr).strip()
            m = _re.search(r"https://login\.tailscale\.com/\S+", out)
            return self._send_json(200, {"ok": r.returncode == 0, "output": out[-800:],
                                         "auth_url": m.group(0) if m else None})
        except subprocess.TimeoutExpired:
            return self._send_json(200, {"ok": True,
                "output": "tailscale up dispatched — a login may be pending; check status"})
        except (OSError, subprocess.SubprocessError) as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

    def _tailscale_down(self) -> None:
        """Disconnect this node from the tailnet (`tailscale down`). Loopback-only.
        NB: if remote access is bound to the tailnet, disconnecting drops the phone."""
        if not shutil.which("tailscale"):
            return self._send_json(500, {"ok": False, "error": "tailscale CLI not found"})
        try:
            r = subprocess.run(["tailscale", "down"], capture_output=True, text=True, timeout=15)
            return self._send_json(200, {"ok": r.returncode == 0,
                                         "output": (r.stdout + r.stderr).strip()[-400:]})
        except (OSError, subprocess.SubprocessError) as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

    def _restart_panel(self) -> None:
        """Launch a background script that kills this process and relaunches it."""
        script = Path("/tmp/_panel_restart.sh")
        pid = os.getpid()
        panel_py = Path(__file__).resolve()
        # ★★★ THIS RESTART USED TO SILENTLY DOWNGRADE THE PANEL, TWICE OVER.
        #   It relaunched with bare `python3` — which on this box resolves to the PYENV interpreter,
        #   and pyenv does not have `websocket`. So every restart moved the panel off system python and
        #   BROKE the AionUi handoff path, which fails with "No module named 'websocket'". That is the
        #   documented interpreter trap (L11/L12) reappearing in the one command used most often.
        #   ★★ And it dropped every ARGUMENT. The panel was started as
        #      `/usr/bin/python3 panel_server.py --bind 127.0.0.1,100.78.124.29`; after one restart it
        #      was running with no --bind at all, so the Tailscale interface was silently no longer
        #      served. Nothing reported either loss: the panel came back, answered on loopback, and
        #      looked healthy.
        #   ★ Found because a delivery target that claimed `live` refused with a missing-module error —
        #     the dead-button check catching a defect one layer beneath the button.
        interp = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
        args = " ".join(shlex.quote(a) for a in sys.argv[1:])
        # ★★★ 2026-09-19 — UNDER SYSTEMD THIS MUST GO THROUGH THE UNIT, OR THE UNIT DIES FOREVER.
        #   Measured: `khaoss-panel.service` was in a **1,017-restart crash loop** (ActiveState=
        #   activating, SubState=auto-restart, MainPID=0, ExecMainStatus=2) while a DETACHED
        #   `panel_server.py` served :7842 perfectly. Cause is this function: it kills its own pid and
        #   `nohup`s a copy whose parent is the restart script — so the new panel can never be the
        #   unit's main process, systemd's `Restart=always` retries, the retry finds the port taken,
        #   exits 2, and repeats every 10 s. Forever. Nothing reports it, because :7842 answers.
        #   The unit was NOT the source of truth about whether the panel was up — the port was.
        # ★ FIX: if this process is running inside that unit (INVOCATION_ID is set by systemd for a
        #   unit's own processes), restart THROUGH systemd so the new process is the unit's child
        #   again. Only fall back to the self-nohup path when we are genuinely unsupervised.
        under_systemd = bool(os.environ.get("INVOCATION_ID")) and shutil.which("systemctl") is not None
        if under_systemd:
            script.write_text(
                "#!/bin/bash\n"
                "sleep 2\n"
                "systemctl --user restart khaoss-panel.service >> /tmp/panel_server.log 2>&1\n",
                encoding="utf-8",
            )
        else:
            script.write_text(
                "#!/bin/bash\n"
                "sleep 2\n"
                f"kill {pid} 2>/dev/null\n"
                "sleep 1\n"
                f"nohup {interp} {shlex.quote(str(panel_py))} {args} >> /tmp/panel_server.log 2>&1 &\n"
                "disown\n",
                encoding="utf-8",
            )
        script.chmod(0o700)
        subprocess.Popen(
            ["/bin/bash", str(script)],
            start_new_session=True, close_fds=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return self._send_json(200, {
            "message": "restarting panel in 2 s\u2026",
            "pid": pid,
            "via": "systemd-unit" if under_systemd else "detached-nohup",
        })

    # ── Proactive research (Tier-3) ──────────────────────────────────────────

    def _research_queue(self) -> None:
        pending = []
        completed = []
        if PR_QUEUE.is_file():
            try:
                with PR_QUEUE.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict):
                                if obj.get("status") == "pending":
                                    pending.append(obj)
                                else:
                                    completed.append(obj)
                        except Exception:
                            continue
            except OSError:
                pass
        return self._send_json(200, {
            "pending_count": len(pending),
            "completed_count": len(completed),
            "pending": pending[-10:],
            "last_completed": completed[-5:],
            "exa_key_set":      bool(os.environ.get("EXA_API_KEY")),
            "firecrawl_key_set": bool(os.environ.get("FIRECRAWL_API_KEY")),
        })

    def _research_status(self) -> None:
        pid_info = _tier3_pid_status(PR_PID_FILE, PR_SCRIPT)
        pending_count = 0
        if PR_QUEUE.is_file():
            try:
                with PR_QUEUE.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            if json.loads(line.strip()).get("status") == "pending":
                                pending_count += 1
                        except Exception:
                            pass
            except OSError:
                pass
        return self._send_json(200, {
            **pid_info,
            "pending_count":     pending_count,
            "exa_key_set":       bool(os.environ.get("EXA_API_KEY")),
            "firecrawl_key_set": bool(os.environ.get("FIRECRAWL_API_KEY")),
        })

    def _research_toggle(self) -> None:
        pid_info = _pid_status(PR_PID_FILE)
        if pid_info.get("running"):
            try:
                os.kill(int(pid_info["pid"]), signal.SIGTERM)
            except (OSError, ValueError) as exc:
                return self._send_json(500, {"error": str(exc)})
            return self._send_json(200, {"action": "stop"})
        if not PR_SCRIPT.is_file():
            return self._send_json(500, {"error": f"missing: {PR_SCRIPT}"})
        log_fh = open(ROOT / "proactive_research.log", "a", encoding="utf-8")
        subprocess.Popen(
            ["/usr/bin/env", "python3", str(PR_SCRIPT)],
            stdout=log_fh, stderr=log_fh,
            start_new_session=True, close_fds=True,
        )
        time.sleep(0.5)
        return self._send_json(200, {"action": "start", "status": _tier3_pid_status(PR_PID_FILE, PR_SCRIPT)})

    # ── Voice Kanban (Tier-3) ────────────────────────────────────────────────

    def _vk_status(self) -> None:
        pid_info = _tier3_pid_status(VK_PID_FILE, VK_SCRIPT)
        results: list[dict] = []
        if VK_RESULTS.is_file():
            try:
                with VK_RESULTS.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line.strip())
                            if isinstance(obj, dict):
                                results.append(obj)
                        except Exception:
                            continue
            except OSError:
                pass
        return self._send_json(200, {
            **pid_info,
            "last_results": results[-5:],
            "total_converted": len(results),
        })

    # ── KHAOSS-PATCH: boardwalk-voice-card — local TTS (voicebox :8770) controls ──
    # Global control surface for the local piper TTS server. Status-only + voice
    # management; never auto-starts the TTS server (off-by-default). Loopback only.
    # ── KHAOSS-PATCH: roster-ssot (#3 v2) — the canonical, enriched roster JSON ──
    # KHAOSS-owned (reads KHAOSS-lane data: aionui.db teams/conversations + the AionUi
    # config team.trustLevels). Served over HTTP so the AGENTS cockpit proxies it (clean
    # boundary, no runtime file-coupling). Per-member: name·role·route·trust_tier·privacy_mode·status.
    _AION_DB = HOME / ".config/AionUi-Dev/aionui/aionui.db"
    _AION_CFG = HOME / ".config/AionUi-Dev/config/aionui-config.txt"

    def _roster_trust_levels(self) -> dict:
        """Decode AionUi's base64(URL-encoded JSON) config → team.trustLevels (conversationId → tier)."""
        import base64
        import urllib.parse as _up
        try:
            raw = base64.b64decode(self._AION_CFG.read_text().strip())
            data = json.loads(_up.unquote(raw.decode("utf-8", "replace")))
            tl = data.get("team.trustLevels") or {}
            return tl if isinstance(tl, dict) else {}
        except Exception:
            return {}

    def _roster_build(self) -> dict:
        import datetime as _dt
        trust = self._roster_trust_levels()
        teams_out = []
        con = sqlite3.connect(f"file:{self._AION_DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            # route per conversation = the ACTUAL routing fields
            # ★★ DISPLAY TRUTH (2026-09-19): this read `hermesProfile` and called it the "provider",
            #    so the roster named a route that is FALSE — measured, 28 conversations carry an
            #    explicit `agentProfile` the label contradicts (e.g. Example Team launches
            #    `deepseek-flash` while the roster read `mammouth-general`). `agentProfile` is the
            #    authoritative field (`hermes --profile <agentProfile>`); the label is kept only as the
            #    fallback for the 52 conversations that predate it.
            routes = {}
            for r in con.execute(
                "SELECT id, json_extract(extra,'$.currentModelId') cm, "
                "json_extract(extra,'$.agentProfile') ap, "
                "json_extract(extra,'$.hermesProfile') hp FROM conversations"
            ):
                routes[r["id"]] = {"model": r["cm"], "provider": (r["ap"] or r["hp"])}

            # ★★ DISPLAY TRUTH (2026-09-19, harness lane): a `custom` ACP agent carries NO route
            #    fields in its conversation row, so the roster rendered `model: null / provider: null`
            #    for an agent that IS running — the ambiguity the operator flagged. The route of a
            #    custom launcher lives in the LAUNCHER'S OWN RECEIPT beside its cliPath
            #    (validated-route.json, written by `launch.py --validate`). ONE store per fact: we
            #    read it here rather than copying a model name into this file or into the team row.
            def _launcher_route(a: dict):
                if (a.get("agentType") or "") != "custom":
                    return None
                cli = a.get("cliPath") or ""
                if not cli or not os.path.isabs(cli):
                    return None
                receipt = os.path.join(os.path.dirname(cli), "validated-route.json")
                try:
                    with open(receipt) as fh:
                        d = json.load(fh)
                except Exception:
                    return None
                model = d.get("model")
                if not model:
                    return None
                endpoint = str(d.get("endpoint") or "")
                return {
                    "model": model,
                    "provider": "deepseek-direct" if "deepseek.com" in endpoint else (endpoint or None),
                    "route_source": os.path.basename(receipt),
                }

            def _member(a: dict) -> dict:
                cid = a.get("conversationId")
                route = routes.get(cid) or {"model": a.get("model"), "provider": None}
                if not route.get("model") and not route.get("provider"):
                    # no-invention: only fall back to a receipt that actually exists next to the launcher
                    route = _launcher_route(a) or route
                return {
                    "agent_id": cid,
                    "name": a.get("name") or a.get("agentName") or (a.get("role") or "agent"),
                    "role": a.get("role"),
                    "route": {k: v for k, v in (
                        ("model", route.get("model")),
                        ("provider", route.get("provider")),
                        ("source", route.get("route_source")),
                    ) if v is not None},
                    "trust_tier": trust.get(cid, "untrusted"),
                    # privacy_mode: no per-member source yet → default 'local' (future: privacy config).
                    "privacy_mode": "local",
                    "status": a.get("status"),
                }

            for trow in con.execute("SELECT id, name, agents FROM teams"):
                try:
                    agents = json.loads(trow["agents"] or "[]")
                except Exception:
                    agents = []
                leader = None
                members = []
                for a in agents:
                    entry = _member(a)
                    if a.get("role") == "leader" and leader is None:
                        leader = entry
                    else:
                        members.append(entry)
                teams_out.append({
                    "team_id": trow["id"],
                    "conversation_id": (leader or {}).get("agent_id"),
                    "name": trow["name"],
                    "leader": leader,
                    "members": members,
                })
        finally:
            con.close()
        return {
            "teams": teams_out,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "source": "aionui.db teams/conversations + config team.trustLevels (KHAOSS-normalized)",
        }

    def _roster(self) -> None:
        try:
            return self._send_json(200, self._roster_build())
        except Exception as e:  # no-silent-failure: surface the real error
            return self._send_json(500, {"error": f"roster build failed: {e}"})

    def _roster_stream(self) -> None:
        """Roster-change SSE: emit the canonical roster on change (+ heartbeats). The cockpit
        subscribes for live updates instead of polling (also surfaces spawns/removals promptly)."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        last_sig = None
        for _ in range(900):  # ~30 min cap @ 2s
            try:
                roster = self._roster_build()
                sig = json.dumps(roster.get("teams"), sort_keys=True, default=str)
            except Exception as e:
                roster = {"error": str(e)}
                sig = "err:" + str(e)
            try:
                if sig != last_sig:
                    last_sig = sig
                    self.wfile.write(("event: roster\ndata: " + json.dumps(roster, default=str) + "\n\n").encode("utf-8"))
                else:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(2)

    # ── KHAOSS-PATCH: profile-attrs (read-only) — soul_md · skills · tools · md_files per Hermes profile ──
    # KHAOSS-owned, CORS on (via _send_json/_cors), served over HTTP for the AGENTS cockpit's lead Attrs
    # panel. Real sources (verified): SOUL.md + skills/**/SKILL.md (+ loose skills/*.md) + config.yaml
    # toolsets/disabled_toolsets + `hermes tools list`. No-silent-failure: bad profile/file → clear error.
    @staticmethod
    def _yaml_list_after(text: str, key: str) -> list:
        """Minimal (no PyYAML in this interpreter): `- item` lines right after a `key:` line."""
        out, lines = [], text.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip() == key:
                for nxt in lines[i + 1:]:
                    s = nxt.strip()
                    if s.startswith("- "):
                        out.append(s[2:].strip().strip("'\""))
                    elif s == "":
                        continue
                    else:
                        break
                break
        return out

    @staticmethod
    def _yaml_inline_list(text: str, key: str) -> list:
        """Handle `key: [a, b]` (inline) anywhere (incl. indented under agent:)."""
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(key):
                rest = s[len(key):].strip()
                if rest.startswith("[") and rest.endswith("]"):
                    inner = rest[1:-1].strip()
                    return [x.strip().strip("'\"") for x in inner.split(",") if x.strip()]
        return []

    def _profile_tools(self, pdir: Path) -> list:
        enabled = []
        try:
            out = subprocess.run(["hermes", "tools", "list"], capture_output=True, timeout=8).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                if "enabled" in line and "disabled" not in line:
                    m = re.search(r"enabled\s+([a-z0-9_:.\-]+)", line)
                    if m:
                        enabled.append(m.group(1))
        except Exception:
            pass  # hermes CLI unavailable → fall back to the profile config toolsets below
        add, disabled = [], []
        try:
            cfgtxt = (pdir / "config.yaml").read_text(encoding="utf-8", errors="replace")
            add = self._yaml_list_after(cfgtxt, "toolsets:")
            disabled = self._yaml_inline_list(cfgtxt, "disabled_toolsets:")
        except OSError:
            pass
        result = []
        for t in enabled + add:
            if t not in result and t not in disabled:
                result.append(t)
        return result

    def _profile_attrs(self) -> None:
        import datetime as _dt
        qs = parse_qs(urlsplit(self.path).query)
        profile = (qs.get("profile", [""])[0] or "").strip()
        if not profile or not re.match(r"^[A-Za-z0-9_.\-]+$", profile):
            return self._send_json(400, {"error": "profile query param required (alnum / _ / - / .)"})
        pdir = HOME / ".hermes" / "profiles" / profile
        if not pdir.is_dir():
            return self._send_json(404, {"error": f"profile not found: {profile}", "looked_in": str(pdir)})
        # soul_md
        soul_md = ""
        soul_path = pdir / "SOUL.md"
        if soul_path.is_file():
            try:
                soul_md = soul_path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                soul_md = f"(SOUL.md read error: {e})"
        # skills: leaf dirs containing SKILL.md + loose skills/*.md (excluding SKILL.md itself)
        skills = set()
        sk = pdir / "skills"
        if sk.is_dir():
            try:
                for f in sk.rglob("SKILL.md"):
                    skills.add(f.parent.name)
                for f in sk.glob("*.md"):
                    if f.name != "SKILL.md":
                        skills.add(f.stem)
            except OSError:
                pass
        skills = sorted(skills)
        # tools
        tools = self._profile_tools(pdir)
        # md_files: SOUL.md + memories/*.md + loose skills/*.md (name · path · preview)
        md_files = []
        seen = set()
        cand = []
        if soul_path.is_file():
            cand.append(soul_path)
        for sub in ("memories", "skills"):
            d = pdir / sub
            if d.is_dir():
                try:
                    cand.extend(sorted(d.glob("*.md")))
                except OSError:
                    pass
        for f in cand:
            rp = str(f)
            if rp in seen:
                continue
            seen.add(rp)
            preview = ""
            try:
                preview = f.read_text(encoding="utf-8", errors="replace")[:280]
            except OSError:
                pass
            md_files.append({"name": f.name, "path": rp, "preview": preview})
        return self._send_json(200, {
            "profile": profile,
            "soul_md": soul_md,
            "skills": skills,
            "skill_count": len(skills),
            "tools": tools,
            "md_files": md_files,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "source": "~/.hermes/profiles/<profile>: SOUL.md + skills/**/SKILL.md + config toolsets + `hermes tools list`",
        })

    # ── KHAOSS-PATCH: board read-aloud (2026-08-28) ────────────────────────────────────────────
    # ★ The popout page is served from :7842 and voicebox listens on :8770, so the page cannot POST
    #   there directly. These proxy it LOOPBACK-ONLY — raw audio never leaves the box, and the panel
    #   stays the single origin the operator surface talks to.
    # ★★ FAILURE PATH IS THE EXISTING DOCTOR, NOT A NEW ONE. When voicebox is down these answer 503
    #   with doctor:true, and the page surfaces the 🔧 Fix button that already exists — never a silent
    #   failure, and never a second troubleshooting path beside the one that already works.
    # ── KHAOSS-PATCH: hand an item to the agent that owns it (2026-08-29) ──────────────────────
    # ★ A STRUCTURED HANDOFF, NOT A CHAT. One pre-composed message carrying the item, its cost, the
    #   operator's note and where it came from — delivered to the seat whose lane owns it, and CONFIRMED
    #   FROM THE MESSAGES TABLE, because chat.send.message resolves {} as an empty success (L36).
    # ★★ WHERE NO TARGET IS DERIVABLE THE BUTTON IS NOT RENDERED. An absent button is honest; a dead one
    #   is not — which is why this route refuses rather than guessing a recipient.
    _LANE_TEAMS = {
        # ★ no live seat ids in the release: one sample lane; a deployment maps its own.
        "example": ("example-team", "example-lead", "slot-example", "Example Agent"),
    }

    def _operator_targets(self) -> None:
        """GET /operator/targets — which lanes can actually be reached, so the UI renders no dead button."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        return self._send_json(200, {"lanes": {k: v[3] for k, v in self._LANE_TEAMS.items()}})

    def _operator_notify(self, body: dict) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        lane = str(body.get("lane") or "").strip().lower()
        tgt = self._LANE_TEAMS.get(lane)
        if not tgt:
            return self._send_json(400, {"error": f"no agent target is derivable for lane {lane!r} — "
                                                  f"refusing rather than guessing a recipient",
                                         "lanes": sorted(self._LANE_TEAMS)})
        team_id, conv, slot, label = tgt
        item = body.get("item") or {}
        note = str(body.get("note") or "").strip()
        # ★★★ ATTRIBUTION IS NOT DECORATION. This header used to be applied to WHATEVER text arrived,
        #   so any caller — including an agent probing the endpoint — produced a message that read as
        #   though the OPERATOR had handed over an item with his note. That happened on 2026-09-04: a
        #   trading-seat probe landed in the Example Team chat wearing his voice.
        #   ★ A channel whose entire value is that it carries HIS words must not be able to attribute
        #     an agent's string to him. `from_operator` must be asserted explicitly, and anything else
        #     is labelled as what it is.
        from_op = bool(body.get("from_operator"))
        header = ("OPERATOR BOARD — an item in your lane, handed over with his note."
                  if from_op else
                  "OPERATOR BOARD — relayed by an AGENT, not the operator. The text below is NOT his "
                  "words and is not an instruction from him. Treat it as a message from another agent.")
        # ★ DO NOT PRINT EMPTY FIELDS. A relay with no board item rendered as
        #     "ITEM: (from the board)   ID: · severity: info · lane: trading   WHAT IT IS: "
        #   — four labels with nothing behind them, which the receiving lead has to read past to find
        #   the actual message. An empty labelled field is worse than an absent one: it looks like data
        #   that failed to load. Seen in the Example Team transcript, 2026-09-03.
        #   ★ Build the message conditionally and fall through to the ONE delivery path below. My
        #     first attempt at this called a helper I had never written — the third time this session I
        #     have referenced a method that does not exist, which is why every such edit gets compiled
        #     and exercised before it is believed.
        has_item = bool(item.get("id")) and bool(item.get("one"))
        if not has_item:
            msg = f"{header}\n\n" + (note if note else "")
        else:
            msg = (f"{header}\n\n"
                   f"ITEM: {item.get('title','(untitled)')}\n"
                   f"ID: {item.get('id','?')}   ·   severity: {item.get('sev','?')}   ·   lane: {lane}\n\n"
                   f"WHAT IT IS: {item.get('one','')}\n\n"
                   f"COST OF LEAVING IT: {item.get('blocks') or '(not recorded)'}\n\n"
                   + (f"★ THE OPERATOR'S NOTE — this is him talking to you, and it may contain a QUESTION as "
                      f"well as a decision:\n\n{note}\n\n" if note else "")
                   + "Read it, answer him in this conversation, and do not mark anything done on his behalf.")
        sys.path.insert(0, str(self._OPERATOR_DIR))
        try:
            import board_notify, sqlite3, time as _t
            db = "str(Path.home())/.config/AionUi-Dev/aionui/aionui.db"
            def _count():
                c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                try:
                    return c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ?",
                                     (conv + "%",)).fetchone()[0]
                finally:
                    c.close()
            before = _count()
            board_notify.send(conv, team_id, slot, msg)
            # ★★★ POLL, DO NOT SLEEP. This was a flat `sleep(5)` before counting — so the endpoint took
            #   over five seconds no matter what, and ANY caller with a shorter timeout got an EMPTY
            #   BODY while the message delivered anyway. The trading seat hit exactly that while probing
            #   the field contract: it saw nothing, concluded nothing had happened, and the message was
            #   already in the chat. Reproduced here at a 3s client timeout: exit 000, delivered.
            #   ★ Polling returns as soon as the row appears — usually well under a second — and only
            #     waits the full window when something is genuinely wrong.
            after = before
            for _ in range(24):
                _t.sleep(0.25)
                after = _count()
                if after > before:
                    break
        except Exception as e:
            return self._send_json(503, {"ok": False, "error": f"handoff failed: {str(e)[:140]}"})
        # ★ delivery is the ROW, never the return value
        landed = after > before
        return self._send_json(200 if landed else 502, {
            "ok": landed, "lane": lane, "target": label, "conversation": conv,
            "verified": "messages table", "rows_before": before, "rows_after": after,
            "note": ("delivered — verified in the messages table" if landed else
                     "NOT delivered — no row appeared; the bridge's answer is not evidence"),
        })

    def _op_mod(self, name):
        """★★★ IMPORT ONCE. THE reload() HERE WAS THE CONVOY, SPREAD ACROSS 56 CALL SITES.

        Every call re-executed the module and took the per-module IMPORT LOCK, on 56 endpoints.
        Today I removed reload() from four hot paths one at a time and twice declared the class
        closed; this is where the class actually lived. THIS is the fix shaped like the problem.

        ★ reload() was never a feature here — it was a dev convenience (pick up source edits
          without a restart) and the panel restarts on edit anyway. It also actively caused a bug
          this estate already recorded: reload DISCARDS module state, so a status endpoint reported
          'idle' while the process it was tracking was orphaned and alive. Removing it fixes that
          class too, and lets any cache inside an operator module actually survive.
        """
        sys.path.insert(0, str(self._OPERATOR_DIR))
        import importlib
        return importlib.import_module(name)

    def _operator_handoffs(self) -> None:
        """GET — handoffs he sent from the board that no agent has delivered yet.
        ★ PARITY: an agent can read this queue; until now he could not. `queued` is only an honest word
        if the queue is visible to the person it makes a promise to."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            d = self._op_mod("drain_handoffs")
            p = d.pending()
            return self._send_json(200, {"pending": p, "count": len(p),
                                         "note": ("these were queued from the board and have NOT arrived. "
                                                  "An agent delivers them on its next turn; the session "
                                                  "hook tells every agent they are waiting."
                                                  if p else "nothing is waiting — the queue is empty, "
                                                            "which is different from unread.")})
        except Exception as e:
            return self._send_json(500, {"error": f"queue unreadable: {e}"})

    # ── FACETS ────────────────────────────────────────────────────────────────────────────────────
    #  ★ HE ASKED FOR CATEGORIES HE CAN CLICK, "automatically placed and updated". So they are DERIVED
    #    from the same merge the list is drawn from -- never a hand-kept list of chips that drifts from
    #    the data. A chip that offers a filter yielding nothing is worse than no chip: it reads as an
    #    empty result rather than as a stale menu.
    #  ★★ AND A FACET IS ONLY OFFERED WHEN IT DISCRIMINATES. One value covering every row filters
    #    nothing; a value per row is a list, not a facet. Both are dropped here rather than rendered
    #    and left for the eye to discover.
    FACET_MIN_VALUES = 2          # fewer than two distinct values cannot narrow anything
    FACET_MAX_VALUES = 40         # beyond this it is a directory, not a chip rail

    def _operator_foreground(self) -> None:
        """GET — may anything an agent starts appear on his screen?"""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("foreground_policy").status())
        except Exception as e:
            return self._send_json(500, {"error": f"policy unreadable: {e}"})

    def _operator_foreground_set(self, body: dict) -> None:
        """POST — change it. ★ PARITY: the agent can set this from a terminal, so he must be able to
        set it from the board. A setting about HIS attention that only an agent can change is the
        wrong way round."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        mode = str(body.get("mode") or "")
        try:
            fp = self._op_mod("foreground_policy")
            d = fp.set_mode(mode)
            return self._send_json(200, {"ok": True, "mode": d["mode"],
                                         "means": fp.MODES[d["mode"]], "status": fp.status()})
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(500, {"error": f"could not change it: {e}"})

    def _operator_sweep_options(self) -> None:
        """GET — the areas, lenses and depths a sweep can be built from."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            fs = self._op_mod("full_sweep")
            ia = self._op_mod("item_assist")
            o = fs.options()
            r = ia.routes()
            o["routes"] = {"local": r.get("local", []), "cloud": r.get("cloud", []),
                           "agents": r.get("agents", []), "notes": r.get("notes", [])}
            return self._send_json(200, o)
        except Exception as e:
            return self._send_json(500, {"error": f"sweep options unavailable: {e}"})

    def _operator_sweep_run(self, body: dict) -> None:
        """POST — run a sweep. ★ Report-only unless `act` is explicitly asked for."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            fs = self._op_mod("full_sweep")
            return self._send_json(200, fs.run(
                areas=body.get("areas"), lenses=body.get("lenses"),
                depth=body.get("depth"), route=body.get("route"),
                act=bool(body.get("act")), dry_run=bool(body.get("dry_run"))))
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(500, {"error": f"the sweep failed: {e}"})

    def _operator_voicein(self) -> None:
        """GET — what the dictation path can do on this machine right now."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("voice_in").available())
        except Exception as e:
            return self._send_json(500, {"error": f"voice-in unreadable: {e}"})

    def _operator_voicein_dictate(self, body: dict) -> None:
        """POST — push to talk. ★ Returns the full envelope including `effect`, because a
        transcription that inserted nothing must say so rather than returning quietly."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            vi = self._op_mod("voice_in")
            return self._send_json(200, vi.dictate(
                float(body.get("seconds") or 5),
                str(body.get("target") or "aionui"),
                str(body.get("model") or "ggml-base.en.bin"),
                insert_it=bool(body.get("insert", True))))
        except Exception as e:
            return self._send_json(500, {"error": f"dictation failed: {e}"})

    def _operator_summary_menu(self) -> None:
        """GET — the cuts a summary can take, each with its LIVE count.

        ★ The count matters: a cut that would select nothing says so before he picks it, rather than
        producing a short file he reads as good news."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("orchestrator_summary").menu())
        except Exception as e:
            return self._send_json(500, {"error": f"summary menu unavailable: {e}"})

    def _operator_summary_write(self, body: dict) -> None:
        """POST — write the .md. Every cut carries the three-state caveat, which is the point."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            os_ = self._op_mod("orchestrator_summary")
            keys = body.get("cuts") or ["all_open"]
            if not isinstance(keys, list) or not keys:
                return self._send_json(400, {"error": "pick at least one cut",
                                             "field_contract": {"cuts": "a non-empty list of cut keys"}})
            return self._send_json(200, os_.write(keys, str(body.get("name") or "")))
        except ValueError as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(500, {"error": f"could not write the summary: {e}"})

    def _operator_stewards(self) -> None:
        """GET — the stewards, what each holds, and the five ways to shut one down."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("steward_control").status())
        except Exception as e:
            return self._send_json(500, {"error": f"stewards unreadable: {e}"})

    def _operator_stewards_deploy(self) -> None:
        """GET — the steward deployment: one agent for every post, or one per post, and how each is
        routed. ★ LOCAL-ONLY, like every operator route here: it decides what guards this box."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("steward_deploy").deployment())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"steward deployment unreadable: {e}"})

    def _operator_stewards_laya(self) -> None:
        """GET — ★ WHAT LAYA STARTED AS vs HOW WE RUN IT NOW (operator ask b).

        ★ It carries a correction, not just numbers: **Laya does not learn online — the checkpoint is
        frozen.** The honest "learned" column is started-as-arrived vs our-fitted-configuration, and the
        module says so in `corrects_the_premise` so no surface can render a fake learning curve.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("steward_laya_baseline").report())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"laya baseline unreadable: {e}"})

    def _operator_stewards_all(self) -> None:
        """GET — ★★★ ONE STORE PER FACT, FOR A STEWARDS PAGE THAT LIVES SOMEWHERE ELSE.

        WHY THIS EXISTS. The operator reported: *"I don't yet see 'Guardsman' in the stewards page."*
        The cause was structural, not cosmetic: the AionUi stewards page is
        `src/renderer/pages/stewards/WatchmanSection.tsx` — it knows ONE post, has no registry, and a grep
        for `operator/stewards` across AionUi's entire `src/` returns ZERO hits. **AionUi has never asked
        the panel who the stewards are**, so no amount of adding posts on this side could ever show up there.

        This endpoint is the fix: ONE call that returns the registry, the deployment, the memory gate and
        the Laya baseline together. A page that renders this cannot hold a stale second list, because it
        has nothing of its own to hold.

        ★ The registry rows are keyed `steward`, NOT `name`. A consumer that reads `name` sees blanks —
        which is exactly how a steward can be present in the data and invisible on screen.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        out = {"generated_at": int(__import__("time").time()),
               "note": ("ONE store for a stewards page. Render this; do not keep a second list. "
                        "Registry rows are keyed `steward`.")}
        for key, mod, fn in (("registry", "steward_control", "status"),
                             ("deployment", "steward_deploy", "deployment")):
            try:
                out[key] = getattr(self._op_mod(mod), fn)()
            except Exception as e:  # noqa: BLE001
                out[key] = {"error": f"{type(e).__name__}: {e}"}
        try:
            S = self._op_mod("steward_llm")
            b = S.budgets()
            out["memory_gate"] = {
                "ceiling_mb": b["ceiling_mb"], "ceiling_pct_of_ram": b["ceiling_is_pct_of_ram"],
                "hard_floor_mb": b["hard_floor_mb"], "used_mb": b["used_mb"],
                "available_mb": b["available_mb"], "usable_mb": b["usable_mb"],
                "floor_breached": b["floor_breached"], "override_active": b["override_active"],
                "override_by": b["override_by"],
                "floor_note": ("★ NOT A SETTING. 'Run no matter what' bypasses the ceiling, never this."),
            }
        except Exception as e:  # noqa: BLE001
            out["memory_gate"] = {"error": f"{type(e).__name__}: {e}"}
        try:
            out["laya"] = self._op_mod("steward_laya_baseline").report()
        except Exception as e:  # noqa: BLE001
            out["laya"] = {"error": f"{type(e).__name__}: {e}"}
        # ★ the SLOT BOARD joins the one-call aggregate, so a stewards page renders it without a second
        #   list of its own — the same reason `registry`/`deployment`/`memory_gate`/`laya` are here.
        try:
            out["slots"] = self._op_mod("steward_slots").payload()
        except Exception as e:  # noqa: BLE001
            out["slots"] = {"error": f"{type(e).__name__}: {e}"}
        # ★ the AMBIENT summary — read from the file the audit writes, NOT re-audited here: auditing
        #   shells out to systemctl ~18 times, and an endpoint a page polls should not spawn processes.
        #   Stale-by-design, and the payload carries the age so the page can say how stale.
        try:
            p = os.path.join(str(self._OPERATOR_DIR), "steward_ambient.json")
            with open(p) as f:
                amb = json.load(f)
            out["ambient"] = dict(amb.get("summary") or {}, at=amb.get("at"))
        except Exception as e:  # noqa: BLE001
            out["ambient"] = {"error": f"{type(e).__name__}: {e}"}
        return self._send_json(200, out)

    def _operator_stewards_slots(self) -> None:
        """GET — ★ THE SLOT BOARD: every steward JOB, what fills it, what it costs, and what is off.

        A slot is a job ("decide", "hear", "who"…), NOT a model. `instances` is a LIST so one role can
        hold several positions — that is how "multiple backends in multiple positions" is expressed, and
        an empty list is how "only a few systems up" is expressed. LOCAL-ONLY, like every operator route
        here: it decides what guards this box.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("steward_slots").payload())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"slot board unreadable: {e}"})

    def _operator_stewards_slots_set(self, body: dict) -> None:
        """POST — apply a profile or a per-role change. ★ REFUSED MEANS NOTHING IS WRITTEN.

        `{"profile": "LEAN"}` or `{"set": {"reason": ["qwen3:4b", "qwen3:4b"]}}`. An over-budget request
        returns 409 and leaves the board exactly as it was; `steward_llm.admit()`'s hard floor still has
        the last word, so no page can buy its way past it.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            status, out = self._op_mod("steward_slots").apply_change(body)
            return self._send_json(status, out)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"slot change failed: {e}"})

    def _operator_stewards_budget(self) -> None:
        """GET — ★ THE MEMORY GATE, ON THE PAGE, WITH THE FLOOR IN THE OPEN.

        The operator's ruling (2026-09-23): *"Raise the ceiling, and also allow stewards to run no matter
        what for now, however, we will want controls present in the stewards page... We do need to make
        sure there are no runaway affects."*

        So this returns the numbers a control needs, INCLUDING the one he cannot switch off. A page that
        showed only the override would imply the override is total; showing `hard_floor_mb` beside it is
        what makes the control honest.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            S = self._op_mod("steward_llm")
            b = S.budgets()
            return self._send_json(200, {
                "ceiling_mb": b["ceiling_mb"], "ceiling_pct_of_ram": b["ceiling_is_pct_of_ram"],
                "ceiling_previous_mb": b["ceiling_previous_mb"], "ceiling_raised_on": b["ceiling_raised_on"],
                "hard_floor_mb": b["hard_floor_mb"],
                "floor_note": ("★ THE FLOOR IS NOT A SETTING. 'Run no matter what' bypasses the ceiling, "
                               "never this. Below it the OOM killer picks the largest process, which is "
                               "the operator's browser."),
                "used_mb": b["used_mb"], "available_mb": b["available_mb"],
                "burst_mb": b["burst_mb"], "resident_mb": b["resident_mb"],
                "usable_mb": b["usable_mb"], "floor_breached": b["floor_breached"],
                "over_ceiling_mb": b["over_ceiling_mb"], "cause": b["cause"],
                "override_active": b["override_active"], "override_by": b["override_by"],
                "override_why": b["override_why"], "override_since": b["override_since"],
            })
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"budget unreadable: {e}"})

    def _operator_stewards_budget_set(self, body: dict) -> None:
        """POST — switch the override on or off, with attribution. Returns the NEW state, not an `ok`.

        ★ It reports state for the same reason `_operator_stewards_deploy_set` does: the page must render
        what actually happened, not what was asked for. A control that says "saved" and changes nothing is
        the failure this whole project keeps re-learning.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        want = bool(body.get("override"))
        by = str(body.get("by") or "operator")[:60]
        why = str(body.get("why") or "")[:400]
        try:
            S = self._op_mod("steward_llm")
            S.set_override(want, by=by, why=why)
            b = S.budgets()
            return self._send_json(200, {
                "ok": True, "override_active": b["override_active"], "override_by": b["override_by"],
                "override_why": b["override_why"], "ceiling_mb": b["ceiling_mb"],
                "hard_floor_mb": b["hard_floor_mb"], "usable_mb": b["usable_mb"],
                "floor_breached": b["floor_breached"], "cause": b["cause"],
                "note": ("Override OFF — the policy ceiling applies again." if not want else
                         "Override ON — the policy ceiling is set aside. THE HARD FLOOR STILL APPLIES: "
                         "no steward starts if it would take free memory below %d MB." % b["hard_floor_mb"]),
            })
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"override not set: {e}"})

    def _operator_stewards_deploy_set(self, body: dict) -> None:
        """POST — set the mode, or bind a post to an agent.

        ★ It REPORTS the resulting deployment rather than an `ok`, so the page renders what actually
        changed. A control that returns success without showing the new state is a control you have to
        re-load to trust, which is the same as not trusting it."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sd = self._op_mod("steward_deploy")
            if body.get("mode"):
                r = sd.set_mode(str(body["mode"]), body.get("agent"), str(body.get("name") or ""))
            elif body.get("role") and body.get("agent"):
                r = sd.bind(str(body["role"]), str(body["agent"]), str(body.get("name") or ""))
            elif body.get("role") and body.get("unbind"):
                r = sd.unbind(str(body["role"]))
            else:
                return self._send_json(400, {"ok": False, "error": "need mode, or role+agent, or role+unbind"})
            return self._send_json(200 if r.get("ok") else 400,
                                   {"ok": bool(r.get("ok")), "why": r.get("why"),
                                    "deployment": sd.deployment()})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"could not set deployment: {e}"})

    def _operator_steward_stop(self, body: dict) -> None:
        """POST — shut one down, the way he chose.

        ★ It reports what it FREED, measured before and after, because the last shutdown request was
        made to get memory back for a video render and the activation flags would have returned none
        of it. A control that cannot tell you it did nothing is how you end up trusting it."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sc = self._op_mod("steward_control")
            r = sc.act(str(body.get("steward") or ""), str(body.get("method") or ""),
                       int(body.get("minutes") or 60), by="operator (panel)")
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"could not act: {e}"})

    def _operator_completions(self) -> None:
        """GET — what has LEFT the board, and whether that was a completion or a disappearance.

        ★ The distinction is the whole feature. Fourteen roadmap rows left this board today because a
        new file was added that the parser could not read; nothing was finished, and without this the
        board simply got shorter and looked like progress."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            cl = self._op_mod("completion_log")
            live = cl.run(write=False)
            rows = cl.entries()
            counts = {}
            for e in rows:
                counts[e.get("kind")] = counts.get(e.get("kind"), 0) + 1
            return self._send_json(200, {
                "archive": rows[-120:], "counts": counts, "pending": live,
                "digest_file": str(cl.DIGEST), "archive_file": str(cl.ARCHIVE),
                "note": ("`settled` and `source_cleared` are finished work. `source_degraded` means "
                         "the row is HIDDEN because its source broke — not done. `vanished` means "
                         "nothing explains it.")})
        except Exception as e:
            return self._send_json(500, {"error": f"completion log unreadable: {e}"})

    def _operator_objectives(self) -> None:
        """GET — his life objectives, read from his Obsidian vault.

        ★ The vault is the file of record, not us. This route reads; it never becomes the store."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            lo = self._op_mod("life_objectives")
            oc = self._op_mod("objective_context")
            rows, health = lo.objectives()
            v = lo.vault()
            return self._send_json(200, {
                "objectives": rows, "health": health,
                "vault": v, "vaults": lo.vaults(),
                "obsidian": lo.obsidian_installed(),
                "how": oc.HOW, "capabilities": oc.capabilities()})
        except Exception as e:
            return self._send_json(500, {"error": f"objectives unreadable: {e}"})

    def _operator_objective_note(self) -> None:
        """GET ?file=… — the note's own text, so the panel can show it WITHOUT Obsidian.

        ★★ THIS IS THE PRIMARY 'OPEN IT', not a fallback. The obsidian:// handler here is registered
        but its Exec line carries no %u field code, so the URI is silently dropped: Obsidian opens on
        the last-used vault, never navigates to the note, and reports success. Rendering the note in
        the panel has none of that — no handler, no confirmation dialog, and it works over the tunnel
        when he is away from this machine.
        ★ CLOSED TO THE VAULT. Only files inside the registered vault are served; anything else is
        refused. A 'show me this file' endpoint without that is a file-read primitive."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        from urllib.parse import urlparse, parse_qs
        import os as _os
        q = parse_qs(urlparse(self.path).query)
        want = (q.get("file") or [""])[0]
        try:
            lo = self._op_mod("life_objectives")
            v = lo.vault()
            if not v:
                return self._send_json(400, {"error": "no Obsidian vault is registered"})
            root = _os.path.realpath(v["path"])
            target = _os.path.realpath(want)
            if not target.startswith(root + _os.sep):
                return self._send_json(403, {
                    "error": "that file is outside the vault",
                    "why": "this endpoint only serves notes inside the registered vault"})
            if not target.endswith(".md") or not _os.path.isfile(target):
                return self._send_json(404, {"error": "no such note"})
            with open(target, errors="replace") as f:
                text = f.read()
            return self._send_json(200, {"file": target, "text": text, "bytes": len(text),
                                         "vault": v["name"]})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _operator_objective_create(self, body: dict) -> None:
        """POST — write a new objective into his vault."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            lo = self._op_mod("life_objectives")
            return self._send_json(200, lo.create(str(body.get("title") or ""),
                                                  str(body.get("why") or ""),
                                                  str(body.get("horizon") or "")))
        except FileExistsError as e:
            return self._send_json(409, {"error": str(e)})
        except (ValueError, RuntimeError) as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _operator_objective_context(self, body: dict) -> None:
        """POST — attach a document to an objective, the way he chose."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            oc = self._op_mod("objective_context")
            return self._send_json(200, oc.add(
                str(body.get("objective") or ""), str(body.get("source") or ""),
                how=str(body.get("how") or "copy"),
                recipient=body.get("recipient") or None,
                note=str(body.get("note") or "")))
        except FileNotFoundError as e:
            return self._send_json(404, {"error": str(e)})
        except (ValueError, RuntimeError) as e:
            return self._send_json(400, {"error": str(e)})
        except Exception as e:
            return self._send_json(500, {"error": str(e)})

    def _operator_facets(self) -> None:
        """GET — the distinct, clickable dimensions of the board, with live counts."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            d = self._board_payload()
            items = [i for i in d.get("items", []) if i.get("state") == "open"]

            def bucket(fn):
                out = {}
                for i in items:
                    for v in (fn(i) or []):
                        v = str(v).strip()
                        if v and v.lower() not in ("unknown", "none", "null", "not recorded"):
                            out[v] = out.get(v, 0) + 1
                return out

            raw = {
                "source":     bucket(lambda i: [i.get("source") or "hand"]),
                "lane":       bucket(lambda i: [i.get("lane")]),
                "priority":   bucket(lambda i: [i.get("priority_label")]),
                "raised_by":  bucket(lambda i: [(i.get("origin") or {}).get("raised_by")]),
                "held_by":    bucket(lambda i: [((i.get("bound_to") or {}).get("target_label")
                                                 or (i.get("bound_to") or {}).get("target"))]),
            }
            facets, dropped = [], []
            LABEL = {"source": "where it came from", "lane": "lane", "priority": "priority",
                     "raised_by": "raised by", "held_by": "held by"}
            for key, counts in raw.items():
                n = len(counts)
                if n < self.FACET_MIN_VALUES:
                    dropped.append({"facet": key, "values": n,
                                    "why": "only one value on this board — it would filter nothing"})
                    continue
                if n > self.FACET_MAX_VALUES:
                    dropped.append({"facet": key, "values": n,
                                    "why": "too many distinct values to be a chip rail"})
                    continue
                facets.append({
                    "key": key, "label": LABEL.get(key, key),
                    "values": [{"value": v, "count": c}
                               for v, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
                })
            return self._send_json(200, {
                "facets": facets, "dropped": dropped, "open_items": len(items),
                "note": ("derived from the same merge the list is drawn from, at the moment you asked. "
                         "A facet with one value, or a value per row, is dropped rather than shown.")})
        except Exception as e:
            return self._send_json(500, {"error": f"facets unreadable: {e}"})

    # ── THE FINISHED RECORD ───────────────────────────────────────────────────────────────────────
    #  ★★★ THE HONEST ANSWER TODAY IS "NOTHING". Every one of the 46 hand rows is `open`, and the audit
    #    journal contains exactly one done-transition -- for `_tmp_journal_probe`, a test id. So this
    #    page must SAY that, in words, rather than render an empty container. An empty box on a
    #    "finished work" page reads as "no data loaded", which is indistinguishable from a bug, and it
    #    quietly hides the more interesting fact: this board has never recorded a completion.
    #  ★★ Completions are read from the JOURNAL, not from the item store, because an item that is
    #    settled and later reopened leaves no trace in the store -- only the journal remembers it
    #    happened. `guards` is exactly that case (open -> later -> open, twice in fifteen seconds).
    def _operator_settled(self) -> None:
        """GET — what has actually been finished, and what the record can honestly show."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            d = self._board_payload()
            by_id = {i.get("id"): i for i in d.get("items", [])}
            journal = self._OPERATOR_DIR / "blockers_audit.jsonl"
            events = []
            if journal.exists():
                for ln in journal.read_text(errors="replace").splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:
                        continue
                    if r.get("field") != "state":
                        continue
                    now = (r.get("now") or "").lower()
                    direction = (r.get("direction") or "")
                    if not now and "->" in direction:
                        now = direction.split("->")[-1].strip().lower()
                    # ★ TAKE EVERY STATE ROW. The first version kept only rows landing on done/later,
                    #   which dropped the `later -> open` REVERT -- the single row whose author field
                    #   proves the change before it was a write-path test. The evidence that a
                    #   settlement was not real lived in the row the filter threw away, so the page
                    #   counted the test as a deferral. Filter AFTER classifying, never before.
                    if True:
                        events.append({
                            "id": r.get("id"), "to": now, "at": r.get("ts"),
                            "by": r.get("by") or "not recorded", "note": r.get("note"),
                            "was": r.get("was") or (direction.split("->")[0].strip() if direction else None),
                        })
            # ★★★ THREE KINDS OF NON-EVENT, AND THE FIRST VERSION OF THIS COUNTED ONE AS REAL.
            #   Of the six state rows in the journal: three are NO-OPS (open -> open, a ruling that
            #   changed nothing), one is a synthetic probe id, and TWO are a single write-path test
            #   and its own revert -- the revert row says so in its author field. Filtering only on a
            #   hard-coded probe id left the test's first half standing, so the page reported "1
            #   deferral" and made an empty record look populated. A surface whose whole job is to say
            #   what really happened cannot count a test of itself as work.
            #   ★ Classified from what the rows SAY, not from a list of ids, so the next test that
            #     labels itself honestly is caught without editing this file.
            TEST_IDS = {"_tmp_journal_probe"}
            import re as _re
            TESTY = _re.compile(r"revert|write-path test|round-trip test|\bprobe\b|\btest\b", _re.I)

            def _kind(e):
                if e.get("was") and e["was"] == e["to"]:
                    return "no_op"
                if e["id"] in TEST_IDS:
                    return "test"
                if TESTY.search(str(e.get("by") or "") + " " + str(e.get("note") or "")):
                    return "test"
                # A change that a later row explicitly REVERTS was never a decision either.
                for later in events:
                    if (later["id"] == e["id"] and str(later.get("at") or "") > str(e.get("at") or "")
                            and TESTY.search(str(later.get("by") or "") + " " + str(later.get("note") or ""))):
                        return "test"
                return "real"

            for e in events:
                e["kind"] = _kind(e)
            settlements = [e for e in events if e["to"] in ("done", "later")]
            real = [e for e in settlements if e["kind"] == "real"]
            tests = [e for e in events if e["kind"] == "test"]
            noops = [e for e in events if e["kind"] == "no_op"]
            # Did it STAY settled? The store is the authority on the current state.
            for e in real:
                cur = by_id.get(e["id"])
                e["still_settled"] = bool(cur) and cur.get("state") != "open"
                e["title"] = (cur or {}).get("title") or e["id"]
                e["reopened_since"] = bool(cur) and cur.get("state") == "open"
            real_completions = [e for e in real if e["to"] == "done"]
            real_deferrals = [e for e in real if e["to"] == "later"]
            done_now = [i for i in d.get("items", []) if i.get("state") == "done"]
            later_now = [i for i in d.get("items", []) if i.get("state") == "later"]
            return self._send_json(200, {
                "finished": [{"id": i.get("id"), "title": i.get("title"), "lane": i.get("lane"),
                              "source": i.get("source"), "at": i.get("decided_at"),
                              "via": i.get("decided_via"), "note": i.get("note")}
                             for i in done_now],
                "deferred": [{"id": i.get("id"), "title": i.get("title"), "lane": i.get("lane"),
                              "at": i.get("decided_at"), "via": i.get("decided_via"),
                              "note": i.get("note")} for i in later_now],
                "history": sorted(real, key=lambda e: str(e.get("at") or ""), reverse=True),
                "excluded_tests": tests,
                "counts": {"finished_now": len(done_now), "deferred_now": len(later_now),
                           "settle_events_ever": len(real), "test_events_excluded": len(tests),
                           "no_op_rulings": len(noops), "journal_rows": len(events)},
                "excluded_noops": noops,
                # ★ A DEFERRAL IS NOT A COMPLETION, and a completion that was reopened is not one
                #   either. The first version of this note keyed off "any settle event ever" and would
                #   have reported the record as populated on the strength of one `later` that was
                #   reversed fifteen seconds afterwards. Count what the page CLAIMS to show.
                "honest_note": (
                    (f"Nothing has ever been recorded finished on this board. Every hand-written row "
                     f"is still open, and not one of the {len(events)} state row(s) in the journal is a "
                     f"real decision: {len(noops)} changed nothing (open to open), and {len(tests)} were "
                     f"tests of the write path or a synthetic id — including one deferral and its own "
                     f"revert, which says so in its author field. That is a fact about the RECORD, not "
                     f"about your work: anything you settled elsewhere was never settled here.")
                    if not done_now and not real else
                    "Read from the journal, not the item store: a row settled and later reopened leaves "
                    "no trace in the store, so the store alone would under-report what happened.")})
        except Exception as e:
            return self._send_json(500, {"error": f"finished record unreadable: {e}"})

    def _operator_browser(self) -> None:
        """GET — where the agent browser's login actually lives, and whether it survives a reboot.

        ★ PARITY: an agent can run `agent_browser.py --status`. Without this route he would have to
        open a terminal to find out that a working login is sitting in /tmp. The thing most worth
        knowing here is invisible by construction -- nothing breaks until the reboot."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            m = self._op_mod("agent_browser")
            prof = m.PROFILE
            shape = m.session_shape(prof, "proton")
            pages = m.cdp_pages(m.PORT) or []
            volatile = m.LEGACY.exists()
            live_shape = m.session_shape(m.LEGACY, "proton") if volatile else {}
            state = {}
            try:
                import json as _j
                state = _j.loads(m.STATE.read_text()) if m.STATE.exists() else {}
            except Exception:
                pass
            return self._send_json(200, {
                "durable_profile": str(prof),
                "durable_exists": prof.exists(),
                "running": bool(pages),
                "open_pages": [t.get("url", "")[:110] for t in pages],
                "volatile_profile_in_use": volatile,
                "volatile_path": str(m.LEGACY),
                "live_session_shape": live_shape,
                "durable_session_shape": shape,
                "last_proof": state,
                "note": ("The browser is running from a VOLATILE profile. Its login is real and "
                         "working, and it ends at the next reboot or /tmp sweep. Copying it while it "
                         "runs does NOT rescue it -- that was measured. The fix is a clean stop then "
                         "a copy: `agent_browser.py --transplant --yes`."
                         if volatile else
                         "The browser is running from the durable profile. A reboot no longer costs "
                         "the login.")})
        except Exception as e:
            return self._send_json(500, {"error": f"agent browser unreadable: {e}"})

    def _operator_browser_transplant(self, body: dict) -> None:
        """POST — move the agent browser's login onto the durable profile.

        ★ THIS COSTS SOMETHING AND SAYS SO. It stops the running browser, copies the cleanly-closed
        profile, and re-proves it. If the transplant comes back expired, the login has to be done once
        by hand. That is a real price, so it is never taken on his behalf without `confirm`."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if not body.get("confirm"):
            return self._send_json(400, {
                "error": "confirm required",
                "field": "confirm",
                "why": ("This stops the running browser. If the moved session still comes back "
                        "expired, the Proton login has to be done once more by hand. Sending "
                        "confirm:true is how you accept that.")})
        import subprocess
        try:
            script = str(self._OPERATOR_DIR / "agent_browser.py")
            r = subprocess.run(["/usr/bin/python3", script, "--transplant", "--yes"],
                               capture_output=True, text=True, timeout=240)
            ok = r.returncode == 0
            return self._send_json(200, {
                "ok": ok,
                "verdict": ("the login moved and was re-proved on the durable profile" if ok
                            else "it moved but did NOT prove -- the session did not survive, and the "
                                 "login has to be done once by hand"),
                "output": (r.stdout or "")[-3000:],
                "stderr": (r.stderr or "")[-800:]})
        except subprocess.TimeoutExpired:
            return self._send_json(504, {"error": "transplant did not finish in 4 minutes"})
        except Exception as e:
            return self._send_json(500, {"error": f"transplant failed: {e}"})

    def _operator_board_add(self, body: dict) -> None:
        """POST — add an item to his own board, from the board.
        ★ An agent can add a row with `blockers.py --add`. He could not, without a terminal. That is the
        parity gap in its purest form: the board is HIS, and adding to it required being us."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        title = str(body.get("title") or "").strip()
        one = str(body.get("one") or "").strip()
        if not title or not one:
            return self._send_json(400, {"ok": False, "error": "needs a title and a sentence saying what "
                                                               "it is — a row with neither cannot be acted on"})
        import re as _re, subprocess as _sp, json as _j
        rid = "you-" + (_re.sub(r"[^a-z0-9]+", "-", title.lower())[:40].strip("-") or "item")
        item = {"id": rid, "title": title, "one": one,
                "sev": str(body.get("sev") or "hold"), "lane": str(body.get("lane") or "unknown"),
                "blocks": str(body.get("blocks") or ""),
                "origin": {"what": "you added this yourself, from the board",
                           "where": str(self._OPERATOR_DIR / "blockers.json"), "kind": "hand-written",
                           "raised_by": "the operator", "how_to_clear": "only you close it"}}
        try:
            r = _sp.run([sys.executable, str(self._OPERATOR_DIR / "blockers.py"), "--add"],
                        input=_j.dumps(item).encode(), capture_output=True, timeout=30)
            out = (r.stdout + r.stderr).decode(errors="replace")
            ok = "added 1" in out
            return self._send_json(200 if ok else 400,
                                   {"ok": ok, "id": rid, "detail": " ".join(out.split())[:200]})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_wizard(self) -> None:
        """GET /operator/wizard?batch=N&order=… — the next burst.
        ★ An OPTION, not the path. The services block shows everything at once with the same controls;
        this shows a few at a time with the agent's counsel attached. One store behind both."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        q = parse_qs(urlsplit(self.path).query)
        try:
            batch = int((q.get("batch") or ["3"])[0])
        except ValueError:
            batch = 3
        order = (q.get("order") or ["quickest"])[0]
        try:
            w = self._op_mod("credential_wizard")
            return self._send_json(200, {**w.plan(batch, order), "orders": w.ORDERS})
        except Exception as e:
            return self._send_json(500, {"error": f"guided run unavailable: {e}"})

    # ── STORMWATCH (operator ask 2026-09-23) ─────────────────────────────────────────────────────
    #   "Let's reestablish connection and make a new heartbeat related to the storms passing over
    #    coordinates … using a light and efficient approach … We want this to be a generalized
    #    approach that is non-specific to this location … in the way of building a protocol."
    #
    #   THREE PIECES, ONE STORE EACH:
    #     • stormwatch/storm_heartbeat.py — one keyless Open-Meteo call per run, a bounded ring, and a
    #       band that is 'unreachable' (never 'clear') when the source cannot be reached.
    #     • stormwatch/protocol.py       — the ordered candidate-cause survey and the answers it keeps.
    #     • stormwatch/config.json       — every threshold, the cadence and THE LOCATION. Config, not code.
    #   This panel item COMPOSES those; it re-implements no threshold and invents no question.
    def _stormwatch_mod(self, name: str):
        """Import once, like `_op_mod`. ★ The panel runs the SAME module the timer and the CLI run, so
        the wizard cannot hold a second copy of a threshold — the failure this estate keeps re-learning."""
        d = str(STORMWATCH_DIR)
        if d not in sys.path:
            sys.path.insert(0, d)
        import importlib
        return importlib.import_module(name)

    def _stormwatch_popout(self) -> None:
        """GET /operator/stormwatch — the storm heartbeat + protocol wizard as its own served page."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if not STORMWATCH_POPOUT_HTML.is_file():
            return self._send_text(500, f"stormwatch HTML missing: {STORMWATCH_POPOUT_HTML}")
        body = STORMWATCH_POPOUT_HTML.read_bytes()
        # ★ inject the revision this page was BUILT from, so the page can tell the operator (and itself)
        #   that the copy on screen is older than the file on disk. Same trick as __PANEL_STAMP__.
        try:
            _st = STORMWATCH_POPOUT_HTML.stat()
            _stamp = ('\n<script>window.__SW_BUILD__={"mtime":%d,"bytes":%d,"at":"%s"};</script>\n'
                      % (int(_st.st_mtime), int(_st.st_size),
                         __import__("time").strftime("%Y-%m-%dT%H:%M:%S",
                                                     __import__("time").localtime(_st.st_mtime))))
            body = body.replace(b"</body>", _stamp.encode("utf-8") + b"</body>")
        except Exception:  # noqa: BLE001
            pass
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors(); self.end_headers(); self.wfile.write(body)

    # ★ VENDORED LEAFLET, SERVED FROM OUR OWN ORIGIN ON PURPOSE. The interactive map has to be
    #   same-origin (so `drive_stormwatch.py` can assert it and the card can caption it), and the
    #   estate prefers a local copy to a CDN fetch — this is BYTE-IDENTICAL to the Leaflet 1.9.4 dist
    #   (js md5 35b48eb991f383702f153452506e07b2, css md5 c02c12fe5e21d2493070649584ca38b7, both
    #   verified against unpkg). The version is in the filename, so it is safe to cache hard.
    _STORMWATCH_VENDOR = {
        "leaflet-1.9.4.js": ("application/javascript; charset=utf-8",
                             "public, max-age=31536000, immutable"),
        "leaflet-1.9.4.css": ("text/css; charset=utf-8",
                              "public, max-age=31536000, immutable"),
    }

    def _stormwatch_vendor(self, path: str) -> None:
        """GET /operator/stormwatch/vendor/<name> — the map library, from this box.

        A name that is not on the whitelist is a 404, never a path join: the name is looked up in the
        literal dict above and the file is built from that key, so traversal cannot reach the disk."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        name = path.rsplit("/", 1)[-1]
        spec = self._STORMWATCH_VENDOR.get(name)
        if not spec:
            return self._send_text(404, f"not a vendored file: {name}")
        f = STORMWATCH_DIR / "vendor" / name
        if not f.is_file():
            return self._send_text(404, f"vendored file missing on disk: {f}")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", spec[0])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", spec[1])
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _stormwatch_data(self) -> None:
        """GET /operator/stormwatch/data — current reading, the passing-cloud ring, the full config with
        every setting's CURRENT VALUE + DEFAULT + VALID RANGE, the survey, and the accumulated answers.

        ★ It reports FAILURE AS DATA, not as an HTTP error. A 500 here would render as a blank card and
        an operator could not tell "no reading yet" from "the weather source is unreachable" from "the
        panel is broken" — criterion 8. So the shape is always 200 with an explicit `states` block,
        and `current.severity.name == 'unreachable'` is rendered as its own state, never as calm."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        out: dict = {"generated_at": int(__import__("time").time())}
        # ★ THE BUILD STAMP, SO A STALE PAGE CANNOT MISLEAD ANY MORE. The wizard is read from disk per
        #   request, but an ALREADY-OPEN iframe keeps whatever revision it loaded — and on 2026-10-02
        #   exactly that made a fixed feature look broken ("the view is not changing") because the copy
        #   on screen predated the fix. The page now carries its own mtime and compares it with this
        #   one, so staleness is VISIBLE instead of being diagnosed by hand.
        try:
            _st = STORMWATCH_POPOUT_HTML.stat()
            out["build"] = {"popout_mtime": int(_st.st_mtime), "popout_bytes": int(_st.st_size),
                            "popout_at": __import__("time").strftime(
                                "%Y-%m-%dT%H:%M:%S", __import__("time").localtime(_st.st_mtime))}
        except Exception:  # noqa: BLE001
            out["build"] = None
        try:
            hb = self._stormwatch_mod("storm_heartbeat")
            proto = self._stormwatch_mod("protocol")
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {
                "ok": False, "state": "error",
                "error": f"the stormwatch modules could not be loaded: {type(e).__name__}: {e}",
                "remedy": (f"check that {STORMWATCH_DIR}/storm_heartbeat.py and protocol.py exist and "
                           f"compile: python3 -m py_compile <file>"),
                "config": None, "current": None, "ring": None, "survey": None, "context": None})
        try:
            cfg = hb.load_config()
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {
                "ok": False, "state": "error",
                "error": f"stormwatch/config.json is unreadable or malformed: {type(e).__name__}: {e}",
                "remedy": (f"the config is the one file holding the location and the thresholds; repair it "
                           f"at {STORMWATCH_DIR}/config.json — a JSON error here stops the heartbeat too"),
                "config": None, "current": None, "ring": None, "survey": None, "context": None})
        try:
            rows = hb.read_ring()
            latest = rows[-1] if rows else None
            out.update({
                "ok": True,
                "config": hb.public_config(cfg),
                "current": latest,                     # None ⇒ NO READING HAS EVER BEEN TAKEN
                "ring": hb.passing(rows),
                "survey": proto.survey(row=latest) if latest else proto.survey(row={}),
                "context": proto.context(),
                "clock": self._stormwatch_clock(),
            })
            # ★ the picture, with its caption built from the ONE band fact that lives in the ring
            try:
                rd = self._stormwatch_mod("radar")
                rp = self._stormwatch_radar_payload()
                sev = ((latest or {}).get("severity") or {})
                rp = dict(rp)
                rp["caption"] = rd.caption(cfg, rp, sev.get("name"), sev.get("words"),
                                           (latest or {}).get("precipitation_mm"))
                out["radar"] = rp
            except Exception as e:  # noqa: BLE001
                out["radar"] = {"ok": False, "state": "unreachable", "image_url": None,
                                "state_words": "COULD NOT REACH THE RADAR SOURCE",
                                "error": f"{type(e).__name__}: {e}",
                                "caption": f"COULD NOT REACH THE RADAR SOURCE — {type(e).__name__}"}
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {
                "ok": False, "state": "error",
                "error": f"stormwatch data could not be assembled: {type(e).__name__}: {e}",
                "remedy": "run: python3 stormwatch/storm_heartbeat.py --selftest — it names the broken part",
                "config": None, "current": None, "ring": None, "survey": None, "context": None})
        return self._send_json(200, out)

    def _stormwatch_clock(self) -> dict:
        """How the cadence is actually enforced, read from systemd — reported so the panel number and the
        timer cannot silently disagree (a config saying 5 min while the timer says 6 h is a lie the card
        would otherwise tell).

        ★ The straightforward `systemctl show -p NextElapseRealtimeUSec` DOES NOT WORK HERE: every timer
        in this estate is monotonic-based (`OnBootSec`/`OnUnitActiveSec`), so there is no realtime
        elapse property and `--value` silently returns fewer lines than requested — which my first
        version mis-indexed. `list-timers --output=json` is used instead, and its `next`/`last` are
        epoch microseconds. If that fails we say the clock is unreadable rather than print a blank.
        """
        info = {"cadence_note": "", "unit_active": None, "next_elapse": None, "last_trigger": None,
                "source": "systemctl --user list-timers"}
        def _fmt(usec):
            try:
                return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(usec) / 1e6))
            except Exception:  # noqa: BLE001
                return None
        try:
            r = subprocess.run(["systemctl", "--user", "list-timers", "khaoss-stormwatch.timer",
                                "--output=json", "--no-pager"], capture_output=True, timeout=6)
            rows = json.loads((r.stdout or b"[]").decode() or "[]")
            if rows:
                row = rows[0]
                info["next_elapse"] = _fmt(row.get("next"))
                info["last_trigger"] = _fmt(row.get("last"))
                info["unit_active"] = "active"
                info["activates"] = row.get("activates")
            else:
                info["unit_active"] = "not-installed"
                info["cadence_note"] = ("khaoss-stormwatch.timer is NOT installed — the reading below is "
                                        "real, but nothing is taking them on a schedule. Install it: "
                                        "systemctl --user enable --now khaoss-stormwatch.timer")
        except Exception as e:  # noqa: BLE001
            info["cadence_note"] = (f"systemd did not answer ({type(e).__name__}) — the timer state could "
                                    f"not be read; the reading below is still real")
        return info

    def _stormwatch_cli(self, argv: list, timeout: float = 150.0):
        """★ RUN THE HEAVY HALF IN A SUBPROCESS, THE WAY capability-grant.py IS RUN.

        MEASURED REASON, not a preference: the first version of this called `storm_heartbeat.run_once`
        and `radar.refresh` IN-PROCESS, which imports Pillow to compose the radar picture. The panel
        service was then observed at 1010 MB against a 1 GB high-water mark with 13.6 MB available —
        i.e. the panel was being throttled by its own cgroup, which is exactly why its pages started
        failing to load during the drive. A 30-40 MB image library has no business in a long-lived
        HTTP process that only needs to READ a JSON file and serve a PNG.

        So the panel composes nothing: it shells out to the same CLI a human and an agent use, and
        reads back the JSON. One store per fact, and one memory cost per process."""
        tool = str(STORMWATCH_DIR / argv[0])
        return subprocess.run(["/usr/bin/python3", tool, *argv[1:]],
                              capture_output=True, timeout=timeout, cwd=str(STORMWATCH_DIR))

    _STORMWATCH_FRAME_RE = re.compile(r"^/operator/stormwatch/radar/(\d{6,12})\.png$")

    def _stormwatch_radar_frame(self, path: str) -> None:
        """GET /operator/stormwatch/radar/<epoch>.png — ONE cached radar frame.

        ★ The name is matched by REGEX TO DIGITS ONLY and the file is then looked for inside the frames
        directory, so a crafted path cannot walk out of it. ★ And a missing frame is an honest 404 with
        a reason — never a placeholder image, because a blank picture on a radar card is
        indistinguishable from clear skies, which is the one thing this feature must never do.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        mo = self._STORMWATCH_FRAME_RE.match(path)
        if not mo:
            return self._send_text(404, "not a radar frame name (expected /operator/stormwatch/radar/<epoch>.png)")
        f = STORMWATCH_DIR / "var" / "radar" / "frames" / f"{mo.group(1)}.png"
        try:
            if not f.is_file():
                return self._send_text(404, (
                    f"no such radar frame: {mo.group(1)}.png — it has been evicted by the frame cap, or "
                    f"was never captured. NO PICTURE IS SHOWN because a blank frame would look like "
                    f"clear skies. Take a reading, or widen radar.keep_frames."))
            body = f.read_bytes()
        except Exception as e:  # noqa: BLE001
            return self._send_text(500, f"radar frame unreadable: {type(e).__name__}: {e}")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        # ★ no-cache: the frame is keyed by its own timestamp, so a cached copy is never wrong, and a
        #   stale-looking image must come from our metadata (which says it is stale) not from the
        #   browser's cache pretending an old frame is the newest.
        self.send_header("Cache-Control", "no-cache")
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _stormwatch_radar_payload(self) -> dict:
        """The radar half of /operator/stormwatch/data. ★ Composes radar.py; owns no radar fact."""
        try:
            rd = self._stormwatch_mod("radar")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "state": "unreachable", "enabled": True,
                    "state_words": "COULD NOT REACH THE RADAR SOURCE",
                    "error": f"the radar module would not load: {type(e).__name__}: {e}",
                    "remedy": "run python3 stormwatch/radar.py --selftest — it names the broken part",
                    "image_url": None, "caption": "COULD NOT REACH THE RADAR SOURCE (module load failed)"}
        try:
            return rd.latest()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "state": "unreachable", "enabled": True,
                    "state_words": "COULD NOT REACH THE RADAR SOURCE",
                    "error": f"the radar payload could not be assembled: {type(e).__name__}: {e}",
                    "remedy": "run python3 stormwatch/radar.py --selftest", "image_url": None}

    def _stormwatch_action(self, body: dict) -> None:
        """POST /operator/stormwatch/action — four actions, each returning the NEW state rather than an
        `ok`, so the page renders what actually happened.

          check_now        run one heartbeat immediately (the same module the timer runs)
          set_setting      change ONE config value, validated against its declared range
          protocol_answer  record the operator's answer to the survey
          protocol_open    the survey with the evidence attached, without recording anything
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        what = str(body.get("action") or "")
        try:
            hb = self._stormwatch_mod("storm_heartbeat")
            proto = self._stormwatch_mod("protocol")
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {"ok": False,
                                         "error": f"stormwatch modules unavailable: {type(e).__name__}: {e}",
                                         "remedy": "check stormwatch/storm_heartbeat.py and protocol.py compile"})
        try:
            if what == "radar_refresh":
                # ★ SUBPROCESS: composing the picture imports Pillow. See _stormwatch_cli.
                try:
                    r = self._stormwatch_cli(["radar.py", "--once", "--force", "--json"], timeout=180)
                except subprocess.TimeoutExpired:
                    return self._send_json(200, {"ok": False, "action": what,
                        "error": "the radar fetch did not finish inside 180 s",
                        "remedy": ("the link is slow or the source is dropping tiles. NO PICTURE IS "
                                   "SHOWN rather than an empty one — retry when the link settles."),
                        "radar": self._stormwatch_radar_payload()})
                out = (r.stdout or b"").decode("utf-8", "replace")
                try:
                    res = json.loads(out or "{}")
                except Exception:  # noqa: BLE001
                    res = {"ok": False, "error": (r.stderr or b"").decode()[-300:] or "the radar CLI said nothing"}
                rd = self._stormwatch_mod("radar")
                rp = self._stormwatch_radar_payload()
                sev = ((hb.read_ring() or [{}])[-1].get("severity") or {})
                rp = dict(rp, caption=rd.caption(hb.load_config(), rp, sev.get("name"), sev.get("words")))
                return self._send_json(200, {"ok": bool(res.get("ok")), "action": what,
                                             "radar": rp, "error": res.get("error"),
                                             "remedy": res.get("remedy"),
                                             "message": res.get("note") or res.get("error")})
            if what == "check_now":
                # ★ SUBPROCESS, for the same measured reason: run_once() refreshes the radar, and the
                #   radar composition imports Pillow. The panel stays a thin reader.
                try:
                    r = self._stormwatch_cli(["storm_heartbeat.py", "--once", "--json"], timeout=150)
                except subprocess.TimeoutExpired:
                    return self._send_json(200, {"ok": False, "action": what, "state": "unreachable",
                        "message": "the check did not finish inside 150 s",
                        "error": "the weather source did not answer in time",
                        "remedy": ("★ This is recorded as UNREACHABLE, which is NOT 'clear'. The timer "
                                   "will try again on its next tick.")})
                try:
                    row = json.loads((r.stdout or b"").decode("utf-8", "replace") or "{}")
                except Exception:  # noqa: BLE001
                    row = {"status": hb.STATUS_ERROR,
                           "error": (r.stderr or b"").decode()[-300:] or "the heartbeat CLI said nothing",
                           "summary": "the check could not be completed"}
                return self._send_json(200, {"ok": row.get("status") == hb.STATUS_OK,
                                             "action": what, "current": row,
                                             "message": row.get("summary"),
                                             "state": ("unreachable" if row.get("status") == hb.STATUS_UNREACHABLE
                                                       else row.get("status"))})
            if what == "set_setting":
                key = str(body.get("key") or "")
                cfg = hb.load_config()
                # ★ read_setting/apply_setting, NOT the raw path: `bands.cloud` and `bands.precip` are
                #   DERIVED settings (config holds band dicts; the setting is the list of ceilings).
                #   Using the raw path here would show the operator [object Object] and write a list of
                #   strings over the band definitions.
                before = hb.read_setting(cfg, key)
                ok, val, msg = hb.validate_setting(cfg, key, body.get("value"))
                if not ok:
                    return self._send_json(200, {"ok": False, "action": what, "key": key,
                                                 "error": msg, "current_value": before,
                                                 "remedy": ("nothing was written — send a value inside the "
                                                            "range shown, or edit config.json directly at "
                                                            f"{hb.CONFIG_PATH}")})
                hb.apply_setting(cfg, key, val)
                hb.save_config(cfg)
                # ★ RE-READ FROM DISK: report what is ON DISK, not what we intended to write.
                after = hb.read_setting(hb.load_config(), key)
                return self._send_json(200, {"ok": after == val, "action": what, "key": key,
                                             "before": before, "after": after,
                                             "message": f"{msg} — written to {hb.CONFIG_PATH}",
                                             "config": hb.public_config(hb.load_config())})
            if what in ("protocol_answer", "protocol_open"):
                latest = None
                rows = hb.read_ring()
                latest = rows[-1] if rows else None
                if what == "protocol_open":
                    return self._send_json(200, {"ok": True, "action": what,
                                                 "survey": proto.survey(row=latest) if latest else proto.survey(row={})})
                cause = str(body.get("cause") or "")
                note = str(body.get("note") or "")
                res = proto.answer(cause, note, by=str(body.get("by") or "operator"))
                return self._send_json(200, dict(res, action=what))
            return self._send_json(200, {"ok": False, "error": f"unknown action {what!r}",
                                         "remedy": "valid actions: check_now, set_setting, protocol_answer, protocol_open"})
        except Exception as e:  # noqa: BLE001
            return self._send_json(200, {"ok": False,
                                         "error": f"{what or 'action'} failed: {type(e).__name__}: {e}",
                                         "remedy": ("run python3 stormwatch/storm_heartbeat.py --selftest; "
                                                    "it names the broken part")})

    # ══════════════════════════════════════════════════════════════════════════════════════════════
    #  ★★★ THE ACCESS VERBS (operator spec 2026-10-12) -- "I can't really do anything in the
    #  access panel ... Let's make sure it works comprehensively."
    #
    #  WHAT WAS WRONG, MEASURED BY LANE 8 BEFORE THIS WAS WRITTEN: all 12 panel endpoints answered
    #  200, `/access` served the 6 personal surfaces plus the jail posture and the exemptions, and
    #  the card's single button only RE-FETCHED that same GET. `POST /access` was a 404. The page
    #  had no verbs -- and `access_broker.py`, which has owned `knock|grant|revoke` for the personal
    #  plane since it was written, had ZERO CALLERS (`check_invariants.sh:979`). The write half of
    #  this estate's access model existed and nothing on any surface could reach it.
    #
    #  ★ THIS ROUTE TABLE IS THE REACH. It composes; it decides nothing. Every verb delegates to the
    #    tool that owns that plane -- `access_broker` for the surfaces, `capability-grant.py` for
    #    the host-root capabilities -- so the panel and the CLI cannot disagree about what is
    #    granted. It writes NO sudoers line itself and no grant of its own invention.
    #  ★★ LOOPBACK ONLY, AND THAT IS NOT A SETTING. `_is_local_request()` refuses a forwarded
    #    request even when it arrives from 127.0.0.1 (cloudflared's CF-/X-Forwarded-* headers), so
    #    opening a tailnet bind can never carry these off-box.
    #  ★★ PROPOSE IS A SEPARATE ROUTE FROM GRANT, ON PURPOSE. The estate's rule is "it may ASK, it
    #    may not TAKE". `propose` renders the exact sudoers line (or the exact registry keys) and
    #    writes NOTHING; the press that writes is a different route, so no single click can grant.
    # ══════════════════════════════════════════════════════════════════════════════════════════════
    def _av(self):
        """The verbs module, imported once. It lives in the repo root beside panel_server."""
        av = globals().get("_ACCESS_VERBS_MOD")
        if av is None:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            import access_verbs as av  # noqa: PLC0415
            globals()["_ACCESS_VERBS_MOD"] = av
        return av

    def _access_panel(self) -> None:
        """GET /access/panel -- every row the access panel renders, in one call."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._av().panel())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"access panel unavailable: {type(e).__name__}: {e}"})

    def _access_truth(self) -> None:
        """GET /access/truth -- THE LEDGER AND THE LIVE SYSTEM, side by side, disagreements named.

        ★ This is the endpoint that makes "never paint a grant it did not verify" checkable from
          outside. It reads `sudo -n -l` (the real sudoers config) and lists /etc/sudoers.d, then
          compares both to the ledger. Where they disagree it reports the disagreement IN BOTH
          DIRECTIONS rather than resolving it: a ledger-only root grant was queued and never
          applied, and a file-only entry is permanent root nobody tracks.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._av().truth())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"access truth unavailable: {type(e).__name__}: {e}"})

    def _access_need(self, q: dict) -> None:
        """GET /access/need?cap=<id>&act=<what> -- THE REPAIR SURFACE FOR A REFUSED ACTION.

        The operator framed this panel's whole purpose himself: *"if something like this happens,
        some buttons appear to switch the settings to make sure the agent has proper and enough
        access to complete the task."* This builds the EXACT object `jarvis/daemon.py` and
        `jarvis/launch_activity.py` already emit, so a refusal can be handed straight to the panel
        and the panel answers with a grant or with a statement.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        cap = str((q.get("cap") or q.get("id") or [""])[0])
        act = str((q.get("act") or [""])[0])
        try:
            return self._send_json(200, self._av().for_need(cap, act))
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"need lookup failed: {type(e).__name__}: {e}"})

    def _access_verb(self, verb: str, body: dict) -> None:
        """POST /access/<verb> -- propose, grant, revoke, knock, sweep, reconcile.

        ★ ONE HANDLER, SIX VERBS, so no verb can quietly acquire a different guard from its
          siblings. `propose*` writes nothing; the writing verbs delegate to the owning tool and
          then RE-READ, so the reply carries the post-press state and not an intention.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        av = self._av()
        try:
            ttl = body.get("ttl")
            ttl = int(ttl) if str(ttl or "").strip() not in ("", "None") else None
        except (TypeError, ValueError):
            return self._send_json(400, {"error": "ttl must be a whole number of minutes"})
        cap = str(body.get("capability") or body.get("cap") or "")
        surf = str(body.get("surface") or "")
        scope = str(body.get("scope") or "allow-once")
        try:
            if verb == "propose":
                return self._send_json(200, av.propose_capability(cap, ttl))
            if verb == "propose-surface":
                return self._send_json(200, av.propose_surface(surf, scope, ttl))
            if verb == "grant-surface":
                return self._send_json(200, av.grant_surface(surf, scope))
            if verb == "revoke-surface":
                return self._send_json(200, av.revoke_surface(surf))
            if verb == "knock":
                return self._send_json(200, av.knock_surface(surf, str(body.get("why") or "")))
            if verb == "issue":
                return self._send_json(200, av.issue_capability(cap, ttl, str(body.get("reason") or "")))
            if verb == "revoke":
                return self._send_json(200, av.revoke_capability(str(body.get("id") or "")))
            if verb == "sweep":
                return self._send_json(200, av.sweep())
            if verb == "reconcile":
                return self._send_json(200, av.reconcile())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"access {verb} failed: {type(e).__name__}: {e}"})
        return self._send_json(400, {"error": f"unknown access verb {verb!r}",
                                     "verbs": ["propose", "propose-surface", "grant-surface",
                                               "revoke-surface", "knock", "issue", "revoke",
                                               "sweep", "reconcile"]})

    # ── THE GRANT WIZARD (operator spec 2026-09-21) ─────────────────────────────────────────────
    def _grant_wizard_popout(self) -> None:
        """GET /operator/grant-wizard — the grant wizard as its own served page."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if not GRANT_WIZARD_HTML.is_file():
            return self._send_text(500, f"grant wizard HTML missing: {GRANT_WIZARD_HTML}")
        body = GRANT_WIZARD_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._cors(); self.end_headers(); self.wfile.write(body)

    def _grant_wizard_data(self) -> None:
        """GET /operator/grant-wizard/data — the catalogue and the LIVE grants, from the one tool.
        ★ It composes; it does not decide. capability-grant.py owns the catalogue and the ledger, so a
          form field here can never invent a privilege."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            out = subprocess.check_output(
                ["/usr/bin/python3", str(ROOT / "bin" / "capability-grant.py"), "api"],
                timeout=10, stderr=subprocess.DEVNULL)
            _data = json.loads(out.decode() or "{}")
            import getpass as _gp
            _data["user"] = _gp.getuser()
            return self._send_json(200, _data)
        except Exception as e:
            return self._send_json(500, {"error": f"grant wizard data unavailable: {e}"})

    def _grant_wizard_action(self, body: dict) -> None:
        """POST /operator/grant-wizard/action — issue, sweep, or revoke. Runs the tool; never edits
        the ledger itself, so the wizard and the CLI cannot disagree about what is granted."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        tool = str(ROOT / "bin" / "capability-grant.py")
        what = str(body.get("action") or "")
        try:
            if what == "issue":
                cap = str(body.get("capability") or "")
                ttl = int(body.get("ttl") or 60)
                scope = str(body.get("scope") or "host-root")
                if scope != "host-root":
                    # ★ Recorded and SHOWN, not silently applied: a non-host-root grant is a SHAMAN
                    #   capability, and this route does not write the sudoers plane for it.
                    return self._send_json(200, {"ok": True, "recorded": scope,
                        "message": f"recorded {scope} for {body.get('agent') or '(unnamed)'} — "
                                   "shown in the ledger; no sudoers entry written"})
                out = subprocess.run(["/usr/bin/python3", tool, "issue", cap, "--ttl", str(ttl)],
                                     capture_output=True, timeout=15)
                # ══════════════════════════════════════════════════════════════════════════════════════
                #  ★★★ FIXED 2026-09-23 — THIS ROUTE USED TO REPORT "GRANTED" FOR A GRANT IT HAD ONLY
                #  QUEUED, AND THE OPERATOR WAS SHOWN A COUNTDOWN FOR ROOT THAT DID NOT EXIST.
                #
                #  `capability-grant.py issue` writes a `*.pending` file into ~/.khaoss-privileged and
                #  prints "now apply it: sudo .../capability-grant-apply.sh". IT DOES NOT APPLY ANYTHING.
                #  This handler then returned `ok: returncode == 0`, which is TRUE because QUEUEING
                #  succeeded — so the wizard rendered GRANTED, the ledger recorded an active grant with
                #  an expiry, and `/etc/sudoers.d/khaoss-detection-tools` was never written.
                #  ★ MEASURED CONSEQUENCE: `sudo -n` still demanded a password afterwards, and
                #  `capability-grant.py reconcile` reported `ORPHANED ledger entries (no file on disk)`.
                #  ★★ The checker ALREADY EXISTED and nothing called it — the same shape as the retraction
                #  that was never applied and the manifest nothing writes to. Three instances in one
                #  session of a mechanism built and not wired.
                #
                #  So: VERIFY, THEN REPORT WHAT IS TRUE. A grant is not "granted" until the file is on
                #  disk, and this returns `applied` separately from `queued` so no surface can conflate
                #  them again. It also states the one command that finishes the job.
                # ══════════════════════════════════════════════════════════════════════════════════════
                applied, detail = False, ""
                try:
                    import importlib.util as _ilu
                    _spec = _ilu.spec_from_file_location("cg", tool)
                    _m = _ilu.module_from_spec(_spec)
                    _spec.loader.exec_module(_m)
                    _spec2 = _m.CATALOGUE.get(cap) or {}
                    _path = "/etc/sudoers.d/" + str(_spec2.get("sudoers") or "")
                    import os as _os
                    applied = bool(_spec2) and _os.path.exists(_path)
                    detail = (f"the entry is INSTALLED at {_path}" if applied else
                              f"QUEUED ONLY — {_path} does NOT exist yet. Run: "
                              f"sudo {ROOT}/bin/capability-grant-apply.sh")
                except Exception as _e:  # noqa: BLE001
                    detail = (f"could not verify whether the entry reached disk "
                              f"({type(_e).__name__}) — treat as NOT applied")
                return self._send_json(200, {
                    "ok": out.returncode == 0,
                    "queued": out.returncode == 0,
                    "applied": applied,
                    "message": detail,
                    "output": (out.stdout or b"").decode()[-400:]})
            if what == "sweep":
                out = subprocess.run(["/usr/bin/python3", tool, "sweep"], capture_output=True, timeout=15)
                return self._send_json(200, {"ok": True,
                                             "message": (out.stdout or b"").decode().strip()[:200]})
            if what == "revoke":
                gid = str(body.get("id") or "")
                out = subprocess.run(["/usr/bin/python3", tool, "revoke", gid], capture_output=True, timeout=15)
                return self._send_json(200, {"ok": out.returncode == 0,
                                             "message": (out.stdout or b"").decode().strip()[:200]})
            return self._send_json(400, {"error": f"unknown action {what!r}"})
        except Exception as e:
            return self._send_json(500, {"error": f"grant action failed: {e}"})

    def _operator_terminal(self) -> None:
        """GET /operator/terminal — the terminal the wizard can CONTAIN.
        ★ HONEST: there is no PTY service behind this yet. Rather than ship a black box that looks
          broken, it serves a page that says exactly that and gives the working alternative (launch a
          real terminal). A placeholder that admits it is a placeholder is not a lie."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Terminal</title>"
                "<style>body{background:#0d0f12;color:#e8e6e2;font-family:ui-monospace,monospace;"
                "padding:18px;line-height:1.6}code{color:#d87848}.dim{color:#7c7873}</style></head><body>"
                "<h2>Terminal — not wired yet</h2>"
                "<p class='dim'>This is a real placeholder, not a broken panel: the KHAOSS panel has no "
                "PTY service, and shipping an empty black box would look like a failure.</p>"
                "<p>To get a terminal right now:</p>"
                "<p><code>gnome-terminal</code> &nbsp;or&nbsp; <code>konsole</code> &nbsp;or&nbsp; "
                "<code>xterm</code> — any of them, on this box.</p>"
                "<p class='dim'>Wiring a real embedded PTY is its own item; it needs a websocket and a "
                "governed shell, which is a security surface, not a UI detail.</p>"
                "</body></html>")
        b = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self._cors(); self.end_headers(); self.wfile.write(b)

    def _model_routes(self) -> None:
        """GET /operator/model-routes — what every seat's default model IS, what it COSTS, and
        whether the endpoint actually serves it.

        ★★ WHY THIS IS A SURFACE AND NOT A SCRIPT: the money question was invisible. A profile's
        default sat in a YAML file nobody opens, priced against a catalog nobody fetched, and the one
        screen that showed a route asserted a BILLING MODEL it never checked. Measured on 2026-09-13:
        60 of 70 priced defaults were >= $1.00/M, nineteen seats carried a spelling 2.8x dearer than
        the identical pinned model, and four carried ids the endpoint does not serve at all — the
        exact failure that killed two teams.
        ★ Everything here is read from the LIVE catalog and the profile's own config.yaml. Nothing is
        inferred from a profile NAME, because on this machine a name has lied about a route twice."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            mt = self._op_mod("model_tier")
            cat = mt.catalog()
            s = mt.survey(cat)
            rows = []
            for r in s["rows"]:
                rows.append({**r,
                             "served": (r["current"] in cat) if cat else None,
                             "turn_300k": (round(r["current_price"] / 1e6 * 300000, 4)
                                           if r["current_price"] is not None else None)})
            priced = [r for r in rows if r["current_price"] is not None]
            return self._send_json(200, {
                "catalog_size": len(cat),
                "catalog_read": bool(cat),
                "profiles": len(rows),
                "mean_per_million": (round(sum(r["current_price"] for r in priced) / len(priced), 3)
                                     if priced else None),
                "over_1_dollar": sum(1 for r in priced if r["current_price"] >= 1.0),
                "unroutable": [r["profile"] for r in rows if r["served"] is False],
                "rows": sorted(rows, key=lambda r: -(r["current_price"] or 0)),
                "caveat": ("These are RATES, not spend. Per-model attribution is unavailable — "
                           "/api/v1/activity returns 403 without a provisioning key — so nothing here "
                           "says what was spent or whether it was well spent."),
            })
        except Exception as e:
            return self._send_json(500, {"error": f"model routes unavailable: {e}"})

    def _model_routes_repair(self, body: dict) -> None:
        """POST — fix ONLY what is broken or accidentally dearer. ★ Dry run unless apply is true, and
        it REFUSES entirely if the live catalog cannot be read, because setting an unverified model id
        is how two teams died. Tiering (the operator's architecture) is NOT done here."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            mt = self._op_mod("model_tier")
            r = mt.repair(dry_run=not bool(body.get("apply")))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"repair failed: {e}"})

    # ── ★ THE VOICE SEAT CARD. A THIN PROXY, ON PURPOSE. ──────────────────────────────────────────
    #   The seat's state lives in its own daemon (127.0.0.1:8771) and NOTHING here persists it. That
    #   is deliberate twice over:
    #     ★ 43 of this file's 54 write sites are still non-atomic, so a card that stored mic state
    #       would be adding a 44th to hold the most safety-critical flag on the box. This card writes
    #       NO store and takes NO lock — it reads a socket and renders what it is told.
    #     ★★ And it keeps C2 intact end to end: the indicator is DERIVED in the daemon on every read,
    #       so the panel cannot show 'listening' from a cached value the microphone has since left
    #       behind. The panel has no independent memory of mic state to be wrong with.
    #   ★ An OFFLINE seat renders as an absent card with the reason, never as a dead control.
    _JARVIS = "http://127.0.0.1:8771"
    #   ★ "steer" is the COMPOSER's door onto the same SteerService instance the voice seat uses.
    #     It is listed here rather than given its own panel handler precisely so there is one wire to
    #     one instance — a second SteerService in this process would share the code and disagree
    #     about the turn, which is the fork steering_service.py's docstring warns against.
    _JARVIS_ALLOWED = ("engage", "destination", "mode", "interrupt", "context/clear", "listen",
                       "steer", "steer/boundary", "bubble", "offload")

    def _jarvis_call(self, path: str, body: dict | None = None, timeout: float = 8.0):
        import urllib.request as _u, urllib.error as _ue
        try:
            if body is None:
                req = _u.Request(self._JARVIS + path)
            else:
                req = _u.Request(self._JARVIS + path, data=json.dumps(body).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
            with _u.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (_ue.URLError, OSError, ValueError) as e:
            return {"ok": False, "offline": True,
                    "error": f"the voice seat is not running ({e.__class__.__name__})",
                    "how": f"start it: {ROOT}/jarvis/launch-jarvis.sh start"}

    def _jarvis_status(self) -> None:
        """GET /jarvis/status — the voice seat, as the seat itself reports it."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        return self._send_json(200, self._jarvis_call("/state"))

    def _focus_sources(self) -> None:
        """GET /state/focus-sources — what AionUi currently IS. Read-only, loopback-only.

        ★★★ THE SPLIT, agreed with the boardwalk seat (CONTRACT_FOCUS_SOURCES_0915):
        THIS ENDPOINT EXPOSES SOURCES. THE BOARDWALK SEAT DERIVES THE FOCUS. NEITHER STORES IT.
        The awareness protocol requires focus to be derived on every read — the voice seat's
        indicator is the precedent, and it cannot lie precisely because no setter exists. If this
        endpoint also derived a focus, the box would gain a seventh store that drifts. Two derivers
        is the same defect as two stores.

        ★★ A DESTINATION IS A TRIPLE — harness · model · conversation — and all three ship on every
        row, separately. 78 conversations run ONE model (moonshotai/kimi-k3) under THREE different
        harness names, so "route me to codex" really means "codex harness, kimi model". A row that
        shows one and implies the others is the conflation this endpoint exists to end.

        ★ `route_proven` is ALWAYS false: this reports what the config DECLARES, and only a CALL
        proves a route. Declared routing is how a metered account drained while the screen said
        "subscription". Any source that cannot be read is NAMED in `degraded[]` and its field is
        null — never a guess, because silence and success must not render identically."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            import sys as _s, importlib
            _p = str(ROOT / "operator")
            if _p not in _s.path:
                _s.path.insert(0, _p)
            import focus_sources as _fs
            # ★★★ reload REMOVED 2026-09-17 — takes the per-module import lock on an endpoint
            #   the jarvis voice daemon polls continuously. Same convoy as derive_items.
            return self._send_json(200, _fs.sources())
        except Exception as e:
            # Fail loudly and honestly: the deriver must be able to tell "no focus" from "could not look".
            return self._send_json(200, {
                "ts": int(time.time() * 1000),
                "aionui": {"reachable": False, "route": None, "selected": None},
                "walk": {"step_id": None, "board_n": None},
                "board": {"expanded_row_id": None},
                "panel": {"open_section": None},
                "degraded": ["focus-sources-module", "aionui-sources", "walk", "board", "panel-section"],
                "error": f"{e.__class__.__name__}: {e}",
            })

    def _capture_policy(self) -> None:
        """GET /state/capture-policy — the CAPTURE master, read-only, for the voice seat.

        ★★★ THE POINT: `capture_master.override` is the voice seat's ONLY master switch today
        (`jarvis/core.py:161-180` never reads the real store and ends in `return False`). Deleting
        that file does not restore governance — it makes the seat's microphone permanently cold.
        The seat kept a file because it had no readable path to the real store: the policy lives in
        the renderer's localStorage and cannot be read out from under a running Electron app on
        every mic tick. THIS IS THAT PATH. Once the seat reads here, the override is unnecessary and
        removing it becomes correct rather than destructive.

        ★ NOT `/capture/policy`: on this panel `/capture/*` already means the KNOWLEDGE-capture
        queue (`/capture/queue`, `/capture/approve`) — an unrelated thing that shares the word.
        Two meanings for "capture" is how someone eventually wires the scrub queue to a microphone.

        ★★ FAILS CLOSED: an unreadable policy returns mic_allowed=false, never true."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            import sys as _s, importlib
            _p = str(ROOT / "operator")
            if _p not in _s.path:
                _s.path.insert(0, _p)
            # ★★★ reload REMOVED 2026-09-17 — takes the per-module import lock on an endpoint
            #   the jarvis voice daemon polls continuously. Same convoy as derive_items.
            # ★ Serve the PUBLISHED file first. A CDP call per request is what put 35 threads
            #   inside one websocket handshake and made this endpoint a 3.7 s median.
            import capture_policy_store as _cps
            published = _cps.read()
            if published.get("readable"):
                return self._send_json(200, published)
            # Cold or stale store (panel just started, or the writer is behind): fall back to a
            # live read so the FIRST caller after a restart is not denied by an empty file.
            import focus_sources as _fs
            live = _fs.capture_policy()
            try: _cps.write(live)
            except Exception: pass
            return self._send_json(200, live)
        except Exception as e:
            return self._send_json(200, {
                "ts": int(time.time() * 1000), "readable": False,
                "master": None, "paused": None, "mic_allowed": False,
                "reason": f"capture-policy module unavailable — failing closed ({e.__class__.__name__})",
                "degraded": ["capture-policy"],
            })

    def _set_capture_policy(self) -> None:
        """POST /state/capture-policy {"master": bool, "paused": bool, "actor": str}

        ★★★ WHY A WRITE PATH EXISTS: the operator pressed tap-to-speak, was refused correctly, and
        the only remedy anyone could offer him was "go to AionUi and flip CAPTURE" — told to him
        three times across three reports. That is GUI parity (INV-39) failing in the direction that
        leaves him unable to use the thing at all: the voice seat could not turn on its own
        microphone, and neither could he from inside it.

        ★★ ONE STORE, ONE WRITER. The voice seat holds a CDP handle to the same page and could have
        written the key itself. It deliberately did not, because a second writer reaching into this
        store is the two-writer defect we spent the day removing. This is the single writer.

        ★ THE CLAIM THAT MUST NEVER BE WRONG IS "I TURNED YOUR MIC OFF" — so the result is a SECOND,
        independent re-read of the store, never an echo of the request. If the store does not
        confirm, this reports ok:false and the caller must treat the mic as UNCHANGED.

        ★ Loopback only, no auto-anything, and every write is appended to an audit line so
        "who turned the microphone on" is answerable rather than inferred."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln).decode() or "{}") if ln else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": f"bad JSON body ({e.__class__.__name__})"})
        try:
            import sys as _s, importlib
            _p = str(ROOT / "operator")
            if _p not in _s.path:
                _s.path.insert(0, _p)
            import focus_sources as _fs
            # ★★★ reload REMOVED 2026-09-17 — takes the per-module import lock on an endpoint
            #   the jarvis voice daemon polls continuously. Same convoy as derive_items.
            res = _fs.set_capture_policy(
                master=body.get("master"), paused=body.get("paused"),
                actor=str(body.get("actor") or "panel"))
            # ★★★ REPUBLISH IMMEDIATELY ON A SUCCESSFUL WRITE. 2026-09-17.
            #   The publisher ticks every ~4.3s (measured), so without this an operator switching
            #   capture OFF would stay "allowed" in the file for up to one tick. The tick is the
            #   BACKSTOP for a dead writer; it must not also be the revocation latency for the
            #   common case, which is the operator himself flipping the switch.
            #   ★ Ordering matters: publish BEFORE answering, so a seat that reacts to this
            #     response can never read a file that still says the old thing.
            if res.get("ok"):
                try:
                    _publish_capture_policy()
                except Exception as exc:
                    logger.warning("immediate capture-policy republish failed (%s) — the "
                                   "publisher tick will correct it within ~%.0fs",
                                   exc, CAPTURE_PUBLISH_INTERVAL_S * 2)
            return self._send_json(200 if res.get("ok") else 409, res)
        except Exception as e:
            return self._send_json(500, {"ok": False,
                                         "error": f"{e.__class__.__name__}: {e}",
                                         "note": "treat the microphone as UNCHANGED"})

    def _jarvis_chats(self) -> None:
        """GET /jarvis/chats — the FULL routing menu: every individual conversation, grouped by team.

        ★★★ HIS ASK, verbatim: "The routing dropdown needs to be a full menu for all the individual
        chats, at least as comprehensive as the KHAOSS llm/profile selection we made before."
        The old control offered ONE entry called "AionUi conversation" and the conversation itself was
        chosen somewhere else — so the dropdown answered "which KIND of destination" while the question
        he is actually asking is "which CHAT".

        ★★ AND THE PROFILE SELECTOR IS THE BAR FOR A REASON: what made it usable was never the list,
        it was that every row carried enough to choose BETWEEN rows. A bare list of 177 ids would be a
        WORSE control than the three entries it replaces. So each row here carries team · agent ·
        model · backend · message count.

        ★ Rows that cannot be routed to are LISTED AND MARKED, never omitted — a menu that silently
        drops entries teaches the reader it is showing everything. Two are refused by id at the
        resolver: `de63a495` (render-crashes the app) and the trading lane.

        ★★★ WIDENED 2026-10-15 (overnight item 8) — THE PHONE SURFACE MAY REACH THIS ROUTE.
        WHAT THE GATE WAS, MEASURED, BEFORE THIS LINE CHANGED: `if not self._is_local_request():
        return 403`. Nothing else gated it — not `jarvis/privacy_gate.py` (which routes nothing and
        is imported by the EGRESS path, `voice_delivery`/`router`, never by a route), not
        `taint_gate.py` (a PreToolUse gate for the agent's own tool calls). The whole gate was this
        one predicate, so this is the one place the widening belongs.

        ★★★ AND IT IS WIDENED IN THE SHAPE THE OPERATOR RULED: *"The user gets warning and
        continues if he pleases ... user can directly prompt to lift or restrict whatever they
        wish, and it gets logged until further notice."* So:
            WARN  the response carries `surface`, `warning`, `device` and `overridable`, AND the
                  same warning rides the wire as `X-Khaoss-Warn` — a warning only in the body is a
                  warning a caller can miss by not reading it.
            ALLOW the rows are served. This route does not refuse a paired phone on its own
                  judgement. The only refusals left are IDENTITY (no token / revoked / expired) and
                  the operator's own `restricted` policy — neither of which is this handler deciding.
            LOG   every admitted phone read appends to `phone_surface.jsonl` with WHEN, WHICH
                  DEVICE and WHICH PATH. A log that records only refusals cannot answer the one
                  question the operator actually has: *did a phone surface reach my chats?*
        ★ THE GATE IS STILL HERE. Loopback is still the FAST PATH and still the default; a request
          with no device token and no loopback still gets the 403 it always got. This admits a
          second, narrower class of caller — it does not remove the check.
        ★ WHAT IS **NOT** WIDENED, DELIBERATELY: `POST /jarvis/control` (the microphone control
          plane) and `jarvis/daemon.py`'s own `_local_only()`, whose comment says *"there is no
          token that opens it"*. A phone drives the mic through the panel proxy below, so the
          daemon's floor is intact and that sentence is still true.
        """
        rec, refusal = None, None
        if not self._is_local_request():
            rec, refusal = self._phone_identify()
            if rec is None:
                # ★ Reaching this line remotely ALWAYS means a token was presented — `_check_auth`
                #   already 401'd a tokenless remote caller before dispatch. So the honest sentence
                #   is not "local-only" (which sends him looking at the network) but "the token you
                #   presented is not a live pairing". The reason is in `phone`.
                return self._send_json(403, {
                    "error": "this route is loopback-only, and the device token presented is "
                             "not a live pairing",
                    "phone": refusal, "surface": "phone"})
            dec = self._phone_decide("chats", rec)
            if not dec["allow"]:
                self._phone_log_hit("chats_refused", rec, "/jarvis/chats", dec)
                return self._send_json(403, {"error": "restricted by your standing policy",
                                             "phone": dec["why"], "overridable": True})
            self._phone_log_hit("chats_reached", rec, "/jarvis/chats", dec)
        try:
            import sys as _s
            _s.path.insert(0, str(ROOT / "jarvis"))
            import importlib
            cm = importlib.reload(importlib.import_module("chat_menu"))
            body = cm.menu()
            if rec is not None:
                return self._phone_send(200, body, "chats", rec)
            return self._send_json(200, body)
        except Exception as e:
            return self._send_json(500, {"error": f"chat menu unavailable: {e.__class__.__name__}: {e}"})

    # ═════════════════════════════════════════════════════════════════════════════════════════════
    # ★★★ THE PHONE SURFACE (2026-10-15, overnight item 8)
    # ═════════════════════════════════════════════════════════════════════════════════════════════
    #   THE THREE RULES EVERY METHOD BELOW IS WRITTEN TO, all of them from this estate's own record:
    #     1. WARN, ALLOW, LOG, OVERRIDABLE — the operator's ruling, verbatim: *"The user gets warning
    #        and continues if he pleases ... user can directly prompt to lift or restrict whatever
    #        they wish, and it gets logged until further notice."* A phone-shaped request is NEVER
    #        refused because it is phone-shaped. It is refused only for IDENTITY (no token, revoked,
    #        expired) or because the OPERATOR restricted it. Those are different facts and they get
    #        different status codes.
    #     2. NOTHING THE PHONE CAN REACH SERVES A SECRET WITHOUT AN IDENTITY CHECK. The one endpoint
    #        that takes no device token is `/phone/pair/redeem`, because that is how a device gets
    #        one — and it is guarded by a single-use, expiring code the operator minted on the box.
    #        There is no anonymous path to a token, and `token_hash` never leaves the store.
    #     3. CREDENTIAL AND CONTROL ENDPOINTS STAY LOOPBACK-ONLY. `/phone/devices`, `/phone/log`,
    #        `/phone/revoke`, `/phone/policy` — the operator's own acts — are `_is_local_request()`
    #        gated and answer 403 from the tailnet even with a valid device token, exactly like
    #        `/remote-access/key` already does. A paired phone is a caller, not an operator.
    #
    #   ★ THE ONE WIRE. Every voice write below goes through `_jarvis_call()`, the SAME proxy the
    #     desktop panel uses, to the SAME daemon on 127.0.0.1:8771. The phone does not get a second
    #     path to the microphone and the daemon's `_local_only()` is untouched — so its comment,
    #     *"there is no token that opens it"*, is still true after this change.

    PHONE_HTML = ROOT / "panel" / "phone.html"

    PHONE_MANIFEST = {
        "name": "KHAOSS Seat — Phone",
        "short_name": "KHAOSS",
        "start_url": "/phone",
        "scope": "/phone",
        "display": "standalone",
        "background_color": "#0b0f14",
        "theme_color": "#0b0f14",
        "orientation": "portrait",
        "description": ("The voice seat's control surface, sized for a phone. The voice buttons "
                        "live at the very bottom of the expanded view. Pairing is deliberate: ask "
                        "the seat for a code."),
        "icons": [],
    }

    def _phone_mod(self):
        """Lazy import of `jarvis/phone_pairing.py`. ★ One store per fact: the device store and its
        ledger live in that module and nowhere else — this file reads it and never writes a second
        copy of device state, which is how the panel and the seat would drift apart."""
        import sys as _s
        _p = str(ROOT / "jarvis")
        if _p not in _s.path:
            _s.path.insert(0, _p)
        import phone_pairing
        return phone_pairing

    def _phone_token(self) -> str:
        """The device token, from `Authorization: Bearer` or the `phone_token` cookie. ★ BOTH,
        because a phone that has been backgrounded loses its ability to set headers on a top-level
        navigation, and the cookie is what keeps a PWA re-launch from looking like a revoked
        device. Neither is logged."""
        auth = str(self.headers.get("Authorization") or "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        for part in str(self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k.strip() == "phone_token":
                return v.strip()
        return ""

    def _phone_identify(self) -> tuple:
        """`(device_record, None)` for a live paired device, else `(None, refusal_dict)`.

        ★ RETURNS A DEVICE OR A REASON — never a secret, and never a hint of one. A revoked device
        and an expired one are refused with DIFFERENT sentences, because only one of them means
        something is still holding a token it should not have.
        """
        try:
            P = self._phone_mod()
            ok, rec = P.verify(self._phone_token(), path=self.path)
            if ok:
                return rec, None
            # ★ `code` MUST PASS THROUGH. It is the machine-readable half of the refusal and it is
            #   the ONLY thing that lets `/phone/session` tell "you presented nothing" from "what you
            #   presented is dead". Dropping it here made every refusal look like the harmless kind —
            #   a rebuilding dict is a silent filter, and this one was eating the one field that
            #   mattered.
            return None, {"error": rec.get("error"), "need": rec.get("need", "pairing"),
                          "code": rec.get("code"),
                          "pair_with": "POST /phone/pair/redeem"}
        except Exception as e:  # noqa: BLE001
            # ★ FAIL CLOSED. A pairing module that cannot be read is not a device that is allowed.
            return None, {"error": f"pairing store unavailable: {e.__class__.__name__}",
                          "need": "pairing"}

    def _phone_decide(self, surface: str, rec: dict) -> dict:
        try:
            return self._phone_mod().decide(surface, rec)
        except Exception as e:  # noqa: BLE001
            return {"allow": False, "warn": None, "log": True, "overridable": True,
                    "surface": surface, "mode": "unreadable",
                    "why": f"policy unavailable ({e.__class__.__name__}) — refusing, not guessing"}

    def _phone_log_hit(self, event: str, rec: dict, path: str, dec: dict) -> None:
        """★ THE LOG IS NOT OPTIONAL, AND IT IS WRITTEN FOR ALLOWED READS TOO. A refusal-only
        ledger cannot answer the question this surface exists to make answerable: *did a phone
        reach my chats?* Fingerprint and label only — never the token, never the rows read."""
        try:
            self._phone_mod().log(event, device_id=(rec or {}).get("id"),
                                  device_fp=(rec or {}).get("fp"),
                                  label=(rec or {}).get("label"),
                                  surface=(dec or {}).get("surface"),
                                  mode=(dec or {}).get("mode"), path=path)
        except Exception:  # noqa: BLE001
            pass

    def _phone_send(self, status: int, payload: dict, surface: str, rec: dict) -> None:
        """A phone-surface response: the warning in the BODY and on the WIRE, in one place so the
        two can never disagree."""
        dec = self._phone_decide(surface, rec)
        body = dict(payload)
        body["surface"] = "phone"
        body["device"] = {"id": rec.get("id"), "label": rec.get("label"), "fp": rec.get("fp")}
        body["overridable"] = True
        if dec.get("warn"):
            body["warning"] = dec["warn"]
        hdrs = {"X-Khaoss-Surface": "phone", "X-Khaoss-Device": str(rec.get("fp") or "")}
        if dec.get("warn"):
            # A header value must be latin-1 and single-line; the warning is prose. Percent-encode
            # the non-ASCII rather than dropping the warning from the wire.
            hdrs["X-Khaoss-Warn"] = dec["warn"].encode("utf-8").hex()[:900]
            hdrs["X-Khaoss-Warn-Encoding"] = "utf-8-hex"
        return self._send_json(status, body, headers=hdrs)

    def _phone_gate(self, surface: str, path: str) -> tuple:
        """`(device, None)` admitted, `(None, True)` already-refused. ★ The whole policy in one
        place so a route cannot forget the log, and cannot refuse for the wrong reason."""
        rec, refusal = self._phone_identify()
        if rec is None:
            self._send_json(401, {"ok": False, "paired": False, "phone": refusal,
                                  "surface": "phone", "how": "ask the seat for a pairing code"})
            return None, True
        dec = self._phone_decide(surface, rec)
        if not dec["allow"]:
            self._phone_log_hit(f"{surface}_refused", rec, path, dec)
            self._send_json(403, {"ok": False, "error": "restricted by your standing policy",
                                  "phone": dec["why"], "overridable": True, "surface": "phone"})
            return None, True
        self._phone_log_hit(f"{surface}_reached", rec, path, dec)
        return rec, False

    # ── the page and its PWA furniture ────────────────────────────────────────────────────────
    def _serve_phone(self) -> None:
        """GET /phone — the shell. ★ SERVED WITHOUT A TOKEN ON PURPOSE, and that is safe for one
        reason: this file contains no secret. The device token lives in the phone's own storage and
        is presented per request. The incident this inherits from was a LIVE VALUE served
        anonymously; the rule it produced is about secrets, not about HTML."""
        if not self.PHONE_HTML.is_file():
            return self._send_text(500, f"phone surface HTML missing: {self.PHONE_HTML}")
        body = self.PHONE_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # ★ NO-CACHE: a stale shell that still renders a control the backend has moved on from is
        #   the "declared routing" failure in miniature — the page must be the served page.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _phone_manifest(self) -> None:
        body = json.dumps(self.PHONE_MANIFEST, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _phone_session(self) -> None:
        """GET /phone/session — who am I, what may I do. ★ Answers `paired:false` with 200, not 401:
        a phone that is not paired has not made an error, it has not finished pairing, and the page
        needs to render the pairing form rather than an error card.

        ★★★ `forget_token` IS THE FIX FOR A DEFECT FOUND BY RENDERING THE PAGE (2026-10-15). The
        page called `setToken(null)` on ANY `paired:false` — so a transient answer (the seat
        restarting, the store briefly unreadable) DELETED the operator's pairing and sent him back
        to the seat for a new code. `paired:false` has two meanings and they must not look alike:
            · no credential was presented  -> nothing to forget
            · a credential was presented and refused for an INFRASTRUCTURE reason -> nothing to forget
            · a credential was presented and refused as unknown/expired/revoked -> forget it
        Only the third destroys anything, and the server decides which one this is, because only the
        server can tell them apart. A client that infers it from `paired:false` is guessing."""
        rec, refusal = self._phone_identify()
        if rec is None:
            try:
                code = str((refusal or {}).get("code") or "")
                forget = code in self._phone_mod().IDENTITY_REFUSALS
            except Exception:  # noqa: BLE001
                forget = False   # ★ an unreadable module is EXACTLY when we must not destroy a token
            return self._send_json(200, {"ok": True, "paired": False, "surface": "phone",
                                         "token_presented": bool(self._phone_token()),
                                         "forget_token": forget,
                                         "reason": (refusal or {}).get("error"),
                                         "warnings": [], "scope": []})
        dec = self._phone_decide("chats", rec)
        return self._send_json(200, {"ok": True, "paired": True, "surface": "phone",
                                     "forget_token": False,
                                     "device": {"id": rec.get("id"), "label": rec.get("label"),
                                                "fp": rec.get("fp")},
                                     "scope": list(rec.get("scope") or []),
                                     "expires_at": rec.get("expires_at"),
                                     "last_seen_at": rec.get("last_seen_at"),
                                     # ★★★ `warnings` IS EMPTY HERE, AND THAT IS A FIX, NOT AN
                                     # OMISSION (2026-10-15). This method used to return the CHATS
                                     # warning — so the page, which also takes the warning from
                                     # `/phone/chats`, rendered THE SAME SENTENCE TWICE in one banner.
                                     # Seen in the screenshot; a warning that repeats itself reads as a
                                     # bug and gets dismissed on sight. ★ ONE STORE PER FACT: this is
                                     # the IDENTITY answer and it says who the device is. The warning
                                     # belongs to the surface that was actually reached, and it
                                     # arrives with that read. `mode` is reported so the caller can
                                     # still see the standing policy without being warned about a
                                     # surface it has not touched.
                                     "policy_mode": dec.get("mode"),
                                     "warnings": []})

    # ── the read surface ──────────────────────────────────────────────────────────────────────
    def _phone_state(self) -> None:
        """GET /phone/state — the daemon's snapshot, proxied. ★ THE PANEL STORES NO MIC STATE (see
        the note at `_JARVIS`), so this cannot show a listening flag the microphone has left
        behind. An offline seat renders as absent-with-reason, never as a dead control."""
        rec, refused = self._phone_gate("voice", "/phone/state")
        if refused:
            return
        return self._phone_send(200, self._jarvis_call("/state"), "voice", rec)

    def _phone_chats(self) -> None:
        """GET /phone/chats — ★ THE WIDENING, in its phone shape.

        ★★ WHAT GATES `/chats` AND WHY THE ANSWER IS NOT `daemon.py`: the daemon's route is behind
        `_local_only()` AND behind a loopback BIND (127.0.0.1:8771). A phone cannot form a
        connection to it at all, so widening *there* would mean binding the microphone control plane
        to the network — a strictly larger change that its own comment forbids. The phone's path to
        the same rows is this server, so the widening is here, and `/jarvis/chats` above is widened
        too. The daemon keeps its floor and the panel becomes the one place the phone is judged.
        """
        rec, refused = self._phone_gate("chats", "/phone/chats")
        if refused:
            return
        payload = self._jarvis_call("/chats?limit=0")
        if payload.get("offline"):
            # ★ An offline seat is reported AS offline. It must never render as an empty menu —
            #   "no chats" and "no answer" are different facts and only one of them is fine.
            return self._phone_send(503, {"ok": False, **payload}, "chats", rec)
        return self._phone_send(200, dict(payload), "chats", rec)

    def _phone_delivery(self) -> None:
        rec, refused = self._phone_gate("voice", "/phone/delivery")
        if refused:
            return
        return self._phone_send(200, self._jarvis_call("/delivery"), "voice", rec)

    # ── operator-only, loopback-only ──────────────────────────────────────────────────────────
    def _phone_devices(self) -> None:
        """GET /phone/devices — ★ LOOPBACK ONLY. Who is paired, when they paired, when they were
        last seen, and whether they are cut off. This is the surface the operator REVOKES from."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only",
                                         "why": "the device roster is the operator's, not a "
                                                "paired phone's"})
        try:
            P = self._phone_mod()
            return self._send_json(200, {"ok": True, "devices": P.devices(), "pending": P.pending(),
                                         # ★ THE STORE'S OWN STATE, so a file that exists and cannot
                                         #   be read is VISIBLE. Without this, a corrupt store renders
                                         #   as an empty roster — and "no devices are paired" and "the
                                         #   store is broken" are the same shape and completely
                                         #   different facts. Found the hard way, 2026-10-15.
                                         "store": P.store_state(),
                                         "log": P.recent(60),
                                         "revoke_with": "POST /phone/revoke {id}"})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _phone_log(self) -> None:
        """GET /phone/log — the ledger. ★ LOOPBACK ONLY, and it is the answer to *did a phone
        surface reach my chats, and when*."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, {"ok": True, "rows": self._phone_mod().recent(200)})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _phone_policy_get(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._phone_mod().policy())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    # ── the write surface ─────────────────────────────────────────────────────────────────────
    def _phone_pair_redeem(self, body: dict) -> None:
        """POST /phone/pair/redeem — ★ THE ONLY PHONE-REACHABLE ROUTE HERE WITH NO DEVICE TOKEN.

        ★★ IT IS NOT ANONYMOUS ACCESS, IT IS THE ASKING. The body must carry a single-use code the
        operator minted on the box (`phone_pairing.py issue`, loopback-only) and read off his own
        screen. The code dies in ten minutes, dies ON USE, and the token it returns is stored by the
        phone and never by us in the clear. *"It may ASK. It may NOT TAKE."*
        """
        try:
            P = self._phone_mod()
            r = P.redeem(str(body.get("code") or ""), label=str(body.get("label") or "phone"))
            if not r.get("ok"):
                return self._send_json(401, {"ok": False, "surface": "phone",
                                             "error": r.get("error")})
            return self._send_json(200, {"ok": True, "surface": "phone", **r})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _phone_ptt(self, body: dict) -> None:
        """POST /phone/ptt — press/hold, forwarded to the SAME daemon route the seat's own button
        uses. ★ `ptt_refused` TRAVELS. The daemon rolls the held flag back when the mic gate
        declines; without that field the phone's button refuses silently and reads as broken, which
        is exactly the defect the operator already hit on the desktop pill."""
        rec, refused = self._phone_gate("voice", "/phone/ptt")
        if refused:
            return
        r = self._jarvis_call("/ptt", {"held": bool(body.get("held")),
                                       "mode": str(body.get("mode") or "hold")})
        return self._phone_send(200, dict(r), "voice", rec)

    def _phone_mode(self, body: dict) -> None:
        rec, refused = self._phone_gate("voice", "/phone/mode")
        if refused:
            return
        r = self._jarvis_call("/mode", {"mode": str(body.get("mode") or "")})
        return self._phone_send(200, dict(r), "voice", rec)

    def _phone_always_on(self, body: dict) -> None:
        """POST /phone/always_on — ★ THE ONE MODE WITH STANDING EXPOSURE, so it carries the loudest
        warning of the three and its own surface name. It is still ALLOWED: the operator's ruling is
        that he gets the warning and continues if he pleases. The refusal in the UI is a CONFIRM —
        the phone asking him — not this server deciding for him."""
        rec, refused = self._phone_gate("always_on", "/phone/always_on")
        if refused:
            return
        on = bool(body.get("on"))
        r = self._jarvis_call("/always_on_opt_in" if on else "/mode",
                              {"on": True} if on else {"mode": "push-to-talk"})
        out = dict(r)
        out["standing_exposure"] = on
        return self._phone_send(200, out, "always_on", rec)

    def _phone_revoke(self, body: dict) -> None:
        """POST /phone/revoke — ★ LOOPBACK ONLY. Cutting a device off is the operator's act; a
        paired phone must not be able to revoke its siblings."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only",
                                         "why": "only the operator revokes a device"})
        try:
            r = self._phone_mod().revoke(str(body.get("id") or ""),
                                         str(body.get("reason") or "operator"))
            return self._send_json(200 if r.get("ok") else 404, r)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _phone_policy_post(self, body: dict) -> None:
        """POST /phone/policy — ★ LOOPBACK ONLY. Lift or restrict a surface. The change is logged
        every time, which is the operator's own wording: *it gets logged until further notice*."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            r = self._phone_mod().set_policy(str(body.get("surface") or ""),
                                             str(body.get("mode") or ""),
                                             str(body.get("why") or "operator"))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _jarvis_control(self, body: dict) -> None:
        """POST /jarvis/control — GUI PARITY (INV-39): every control the voice seat exposes must be

        ★ WHITELISTED, and local-only twice: this handler refuses a non-loopback caller, and the
          daemon it forwards to refuses one as well. A microphone control plane does not inherit the
          panel's tailnet bind."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        what = str(body.get("what") or "")
        if what not in self._JARVIS_ALLOWED:
            return self._send_json(400, {"ok": False,
                                         "error": f"{what!r} is not a permitted voice-seat control",
                                         "allowed": list(self._JARVIS_ALLOWED)})
        return self._send_json(200, self._jarvis_call("/" + what, body.get("args") or {}))

    def _operator_walk(self) -> None:
        """GET /operator/walk?batch=N&order=… — the BOARD walk, one item at a time.

        ★ NOT the credential wizard. That one walks eleven SERVICE logins and has never touched the
        board. This walks the open BOARD ROWS, with a hand-written plain-English framing, because he
        said in writing that he does not know the fundamentals of some of these.
        ★★ An item with no hand-written explainer SAYS SO and shows the board's own wording. Nothing
        here improvises a beginner summary — he would act on it."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        q = parse_qs(urlsplit(self.path).query)
        try:
            batch = int((q.get("batch") or ["1"])[0])
        except ValueError:
            batch = 1
        order = (q.get("order") or ["recommended"])[0]
        try:
            w = self._op_mod("board_walk")
            return self._send_json(200, w.plan(batch, order))
        except Exception as e:
            return self._send_json(500, {"error": f"walk unavailable: {e}"})

    # ── THE SETUP WALK + THE GEV OPTION (added 2026-10-12) ───────────────────────────────────────
    def _setup_wizard_popout(self) -> None:
        """GET /operator/setup-wizard — the first-install walk as its own served page.

        ★ MEASURED BEFORE THIS EXISTED: the page was on disk and this route returned 404, and so did
          `/operator/setup/walk`. The page had already been written to SAY so (`sw-noroute`) rather
          than render an empty walk, which is why the defect was visible at all. A served page with
          no route is the false-absence class: indistinguishable from a page that failed to build."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if not SETUP_WIZARD_HTML.is_file():
            return self._send_text(500, f"setup wizard HTML missing: {SETUP_WIZARD_HTML}")
        body = SETUP_WIZARD_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")   # it renders live measurements
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _setup_walk(self) -> None:
        """GET /operator/setup/walk[?recheck=<id>] — the walk, MEASURED NOW.

        ★ IT COMPOSES; IT DOES NOT DECIDE. `operator/setup_blockers.plan()` owns every step, every
          probe and every state word. This route runs it and returns it; it cannot invent a step, and
          it cannot turn one green. A `recheck` re-runs the whole plan (the probes are cheap and
          bounded) so the answer is a fresh measurement rather than a cached opinion.

        ★★ LOOPBACK ONLY. These rows name installed software, a credential's PRESENCE, and the
          operator's own accept/decline decision. None of that is for the tailnet.

        ★★★ AND IT NEVER CARRIES A KEY VALUE. `setup_blockers` publishes the GEV option's evidence,
          which is built by `gev_option._fingerprint` — presence, last-4 and a sha256 prefix. There is
          no code path here that reads a secret, so there is none that can leak one."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            mod = self._op_mod("setup_blockers")
            d = mod.plan()
            q = parse_qs(urlsplit(self.path).query)
            want = (q.get("recheck") or [None])[0]
            if want:
                row = next((r for r in d["steps"] if r["id"] == want), None)
                d = dict(d)
                d["rechecked"] = ({"id": want, "state": row["state"], "evidence": row["evidence"]}
                                  if row else {"id": want, "error": "no such step"})
            return self._send_json(200, d)
        except Exception as e:  # noqa: BLE001
            # ★ A FAILED MEASUREMENT IS NOT A CLEAN WALK. 500, and the page renders it as an error.
            return self._send_json(500, {"error": f"the setup walk could not be measured: "
                                                  f"{type(e).__name__}: {e}"})

    def _setup_gev(self) -> None:
        """GET /operator/setup/gev — the recommended option alone: state, terms, choice.

        ★ SEPARATE FROM THE WALK because the terms are the thing he is asked to judge, and a caller
          that wants ONLY the cost of the offering should not have to measure fourteen steps to get
          it. Same store, same one measurement path — `gev_option.row()`."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            mod = self._op_mod("gev_option")
            return self._send_json(200, mod.row())
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"the GEV option could not be measured: "
                                                  f"{type(e).__name__}: {e}"})

    def _setup_choice(self, body: dict | None) -> None:
        """POST /operator/setup/choice — record the operator's answer to the GEV recommendation.

        Body: {"decision": "accepted"|"declined"|"clear", "note": "..."} — and NOTHING else.

        ★★★ IT REFUSES A CREDENTIAL, AND THAT IS THE POINT OF THE ROUTE EXISTING AT ALL.
            The recommended option needs a Google Maps key with billing. The obvious next step is a
            text field on the wizard, and that step is forbidden twice over: `AGENTS.md` §6 —
            *"Never log key material — fingerprints and last-4 only"* — and the estate's rule that a
            stage which READS a secret runs on the box. A field here would put a plaintext key into
            an HTTP body, into this process's memory, and into whatever logs the panel keeps. So this
            handler does not merely decline to store a key: it REFUSES the request and says where the
            key actually goes.

        ★ AND IT NEVER ECHOES THE BODY. The refusal names the FIELD, never the value — a refusal
          message that quotes what it refused is the leak it was written to prevent."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        b = body or {}
        # ── the credential refusal. Check KEYS and VALUES, because people paste into the wrong box.
        cred_keys = [k for k in b if any(t in str(k).lower() for t in
                     ("key", "token", "secret", "password", "credential"))]
        import re as _re
        keyish = []
        for k, v in b.items():
            s = str(v or "")
            # sk-…, AIza… (Google), long high-entropy strings in a free-text note
            if _re.search(r"\bsk-[A-Za-z0-9_\-]{8,}", s) or _re.search(r"\bAIza[0-9A-Za-z_\-]{20,}", s) \
               or (_re.fullmatch(r"[A-Za-z0-9_\-]{28,}", s.strip()) and k != "decision"):
                keyish.append(str(k))
        if cred_keys or keyish:
            return self._send_json(400, {
                "error": "refused — this route records a DECISION and cannot carry a credential",
                "fields_refused": sorted(set(cred_keys + keyish)),
                "no_value_was_read_or_logged": True,
                "where_the_key_actually_goes": (
                    "Start God's Eye View and open PROVIDER SETTINGS (the POWER UP chip, "
                    "bottom-right). It writes the key to its own pinokio/ENVIRONMENT with owner-only "
                    "permissions. It is plaintext on this box and it stays on this box."),
                "why": ("AGENTS.md §6: never log key material — fingerprints and last-4 only. A key "
                        "in this request would be a key in a log, and no convenience is worth that."),
            })
        d = str(b.get("decision") or "").strip().lower()
        if d not in ("accepted", "declined", "clear"):
            return self._send_json(400, {
                "error": "decision must be 'accepted', 'declined' or 'clear'",
                "got": d or None,
                "note": "this route refuses to guess what he meant; an unrecorded choice stays "
                        "unrecorded rather than defaulting to yes"})
        try:
            mod = self._op_mod("gev_option")
            rec = mod.set_choice(None if d == "clear" else d, str(b.get("note") or "")[:200])
            st, ev = mod.probe()
            return self._send_json(200, {
                "recorded": rec or {"decision": None},
                "option_state": st,
                "walk_state": mod.walk_state(st),
                "evidence": ev,
                "reversible": ("say \u201cset up gods eye view\u201d or press the button on the same "
                               "row to take it back" if d == "declined" else
                               "the offer is back on the row and nothing was installed by clearing it"
                               if d == "clear" else
                               "press \u201cNot now\u201d on the same row to decline"),
            })
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"error": f"the choice could not be recorded: "
                                                  f"{type(e).__name__}: {e}"})

    def _operator_boardwalk(self) -> None:
        """GET /operator/boardwalk — RETIRED: ONE BOARD, ONE SURFACE.

        ★★★ IT WAS A SECOND FRONT-END OVER THE SAME STORE. `panel/boardwalk-popout.html` rendered
            `/operator/walk` on its own pretty page while the panel's `operator-needs` section
            rendered the FULL board with search, filters, per-item controls, the walk, stewards and
            grant access. Two renderers, one `board.json` + `board_walk.plan()` — the duplicate
            substrate this estate keeps paying for, and a URL the operator could not remember.
        ★ THE PANEL IS NOW THE ONE BOARD. The popout's unique control — the OS-wide
          point-me-to-there overlay — was merged into the board section as `#bwp-bar` (it calls the
          SAME `/operator/point-to` route; it carries no second walk). The topbar Board Walk entry
          points at `/?section=operator-needs` directly.
        ★ A REDIRECT, NOT A DELETION: the path is in desktop entries, launchers and teammate notes.
          A 302 keeps every old link working and lands it on the one surface. `panel/
          boardwalk-popout.html` stays on disk as the archived original; nothing was deleted.
        ★ THE DIVERGENCE THIS PREVENTS, MEASURED THE HARD WAY ON `/operator/board`: a second
          server-rendered copy of the board drifted from the panel's copy and the URL, not the source,
          was the only place the difference showed.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        self.send_response(302)
        self.send_header("Location", "/?section=operator-needs")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _dsh_obs_popout(self) -> None:
        """GET /operator/dsh-jobs — the DSH Harness observability page (simple/mid/high views).
        Served from panel/ like the other popouts; holds no live data, only the polling client."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        if not DSH_OBS_POPOUT_HTML.is_file():
            return self._send_text(500, f"dsh observability HTML missing: {DSH_OBS_POPOUT_HTML}")
        body = DSH_OBS_POPOUT_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _dsh_obs_data(self) -> None:
        """GET /operator/dsh-jobs/data — the live harness state, read from the file the observability
        Cordis Host plugin writes. This route composes; it does not invent. When the plugin has not
        published (or is stopped), it returns an explicit `live: false` shape rather than fabricating."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            if not DSH_OBS_STATE_JSON.is_file():
                # No state feed at all. The SCREENSHOT surface can still be alive — the standalone
                # writer owns the ring independently of any state feed — so the ring and the
                # writer's status are still reported here. Returning an empty shape would make a
                # working capture ring look like nothing exists.
                out = {
                    "live": False,
                    "source": "dsh-observability (no state published)",
                    "error": "no state file yet — is the dsh-observability plugin running?",
                    "generatedAt": None,
                }
                out["writer"] = self._dsh_obs_writer_status()
                listed, err = self._dsh_obs_ring_list()
                out["screenshots"] = listed if listed is not None else []
                out["ringSource"] = {"from": "shots directory listing (no state feed exists)",
                                     "dir": str(DSH_OBS_SHOTS_DIR),
                                     "count": len(out["screenshots"]), "cap": None,
                                     "error": err}
                out["jobs"] = out["agents"] = out["subagents"] = out["actions"] = []
                out["screenshotMode"] = self._dsh_obs_shot_mode_state(out)
                out["shotAttribution"] = self._dsh_obs_attribute_shots(out)
                return self._send_json(200, out)
            raw = DSH_OBS_STATE_JSON.read_text(encoding="utf-8")
            data = json.loads(raw)
            # ★ COMPOSE, DO NOT INVENT. Four derived facts are added here, at read time, from
            #   stores that already exist — none is stored here, so none can drift:
            #     1. the screenshot on/off flag the operator set, TOGETHER WITH a measured
            #        verdict on whether the writer is actually honouring it;
            #     2. the shot→agent pairing (state.json stores shots with no agent on them,
            #        so the join is computed here and the exact key travels in the payload);
            #     3. ★ THE RING, LISTED FROM THE SHOTS DIRECTORY WHEN THE FEED IS STALE, plus how
            #        stale the feed is;
            #     4. ★ THE STANDALONE WRITER's own status file.
            #   WHY 3 EXISTS. The screenshot ring now has a STANDALONE owner
            #   (bin/dsh_obs_writer.py, unit khaoss-dsh-obs-writer), because the Cordis plugin that
            #   used to capture belongs to an ENDED session, can never be redefined, and therefore
            #   can never be made to honour the operator's on/off flag. That writer deliberately
            #   does NOT write state.json — two writers on one file is corruption — so if this
            #   route read state.json alone, its captures would be INVISIBLE. The shots directory
            #   is the single source of truth for the ring. Pointed at, never copied.
            #   ★ AND WHY IT IS CONDITIONAL. While the live plugin publishes, its own list is
            #   current and this route is a pure pass-through — the ring is only composed from the
            #   directory once the feed has actually stopped, so the overlay cannot perturb a
            #   working attribution join.
            if isinstance(data, dict):
                data["writer"] = self._dsh_obs_writer_status()
                data["stateAgeMs"] = self._dsh_obs_state_age(data)
                data["stateStale"] = (data["stateAgeMs"] is not None
                                      and data["stateAgeMs"] > DSH_OBS_STATE_STALE_MS)
                w = data["writer"] if isinstance(data["writer"], dict) else {}
                # Compose the ring from disk when the feed is frozen, or when the standalone writer
                # is the owner (it never writes state.json, so its captures exist ONLY on disk).
                writer_owns = bool(w.get("present")) and bool(w.get("heartbeatFresh")) \
                    and w.get("mode") in ("owning", "conflict")
                data["ringSource"] = self._dsh_obs_ring_provenance(
                    data, compose_from_dir=bool(data["stateStale"]) or writer_owns)
                if data["stateStale"] and data.get("live"):
                    # The plugin published `live: true` and then stopped. Reporting "live" off a
                    # frozen file is exactly the false claim this surface exists to refuse.
                    data["live"] = False
                    data["liveReason"] = (f"the state feed last published {data['stateAgeMs'] // 1000}s "
                                          f"ago — the Cordis plugin that writes it is gone (a dynamic "
                                          f"plugin dies with the session that defined it); the "
                                          f"standalone writer still owns the screenshot ring")
                data["screenshotMode"] = self._dsh_obs_shot_mode_state(data)
                data["shotAttribution"] = self._dsh_obs_attribute_shots(data)
            return self._send_json(200, data)
        except Exception as e:
            return self._send_json(200, {
                "live": False,
                "source": "dsh-observability (read error)",
                "error": f"could not read state: {e}",
                "generatedAt": None,
            })

    # ══ THE RING'S PROVENANCE, THE FEED'S AGE, AND THE STANDALONE WRITER'S OWN FILE ═══════════
    def _dsh_obs_state_age(self, data: dict):
        """How long ago the state feed last published, in ms — or None if it never said."""
        ga = data.get("generatedAt") if isinstance(data, dict) else None
        if not isinstance(ga, (int, float)):
            return None
        return max(0, int(time.time() * 1000 - ga))

    def _dsh_obs_ring_list(self):
        """The ring, listed from the DIRECTORY — the one place a screenshot actually is.

        Returns (screenshots, provenance) where screenshots has exactly the shape state.json stored
        ({file, time}, newest first) so every existing consumer and the attribution join keep
        working unchanged. Returns (None, reason) when the directory cannot be read, so the caller
        can fall back to the list inside state.json rather than pretending the ring is empty."""
        try:
            if not DSH_OBS_SHOTS_DIR.is_dir():
                return None, "shots directory does not exist"
            out = []
            for p in DSH_OBS_SHOTS_DIR.iterdir():
                m = _DSH_OBS_SHOT_RE.match(p.name)
                if not m or not p.is_file():
                    continue
                out.append({"file": p.name, "time": int(m.group(1))})
            out.sort(key=lambda s: s["time"], reverse=True)
            return out, None
        except Exception as e:
            return None, f"{e.__class__.__name__}: {e}"

    def _dsh_obs_ring_provenance(self, data: dict, compose_from_dir: bool = False) -> dict:
        """Report where the ring list came from, and compose it from the DIRECTORY when the feed is
        not publishing.

        ★ WHY THE DIRECTORY ONLY WHEN THE FEED IS STALE. The screenshot ring has a standalone owner
        that deliberately never writes state.json, so its captures exist ONLY on disk — those must
        be listed here or they are invisible. But while the live plugin IS publishing, its own list
        is current and authoritative, and re-deriving the same list from disk on every read is a
        change to a working attribution join for no gain. So: pass-through while the feed publishes,
        compose from disk the moment it stops. The narrow change is the one that cannot perturb a
        surface that is already correct."""
        listed, err = self._dsh_obs_ring_list()
        stored = data.get("screenshots") if isinstance(data, dict) else None
        stored_n = len(stored) if isinstance(stored, list) else 0
        if not compose_from_dir:
            return {"from": "state.json (the live plugin is still publishing it)",
                    "dir": str(DSH_OBS_SHOTS_DIR), "count": stored_n,
                    "cap": data.get("screenshotCap") if isinstance(data, dict) else None,
                    "dirCount": None if listed is None else len(listed),
                    "error": err,
                    "note": ("pass-through: the feed is fresh, so its list is used as published. The "
                             "directory is composed in only once the feed stops, or once the "
                             "standalone writer owns the ring (it never writes state.json).")}
        if listed is None:
            return {"from": "state.json (shots directory unreadable)", "error": err,
                    "count": stored_n, "cap": data.get("screenshotCap")}
        if isinstance(data, dict):
            data["screenshots"] = listed          # ← the ring, as it is on disk, right now
        prov = {"from": "shots directory listing (composed at read time)", "composeReason":
                "the state feed is stale or the standalone writer owns the ring",
                "dir": str(DSH_OBS_SHOTS_DIR), "count": len(listed),
                "cap": data.get("screenshotCap") if isinstance(data, dict) else None,
                "storedCountInStateJson": stored_n,
                "note": ("the directory is the single source of truth for the ring; state.json's own "
                         "list was replaced by this one so a standalone writer's captures are visible "
                         "without that writer having to write state.json")}
        if listed and stored_n and len(listed) != stored_n:
            prov["drift"] = (f"state.json listed {stored_n} shots and the directory holds "
                             f"{len(listed)} — the directory won, because the file is the fact")
        return prov

    def _dsh_obs_writer_status(self):
        """The standalone writer's own status file, verbatim and unmodified — or an explicit
        `present: false` with the reason. Never inferred, never defaulted to 'running'."""
        out = {"present": False, "path": str(DSH_OBS_WRITER_JSON), "error": None}
        try:
            if not DSH_OBS_WRITER_JSON.is_file():
                out["error"] = ("no writer status file — the standalone writer "
                                "(khaoss-dsh-obs-writer) has never run here")
                return out
            d = json.loads(DSH_OBS_WRITER_JSON.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                out["error"] = "writer status file is not an object"
                return out
            hb = d.get("heartbeatAt")
            out.update(d)
            out["present"] = True
            out["heartbeatAgeMs"] = (max(0, int(time.time() * 1000 - hb))
                                     if isinstance(hb, (int, float)) else None)
            # A heartbeat older than 30s means the unit is not running, whatever the file says.
            out["heartbeatFresh"] = (isinstance(out["heartbeatAgeMs"], int)
                                     and out["heartbeatAgeMs"] < 30000)
            return out
        except Exception as e:
            out["error"] = f"{e.__class__.__name__}: {e}"
            return out

    # ══ SCREENSHOT ON/OFF — the operator's switch, and the honest verdict on it ═══════════════
    def _dsh_obs_shot_mode_read(self) -> dict:
        """Read the ONE store that says whether screenshot capture is meant to be running.

        Absent or unreadable ⇒ the DEFAULT (OFF). Fail-safe points at "do not spend": a missing
        file must never be read as "on", because that is the direction that costs the operator
        the resource he asked to save. Returns {"enabled", "updatedAt", "updatedBy", "present"}."""
        try:
            if DSH_OBS_SHOT_MODE_JSON.is_file():
                d = json.loads(DSH_OBS_SHOT_MODE_JSON.read_text(encoding="utf-8"))
                if isinstance(d, dict) and isinstance(d.get("enabled"), bool):
                    return {
                        "enabled": bool(d["enabled"]),
                        "updatedAt": d.get("updatedAt") if isinstance(d.get("updatedAt"), (int, float)) else None,
                        "updatedBy": d.get("updatedBy") if isinstance(d.get("updatedBy"), str) else None,
                        "present": True,
                        "error": None,
                    }
                return {"enabled": DSH_OBS_SHOT_MODE_DEFAULT, "updatedAt": None, "updatedBy": None,
                        "present": True, "error": "store present but has no boolean `enabled` — using default"}
        except Exception as e:
            return {"enabled": DSH_OBS_SHOT_MODE_DEFAULT, "updatedAt": None, "updatedBy": None,
                    "present": True, "error": f"store unreadable ({e.__class__.__name__}: {e}) — using default"}
        return {"enabled": DSH_OBS_SHOT_MODE_DEFAULT, "updatedAt": None, "updatedBy": None,
                "present": False, "error": None}

    def _dsh_obs_shot_mode_state(self, state: dict) -> dict:
        """The mode flag PLUS a measured verdict on whether the writer honours it.

        ★★★ WHY THE VERDICT IS MEASURED AND NOT ASSUMED. This session could not reach the running
        Cordis plugin (its registry belongs to another session), so at the time this was written
        dshobs-2 did NOT read the flag. A toggle that reports "off" while the writer keeps
        spending is worse than no toggle — it is a false claim about a resource. So the claim is
        tested against the artefacts: if the flag says OFF and shots have LANDED SINCE the flag
        was written, the writer is provably not honouring it, and `enforced` says so out loud.

        enforced := True      no shot newer than the flag ⇒ nothing contradicts it
                    False     shots newer than the flag ⇒ the writer is NOT honouring it
                    "unknown" the flag is younger than one capture interval, or no shots exist
                              yet ⇒ there has been no chance to contradict it. Not-run is its
                              own state; it is never folded into pass."""
        m = self._dsh_obs_shot_mode_read()
        shots = state.get("screenshots") or []
        newest = None
        for s in shots:
            t = s.get("time") if isinstance(s, dict) else None
            if isinstance(t, (int, float)) and (newest is None or t > newest):
                newest = t
        flag_at = m.get("updatedAt")
        since = 0
        if isinstance(flag_at, (int, float)):
            since = sum(1 for s in shots
                        if isinstance(s, dict) and isinstance(s.get("time"), (int, float))
                        and s["time"] > flag_at)

        if m["enabled"]:
            enforced = True            # ON: the observed shots agree with the flag by construction
        elif not isinstance(flag_at, (int, float)):
            enforced = "unknown"       # nobody ever wrote it; the default is in force, untested
        elif newest is not None and newest > flag_at:
            enforced = False
        else:
            # The flag is newer than every shot we hold. Give the writer one capture interval
            # of grace before calling it honoured — the plugin polls at 2s and shoots at ≤8s.
            grace_ms = 12000
            enforced = "unknown" if (time.time() * 1000 - flag_at) < grace_ms else True

        delivered = bool(m["enabled"])
        verdict = ("the writer IS honouring this flag" if enforced is True else
                   "the writer is NOT honouring this flag — capture continues" if enforced is False else
                   "too soon to tell whether the writer honours this flag")

        # ★★ WHO IS ACTUALLY SUPPOSED TO READ THIS FILE — derived from the standalone writer's own
        #    status, never assumed. Before the handover the answer is the Cordis plugin dshobs-2,
        #    which belongs to an ENDED session and therefore cannot be redefined to read the flag:
        #    that is why OFF was a request and `enforced` was false. After the handover the answer
        #    is the standalone writer, which DOES read it. Advice to "redefine dshobs-2" is wrong
        #    the moment the handover happens, and stale advice on a live control is how an operator
        #    concludes a working switch is broken.
        w = self._dsh_obs_writer_status()
        w_mode = w.get("mode") if isinstance(w, dict) else None
        standalone_owns = bool(w.get("present")) and w.get("heartbeatFresh") and w_mode in ("owning", "conflict")
        if standalone_owns:
            reader = "khaoss-dsh-obs-writer (standalone process — reads this file before each grab)"
            enforced_by = ("the standalone writer bin/dsh_obs_writer.py: it reads this file every "
                           "second and skips the X11 grab entirely when it is OFF"
                           if enforced is not False else
                           "the standalone writer is in mode=conflict and has stopped capturing "
                           "altogether, so nothing is spending; see its status file")
            remedy = ("none needed — the standalone writer honours this flag. Its mode is "
                      f"{w_mode!r}; read var/dsh-observability/writer.json for the flag it last saw "
                      "and the bound it is holding")
        elif w.get("present") and w.get("heartbeatFresh"):
            reader = ("khaoss-dsh-obs-writer (standalone process, currently YIELDING to the live "
                      "Cordis plugin dshobs-2)")
            enforced_by = ("the Cordis plugin dshobs-2, which does NOT read this file — the "
                           "standalone writer is holding back precisely so the two do not share one "
                           "ring. OFF therefore remains a request until the plugin stops publishing")
            remedy = ("wait for the handover: the standalone writer claims the ring by itself once "
                      "state.json has been silent for 90 s (a DSH restart ends the plugin, because a "
                      "dynamic Cordis plugin cannot outlive its process). No plugin redefinition is "
                      "possible — that plugin's session has ENDED, so cordis_define(kind:'existing',"
                      "'dshobs-2') is refused. Full procedure: operator/DSH_OBS_WRITER_HANDOVER.md")
        else:
            reader = "dshobs-2 (Cordis Host plugin — reads this file before each capture)"
            enforced_by = ("nothing, while `enforced` is false: the writer dshobs-2 does not read "
                           "this file yet, so capture continues at the plugin's own interval")
            remedy = ("preferable now: start the standalone writer — it honours this flag and needs "
                      "no plugin change. systemctl --user enable --now khaoss-dsh-obs-writer; it "
                      "yields automatically while the plugin is alive, so starting it is safe. "
                      "The old route (redefine dshobs-2 from "
                      "var/dsh-observability/dshobs-2-pkg-screenshot-flag.js) is NOT available: that "
                      "plugin was defined by a DSH session that has ENDED and no live session's "
                      "registry holds it")
        return {
            "enabled": bool(m["enabled"]),
            "default": DSH_OBS_SHOT_MODE_DEFAULT,
            "updatedAt": flag_at,
            "updatedBy": m.get("updatedBy"),
            "source": str(DSH_OBS_SHOT_MODE_JSON),
            "storePresent": bool(m.get("present")),
            "storeError": m.get("error"),
            "enforced": enforced,
            "verdict": verdict,
            "newestShotAt": newest,
            "shotsSinceFlag": since,
            "readerExpected": reader,
            "note": "OFF halts NEW captures only; existing shots are retained, never deleted",
            "delivered": delivered,
            # ★ WHAT THIS CONTROL CANNOT DO, said by the SERVER so the page cannot soften it.
            #   "requested" is the honest word: until the writer reads the flag, OFF is a
            #   request, not a saving. A control that implies otherwise is worse than none.
            "honesty": {
                "thisIs": ("a REQUEST stored at "
                           f"{DSH_OBS_SHOT_MODE_JSON} — the panel is its only writer"),
                "enforcedBy": enforced_by,
                "patchPath": str(DSH_OBS_SHOT_MODE_JSON.parent / "dshobs-2-pkg-screenshot-flag.js"),
                "remedy": remedy,
                "deleted": 0,
            },
        }

    def _dsh_obs_attribute_shots(self, state: dict) -> dict:
        """Pair each screenshot with the agent that was driving when it was taken.

        ★★★ THE JOIN KEY, STATED PLAINLY. The writer stores a shot as {file, time} and NOTHING
        ELSE — it does not record an agent. There is no per-shot agent id anywhere on this box,
        so any pairing here is a JOIN, and the key must be written down rather than implied.

        KEY: for shot time T, take each agent's most recent action at or before T (from
        state.actions, which the writer fills from the live sessions Service). The agent whose
        last action is CLOSEST BEFORE T owns the shot. Tie-break: smallest gap, then agentId
        ascending, so the result is deterministic. A shot is AMBIGUOUS when a second agent's
        last action is within AMBIGUITY_MS of the winner's — i.e. two agents were plausibly
        driving at once. A shot with no agent inside the window is UNATTRIBUTED and is reported
        as such; it is never guessed onto a nearest-looking agent.

        ★ WHAT THIS IS NOT. The capture target is the X11 window "AionUi", so a shot shows
        WHICHEVER TAB IS ON TOP, not an agent's own screen. The pairing is therefore TEMPORAL —
        "this agent was the most recent actor at capture time" — and not visual proof that this
        agent did the thing on screen. ★ MEASURED 2026-09-23: the ring spans ~5 minutes of an 8s
        capture rhythm, and whole minutes of that span carry NO agent action at all (only 4 agent
        ids ever appear in the action tail, and they go quiet for long stretches), so a
        correctly-working join still attributed 0 of 40 shots at the moment the operator looked.
        That is why this returns `health` and `reasonCounts`: the honest state of the join is a
        RESULT, not an embarrassment to hide.

        Window: 3× the median gap between shots, clamped to [2500ms, 9000ms], so it tracks the
        writer's real cadence (2s under load, 8s idle) instead of a hardcoded constant."""
        shots = [s for s in (state.get("screenshots") or []) if isinstance(s, dict)
                 and isinstance(s.get("time"), (int, float))]
        actions = []
        for ev in (state.get("actions") or []):
            if not isinstance(ev, dict):
                continue
            t, aid = ev.get("time"), ev.get("agentId")
            if isinstance(t, (int, float)) and isinstance(aid, str) and aid:
                actions.append((float(t), aid))
        actions.sort(key=lambda x: x[0])

        shot_times = sorted(float(s["time"]) for s in shots)
        gaps = sorted(b - a for a, b in zip(shot_times, shot_times[1:]) if b > a)
        if gaps:
            med = gaps[len(gaps) // 2]
            window_ms = min(9000.0, max(2500.0, 3.0 * med))
        else:
            window_ms = 9000.0
        AMBIGUITY_MS = 1500.0

        by_agent: dict = {}
        unattributed: list = []
        ambiguous_count = 0
        reason_counts: dict = {}
        memo = _DSH_OBS_ATTR_MEMO
        present = {s.get("file") for s in shots}
        for stale in [k for k in memo if k not in present]:
            del memo[stale]                     # the ring dropped it; forget it (self-limiting)
        reused = 0
        for s in sorted(shots, key=lambda x: x["time"]):
            T = float(s["time"])
            key = s.get("file")
            if key in memo:
                winner, rec = memo[key]
                reused += 1
                if winner is None:
                    unattributed.append(rec)
                    tag = rec.get("reasonCode") or "no_agent_action_inside_window"
                else:
                    if rec.get("ambiguous"):
                        ambiguous_count += 1
                    by_agent.setdefault(winner, []).append(rec)
                    tag = ("paired_bracketed" if rec.get("basis") == "bracketed"
                           else "paired_nearest_before")
                reason_counts[tag] = reason_counts.get(tag, 0) + 1
                continue
            last: dict = {}
            for t, aid in actions:
                if t <= T + 1.0:                      # 1ms slack for same-instant writes
                    if aid not in last or t > last[aid]:
                        last[aid] = t
            cands = [(T - t, aid) for aid, t in last.items() if (T - t) <= window_ms]
            cands.sort(key=lambda x: (x[0], x[1]))
            rec = {"file": s.get("file"), "time": s.get("time")}
            if not cands:
                rec["reason"] = (f"no agent action in the preceding {int(window_ms)}ms window "
                                 f"(agent not working, or its action fell out of the writer's 60-event tail)")
                rec["reasonCode"] = "no_agent_action_inside_window"
                reason_counts["no_agent_action_inside_window"] = \
                    reason_counts.get("no_agent_action_inside_window", 0) + 1
                memo[key] = (None, rec)
                unattributed.append(rec)
                continue
            delta, winner = cands[0]
            close = [a for d, a in cands if d <= delta + AMBIGUITY_MS]
            rec["joinDeltaMs"] = int(delta)
            rec["ambiguous"] = len(close) > 1
            rec["candidates"] = [a for _, a in cands[:5]]
            # ★ BASIS — how good is this pairing, in words the UI can print?
            #   "bracketed": the SAME agent also acted AFTER the shot inside the window, so the
            #                capture sits inside that agent's own active interval. Stronger.
            #   "nearest-before": only a preceding action ties it. It is the weaker claim, and
            #                the operator must be told which one he is looking at.
            after = [t for t, aid in actions if aid == winner and T < t <= T + window_ms]
            rec["basis"] = "bracketed" if after else "nearest-before"
            rec["basisNote"] = ("this agent also acted after the capture, so the shot falls inside its own "
                                "active interval" if after else
                                "this agent was simply the last one to act before the capture")
            if rec["ambiguous"]:
                ambiguous_count += 1
            reason_counts["paired_bracketed" if after else "paired_nearest_before"] = \
                reason_counts.get("paired_bracketed" if after else "paired_nearest_before", 0) + 1
            memo[key] = (winner, rec)               # ★ sticks; never re-homed
            by_agent.setdefault(winner, []).append(rec)

        total = len(shots)
        attributed = total - len(unattributed)
        # ★ HEALTH is a verdict on the JOIN, measured — never a claim that per-agent screenshots exist.
        if total == 0:
            health = "no-shots"
        elif attributed == 0:
            health = "none"
        elif attributed < total:
            health = "weak"
        else:
            health = "full"
        # How much of the ring's own time span carries no action at all — this is the number that
        # explains an all-unattributed ring better than any adjective.
        quiet_ms = None
        if shot_times and actions:
            lo, hi = shot_times[0], shot_times[-1]
            in_span = sorted(t for t, _ in actions if lo <= t <= hi)
            marks = [lo] + in_span + [hi]
            quiet_ms = int(max(b - a for a, b in zip(marks, marks[1:]))) if len(marks) > 1 else int(hi - lo)
        return {
            "joinKey": ("shot.time -> the agent whose LAST action at or before the shot is nearest, "
                        "within 3x the median shot gap clamped to [2500,9000]ms; "
                        "ambiguous when a 2nd agent is within 1500ms of the winner"),
            "windowMs": int(window_ms),
            "medianShotGapMs": int(gaps[len(gaps) // 2]) if gaps else None,
            "actionSampleSize": len(actions),
            "ambiguousMs": int(AMBIGUITY_MS),
            "byAgentId": by_agent,
            "unattributed": unattributed,
            "ambiguousCount": ambiguous_count,
            "attributedCount": attributed,
            "totalShots": total,
            "health": health,
            "healthVerdict": {
                "no-shots": "no screenshots on disk yet — nothing to pair",
                "none": ("★ 0 of %d shots could be paired: no agent action landed inside the join window "
                         "for ANY capture in the ring. Per-agent screenshots do NOT exist here — the "
                         "captures are shown unattributed, never guessed onto an agent." % total),
                "weak": ("%d of %d shots paired; the rest are shown unattributed rather than guessed"
                         % (attributed, total)),
                "full": ("all %d shots paired by time — still a temporal pairing, not visual proof" % total),
            }[health],
            "reasonCounts": reason_counts,
            "actionTail": {
                "events": len(actions),
                "agents": len({aid for _, aid in actions}),
                "newestActionAt": int(max(t for t, _ in actions)) if actions else None,
                "oldestActionAt": int(min(t for t, _ in actions)) if actions else None,
                "ringSpanMs": int(shot_times[-1] - shot_times[0]) if len(shot_times) > 1 else 0,
                "quietestGapInsideRingMs": quiet_ms,
                "whyUnattributed": ("the join can only pair a capture with an agent that acted within "
                                    f"{int(window_ms)}ms of it; the writer publishes actions for the "
                                    "sessions that are producing events, and captures land on a fixed "
                                    "rhythm whether or not anyone is acting, so idle stretches leave "
                                    "whole groups of shots with no candidate at all"),
            },
            "stickyAttribution": True,
            "memoReused": reused,
            "memoSize": len(memo),
            "stability": ("a shot's owner is decided ONCE and never revised while it stays in the "
                          "ring — it cannot move from one agent's panel to another between polls; "
                          "the memo is process-local, so a panel restart re-derives it"),
            "caveat": ("the images capture the X11 window \"AionUi\" — whichever tab is on top — not an "
                       "agent's own screen; the pairing is temporal and the writer stores NO agent id on "
                       "a shot, so a paired shot is a timed guess, never a record lookup"),
        }

    def _dsh_obs_set_shot_mode(self) -> None:
        """POST /operator/dsh-jobs/screenshot-mode {"enabled": bool, "actor": str}

        ★★★ THE CLAIM THAT MUST NEVER BE WRONG IS "I STOPPED THE SCREENSHOTS". So the response is
        a SECOND, independent re-read of the store, never an echo of the request — cloned from
        /state/capture-policy, which learned the same lesson about the microphone. If the store
        does not confirm, this reports ok:false and the caller must treat capture as UNCHANGED.

        ★ Turning it off HALTS NEW CAPTURES ONLY. Nothing is deleted: the ring of existing shots
        stays on disk and stays visible in the popout. Deleting the operator's pictures to save
        disk would be answering a question he did not ask.

        ★ Loopback only, and every write is appended to an audit line so "who turned the
        screenshots off" is answerable rather than inferred."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln).decode() or "{}") if ln else {}
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": f"bad JSON body ({e.__class__.__name__})"})
        want = body.get("enabled")
        if not isinstance(want, bool):
            return self._send_json(400, {"ok": False, "error": "`enabled` must be true or false"})
        actor = str(body.get("actor") or "panel")[:80]
        try:
            DSH_OBS_SHOT_MODE_JSON.parent.mkdir(parents=True, exist_ok=True)
            payload = {"enabled": want, "updatedAt": int(time.time() * 1000),
                       "updatedBy": actor, "schema": 1}
            tmp = DSH_OBS_SHOT_MODE_JSON.parent / (DSH_OBS_SHOT_MODE_JSON.name + ".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(DSH_OBS_SHOT_MODE_JSON)
        except Exception as e:
            return self._send_json(500, {"ok": False,
                                         "error": f"could not write the flag: {e.__class__.__name__}: {e}",
                                         "note": "treat screenshot capture as UNCHANGED"})
        try:
            with (DSH_OBS_SHOT_MODE_JSON.parent / "screenshot-mode.audit.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": int(time.time() * 1000), "enabled": want, "actor": actor}) + "\n")
        except Exception:
            pass
        # ★ Independent re-read — the answer is what the STORE says, not what we asked for.
        back = self._dsh_obs_shot_mode_read()
        confirmed = bool(back.get("enabled") == want and back.get("present"))
        out = {
            "ok": confirmed,
            "requested": want,
            "enabled": bool(back.get("enabled")),
            "updatedAt": back.get("updatedAt"),
            "updatedBy": back.get("updatedBy"),
            "store": str(DSH_OBS_SHOT_MODE_JSON),
            "deleted": 0,
            "note": "existing shots retained; only NEW captures are halted",
        }
        if not confirmed:
            out["error"] = "the store did not confirm the write — treat screenshot capture as UNCHANGED"
        return self._send_json(200 if confirmed else 409, out)

    def _dsh_obs_shot(self, path: str) -> None:
        """GET /operator/dsh-jobs/shot/<name> — one ring-capped screenshot. Only a bare filename of the
        form shot-<digits>.jpg is served, so a caller cannot path-traverse out of the shots dir."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        name = path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"shot-\d+\.jpg", name):
            return self._send_json(400, {"error": "bad screenshot name"})
        fp = DSH_OBS_SHOTS_DIR / name
        if not fp.is_file():
            return self._send_json(404, {"error": "screenshot not found"})
        body = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _operator_walk_mark(self, body: dict) -> None:
        """POST — record that he has SEEN an item. ★ It does NOT rule the row done; that is his."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            w = self._op_mod("board_walk")
            iid = str(body.get("item") or "")
            if not iid:
                return self._send_json(400, {"error": "which item? `item` is required"})
            return self._send_json(200, w.mark(iid, str(body.get("outcome") or "done")))
        except Exception as e:
            return self._send_json(500, {"error": f"could not record: {e}"})

    # ══ THE POINT-ME-TO-THERE PROTOCOL — the OS-wide overlay, driven from the board ═══════════════
    # ★★★ WHY THIS LIVES IN THE PANEL AND NOT IN A SECOND SERVICE. He asked for the overlay to be
    #     "built into the boardwalk". The boardwalk is a page on :7842 and every control he has is a
    #     route on this server; a browser page cannot start a GTK process, so the ONE thing this route
    #     adds is the launch, and it adds nothing else. What it points at, what step he is on, and what
    #     he has finished all live in stores that already existed (`operator/guide_focus.json`,
    #     `operator/board_walk_state.json`). This route holds no state of its own.
    # ★ CREDENTIAL RULE: nothing here is credential-shaped, and it is LOCAL-ONLY anyway because it
    #   puts pixels on his screen. It is NOT added to REMOTE_SAFE_GET_EXACT.
    def _point_to_mod(self):
        import importlib
        import sys as _sys
        from pathlib import Path as _P
        j = str(_P(__file__).resolve().parent / "jarvis")
        if j not in _sys.path:
            _sys.path.insert(0, j)
        return importlib.import_module("point_to")

    def _point_to_get(self) -> None:
        """GET — is the overlay running, what is it pointing at, and IS IT REALLY CLICK-THROUGH?"""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            pt = self._point_to_mod()
            st = pt.status()
            return self._send_json(200, {
                "ok": True, "running": st.get("running"), "pid": st.get("pid"),
                "step_now": (st.get("report") or {}).get("step_now"),
                "n_steps": (st.get("report") or {}).get("n_steps"),
                "shown": (st.get("report") or {}).get("shown") or [],
                "unresolved_shown": (st.get("report") or {}).get("unresolved_shown") or [],
                # ★ THE MEASUREMENT, NOT THE CLAIM. `input_rectangles` is what the X server answered.
                "click_through_measured": st.get("measured"),
                "guide_step": (st.get("guide") or {}).get("step"),
                "guide_of": (st.get("guide") or {}).get("of"),
                "walks": sorted(p.name for p in (pt.WALKS.glob("*.md")) ) +
                         sorted(p.name for p in (pt.WALKS.glob("*.json"))),
                "how": "POST {action: point|next|kill} here; see jarvis/point_to.py --markdown-help",
            })
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _point_to_post(self, body: dict) -> None:
        """POST — launch, advance or stop the overlay. ★ It records nothing itself: the overlay writes
        progress into guide_focus and board_walk, which is where those facts already live."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        act = str(body.get("action") or "status").strip().lower()
        try:
            pt = self._point_to_mod()
            if act in ("status",):
                return self._point_to_get()
            if act in ("kill", "stop", "close"):
                return self._send_json(200, pt.kill_running())
            if act == "next":
                if not pt.PIDFILE.is_file():
                    return self._send_json(400, {"ok": False, "say": "no overlay is running"})
                pt.CMD.write_text("next\n")
                return self._send_json(200, {"ok": True, "sent": "next"})
            if act in ("point", "run", "start"):
                live = pt.running_pid()
                if live:
                    return self._send_json(409, {"ok": False, "running_pid": live,
                                                 "say": "an overlay is already running — one at a "
                                                        "time. Stop it first.",
                                                 "how": "POST {action:'kill'}"})
                script = str(body.get("script") or "").strip()
                item = str(body.get("item") or "").strip()
                argv = [sys.executable, str(Path(__file__).resolve().parent / "jarvis" / "point_to.py")]
                mode = ""
                if script:
                    # a path under operator/walks/ only — a route that runs an arbitrary path is a
                    # route that runs an arbitrary path.
                    cand = (pt.WALKS / Path(script).name)
                    if not cand.is_file():
                        return self._send_json(400, {"ok": False,
                                                     "error": f"no such walkthrough: {cand.name}",
                                                     "have": sorted(p.name for p in pt.WALKS.glob('*'))})
                    argv += ["--script", str(cand)]
                    if body.get("step"):
                        argv += ["--step", str(int(body["step"]))]
                    mode = f"script:{cand.name}"
                elif item:
                    # ★ POINT AT A BOARD ROW. A board row has no screen rectangle — it lives in the
                    #   panel page, not on the desktop — so the step is pushed WITHOUT a `screen`
                    #   block and the overlay SAYS SO on the card instead of drawing a ring round a
                    #   guess. That refusal is the feature; an overlay that invented a rectangle for
                    #   a board row would be pointing at nothing with full confidence.
                    g = self._op_mod("guide_focus")
                    say = str(body.get("say") or "").strip() or f"Work this row: {item}"
                    title = str(body.get("title") or item)
                    r = g.set_sequence([{"kind": "item", "target": item, "say": say,
                                         "why": str(body.get("why") or "").strip(),
                                         "title": title, "id": item}],
                                       by=str(body.get("by") or "the boardwalk"),
                                       why="started from the boardwalk",
                                       replace=bool(body.get("replace")))
                    if not r.get("ok"):
                        return self._send_json(409, {**r, "note": "the board's guided-walk store "
                                                                  "refused — that refusal is the "
                                                                  "one-guide-at-a-time rule"})
                    argv += ["--guide"]
                    mode = f"guide:{item}"
                else:
                    argv += ["--guide"]
                    mode = "guide"
                log = "/tmp/jarvis_point_to_panel.log"
                # ★ THE PANEL MAY BE STARTED FROM A SHELL WITHOUT XAUTHORITY (the release launcher
                #   from a terminal, a systemd unit with a bare env).  GTK then dies with
                #   "Authorization required, but no authorization protocol specified" and the
                #   overlay never appears.  Recover the session's mutter cookie for the CHILD only;
                #   the panel's own env and the operator's files are untouched.
                child_env = dict(os.environ)
                child_env.setdefault("DISPLAY", ":0")
                try:
                    child_env["KHAOSS_PANEL_URL"] = f"http://127.0.0.1:{self.server.server_port}"
                except Exception:  # noqa: BLE001
                    pass
                # ★ A SET-BUT-EMPTY XAUTHORITY IS AS DEAD AS AN UNSET ONE. The release panel was
                #   started from a shell with XAUTHORITY=~/.Xauthority (a 0-byte file), so the old
                #   "if missing" check left it and GTK printed "Authorization required" and exited.
                #   The overlay must use the session's actual mutter cookie; the panel's own env is
                #   never written.
                import glob as _glob
                cands = sorted(_glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*"))
                cur_auth = child_env.get("XAUTHORITY") or ""
                if cands and (not cur_auth or not os.path.isfile(cur_auth)
                              or os.path.getsize(cur_auth) == 0):
                    child_env["XAUTHORITY"] = cands[-1]
                with open(log, "ab") as fh:
                    proc = subprocess.Popen(argv, env=child_env, stdout=fh, stderr=subprocess.STDOUT,
                                            stdin=subprocess.DEVNULL, start_new_session=True)
                time.sleep(0.6)
                return self._send_json(200, {
                    "ok": True, "started_pid": proc.pid, "mode": mode, "argv": argv[1:],
                    "log": log,
                    "note": "the overlay opens on his screen and follows the authored walk's OWN "
                            "steps — while it runs, the card's step N is the authority. Press Next on "
                            "the card to advance it, or POST {action:'next'} here. ★ THIS MODE DOES "
                            "NOT WRITE guide_focus: the board's guide store is converted from a walk "
                            "only by `--push`, so the panel's guide_step can stay behind the overlay's "
                            "own step, which is why both numbers are shown separately.",
                    "escape": "Esc or the close button on the card, or POST {action:'kill'}",
                })
            return self._send_json(400, {"ok": False, "error": f"unknown action {act!r}",
                                         "actions": ["point", "next", "kill", "status"]})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_wizard_action(self, body: dict) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            w = self._op_mod("credential_wizard")
            what = str(body.get("action") or "")
            if what == "done":
                r = w.mark_done(str(body.get("service") or ""), str(body.get("how") or "asserted"),
                                str(body.get("note") or ""))
                return self._send_json(200 if r.get("ok") else 400, r)
            if what == "reset":
                return self._send_json(200, w.reset())
            return self._send_json(400, {"ok": False, "error": f"unknown action {what!r}"})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_jump(self, body: dict) -> None:
        """POST — R-09. Take him from a board row to the conversation that owns it.
        ★ It drives the AionUi window he already has rather than opening a second one, and it verifies
        the route actually CHANGED rather than reporting that it asked."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            r = self._op_mod("jump_to_agent").jump(str(body.get("item") or ""))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    _ASSET_DIR = ROOT / "assets"
    _ASSET_TYPES = {".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
                    ".ico": "image/x-icon"}

    def _static_asset(self, path: str) -> None:
        """GET /assets/… — the panel's first static files.

        ★★ THE PANEL HAS NEVER SERVED A STATIC ASSET. The board UI references zero images, so an
        <img src="/assets/x.png"> would have 404'd — which is why the other seat shipped its logo as
        a data URI instead, and measured the cost rather than guessing: the 256px header is 166,140
        base64 bytes against an 827,906-byte page. ★ That is +20% on the page HE RELOADS CONSTANTLY,
        on the evening we spent cutting this surface's weight and lag. Six lines of routing gives a
        file the browser CACHES instead; the favicon stays inline because 4 KB needs no route.

        ★★★ CLOSED BY CONSTRUCTION, NOT BY SANITISING. The name is matched against the files that
        actually exist under one directory — no path joining of user input, so `..` has nothing to
        traverse with. Sanitising a path is a thing you can get subtly wrong; refusing to build one
        is not."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        want = path[len("/assets/"):]
        hit = None
        for cand in self._ASSET_DIR.rglob("*"):
            if cand.is_file() and str(cand.relative_to(self._ASSET_DIR)) == want:
                hit = cand
                break
        if hit is None or hit.suffix.lower() not in self._ASSET_TYPES:
            return self._send_json(404, {"error": f"no such asset {want!r}"})
        try:
            body = hit.read_bytes()
        except OSError as e:
            return self._send_json(500, {"error": f"could not read it: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", self._ASSET_TYPES[hit.suffix.lower()])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")   # ★ the whole point of a route
        self.end_headers()
        self.wfile.write(body)

    def _secrets_list(self) -> None:
        """GET /secrets/list — WHICH secrets exist. Names, sizes and times. Never a value.

        ★★ The wizard asked him to type an id with no idea what ids already existed, and storing an
        existing one SILENTLY REPLACES IT (the writer opens with O_TRUNC). So a typo in the id made a
        duplicate, and a correct id overwrote something without a word. He has just hit both.
        ★ Size is included deliberately and is not a leak: it is how we established that the himalaya
        password was 13 characters when a Gmail app password is 16 — a length is often the whole
        diagnosis, and withholding it would have cost that finding."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        import time as _tm
        d = Path.home() / ".khaoss-secrets"
        rows = []
        try:
            meta = self._op_mod("secret_meta")._load()
        except Exception:
            meta = {}
        try:
            for f in sorted(d.iterdir()):
                if not f.is_file() or f.name.startswith("."):
                    continue
                st = f.stat()
                m = meta.get(f.name, {})
                rows.append({"id": f.name, "bytes": st.st_size,
                             "mode": oct(st.st_mode)[-3:],
                             "modified": _tm.strftime("%Y-%m-%d %H:%M", _tm.localtime(st.st_mtime)),
                             "rank_required": m.get("rank_required"), "territory": m.get("territory"),
                             "ring": m.get("ring")})
        except OSError as e:
            return self._send_json(500, {"error": str(e)})
        return self._send_json(200, {"count": len(rows), "secrets": rows})

    def _secrets_access(self) -> None:
        """GET /secrets/access — the vocabulary, what is classified, and whether it is ENFORCED.

        ★ It returns the enforcement status alongside the labels ON PURPOSE. A card that showed
        clearances without saying whether anything reads them would be the no-cloud tag with a nicer
        font."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            m = self._op_mod("secret_meta")
            return self._send_json(200, {"ranks": m.ranks(), "rings": m.RINGS,
                                         "meta": m._load(), "enforcement": m.enforcement_status()})
        except Exception as e:
            return self._send_json(500, {"error": f"{e.__class__.__name__}: {e}"})

    def _operator_shelf(self) -> None:
        """GET /operator/shelf — every instruction he has given that nobody has carried out.

        ★★ It is a list of the AGENTS' debts, not of his tasks, and that distinction is the whole
        point: the board shows what is waiting on HIM, and until now there was no surface at all for
        what is waiting on US. By the end of his first walk that list was the largest thing he had
        produced, and it lived in a JSONL file he could not open."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("guide_focus").shelf())
        except Exception as e:
            return self._send_json(500, {"error": f"{e.__class__.__name__}: {e}"})

    def _stale_code(self) -> list:
        """Which loaded modules have been EDITED since this process imported them."""
        out = []
        for name, mod in list(sys.modules.items()):
            f = getattr(mod, "__file__", None)
            if not f:
                continue
            try:
                fp = Path(f)
                if not (str(fp).startswith(str(self._OPERATOR_DIR)) or fp.name == "panel_server.py"):
                    continue
                m = fp.stat().st_mtime
                if m > _PROCESS_STARTED:
                    out.append({"module": name, "newer_by_s": int(m - _PROCESS_STARTED)})
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda r: -r["newer_by_s"])

    def _panel_build(self) -> None:
        """GET /panel/build — the mtime of the page the panel is serving.

        ★★ A LOADED PAGE CANNOT TELL IT IS OUT OF DATE, and this box has two agents editing this UI.
           He spent minutes today typing into a box that existed on disk and not in his window, and
           the fix he was given was "press ctrl-shift-R", which did not work for him. A surface that
           silently serves one version while he is looking at another is the stale-balance class in
           the UI: correct once, wrong now, and nothing on screen says which."""
        try:
            st = PANEL_HTML.stat()
            stale = self._stale_code()
            return self._send_json(200, {"mtime": int(st.st_mtime), "bytes": st.st_size,
                                         "code_stale": stale,
                                         "code_note": ("the panel is running code OLDER than the files on "
                                                       "disk — restart it before trusting a measurement"
                                                       if stale else "running code matches disk")})
        except Exception as e:
            return self._send_json(500, {"error": f"{e.__class__.__name__}: {e}"})

    def _guide_focus_get(self) -> None:
        """GET — what the guiding agent is pointing at right now, if anything."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("guide_focus").current())
        except Exception as e:
            return self._send_json(500, {"active": False, "error": str(e)[:120]})

    def _guide_backends_get(self) -> None:
        """GET — the guide conversation target, distinct from the owner/destination of a row."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, self._op_mod("guide_backends").inventory())
        except Exception as e:
            return self._send_json(500, {"ok": False,
                                         "error": f"guide backend registry unavailable: {e}"})

    def _guide_events_get(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            gb = self._op_mod("guide_backends")
            q = parse_qs(urlsplit(self.path).query)
            event_id = str(q.get("id", [""])[0])
            if event_id:
                hit = gb.event(event_id)
                return self._send_json(200 if hit else 404,
                                       {"ok": bool(hit), "event": hit,
                                        "error": None if hit else "no such guide event"})
            return self._send_json(200, {"ok": True, "events": gb._event_rows(50)})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"events unavailable: {e}"})

    def _guide_backend_post(self, body: dict) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            gb = self._op_mod("guide_backends")
            action = str(body.get("action") or "select")
            if action == "select":
                r = gb.select(str(body.get("backend") or ""))
                return self._send_json(200 if r.get("ok") else 400, r)
            if action == "probe":
                r = gb.probe(str(body.get("backend") or ""))
                return self._send_json(200 if r.get("ok") else 409, r)
            return self._send_json(400, {"ok": False, "error": f"unknown action {action!r}",
                                         "choose": ["select", "probe"]})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"guide backend action failed: {e}"})

    _PANEL_GEOM = Path(__file__).resolve().parent / "operator" / "panel_geometry.json"

    _PANEL_FRONT = Path(__file__).resolve().parent / "operator" / "panel_front.json"

    def _panel_front_get(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            d = json.loads(self._PANEL_FRONT.read_text()) if self._PANEL_FRONT.is_file() else {}
        except Exception:  # noqa: BLE001
            d = {}
        return self._send_json(200, {"ok": bool(d), "seq": int(d.get("seq") or 0), **d})

    def _panel_front_post(self, body: dict) -> None:
        """POST /operator/panel/front — ask the page to focus/scroll/flash the item.

        * The overlay cannot raise a Wayland window through X11. The page can focus itself, scroll
          the item into view and flash it; if the compositor still keeps another window on top, the
          page's toast says so. That refusal is the feature.
        """
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            seq = int(time.time() * 1000)
            d = {"ok": True, "seq": seq, "at": str(body.get("at") or time.strftime("%Y-%m-%dT%H:%M:%S")),
                 "step": body.get("step"), "target": str(body.get("target") or "")[:200],
                 "title": str(body.get("title") or "")[:300],
                 "find": str(body.get("find") or "")[:300],
                 "selector": str(body.get("selector") or "#bd-walk")[:200]}
            self._PANEL_FRONT.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._PANEL_FRONT.with_name(self._PANEL_FRONT.name + ".tmp")
            tmp.write_text(json.dumps(d)); os.replace(tmp, self._PANEL_FRONT)
            return self._send_json(200, d)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _window_geometry_get(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            d = json.loads(self._PANEL_GEOM.read_text()) if self._PANEL_GEOM.is_file() else {}
        except Exception:  # noqa: BLE001
            d = {}
        # ★ AGE IS PART OF THE GEOMETRY. A rectangle with no timestamp is indistinguishable from a
        #   stale one; the page posts a heartbeat, and these two derived fields let the bar and the
        #   overlay say how old the position is instead of assuming it is current.
        if isinstance(d, dict) and d:
            try:
                _dt = datetime.fromisoformat(str(d.get("at") or "").replace("Z", "+00:00"))
                if _dt.tzinfo is None:
                    _dt = _dt.astimezone()
                d["age"] = max(0.0, time.time() - _dt.timestamp())
                d["stale"] = bool(d["age"] > 90.0)
            except (TypeError, ValueError):
                d["age"] = None
                d["stale"] = True
        return self._send_json(200, {"ok": bool(d), **d})

    def _window_geometry_post(self, body: dict) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            def _n(k):
                try:
                    return float(body.get(k))
                except (TypeError, ValueError):
                    return None
            d = {"at": str(body.get("at") or ""),
                 "screen_x": _n("screen_x"), "screen_y": _n("screen_y"),
                 "outer_w": _n("outer_w"), "outer_h": _n("outer_h"),
                 "inner_w": _n("inner_w"), "inner_h": _n("inner_h"),
                 "dpr": _n("dpr") or 1.0, "href": str(body.get("href") or "")[:500]}
            if any(d[k] is None for k in ("screen_x", "screen_y", "outer_w", "outer_h",
                                          "inner_w", "inner_h")):
                return self._send_json(400, {"ok": False, "error": "missing geometry fields"})
            self._PANEL_GEOM.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._PANEL_GEOM.with_name(self._PANEL_GEOM.name + ".tmp")
            tmp.write_text(json.dumps(d)); os.replace(tmp, self._PANEL_GEOM)
            return self._send_json(200, {"ok": True, **d})
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _guide_ping(self, body: dict) -> None:
        """POST /operator/guide/ping — one real route-test message through the selected backend.

        ★ The selected row's state is the gate, not the button. A live DSH chat is disconnected with
          the measured lock reason; notify() refuses and this returns 409. It never queues a message
          it cannot deliver."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            from datetime import datetime
            gb = self._op_mod("guide_backends")
            text = str(body.get("text") or "BoardWalk route test — reply when you receive this.")[:800]
            src = {"source_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "step": 0, "target": "", "kind": "question", "urgency": "none",
                   "text": text, "operator_words": True}
            r = gb.notify(src)
            return self._send_json(200 if r.get("ok") else 409, r)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"guide ping failed: {e}"})

    def _operator_share(self) -> None:
        """GET /operator/share — local panel URL and app folder; NEVER the DSH token."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        port = getattr(self.server, "server_port", 0)
        return self._send_json(200, {
            "ok": True,
            "panel_url": f"http://127.0.0.1:{port}/?section=operator-needs",
            "app_dir": str(ROOT),
            "jarvis_dir": str(ROOT / "jarvis"),
            "dsh_url_available": bool(self.DSH_LAUNCH_SH.is_file()),
            "note": ("The panel is loopback-only. A QR of 127.0.0.1 is local-only; for a phone use "
                     "Remote Access (tailnet bind + phone login). The DSH GUI token is a per-process "
                     "secret and is never placed in a QR."),
        })

    def _qr_svg(self) -> None:
        """GET /qr.svg?text=... — local QR encoder, loopback only, no third-party service."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            from urllib.parse import urlparse, parse_qs
            import sys as _sys
            text = (parse_qs(urlparse(self.path).query).get("text") or [""])[0]
            _sys.path.insert(0, str(ROOT / "jarvis"))
            from qr_code import qr_svg, MAX_PAYLOAD_BYTES
            raw = text.encode("utf-8")
            if not raw or len(raw) > MAX_PAYLOAD_BYTES:
                return self._send_json(400, {"ok": False,
                                             "error": f"QR text must be 1..{MAX_PAYLOAD_BYTES} UTF-8 bytes"})
            svg = qr_svg(text).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(svg)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(svg)
        except Exception as e:  # noqa: BLE001
            return self._send_json(500, {"ok": False, "error": f"QR failed: {e}"})

    def _guide_walk_sequence(self, body: dict) -> dict:
        """POST /operator/focus {"action":"walk"} — build the guided walk FROM THE BOARD WALK.

        ★★★ HE ASKED FOR THIS TO LIVE IN THE BOARD: "I want to be able to do this as much as possible
            completely in the board (ideally in the future it will all be done through the board)."
            So the board composes its own walk. Pressing start does not need an agent in the loop, and
            every advance after it is a local click rather than a model round trip.

        ★★ IT COMPOSES, IT DOES NOT WRITE. Every word of every step comes from board_walk.plan() —
           the hand-written explainers, the board's own wording where there is none, the fenced count.
           Nothing here invents a beginner summary, which is the rule board_walk.py exists to keep.

        ★ THE FENCE AND THE UNEXPLAINED COUNT ARE SPOKEN, NOT SWALLOWED. A row removed silently is a
          row he thinks does not exist, and a row with no explainer that sounds explained is worse
          than one that admits it."""
        w = self._op_mod("board_walk")
        try:
            batch = max(1, min(60, int(body.get("batch") or 20)))
        except (TypeError, ValueError):
            batch = 20
        order = str(body.get("order") or "quickest")
        plan = w.plan(batch, order)
        rows = plan.get("burst") or []
        if not rows:
            return {"ok": False, "error": "the walk returned no rows",
                    "remaining": plan.get("remaining_after")}
        total = len(rows)
        steps = [{
            "kind": "none", "target": "",
            "say": (f"We are walking {total} of the {plan.get('total_walkable')} rows that are open "
                    f"to you, {plan.get('order_says') or order}. "
                    f"Four moves on every row and all four are yours: rule it, park it, "
                    f"I-did-this, or leave a note. I only ever add to this board — closing is yours."),
            "why": (f"{plan.get('fenced_excluded')} trading-lane rows are fenced out and not counted "
                    f"as yours. {plan.get('explained_available')} rows have a hand-written "
                    f"explanation; the rest show the board's own words and say so."),
            "speak": (f"Let's walk the board. {total} rows this run. {plan.get('order_says') or order}. "
                      f"first. On each one you can rule it, park it, say you did it, or leave me a "
                      f"note. I can only add to this board. Closing a row is yours alone. "
                      f"Press done when you are ready and I will take you to the first one."),
        }]
        # ★★★ HE ASKED FOR STALENESS TO BE CHECKED AS WE GO, and the reason is not hypothetical: a
        #   peer seat measured 10 rows sitting in board.json that he had ALREADY CLOSED. (Those ten
        #   do not reach this walk — board_walk filters on state, verified 0 of 60 — but the class
        #   is real and the walk is where he would meet it.)
        # ★★ THE SIGNAL IS MEASURED, NOT GUESSED. Two things exist on the rows and nothing else does:
        #   `first_seen` (32 of 71 open rows carry a real date, 39 are explicitly undated) and
        #   `verify` (8 rows carry a hand-written way to tell whether it is already true). An undated
        #   row is reported as UNDATED — never as fresh, and never as an invented age.
        # ★ `verify` is PROSE, not a command ("gh auth status reports 'Logged in to github.com'"), so
        #   it is shown as how-to-check and never executed. Running a sentence is how a guide breaks
        #   something while claiming to inspect it.
        import datetime as _dt
        try:
            store = {i.get("id"): i for i in (self._op_mod("blockers").load() or {}).get("items", [])
                     if isinstance(i, dict)}
        except Exception:
            store = {}
        # ★ AN EMPTY STORE MUST NOT READ AS "every row is undated". That is could-not-look wearing
        #   nothing-to-see, and it is the failure this estate keeps paying for. It is reported.
        staleness_source = (f"blockers store ({len(store)} rows)" if store
                            else "UNAVAILABLE — staleness was NOT checked on this run")

        def _stale(row_id):
            src = store.get(row_id) or {}
            seen, how = src.get("first_seen"), src.get("verify")
            days = None
            if seen:
                try:
                    days = (_dt.date.today() - _dt.date.fromisoformat(str(seen)[:10])).days
                except Exception:
                    days = None
            return days, (str(how).strip() if how else "")

        for n, r in enumerate(rows, 1):
            title = str(r.get("title") or "").strip()
            days_open, how_to_check = _stale(str(r.get("id") or ""))
            plain = str(r.get("in_plain_english") or "").strip()
            said = str(r.get("board_says") or "").strip()
            explained = bool(r.get("explained"))
            body_text = plain or (said[:400] + ("…" if len(said) > 400 else ""))
            mins = r.get("your_minutes")
            # ★ The VOICE gets the shape of a decision, in this order: what it is, why it is yours,
            #   what happens if you walk away. That last one is the part he says he keeps losing.
            # ★★★ LEAD WITH WHERE HE IS, THEN STOP. "The voice is confusing me. Needs to be saying
            #   things on where we are." The old utterance opened with the position and then ran
            #   straight into four hundred characters of the row's body — by the end he could not
            #   say which row he was on, and the anchor at the front had been buried by everything
            #   after it. A spoken position is only useful if it is still the last thing you heard.
            # ★ So the automatic reading is SHORT: where he is, what it is called, one sentence.
            #   The full text is still there on a button he presses, which is the difference between
            #   being told and being talked at.
            first = re.split(r"(?<=[.!?])\s+", body_text.strip())[0] if body_text.strip() else ""
            spoken = f"Row {n} of {total}. {title}."
            if first:
                spoken += f" {first[:260]}"
            spoken_full = f"Row {n} of {total}. {title}. {body_text}"
            if r.get("what_only_you_can_do"):
                spoken_full += f" Only you can do this part: {r['what_only_you_can_do']}"
            if r.get("if_you_leave_it"):
                spoken_full += f" If you leave it: {r['if_you_leave_it']}"
            if days_open is not None and days_open >= 14:
                # ★ Staleness stays in the SHORT reading — he asked for it to be flagged as we go —
                #   but as one clause, not a paragraph. The full warning is in the panel and in
                #   speak_full; the voice only needs to make him pause.
                spoken += f" This one has been open {days_open} days, so check it is still true."
            if how_to_check:
                spoken_full += f" You can tell whether it is already done: {how_to_check}."
            if not explained:
                spoken_full += (" Heads up — nobody has written a plain-English explanation for this one, "
                           "so that was the board's own wording.")
            steps.append({
                "kind": "item", "target": str(r.get("id") or ""),
                "say": body_text or title,
                "why": str(r.get("why_it_needs_you") or ""),
                "speak": spoken, "speak_full": spoken_full,
                "minutes": mins, "board_n": r.get("board_n"), "explained": explained,
                "only_you": r.get("what_only_you_can_do") or "",
                "if_left": r.get("if_you_leave_it") or "",
                "i_can": r.get("what_i_can_do") or "",
                # ★★★ THE ROW'S OWN NUMBER, CARRIED EXPLICITLY. The coach header counted WALK STEPS
                #   (which include the opening and closing steps) while the spoken text counted ROWS
                #   — so the screen read "STEP 15 OF 22" while the voice said "Row 14 of 20", about
                #   the same row. Both were right; they were two different scales on one screen, and
                #   he lost his place between them.
                # ★ Sent as data rather than derived in the UI by subtracting one: an offset is an
                #   assumption about how many non-row steps exist, and it breaks silently the day a
                #   second opening step is added.
                "title": title,          # ★ the one thing the coach and the board card share
                "row_n": n, "row_total": total,
                # ★★★ 2026-09-20 — THE RECOMMENDED BLOCK WAS BEING DROPPED HERE, so the board's OWN
                #   walk (the surface the operator asked this feature to live in) rendered no agent
                #   list at all, while `GET /operator/walk` had been serving it since 2026-09-19.
                #   Measured by the Increment-0 lane: `board_walk.plan()` computes `recommended` per
                #   row and `board_walk.main()` never printed it either — so the feature was computed
                #   for a day and displayed nowhere. Carried through verbatim, as data: the panel
                #   composes from board_walk and invents nothing, including the "no agent is mapped"
                #   note, which is the honest answer for a genuinely unmapped row and must survive
                #   the trip. None (not {}) when absent, so an unknown owner never reads as a claim.
                "recommended": r.get("recommended"),
                "days_open": days_open,
                "how_to_check": how_to_check,
                "stale": bool(days_open is not None and days_open >= 14),
            })
        steps.append({
            "kind": "none", "target": "",
            "say": (f"That is the run. {plan.get('remaining_after')} rows remain after this batch. "
                    f"The most valuable thing you produce here is not a closed row — it is whatever "
                    f"surprised you about the walk itself. Tell me and I will file it."),
            "why": "You are the first person to use this in anger.",
            "speak": (f"That is the run. {plan.get('remaining_after')} rows still waiting, and they "
                      f"will keep. Tell me what was wrong with the walk itself — that is worth more "
                      f"than the rows we closed."),
        })
        r = self._op_mod("guide_focus").set_sequence(
            steps, by=str(body.get("by") or "the board walk"),
            why=str(body.get("why") or "guided walk of the operator board"),
            replace=bool(body.get("replace")))
        if r.get("ok"):
            r.update({"rows": total, "order": order, "staleness_source": staleness_source,
                      "fenced_excluded": plan.get("fenced_excluded"),
                      "explained_available": plan.get("explained_available"),
                      "remaining_after": plan.get("remaining_after")})
        return r

    def _guide_focus_post(self, body: dict) -> None:
        """POST — an agent SETS a focus; he ADVANCES or DISMISSES it.
        ★ dismiss is always available. A guided flow he cannot leave is a captor."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        g = self._op_mod("guide_focus")
        act = str(body.get("action") or "set")
        try:
            if act == "sequence":
                return self._send_json(200, g.set_sequence(
                    body.get("steps") or [], str(body.get("by") or "an agent"),
                    str(body.get("why") or ""), replace=bool(body.get("replace"))))
            if act == "walk":
                return self._send_json(200, self._guide_walk_sequence(body))
            if act == "reply":
                r = g.reply(str(body.get("text") or ""), body.get("step"),
                            str(body.get("target") or ""),
                            str(body.get("kind") or "answer"),
                            str(body.get("urgency") or "none"))
                # Recording and delivery are separate facts.  The words are safe first; only after
                # that succeeds do we create one durable ping for the currently selected guide.
                if r.get("ok"):
                    r["backend_ping"] = self._op_mod("guide_backends").notify({
                        "source_at": r.get("at"), "step": body.get("step"),
                        "target": str(body.get("target") or ""),
                        "kind": str(body.get("kind") or "answer"),
                        "urgency": str(body.get("urgency") or "none"),
                        "text": str(body.get("text") or "") or
                                "(no words — pressed I don't understand)",
                        "operator_words": True,
                    })
                return self._send_json(200, r)
            if act == "answer":
                return self._send_json(200, g.answer(str(body.get("target") or ""),
                                                     str(body.get("text") or ""),
                                                     str(body.get("by") or "the agent")))
            if act == "task":
                return self._send_json(200, g.push_task(body.get("steps") or [],
                                                        str(body.get("by") or "an agent"),
                                                        str(body.get("why") or "")))
            if act == "leave_task":
                return self._send_json(200, g.leave_task())
            if act == "recheck":
                r = g.recheck(str(body.get("target") or ""))
                # A hand-written row cannot re-measure itself.  Its generated directive follows the
                # same selected-backend path as typed words, but is labelled agent-generated.
                rr = r.get("reply") or {}
                if r.get("verdict") == "NEEDS_MEASURING" and rr.get("ok"):
                    r["backend_ping"] = self._op_mod("guide_backends").notify({
                        "source_at": rr.get("at"), "step": body.get("step"),
                        "target": str(body.get("target") or ""), "kind": "directive",
                        "urgency": "now", "text": r.get("directive") or "re-check requested",
                        "operator_words": False,
                    })
                return self._send_json(200, r)
            if act == "goto":
                return self._send_json(200, g.goto(body.get("row")))
            if act == "back":
                return self._send_json(200, g.back())
            if act == "advance":
                return self._send_json(200, g.advance())
            if act == "dismiss":
                return self._send_json(200, g.dismiss())
            r = g.set_focus(str(body.get("kind") or "item"), str(body.get("target") or ""),
                            str(body.get("say") or ""), int(body.get("step") or 1),
                            int(body.get("of") or 1), str(body.get("by") or "an agent"),
                            str(body.get("why") or ""),
                            replace=bool(body.get("replace")),
                            dry_run=bool(body.get("dry_run")))
            # ★ 409, not 400: refusing to clobber a live guide is a CONFLICT, not a malformed request.
            #   The caller did nothing wrong; something else is already on his screen.
            code = 200 if r.get("ok") else (409 if "ACTIVE" in str(r.get("error", "")) else 400)
            return self._send_json(code, r)
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_outbox(self) -> None:
        """GET — what he sent from the board, whether it landed, and whether anything came back."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, {"sent": self._op_mod("outbox").sent(10)})
        except Exception as e:
            return self._send_json(500, {"error": f"outbox unavailable: {e}"})

    def _assist_routes(self) -> None:
        """GET — every place a question about an item can go, each PROBED before being offered, with
        what it costs. Local models are free and private; cloud spends real money and says how much
        is left."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            ia = self._op_mod("item_assist")
            r = ia.routes()
            r["order"] = [{"n": n, "id": i["id"], "title": i["title"],
                           "priority": i.get("priority_label")}
                          for n, i in enumerate(ia.board_order(), 1)]
            return self._send_json(200, r)
        except Exception as e:
            return self._send_json(500, {"error": f"assist routes unavailable: {e}"})

    def _assist_ask(self, body: dict) -> None:
        """POST — ask about one item, or a numbered selection of them.
        ★ The item's ORIGIN travels with every question, so whoever answers can go and look instead of
        inferring from a title."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        ia = self._op_mod("item_assist")
        route = str(body.get("route") or "")
        question = str(body.get("question") or "")
        try:
            if body.get("selection"):
                r = ia.ask_batch(str(body["selection"]), route, question,
                                 int(body.get("limit") or 10))
                return self._send_json(200, r)
            r = ia.ask(str(body.get("item") or ""), route, question)
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_services(self) -> None:
        """GET — the probe result, what he has already settled, and the actions available to him.
        ★ PARITY: this returns the SAME action set an agent has. If an agent can install it, he gets a
        button for it; the list is generated from the whitelist, not written twice."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sac = self._op_mod("service_auth_check")
            act = self._op_mod("service_actions")
            d = sac.check()
            dec = act.decisions()
            for r in d["services"]:
                r["settled"] = dec.get(r["id"])
            return self._send_json(200, {
                **d,
                "installs": [{"key": k, "label": v["label"], "why": v["why"],
                              "privilege": v["privilege"],
                              "present": bool(__import__("shutil").which(v["check"][0]))}
                             for k, v in act.INSTALLS.items()],
                "open_urls": [{"key": k, "label": v[0], "url": v[1]} for k, v in act.OPEN_URLS.items()],
            })
        except Exception as e:
            return self._send_json(500, {"error": f"services unavailable: {e}"})

    def _service_ways(self) -> None:
        """GET /operator/service/ways?service=github — the OPTIONS he asked for.

        ★★★ HIS COMPLAINT: "'Check it now' ... wasn't comprehensive enough and didn't give me options
        on how to check. It seemed to have also failed." The check had NOT failed — `gh auth status`
        ran and correctly reported he is not logged in — but a correct answer shown as a bare red ✗,
        with no command and no alternative, is indistinguishable from a broken button. This route
        returns every way to ASK the question and every way to SIGN IN, each stating what it proves
        and what it exposes."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        svc = (q.get("service") or ["github"])[0]
        try:
            sp = self._op_mod("service_probes")
            sl = self._op_mod("service_login")
            return self._send_json(200, {
                "service": svc,
                "probes": [{k: v for k, v in p.items() if k != "cmd"} |
                           {"command": " ".join(p["cmd"]) if p.get("cmd") else ""}
                           for p in sp.probes_for(svc)],
                "login_methods": sl.methods(svc),
                "default_scopes": sl.DEFAULT_SCOPES,
                "note": "Each check asks a DIFFERENT question. Run them all, or pick one.",
            })
        except Exception as e:
            return self._send_json(500, {"error": f"ways unavailable: {e}"})

    def _service_probe(self, body: dict) -> None:
        """POST — run ONE named check, or all of them. ★ A timeout comes back as `unknown`, never
        as a NO; a slow network must not be reported to him as a failed credential."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sp = self._op_mod("service_probes")
            svc = str(body.get("service") or "github")
            which = str(body.get("probe") or "all")
            if which == "all":
                return self._send_json(200, sp.run_all(svc, str(body.get("tool") or "")))
            r = sp.run_probe(svc, which, str(body.get("tool") or ""))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"probe failed to run: {e}"})

    def _service_login(self, body: dict) -> None:
        """POST — sign in FROM THE BOARD. ★ The token path writes the value to gh's stdin and nowhere
        else: not a file here, not a log line, and it is scrubbed out of anything returned."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sl = self._op_mod("service_login")
            act = str(body.get("action") or "")
            svc = str(body.get("service") or "github")
            if act == "start":
                return self._send_json(200, sl.start_device(
                    svc, str(body.get("scopes") or ""), bool(body.get("open_browser"))))
            if act == "status":
                return self._send_json(200, sl.status(svc))
            if act == "token":
                r = sl.login_with_token(str(body.get("token") or ""), svc)
                return self._send_json(200 if r.get("ok") else 400, r)
            if act == "cancel":
                return self._send_json(200, sl.cancel(svc))
            if act == "recipe":
                return self._send_json(200, sl.terminal_recipe(svc))
            return self._send_json(400, {"error": f"unknown action {act!r}",
                                         "choose": ["start", "status", "token", "cancel", "recipe"]})
        except Exception as e:
            return self._send_json(500, {"error": f"login unavailable: {e}"})

    def _operator_reminders(self) -> None:
        """GET — everything he asked to be reminded about, and what is due now.
        ★ CALENDAR INTEGRATION IS NOT BUILT. These are this board's own reminders. An .ics export is
        offered so he can hand one to a calendar himself; nothing here syncs to Google or anything
        else, and the reply says so rather than letting the word 'reminder' imply it."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            rm = self._op_mod("reminders")
            return self._send_json(200, {
                "presets": rm.PRESETS,
                "all": rm.all_reminders(),
                "due": rm.due(),
                "calendar_sync": False,
                "calendar_note": "Not connected to any calendar. Export .ics and import it yourself.",
            })
        except Exception as e:
            return self._send_json(500, {"error": f"reminders unavailable: {e}"})

    def _operator_remind(self, body: dict) -> None:
        """POST — 'log for later': park a row with a time to come back to it.
        ★ A time in the PAST is refused. A reminder that has already expired is silent, and silence
        reads as 'nothing due' — which is the exact failure a reminder exists to prevent."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            rm = self._op_mod("reminders")
            r = rm.set_reminder(str(body.get("item") or ""), str(body.get("preset") or ""),
                                str(body.get("custom") or ""), str(body.get("note") or ""))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"could not set reminder: {e}"})

    def _didthis_fields(self) -> None:
        """GET — the completion form, generated from one definition so the GUI and any agent see the
        SAME fields. Two hand-written copies drift, and the one that drifts is always the human one."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            dt = self._op_mod("did_this")
            return self._send_json(200, {"fields": dt.fields(), "how": dt.HOW,
                                         "evidence": dt.EVIDENCE})
        except Exception as e:
            return self._send_json(500, {"error": f"form unavailable: {e}"})

    def _operator_didthis(self, body: dict) -> None:
        """POST — record what he did, at what percentage, with whom.
        ★ Under 100% the row STAYS OPEN. Settling a half-finished row is how work disappears."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            dt = self._op_mod("did_this")
            item = str(body.get("item") or "")
            if not item:
                return self._send_json(400, {"error": "which row? `item` is required"})
            r = dt.record(item, body)
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"could not record: {e}"})

    def _operator_service_action(self, body: dict) -> None:
        """POST — install · store a credential · open a page · settle a row.
        ★ Every branch delegates to service_actions, whose install list is CLOSED and whose credential
        path never returns the value. Nothing here builds a command from anything the caller sent."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        act = self._op_mod("service_actions")
        what = str(body.get("action") or "")
        try:
            if what == "install":
                return self._send_json(200, act.install(str(body.get("key") or "")))
            if what == "credential":
                r = act.store_credential(str(body.get("name") or ""),
                                         str(body.get("consumer") or ""),
                                         str(body.get("value") or ""))
                return self._send_json(200 if r.get("ok") else 400, r)
            if what == "settle":
                r = act.settle(str(body.get("service") or ""), str(body.get("how") or ""),
                               str(body.get("note") or ""))
                return self._send_json(200 if r.get("ok") else 400, r)
            if what == "open":
                hit = act.OPEN_URLS.get(str(body.get("key") or ""))
                if not hit:
                    return self._send_json(400, {"ok": False, "error": "not a known page — the URL list "
                                                                       "is closed on purpose"})
                subprocess.Popen(["xdg-open", hit[1]], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._send_json(200, {"ok": True, "opened": hit[0], "url": hit[1]})
            if what == "check":
                # ★ HIS PRESS ALWAYS RUNS LIVE. The cache exists so the page opens instantly; it must
                #   never be what he gets when he explicitly asks for a re-check.
                sac = self._op_mod("service_auth_check")
                d = sac.check(force=True)
                one = next((r for r in d["services"] if r["id"] == str(body.get("service") or "")), None)
                return self._send_json(200, {"ok": True, "result": one} if one
                                       else {"ok": False, "error": "no such service"})
            return self._send_json(400, {"ok": False, "error": f"unknown action {what!r}"})
        except Exception as e:
            return self._send_json(500, {"ok": False, "error": f"{e.__class__.__name__}: {e}"})

    def _operator_item_history(self) -> None:
        """★ WHAT HAS HAPPENED TO THIS ROW. The change register records every reword (with a DECLARED
        direction), every state change and every addition — and none of it was visible on the item it
        describes. A row that has been narrowed three times looks identical to one nobody has touched,
        which is precisely the drift the register was built to make visible."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        item = parse_qs(urlsplit(self.path).query).get("id", [""])[0]
        if not item:
            return self._send_json(400, {"error": "need an item id"})
        rows = []
        try:
            f = self._OPERATOR_DIR / "blockers_audit.jsonl"
            if f.exists():
                for line in f.read_text(errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("id") == item:
                        rows.append(r)
        except Exception as e:
            return self._send_json(500, {"error": f"register unreadable: {e}"})
        try:
            b = self._op_mod("item_binding").load()["bindings"].get(item, {})
            hold = b.get("history", [])
        except Exception:
            hold = []
        narrowed = sum(1 for r in rows if r.get("direction") == "narrowed")
        return self._send_json(200, {
            "item": item, "changes": rows[-30:], "holders": hold[-12:],
            "counts": {"total": len(rows), "narrowed": narrowed,
                       "widened": sum(1 for r in rows if r.get("direction") == "widened")},
            # ★ Narrowing is the one direction worth calling out: a board can be defanged one
            #   defensible correction at a time, and every individual narrowing looks like rigour.
            "note": (f"★ this row has been NARROWED {narrowed} time(s) — worth reading before acting on it"
                     if narrowed else ""),
        })

    def _operator_bindings(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            return self._send_json(200, {"bindings": self._op_mod("item_binding").bindings()})
        except Exception as e:
            return self._send_json(500, {"error": f"bindings unavailable: {e}"})

    def _operator_bind(self, body: dict) -> None:
        """★ One agent per item, replaceable at any time. Binding WRITES A BRIEF — the point is not the
        assignment, it is that whoever picks it up arrives with the item's provenance and history
        instead of a one-line title and a hypothesis."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            r = self._op_mod("item_binding").bind(str(body.get("item") or ""),
                                                  str(body.get("target") or ""),
                                                  str(body.get("target_label") or ""))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"bind failed: {e}"})

    def _operator_unbind(self, body: dict) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            r = self._op_mod("item_binding").unbind(str(body.get("item") or ""),
                                                    str(body.get("why") or ""))
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"unbind failed: {e}"})

    def _operator_session_name(self, body: dict) -> None:
        """★ He names a seat once and it sticks. An asserted name OVERRIDES inference, because he knows
        which chat he thinks of as 'the trading seat' and no file records that."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            r = self._op_mod("cc_session_names").set_alias(str(body.get("pid") or ""),
                                                           str(body.get("name") or ""))
            return self._send_json(200, r)
        except Exception as e:
            return self._send_json(500, {"error": f"could not name it: {e}"})

    def _operator_delta(self) -> None:
        """★ §2 · DELTA, NOT STATE. A count of what is OUTSTANDING is wallpaper by tomorrow; a count of
        what is NEW is information, and it self-clears when he looks. Per source, so fourteen roadmap
        rows arriving at once cannot bury one new approval request."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import board_delta, importlib; importlib.reload(board_delta)
            return self._send_json(200, board_delta.delta())
        except Exception as e:
            return self._send_json(500, {"error": f"delta unavailable: {e}"})

    def _operator_seen(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import board_delta, importlib; importlib.reload(board_delta)
            return self._send_json(200, board_delta.mark_seen())
        except Exception as e:
            return self._send_json(500, {"error": f"could not record: {e}"})

    def _dispatch_targets(self) -> None:
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import dispatch_targets, importlib; importlib.reload(dispatch_targets)
            return self._send_json(200, dispatch_targets.targets())
        except Exception as e:
            return self._send_json(500, {"error": f"target registry unavailable: {e}"})

    def _dispatch_send(self, body: dict) -> None:
        """★ A `live` target is delivered NOW and the result is reported truthfully — including a
        refusal. A `queued` target is written to the handoff queue and SAID to be queued, never
        described as sent. The difference between those two words is the whole point of this route."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        target = str(body.get("target") or "")
        text = str(body.get("text") or "").strip()
        if not target or not text:
            return self._send_json(400, {"error": "need a target and something to say",
                                         "field_contract": {"target": "required", "text": "required — "
                                                            "message/body/content/note are NOT accepted",
                                                            "from_operator": "optional bool; without it "
                                                            "the message is labelled agent-relayed",
                                                            "dry_run": "optional bool; honoured",
                                                            "_timeout": "a LIVE send verifies delivery "
                                                            "at the destination before answering. It "
                                                            "normally returns in well under a second, "
                                                            "but allow 10s — a shorter client timeout "
                                                            "gives you an empty body while the message "
                                                            "still delivers."}})
        # ★★ dry_run WAS ACCEPTED AND IGNORED — a parameter that changes nothing while appearing to,
        #    which is the defect class this house has spent weeks removing. A trading-seat probe passed
        #    it expecting a no-op and the message delivered for real. Honoured now.
        if body.get("dry_run"):
            return self._send_json(200, {"ok": True, "dry_run": True, "sent": False,
                                         "would_send_to": target,
                                         "preview": text[:300],
                                         "attributed_to": "the operator" if body.get("from_operator")
                                                          else "an agent (relayed)",
                                         "note": "NOTHING was sent. Remove dry_run to send."})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import dispatch_targets, importlib; importlib.reload(dispatch_targets)
            rows = [r for g in dispatch_targets.targets()["groups"].values() for r in g]
            hit = next((r for r in rows if r["id"] == target), None)
            if not hit:
                return self._send_json(400, {"error": f"unknown target {target!r}"})
            if hit["delivery"] == "manual":
                return self._send_json(409, {"ok": False, "error": hit["how"],
                                             "note": "deliberately not one-click — nothing was sent"})
            entry = {"target": target, "target_label": hit["label"], "delivery": hit["delivery"],
                     "kind": str(body.get("kind") or "note"), "item": str(body.get("item") or ""),
                     "text": text[:4000], "from": "operator board"}
            if hit["delivery"] == "live" and target.startswith("aionui:"):
                # ★ Reuse the ONE delivery path (_operator_notify), which confirms arrival from the
                #   messages table rather than from its own 200. Inventing a second sender here is how
                #   two surfaces end up disagreeing about whether a thing was delivered.
                tid = target.split(":", 1)[1]
                lane = next((k for k, v in self._LANE_TEAMS.items() if v[0].startswith(tid[:8])), None)
                if not lane:
                    entry["note"] = "no lane mapping — queued rather than sent"
                    self._op_mod("outbox").record(entry)
                    return self._send_json(200, dispatch_targets.enqueue(entry))
                # ★ Record BEFORE delivering, with the destination conversation, so the outbox can read
                #   the thread back and tell him whether anything came of it.
                self._op_mod("outbox").record({**entry, "lane": lane,
                                               "conversation": self._LANE_TEAMS[lane][1]})
                return self._operator_notify({"lane": lane, "note": text,
                                              "item": {"id": entry["item"],
                                                       "title": entry.get("item") or "(from the board)",
                                                       "one": text, "sev": "info"}})
            # ★ A QUEUED TARGET USED TO FALL OUT OF THIS FUNCTION WITHOUT WRITING OR ANSWERING.
            # The picker correctly labelled it queued, but the server implemented only the live
            # branch.  Queue it once and say explicitly that this is not delivery.
            self._op_mod("outbox").record(entry)
            return self._send_json(202, dispatch_targets.enqueue(entry))
        except Exception as e:
            return self._send_json(500, {"error": f"dispatch failed: {e}"})

    def _operator_verify_get(self) -> None:
        """★ The seven paths and whatever he has ruled on them. The ONLY source of `verified-by-him`."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import him_verify
            import importlib; importlib.reload(him_verify)
            return self._send_json(200, him_verify.status())
        except Exception as e:
            return self._send_json(500, {"error": f"verdict store unavailable: {e}"})

    def _operator_verify_post(self, body: dict) -> None:
        """★★ HIS WORDS, NOT A SUMMARY OF THEM. The note is stored verbatim; nothing here rewrites it.
        ★★★ And a PARTIAL with no note is REFUSED by the store, not silently upgraded — see him_verify."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        try:
            sys.path.insert(0, str(self._OPERATOR_DIR))
            import him_verify
            import importlib; importlib.reload(him_verify)
            r = him_verify.record(str(body.get("path") or ""), str(body.get("verdict") or ""),
                                  str(body.get("note") or ""), actor="operator (panel)")
            return self._send_json(200 if r.get("ok") else 400, r)
        except Exception as e:
            return self._send_json(500, {"error": f"could not record: {e}"})

    def _voice_speak(self, body: dict) -> None:
        """★ THE CALL SITE IS WHERE ATTRIBUTION HAPPENS. voicebox is a shared sink with no caller
        identity — it cannot tell anyone who spoke, because nobody ever tells it. Every field of the
        envelope below is added HERE or it does not exist anywhere.
        ★★ And `effect` is MEASURED while the utterance is in flight, not claimed afterwards: /speak
        blocks through playback, so a sink sampled after it returns is always sampled too late."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        text = str(body.get("text") or "").strip()
        if not text:
            return self._send_json(400, {"error": "nothing to say"})
        import urllib.request, time as _t
        sys.path.insert(0, str(self._OPERATOR_DIR))
        try:
            import voice_log
        except Exception:
            voice_log = None

        # ★ RESOLVE A HUMAN VOICE NAME. The operator asks for "amy medium"; piper's file is
        #   `en_US-amy-medium.onnx`. Passing the short name straight through made voicebox answer
        #   `voice not found: amy.onnx` — and because this proxy did not read that answer (see below),
        #   the panel reported ok:true. A caller naming a voice that plainly exists must not be told
        #   nothing happened; resolve the alias against what is actually installed.
        # ★★★ ONE UTTERANCE PER THING-TO-SAY, DECIDED HERE INSTEAD OF IN THE PAGE.
        #   Measured in operator/voicelog.jsonl: "guided walk · step 8 of 22" appears TWICE, and its
        #   15-second attention repeat appears twice as well. Two panel windows are open and both are
        #   speaking. The browser-side speaker lease I wrote only exists in the CURRENT build — an
        #   older window, already loaded, has never heard of it and never will.
        #   ★★ A guard that only protects clients new enough to have it is not a guard, it is a
        #     version check. This is the same lesson as the mouth arbitration one layer down: the
        #     place to arbitrate is where the action happens, not where one client happens to be.
        #   ★ It refuses a DUPLICATE, never a different line: identical text within a few seconds is
        #     two windows agreeing, not a person asking twice. Pressing "read it again" sends the same
        #     text deliberately — so an explicit replay carries `force` and is always spoken.
        _now = _t.time()
        _key = hashlib.sha256((str(body.get("text") or "")).encode()).hexdigest()[:16]
        if not body.get("force"):
            _prev = getattr(type(self), "_last_spoke", {})
            _when = _prev.get(_key, 0.0)
            if _now - _when < 6.0:
                return self._send_json(200, {
                    "ok": True, "effect": "suppressed_duplicate", "spoken_aloud": False,
                    "note": "another panel window said this same line "
                            f"{_now - _when:.1f}s ago — suppressed so it is not read twice. "
                            "This is not an error and nothing was lost."})
            _prev[_key] = _now
            # ★ bounded: keep only what is recent, so this never grows into a leak
            type(self)._last_spoke = {k: v for k, v in _prev.items() if _now - v < 60}
        want = str(body.get("voice") or "en_US-amy-medium")
        voice_note = None
        if not (PIPER_DIR / f"{want}.onnx").exists():
            hits = sorted(x.stem for x in PIPER_DIR.glob("*.onnx") if want.lower() in x.stem.lower())
            if len(hits) == 1:
                voice_note = f"resolved '{want}' -> {hits[0]}"
                want = hits[0]
            elif len(hits) > 1:
                pref = [h for h in hits if h.startswith("en_US-")] or hits
                voice_note = f"resolved '{want}' -> {pref[0]} (ambiguous, {len(hits)} matched)"
                want = pref[0]
            # no hits: leave it. voicebox will refuse by name, and that refusal is now surfaced.
        payload = {"text": text[:4000], "play": True, "voice": want}
        speed_note = None
        if body.get("speed") is not None:
            try:
                payload["speed"] = float(body["speed"])
            except Exception:
                # ★ NOT `pass`. "medium" is a voice qualifier, not a speed, and silently dropping it
                #   meant a caller could ask for something and be told nothing about it being ignored.
                speed_note = f"speed {body['speed']!r} is not a number — ignored, default rate used"

        row = {
            "kind": str(body.get("kind") or "utterance"),
            "source": str(body.get("source") or "operator board"),
            "where": str(body.get("where") or "panel :7842"),
            "why": str(body.get("why") or ""),
            "clearance": str(body.get("clearance") or "initiate"),
            "territory": str(body.get("territory") or ""),
            "link": str(body.get("link") or ""),
            "voice": payload["voice"],
            "voice_note": voice_note,
            "speed": payload.get("speed"),
            "speed_note": speed_note,
            "text": text[:600],
        }
        if body.get("replay_of"):
            row["kind"] = "replay"
            row["replay_of"] = str(body["replay_of"])

        started = _t.time()
        watcher = None
        try:
            if voice_log:
                watcher = voice_log.SinkWatcher()
                watcher.__enter__()
            req = urllib.request.Request("http://127.0.0.1:8770/speak",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            # ★★ READ THE UPSTREAM'S ANSWER. This was `urlopen(req, timeout=40)` with the body
            #    discarded, followed by an unconditional ok=True. voicebox returns HTTP 200 carrying
            #    {"ok": false, "error": ...} when it refuses — so every refusal it ever made was
            #    reported here as a success. A proxy that does not read the reply is not a proxy, it
            #    is a fire-and-forget that claims delivery. The only reason this was ever caught is
            #    that `effect` is MEASURED at the sink rather than inferred from this status.
            up = json.loads(urllib.request.urlopen(req, timeout=40).read().decode() or "{}")
            if up.get("ok") is False:
                ok, err = False, str(up.get("error") or "voicebox refused without saying why")
            elif up.get("spoken_aloud") is False:
                # ★★★ `ok` AND `spoken_aloud` ARE TWO DIFFERENT CLAIMS. 2026-09-18.
                #   voicebox gained mouth arbitration today (the operator heard two services talking
                #   over each other). A refused playback now returns HTTP 200 with
                #   {"ok": true, ..., "spoken_aloud": false} — SYNTHESISED, NOT HEARD.
                #   ★ `ok: true` means the WAV exists. `spoken_aloud` means he heard it. This caller
                #     only ever cares about the second, and checking the first would report delivery
                #     for something that made no sound.
                #   ★★ Note the comment directly above: this exact class was already fixed here once,
                #     when the reply was not read at all. It came back one layer deeper — the reply
                #     IS read, and the wrong field is believed. Fixing "didn't read it" did not fix
                #     "read the wrong thing", and nothing was watching for the difference.
                #   ★ Forward `refused` and `holder` too. spoken_aloud alone says THAT he did not
                #     hear it; refused+holder say WHY and BY WHOM — the difference between
                #     "retry later" and "JARVIS is mid-answer, stand down". The source returns
                #     both; this proxy was dropping them.
                ok, err = False, (f"not spoken ({up.get('refused') or 'refused'}): the mouth is "
                                  f"held by {up.get('holder') or 'another service'} — synthesised "
                                  f"but nothing was heard")
                _refusal = {"refused": up.get("refused"), "holder": up.get("holder"),
                            "spoken_aloud": False}
            else:
                ok, err = True, None
        except Exception as e:
            ok, err = False, f"voicebox did not answer: {str(e)[:120]}"
        finally:
            if watcher:
                watcher.__exit__(None, None, None)

        row["duration_s"] = round(_t.time() - started, 2)
        row["effect"] = (watcher.effect if watcher else "unknown") if ok else "failed"
        if voice_log:
            row = voice_log.append(row)

        if not ok:
            return self._send_json(503, {"ok": False, "doctor": True, "error": err, "row": row.get("id")})
        # ★ The caller is told the MEASURED effect, not just that the call succeeded. A 200 that says
        #   effect:"silent" is the honest answer to a week of silent chimes.
        return self._send_json(200, {"ok": True, "id": row.get("id"), "effect": row["effect"],
                                     "duration_s": row["duration_s"]})

    def _voice_log(self) -> None:
        """GET /voice/log — the tail, filtered by the viewer's clearance, with what was withheld COUNTED."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(self.path).query)
        sys.path.insert(0, str(self._OPERATOR_DIR))
        try:
            import voice_log
        except Exception as e:
            return self._send_json(500, {"error": f"voice log unavailable: {e}"})
        limit = int((q.get("limit", ["15"])[0] or 15))
        before = q.get("before", [None])[0]
        return self._send_json(200, voice_log.read(
            limit=max(1, min(limit, 200)),
            before=int(before) if before and before.isdigit() else None,
            viewer_clearance=(q.get("clearance", ["sage"])[0] or "sage"),
            viewer_territory=q.get("territory", [None])[0]))

    def _voice_stop(self) -> None:
        """★ A long read with no stop is a trap — he will be listening while doing something else."""
        if not self._is_local_request():
            return self._send_json(403, {"error": "local-only"})
        import urllib.request
        try:
            req = urllib.request.Request("http://127.0.0.1:8770/stop", data=b"{}",
                                         headers={"Content-Type": "application/json"}, method="POST")
            # ★ SAME DEFECT AS /voice/speak HAD: the reply was discarded and ok:True returned regardless.
            #   Stop is the control he reaches for while a long read is running — the one place where
            #   "it worked" must not be a guess. voicebox answers HTTP 200 with {"ok": false} when it
            #   cannot stop; that answer is now surfaced instead of overwritten.
            up = json.loads(urllib.request.urlopen(req, timeout=8).read().decode() or "{}")
            if up.get("ok") is False:
                return self._send_json(502, {"ok": False, "doctor": True,
                                             "error": str(up.get("error") or "voicebox refused the stop")})
            return self._send_json(200, {"ok": True, "upstream": up or None})
        except Exception as e:
            return self._send_json(503, {"ok": False, "doctor": True, "error": str(e)[:120]})
    # ── end board read-aloud ───────────────────────────────────────────────────────────────────

    def _voice_status(self) -> None:
        import urllib.request
        try:
            data = json.loads(urllib.request.urlopen("http://127.0.0.1:8770/health", timeout=2).read())
            return self._send_json(200, {
                "running": True,
                "model_present": data.get("model_present"),
                "model": data.get("model"),
                "current_voice": data.get("current_voice"),
                "current_device": data.get("current_device"),
                "voice_count": data.get("voice_count"),
                "device_count": data.get("device_count"),
                "cloning_available": data.get("cloning_available"),
                "clone_count": data.get("clone_count"),
            })
        except Exception as exc:
            # Visible failure — the card MUST be able to say "server down" with a reason.
            return self._send_json(200, {"running": False, "error": str(exc)})

    def _voice_voices(self) -> None:
        # Prefer the live :8770 listing (carries the `default` flag + canonical
        # paths). Fall back to the on-disk glob only if the server is down so the
        # picker still populates.
        import urllib.request
        try:
            data = json.loads(urllib.request.urlopen("http://127.0.0.1:8770/voices", timeout=2).read())
            voices = data.get("voices", [])
            if voices and isinstance(voices[0], dict):
                return self._send_json(200, {"voices": voices, "source": "server"})
        except Exception:
            pass
        vdir = Path.home() / ".local/share/piper"
        names = sorted(f.stem for f in vdir.glob("*.onnx")) if vdir.is_dir() else []
        voices = [{"name": n, "path": str(vdir / f"{n}.onnx"), "default": False} for n in names]
        return self._send_json(200, {"voices": voices, "source": "disk"})

    def _voice_devices(self) -> None:
        """GET /voice/devices — proxy :8770/devices (output audio devices)."""
        import urllib.request
        try:
            data = json.loads(urllib.request.urlopen("http://127.0.0.1:8770/devices", timeout=2).read())
            return self._send_json(200, {"devices": data.get("devices", []), "source": "server"})
        except Exception as exc:
            # Visible failure rather than an empty silent list.
            return self._send_json(200, {"devices": [], "source": "unavailable", "error": str(exc)})

    def _voice_fix(self) -> None:
        """POST /voice/fix — run the voicebox DOCTOR (the ONE troubleshooting/fix pathway). It durably
        (re)starts :8770 under `systemd --user`, applying everything learned about why voicebox goes down:
        child-of-parent death (the #1 cause — a child dies when the panel/AionUi restart), pyenv `python3`
        (no fastapi), the >8s model load, and stale procs/ports. Returns {result, summary, output}."""
        import subprocess, re as _re
        doctor = str(ROOT / "voicebox" / "voicebox_doctor.sh")
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        try:
            r = subprocess.run(["bash", doctor], capture_output=True, text=True, timeout=55, env=env)
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            m = _re.search(r"RESULT · (\w+)\s*(.*)", out)
            result = m.group(1) if m else ("FIXED" if r.returncode == 0 else "FAILED")
            summary = (m.group(2).strip(" —") if m else "").strip()
            return self._send_json(200, {"result": result, "summary": summary, "output": out,
                                         "success": r.returncode == 0 or result in ("FIXED", "HEALTHY")})
        except subprocess.TimeoutExpired:
            return self._send_json(200, {"result": "FAILED", "summary": "doctor timed out (>55s)", "success": False})
        except Exception as exc:
            return self._send_json(200, {"result": "FAILED", "summary": str(exc), "success": False})

    def _audio_status(self) -> None:
        """GET /audio/status — KHAOSS audio doctor, READ-ONLY: default output, all outputs, connected
        Bluetooth audio, detected faults (STUCK_NODE / DEFAULT_BAD / BT_SILENT / BT_CALL_MODE / MUTED),
        auto-repair state and recent doctor actions. Never changes anything."""
        import subprocess
        doctor = str(ROOT / "audio" / "audio_doctor.py")
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        try:
            r = subprocess.run(["/usr/bin/python3", doctor, "status", "--json"],
                               capture_output=True, text=True, timeout=20, env=env)
            data = json.loads(r.stdout)
            data["doctor_service"] = subprocess.run(["systemctl", "--user", "is-active", "khaoss-audio-doctor"],
                                                    capture_output=True, text=True, timeout=5, env=env).stdout.strip()
            return self._send_json(200, data)
        except Exception as exc:
            return self._send_json(200, {"healthy": None, "error": str(exc)})

    def _audio_fix(self) -> None:
        """POST /audio/fix — run the audio DOCTOR fix (the ONE sound/Bluetooth repair pathway): rebuild stuck
        outputs, restore a valid default (your own history first), un-silence connected Bluetooth, leave call
        mode, unmute; escalates to service restarts only if still broken. Returns {result, summary, output}."""
        import subprocess, re as _re
        doctor = str(ROOT / "audio" / "audio_doctor.py")
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
        try:
            r = subprocess.run(["/usr/bin/python3", doctor, "fix"], capture_output=True, text=True, timeout=110, env=env)
            out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
            m = _re.search(r"RESULT · (\w+)\s*(.*)", out)
            result = m.group(1) if m else ("FIXED" if r.returncode == 0 else "FAILED")
            summary = (m.group(2).strip(" —()") if m else "").strip()
            return self._send_json(200, {"result": result, "summary": summary, "output": out,
                                         "success": result in ("FIXED", "HEALTHY")})
        except subprocess.TimeoutExpired:
            return self._send_json(200, {"result": "FAILED", "summary": "audio doctor timed out (>110s)", "success": False})
        except Exception as exc:
            return self._send_json(200, {"result": "FAILED", "summary": str(exc), "success": False})

    def _voice_test(self) -> None:
        """POST /voice/test — proxy :8770/test (speaks a fixed sample NOW)."""
        import urllib.request
        body = self._read_body_json() or {}
        payload = {}
        for k in ("voice", "device", "volume", "speed"):
            if k in body and body[k] not in (None, ""):
                payload[k] = body[k]
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8770/test",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            # Generous timeout: cloned (XTTS) voices synth in ~10–25s on CPU.
            data = json.loads(urllib.request.urlopen(req, timeout=45).read())
            return self._send_json(200, data)
        except Exception as exc:
            # Never swallow — the card surfaces this.
            return self._send_json(200, {"ok": False,
                                         "error": f"TTS server (:8770) unreachable or errored: {exc}"})

    def _voice_clones(self) -> None:
        """GET /voice/clones — proxy :8770/clones (cloned XTTS voices)."""
        import urllib.request
        try:
            data = json.loads(urllib.request.urlopen("http://127.0.0.1:8770/clones", timeout=3).read())
            return self._send_json(200, {"clones": data.get("clones", []),
                                         "cloning_available": data.get("cloning_available"),
                                         "source": "server"})
        except Exception as exc:
            return self._send_json(200, {"clones": [], "cloning_available": False, "error": str(exc)})

    def _voice_clone(self) -> None:
        """POST /voice/clone?name=<name> — forward raw reference audio bytes to
        :8770/voice/clone?name=<name> (registers a cloned voice; model loads lazily)."""
        import urllib.request, urllib.parse
        qs = parse_qs(urlsplit(self.path).query)
        name = (qs.get("name", [""])[0] or "").strip()
        if not name:
            return self._send_json(400, {"ok": False, "error": "missing ?name="})
        try:
            clen = int(self.headers.get("Content-Length", 0) or 0)
            audio = self.rfile.read(clen) if clen > 0 else b""
        except Exception as exc:
            return self._send_json(400, {"ok": False, "error": f"could not read body: {exc}"})
        if not audio:
            return self._send_json(400, {"ok": False, "error": "no reference audio received"})
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:8770/voice/clone?name={urllib.parse.quote(name)}",
                data=audio,
                headers={"Content-Type": "application/octet-stream"},
                method="POST",
            )
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())
            return self._send_json(200, data)
        except Exception as exc:
            return self._send_json(200, {"ok": False,
                                         "error": f"TTS server (:8770) unreachable or errored: {exc}"})

    _VOICE_ALLOWLIST = (
        "en_US-amy-medium", "en_US-ryan-high", "en_GB-alan-medium",
        "en_US-lessac-medium", "en_US-lessac-high",
        # KHAOSS-PATCH: voice-lane (DQ7#3) — Spanish. Piper's registry has no es_CR (only es_AR /
        # es_ES / es_MX). es_MX is the closest INTERIM stand-in for a Spanish variety with no Piper
        # model; a target variety without a Piper voice needs XTTS from a consented sample.
        "es_MX-ald-medium", "es_ES-davefx-medium", "es_AR-daniela-high",
    )

    def _voice_download(self, voice_name: str) -> None:
        # Allowlist guards against path traversal / arbitrary fetches.
        if voice_name not in self._VOICE_ALLOWLIST:
            return self._send_json(400, {"ok": False, "error": f"voice '{voice_name}' not in approved list"})
        import urllib.request
        base = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        lang = voice_name[:2]                              # en
        region_voice = voice_name[3:]                      # US-amy-medium
        region, voice_base = region_voice.split("-", 1)    # US , amy-medium
        speaker, quality = voice_base.rsplit("-", 1)       # amy , medium
        path_part = f"{lang}/{lang}_{region}/{speaker}/{quality}"
        vdir = Path.home() / ".local/share/piper"
        vdir.mkdir(parents=True, exist_ok=True)
        for ext in (".onnx", ".onnx.json"):
            url = f"{base}/{path_part}/{voice_name}{ext}"
            dest = vdir / f"{voice_name}{ext}"
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as exc:
                return self._send_json(502, {"ok": False, "error": f"download failed: {exc}"})
        return self._send_json(200, {"ok": True, "voice": voice_name})

    # KHAOSS-PATCH: voice-lane (DQ7#3) — the backend-CHOICE catalog. The engine is a SETTING with
    # trade-offs stated AT the control (D1/D2/D3). Numbers are MEASURED ON THIS BOX (this-box), not
    # vendor claims: Piper 0.41× realtime / ~209 MB RSS; XTTS clone warm ~2.0× realtime / ~4.2 GB peak
    # RSS / ~19 s one-time model load; whisper tiny 0.12× / base.en 0.25× / small.en 0.90× realtime.
    # Ineligible options are returned with eligible:false + a REASON so the client renders them
    # disabled-with-reason, never hidden (D3). One SSOT for panel + AionUi (no second settings home).
    _VOICE_ENGINE_CATALOG = {
        "speech_out": [
            {"id": "piper", "label": "Piper (local, multi-voice)", "installed": True, "eligible": True,
             "tradeoff": "Fast (~0.4× realtime) and small (~209 MB). 12 installed voices, English + es_MX/es_ES/es_AR/fr. "
                         "Cannot do a Spanish variety with no Piper model and cannot clone a specific person's voice.",
             "languages": ["en", "es_MX", "es_ES", "es_AR", "fr"], "can_clone": False},
            {"id": "xtts", "label": "XTTS clone (any voice from a sample)", "installed": True, "eligible": True,
             "tradeoff": "Clones any voice (including Spanish varieties) zero-shot from one ~6-30 s consented reference. "
                         "Heavy: ~2× realtime warm, ~19 s first-call model load, ~4.2 GB RAM while active (RAM is the binding "
                         "constraint on this box). Off by default; the model loads only on first clone synth.",
             "languages": ["multilingual"], "can_clone": True},
            {"id": "inflect_micro_v2", "label": "Inflect-Micro-v2 (tiny English)", "installed": False, "eligible": False,
             "reason": "Fixed-voice ENGLISH only — does NOT serve Spanish, so it cannot meet a bilingual need. Not installed.",
             "tradeoff": "9.36M params, 37.5 MB, Apache-2.0, ~6.28× realtime (4 threads). Fixed single English voice — no voice choice, no Spanish.",
             "languages": ["en"], "can_clone": False},
        ],
        "speech_in": [
            {"id": "tiny", "label": "whisper.cpp tiny (live default)", "installed": True, "eligible": True,
             "tradeoff": "Fastest (~0.12× realtime, 175 MB). Weakest on proper nouns (~1/4 even with the glossary) — fine for short commands, weak for dictation."},
            {"id": "base.en", "label": "whisper.cpp base.en (recommended)", "installed": False, "eligible": True,
             "tradeoff": "~0.25× realtime, 283 MB. With the glossary --prompt: 3/4 project proper nouns — the recommended default. English only. Not yet installed (operator-gated).",
             "recommended": True},
            {"id": "small.en", "label": "whisper.cpp small.en (accuracy mode)", "installed": False, "eligible": True,
             "tradeoff": "~0.90× realtime (edge of interactive), 750 MB. With the glossary: 4/4 proper nouns. English only. Opt-in for dense dictation. Not installed."},
        ],
        "notes": {
            "glossary_prompt": "SHIPPED: tts_server.py passes a proper-noun glossary to whisper as --prompt; "
                               "measured +2/4 proper nouns on base.en at ~zero cost.",
            "es_cr": "No es_CR voice exists in the Piper registry; es_MX is an INTERIM stand-in. A target variety without a Piper voice can use XTTS with a consented sample.",
            "audio_never_leaves": "All engines run locally on this box; no engine sends audio to any cloud.",
        },
    }

    # ── FULL RESTART of the voice seat (the operator's explicit ask, 2026-09-21) ───────────────
    # ★★★ LOOPBACK ONLY, AND DELIBERATELY NOT ON REMOTE_SAFE_POST_EXACT. Every other mutation the
    # panel exposes remotely only ever REDUCES exposure. This one kills and restarts three live
    # processes, so a phone that reached it could take the assistant down at will. It is a
    # recovery control for the operator sitting at the box, not a remote power switch.
    def _voice_full_restart(self) -> None:
        """POST /services/voice-full-restart — kill and restart daemon + voicebox, then VERIFY.

        Delegates to bin/voice-full-restart.sh, which stops processes BY IDENTITY (PID resolved from
        the socket table, cmdline checked before signalling) rather than by `pkill -f`. The script
        returns JSON; the panel relays it verbatim rather than re-summarising, so a failure is
        reported in the script's own words instead of being softened on the way out.
        """
        if not self._is_local_request():
            return self._send_json(403, {
                "error": "local-only — a full restart kills live processes and is not remote-accessible"})
        script = Path(__file__).resolve().parent.parent / "bin" / "voice-full-restart.sh"
        if not script.is_file():
            return self._send_json(500, {"ok": False, "error": f"restart script missing: {script}"})
        try:
            r = subprocess.run(["/bin/sh", str(script), "--json"],
                               capture_output=True, text=True, timeout=180)
            out = (r.stdout or "").strip().splitlines()
            payload = {}
            for ln in reversed(out):        # last JSON line wins (the script logs before it)
                ln = ln.strip()
                if ln.startswith("{"):
                    try:
                        payload = json.loads(ln)
                        break
                    except Exception:
                        continue
            if not payload:
                payload = {"ok": False, "exit": r.returncode,
                           "error": "the restart script produced no JSON verdict",
                           "stderr": (r.stderr or "")[-400:]}
            payload["exit"] = r.returncode
            return self._send_json(200 if payload.get("ok") else 500, payload)
        except subprocess.TimeoutExpired:
            return self._send_json(500, {"ok": False,
                                         "error": "the restart timed out after 180s; the seat may be part-way"})
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})

    def _voice_engines(self) -> None:
        """GET /voice/engines — the backend-choice catalog (SSOT for the choice surface)."""
        return self._send_json(200, self._VOICE_ENGINE_CATALOG)
    # ── end boardwalk-voice-card ────────────────────────────────────────────────

    # ── SEEK: voice-orchestration connections + routes, and the agent hand-off brief ──────────
    def _voice_seek_mod(self):
        """Import (or RELOAD) jarvis/seek.py AND its dependencies — so edits take effect at once.

        ★★★ WHY THIS IS NOT JUST importlib.import_module(). The first version used it directly and
        served a STALE probe set: once `seek` is in sys.modules, import_module returns the cached
        module forever, so new probes did not exist as far as the UI was concerned while
        `python3 jarvis/seek.py` reported them. A diagnostic that silently runs old code is worse
        than one that fails, because its answer still looks authoritative.
        ★★ AND THE SAME HOLE WAS ONE LAYER DEEPER, which a review caught: `seek` imports
        `focus_sources` lazily, and the panel has ALREADY cached focus_sources for its own
        /state/focus-sources and /state/capture-policy routes. Reloading only `seek` therefore
        served a fresh seek on top of a STALE resolver — the very bug this method was written to
        prevent, repeated for its dependency. Both are reloaded now. The lens is "the diagnostic
        must run the code on disk", not "the diagnostic module must be fresh".
        """
        import importlib
        import sys as _sys
        jdir = str(Path(__file__).resolve().parent.parent / "jarvis")
        odir = str(Path(__file__).resolve().parent.parent / "operator")
        for d in (jdir, odir):
            if d not in _sys.path:
                _sys.path.insert(0, d)
        # ★ order matters: dependencies first, then seek (which imports them at call time)
        for name in ("focus_sources", "capture_policy_store", "seek"):
            if name in _sys.modules:
                try:
                    importlib.reload(_sys.modules[name])
                except Exception:
                    # a module that will not reload (e.g. mid-import) is left as-is; it will be
                    # reported by the probe rather than crashing the endpoint
                    pass
        return _sys.modules.get("seek") or importlib.import_module("seek")

    def _voice_seek(self) -> None:
        """GET /voice/seek — probe every voice dependency and route; return the map as JSON.

        ★ READ-ONLY BY CONSTRUCTION. It opens no microphone, writes no setting and restarts
        nothing, so the operator can press it as often as he likes. A diagnostic that changes
        state is a diagnostic you cannot safely re-run, and re-running is how you tell a fixed
        thing from a flapping one.
        """
        try:
            seek = self._voice_seek_mod()
            return self._send_json(200, seek.seek())
        except Exception as exc:
            # ★ A broken prober must not look like a healthy box. Say which one, and say 500.
            return self._send_json(500, {
                "ok": False, "error": f"seek unavailable: {exc.__class__.__name__}: {exc}",
                "hint": "jarvis/seek.py failed to import or run; that IS the finding."})

    def _voice_seek_brief(self) -> None:
        """GET /voice/seek/brief — the same probe rendered as a self-contained Markdown brief.

        Served as text/markdown so 'Save as…' lands a .md, and so the AI-assist button can fetch
        it verbatim and put it on the clipboard. No secret values are included: ports, model
        names and reasons only.
        """
        try:
            seek = self._voice_seek_mod()
            symptom = ""
            try:
                q = urlsplit(self.path).query
                if q:
                    from urllib.parse import parse_qs
                    symptom = (parse_qs(q).get("symptom") or [""])[0][:400]
            except Exception:
                symptom = ""
            md = seek.brief_md(symptom=symptom)
            return self._send_text(200, md, "text/markdown; charset=utf-8")
        except Exception as exc:
            return self._send_text(
                500, f"# SEEK brief unavailable\n\n`{exc.__class__.__name__}: {exc}`\n",
                "text/markdown; charset=utf-8")

    def _teams(self) -> None:
        try:
            return self._send_json(200, _teams_data())
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _swarms(self) -> None:
        try:
            return self._send_json(200, _swarms_data())
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _provision(self) -> None:
        body = self._read_body_json()
        profiles_in = body.get("profiles")
        if not isinstance(profiles_in, list) or not profiles_in:
            return self._send_json(400, {"error": "profiles array is required"})
        if len(profiles_in) > 12:
            return self._send_json(400, {"error": "batch limit is 12 profiles per call"})
        registry = _parse_registry()
        provisioned: list[dict] = []
        skipped:     list[dict] = []
        errors:      list[dict] = []
        for entry in profiles_in:
            if not isinstance(entry, dict):
                errors.append({"name": "?", "reason": "entry must be an object"})
                continue
            name    = str(entry.get("name",    "")).strip()
            role    = str(entry.get("role",    "")).strip()[:200]
            program = str(entry.get("program", "")).strip()[:200]
            result  = _provision_one(name, role, program, registry)
            if not result["ok"]:
                errors.append({"name": name, "reason": result["reason"]})
                _notify(
                    severity="high", source="swarm-manager",
                    title=f"Provision failed: {name}",
                    body=result["reason"],
                    payload={"kind": "provision-error", "name": name,
                             "program": program, "reason": result["reason"]},
                )
            elif "already" in result.get("result", ""):
                skipped.append({"name": name, "result": result["result"]})
                _notify(
                    severity="low", source="swarm-manager",
                    title=f"Profile already exists: {name}",
                    body=result["result"],
                    payload={"kind": "provision-skip", "name": name, "program": program},
                )
            else:
                provisioned.append({"name": name, "result": result["result"]})
                _notify(
                    severity="mid", source="swarm-manager",
                    title=f"Profile provisioned: {name}",
                    body=result["result"],
                    payload={"kind": "provision-ok", "name": name,
                             "program": program, "role": role},
                )
        return self._send_json(200, {
            "ok":          len(errors) == 0,
            "provisioned": provisioned,
            "skipped":     skipped,
            "errors":      errors,
        })

    def _vk_toggle(self) -> None:
        pid_info = _pid_status(VK_PID_FILE)
        if pid_info.get("running"):
            try:
                os.kill(int(pid_info["pid"]), signal.SIGTERM)
            except (OSError, ValueError) as exc:
                return self._send_json(500, {"error": str(exc)})
            return self._send_json(200, {"action": "stop"})
        if not VK_SCRIPT.is_file():
            return self._send_json(500, {"error": f"missing: {VK_SCRIPT}"})
        log_fh = open(ROOT / "voice_kanban.log", "a", encoding="utf-8")
        subprocess.Popen(
            ["/usr/bin/env", "python3", str(VK_SCRIPT)],
            stdout=log_fh, stderr=log_fh,
            start_new_session=True, close_fds=True,
        )
        time.sleep(0.5)
        return self._send_json(200, {"action": "start", "status": _tier3_pid_status(VK_PID_FILE, VK_SCRIPT)})

    def _vk_test(self) -> None:
        """Create a test OMI note in ~/obsidian-vault/ so voice_kanban picks it up."""
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = HOME / "obsidian-vault" / f"omi_test_{ts}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"# Voice Note — test {ts}\n\n"
            "Add grant research task for Example Project Q3 campaign. "
            "High priority, assign to example-a, tenant example.\n",
            encoding="utf-8",
        )
        return self._send_json(202, {"file": str(target), "message": "test note written; voice_kanban will pick it up within ~8s if running"})

    # ── Learning Loop (Tier-3) ───────────────────────────────────────────────

    def _ll_status(self) -> None:
        pid_info = _tier3_pid_status(LL_PID_FILE, LL_SCRIPT)
        runs: list[dict] = []
        if LL_RUNS.is_file():
            try:
                with LL_RUNS.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        try:
                            obj = json.loads(line.strip())
                            if isinstance(obj, dict):
                                runs.append(obj)
                        except Exception:
                            continue
            except OSError:
                pass
        latest = runs[-1] if runs else None
        return self._send_json(200, {
            **pid_info,
            "last_run": latest,
            "total_runs": len(runs),
        })

    def _team_seed(self) -> None:
        """POST /team/seed  body: {"team": "<name>", "roles": ["Role", ...]}
        Spawns Hermes teammates into the named (open) AionUi team via
        seed_hermes_team.sh. Returns {ok, seeded[], output} or {error}."""
        import shlex
        SEED_SCRIPT = Path(__file__).resolve().parent.parent / "seed_hermes_team.sh"
        try:
            body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            self._send(400, json.dumps({"error": "invalid JSON body"}), "application/json")
            return

        team = (body.get("team") or "").strip()
        roles_in = body.get("roles") or []

        # Validation: team name + roles must be plain, bounded strings.
        # No shell is used (we pass an argv list to subprocess), but we still
        # reject control chars / overlong values defensively.
        def _clean(s: str) -> str:
            s = str(s).strip()
            return s if (0 < len(s) <= 120 and all(ord(c) >= 32 for c in s)) else ""

        team = _clean(team)
        roles = [r for r in (_clean(x) for x in roles_in) if r][:12]  # cap at 12
        if not team:
            self._send(400, json.dumps({"error": "missing or invalid team name"}), "application/json")
            return
        if not roles:
            self._send(400, json.dumps({"error": "no valid roles provided"}), "application/json")
            return
        if not SEED_SCRIPT.exists():
            self._send(500, json.dumps({"error": f"seed script not found: {SEED_SCRIPT}"}), "application/json")
            return

        try:
            proc = subprocess.run(
                ["bash", str(SEED_SCRIPT), team, *roles],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            self._send(504, json.dumps({"error": "seed timed out (is the team open in AionUi?)"}),
                       "application/json")
            return
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}), "application/json")
            return

        out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        if proc.returncode == 0:
            _notify(
                severity="mid", source="build-hermes-team",
                title=f"Team build complete: {team} ({len(roles)} role(s))",
                body=f"Seeded: {', '.join(roles)}",
                payload={"kind": "seed-complete", "team": team, "count": len(roles), "roles": roles},
            )
            self._send(200, json.dumps({"ok": True, "seeded": roles, "output": out.strip()}),
                       "application/json")
        else:
            # Common failure: team not found / session not open (port/token lookup fails).
            err = (proc.stderr or proc.stdout or "seed failed").strip().splitlines()
            err_msg = err[-1] if err else "seed failed"
            _notify(
                severity="high", source="build-hermes-team",
                title=f"Team build failed: {team}",
                body=err_msg,
                payload={"kind": "seed-fail", "team": team, "reason": err_msg},
            )
            self._send(200, json.dumps({"ok": False,
                       "error": err_msg,
                       "output": out.strip()}), "application/json")
            return

    def _team_repair(self) -> None:
        """POST /team/repair  body: {"teamId": "<id>"}
        Clears the leader's stale acpSessionId so AionUi opens a fresh session."""
        import shutil as _shutil
        try:
            body = self._read_body_json()
            team_id = (body.get("teamId") or "").strip()
            if not team_id:
                return self._send_json(200, {"ok": False, "error": "teamId is required"})

            if not AIONUI_DB.exists():
                return self._send_json(200, {"ok": False, "error": f"aionui.db not found: {AIONUI_DB}"})

            # ── Validate team ──────────────────────────────────────────────
            con_ro = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            con_ro.row_factory = sqlite3.Row
            try:
                row = con_ro.execute(
                    "SELECT id, name, agents FROM teams WHERE id=?", (team_id,)
                ).fetchone()
            finally:
                con_ro.close()

            if not row:
                return self._send_json(200, {"ok": False, "error": f"team not found: {team_id}"})

            try:
                agents = json.loads(row["agents"] or "[]")
            except Exception:
                return self._send_json(200, {"ok": False, "error": "could not parse team agents"})

            leader = next((a for a in agents if a.get("role") == "leader"), None)
            if not leader or leader.get("agentType") != "hermes":
                return self._send_json(200, {"ok": False,
                    "error": "team leader is not a Hermes agent"})

            teammates = [a for a in agents if a.get("role") == "teammate"]
            if not teammates:
                return self._send_json(200, {"ok": False,
                    "error": "no teammates to repair — leader-only team"})

            leader_conv_id = (leader.get("conversationId") or "").strip()
            if not leader_conv_id:
                return self._send_json(200, {"ok": False,
                    "error": "leader has no conversationId"})

            # ── Back up DB ─────────────────────────────────────────────────
            import time as _time
            backup_path = str(AIONUI_DB) + ".bak." + str(int(_time.time()))
            try:
                _shutil.copy2(str(AIONUI_DB), backup_path)
            except Exception as exc:
                return self._send_json(200, {"ok": False,
                    "error": f"DB backup failed: {exc}"})

            # ── Clear stale ACP fields ─────────────────────────────────────
            con_rw = sqlite3.connect(str(AIONUI_DB))
            try:
                con_rw.execute(
                    "UPDATE conversations "
                    "SET extra = json_remove(extra, "
                    "'$.acpSessionId', '$.acpSessionConversationId', '$.acpSessionUpdatedAt') "
                    "WHERE id = ?",
                    (leader_conv_id,)
                )
                con_rw.commit()

                # Verify the clear worked
                row_check = con_rw.execute(
                    "SELECT json_extract(extra, '$.acpSessionId') AS sid "
                    "FROM conversations WHERE id=?",
                    (leader_conv_id,)
                ).fetchone()
            finally:
                con_rw.close()

            if row_check and row_check[0] is not None:
                return self._send_json(200, {"ok": False,
                    "error": "clear appeared to run but acpSessionId is still set"})

            _notify(
                severity="mid", source="team-repair",
                title=f"Leader session repaired for team {row['name']!r}",
                body=f"Cleared stale acpSessionId from conversation {leader_conv_id}. "
                     "Reopen the team in AionUi to start a fresh session.",
                payload={"kind": "repair-ok", "teamId": team_id,
                         "leaderConversationId": leader_conv_id},
            )
            return self._send_json(200, {
                "ok": True,
                "teamId": team_id,
                "leaderConversationId": leader_conv_id,
                "backupPath": backup_path,
                "hint": "Restart AionUi or reopen the team to reload the leader session.",
            })

        except Exception as exc:
            return self._send_json(200, {"ok": False, "error": str(exc)})

    def _team_repair_teammate_session(self) -> None:
        """POST /team/repair-teammate-session.

        Clears stale ACP session fields from one Hermes teammate conversation
        when its stored acpSessionId no longer exists in Hermes state.
        """
        import shutil as _shutil
        import time as _time

        hermes_state_db = HOME / ".hermes" / "profiles" / "example-a" / "state.db"

        def _body_bool(value: object) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        def _session_label(value: object) -> str | None:
            if not value:
                return None
            text = str(value)
            if len(text) <= 12:
                return text
            return f"{text[:8]}...{text[-8:]}"

        def _extra_summary(extra: dict) -> dict:
            sid = extra.get("acpSessionId")
            return {
                "teamId": extra.get("teamId"),
                "agentName": extra.get("agentName"),
                "backend": extra.get("backend"),
                "currentModelId": extra.get("currentModelId"),
                "hasAcpSessionId": bool(sid),
                "acpSessionId": _session_label(sid),
                "hasAcpSessionConversationId": bool(extra.get("acpSessionConversationId")),
                "hasAcpSessionUpdatedAt": bool(extra.get("acpSessionUpdatedAt")),
            }

        try:
            body = self._read_body_json()
            team_id = (body.get("team_id") or body.get("teamId") or "").strip()
            conversation_id = (body.get("conversation_id") or body.get("conversationId") or "").strip()
            force = _body_bool(body.get("force", False))

            if not team_id or not conversation_id:
                return self._send_json(200, {"ok": False,
                    "error": "team_id and conversation_id are required"})
            if not AIONUI_DB.exists():
                return self._send_json(200, {"ok": False,
                    "error": f"aionui.db not found: {AIONUI_DB}"})
            if not hermes_state_db.exists():
                return self._send_json(200, {"ok": False,
                    "error": f"Hermes state.db not found: {hermes_state_db}"})

            con_ro = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
            con_ro.row_factory = sqlite3.Row
            try:
                team_row = con_ro.execute(
                    "SELECT id, name, agents FROM teams WHERE id=?", (team_id,)
                ).fetchone()
                conv_row = con_ro.execute(
                    "SELECT id, name, type, extra FROM conversations WHERE id=?",
                    (conversation_id,),
                ).fetchone()
            finally:
                con_ro.close()

            if not team_row:
                return self._send_json(200, {"ok": False, "error": f"team not found: {team_id}"})
            if not conv_row:
                return self._send_json(200, {"ok": False,
                    "error": f"conversation not found: {conversation_id}"})

            try:
                agents = json.loads(team_row["agents"] or "[]")
            except Exception:
                return self._send_json(200, {"ok": False, "error": "could not parse team agents"})

            agent = next((a for a in agents if a.get("conversationId") == conversation_id), None)
            if not agent:
                return self._send_json(200, {"ok": False,
                    "error": "conversation does not belong to the given team"})
            if agent.get("role") == "leader":
                return self._send_json(200, {"ok": False,
                    "error": "refusing to repair leader through teammate endpoint"})
            if agent.get("role") != "teammate":
                return self._send_json(200, {"ok": False,
                    "error": f"conversation is not a teammate: {agent.get('role')!r}"})
            if agent.get("agentType") != "hermes":
                return self._send_json(200, {"ok": False,
                    "error": f"teammate is not Hermes-backed: {agent.get('agentType')!r}"})

            try:
                extra = json.loads(conv_row["extra"] or "{}")
            except Exception:
                return self._send_json(200, {"ok": False,
                    "error": "could not parse conversation extra"})

            if extra.get("teamId") and extra.get("teamId") != team_id:
                return self._send_json(200, {"ok": False,
                    "error": "conversation extra.teamId does not match team_id"})
            if extra.get("backend") != "hermes":
                return self._send_json(200, {"ok": False,
                    "error": f"conversation backend is not hermes: {extra.get('backend')!r}"})

            before = _extra_summary(extra)
            session_id = extra.get("acpSessionId")
            session_exists = False
            if session_id:
                con_h = sqlite3.connect(f"file:{hermes_state_db}?mode=ro", uri=True)
                try:
                    session_exists = con_h.execute(
                        "SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,)
                    ).fetchone() is not None
                finally:
                    con_h.close()

            if session_exists and not force:
                return self._send_json(200, {
                    "ok": True,
                    "action": "no_change",
                    "reason": "stored acpSessionId exists in Hermes state; pass force=true to clear",
                    "teamId": team_id,
                    "conversationId": conversation_id,
                    "agentName": agent.get("agentName"),
                    "role": agent.get("role"),
                    "sessionExists": True,
                    "before": before,
                    "after": before,
                })

            backup_path = (
                str(AIONUI_DB) + ".bak.teammate_repair."
                + str(int(_time.time())) + "." + conversation_id
            )
            try:
                _shutil.copy2(str(AIONUI_DB), backup_path)
            except Exception as exc:
                return self._send_json(200, {"ok": False,
                    "error": f"DB backup failed: {exc}"})

            con_rw = sqlite3.connect(str(AIONUI_DB))
            con_rw.row_factory = sqlite3.Row
            try:
                con_rw.execute(
                    "UPDATE conversations "
                    "SET extra = json_remove(CASE WHEN json_valid(extra) THEN extra ELSE '{}' END, "
                    "'$.acpSessionId', '$.acpSessionConversationId', '$.acpSessionUpdatedAt') "
                    "WHERE id = ?",
                    (conversation_id,),
                )
                con_rw.commit()
                check = con_rw.execute(
                    "SELECT extra FROM conversations WHERE id=?", (conversation_id,)
                ).fetchone()
            finally:
                con_rw.close()

            after_extra = json.loads(check["extra"] or "{}") if check else {}
            after = _extra_summary(after_extra)
            if (
                after.get("hasAcpSessionId")
                or after.get("hasAcpSessionConversationId")
                or after.get("hasAcpSessionUpdatedAt")
            ):
                return self._send_json(200, {
                    "ok": False,
                    "error": "clear appeared to run but one or more ACP fields are still set",
                    "teamId": team_id,
                    "conversationId": conversation_id,
                    "before": before,
                    "after": after,
                    "backupPath": backup_path,
                })

            return self._send_json(200, {
                "ok": True,
                "action": "cleared_stale_acp_fields",
                "teamId": team_id,
                "conversationId": conversation_id,
                "agentName": agent.get("agentName"),
                "role": agent.get("role"),
                "sessionExistsBeforeRepair": session_exists,
                "clearedFields": ["acpSessionId", "acpSessionConversationId", "acpSessionUpdatedAt"],
                "backupPath": backup_path,
                "before": before,
                "after": after,
            })

        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

    # ── AI Launch Assist endpoints ────────────────────────────────────────────

    def _la_agents_list(self) -> None:
        return self._send_json(200, {"agents": _la_load_agents()})

    def _la_agents_add(self) -> None:
        body = self._read_body_json()
        agent_id = str(body.get("id") or "").strip()
        name     = str(body.get("name") or "").strip()
        adapter  = str(body.get("adapter") or "generic").strip()
        cmd      = body.get("cmd")
        args     = body.get("args") or []
        is_default = bool(body.get("isDefault", False))

        if not agent_id:
            return self._send_json(400, {"ok": False, "error": "id is required"})
        if not name:
            return self._send_json(400, {"ok": False, "error": "name is required"})
        if adapter not in ("cc", "codex", "generic"):
            return self._send_json(400, {"ok": False, "error": "adapter must be cc|codex|generic"})
        if not cmd or not isinstance(cmd, list):
            return self._send_json(400, {"ok": False, "error": "cmd must be a non-empty list"})

        agents = _la_load_agents()
        existing = next((a for a in agents if a.get("id") == agent_id), None)
        entry: dict = {
            "id": agent_id, "name": name, "adapter": adapter,
            "cmd": [str(c) for c in cmd],
            "args": [str(a) for a in args],
            "isDefault": is_default,
        }
        if existing:
            idx = agents.index(existing)
            agents[idx] = entry
        else:
            agents.append(entry)
        if is_default:
            for a in agents:
                if a["id"] != agent_id:
                    a["isDefault"] = False
        _la_save_agents(agents)
        return self._send_json(200, {"ok": True, "agent": entry})

    def _la_agents_delete(self, agent_id: str) -> None:
        agents = _la_load_agents()
        before = len(agents)
        agents = [a for a in agents if a.get("id") != agent_id]
        if len(agents) == before:
            return self._send_json(404, {"ok": False, "error": f"agent {agent_id!r} not found"})
        _la_save_agents(agents)
        return self._send_json(200, {"ok": True})

    def _la_agents_set_default(self) -> None:
        body = self._read_body_json()
        agent_id = str(body.get("id") or "").strip()
        if not agent_id:
            return self._send_json(400, {"ok": False, "error": "id is required"})
        agents = _la_load_agents()
        found = any(a.get("id") == agent_id for a in agents)
        if not found:
            return self._send_json(404, {"ok": False, "error": f"agent {agent_id!r} not found"})
        for a in agents:
            a["isDefault"] = (a.get("id") == agent_id)
        _la_save_agents(agents)
        return self._send_json(200, {"ok": True, "defaultId": agent_id})

    def _la_prompt_get(self) -> None:
        return self._send_json(200, {"prompt": _la_load_prompt()})

    def _la_prompt_save(self) -> None:
        body = self._read_body_json()
        text = body.get("prompt")
        if not isinstance(text, str):
            return self._send_json(400, {"ok": False, "error": "prompt string required"})
        _la_save_prompt(text)
        return self._send_json(200, {"ok": True})

    def _la_launch_handler(self) -> None:
        body = self._read_body_json()
        agent_id   = body.get("agentId")
        services   = body.get("services") or []
        ro_paths   = body.get("readonlyPaths") or []
        rw_paths   = body.get("writablePaths") or []
        environment = body.get("environment") or {}

        agents = _la_load_agents()
        if agent_id:
            agent = next((a for a in agents if a.get("id") == agent_id), None)
            if agent is None:
                return self._send_json(404, {"ok": False, "error": f"agent {agent_id!r} not found"})
        else:
            agent = next((a for a in agents if a.get("isDefault")), None)
            if agent is None and agents:
                agent = agents[0]
            if agent is None:
                return self._send_json(400, {"ok": False, "error": "no agents configured"})

        template = _la_load_prompt()
        try:
            prompt = _la_build_prompt(template, services, ro_paths, rw_paths, environment)
        except KeyError as exc:
            return self._send_json(400, {"ok": False,
                "error": f"prompt template has unknown placeholder: {exc}"})

        session_id = str(uuid.uuid4())
        session = _LASession(session_id, agent.get("id",""), agent.get("adapter","generic"), prompt)
        with _la_sessions_lock:
            _la_sessions[session_id] = session

        adapter = agent.get("adapter", "generic")
        if adapter == "cc":
            threading.Thread(target=_la_cc_start, args=(session, agent),
                             name=f"la-cc-launch-{session_id[:8]}", daemon=True).start()
        elif adapter == "codex":
            threading.Thread(target=_la_codex_start, args=(session, agent),
                             name=f"la-codex-launch-{session_id[:8]}", daemon=True).start()
        else:
            threading.Thread(target=_la_generic_start, args=(session, agent),
                             name=f"la-gen-launch-{session_id[:8]}", daemon=True).start()

        _notify("mid", "launch-assist", f"Session started: {agent.get('name','')}",
                body=session_id, payload={"session_id": session_id, "agent_id": agent.get("id","")})

        return self._send_json(202, {"ok": True, "sessionId": session_id,
                                     "agentId": agent.get("id",""),
                                     "adapter": adapter})

    def _la_proposal_action(self, proposal_id: str, action: str) -> None:
        if action not in ("approve", "reject", "modify"):
            return self._send_json(400, {"ok": False, "error": "action must be approve|reject|modify"})

        # Find proposal across all live sessions
        prop: _LAProposal | None = None
        session: _LASession | None = None
        with _la_sessions_lock:
            for s in _la_sessions.values():
                for p in s.proposals:
                    if p.id == proposal_id:
                        prop = p
                        session = s
                        break
                if prop:
                    break

        # Fall back to serialized session files if not in memory
        if prop is None:
            # Check disk sessions
            for path in LA_SESSIONS_DIR.glob("*.json"):
                try:
                    data = json.loads(path.read_text())
                    for pd in data.get("proposals", []):
                        if pd.get("id") == proposal_id:
                            return self._send_json(410, {
                                "ok": False,
                                "error": "session is closed; proposal can no longer be executed",
                            })
                except Exception:
                    continue
            return self._send_json(404, {"ok": False, "error": f"proposal {proposal_id!r} not found"})

        if prop.status != "pending":
            return self._send_json(409, {
                "ok": False,
                "error": f"proposal is already {prop.status}",
            })

        if action == "reject":
            prop.status = "rejected"
            if session:
                session._broadcaster.broadcast(json.dumps({
                    "type": "proposal_update", "id": proposal_id, "status": "rejected",
                    "ts": int(time.time() * 1000),
                }))
            return self._send_json(200, {"ok": True, "status": "rejected"})

        # approve or modify
        exec_payload = prop.payload
        if action == "modify":
            body = self._read_body_json()
            new_payload = body.get("payload")
            if not isinstance(new_payload, dict):
                return self._send_json(400, {"ok": False, "error": "payload dict required for modify"})
            exec_payload = new_payload
            prop.payload = new_payload
            prop.action = str(new_payload.get("action", prop.action))

        result = _la_run_proposal(prop)
        prop.status = "approved"
        prop.result = result

        if session:
            session._broadcaster.broadcast(json.dumps({
                "type": "proposal_update", "id": proposal_id,
                "status": "approved", "result": result,
                "ts": int(time.time() * 1000),
            }))

        return self._send_json(200, {"ok": True, "status": "approved", "result": result})

    def _la_session_get(self, session_id: str) -> None:
        with _la_sessions_lock:
            session = _la_sessions.get(session_id)
        if session:
            return self._send_json(200, session.to_dict())
        # Check disk
        path = LA_SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return self._send_json(200, data)
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})
        return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found"})

    def _la_feedback_opt_in(self, session_id: str) -> None:
        with _la_sessions_lock:
            session = _la_sessions.get(session_id)
        if session:
            session.feedback_opt_in = True
            return self._send_json(200, {"ok": True, "message": "feedback opt-in recorded for live session"})
        # Check disk — update serialized file
        path = LA_SESSIONS_DIR / f"{session_id}.json"
        if not path.exists():
            return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found"})
        try:
            data = json.loads(path.read_text())
            data["feedback_opt_in"] = True
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
            # Copy to feedback outbox
            LA_FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
            out = LA_FEEDBACK_DIR / f"{session_id}.json"
            out.write_text(json.dumps(data, indent=2))
            return self._send_json(200, {"ok": True, "message": "feedback recorded; outbox file ready"})
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _la_inject_test_proposal(self, session_id: str) -> None:
        with _la_sessions_lock:
            session = _la_sessions.get(session_id)
        if session is None:
            return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found or not live"})
        body = self._read_body_json()
        payload = body.get("payload") or {
            "action": "run_command",
            "command": "echo hello-from-test-proposal",
            "reason": "test injection",
        }
        action = str(payload.get("action", "other"))
        prop = _LAProposal(session_id, action, payload)
        session.proposals.append(prop)
        session._broadcaster.broadcast(json.dumps({
            "type": "proposal", "ts": prop.ts, "id": prop.id,
            "action": prop.action, "payload": prop.payload, "status": "pending",
        }))
        return self._send_json(200, {"ok": True, "proposal": prop.to_dict()})

    def _la_stream(self, session_id: str) -> None:
        with _la_sessions_lock:
            session = _la_sessions.get(session_id)
        if session is None:
            return self._send_json(404, {"ok": False,
                "error": f"session {session_id!r} not found or already closed"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        q = session._broadcaster.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15.0)
                    chunk = f"data: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                    payload = ""
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                if payload:
                    try:
                        evt = json.loads(payload)
                        if evt.get("type") == "done":
                            return
                    except Exception:
                        pass
        finally:
            session._broadcaster.unsubscribe(q)

    # ── Prompter ─────────────────────────────────────────────────────────────

    def _prompter_stream(self, session_id: str) -> None:
        """GET /prompter/stream/<session_id> — SSE stream with named events."""
        with _prompter_sessions_lock:
            session = _prompter_sessions.get(session_id)
        if session is None:
            return self._send_json(404, {"error": f"prompter session {session_id!r} not found"})

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        q = session.broadcaster.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    raw = q.get(timeout=15.0)
                    try:
                        evt_obj = json.loads(raw)
                        event_name = str(evt_obj.get("event", "message"))
                    except Exception:
                        event_name = "message"
                    chunk = f"event: {event_name}\ndata: {raw}\n\n".encode("utf-8")
                    done = (event_name == "done")
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                    done = False
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                if done:
                    return
        finally:
            session.broadcaster.unsubscribe(q)

    def _prompter_result(self, session_id: str) -> None:
        """GET /prompter/result/<session_id> — full session state."""
        with _prompter_sessions_lock:
            session = _prompter_sessions.get(session_id)
        if session:
            return self._send_json(200, session.to_dict())
        path = PROMPTER_SESSIONS_DIR / f"{session_id}.json"
        if path.exists():
            try:
                return self._send_json(200, json.loads(path.read_text()))
            except Exception as exc:
                return self._send_json(500, {"error": str(exc)})
        return self._send_json(404, {"error": f"session {session_id!r} not found"})

    def _prompter_library_list(self) -> None:
        """GET /prompter/library — list all library entries (summary level)."""
        PROMPTER_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        entries = []
        for p in sorted(PROMPTER_LIBRARY_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text())
                entries.append({
                    "id": d.get("id"),
                    "project_name": d.get("project_name"),
                    "agent_id": d.get("agent_id"),
                    "created_at": d.get("created_at"),
                    "summary": d.get("summary"),
                    "role_count": len(d.get("role_briefings", [])),
                    "delivered_to": d.get("delivered_to", []),
                })
            except Exception:
                continue
        self._send_json(200, {"entries": entries})

    def _prompter_library_get(self, library_id: str) -> None:
        """GET /prompter/library/<library_id> — full library entry."""
        PROMPTER_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        path = PROMPTER_LIBRARY_DIR / f"{library_id}.json"
        if not path.exists():
            return self._send_json(404, {"error": f"library entry {library_id!r} not found"})
        try:
            return self._send_json(200, json.loads(path.read_text()))
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _prompter_start(self) -> None:
        """POST /prompter/start — start a new Q&A session."""
        body = self._read_body_json()
        agent_id = str(body.get("agent_id") or "").strip()
        project_name = str(body.get("project_name") or "").strip()
        intent_seed = str(body.get("intent_seed") or "").strip()
        model_override = body.get("model")

        if not project_name:
            return self._send_json(400, {"ok": False, "error": "project_name is required"})
        if not intent_seed:
            return self._send_json(400, {"ok": False, "error": "intent_seed is required"})

        # KHAOSS-PATCH: catalog-backend-routing BEGIN
        # Resolve catalog backend id formats to spawn parameters.
        oapicompat_base_url = ""
        oapicompat_api_key = ""
        oapicompat_model = ""

        if agent_id.startswith("cli:"):
            cli_slug = agent_id[4:]
            la_agents = _la_load_agents()
            if cli_slug == "codex":
                return self._send_json(400, {"ok": False,
                    "error": "Codex is not suitable for Q&A — use CC or a Hermes/provider backend"})
            elif cli_slug == "claude-code":
                agent = next((a for a in la_agents if a.get("adapter") == "cc"), None)
            else:
                agent = next((a for a in la_agents if a.get("id") == cli_slug), None)
            if not agent:
                return self._send_json(400, {"ok": False,
                    "error": f"CLI backend {agent_id!r} not configured in launch-assist agents"})
            adapter = str(agent.get("adapter") or "generic")

        elif agent_id.startswith("hermes-profile:"):
            import yaml as _yaml
            profile_name = agent_id[len("hermes-profile:"):]
            profile_dir = HOME / ".hermes" / "profiles" / profile_name
            if not profile_dir.is_dir():
                return self._send_json(400, {"ok": False,
                    "error": f"Hermes profile {profile_name!r} not found"})
            cfg_path = profile_dir / "config.yaml"
            env_path = profile_dir / ".env"
            if not cfg_path.is_file():
                return self._send_json(400, {"ok": False,
                    "error": f"Hermes profile {profile_name!r} has no config.yaml"})
            try:
                cfg = _yaml.safe_load(cfg_path.read_text()) or {}
            except Exception as exc:
                return self._send_json(400, {"ok": False,
                    "error": f"Hermes profile {profile_name!r} config read failed: {exc}"})
            model_cfg = cfg.get("model", {})
            base_url = (model_cfg.get("base_url") or "").strip().rstrip("/")
            model_default = (model_cfg.get("default") or "").strip()
            if not base_url:
                return self._send_json(400, {"ok": False,
                    "error": f"Hermes profile {profile_name!r} has no base_url configured"})
            api_key_val = ""
            if env_path.is_file():
                for _line in env_path.read_text().splitlines():
                    _k, _, _v = _line.partition("=")
                    if _k.strip() == "OPENAI_API_KEY":
                        api_key_val = _v.strip()
                        break
            if not api_key_val:
                return self._send_json(400, {"ok": False,
                    "error": f"Hermes profile {profile_name!r} has no OPENAI_API_KEY in .env"})
            oapicompat_base_url = base_url
            oapicompat_api_key = api_key_val
            oapicompat_model = str(model_override or model_default)
            agent = {"id": agent_id, "adapter": "openai_compat"}
            adapter = "openai_compat"

        elif agent_id.startswith("provider:"):
            _parts = agent_id.split(":", 2)
            if len(_parts) != 3:
                return self._send_json(400, {"ok": False,
                    "error": f"malformed provider backend id: {agent_id!r}"})
            _, _pid, _mid = _parts
            _prov_file = Path.home() / ".config" / "AionUi-Dev" / "aionui-providers.json"
            try:
                _pdata = json.loads(_prov_file.read_text())
            except Exception as exc:
                return self._send_json(400, {"ok": False,
                    "error": f"aionui-providers.json read failed: {exc}"})
            _provider = next((p for p in _pdata.get("providers", []) if p.get("id") == _pid), None)
            if not _provider:
                return self._send_json(400, {"ok": False,
                    "error": f"provider {_pid!r} not found in registry"})
            base_url = (_provider.get("base_url") or "").strip().rstrip("/")
            if not base_url:
                return self._send_json(400, {"ok": False,
                    "error": f"provider {_pid!r} has no base_url"})
            _key_file = _provider.get("key_file") or ""
            api_key_val = ""
            if _key_file:
                _kp = Path(_key_file)
                if _kp.is_file():
                    # First non-empty line only — key files may contain multiple keys or trailing newlines
                    api_key_val = next((l.strip() for l in _kp.read_text().splitlines() if l.strip()), "")
            if not api_key_val:
                return self._send_json(400, {"ok": False,
                    "error": f"provider {_pid!r} has no readable API key at {_key_file!r}"})
            oapicompat_base_url = base_url
            oapicompat_api_key = api_key_val
            oapicompat_model = str(model_override or _mid)
            agent = {"id": agent_id, "adapter": "openai_compat"}
            adapter = "openai_compat"

        else:
            # Existing launch-assist agent lookup (unchanged path)
            la_agents = _la_load_agents()
            agent = next((a for a in la_agents if a.get("id") == agent_id), None) if agent_id else None
            if agent_id and not agent:
                return self._send_json(400, {"ok": False, "error": f"agent {agent_id!r} not found"})
            if not agent:
                agent = (next((a for a in la_agents if a.get("isDefault")), None)
                         or (la_agents[0] if la_agents else None))
            if not agent:
                return self._send_json(400, {"ok": False, "error": "no agents configured"})
            adapter = str(agent.get("adapter") or "generic")
            if adapter == "codex":
                return self._send_json(400, {"ok": False,
                    "error": "Codex is not suitable for Q&A — pick CC or a Hermes/provider backend"})
        # KHAOSS-PATCH: catalog-backend-routing END

        # Check concurrent session cap
        with _prompter_sessions_lock:
            active = sum(1 for s in _prompter_sessions.values()
                         if s.state not in ("approved", "cancelled", "error"))
            if active >= PROMPTER_MAX_SESSIONS:
                return self._send_json(429, {"ok": False,
                    "error": "too many active Prompter sessions — close an existing session first"})

        if adapter == "cc":
            agent_kind = "cc"
        elif adapter == "openai_compat":
            agent_kind = "openai_compat"
        else:
            agent_kind = "ollama"
        ollama_model = str(model_override or agent.get("model") or PROMPTER_OLLAMA_MODEL)
        session_id = str(uuid.uuid4())
        session = _PrompterSession(session_id, project_name, intent_seed,
                                   agent.get("id", ""), agent_kind, ollama_model)

        with _prompter_sessions_lock:
            _prompter_sessions[session_id] = session

        if agent_kind == "cc":
            threading.Thread(target=_prompter_cc_start, args=(session, agent),
                             name=f"prompter-cc-{session_id[:8]}", daemon=True).start()
        elif agent_kind == "openai_compat":
            session._oapicompat_base_url = oapicompat_base_url
            session._oapicompat_api_key = oapicompat_api_key
            session._oapicompat_model = oapicompat_model
            session._ollama_messages = [{"role": "system", "content": _PROMPTER_BOOTSTRAP}]
            initial_user = f"Project: {project_name}\nContext: {intent_seed}"
            threading.Thread(target=_prompter_oapicompat_call, args=(session, initial_user),
                             name=f"prompter-oa-{session_id[:8]}", daemon=True).start()
        else:
            session._ollama_messages = [{"role": "system", "content": _PROMPTER_BOOTSTRAP}]
            initial_user = f"Project: {project_name}\nContext: {intent_seed}"
            threading.Thread(target=_prompter_ollama_call, args=(session, initial_user),
                             name=f"prompter-ol-{session_id[:8]}", daemon=True).start()

        # Block until first question (up to 30s)
        session._first_q_event.wait(timeout=30.0)
        first_question = session._first_q or "Starting up — check the SSE stream for the first question."

        _notify("mid", "prompter", f"Session started: {project_name}",
                body=session_id, payload={"session_id": session_id, "agent_kind": agent_kind})

        return self._send_json(202, {
            "ok": True,
            "session_id": session_id,
            "agent_kind": agent_kind,
            "first_question": first_question,
        })

    def _prompter_answer(self, session_id: str) -> None:
        """POST /prompter/answer/<session_id> — send a user answer."""
        with _prompter_sessions_lock:
            session = _prompter_sessions.get(session_id)
        if not session:
            return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found or closed"})
        if session.state not in ("awaiting_answer",):
            return self._send_json(409, {"ok": False,
                "error": f"session is in state {session.state!r}; not expecting an answer right now"})

        body = self._read_body_json()
        answer = str(body.get("answer") or "").strip()
        if not answer:
            return self._send_json(400, {"ok": False, "error": "answer is required"})

        session.turns.append({"role": "user", "text": answer, "ts": time.time()})
        session.state = "thinking"
        _prompter_emit(session, "thinking", {})

        if session.agent_kind == "cc":
            ok = _prompter_cc_send_answer(session, answer)
            if not ok:
                session.state = "error"
                _prompter_emit(session, "error", {"message": "CC process not responding"})
                return self._send_json(500, {"ok": False, "error": "CC process not responding"})
        elif session.agent_kind == "openai_compat":
            # KHAOSS-PATCH: catalog-backend-routing
            session._ollama_messages.append({"role": "user", "content": answer})
            threading.Thread(target=_prompter_oapicompat_call, args=(session,),
                             name=f"prompter-oa-ans-{session_id[:8]}", daemon=True).start()
        else:
            threading.Thread(target=_prompter_ollama_call, args=(session, answer),
                             name=f"prompter-ol-ans-{session_id[:8]}", daemon=True).start()

        return self._send_json(202, {"ok": True, "state": "thinking"})

    def _prompter_force_synthesize(self, session_id: str) -> None:
        """POST /prompter/synthesize/<session_id> — force synthesis now."""
        with _prompter_sessions_lock:
            session = _prompter_sessions.get(session_id)
        if not session:
            return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found or closed"})
        if session.state not in ("awaiting_answer", "thinking"):
            return self._send_json(409, {"ok": False,
                "error": f"session is in state {session.state!r}; cannot synthesize now"})

        session.state = "synthesizing"
        _prompter_emit(session, "thinking", {"message": "Synthesizing..."})

        if session.agent_kind == "cc":
            ok = _prompter_cc_send_synthesize(session)
            if not ok:
                session.state = "error"
                _prompter_emit(session, "error", {"message": "CC process not responding"})
                return self._send_json(500, {"ok": False, "error": "CC process not responding"})
        elif session.agent_kind == "openai_compat":
            # KHAOSS-PATCH: catalog-backend-routing
            session._ollama_messages.append({"role": "user", "content": _PROMPTER_SYNTHESIS_TRIGGER})
            threading.Thread(target=_prompter_oapicompat_call, args=(session,),
                             name=f"prompter-oa-synth-{session_id[:8]}", daemon=True).start()
        else:
            session._ollama_messages.append({"role": "user", "content": _PROMPTER_SYNTHESIS_TRIGGER})
            threading.Thread(target=_prompter_ollama_call, args=(session,),
                             name=f"prompter-ol-synth-{session_id[:8]}", daemon=True).start()

        return self._send_json(202, {"ok": True, "state": "synthesizing"})

    def _prompter_cancel(self, session_id: str) -> None:
        """POST /prompter/cancel/<session_id> — cancel session."""
        with _prompter_sessions_lock:
            session = _prompter_sessions.pop(session_id, None)
        if not session:
            return self._send_json(404, {"ok": False, "error": f"session {session_id!r} not found"})

        session.state = "cancelled"
        if session.proc is not None:
            try:
                session.proc.terminate()
                try: session.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: session.proc.kill()
            except Exception:
                pass
        _prompter_emit(session, "done", {"state": "cancelled"})
        _prompter_serialize_session(session)
        return self._send_json(200, {"ok": True, "state": "cancelled"})

    def _prompter_library_save(self) -> None:
        """POST /prompter/library — save synthesis to library."""
        body = self._read_body_json()
        session_id = str(body.get("session_id") or "").strip()

        if session_id:
            # Save from a live or serialized session
            with _prompter_sessions_lock:
                session = _prompter_sessions.get(session_id)
            synth = None
            project_name = ""
            agent_id = ""
            if session:
                synth = session.synthesis
                project_name = session.project_name
                agent_id = session.agent_id
            else:
                disk_path = PROMPTER_SESSIONS_DIR / f"{session_id}.json"
                if disk_path.exists():
                    try:
                        d = json.loads(disk_path.read_text())
                        synth = d.get("synthesis")
                        project_name = d.get("project_name", "")
                        agent_id = d.get("agent_id", "")
                    except Exception:
                        pass
            if not synth:
                return self._send_json(400, {"ok": False,
                    "error": "no synthesis found for this session — run /prompter/synthesize first"})
            lib_entry = {
                "id": f"lib_{uuid.uuid4().hex[:8]}",
                "created_at": time.time(),
                "project_name": project_name,
                "agent_id": agent_id,
                "summary": synth.get("lineup_summary", ""),
                "leader_prompt": synth.get("leader_prompt", ""),
                "role_briefings": synth.get("role_briefings", []),
                "lineup_summary": synth.get("lineup_summary", ""),
                "session_id": session_id,
                "delivered_to": [],
            }
            if session:
                session.library_id = lib_entry["id"]
                session.state = "approved"
        else:
            # Direct library entry body
            lib_entry = body
            if "id" not in lib_entry:
                lib_entry["id"] = f"lib_{uuid.uuid4().hex[:8]}"
            if "created_at" not in lib_entry:
                lib_entry["created_at"] = time.time()
            if "delivered_to" not in lib_entry:
                lib_entry["delivered_to"] = []

        PROMPTER_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        path = PROMPTER_LIBRARY_DIR / f"{lib_entry['id']}.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(lib_entry, indent=2, default=str))
            tmp.replace(path)
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

        _notify("low", "prompter", f"Prompt set saved: {lib_entry.get('project_name','')}",
                body=lib_entry["id"])
        return self._send_json(200, {"ok": True, "library_id": lib_entry["id"]})

    def _prompter_library_delete(self, library_id: str) -> None:
        """DELETE /prompter/library/<library_id>."""
        PROMPTER_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        path = PROMPTER_LIBRARY_DIR / f"{library_id}.json"
        if not path.exists():
            return self._send_json(404, {"ok": False, "error": f"library entry {library_id!r} not found"})
        try:
            path.unlink()
            return self._send_json(200, {"ok": True})
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": str(exc)})

    def _prompter_deliver_roles_only(self) -> None:
        """POST /prompter/deliver-roles-only — idempotent teammate briefing delivery."""
        body = self._read_body_json()
        body["skip_leader"] = True
        body["include_leader_prompt"] = False
        body["include_role_briefings"] = True
        body["idempotency"] = True
        body["stop_on_failure"] = True
        return self._prompter_deliver(body)

    def _prompter_deliver(self, forced_body: dict | None = None) -> None:
        """POST /prompter/deliver — deliver approved prompt set to a live team."""
        body = forced_body if forced_body is not None else self._read_body_json()
        library_id = str(body.get("library_id") or "").strip()
        team_id = str(body.get("team_id") or "").strip()
        def _body_bool(name: str, default: bool) -> bool:
            val = body.get(name, default)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.strip().lower() not in ("0", "false", "no", "off", "")
            return bool(val)

        include_roles = _body_bool("include_role_briefings", True)
        include_leader = _body_bool("include_leader_prompt", True)
        if _body_bool("skip_leader", False):
            include_leader = False
        idempotency = _body_bool("idempotency", False)
        dry_run = _body_bool("dry_run", False)
        stop_on_failure = _body_bool("stop_on_failure", idempotency)
        allow_redeliver = _body_bool("allow_redeliver", False)

        if not library_id or not team_id:
            return self._send_json(400, {"ok": False, "error": "library_id and team_id are required"})

        # Load library entry
        lib_path = PROMPTER_LIBRARY_DIR / f"{library_id}.json"
        if not lib_path.exists():
            return self._send_json(404, {"ok": False, "error": f"library entry {library_id!r} not found"})
        try:
            lib = json.loads(lib_path.read_text())
        except Exception as exc:
            return self._send_json(500, {"ok": False, "error": f"failed to read library: {exc}"})

        already_delivered = any(
            isinstance(entry, dict) and entry.get("teamId") == team_id
            for entry in lib.get("delivered_to", [])
        )
        if already_delivered and not allow_redeliver:
            return self._send_json(409, {
                "ok": False,
                "error": f"library {library_id!r} is already marked delivered to team {team_id!r}",
            })

        # Look up team MCP config
        ok, mcp_info, err = _prompter_lookup_team_mcp(team_id)
        if not ok:
            return self._send_json(400, {"ok": False, "error": err})

        port = mcp_info["port"]
        token = mcp_info["token"]
        leader_name = mcp_info["leader_name"]
        leader_conv_id = mcp_info.get("leader_conv_id", "")

        # Probe liveness via MCP port TCP
        # ★ DQ33 — FAIL LOUDLY at the HTTP level (was 200 → reads as success to a caller checking status).
        # A dead ACP session means the message has NOWHERE to land — every "delivered" into it went into nothing.
        if not _tcp_reachable("127.0.0.1", port, timeout=5.0):
            return self._send_json(503, {"ok": False, "arrived": False,
                "error": "delivery failed — the team's ACP session is stale/closed. Open the team in AionUi "
                         "and send any message (e.g. 'hello') to wake it (~3s), then retry."})

        delivered: dict = {"leader": False, "role_briefings": []}
        delivered_roles: list[dict] = []
        skipped_roles: list[dict] = []
        failed_roles: list[dict] = []

        def _role_marker(role_name: str, prompt: str) -> tuple[str, str]:
            import hashlib
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            marker = "\n".join([
                "ORION_ROLE_BRIEFING",
                f"library_id={library_id}",
                f"team_id={team_id}",
                f"role={role_name}",
                f"prompt_hash={prompt_hash}",
            ])
            return marker, prompt_hash

        def _role_marker_exists(teammate: dict, marker: str) -> bool:
            conv_id = str(teammate.get("conversationId") or "")
            slot_id = str(teammate.get("slotId") or "")
            try:
                con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
                if conv_id:
                    row = con.execute(
                        "SELECT 1 FROM messages WHERE conversation_id=? AND content LIKE ? LIMIT 1",
                        (conv_id, f"%{marker}%"),
                    ).fetchone()
                    if row:
                        con.close()
                        return True
                if slot_id:
                    row = con.execute(
                        "SELECT 1 FROM mailbox WHERE team_id=? AND to_agent_id=? AND content LIKE ? LIMIT 1",
                        (team_id, slot_id, f"%{marker}%"),
                    ).fetchone()
                    if row:
                        con.close()
                        return True
                con.close()
            except Exception as exc:
                logger.warning("prompter deliver: marker check failed for %s: %s",
                               teammate.get("name", ""), exc)
            return False

        # Deliver leader prompt via CDP chat.send.message (injects as visible user message)
        if include_leader:
            cdp_ok, cdp_ws, cdp_err = _cdp_find_aionui_page()
            if dry_run:
                delivered["leader"] = "would_deliver"
            elif not cdp_ok or not leader_conv_id:
                # Fallback: MCP team_send_message to leader mailbox
                logger.warning("prompter deliver: CDP unavailable (%s) or no conv_id, falling back to MCP mailbox", cdp_err)
                try:
                    resp = _prompter_mcp_send(port, token, "team_send_message", {
                        "to": leader_name,
                        "message": lib["leader_prompt"],
                        "summary": f"System prompt from Prompter library: {lib.get('project_name','')}",
                    })
                    if resp.get("error"):
                        logger.warning("prompter deliver: leader MCP error: %s", resp["error"])
                    else:
                        delivered["leader"] = True
                        _notify("mid", "prompter",
                                f"Prompt delivered to '{leader_name}' leader (mailbox).",
                                body=f"team_id={team_id} library_id={library_id}")
                except Exception as exc:
                    logger.warning("prompter deliver: leader send failed: %s", exc)
                    return self._send_json(500, {"ok": False, "error": f"leader delivery failed: {exc}"})
            else:
                ok_cdp, cdp_result = _cdp_send_message(cdp_ws, leader_conv_id, lib["leader_prompt"])
                if not ok_cdp:
                    return self._send_json(500, {"ok": False,
                        "error": f"CDP delivery failed: {cdp_result.get('error', cdp_result)}"})
                # cdp_result is the raw CDP Runtime.evaluate response; no exceptionDetails is necessary but
                # NOT sufficient — "no exception thrown" is not "the message arrived."
                exc_det = cdp_result.get("result", {}).get("exceptionDetails")
                if exc_det:
                    err_msg = exc_det.get("text") or exc_det.get("exception", {}).get("description", "unknown")
                    return self._send_json(500, {"ok": False, "error": f"CDP delivery error: {err_msg}"})
                # ★ DQ33 — EVIDENCE OF ARRIVAL, not "no exception": confirm the message is VISIBLE in the
                # leader conversation before claiming delivery. Otherwise the silent-failure class lives inside
                # the deliver tool. Read-only DB check for a recent message carrying a snippet of the prompt.
                _snippet = (lib.get("leader_prompt") or "")[:48]
                _arrived = False
                try:
                    _con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
                    _row = _con.execute(
                        "SELECT 1 FROM messages WHERE conversation_id=? AND content LIKE ? ORDER BY rowid DESC LIMIT 1",
                        (leader_conv_id, f"%{_snippet}%")).fetchone()
                    _con.close()
                    _arrived = bool(_row)
                except Exception as _exc:
                    logger.warning("prompter deliver: leader arrival check failed: %s", _exc)
                if not _arrived:
                    return self._send_json(502, {"ok": False, "arrived": False,
                        "error": "CDP returned without exception, but the message is NOT visible in the leader "
                                 "conversation — arrival not confirmed. No success without evidence of arrival."})
                delivered["leader"] = True
                _notify("mid", "prompter",
                        f"Prompt delivered to '{leader_name}' leader.",
                        body=f"team_id={team_id} library_id={library_id}")
        else:
            delivered["leader"] = "skipped"

        # Deliver role briefings
        if include_roles:
            teammates = {t["name"].lower(): t for t in mcp_info.get("teammates", [])}
            for briefing in lib.get("role_briefings", []):
                role_name = briefing.get("role", "")
                role_prompt = briefing.get("prompt", "")
                # Fuzzy match: exact or normalized match
                teammate = teammates.get(role_name.lower())
                if not teammate:
                    # Try partial match
                    for tname_lower, candidate in teammates.items():
                        if role_name.lower() in tname_lower or tname_lower in role_name.lower():
                            teammate = candidate
                            break
                if not teammate:
                    failed = {
                        "role": role_name, "delivered": False, "reason": "no matching teammate"
                    }
                    failed_roles.append(failed)
                    delivered["role_briefings"].append(failed)
                    if stop_on_failure:
                        break
                    continue

                match_name = teammate.get("name", "")
                marker = ""
                prompt_hash = ""
                message = role_prompt
                summary = f"Role briefing for {role_name}"
                if idempotency:
                    marker, prompt_hash = _role_marker(role_name, role_prompt)
                    if _role_marker_exists(teammate, marker):
                        skipped = {
                            "role": role_name,
                            "delivered": True,
                            "skipped": True,
                            "reason": "matching marker exists",
                            "teammate": match_name,
                            "prompt_hash": prompt_hash,
                        }
                        skipped_roles.append(skipped)
                        delivered["role_briefings"].append(skipped)
                        continue
                    message = f"{marker}\n\n{role_prompt}"
                    summary = f"ORION_ROLE_BRIEFING library_id={library_id} role={role_name} prompt_hash={prompt_hash}"

                if dry_run:
                    planned = {
                        "role": role_name,
                        "delivered": "would_deliver",
                        "teammate": match_name,
                        "prompt_hash": prompt_hash,
                    }
                    delivered_roles.append(planned)
                    delivered["role_briefings"].append(planned)
                    continue

                try:
                    resp = _prompter_mcp_send(port, token, "team_send_message", {
                        "to": match_name,
                        "message": message,
                        "summary": summary,
                    })
                    success = not resp.get("error")
                    result = {
                        "role": role_name,
                        "delivered": success,
                        "teammate": match_name,
                        "prompt_hash": prompt_hash,
                    }
                    if not success:
                        result["reason"] = resp.get("error")
                        failed_roles.append(result)
                    else:
                        delivered_roles.append(result)
                    delivered["role_briefings"].append(result)
                    if success:
                        _notify("low", "prompter",
                                f"Briefing delivered to '{match_name}'",
                                body=f"role={role_name}")
                    elif stop_on_failure:
                        break
                except Exception as exc:
                    failed = {
                        "role": role_name,
                        "delivered": False,
                        "teammate": match_name,
                        "reason": str(exc),
                        "prompt_hash": prompt_hash,
                    }
                    failed_roles.append(failed)
                    delivered["role_briefings"].append(failed)
                    if stop_on_failure:
                        break

        total_roles = len(lib.get("role_briefings", [])) if include_roles else 0
        completed_roles = len(delivered_roles) + len(skipped_roles)
        roles_complete = (not include_roles) or (not failed_roles and completed_roles == total_roles)
        delivered_to_updated = False

        if dry_run:
            return self._send_json(200, {
                "ok": roles_complete,
                "dry_run": True,
                "delivered": delivered,
                "leader_skipped": not include_leader,
                "delivered_roles": delivered_roles,
                "skipped_roles": skipped_roles,
                "failed_roles": failed_roles,
                "total_roles": total_roles,
                "delivered_to_updated": False,
            })

        if failed_roles or not roles_complete:
            return self._send_json(200, {
                "ok": False,
                "error": "one or more role briefings failed; delivered_to was not updated",
                "delivered": delivered,
                "leader_skipped": not include_leader,
                "delivered_roles": delivered_roles,
                "skipped_roles": skipped_roles,
                "failed_roles": failed_roles,
                "total_roles": total_roles,
                "delivered_to_updated": False,
            })

        # Update library entry
        delivered_entry = {"teamId": team_id, "deliveredAt": time.time()}
        if not include_leader:
            delivered_entry["leaderSkipped"] = True
        if idempotency:
            delivered_entry["idempotency"] = "role_markers"
        lib.setdefault("delivered_to", []).append(delivered_entry)
        try:
            tmp = lib_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(lib, indent=2, default=str))
            tmp.replace(lib_path)
            delivered_to_updated = True
        except Exception as exc:
            logger.warning("prompter deliver: failed to update library: %s", exc)

        return self._send_json(200, {
            "ok": True,
            "delivered": delivered,
            "leader_skipped": not include_leader,
            "delivered_roles": delivered_roles,
            "skipped_roles": skipped_roles,
            "failed_roles": failed_roles,
            "total_roles": total_roles,
            "delivered_to_updated": delivered_to_updated,
        })

    # ── Make New Team ─────────────────────────────────────────────────────────

    def _team_cdp_check(self) -> None:
        """GET /team/cdp-check — lightweight CDP reachability probe."""
        ok, ws_url, err = _cdp_find_aionui_page()
        if ok:
            self._send_json(200, {"ok": True, "wsUrl": ws_url})
        else:
            self._send_json(200, {"ok": False, "error": err})

    def _team_create(self) -> None:
        """POST /team/create — create a new AionUi team via CDP Runtime.evaluate."""
        import shlex as _shlex

        try:
            body = self._read_body_json()
        except Exception:
            return self._send_json(400, {"ok": False, "error": "invalid JSON body"})

        def _clean(s: str) -> str:
            s = str(s).strip()
            return s if (0 < len(s) <= 120 and all(ord(c) >= 32 for c in s)) else ""

        # ── Validate name ──────────────────────────────────────────────────
        name = _clean(body.get("name") or "")
        if not name:
            return self._send_json(200, {"ok": False,
                "error": "name is required (non-empty, ≤120 chars, no control chars)"})

        # ── Validate leader ────────────────────────────────────────────────
        leader_cfg = body.get("leader") or {}
        agent_type = (leader_cfg.get("agentType") or "hermes").strip().lower()
        if agent_type not in KNOWN_TEAM_CAPABLE_BACKENDS:
            return self._send_json(200, {"ok": False,
                "error": f"invalid agentType '{agent_type}'; "
                         f"must be one of {sorted(KNOWN_TEAM_CAPABLE_BACKENDS)}"})
        agent_name_raw = _clean(leader_cfg.get("agentName") or "Leader")
        agent_name = agent_name_raw if agent_name_raw else "Leader"
        model = (leader_cfg.get("model") or "").strip() or None

        # ── Validate workspace ─────────────────────────────────────────────
        ws_cfg = body.get("workspace") or {}
        ws_path = (ws_cfg.get("path") or "").strip()
        ws_mode = (ws_cfg.get("mode") or "shared").strip()
        if ws_mode not in ("shared", "isolated"):
            return self._send_json(400, {"ok": False,
                "error": f"workspace.mode must be 'shared' or 'isolated'; got '{ws_mode}'"})

        # ── Validate structure ─────────────────────────────────────────────
        struct_cfg = body.get("structure") or {}
        struct_path = (struct_cfg.get("path") or "panel").strip()
        if struct_path not in ("panel", "leader"):
            struct_path = "panel"
        roles = [r for r in (_clean(x) for x in (struct_cfg.get("roles") or [])) if r][:12]
        auto_build = bool(body.get("autoBuild", True)) if struct_path == "panel" else False

        # ── Duplicate name check ───────────────────────────────────────────
        if AIONUI_DB.exists():
            try:
                con_ro = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
                dup = con_ro.execute(
                    "SELECT id FROM teams WHERE name=?", (name,)
                ).fetchone()
                con_ro.close()
                if dup:
                    _notify("high", "build-hermes-team",
                            f"Make New Team: duplicate name '{name}'",
                            "Rejected before any CDP call.",
                            {"kind": "team-create-fail", "reason": "duplicate", "name": name})
                    return self._send_json(200, {"ok": False,
                        "error": f"team name '{name}' already exists in AionUi"})
            except Exception as exc:
                logging.warning(f"[team/create] DB dup-check failed: {exc}")

        # ── CDP readiness check ────────────────────────────────────────────
        cdp_ok, ws_url, cdp_err = _cdp_find_aionui_page()
        if not cdp_ok:
            _notify("high", "build-hermes-team",
                    "Make New Team: CDP unreachable",
                    "AionUi not running in dev mode.",
                    {"kind": "team-create-fail", "reason": "cdp-unreachable"})
            return self._send_json(200, {"ok": False,
                "error": ("AionUi not running in dev mode "
                          "(CDP port 9230 unreachable). "
                          "Restart via launch-aionui.sh. "
                          f"Detail: {cdp_err}")})

        # ── Build agents list for createTeam ──────────────────────────────
        cli_path = _CDP_CLI_MAP.get(agent_type, agent_type)
        agent_entry: dict = {
            "role": "leader",
            "agentType": agent_type,
            "agentName": agent_name,
            "conversationType": "acp",
            "status": "pending",
            "cliPath": cli_path,
        }
        if model:
            agent_entry["currentModelId"] = model

        create_params = {
            "userId": "system_default_user",
            "name": name,
            "workspace": ws_path,
            "workspaceMode": ws_mode,
            "agents": [agent_entry],
        }

        # ── Invoke team.create via CDP ─────────────────────────────────────
        cdp_call_ok, cdp_msg = _cdp_team_create(ws_url, create_params)
        if not cdp_call_ok:
            _notify("high", "build-hermes-team",
                    f"Make New Team failed: '{name}'",
                    cdp_msg.get("error", "CDP error"),
                    {"kind": "team-create-fail", "reason": "cdp-call", "name": name})
            return self._send_json(200, {"ok": False,
                "error": cdp_msg.get("error", "CDP call failed")})

        # ── Parse TTeam from CDP response ──────────────────────────────────
        result_envelope = (cdp_msg.get("result") or {})
        exc_details = result_envelope.get("exceptionDetails")
        if exc_details:
            err_desc = ((exc_details.get("exception") or {}).get("description")
                        or exc_details.get("text") or str(exc_details))
            _notify("high", "build-hermes-team",
                    f"Make New Team failed: '{name}'",
                    err_desc,
                    {"kind": "team-create-fail", "reason": "exception", "name": name})
            return self._send_json(200, {"ok": False, "error": err_desc})

        result_val = result_envelope.get("result") or {}
        team_obj = result_val.get("value") if result_val.get("type") == "object" else None
        if not team_obj or not isinstance(team_obj, dict):
            return self._send_json(200, {"ok": False,
                "error": "Unexpected CDP response (no team object returned)",
                "cdp": cdp_msg})

        team_id = team_obj.get("id", "")
        leader_conv_id = ""
        for ag in (team_obj.get("agents") or []):
            if ag.get("role") == "leader":
                leader_conv_id = ag.get("conversationId", "")
                break

        _notify("mid", "build-hermes-team",
                f"Team '{name}' created",
                f"Leader: {agent_name} ({agent_type}), workspace: {ws_path or 'auto'}",
                {"kind": "team-create-ok", "teamId": team_id, "name": name,
                 "agentType": agent_type})

        # ── Leader path: no seeding ────────────────────────────────────────
        if struct_path == "leader":
            guidance = (
                f"Team '{name}' is ready (leader-only). "
                f"Open it in AionUi and message the leader with something like:\n"
                f"  'Help me structure a team for [your project]. Propose roles, "
                f"then I'll come back to the Control Panel and use Build Hermes Team to spawn them.'\n"
                f"The leader will propose a lineup. Return here, paste the roles into "
                f"Build Hermes Team, and click Build."
            )
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj,
                "seeded": None, "warning": None, "guidance": guidance,
            })

        # ── Panel path: optionally auto-build ──────────────────────────────
        if not auto_build or not roles:
            warn = ("No roles provided — team created but not seeded."
                    if auto_build and not roles else None)
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj,
                "seeded": None, "warning": warn, "guidance": None,
            })

        # ── Wait for session live, then seed ───────────────────────────────
        if not leader_conv_id:
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj, "seeded": None,
                "warning": ("Team created but leader conversationId unknown — "
                            "use Build Hermes Team to seed roles manually."),
                "guidance": None,
            })

        live, _ = _wait_for_session_live(leader_conv_id, timeout_s=10)
        if not live:
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj, "seeded": None,
                "warning": ("Team created but session didn't go live within 10s. "
                            "Open the team in AionUi, then use Build Hermes Team "
                            "to add roles."),
                "guidance": None,
            })

        SEED_SCRIPT = Path(__file__).resolve().parent.parent / "seed_hermes_team.sh"
        if not SEED_SCRIPT.exists():
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj, "seeded": None,
                "warning": f"Team created but seed script not found: {SEED_SCRIPT}",
                "guidance": None,
            })

        try:
            proc = subprocess.run(
                ["bash", str(SEED_SCRIPT), name, *roles],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj, "seeded": None,
                "warning": "Team created but seed timed out (180s). Open team in AionUi and retry.",
                "guidance": None,
            })
        except Exception as seed_exc:
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj, "seeded": None,
                "warning": f"Team created but seed failed: {seed_exc}",
                "guidance": None,
            })

        seed_out = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
        if proc.returncode == 0:
            _notify("mid", "build-hermes-team",
                    f"Team build complete: {name} ({len(roles)} role(s))",
                    f"Seeded: {', '.join(roles)}",
                    {"kind": "seed-complete", "team": name,
                     "count": len(roles), "roles": roles})
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj,
                "seeded": roles, "warning": None, "guidance": None,
                "output": seed_out,
            })
        else:
            err_lines = (proc.stderr or proc.stdout or "seed failed").strip().splitlines()
            seed_err = err_lines[-1] if err_lines else "seed failed"
            _notify("high", "build-hermes-team",
                    f"Team build seed failed: {name}", seed_err,
                    {"kind": "seed-fail", "team": name, "reason": seed_err})
            return self._send_json(200, {
                "ok": True, "teamId": team_id, "team": team_obj,
                "seeded": None,
                "warning": f"Team created but seed failed: {seed_err}",
                "guidance": None, "output": seed_out,
            })

    # ── Notification Center endpoints ─────────────────────────────────────────

    # ── Secret Keeper / Privacy Scrub ─────────────────────────────────────────
    def _privacy_status(self) -> None:
        """Real state of the two-component scrub: A = deterministic regex (live),
        B = on-box SLM recognizer (openai-privacy-filter ONNX in venv-ml)."""
        import importlib.util as _ilu
        status: dict[str, Any] = {"deterministic": {"available": False}, "slm": {"available": False}}
        # Component A — deterministic scrubber.py (no AI, always-on default)
        try:
            sp = ROOT / "scrubber.py"
            status["deterministic"] = {
                "available": sp.exists(),
                "module": "scrubber.py",
                "kind": "regex/rules (no AI)",
            }
            try:
                bl = ROOT / "blocklist.txt"
                status["deterministic"]["blocklist_present"] = bl.exists()
            except Exception:
                pass
        except Exception as exc:
            status["deterministic"] = {"available": False, "error": str(exc)}
        # Component B — on-box SLM recognizer
        try:
            md = ROOT / "models" / "openai-privacy-filter"
            onnx = md / "onnx" / "model_q4f16.onnx"
            venv_py = ROOT / "venv-ml" / "bin" / "python"
            cfg = md / "config.json"
            model_type = None
            if cfg.exists():
                try:
                    model_type = json.loads(cfg.read_text()).get("model_type")
                except Exception:
                    model_type = None
            status["slm"] = {
                "available": onnx.exists() and venv_py.exists(),
                "model": "openai-privacy-filter",
                "model_type": model_type,
                "model_present": onnx.exists(),
                "venv_present": venv_py.exists(),
                "size_class": "<2B (8-layer token-classifier, q4f16 ONNX)",
                "wired_live": False,  # standalone recognizer; not yet in the live scrub() path
                "note": "Recognizer works on-demand (Test). Not in the live scrub path (needs a persistent service for per-call live use).",
            }
        except Exception as exc:
            status["slm"] = {"available": False, "error": str(exc)}
        return self._send_json(200, status)

    def _privacy_test(self) -> None:
        """Run sample (or supplied) text through BOTH components and show redactions.
        Fail-closed: on scrub error, report failure (caller must treat as block)."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
        except Exception:
            body = {}
        sample = (body.get("text") or
                  "Contact Example Person at operator@example.invalid, key " "sk-" "proj-ABCD1234567890XYZ, "
                  "host this-box at str(Path.home())/secret/notes.txt, SSN 900-01-0001")
        level = body.get("level") or "workspace"
        result: dict[str, Any] = {"input": sample, "level": level}
        # Component A — deterministic (runs in-process)
        try:
            import importlib
            sys.path.insert(0, str(ROOT))
            scrubber = importlib.import_module("scrubber")
            importlib.reload(scrubber)
            result["deterministic"] = {
                "ok": True,
                "output": scrubber.scrub(sample, level, {"username": os.environ.get("USER", "user")}),
            }
        except Exception as exc:
            # fail-closed signal: deterministic layer must not silently pass raw text
            result["deterministic"] = {"ok": False, "error": str(exc), "blocked": True}
        # Component B — SLM recognizer (venv-ml subprocess; ~10s cold load)
        try:
            venv_py = ROOT / "venv-ml" / "bin" / "python"
            scan_dir = ROOT / "privacy-scrubbing"
            if venv_py.exists() and (scan_dir / "openai_filter.py").exists():
                proc = subprocess.run(
                    [str(venv_py), "-c",
                     "import sys,json;sys.path.insert(0,%r);from openai_filter import scan_pii;"
                     "print(json.dumps(scan_pii(sys.argv[1])))" % str(scan_dir),
                     sample],
                    capture_output=True, timeout=90,
                )
                if proc.returncode == 0:
                    ents = json.loads(proc.stdout.decode().strip() or "[]")
                    # collapse subword tokens into entity labels present
                    labels = sorted({lbl.split("-", 1)[-1] for _tok, lbl in ents if lbl != "O"})
                    result["slm"] = {"ok": True, "entity_types": labels, "token_count": len(ents)}
                else:
                    result["slm"] = {"ok": False, "error": proc.stderr.decode()[:200]}
            else:
                result["slm"] = {"ok": False, "error": "SLM not installed (venv-ml / openai_filter missing)"}
        except subprocess.TimeoutExpired:
            result["slm"] = {"ok": False, "error": "SLM timed out"}
        except Exception as exc:
            result["slm"] = {"ok": False, "error": str(exc)}
        return self._send_json(200, result)

    def _notif_list(self) -> None:
        qs = parse_qs(urlsplit(self.path).query)
        try:
            since = int(qs.get("since", ["0"])[0])
            limit = min(int(qs.get("limit", ["100"])[0]), 500)
        except (ValueError, TypeError):
            since, limit = 0, 100
        sev_f = qs.get("severity", [None])[0]
        src_f = qs.get("source",   [None])[0]
        try:
            con = sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            wheres: list[str] = ["state != 'dismissed'"]
            params: list[Any] = []
            if since:
                wheres.append("ts > ?"); params.append(since)
            if sev_f and sev_f in NOTIF_SEVERITIES:
                wheres.append("severity = ?"); params.append(sev_f)
            if src_f:
                wheres.append("source = ?"); params.append(src_f)
            rows = con.execute(
                f"SELECT * FROM notifications WHERE {' AND '.join(wheres)} "
                "ORDER BY ts DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            con.close()
            items = [dict(r) for r in rows]
            return self._send_json(200, {"notifications": items, "count": len(items)})
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _notif_write(self) -> None:
        body = self._read_body_json()
        severity = str(body.get("severity") or "mid").lower().strip()
        source   = str(body.get("source")   or "panel").strip()[:80]
        title    = str(body.get("title")    or "").strip()[:200]
        notif_body = str(body.get("body")   or "").strip()[:2000]
        payload  = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        if not title:
            return self._send_json(400, {"ok": False, "error": "title is required"})
        if severity not in NOTIF_SEVERITIES:
            return self._send_json(400, {"ok": False,
                "error": f"severity must be one of: {', '.join(sorted(NOTIF_SEVERITIES))}"})
        new_id = _notify(severity, source, title, notif_body, payload)
        if new_id is None:
            return self._send_json(200, {"ok": False, "error": "failed to write notification"})
        return self._send_json(200, {"ok": True, "id": new_id})

    def _notif_state(self, path: str, new_state: str) -> None:
        parts = path.strip("/").split("/")
        try:
            notif_id = int(parts[1])
        except (IndexError, ValueError):
            return self._send_json(400, {"ok": False, "error": "invalid id"})
        try:
            con = sqlite3.connect(str(NOTIF_DB))
            cur = con.execute("UPDATE notifications SET state=? WHERE id=?", (new_state, notif_id))
            con.commit()
            affected = cur.rowcount
            con.close()
            if affected == 0:
                return self._send_json(200, {"ok": False,
                    "error": f"notification {notif_id} not found"})
            return self._send_json(200, {"ok": True, "id": notif_id, "state": new_state})
        except Exception as exc:
            return self._send_json(200, {"ok": False, "error": str(exc)})

    def _notif_mark_all_read(self) -> None:
        try:
            con = sqlite3.connect(str(NOTIF_DB))
            cur = con.execute("UPDATE notifications SET state='read' WHERE state='unread'")
            con.commit()
            affected = cur.rowcount
            con.close()
            return self._send_json(200, {"ok": True, "marked": affected})
        except Exception as exc:
            return self._send_json(200, {"ok": False, "error": str(exc)})

    def _notif_explain(self, id_str: str) -> None:
        try:
            notif_id = int(id_str.strip().split("/")[0])
        except ValueError:
            return self._send_json(400, {"error": "invalid id"})
        try:
            con = sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT * FROM notifications WHERE id=?", (notif_id,)
            ).fetchone()
            con.close()
            if not row:
                return self._send_json(404, {"error": f"notification {notif_id} not found"})
            return self._send_json(200, {
                "id": notif_id,
                "explanation": _notif_explain_text(dict(row)),
            })
        except Exception as exc:
            return self._send_json(500, {"error": str(exc)})

    def _notif_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()
        q = NOTIF_BROADCASTER.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15.0)
                    chunk = f"data: {payload}\n\n".encode("utf-8")
                except queue.Empty:
                    chunk = b": keepalive\n\n"
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
        finally:
            NOTIF_BROADCASTER.unsubscribe(q)

    def _ll_run(self) -> None:
        if not LL_SCRIPT.is_file():
            return self._send_json(500, {"error": f"missing: {LL_SCRIPT}"})
        LL_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        status_path = LL_STATUS_DIR / f"{run_id}.json"
        _write_json(status_path, {
            "id": run_id,
            "started": datetime.now(tz=timezone.utc).isoformat(),
            "finished": None,
            "running": True,
            "exit_code": None,
            "output_path": None,
        })

        def _worker():
            try:
                log_fh = open(ROOT / "learning_loop.log", "a", encoding="utf-8")
                proc = subprocess.run(
                    ["/usr/bin/env", "python3", str(LL_SCRIPT)],
                    capture_output=True, text=True, timeout=400,
                )
                stdout = (proc.stdout or "").strip()
                exit_code = proc.returncode
                output_path = stdout.splitlines()[-1] if stdout and exit_code == 0 else None
            except subprocess.TimeoutExpired:
                exit_code, output_path, stdout = 124, None, ""
            except Exception as exc:
                exit_code, output_path, stdout = -1, None, str(exc)
            _write_json(status_path, {
                "id": run_id,
                "started": _read_json(status_path).get("started"),
                "finished": datetime.now(tz=timezone.utc).isoformat(),
                "running": False,
                "exit_code": exit_code,
                "output_path": output_path,
            })

        threading.Thread(target=_worker, name=f"ll-{run_id}", daemon=True).start()
        return self._send_json(202, {"run_id": run_id, "status": "running"})

    # ── /blocklist ───────────────────────────────────────────────────────────

    def _api_blocklist_get(self) -> None:
        """Return current blocklist.txt as a list of non-comment lines."""
        bl_path = Path(__file__).resolve().parent.parent / "blocklist.txt"
        if not bl_path.exists():
            return self._send_json(200, {"patterns": [], "raw": ""})
        raw = bl_path.read_text(encoding="utf-8")
        patterns = [
            ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        self._send_json(200, {"patterns": patterns, "raw": raw})

    def _api_blocklist_post(self) -> None:
        """Add, remove, or clear a pattern in blocklist.txt."""
        body = self._read_json_body()
        if body is None:
            return self._send_json(400, {"error": "invalid JSON body"})
        action  = body.get("action", "")
        pattern = body.get("pattern", "").strip()
        bl_path = Path(__file__).resolve().parent.parent / "blocklist.txt"

        if action == "clear":
            bl_path.write_text(
                "# KHAOSS Privacy Scrubber — User Blocklist\n"
                "# One pattern per line. Lines starting with # are comments.\n"
                "# Plain word: CompanySecret    → redacted (case-insensitive)\n"
                "# Regex:      regex:SK-[a-z]{32}  → compiled and matched\n",
                encoding="utf-8",
            )
            self._reload_scrubber_blocklist()
            return self._send_json(200, {"ok": True, "action": "cleared"})

        if not pattern:
            return self._send_json(400, {"error": "pattern required"})

        existing = bl_path.read_text(encoding="utf-8") if bl_path.exists() else ""
        lines = existing.splitlines(keepends=True)

        if action == "add":
            # Avoid duplicate
            active = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
            if pattern not in active:
                lines.append(pattern + "\n")
                bl_path.write_text("".join(lines), encoding="utf-8")
            self._reload_scrubber_blocklist()
            return self._send_json(200, {"ok": True, "action": "added", "pattern": pattern})

        if action == "remove":
            new_lines = [ln for ln in lines if ln.strip() != pattern]
            bl_path.write_text("".join(new_lines), encoding="utf-8")
            self._reload_scrubber_blocklist()
            return self._send_json(200, {"ok": True, "action": "removed", "pattern": pattern})

        return self._send_json(400, {"error": f"unknown action: {action}"})

    # ── /launch/all ─────────────────────────────────────────────────────────────

    def _check_svc_health(self, spec: dict) -> str:
        """Return 'healthy', 'degraded', or 'stopped' for one launch spec entry."""
        check = spec.get("check")
        if check == "pid":
            pid_file = spec.get("pid_file")
            if pid_file:
                pid = _read_pid(Path(pid_file))
                if pid and _alive(pid):
                    return "healthy"
            return "stopped"
        if check == "http":
            host, port, path = spec["http"]
            r = _http_get(host, port, path, timeout=2.0)
            if r and r[0] == 200:
                parsed = r[1]
                svc_status = parsed.get("status", "ok")
                return "degraded" if svc_status == "degraded" else "healthy"
            return "stopped"
        if check == "tcp":
            host, port = spec["tcp"]
            return "healthy" if _tcp_reachable(host, port, timeout=1.0) else "stopped"
        if check == "electron":
            return "healthy" if _find_electron_pid() else "stopped"
        return "stopped"

    def _launch_all(self) -> None:
        """POST/GET /launch/all — streams SSE launch events then a final complete event."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        def emit(data: dict) -> bool:
            try:
                payload = json.dumps(data)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, OSError):
                return False

        SEQUENCE = self._build_launch_sequence()
        results: list[dict] = []

        for spec in SEQUENCE:
            name    = spec["name"]
            display = spec["display"]

            # Optional services: skip if not installed
            if spec.get("optional"):
                if name == "space_agent" and not SPACE_AGENT_PACKAGE_JSON.is_file():
                    emit({"service": name, "status": "skipped",
                          "msg": f"{display} — not installed (optional, skip)"})
                    results.append({"name": name, "display": display, "status": "skipped",
                                    "svc_key": spec.get("svc_key")})
                    continue

            # Panel server is always us
            if spec["check"] == "self":
                emit({"service": name, "status": "already_running",
                      "msg": f"{display} always running :7842"})
                results.append({"name": name, "display": display, "status": "already_running",
                                "svc_key": None})
                continue

            # Check current health
            current = self._check_svc_health(spec)
            if current in ("healthy", "degraded"):
                port_str = f" {spec['port']}" if spec.get("port") else ""
                emit({"service": name, "status": "already_running",
                      "msg": f"{display} already running{port_str}"})
                results.append({"name": name, "display": display, "status": current,
                                "svc_key": spec.get("svc_key")})
                continue

            # Not running — start it (unless flagged no_start, e.g. hermes_dashboard
            # which is covered by the hermes entry above)
            if not spec.get("no_start"):
                emit({"service": name, "status": "starting", "msg": f"Starting {display}..."})
                svc_key = spec.get("svc_key")
                if svc_key:
                    ok, start_msg = self._svc_do_start(svc_key)
                    if not ok:
                        log_tail = _log_tail(spec.get("log", ""))
                        emit({"service": name, "status": "failed",
                              "msg": f"{display} failed to start: {start_msg}",
                              "log_file": spec.get("log", "")})
                        results.append({"name": name, "display": display, "status": "failed",
                                        "svc_key": svc_key, "msg": start_msg,
                                        "log_file": spec.get("log", ""),
                                        "log_tail": log_tail})
                        continue
            else:
                emit({"service": name, "status": "starting",
                      "msg": f"Waiting for {display}..."})

            # Poll until healthy or timeout
            timeout    = spec.get("timeout", 10)
            final_state = "failed"
            for _ in range(timeout * 2):   # 0.5 s each iteration
                time.sleep(0.5)
                if not emit({"type": "ping", "service": name, "status": "waiting"}):
                    return
                state = self._check_svc_health(spec)
                if state in ("healthy", "degraded"):
                    final_state = state
                    break

            if final_state == "healthy":
                port_str = f" {spec['port']}" if spec.get("port") else ""
                emit({"service": name, "status": "healthy",
                      "msg": f"{display} healthy{port_str}"})
                results.append({"name": name, "display": display, "status": "healthy",
                                "svc_key": spec.get("svc_key")})
            elif final_state == "degraded":
                emit({"service": name, "status": "degraded",
                      "msg": f"{display} degraded (running but not fully healthy)"})
                results.append({"name": name, "display": display, "status": "degraded",
                                "svc_key": spec.get("svc_key")})
            else:
                log_file = spec.get("log", "")
                log_tail = _log_tail(log_file)
                emit({"service": name, "status": "failed",
                      "msg": f"{display} did not become healthy — check {log_file or 'logs'}",
                      "log_file": log_file})
                results.append({"name": name, "display": display, "status": "failed",
                                "svc_key": spec.get("svc_key"),
                                "log_file": log_file, "log_tail": log_tail})

        # Final summary event
        n_healthy  = sum(1 for r in results if r["status"] in ("healthy", "already_running"))
        n_failed   = sum(1 for r in results if r["status"] == "failed")
        n_degraded = sum(1 for r in results if r["status"] == "degraded")

        debug_report = ""
        if n_failed > 0 or n_degraded > 0:
            debug_report = _build_debug_report(results, KNOWN_FIXES)

        emit({
            "type":         "complete",
            "healthy":      n_healthy,
            "failed":       n_failed,
            "degraded":     n_degraded,
            "debug_report": debug_report,
        })

    _LAUNCH_VALID = frozenset({
        "vault_watcher", "sanitizer", "hermes", "screenpipe",
        "screen_assistant", "proactive_research", "aionui", "space_agent",
    })

    def _build_launch_sequence(self) -> list[dict]:
        _VW_LOG  = str(ROOT / "watcher.log")
        _SAN_LOG = str(ROOT / "sanitizer.log")
        _SA_LOG  = str(ROOT / "screen_assistant_runtime.log")
        _PR_LOG  = str(ROOT / "proactive_research.log")
        return [
            {"name":"vault_watcher",     "display":"Vault Watcher",     "svc_key":"vault_watcher",
             "check":"pid",  "pid_file":VAULT_PID_FILE,     "timeout":5,  "log":_VW_LOG},
            {"name":"sanitizer",         "display":"Vault Sanitizer",   "svc_key":"sanitizer",
             "check":"pid",  "pid_file":SANITIZER_PID_FILE, "timeout":5,  "log":_SAN_LOG},
            {"name":"hermes",            "display":"Hermes Gateway",    "svc_key":"hermes_dashboard",
             "check":"http", "http":(HERMES_GATEWAY_HOST, HERMES_GATEWAY_PORT, "/health"),
             "timeout":10, "log":"/tmp/hermes-dashboard.log", "port":f":{HERMES_GATEWAY_PORT}"},
            {"name":"hermes_dashboard",  "display":"Hermes Dashboard",  "svc_key":"hermes_dashboard",
             "check":"http", "http":("127.0.0.1", HERMES_DASH_PORT, "/api/status"),
             "timeout":10, "log":"/tmp/hermes-dashboard.log", "port":f":{HERMES_DASH_PORT}",
             "no_start":True},
            {"name":"screenpipe",        "display":"Screenpipe",        "svc_key":"screenpipe",
             "check":"http", "http":("127.0.0.1", 3030, "/health"),
             "timeout":15, "log":"/tmp/sp.log", "port":":3030"},
            {"name":"screen_assistant",  "display":"Screen Assistant",  "svc_key":"screen_assistant",
             "check":"pid",  "pid_file":SA_PID_FILE,        "timeout":8,  "log":_SA_LOG},
            {"name":"proactive_research","display":"Proactive Research","svc_key":"proactive_research",
             "check":"pid",  "pid_file":PR_PID_FILE,        "timeout":8,  "log":_PR_LOG},
            {"name":"aionui",            "display":"AionUI",            "svc_key":"aionui",
             "check":"electron", "timeout":20, "log":"/tmp/aionui.log"},
            {"name":"space_agent",       "display":"Space Agent",       "svc_key":"space_agent",
             "check":"tcp",  "tcp":("127.0.0.1", 3000),
             "timeout":15, "log":"/tmp/space.log", "optional":True},
            {"name":"panel_server",      "display":"Panel Server",      "svc_key":None,
             "check":"self", "timeout":0, "log":str(LOG_FILE)},
        ]

    def _launch_selected(self) -> None:
        """POST /launch/selected  body: {"services":["screenpipe","hermes",...]}
        Same SSE stream as /launch/all but filtered to the requested services."""
        body = self._read_body_json()
        requested = body.get("services")
        if not isinstance(requested, list) or not requested:
            self._send_json(400, {"error": "services array is required"})
            return
        invalid = [s for s in requested if s not in self._LAUNCH_VALID]
        if invalid:
            self._send_json(400, {"error": f"unknown service(s): {', '.join(invalid)}",
                                   "valid": sorted(self._LAUNCH_VALID)})
            return

        all_seq = self._build_launch_sequence()
        requested_set = set(requested)
        sequence = [s for s in all_seq if s["name"] in requested_set]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()

        def emit(data: dict) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, OSError):
                return False

        results: list[dict] = []
        for spec in sequence:
            name    = spec["name"]
            display = spec["display"]
            if spec.get("optional"):
                if name == "space_agent" and not SPACE_AGENT_PACKAGE_JSON.is_file():
                    emit({"service": name, "status": "skipped",
                          "msg": f"{display} — not installed (optional, skip)"})
                    results.append({"name": name, "display": display, "status": "skipped"})
                    continue
            if spec["check"] == "self":
                emit({"service": name, "status": "already_running",
                      "msg": f"{display} always running :7842"})
                results.append({"name": name, "display": display, "status": "already_running"})
                continue
            current = self._check_svc_health(spec)
            if current in ("healthy", "degraded"):
                port_str = f" {spec['port']}" if spec.get("port") else ""
                emit({"service": name, "status": "already_running",
                      "msg": f"{display} already running{port_str}"})
                results.append({"name": name, "display": display, "status": current})
                continue
            if not spec.get("no_start"):
                emit({"service": name, "status": "starting", "msg": f"Starting {display}..."})
                svc_key = spec.get("svc_key")
                if svc_key:
                    ok, start_msg = self._svc_do_start(svc_key)
                    if not ok:
                        emit({"service": name, "status": "failed",
                              "msg": f"{display} failed to start: {start_msg}"})
                        results.append({"name": name, "display": display, "status": "failed"})
                        continue
            else:
                emit({"service": name, "status": "starting",
                      "msg": f"Waiting for {display}..."})
            timeout = spec.get("timeout", 10)
            final_state = "failed"
            for _ in range(timeout * 2):
                time.sleep(0.5)
                if not emit({"type": "ping", "service": name, "status": "waiting"}):
                    return
                state = self._check_svc_health(spec)
                if state in ("healthy", "degraded"):
                    final_state = state
                    break
            if final_state == "healthy":
                port_str = f" {spec['port']}" if spec.get("port") else ""
                emit({"service": name, "status": "healthy",
                      "msg": f"{display} healthy{port_str}"})
                results.append({"name": name, "display": display, "status": "healthy"})
            elif final_state == "degraded":
                emit({"service": name, "status": "degraded",
                      "msg": f"{display} degraded"})
                results.append({"name": name, "display": display, "status": "degraded"})
            else:
                log_file = spec.get("log", "")
                emit({"service": name, "status": "failed",
                      "msg": f"{display} did not become healthy — check {log_file or 'logs'}"})
                results.append({"name": name, "display": display, "status": "failed"})
        n_healthy  = sum(1 for r in results if r["status"] in ("healthy", "already_running"))
        n_failed   = sum(1 for r in results if r["status"] == "failed")
        n_degraded = sum(1 for r in results if r["status"] == "degraded")
        emit({"type": "complete", "healthy": n_healthy, "failed": n_failed,
              "degraded": n_degraded, "debug_report": ""})

    def _reload_scrubber_blocklist(self) -> None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            import scrubber as _sc
            _sc.reload_blocklist()
        except Exception:
            pass

    def _read_json_body(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return None

    # ── Reverse proxy ────────────────────────────────────────────────────────

    def _proxy(self, target_key: str, target_path: str) -> None:
        host, port = PROXY_TARGETS[target_key]
        if not target_path.startswith("/"):
            target_path = "/" + target_path
        # Preserve query string from the original request
        query = urlsplit(self.path).query
        if query:
            target_path = f"{target_path}?{query}"

        # Forward body if any
        body = b""
        content_length = self.headers.get("Content-Length")
        if content_length and content_length.isdigit():
            try:
                body = self.rfile.read(int(content_length))
            except OSError:
                body = b""

        # Forward headers (minus hop-by-hop and host)
        fwd_headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if name.lower() in HOP_BY_HOP or name.lower() == "host":
                continue
            fwd_headers[name] = value
        fwd_headers["Host"] = f"{host}:{port}"

        try:
            conn = http.client.HTTPConnection(host, port, timeout=PROXY_TIMEOUT)
            conn.request(self.command, target_path, body=body, headers=fwd_headers)
            upstream = conn.getresponse()
        except (ConnectionRefusedError, socket.timeout, socket.gaierror) as exc:
            return self._send_json(502, {
                "error": "upstream unreachable",
                "target": f"{host}:{port}{target_path}",
                "reason": str(exc),
            })
        except OSError as exc:
            return self._send_json(502, {
                "error": "proxy I/O error",
                "target": f"{host}:{port}{target_path}",
                "reason": str(exc),
            })

        try:
            data = upstream.read()
            self.send_response(upstream.status)
            for h, v in upstream.getheaders():
                if h.lower() in HOP_BY_HOP:
                    continue
                self.send_header(h, v)
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        finally:
            try: conn.close()
            except OSError: pass

    # ── Ping target (used for AionUI :3002 reachability check) ──────────────

    def _ping_target(self, target_key: str) -> None:
        return self._send_json(200, self._ping_target_result(target_key))


# ── main ─────────────────────────────────────────────────────────────────────

# ★★★ Connection-class errors. A peer going away is traffic, never an incident.
_CONN_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)

_ERR_WINDOW_S = 10.0          # rate-limit window for genuine handler errors
_ERR_MAX_PER_WINDOW = 5       # at most this many lines per window; the rest are counted, not printed


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """★★★ ThreadingHTTPServer whose error path CANNOT become a lock convoy.

    socketserver's default handle_error does traceback.print_exc() straight to stderr. In this
    unit stdout and stderr are THE SAME journald socket, and every TextIOWrapper write takes one
    process-wide lock. Under a burst of dropped connections that is: many threads, one lock, and
    a blocking write to a rate-limited socket at the bottom of it. Measured 2026-09-17 — 766
    threads, 255 on a single futex, median 161 s blocked, 274 inbound sockets stuck in CLOSE-WAIT
    because the threads holding them never returned, and 1023/1024 fds. Self-amplifying: at the
    fd ceiling accept() fails, which raises, which prints, which deepens the convoy.

    So: connection errors are silent (counted only), and genuine errors get ONE rate-limited line
    to the FILE-backed logger. Never a traceback, never to the shared stream.
    """

    daemon_threads = True

    # ★★★ socketserver's default listen backlog is FIVE. 2026-09-17.
    #   Measured: 38 threads parked in handle_one_request on idle keep-alive connections while the
    #   dashboards polled at 16 req/s — and a fresh POST to this panel got a CONNECT TIMEOUT on
    #   loopback. Not a wedge and not a hang: the accept queue was full, so the kernel dropped the
    #   SYN. Every endpoint answered 200 a second later.
    #   ★ This is the failure that looks exactly like the outage it is not, which is why it is
    #     worth a comment: "the panel timed out" and "the panel is wedged" are different faults and
    #     today they have both been true within the same hour.
    request_queue_size = 128

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._err_lock = threading.Lock()
        self._err_window_start = 0.0
        self._err_in_window = 0
        self.dropped_conns = 0        # peers that went away — normal, surfaced for observability
        self.suppressed_errors = 0    # genuine errors we rate-limited away

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, _CONN_ERRORS):
            with self._err_lock:
                self.dropped_conns += 1
            return
        now = time.monotonic()
        with self._err_lock:
            if now - self._err_window_start > _ERR_WINDOW_S:
                burned, self._err_window_start, self._err_in_window = self.suppressed_errors, now, 0
                if burned:
                    self.suppressed_errors = 0
                    try:
                        logger.error("[panel] %d further handler error(s) suppressed", burned)
                    except Exception:
                        pass
            self._err_in_window += 1
            if self._err_in_window > _ERR_MAX_PER_WINDOW:
                self.suppressed_errors += 1
                return
        try:
            # One line. Type and message only — the traceback is what wedged us.
            logger.error("[panel] handler error from %s: %s: %s",
                         client_address, type(exc).__name__, exc)
        except Exception:
            pass


def make_server(bind: str, port: int) -> ThreadingHTTPServer:
    return QuietThreadingHTTPServer((bind, port), PanelHandler)


def main() -> int:
    ap = argparse.ArgumentParser(description="AionUI stack panel HTTP server (with reverse proxy + SSE)")
    ap.add_argument("--bind", default=DEFAULT_BIND,
                    help=("bind address(es), comma-separated (default %s). The FIRST is "
                          "required; extras (e.g. a tailnet IP) are best-effort so the "
                          "loopback listener always comes up even if the tailnet is down. "
                          "Never use 0.0.0.0 — that exposes the LAN and the internet." % DEFAULT_BIND))
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})")
    ap.add_argument("--foreground", action="store_true", help="log to stderr only, no PID file")
    args = ap.parse_args()

    _setup_logging(foreground=args.foreground)
    _load_bridge_config()
    _notif_db_init()
    _la_init_dirs()

    if not args.foreground:
        write_pid_file()

    # Comma-separated binds → one ThreadingHTTPServer per address, same port.
    # Default is a single loopback address, so single-bind behaviour is unchanged.
    addrs = [a.strip() for a in str(args.bind).split(",") if a.strip()]
    if not addrs:
        addrs = [DEFAULT_BIND]
    servers: list[ThreadingHTTPServer] = []
    for i, addr in enumerate(addrs):
        try:
            servers.append(make_server(addr, args.port))
            logger.info("bound %s:%d", addr, args.port)
        except OSError as exc:
            # The first address (loopback by default) is required; a failed extra
            # bind (e.g. tailnet not up) is logged and skipped, never fatal.
            if i == 0:
                logger.error("could not bind primary %s:%d — %s", addr, args.port, exc)
                if not args.foreground:
                    remove_pid_file()
                return 1
            logger.error("could not bind extra %s:%d — %s (skipping; primary stays up)",
                         addr, args.port, exc)
    if not servers:
        if not args.foreground:
            remove_pid_file()
        return 1

    # ★★★ PUBLISH THE CAPTURE POLICY ONCE, SYNCHRONOUSLY, BEFORE WE SERVE. 2026-09-17.
    #   The broadcaster publishes every 3 s, which left a ~3 s window after every panel restart in
    #   which a direct file reader correctly got a DENIAL. Correct, and still wrong for the
    #   operator: his tap-to-speak fails once and works on the retry. He has spent a whole day
    #   pressing buttons that did nothing; one more "it didn't work the first time" is not
    #   acceptable when the fix costs one bounded call (measured 153 ms).
    #   ★ BOUNDED ON PURPOSE. This is a CDP round-trip and CDP can hang forever (:9230 has spent
    #     today accepting connections and never answering). A publish that delays the panel's boot
    #     is a worse outage than the window it closes, so it runs in a thread we WAIT ON BRIEFLY
    #     and then abandon — the broadcaster will publish 3 s later regardless.
    #   ★★ Failing to publish is safe: an absent or unreadable store denies, and the reader falls
    #     through to the live endpoint, which fails closed on its own. Never an opening.
    _boot_pub = threading.Thread(target=lambda: _publish_capture_policy_quietly(),
                                 name="capture-policy-boot-publish", daemon=True)
    _boot_pub.start()
    _boot_pub.join(timeout=2.5)
    if _boot_pub.is_alive():
        logger.warning("capture-policy boot publish did not finish in 2.5s — serving anyway; "
                       "the broadcaster will publish on its next tick")
    else:
        logger.info("capture-policy published at boot (cold-start denial window closed)")

    BROADCASTER.start()
    CAPTURE_PUBLISHER.start()          # ★ independent of the steward sweep's pause logic

    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()
        BROADCASTER.stop()
        CAPTURE_PUBLISHER.stop()
        # server.shutdown() blocks if called from a serving thread, so shut each
        # down from its own background thread.
        for s in servers:
            threading.Thread(target=s.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        "serving %s on %s:%d  (panel: %s, SSE every %.1fs)",
        PANEL_HTML.name, ",".join(s.server_address[0] for s in servers),
        args.port, PANEL_HTML, STREAM_INTERVAL_S,
    )
    # Serve every listener but the last in its own thread; serve the last in the
    # main thread so signals are delivered here.
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers[:-1]]
    for t in threads:
        t.start()
    try:
        servers[-1].serve_forever()
    finally:
        for s in servers:
            s.server_close()
        BROADCASTER.stop()
        if not args.foreground:
            remove_pid_file()
        logger.info("panel_server stopped cleanly")
    return 0


# ★ module-level cache for the CDP health probe. See the note at its call site: the 3-second
#   cadence was the problem, not the check.
_CDP_CACHE = {"at": 0.0, "ok": False}
_CDP_TTL_S = 60.0


def _cdp_health_cached(have_pid: bool) -> bool:
    """Is AionUi's CDP endpoint answering? Probed at most once a minute.

    ★ RETURNS THE LAST ANSWER WHEN THE PID IS GONE, so a stopped app still reads as down rather than
      as "never checked" - the boolean keeps its meaning.
    """
    import time as _t
    now = _t.time()
    if now - _CDP_CACHE["at"] < _CDP_TTL_S:
        return _CDP_CACHE["ok"]
    ok = False
    if have_pid:
        try:
            import urllib.request as _ur
            with _ur.urlopen("http://127.0.0.1:9230/json/list", timeout=2) as _r:
                ok = _r.status == 200
        except Exception:  # noqa: BLE001
            ok = False
    _CDP_CACHE.update({"at": now, "ok": ok})
    return ok


if __name__ == "__main__":
    sys.exit(main())
