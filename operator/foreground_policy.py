#!/usr/bin/env python3
"""foreground_policy.py — nothing an agent launches may take the screen while he is working.

★★★ WHY. He was working, a window came up in front of him, and he pressed the spacebar into it before
    he could read what it was. Spacebar is not harmless on a raised window: on a focused button it
    ACTIVATES it, and on a browser confirm dialog it accepts the default. The panel has buttons that
    stop services and move logins. Nothing bad happened this time -- checked, and the audit journal,
    the outbox and the transplant record all show no action -- but "nothing happened" was luck, and
    the mechanism that produced the risk is still there.

★★ THE MIDDLE GROUND HE ASKED FOR ALREADY EXISTS ON THIS BOX. Headless is not equivalent: downsub
    blocks headless permanently, and Cloudflare treats it differently, which is why capture runs
    headed under Xvfb :91. So "headed but not on his screen" is a real, proven third state here --
    the browser renders exactly as a real one, on a display he is not looking at.

★ THE POLICY IS A DEFAULT, NOT A CAGE. `quiet` is the default because the cost of being wrong is his
    attention, which he cannot get back. `visible` exists for when he WANTS to watch -- a guided
    login, a debugging session -- and is one setting away. Modes are named for what he experiences,
    not for the mechanism.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, time
from pathlib import Path


def _ss():
    """★ One atomic+locked writer for every store in this directory. Each of these used
    write_text(json.dumps(...)), which truncates before it writes — a crash between those two moments
    left the store empty, and two writers clobbered each other undetectably."""
    import importlib, sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module("safe_store")

HOME = Path.home()
SETTINGS = Path(__file__).resolve().parent.parent / "operator" / "khaoss-foreground.json"
QUIET_DISPLAY = ":91"            # the Xvfb the capture pipeline already runs on
DEFAULT = {
    "mode": "quiet",
    "quiet_display": QUIET_DISPLAY,
    "note": "quiet = agents render on an off-screen display and can never raise a window over you.",
}
MODES = {
    "quiet":    "Agents render on an off-screen display. Nothing they open can appear over your work.",
    "visible":  "Agents use your screen. You can watch what they do — and they can steal focus.",
    "offscreen":"Your screen, but windows are pushed off the visible area. A fallback if the "
                "off-screen display cannot start; it is weaker, because a window can still be raised.",
}


def load() -> dict:
    try:
        d = json.loads(SETTINGS.read_text())
        return {**DEFAULT, **d}
    except Exception:
        return dict(DEFAULT)


def save(d: dict) -> None:
    _ss().write_json(SETTINGS, d)


def set_mode(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
    d = load(); d["mode"] = mode; d["changed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save(d)
    return d


def _display_alive(disp: str) -> bool:
    if not shutil.which("xdpyinfo"):
        return False
    return subprocess.run(["xdpyinfo"], env={**os.environ, "DISPLAY": disp},
                          capture_output=True, timeout=6).returncode == 0


def ensure_quiet_display(disp: str | None = None) -> tuple[bool, str]:
    """Start the off-screen display if it is not already up. Returns (ok, why)."""
    disp = disp or load().get("quiet_display", QUIET_DISPLAY)
    if _display_alive(disp):
        return True, f"{disp} already up"
    if not shutil.which("Xvfb"):
        return False, "Xvfb is not installed — cannot render off-screen"
    subprocess.Popen(["Xvfb", disp, "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        if _display_alive(disp):
            return True, f"started {disp}"
    return False, f"{disp} did not come up within 10s"


def launch_env(base: dict | None = None) -> tuple[dict, str]:
    """The environment a launcher should use, plus a one-line explanation of what it did.

    ★ IT NEVER SILENTLY FALLS BACK TO HIS SCREEN. If quiet is asked for and the off-screen display
      cannot start, the caller is told so in the reason string and gets `offscreen` instead of the
      thing it asked to avoid. A policy that degrades into its own opposite without saying so is
      worse than no policy.
    """
    env = dict(base or os.environ)
    d = load()
    mode = d.get("mode", "quiet")
    if mode == "visible":
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        return env, f"visible: using your screen ({env['DISPLAY']}) — this CAN take focus"
    if mode == "quiet":
        ok, why = ensure_quiet_display(d.get("quiet_display", QUIET_DISPLAY))
        if ok:
            env["DISPLAY"] = d.get("quiet_display", QUIET_DISPLAY)
            return env, f"quiet: rendering on {env['DISPLAY']} — cannot appear over your work ({why})"
        env["DISPLAY"] = env.get("DISPLAY") or ":0"
        env["_KHAOSS_FOREGROUND_DEGRADED"] = why
        return env, (f"quiet was requested but FAILED ({why}); falling back to windows pushed "
                     f"off-screen on {env['DISPLAY']} — weaker, and a window could still be raised")
    env["DISPLAY"] = env.get("DISPLAY") or ":0"
    return env, f"offscreen: your screen ({env['DISPLAY']}), windows positioned off the visible area"


def window_args() -> list:
    """Extra browser flags for the current mode. Off-screen placement is belt-and-braces on top of
    the display choice, and is what makes `offscreen` mean anything at all."""
    mode = load().get("mode", "quiet")
    if mode == "visible":
        return []
    # -32000 is the conventional 'far away' position; browsers accept it and it is off every screen.
    return ["--window-position=-32000,-32000", "--window-size=1280,1024"]


def status() -> dict:
    d = load()
    disp = d.get("quiet_display", QUIET_DISPLAY)
    return {"mode": d.get("mode", "quiet"), "modes": MODES, "quiet_display": disp,
            "quiet_display_up": _display_alive(disp),
            "your_display": os.environ.get("DISPLAY", ":0"),
            "changed_at": d.get("changed_at"),
            "settings_file": str(SETTINGS)}


def self_test() -> int:
    """INV-114 negative control — each check must be able to say the other thing."""
    fails = []
    before = load()
    try:
        try:
            set_mode("nonsense-mode"); fails.append("set_mode accepted an unknown mode")
        except ValueError:
            pass
        set_mode("visible")
        env, why = launch_env({"DISPLAY": ":0"})
        if env.get("DISPLAY") != ":0" or "CAN take focus" not in why:
            fails.append(f"visible mode did not use your screen or did not warn: {why}")
        set_mode("quiet")
        env, why = launch_env({"DISPLAY": ":0"})
        if env.get("DISPLAY") == ":0" and "FAILED" not in why:
            fails.append("quiet mode returned YOUR display without reporting a failure — "
                         "that is the silent-degradation the policy exists to prevent")
        if not window_args():
            fails.append("quiet mode returned no off-screen window args")
        set_mode("visible")
        if window_args():
            fails.append("visible mode still pushed windows off-screen")
    finally:
        save(before)
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  4 controls pass: unknown mode refused · visible warns about focus · "
              "quiet never silently returns your screen · window args follow the mode")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if a and a[0] == "--set":
        if len(a) < 2:
            print(f"usage: --set {'|'.join(MODES)}", file=sys.stderr); return 2
        try:
            d = set_mode(a[1])
        except ValueError as e:
            print(f"  {e}", file=sys.stderr); return 2
        print(f"  mode → {d['mode']}\n  {MODES[d['mode']]}")
        return 0
    s = status()
    print(f"\n  FOREGROUND POLICY — what agents may put on your screen\n")
    print(f"    mode              {s['mode']}")
    print(f"    {MODES[s['mode']]}")
    print(f"    off-screen display {s['quiet_display']} {'(up)' if s['quiet_display_up'] else '(NOT running — it starts on demand)'}")
    print(f"    your display       {s['your_display']}")
    if s.get("changed_at"):
        print(f"    changed            {s['changed_at']}")
    print(f"\n    change it:  foreground_policy.py --set quiet|visible|offscreen\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
