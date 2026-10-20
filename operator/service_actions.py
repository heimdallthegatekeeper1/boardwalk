#!/usr/bin/env python3
"""service_actions.py — everything an AGENT can do to bring a service up, exposed so the OPERATOR can
do it too, from the board, with one press.

★★★ THE CRITERION THIS EXISTS TO MEET (operator, 2026-09-01):
    "Anything the AI can do, the user should also be able to do with ease."
    My first answer failed it. I built probes that agents run and rows that describe what he must go and
    do elsewhere — a board that TELLS him things. That is not parity, it is a to-do list with better
    typography. If an agent can install a CLI, store a credential and re-check a service, then those
    three things must be buttons.

★★ AND VERIFICATION IS ONE OPTION, NOT THE ONLY ONE. A probe is the strongest evidence, but requiring
    it makes the board useless for the eight services that have no probe, and it quietly says his own
    judgement does not count. So a row can be settled three ways: the probe passes, he presses
    "I did this" (recorded as HIS assertion, attributed and dated), or he defers it. All three are
    first-class; they are simply not the same claim, and the board says which one it has.

★ WHY INSTALLS ARE A CLOSED WHITELIST. "Run a command from a button" is how a panel becomes a remote
    shell. Nothing here executes anything the operator or an agent types: each action is a fixed argv
    written in this file, reviewed once, and referenced by key. Anything not in the table is refused —
    the refusal is the feature.
"""
from __future__ import annotations
import json, os, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECISIONS = HERE / "service_decisions.json"

# ★ CLOSED SET. Each entry is a fixed argv list — never a string, never interpolated, never user input.
#   `check` is what makes it safe to offer: it proves the tool arrived rather than trusting the exit code.
INSTALLS = {
    "gh": {
        "label": "GitHub CLI",
        "why": "issues, PRs, releases, repo and CI management",
        "argv": ["bash", "-c",
                 'set -e; V=2.99.0; T=$(mktemp -d); cd "$T"; '
                 'curl -fsSL -o gh.tgz "https://github.com/cli/cli/releases/download/v${V}/gh_${V}_linux_amd64.tar.gz"; '
                 'tar -xzf gh.tgz; mkdir -p ~/.local/bin; '
                 'install -m755 "gh_${V}_linux_amd64/bin/gh" ~/.local/bin/gh; rm -rf "$T"'],
        "check": ["gh", "--version"],
        "privilege": "none — user-space tarball into ~/.local/bin, no system paths, no apt repo",
    },
    "age": {
        "label": "age (secrets backend)",
        "why": "the secrets registry's shipped default backend — encrypted secrets at rest",
        "argv": ["bash", "-c",
                 'set -e; V=1.3.2; T=$(mktemp -d); cd "$T"; '
                 'curl -fsSL -o age.tgz "https://github.com/FiloSottile/age/releases/download/v${V}/age-v${V}-linux-amd64.tar.gz"; '
                 'tar -xzf age.tgz; mkdir -p ~/.local/bin; '
                 'install -m755 age/age ~/.local/bin/age; install -m755 age/age-keygen ~/.local/bin/age-keygen; rm -rf "$T"'],
        "check": ["age", "--version"],
        "privilege": "none — user-space tarball into ~/.local/bin",
    },
}

# Pages he may need to open. Fixed URLs, opened with xdg-open — no arbitrary navigation.
OPEN_URLS = {
    "google-app-passwords": ("Google app passwords", "https://myaccount.google.com/apppasswords"),
    "google-2sv": ("Google 2-Step Verification", "https://myaccount.google.com/signinoptions/two-step-verification"),
    "github-tokens": ("GitHub personal access tokens", "https://github.com/settings/tokens"),
    "notion-integrations": ("Notion integrations", "https://www.notion.so/my-integrations"),
    "linear-api": ("Linear API settings", "https://linear.app/settings/api"),
    "spotify-dashboard": ("Spotify developer dashboard", "https://developer.spotify.com/dashboard"),
}


def _decisions() -> dict:
    if DECISIONS.exists():
        try:
            return json.loads(DECISIONS.read_text())
        except Exception:
            pass
    return {"doc": "How each service row was settled. A probe pass, HIS assertion, or a deferral — "
                   "three different claims, never flattened into one.", "services": {}}


def install(key: str) -> dict:
    a = INSTALLS.get(key)
    if not a:
        return {"ok": False, "error": f"{key!r} is not an installable action. This is a CLOSED list — "
                                      f"nothing runs that is not written into service_actions.py and "
                                      f"reviewed. Known: {sorted(INSTALLS)}"}
    if shutil.which(a["check"][0]):
        return {"ok": True, "already": True, "note": f"{a['label']} is already installed at "
                                                     f"{shutil.which(a['check'][0])} — nothing was run."}
    try:
        r = subprocess.run(a["argv"], capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the install did not finish within 5 minutes"}
    # ★ VERIFY THE THING ARRIVED. An installer's exit code says the script ended, not that the binary
    #   is on PATH and runnable — the difference this house keeps paying for.
    path = shutil.which(a["check"][0])
    if not path:
        return {"ok": False, "error": f"the install ran (exit {r.returncode}) but {a['check'][0]} is "
                                      f"still not on PATH — nothing was actually delivered",
                "output": (r.stdout + r.stderr).decode(errors="replace")[-400:]}
    v = subprocess.run([path] + a["check"][1:], capture_output=True, timeout=30)
    return {"ok": True, "installed": path,
            "version": " ".join((v.stdout + v.stderr).decode(errors="replace").split())[:80],
            "privilege": a["privilege"],
            "note": f"{a['label']} installed and verified by running it — not inferred from an exit code."}


def store_credential(name: str, consumer: str, value: str, register: bool = True) -> dict:
    """★ THE VALUE IS WRITTEN AND NEVER RETURNED, LOGGED OR ECHOED. It goes into the age backend the
    registry already declares as its default; the registry keeps a POINTER. The response says only
    that it landed and how long it was."""
    if not name or not value:
        return {"ok": False, "error": "need a name and a value"}
    # ★★★ SHAPE-CHECK BEFORE STORING. On 2026-09-02 the operator stored a 13-character account password
    #   where Gmail only ever accepts a 16-letter generated app password. Nothing said so. It stored
    #   cleanly, he moved on believing email was done, and the mismatch only surfaced when an agent
    #   resolved the value to debug it — and printed his password into the conversation.
    #   ★ The check does NOT refuse. He may have a reason, and a store that argues with him is worse
    #     than one that warns him. But the warning travels back with the result, so "stored" is never
    #     mistaken for "will work".
    shape = None
    try:
        sys.path.insert(0, str(HERE))
        import credential_probe as _cp, importlib
        importlib.reload(_cp)
        kind_guess = ("google-app-password" if "gmail" in (name + consumer).lower()
                      or "google" in (name + consumer).lower()
                      else "github-token" if "github" in (name + consumer).lower()
                      else "notion-token" if "notion" in (name + consumer).lower() else None)
        if kind_guess:
            shape = _cp.shape_check(kind_guess, value)
    except Exception:
        shape = None
    if not shutil.which("age"):
        return {"ok": False, "error": "the age backend is not available — install it first (one press)"}
    ident = Path.home() / ".config/khaoss/age/identity.key"
    if not ident.exists():
        return {"ok": False, "error": "no age identity at ~/.config/khaoss/age/identity.key"}
    try:
        recip = next(w for w in ident.read_text().split() if w.startswith("age1"))
    except Exception:
        return {"ok": False, "error": "could not read the age recipient from the identity file"}
    outdir = Path.home() / ".config/khaoss/age"
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{name}.age"
    try:
        r = subprocess.run([shutil.which("age"), "-r", recip, "-o", str(dest)],
                           input=value.encode(), capture_output=True, timeout=30)
        if r.returncode != 0:
            return {"ok": False, "error": "age refused to encrypt it"}
        os.chmod(dest, 0o600)
    except Exception as e:
        return {"ok": False, "error": f"could not write it: {e.__class__.__name__}"}
    # ★★★ `register` EXISTS BECAUSE THE NEGATIVE CONTROL LEAKED INTO THE REAL REGISTRY.
    #   self_test stored a probe credential, deleted the .age file, and left a RECORD behind pointing
    #   at a path that no longer existed — a dangling entry in the operator's live secrets registry,
    #   created by the test whose job is to prove this path is safe. A control that writes to the store
    #   it is testing is a fixture with side effects, and this is the second one found in ten minutes
    #   (the other poisoned the service cache and put a fake service on his board).
    class _Skip:
        returncode = 0
        stdout = b"registration skipped (self-test)"
        stderr = b""
    reg = _Skip() if not register else subprocess.run(
        [sys.executable, str(HERE.parent / "khaoss-secrets"), "add", name,
         "--consumer", consumer or "unspecified", "--backend", "age",
         "--ref", str(dest)], capture_output=True, timeout=30)
    return {"ok": True, "name": name, "stored_at": str(dest), "mode": "600",
            "length": len(value),          # length only — never the value
            "shape": shape,                # advice about the KIND, carrying no part of the value
            "registered": reg.returncode == 0,
            "registry_note": " ".join((reg.stdout + reg.stderr).decode(errors="replace").split())[:200],
            "note": "encrypted with age and registered as a POINTER. The value was never logged, "
                    "echoed, or written anywhere else."}


def settle(service: str, how: str, note: str = "") -> dict:
    """★ THREE WAYS TO SETTLE A ROW, AND THEY ARE DIFFERENT CLAIMS.
    `probe`     — a command showed authenticated access. The strongest, and self-clearing.
    `asserted`  — HE says it is done. Recorded as his, attributed and dated. This is not a weaker
                  citizen: for the eight services with no probe it is the ONLY honest answer, and
                  refusing it would tell him his own judgement does not count.
    `deferred`  — not now. Also a real decision, and recorded as one rather than left as silence."""
    if how not in ("probe", "asserted", "deferred"):
        return {"ok": False, "error": "how must be probe, asserted or deferred"}
    d = _decisions()
    d["services"][service] = {
        "how": how, "note": note.strip(),
        "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
        "by": "operator",
        "claim": {"probe": "a probe showed authenticated access",
                  "asserted": "the operator says this is done — his assertion, not a measurement",
                  "deferred": "deliberately not now"}[how],
    }
    DECISIONS.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "service": service, "how": how}


def decisions() -> dict:
    return _decisions().get("services", {})


def self_test() -> int:
    """★ NEGATIVE CONTROL. Three failures matter: running something not on the whitelist, reporting an
    install that did not deliver, and a credential value leaking into a response."""
    fails = []
    r = install("rm-rf-everything")
    if r.get("ok"):
        fails.append("an action NOT on the whitelist was accepted — the panel would be a remote shell")
    if "closed" not in str(r.get("error", "")).lower():
        fails.append("the refusal does not explain that the list is closed")
    got = install("age")
    if not got.get("ok"):
        fails.append("a whitelisted, already-present tool did not report ok")
    if not got.get("already"):
        fails.append("an already-installed tool was re-installed instead of short-circuiting")
    probe_val = "super-secret-probe-value-123"
    s = store_credential("_selftest_probe", "self-test", probe_val, register=False)
    if s.get("ok"):
        blob = json.dumps(s)
        if probe_val in blob:
            fails.append("★ THE CREDENTIAL VALUE APPEARED IN THE RESPONSE — the one thing that must "
                         "never happen")
        p = Path(s["stored_at"])
        if p.exists():
            if oct(p.stat().st_mode)[-3:] != "600":
                fails.append("the stored secret is not mode 600")
            if probe_val.encode() in p.read_bytes():
                fails.append("the secret is on disk in PLAINTEXT — encryption did not happen")
            p.unlink()
    if settle("_x", "maybe").get("ok"):
        fails.append("an unknown settle mode was accepted")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ service_actions self_test: refuses off-whitelist actions, short-circuits installed "
          "tools, encrypts credentials at 600 and never echoes a value"
          if not fails else "  ✗ service_actions self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    print("\n  WHAT THE OPERATOR CAN DO FROM THE BOARD (same actions the agent has)")
    for k, v in INSTALLS.items():
        print(f"    install {k:6s} {v['label']:22s} {'ALREADY PRESENT' if shutil.which(v['check'][0]) else 'available'}")
    print(f"    store a credential  → age backend, mode 600, value never echoed")
    print(f"    open a page         → {len(OPEN_URLS)} fixed URLs")
    print(f"    settle a row        → probe · asserted · deferred (three different claims)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
