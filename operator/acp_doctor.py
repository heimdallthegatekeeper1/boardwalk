#!/usr/bin/env python3
"""★ ACP DOCTOR — self-diagnosis, self-recovery and prevention for AionUi↔Hermes ACP seats.

Built 2026-09-17 (PA lead, boardwalk) after the 10:25 incident:
  a mid-turn context compression (~82 s, no ACP progress event streamed) outlived the
  remaining window of AionUi's 300 s inactivity PromptTimer (last event 10:21:34 →
  cancel 10:26:33) → session/cancel mid-retry → the operator's prompt was lost
  ("Prompt timed out" / "unknown route"). The 10:41 re-prompt respawned the ACP process
  and restored the compressed session from the DB — that recovery worked, but only
  because the operator noticed and typed again. This tool makes the noticing automatic.

CHECK IDS are bound to ~/boardwalk/skills/turn-diagnostics-catalog.md — the same ids
the in-app Diagnose button runs in turnDiagnostics.ts, plus one this file adds:
  stale_live_session — a recently-active conversation whose ACP task is dead while its
  session file still exists on disk. The FC-3 orphan detector (steward_patrol) CANNOT see
  this shape: the file resolves, so it passes — but no live process/task stands behind it.
  This is exactly the case that bit the PA seat at 10:25.

RECOVERY BINDS TO EXISTING RECOVERY LEGS ONLY — this file mints no new repair (steward
doctrine):
  * re-deliver a lost user prompt  → POST /operator/dispatch/send (the board's proven
    channel — proven 3× against the example team, confirmed from the messages table)
  * re-mint a stale ACP session    → POST /remediate/remint-acp-session {confirm:true}
    (the panel runs the MANDATORY circuit breaker + journal, then CDP → acp.reset-session)
confirm:true is authorised by the operator's 2026-09-17 instruction ("along with a fix for
this situation so you recover"); the in-flight guard is what makes that safe:
  * never touch a conversation whose session file was written within 5 minutes
  * never touch a conversation with ACP log activity within 5 minutes
  * --watch auto-fires ONLY re-delivery; re-minting in watch mode stays PROPOSED unless
    the conversation has had zero traffic for 24 h (re-minting a live seat RESURRECTS it —
    the board's ruling on orphaned leads, applied by default)

Usage:
  acp_doctor.py                   # diagnose the most recent at-risk conversation
  acp_doctor.py <conversation_id> # diagnose one
  acp_doctor.py --all             # scan every hermes-ACP conversation (FAIL/WARN only)
  acp_doctor.py --fix [conv]      # diagnose, then recover the safe cases (interactive)
  acp_doctor.py --watch           # one patrol pass: --all + safe auto-recovery + journal
                                  # + board escalation when the breaker trips. Cron: every 5 min.
  acp_doctor.py --self-test       # prove-fail against synthetic inputs (no live state touched)
  --json                          # machine-readable report on stdout

Exit codes: 0 clean · 1 findings, report only · 2 findings + recovery fired
            3 self-test failed · 4 environment unreadable (UNREADABLE, not "none")
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def real_home() -> Path:
    """The operator's real home. Path.home() is NOT trusted: hermes terminal sessions run in a
    bwrap jail whose HOME is the profile's home dir (…/profiles/<id>/home) — resolving config
    paths against it would report 'DB not found' for a DB that is right there. The real /etc is
    bound into the jail, so the passwd lookup is the ground truth; the DB-existence checks are
    the belt."""
    import pwd
    h = Path.home()
    if (h / ".config/AionUi-Dev/aionui/aionui.db").exists():
        return h
    try:
        d = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if (d / ".config/AionUi-Dev/aionui/aionui.db").exists():
            return d
    except Exception:
        pass
    for cand in (Path("str(Path.home())"),):
        if (cand / ".config/AionUi-Dev/aionui/aionui.db").exists():
            return cand
    return h


_REAL_HOME = real_home()
DB = _REAL_HOME / ".config/AionUi-Dev/aionui/aionui.db"
PANEL_SERVER = ROOT / "panel_server.py"
PANEL = "http://127.0.0.1:7842"
PROFILES = _REAL_HOME / ".hermes" / "profiles"
PROVIDERS_JSON = _REAL_HOME / ".config/AionUi-Dev/aionui-providers.json"
CATALOG_TS = Path("str(Path.home())/Downloads/AIonUi/AionUi-main/src/common/utils/acpModelCatalog.ts")
LOG_DIR = _REAL_HOME / ".config/AionUi-Dev/logs"
FREEZE_LOG = LOG_DIR / "freeze-reports.log"
JOURNAL = ROOT / "failure_journal.jsonl"
BLOCKERS = HERE / "blockers.py"

LIVE_WINDOW_S = 300       # AionUi PromptTimer default (AcpSession.ts: promptTimeoutMs ?? 300_000)
INFLIGHT_S = 300          # session-file/log activity within 5 min ⇒ treat as in-flight
STUCK_MIN = 5             # unanswered user message older than this is "stuck"
STUCK_WINDOW_H = 24       # only chase unanswered prompts from the last 24 h

SEV_ORDER = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3}


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── read layer (ALL read-only; one store per fact, pointed at never copied) ────────────────────

def db_conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def acp_conversations(con: sqlite3.Connection) -> list[dict]:
    """Every conversation that can run a hermes ACP seat. A VACUOUS SET IS REPORTED, not passed."""
    out = []
    for cid, name, ctype, extra in con.execute("SELECT id, name, type, extra FROM conversations"):
        try:
            e = json.loads(extra or "{}")
        except Exception:
            e = {}
        if ctype == "acp" or e.get("agentProfile") or e.get("backend") == "hermes":
            out.append({"id": cid, "name": name or "", "type": ctype, "extra": e})
    return out


def messages_recent(con: sqlite3.Connection, cid: str, n: int = 40) -> list[dict]:
    rows = con.execute(
        "SELECT type, content, position, created_at FROM messages "
        "WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?", (cid, n)).fetchall()
    return [{"type": t, "content": c, "position": p, "created_at": ts} for t, c, p, ts in rows]


def last_activity_ms(con: sqlite3.Connection, cid: str) -> int | None:
    r = con.execute("SELECT MAX(created_at) FROM messages WHERE conversation_id=?", (cid,)).fetchone()
    return r[0] if r and r[0] else None


def proc_alive(profile: str) -> list[int]:
    """PIDs of hermes ACP processes for this profile. The pattern is the LAUNCH line,
    never a loose substring (a loose match is the orphan-killer shape)."""
    pat = r"\bhermes\s+--profile\s+" + re.escape(profile or "") + r"\s+acp\b"
    try:
        out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=10)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def session_files(profile: str, sid: str) -> list[Path]:
    if not profile or not sid:
        return []
    d = PROFILES / profile / "sessions"
    if not d.is_dir():
        return []
    return sorted(d.glob(f"session_{sid}*.json"))


def log_tail_lines(n: int = 4000) -> tuple[str, list[str]]:
    """Today's AionUi log; /tmp/aionui.log as fallback. Returns (source, lines)."""
    today = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    for p in (today, Path("/tmp/aionui.log")):
        try:
            if p.exists():
                lines = p.read_text(errors="replace").splitlines()
                return str(p), lines[-n:]
        except Exception:
            continue
    return "(none)", []


def mammouth_ids() -> set[str]:
    try:
        src = CATALOG_TS.read_text(errors="replace")
        m = re.search(r"MAMMOUTH_CATALOG_IDS: readonly string\[\] = \[(.*?)\];", src, re.S)
        if not m:
            return set()
        return {x for x in re.findall(r"'([^']+)'", m.group(1))}
    except Exception:
        return set()


def registry_ids() -> set[str]:
    try:
        d = json.loads(PROVIDERS_JSON.read_text())
        ids = set()
        for p in d.get("providers", []):
            for m in p.get("models", []):
                mid = m if isinstance(m, str) else (m.get("id") or m.get("key"))
                if mid:
                    ids.add(str(mid).lower().split(":")[-1])
        return ids
    except Exception:
        return set()


def lane_map() -> dict[str, dict]:
    """conversation-id-prefix → {lane, team, label}. PARSED from panel_server.py, not copied —
    a copy is how the writer and the reader drift apart (dispatch_targets rule)."""
    try:
        src = PANEL_SERVER.read_text(errors="replace")
        m = re.search(r"_LANE_TEAMS = \{(.*?)\n    \}", src, re.S)
        out = {}
        if not m:
            return out
        for lane, team, conv, _slot, label in re.findall(
                r'"(\w+)"\s*:\s*\(\s*"([0-9a-f-]+)"\s*,\s*"(\w+)"\s*,\s*"([0-9a-z-]+)"\s*,\s*"([^"]+)"\s*\)',
                m.group(1)):
            out[conv] = {"lane": lane, "team": team, "label": label}
        return out
    except Exception:
        return {}


def profile_default_model(profile: str) -> str | None:
    try:
        import yaml
        d = yaml.safe_load((PROFILES / profile / "config.yaml").read_text()) or {}
        return (d.get("model") or {}).get("default")
    except Exception:
        return None


def freeze_near(ts_ms: int, window_ms: int = 120_000) -> list[str]:
    try:
        lines = FREEZE_LOG.read_text(errors="replace").splitlines()
    except Exception:
        return []
    near = []
    for l in lines:
        m = re.search(r"(\d{13})|(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2})", l)
        if not m:
            continue
        t = int(m.group(1)) if m.group(1) else int(datetime.fromisoformat(m.group(2)).timestamp() * 1000)
        if abs(t - ts_ms) < window_ms:
            near.append(l.strip()[:160])
    return near[-3:]


def error_tag_of(text: str) -> str | None:
    if not text:
        return None
    if re.search(r"401|unauthorized|invalid.{0,10}key|auth.?exhaust", text, re.I):
        return "401 auth / exhausted"
    if re.search(r"402|payment|insufficient|balance|unfunded", text, re.I):
        return "402 payment / unfunded"
    if re.search(r"403|forbidden", text, re.I):
        return "403 forbidden"
    if re.search(r"429|rate.?limit", text, re.I):
        return "429 rate limit"
    if re.search(r"5\d\d|internal server", text):
        return "5xx server error"
    if re.search(r"timeout|timed out|stall", text, re.I):
        return "timeout / stall"
    if re.search(r"unknown route|no output", text, re.I):
        return "unknown route / no output"
    return None


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if isinstance(content.get("content"), str):
            return content["content"]
        try:
            return json.dumps(content)
        except Exception:
            return ""
    return ""


# ── live context builder (synthetic injection for self-test lives in diagnose()) ───────────────

def live_ctx(conv: dict, con: sqlite3.Connection, log_lines: list[str], log_src: str) -> dict:
    e = conv["extra"]
    prof = e.get("agentProfile") or e.get("hermesProfile")
    sid = e.get("acpSessionId")
    files = session_files(prof, sid)
    mtime = max((f.stat().st_mtime for f in files), default=None)
    # ACP log activity for THIS session in the live window (the second half of the guard)
    recent_log = 0.0
    if sid:
        for l in log_lines:
            if sid[:8] not in l:
                continue
            m = re.search(r"(\d{2}):(\d{2}):(\d{2})\.\d+", l)
            if m:
                try:
                    hh, mm, ss = (int(x) for x in m.groups())
                    dt = datetime.now().replace(hour=hh, minute=mm, second=ss, microsecond=0).timestamp()
                    age_s = time.time() - dt
                    if 0 <= age_s <= INFLIGHT_S:
                        recent_log = max(recent_log, dt)
                except Exception:
                    pass
    return {
        "conv": conv, "profile": prof, "sid": sid,
        "proc": proc_alive(prof) if prof else [],
        "files": files, "file_mtime": mtime,
        "recent_log_activity": recent_log,
        "msgs": messages_recent(con, conv["id"]),
        "last_activity_ms": last_activity_ms(con, conv["id"]),
        "log_lines": log_lines, "log_src": log_src,
        "registry": registry_ids(), "mammouth": mammouth_ids(),
        "profile_model": profile_default_model(prof) if prof else None,
    }


# ── the eight catalog checks (+1). Each takes ctx; none of them mutates anything ───────────────

def unscope(id_: str | None) -> str | None:
    if not id_:
        return None
    n = id_.lower()
    if ":" in n:
        n = n.split(":")[-1]
    # AionUi model ids carry a trailing effort modifier (gpt-5.5/high) — the modifier is not part
    # of the model identity and must not break catalog matching
    if n.endswith(("/high", "/medium", "/low", "/minimal")):
        n = n.rsplit("/", 1)[0]
    return n


def dormant_min(c: dict) -> float | None:
    """Minutes since last traffic. None = never. ACP processes spawn ON DEMAND per conversation —
    for a dormant seat, an absent process is NORMAL, not a failure (the 10:25 failure was a LIVE
    seat whose process died under traffic; these are different states and need different answers)."""
    if not c["last_activity_ms"]:
        return None
    return (now_ms() - c["last_activity_ms"]) / 60000


def is_dormant(c: dict) -> bool:
    """No messages at all, or nothing within the live window. A conversation nobody has spoken to
    in 24 h has no live process to miss — its absence is the resting state, not a fault."""
    idle = dormant_min(c)
    return idle is None or idle > STUCK_WINDOW_H * 60


def is_hermes(c: dict) -> bool:
    return c["conv"]["extra"].get("backend") in (None, "hermes") or bool(c["profile"])


def check_route_resolution(c: dict) -> dict:
    cfg = c["conv"]["extra"].get("currentModelId")
    idle = dormant_min(c)
    if not is_hermes(c):
        return {"id": "route_resolution", "title": "Route resolution", "status": "unknown",
                "summary": f"Non-hermes backend ({c['conv']['extra'].get('backend')}), "
                           "no external process approximation exists — the in-app Diagnose button is authoritative here.",
                "evidence": [f"configured (conversation.extra.currentModelId): {cfg or '—'}",
                             f"backend: {c['conv']['extra'].get('backend') or '—'}"]}
    alive = bool(c["proc"]) and bool(c["files"])
    if not alive and is_dormant(c):
        idle = dormant_min(c)
        idle_h = "never" if idle is None else f"{idle/60:.1f} h"
        return {"id": "route_resolution", "title": "Route resolution", "status": "ok",
                "summary": f"Dormant seat (last traffic {idle_h} ago) — ACP processes spawn on demand; "
                           "an absent process is normal here, not a failure.",
                "evidence": [f"configured (conversation.extra.currentModelId): {cfg or '—'}",
                             f"hermesProfile: {c['profile'] or '—'}",
                             f"idle: {idle_h} — outside the {STUCK_WINDOW_H} h live window"]}
    evidence = [
        f"configured (conversation.extra.currentModelId): {cfg or '—'}",
        f"running (external approx: ACP process for profile + session file): "
        f"{'present' if alive else ('process alive, session file missing' if c['proc'] else 'no live ACP process for profile ' + str(c['profile'] or '—'))}",
        f"hermesProfile: {c['profile'] or '—'} · backend: {c['conv']['extra'].get('backend') or '—'}",
        f"NOTE: external approx — task.getModelInfo() is only readable in-process; this check "
        f"proves the RESURRECTION PATH (process+file), not the in-memory task",
    ]
    if alive:
        return {"id": "route_resolution", "title": "Route resolution", "status": "ok",
                "summary": "A live ACP process stands behind the configured profile and its session file resolves.",
                "evidence": evidence}
    return {"id": "route_resolution", "title": "Route resolution", "status": "fail",
            "summary": 'No running route — the ACP session has no live process/session pair. "unknown route" is this check failing.',
            "evidence": evidence}


def check_model_validity(c: dict) -> dict:
    cfg = c["conv"]["extra"].get("currentModelId")
    if not is_hermes(c):
        return {"id": "model_validity", "title": "Model validity", "status": "unknown",
                "summary": f"Non-hermes backend ({c['conv']['extra'].get('backend')}) — the hermes/Mammouth "
                           "catalogs do not govern its model ids; the in-app Diagnose check is authoritative.",
                "evidence": [f"model: {cfg or '—'}", f"backend: {c['conv']['extra'].get('backend') or '—'}"]}
    eff = unscope(cfg)
    in_reg = bool(eff) and eff in c["registry"]
    in_mam = bool(eff) and (eff in c["mammouth"] or (cfg or "").lower().startswith("mammouth:"))
    in_prof = bool(eff) and bool(c["profile_model"]) and unscope(c["profile_model"]) == eff
    valid = in_reg or in_mam or in_prof
    return {
        "id": "model_validity", "title": "Model validity",
        "status": "unknown" if not eff else ("ok" if valid else "fail"),
        "summary": (f'Model "{eff}" is routable.' if valid
                    else f'Model "{eff}" is NOT in the live provider catalog, the Mammouth catalog, or its profile default — registry existence ≠ routability.'),
        "evidence": [f"model: {eff or '—'}",
                     f"in provider registry (aionui-providers.json): {in_reg}",
                     f"in Mammouth catalog (acpModelCatalog.ts): {in_mam}",
                     f"is the profile's own default model: {in_prof} (profile={c['profile'] or '—'})",
                     f"registry model count: {len(c['registry'])}"]
    }


def check_provider_health(c: dict) -> dict:
    errs = [m for m in c["msgs"] if m["position"] in ("left", "center")
            and m["type"] in ("tips", "error") and error_tag_of(text_of(m["content"]))]
    tag = error_tag_of(text_of(errs[0]["content"])) if errs else None
    return {
        "id": "provider_health", "title": "Provider health",
        "status": tag and ("fail" if re.search(r"40[12]", tag) else "warn") or "unknown",
        "summary": (f'The provider returned "{tag}" on a recent turn. Auth-exhausted / 402-over-balance both look like "unknown route" but are provider-side.'
                    if tag else "No provider error tag found on recent turns (or none captured)."),
        "evidence": [f"last error tag: {tag or 'none detected'}",
                     f"recent messages examined: {len(c['msgs'])}"]
    }


def check_compression_preflight(c: dict) -> dict:
    sid = (c["sid"] or "")[:8]
    if sid:
        comp = [l.strip()[:160] for l in c["log_lines"]
                if re.search(r"compress|compaction|preflight|summariz", l, re.I) and sid in l]
    else:
        # no session id to scope to — count compression lines only if this conversation was
        # actively running when they fired (within ±30 min of its last activity)
        comp = []
        if c["last_activity_ms"]:
            last_act_s = c["last_activity_ms"] / 1000
            for l in c["log_lines"]:
                if not re.search(r"compress|compaction|preflight|summariz", l, re.I):
                    continue
                m = re.search(r"(\d{2}):(\d{2}):(\d{2})", l)
                if not m:
                    continue
                try:
                    dt = datetime.now().replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                                second=int(m.group(3)), microsecond=0).timestamp()
                    if abs(dt - last_act_s) <= 1800:
                        comp.append(l.strip()[:160])
                except Exception:
                    pass
    return {
        "id": "compression_preflight", "title": "Compression preflight",
        "status": "warn" if comp else "unknown",
        "summary": ("A context-compression fired near this seat. While it runs (measured ~82 s on the aux route), "
                    "NO ACP progress event is streamed — a gap that can outlive AionUi's 300 s inactivity timer "
                    "and cancel the turn mid-retry." if comp
                    else "No compression markers found near this seat (or logs unavailable)."),
        "evidence": comp[-5:] or [f"no compression markers near this seat in {c['log_src']}"]
    }


def fname(f) -> str:
    return getattr(f, "name", f)


def check_process_health(c: dict) -> dict:
    idle = dormant_min(c)
    if not is_hermes(c):
        return {"id": "process_health", "title": "Process / session health", "status": "unknown",
                "summary": f"Non-hermes backend ({c['conv']['extra'].get('backend')}) — "
                           "the hermes ACP process check does not apply; in-app Diagnose is authoritative.",
                "evidence": [f"backend: {c['conv']['extra'].get('backend') or '—'}"]}
    alive = bool(c["proc"]) and bool(c["files"])
    if not alive and is_dormant(c):
        idle = dormant_min(c)
        idle_h = "never" if idle is None else f"{idle/60:.1f} h"
        return {"id": "process_health", "title": "Process / session health", "status": "ok",
                "summary": f"Dormant seat (last traffic {idle_h} ago) — nothing live to be dead.",
                "evidence": [f"idle: {idle_h} — outside the {STUCK_WINDOW_H} h live window"]}
    return {
        "id": "process_health", "title": "Process / session health",
        "status": "ok" if alive else "fail",
        "summary": ("A live ACP process and its session file both exist." if alive
                    else "No live ACP task — the session exited or went missing (e.g. null→createSession, a stale session after an app relaunch, or a mid-turn cancel)."),
        "evidence": [f"hermes ACP process for {c['profile'] or '—'}: {'pid ' + ','.join(map(str, c['proc'])) if c['proc'] else 'absent'}",
                     f"session file for acpSessionId {str(c['sid'])[:8] or '—'}: {[fname(f) for f in c['files']] or 'absent'}",
                     f"(external approx of workerTaskManager.getTask())"]
    }


def check_empty_turn(c: dict) -> dict:
    last_left = next((m for m in c["msgs"] if m["position"] == "left" and m["type"] == "text"), None)
    last_err = next((m for m in c["msgs"] if m["type"] in ("tips", "error")
                     and re.search(r"no output|produced no output|empty", text_of(m["content"]), re.I)), None)
    suspect = bool(last_err and last_left and text_of(last_left["content"]).strip())
    return {
        "id": "empty_turn_classification", "title": "Empty-turn classification",
        "status": "warn" if suspect else "ok",
        "summary": ("A non-empty reply is present alongside a \"no output\" error — possible Bug #2 regression."
                    if suspect else "No empty-turn false-positive signature detected."),
        "evidence": [f"no-output error present: {bool(last_err)}",
                     f"non-empty left reply present: {bool(last_left)}"]
    }


def check_freeze(c: dict, fail_ts: int | None) -> dict:
    ts = fail_ts or now_ms()
    near = freeze_near(ts)
    return {
        "id": "freeze_correlation", "title": "Freeze correlation",
        "status": "warn" if near else "ok",
        "summary": ("An Electron freeze/unresponsive event is logged within ±2 min of the failure."
                    if near else "No freeze event correlated with the failure."),
        "evidence": near or ["no freeze-reports.log entry near the failure timestamp"]
    }


def check_log_tail(c: dict) -> dict:
    sid = (c["sid"] or "")[:8]
    if sid:
        scoped = [l.strip()[:160] for l in c["log_lines"] if sid in l][-6:]
        evidence = scoped or c["log_lines"][-12:] or ["log unavailable"]
        return {
            "id": "log_tail", "title": "Log tail", "status": "unknown",
            "summary": f"Last lines of {c['log_src']} (scoped to this session where possible):",
            "evidence": evidence}
    return {
        "id": "log_tail", "title": "Log tail", "status": "unknown",
        "summary": "No acpSessionId bound to this conversation — the log cannot be scoped to it "
                   "(showing another seat's tail here would be misleading, so it is withheld).",
        "evidence": ["no acpSessionId in conversation.extra"]
    }


def check_stale_live(c: dict) -> dict:
    """THE CASE FC-3 MISSES: session file resolves on disk (so the orphan check passes) but no
    live ACP process stands behind it, while the conversation still carries recent traffic."""
    if not is_hermes(c):
        return {"id": "stale_live_session", "title": "Stale live session (KHAOSS)", "status": "unknown",
                "summary": "Non-hermes backend — liveness of the backing process is not externally measurable.",
                "evidence": [f"backend: {c['conv']['extra'].get('backend') or '—'}"]}
    idle = dormant_min(c)
    active = not is_dormant(c)
    file_exists = bool(c["files"])
    dead = not c["proc"] or not file_exists
    idle_min = idle if idle is not None else float("inf")
    return {
        "id": "stale_live_session", "title": "Stale live session (KHAOSS)",
        "status": "fail" if (active and dead) else "ok",
        "summary": ("Conversation active recently (last message %.1f min ago) but its ACP task is dead "
                    "(process/file missing) while the session file 'resolves' — the FC-3 orphan check passes "
                    "this shape, so only this check sees it. Next message respawns the process and restores "
                    "the session from the DB; a timed-out prompt BEFORE the respawn is lost and must be re-delivered."
                    % idle_min
                    if active and dead else
                    ("Session resolves where it should and a live process stands behind it."
                     if not dead else "No recent traffic — stale state, but nothing live to drop.")),
        "evidence": [f"last activity: {idle_min if idle_min != float('inf') else 'never'} min ago",
                     f"ACP process: {'alive' if c['proc'] else 'absent'}",
                     f"session file: {'present' if file_exists else 'absent'}",
                     "window: " + str(STUCK_WINDOW_H) + " h of recent traffic"]
    }


def diagnose(ctx: dict) -> dict:
    """Run all checks against ctx (live or synthetic) and rank. Same report shape as the in-app panel."""
    e = ctx["conv"]["extra"]
    fail_ts = None
    for m in ctx["msgs"]:
        if m["type"] in ("tips", "error") and error_tag_of(text_of(m["content"])):
            fail_ts = m["created_at"]
            break
    checks = [
        check_route_resolution(ctx),
        check_model_validity(ctx),
        check_provider_health(ctx),
        check_compression_preflight(ctx),
        check_process_health(ctx),
        check_empty_turn(ctx),
        check_freeze(ctx, fail_ts),
        check_log_tail(ctx),
        check_stale_live(ctx),
    ]
    ranked = sorted([c for c in checks if c["status"] in ("fail", "warn")],
                    key=lambda c: SEV_ORDER[c["status"]])
    top = ranked[0] if ranked else None
    return {
        "conversationId": ctx["conv"]["id"],
        "conversationName": ctx["conv"]["name"],
        "generatedAt": now_ms(),
        "route": {
            "configuredModelId": e.get("currentModelId"),
            "profile": ctx["profile"],
            "backend": e.get("backend"),
            "acpSessionId": e.get("acpSessionId"),
            "sessionAlive": bool(ctx["proc"]) and bool(ctx["files"]),
        },
        "checks": checks,
        "rankedCauses": ranked,
        "summary": (f"Most likely: {top['title'].lower()} — {top['summary']}" if top
                    else "No failing checks — route, model, provider and process all look healthy."),
    }


# ── stuck-turn detection (the lost prompt) ──────────────────────────────────────────────────────

def _line_ts_ms(l: str) -> int | None:
    m = re.search(r"(\d{2}):(\d{2}):(\d{2})", l)
    if not m:
        return None
    try:
        dt = datetime.now().replace(hour=int(m.group(1)), minute=int(m.group(2)),
                                    second=int(m.group(3)), microsecond=0)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def stuck_turn(ctx: dict) -> dict | None:
    """Last user message with no assistant reply after it, older than STUCK_MIN, inside the window,
    AND with a failure signature NEWER than that message (or a measured-dead task for hermes seats,
    or an error tag in the conversation for non-hermes seats). Returns {ts, text} or None.
    A conversation that is merely waiting on a live turn must NOT be re-prompted (double-spend),
    and an OLD marker (the 10:26 cancel) must not declare a LATER message (the 10:41 one) lost."""
    msgs = list(reversed(ctx["msgs"]))  # oldest→newest within the fetched window
    last_user = None
    for m in msgs:
        if m["position"] == "right" and m["type"] == "text":
            last_user = m
        elif m["position"] == "left" and m["type"] == "text" and last_user is not None:
            return None  # answered
    if last_user is None:
        return None
    age_ms = now_ms() - last_user["created_at"]
    if age_ms < STUCK_MIN * 60_000 or age_ms > STUCK_WINDOW_H * 3600_000:
        return None
    hermes = is_hermes(ctx)
    dead = (not (ctx["proc"] and ctx["files"])) if hermes else False
    sid = (ctx["sid"] or "")[:8]
    marker = re.compile(r"Prompt timed out|unknown route|Cancelled session|Turn ended: reason=interrupted", re.I)
    scoped_recent = False
    for l in ctx["log_lines"]:
        if not marker.search(l) or not sid or sid not in l:
            continue
        t = _line_ts_ms(l)
        if t is not None and t >= last_user["created_at"]:
            scoped_recent = True
            break
    tagged = any(error_tag_of(text_of(m["content"]))
                 for m in ctx["msgs"] if m["type"] in ("tips", "error"))
    if not (scoped_recent or dead or (tagged and not hermes)):
        return None
    return {"ts": last_user["created_at"], "text": text_of(last_user["content"])[:4000], "age_min": round(age_ms / 60000, 1)}


# ── the in-flight guard (what makes confirm:true safe) ──────────────────────────────────────────

def in_flight(ctx: dict) -> tuple[bool, str]:
    if ctx["file_mtime"] and (time.time() - ctx["file_mtime"]) < INFLIGHT_S:
        return True, (f"session file written {int(time.time() - ctx['file_mtime'])} s ago "
                      f"(< {INFLIGHT_S} s) — a turn may be in flight")
    if ctx["recent_log_activity"] and (time.time() - ctx["recent_log_activity"]) < INFLIGHT_S:
        return True, (f"ACP log activity for this session {int(time.time() - ctx['recent_log_activity'])} s ago "
                      f"(< {INFLIGHT_S} s) — a turn may be in flight")
    return False, ""


# ── recovery — bound to the panel's existing legs, nothing invented ─────────────────────────────

def panel_post(path: str, body: dict, timeout: int = 30) -> tuple[int | None, dict]:
    req = urllib.request.Request(PANEL + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": str(e)}


def journal(entry: dict) -> None:
    entry = {"ts": iso_now(), "source": "acp-doctor", **entry}
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"  [journal-unavailable] {e}", file=sys.stderr)


def already_journaled(conversation: str, top_cause: str | None, within_h: float = 6.0) -> bool:
    """Watch mode runs every 5 min — a standing finding must not be journaled 288×/day.
    Detect entries are deduped per (conversation, top_cause) over a 6 h window;
    remediation/escalation entries are never deduped (each fire is an event worth keeping)."""
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - within_h * 3600
        for line in JOURNAL.read_text(errors="replace").splitlines()[-2000:]:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("source") != "acp-doctor" or e.get("action") != "detect":
                continue
            if e.get("conversation") != conversation or e.get("top_cause") != top_cause:
                continue
            ts = e.get("ts", "")
            try:
                if datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= cutoff:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def board_escalate(genre: str, detail: str) -> None:
    """The breaker tripped: auto-recovery STOPPED and the operator must see it. An agent may ADD
    an item it discovered; only the operator closes one."""
    item = {
        "id": f"acp-doctor-plagued-{genre}",
        "title": f"ACP doctor: {genre} auto-recovery tripped the circuit breaker — STOPPED",
        "one": (f"acp_doctor.py fired the {genre} remediation ≥3× in 30 min; it is now PLAGUED and "
                f"refuses to auto-fire. Detail: {detail[:400]}"),
        "sev": "stop",
        "lane": "khaoss",
    }
    try:
        r = subprocess.run(["/usr/bin/python3", str(BLOCKERS), "--add"],
                           input=json.dumps(item), capture_output=True, text=True, timeout=30)
        print(f"  [board] {r.stdout.strip() or r.stderr.strip()}")
    except Exception as e:
        print(f"  [board-unavailable] {e}", file=sys.stderr)


def breaker_allows(genre: str, remediation: str) -> tuple[bool, dict]:
    """Same breaker the panel runs, computed here so --watch can stop BEFORE a futile fire."""
    try:
        sys.path.insert(0, str(ROOT))
        import failure_immune as fim
        entries = []
        if JOURNAL.exists():
            for line in JOURNAL.read_text(errors="replace").splitlines()[-2000:]:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
        dec = fim.breaker_decision(genre, remediation, entries, iso_now())
        return bool(dec.get("allow")), dec
    except Exception as e:
        return True, {"reason": f"breaker unavailable ({e}) — allowing once, saying so"}


def re_deliver(conv: dict, text: str, dry: bool) -> dict:
    lane = lane_map().get(conv["id"][:8])
    if not lane:
        return {"fired": False, "why": f"conversation {conv['id'][:8]} has no lane mapping in panel_server._LANE_TEAMS — nothing to re-deliver into; PROPOSED only"}
    body = {"target": f"aionui:{lane['team']}",
            "text": ("Re-delivery (acp-doctor): your earlier prompt timed out and was lost before it "
                     "reached the model — re-sending it verbatim:\n" + text),
            "item": f"acp-doctor re-delivery for {conv['id'][:8]}", "from_operator": False}
    if dry:
        return {"fired": False, "dry_run": True, "would_send": body}
    status, res = panel_post("/operator/dispatch/send", body, timeout=30)
    return {"fired": status == 200, "status": status, "result": res, "lane": lane["lane"]}


def remint(conv: dict, dry: bool) -> dict:
    body = {"conversationId": conv["id"], "confirm": True, "genre": "process_health"}
    if dry:
        return {"fired": False, "dry_run": True, "would_fire": body}
    allow, dec = breaker_allows("process_health", "remint-acp-session")
    if not allow:
        journal({"action": "remediation", "genre": "process_health", "remediation": "remint-acp-session",
                 "fired": False, "plagued": True, "breaker": dec, "conversation": conv["id"][:8]})
        board_escalate("process_health", dec.get("reason", "breaker tripped"))
        return {"fired": False, "plagued": True, "breaker": dec}
    status, res = panel_post("/remediate/remint-acp-session", body, timeout=30)
    ok = status == 200 and res.get("ok", status == 200)
    journal({"action": "remediation", "genre": "process_health", "remediation": "remint-acp-session",
             "fired": True, "ok": ok, "status": status, "conversation": conv["id"][:8],
             "note": "confirm:true under operator instruction 2026-09-17; in-flight guard passed"})
    return {"fired": True, "status": status, "result": res}


def apply_fixes(ctx: dict, report: dict, dry: bool, watch: bool = False) -> list[dict]:
    """Pick the recovery the diagnosis names. Order matters: re-delivery needs no session
    surgery; re-minting is the heavier leg. In watch mode re-minting is ALWAYS proposed,
    never fired — re-minting a live seat RESURRECTS it, and the board's ruling on orphaned
    leads is that RETIRE-vs-re-mint is the operator's call, not a detector's (steward doctrine:
    'a detector that can destroy the work it was watching over')."""
    actions = []
    conv = ctx["conv"]
    busy, why = in_flight(ctx)
    if busy:
        return [{"action": "refused", "why": why,
                 "note": "the in-flight guard outranks recovery — a detector must not destroy the work it watches"}]

    st = stuck_turn(ctx)
    proc_fail = next((c for c in report["checks"] if c["id"] == "process_health" and c["status"] == "fail"), None)
    stale_fail = next((c for c in report["checks"] if c["id"] == "stale_live_session" and c["status"] == "fail"), None)

    if proc_fail or stale_fail:
        if watch:
            actions.append({"action": "proposed", "leg": "remint-acp-session",
                            "why": "re-minting is an operator decision (it RESURRECTS the conversation); "
                                   f"fire with: acp_doctor.py --fix {conv['id'][:8]}  — or the in-app Diagnose button"})
        else:
            actions.append({"action": "remint", "leg": "remint-acp-session", **remint(conv, dry)})
    if st:
        actions.append({"action": "re-deliver", "lost_prompt_age_min": st["age_min"],
                        "text_preview": st["text"][:120], **re_deliver(conv, st["text"], dry)})
    if not actions:
        actions.append({"action": "none-needed"})
    return actions


def verify_response(con: sqlite3.Connection, conv_id: str, since_ms: int, wait_s: int = 120) -> bool:
    """Did the seat actually answer? Confirmed from the messages table — the destination,
    not the sender's 200 (the dispatch rule)."""
    deadline = time.time() + wait_s
    while time.time() < deadline:
        r = con.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND position='left' "
            "AND type='text' AND created_at>?", (conv_id, since_ms)).fetchone()
        if r and r[0] > 0:
            return True
        time.sleep(5)
    return False


# ── reporting ───────────────────────────────────────────────────────────────────────────────────

def render(report: dict, actions: list[dict] | None = None) -> str:
    L = []
    r = report["route"]
    L.append(f"═══ ACP DOCTOR — conversation {report['conversationId']} "
             f"({report['conversationName'][:40]}) ═══")
    L.append(f"route: {r['configuredModelId'] or '—'} · profile {r['profile'] or '—'} · "
             f"session {str(r['acpSessionId'] or '—')[:8]} · live: {r['sessionAlive']}")
    L.append(f"verdict: {report['summary']}")
    for c in report["checks"]:
        mark = {"ok": "PASS", "warn": "WARN", "fail": "FAIL", "unknown": "·  ·"}[c["status"]]
        L.append(f"  [{mark}] {c['title']}")
        for e in c["evidence"]:
            L.append(f"         {e}")
    if actions is not None:
        L.append("── recovery ──")
        for a in actions:
            L.append(f"  {a}")
    return "\n".join(L)


# ── modes ───────────────────────────────────────────────────────────────────────────────────────

def pick_default_conversation(con) -> dict | None:
    """Most recent at-risk conversation: an unanswered user prompt first, else the most recently
    active hermes-ACP conversation. None ⇒ nothing to diagnose (said, not assumed)."""
    convs = acp_conversations(con)
    scored = []
    for cv in convs:
        last = last_activity_ms(con, cv["id"])
        if not last:
            continue
        msgs = messages_recent(con, cv["id"], 6)
        unanswered = bool(msgs) and msgs[0]["position"] == "right" and msgs[0]["type"] == "text"
        scored.append((1 if unanswered else 0, last, cv))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def run(what: str | None, dry: bool = False, watch: bool = False) -> int:
    if not DB.exists():
        print(f"UNREADABLE: AionUi DB not found at {DB} — this is NOT 'no findings'", file=sys.stderr)
        return 4
    con = db_conn()
    log_src, log_lines = log_tail_lines()
    convs = acp_conversations(con)
    if not convs:
        print("UNREADABLE-EMPTY: zero hermes-ACP conversations in the DB — an empty scan is not "
              "a clean scan (the FC-3 rule). Check the DB/schema, do not report all-clear.", file=sys.stderr)
        return 4

    findings = 0
    fired = False
    try:
        if watch:
            targets = convs
        elif what:
            target = next((c for c in convs if c["id"].startswith(what) or what in c["id"]), None)
            if not target:
                print(f"conversation {what!r} not found among {len(convs)} hermes-ACP conversations", file=sys.stderr)
                return 4
            targets = [target]
        else:
            t = pick_default_conversation(con)
            if not t:
                print("No hermes-ACP conversation with traffic to diagnose.")
                return 0
            targets = [t]

        single = len(targets) == 1  # a targeted diagnosis always reports; a scan only speaks up when something needs a word
        for cv in targets:
            ctx = live_ctx(cv, con, log_lines, log_src)
            report = diagnose(ctx)
            is_find = any(c["status"] == "fail" for c in report["checks"])
            has_warn = any(c["status"] == "warn" for c in report["checks"])
            if not single and not is_find and not has_warn:
                continue
            if is_find or has_warn:
                findings += 1
            actions = []
            if is_find and (watch or "--fix" in sys.argv):
                actions = apply_fixes(ctx, report, dry, watch=watch)
                if any(a.get("fired") for a in actions):
                    fired = True
                if not dry and any(a.get("action") == "re-deliver" and a.get("fired") for a in actions):
                    ok = verify_response(con, cv["id"], now_ms() - 60_000, wait_s=120 if not watch else 0)
                    actions.append({"action": "verify", "assistant_answered": ok,
                                    "note": "confirmed from the messages table (the destination), not the sender's 200"})
            if watch and (is_find or has_warn):
                top_cause = report["rankedCauses"][0]["id"] if report["rankedCauses"] else None
                if not already_journaled(cv["id"][:8], top_cause):
                    journal({"action": "detect", "status": "failed" if is_find else "warn",
                             "conversation": cv["id"][:8], "conversation_name": cv["name"][:30],
                             "top_cause": top_cause,
                             "actions": [a.get("action") for a in actions]})
            print(render(report, actions if actions else None))
            print()
    finally:
        con.close()
    if fired:
        return 2
    return 1 if findings else 0


# ── self-test: PROVE-FAIL — it must find what it exists to find, and refuse what it must ───────

def self_test() -> int:
    import importlib.util
    spec = importlib.util.spec_from_file_location("failure_immune", ROOT / "failure_immune.py")
    if spec is None or spec.loader is None:
        print("cannot load failure_immune.py for self-test", file=sys.stderr)
        return 3
    fim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fim)

    base_conv = {"id": "testconv01", "name": "synthetic", "type": "acp",
                 "extra": {"currentModelId": "qwen/qwen3.8-27b", "agentProfile": "lead-example",
                           "backend": "hermes", "acpSessionId": "example-session"}}
    now = time.time()
    base_ctx = {
        "conv": base_conv, "profile": "lead-example", "sid": "example-session",
        "proc": [12345], "files": ["synthetic-session-file.json"], "file_mtime": now,
        "recent_log_activity": 0.0,
        "msgs": [{"type": "text", "content": "hello", "position": "right", "created_at": now_ms() - 600_000},
                 {"type": "text", "content": "hi there, all good", "position": "left",
                  "created_at": now_ms() - 300_000}],
        "last_activity_ms": now_ms() - 300_000,
        "log_lines": [], "log_src": "synthetic",
        "registry": set(), "mammouth": {"qwen/qwen3.8-27b"}, "profile_model": "qwen/qwen3.8-27b",
    }

    def ctx(**over):
        import copy
        c = copy.deepcopy(base_ctx)
        c.update(over)
        return c

    results = []

    # 1. healthy → quiet
    r = diagnose(ctx())
    fails = [c["id"] for c in r["checks"] if c["status"] == "fail"]
    results.append(("healthy seat reports NO fail", not fails, fails))

    # 2. the 10:25 shape: file present (FC-3 passes it) but process dead, traffic recent
    r = diagnose(ctx(proc=[]))
    stale = next(c for c in r["checks"] if c["id"] == "stale_live_session")
    proc = next(c for c in r["checks"] if c["id"] == "process_health")
    results.append(("★ dead task behind a resolving file is FOUND (stale_live_session)",
                    stale["status"] == "fail" and proc["status"] == "fail",
                    [stale["status"], proc["status"]]))

    # 3. in-flight guard: fresh session file → recovery REFUSED
    c3 = ctx(proc=[])
    a = apply_fixes(c3, diagnose(c3), dry=False)
    results.append(("★ fresh session file REFUSES recovery (in-flight guard)",
                    any(x.get("action") == "refused" for x in a), a))

    # 4. lost prompt: unanswered user msg + 'Prompt timed out' log marker NEWER than the msg,
    #    on a LIVE process (so the detection rides the marker path, not the dead-task path)
    sid8 = "aaaaaaaa"  # must equal the test ctx's sid[:8]
    now_hms = datetime.now().strftime("%H:%M:%S")
    c4 = ctx(proc=[999], file_mtime=now - 3600,
             msgs=[{"type": "text", "content": "the lost prompt", "position": "right",
                    "created_at": now_ms() - 600_000}],
             log_lines=[f"{now_hms} › acp_adapter.server: Cancelled session {sid8}"])
    st = stuck_turn(c4)
    results.append(("★ lost prompt is DETECTED from the timed-out turn",
                    bool(st and st["text"] == "the lost prompt"), st))

    # 4b. an OLD marker (pre-dating the message) must NOT declare the later message lost
    old_hms = datetime.fromtimestamp(time.time() - 7200).strftime("%H:%M:%S")
    c4b2 = ctx(proc=[999], file_mtime=now - 3600,
               msgs=[{"type": "text", "content": "the later prompt", "position": "right",
                      "created_at": now_ms() - 600_000}],
               log_lines=[f"{old_hms} › acp_adapter.server: Cancelled session {sid8}"])
    results.append(("★ a stale marker (older than the message) does NOT declare the later prompt lost",
                    stuck_turn(c4b2) is None, stuck_turn(c4b2)))

    # 5. no marker + live process → NOT stuck (a waiting seat is not a lost seat)
    c5 = ctx(msgs=[{"type": "text", "content": "still working", "position": "right",
                    "created_at": now_ms() - 600_000}], log_lines=[])
    results.append(("a live seat with no failure marker is NOT declared lost",
                    stuck_turn(c5) is None, stuck_turn(c5)))

    # 6. breaker: 3 fires in 30 min → the 4th is refused (pure function, no live state)
    #    entries match the REAL journal shape — action='remediation' is what the breaker counts
    entries = [{"ts": (datetime.now(timezone.utc).replace(microsecond=0)).isoformat(),
                "action": "remediation", "genre": "process_health",
                "remediation": "remint-acp-session", "fired": True}
               for _ in range(3)]
    dec = fim.breaker_decision("process_health", "remint-acp-session", entries, iso_now())
    results.append(("★ breaker REFUSES the 4th auto-fire in 30 min", dec.get("allow") is False, dec))

    bad = 0
    print("═══ ACP DOCTOR SELF-TEST — prove-fail ═══")
    for label, ok, detail in results:
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label}")
        if not ok:
            print(f"      got: {detail}")
    print("★ SELF-TEST PASSED\n" if not bad else "✗ SELF-TEST FAILED\n")
    return 0 if not bad else 3


def main() -> int:
    args = [a for a in sys.argv[1:]]
    if "--self-test" in args:
        return self_test()
    dry = "--dry" in args
    # --all and --watch are the same patrol pass; --fix (or plain interactive) recovers the findings
    watch = "--watch" in args or "--all" in args
    what = next((a for a in args if not a.startswith("--")), None)
    return run(what, dry=dry, watch=watch)


if __name__ == "__main__":
    sys.exit(main())
