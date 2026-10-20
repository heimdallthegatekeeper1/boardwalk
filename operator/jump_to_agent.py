#!/usr/bin/env python3
"""jump_to_agent.py — R-09: take him from a board row to the conversation that owns it.

★★★ HIS ASK, and he called it especially important: "be able to press a button to have us be able to
    jump-to that agent". The board can already SEND to an agent and BIND an item to one. What it could
    not do is the simplest thing: put him in front of that conversation.

★★ IT NAVIGATES THE LIVE WINDOW, it does not open a second one. AionUi is a single Electron window with
    a hash route per team; a new window would be a second surface showing the same conversation, and
    this house has paid twice for two surfaces disagreeing. So it drives the window he already has.

★ AND IT SAYS WHAT IT DID. Navigation is invisible if he is looking at the panel rather than at AionUi —
  the app changes behind him and nothing on his screen moves. So the response names the team it went to
  and warns when AionUi is not reachable, rather than reporting success into the void.
"""
from __future__ import annotations
import json, sqlite3, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = Path.home() / ".config/AionUi-Dev/aionui/aionui.db"
CDP = "http://127.0.0.1:9230"

# lane → team id. Read from the panel's own map rather than kept as a second copy.
def _lane_teams() -> dict:
    try:
        src = (HERE.parent / "panel_server.py").read_text(errors="replace")
        blk = src.split("_LANE_TEAMS = {", 1)[1].split("}", 1)[0]
        import re
        out = {}
        for m in re.finditer(r'"([a-z-]+)":\s*\("([0-9a-f-]+)",\s*"([0-9a-f]+)",[^,]*,\s*"([^"]+)"', blk):
            out[m.group(1)] = {"team": m.group(2), "conv": m.group(3), "label": m.group(4)}
        return out
    except Exception:
        return {}


# ★★★ CRASH-BOUND CONVERSATIONS — refused HERE, at the resolver, not at any UI.
#   `de63a495` render-crashes AionUi when opened. It is the `pa` lane's conversation in _LANE_TEAMS
#   (panel_server.py:9460), so EIGHT of the thirteen rows that resolve at all resolve to it —
#   pa-gh-auth, pa-himalaya-config, pa-gws-oauth, pa-linear-auth, pa-spotify-auth,
#   credential-leaked-into-chat, proton-bridge-impossible-on-free, gmail-session-lost-netmeter-profile.
#   ★ A voice-driven "take me to the owner" fires hands-free with no chance to stop it, so hiding the
#     button would not be enough: a crash-bound target must be UNREACHABLE FROM EVERY DOOR, and the
#     only place that is true of is the one function every door calls.
# ★★★ THE LOCAL FLOOR — never the whole fence. 2026-09-17.
#   This set was a SECOND, INDEPENDENT COPY of jarvis/chat_menu.py's NEVER_OPEN. Two copies of one
#   fence in two territories: if either seat adds a crash-bound conversation to its own copy, THE
#   OTHER DOOR STAYS OPEN. The other seat found this shape in their own WebView today and gave the
#   rule it breaks: A FENCE MUST BE CONSULTED, NEVER RE-DERIVED.
#   ★ This door is reachable from panel_server.py (/operator/jump), so it is my door and my fix.
CRASH_BOUND_CONVS = {"de63a495"}


def _crash_bound_ids() -> set[str]:
    """Every id any seat has fenced. UNION, never replacement.

    ★★ The union is the safety property, not a convenience. Consulting an upstream fence can only
    ever ADD ids — so an ImportError, a renamed module, or an upstream set that shrinks can never
    OPEN a door that was closed. A delegating fence that fails to load must refuse MORE, not less.
    """
    ids = set(CRASH_BOUND_CONVS)
    try:
        import sys as _s
        from pathlib import Path as _P
        _j = str(_P.home() / "boardwalk" / "jarvis")
        if _j not in _s.path:
            _s.path.insert(0, _j)
        from chat_menu import NEVER_OPEN as _n
        ids |= {str(x) for x in _n}
    except Exception:
        pass                    # an unreadable upstream fence narrows nothing
    return ids


def _crash_bound(tgt: dict | None) -> bool:
    conv = str((tgt or {}).get("conv") or "")
    return any(conv.startswith(c) for c in _crash_bound_ids())


def target_for(item: dict) -> dict | None:
    """Where does this item belong? Lane first, then an explicit binding.

    ★ Returns None for a crash-bound target rather than a target with a warning attached, because a
      caller that gets a dict will navigate to it. The REASON is available from `refusal_for()` so the
      surface can say why the jump is absent instead of rendering a dead button."""
    lanes = _lane_teams()
    lane = (item.get("lane") or "").lower()
    if lane in lanes:
        t = {**lanes[lane], "via": f"the item's lane ({lane})"}
        return None if _crash_bound(t) else t
    b = item.get("bound_to") or {}
    t = str(b.get("target") or "")
    if t.startswith("aionui:"):
        tid = t.split(":", 1)[1]
        for l, v in lanes.items():
            if v["team"].startswith(tid[:8]):
                t = {**v, "via": "the agent this item is bound to"}
                return None if _crash_bound(t) else t
        return {"team": tid, "conv": None, "label": b.get("target_label") or tid[:8],
                "via": "the agent this item is bound to"}
    return None


def refusal_for(item: dict) -> str | None:
    """Why is there no jump for this row? None means 'no reason — it simply has no owner'."""
    lanes = _lane_teams()
    lane = (item.get("lane") or "").lower()
    cand = lanes.get(lane)
    if cand and _crash_bound(cand):
        return (f"the {lane} lane's conversation ({cand['conv']}) render-crashes the app, so this jump "
                f"is refused at the resolver — reach that team another way")
    return None


def jump(item_id: str) -> dict:
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    item = next((i for i in b.items_of(b.load()) if i["id"] == item_id), None)
    if not item:
        return {"ok": False, "error": f"no board item {item_id!r}"}
    tgt = target_for(item)
    if not tgt:
        why = refusal_for(item)
        if why:
            return {"ok": False, "refused": "crash-bound", "error": why,
                    "how": "this is a safety refusal, not a missing binding — do not re-point it here"}
        return {"ok": False, "error": "this item has no lane and is not bound to an agent, so there is "
                                      "no conversation to jump to",
                "how": "bind it to an agent first, or give it a lane"}
    try:
        tl = json.load(urllib.request.urlopen(f"{CDP}/json/list", timeout=6))
        pg = next((t for t in tl if t.get("type") == "page" and "index.html" in (t.get("url") or "")), None)
    except Exception:
        pg = None
    if not pg:
        return {"ok": False, "target": tgt,
                "error": "AionUi is not answering on CDP 9230 — it may be closed",
                "how": f"open AionUi and go to the {tgt['label']} team yourself"}
    try:
        import websocket
        ws = websocket.create_connection(pg["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
        try:
            was = None
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": "location.hash", "returnByValue": True}}))
            t0 = time.time()
            while time.time() - t0 < 10:
                m = json.loads(ws.recv())
                if m.get("id") == 1:
                    was = (m.get("result", {}).get("result", {}) or {}).get("value")
                    break
            ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                "expression": f"location.hash = {json.dumps('#/team/' + tgt['team'])}; location.hash",
                "returnByValue": True}}))
            now = None
            t0 = time.time()
            while time.time() - t0 < 10:
                m = json.loads(ws.recv())
                if m.get("id") == 2:
                    now = (m.get("result", {}).get("result", {}) or {}).get("value")
                    break
        finally:
            ws.close()
    except Exception as e:
        return {"ok": False, "target": tgt, "error": f"{e.__class__.__name__}: {str(e)[:120]}"}
    # ★ Prove the route CHANGED, rather than reporting that we asked it to.
    moved = bool(now) and tgt["team"][:8] in str(now)
    return {"ok": moved, "target": tgt, "was": was, "now": now,
            "note": (f"AionUi is now showing {tgt['label']} (found via {tgt['via']}). "
                     f"★ It changed BEHIND this panel — switch to the AionUi window to see it."
                     if moved else
                     "the navigation was sent but the route did not change — AionUi may be busy or on "
                     "a modal; nothing here should be read as success")}


def jump_to_conversation(conv: str, label: str | None = None) -> dict:
    """★★★ THE SAME JUMP, KEYED BY CONVERSATION RATHER THAN BY A BOARD ITEM -- 2026-10-13.

    ★ WHY THIS IS A GENERALISATION AND NOT A SECOND JUMP. The voice seat needed to get from a REPLY to
      the agent that wrote it. That reply carries a conversation id and a provenance label, and no
      board item — so `jump()` could not be called. Everything else about the jump is IDENTICAL, and
      the two things that must never be duplicated are duplicated nowhere here:
        · THE FENCE. `_crash_bound()` is consulted, exactly as `target_for()` consults it. `de63a495`
          render-crashes AionUi and is reachable from every door, so the check lives at the one
          function every door calls — which is this module.
        · THE PROOF. The route is READ BACK after it is set, and `ok` is `now contains the id`, never
          "we sent the message". A caller cannot report a jump that did not happen.
    ★ WHAT IT LANDS ON: the AionUi window he ALREADY has, navigated to `#/conversation/<id>` — the
      same route `SpeechAttribution.tsx` jumps to from the titlebar. It does NOT open a second window
      (this house has paid twice for two surfaces disagreeing).
    ★ WHAT IT CANNOT DO, SAID PLAINLY: an agent that is a WEB UI or a LOCAL PROGRAM has no AionUi
      conversation. `transport` is carried on the receipt for exactly that reason; when the caller
      knows the transport it should refuse before calling. This returns a named error rather than
      guessing a route.
    """
    conv = str(conv or "").strip()
    if not conv:
        return {"ok": False, "error": "no conversation id on this row, so there is nowhere to jump"}
    if _crash_bound({"conv": conv}):
        return {"ok": False, "refused": "crash-bound",
                "error": (f"conversation {conv} render-crashes AionUi, so this jump is refused at the "
                          f"resolver — reach that agent another way"),
                "how": "this is a safety refusal, not a missing binding — do not re-point it here"}
    tgt = {"conv": conv, "team": None, "label": str(label or conv), "via": "the reply's receipt"}
    try:
        tl = json.load(urllib.request.urlopen(f"{CDP}/json/list", timeout=6))
        pg = next((t for t in tl if t.get("type") == "page" and "index.html" in (t.get("url") or "")),
                  None)
    except Exception:
        pg = None
    if not pg:
        return {"ok": False, "target": tgt,
                "error": "AionUi is not answering on CDP 9230 — it may be closed",
                "how": f"open AionUi and go to {tgt['label']} yourself"}
    try:
        import websocket
        ws = websocket.create_connection(pg["webSocketDebuggerUrl"], timeout=20, suppress_origin=True)
        try:
            def _ev(_id, _expr, _t=10):
                ws.send(json.dumps({"id": _id, "method": "Runtime.evaluate",
                                    "params": {"expression": _expr, "returnByValue": True}}))
                _t0 = time.time()
                while time.time() - _t0 < _t:
                    m = json.loads(ws.recv())
                    if m.get("id") == _id:
                        return (m.get("result", {}).get("result", {}) or {}).get("value")
                return None
            was = _ev(1, "location.hash")
            now = _ev(2, f"location.hash = {json.dumps('#/conversation/' + conv)}; location.hash")
        finally:
            ws.close()
    except Exception as e:
        return {"ok": False, "target": tgt, "error": f"{e.__class__.__name__}: {str(e)[:120]}"}
    # ★ PROVE THE ROUTE CHANGED, rather than reporting that we asked it to.
    moved = bool(now) and conv[:8] in str(now)
    return {"ok": moved, "target": tgt, "was": was, "now": now,
            "note": (f"AionUi is now showing {tgt['label']} (conversation {conv}). "
                     f"★ It changed BEHIND this seat — switch to the AionUi window to see it."
                     if moved else
                     "the navigation was sent but the route did not change — AionUi may be busy or on "
                     "a modal; nothing here should be read as success")}


def self_test() -> int:
    """★ NEGATIVE CONTROL. Two failures matter: claiming a jump for an item with nowhere to go, and
    reporting success when the route did not actually change."""
    fails = []
    r = jump("__no_such_item__")
    if r.get("ok"):
        fails.append("claimed a jump for an item that does not exist")
    lanes = _lane_teams()
    if not lanes:
        fails.append("no lane→team map could be read — every jump would fail and the reason would be "
                     "invisible")
    else:
        fake = {"id": "x", "lane": "trading"}
        if not target_for(fake):
            fails.append("an item in a mapped lane resolved to no target")
        orphan = {"id": "y", "lane": "unknown"}
        if target_for(orphan):
            fails.append("an item with no lane and no binding was given a target anyway — he would be "
                         "sent somewhere arbitrary")
    for f in fails:
        print(f"  ✗ {f}")
    print(f"  ✓ jump_to_agent self_test: {len(lanes)} lane(s) mapped; refuses items with nowhere to go "
          f"and never invents a destination" if not fails else "  ✗ jump_to_agent self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if len(sys.argv) > 1:
        print(json.dumps(jump(sys.argv[1]), indent=2)); return 0
    print("usage: jump_to_agent.py <item-id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
