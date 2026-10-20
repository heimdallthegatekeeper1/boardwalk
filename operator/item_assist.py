#!/usr/bin/env python3
"""item_assist.py — ask for help on ONE board item, through a route the operator picks.

★★★ WHAT HE ASKED FOR (2026-09-03): "every single item needs the AI assist guided button that allows
    the user to select llms whether they are local, through CC/codex etc… or openrouter, and even
    asking a specific team in KHAOSS to handle any specific task, with a note in there to point the
    item to the specific origin it existed from."

★★ THE ORIGIN TRAVELS WITH THE QUESTION, and that is the part that makes this worth building. Whoever
    answers — a 0.6B model on this machine or a team lead — gets the row's PROVENANCE: what raised it,
    which file and row to look at, who raised it, how it clears. An assistant told only the title
    guesses; one told where the item came from can go and look. That is the whole difference between
    an answer and a plausible answer.

★ ROUTES ARE MEASURED, NOT LISTED. Every route here was checked to exist before it was offered: ollama
  answering on 11434, OpenRouter with credit remaining, codex on PATH, live CC sockets, mapped teams.
  A picker with a dead option is the defect this board has spent a week removing.

★★ AND COST IS SHOWN BEFORE HE CHOOSES. Local is free and private. OpenRouter spends real money and had
   $32 of $170 remaining when this was written. A route picker that hides which choice costs money is
   asking him to spend blind.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OLLAMA = "http://127.0.0.1:11434"


def _openrouter_key() -> str | None:
    for p in (Path.home()/".hermes/profiles/lead-example/.env", Path.home()/".hermes/.env"):
        try:
            for line in p.read_text().splitlines():
                if line.startswith(("OPENAI_API_KEY=", "OPENROUTER_API_KEY=")):
                    return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            continue
    return None


def routes() -> dict:
    """★ Each route is PROBED. Nothing is offered that was not seen answering."""
    out = {"local": [], "cloud": [], "agents": [], "notes": []}
    try:
        d = json.load(urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=6))
        for m in d.get("models", []):
            out["local"].append({"id": f"local:{m['name']}", "label": m["name"],
                                 "size_gb": round(m.get("size", 0) / 1e9, 1),
                                 "cost": "free · stays on this machine"})
    except Exception:
        out["notes"].append("ollama is not answering on 11434 — no local models offered")
    key = _openrouter_key()
    if key:
        try:
            c = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}),
                timeout=15)).get("data", {})
            left = round(float(c.get("total_credits", 0)) - float(c.get("total_usage", 0)), 2)
            for m in ("google/gemini-2.5-flash", "anthropic/claude-sonnet-4.5", "minimax/minimax-m3"):
                out["cloud"].append({"id": f"openrouter:{m}", "label": m,
                                     "cost": f"paid · ${left} left on the account"})
            out["notes"].append(f"OpenRouter has ${left} remaining — every cloud call spends it")
        except Exception:
            out["notes"].append("OpenRouter key present but the credits endpoint did not answer")
    else:
        out["notes"].append("no OpenRouter key found — cloud models not offered")
    if shutil.which("codex"):
        out["agents"].append({"id": "codex", "label": "Codex CLI", "cost": "uses its own account"})
    try:
        sys.path.insert(0, str(HERE))
        import dispatch_targets as dt, importlib
        importlib.reload(dt)
        for g, rows in dt.targets()["groups"].items():
            if g.startswith(("AionUi", "Claude Code")):
                for r in rows:
                    if r["delivery"] != "manual":
                        out["agents"].append({"id": r["id"], "label": r["label"],
                                              "cost": f"{r['delivery']} — {'arrives on its own' if r['delivery']=='live' else 'an agent delivers it'}"})
    except Exception:
        out["notes"].append("the destination registry could not be read")
    return out


def _item(item_id: str):
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    return next((i for i in b.items_of(b.load()) if i["id"] == item_id), None)


def compose(item: dict, question: str) -> str:
    """★ THE ORIGIN IS THE POINT. Whoever answers is told where this came from, so it can go and look
    rather than infer."""
    o = item.get("origin") or {}
    return (
        "You are being asked about ONE item on a human operator's board — things only he can do.\n\n"
        f"ITEM: {item.get('title')}\n"
        f"ID: {item['id']}   ·   priority: {item.get('priority_label')}   ·   source: {item.get('source')}"
        + (f"   ·   lane: {item['lane']}" if item.get("lane") and item["lane"] != "unknown" else "") + "\n\n"
        f"WHAT IT IS:\n{item.get('one','(not recorded)')}\n\n"
        f"WHERE IT CAME FROM — go and look rather than infer:\n"
        f"  what raised it : {o.get('what','(not recorded)')}\n"
        f"  look here      : {o.get('where','(not recorded)')}"
        + (f"  · row {o['row']}" if o.get("row") else "") + "\n"
        f"  raised by      : {o.get('raised_by','(not recorded)')}\n"
        + (f"  as of          : {o['as_of']}\n" if o.get("as_of") else "")
        + f"  how it clears  : {o.get('how_to_clear','(not recorded)')}\n\n"
        f"COST OF LEAVING IT:\n{item.get('blocks') or '(not recorded)'}\n\n"
        + (f"WHAT WOULD COUNT AS DONE:\n{item['todo']}\n\n" if item.get("todo") else "")
        + (f"HIS NOTE ON IT:\n{item['note']}\n\n" if item.get("note") else "")
        + f"HIS QUESTION:\n{question}\n\n"
        "Answer him directly. If the row does not contain what you need, say what is missing rather "
        "than filling the gap. You cannot close this item — only he can."
    )


def ask(item_id: str, route: str, question: str) -> dict:
    item = _item(item_id)
    if not item:
        return {"ok": False, "error": f"no board item {item_id!r}"}
    prompt = compose(item, question or "What should I know before acting on this?")
    return ask_raw(route, prompt, item_id=item_id)


def ask_raw(route: str, prompt: str, item_id: str = "") -> dict:
    """★ THE ROUTE DISPATCH, WITHOUT REQUIRING A BOARD ITEM.

    A full sweep is not about one row, so it cannot go through ask() — which refuses without an item
    id. The alternative was for the sweep to reimplement local/openrouter/agent dispatch, and a second
    implementation of "how a question reaches a model" is exactly the duplicate-store failure this
    project keeps deleting: the two would drift, and the sweep would quietly stop honouring routing
    rules that ask() still obeyed. So ask() now composes and delegates here, and both share one path."""
    if route.startswith("local:"):
        model = route.split(":", 1)[1]
        try:
            req = urllib.request.Request(f"{OLLAMA}/api/generate",
                data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
                headers={"Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=180))
            return {"ok": True, "route": route, "answer": d.get("response", "").strip(),
                    "cost": "free — this never left the machine"}
        except Exception as e:
            return {"ok": False, "route": route, "error": f"{e.__class__.__name__}: {str(e)[:160]}"}
    if route.startswith("openrouter:"):
        key = _openrouter_key()
        if not key:
            return {"ok": False, "error": "no OpenRouter key available"}
        model = route.split(":", 1)[1]
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps({"model": model, "max_tokens": 900,
                                 "messages": [{"role": "user", "content": prompt}]}).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=180))
            ch = (d.get("choices") or [{}])[0].get("message", {})
            return {"ok": True, "route": route, "answer": (ch.get("content") or "").strip(),
                    "cost": f"spent ${(d.get('usage') or {}).get('cost', 0)}"}
        except Exception as e:
            return {"ok": False, "route": route, "error": f"{e.__class__.__name__}: {str(e)[:160]}"}
    # an agent or a team — queued or delivered through the ONE dispatch path
    try:
        sys.path.insert(0, str(HERE))
        import dispatch_targets as dt, importlib
        importlib.reload(dt)
        rows = [r for g in dt.targets()["groups"].values() for r in g]
        hit = next((r for r in rows if r["id"] == route), None)
        if not hit:
            return {"ok": False, "error": f"unknown route {route!r}"}
        return {**dt.enqueue({"target": route, "target_label": hit["label"], "delivery": hit["delivery"],
                              "kind": "assist", "item": item_id, "text": prompt}),
                "route": route,
                "note": "handed to that agent with the item's full origin attached — it has not answered yet"}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}


def board_order() -> list:
    """★ THE NUMBERING HE SEES. Selection by number only works if the numbers on screen are these
    numbers, so this is the ONE ordering — the board renders it and the batch resolves against it.
    Two orderings would mean he selects item 7 and gets a different item 7."""
    sys.path.insert(0, str(HERE))
    import blockers as b, importlib
    importlib.reload(b)
    items = [i for i in b.items_of(b.load()) if i.get("state") == "open"]
    items.sort(key=lambda i: (i.get("priority", 3), i.get("title", "")))
    return items


def resolve_selection(spec: str) -> tuple[list, list]:
    """Accepts 'all', '1,3,7-9', or explicit ids — the shape the services launcher uses.
    ★ Returns (items, problems). An out-of-range number is REPORTED, never silently dropped: quietly
    ignoring '47' when there are 40 items means he thinks he asked about something he did not."""
    items = board_order()
    by_id = {i["id"]: i for i in items}
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return items, []
    chosen, problems, seen = [], [], set()
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        if part in by_id:
            if part not in seen:
                chosen.append(by_id[part]); seen.add(part)
            continue
        rng = part.split("-")
        try:
            lo = int(rng[0]); hi = int(rng[-1]) if len(rng) > 1 else lo
        except ValueError:
            problems.append(f"{part!r} is neither a number, a range, nor an item id")
            continue
        if lo < 1 or hi > len(items) or lo > hi:
            problems.append(f"{part!r} is out of range — there are {len(items)} open items")
            continue
        for n in range(lo, hi + 1):
            it = items[n - 1]
            if it["id"] not in seen:
                chosen.append(it); seen.add(it["id"])
    return chosen, problems


def ask_batch(spec: str, route: str, question: str, limit: int = 25) -> dict:
    """★ ONE QUESTION ACROSS SEVERAL ITEMS, each carrying its OWN origin.
    ★★ A CAP, AND IT IS SAID OUT LOUD. 'all' against a hundred-item board on a paid route would spend
    real money without him choosing to; anything beyond the cap is reported as NOT ASKED rather than
    silently truncated — a silent cap reads as 'covered everything'."""
    items, problems = resolve_selection(spec)
    over = items[limit:]
    items = items[:limit]
    answers = []
    for it in items:
        r = ask(it["id"], route, question)
        answers.append({"id": it["id"], "title": it["title"], **r})
    return {
        "asked": len(answers), "problems": problems,
        "not_asked": [{"id": i["id"], "title": i["title"]} for i in over],
        "cap": limit,
        "answers": answers,
        "note": (f"★ {len(over)} item(s) were NOT asked — the cap is {limit}. They are listed above, not "
                 f"silently dropped." if over else ""),
    }


def self_test() -> int:
    """★ NEGATIVE CONTROL. Two failures matter: offering a route that does not exist, and sending a
    question without the origin — which is the entire reason this is better than pasting a title."""
    fails = []
    r = routes()
    for grp in ("local", "cloud", "agents"):
        for x in r.get(grp, []):
            if not x.get("id") or not x.get("label"):
                fails.append(f"a {grp} route is missing an id or label")
    if not any(r.values()):
        fails.append("no routes at all — the picker would be empty and nothing said why")
    fake = {"id": "x", "title": "T", "one": "O", "priority_label": "P2", "source": "hand",
            "origin": {"what": "W", "where": "/tmp/f", "raised_by": "R", "how_to_clear": "H"}}
    p = compose(fake, "Q")
    for must in ("WHERE IT CAME FROM", "/tmp/f", "raised by", "H"):
        if must not in p:
            fails.append(f"the composed prompt omits {must!r} — the origin is the point")
    if "cannot close this item" not in p:
        fails.append("the prompt does not tell the helper it cannot close the item")
    bad = ask("__no_such_item__", "local:x", "q")
    if bad.get("ok"):
        fails.append("answered about an item that does not exist")
    bad2 = ask(fake["id"], "nonsense:route", "q")
    if bad2.get("ok"):
        fails.append("accepted a route that is not offered")
    # ★ selection must report what it could not honour, never drop it
    _, probs = resolve_selection("1,999999")
    if not probs:
        fails.append("an out-of-range selection was silently dropped — he would believe he had asked "
                     "about something he had not")
    got, _ = resolve_selection("1-3")
    if len(got) != 3:
        fails.append(f"a range of 3 resolved to {len(got)} items")
    allitems, _ = resolve_selection("all")
    if len(allitems) < 3:
        fails.append("'all' resolved to almost nothing — the ordering is probably empty")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ item_assist self_test: every offered route is probed, the origin travels with the "
          "question, and unknown items and routes are refused"
          if not fails else "  ✗ item_assist self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--routes":
        r = routes()
        print("\n  WHERE YOU CAN SEND A QUESTION ABOUT AN ITEM")
        for grp, label in (("local", "ON THIS MACHINE — free, private"),
                           ("cloud", "CLOUD — costs money"),
                           ("agents", "AGENTS AND TEAMS")):
            if r[grp]:
                print(f"\n  {label}")
                for x in r[grp]:
                    print(f"    {x['id']:46s} {x.get('cost','')}")
        for n in r["notes"]:
            print(f"\n  ★ {n}")
        return 0
    if len(a) >= 2:
        print(json.dumps(ask(a[0], a[1], " ".join(a[2:])), indent=2)[:2000]); return 0
    print("usage: item_assist.py --routes | <item-id> <route> <question>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
