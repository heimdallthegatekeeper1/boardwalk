#!/usr/bin/env python3
"""The one registry for the agent that receives messages from the guided board.

The guide agent is deliberately NOT the board row's destination.  Selecting Codex here means
"send my next guide message to Codex"; it does not reassign, renumber, advance, or rewrite the row.
Every delivery gets a durable id and an append-only status trail.  `queued` is never painted as an
answer, and a transport acknowledgement is never painted as completed work.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = Path(os.environ.get("KHAOSS_GUIDE_REGISTRY") or (HERE / "guide_backends.json"))
# ★ THE STATE AND THE EVENT TRAIL ARE OVERRIDABLE FOR AN ISOLATED MACHINE TEST. A suite that drives
#   the card's Ask with a disconnected backend must not change the operator's selected agent or append
#   a refusal to his real trail. The defaults are byte-identical to the shipped paths.
STATE = Path(os.environ.get("KHAOSS_GUIDE_STATE") or (HERE / "guide_backend_state.json"))
EVENTS = Path(os.environ.get("KHAOSS_GUIDE_EVENTS") or (HERE / "guide_backend_events.jsonl"))
FOCUS = Path(os.environ.get("KHAOSS_GUIDE_FOCUS") or (HERE / "guide_focus.json"))
WORKER = HERE / "guide_backend_worker.py"
VALID_STATES = {"queued", "acknowledged", "running", "failed", "completed", "disconnected"}

# ── THE DEEPSEEK HARNESS ROUTE ───────────────────────────────────────────────────────────────────
# ★★ A ROUTE IS A BINARY, A CREDENTIAL AND A FRESH RECEIPT — never a label.  `launch.py` is the
#    installed launcher for the DeepSeek Harness: it reads the key ON THIS BOX, injects it into the
#    child, and refuses a missing/stale/other-key receipt BEFORE any provider request is made.  This
#    module therefore never reads the credential's value at all — it checks that the file exists and
#    that the receipt is fresh, and nothing else about the credential ever leaves launch.py.
# ★ The receipt is a 24h activation receipt by the launcher's own contract, so a stale receipt is
#   reported as `disconnected` rather than as ready: launch.py would genuinely refuse, and a picker
#   that says READY for a route the launcher rejects is the dead button this registry exists to end.
# ★ RELEASE DEFAULT: the launcher/workspace are OPTIONAL local integrations, not this repo's files.
#   An env var names them; with neither set the probe reports "no launcher" and the backend is
#   disconnected, which is the honest state on a fresh machine. No private Downloads path ships.
DSH_LAUNCHER = Path(os.environ.get("KHAOSS_DSH_LAUNCHER")
                    or (HERE.parent / "tools" / "deepseek-harness" / "launch.py"))
DSH_WORKSPACE = str(Path(os.environ.get("KHAOSS_DSH_WORKSPACE") or HERE.parent))
DSH_RECEIPT = DSH_LAUNCHER.parent / "validated-route.json"
DSH_KEY = Path(os.environ.get("KHAOSS_DSH_KEY") or (HERE.parent / ".khaoss-secrets" / "deepseek-api-key"))
DSH_RECEIPT_TTL = 86400
DSH_PROMPT_TIMEOUT = 600
_NODE_CACHE: dict = {}


def _ss():
    sys.path.insert(0, str(HERE))
    import safe_store
    return safe_store


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _read_json(path: Path, default):
    return _ss().read_json(path, default)


def _write_json(path: Path, value):
    return _ss().write_json(path, value)


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".lock")
    with lock.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def _backup_keep20(path: Path, tag: str = "backup") -> str:
    """★ BACKUP BEFORE WRITE, KEEP-20.  The registry is the address book every delivery reads; a
    write to it without a copy is a change to the operator's agent choices with no way back."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_name(f"{path.name}.bak.{ts}_{tag}")
    try:
        if not dst.exists():
            shutil.copy2(path, dst)
        stale = sorted(path.parent.glob(path.name + ".bak.*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[20:]
        for old in stale:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        return ""
    return str(dst)


def _dsh_route() -> tuple[bool, str]:
    """Is the harness REACHABLE right now — and say which part failed.

    ★ It answers about presence and freshness only.  The credential's value is read by launch.py,
      never here, and is never copied, hashed or logged by this module."""
    if not DSH_LAUNCHER.is_file() or not os.access(DSH_LAUNCHER, os.X_OK):
        return False, f"the harness launcher is not an executable file at {DSH_LAUNCHER}"
    node_dir, node_detail = _dsh_node()
    if not node_dir:
        return False, node_detail
    if not DSH_KEY.is_file():
        return False, ("no DeepSeek API key is saved for the harness — launch.py would refuse "
                       "locally, so no route exists")
    try:
        receipt = json.loads(DSH_RECEIPT.read_text())
    except (OSError, ValueError):
        return False, ("the harness has no readable validated-route.json — run "
                       "launch.py --validate after saving the key")
    age = time.time() - float(receipt.get("validated_at") or 0)
    if not 0 <= age < DSH_RECEIPT_TTL:
        return False, (f"the harness route receipt is stale ({age / 3600:.1f}h old, cap "
                       f"{DSH_RECEIPT_TTL / 3600:.0f}h) — run launch.py --validate; no fallback")
    model = str(receipt.get("model") or "")
    if not model:
        return False, "the harness route receipt names no model — run launch.py --validate"
    return True, (f"ACP route ready through launch.py on model {model} ({node_detail}); the launcher "
                  f"was last validated {age / 3600:.1f}h ago. A prompt starts only after you send.")


def _dsh_node() -> tuple[str, str]:
    """★★★ A NODE THAT CAN RUN THE HARNESS — MEASURED, NOT ASSUMED.

    The launcher's binary is `node_modules/.bin/dsh`, whose `#!/usr/bin/env node` resolves whatever
    `node` is first on PATH.  The panel SERVICE's PATH is not this shell's PATH: it resolves Node
    v20.19.6, under which dsh 0.1.5-rc.2 exits 0 having printed NOTHING — no error, no version, no
    protocol.  The board therefore built a guide backend that answered perfectly from a terminal and
    died with "the harness closed its ACP stream" from the one surface the operator actually uses.

    ★ So the candidate list is TESTED by making the harness print its own version.  A version string
      is evidence; a node major number copied into this file is a guess that rots.  The result is
      cached for the life of the process, because the picker polls inventory() and this spawns.
    ★ The resolved directory is only ever PREFIXED onto the child's PATH — nothing global changes."""
    if _NODE_CACHE:
        return _NODE_CACHE.get("dir", ""), _NODE_CACHE.get("detail", "")
    dsh_bin = DSH_LAUNCHER.parent / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    candidates = [os.environ.get("KHAOSS_DSH_NODE") or "", shutil.which("node") or "",
                  "/usr/bin/node", "/usr/local/bin/node"]
    try:
        candidates += sorted((str(p) for p in Path.home().glob(".nvm/versions/node/*/bin/node")),
                             reverse=True)
    except OSError:
        pass
    tried = []
    for binary in candidates:
        if not binary or binary in tried or not Path(binary).is_file():
            continue
        tried.append(binary)
        try:
            version = subprocess.run([binary, "--version"], text=True, capture_output=True,
                                     timeout=20).stdout.strip()
            probe = subprocess.run([binary, str(dsh_bin), "--version"], text=True,
                                   capture_output=True, timeout=30)
        except Exception:
            continue
        if probe.stdout.strip():
            detail = f"{version} at {binary} runs dsh {probe.stdout.strip()}"
            _NODE_CACHE.update({"dir": str(Path(binary).parent), "detail": detail})
            return _NODE_CACHE["dir"], detail
    _NODE_CACHE.update({"dir": "", "detail": (
        f"no Node on this box could run the harness: every candidate ({', '.join(tried) or 'none'}) "
        f"exited without printing a version for {dsh_bin}")})
    return "", _NODE_CACHE["detail"]


def _dsh_env() -> dict:
    """The child environment for the harness: this process's own, with the working Node first.

    ★★ FIRST, NOT MERELY PRESENT.  The first version of this guard asked whether the working node's
    directory appeared ANYWHERE on PATH and skipped the rewrite when it did — and the panel's PATH
    already ends in `/usr/bin`, so the working Node was present and never reached.  The harness kept
    dying on a Node that cannot run it while this function reported that it had fixed the path.
    Position is the whole property; membership is not."""
    env = dict(os.environ)
    node_dir, _ = _dsh_node()
    parts = [p for p in str(env.get("PATH", "")).split(os.pathsep) if p]
    if node_dir and (not parts or parts[0] != node_dir):
        env["PATH"] = os.pathsep.join([node_dir] + [p for p in parts if p != node_dir])
    return env


def _dsh_session_log(workspace: str, session_id: str) -> str:
    """Where the harness itself writes this session's durable log, or "" if persistence is off.

    ★ THE HARNESS'S OWN RECORD IS THE RECEIPT THAT MATTERS.  Our event trail says what the board
      did; this path is the artifact the harness wrote from the inside, and it is what makes
      "it arrived" checkable by someone who does not trust this process."""
    if not workspace or not session_id:
        return ""
    slug = "--" + str(Path(workspace)).strip("/").replace("/", "-") + "--"
    root = Path(workspace) / ".dsh" / "sessions" / slug / session_id
    for name in ("session.v3.jsonl.zstd", "session.v2.jsonl.zstd", "session.jsonl"):
        if (root / name).exists():
            return str(root / name)
    return ""


def bind(backend_id: str, session_id: str) -> dict:
    """★★ THE ADDRESS BELONGS IN THE REGISTRY ROW.  A harness session that a later guide message
    must resume cannot live in a second file beside this one — that is two places to disagree about
    which conversation the operator is talking to.  Codex stores its thread here for the same reason."""
    store = _read_json(REGISTRY, {"backends": []}) or {"backends": []}
    hit = next((r for r in store.get("backends", []) if r.get("id") == backend_id), None)
    if hit is None:
        return {"ok": False, "error": f"no registered backend {backend_id!r}"}
    if str(hit.get("session_id") or "") == str(session_id):
        return {"ok": True, "unchanged": True, "backend_id": backend_id, "session_id": session_id}
    _backup_keep20(REGISTRY, "before_bind")
    hit["session_id"] = str(session_id)
    _write_json(REGISTRY, store)
    return {"ok": True, "backend_id": backend_id, "session_id": session_id}


def register(row: dict) -> dict:
    """Register a real address.  A label alone is not a route."""
    backend_id = str(row.get("id") or "").strip()
    runtime = str(row.get("runtime") or "").strip()
    if not backend_id or not runtime:
        return {"ok": False, "error": "backend registration needs id and runtime"}
    store = _read_json(REGISTRY, {"backends": []}) or {"backends": []}
    previous = next((r for r in store.get("backends", []) if r.get("id") == backend_id), {})
    rows = [r for r in store.get("backends", []) if r.get("id") != backend_id]
    clean = {
        "id": backend_id,
        "label": str(row.get("label") or backend_id),
        "group": str(row.get("group") or "Registered agents"),
        "runtime": runtime,
        "thread_id": str(row.get("thread_id") or ""),
        "session_id": str(row.get("session_id") or ""),
        "pid": str(row.get("pid") or ""),
        "lane": str(row.get("lane") or ""),
        "delivery": str(row.get("delivery") or "queued"),
        "how": str(row.get("how") or ""),
        "capabilities": list(row.get("capabilities") or []),
        "registered_at": str(row.get("registered_at") or _now()),
    }
    # ★ A ROUTE CAN NEED MORE THAN AN ID AND A RUNTIME.  These are the fields a runtime genuinely
    #   reads to reach its target: Codex's rollout file, and the harness's launcher and workspace.
    # ★★ AND A RE-REGISTER MUST NOT WIPE A LIVE ADDRESS.  Re-running registration with only a label
    #    ("update the wording") would otherwise silently drop the launcher and the one resumable
    #    harness session — a route destroyed by a description change. An omitted field inherits the
    #    row it is replacing; only an explicit non-empty value replaces one.
    for extra in ("rollout_path", "launcher", "workspace"):
        value = row.get(extra) or previous.get(extra)
        if value:
            clean[extra] = str(value)
    if not str(row.get("session_id") or "").strip() and previous.get("session_id"):
        clean["session_id"] = str(previous["session_id"])
    rows.append(clean)
    _write_json(REGISTRY, {"version": 1, "backends": rows})
    return {"ok": True, "backend": clean}


def _process_cmdlines() -> list[tuple[str, str]]:
    out = []
    for p in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            out.append((p.parent.name, p.read_bytes().replace(b"\0", b" ").decode(errors="replace")))
        except OSError:
            pass
    return out


def _runtime_state(row: dict, processes=None) -> tuple[str, str]:
    processes = processes if processes is not None else _process_cmdlines()
    runtime = row.get("runtime")
    if runtime == "codex-app-server":
        if row.get("thread_id") and Path(row.get("rollout_path") or "/not-present").exists():
            return "ready", "thread is registered on disk; a turn starts only after you send"
        if row.get("thread_id"):
            return "ready", "thread id is registered; use Test route to read it live"
        return "disconnected", "no Codex thread id is registered"
    if runtime == "cc-guide-watcher":
        sid = str(row.get("session_id") or "")
        pid = str(row.get("pid") or "")
        if not pid:
            return "disconnected", "no exact process is bound to this Claude Code session"
        cmd_by_pid = dict(processes)
        session_alive = sid and sid in cmd_by_pid.get(pid, "")
        # A watcher anywhere on the box is not this seat's watcher. Walk /proc parent links and
        # require the tail process to be a descendant of the registered Claude process.
        descendants, frontier = set(), {pid}
        while frontier:
            new = set()
            for proc in Path("/proc").glob("[0-9]*/stat"):
                try:
                    fields = proc.read_text(errors="replace").split()
                    child, parent = proc.parent.name, fields[3]
                except (OSError, IndexError):
                    continue
                if parent in frontier and child not in descendants:
                    new.add(child)
            descendants.update(new); frontier = new
        watcher = any(p in descendants and "guide_replies.jsonl" in cmd for p, cmd in processes)
        if session_alive and watcher:
            return "watching", "session and guide-reply watcher are both running"
        if session_alive:
            return "queue-only", "session is running, but its guide-reply watcher was not proven"
        return "disconnected", "the named Claude Code session is not running"
    if runtime == "dsh-acp":
        # ★ A LIVE PUSH PATH, judged by the launcher's own three gates rather than by optimism.
        ok, detail = _dsh_route()
        return ("ready" if ok else "disconnected"), detail
    delivery = row.get("delivery")
    if delivery == "live":
        return "ready", "live dispatch route is registered; response is not implied"
    if delivery == "queued":
        return "queue-only", "handoff queue only; an agent must collect it"
    return "disconnected", str(row.get("how") or "no one-click route")


# ── THE VOICE SEAT'S CAPABILITY, DERIVED FROM THE RUNTIME ─────────────────────────────────────────
# ★★★ ONE FACT, ONE PLACE.  The voice seat needs to know, per registered agent, whether a spoken turn
#     can come back as a MODEL ANSWER or only as an ACKNOWLEDGEMENT.  That is already implied by the
#     runtime — `codex-app-server` can start a turn and read the reply; a watcher that only tails a
#     file cannot — so it is DERIVED here and never stored.  A stored flag would be a second copy of a
#     fact the runtime implies, and the copy is what rots when a runtime is added.
# ★★ AND IT IS NOT A SILENT FILTER.  A row that can only acknowledge is OFFERED, marked `queue-only`,
#    and routed to; it is never dropped, because "the agent you picked is missing from the list" is
#    the dead control this registry exists to end.
VOICE_TURNS = ("answer", "queue-only", "live-send")


def _voice_turn(row: dict) -> str:
    """What ONE spoken turn through this row can produce.  Derived from `runtime` alone.

    ★ EXCEPT FOR A LINK-ONLY ROW, WHERE THE RUNTIME IS NOT THE WHOLE FACT. A specific DSH chat that
      is live in the GUI has runtime `dsh-acp` but capabilities `["link-only"]`; the worker cannot
      attach, so `notify()` returns 409 and queues nothing. Advertising `answer` for it in the voice
      seat's backing list was an over-claim: the seat would have offered a route that can only open
      a window. `queue-only` is the honest capability of a link-only row, and the row is still
      OFFERED and marked rather than dropped."""
    runtime = row.get("runtime")
    caps = row.get("capabilities") or []
    _chat = row.get("_chat") if isinstance(row.get("_chat"), dict) else None
    if caps == ["link-only"] or (_chat is not None and not _chat.get("resumable")):
        return "queue-only"
    if runtime in ("codex-app-server", "dsh-acp"):
        # Both runtimes are driven synchronously by guide_backend_worker.py: resume/bind a session,
        # send one prompt, collect the assistant text.  dsh-acp is included because the row exists in
        # this registry and the worker has the answer path (`dsh_prompt`); calling it queue-only would
        # be a false claim about a route that genuinely answers.
        return "answer"
    if runtime == "cc-guide-watcher":
        # A transport acknowledgement is not a model answer — the worker says so in its own words.
        return "queue-only"
    if runtime == "operator-dispatch":
        return "live-send" if str(row.get("delivery") or "") == "live" else "queue-only"
    return "live-send" if str(row.get("delivery") or "") == "live" else "queue-only"


def _voice_turn_cost(row: dict) -> str:
    """Where a spoken turn's spend lands.  Derived from the same runtime, stated at the control."""
    runtime = row.get("runtime")
    caps = row.get("capabilities") or []
    _chat = row.get("_chat") if isinstance(row.get("_chat"), dict) else None
    if caps == ["link-only"] or (_chat is not None and not _chat.get("resumable")):
        return "no spend: this chat is link-only while it is open; no ACP prompt is sent"
    if runtime == "codex-app-server":
        return "spends the Codex seat already open; no new key"
    if runtime == "dsh-acp":
        return "spends the DeepSeek key on this box (read by launch.py, never by the seat)"
    if runtime == "cc-guide-watcher":
        return "no spend: this route can only acknowledge, it returns no answer"
    return ("no spend from the seat; the destination agent does the work"
            if row.get("delivery") == "live" else "no spend: queued for an agent to collect")


def _dispatch_rows() -> list[dict]:
    try:
        sys.path.insert(0, str(HERE))
        import dispatch_targets
        d = dispatch_targets.targets()
    except Exception:
        return []
    out = []
    for group, rows in d.get("groups", {}).items():
        for r in rows:
            out.append({
                "id": "dispatch:" + r["id"], "target": r["id"], "label": r["label"],
                "group": group, "runtime": "operator-dispatch", "delivery": r.get("delivery"),
                "how": r.get("how", ""), "capabilities": ["guide-message"],
            })
    return out


def _event_rows(limit: int = 200) -> list[dict]:
    rows = []
    try:
        lines = EVENTS.read_text(errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines[-max(limit, 1):]:
        try:
            r = json.loads(line)
            if r.get("status") in VALID_STATES:
                rows.append(r)
        except ValueError:
            pass
    return rows


def _state_key(consumer: str) -> tuple[str, str]:
    """★ ONE FILE, TWO CONSUMERS, TWO KEYS.  The guide board and the voice seat select from the same
    registry but not the same agent — the board's guide conversation must not be reassigned because
    the operator picked a voice backing, and vice versa.  The keys live in one file so there is one
    writer and one place to look."""
    if consumer == "voice":
        return "voice_selected", "voice_selected_at"
    if consumer == "guide":
        return "selected", "selected_at"
    raise ValueError(f"consumer must be 'guide' or 'voice', got {consumer!r}")


def _dsh_chat_rows() -> list[dict]:
    """Specific DSH chats as guide-agent rows, with the measured attach limits."""
    try:
        import dsh_sessions
        d = dsh_sessions.chats()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for c in d.get("chats") or []:
        live = bool(c.get("live"))
        resumable = bool(c.get("resumable"))
        detail = ("this chat is open in the DSH GUI; ACP cannot attach — type in the DSH GUI"
                  if live else (c.get("why_not") or
                                ("ended chat in the ACP home; a prompt resumes it" if resumable
                                 else "link-only")))
        out.append({
            "id": "dsh:chat:" + str(c.get("session_id")),
            "label": "DeepSeek Harness — " + str(c.get("title") or "(untitled)")[:70],
            "group": "DeepSeek Harness — chats",
            "runtime": "dsh-acp",
            "thread_id": "",
            "session_id": str(c.get("session_id") or ""),
            "pid": "",
            "lane": "dsh-chat",
            "workspace": str(c.get("cwd") or ""),
            "launcher": str(DSH_LAUNCHER),
            "delivery": "live",
            "how": "a specific DeepSeek Harness chat; a live GUI chat cannot be attached",
            "capabilities": (["session-resume", "prompt", "answer-back"] if resumable
                             else ["link-only"]),
            "registered_at": _now(),
            "state": "ready" if resumable else "disconnected",
            "state_detail": detail,
            "_chat": c,
        })
    return out


def inventory(consumer: str = "guide") -> dict:
    key, _at = _state_key(consumer)
    registered = list((_read_json(REGISTRY, {"backends": []}) or {}).get("backends", []))
    rows = registered + [r for r in _dispatch_rows()
                         if r.get("id") not in {x.get("id") for x in registered}] + _dsh_chat_rows()
    processes = _process_cmdlines()
    latest = {}
    for ev in _event_rows():
        latest[ev.get("backend_id")] = ev
    for row in rows:
        if str(row.get("id", "")).startswith("dsh:chat:"):
            row["state"] = "ready" if (row.get("_chat") or {}).get("resumable") else "disconnected"
            row["state_detail"] = str(row.get("state_detail") or "")
        else:
            state, detail = _runtime_state(row, processes)
            row["state"] = state
            row["state_detail"] = detail
        # ★ the voice seat's two derived facts, on every row, for every consumer (the board's picker
        #   shows them too — an agent that cannot answer a spoken turn should say so where it is
        #   chosen, not only where it is used).
        row["voice_turn"] = _voice_turn(row)
        row["voice_turn_cost"] = _voice_turn_cost(row)
        if row.get("id") in latest:
            row["last_event"] = latest[row["id"]]
    persisted = _read_json(STATE, {}) or {}
    selected_id = str(persisted.get(key) or "")
    ids = {r.get("id") for r in rows}
    if selected_id not in ids:
        # ★ THE EXISTING FALLBACK RULE, UNCHANGED: an id that is no longer registered falls back to
        #   the first runtime that can genuinely answer, and never raises.
        selected_id = next((r["id"] for r in rows if r.get("runtime") == "codex-app-server"), "")
    selected = next((r for r in rows if r.get("id") == selected_id), None)
    voice_id = str(persisted.get("voice_selected") or "")
    if voice_id not in ids:
        voice_id = next((r["id"] for r in rows if r.get("runtime") == "codex-app-server"), "")
    for row in rows:
        row["voice_selected"] = row.get("id") == voice_id
    groups = {}
    for row in rows:
        groups.setdefault(row.get("group") or "Other", []).append(row)
    return {
        "ok": True, "groups": groups, "selected_id": selected_id, "selected": selected,
        "consumer": consumer,
        # ★ published whole so the voice seat can mark its own choice without a second read of the
        #   state file (and therefore without a second opinion about it)
        "voice_selected_id": voice_id,
        "voice_turns": sorted(VOICE_TURNS),
        "states": sorted(VALID_STATES),
        "pathways": {
            "guide_agent": "receives what you type in the guided panel",
            "row_destination": "owns or receives the board row; unchanged by this selector",
            "local_controls": "Next, Back, re-check, and rulings run locally; no guide agent needed",
        },
        "note": "Switching guide agent does not reassign a row or move the walk.",
    }


def select(backend_id: str, consumer: str = "guide") -> dict:
    """Choose the guide's agent, or the voice seat's backing — one writer, one key each.

    ★★ THE READ-MODIFY-WRITE IS UNDER ITS OWN LOCK, AND THE LOCK IS NOT THE STORE'S.  safe_store
       serialises each read and each write, but not the PAIR — so two selectors (the board and the
       seat) could both read, both write, and one choice would vanish.  A dedicated lock file is used
       because re-flocking the store's own lock inside an already-held lock would deadlock (flock is
       held per open file description, and a second open of the same file conflicts with itself).
    ★ THE OTHER CONSUMER'S KEY IS PRESERVED BY CONSTRUCTION: we never write a fresh dict, we update
      the one we just read."""
    key, at_key = _state_key(consumer)
    before = FOCUS.read_bytes() if FOCUS.exists() else b""
    inv = inventory(consumer=consumer)
    rows = [r for group in inv["groups"].values() for r in group]
    hit = next((r for r in rows if r.get("id") == backend_id), None)
    if not hit:
        return {"ok": False, "consumer": consumer,
                "error": f"unknown guide backend {backend_id!r}", "changed": False}
    sel_lock = STATE.with_name(STATE.name + ".select.lock")
    with sel_lock.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cur = _read_json(STATE, {}) or {}
        if str(cur.get(key) or "") == str(backend_id):
            changed = False
        else:
            _backup_keep20(STATE, f"before_select_{consumer}")
            changed = True
        cur["version"] = 1
        cur[key] = backend_id
        cur[at_key] = _now()
        _write_json(STATE, cur)
    after = FOCUS.read_bytes() if FOCUS.exists() else b""
    return {"ok": True, "consumer": consumer, "selected": hit, "changed": changed,
            "walk_preserved": before == after,
            "focus_sha256": hashlib.sha256(after).hexdigest(),
            "note": "guide agent changed; row destination and walk position were not changed"}



def _event_id(source: dict, backend_id: str) -> str:
    seed = "\0".join(str(source.get(k) or "") for k in
                     ("source_at", "step", "target", "kind", "urgency", "text")) + "\0" + backend_id
    return "guide-" + hashlib.sha256(seed.encode()).hexdigest()[:20]


def transition(event_id: str, backend_id: str, status: str, detail: str = "", **extra) -> dict:
    if status not in VALID_STATES:
        raise ValueError(f"invalid state {status!r}")
    row = {"at": _now(), "event_id": event_id, "backend_id": backend_id,
           "status": status, "detail": str(detail)[:1200], **extra}
    _append(EVENTS, row)
    return row


def event(event_id: str) -> dict | None:
    rows = [r for r in _event_rows(2000) if r.get("event_id") == event_id]
    if not rows:
        return None
    first, last = rows[0], rows[-1]
    return {**first, **last, "transitions": rows}


def notify(source: dict, start_worker: bool = True, consumer: str = "guide") -> dict:
    """Create ONE durable delivery record for the selected agent, then (optionally) start the worker.

    ★★ `consumer` SELECTS WHICH KEY.  The board's guide message and the voice seat's spoken turn are
       the same mechanism aimed at different selected agents; they must not share one pointer.
    ★ `start_worker=False` is what the voice seat uses: it wants the durable id and the `queued`
      transition HERE, and then runs `guide_backend_worker.py --voice-turn <id>` synchronously so the
      answer can be spoken in the same turn.  A background worker cannot speak."""
    _state_key(consumer)                       # reject an unknown consumer before writing anything
    inv = inventory(consumer=consumer)
    backend = inv.get("selected")
    if not backend:
        return {"ok": False, "consumer": consumer, "error": "no guide agent is selected"}
    eid = _event_id(source, backend["id"])
    old = event(eid)
    if old:
        return {"ok": True, "consumer": consumer, "deduplicated": True, "event": old,
                "note": "this exact guide message already has a durable delivery record"}
    payload = {k: source.get(k) for k in
               ("source_at", "step", "target", "kind", "urgency", "text", "operator_words")}
    state = "disconnected" if backend.get("state") == "disconnected" else "queued"
    transition(eid, backend["id"], state, backend.get("state_detail", ""),
               backend=backend, payload=payload, consumer=consumer)
    if state == "disconnected":
        return {"ok": False, "consumer": consumer, "event_id": eid, "status": state,
                "backend": backend,
                "error": backend.get("state_detail") or "selected guide agent is disconnected"}
    if start_worker:
        subprocess.Popen([sys.executable, str(WORKER), eid], cwd=str(HERE.parent),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True, "consumer": consumer, "event_id": eid, "status": "queued",
            "backend": backend, "voice_turn": backend.get("voice_turn"),
            "note": "queued to the selected guide agent; no response has been claimed"}


def probe(backend_id: str = "") -> dict:
    inv = inventory()
    rows = [r for g in inv["groups"].values() for r in g]
    hit = next((r for r in rows if r.get("id") == (backend_id or inv.get("selected_id"))), None)
    if not hit:
        return {"ok": False, "state": "disconnected", "error": "no such guide backend"}
    if hit.get("runtime") == "codex-app-server":
        cmd = [sys.executable, str(WORKER), "--probe-codex", hit.get("thread_id", "")]
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=25)
        try:
            result = json.loads(cp.stdout.strip().splitlines()[-1])
        except Exception:
            result = {"ok": False, "state": "failed", "error": (cp.stderr or cp.stdout)[-600:]}
        result["backend"] = hit
        return result
    if hit.get("runtime") == "dsh-acp":
        # ★ Keyless ACP handshake: it proves the binary, the launcher and the protocol, and it
        #   deliberately does NOT exercise the credential or send a prompt. The route receipt is
        #   reported beside it so "the protocol answers" is never read as "the account works".
        cmd = [sys.executable, str(WORKER), "--probe-dsh"]
        cp = None
        try:
            cp = subprocess.run(cmd, text=True, capture_output=True, timeout=90)
            result = json.loads(cp.stdout.strip().splitlines()[-1])
        except Exception as exc:
            result = {"ok": False, "state": "failed",
                      "error": f"{exc.__class__.__name__}: {(cp.stderr if cp else '')[-400:]}"}
        result["backend"] = hit
        return result
    state, detail = _runtime_state(hit)
    return {"ok": state != "disconnected", "state": state, "detail": detail, "backend": hit,
            "note": "route probe only; it did not send a guide message"}


def self_test(prove_fail: bool = False, prove_fail_voice: bool = False,
              prove_fail_linkonly: bool = False) -> int:
    global REGISTRY, STATE, EVENTS, FOCUS
    root = Path(tempfile.mkdtemp(prefix="guide-backends-test-"))
    REGISTRY, STATE, EVENTS, FOCUS = (root / "registry.json", root / "state.json",
                                      root / "events.jsonl", root / "focus.json")
    FOCUS.write_text('{"steps":[{"target":"row-1"}],"i":0,"done":false}\n')
    register({"id": "codex:test", "label": "Codex test", "runtime": "codex-app-server",
              "thread_id": "thread-test", "group": "Codex"})
    # ★ THE HARNESS RUNTIME IS PART OF THE ASSERTED CONTRACT, not a row added later by hand: a
    #   backend whose runtime the worker cannot dispatch is the dead button this registry exists to
    #   prevent, so it is registered here and the exact set below fails if it goes missing.
    register({"id": "dsh:test", "label": "DeepSeek Harness test", "runtime": "dsh-acp",
              "group": "DeepSeek Harness", "delivery": "live", "launcher": str(DSH_LAUNCHER),
              "workspace": DSH_WORKSPACE})
    # Freeze external discovery: this suite asserts the exact registered contract, not the live box.
    globals()["_dispatch_rows"] = lambda: []
    globals()["_dsh_chat_rows"] = lambda: []
    failures = []
    expected = {"codex:test", "dsh:test"}
    actual = {r["id"] for g in inventory()["groups"].values() for r in g}
    if prove_fail:
        expected.add("deliberately-missing-negative-control")
    if actual != expected:
        failures.append(f"backend SET differs: expected {sorted(expected)}, got {sorted(actual)}")
    before = FOCUS.read_bytes()
    picked = select("codex:test")
    if not picked.get("walk_preserved") or FOCUS.read_bytes() != before:
        failures.append("selecting a guide agent changed the active walk")
    if select("not-registered").get("ok"):
        failures.append("unknown backend was accepted")
    # ★★★ ONE FILE, TWO CONSUMERS, TWO KEYS — asserted by SET EQUALITY on the persisted keys, not by
    #     "the voice key exists". The failure this guards is the one that costs the operator a choice:
    #     a second consumer writing a fresh dict and silently dropping the first consumer's pointer.
    if select("dsh:test", consumer="voice").get("ok") is not True:
        failures.append("a voice backing could not be selected")
    if prove_fail_voice:
        # ★★ THE NEGATIVE CONTROL FOR THE TWO-KEY RULE, AND IT REPRODUCES THE REAL DEFECT: the shipped
        #    select() (before this change) wrote a FRESH dict — one consumer's write, the sibling's
        #    choice gone.  Injected here, after the voice pick and before the assertions, so this run
        #    goes red for the right reason.  A guard never tested against its own bypass is a hope.
        _write_json(STATE, {"version": 1, "selected": "codex:test"})
    persisted = _read_json(STATE, {}) or {}
    if set(persisted) < {"selected", "voice_selected"}:
        failures.append(f"the two consumers did not leave two independent keys: {sorted(persisted)}")
    if persisted.get("selected") != "codex:test" or persisted.get("voice_selected") != "dsh:test":
        failures.append(f"a selection clobbered its sibling: {persisted}")
    if inventory(consumer="guide")["selected_id"] != "codex:test":
        failures.append("inventory(guide) read the wrong key")
    if inventory(consumer="voice")["selected_id"] != "dsh:test":
        failures.append("inventory(voice) read the wrong key")
    for bad_consumer in ("", "board", None):
        try:
            _state_key(bad_consumer)
            failures.append(f"an undeclared consumer {bad_consumer!r} was accepted")
        except ValueError:
            pass
    # ★★ THE VOICE CAPABILITY IS DERIVED FROM THE RUNTIME, AND THE WHOLE TABLE IS ASSERTED. A row
    #    that can only acknowledge must be OFFERED and MARKED, never dropped — so the map is checked
    #    as a set of exact answers, including the per-delivery dispatch branch.
    want_turns = {
        ("codex-app-server", "live"): "answer",
        ("dsh-acp", "live"): "answer",
        ("cc-guide-watcher", "live"): "queue-only",
        ("operator-dispatch", "live"): "live-send",
        ("operator-dispatch", "queued"): "queue-only",
        ("operator-dispatch", "manual"): "queue-only",
    }
    for (runtime, delivery), want in want_turns.items():
        got = _voice_turn({"runtime": runtime, "delivery": delivery})
        if got != want:
            failures.append(f"voice_turn({runtime}/{delivery}) = {got!r}, want {want!r}")
    if _voice_turn({"runtime": "codex-app-server", "delivery": "queued"}) != "answer":
        failures.append("the capability was taken from `delivery` instead of from the runtime")
    if _voice_turn({"runtime": "invented-runtime"}) not in VOICE_TURNS:
        failures.append("an unknown runtime produced a capability outside the closed set")
    # ★ A LINK-ONLY CHAT IS NOT AN ANSWER ROUTE. The live BoardWalk chat has runtime `dsh-acp` but
    #   capabilities ["link-only"] and a held session lock; `notify()` refuses it. The voice seat's
    #   backing list advertised `answer` for exactly that row. The synthetic row below is the shape
    #   `_dsh_chat_rows()` builds, asserted directly so this guard does not depend on the live box.
    _link_row = {"runtime": "dsh-acp", "delivery": "live", "capabilities": ["link-only"],
                 "_chat": {"live": True, "resumable": False}}
    _orig_vt = _voice_turn
    if prove_fail_linkonly:
        globals()["_voice_turn"] = lambda row: ("answer" if row.get("capabilities") == ["link-only"]
                                                else _orig_vt(row))
    try:
        if _voice_turn(_link_row) != "queue-only":
            failures.append("a link-only DSH chat advertised %r as a voice-turn capability; it can "
                            "only open a window, and notify() refuses it"
                            % (_voice_turn(_link_row),))
        if "link-only" not in _voice_turn_cost(_link_row):
            failures.append("a link-only DSH chat did not state that no ACP prompt is sent")
    finally:
        globals()["_voice_turn"] = _orig_vt
    # ★ the queue-only row is REGISTERED and therefore offered; and an id that goes away falls back
    #   rather than raising — the existing rule, now asserted on the voice key too.
    register({"id": "cc:test", "label": "CC test", "runtime": "cc-guide-watcher", "group": "Claude Code"})
    expected.add("cc:test")
    rows_now = {r["id"]: r for g in inventory(consumer="voice")["groups"].values() for r in g}
    if set(rows_now) != expected:
        failures.append(f"the voice list is not the registered set: {sorted(rows_now)} != "
                        f"{sorted(expected)}")
    if rows_now.get("cc:test", {}).get("voice_turn") != "queue-only":
        failures.append("the queue-only row was dropped from the voice list instead of marked")
    _write_json(STATE, {**persisted, "voice_selected": "a-backend-that-was-retired"})
    if inventory(consumer="voice")["selected_id"] not in rows_now:
        failures.append("a retired voice selection did not fall back to a registered backend")
    if not (rows_now.get("cc:test") or {}).get("voice_turn_cost"):
        failures.append("a voice row does not state where its spend lands")
    src = {"source_at": "2026-01-01T00:00:00+0000", "step": 2, "target": "row-1",
           "kind": "question", "urgency": "none", "text": "why?", "operator_words": True}
    one = notify(src, start_worker=False)
    two = notify(src, start_worker=False)
    if not two.get("deduplicated") or len({r["event_id"] for r in _event_rows()}) != 1:
        failures.append("duplicate message produced more than one durable directive id")
    try:
        transition(one["event_id"], "codex:test", "invented-state")
        failures.append("an undeclared delivery state was accepted")
    except ValueError:
        pass
    # ★ THE HARNESS ADDRESS MUST SURVIVE ITS OWN REGISTRATION.  Two ways to lose it silently:
    #   a re-register that only changes wording, and a bind that writes a second store.
    if not bind("dsh:test", "session-probe-1").get("ok"):
        failures.append("a harness session id could not be bound to its own registry row")
    if bind("not-registered", "x").get("ok"):
        failures.append("a session id was bound to a backend that does not exist")
    register({"id": "dsh:test", "label": "DeepSeek Harness test", "runtime": "dsh-acp",
              "group": "DeepSeek Harness"})
    kept = next((r for r in _read_json(REGISTRY, {}).get("backends", []) if r["id"] == "dsh:test"), {})
    if not kept.get("launcher") or kept.get("session_id") != "session-probe-1":
        failures.append("re-registering a harness row dropped its launcher or its live session id")
    # ★ POSITION, NOT MEMBERSHIP.  The working Node must be FIRST on the child's PATH even when its
    #   directory already appears LATER in this process's PATH — the panel service's PATH ends in
    #   /usr/bin, and the harness kept dying on a Node that cannot run it while the fix-up reported
    #   that it had applied itself.  Tested by constructing exactly that shape.
    node_dir, _node_why = _dsh_node()
    if node_dir:
        saved = os.environ.get("PATH", "")
        os.environ["PATH"] = saved + os.pathsep + node_dir
        try:
            if _dsh_env()["PATH"].split(os.pathsep)[0] != node_dir:
                failures.append("the harness child PATH did not put the working node first when it "
                                "was already present further down this process's PATH")
        finally:
            os.environ["PATH"] = saved
    if prove_fail and not failures:
        failures.append("PROVE-FAIL DID NOT GO RED")
    for failure in failures:
        print("  CONTROL FAILED:", failure)
    if not failures:
        print("  PASS: exact backend set; unknown target refused; selection preserves focus bytes; "
              "directive ids deduplicate; state vocabulary is closed; a harness route survives "
              "re-registration and its session id lives only in the registry row; the voice seat's "
              "backing is a SECOND key in the same file that cannot clobber the guide's, the voice "
              "capability is derived from the runtime for the whole table, and a queue-only row is "
              "offered and marked rather than dropped")
        print(f"  PROVE-FAIL: {Path(__file__).name} --self-test --prove-fail (must exit 1)")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--register-current-codex", action="store_true")
    ap.add_argument("--register-dsh", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--prove-fail", action="store_true")
    ap.add_argument("--prove-fail-voice", action="store_true")
    ap.add_argument("--prove-fail-linkonly", action="store_true")
    ns = ap.parse_args()
    if ns.self_test:
        return self_test(ns.prove_fail, ns.prove_fail_voice, ns.prove_fail_linkonly)
    if ns.register_dsh:
        # ★ THE HARNESS IS ADDRESSED THROUGH THE LAUNCHER IT SHIPPED WITH — the same entry point the
        #   AionUi pairing uses — so the guide board and the team share one harness, one key handling
        #   and one refusal path, rather than a second way in invented here.
        result = register({
            "id": "dsh:harness",
            "label": "DeepSeek Harness — deepseek-flash (ACP)",
            "group": "DeepSeek Harness",
            "runtime": "dsh-acp",
            "lane": "dev",
            "delivery": "live",
            "launcher": str(DSH_LAUNCHER),
            "workspace": DSH_WORKSPACE,
            "how": "the board's worker drives the installed harness over its own ACP stdio "
                   "(launch.py): initialize, resume or open a session, one prompt, and the model's "
                   "answer is written back to the event. The key is read on-box by launch.py and is "
                   "never touched by the board.",
            "capabilities": ["session-new", "session-resume", "prompt", "answer-back"],
        })
        if result.get("ok"):
            result["route"] = _dsh_route()[1]
            result["launcher"] = str(DSH_LAUNCHER)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 2
    if ns.register_current_codex:
        tid, sid = os.environ.get("CODEX_THREAD_ID", ""), os.environ.get("CODEX_SESSION_ID", "")
        if not tid:
            print(json.dumps({"ok": False, "error": "CODEX_THREAD_ID is absent"}))
            return 2
        rollouts = list((Path.home() / ".codex/sessions").glob(f"**/*{tid}*.jsonl"))
        result = register({"id": "codex:boardwalk", "label": "Codex — Board Walk (this chat)",
                           "group": "Codex", "runtime": "codex-app-server", "thread_id": tid,
                           "session_id": sid, "lane": "boardwalk", "delivery": "live",
                           "rollout_path": str(rollouts[-1]) if rollouts else "",
                           "how": "resumes this exact Codex thread through the official App Server; "
                                  "the current model and reasoning level are preserved",
                           "capabilities": ["thread-read", "thread-resume", "turn-start", "answer-back"]})
        # register() intentionally keeps a closed portable shape; add rollout only after validation.
        if result.get("ok") and rollouts:
            store = _read_json(REGISTRY, {"backends": []})
            for row in store["backends"]:
                if row["id"] == "codex:boardwalk": row["rollout_path"] = str(rollouts[-1])
            _write_json(REGISTRY, store)
        print(json.dumps(result))
        return 0 if result.get("ok") else 2
    print(json.dumps(inventory(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
