#!/usr/bin/env python3
"""steward_cleanup.py — find processes worth reclaiming, and REFUSE to guess about the rest.

★★★ WHY THIS FILE IS CAUTIOUS TO THE POINT OF BEING BORING. Asked to free RAM, I ran
    `ps | grep -E "chromium.*(cprof|headless)"`, found 203MB across six processes idle for six days,
    and was one command from killing them. They were the OPERATOR'S OWN BROWSER — a real chromium with
    extensions and a crash handler, matched because my pattern was loose and I had spawned headless
    chromium under similar-looking profile directories earlier.
    ★ Killing a person's browser to reclaim 200MB is not a trade anyone would accept, and the failure
      mode of a cleanup tool is unrecoverable in a way that a monitoring tool's is not.

★★ SO THE RULE IS POSITIVE IDENTIFICATION, NOT PATTERN ABSENCE. Nothing is proposed for reclamation
    unless it can be shown to be agent-spawned — a scratchpad user-data-dir, a headless flag, a working
    directory inside a session temp, a dead parent plus an agent marker. Anything that cannot be
    positively identified is reported as UNKNOWN and left alone, however much memory it holds.
    "It did not match my protect-list" is not identification. That reasoning is what nearly killed the
    browser.

★ AND IT NEVER KILLS ANYTHING BY ITSELF unless asked with --reclaim, which still refuses everything it
  has not positively identified. The default is a report.
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

# ★ Things that are the operator's, or the system's, and are never candidates whatever else matches.
#   This is a backstop, NOT the safety mechanism — positive identification is.
NEVER = re.compile(
    r"(firefox|thunderbird|gnome-shell|gnome-session|systemd|dbus|pipewire|wireplumber|Xwayland|"
    r"/usr/share/code/|code --type|electron|aionui|hermes|pulseaudio|gvfs|tracker|evolution|"
    r"NetworkManager|sshd|cron|kwin|plasma)", re.I)

# ★ Positive markers that a process was started BY AN AGENT for a transient purpose.
AGENT_MARKERS = [
    (re.compile(r"--user-data-dir=\S*(/tmp/claude-|scratchpad|cprof)"), "a scratchpad browser profile"),
    (re.compile(r"--headless"), "a headless browser run"),
    (re.compile(r"--virtual-time-budget|--screenshot="), "a screenshot run"),
    (re.compile(r"/tmp/claude-\d+/"), "started from a session temp directory"),
    (re.compile(r"xvfb-run|Xvfb :\d+"), "a virtual display started for a capture"),
]

IDLE_SECONDS = 3600          # ★ an hour. Labelled: long enough that nothing mid-task is caught.


def _procs() -> list:
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace").replace("\0", " ").strip()
            if not cmd:
                continue
            st = open(f"/proc/{pid}/status").read()
            rss = int(next(l for l in st.splitlines() if l.startswith("VmRSS")).split()[1])
            ppid = int(next(l for l in st.splitlines() if l.startswith("PPid")).split()[1])
            started = os.path.getmtime(f"/proc/{pid}")
        except Exception:
            continue
        out.append({"pid": int(pid), "cmd": cmd, "rss_kb": rss, "ppid": ppid,
                    "age_s": int(time.time() - started)})
    return out


def _protected_patterns():
    """★ THE VETO. Anything a team has DECLARED must stay open is never offered, whatever else matches.
    This exists because the browser that is the only route to Proton on a Free plan was, for one
    command, a candidate for reclamation — and nothing anywhere said it mattered."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import persistent_registry, importlib
        importlib.reload(persistent_registry)
        return persistent_registry.protected_patterns()
    except Exception:
        return []


def classify(p: dict) -> tuple[str, str]:
    """Returns (verdict, why). Only `reclaimable` is ever offered for killing."""
    cmd = p["cmd"]
    for pat in _protected_patterns():
        try:
            if re.search(pat, cmd):
                return "declared-persistent", (f"a team has DECLARED this must stay open (matches "
                                               f"{pat!r}) — never a candidate")
        except re.error:
            continue
    # ★ THE SELF-EXCLUSION THAT IS EASY TO FORGET: this very process, and the shell running it, match
    #   every marker they are searching for. `pgrep -f` has caught this house twice.
    if str(os.getpid()) == str(p["pid"]) or "steward_cleanup" in cmd:
        return "self", "this tool, or the shell running it"
    if NEVER.search(cmd):
        return "protected", "matches the never-touch list (operator or system software)"
    # ★★ THREE CATEGORIES THAT ARE NEITHER "OURS TO KILL" NOR TRULY UNKNOWN. Leaving 5.4GB in an
    #    opaque `unknown` bucket is technically safe and practically useless — he cannot act on it. So
    #    the things we CAN name get named, with the action that actually applies to each.
    if "panel_server.py" in cmd:
        return "restartable", (f"the KHAOSS panel, {p['rss_kb']//1024}MB after {p['age_s']//3600}h. Its "
                               f"memory swings widely (1383MB and 345MB observed minutes apart). Do NOT "
                               f"kill it — restart it: it comes back at well under 100MB and the restart "
                               f"is already routine")
    if "claude-code" in cmd and "resources" in cmd:
        return "operator-session", (f"a Claude Code session, {p['rss_kb']//1024}MB, idle "
                                    f"{p['age_s']//3600}h. This is one of HIS chats — killing it loses "
                                    f"the conversation. Only he decides")
    if re.search(r"/usr/bin/python3?\s+\S*(boardwalk|cockpit|run_ui)", cmd):
        return "khaoss-service", (f"a KHAOSS service, {p['rss_kb']//1024}MB. Restartable, but check what "
                                  f"depends on it first")
    marker = next(((why) for rx, why in AGENT_MARKERS if rx.search(cmd)), None)
    if not marker:
        # ★ NOT a candidate. Absence of evidence that it is the operator's is not evidence that it is ours.
        return "unknown", "cannot be positively identified as agent-spawned — left alone on purpose"
    if p["age_s"] < IDLE_SECONDS:
        return "recent", f"{marker}, but only {p['age_s']}s old — may still be in use"
    if p["ppid"] != 1:
        return "parented", f"{marker}, but its parent is still alive (ppid {p['ppid']}) — not orphaned"
    return "reclaimable", f"{marker}, orphaned (parent gone) and idle {p['age_s']//3600}h"


def scan() -> dict:
    rows = [dict(p, **dict(zip(("verdict", "why"), classify(p)))) for p in _procs()]
    buckets = {}
    for r in rows:
        buckets.setdefault(r["verdict"], []).append(r)
    reclaim = sorted(buckets.get("reclaimable", []), key=lambda r: -r["rss_kb"])
    advice = []
    for kind, label in (("restartable", "restart to reclaim"),
                        ("operator-session", "his call — killing loses a conversation"),
                        ("khaoss-service", "restartable, check dependants first")):
        rows_k = sorted(buckets.get(kind, []), key=lambda r: -r["rss_kb"])
        if rows_k:
            advice.append({"kind": kind, "action": label,
                           "total_mb": round(sum(r["rss_kb"] for r in rows_k) / 1024),
                           "items": [{"pid": r["pid"], "mb": r["rss_kb"] // 1024, "why": r["why"]}
                                     for r in rows_k[:6]]})
    return {
        "reclaimable": reclaim,
        "advice": advice,
        "reclaimable_mb": round(sum(r["rss_kb"] for r in reclaim) / 1024),
        "counts": {k: len(v) for k, v in buckets.items()},
        "unknown_mb": round(sum(r["rss_kb"] for r in buckets.get("unknown", [])) / 1024),
        "note": ("`unknown` is deliberately never touched. Positive identification is the safety "
                 "mechanism; not matching a protect-list is not identification."),
    }


def reclaim(dry: bool = True) -> dict:
    s = scan()
    killed, failed = [], []
    for r in s["reclaimable"]:
        if dry:
            continue
        try:
            os.kill(r["pid"], 15)
            killed.append(r["pid"])
        except Exception as e:
            failed.append({"pid": r["pid"], "error": e.__class__.__name__})
    return {**s, "dry_run": dry, "killed": killed, "failed": failed}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is proposing to kill something of the operator's.
    So the test feeds it the real browser command line it nearly killed, and fails if that is offered."""
    fails = []
    real_browser = ("/usr/lib/chromium/chromium --show-component-extension-options "
                    "--enable-gpu-rasterization --no-default-browser-check --disable-pings")
    v, why = classify({"pid": 999999, "cmd": real_browser, "rss_kb": 200000, "ppid": 1, "age_s": 500000})
    if v == "reclaimable":
        fails.append("★ THE OPERATOR'S OWN BROWSER WAS OFFERED FOR RECLAMATION — the exact near-miss "
                     "this file exists to prevent")
    firefox = "/usr/lib/firefox-esr/firefox-esr -contentproc -isForBrowser"
    if classify({"pid": 999998, "cmd": firefox, "rss_kb": 2600000, "ppid": 1, "age_s": 500000})[0] != "protected":
        fails.append("firefox was not protected")
    # and it must still be able to say YES, or its NO means nothing
    mine = ("/usr/lib/chromium/chromium --headless=new --user-data-dir=/tmp/claude-1000/x/scratchpad/cprof "
            "--screenshot=/tmp/x.png")
    v2, _ = classify({"pid": 999997, "cmd": mine, "rss_kb": 60000, "ppid": 1, "age_s": 90000})
    if v2 != "reclaimable":
        fails.append(f"a genuinely orphaned headless screenshot run was NOT offered (got {v2!r}) — a "
                     f"cleanup tool that can only say no reclaims nothing")
    # recent work must be spared
    v3, _ = classify({"pid": 999996, "cmd": mine, "rss_kb": 60000, "ppid": 1, "age_s": 30})
    if v3 == "reclaimable":
        fails.append("a 30-second-old run was offered — work in progress would be killed")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ steward_cleanup self_test: refuses the operator's browser and firefox, still offers a "
          "genuinely orphaned headless run, and spares recent work"
          if not fails else "  ✗ steward_cleanup self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    if "--json" in a:
        print(json.dumps(scan(), indent=2)); return 0
    if "--reclaim" in a:
        r = reclaim(dry=False)
        print(f"  reclaimed {len(r['killed'])} process(es), ~{r['reclaimable_mb']}MB")
        for p in r["killed"]:
            print(f"    killed pid {p}")
        for f in r["failed"]:
            print(f"    could not kill {f['pid']}: {f['error']}")
        return 0
    s = scan()
    print(f"\n  PROCESS HYGIENE — {s['reclaimable_mb']}MB reclaimable")
    for r in s["reclaimable"]:
        print(f"    pid {r['pid']:>8}  {r['rss_kb']/1024:6.0f}MB  {r['why']}")
        print(f"             {r['cmd'][:96]}")
    if not s["reclaimable"]:
        print("    nothing is safely reclaimable right now.")
    for a in s.get("advice", []):
        print(f"\n  {a['kind'].upper()} — {a['total_mb']}MB · {a['action']}")
        for i in a["items"]:
            print(f"    pid {i['pid']:>8}  {i['mb']:>5}MB  {i['why'][:96]}")
    print(f"\n  {json.dumps(s['counts'])}")
    print(f"  ★ {s['unknown_mb']}MB sits in processes that could not be positively identified. "
          f"They are NOT candidates.")
    print(f"  {s['note']}")
    print(f"\n  to act: steward_cleanup.py --reclaim   (only touches the list above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
