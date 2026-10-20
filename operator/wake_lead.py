#!/usr/bin/env python3
"""wake_lead.py — nudge a team lead into taking a turn, and PROVE it took one.

★★★ THE FAILURE THIS EXISTS TO CLOSE. The board's dispatch path reports "delivered — verified in the
    messages table", and that verification is real: the row is there. But a row in a table is STORAGE,
    not COMMUNICATION. A lead that is `disconnected` has the message and will never act on it, and the
    sender has a green tick. That is FC-9 — transport verified, communication not — and it is the exact
    shape this house has paid for repeatedly.

★★ SO THE PROOF IS THE LEAD MOVING, NOT THE MESSAGE ARRIVING. This sends, then watches the conversation
    for a NEW row that the lead itself produced — an assistant message, a tool call, a status flipping
    to active. If none appears inside the window, it says so plainly rather than reporting success
    because the send returned 200.

★ ORDER MATTERS: `team.ensure-session` BEFORE `chat.send.message`. A relaunched AionUi leaves a stale
  team MCP config behind, and a send into that lands nowhere. Emitting ensure-session first is the
  documented repair, and skipping it is how a send silently goes to a dead socket.
"""
from __future__ import annotations
import json, sqlite3, sys, time
from pathlib import Path

DB = Path.home() / ".config/AionUi-Dev/aionui/aionui.db"
CDP = "http://127.0.0.1:9230"


def _page():
    import urllib.request
    tl = json.load(urllib.request.urlopen(f"{CDP}/json/list", timeout=8))
    for t in tl:
        if t.get("type") == "page" and "index.html" in (t.get("url") or ""):
            return t
    return None


def _conv_activity(conv: str) -> tuple[int, str]:
    """How many rows the conversation has, and the newest agent_status."""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ?",
                        (conv + "%",)).fetchone()[0]
        r = con.execute("""SELECT content FROM messages WHERE conversation_id LIKE ?
                           AND type='agent_status' ORDER BY created_at DESC LIMIT 1""",
                        (conv + "%",)).fetchone()
        st = ""
        if r:
            try:
                st = json.loads(r[0]).get("status", "")
            except Exception:
                st = ""
        return n, st
    finally:
        con.close()


def wake(conv: str, team_id: str, text: str, wait: int = 75) -> dict:
    """Send, then WATCH. Returns what actually happened, not what was attempted."""
    try:
        import websocket
    except ImportError:
        return {"ok": False, "error": "the websocket module is not available to this interpreter — "
                                      "use /usr/bin/python3, not bare python3 (which is pyenv here)"}
    pg = _page()
    if not pg:
        return {"ok": False, "error": "no AionUi page on CDP 9230 — the app may be closed"}

    before_n, before_st = _conv_activity(conv)
    ws = websocket.create_connection(pg["webSocketDebuggerUrl"], timeout=30, suppress_origin=True)
    try:
        def ev(expr):
            ws.send(json.dumps({"id": int(time.time() * 1000) % 100000, "method": "Runtime.evaluate",
                                "params": {"expression": expr, "awaitPromise": True,
                                           "returnByValue": True}}))
            t0 = time.time()
            while time.time() - t0 < 30:
                m = json.loads(ws.recv())
                if "result" in m and m.get("id"):
                    return m
            return {}

        # ★ ensure-session FIRST — a send into a stale team config lands nowhere.
        if team_id:
            ev(f"window.electronAPI.emit('team.ensure-session', {json.dumps({'teamId': team_id})}), true")
            time.sleep(2)

        uid = "w" + str(int(time.time()))
        expr = f"""(function(){{
          return new Promise(function(resolve){{
            var cb = 'subscribe.callback-chat.send.message' + {json.dumps(uid)};
            var done = false;
            var to = setTimeout(function(){{ if(!done) resolve({{sent:false, reason:'timeout'}}); }}, 22000);
            window.electronAPI.on(cb, function(r){{ done=true; clearTimeout(to); resolve({{sent:true, r:r}}); }});
            window.electronAPI.emit('subscribe-chat.send.message', {{
              conversation_id: {json.dumps(conv)}, input: {json.dumps(text)}, callback: cb }});
          }});
        }})()"""
        res = ev(expr)
        sent = json.dumps(res)[:300]
    finally:
        try:
            ws.close()
        except Exception:
            pass

    # ★★ NOW THE PART THAT MATTERS: did the lead MOVE?
    moved, waited = False, 0
    while waited < wait:
        time.sleep(5); waited += 5
        n, st = _conv_activity(conv)
        if n > before_n + 1 or (st == "session_active" and before_st != "session_active"):
            moved = True
            break
    after_n, after_st = _conv_activity(conv)
    return {
        "ok": moved,
        "sent_result": sent,
        "rows_before": before_n, "rows_after": after_n,
        "status_before": before_st, "status_after": after_st,
        "waited_seconds": waited,
        "verdict": ("the lead took a turn — new rows appeared after the send" if moved else
                    "★ NOTHING MOVED. The message is in the conversation, but the lead produced no rows "
                    "and its status did not go active within the window. Delivered is not the same as "
                    "read: treat this as UNDELIVERED to the agent, whatever the send returned."),
    }


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is reporting success when nothing woke. So: with a
    conversation that cannot move, wake() must report NOT ok."""
    fails = []
    r = wake("nosuchconversation0000", "", "probe", wait=5)
    if r.get("ok"):
        fails.append("★ reported success for a conversation that cannot exist — it is measuring the "
                     "SEND, not the lead, which is the exact failure it was built to close")
    if r.get("ok") is False and "NOTHING MOVED" not in str(r.get("verdict", "")) and "error" not in r:
        fails.append("a failed wake does not say plainly that delivered is not read")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ wake_lead self_test: a conversation that cannot move is reported as NOT woken"
          if not fails else "  ✗ wake_lead self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if len(a) < 3:
        print("usage: wake_lead.py <conversation-id> <team-id> <text>"); return 2
    r = wake(a[0], a[1], " ".join(a[2:]))
    print(json.dumps(r, indent=2))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
