#!/usr/bin/env python3
"""managed_check.py — is a service MANAGED by its unit, or merely ADOPTED by systemd?

★★★ WHY THIS EXISTS
    The voice seat's daemon and bubble were started with `setsid`, which reparents to init. Under
    `systemd --user` they therefore showed PPID 1618 — the same parent a properly managed service
    has — so `ps` made them look managed. They were orphans. **A reboot and the voice assistant
    was simply gone, with nothing to bring it back**, and it had been that way its whole life.

    ★★ THE TRAP IS THAT THE OBVIOUS CHECK REPRODUCES THE CONFUSION. Their first orphan detector
    counted processes whose PPID was systemd's and reported "orphans left: 3" for three correctly
    managed services — because MANAGED PROCESSES ALSO HAVE systemd AS PARENT. The check restated
    the ambiguity instead of resolving it.

★ THE DISCRIMINATOR IS THE CGROUP, NOT THE PARENT:
      managed  -> /user.slice/user-1000.slice/operator@example.invalid/app.slice/<unit>.service
      adopted  -> anything else, including a bare app.slice or a scope
    A process can be parented to systemd and live outside its unit's cgroup. Only the cgroup says
    which unit, if any, systemd will stop, restart and bring back after a reboot.

★★ AND "active" IS NOT THE SAME QUESTION. `systemctl is-active` reports the UNIT. A service can be
   serving traffic from an adopted process while its unit reads inactive or failed — which is
   exactly what happened here: :8770 answered 200 from an orphan while the unit restart-looped 21
   times against a port it could not bind. So this checks THREE things that are routinely
   conflated: is the unit enabled (survives reboot), is it active, and is the process actually in
   its cgroup.
"""
from __future__ import annotations

import json
import subprocess
import sys

# Services this estate expects to survive a reboot. A unit missing here is not checked; a unit
# here that is not installed is reported, never silently skipped.
EXPECTED = [
    "khaoss-panel.service",
    "khaoss-voicebox.service",
    "khaoss-jarvis-daemon.service",
    "khaoss-jarvis-bubble.service",
    "khaoss-kb-capture.timer",
    "khaoss-headscale.service",
]


def _sc(*args: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _cgroup(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cgroup") as fh:
            return fh.readline().strip()
    except Exception:
        return ""


def check_one(unit: str) -> dict:
    enabled = _sc("is-enabled", unit) or "unknown"
    active = _sc("is-active", unit) or "unknown"
    pid = _sc("show", unit, "-p", "MainPID", "--value")
    row = {"unit": unit, "enabled": enabled, "active": active, "pid": pid or None}

    if enabled in ("not-found", "", "unknown") and active in ("inactive", "unknown"):
        row["verdict"] = "NOT_INSTALLED"
        row["detail"] = "no such unit — reported rather than skipped"
        return row

    if unit.endswith(".timer"):
        # ★ A timer has no MainPID. For it, "survives a reboot" IS the enabled flag.
        row["verdict"] = "OK" if enabled == "enabled" else "WONT_SURVIVE_REBOOT"
        row["detail"] = f"timer enabled={enabled} active={active}"
        return row

    if not pid or pid == "0":
        row["verdict"] = "NOT_RUNNING" if enabled == "enabled" else "WONT_SURVIVE_REBOOT"
        row["detail"] = f"no MainPID (enabled={enabled}, active={active})"
        return row

    cg = _cgroup(pid)
    row["cgroup"] = cg
    if f"/{unit}" in cg:
        row["verdict"] = "OK" if enabled == "enabled" else "RUNNING_BUT_WONT_SURVIVE_REBOOT"
        row["detail"] = f"pid {pid} is in its own unit's cgroup (enabled={enabled})"
    else:
        # ★★ This is the case the whole file exists for.
        row["verdict"] = "ADOPTED"
        row["detail"] = (f"pid {pid} is NOT in {unit}'s cgroup — it is parented to systemd but "
                         f"systemd does not own it. It will NOT come back after a reboot. "
                         f"cgroup={cg or 'unreadable'}")
    return row


def check() -> dict:
    return {"rows": [check_one(u) for u in EXPECTED]}


def self_test() -> int:
    """★ Controls on the DISCRIMINATION, not on this box's current state."""
    global _cgroup          # ★ must precede ANY use of the name in this function
    fails = 0

    def chk(label, ok):
        nonlocal fails
        fails += (not ok)
        print(f"  {'✓' if ok else '✗'} {label}")

    import os
    me = str(os.getpid())
    cg = _cgroup(me)
    chk("★ can read a cgroup at all (else every verdict is meaningless)", bool(cg))
    chk("★ a bogus unit reports NOT_INSTALLED, never OK",
        check_one("khaoss-definitely-not-a-unit.service")["verdict"] == "NOT_INSTALLED")

    # ★★ The discrimination itself: same PPID, different cgroup must give different verdicts.
    real = check_one("khaoss-panel.service")
    chk("a genuinely managed unit reads OK", real["verdict"] in ("OK", "NOT_RUNNING"))
    if real.get("cgroup"):
        chk("  and its cgroup names its own unit", "khaoss-panel.service" in real["cgroup"])

    # ★★★ NEGATIVE CONTROL — the estate currently reads 6/6 OK, so without this the checker has
    #   only ever seen green and CANNOT BE SHOWN TO CATCH ANYTHING. A measurement that cannot vary
    #   is not a measurement. Plant an ADOPTED process: same parent, different cgroup.
    real_cgroup = _cgroup
    try:
        _cgroup = lambda pid: "0::/user.slice/user-1000.slice/session-3.scope"   # a scope, not a unit
        planted = check_one("khaoss-panel.service")
        chk("★★★ a process in the WRONG cgroup reads ADOPTED",
            planted["verdict"] == "ADOPTED")
        chk("  and it says the reboot consequence out loud",
            "NOT come back after a reboot" in planted.get("detail", ""))
        _cgroup = lambda pid: ""                                                  # unreadable
        blind = check_one("khaoss-panel.service")
        chk("★ an UNREADABLE cgroup is ADOPTED, never OK (could-not-look is not fine)",
            blind["verdict"] == "ADOPTED")
    finally:
        _cgroup = real_cgroup
    chk("  and the real verdict is restored after the plant",
        check_one("khaoss-panel.service")["verdict"] in ("OK", "NOT_RUNNING"))
    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILED'} — "
          f"the verdict comes from the cgroup, not from the parent.")
    return 1 if fails else 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if args == ["--self-test"]:
        return self_test()
    if args and args != ["--json"]:
        print(f"unknown argument(s): {' '.join(args)}\n"
              f"usage: managed_check.py [--json | --self-test]", file=sys.stderr)
        return 2
    d = check()
    if args == ["--json"]:
        print(json.dumps(d, indent=2))
    bad = 0
    for r in d["rows"]:
        v = r["verdict"]
        if v in ("ADOPTED", "WONT_SURVIVE_REBOOT", "RUNNING_BUT_WONT_SURVIVE_REBOOT"):
            bad += 1
        if args != ["--json"]:
            mark = "★" if v != "OK" else " "
            print(f"  {mark} {v:<32} {r['unit']}")
            if v != "OK":
                print(f"      {r['detail']}")
    if args != ["--json"]:
        ok = sum(1 for r in d["rows"] if r["verdict"] == "OK")
        print(f"\n  {len(d['rows'])} unit(s) · {ok} OK · {bad} that will not survive a reboot")
        print("  ★ 'managed' is decided by the CGROUP. A process parented to systemd may still be "
              "an orphan\n    systemd does not own — which is how a seat ran its whole life "
              "unable to come back.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
