#!/usr/bin/env python3
"""seat_coord.py — who is touching what, so two agents on one box stop crossing paths.

★★★ WHY THIS EXISTS, IN THE OPERATOR'S WORDS: "You were supposed to be checking regularly (that's
    what working together means too)... Messing with eachothers work is a recipe for missunderstanding
    what's going on and making poor assumptions that end up in development failure."
    He was right and the criticism landed. Two seats (boardwalk = jarvis/, KHAOSS dev = panel +
    AionUi) spent today restarting and probing the SAME live services minute by minute. No file ever
    collided — but every restart invalidated the other's measurement mid-flight, and his microphone
    flapped three times while we each concluded things about a box the other was changing underneath.

★★ A PROMISE TO COORDINATE IS NOT COORDINATION. We exchanged careful messages all day and STILL
   crossed, because a message is read once and then forgotten, while a shared service is touched
   again five minutes later. This is the same lesson the capture corpus states as "load-bearing rules
   belong in hooks, not in rules files" — so the protocol lives in something that can be RUN and can
   FAIL, not in a paragraph either of us remembers differently.

★ IT IS ADVISORY, DELIBERATELY. It cannot and must not block another agent's work: it OBSERVES
  ownership and announces intent. Enforcement across independent sessions would need a lock nobody
  can be trusted to release — and this estate has spent today proving what an unreleased lock does.

Usage:
    seat_coord.py check                 who owns what, what moved, what crossed  (RUN THIS OFTEN)
    seat_coord.py claim <svc> <why>     announce you are about to touch a shared service
    seat_coord.py yield <svc> <why>     announce you are STANDING BACK so the other seat may act
    seat_coord.py done  <svc> <what>    announce you have finished
    seat_coord.py log [n]               recent entries from both seats
    seat_coord.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "reports" / "seat_coordination.jsonl"

# ★ WHO OWNS WHAT. Agreed with the KHAOSS dev seat 2026-09-17. Ownership means: you restart it, you
#   edit it, and the other seat ASKS rather than acts. Neither list is a permission boundary — it is
#   a convention two agents keep so a third party (the operator) is not the one who notices a clash.
OWNERS = {
    "boardwalk": {
        "paths": ["jarvis/"],
        # ★★★ VOICEBOX WAS IN NEITHER SEAT'S LIST, and that absence is the root cause of the
        #     overlapping-TTS the operator heard on 2026-09-18. The one service BOTH seats speak
        #     through (khaoss-dev's panel_server alone has 19 call sites; the voice seat synthesises
        #     every word here) had no owner at all — so it acquired two uncoordinated writers and
        #     nobody was responsible for noticing. Same class as a fence duplicated in two files:
        #     a shared thing with no single owner drifts by default.
        # ★ Assigned to boardwalk by agreement with khaoss-dev, 2026-09-18, on the grounds that the
        #   voice loop is the surface that breaks when it misbehaves. The other seat's five callers
        #   are CONSUMERS: they claim the mouth through :8770 rather than owning it.
        "services": {"jarvis-daemon": 8771, "jarvis-bubble": None, "jarvis-tray": None,
                     "voicebox": 8770},
    },
    "khaoss-dev": {
        "paths": ["cockpit/", "panel_server.py"],
        "services": {"panel": 7842, "aionui": 25808, "aionui-cdp": 9230},
    },
}
# ★★★ operator/ IS SHARED AND SAYING OTHERWISE WAS THE FIRST THING THIS TOOL GOT WRONG. My initial
#     table handed the whole directory to khaoss-dev, and the very first run flagged SIX files as
#     "crossed" — including operator/derive_items.py, which I edited myself, and this file, which I
#     had just written. An ownership model that reports false alarms gets ignored within a day, and
#     an ignored check is worse than none because it looks like coverage.
# ★★ AND ONE OF THOSE SIX IS A REAL DEPENDENCY, WHICH IS THE POINT: operator/focus_sources.py holds
#    CDP_PORT = 9230 and jarvis/focus.py reads it. When the other seat edits that file, my focus
#    behaviour can change without a single byte of jarvis/ moving. Shared files are exactly where
#    two agents damage each other WITHOUT a merge conflict to warn them.
SHARED = ["operator/"]
SESSIONS = REPO / "operator" / "seat_sessions.json"


def _resolve_me() -> str | None:
    """★★★ IDENTITY IS MEASURED, NEVER DEFAULTED.

    This line used to read `os.environ.get("KHAOSS_SEAT", "boardwalk")`. With the variable unset —
    which is the normal state, because a fresh shell per tool call does not inherit an export — BOTH
    seats resolved to 'boardwalk'. Measured 2026-09-18 on the enforcing hook:

        restart khaoss-panel, ME=boardwalk (the guess)  -> ALLOW
        restart khaoss-panel, ME=khaoss-dev (the truth) -> DENY

    ★★ So the wrong default INVERTED the guard: the KHAOSS seat's own claim on its own panel was
       signed 'boardwalk', which shielded the panel from its owner and authorised the peer to
       restart it. A safety tool that mislabels the actor does not merely fail to help — it hands
       the other seat a green light on your service. That is the exact collision this file exists
       to prevent, manufactured by the file itself.

    ★ CLAUDE_CODE_SESSION_ID is a real discriminator: present in every tool call, stable for the
      life of a session, and different per seat. Registry first, env override for humans/tests, and
      then UNKNOWN — because 'I could not tell' is a first-class answer and a guessed seat is not.
    """
    env = os.environ.get("KHAOSS_SEAT")
    if env:
        return env
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid:
        try:
            reg = json.loads(SESSIONS.read_text(encoding="utf-8"))
            seat = (reg.get("sessions") or {}).get(sid)
            if seat:
                return seat
        except Exception:
            pass
    return None


ME = _resolve_me()


def _append(entry: dict) -> None:
    # ★★ AN UNSIGNED ENTRY IS WORSE THAN NO ENTRY. The ledger's whole value is that `seat` names
    #    who acted; a row signed by a guess is read by the enforcing hook as the PEER's claim (or,
    #    worse, as the reader's own) and silently inverts the fence. Refuse, and say how to fix it.
    if not ME:
        print("  ★ REFUSED — this session's seat is UNKNOWN, so the entry would be unsigned.\n"
              "    An entry signed with the wrong seat inverts the guard; a guess is not allowed.\n"
              "    Register once:  python3 seat_coord.py whoami --set <boardwalk|khaoss-dev>")
        raise SystemExit(2)
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "seat": ME, **entry}
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())      # ★ the other seat may read this a second from now
    except OSError as e:
        print(f"  could not write the ledger: {e}")


def _entries(n: int = 40) -> list:
    try:
        lines = LEDGER.read_text().splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    # ★ a retracted row is still in the file (history is not edited) but is no longer honoured
    dead = {e.get("voids") for e in out if e.get("action") == "void" and e.get("voids")}
    return [e for e in out if e.get("at") not in dead and e.get("action") != "void"]


def _procs() -> dict:
    """Running services, by exact argv match — never a substring scan of the whole cmdline.

    ★ The self-match hazard has bitten this estate six times, once killing the asking shell (exit
      144), because a /proc loop searched for a string its own command line contained."""
    want = {
        "jarvis-daemon": "/usr/bin/python3 jarvis/daemon.py",
        "jarvis-bubble": "/usr/bin/python3 jarvis/bubble.py",
        "jarvis-tray": "/usr/bin/python3 jarvis/tray.py",
    }
    found = {}
    me = os.getpid()
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        for name, exact in want.items():
            if cmd == exact:
                found[name] = int(d.name)
        if "panel_server.py" in cmd and "--bind" in cmd:
            found["panel"] = int(d.name)
    return found


def _started(pid: int):
    try:
        clk = os.sysconf("SC_CLK_TCK")
        st = int((Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[-1].split()[19])
        up = float(Path("/proc/uptime").read_text().split()[0])
        return time.strftime("%H:%M:%S", time.localtime(time.time() - (up - st / clk)))
    except (OSError, ValueError, IndexError):
        return "?"


def check(minutes: int = 45) -> int:
    """★ THE COMMAND TO RUN OFTEN. Says who owns what, what changed recently, and whether anything
    crossed a boundary — so a clash is noticed by an agent rather than by the operator."""
    cutoff = time.time() - minutes * 60
    print(f"SEAT COORDINATION — I am '{ME}'   (window: last {minutes} min)\n")

    crossed, shared_hits = [], []
    print("  FILES CHANGED RECENTLY")
    for rel in SHARED:
        base = REPO / rel
        files = sorted(base.rglob("*.py")) if base.is_dir() else []
        for f in sorted([q for q in files if q.stat().st_mtime >= cutoff],
                        key=lambda q: -q.stat().st_mtime)[:8]:
            when = time.strftime("%H:%M:%S", time.localtime(f.stat().st_mtime))
            print(f"    ~  {when}  {f.relative_to(REPO)}   (SHARED — either seat may edit)")
            shared_hits.append(str(f.relative_to(REPO)))
    for seat, spec in OWNERS.items():
        for rel in spec["paths"]:
            base = REPO / rel
            files = sorted(base.rglob("*.py")) if base.is_dir() else ([base] if base.exists() else [])
            recent = [f for f in files if f.stat().st_mtime >= cutoff]
            for f in sorted(recent, key=lambda q: -q.stat().st_mtime)[:8]:
                when = time.strftime("%H:%M:%S", time.localtime(f.stat().st_mtime))
                mark = "   " if seat == ME else "★ "
                print(f"    {mark}{when}  {f.relative_to(REPO)}   (owner: {seat})")
                if seat != ME:
                    crossed.append((seat, str(f.relative_to(REPO))))
            if not recent:
                print(f"       (nothing under {rel} — owner: {seat})")

    print("\n  SERVICES RUNNING")
    for name, pid in sorted(_procs().items()):
        owner = next((s for s, sp in OWNERS.items() if name in sp["services"]), "?")
        mark = "   " if owner == ME else "★ "
        print(f"    {mark}{name:16} pid={pid:<8} up since {_started(pid)}   (owner: {owner})")

    print("\n  RECENT ANNOUNCEMENTS (both seats)")
    ents = [e for e in _entries(30)]
    if not ents:
        print("    (none yet — use `claim` before touching a shared service)")
    for e in ents[-8:]:
        who = e.get("seat", "?")
        mark = "   " if who == ME else "★ "
        print(f"    {mark}{e.get('at','')}  {who:12} {e.get('action','?'):6} "
              f"{e.get('service','')}  {e.get('why','')[:54]}")

    # ★ The verdict must be something that can FAIL, not a summary that always reads fine.
    print()
    # ★ Shared-file churn is not a violation, but it IS the thing most likely to break the other seat
    #   silently, so it gets its own line rather than being folded into "all clear".
    if shared_hits:
        print(f"  ~ {len(shared_hits)} shared file(s) changed — either seat may have done this, and")
        print("    jarvis/focus.py reads operator/focus_sources.py, so re-check before trusting a")
        print("    measurement that depends on them.")
    if crossed:
        print(f"  ⚠ CROSSED: {len(crossed)} file(s) under another seat's paths changed in this window:")
        for seat, f in crossed[:6]:
            print(f"      {f}  (owned by {seat})")
        print("    -> say so before assuming your measurement of it is still valid.")
        return 1
    print("  ✓ no file crossed a seat boundary in this window")
    return 0


def yield_to(service: str, why: str) -> int:
    """★★★ "I AM STANDING BACK" IS THE OPPOSITE OF "I AM TAKING THIS", AND THEY SHARED ONE VERB.
        I logged `claim panel "khaoss-dev landing the fix - boardwalk holding"` meaning I was YIELDING
        the panel to the other seat. The ledger recorded a CLAIM by boardwalk, and the peer's
        enforcement hook — which reads this ledger — then correctly blocked khaoss-dev from touching
        the very service I was handing them.
    ★★ Caught by the other seat within minutes of it going live, and it is the right kind of bug:
       nothing was broken, the words were simply wrong, and the wrongness only became visible once
       something ACTED on the record rather than read it. A log nobody enforces can afford a sloppy
       verb; the moment a hook consumes it, the verb is an API.
    ★ `yield` records the intent explicitly and never registers a hold in the yielder's name.
    """
    _append({"action": "yield", "service": service, "why": why, "holds": False})
    print(f"  announced: {ME} is STANDING BACK from {service} — {why}")
    return 0


def claim(service: str, why: str) -> int:
    _append({"action": "claim", "service": service, "why": why})
    print(f"  announced: {ME} is about to touch {service} — {why}")
    return 0


def done(service: str, what: str) -> int:
    _append({"action": "done", "service": service, "why": what})
    print(f"  announced: {ME} finished with {service} — {what}")
    return 0


def self_test() -> int:
    fails = []
    n0 = len(_entries(10_000))
    claim("selftest-service", "self-test probe")
    done("selftest-service", "self-test probe")
    if len(_entries(10_000)) != n0 + 2:
        fails.append("the ledger did not record both entries")
    # ★ ownership must be a partition: no service owned twice, or "who owns it" has no answer
    seen = {}
    for seat, spec in OWNERS.items():
        for svc in spec["services"]:
            if svc in seen:
                fails.append(f"{svc} is claimed by both {seen[svc]} and {seat}")
            seen[svc] = seat
    # ★ and the process matcher must be exact — a substring rule would match this very process
    if any(v == os.getpid() for v in _procs().values()):
        fails.append("the process scan matched ITSELF — the self-match hazard is back")
    print("operator/seat_coord self_test: " + ("PASS" if not fails else "FAIL"))
    for f in fails:
        print("   ✗", f)
    return 1 if fails else 0



def voided_keys(entries: list) -> set:
    """★ Which ledger rows have been retracted. A wrongly-signed row cannot be honestly fixed by
    appending another row in the same wrong name — that repeats the forgery. It is VOIDED: the
    retraction is itself signed, the original stays visible, and readers skip it."""
    return {e.get("voids") for e in entries if e.get("action") == "void" and e.get("voids")}


def void(at: str, why: str) -> int:
    """Retract a ledger row by its timestamp. The row stays; readers stop honouring it."""
    if not at:
        print("  which row? usage: seat_coord.py void <at-timestamp> <why>")
        return 2
    _append({"action": "void", "voids": at, "service": None, "why": why or "retracted"})
    print(f"  voided {at} — {why}")
    return 0


def whoami(args: list) -> int:
    if args and args[0] == "--set":
        seat = args[1] if len(args) > 1 else ""
        if seat not in OWNERS:
            print(f"  unknown seat {seat!r} — known: {', '.join(OWNERS)}")
            return 2
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not sid:
            print("  no CLAUDE_CODE_SESSION_ID in this environment — cannot register a session.")
            return 2
        try:
            reg = json.loads(SESSIONS.read_text(encoding="utf-8"))
        except Exception:
            reg = {"sessions": {}}
        reg.setdefault("sessions", {})[sid] = seat
        SESSIONS.write_text(json.dumps(reg, indent=2), encoding="utf-8")
        print(f"  registered session {sid[:8]}… as '{seat}'")
        return 0
    src = ("KHAOSS_SEAT" if os.environ.get("KHAOSS_SEAT")
           else "session registry" if ME else "nothing — UNKNOWN")
    print(f"  seat: {ME or 'UNKNOWN'}   (from {src})")
    if not ME:
        print("  ★ register once:  python3 seat_coord.py whoami --set <boardwalk|khaoss-dev>")
    return 0 if ME else 2

def main(argv) -> int:
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "check":
        return check(int(argv[2]) if len(argv) > 2 else 45)
    if cmd == "claim":
        return claim(argv[2], " ".join(argv[3:]) or "unspecified")
    if cmd in ("yield", "standback"):
        return yield_to(argv[2], " ".join(argv[3:]) or "unspecified")
    if cmd == "done":
        return done(argv[2], " ".join(argv[3:]) or "unspecified")
    if cmd == "void":
        return void(argv[2] if len(argv) > 2 else "", " ".join(argv[3:]))
    if cmd == "whoami":
        return whoami(argv[2:] if len(argv) > 2 else [])
    if cmd == "log":
        for e in _entries(int(argv[2]) if len(argv) > 2 else 20):
            print("  " + json.dumps(e))
        return 0
    if cmd in ("selftest", "self-test"):
        return self_test()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
