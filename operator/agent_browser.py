#!/usr/bin/env python3
"""
agent_browser.py — the ONE launcher that owns the agent-drivable browser's profile.

WHY THIS EXISTS
---------------
The agent browser was started ad hoc, so its profile landed in /tmp/claude-1000/cdpprof.
It held a REAL logged-in Proton Mail session (the only strategy that reaches Proton on a
Free plan, since Free has no IMAP). One reboot or one /tmp sweep would have destroyed a
login that cost the support team hours -- and nobody would have known why it was gone.

No launcher owned the path, so the path was re-invented by whoever launched next.
This file owns it. `PROFILE` below is the single source of truth.

PARITY: everything here is reachable three ways -- this CLI, the panel card, and the
desktop entry -- because the operator must be able to do anything the agent can do.
"""
import argparse, json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path


def _ss():
    """★ One atomic+locked writer for every store in this directory. Each of these used
    write_text(json.dumps(...)), which truncates before it writes — a crash between those two moments
    left the store empty, and two writers clobbered each other undetectably."""
    import importlib, sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parent))
    return importlib.import_module("safe_store")
from urllib.request import urlopen

HOME = Path.home()
PROFILE   = Path(__file__).resolve().parent.parent / "browser-profiles" / "agent"
LEGACY    = Path("/tmp/claude-1000/cdpprof")
PORT      = 9333
STATE     = Path(__file__).resolve().parent.parent / "operator" / "agent_browser_state.json"


RUNTIME = Path(__file__).resolve().parent.parent / "operator" / "agent_browser_pids.json"


def _record_pid(port: int, pid: int) -> None:
    try:
        d = json.loads(RUNTIME.read_text()) if RUNTIME.exists() else {}
    except Exception:
        d = {}
    d[str(port)] = pid
    _ss().write_json(RUNTIME, d)


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ")
    except Exception:
        return ""


def stop_browser(port: int, profile: Path | None = None) -> str:
    """Stop a browser we started, by POSITIVE IDENTIFICATION -- never by pattern.

    ★ INV-101 exists because `pkill -f <pattern>` has repeatedly matched the wrong thing,
      including the shell that invoked it. This module nearly repeated it: an earlier
      version ran `pkill -f remote-debugging-port=9433`, and a near-miss in this same
      project once came one keystroke from killing the operator's own browser.

    ★★ So the rule here is stronger than "use a safer pattern tool": we kill ONLY a pid we
      recorded ourselves, and only after reading /proc and confirming that process really
      carries both our profile path and our port. If the recorded pid has been recycled to
      something else, the cmdline check fails and we kill nothing.
    """
    try:
        d = json.loads(RUNTIME.read_text()) if RUNTIME.exists() else {}
    except Exception:
        d = {}
    pid = d.get(str(port))
    if not pid:
        return f"no pid recorded for :{port} -- refusing to guess which process to stop"
    cl = _cmdline(int(pid))
    if not cl:
        return f"pid {pid} is gone already"
    if f"remote-debugging-port={port}" not in cl:
        return (f"REFUSED: pid {pid} does not carry --remote-debugging-port={port}. "
                f"That pid was recycled; killing it would hit an unrelated process.")
    if profile is not None and f"user-data-dir={profile}" not in cl:
        return (f"REFUSED: pid {pid} is not running our profile ({profile}). Not ours to stop.")
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except Exception:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception as e:
            return f"could not stop pid {pid}: {e}"
    for _ in range(30):
        time.sleep(0.5)
        if not _cmdline(int(pid)):
            d.pop(str(port), None); _ss().write_json(RUNTIME, d)
            return f"stopped pid {pid} on :{port}"
    return f"pid {pid} did not exit within 15s"


def _du(p) -> str:
    try:
        import subprocess as _sp
        return _sp.run(["du", "-sh", str(p)], capture_output=True, text=True,
                       timeout=20).stdout.split()[0]
    except Exception:
        return "?"


def refuse_volatile(p: Path) -> None:
    """A profile under /tmp is a login with a countdown on it."""
    parts = p.resolve().parts
    if "tmp" in parts[:3] or str(p).startswith("/tmp/") or str(p).startswith("/var/tmp/"):
        sys.exit(f"REFUSE: {p} is volatile. A login stored there dies on reboot.\n"
                 f"        The durable path is {PROFILE}")


def cdp_pages(port: int, timeout: float = 4.0):
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as r:
            return [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return None


def running(port: int) -> bool:
    return cdp_pages(port) is not None


def live_profile(port: int = PORT):
    """★★★ WHICH PROFILE IS THE RUNNING BROWSER ACTUALLY USING?

    INV-116 asked the wrong question for three days. It checked whether the directory
    /tmp/claude-1000/cdpprof still EXISTS — and reported FAIL because a 147 MB leftover was sitting
    there with no process attached, while the live logged-in Proton session had been moved to
    ~/.config/khaoss/agent-browser, which is durable. The invariant was red about a risk that had
    already been closed, and it would have stayed red no matter how many times the login was rescued.
    ★ The question is not "does a volatile directory exist somewhere" but "is the browser that holds
      the session running out of one". Read the process, not the filesystem.
    """
    import os as _os
    for d in _os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            cl = open(f"/proc/{d}/cmdline", "rb").read().decode("utf-8", "replace").replace("\0", " ")
            exe = _os.path.basename(_os.path.realpath(f"/proc/{d}/exe"))
        except Exception:
            continue
        if "chrom" not in exe or "--type=" in cl:
            continue
        if f"remote-debugging-port={port}" not in cl:
            continue
        for tok in cl.split():
            if tok.startswith("--user-data-dir="):
                path = tok.split("=", 1)[1]
                volatile = path.startswith("/tmp/") or path.startswith("/var/tmp/")
                return {"pid": int(d), "profile": path, "volatile": volatile}
    return None


def cookies(profile: Path, host_like: str = "%"):
    """Read a profile's cookies WITHOUT disturbing it (copy db + wal + shm)."""
    src = profile / "Default" / "Cookies"
    if not src.exists():
        return []
    t = Path(tempfile.mkdtemp())
    for suf in ("", "-wal", "-shm"):
        s = Path(str(src) + suf)
        if s.exists():
            shutil.copy(s, t / ("Cookies" + suf))
    try:
        c = sqlite3.connect(t / "Cookies")
        return c.execute(
            "select host_key, name from cookies where host_key like ?", (host_like,)
        ).fetchall()
    except Exception:
        return []


def session_shape(profile: Path, service: str = "proton"):
    """Does this profile CARRY a session for `service`? Shape only -- never the value.

    Carrying the cookies is NOT the same as the session being accepted. This reports
    shape; `--prove` is what reports acceptance.
    """
    rows = cookies(profile, f"%{service}%")
    names = [n for _, n in rows]
    return {
        "service": service,
        "cookies": len(names),
        "auth": any(n.startswith("AUTH-") for n in names),
        "refresh": any(n.startswith("REFRESH-") for n in names),
    }


def launch(profile: Path, port: int, headless: bool = False, url: str | None = None):
    refuse_volatile(profile)
    if not profile.exists():
        sys.exit(f"REFUSE: no profile at {profile}. Nothing to launch with.")
    if running(port):
        return f"already running on :{port} -- not launching a second one"
    cmd = ["/usr/bin/chromium",
           f"--user-data-dir={profile}",
           f"--remote-debugging-port={port}",
           "--remote-allow-origins=*",
           "--no-first-run", "--no-default-browser-check"]
    # ★★★ NEVER TAKE HIS SCREEN. He was working, a window came up in front of him, and he pressed
    #   the spacebar into it before he could read what it was. Spacebar activates a focused button
    #   and accepts a confirm dialog; this panel has buttons that stop services and move logins.
    #   The policy decides WHERE this renders -- by default an off-screen display, so a browser we
    #   start is still fully headed (Cloudflare and downsub both refuse headless) but cannot appear
    #   over his work. `foreground_policy.py --set visible` when he wants to watch.
    env = None
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import foreground_policy as _fp
        env, why = _fp.launch_env()
        cmd += _fp.window_args()
        print(f"  foreground: {why}")
    except Exception as e:
        print(f"  foreground policy unavailable ({e}) — launching with the inherited display")
    if headless:
        cmd += ["--headless=new"]
    if url:
        cmd += [url]
    # setsid: the harness SIGTERMs children of a turn. A browser that dies with the
    # turn that started it is not a persistent browser.
    # ★ NO EXTERNAL `setsid` HERE, DELIBERATELY. Wrapping the command in the setsid
    #   binary means the pid we get back is setsid's -- and setsid forks and exits at
    #   once, so the recorded pid is dead within milliseconds while the browser lives on
    #   under a pid we never learned. stop_browser() then read an empty cmdline and
    #   cheerfully reported "gone already" about a browser that was still running.
    #   That is not a cosmetic bug: --transplant stops the browser BEFORE copying, so a
    #   stop that silently does nothing produces a copy of a LIVE profile -- the exact
    #   expired-session outcome the transplant exists to avoid, and the login is lost.
    #   Popen(start_new_session=True) already calls setsid(2) in the child, so we get
    #   detachment AND the real pid.
    proc = subprocess.Popen(cmd, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    _record_pid(port, proc.pid)
    for _ in range(40):
        time.sleep(0.5)
        if running(port):
            return f"launched on :{port} with {profile}"
    return f"launched but CDP never answered on :{port} within 20s"


def self_test() -> int:
    """INV-114 negative control: make each check say the OTHER thing on demand.

    ★ Both of these WOULD HAVE PASSED VACUOUSLY as written the first time. `session_shape`
      returns a dict, so a caller reading `shape["auth"]` on a profile with no cookie
      database at all gets False and looks correct -- which is why control 2 asserts on an
      EMPTY profile rather than trusting the happy path. And `refuse_volatile` exits via
      sys.exit, so a test that merely calls it and continues would never notice it had
      become a no-op.
    """
    import tempfile as _t
    failures = []

    # 1. a volatile path MUST be refused. If it returns, the guard is a no-op.
    refused = False
    try:
        refuse_volatile(Path("/tmp/anything/cdpprof"))
    except SystemExit:
        refused = True
    if not refused:
        failures.append("refuse_volatile() accepted a /tmp path -- the guard does not guard")

    # 2. a durable path MUST be accepted, or the guard refuses everything and is equally useless.
    try:
        refuse_volatile(Path(__file__).resolve().parent.parent / "browser-profiles" / "agent")
    except SystemExit:
        failures.append("refuse_volatile() rejected a durable path -- it refuses everything")

    # 3. an EMPTY profile must not report a session. If it does, every 'we are logged in'
    #    verdict this module produces is unfalsifiable.
    empty = Path(_t.mkdtemp())
    shape = session_shape(empty, "proton")
    if shape["auth"] or shape["refresh"] or shape["cookies"]:
        failures.append(f"session_shape() found a session in an empty profile: {shape}")

    for f in failures:
        print("  CONTROL FAILED:", f)
    if not failures:
        print("  3 controls pass: volatile refused, durable accepted, empty profile reports no session")
    return 1 if failures else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true", help="where the profile is and what it carries")
    ap.add_argument("--launch", action="store_true", help="start the browser on the DURABLE profile")
    ap.add_argument("--prove", action="store_true",
                    help="prove the durable profile is ACCEPTED (lands logged in, not at a login page)")
    ap.add_argument("--transplant", action="store_true",
                    help="MIGRATE the live login into the durable profile (stops the browser; "
                         "costs one re-login if it fails)")
    ap.add_argument("--yes", action="store_true", help="required to actually transplant")
    ap.add_argument("--profile", default=str(PROFILE))
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--service", default="proton")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="INV-114 negative control")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(self_test())
    prof = Path(a.profile)

    if a.status or not any([a.launch, a.prove, a.transplant]):
        live = running(a.port)
        pages = cdp_pages(a.port) or []
        shape = session_shape(prof, a.service)
        lp = live_profile(a.port)
        # ★ The invariant now asks about the RUNNING browser. A leftover directory in /tmp is
        #   untidy; a running browser whose session lives there is the actual risk.
        legacy_live = bool(lp and lp["volatile"])
        leftover = LEGACY.exists() and not (lp and lp["profile"] == str(LEGACY))
        out = {
            "durable_profile": str(prof),
            "durable_exists": prof.exists(),
            "legacy_tmp_profile_still_present": legacy_live,
            "running_profile": (lp or {}).get("profile"),
            "running_profile_is_volatile": bool(lp and lp["volatile"]),
            "stale_tmp_leftover": leftover,
            "port": a.port, "running": live,
            "open_pages": [p.get("url", "")[:90] for p in pages],
            "session_shape": shape,
        }
        if a.json:
            print(json.dumps(out, indent=2)); return
        print(f"\n  durable profile   {prof}")
        print(f"  exists            {'yes' if prof.exists() else 'NO'}")
        print(f"  browser on :{a.port}   {'running' if live else 'not running'}")
        for p in pages:
            print(f"      {p.get('url','')[:88]}")
        print(f"\n  {a.service} session SHAPE in the durable profile:")
        print(f"      cookies {shape['cookies']}   AUTH {shape['auth']}   REFRESH {shape['refresh']}")
        if shape["auth"] and shape["refresh"]:
            print("      ^ it CARRIES a session. That is not the same as the session being")
            print("        accepted -- run --prove for that.")
        print(f"\n  the RUNNING browser's profile: {(lp or {}).get('profile') or '(none running)'}")
        if lp and lp["volatile"]:
            print("  ★ IT IS VOLATILE — the live session dies at the next reboot or /tmp sweep.")
        elif lp:
            print("  ✓ durable — a reboot does not cost this login.")
        if leftover:
            print(f"\n  ! {LEGACY} still exists ({_du(LEGACY)}) but NOTHING is using it.")
            print( "    That is untidy, not dangerous. It is a leftover, and it is why this check")
            print( "    used to report a risk that had already been closed.")
        return

    if a.launch:
        print(launch(prof, a.port)); return

    if a.transplant:
        # WHY A CLEAN SHUTDOWN IS REQUIRED
        # Copying a RUNNING browser's profile does not copy its login. Chromium holds
        # auth state in memory and in sqlite WAL; the cookies you copy are the ones it
        # had at some earlier moment, and Proton rotates them. The copy taken this way
        # landed at account.proton.me?reason=session-expired while the live browser was
        # still happily showing the inbox. Only a clean exit flushes a usable session.
        if not a.yes:
            print("""
  This stops the running agent browser, then copies its profile to the durable path.

  It is not free: if the transplanted session still comes back expired, the login has
  to be done once more by hand. The live session is working RIGHT NOW, so this trades a
  small chance of one re-login against losing the session outright at the next reboot.

  A copy taken while it runs does NOT work -- that was measured, not assumed.

  Re-run with --yes when you want it done.""")
            return
        print("  stopping the agent browser cleanly (SIGTERM, so it flushes) ...")
        print(" ", stop_browser(a.port, LEGACY))
        for _ in range(30):
            time.sleep(1)
            if not running(a.port):
                break
        if running(a.port):
            sys.exit("REFUSE: it did not stop. Not copying a live profile again.")
        time.sleep(2)
        backup = Path(str(prof) + ".before-transplant")
        if prof.exists() and not backup.exists():
            shutil.move(str(prof), str(backup))
            print(f"  kept the old durable copy at {backup}")
        if not LEGACY.exists():
            # Put the old durable copy back rather than leaving nothing behind.
            if backup.exists():
                shutil.move(str(backup), str(prof))
            sys.exit(f"REFUSE: {LEGACY} is gone -- there is nothing to transplant. "
                     f"Restored the previous durable copy.")
        shutil.copytree(LEGACY, prof)
        os.chmod(prof, 0o700)
        print(f"  copied cleanly-closed profile -> {prof}")

        # ★ BRING IT BACK UP. Stopping the browser to move its login and then leaving it
        # down would trade a reboot-shaped loss for an immediate one -- and the operator
        # pressed a button labelled "move the login", not "close the browser".
        print("  relaunching on the durable profile ...")
        print(" ", launch(prof, a.port))
        print("  now proving it ...")
        a.prove = True

    if a.prove:
        # Launch a SECOND browser on the durable copy, on a port of its own, and read
        # what the page ACTUALLY says once the SPA has settled.
        #
        # L82: the first URL you see is not the landing page. mail.proton.me/u/1/inbox
        # is served for one beat and THEN redirects to account.proton.me?reason=
        # session-expired. An early URL check reports ACCEPTED for a dead session.
        # Wait for a terminal state, then read the page, then decide.
        port = a.port + 100
        stop_browser(port, prof)
        time.sleep(1)
        print(f"  launching a probe browser on :{port} using {prof} ...")
        print(" ", launch(prof, port, headless=True, url="https://mail.proton.me/u/1/inbox"))
        verdict, url, title, body = "UNPROVEN", None, None, ""
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                pg = b.contexts[0].pages[0]
                for _ in range(25):
                    time.sleep(2)
                    url = pg.url
                    try:
                        body = pg.evaluate("document.body.innerText")
                    except Exception:
                        body = ""
                    title = pg.title()
                    # terminal states, either direction
                    if "session-expired" in url or "Sign-in" in (title or ""):
                        break
                    if "@proton.me" in (title or "") or "@proton.me" in body:
                        break
                signin = any(w in body for w in ("Sign in", "Log in", "Sign In"))
                # The account address appears in the TITLE, not the body -- looking only
                # at the body reported INCONCLUSIVE for a perfectly good session.
                logged_in = "@" in (title or "") and not signin
                if "session-expired" in (url or "") or signin:
                    verdict = "REJECTED -- lands at a sign-in page; the session is not accepted"
                elif logged_in:
                    verdict = "ACCEPTED -- the durable profile logs in on its own"
        except Exception as e:
            verdict = f"UNPROVEN -- could not drive the probe browser ({e})"
        print(f"\n  final url: {url}")
        print(f"  title:     {title}")
        print(f"\n  VERDICT: {verdict}")
        print(" ", stop_browser(port, prof))
        STATE.write_text(json.dumps({
            "proved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "profile": str(prof), "url": url, "title": title, "verdict": verdict,
        }, indent=2))
        print(f"  recorded -> {STATE}")
        sys.exit(0 if verdict.startswith("ACCEPTED") else 1)


if __name__ == "__main__":
    main()
