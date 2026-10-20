#!/usr/bin/env python3
"""completion_log.py — when something leaves the board, write down WHY it left.

★★★ THE PROBLEM, WITH TODAY'S EXAMPLE. Rows leave this board in four different ways and, until now,
    all four looked identical: the count went down and the row was gone. Today FOURTEEN roadmap rows
    disappeared because a new ROADMAP file was added whose format the parser could not read. Nothing
    was completed. Nothing was recorded. Every surface agreed with itself, because they all read the
    same empty answer.
    ★ So "the board got shorter" is not good news until you know which kind of shorter it was.

★★ FOUR KINDS OF LEAVING, and only two of them are completions:
      settled            he ruled on it — a real completion, and the only one he authored
      source_cleared     a derived row's condition stopped being true (an invariant went green) —
                         a real completion that nobody had to type
      source_degraded    the SOURCE broke or went stale, so the row is merely hidden. NOT done.
      vanished           it left and nothing explains it. The loudest case, and the one today.

★ THE ARCHIVE IS APPEND-ONLY AND SEPARATE FROM THE BOARD. Packing completed work away is the point —
  a board that keeps everything it ever held is unreadable — but a completion that is deleted rather
  than filed is indistinguishable from work that was never done.
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path


def _ss():
    """★ One atomic+locked writer for every store in this directory. Each of these used
    write_text(json.dumps(...)), which truncates before it writes — a crash between those two moments
    left the store empty, and two writers clobbered each other undetectably."""
    import importlib, sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module("safe_store")
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "board_snapshot.json"
ARCHIVE = HERE / "completion_log.jsonl"
DIGEST = HERE / "COMPLETED_WORK.md"
PANEL = "http://127.0.0.1:7842"


def board() -> dict:
    with urlopen(f"{PANEL}/operator/board.json", timeout=120) as r:
        return json.load(r)


def _snapshot_of(d: dict) -> dict:
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "items": {i["id"]: {"title": i.get("title"), "source": i.get("source"),
                            "sev": i.get("sev"), "lane": i.get("lane"),
                            "state": i.get("state"), "priority": i.get("priority")}
                  for i in d.get("items", [])},
        "health": {h["source"]: h.get("status") for h in d.get("source_health", [])},
    }


def classify(prev: dict, now: dict) -> list:
    """What left, and why. ★ The health map is what separates 'done' from 'hidden'."""
    gone = []
    now_ids = set(now["items"])
    bad_health = {"unavailable", "error", "degraded", "stale", "absent", "empty"}
    for iid, meta in prev["items"].items():
        if iid in now_ids:
            # still here, but did its state change?
            cur = now["items"][iid]
            if meta.get("state") == "open" and cur.get("state") not in (None, "open"):
                # ★★★ WHO CLOSED IT IS NOT AN ASSUMPTION. The first version said "you ruled it done"
                #   for every state change — including four rows CC closed itself as factually false
                #   under an overnight dispatch he never saw. That put his name on decisions he did
                #   not make, in the file that exists to record what was decided. `decided_via`
                #   carries the channel; read it rather than guessing the actor.
                via = str(cur.get("decided_via") or "")
                by_him = "operator docket" in via or not via
                gone.append({**meta, "id": iid,
                             "kind": "settled" if by_him else "closed_by_agent",
                             "why": (f"you ruled it {cur.get('state')}" if by_him else
                                     f"closed by an agent as {cur.get('state')} — {via[:120]}"),
                             "closed_by": "operator" if by_him else "agent",
                             "via": via[:160],
                             "now_state": cur.get("state")})
            continue
        src = meta.get("source") or "hand"
        h_now = now["health"].get(src)
        if src == "hand":
            # a hand row that is simply absent was deleted from the store, not ruled on
            gone.append({**meta, "id": iid, "kind": "vanished",
                         "why": "a hand-written row is no longer in the store, and no ruling was "
                                "recorded for it. Deleted rather than settled."})
        elif h_now in bad_health:
            gone.append({**meta, "id": iid, "kind": "source_degraded",
                         "why": f"its source `{src}` is now `{h_now}` — this row is HIDDEN, not done. "
                                f"Whatever it was asking for is still true; nobody can see it."})
        elif h_now == "ok":
            gone.append({**meta, "id": iid, "kind": "source_cleared",
                         "why": f"`{src}` still reports fine and no longer lists it — the condition "
                                f"that raised it stopped being true. This one really is done."})
        else:
            gone.append({**meta, "id": iid, "kind": "vanished",
                         "why": f"it left and `{src}` reports `{h_now}` — nothing explains this."})
    return gone


def run(write: bool = True) -> dict:
    d = board()
    now = _snapshot_of(d)
    if not SNAPSHOT.exists():
        if write:
            _ss().write_json(SNAPSHOT, now)
        return {"first_run": True, "tracking": len(now["items"]),
                "note": ("first snapshot taken — there is nothing to compare against yet. This is not "
                         "'nothing completed', it is 'no previous state'.")}
    prev = json.loads(SNAPSHOT.read_text())
    events = classify(prev, now)
    if write:
        _ss().write_json(SNAPSHOT, now)
        if events:
            with ARCHIVE.open("a") as f:
                for e in events:
                    f.write(json.dumps({**e, "logged_at": now["at"],
                                        "since": prev.get("at")}) + "\n")
            _write_digest()
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return {"since": prev.get("at"), "at": now["at"], "events": events, "counts": counts,
            "board_now": len(now["items"]), "board_before": len(prev["items"]),
            "note": ("`source_degraded` and `vanished` are NOT completions — they are rows that "
                     "stopped being visible. Only `settled` and `source_cleared` are done.")}


def entries() -> list:
    if not ARCHIVE.exists():
        return []
    out = []
    for ln in ARCHIVE.read_text(errors="replace").splitlines():
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _write_digest() -> None:
    """★ A human-readable archive beside the machine one. The jsonl is for tools; this is the thing
    he can actually read, and the thing that survives this project."""
    rows = entries()
    done = [e for e in rows if e["kind"] in ("settled", "source_cleared")]
    agent_closed = [e for e in rows if e["kind"] == "closed_by_agent"]
    hidden = [e for e in rows if e["kind"] == "source_degraded"]
    lost = [e for e in rows if e["kind"] == "vanished"]
    L = ["# Completed work — packed away from the operator board", "",
         f"*{len(done)} completed by him · {len(agent_closed)} closed by an agent · "
         f"{len(hidden)} hidden by a broken source · {len(lost)} unexplained.*",
         "", "A row leaving the board is not the same as work being finished. Only the first section",
         "below is finished work.", ""]
    def sect(title, rs, note):
        L.append(f"## {title}")
        L.append("")
        L.append(note)
        L.append("")
        if not rs:
            L.append("*none*"); L.append(""); return
        for e in sorted(rs, key=lambda x: str(x.get("logged_at")), reverse=True):
            L.append(f"- **{e.get('title') or e['id']}**  ")
            L.append(f"  `{e['id']}` · {e.get('source','hand')}"
                     + (f" · {e.get('lane')}" if e.get("lane") and e["lane"] != "unknown" else "")
                     + f" · logged {e.get('logged_at')}  ")
            L.append(f"  {e.get('why','')}")
        L.append("")
    sect("Finished", done, "Ruled on by the operator, or the condition that raised it stopped being true.")
    sect("Closed by an agent — NOT his ruling", agent_closed,
         "An agent closed these, usually as factually false against the running system. He has not "
         "seen them. If any was closed wrongly, it is closed wrongly with nobody's authority behind it.")
    sect("Hidden, not finished", hidden,
         "These left the board because their SOURCE broke or went stale. Whatever they were asking "
         "for is probably still true — nobody can see it.")
    sect("Unexplained", lost,
         "These left and nothing accounts for it. Each one is a question, not an achievement.")
    DIGEST.write_text("\n".join(L))


def self_test() -> int:
    fails = []
    prev = {"at": "t0",
            "items": {"a": {"title": "A", "source": "hand", "state": "open"},
                      "b": {"title": "B", "source": "invariants", "state": "open"},
                      "c": {"title": "C", "source": "roadmap", "state": "open"},
                      "d": {"title": "D", "source": "hand", "state": "open"}},
            "health": {"invariants": "ok", "roadmap": "ok"}}
    now = {"at": "t1",
           "items": {"d": {"title": "D", "source": "hand", "state": "done"}},
           "health": {"invariants": "ok", "roadmap": "empty"}}
    got = {e["id"]: e["kind"] for e in classify(prev, now)}
    want = {"a": "vanished", "b": "source_cleared", "c": "source_degraded", "d": "settled"}
    for k, v in want.items():
        if got.get(k) != v:
            fails.append(f"{k}: expected {v}, got {got.get(k)}")
    # ★ an agent-closed row must never be attributed to him
    prev2={"at":"t0","items":{"z":{"title":"Z","source":"hand","state":"open"}},"health":{}}
    now2={"at":"t1","items":{"z":{"title":"Z","source":"hand","state":"done",
          "decided_via":"cc-khaoss-dev, overnight §3 — factual closure, not a ruling"}},"health":{}}
    ev=classify(prev2, now2)
    if ev and ev[0]["kind"] == "settled":
        fails.append("a row closed BY AN AGENT was recorded as the operator having ruled it — "
                     "that puts his name on a decision he did not make")
    if ev and ev[0].get("closed_by") != "agent":
        fails.append("agent closure not attributed to the agent")

    # ★ the control that matters: a broken source must NEVER read as completion
    if got.get("c") in ("settled", "source_cleared"):
        fails.append("a row hidden by a degraded source was counted as COMPLETED — the exact lie "
                     "this module exists to prevent")
    # nothing left = no events
    if classify(prev, prev):
        fails.append("comparing a snapshot to itself produced events")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  7 controls pass: settled / source_cleared / source_degraded / vanished each "
              "classified correctly · a degraded source never reads as completion · an "
              "AGENT closure is never attributed to him · no change produces no events")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    r = run(write="--dry-run" not in a)
    if "--json" in a:
        print(json.dumps(r, indent=2)); return 0
    if r.get("first_run"):
        print(f"\n  First snapshot taken — tracking {r['tracking']} item(s).")
        print(f"  {r['note']}\n"); return 0
    print(f"\n  SINCE {r['since']} — board {r['board_before']} → {r['board_now']}\n")
    if not r["events"]:
        print("  Nothing has left the board.\n"); return 0
    label = {"settled": "FINISHED — you ruled on it",
             "closed_by_agent": "CLOSED BY AN AGENT — not your ruling",
             "source_cleared": "FINISHED — the condition stopped being true",
             "source_degraded": "HIDDEN, NOT FINISHED — its source broke",
             "vanished": "UNEXPLAINED"}
    for kind in ("settled", "closed_by_agent", "source_cleared", "source_degraded", "vanished"):
        rs = [e for e in r["events"] if e["kind"] == kind]
        if not rs:
            continue
        print(f"  {label[kind]}  ({len(rs)})")
        for e in rs[:12]:
            print(f"    · {str(e.get('title') or e['id'])[:72]}")
            print(f"      {e['why'][:110]}")
        print()
    print(f"  {r['note']}")
    print(f"  archive: {ARCHIVE}\n  readable: {DIGEST}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
