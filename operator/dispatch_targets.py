#!/usr/bin/env python3
"""dispatch_targets.py — WHERE a board item can be sent, and honestly how far it actually gets.

★★★ THE RULE THIS FILE EXISTS TO OBEY: no dead buttons.
    Today produced four separate defects of the same shape — a control that is offered, accepted, and
    silently does nothing (a voice parameter ignored, a refusal reported as success, Done buttons on
    rows the store cannot hold, an exit code that could not honour its own documented contract).
    A destination picker is the easiest place in the whole board to repeat that: it is trivial to list
    twelve places to send something and wire two of them.

★★ SO EVERY TARGET DECLARES ITS DELIVERY MODE, AND THE UI SHOWS IT:
      live    — there is a proven push path and the message arrives without an agent
      queued  — it is written to the handoff queue; an agent delivers it on its next turn
      manual  — reachable, but only a person can carry it. Listed so it is not silently missing.
    `queued` is not a euphemism for nothing: the queue is a real file, it is read, and the row records
    when delivery happened and by whom. But it is NOT `live`, and conflating the two is the lie.
"""
from __future__ import annotations
import json, os, re, shutil, sqlite3, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = HERE / "handoff_queue.jsonl"
HOME = Path.home()
AIONUI_DB = next((p for p in (HOME / ".config/AionUi-Dev/aionui/aionui.db",
                              HOME / ".config/AionUi/aionui.db") if p.exists()), None)
CC_SOCKS = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "cc-socks"
HERMES_PROFILES = HOME / ".hermes" / "profiles"


def _aionui_teams() -> list:
    if not AIONUI_DB:
        return []
    try:
        con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT id, name FROM teams ORDER BY name").fetchall()
        finally:
            con.close()
    except Exception:
        return []
    # ★★★ live ONLY WHERE A DELIVERY PATH ACTUALLY EXISTS, AND THE ANSWER IS READ FROM THE DELIVERY
    #   CODE ITSELF. panel_server's _LANE_TEAMS is the map the notify path uses; a team not in it has
    #   no proven route, so listing it as `live` would be a dead button wearing a green badge.
    #   ★ The map is PARSED from panel_server.py rather than copied here. A copy is how the writer and
    #     the reader drift apart, which cost two defects today already.
    mapped = set()
    try:
        src = (HERE.parent / "panel_server.py").read_text(errors="replace")
        blk = src.split("_LANE_TEAMS = {", 1)[1].split("}", 1)[0]
        mapped = set(re.findall(r'\("([0-9a-f]{8})[0-9a-f-]*"', blk))
    except Exception:
        mapped = set()
    out = []
    for r in rows:
        live = r[0][:8] in mapped
        out.append({"id": f"aionui:{r[0]}", "label": r[1], "group": "AionUi team chats",
                    "delivery": "live" if live else "queued",
                    "how": ("posted into the team's chat and confirmed in the messages table (R-08)"
                            if live else
                            "no lane mapping exists for this team, so there is no proven push path — "
                            "queued for an agent instead of pretending")})
    return out


def _cc_sessions() -> list:
    """★ A PROCESS ID IS NOT AN IDENTITY. This listed "Claude Code session 2403068" five times over —
    five seats sharing one working directory, indistinguishable, and no way to tell the one running the
    trading lane from the one reading telemetry. He cannot route an item to a chat he cannot recognise.
    Names now come from cc_session_names: a human-asserted alias first, then the session's own
    transcript subject, and UNNAMED where neither is available — never a confident guess."""
    try:
        sys.path.insert(0, str(HERE))
        import cc_session_names, importlib
        importlib.reload(cc_session_names)
        rows = cc_session_names.sessions()
    except Exception:
        rows = []
    if not rows:
        try:
            rows = [{"pid": s.stem, "label": f"session {s.stem}", "confidence": "unmatched",
                     "why": "name resolver unavailable", "started": None}
                    for s in sorted(CC_SOCKS.glob("*.sock"))]
        except Exception:
            rows = []
    out = []
    for r in rows:
        badge = {"named": "", "matched": "  (from its transcript)", "unmatched": "  (unnamed — you can name it)"}
        # ★ AVAILABILITY IS NOT KNOWABLE FROM HERE AND THE PICKER SAYS SO RATHER THAN IMPLYING IDLE.
        #   The Starlink seat pointed out it is usually BUSY — the operator drives it directly — so a
        #   picker that lists every seat identically implies they are all free to take work. A socket
        #   existing means the session is ALIVE, not that it is available.
        busy = "usually BUSY" in str(r.get("purpose") or "")
        out.append({"id": f"cc:{r['pid']}", "label": r["label"] + badge.get(r.get("confidence"), "")
                    + ("  ⏳" if busy else ""),
                    "purpose": r.get("purpose", ""),
                    "availability": ("known to be busy — the operator drives this seat directly"
                                     if busy else "unknown — a live socket means alive, not free"),
                    "group": "Claude Code (running)", "delivery": "queued",
                    "pid": r["pid"], "confidence": r.get("confidence"),
                    "started": r.get("started"),
                    "how": "written to the handoff queue; that session reads and acts on it on its next "
                           "turn. " + str(r.get("why") or "")})
    return out


def _hermes_profiles() -> list:
    try:
        names = sorted(p.name for p in HERMES_PROFILES.iterdir()
                       if p.is_dir() and not p.name.startswith(("ARCHIVED_", "STALE_")))
    except Exception:
        return []
    return [{"id": f"hermes:{n}", "label": n, "group": "Hermes profiles (spawn a new agent)",
             "delivery": "queued", "how": "a spawn request an agent executes; nothing starts by itself"}
            for n in names]


def _tools() -> list:
    t = []
    if shutil.which("codex"):
        t.append({"id": "codex", "label": "Codex CLI", "group": "Other runtimes", "delivery": "queued",
                  "how": "queued as a codex invocation; an agent runs it — this panel does not exec it"})
    t.append({"id": "cc:new", "label": "New Claude Code session (local)", "group": "Other runtimes",
              "delivery": "queued", "how": "queued; an agent opens it. A panel cannot attach a terminal for you."})
    t.append({"id": "cc:cloud", "label": "New Claude Code session (cloud)", "group": "Other runtimes",
              "delivery": "queued",
              "how": "queued. ★ Requires cloud access on the session that picks it up; if that is not "
                     "available the queue row records the refusal rather than appearing to have run."})
    # ★ PRIME IS LISTED AND MARKED manual ON PURPOSE. The recorded verdict is that Prime is containable
    #   only GATED ON EGRESS (local weights + --unshare-net, or it is an exfiltration engine). Offering
    #   a one-click spawn for it from a board would route around a containment decision that has not
    #   been made. Visible, deliberately not one-click.
    t.append({"id": "prime", "label": "Prime Agent  (blocked: egress gate undecided)", "group": "Other runtimes",
              "delivery": "manual",
              "how": "NOT one-click by design — Prime is containable only behind an egress gate that has "
                     "not been decided. A button here would route around that decision."})
    return t


def _flag_collisions(rows: list) -> list:
    """★★★ TWO DESTINATIONS THAT LOOK LIKE ONE IS THE FAILURE THIS PICKER EXISTS TO END.
    An AionUi TEAM called "the example team" and a CC SEAT called "the example team seat" are different places —
    one is a team chat whose lead reads it, the other is a running agent — and side by side in a
    dropdown they read as the same destination. He would pick either and believe he had reached the
    other. Raised by the LIQUID-100 seat, which asked NOT to be called "the example team" for exactly this
    reason and named itself after the work instead.
    ★ Detected on the normalised name, so "the example team" and "trading-ops seat" still collide."""
    def norm(x):
        return re.sub(r"[^a-z0-9]+", "", str(x).lower())
    seen = {}
    for r in rows:
        k = norm(r["label"])[:24]
        if not k:
            continue
        seen.setdefault(k, []).append(r)
    for k, group in seen.items():
        if len(group) > 1 and len({g["group"] for g in group}) > 1:
            for g in group:
                others = [x["group"] for x in group if x is not g]
                g["collision"] = (f"★ this name also appears under {', '.join(others)} — two different "
                                  f"destinations reading as one. Rename one of them.")
    return rows


def targets() -> dict:
    groups = {}
    for t in _flag_collisions(_aionui_teams() + _cc_sessions() + _hermes_profiles() + _tools()):
        groups.setdefault(t["group"], []).append(t)
    counts = {}
    for g, rows in groups.items():
        for r in rows:
            counts[r["delivery"]] = counts.get(r["delivery"], 0) + 1
    return {"groups": groups, "counts": counts,
            "legend": {"live": "arrives without an agent; arrival is confirmed at the destination",
                       "queued": "written to the handoff queue and delivered by an agent's next turn",
                       "manual": "reachable, but deliberately not one-click — the row says why"}}


def enqueue(entry: dict) -> dict:
    row = {"ts": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
           "delivered": False, "delivered_at": None, "delivered_by": None, **entry}
    with QUEUE.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return {"ok": True, "queued": True, "target": entry.get("target"),
            "note": "queued — an agent delivers this on its next turn; it has NOT been delivered yet"}


def pending() -> list:
    if not QUEUE.exists():
        return []
    out = []
    for line in QUEUE.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                if not r.get("delivered"):
                    out.append(r)
            except Exception:
                pass
    return out


def self_test() -> int:
    """★ NEGATIVE CONTROL: the failure this file exists to prevent is a target that LOOKS deliverable
    and is not. So the test is that every listed target declares a mode, that `queued` is never
    described as delivered, and that the registry is not silently empty."""
    fails = []
    t = targets()
    rows = [r for g in t["groups"].values() for r in g]
    if not rows:
        fails.append("the registry is EMPTY — an empty picker is indistinguishable from 'nothing is "
                     "reachable', which is a claim this has not established")
    for r in rows:
        if r.get("delivery") not in ("live", "queued", "manual"):
            fails.append(f"{r['id']} declares no delivery mode")
        if not r.get("how"):
            fails.append(f"{r['id']} does not say how far it actually gets")
    q = enqueue({"target": "_selftest", "kind": "probe", "text": "x"})
    if q.get("note", "").find("NOT been delivered") < 0:
        fails.append("enqueue() reports success without saying the thing has not yet been delivered")
    # remove the probe so the queue is not polluted by its own test
    if QUEUE.exists():
        keep = [l for l in QUEUE.read_text().splitlines() if '"_selftest"' not in l]
        QUEUE.write_text("\n".join(keep) + ("\n" if keep else ""))
    for f in fails:
        print(f"  ✗ {f}")
    print(f"  ✓ dispatch_targets self_test: {len(rows)} target(s), every one declares a delivery mode; "
          f"queued is reported as queued" if not fails else "  ✗ dispatch_targets self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    t = targets()
    print(f"\n  DISPATCH TARGETS — {sum(len(v) for v in t['groups'].values())} reachable")
    for g, rows in t["groups"].items():
        print(f"\n  {g}")
        for r in rows:
            print(f"    [{r['delivery']:6s}] {r['label']}")
    print(f"\n  {t['counts']}")
    print(f"  pending in queue: {len(pending())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
