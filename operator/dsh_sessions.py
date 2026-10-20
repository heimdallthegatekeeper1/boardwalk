#!/usr/bin/env python3
"""Read-only discovery of DeepSeek Harness chats for the guide-agent picker.

★ WHY THIS EXISTS: the guide picker listed *agents*, and the chat the operator was actually in
  ("BoardWalk visual guide orchestrator") was nowhere in it.  The DSH GUI stores its chats in two
  homes — the GUI home (~/.dsh) and the ACP launch home (<workspace>/.dsh) — and the existing
  `dsh:harness` row is pinned to ONE session in the ACP home.  This module lists the top-level chats
  from both homes, with the measured live/resumable facts the UI must not guess.

★ HARD FACTS (measured by the research pass, not inferred here):
  · A chat that the DSH GUI currently has open holds its `session.lock`; an ACP client cannot attach
    to it (`session is already active`).  It is reported `live` and the picker must refuse a ping.
  · ACP resume also refuses subagent/parented sessions and a cwd mismatch.
  · There is no per-session DSH URL.  The best honest link is the GUI root; the chat is in its sidebar.

This module only reads: the workspace index, the projcache titles, and the session locks.  It never
writes, never opens the lock for writing (the harness's acquire would truncate it), and never reads
transcript bodies.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path

GUI_HOME = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))
# ★ RELEASE DEFAULT: the release directory itself. The private working tree's checkout name is not
#   shipped; a real workspace can be named with KHAOSS_DSH_WORKSPACE on the command line.
ACP_WORKSPACE = Path(os.environ.get("KHAOSS_DSH_WORKSPACE")
                     or Path(__file__).resolve().parent.parent)
ACP_HOME = ACP_WORKSPACE / ".dsh"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _sid(raw: str) -> str:
    """Two shapes coexist: 'session-<uuid>' and '<uuid>'. Normalise to the bare id."""
    s = str(raw or "").strip()
    return s[len("session-"):] if s.startswith("session-") else s


def _slug(cwd: str) -> str:
    # The DSH home escapes a space as ~0020 and a slash as '-': /home/<user>/Deepseek harness
    # -> --home-USER-Deepseek~0020harness--.  A bare replace('/','-') found no lock and reported a
    # live chat as free — the exact "it should have been able to attach" lie this module prevents.
    s = str(cwd).strip("/").replace("/", "-")
    s = re.sub(r"[^A-Za-z0-9._-]", lambda m: "~%04x" % ord(m.group(0)), s)
    return "--" + s + "--"


def _title(home: Path, sid: str) -> str:
    p = home / "storages" / "session_projcache" / "sessions" / f"session-{sid}.json"
    if not p.is_file():
        p = home / "storages" / "session_projcache" / "sessions" / f"{sid}.json"
    d = _read_json(p, {}) or {}
    rows = ((d.get("record") or {}).get("rows") or {})
    for key in ("title", "titleInput"):
        v = (rows.get(key) or {}).get("val")
        if isinstance(v, str) and v.strip():
            return v.strip()[:120]
    return "(untitled)"


def _live(home: Path, cwd: str, sid: str) -> bool:
    """Non-blocking flock on a READ-ONLY fd.  Do NOT open for write: acquire truncates the lock."""
    # The session directory keeps the 'session-' prefix even when the workspace index stores the
    # bare uuid.  Accept both shapes so an index change cannot silently hide a live lock.
    for name in (f"session-{sid}", sid):
        lock = home / "sessions" / _slug(cwd) / name / "session.lock"
        if lock.is_file():
            break
    else:
        return False
    try:
        fd = os.open(lock, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _home_rows(home: Path, label: str) -> list[dict]:
    ws = _read_json(home / "storages" / "workspace.json", {}) or {}
    tables = (ws.get("tables") or {}).get("workspaces") or {}
    archived = {_sid(x) for x in ((ws.get("global") or {}).get("archivedSessionIds") or [])}
    out = []
    for wid, w in tables.items():
        if not isinstance(w, dict):
            continue
        cwd = str(w.get("path") or "")
        for raw in (w.get("sessionIds") or []):
            sid = _sid(raw)
            if not sid:
                continue
            out.append({
                "session_id": sid,
                "title": _title(home, sid),
                "cwd": cwd,
                "home": label,
                "home_path": str(home),
                "archived": sid in archived,
                "live": _live(home, cwd, sid),
                "workspace_title": str(w.get("title") or ""),
            })
    # dedupe (a session can be listed under more than one workspace) — keep the first
    seen, uniq = set(), []
    for c in out:
        if c["session_id"] in seen:
            continue
        seen.add(c["session_id"])
        uniq.append(c)
    return uniq


def chats(include_archived: bool = True) -> dict:
    """Every top-level chat in both DSH homes, with the measured facts and an honest `resumable`."""
    rows = []
    for home, label in ((GUI_HOME, "DSH GUI"), (ACP_HOME, "ACP launch")):
        try:
            rows.extend(_home_rows(home, label))
        except Exception:  # noqa: BLE001 — a store change must not kill the picker
            continue
    for c in rows:
        if not include_archived and c.get("archived"):
            continue
        # ACP resume is only possible from the ACP home, when the chat is not live, and its cwd
        # must match the workspace the launcher boots into.  Everything else is link-only.
        c["resumable"] = bool(
            c.get("home") == "ACP launch" and not c.get("live") and not c.get("archived")
            and str(c.get("cwd") or "") == str(ACP_WORKSPACE))
        c["why_not"] = (
            "this chat is open in the DSH GUI right now; an ACP client cannot attach to a live "
            "session — type in the GUI" if c.get("live") else
            "this chat lives in the DSH GUI home; the ACP launcher reads the checkout home — open "
            "the DSH GUI instead" if c.get("home") == "DSH GUI" else
            "archived chats are not resumable through the picker" if c.get("archived") else
            "ACP requires the chat's cwd to match the launcher workspace" if not c["resumable"]
            else "")
    rows.sort(key=lambda c: (c.get("archived", False), c.get("live", False), c.get("home") != "DSH GUI"))
    return {
        "ok": True,
        "chats": rows,
        "count": len(rows),
        "live": sum(1 for c in rows if c.get("live")),
        "resumable": sum(1 for c in rows if c.get("resumable")),
        "gui_url": "http://127.0.0.1:3080",
        "note": ("No per-chat URL exists in the DSH GUI; open the GUI and pick the chat by title. "
                 "A live chat holds its lock, so a guide ping must be refused rather than queued. "
                 "Top-level chats only; subagent sessions are not chats and are excluded."),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    d = chats()
    if a.json:
        print(json.dumps(d, indent=1))
    else:
        print(f"{d['count']} chat(s) · {d['live']} live · {d['resumable']} resumable")
        for c in d["chats"]:
            mark = "LIVE" if c["live"] else ("RESUMABLE" if c["resumable"] else "link-only")
            print(f"  [{mark:9}] {c['title']} · {c['session_id'][:8]} · {c['home']} · {c['cwd']}")
