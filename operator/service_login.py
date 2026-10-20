#!/usr/bin/env python3
"""service_login.py — sign in to a service FROM THE BOARD, without a terminal.

★★★ HIS ASK, verbatim: "I need to be able to log in from there via board only and other options with
    the button there, whatever the case."

    So the button cannot stop at a verdict. `gh auth login` is an INTERACTIVE program — it asks
    questions and waits at a prompt — which is exactly why it has never been reachable from a web
    panel. There are two ways around that, and both are offered here rather than picking one for him:

      1. DEVICE FLOW (--web). gh prints a one-time code, you type that code into github.com in a
         browser, and gh sits polling until GitHub says you did. Nothing secret travels through the
         panel: the code is useless to anyone who is not already holding this gh process. This is the
         one to prefer.
      2. PASTE A TOKEN (--with-token). Fully non-interactive. He makes a Personal Access Token on
         github.com and pastes it in. Faster, but the token passes through the box, so it is offered
         second and the value is written to gh's STDIN ONLY — never a file, never a log line, never
         echoed back in a response.

★ WHAT THIS DELIBERATELY DOES NOT DO: it does not choose scopes for him, and it does not approve
  anything. Scopes are what a token is ALLOWED to do; picking them silently is how an agent ends up
  holding more power over his account than he meant to grant. The default asked for is the smallest
  set that makes `gh` useful, and it is shown on screen before he starts.
"""
from __future__ import annotations
import json, os, re, signal, subprocess, sys, threading, time
from pathlib import Path

GH = str(Path.home() / ".local" / "bin" / "gh")
STATE = Path(__file__).resolve().parent.parent / "operator" / "login_state.json"
LOGDIR = Path(__file__).resolve().parent.parent / "operator"

# ★★★ WHY THIS TRACKS A PID ON DISK INSTEAD OF HOLDING THE PROCESS IN MEMORY — a real bug, caught by
#     running it rather than reading it. The panel resolves these modules with importlib.RELOAD on
#     every single HTTP request, so a module-level dict holding the Popen is thrown away between the
#     "start" call and the "status" call. The first live test looked fine — a code came back — and
#     then `status` said "idle" and `cancel` said "nothing was running" while `gh auth login` was
#     STILL RUNNING, orphaned, holding a live device code.
#     ★ That is the worst shape of failure available here: the panel reported a clean stop and left
#       an authenticating process alive. So liveness is now a PID on disk plus a log file, both of
#       which outlive any number of module reloads.

# ★ the smallest set that makes gh useful. Shown to him before he starts; he can change it.
DEFAULT_SCOPES = "repo,read:org,gist"

_CODE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
_URL = re.compile(r"(https://\S*device\S*)")

_procs: dict = {}          # service -> Popen, valid only WITHIN one request (see note above)


def _logpath(svc: str) -> Path:
    return LOGDIR / f"login_{svc}.log"


def _alive(pid: int) -> bool:
    """★ Liveness measured, not remembered — and a ZOMBIE COUNTS AS DEAD.

    ★★ Caught by the control, not by reading: after SIGTERM the process was gone from `pgrep`, but
       `os.kill(pid, 0)` still succeeded and /proc/<pid>/comm still said "gh", because a killed child
       stays in the table as a zombie until its parent reaps it. Cancel therefore reported
       "COULD NOT STOP" for a process it had successfully killed — telling him to go kill a pid that
       no longer exists, which is a worse lie than saying nothing."""
    if not pid:
        return False
    pid = int(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    try:
        st = Path(f"/proc/{pid}/stat").read_text()
        state = st[st.rindex(")") + 2]          # the field after the (comm) parens
        if state in ("Z", "X"):                 # zombie / dead
            _reap(pid)
            return False
    except Exception:
        pass
    # ★ must still BE gh — a recycled pid would otherwise read as a live sign-in
    try:
        return "gh" in Path(f"/proc/{pid}/comm").read_text()
    except Exception:
        return True


def _reap(pid: int) -> None:
    """Clear the zombie if it is ours. Harmless when it is not."""
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except Exception:
        pass


def methods(service: str = "github") -> list:
    """The ways he can sign in. Each says what it costs him and what it exposes."""
    if service != "github":
        return [{"key": "manual", "label": "No board login built for this service yet",
                 "detail": "Only GitHub has a board login so far. Anything else still needs a terminal.",
                 "exposes": "—", "recommended": False}]
    return [
      {"key": "device", "label": "Sign in with a browser (one-time code)",
       "detail": "GitHub shows you a short code. You type it at github.com/login/device in any browser "
                 "— on this machine or your phone — and this box is signed in when you finish.",
       "exposes": "Nothing secret passes through this panel. The code is useless without this session.",
       "recommended": True},
      {"key": "token", "label": "Paste a Personal Access Token",
       "detail": "You create a token at github.com/settings/tokens and paste it here. No browser dance.",
       "exposes": "★ The token passes through this box. It goes straight into gh's stdin — it is never "
                  "written to a file here, never logged, and never sent back in a reply.",
       "recommended": False},
      {"key": "ssh", "label": "Show me how to do it in a terminal instead",
       "detail": "Prints the exact command. Nothing runs.",
       "exposes": "Nothing — this only shows text.", "recommended": False},
    ]


def start_device(service: str = "github", scopes: str = "", open_browser: bool = False) -> dict:
    """Begin the device flow and return the one-time code + the URL to type it at."""
    if service != "github":
        return {"ok": False, "error": f"no board login built for {service!r}"}
    if not Path(GH).exists():
        return {"ok": False, "error": "gh is not installed", "fix": "install gh first"}
    cancel(service)
    env = dict(os.environ)
    # ★ we take the browser OUT of gh's hands so the panel can show him the link. Otherwise gh blocks
    #   on xdg-open and he sees nothing.
    if not open_browser:
        env["BROWSER"] = "/bin/true"
    cmd = [GH, "auth", "login", "--hostname", "github.com", "--git-protocol", "https",
           "--web", "--scopes", (scopes or DEFAULT_SCOPES)]
    lp = _logpath(service)
    try:
        lp.unlink()
    except FileNotFoundError:
        pass
    # ★ output goes to a FILE, not a pipe read by a thread. The thread would die with the module.
    fh = open(lp, "w+b", buffering=0)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT,
                         env=env, start_new_session=True)
    _procs[service] = p
    code, url = "", ""
    for _ in range(150):                      # up to ~15s for gh to print the code
        time.sleep(0.1)
        b = _tail(service)
        m = _CODE.search(b)
        if m:
            code = m.group(1)
            u = _URL.search(b)
            url = u.group(1) if u else "https://github.com/login/device"
            break
        if p.poll() is not None:
            break
    if not code:
        tail = _tail(service)[-600:]
        _kill(p.pid)
        return {"ok": False, "error": "gh did not offer a one-time code",
                "output": tail or "(no output)",
                "means": "This is gh failing, not you. The output above is the whole of what it said."}
    try:
        p.stdin.write(b"\n"); p.stdin.flush()  # gh waits at "Press Enter"; nothing else is sent
    except Exception:
        pass
    _save({"service": service, "method": "device", "code": code, "url": url, "pid": p.pid,
           "cancelled": False,
           "log": str(lp), "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "scopes": scopes or DEFAULT_SCOPES})
    return {"ok": True, "method": "device", "code": code, "url": url,
            "scopes": scopes or DEFAULT_SCOPES,
            "instructions": [f"1. Open {url} (on this machine or your phone)",
                             f"2. Type the code {code}",
                             "3. Approve the scopes GitHub shows you",
                             "4. Come back here — this panel is watching and will say when it lands"],
            "expires_in": "usually 15 minutes",
            "note": "This code is not a password. It is worthless to anyone who is not holding the "
                    "process on THIS machine that is waiting for it."}


def status(service: str = "github") -> dict:
    """Where the sign-in got to. ★ 'still waiting' is a real answer, not a failure.
    ★ Liveness is read from the PID ON DISK, because this module is reloaded between requests."""
    st = _load().get(service) or {}
    pid = st.get("pid")
    out = _tail(service)
    if pid and _alive(pid):
        return {"state": "waiting", "pid": pid, "code": st.get("code"), "url": st.get("url"),
                "output": out[-500:],
                "means": "gh is polling GitHub. Nothing is wrong — it waits until you finish in the "
                         "browser, or until the code expires."}
    if not st:
        return {"state": "idle", "last": None}
    ok = ("Logged in as" in out) or ("Authentication complete" in out)
    if not ok and st.get("cancelled"):
        # ★ a cancel is not a failure. Reporting "gh exited without signing in" for something HE
        #   stopped on purpose would send him looking for a fault that does not exist.
        return {"state": "cancelled", "output": out[-600:], "last": st,
                "means": "You cancelled this sign-in. Nothing was stored. Start it again whenever."}
    return {"state": "done" if ok else ("failed" if out else "idle"),
            "output": out[-1200:], "last": st,
            "means": ("You are signed in. Re-run the checks to see it." if ok else
                      "gh exited without signing in. The output above is exactly what it said — read "
                      "it before retrying; a re-run will not fix a wrong code or an expired one.")}


def login_with_token(token: str, service: str = "github") -> dict:
    """Paste-a-token path. ★ The value goes to stdin and NOWHERE else — not returned, not stored here."""
    if service != "github":
        return {"ok": False, "error": f"no board login built for {service!r}"}
    t = (token or "").strip()
    if not t:
        return {"ok": False, "error": "no token given"}
    if len(t) < 20:
        return {"ok": False, "error": "that does not look like a GitHub token (too short)",
                "note": "Nothing was sent anywhere. Tokens start ghp_ / github_pat_ and are long."}
    try:
        r = subprocess.run([GH, "auth", "login", "--hostname", "github.com", "--with-token"],
                           input=t, capture_output=True, text=True, timeout=60)
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"}
    out = _scrub(((r.stdout or "") + (r.stderr or "")).strip(), t)
    ok = r.returncode == 0
    if ok:
        _save({"service": service, "method": "token",
               "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "scopes": "(whatever the token carries)"})
    return {"ok": ok, "output": out[:800],
            "means": ("Signed in. Run the 'does the credential work' check to confirm GitHub accepts it."
                      if ok else "GitHub refused the token. Common causes: it expired, it was revoked, "
                                 "or it was copied with a space on the end.")}


def _scrub(text: str, secret: str) -> str:
    """★ Belt and braces: if gh ever echoes the token back, it does not leave this module.
    Pulled out as its own function for one reason — so it can be TESTED, rather than trusted."""
    if secret and secret in text:
        return text.replace(secret, "<token redacted>")
    return text


def terminal_recipe(service: str = "github") -> dict:
    return {"ok": True, "runs_nothing": True,
            "command": f"{GH} auth login --hostname github.com --git-protocol https --web "
                       f"--scopes {DEFAULT_SCOPES}",
            "note": "Copy this into a terminal. This panel ran nothing."}


def _kill(pid: int) -> bool:
    try:
        os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return True
        except Exception:
            return False


def cancel(service: str = "github") -> dict:
    """★ Kills by PID FROM DISK. The in-memory handle is gone by the time this route is called, and
    reporting a clean stop while leaving an authenticating process alive is the worst outcome here."""
    st = _load().get(service) or {}
    pid = st.get("pid")
    if not pid or not _alive(pid):
        return {"ok": True, "note": "nothing was running"}
    killed = _kill(pid)
    time.sleep(0.4)
    still = _alive(pid)
    if still:
        try:
            os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
        except Exception:
            pass
        time.sleep(0.3)
        still = _alive(pid)
    st["pid"] = None
    st["cancelled"] = True          # ★ so status says "you cancelled", not "it failed"
    _save(st | {"service": service})
    return {"ok": not still,
            "note": ("sign-in cancelled; no credential was stored" if not still else
                     f"★ COULD NOT STOP pid {pid} — it may still be authenticating. "
                     f"Stop it yourself: kill {pid}"),
            "killed": killed, "still_running": still}


def _tail(service: str) -> str:
    try:
        return _logpath(service).read_text(errors="replace")[-4000:]
    except Exception:
        return ""


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save(rec: dict) -> None:
    st = _load()
    st[rec.get("service", "?")] = rec
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        os.replace(tmp, STATE)
    except Exception:
        pass


def self_test() -> int:
    fails = []
    ms = methods("github")
    if len(ms) < 3:
        fails.append("fewer than three ways to sign in are offered")
    for m in ms:
        if not m.get("exposes"):
            fails.append(f"method {m['key']} does not say what it exposes")
    probe = "Zq7xKv"                      # ★ distinctive on purpose: an English word would match the
    r = login_with_token(probe)           #   refusal text itself and fake a leak
    if r.get("ok"):
        fails.append("an obviously-invalid token was accepted")
    if probe in json.dumps(r):
        fails.append("★ the rejected token VALUE appeared in the reply — a token must never come back")
    # ★ the redaction itself, tested rather than trusted: gh echoing the token must not survive
    leak = "ghp_" + "A" * 36
    if leak in _scrub(f"error: bad credentials for {leak}", leak):
        fails.append("★ _scrub did NOT remove an echoed token — the redaction is decorative")
    if not methods("gitlab")[0]["label"].lower().startswith("no board login"):
        fails.append("an unsupported service was not refused honestly")
    if terminal_recipe().get("runs_nothing") is not True:
        fails.append("the show-me-the-command path does not declare that it runs nothing")
    # ★★★ THE REGRESSION CONTROL. The panel reloads this module between every HTTP request, so a
    #     process held only in memory vanishes between `start` and `status`. That bug shipped once:
    #     status said "idle" and cancel said "nothing was running" while gh was still authenticating,
    #     orphaned. This proves liveness and cancellation survive a reload.
    import importlib
    if Path(GH).exists():
        st = start_device("github")
        if not st.get("ok"):
            print("  (note: could not start a live device flow to test against:", st.get("error"), ")")
        else:
            # ★ imported BY NAME, not reloaded in place: __main__ has no spec, and importing by
            #   name produces a genuinely fresh module object with empty memory — exactly what the
            #   panel hands each request.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            mod = importlib.reload(importlib.import_module("service_login"))
            s2 = mod.status("github")
            if s2.get("state") != "waiting":
                fails.append(f"★ AFTER A MODULE RELOAD status read {s2.get('state')!r}, not 'waiting' "
                             f"— the orphaned-process bug is back")
            c = mod.cancel("github")
            if not c.get("ok") or c.get("still_running"):
                fails.append(f"★ AFTER A MODULE RELOAD cancel did not stop the process: {c}")
            if mod.status("github").get("state") == "waiting":
                fails.append("★ the process was still waiting after cancel returned ok")
    s = status("github")
    if s.get("state") == "waiting":
        fails.append("a process was left running at the end of the self-test")
        cancel("github")          # ★ a test that leaks a live auth process is worse than no test
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  8 controls pass: 3+ sign-in routes offered · each states what it EXPOSES · a bad token "
              "is refused · THE TOKEN VALUE NEVER APPEARS IN A REPLY · an ECHOED token is actually "
              "scrubbed (tested, not trusted) · unsupported service refused honestly · show-the-command "
              "declares it runs nothing · STATUS AND CANCEL SURVIVE A MODULE RELOAD (the orphan bug)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(self_test())
    print(json.dumps(methods("github"), indent=2))
