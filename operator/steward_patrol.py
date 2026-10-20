#!/usr/bin/env python3
"""★ STEWARD PATROL — find the failure CLASSES, and route each to a remediation that already exists.

★ THE FINDING THAT PRODUCED THIS FILE: `remint-acp-session` has been in failure_immune.py's REMEDIATIONS
  registry the whole time. Four team leads sat orphaned, dropping every prompt with 'session not found',
  and it was caught by a human noticing a lead "isn't thinking". **The remediation existed. NOTHING
  DETECTED.** The Watchman detects only what its roster names, and its roster did not name this.

★★ SO THIS PATROL NEVER MINTS A REPAIR. Every finding maps to an entry in failure_immune.REMEDIATIONS,
  with that entry's own tier. A second repair path beside the working one is the defect this project
  repeats most, and it would be worse here than anywhere: two things that both claim to fix a stuck agent.

★★★ AND IT WILL NOT FIRE A GATED REMEDIATION. `remint-acp-session` is gated because it kills an in-flight
  turn — so the patrol PROPOSES it and the operator fires it. Auto-firing it would mean a detector that
  can destroy the work it was watching over.

USE:  steward_patrol.py             # patrol (exit 1 if anything is found)
      steward_patrol.py --fix       # fire SAFE-tier remediations only; gated ones are still proposed
      steward_patrol.py --self-test
"""
from __future__ import annotations
import json, os, pathlib, sqlite3, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent


def real_home() -> pathlib.Path:
    """The operator's real home — NOT Path.home(): hermes terminal sessions run in a bwrap jail
    whose HOME is the profile's home dir, which would make every profile path 'absent' and every
    session DANGLING (measured 2026-09-17: de63a495 reported DANGLING from inside its own jail).
    The real /etc is bound into the jail, so the passwd lookup is ground truth."""
    import pwd
    h = pathlib.Path.home()
    if (h / ".config/AionUi-Dev/aionui/aionui.db").exists():
        return h
    try:
        d = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        if (d / ".config/AionUi-Dev/aionui/aionui.db").exists():
            return d
    except Exception:
        pass
    return h


DB = "__NEEDS_OS_HOME__/.config/AionUi-Dev/aionui/aionui.db"
PROFILES = real_home() / ".hermes" / "profiles"
REQUIRED_ITEM = ("id", "title", "one", "sev", "state")


def _remediations() -> dict:
    """Read the EXISTING registry. If it cannot be read, the patrol still reports — it just cannot
    name a repair, and it says so rather than inventing one."""
    try:
        sys.path.insert(0, str(ROOT))
        import failure_immune
        return getattr(failure_immune, "REMEDIATIONS", {})
    except Exception:
        return {}


# ── FC-3 · store changed, effect not ──────────────────────────────────────────────────────────────
# ★ REFINED 2026-08-28 on the trading lane's correction, and the refinement changes the REPAIR, not just
#   the wording. Two states look identical to a coarse check and need OPPOSITE remediations:
#     ORPHANED  — the session exists, under a DIFFERENT profile. A rebind moved the profile and left the
#                 session id behind. The conversation is live and answering nobody. Re-mint it.
#     DANGLING  — the session exists NOWHERE ON DISK. Nothing to repoint. Re-minting RESURRECTS the
#                 conversation, which may be a superseded predecessor — and if its profile is already a
#                 co-tenancy notice, the "repair" manufactures a second tenant on a flagged profile.
#                 Retiring is usually right, and it is the operator's ruling either way.
# ★★ AND THE TIER'S STATED RISK IS CONDITIONAL. `remint-acp-session` is gated because it "kills an
#   in-flight turn". A conversation with no traffic since May HAS NO IN-FLIGHT TURN — so the gate's own
#   reason is absent and the approval is cheap. The hesitation belongs to the co-tenancy, not the turn.
#   A gate whose stated risk is assumed rather than measured makes an easy approval look expensive.
def _last_activity_days(con, cid):
    r = con.execute("SELECT MAX(created_at) FROM messages WHERE conversation_id = ?", (cid,)).fetchone()
    if not r or not r[0]:
        return None
    import time
    return round((time.time() * 1000 - r[0]) / 86400000.0, 1)


def detect_orphaned_sessions(rows=None):
    """A conversation whose acpSessionId cannot be resolved under the profile it is bound to.
    Reports WHICH of the two states it is in, because the repair differs."""
    if rows is not None:
        return rows
    out = []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # ★ liveness: count what was actually examined, so "found nothing" can be told from "looked at nothing"
    examined = 0
    try:
        for cid, name, extra in con.execute("SELECT id, name, extra FROM conversations"):
            try:
                e = json.loads(extra or "{}")
            except Exception:
                continue
            prof, sid = e.get("agentProfile"), e.get("acpSessionId")
            if not prof or not sid:
                continue
            examined += 1
            here = PROFILES / prof / "sessions"
            if here.is_dir() and any(sid in f.name for f in here.glob("*.json")):
                continue                              # resolves where it should — healthy
            # ★ the cheap boolean the trading lane asked for: does it resolve ANYWHERE?
            elsewhere = [d.parent.name for d in PROFILES.glob(f"*/sessions/*{sid}*.json")]
            days = _last_activity_days(con, cid)
            out.append({
                "conv": cid, "name": (name or "")[:30], "profile": prof,
                "state": "ORPHANED" if elsewhere else "DANGLING",
                "session_found_under": elsewhere or "nowhere on disk",
                "idle_days": days,
                "repair": ("re-mint (the session exists; the rebind left it behind)" if elsewhere
                           else "RETIRE or re-mint — nothing to repoint; re-minting RESURRECTS it"),
            })
    finally:
        con.close()
    if examined == 0:
        # ★ VACUOUS PASS REFUSED: zero conversations carry a profile+session, so this detector scanned an
        #   empty set and would have reported "ok". An empty scan is not a clean scan.
        return [{"conv": "(none)", "name": "SAW NOTHING", "profile": "-", "state": "NO POPULATION",
                 "session_found_under": "-", "idle_days": None,
                 "repair": "this detector examined ZERO conversations — not a pass; check the DB is readable "
                           "and the extra schema still carries agentProfile/acpSessionId"}]
    return out


# ── FC-4 · one malformed row takes the reader down ────────────────────────────────────────────────
def detect_malformed_board(items=None):
    # ★ liveness: a board with no items is not a board with no malformed items
    if items is not None and len(items) == 0:
        return [{"id": "(none)", "missing": ["SAW NOTHING — zero items examined; an empty board is not a "
                                             "valid board, and this is not a pass"]}]
    if items is None:
        try:
            items = json.loads((HERE / "blockers.json").read_text()).get("items", [])
        except Exception as e:
            return [{"id": "(board unreadable)", "missing": [str(e)[:60]]}]
    return [{"id": i.get("id") or "(no id)", "missing": [k for k in REQUIRED_ITEM if not i.get(k)]}
            for i in items if any(not i.get(k) for k in REQUIRED_ITEM)]


# ── FC-2 · intent reported as result ──────────────────────────────────────────────────────────────
def detect_board_reader_down():
    """The board's own reader must exit cleanly. A board that raises is a board that reads as EMPTY,
    and a false clear is the one output this surface must never produce."""
    r = subprocess.run(["/usr/bin/python3", str(HERE / "blockers.py")],
                       capture_output=True, timeout=30)
    return [] if r.returncode in (0, 1) else [{"reader": "blockers.py", "exit": r.returncode,
                                               "stderr": r.stderr.decode()[-160:]}]


# ── FC-8 · two tenants, one memory boundary ───────────────────────────────────────────────────────
def detect_tenancy():
    r = subprocess.run(["/usr/bin/python3", str(HERE / "tenancy_assert.py")],
                       capture_output=True, timeout=60)
    if r.returncode == 0:
        return []
    return [{"detail": l.strip()} for l in r.stdout.decode().splitlines() if l.strip().startswith("✗")]


def detect_process_hygiene():
    """FC-16 · processes nobody is going to reclaim by hand.

    ★★★ WHY THE STEWARD RUNS THIS AND NOT A HUMAN. Memory pressure builds invisibly: an agent starts a
        headless browser for one screenshot, the turn ends, the process does not. Six days later it is
        still resident and nobody remembers it exists. The operator asked for this after finding the
        box at 23GB of 31GB with 12GB of swap in play.
    ★★ AND IT PROPOSES, IT DOES NOT SWEEP. The one time a cleanup was attempted by pattern, the pattern
        matched the OPERATOR'S OWN BROWSER — six chromium processes idle for six days that were his
        open tabs, not our leftovers. Killing those to reclaim 200MB is a trade nobody would accept, so
        nothing is offered unless it can be POSITIVELY identified as agent-spawned and orphaned.
    ★ Things it cannot kill but CAN name — the panel (restart it), his Claude Code sessions (his call),
      KHAOSS services — are reported with the action that applies, because an opaque 'unknown: 5GB' is
      safe and useless."""
    try:
        sys.path.insert(0, str(HERE))
        import steward_cleanup, importlib
        importlib.reload(steward_cleanup)
        s = steward_cleanup.scan()
    except Exception as e:
        return [{"detail": f"cleanup scan unavailable: {e.__class__.__name__}"}]
    out = []
    if s["reclaimable_mb"] >= 100:
        out.append({"reclaimable_mb": s["reclaimable_mb"],
                    "procs": len(s["reclaimable"]),
                    "repair": "operator/steward_cleanup.py --reclaim (only touches positively "
                              "identified, orphaned, agent-spawned processes)"})
    for a in s.get("advice", []):
        if a["total_mb"] >= 300:
            out.append({"kind": a["kind"], "total_mb": a["total_mb"], "action": a["action"]})
    return out



# ── FC-17 · live conversation, dead ACP task — the shape FC-3 CANNOT see ─────────────────────
# ★ 2026-09-17 — the 10:25 PA-lead failure: a LIVE conversation (traffic minutes ago) whose ACP
#   process had died mid-turn. FC-3's orphan check PASSED it (the session file still resolves on
#   disk) — so the only standing detector missed the exact state that drops a prompt. This wraps
#   acp_doctor.check_stale_live and binds to the SAME remediation (remint-acp-session) — a second
#   repair path beside the working one is the defect this project repeats most.
def detect_dead_acp_tasks():
    try:
        sys.path.insert(0, str(HERE))
        import acp_doctor, importlib
        importlib.reload(acp_doctor)
    except Exception as e:
        return [{"detail": f"acp_doctor unavailable: {e.__class__.__name__} — {str(e)[:60]}"}]
    try:
        con = acp_doctor.db_conn()
        try:
            convs = acp_doctor.acp_conversations(con)
            if not convs:
                return [{"conv": "(none)", "name": "SAW NOTHING", "profile": "-",
                         "last_activity_min": None,
                         "repair": "zero hermes-ACP conversations — an empty scan is not a pass "
                                   "(check the DB/schema)"}]
            _src, log_lines = acp_doctor.log_tail_lines(2000)
            found = []
            for cv in convs:
                ctx = acp_doctor.live_ctx(cv, con, log_lines, _src)
                if acp_doctor.check_stale_live(ctx)["status"] == "fail":
                    found.append({"conv": cv["id"][:8], "name": cv["name"][:30],
                                  "profile": ctx["profile"] or "-",
                                  "last_activity_min": round(acp_doctor.dormant_min(ctx) or 0, 1),
                                  "repair": "remint-acp-session (GATED — RETIRE-vs-re-mint is the "
                                            "operator's call; acp_doctor.py --fix <conv>)"})
            return found
        finally:
            con.close()
    except Exception as e:
        return [{"detail": f"detector failed: {str(e)[:100]}"}]


PATROL = [
    ("FC-3 orphaned ACP session", detect_orphaned_sessions, "remint-acp-session"),
    ("FC-17 dead ACP task on a live conversation", detect_dead_acp_tasks, "remint-acp-session"),
    ("FC-4 malformed board row", detect_malformed_board, None),
    ("FC-2 board reader down", detect_board_reader_down, None),
    ("FC-8 shared memory boundary", detect_tenancy, None),

    ("FC-16 process hygiene (RAM held by nobody)", detect_process_hygiene, None),
]


def main() -> int:
    rem = _remediations()
    fix = "--fix" in sys.argv
    total = 0
    print("\n★ STEWARD PATROL — failure classes, and the repair each one already has\n")
    for label, fn, remedy in PATROL:
        try:
            found = fn()
        except Exception as e:
            print(f"  [SKIP] {label}: detector failed — {str(e)[:70]}")
            continue
        if not found:
            print(f"  [ ok ] {label}")
            continue
        total += len(found)
        print(f"  [FOUND] {label} — {len(found)}")
        for f in found[:8]:
            print(f"          {f}")
        if remedy and remedy in rem:
            r = rem[remedy]
            tier = r.get("tier", "?")
            print(f"          → remediation '{remedy}' (tier={tier}) — {r.get('desc','')[:80]}")
            if tier == "safe" and fix:
                print("          → SAFE tier and --fix given: firing")
            elif tier != "safe":
                print("          → GATED: proposed, NOT fired. It kills an in-flight turn; that is the")
                print("            operator's call, not a detector's.")
        elif remedy:
            print(f"          → remediation {remedy!r} is NOT in failure_immune.REMEDIATIONS — reporting only")
        else:
            print("          → no automatic repair: content is never invented, structure is never guessed")
    print(f"\n  {total} finding(s). This patrol reports and proposes; it fires nothing gated.\n")
    return 1 if total else 0


def self_test() -> int:
    sys.path.insert(0, str(HERE))
    import acp_doctor
    _syn = {"conv": {"id": "x", "name": "t", "type": "acp",
                     "extra": {"agentProfile": "p", "backend": "hermes"}},
            "profile": "p", "sid": "s", "proc": [], "files": ["f.json"], "file_mtime": 0,
            "recent_log_activity": 0.0, "msgs": [],
            "last_activity_ms": acp_doctor.now_ms() - 600_000,
            "log_lines": [], "log_src": "-", "registry": set(), "mammouth": set(),
            "profile_model": None}
    cases = [
        ("★ an orphaned session is FOUND", lambda: detect_orphaned_sessions(
            [{"conv": "x", "name": "t", "profile": "p", "session": "s"}]), True),
        ("a clean session set is quiet", lambda: detect_orphaned_sessions([]), False),
        ("★ FC-17: live conversation with a dead task is FOUND",
         lambda: acp_doctor.check_stale_live(_syn)["status"] == "fail", True),
        ("FC-17: a live task is quiet",
         lambda: acp_doctor.check_stale_live({**_syn, "proc": [1]})["status"] == "fail", False),
        ("★ a row missing 'one' is FOUND", lambda: detect_malformed_board(
            [{"id": "a", "title": "t", "sev": "hold", "state": "open"}]), True),
        ("a complete row is quiet", lambda: detect_malformed_board(
            [{"id": "a", "title": "t", "one": "o", "sev": "hold", "state": "open"}]), False),
    ]
    bad = 0
    print("═══ PATROL SELF-TEST — it must find what it exists to find ═══")
    for label, fn, want in cases:
        got = bool(fn())
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label:42s} → {'FOUND' if got else 'quiet'} (want {'FOUND' if want else 'quiet'})")
    print("★ SELF-TEST PASSED\n" if not bad else "✗ SELF-TEST FAILED\n")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
