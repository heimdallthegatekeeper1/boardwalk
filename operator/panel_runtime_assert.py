#!/usr/bin/env python3
"""INV-115 · the serving panel runs on the RIGHT interpreter, with the arguments it was given.

★★★ WHAT HAPPENED (2026-08-29). /services/restart-panel relaunched with bare `python3`. On this box
    that resolves to the PYENV interpreter, which does not have `websocket` — so every restart silently
    moved the panel off system python and BROKE the AionUi handoff path. And it dropped every argument,
    so a panel started as `--bind 127.0.0.1,100.78.124.29` came back bound to loopback only, taking the
    Tailscale interface down. Neither loss was reported: the panel restarted, answered on 127.0.0.1,
    and looked completely healthy.
★★ The failure is invisible from the outside. A panel serving 200s on loopback is indistinguishable
    from a working one until you exercise the one feature whose dependency is missing — which is why
    this was found by a delivery target that claimed `live` and refused with a missing-module error,
    not by any health check.
★ THE MEASUREMENT IS OF THE RUNNING PROCESS, not of the launch script — the script was fixed, and a
    fixed script proves nothing about a process that has been running since before the fix (L40).
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

PORT = 7842
NEEDED = ("websocket",)          # modules panel features import at call time, not at import time


def _pid_on_port(port: int):
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"],
                             capture_output=True, timeout=6).stdout.decode()
    except Exception:
        return None
    for tok in out.replace(",", " ").split():
        if tok.startswith("pid="):
            return tok.split("=", 1)[1]
    return None


def check() -> tuple[bool, list]:
    notes, ok = [], True
    pid = _pid_on_port(PORT)
    if not pid:
        # ★ Nothing listening is not a PASS. An invariant that goes quiet when its subject is absent
        #   is the vacuous-pass shape this house has been bitten by repeatedly.
        return False, [f"nothing is listening on :{PORT} — the panel is DOWN, which is not a pass"]
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
    except Exception as e:
        return False, [f"cannot read the interpreter of pid {pid} ({e.__class__.__name__})"]
    notes.append(f"pid {pid} · {exe}")
    if "pyenv" in exe or "/.pyenv/" in exe:
        ok = False
        notes.append("★ RUNNING UNDER PYENV — this interpreter lacks modules the panel needs at call "
                     "time (websocket). Handoff delivery will fail with 'No module named websocket' "
                     "while every health check stays green. Relaunch with /usr/bin/python3.")
    for mod in NEEDED:
        r = subprocess.run([exe, "-c", f"import {mod}"], capture_output=True, timeout=15)
        if r.returncode != 0:
            ok = False
            notes.append(f"★ the serving interpreter cannot import {mod!r} — any feature that needs it "
                         f"fails only when exercised, never at startup")
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    except Exception:
        cmd = []
    if not any(a == "--bind" for a in cmd):
        notes.append("· no --bind argument: serving loopback only. That is correct if it was launched "
                     "that way, and a silent downgrade if it was not — the restart path used to drop it.")
    return ok, notes


def self_test() -> int:
    """★ NEGATIVE CONTROL: prove the pyenv detection can FIRE, and that a dead port is not a pass."""
    fails = []
    saved = globals()["_pid_on_port"]
    try:
        globals()["_pid_on_port"] = lambda p: None
        ok, notes = check()
        if ok:
            fails.append("a DOWN panel was reported as a pass — an invariant that goes quiet when its "
                         "subject is absent proves nothing")
    finally:
        globals()["_pid_on_port"] = saved
    ok, notes = check()
    if not any("pid" in n for n in notes):
        fails.append("the live check reports no interpreter at all, so its verdict is not about anything")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ panel_runtime_assert self_test: a down panel FAILS, and the live check names the "
          "interpreter it judged" if not fails else "  ✗ panel_runtime_assert self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    ok, notes = check()
    print(("  ✓ INV-115 panel runtime is correct" if ok else "  ✗ INV-115 PANEL RUNTIME DEGRADED"))
    for n in notes:
        print(f"      {n}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
