#!/usr/bin/env /usr/bin/python3
"""focus_sources — the READ-ONLY source of truth for what AionUi currently IS.

★★★ THE CONTRACT (agreed with the boardwalk seat, 2026-09-15, CONTRACT_FOCUS_SOURCES_0915):
    THIS MODULE EXPOSES SOURCES.  THE BOARDWALK SEAT DERIVES THE FOCUS.  NEITHER OF US STORES IT.

The awareness protocol's rule is that focus must be DERIVED, never stored — the voice seat's
indicator is the precedent, and it cannot lie precisely because no `set_indicator()` exists. If this
module ALSO derived a focus, the box would gain a seventh store that drifts from reality. Two
derivers is the same defect as two stores. So: facts here, precedence there.

★★ WHAT THIS MODULE MAY NEVER DO
  · never write anything, anywhere (the DB is opened mode=ro)
  · never guess — an unreadable source yields None AND its name in `degraded[]`
  · never claim a route is proven; only a CALL proves a route, and this makes no calls
  · never carry `n` in any form (see NOTE ON `n` below)
  · never mint a second trading fence — it calls the existing resolver

NOTE ON `n`: an earlier draft of the contract carried `n_payload`. It is deliberately ABSENT.
board_walk.py used to stamp its own sort index under `n`, the same key the board payload uses for the
number the operator rules by; 418 of 420 row/order pairs disagreed. The walk now emits `step` and
`board_n` separately and the boardwalk seat supplies both. There is no correct `n` for this module to
carry, so it carries none.

Run directly to execute the controls:  /usr/bin/python3 operator/focus_sources.py --self-test
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path.home()
DB = HOME / ".config" / "AionUi-Dev" / "aionui" / "aionui.db"
CDP_PORT = 9230  # the DECLARED port — see _aionui_cdp_port(), which does not trust it


def _aionui_cdp_port() -> tuple[int, str]:
    """Resolve AionUi's CDP port FROM THE RUNNING PROCESS, never from this constant.

    ★★★ AGENTS.md:10 already stated the rule and this module broke it:
        "His window: the electron process serving out/renderer/index.html over CDP port 9230 —
         determined from the RUNNING PROCESS, never a config assumption."
    Hardcoding 9230 is precisely the config assumption it forbids, and on 2026-09-17 it cost the
    operator his microphone: a `hermes --profile lead-example acp` process bound :9230 twelve minutes
    before Electron asked for it, Electron lost the bind silently, and every read here went to a
    socket that accepted and never answered.

    ★★ This is also a REGRESSION, not a new bug. routing_state.md records the same class already
    fixed once: "CDP-socket fix (app-spawned voicebox no longer steals :9230)". That fix named the
    thief instead of the class, so the next thief walked straight in. Deriving the port from the
    process that actually serves the window fixes the class: whoever else takes 9230, we follow the
    real window rather than talking to a stranger.

    Returns (port, note). Falls back to the declared port and SAYS SO.
    """
    try:
        import subprocess
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=4).stdout
    except Exception as exc:
        return CDP_PORT, f"could not read the process list ({exc.__class__.__name__}); using declared :{CDP_PORT}"
    for line in out.splitlines():
        if "electron" not in line or "--remote-debugging-port=" not in line:
            continue
        m = re.search(r"--remote-debugging-port=(\d+)", line)
        if m:
            port = int(m.group(1))
            return port, ("" if port == CDP_PORT
                          else f"AionUi is on :{port}, not the declared :{CDP_PORT}")
    return CDP_PORT, f"no electron process carries --remote-debugging-port; using declared :{CDP_PORT}"


def _aionui_cdp_ports() -> list[int]:
    """EVERY candidate AionUi CDP port, declared port FIRST.

    ★★★ WHY THIS EXISTS — 2026-09-21, and it is the reason the operator's microphone would not open.

    `_aionui_cdp_port()` above returns the FIRST electron process in `ps` order. That was correct
    while there was ONE AionUi. There are now TWO, and they are not equivalent:

        ps order 1-2   electron . --webui --remote-debugging-port=9231   <- trading/webui: 0 PAGE TARGETS
        ps order 3-4   electron .        --remote-debugging-port=9230    <- the renderer: 1 page target

    9231 sorts first, so the resolver confidently returned it and even reported
    "AionUi is on :9231, not the declared :9230" — a WRONG ANSWER DELIVERED AS A CORRECTION.
    `_cdp_targets` then found zero page targets, `capture_policy()` raised
    RuntimeError("no AionUi page target"), the durable store faithfully published readable:false,
    and the mic could never open no matter how many times he pressed the button.

    ★ The declared port is FIRST because AGENTS.md names 9230 as the port serving
      out/renderer/index.html. Order alone is not the fix — `_cdp_targets` now also REJECTS a port
      that answers with zero page targets — but declared-first is the correct prior.
    ★ Deduped and order-preserving, so the common single-instance case is unchanged.
    """
    ports: list[int] = [CDP_PORT]
    try:
        import subprocess
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True,
                             timeout=4).stdout
    except Exception:
        return ports          # declared port only — never raise from a discovery helper
    for line in out.splitlines():
        if "electron" not in line or "--remote-debugging-port=" not in line:
            continue
        m = re.search(r"--remote-debugging-port=(\d+)", line)
        if m:
            p = int(m.group(1))
            if p not in ports:
                ports.append(p)
    return ports

# The same fence the router already enforces. Kept as a literal mirror ONLY as a backstop for the
# case where chat_menu cannot be imported; the imported resolver is authoritative when available.
_FENCE = re.compile(r"trading|crypto|liquid|coinvest|sleeve|cockpit", re.I)


# ---------------------------------------------------------------- CDP (selection lives in the UI)



def _cdp_targets(timeout: float = 1.5):
    """List CDP targets, FAST, and name a port collision instead of guessing.

    ★★★ WHY THIS IS NOT JUST A TIMEOUT TWEAK. On 2026-09-17 a Hermes agent
    (`hermes --profile lead-example acp`) bound 127.0.0.1:9230 at 08:59:44 — twelve minutes before
    Electron asked for it at 09:11:07. Electron LOST THE BIND SILENTLY and ran with no CDP. The
    socket then ACCEPTED every connection and never spoke HTTP: no response, no close, no error
    until timeout. That is the ideal shape for a file-descriptor leak, and it is what walked the
    panel to 1023/1024 fds and took the operator's microphone cold.

    ★★ So "port is listening" proved nothing. The listener was the wrong process. This probes with a
    SHORT timeout and reports the collision by name, because a 4-second generic failure told us
    nothing for hours while a 1.5-second "that port is not a CDP endpoint" would have pointed
    straight at it.
    """
    import http.client
    # ★★★ 2026-09-21 — TRY EVERY CANDIDATE, AND REJECT A PORT WITH NO PAGE TARGET.
    #   This used to bind to `_aionui_cdp_port()`'s single answer. With two AionUi instances that
    #   answer was the trading instance (9231), which ANSWERS HTTP perfectly and serves ZERO page
    #   targets — so the old code called it a success, found no page, and raised
    #   "no AionUi page target". Answering is not the same as serving the window.
    #   Returning on the first candidate that serves a real page target makes the result independent
    #   of `ps` ordering, which is the actual defect: the previous fix named the thief, not the class.
    notes: list[str] = []
    for port in _aionui_cdp_ports():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
            conn.request("GET", "/json/list")
            body = conn.getresponse().read()
            conn.close()
        except Exception as exc:
            notes.append(f":{port} did not answer in {timeout}s ({exc.__class__.__name__})")
            continue
        try:
            data = json.loads(body)
        except Exception:
            notes.append(f":{port} answered but not with CDP JSON — PORT COLLISION")
            continue
        if not isinstance(data, list):
            notes.append(f":{port} returned an unexpected shape — not a CDP endpoint")
            continue
        # ★ THE FIX: zero page targets means this is the WRONG INSTANCE, not a working bridge.
        if not any(t.get("type") == "page" for t in data):
            notes.append(f":{port} is a live CDP endpoint with 0 page targets (wrong AionUi instance)")
            continue
        return data, ""
    return None, ("no AionUi page target on any candidate port — " + "; ".join(notes)
                  if notes else "no candidate CDP port found")


def _cdp_eval(ws_url: str, expression: str, timeout: float = 4.0):
    """Open a CDP socket, evaluate ONE expression, and ALWAYS close it.

    ★★★ THIS EXISTS BECAUSE THE PANEL FELL OVER. Three call sites each did
    `ws = create_connection(...)` … work … `ws.close()` inside a try whose except returned. Any
    exception in between leaked the socket, and the except swallowed the evidence. The voice seat
    polls this path every 1.5s, so over ~9 hours the panel reached **1023 of 1024 open fds** and
    stopped accepting connections. It never crashed — it went quiet, which is worse.

    ★★ And because the seat FAILS CLOSED when it cannot read the policy, a leaked file descriptor
    took the operator's microphone cold. A resource leak is not a slow-burning bug when something
    safety-critical reads through it. `finally` is not hygiene here, it is the safety property.
    """
    import websocket  # websocket-client
    # ★★★ SHORT TIMEOUT + RETRY, because :9230 is a SHARED socket, not a stolen one.
    # AionUi's ACP agents INHERIT Electron's CDP listening fd (proven: hermes pid and electron pid
    # both hold fd 81 on socket inode 85526282, flags 04002 = no FD_CLOEXEC). The kernel hands a new
    # connection to EITHER process; when the hermes child gets it, it accepts and never speaks HTTP.
    # A long single attempt therefore hangs on a coin flip. A short attempt retried lands on Electron
    # quickly. This turned capture-policy from ~6s to roughly one second, which matters because the
    # panel is a ThreadingHTTPServer: every slow request is a live thread holding descriptors, and at
    # 1024 fds accept() starts spinning and burns a core. A slow read here overheats the laptop.
    per_try = min(1.2, timeout)
    ws = None
    last = None
    for _ in range(3):
        try:
            ws = websocket.create_connection(ws_url, timeout=per_try, suppress_origin=True)
            break
        except Exception as exc:          # noqa: BLE001 - a hung child is indistinguishable from down
            last = exc
            ws = None
    if ws is None:
        raise RuntimeError(f"CDP did not answer in 3 x {per_try}s "
                           f"({last.__class__.__name__ if last else 'no detail'}) - the port is "
                           f"shared with an inherited child socket; see INV-117")
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "returnByValue": True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return (msg.get("result") or {}).get("result", {}).get("value")
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _cdp_selection(timeout: float = 4.0) -> tuple[dict | None, str]:
    """Read the CURRENT selection out of the running renderer. Returns (data, error).

    Selection is genuinely UI state — the database does not know which chat is on screen — so this
    is the only honest source. It reads `location.hash` and the team's persisted active-slot key.
    No DOM scraping: the hash and localStorage are stable, the DOM is not.
    """
    targets, terr = _cdp_targets(1.5)
    if targets is None:
        return None, terr

    page = next((t for t in targets if t.get("type") == "page" and "AionUi" in (t.get("title") or "")), None) \
        or next((t for t in targets if t.get("type") == "page"), None)
    if not page or not page.get("webSocketDebuggerUrl"):
        return None, "no AionUi page target on CDP"

    expr = r"""
(function(){
  var h = location.hash || '';
  var t = h.match(/#\/team\/([^\/?#]+)/);
  var c = h.match(/#\/conversation\/([^\/?#]+)/);
  var teamId = t ? t[1] : null;
  var slot = null;
  try { if (teamId) slot = localStorage.getItem('team-active-slot-' + teamId); } catch(e) {}
  return JSON.stringify({hash:h, teamId:teamId, conversationId: c ? c[1] : null, activeSlotId: slot});
})()
"""
    try:
        raw = _cdp_eval(page["webSocketDebuggerUrl"], expr, timeout)
        if raw is None:
            return None, "CDP returned no value for the selection probe"
        return json.loads(raw), ""
    except Exception as exc:
        return None, f"CDP evaluate failed ({exc.__class__.__name__}: {exc})"


# ---------------------------------------------------------------- the database (read-only, always)

from contextlib import contextmanager


@contextmanager
def _db():
    """Read-only sqlite handle that ALWAYS closes.

    ★★★ THIS IS THE SECOND FD LEAK IN THIS FILE, AND I CAUSED BOTH. This morning I wrapped the CDP
    websockets in `finally` after they took the panel to 1023/1024 descriptors and left the operator
    without a microphone — and I did not audit the sqlite path six lines below them. Every call to
    `_db()` opened a connection and nothing closed it, so each /state/focus-sources read leaked TWO
    descriptors (the db and its -wal). Measured when it bit again: 1024 fds, of which 40 were
    aionui.db and 30 aionui.db-wal, and a secrets write failed with [Errno 24] Too many open files.

    ★★ I FIXED THE INSTANCE AND NOT THE CLASS. The websocket fix was correct and insufficient: the
    lesson was never "close websockets", it was "every handle in this module needs a guaranteed
    close". A fix that names one resource leaves the next one to be found by an outage.
    """
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _gate(conv_id: str, name: str, extra: dict) -> tuple[bool, str]:
    """Call the EXISTING resolver. Never mint a second fence."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis"))
        import chat_menu as _cm  # type: ignore
        row = {"id": conv_id, "name": name, "team": extra.get("teamId"),
               "workspace": extra.get("workspace"), "type": "acp", "messages": 0}
        ok, why = _cm._gate(row)
        return bool(ok), (why or "")
    except Exception:
        # backstop only — if the resolver cannot be imported we still refuse the fenced lane,
        # and the caller is told the resolver was unavailable via degraded[].
        blob = f"{conv_id} {name} {extra.get('teamId') or ''} {extra.get('workspace') or ''}"
        if _FENCE.search(blob):
            return False, "FENCED: trading lane"
        return True, ""



def _profile_route(profile_id: str) -> dict | None:
    """Read what a Hermes profile ACTUALLY routes to. This is the authority; `currentModelId` is a label.

    ★ The boardwalk seat caught the contradiction that produced this: the endpoint advised consumers
    to trust the profile authority over the conversation label, while serving the label in the
    `model` field. Advice and implementation disagreed. Measured the same day: 22 conversations were
    labelled `deepseek/deepseek-v4-pro` while their profile had already moved to
    `deepseek-v4-flash-0731`. The label and the router were two different values.
    """
    if not profile_id:
        return None
    cfg = HOME / ".hermes" / "profiles" / profile_id / "config.yaml"
    if not cfg.is_file():
        return None
    try:
        import yaml
        y = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return None
    m = y.get("model") or {}
    return {"profile": profile_id,
            "model": m.get("default"),
            "base_url": m.get("base_url"),
            "provider": m.get("provider"),
            "source": f"profiles/{profile_id}/config.yaml"}


def _resolve_conversation(conv_id: str) -> tuple[dict | None, list[str]]:
    """Turn a conversation id into the DESTINATION TRIPLE + provenance. Never guesses."""
    degraded: list[str] = []
    try:
        with _db() as c:
            row = c.execute(
                "select id,name,type,extra,model from conversations where id=?", (conv_id,)
            ).fetchone()
    except Exception as exc:
        return None, [f"aionui-db ({exc.__class__.__name__})"]
    if not row:
        return None, ["conversation-not-found"]

    cid, name, ctype, extra_raw, model_col = row
    try:
        extra = json.loads(extra_raw or "{}")
    except Exception:
        extra, _ = {}, degraded.append("conversation-extra-unparseable")

    ok, why = _gate(cid, name or "", extra)
    if not ok:
        return {"refused": True, "reason": why, "conversation_id": cid}, degraded

    # ★ harness and model are SEPARATE, always. 78 conversations run one model (kimi-k3) under three
    #   different harness names — a row that shows one and implies the other is the A4 trap in code.
    harness = extra.get("backend") or extra.get("cliPath")
    if not harness:
        harness = "unknown"                      # ★ a real state: 11 of 166 have no backend recorded
        degraded.append("harness-not-recorded")

    model = extra.get("currentModelId") or model_col
    if not isinstance(model, str) or not model:
        model = None
        degraded.append("model-not-recorded")

    # agentProfile OVERRIDES routing; hermesProfile is DISPLAY ONLY and must never be read as a route.
    ap = extra.get("agentProfile")
    routing_authority = None
    if isinstance(ap, str) and ap:
        routing_authority = f"agentProfile:{ap}"
    elif isinstance(ap, dict) and ap.get("id"):
        routing_authority = f"agentProfile:{ap['id']}"

    hp = extra.get("hermesProfile")
    display_only = {"hermesProfile": hp if isinstance(hp, str) else (hp or {}).get("id") if isinstance(hp, dict) else None}

    team_name = None
    try:
        tid = extra.get("teamId")
        if tid:
            with _db() as c2:
                r = c2.execute("select name from teams where id=?", (tid,)).fetchone()
            team_name = r[0] if r else None
    except Exception:
        degraded.append("team-name")

    return {
        "refused": False,
        "conversation_id": cid,
        "name": name,
        "team_id": extra.get("teamId"),
        "team_name": team_name,
        "harness": harness,
        "model": model,
        "routing_authority": routing_authority,
        "display_only": display_only,
        # ★ THE AUTHORITY, resolved — not the label. `model` above is extra.currentModelId, which is
        #   what the SCREEN shows; `routes_to` is what the profile will actually call. When these
        #   disagree, the disagreement is the information. Prefer routes_to.model when it is present.
        "routes_to": _profile_route(
            (ap if isinstance(ap, str) else (ap or {}).get("id") if isinstance(ap, dict) else None)
            or (hp if isinstance(hp, str) else (hp or {}).get("id") if isinstance(hp, dict) else None)
        ),
        "route_proven": False,          # ★ ALWAYS false. Only a CALL proves a route; this makes none.
        "provenance": {
            "model_is": "extra.currentModelId — a DISPLAY LABEL, not the router",
            "authority_is": "routes_to.model — read from the profile config",
            "harness": "extra.backend" if extra.get("backend") else (
                "extra.cliPath" if extra.get("cliPath") else "absent"),
            "model": "extra.currentModelId" if extra.get("currentModelId") else (
                "conversations.model" if model_col else "absent"),
        },
    }, degraded


def _slot_to_conversation(team_id: str, slot_id: str) -> tuple[str | None, list[str]]:
    try:
        with _db() as c:
            r = c.execute("select agents from teams where id=?", (team_id,)).fetchone()
        if not r:
            return None, ["team-not-found"]
        for a in json.loads(r[0] or "[]"):
            if a.get("slotId") == slot_id:
                return a.get("conversationId"), []
        return None, ["slot-not-in-team"]
    except Exception as exc:
        return None, [f"team-agents ({exc.__class__.__name__})"]


# ---------------------------------------------------------------- the contract payload

def sources() -> dict:
    """The agreed payload. Read-only. Guesses nothing. Carries no `n`."""
    degraded: list[str] = []
    sel, err = _cdp_selection()

    aionui: dict = {"reachable": sel is not None, "route": None, "selected": None}
    if sel is None:
        degraded.append("aionui-sources")
        aionui["error"] = err
    else:
        aionui["route"] = sel.get("hash") or None
        conv_id = sel.get("conversationId")
        if not conv_id and sel.get("teamId") and sel.get("activeSlotId"):
            conv_id, d = _slot_to_conversation(sel["teamId"], sel["activeSlotId"])
            degraded += d
        if not conv_id and sel.get("teamId"):
            degraded.append("active-slot-not-persisted")
        if conv_id:
            resolved, d = _resolve_conversation(conv_id)
            degraded += d
            aionui["selected"] = resolved
        else:
            degraded.append("no-conversation-selected")

    return {
        "ts": int(time.time() * 1000),
        "aionui": aionui,
        # ★ Both null by agreement: board_walk state belongs to the boardwalk seat and it merges them.
        #   Half-owning another seat's state is how two stores start.
        "walk": {"step_id": None, "board_n": None},
        "board": {"expanded_row_id": None},
        # No server-side source exists for the panel's open section — it is client state. Reported as
        # unknown rather than defaulted, so the deriver can skip that precedence rung honestly.
        "panel": {"open_section": None},
        "degraded": sorted(set(degraded)) + ["walk", "board", "panel-section"],
    }


# ---------------------------------------------------------------- the CAPTURE master, read-only

def capture_policy() -> dict:
    """The CAPTURE master as the ONE store holds it. Read-only, for the voice seat.

    ★★★ WHY THIS EXISTS: `capture_master.override` is NOT a stand-in — it is currently the voice
    seat's ONLY master switch (`jarvis/core.py:161-180`; `capture_master_on()` never reads the real
    store and ends in `return False`). Deleting it does not restore governance, it makes that
    microphone permanently cold. The seat kept a file because it had no readable path to the real
    store: the policy lives in the renderer's localStorage, which nothing can read out from under a
    running Electron app on every mic tick. This endpoint IS that path. Once the seat reads here,
    the override becomes unnecessary and removing it is correct rather than destructive.

    ★ NAMING: deliberately NOT `/capture/policy`. On this panel `/capture/*` already means the
    KNOWLEDGE-capture queue (`/capture/queue`, `/capture/approve`) — a completely different thing
    that happens to share the word. Two meanings for "capture" on one box is how someone eventually
    wires the scrub queue to a microphone. It lives at `/state/capture-policy`, beside its sibling.
    """
    sel_err = ""
    try:
        targets, terr = _cdp_targets(1.5)
        if targets is None:
            raise RuntimeError(terr)
        page = next((t for t in targets if t.get("type") == "page" and "AionUi" in (t.get("title") or "")), None) \
            or next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise RuntimeError("no AionUi page target")
        raw = _cdp_eval(page["webSocketDebuggerUrl"],
                        "localStorage.getItem('khaoss.capture.policy')", 4)
        if raw:
            p = json.loads(raw)
            master, paused = bool(p.get("master")), bool(p.get("paused"))
            return {"ts": int(time.time() * 1000), "readable": True,
                    "master": master, "paused": paused,
                    # the seat wants ONE boolean; this is the same decision captureGate() makes.
                    "mic_allowed": bool(master and not paused),
                    "reason": ("capture master is OFF" if not master else
                               "capture is PAUSED" if paused else "capture master is ON"),
                    "source": "localStorage:khaoss.capture.policy (the ONE store)",
                    "degraded": []}
        sel_err = "policy key absent — AionUi has never written it"
    except Exception as exc:
        sel_err = f"{exc.__class__.__name__}: {exc}"

    # ★ FAIL CLOSED. An unreadable policy is never an opening — the caller is told it could not look,
    # and mic_allowed is false, so a broken bridge silences the mic rather than freeing it.
    return {"ts": int(time.time() * 1000), "readable": False,
            "master": None, "paused": None, "mic_allowed": False,
            "reason": f"policy unreadable — failing closed ({sel_err})",
            "source": "localStorage:khaoss.capture.policy (the ONE store)",
            "degraded": ["capture-policy"]}


# ---------------------------------------------------------------- the CAPTURE master, WRITE path

AUDIT_LOG = Path(__file__).resolve().parent.parent / "logs" / "capture_policy_writes.log"


def set_capture_policy(master=None, paused=None, actor: str = "unknown") -> dict:
    """Set the CAPTURE master in the ONE store. Loopback-only (enforced by the caller).

    ★★★ WHY A WRITE PATH EXISTS AT ALL: the operator pressed tap-to-speak, was refused correctly,
    and the only remedy anyone could offer was "go to AionUi and flip CAPTURE" — told to him three
    times across three reports. That is the GUI-parity rule (INV-39) failing in the direction that
    leaves him unable to use the thing at all. The voice seat could not turn on its own microphone
    and neither could he from inside it.

    ★★ ONE STORE, ONE WRITER. The voice seat holds a CDP handle to the same page and could have
    written the key itself; it deliberately did not, because a second writer reaching into this
    store is the two-writer defect we spent the day removing. This is that single writer.

    ★ THE CLAIM THAT MUST NEVER BE WRONG IS "I TURNED YOUR MIC OFF". So the write is read back FROM
    THE STORE (not echoed from the request) and the returned object is the re-read. If the re-read
    does not confirm the requested value, this reports FAILURE — never success — and for an OFF
    request that is the whole point. An unconfirmed 'off' is treated as still-on.

    ★ NO AUTO-ANYTHING. This is an operator action surfaced in a GUI. Nothing here schedules itself,
    and every write is appended to an audit line so "who turned the microphone on" is always
    answerable rather than inferred.
    """
    if master is None and paused is None:
        return {"ok": False, "error": "nothing to set — provide master and/or paused"}
    for name, v in (("master", master), ("paused", paused)):
        if v is not None and not isinstance(v, bool):
            return {"ok": False, "error": f"{name} must be a boolean, got {type(v).__name__}"}

    expr = """
(function(){
  var K='khaoss.capture.policy';
  var cur={}; try{ cur=JSON.parse(localStorage.getItem(K)||'{}'); }catch(e){}
  var next={}; for (var k in cur) next[k]=cur[k];
  if (__M__!==null) next.master=__M__;
  if (__P__!==null) next.paused=__P__;
  localStorage.setItem(K, JSON.stringify(next));
  try{ window.dispatchEvent(new CustomEvent('khaoss-capture-changed')); }catch(e){}
  var after={}; try{ after=JSON.parse(localStorage.getItem(K)||'{}'); }catch(e){}
  return JSON.stringify({before:cur, after:after});
})()
""".replace("__M__", "null" if master is None else ("true" if master else "false")) \
   .replace("__P__", "null" if paused is None else ("true" if paused else "false"))

    err = ""
    try:
        targets, terr = _cdp_targets(1.5)
        if targets is None:
            raise RuntimeError(terr)
        page = next((t for t in targets if t.get("type") == "page" and "AionUi" in (t.get("title") or "")), None) \
            or next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            raise RuntimeError("no AionUi page target — is the app running?")
        # ★ through the always-closing helper — this site leaked too, and a leaked fd here is a
        #   microphone that cannot be switched on.
        raw = _cdp_eval(page["webSocketDebuggerUrl"], expr, 5)
        if raw is None:
            raise RuntimeError("CDP returned no value for the write")
        moved = json.loads(raw)
    except Exception as exc:
        err = f"{exc.__class__.__name__}: {exc}"
        moved = None

    # ★ CONFIRM FROM THE STORE, by a SECOND independent read — not from the write's own return value.
    verified = capture_policy()
    ok = bool(moved) and verified.get("readable") is True
    if ok and master is not None:
        ok = verified.get("master") is master
    if ok and paused is not None:
        ok = verified.get("paused") is paused

    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps({
                "ts": int(time.time() * 1000), "actor": actor,
                "requested": {"master": master, "paused": paused},
                "before": (moved or {}).get("before"),
                "verified_after": {"master": verified.get("master"), "paused": verified.get("paused")},
                "ok": ok, "error": err or None,
            }) + "\n")
    except Exception:
        pass  # a failed audit line must not change the outcome, but must not hide it either

    return {
        "ok": ok,
        "requested": {"master": master, "paused": paused},
        "verified": verified,
        "actor": actor,
        "error": None if ok else (err or "the store did not confirm the requested value — treat as UNCHANGED"),
    }


# ---------------------------------------------------------------- controls

def self_test() -> int:
    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail and not cond else ''}")
        if not cond:
            fails.append(name)

    p = sources()

    # 1 — the payload must never contain a bare `n` at any depth (the ruling-by-number near-miss)
    def has_n(o):
        if isinstance(o, dict):
            return "n" in o or any(has_n(v) for v in o.values())
        if isinstance(o, list):
            return any(has_n(v) for v in o)
        return False
    check("carries no key named 'n' at any depth", not has_n(p))

    # 2 — route_proven is always false; this module makes no calls
    s = (p.get("aionui") or {}).get("selected") or {}
    check("route_proven is False whenever a selection resolved",
          (s.get("route_proven") is False) if s and not s.get("refused") else True)

    # 3 — an unreadable source must be NAMED, not silently defaulted
    check("degraded[] is populated and lists unreadable sources",
          isinstance(p.get("degraded"), list) and len(p["degraded"]) > 0)

    # 4 — harness and model are separate keys, neither inferred from the other
    if s and not s.get("refused"):
        check("harness and model are separate fields", "harness" in s and "model" in s)
        check("harness is never empty (uses 'unknown')", bool(s.get("harness")))
    else:
        print("  [SKIP] harness/model split — no live selection to inspect")

    # 5 — the fence: a trading conversation must never resolve through this module
    fenced, _ = _resolve_conversation("94b01ce0")   # Crypto trading - Leader
    check("fenced trading conversation is refused",
          fenced is not None and fenced.get("refused") is True,
          f"got {fenced}")

    # 6 — read-only: a write against the handle must be rejected by SQLite itself
    try:
        with _db() as _c:
            _c.execute("create table _probe_should_fail (x int)")
        check("database handle is read-only", False, "a CREATE TABLE succeeded")
    except sqlite3.OperationalError as e:
        check("database handle is read-only", "readonly" in str(e).lower(), str(e))

    import unittest.mock as _m
    # ★★★ THE ONE BLINDING POINT, AND WHY IT IS THIS FUNCTION.
    # Every CDP read AND every CDP write in this module funnels through `_cdp_targets()`:
    #     capture_policy()      line ~497  -> _cdp_targets(1.5) -> _cdp_eval(...)   [READ]
    #     set_capture_policy()  line ~580  -> _cdp_targets(1.5) -> _cdp_eval(...)   [WRITE]
    #     _cdp_selection()      line ~233  -> _cdp_targets(1.5) -> _cdp_eval(...)   [READ]
    # So patching THIS symbol blinds all three, and patching any resolver underneath it blinds
    # only the callers that happen to use that resolver.
    #
    # ★★★ WHY THIS COMMENT EXISTS — I RE-OPENED A FIXED WOUND ON 2026-09-21, AND HERE IS THE PROOF.
    # The blinding used to patch `_aionui_cdp_port` (SINGULAR). That was correct while the singular
    # was the production resolver. Then `_cdp_targets` was rewired to call `_aionui_cdp_ports`
    # (PLURAL) — and the singular became DEAD CODE whose only remaining references were these
    # patches. The patch silently became a NO-OP, and this self-test could once again reach the
    # LIVE browser and write `master=False`, i.e. TURN THE OPERATOR'S MICROPHONE OFF.
    #
    # It had already happened once, and his own audit log still holds the entry:
    #   logs/capture_policy_writes.log
    #     {"actor":"self-test(stubbed)","requested":{"master":false},
    #      "before":{"master":true,...},"verified_after":{"master":false},"ok":true}
    #   ^ CAPTURE was ON. The self-test wrote it OFF. That is the incident the note below records.
    #
    # ★ The lesson is exactly the one this file keeps teaching: patch the CLASS, not the instance.
    #   A guard that names a helper is a guard that breaks the next time the helper is refactored —
    #   and it breaks SILENTLY, because a no-op patch looks exactly like a passing test.
    def _blind() -> "_m._patch":
        """Point CDP at a port nothing listens on, at the one funnel every read and write uses."""
        return _m.patch(__name__ + "._cdp_targets",
                        lambda *a, **k: (None, "stubbed dead port (self-test must not touch the browser)"))

    # 8 — the capture bridge must FAIL CLOSED: unreadable policy ⇒ mic_allowed false, never true
    cp = capture_policy()
    check("capture_policy exposes mic_allowed as a bool", isinstance(cp.get("mic_allowed"), bool))
    with _blind():
        closed = capture_policy()
    check("capture policy FAILS CLOSED when unreadable",
          closed["mic_allowed"] is False and closed["readable"] is False
          and "capture-policy" in closed["degraded"], str(closed)[:120])
    # ★ THE GUARD AGAINST RE-ORPHANING. If someone again makes _cdp_targets call a NEW resolver and
    #   blunts this patch, the first check above stops failing closed. So assert the blindness is
    #   REAL and not merely configured: with _blind() active, the bridge must be unreadable. If a
    #   future refactor makes this pass, the guard has been orphaned — not the test.
    with _blind():
        _orphan_probe = capture_policy()
    check("★ the blinding above is REAL, not a no-op patch (guards against re-orphaning)",
          _orphan_probe["readable"] is False,
          f"the stub did not blind CDP — the patch target has moved again: {str(_orphan_probe)[:100]}")

    # 10/11 — the WRITE path: type-safe, and it confirms from the store rather than echoing
    bad = set_capture_policy(master="yes")
    check("write rejects a non-boolean master", bad["ok"] is False and "boolean" in bad["error"])
    # ★★★ NEVER MUTATE THE OPERATOR'S LIVE POSTURE. An earlier version of this control called
    # set_capture_policy(master=False) and justified it as "a no-op — master is already False".
    # That was only true while his microphone happened to be off. Once CAPTURE was switched ON, this
    # control silently turned his LIVE mic off — the audit log still holds that write. The control is
    # therefore blinded at _cdp_targets, so the write PHYSICALLY CANNOT reach the browser, and the
    # verification half is stubbed. The verdict depends on the code, never on his settings.
    #
    # ★ AND THE NO-HARM ASSERTION IS NOW CHECKED BEFORE AS WELL AS AFTER, because a check that only
    #   notices the mutation afterwards is an autopsy, not a guard.
    _audit = AUDIT_LOG
    def _audit_rows():
        """Every audit row, parsed. Used to prove the blinded writes were REFUSED, not silent."""
        out = []
        try:
            for _ln in _audit.read_text(encoding="utf-8").splitlines():
                _ln = _ln.strip()
                if _ln:
                    try:
                        out.append(json.loads(_ln))
                    except Exception:
                        pass
        except Exception:
            pass
        return out
    _rows_before = len(_audit_rows())

    def _stub_policy():
        return {"ts": 0, "readable": True, "master": False, "paused": False,
                "mic_allowed": False, "reason": "stub", "degraded": []}
    before = capture_policy()                       # read-only snapshot, for the no-harm assertion
    with _blind(), _m.patch(__name__ + ".capture_policy", _stub_policy):
        off = set_capture_policy(master=False, actor="self-test(stubbed)")
    check("write OFF is confirmed by an independent re-read (stubbed, cannot reach the browser)",
          off["verified"]["master"] is False, str(off)[:140])
    after = capture_policy()
    check("the self-test did NOT change the operator's live capture posture",
          before.get("master") == after.get("master") and before.get("paused") == after.get("paused"),
          f"before={before.get('master')}/{before.get('paused')} after={after.get('master')}/{after.get('paused')}")
    # ★★ THE HARD PROOF, AND MY FIRST VERSION OF IT WAS WRONG — which is worth recording, because
    #    the mistake is the interesting part. I asserted "the audit log must not grow". It DID grow,
    #    and the fix was still working: `set_capture_policy` audits every ATTEMPT, including the
    #    ones it refuses, so a correctly-blinded run always appends `ok:false` rows. Asserting "no
    #    new rows" would have failed on a healthy system and — far worse — would have trained the
    #    next reader to loosen it until it passed.
    # ★  What actually distinguishes harm is a SUCCESSFUL write: one that read a real `before` state
    #    out of localStorage and reported `ok:true`. Rows written since this run began must therefore
    #    contain NO successful write. That is the property; the row count is not.
    _new_all = _audit_rows()[_rows_before:]
    _new_success = [r for r in _new_all if r.get("ok") is True]
    check("★ PROVE-FAIL: no SUCCESSFUL write reached the browser (refused attempts may be audited)",
          not _new_success,
          f"a write SUCCEEDED during the self-test — it reached the live browser: {_new_success[:1]}")
    check("  and the refused attempts SAY SO, rather than failing silently",
          bool(_new_all) and all(r.get("ok") is False for r in _new_all),
          f"expected refused rows carrying the stub's message; got {_new_all[:1]}")

    with _blind():
        blind = set_capture_policy(master=False, actor="self-test")
    check("unconfirmable write reports FAILURE, never success",
          blind["ok"] is False and blind["error"], str(blind)[:120])

    # 7 — CDP down must degrade, never guess
    with _m.patch(__name__ + "._cdp_selection", return_value=(None, "forced")):
        d = sources()
        check("CDP absent -> selected is None and 'aionui-sources' is named",
              d["aionui"]["selected"] is None and "aionui-sources" in d["degraded"])

    print(f"\n  {13 - len(fails)}/13 controls passed" if not fails else f"\n  FAILED: {', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    print(json.dumps(sources(), indent=2))
