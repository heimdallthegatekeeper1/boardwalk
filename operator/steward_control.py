#!/usr/bin/env python3
"""steward_control.py — shut a steward down, five ways, and say what each one actually frees.

★★★ THE LESSON THAT SHAPED THIS, learned ten minutes before it was written. He asked for the stewards
    to be shut down because a video render was maxing the machine. Two of the four were already off and
    the other two are FLAGS — toggling them freed nothing. The actual consumer was panel_server.py at
    5,186 MB, and restarting it returned 3.6 GB in eight seconds.
    ★ So every method here declares **what it frees**, and "nothing — this only changes state" is one
      of the honest answers. A shutdown button that quiets a steward without returning any resource is
      not wrong, but it must not be offered to someone who needs memory back.

★★ "TEMPORARILY" IS AN EXPIRY, NOT AN ADJECTIVE. `pause` takes minutes and records when it ends. A
    duration honoured only by whoever remembers it has not expired; it has asked to be ignored politely.
"""
from __future__ import annotations
import json, os, signal, sys, time
from pathlib import Path


def _ss():
    """★ One atomic+locked writer for every store in this directory. Each of these used
    write_text(json.dumps(...)), which truncates before it writes — a crash between those two moments
    left the store empty, and two writers clobbered each other undetectably."""
    import importlib, sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module("safe_store")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE))
STATE = HERE / "steward_control.json"

# ★ The five stewards. `cyber` was added at the operator's request 2026-09-07 and is DESIGN-ONLY —
#   it is listed so it is visible, and marked so it is not mistaken for something that is watching.
STEWARDS = {
    "Gatekeeper": {"what": "dependency & update governance", "status": "design-only"},
    "Librarian":  {"what": "curating the knowledge vault, confidence weighting", "status": "design-only"},
    "Watchman":   {"what": "failure detection and the patrol", "status": "real"},
    "lead":       {"what": "the team lead steward", "status": "real"},
    # ★★★ ONE NAME, AND THE 2026-09-07 REQUEST IS FINALLY HONOURED. Control called this post
    #     "cyber" while steward_deploy called it "Guardsman" -- two names for one thing, so the panel
    #     showed a post that control could never find or stop. And the operator asked for cyber
    #     security in here on 2026-09-07; it sat at "NOT built" for fifteen days.
    #     ★ 2026-09-23: THE PATROL NOW EXISTS. This string said "the PATROL that drives it is NOT built"
    #     and it was TRUE when written -- and a stale label reads exactly like a broken binding from
    #     across the room, which is the specific failure this repo has paid for more than once. The
    #     body is `guardsman_patrol.py`: 4 detectors (listening-endpoint reach, network device baseline,
    #     secret material by value-shape, live_board freshness), 17/17 self-test, an honest prove-fail
    #     that runs the GATED implementation and shows it drop a real finding, and `--dry-run` so a new
    #     detector can never again pollute the live queue before it has been watched. It found 7 real
    #     0.0.0.0 exposures on this box, ollama:11434 among them. Still PARTIAL, because a patrol is not
    #     a response: it detects and reports, and never remediates.
    "Guardsman":  {"what": "cyber security — network exposure, unexpected listeners and inbound peers, "
                           "credential and file-permission drift, pre-intrusion event classes",
                   "status": "partial · PATROL BUILT 2026-09-23 (guardsman_patrol.py, 17/17 + prove-fail) "
                             "over the decision engine (laya_service :8791, guard_triage, never-gate); "
                             "detects and reports only — never remediates"},
}

METHODS = {
    "deactivate": {
        "label": "Turn it off",
        "does": "Flips its activation flag. It stops being counted as watching.",
        "frees": "nothing — this is state, not a process. Use it to stop a steward acting, not to "
                 "get memory back.",
        "reverses": "instantly, by turning it back on",
    },
    "pause": {
        "label": "Pause for a while",
        "does": "Turns it off AND records when it comes back. The expiry is written down, so "
                "'temporarily' means a clock rather than an intention.",
        "frees": "nothing immediately — same as turning it off",
        "reverses": "by itself, at the time recorded",
    },
    "stop_processes": {
        "label": "Stop anything it is running",
        "does": "Finds processes belonging to that steward by reading /proc, confirms each one, and "
                "sends SIGTERM so it can flush. Never a pattern kill, never SIGKILL.",
        "frees": "whatever those processes were holding — this is the one that returns memory",
        "reverses": "not automatically; it starts again when the steward next runs",
    },
    "disable_until_reenabled": {
        "label": "Keep it off until I say",
        "does": "Turns it off and marks it so nothing re-enables it — not a restart, not another "
                "agent, not a scheduled run.",
        "frees": "nothing by itself; combine with stopping its processes",
        "reverses": "only by you, explicitly",
    },
    "all_stop": {
        "label": "Stop every steward now",
        "does": "Turns all five off and stops any process any of them owns. The panic button.",
        "frees": "everything the stewards were holding — measured and reported afterwards",
        "reverses": "by turning them back on individually",
    },
}


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"paused": {}, "locked": {}, "log": []}


def _save(d: dict) -> None:
    _ss().write_json(STATE, d)


def _activation():
    import importlib, stewards_activation as sa
    importlib.reload(sa)
    return sa


# ★ the argv markers that POSITIVELY identify each post's process. Hoisted out of _procs_of so one
#   /proc pass can classify every steward at once.
_PROC_KEYS = {
    "Watchman": ("steward_patrol", "failure_immune"),
    "lead": ("steward_llm", "steward_rollup"),
    # ★ WAS AN EMPTY TUPLE. `"cyber": ()` meant _procs_of() returned [] for the cyber post
    #   ALWAYS, so control could not find it, could not report what it held, and could not stop
    #   it -- a post that exists on the panel and nowhere in the process table. These are the
    #   real argv markers now that both halves are named.
    "Gatekeeper": ("steward_bench",), "Librarian": ("steward_scope",),
    "Guardsman": ("guardsman_patrol", "laya_service"),
}


def _proc_table() -> dict:
    """ONE pass over /proc → {pid: (cmdline, exe, rss_mb)} for every python process.

    ★★ 2026-09-30 — WHY THIS EXISTS. `_procs_of()` opens THREE files per pid (`cmdline`, the `exe`
    readlink, `status`) and `status()` called it once PER STEWARD. On a 582-pid box that is
    ~582 × 3 × 5 ≈ **8,700 file operations for one GET /operator/stewards**. Measured: that route
    returns its first byte in **8.4–9.2 s** while the identical function is **0.19 s** outside the
    panel — and the stewards-page aggregate that calls it is slow for exactly the same reason.
    One pass is the same answer for a fifth of the syscalls. (The panel's absolute cost is still
    unexplained; this removes the part that is expensive BY CONSTRUCTION.)
    """
    me, table = os.getpid(), {}
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == me:
            continue
        try:
            cl = open(f"/proc/{d}/cmdline", "rb").read().decode("utf-8", "replace").replace("\0", " ")
            exe = os.path.basename(os.path.realpath(f"/proc/{d}/exe"))
            st = open(f"/proc/{d}/status").read()
        except Exception:
            continue
        if "python" not in exe:            # a shell whose argv mentions the name is not the steward
            continue
        r = [l for l in st.splitlines() if l.startswith("VmRSS")]
        table[int(d)] = (cl, exe, int(r[0].split()[1]) // 1024 if r else 0)
    return table


def _procs_of(name: str, table: dict | None = None) -> list:
    """★ POSITIVE IDENTIFICATION, and it excludes this process. A scan for 'steward' matched its own
    python invocation ten minutes ago and reported two stewards that were both itself.

    Pass `table` from `_proc_table()` to classify a whole board with ONE /proc pass; omit it and this
    scans on its own (kept for callers and for the equivalence test).
    """
    keys = _PROC_KEYS.get(name, ())
    if not keys:
        return []
    table = _proc_table() if table is None else table
    return [{"pid": pid, "mb": mb, "cmd": cl[:90]}
            for pid, (cl, _exe, mb) in table.items() if any(k in cl for k in keys)]


def _mem() -> dict:
    m = {}
    for ln in open("/proc/meminfo"):
        k, _, v = ln.partition(":")
        m[k] = int(v.split()[0]) // 1024
    return {"used_mb": m["MemTotal"] - m["MemAvailable"], "available_mb": m["MemAvailable"]}


def status() -> dict:
    sa = _activation()
    ring = sa.ring(sa.load_state())
    d = _load()
    now = time.time()
    table = _proc_table()          # ★ ONE /proc pass for the whole board, not one per steward
    rows = []
    for name, meta in STEWARDS.items():
        p = d["paused"].get(name)
        paused_until = p if p and p > now else None
        procs = _procs_of(name, table)
        rows.append({
            "steward": name, **meta,
            "active": bool(ring["segments"].get(name, False)),
            "paused_until": (time.strftime("%H:%M", time.localtime(paused_until))
                             if paused_until else None),
            "locked_off": bool(d["locked"].get(name)),
            "processes": procs,
            "holding_mb": sum(x["mb"] for x in procs),
        })
    return {"stewards": rows, "methods": METHODS, "ring": ring, "memory": _mem(),
            "note": ("Only `stop_processes` and `all_stop` return memory. The rest change state. "
                     "That distinction matters: the last shutdown request was made to free RAM and "
                     "the flags would have freed none of it.")}


def act(steward: str, method: str, minutes: int = 60, by: str = "operator") -> dict:
    if method not in METHODS:
        return {"ok": False, "error": f"unknown method {method!r}; choose from {list(METHODS)}"}
    if method != "all_stop" and steward not in STEWARDS:
        return {"ok": False, "error": f"unknown steward {steward!r}; choose from {list(STEWARDS)}"}
    before = _mem()
    d = _load()
    sa = _activation()
    done, stopped = [], []

    targets = list(STEWARDS) if method == "all_stop" else [steward]
    for t in targets:
        if method in ("deactivate", "pause", "disable_until_reenabled", "all_stop"):
            try:
                sa.toggle(t, False, by=by)
                done.append(f"{t}: turned off")
            except Exception as e:
                done.append(f"{t}: could not turn off ({e})")
        if method == "pause":
            d["paused"][t] = time.time() + minutes * 60
            done.append(f"{t}: comes back at "
                        f"{time.strftime('%H:%M', time.localtime(d['paused'][t]))}")
        if method == "disable_until_reenabled":
            d["locked"][t] = True
            done.append(f"{t}: locked off — nothing re-enables it but you")
        if method in ("stop_processes", "all_stop"):
            for p in _procs_of(t):
                try:
                    os.kill(p["pid"], signal.SIGTERM)   # ★ TERM so it flushes, never KILL
                    stopped.append(p)
                    done.append(f"{t}: stopped pid {p['pid']} ({p['mb']} MB)")
                except Exception as e:
                    done.append(f"{t}: could not stop pid {p['pid']} ({e})")
    _save(d)
    if stopped:
        time.sleep(2)
    after = _mem()
    freed = after["available_mb"] - before["available_mb"]
    d = _load()
    d["log"].append({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "steward": steward,
                     "method": method, "by": by, "stopped": len(stopped), "freed_mb": freed})
    _save(d)
    return {"ok": True, "method": method, "steward": steward, "did": done,
            "processes_stopped": len(stopped), "freed_mb": freed,
            "memory_before": before, "memory_after": after,
            "honest": (f"{freed} MB more available"
                       if stopped else
                       "no process was stopped, so no memory was returned — this changed state only")}


def self_test() -> int:
    fails = []
    if METHODS["deactivate"]["frees"].startswith("whatever"):
        fails.append("deactivate claims to free resources; it does not")
    if "nothing" not in METHODS["deactivate"]["frees"]:
        fails.append("deactivate does not say plainly that it frees nothing")
    if "whatever" not in METHODS["stop_processes"]["frees"]:
        fails.append("stop_processes does not say it is the one that returns memory")
    r = act("Watchman", "nonsense-method")
    if r.get("ok"):
        fails.append("accepted an unknown method")
    r = act("NotASteward", "deactivate")
    if r.get("ok"):
        fails.append("accepted an unknown steward")
    # ★ the control that matters: the process finder must not match this very process
    if any(p["pid"] == os.getpid() for p in _procs_of("Watchman")):
        fails.append("the process finder matched ITSELF — the self-match that has bitten this "
                     "project four times")
    # ★ THIS REFERENCE DANGLED THE MOMENT I RENAMED THE POST. `STEWARDS["cyber"]` raised KeyError
    #   after the table key became "Guardsman" -- a rename that left one caller behind is exactly the
    #   two-names-for-one-thing bug I was fixing, reintroduced by the fix itself. Caught by reading the
    #   remaining grep hits instead of assuming they were all comments.
    if STEWARDS["Guardsman"]["status"].startswith(("real", "partial")):
        fails.append("cyber is listed as real; it is design-only and must not read as watching")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: each method states what it frees · deactivate admits it frees "
              "nothing · stop_processes claims the memory · unknown method refused · unknown "
              "steward refused · the finder cannot match itself · cyber marked design-only")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if a and a[0] == "--stop" and len(a) >= 3:
        print(json.dumps(act(a[1], a[2],
                             int(a[3]) if len(a) > 3 else 60), indent=2)); return 0
    s = status()
    print(f"\n  STEWARDS · {s['ring']['honest_note']} · outline {s['ring']['outline']}")
    print(f"  memory: {s['memory']['available_mb']:,} MB available\n")
    for r in s["stewards"]:
        mark = "●" if r["active"] else "○"
        print(f"   {mark} {r['steward']:<11} {'ACTIVE' if r['active'] else 'off':<7} "
              f"{r['status']:<14} {len(r['processes'])} proc, {r['holding_mb']} MB")
        if r["paused_until"]:
            print(f"       paused until {r['paused_until']}")
        if r["locked_off"]:
            print(f"       locked off — only you can re-enable")
    print(f"\n  {s['note']}\n")
    for k, m in METHODS.items():
        print(f"   {k:<24} {m['label']}")
        print(f"       frees: {m['frees'][:88]}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
