#!/usr/bin/env python3
"""persistent_registry.py — things that must STAY OPEN, declared by the team that needs them.

★★★ THE TWO FAILURES THIS SITS BETWEEN, both real, both from 2026-09-03:
    · I nearly killed a Chromium to reclaim 200MB. It was the agent-driven browser on debugging port
      9333 — the one strategy that reaches Proton on a Free plan. Nothing anywhere said it mattered.
    · The same browser's profile lives under /tmp, so a login established in it is one cleanup away
      from gone. That is very likely why the operator remembered setting this up and the session was
      not there.
    A process nobody has declared is a process everybody is free to kill, and a persistent thing whose
    STATE is not durable is not persistent — it just has not been restarted yet.

★★ SO A TEAM DECLARES WHAT IT NEEDS KEPT, and the declaration carries who owns it, which steward looks
    after it, and what breaks if it stops. Stewards then detect two opposite faults:
      MARKED BUT DEAD    — something required is not running. That is a board item.
      RUNNING BUT UNMARKED — a long-lived process nobody claims. Not killed; SURFACED, because the
                             answer is usually "someone should declare this", not "kill it".

★ AND THE CLEANUP TOOL READS THIS FIRST. steward_cleanup will not offer anything registered here,
  whatever else matches — the registry is a veto, not a hint.
"""
from __future__ import annotations
import json, os, re, sys, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REG = HERE / "persistent_processes.json"
MIN_AGE_UNCLAIMED = 6 * 3600      # ★ 6h — long enough that transient work is not flagged


def _load() -> dict:
    if REG.exists():
        try:
            return json.loads(REG.read_text())
        except Exception:
            pass
    return {"_doc": "Processes a team has declared must stay open. Read by steward_cleanup as a VETO.",
            "entries": {}}


def _save(d: dict) -> None:
    REG.write_text(json.dumps(d, indent=2) + "\n")


def register(name: str, match: str, why: str, owner: str, steward: str,
             breaks_if_stopped: str = "", durable_state: str | None = None) -> dict:
    if not name or not match:
        return {"ok": False, "error": "need a name and a match pattern"}
    d = _load()
    d["entries"][name] = {
        "match": match, "why": why, "owner": owner, "steward": steward,
        "breaks_if_stopped": breaks_if_stopped,
        # ★ Where its state lives, and whether that survives a reboot. A persistent process whose
        #   profile is in /tmp is not persistent; recording the path is what makes that visible.
        "durable_state": durable_state,
        "registered": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _save(d)
    return {"ok": True, "name": name}


def unregister(name: str) -> dict:
    d = _load()
    if name not in d["entries"]:
        return {"ok": False, "error": f"{name!r} is not registered"}
    d["entries"].pop(name)
    _save(d)
    return {"ok": True, "name": name, "note": "no process was killed — only the declaration removed"}


# ★★★ SINGLE-FLIGHT /proc SCAN — 2026-09-17.
#   This scan walks every pid in /proc and opens TWO files per pid (~800 opens a pass). It is
#   reached from derive_items -> the Operator Board, which is polled continuously by seats, hooks
#   and the panel itself. Uncached, every concurrent request ran its OWN full scan: measured 312
#   request threads inside status() at once, GIL-bound, each making the others slower, clients
#   timing out and retrying into the pile. Thread and fd counts rose monotonically and never fell
#   (766 threads / 1023 fds / 312 sockets in CLOSE-WAIT) until the panel answered nothing at all.
#   Single-flight + short TTL: N concurrent callers now cost ONE scan, not N.
_SCAN_TTL_S = 3.0
_SCAN_LOCK = threading.Lock()
_SCAN_CACHE: dict = {"ts": 0.0, "data": []}


def _scan_procs() -> list:
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").replace("\0", " ").strip()
            if not cmd:
                continue
            age = int(time.time() - os.path.getmtime(f"/proc/{pid}"))
            rss = int(next(l for l in open(f"/proc/{pid}/status") if l.startswith("VmRSS")).split()[1])
        except Exception:
            continue
        out.append({"pid": int(pid), "cmd": cmd, "age_s": age, "rss_kb": rss})
    return out


def _running(max_age_s: float = _SCAN_TTL_S) -> list:
    """Cached view of /proc that SERVES STALE RATHER THAN QUEUEING.

    ★★★ The first version of this took the lock and waited. That is still a convoy: under real GIL
    contention the scan runs longer than the TTL, so the cache is always stale, so every caller
    queues — measured 146 request threads parked on this one lock. A slightly old process list is
    fine for a board; a parked thread holding a client socket is not. So: if another thread is
    already scanning, return what we have and move on. Only a caller that has NOTHING to return
    (a cold cache) ever blocks. Pass max_age_s=0 to demand a guaranteed-fresh scan.
    """
    fresh_enough = max_age_s > 0 and time.monotonic() - _SCAN_CACHE["ts"] < max_age_s
    if fresh_enough:
        return _SCAN_CACHE["data"]

    have_cache = bool(_SCAN_CACHE["data"])
    # Never block behind a scan that is already running when we have something to serve.
    if not _SCAN_LOCK.acquire(blocking=not have_cache or max_age_s == 0):
        return _SCAN_CACHE["data"]
    try:
        if max_age_s > 0 and time.monotonic() - _SCAN_CACHE["ts"] < max_age_s:
            return _SCAN_CACHE["data"]
        data = _scan_procs()
        _SCAN_CACHE["data"] = data
        _SCAN_CACHE["ts"] = time.monotonic()
        return data
    finally:
        _SCAN_LOCK.release()


def status() -> dict:
    """Both faults, in one pass."""
    d = _load()
    procs = _running()
    # ★ EXCLUDE OUR OWN COMMAND LINE. A match pattern appears in the process searching for it — the
    #   pgrep trap this house has now hit three times.
    me = str(os.getpid())
    procs = [p for p in procs if str(p["pid"]) != me and "persistent_registry" not in p["cmd"]]

    marked, dead = [], []
    for name, e in d["entries"].items():
        hits = [p for p in procs if re.search(e["match"], p["cmd"])]
        row = {"name": name, **e, "running": bool(hits),
               "pids": [p["pid"] for p in hits],
               "rss_mb": round(sum(p["rss_kb"] for p in hits) / 1024) if hits else 0}
        if hits:
            marked.append(row)
        else:
            dead.append(row)

    claimed_pids = {p for r in marked for p in r["pids"]}
    unclaimed = []
    for p in procs:
        if p["pid"] in claimed_pids or p["age_s"] < MIN_AGE_UNCLAIMED:
            continue
        # only things that LOOK like they are meant to persist — a listener, a daemon, a browser
        if re.search(r"--remote-debugging-port|--user-data-dir|gateway run|serve|daemon|\bacp\b", p["cmd"]):
            unclaimed.append({"pid": p["pid"], "rss_mb": round(p["rss_kb"] / 1024),
                              "age_h": p["age_s"] // 3600, "cmd": p["cmd"][:120]})
    unclaimed.sort(key=lambda x: -x["rss_mb"])
    return {"marked_running": marked, "marked_dead": dead,
            "running_unclaimed": unclaimed[:12],
            "counts": {"registered": len(d["entries"]), "running": len(marked),
                       "dead": len(dead), "unclaimed": len(unclaimed)},
            "note": ("`marked_dead` is a board item — something a team said it needs is not running. "
                     "`running_unclaimed` is NOT a kill list: the usual answer is that somebody should "
                     "declare it.")}


def protected_patterns() -> list:
    """★ What steward_cleanup must never offer. A veto, read fresh each scan."""
    return [e["match"] for e in _load()["entries"].values()]


def self_test() -> int:
    """★ NEGATIVE CONTROL. Two failures matter: reporting something as running when it is not, and
    failing to veto a registered process in the cleanup tool."""
    fails = []
    d = _load(); before = dict(d["entries"])
    try:
        register("_selftest_ghost", r"khaoss-no-such-process-xyz", "probe", "test", "test")
        st = status()
        ghost = next((r for r in st["marked_dead"] if r["name"] == "_selftest_ghost"), None)
        if not ghost:
            fails.append("a registered process that cannot be running was not reported dead — the "
                         "detector cannot say no")
        if any(r["name"] == "_selftest_ghost" for r in st["marked_running"]):
            fails.append("a nonexistent process was reported RUNNING")
        # the veto must reach the cleanup tool
        register("_selftest_veto", r"chromium.*cdpprof", "probe", "test", "test")
        if r"chromium.*cdpprof" not in protected_patterns():
            fails.append("a registered pattern is not exported as protected — the veto would not reach "
                         "steward_cleanup and the browser could be offered for reclamation again")
    finally:
        d = _load(); d["entries"] = before; _save(d)
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ persistent_registry self_test: reports a required-but-absent process as dead, never as "
          "running, and exports its patterns as a veto"
          if not fails else "  ✗ persistent_registry self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if a and a[0] == "--register" and len(a) >= 6:
        print(json.dumps(register(a[1], a[2], a[3], a[4], a[5],
                                  a[6] if len(a) > 6 else "", a[7] if len(a) > 7 else None), indent=2))
        return 0
    if a and a[0] == "--unregister" and len(a) >= 2:
        print(json.dumps(unregister(a[1]), indent=2)); return 0
    s = status()
    print(f"\n  MUST STAY OPEN — {s['counts']['registered']} declared")
    for r in s["marked_running"]:
        dur = r.get("durable_state")
        print(f"    ✓ {r['name']:26s} {r['rss_mb']:>5}MB  pids {r['pids']}")
        print(f"        {r['why']}")
        print(f"        owner {r['owner']} · steward {r['steward']}")
        if dur and "/tmp" in dur:
            print(f"        ★ STATE IS NOT DURABLE: {dur} — a reboot or a /tmp clean loses it")
    for r in s["marked_dead"]:
        print(f"    ✗ {r['name']:26s} NOT RUNNING — {r.get('breaks_if_stopped') or r['why']}")
        print(f"        steward {r['steward']} owns the restart")
    if s["running_unclaimed"]:
        print(f"\n  LONG-LIVED AND UNCLAIMED — nobody has declared these ({len(s['running_unclaimed'])})")
        for u in s["running_unclaimed"][:6]:
            print(f"    pid {u['pid']:>8} {u['rss_mb']:>5}MB  {u['age_h']:>3}h  {u['cmd'][:78]}")
    print(f"\n  {s['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
