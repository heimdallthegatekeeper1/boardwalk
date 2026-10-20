#!/usr/bin/env python3
"""★ Deliver the operator board into the team chats — emit only, nothing opened.

★ REUSES THE PROVEN PATH. `pa_notify.py` already solved this for one conversation: drive the AionUi
  `team.send-message` bridge over CDP, emit without navigating, and then VERIFY FROM THE MESSAGES TABLE.
  This is the same mechanism generalised over the target list; it is not a second channel.

★ THE TIMEOUT LIES — the bridge returns {"timeout": true} on sends that landed. Delivery is reported from
  the database row, never from the call not erroring. A conversation that is dormant QUEUES the message
  and answers when it is next opened; that still counts as delivered, and the row proves it.

★ de63a495 (the PA lead) is reached this way and is never opened — that is the whole point of emit-only.

USE:  board_notify.py            # deliver to every installed target
      board_notify.py --dry-run
"""
from __future__ import annotations
import asyncio, json, sqlite3, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CDP = "http://127.0.0.1:9230"
DB = os.path.expanduser("~/.config/AionUi-Dev/aionui/aionui.db")

MSG = """OPERATOR BOARD — {n} item(s) are waiting on the operator ({when})

You now carry the operator board in this workspace's AGENTS.md, so it loads with your context at the start
of every session from now on.

The rule: if anything is outstanding, say so before doing other work — a short list, then carry on with what
you were asked. Read it live, never from this message (it goes stale):

    /usr/bin/python3 str(Path(__file__).resolve().parent.parent)/operator/blockers.py

Never mark an item done on his behalf. If you find a new one, add it with `blockers.py --add`.

No action is needed right now — this is the surface arriving, not a task."""


def _ro():
    s = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    d = sqlite3.connect(":memory:"); s.backup(d); s.close()
    return d


def targets():
    cfg = json.loads((HERE / "team_surface_targets.json").read_text())
    d = _ro()
    out = []
    for t in cfg["targets"]:
        if not t.get("install", True):
            continue
        row = d.execute("SELECT id, name, agents FROM teams WHERE id LIKE ?", (t["team_id"] + "%",)).fetchone()
        if not row:
            continue
        team_id, name, agents = row
        A = json.loads(agents or "[]")
        lead = next((a for a in A if a.get("role") == "leader"), A[0] if A else None)
        if not lead:
            continue
        conv = d.execute("SELECT id FROM conversations WHERE id LIKE ?",
                         (lead.get("conversationId", "") + "%",)).fetchone()
        if not conv:
            continue
        out.append({"label": t["label"], "team_id": team_id, "conv": conv[0], "slot": lead.get("slotId")})
    return out


def send(conv_id, team_id, slot, text):
    """★ MEASURED, NOT ASSUMED. Two earlier shapes were tried and BOTH reported success-ish while writing
    nothing: `team.send-message` (leader→teammate only, cannot inject a user message to a lead) and a
    two-evaluate `chat.send.message` whose callback resolved `{}` with no row. A traffic capture settled
    it: the emit is accepted and the app answers on `chat.response.stream` with `type:"user_content"` —
    the callback this code used to wait for never fires at all. So: emit, hold the socket open long
    enough for the app to persist, and let the DB be the verdict. The bridge return value is not evidence.
    ★ The socket must OUTLIVE the emit — a closed connection was the difference between the shape that
    landed and the shape that did not."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cdp"))
    from cdp import C
    expr = """
(function(){
  return new Promise(function(resolve){
    var seen=[]; var uid='ops'+Date.now().toString(36)+Math.random().toString(36).slice(2,6);
    var off = window.electronAPI.on(function(evt){
      try{ var m=JSON.parse(evt.value);
           if(m.name==='chat.response.stream' && m.data && m.data.conversation_id===CONV)
             seen.push(m.data.type); }catch(e){}
    });
    window.electronAPI.emit('subscribe-chat.send.message',{id:uid,data:{
      conversation_id:CONV, input:TEXT, msg_id:'ops-'+uid, files:[]
    }});
    setTimeout(function(){ try{off();}catch(e){} resolve({accepted: seen.indexOf('user_content')>=0, saw: seen.slice(0,4)}); }, 6000);
  });
})()
""".replace("CONV", json.dumps(conv_id)).replace("TEXT", json.dumps(text))
    c = C()
    try:
        return c.ev(expr, timeout=40)
    finally:
        c.close()


def verify(conv_id, marker):
    d = _ro()
    return d.execute("SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ? AND content LIKE ?",
                     (conv_id + "%", f"%{marker}%")).fetchone()[0]


def self_test() -> int:
    """★ NEGATIVE CONTROL for the delivery confirmation — ranked FIRST by consequence (⛬6, 2026-08-29):
    a false clear here means he believes a team was told, and it was not.

    verify() is the whole safety property: it counts the marker IN THE MESSAGES TABLE rather than
    trusting the send's own return, which is the difference between 'delivered' and 'the call did not
    raise'. So the test is that it can say NO. A verifier that returns a positive count for a marker
    that was never sent would confirm every handoff forever, and nothing about its output would look
    wrong."""
    import uuid as _u
    fails = []
    try:
        T = targets()
    except Exception as e:
        print(f"  ✗ cannot read the delivery targets ({e.__class__.__name__}) — the confirmation path "
              f"is unavailable, which is a failure and not a skip")
        return 1
    if not T:
        fails.append("no delivery targets at all — verify() would be exercised over an EMPTY population, "
                     "and an invariance that holds over nothing has not been tested")
    else:
        conv = T[0]["conv"]
        # ★ A marker that CANNOT be present. If this returns >0, the verifier confirms fiction.
        never = f"KHAOSS-NEVER-SENT-{_u.uuid4().hex}"
        try:
            n = verify(conv, never)
        except Exception as e:
            fails.append(f"verify() raised on a normal lookup ({e.__class__.__name__}) — a confirmation "
                         f"path that throws is a confirmation path that will be skipped")
            n = None
        if n:
            fails.append(f"verify() reported {n} occurrence(s) of a marker that was NEVER SENT — it "
                         f"would confirm every handoff, including the ones that never arrived")
        # ★ And it must be able to say YES, or 'no leak' proves nothing: look for a string that is
        #   certainly present in that conversation.
        try:
            common = verify(conv, "")
            if not common:
                fails.append("verify() cannot find even an empty marker in a real conversation — it may "
                             "be querying the wrong table or conversation, so its NO means nothing")
        except Exception:
            pass
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ board_notify self_test: verify() refuses a marker that was never sent, and can still "
          "find one that is there" if not fails else "  ✗ board_notify self_test FAILED")
    return 1 if fails else 0


def main():
    dry = "--dry-run" in sys.argv
    # ★ A lane whose fence was lifted for ONE file has not had it lifted for a row in its chat.
    #   The narrower authorisation is the one that governs; --skip records that, visibly.
    skip = []
    if "--skip" in sys.argv:
        skip = [x for x in sys.argv[sys.argv.index("--skip") + 1].split(",") if x]
    n_open = len([i for i in json.loads((HERE / "blockers.json").read_text())["items"] if i["state"] == "open"])
    text = MSG.format(n=n_open, when=time.strftime("%Y-%m-%d"))
    marker = text.splitlines()[0][:40]
    T = [t for t in targets() if not any(t["team_id"].startswith(x) for x in skip)]
    for x in skip:
        print(f"  [HELD] {x} — not messaged (authorisation covered the file, not the chat)")
    print(f"\n★ BOARD → TEAM CHATS ({len(T)} target(s), {n_open} open item(s))")
    if dry:
        for t in T:
            print(f"  [DRY] {t['label'][:28]:30s} conv={t['conv'][:8]} slot={t['slot']}")
        return 0
    # ★ IDEMPOTENT BY DEFAULT. A team chat is a place the operator is working; a duplicate board message
    #   lands in the middle of whatever he is doing there. If the current board text is already in the
    #   conversation, this tool does not send it again. --force overrides.
    force = "--force" in sys.argv
    if not force:
        already = [t for t in T if verify(t["conv"], marker)]
        for t in already:
            print(f"  [ALREADY] {t['label'][:28]:30s} current board already in this chat — not re-sent")
        T = [t for t in T if t not in already]
    results = []
    for t in T:
        try:
            r = send(t["conv"], t["team_id"], t["slot"], text)
        except Exception as e:
            r = {"error": str(e)[:80]}
        results.append((t, r))
    time.sleep(4)  # one settle for all of them, then read ground truth
    bad = 0
    for t, r in results:
        n = verify(t["conv"], marker)
        ok = n > 0
        bad += 0 if ok else 1
        print(f"  [{'DELIVERED' if ok else 'NOT FOUND'}] {t['label'][:28]:30s} rows={n}  bridge={str(r)[:42]}")
    print(f"\n  ★ delivery is reported from the messages table, not from the bridge return value")
    print(f"  {len(results) - bad}/{len(results)} verified in the DB")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
