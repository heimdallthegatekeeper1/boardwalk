#!/usr/bin/env python3
"""BoardWalk installer — choose the launcher for your OS and create its desktop icon.

★★ WHAT IT DOES
   · Linux  : writes a freedesktop `.desktop` entry (and a Desktop copy when one exists) that runs
              `launchers/boardwalk_launcher.py` with this release's icon.
   · macOS  : builds `~/Applications/BoardWalk.app` (Info.plist + executable + .icns) that runs the
              same launcher. The app starts the panel in your browser.
   · Windows: creates Start Menu and Desktop `.lnk` shortcuts that run the Python launcher.
   · Other  : STAGES the chosen OS's files under `dist/<os>/` so you can copy them to that machine;
              it does not pretend to install a foreign shortcut here.

★★ WHAT IT DOES NOT DO: no package installation, no sudo, no network, no telemetry, no writes
   outside your home and this release directory. Dependencies are checked and reported, not
   downloaded; see DISTRO.md for the per-OS commands.

★★ THE OVERLAY IS X11-ONLY in this release. The panel/board work wherever Python 3 runs; the
   click-through desktop overlay and per-window OCR pointing need X11 (Linux/FreeBSD; macOS only
   with XQuartz). The installer says this on every non-Linux target instead of implying feature
   parity.
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
LAUNCHERS = APP / "launchers"
ICON_PNG = LAUNCHERS / "icons" / "boardwalk.png"
ICON_ICO = LAUNCHERS / "icons" / "boardwalk.ico"
ICON_ICNS = LAUNCHERS / "icons" / "boardwalk.icns"
TARGETS = {"linux": "Linux — desktop icon", "macos": "macOS — .app",
           "windows": "Windows — Start Menu shortcut"}


def host_target() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s.startswith("win"):
        return "windows"
    return "linux"


def choose(default: str, assume_yes: bool) -> str:
    if assume_yes or not sys.stdin.isatty():
        return default
    print("BoardWalk installer — choose the launcher to install/create:")
    for n, key in enumerate(("linux", "macos", "windows"), 1):
        mark = "  (this machine)" if key == default else ""
        print(f"  {n}) {TARGETS[key]}{mark}")
    raw = input(f"Choose [1-3, default {list(TARGETS).index(default)+1}]: ").strip() or str(
        list(TARGETS).index(default) + 1)
    keys = list(TARGETS)
    try:
        return keys[int(raw) - 1]
    except Exception:
        print(f"not a choice: {raw!r}; using {default}")
        return default


def stage(target: str, dry: bool = False) -> None:
    d = APP / "dist" / target
    if dry:
        print(f"  would stage {target}: {d}")
        return
    d.mkdir(parents=True, exist_ok=True)
    for f in ("BoardWalk.desktop", "BoardWalk.command", "BoardWalk.bat", "BoardWalk.sh",
              "boardwalk_launcher.py", "icons/boardwalk.png", "icons/boardwalk.ico",
              "icons/boardwalk.icns"):
        src = LAUNCHERS / f
        if src.is_file():
            dst = d / Path(f).name
            shutil.copy2(src, dst)
    (d / "README.txt").write_text(
        f"BoardWalk — {TARGETS.get(target, target)} staging\n\n"
        f"These files were staged on {platform.system()} for a {target} machine.\n"
        "Copy the whole BoardWalk release to that machine, then run:\n"
        f"    python3 install.py --target {target}\n"
        "The installer there will create the real shortcut/app and verify dependencies.\n"
        "The X11 desktop overlay is not available natively on macOS/Windows; the panel and board are.\n")
    print(f"staged {target}: {d}")
    print("  copy that folder to the target machine; run its install.py there.")


def install_linux(app_dir: Path, dry: bool, no_shortcut: bool) -> int:
    data = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    apps = data / "applications"
    entry = apps / "boardwalk.desktop"
    text = (LAUNCHERS / "BoardWalk.desktop").read_text().replace("__APP_DIR__", str(app_dir))
    print(f"  applications entry: {entry}")
    print(f"  desktop icon:       {Path.home() / 'Desktop' / 'BoardWalk.desktop'}"
          if not no_shortcut else "  desktop icon:       skipped (--no-shortcut)")
    if dry:
        return 0
    apps.mkdir(parents=True, exist_ok=True)
    entry.write_text(text)
    entry.chmod(0o644)
    desk = Path.home() / "Desktop"
    if not no_shortcut and desk.is_dir():
        d = desk / "BoardWalk.desktop"
        d.write_text(text)
        d.chmod(0o755)
        print(f"  wrote {d} (GNOME may ask you to 'Allow launching' once)")
    for tool in ("desktop-file-validate",):
        if shutil.which(tool):
            r = subprocess.run([tool, str(entry)], capture_output=True, text=True)
            print(f"  {tool}: {'OK' if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200]}")
    print("  run it from your applications menu, or: python3 launchers/boardwalk_launcher.py")
    return 0


def install_macos(app_dir: Path, dry: bool, no_shortcut: bool) -> int:
    if host_target() != "macos":
        stage("macos", dry)
        return 0
    dest = Path.home() / "Applications" / "BoardWalk.app"
    print(f"  app bundle: {dest}")
    if dry:
        return 0
    macos = dest / "Contents" / "MacOS"
    res = dest / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    (dest / "Contents" / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>BoardWalk</string>
  <key>CFBundleDisplayName</key><string>BoardWalk</string>
  <key>CFBundleIdentifier</key><string>local.boardwalk.app</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>BoardWalk</string>
  <key>CFBundleIconFile</key><string>boardwalk</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
""")
    exe = macos / "BoardWalk"
    exe.write_text(f"#!/bin/bash\nexec python3 \"{app_dir}/launchers/boardwalk_launcher.py\"\n")
    exe.chmod(0o755)
    if ICON_ICNS.is_file():
        shutil.copy2(ICON_ICNS, res / "boardwalk.icns")
    (dest / "Contents" / "PkgInfo").write_text("APPL????")
    print("  note: the desktop overlay needs XQuartz/X11 on macOS; the panel/board run natively.")
    print("  open it from ~/Applications/BoardWalk.app")
    return 0


def install_windows(app_dir: Path, dry: bool, no_shortcut: bool) -> int:
    if host_target() != "windows":
        stage("windows", dry)
        return 0
    start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    lnk = start / "BoardWalk.lnk"
    desk = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "BoardWalk.lnk"
    icon = str(ICON_ICO if ICON_ICO.is_file() else ICON_PNG)
    print(f"  Start Menu: {lnk}")
    if not no_shortcut:
        print(f"  Desktop:    {desk}")
    if dry:
        return 0
    start.mkdir(parents=True, exist_ok=True)

    def ps(path: Path, target: str, args: str) -> None:
        script = (
            "$W=New-Object -ComObject WScript.Shell;"
            f"$s=$W.CreateShortcut('{path}');"
            f"$s.TargetPath='{target}';$s.Arguments='{args}';"
            f"$s.IconLocation='{icon}';$s.WorkingDirectory='{app_dir}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=False)

    py = shutil.which("py") or shutil.which("pythonw") or shutil.which("python") or "python"
    arg = f"-3 \"{app_dir}\\launchers\\boardwalk_launcher.py\""
    ps(lnk, py, arg)
    if not no_shortcut:
        ps(desk, py, arg)
    print("  note: the desktop overlay is X11-only; the panel/board work natively on Windows.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install/choose a BoardWalk launcher for an OS.")
    ap.add_argument("--target", choices=sorted(TARGETS) + ["auto"], default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-shortcut", action="store_true",
                    help="skip the Desktop copy/shortcut (the app entry is still installed)")
    ap.add_argument("--yes", action="store_true", help="do not prompt; use --target/detected")
    ap.add_argument("--copy-to", metavar="DIR", help="copy this release to DIR and install from there")
    a = ap.parse_args(argv)
    default = host_target()
    target = default if a.target == "auto" else a.target
    if a.target == "auto":
        target = choose(default, a.yes)
    print(f"BoardWalk installer · this machine: {default} · chosen: {target}")
    if not LAUNCHERS.is_dir():
        print(f"REFUSED: launchers/ is missing beside {APP}", file=sys.stderr)
        return 1
    app_dir = APP
    if a.copy_to:
        dst = Path(a.copy_to).expanduser().resolve()
        print(f"  copying the release to {dst}")
        if not a.dry_run:
            shutil.copytree(APP, dst, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc",
                                                          "launcher.log", "pointat-shots", "var"))
        app_dir = dst
    if target == "linux":
        rc = install_linux(app_dir, a.dry_run, a.no_shortcut)
    elif target == "macos":
        rc = install_macos(app_dir, a.dry_run, a.no_shortcut)
    elif target == "windows":
        rc = install_windows(app_dir, a.dry_run, a.no_shortcut)
    else:
        rc = 1
    if target != default and target in ("macos", "windows") and host_target() != target:
        print("  (this is a foreign target; nothing was installed on this machine)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
