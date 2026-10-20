#!/usr/bin/env python3
"""item_binding.py — attach ONE agent to a board item, detach it at any time, and leave behind a brief
that makes the next agent useful on its first turn.

★★★ WHY THE BRIEF MATTERS MORE THAN THE BINDING. Assigning work to an agent is easy; the failure is
    that the agent arrives with nothing. It reads a one-line title, infers the rest, and starts from a
    hypothesis — which is how a lane spends a hundred turns rewriting a working pipeline because
    nobody told it which interpreter to use. So binding WRITES A FILE: what the item is, WHERE IT CAME
    FROM concretely enough to go and look, what it costs to leave, what has already been tried, and
    what would count as done. An agent that reads it starts from the record instead of from a guess.

★★ UNBINDING IS ALWAYS AVAILABLE AND NEVER DESTRUCTIVE. He can detach and hand the same item to a
    different agent whenever he likes; the brief stays, the history accumulates, and the next agent can
    see that someone was here before and what they were told. A handoff that erases its predecessor is
    how the same wrong assumption gets made twice.

★ ONE AGENT AT A TIME, ON PURPOSE. Two agents on one item is how two half-fixes land and neither
    reports the other. Re-binding replaces the holder and says so in the history rather than silently
    adding a second.
"""
from __future__ import annotations
import json, re, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE = HERE / "item_bindings.json"
BRIEFS = HERE / "items"


def _safe(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(item_id))[:80].strip("-") or "item"


def load() -> dict:
    if STORE.exists():
        try:
            return json.loads(STORE.read_text())
        except Exception:
            pass
    return {"doc": "Which agent holds which board item. One at a time. Unbinding is always allowed.",
            "bindings": {}}


def save(d: dict) -> None:
    STORE.write_text(json.dumps(d, indent=2) + "\n")


def _item(item_id: str):
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    for i in b.items_of(b.load()):
        if i["id"] == item_id:
            return i
    return None


def brief_path(item_id: str) -> Path:
    return BRIEFS / f"{_safe(item_id)}.md"


def write_brief(item: dict, binding: dict | None, history: list) -> Path:
    """★ The context a NEW agent needs, and nothing it can get for itself.
    Deliberately NOT a summary of the item — it is the item plus its provenance plus what has already
    been done, because the three things a fresh agent gets wrong are: where this came from, what has
    already been tried, and who is allowed to close it."""
    BRIEFS.mkdir(exist_ok=True)
    o = item.get("origin") or {}
    p = brief_path(item["id"])
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %z")
    lines = [
        f"# {item.get('title','(untitled)')}",
        "",
        f"**Board id:** `{item['id']}`  ·  **Priority:** {item.get('priority_label','?')}  ·  "
        f"**Source:** {item.get('source','hand')}"
        + (f"  ·  **Lane:** {item['lane']}" if item.get("lane") and item["lane"] != "unknown" else ""),
        f"*Brief regenerated {now}. The board is the live record; this file is a snapshot for whoever "
        f"picks the item up.*",
        "",
        "## Read this first — you are picking this up cold",
        "",
        "- **You cannot close this item.** Only the operator can. If you believe it is resolved, say why "
        "and leave it open. An agent closing its own assignment is how a board becomes decoration.",
        "- **Do not re-derive what is below.** It was measured, not guessed. If you disagree with a fact "
        "here, re-measure it and say so explicitly rather than quietly acting on a different belief.",
        "- **If you cannot read something, say UNREADABLE — never 'none'.** Absence of access is not "
        "absence of content.",
        "",
        "## Where this came from",
        "",
        f"- **What raised it:** {o.get('what','(not recorded)')}",
        f"- **Look here:** `{o.get('where','(not recorded)')}`"
        + (f"  ·  row `{o['row']}`" if o.get("row") else ""),
        f"- **Raised by:** {o.get('raised_by','(not recorded)')}",
    ]
    if o.get("as_of"):
        lines.append(f"- **As of:** {o['as_of']}")
    if o.get("your_words"):
        lines += ["", f"> **The operator's own words:** {o['your_words']}"]
    lines += [
        f"- **How it clears:** {o.get('how_to_clear','(not recorded)')}",
        "",
        "## What it is",
        "",
        item.get("one", "(no description recorded)"),
    ]
    if item.get("what"):
        lines += ["", "### The longer form", "", item["what"]]
    lines += [
        "",
        "## What it costs to leave alone",
        "",
        item.get("blocks") or "*(not recorded — if you find out, add it to the row.)*",
        "",
        "## What would count as done",
        "",
        (item.get("todo") or
         "*Not written down.* ★ That is itself worth reporting: an item nobody has defined 'done' for "
         "cannot be finished, only abandoned. Propose a definition and put it in front of the operator "
         "rather than inventing one and acting on it."),
        "",
        "## Who has held this",
        "",
    ]
    if binding:
        lines.append(f"- **NOW:** {binding['target_label']} (`{binding['target']}`) — "
                     f"bound {binding['at'][:16]} by {binding.get('by','the operator')}")
    else:
        lines.append("- **Nobody is holding this right now.** It is unassigned.")
    for h in history[-8:]:
        lines.append(f"- {h['at'][:16]} · {h['action']} · {h.get('target_label') or h.get('target') or ''}"
                     + (f" — {h['why']}" if h.get("why") else ""))
    if item.get("note"):
        lines += ["", "## The operator's note on this item", "",
                  "> " + item["note"].replace("\n", "\n> "),
                  "", "*His notes are instructions, not commentary — they are him talking to whoever holds "
                  "this. If one contains a question, answer it where he will see it.*"]
    lines += [
        "",
        "---",
        "",
        "*Generated by `operator/item_binding.py`. Regenerated on every bind, unbind and note change — "
        "if this file disagrees with the board, the board is right and this is stale.*",
        "",
    ]
    p.write_text("\n".join(lines))
    return p


def bind(item_id: str, target: str, target_label: str = "", by: str = "operator") -> dict:
    item = _item(item_id)
    if not item:
        return {"ok": False, "error": f"no board item with id {item_id!r} — refusing to bind to "
                                      f"something that does not exist"}
    d = load()
    rec = d["bindings"].get(item_id) or {"history": []}
    prev = rec.get("current")
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    if prev:
        rec["history"].append({"at": now, "action": "replaced",
                               "target": prev["target"], "target_label": prev.get("target_label"),
                               "why": f"re-bound to {target_label or target}"})
    rec["current"] = {"target": target, "target_label": target_label or target, "at": now, "by": by}
    rec["history"].append({"at": now, "action": "bound", "target": target,
                           "target_label": target_label or target})
    d["bindings"][item_id] = rec
    save(d)
    p = write_brief(item, rec["current"], rec["history"])
    return {"ok": True, "item": item_id, "target": target, "brief": str(p),
            "replaced": prev["target_label"] if prev else None,
            "note": f"brief written to {p.name} — the agent that picks this up starts from the record"}


def unbind(item_id: str, why: str = "", by: str = "operator") -> dict:
    d = load()
    rec = d["bindings"].get(item_id)
    if not rec or not rec.get("current"):
        return {"ok": False, "error": "nothing is bound to that item"}
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    prev = rec["current"]
    rec["history"].append({"at": now, "action": "unbound", "target": prev["target"],
                           "target_label": prev.get("target_label"), "why": why or "no reason given"})
    rec["current"] = None
    d["bindings"][item_id] = rec
    save(d)
    item = _item(item_id)
    p = write_brief(item, None, rec["history"]) if item else None
    return {"ok": True, "item": item_id, "released": prev.get("target_label"),
            "brief": str(p) if p else None,
            "note": "released. The brief is KEPT — the next agent can see who was here and what they "
                    "were told, which is how the same wrong assumption stops being made twice."}


def bindings() -> dict:
    d = load()
    return {k: v.get("current") for k, v in d.get("bindings", {}).items() if v.get("current")}


def self_test() -> int:
    """★ NEGATIVE CONTROL. Two failures matter: binding to an item that does not exist (a handoff into
    the void, reported as success) and an unbind that does not actually release."""
    fails = []
    r = bind("__no_such_item__", "cc:1", "nowhere")
    if r.get("ok"):
        fails.append("bound an item that does not exist — a handoff into the void reported as success")
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    live = [i for i in b.items_of(b.load()) if i.get("state") == "open"]
    if not live:
        fails.append("no open items — the test ran over an EMPTY population and proves nothing")
    else:
        tid = live[0]["id"]
        was = load().get("bindings", {}).get(tid, {}).get("current")
        r1 = bind(tid, "__selftest__", "self test")
        if not r1.get("ok") or not Path(r1["brief"]).exists():
            fails.append("binding did not write a brief — an agent would arrive with nothing")
        else:
            txt = Path(r1["brief"]).read_text()
            for must in ("Where this came from", "You cannot close this item", "What would count as done"):
                if must not in txt:
                    fails.append(f"the brief omits '{must}' — the section a cold agent most needs")
        if bindings().get(tid, {}).get("target") != "__selftest__":
            fails.append("the binding was not recorded")
        r2 = unbind(tid, why="self test cleanup")
        if not r2.get("ok") or bindings().get(tid):
            fails.append("unbind did not release the item — he could not hand it to someone else")
        if r1.get("ok") and not Path(r1["brief"]).exists():
            fails.append("the brief was deleted on unbind — history a later agent needs was destroyed")
        # restore whatever was there before
        d = load()
        if was:
            d["bindings"][tid]["current"] = was
        else:
            d["bindings"].pop(tid, None)
        save(d)
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ item_binding self_test: refuses a nonexistent item, writes a brief with the sections a "
          "cold agent needs, releases on unbind, and keeps the brief"
          if not fails else "  ✗ item_binding self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--bind" and len(a) >= 3:
        print(json.dumps(bind(a[1], a[2], " ".join(a[3:])), indent=2)); return 0
    if a and a[0] == "--unbind" and len(a) >= 2:
        print(json.dumps(unbind(a[1], " ".join(a[2:])), indent=2)); return 0
    b = bindings()
    print(f"\n  BOUND ITEMS ({len(b)})")
    for k, v in b.items():
        print(f"    {k:38s} → {v['target_label']}  ({v['at'][:16]})")
    if not b:
        print("    nothing is bound. Every item is unassigned.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
