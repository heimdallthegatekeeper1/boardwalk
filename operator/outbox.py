#!/usr/bin/env python3
"""outbox.py — what he sent from the board, whether it landed, and what came back.

★★★ THE GAP HE NAMED (2026-09-03): "I need to be able to see what I'm writing go through there."
    He sent a message to the example team from the board and it did not arrive. There was no way to tell —
    the panel showed a one-line status that vanished on the next render, kept no record, and could not
    show a reply. So a send was an act of faith: he wrote, something flashed, and then nothing.
    ★ A surface that lets you speak but not see whether you were heard is worse than one that does not
      let you speak, because it produces confident silence on both sides.

★★ THREE FACTS PER SEND, AND THEY ARE DIFFERENT:
      WRITTEN   — the row exists in the destination conversation. Storage.
      LANDED    — confirmed at the destination by counting rows before and after. Not the same thing.
      ANSWERED  — something came back AFTER it. This is the only one that means he was heard, and it is
                  the one nothing was tracking.

★ THE THREAD IS READ LIVE FROM THE DESTINATION, never cached here. A copy of a conversation is stale the
  moment the other side speaks, and this board has been bitten by cached counts twice already.
"""
from __future__ import annotations
import json, sqlite3, sys, time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "outbox.jsonl"
DB = Path.home() / ".config/AionUi-Dev/aionui/aionui.db"


def record(entry: dict) -> dict:
    row = {"at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
    try:
        with LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass
    return row


def _thread(conv: str, limit: int = 8) -> list:
    """The tail of a conversation, read LIVE. Types only we can render; nothing invented."""
    if not DB.exists() or not conv:
        return []
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = c.execute("""SELECT type, content, created_at FROM messages
                            WHERE conversation_id LIKE ? ORDER BY created_at DESC LIMIT ?""",
                         (conv + "%", limit)).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for typ, content, ts in reversed(rows):
        text = ""
        try:
            d = json.loads(content)
            text = d.get("content") or d.get("text") or ""
            if not text and isinstance(d.get("update"), dict):
                text = d["update"].get("title") or ""
        except Exception:
            text = str(content)
        if not isinstance(text, str):
            text = json.dumps(text)
        out.append({"type": typ, "at": datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S"),
                    "text": " ".join(text.split())[:400]})
    return out


def sent(limit: int = 12) -> list:
    if not LOG.exists():
        return []
    rows = []
    for line in LOG.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    rows = rows[-limit:]
    # ★ ANSWERED is computed FRESH, never stored. Whether he has been answered changes after the send,
    #   so a stored flag would be wrong from the moment it was written.
    for r in rows:
        conv = r.get("conversation")
        r["thread"] = _thread(conv, 6) if conv else []
        sent_ms = 0
        try:
            sent_ms = int(datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000)
        except Exception:
            pass
        after = 0
        if conv and DB.exists() and sent_ms:
            try:
                c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
                after = c.execute("""SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ?
                                     AND created_at > ? AND type IN ('text','thinking','acp_tool_call')""",
                                  (conv + "%", sent_ms + 1500)).fetchone()[0]
                c.close()
            except Exception:
                after = 0
        r["answered"] = after > 0
        r["rows_since"] = after
        r["verdict"] = ("answered — the other side produced work after this"
                        if after > 0 else
                        "landed, but NOTHING has come back yet — delivered is not the same as read")
    return list(reversed(rows))


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is calling something ANSWERED when nothing came
    back — that is the confident silence this file exists to end."""
    fails = []
    import tempfile
    global LOG
    saved = LOG
    try:
        LOG = Path(tempfile.mkdtemp()) / "o.jsonl"
        record({"target": "x", "target_label": "nowhere", "conversation": "nosuchconv0000",
                "delivery": "live", "text": "probe"})
        s = sent()
        if not s:
            fails.append("a recorded send did not come back from sent()")
        elif s[0]["answered"]:
            fails.append("★ a send into a conversation that cannot exist was reported ANSWERED — the "
                         "check cannot say no, so its yes means nothing")
        elif "NOTHING has come back" not in s[0]["verdict"]:
            fails.append("an unanswered send does not say plainly that delivered is not read")
        # and it must be able to say YES
        real = None
        if DB.exists():
            try:
                c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
                real = c.execute("""SELECT conversation_id FROM messages
                                    GROUP BY conversation_id ORDER BY COUNT(*) DESC LIMIT 1""").fetchone()[0]
                c.close()
            except Exception:
                real = None
        if real:
            record({"target": "y", "target_label": "busy", "conversation": real[:8],
                    "delivery": "live", "text": "probe", "at_override": True})
            # backdate it so existing rows count as "after"
            rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
            rows[-1]["at"] = "2020-01-01T00:00:00+0000"
            LOG.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            s2 = sent()
            if not any(x["answered"] for x in s2):
                fails.append("a send into a busy conversation with later activity was not reported "
                             "answered — the check can only say no, which is equally useless")
    finally:
        LOG = saved
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ outbox self_test: an unanswered send is never called answered, and a conversation with "
          "later activity is" if not fails else "  ✗ outbox self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    rows = sent()
    if not rows:
        print("\n  nothing sent from the board yet.")
        return 0
    print(f"\n  WHAT YOU SENT — {len(rows)} most recent")
    for r in rows:
        mark = "✓" if r.get("answered") else "·"
        print(f"\n  {mark} {r['at'][11:19]}  → {r.get('target_label') or r.get('target')}  [{r.get('delivery')}]")
        print(f"      {str(r.get('text',''))[:110]}")
        print(f"      {r['verdict']}")
        for t in r.get("thread", [])[-3:]:
            print(f"        {t['at']} [{t['type'][:9]}] {t['text'][:96]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
