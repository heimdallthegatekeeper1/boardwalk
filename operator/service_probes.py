#!/usr/bin/env python3
"""service_probes.py — more than one way to check a service, and each says what it proves.

★★★ THE COMPLAINT THIS ANSWERS, in his words: "I tried the 'Check it now' button for GitHub but it
    wasn't comprehensive enough and didn't give me options on how to check. It seemed to have also
    failed."

    ★ IT HAD NOT FAILED. `gh auth status` ran, exited 0, and printed "You are not logged into any
      GitHub hosts" — so the verdict `no` was CORRECT. What was wrong is that a correct answer was
      shown as a bare red ✗ with no command, no output and no alternative, which is indistinguishable
      from a broken button. A check that cannot show its working is not a check to the person reading
      it.

★★ AND ONE PROBE IS ONE QUESTION. "Is GitHub set up?" is at least five different questions:
      · does the CLI exist at all
      · does it hold a credential
      · does that credential actually work against the API
      · what can it reach with it
      · is github.com even reachable from here
    They fail independently and they mean different things — a token that exists but is revoked looks
    identical to no token if you only ask the first question. So each is offered separately, with what
    it proves written beside it.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GH = str(Path.home() / ".local" / "bin" / "gh")


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = ((r.stdout or "") + ("\n" + r.stderr if r.stderr else "")).strip()
        return {"rc": r.returncode, "out": out[:1500]}
    except FileNotFoundError:
        return {"rc": 127, "out": f"{cmd[0]}: not found"}
    except subprocess.TimeoutExpired:
        return {"rc": 124, "out": f"timed out after {timeout}s — that is not a NO, it is an unknown"}
    except Exception as e:
        return {"rc": 1, "out": f"{e.__class__.__name__}: {e}"}


# ★ Each probe declares: what it RUNS, what a pass PROVES, and what a fail does NOT prove.
PROBES = {
  "github": [
    {"key": "installed", "label": "Is the gh CLI installed?",
     "cmd": [GH, "--version"], "pass_if": "gh version",
     "proves": "the binary exists and runs",
     "fail_means": "gh is missing — nothing else here can pass either"},
    {"key": "has_credential", "label": "Does gh hold a credential?",
     "cmd": [GH, "auth", "status"], "pass_if": "Logged in to",
     "proves": "a token is stored for at least one host",
     "fail_means": "no token is stored. It does NOT mean your account is wrong — you have simply "
                   "never run `gh auth login` on this machine"},
    {"key": "credential_works", "label": "Does that credential actually work? (calls the API)",
     "cmd": [GH, "api", "user", "--jq", ".login"], "pass_if": "",
     "proves": "the token is not just present but ACCEPTED by GitHub right now — this is the only "
               "probe that would catch a revoked or expired token",
     "fail_means": "either there is no token, or there is one and GitHub refused it. The output "
                   "distinguishes them"},
    {"key": "scopes", "label": "What can it actually do? (token scopes)",
     "cmd": [GH, "auth", "status", "--show-token"], "pass_if": "Token scopes",
     "proves": "which permissions the token carries — a valid token with no `repo` scope cannot do "
               "the thing you wanted it for",
     "fail_means": "scopes could not be read; it does not mean the token is bad",
     "sensitive": True},
    {"key": "reachable", "label": "Is github.com reachable from this machine at all?",
     "cmd": ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
             "https://api.github.com"], "pass_if": "200",
     "proves": "the network path works — separates 'GitHub is refusing you' from 'you cannot reach "
               "GitHub'",
     "fail_means": "no network path. Every other probe here would fail for that reason alone"},
  ],
}

GENERIC = [
    {"key": "installed", "label": "Is the tool installed?", "cmd": None, "pass_if": "",
     "proves": "the binary exists on PATH",
     "fail_means": "the tool is missing; nothing else can be established"},
]


def probes_for(service: str, tool: str = "") -> list:
    if service in PROBES:
        return PROBES[service]
    t = tool or service
    return [{"key": "installed", "label": f"Is `{t}` installed?",
             "cmd": ["which", t], "pass_if": "/",
             "proves": f"{t} exists on PATH",
             "fail_means": f"{t} is not installed — that is the only thing this establishes"}]


def run_probe(service: str, key: str, tool: str = "") -> dict:
    ps = {p["key"]: p for p in probes_for(service, tool)}
    p = ps.get(key)
    if not p:
        return {"ok": False, "error": f"unknown probe {key!r} for {service!r}; "
                                     f"choose from {list(ps)}"}
    if not p.get("cmd"):
        return {"ok": False, "error": "this probe has no command defined"}
    r = _run(p["cmd"])
    want = p.get("pass_if", "")
    passed = (r["rc"] == 0) and (want in r["out"] if want else True)
    # ★ A timeout is NOT a no. Reporting it as one turns a slow network into a false verdict.
    verdict = "unknown" if r["rc"] == 124 else ("works" if passed else "no")
    return {"ok": True, "service": service, "probe": key, "label": p["label"],
            "command": " ".join(p["cmd"]), "verdict": verdict,
            "output": ("(hidden — this probe can print a token)" if p.get("sensitive") and passed
                       else r["out"]),
            "rc": r["rc"], "proves": p["proves"],
            "fail_means": "" if passed else p["fail_means"],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def run_all(service: str, tool: str = "") -> dict:
    rs = [run_probe(service, p["key"], tool) for p in probes_for(service, tool)]
    good = [r for r in rs if r.get("verdict") == "works"]
    return {"service": service, "results": rs,
            "summary": f"{len(good)} of {len(rs)} checks pass",
            "note": ("Each of these asks a DIFFERENT question. A token that exists but has been "
                     "revoked passes 'holds a credential' and fails 'credential works' — that is the "
                     "pair worth reading together.")}


def self_test() -> int:
    fails = []
    ps = probes_for("github")
    if len(ps) < 4:
        fails.append("github offers fewer than four ways to check")
    for p in ps:
        if not p.get("proves") or not p.get("fail_means"):
            fails.append(f"probe {p['key']} does not say what it proves or what a fail does not prove")
    r = run_probe("github", "nonexistent-probe")
    if r.get("ok"):
        fails.append("an unknown probe was accepted")
    g = probes_for("some-unknown-service", "ls")
    if not g or not g[0].get("cmd"):
        fails.append("an unknown service got no usable generic probe")
    # ★ the control that matters: a timeout must read as unknown, never as a NO
    import unittest.mock as m
    with m.patch.object(sys.modules[__name__], "_run", lambda *a, **k: {"rc": 124, "out": "timed out"}):
        t = run_probe("github", "installed")
    if t.get("verdict") != "unknown":
        fails.append(f"a TIMEOUT was reported as {t.get('verdict')!r} — a slow network became a false "
                     f"verdict, which is the failure this whole module is about")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  5 controls pass: github offers 5 distinct checks · every probe states what it proves "
              "AND what a failure does not prove · unknown probe refused · unknown service still gets "
              "a probe · A TIMEOUT READS AS UNKNOWN, NEVER AS NO")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    svc = a[0] if a and not a[0].startswith("-") else "github"
    r = run_all(svc)
    print(f"\n  {svc} — {r['summary']}\n")
    for x in r["results"]:
        mark = {"works": "✓", "no": "✗", "unknown": "?"}.get(x.get("verdict"), "?")
        print(f"   {mark} {x['label']}")
        print(f"       ran     : {x['command']}")
        print(f"       proves  : {x['proves'][:96]}")
        if x.get("output"):
            print(f"       output  : {x['output'].splitlines()[0][:96]}")
        if x.get("fail_means"):
            print(f"       ★ a fail here means: {x['fail_means'][:96]}")
        print()
    print(f"  {r['note']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
