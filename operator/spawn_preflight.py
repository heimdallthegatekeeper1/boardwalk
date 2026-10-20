#!/usr/bin/env python3
"""spawn_preflight.py — check a team spawn BEFORE it is attempted, and say what is wrong in words.

★★★ THE FAILURE THIS EXISTS FOR, 2026-09-12. The operator created an AionUi team called "Computer
    maintenance". Its one agent slot asked for profile "coder". That profile did not exist. Hermes
    said so perfectly:

        Error: Profile 'coder' does not exist. Create it with: hermes profile create coder

    ★ The operator saw none of that. The only thing written to his screen was a `tips` message
      reading, in full:  "Session failed to start".
    ★★ So an actionable one-line fix became a five-word dead end, and his words the next day were
       "I don't know what went wrong." The information existed; it never reached the person.

★★ AND NOTHING CHECKED FIRST. A grep of the AionUi source for any validation of `agentProfile`
   (valid|exist|check|verify|refuse) returns NOTHING. The profile string is carried from the form to
   the `--profile` flag untouched, so the first thing that knows the name is wrong is the subprocess,
   and the subprocess's voice is the thing that gets thrown away.

★ THIS IS WHERE A GATE BELONGS: before the process is spawned, not after it dies. A check that runs
  after the failure can only explain; a check that runs before it can prevent. Both are useful, and
  the second is cheaper.

Run it standalone, or lift its logic into the spawn path.
"""
from __future__ import annotations
import argparse, json, os, shutil, subprocess, sys
from pathlib import Path

PROFILES = Path.home() / ".hermes" / "profiles"
DB = Path.home() / ".config" / "AionUi-Dev" / "aionui" / "aionui.db"


def check_profile(name: str) -> dict:
    if not name:
        return {"ok": False, "field": "agentProfile", "problem": "no profile given",
                "fix": "choose a profile", "fatal": True}
    d = PROFILES / name
    if not d.is_dir():
        near = sorted(p.name for p in PROFILES.iterdir()
                      if p.is_dir() and (name.lower() in p.name.lower() or p.name.lower() in name.lower()))
        return {"ok": False, "field": "agentProfile", "fatal": True,
                "problem": f"profile {name!r} does not exist",
                # ★ the words hermes itself would have used, surfaced BEFORE the spawn
                "fix": f"create it:  hermes profile create {name} --clone-from openrouter",
                "did_you_mean": near[:5],
                "note": "This is the exact failure that killed the Computer maintenance team."}
    out = {"ok": True, "field": "agentProfile", "profile": name, "path": str(d)}
    # ★ a profile that exists but has no credentials passes every gate and then 401s at the first
    #   real call — a known footgun here, so it is checked and reported as a WARNING, not a pass.
    env = d / ".env"
    if not env.is_file():
        out["warning"] = (".env is MISSING. The profile will start, look healthy, and then fail with "
                          "401 Missing Auth on the first model call.")
    else:
        try:
            body = env.read_text()
            if not any("=" in ln and ln.split("=", 1)[1].strip() for ln in body.splitlines()
                       if ln.strip() and not ln.startswith("#")):
                out["warning"] = ".env exists but every value in it is empty."
        except Exception:
            pass
    if not (d / "config.yaml").is_file():
        out["ok"] = False; out["fatal"] = True
        out["problem"] = "profile has no config.yaml"; out["fix"] = "recreate the profile"
    return out


def check_cli(cli: str) -> dict:
    if not cli:
        return {"ok": True, "field": "cliPath", "note": "none set — the default will be used"}
    p = shutil.which(cli) or (cli if Path(cli).is_file() else None)
    if not p:
        return {"ok": False, "field": "cliPath", "fatal": True,
                "problem": f"{cli!r} is not on PATH and is not a file",
                "fix": f"install it, or set cliPath to a full path"}
    return {"ok": True, "field": "cliPath", "resolved": p}


def check_model(model: str, profile: str) -> dict:
    """★ Checks the model against the endpoint the PROFILE actually uses — not against a global list.
    A model id is only meaningful relative to the route that serves it; that mismatch is a recurring
    failure here, and it is invisible until the first call."""
    if not model:
        return {"ok": True, "field": "model", "note": "none set — the profile default will be used"}
    cfg = PROFILES / profile / "config.yaml"
    if not cfg.is_file():
        return {"ok": True, "field": "model", "note": "cannot check — profile has no config"}
    try:
        text = cfg.read_text()
    except Exception:
        return {"ok": True, "field": "model", "note": "cannot read profile config"}
    base = ""
    for ln in text.splitlines():
        if "base_url:" in ln:
            base = ln.split("base_url:", 1)[1].strip(); break
    if "openrouter.ai" not in base:
        return {"ok": True, "field": "model", "endpoint": base,
                "note": "endpoint is not OpenRouter — model list not checked here"}
    env = PROFILES / profile / ".env"
    key = ""
    try:
        import re
        m = re.search(r"sk-or-[A-Za-z0-9._-]+", env.read_text())
        key = m.group(0) if m else ""
    except Exception:
        pass
    if not key:
        return {"ok": True, "field": "model", "note": "no key available to verify the model list"}
    try:
        import urllib.request
        req = urllib.request.Request("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            ids = {m["id"] for m in json.loads(r.read().decode()).get("data", [])}
    except Exception as e:
        # ★ could not check is NOT the same as wrong
        return {"ok": True, "field": "model", "note": f"could not reach the model list ({e.__class__.__name__}) "
                                                     f"— UNCHECKED, not approved"}
    if model in ids:
        return {"ok": True, "field": "model", "model": model, "served_by": base}
    near = sorted(i for i in ids if model.split("/")[-1][:10] in i)[:5]
    return {"ok": False, "field": "model", "fatal": True,
            "problem": f"{model!r} is not served by {base}",
            "fix": "pick a model this endpoint offers", "did_you_mean": near}


def check_workspace(ws: str) -> dict:
    if not ws:
        return {"ok": True, "field": "workspace", "note": "none set"}
    p = Path(ws)
    if not p.exists():
        return {"ok": False, "field": "workspace", "fatal": False,
                "problem": f"{ws} does not exist", "fix": f"mkdir -p {ws!r}"}
    if not os.access(p, os.W_OK):
        return {"ok": False, "field": "workspace", "fatal": True,
                "problem": f"{ws} is not writable", "fix": "fix the permissions"}
    return {"ok": True, "field": "workspace", "path": str(p),
            "empty": not any(p.iterdir()),
            "note": ("empty — that is NORMAL for a new team and is not why a spawn fails"
                     if not any(p.iterdir()) else "")}


def preflight(profile: str, model: str = "", cli: str = "", workspace: str = "") -> dict:
    checks = [check_profile(profile), check_cli(cli), check_model(model, profile),
              check_workspace(workspace)]
    fatal = [c for c in checks if not c.get("ok") and c.get("fatal")]
    soft = [c for c in checks if not c.get("ok") and not c.get("fatal")]
    warn = [c for c in checks if c.get("ok") and c.get("warning")]
    return {"can_spawn": not fatal, "checks": checks,
            "blocking": fatal, "non_blocking": soft, "warnings": warn,
            "verdict": ("this spawn will fail — " + fatal[0]["problem"]) if fatal else
                       ("this spawn should start" + (" (with warnings)" if warn else ""))}


def check_team(name_like: str) -> dict:
    import sqlite3
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cols = [r[1] for r in c.execute("PRAGMA table_info(teams)")]
    row = c.execute("SELECT * FROM teams WHERE name LIKE ?", (f"%{name_like}%",)).fetchone()
    if not row:
        c.close(); return {"error": f"no team matching {name_like!r}"}
    d = dict(zip(cols, row)); c.close()
    ag = json.loads(d["agents"]) if isinstance(d["agents"], str) else (d["agents"] or [])
    return {"team": d["name"], "workspace": d.get("workspace"),
            "slots": [{"agent": a.get("agentName"), "status": a.get("status"),
                       **preflight(a.get("agentProfile", ""), a.get("model", ""),
                                   a.get("cliPath", ""), d.get("workspace", ""))}
                      for a in ag]}


def self_test() -> int:
    fails = []
    r = check_profile("definitely-not-a-profile-xyz")
    if r["ok"] or not r.get("fatal"):
        fails.append("★ a non-existent profile was not refused — the exact bug this file exists for")
    if "hermes profile create" not in r.get("fix", ""):
        fails.append("★ the refusal did not carry the actionable fix line")
    real = [p.name for p in PROFILES.iterdir() if p.is_dir() and (p / "config.yaml").is_file()]
    if real:
        g = check_profile(real[0])
        if not g["ok"]:
            fails.append(f"a REAL profile ({real[0]}) was refused")
    if not check_cli("definitely-not-a-binary-xyz").get("fatal"):
        fails.append("a missing cliPath was not refused")
    if not check_cli("sh")["ok"]:
        fails.append("a real binary was refused")
    w = check_workspace("/definitely/not/a/path/xyz")
    if w["ok"] or w.get("fatal"):
        fails.append("a missing workspace should be a NON-fatal problem, not fatal and not a pass")
    # ★ an empty workspace must not be reported as a cause of failure
    import tempfile
    t = tempfile.mkdtemp()
    ew = check_workspace(t)
    if not ew["ok"] or "not why a spawn fails" not in ew.get("note", ""):
        fails.append("★ an EMPTY workspace was treated as a fault — it is normal for a new team")
    os.rmdir(t)
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: ★ a NON-EXISTENT PROFILE IS REFUSED with hermes' own fix line · "
              "a real profile passes · a missing binary is refused · a real binary passes · a missing "
              "workspace is non-fatal · ★ AN EMPTY WORKSPACE IS NOT CALLED A FAULT")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--team", help="check every slot of an AionUi team by name")
    ap.add_argument("--profile"); ap.add_argument("--model", default="")
    ap.add_argument("--cli", default=""); ap.add_argument("--workspace", default="")
    a = ap.parse_args()
    if a.selftest:
        return self_test()
    if a.team:
        print(json.dumps(check_team(a.team), indent=2)); return 0
    if not a.profile:
        ap.print_help(); return 2
    print(json.dumps(preflight(a.profile, a.model, a.cli, a.workspace), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
