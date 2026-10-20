#!/usr/bin/env python3
"""Deliver one durable guide event.  One backend lock prevents duplicate execution."""
from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import guide_backends as gb


def _codex_binary() -> str:
    """Use the installed app's own binary.  The panel service PATH does not contain `codex`."""
    configured = os.environ.get("KHAOSS_CODEX_BIN", "")
    if configured and Path(configured).is_file():
        return configured
    # Prefer the binary backing the currently running Codex app-server: it already owns this thread.
    for proc in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv = proc.read_bytes().split(b"\0")
            exe = argv[0].decode(errors="replace") if argv else ""
            cmd = b" ".join(argv)
        except OSError:
            continue
        if b"openai.chatgpt-" in cmd and exe.endswith("/codex") and Path(exe).is_file():
            return exe
    candidates = list((Path.home() / ".vscode/extensions").glob(
        "openai.chatgpt-*/bin/linux-x86_64/codex"))
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise FileNotFoundError("Codex executable not found in PATH or the installed VS Code extension")


class AppServer:
    def __init__(self):
        self.p = subprocess.Popen([_codex_binary(), "app-server", "--stdio"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                  bufsize=1)
        self.next_id = 1

    def send(self, method, params=None, request=True):
        row = {"method": method, "params": params or {}}
        if request:
            row["id"] = self.next_id
            self.next_id += 1
        self.p.stdin.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.p.stdin.flush()
        return row.get("id")

    def read_until(self, wanted_id=None, methods=(), timeout=20):
        end = time.monotonic() + timeout
        seen = []
        while time.monotonic() < end:
            ready, _, _ = select.select([self.p.stdout], [], [], min(.5, end-time.monotonic()))
            if not ready:
                if self.p.poll() is not None:
                    raise RuntimeError(f"Codex App Server exited with code {self.p.returncode}")
                continue
            line = self.p.stdout.readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            seen.append(msg)
            if wanted_id is not None and msg.get("id") == wanted_id:
                return msg, seen
            if msg.get("method") in methods:
                return msg, seen
        raise TimeoutError(f"Codex App Server did not answer within {timeout}s")

    def initialize(self):
        rid = self.send("initialize", {"clientInfo": {"name": "khaoss-operator-board",
                                                        "title": "KHAOSS Operator Board",
                                                        "version": "1.0"}})
        msg, _ = self.read_until(wanted_id=rid)
        if msg.get("error"):
            raise RuntimeError(str(msg["error"]))
        self.send("initialized", {}, request=False)

    def close(self):
        try: self.p.terminate()
        except Exception: pass


def probe_codex(thread_id: str) -> dict:
    if not thread_id:
        return {"ok": False, "state": "disconnected", "error": "no thread id registered"}
    app = AppServer()
    try:
        app.initialize()
        rid = app.send("thread/read", {"threadId": thread_id, "includeTurns": False})
        msg, _ = app.read_until(wanted_id=rid, timeout=20)
        if msg.get("error"):
            return {"ok": False, "state": "disconnected", "error": str(msg["error"])[:700]}
        thread = (msg.get("result") or {}).get("thread") or msg.get("result") or {}
        return {"ok": True, "state": "ready", "thread_id": thread.get("id", thread_id),
                "thread_status": thread.get("status"), "session_id": thread.get("sessionId"),
                "note": "thread/read succeeded; no prompt was sent and no model setting was changed"}
    except Exception as exc:
        return {"ok": False, "state": "failed", "error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        app.close()


# ── DEEPSEEK HARNESS (ACP) ───────────────────────────────────────────────────────────────────────
# ★★ THE HARNESS IS DRIVEN THROUGH ITS OWN PROTOCOL, exactly as Codex is driven through its App
#    Server.  `launch.py` is the installed launcher: it reads the key ON THIS BOX, injects it into
#    the child environment, and refuses a missing/stale/other-key receipt before any provider
#    request.  This worker therefore never reads, copies, hashes or logs credential material — it
#    only ever sees the child's protocol stream.
# ★ `session/resume` is used when a session id is already bound, so consecutive guide messages are
#   one continuing conversation rather than a new agent per message.  Resume is refused for an
#   already-active or workspace-mismatched session; that refusal falls back to `session/new`, and
#   the new id is bound back into the registry row.


class AcpServer:
    """Line-delimited JSON-RPC 2.0 over stdio — the Agent Client Protocol v1 surface."""

    def __init__(self, launcher: str, workspace: str, mode: str = "--acp", timeout: int = 60):
        self.timeout = timeout
        self.notes = []
        self.stderr = []
        self.next_id = 0
        # ★ THE CHILD INHERITS THIS PROCESS'S ENVIRONMENT WITH THE WORKING NODE FIRST.  The launcher's
        #   `#!/usr/bin/env node` resolves whatever `node` is first on PATH, and the panel service's
        #   PATH resolves one under which the harness exits silently. See gb._dsh_node.
        self.p = subprocess.Popen([launcher, mode], cwd=workspace, text=True, bufsize=1,
                                  env=gb._dsh_env(),
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)

    def _read(self, deadline: float) -> dict:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("the harness did not answer in time")
            ready, _, _ = select.select([self.p.stdout, self.p.stderr], [], [], min(.25, remaining))
            if not ready:
                if self.p.poll() is not None:
                    tail = self.stderr[-1] if self.stderr else ""
                    raise RuntimeError(f"the harness exited with code {self.p.returncode}"
                                       + (f": {tail[:300]}" if tail else ""))
                continue
            if self.p.stderr in ready:
                line = self.p.stderr.readline()
                if line:
                    # Bounded on purpose: this is a diagnostic tail, never a transcript to keep.
                    self.stderr.append(line.strip()[:400])
                    del self.stderr[:-8]
                    continue
            if self.p.stdout in ready:
                line = self.p.stdout.readline()
                if not line:
                    raise RuntimeError("the harness closed its ACP stream")
                try:
                    return json.loads(line)
                except ValueError:
                    continue

    def _note(self, msg: dict) -> None:
        if msg.get("method") == "session/update":
            self.notes.append(msg.get("params") or {})

    def call(self, method: str, params: dict, timeout: int | None = None) -> dict:
        self.next_id += 1
        mid = self.next_id
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                       "params": params}) + "\n")
        self.p.stdin.flush()
        limit = timeout or self.timeout
        deadline = time.monotonic() + limit
        while True:
            msg = self._read(deadline)
            if msg.get("id") == mid:
                if msg.get("error"):
                    raise RuntimeError(f"{method}: {str(msg['error'])[:400]}")
                return msg.get("result") or {}
            self._note(msg)

    def answer(self) -> str:
        """★ The model's words, and only those.  A transport acknowledgement is not an answer, so
        this reads committed `agent_message_chunk` updates rather than the call's return value."""
        chunks = []
        for note in self.notes:
            update = note.get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                chunks.append(str((update.get("content") or {}).get("text") or ""))
        return "".join(chunks).strip()

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.terminate()
        except Exception:
            pass


def _dsh_handshake(app: AcpServer) -> dict:
    return app.call("initialize", {"protocolVersion": 1,
                                   "clientInfo": {"name": "khaoss-operator-board", "version": "1"},
                                   "clientCapabilities": {}})


def probe_dsh() -> dict:
    """★ A ROUTE PROBE, NOT A PROMPT.  The harness is started in its own KEYLESS mode, so the
    credential is never exercised and no model request is made.  It proves the launcher, the binary
    and the protocol — and reports the route receipt separately, so "ACP answers" can never be read
    as "the account works"."""
    route_ok, route_detail = gb._dsh_route()
    out = {"ok": False, "state": "disconnected", "route_ok": route_ok, "route": route_detail,
           "inference_tested": False}
    app = None
    try:
        app = AcpServer(str(gb.DSH_LAUNCHER), str(gb.DSH_WORKSPACE), mode="--probe", timeout=45)
        init = _dsh_handshake(app)
        session = app.call("session/new", {"cwd": str(gb.DSH_WORKSPACE), "mcpServers": []})
        sid = str(session.get("sessionId") or "")
        out.update({"protocol_ok": True, "protocol_version": init.get("protocolVersion"),
                    "session_created": bool(sid), "model_options":
                    [o.get("currentValue") for o in (session.get("configOptions") or [])
                     if o.get("id") == "model"]})
        if sid:
            try:
                app.call("session/close", {"sessionId": sid}, timeout=30)
            except Exception:
                pass
        out["ok"] = bool(route_ok and sid)
        out["state"] = "ready" if out["ok"] else ("disconnected" if not route_ok else "failed")
        out["note"] = ("the harness answered ACP and created a keyless session; " + route_detail
                       + " No prompt was sent, so the credential was NOT exercised.")
        if not route_ok:
            out["error"] = route_detail
    except Exception as exc:
        out["state"] = "failed"
        out["error"] = f"{exc.__class__.__name__}: {exc}"
        # ★ THE DIAGNOSTIC TAIL IS THE WHOLE VALUE OF A FAILED PROBE.  Discarding the child's stderr
        #   made "the harness closed its ACP stream" an unactionable sentence; it is bounded and
        #   launch.py never writes credential material to it.
        if app is not None and app.stderr:
            out["harness_stderr"] = app.stderr[-3:]
    finally:
        if app is not None:
            app.close()
    return out


def dsh_prompt(ev: dict, backend: dict) -> tuple[str, str, dict]:
    """Deliver one guide event into the harness and return its answer.  Returns (status, detail, extra)."""
    launcher = str(backend.get("launcher") or gb.DSH_LAUNCHER)
    workspace = str(backend.get("workspace") or gb.DSH_WORKSPACE)
    payload = ev.get("payload") or {}
    app = AcpServer(launcher, workspace, timeout=60)
    try:
        _dsh_handshake(app)
        sid = str(backend.get("session_id") or "")
        note = ""
        if sid:
            try:
                app.call("session/resume", {"sessionId": sid, "cwd": workspace, "mcpServers": []})
                note = f"resumed harness session {sid[:8]}…"
            except Exception as resume_failure:
                # ★ NOT SILENT.  A refused resume is reported in the event: the operator should be
                #   able to see that this message opened a fresh conversation, and why.
                note = (f"the bound session {sid[:8]}… was not resumable "
                        f"({str(resume_failure)[:140]}) — a new one was opened")
                sid = ""
        if not sid:
            sid = str(app.call("session/new", {"cwd": workspace, "mcpServers": []})
                      .get("sessionId") or "")
            if not sid:
                return "failed", "the harness created no session id", {}
            note = note or "opened a new harness session"
            # ★ BIND IMMEDIATELY, before the prompt.  A prompt that fails must not lose the session
            #   the operator is now in — the address is the durable fact, the turn is not.
            gb.bind(backend["id"], sid)
        gb.transition(ev["event_id"], backend["id"], "running",
                      f"prompt sent to DeepSeek Harness session {sid[:8]}… — {note}",
                      harness_session=sid, harness_note=note)
        prompt = _voice_prompt(ev) if _is_voice(ev) else (
            "OPERATOR BOARD GUIDE PING. This is a durable board message, not hidden system text.\n"
            f"Directive id: {ev.get('event_id')}\nKind: {payload.get('kind')}\n"
            f"Urgency: {payload.get('urgency')}\n"
            f"Board target: {payload.get('target') or '(guide step)'}\n"
            f"Operator's exact words: {payload.get('text')}\n\n"
            "Answer in this session. Do not close, mark, or edit an operator board item on the "
            "operator's behalf. If this is a question, finish with a concise answer suitable for "
            "display beside the current row."
        )
        result = app.call("session/prompt",
                          {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                          timeout=gb.DSH_PROMPT_TIMEOUT)
        answer = app.answer()
        stop = str(result.get("stopReason") or "")
        # ★★ CLOSE THE SESSION, DO NOT JUST KILL THE PROCESS.  `session/close` is the quiescent
        #    teardown that DRAINS UPDATES AND FLUSHES PERSISTENCE.  Terminating straight after the
        #    prompt left the harness's own session log truncated at the turn — the user message was
        #    there and the assistant message was NOT, so the one artifact that proves arrival by
        #    someone who does not trust this process was missing its most important row.  Measured,
        #    not assumed: the first delivered event produced exactly that truncated log.
        close_note = ""
        try:
            app.call("session/close", {"sessionId": sid}, timeout=60)
        except Exception as close_failure:
            close_note = (f"the harness session did not close cleanly "
                          f"({str(close_failure)[:140]}) — its own log may be truncated")
        if payload.get("kind") == "question" and answer and payload.get("target"):
            import guide_focus
            guide_focus.answer(str(payload["target"]), answer,
                               by=str(backend.get("label") or backend["id"]))
        extra = {"harness_session": sid, "harness_stop": stop}
        if close_note:
            extra["harness_close"] = close_note
        log = gb._dsh_session_log(workspace, sid)
        if log:
            extra["harness_log"] = log
        if stop and stop not in ("end_turn", "max_tokens"):
            return "failed", f"the harness turn ended with stopReason {stop!r}", extra
        if not answer:
            return ("failed",
                    f"the harness turn ended ({stop or 'no stopReason'}) with no assistant text — "
                    f"a transport acknowledgement is not an answer", extra)
        return "completed", answer[-900:], extra
    except Exception as exc:
        return "failed", f"{exc.__class__.__name__}: {exc}", {}
    finally:
        app.close()


# ── THE VOICE SEAT'S TURN ─────────────────────────────────────────────────────────────────────────
# ★★★ A SPOKEN TURN IS THE SAME MACHINERY WITH A DIFFERENT PROMPT AND A SYNCHRONOUS ANSWER.  The board
#     path writes its answer into an event and moves on; the voice seat has to SPEAK it, so it cannot
#     use the fire-and-forget worker.  Everything that can be shared is shared: the resume, the turn,
#     the delta collection, the twenty-minute cap and the per-backend lock are all the board's, because
#     a second implementation of "wait for the agent's words" is a second thing to keep correct.
VOICE_KIND = "voice-turn"


def _is_voice(ev: dict) -> bool:
    return str((ev.get("payload") or {}).get("kind") or "") == VOICE_KIND


def _voice_prompt(ev: dict) -> str:
    """★ HIS WORDS, UNREWRITTEN.  `payload.text` already carries router.preamble() — the provenance
    block that says these ARE his words AND that they are an ASR transcript which may have misheard a
    name, a path or a number.  The worker prepends nothing and rewrites nothing; it only appends the
    one instruction this path needs and the board path does not: it will be read aloud."""
    text = str((ev.get("payload") or {}).get("text") or "")
    return (text + "\n\n"
            "(Spoken through the operator's voice seat. Answer in this session. Your reply is read "
            "back to him aloud as-is, so keep it short, plain and free of code blocks or tables. Do "
            "not change any operator board item on his behalf.)")


def _codex_run(thread_id, prompt: str, ev: dict | None = None, backend: dict | None = None,
               cap_s: int = 1200) -> tuple[str, str]:
    """Resume the thread, start ONE turn, collect the assistant text until the turn ENDS.

    Returns (status, text) where text is the model's answer on success and the FAILURE DETAIL on
    failure. Extracted from codex_turn unchanged so both the board guide and the voice seat drive the
    same App Server session the same way.

    ★★★ TWO FIXES, BOTH OF THEM MEASURED AGAINST THE LIVE APP SERVER (2026-09-19), AND WITHOUT THEM
        THIS PATH CANNOT ANSWER AT ALL. Captured from `codex app-server --stdio` on this box:
          turn/started · item/started(userMessage) · item/completed(userMessage) ·
          item/started(agentMessage) · item/agentMessage/delta ×N ·
          item/completed(agentMessage, with the FULL text) · thread/tokenUsage/updated ·
          account/rateLimits/updated · thread/status/changed{"type":"idle"}
        1. ★ THERE IS NO `turn/completed` NOTIFICATION. This build never emits it. The shipped loop
           waited for it, so it ran to the delta stream, blocked on the next read, raised TimeoutError
           and returned "failed" — which is why guide_backend_events.jsonl holds ZERO Codex deliveries
           while the Codex row has been registered since 07:53 today. The end of a turn is
           `item/completed` for an agentMessage (which carries the whole answer — better than the
           deltas, which arrive fragmented: "P","ONG"," SE","AT"," TWO") or `thread/status/changed`
           `idle`. Both are honoured, and `turn/completed` is still honoured for builds that send it.
        2. ★ A QUIET MINUTE IS NOT A FAILURE. The deltas for this model arrive ~3 s in, but a
           reasoning-heavy turn can be silent for longer; a read timeout now means "keep waiting"
           until the twenty-minute cap instead of failing a turn that is still working.

    ★ AND A THIRD "FIX" WAS TRIED AND REMOVED, BECAUSE IT WAS MEASURED AND FOUND TO BE WRONG. A
      thread created by `thread/start` has no rollout until its first turn, and `thread/resume`
      refuses it ("no rollout found"); falling through to `turn/start` looked like the obvious cure.
      It is not: from any OTHER app-server process that thread does not exist at all, and `turn/start`
      answers `thread not found`. The two errors are the same fact reported honestly and dishonestly,
      so the refusal is left alone. A freshly started Codex thread is reachable only from the process
      that created it until it has been spoken to once."""
    app = AppServer()
    try:
        app.initialize()
        rid = app.send("thread/resume", {"threadId": thread_id})
        resumed, _ = app.read_until(wanted_id=rid, timeout=30)
        if resumed.get("error"):
            return "failed", "thread/resume refused: " + str(resumed["error"])[:700]
        rid = app.send("turn/start", {"threadId": thread_id,
                                      "input": [{"type": "text", "text": prompt}]})
        started, pending = app.read_until(wanted_id=rid, timeout=30)
        if started.get("error"):
            return "failed", "turn/start refused: " + str(started["error"])[:700]
        if ev and backend:
            gb.transition(ev["event_id"], backend["id"], "running",
                          "Codex voice turn started" if _is_voice(ev) else "Codex turn started")
        chunks: dict = {}
        answer = ""
        turn_seen = False
        deadline = time.monotonic() + cap_s

        def buffered() -> str:
            if answer:
                return answer
            return "".join(part for item in chunks.values() for part in item).strip()

        while time.monotonic() < deadline:
            # A very short turn can finish before the response to `turn/start` reaches us.  Those
            # notifications are in `pending`; discarding them makes a successful turn look hung for
            # twenty minutes.  Drain every message already seen before blocking for another one.
            if not pending:
                try:
                    msg, pending = app.read_until(
                        methods=("item/agentMessage/delta", "item/started", "item/completed",
                                 "turn/completed", "thread/status/changed"), timeout=60)
                except TimeoutError:
                    # ★ fix 2: a quiet minute is not a failure. If the turn has begun and we already
                    #   hold the agent's words, it is over and the terminal signal was one this build
                    #   does not send; otherwise keep waiting, bounded by the cap.
                    if turn_seen:
                        return "completed", buffered()
                    continue
            completed = None
            for msg in pending:
                method = msg.get("method")
                params = msg.get("params") or {}
                if method == "item/agentMessage/delta":
                    # ★ fix 1: keep the deltas, but keyed by item, because a turn can emit more than
                    #   one agent message and only the LAST one is the answer.
                    chunks.setdefault(str(params.get("itemId") or ""), []).append(
                        str(params.get("delta") or ""))
                    turn_seen = True
                elif method == "item/started":
                    turn_seen = True
                elif method in ("turn/started", "thread/status/changed"):
                    if method == "turn/started":
                        turn_seen = True
                    elif (str((params.get("status") or {}).get("type") or "") == "idle"
                          and turn_seen):
                        completed = {"method": "turn/ended", "params": {}}   # this build's real end
                elif method == "item/completed":
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        # ★ the completed item carries the WHOLE message; the deltas are fragments
                        text = str(item.get("text") or "").strip()
                        if not text:
                            text = "".join(chunks.get(str(item.get("id") or ""), [])).strip()
                        if text:
                            answer = text
                    turn_seen = True
                elif method == "turn/completed":
                    completed = msg
            pending = []
            if completed:
                if completed.get("method") == "turn/completed":
                    status = str(((completed.get("params") or {}).get("turn") or {})
                                 .get("status") or "completed")
                    if status not in ("completed", "success"):
                        return "failed", f"Codex turn ended with status {status}"
                return "completed", buffered()
        return "failed", f"Codex turn exceeded the {cap_s // 60}-minute delivery cap"
    except Exception as exc:
        return "failed", f"{exc.__class__.__name__}: {exc}"
    finally:
        app.close()


def codex_turn(ev: dict, backend: dict) -> tuple[str, str]:
    payload = ev.get("payload") or {}
    prompt = _voice_prompt(ev) if _is_voice(ev) else (
        "OPERATOR BOARD GUIDE PING. This is a durable board message, not hidden system text.\n"
        f"Directive id: {ev.get('event_id')}\nKind: {payload.get('kind')}\n"
        f"Urgency: {payload.get('urgency')}\nBoard target: {payload.get('target') or '(guide step)'}\n"
        f"Operator's exact words: {payload.get('text')}\n\n"
        "Acknowledge the message, inspect the live board/focus if needed, and respond in this thread. "
        "Do not close an operator item on the operator's behalf. If this is a question, finish with a concise "
        "answer suitable for display beside the current row."
    )
    status, answer = _codex_run(backend.get("thread_id"), prompt, ev=ev, backend=backend)
    if status != "completed":
        return status, answer
    # ★ THE BOARD PATH'S OWN SIDE EFFECT, UNCHANGED AND DELIBERATELY NOT MOVED INTO _codex_run: a
    #   spoken turn must never write a ruling beside a board row.
    if payload.get("kind") == "question" and answer and payload.get("target"):
        import guide_focus
        guide_focus.answer(str(payload["target"]), answer,
                           by=str(backend.get("label") or backend["id"]))
    return "completed", (answer[-900:] if answer else "Codex turn completed")


def codex_voice_turn(ev: dict, backend: dict) -> tuple[str, str]:
    """One spoken turn through Codex, synchronously, returning the model's words."""
    status, answer = _codex_run(backend.get("thread_id"), _voice_prompt(ev), ev=ev, backend=backend)
    if status != "completed":
        return status, answer
    if not answer:
        # ★ NO WORDS IS NOT AN ANSWER. A completed turn with no assistant text is a transport fact;
        #   speaking it (or worse, speaking "Codex turn completed") would be the exact lie the guide
        #   registry refuses on its own path.
        return "failed", ("the Codex turn completed with no assistant text — a transport "
                          "acknowledgement is not an answer")
    return "completed", answer


def deliver_voice(event_id: str) -> dict:
    """★★★ THE VOICE SEAT'S SYNCHRONOUS TURN — one call in, the agent's words out.

    The board's worker is fire-and-forget because the board can be told later by re-reading its event
    trail. A voice seat cannot: the operator is standing there waiting to HEAR it. So this mode runs
    the same delivery under the same per-backend lock and then RETURNS the result instead of only
    recording it — `{"ok", "status", "reply"}` is the whole contract.

    ★ Rows that can only acknowledge go through the EXISTING deliver(): their status vocabulary is
      already the honest one and `reply` is None. The seat says "Sent to X." rather than inventing an
      answer, which is what keeps `queued` from ever being painted as one."""
    ev = gb.event(event_id)
    if not ev:
        return {"ok": False, "status": "failed", "reply": None, "event_id": event_id,
                "error": "no such guide event — nothing was delivered"}
    backend = ev.get("backend") or {}
    backend_id = str(backend.get("id") or "")
    runtime = str(backend.get("runtime") or "")
    voice_turn = gb._voice_turn(backend)
    lock_path = HERE / ("guide_backend_" + backend_id.replace("/", "_").replace(":", "_") + ".lock")
    with lock_path.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        fresh = gb.event(event_id) or ev
        if fresh.get("status") in ("completed", "failed", "disconnected", "acknowledged", "running"):
            done = str(fresh.get("status"))
            return {"ok": done in ("completed", "acknowledged"), "status": done, "reply": None,
                    "event_id": event_id, "backend_id": backend_id, "voice_turn": voice_turn,
                    "deduplicated": True, "detail": str(fresh.get("detail") or "")[:1200]}
        if runtime == "codex-app-server":
            status, text = codex_voice_turn(fresh, backend)
            reply = text if status == "completed" else None
            gb.transition(event_id, backend_id, status, (reply or text or "")[:1200],
                          voice_turn=voice_turn, reply=reply)
            return {"ok": status == "completed", "status": status, "reply": reply,
                    "event_id": event_id, "backend_id": backend_id, "voice_turn": voice_turn,
                    "detail": (reply or text or "")[:400]}
        if runtime == "dsh-acp":
            # ★ the harness path is the board's own, reused whole: resume-or-new session, one prompt,
            #   assistant chunks, `session/close` so the harness's own log is flushed and not truncated.
            status, detail, extra = dsh_prompt(fresh, backend)
            reply = detail if status == "completed" else None
            gb.transition(event_id, backend_id, status, str(detail)[:1200], **extra,
                          voice_turn=voice_turn, reply=reply)
            return {"ok": status == "completed", "status": status, "reply": reply,
                    "event_id": event_id, "backend_id": backend_id, "voice_turn": voice_turn,
                    "detail": str(detail)[:400]}
        # QUEUE-ONLY / LIVE-SEND — reuse the existing delivery, claim no answer.
        rc = deliver(event_id)
        final = gb.event(event_id) or {}
        status = str(final.get("status") or "failed")
        return {"ok": rc == 0, "status": status, "reply": None, "event_id": event_id,
                "backend_id": backend_id, "voice_turn": voice_turn,
                "detail": str(final.get("detail") or "")[:400],
                "note": "this route can acknowledge a turn; it produces no model answer to speak"}


def deliver(event_id: str) -> int:
    ev = gb.event(event_id)
    if not ev:
        return 2
    backend = ev.get("backend") or {}
    lock_path = HERE / ("guide_backend_" + str(backend.get("id") or "unknown").replace("/", "_")
                        .replace(":", "_") + ".lock")
    with lock_path.open("a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        fresh = gb.event(event_id) or ev
        if fresh.get("status") in ("completed", "failed", "disconnected", "acknowledged", "running"):
            return 0
        runtime = backend.get("runtime")
        if runtime == "codex-app-server":
            status, detail = codex_turn(fresh, backend)
            gb.transition(event_id, backend["id"], status, detail)
            return 0 if status == "completed" else 1
        if runtime == "dsh-acp":
            # ★ The status written here is the MEASURED one: `completed` only after the harness
            #   emitted assistant text.  A turn that ends with no words is `failed`, because a
            #   transport acknowledgement is not an answer and must never be painted as one.
            status, detail, extra = dsh_prompt(fresh, backend)
            gb.transition(event_id, backend["id"], status, detail, **extra)
            return 0 if status == "completed" else 1
        if runtime == "cc-guide-watcher":
            # The operator's words live once in guide_messages.jsonl.  This is a transport envelope
            # for the selected legacy watcher, not a second canonical message.  The guide reader
            # explicitly ignores `_transport_only`, while the already-running watcher prints it.
            state, detail = gb._runtime_state(backend)
            if state == "watching":
                payload = fresh.get("payload") or {}
                gb._append(HERE / "guide_replies.jsonl", {
                    "at": gb._now(), "kind": payload.get("kind"),
                    "urgency": payload.get("urgency"), "target": payload.get("target"),
                    "text": payload.get("text"), "event_id": event_id,
                    "_transport_only": True,
                })
                status = "acknowledged"
            else:
                status = "failed"
            gb.transition(event_id, backend["id"], status, detail)
            return 0 if status == "acknowledged" else 1
        if runtime == "operator-dispatch":
            payload = fresh.get("payload") or {}
            if backend.get("delivery") == "live":
                import urllib.request
                body = json.dumps({"target": backend.get("target"),
                                   "kind": payload.get("kind"), "item": payload.get("target"),
                                   "text": payload.get("text"),
                                   "from_operator": bool(payload.get("operator_words"))}).encode()
                req = urllib.request.Request("http://127.0.0.1:7842/operator/dispatch/send", body,
                                             {"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=15) as response:
                        result = json.loads(response.read())
                    if not result.get("ok", True):
                        gb.transition(event_id, backend["id"], "failed",
                                      result.get("error") or result.get("note") or "live send refused")
                        return 1
                    gb.transition(event_id, backend["id"], "acknowledged",
                                  result.get("note") or "destination confirmed arrival; response not established")
                    return 0
                except Exception as exc:
                    gb.transition(event_id, backend["id"], "failed",
                                  f"live dispatch failed: {exc.__class__.__name__}: {exc}")
                    return 1
            import dispatch_targets
            result = dispatch_targets.enqueue({"target": backend.get("target"),
                                               "target_label": backend.get("label"),
                                               "delivery": backend.get("delivery"),
                                               "kind": payload.get("kind"),
                                               "item": payload.get("target"),
                                               "text": payload.get("text"),
                                               "from": "operator board guide"})
            gb.transition(event_id, backend["id"], "queued", result.get("note", "queued"))
            return 0
        gb.transition(event_id, backend.get("id", ""), "failed", f"unsupported runtime {runtime!r}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--probe-codex":
        print(json.dumps(probe_codex(sys.argv[2])))
        raise SystemExit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-dsh":
        print(json.dumps(probe_dsh()))
        raise SystemExit(0)
    if len(sys.argv) >= 3 and sys.argv[1] == "--voice-turn":
        # ★ ONE JSON LINE ON STDOUT IS THE CONTRACT: the seat parses the last line and speaks `reply`.
        #   The exit code repeats the verdict for a human; the JSON is what the caller trusts.
        verdict = deliver_voice(sys.argv[2])
        print(json.dumps(verdict))
        raise SystemExit(0 if verdict.get("ok") else 1)
    raise SystemExit(deliver(sys.argv[1]) if len(sys.argv) > 1 else 2)
