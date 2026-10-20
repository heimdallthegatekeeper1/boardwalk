#!/usr/bin/env python3
"""strategy_registry.py — WHAT WE ALREADY KNOW ABOUT REACHING A SERVICE, checked before anyone starts.

★★★ THE INCIDENT THIS EXISTS TO PREVENT (2026-09-03). The operator started a guided Proton Mail login.
    The PA lead reached for Proton Bridge, spent metered data on a ~100MB download and reached for
    `sudo snap install` — and Bridge does not work on Proton Free at all. Meanwhile:
      · a Proton credential ALREADY EXISTED in the registry (`example-account`, since 2026-08-21)
      · a second one had been stored the day before through the board
      · and a persistent agent-driven Chromium was ALREADY RUNNING on a debugging port, which reaches
        Proton's WEB interface — the approach that works on Free, where IMAP does not exist
    Nobody checked. Not because anyone was careless, but because there was nowhere to check.

★★ SO A STRATEGY IS A FIRST-CLASS THING, not something rediscovered per attempt. Each records how it
    works, what it needs, whether the secret it needs is ALREADY HELD, and what would make it
    impossible. `check_first()` is the one call an agent makes before choosing a path.

★ CLEARANCE AND TERRITORY ARE PART OF THE ANSWER, not a separate gate bolted on afterwards. A strategy
  an agent cannot execute at its rank is not "available to it", and saying so up front is kinder and
  cheaper than letting it discover the wall. The ledger is khaoss-access.json (SHAMAN ranks); this
  reads it rather than keeping a second copy.
"""
from __future__ import annotations
import json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ACCESS = Path.home() / ".config/AionUi-Dev/khaoss-access.json"
SECRETS_CLI = ROOT / "khaoss-secrets"

RANK_ORDER = ["Initiate", "Seeker", "Adept", "Oracle", "Sage"]

# One record per KNOWN WAY to reach a service. Agents add to this; nobody rediscovers.
STRATEGIES = {
    "proton-mail": [
        {
            "id": "proton-web-session",
            "name": "Drive Proton's web interface in the persistent agent browser",
            "how": ("A Chromium already runs with --remote-debugging-port=9333 and its own profile. Log "
                    "in to mail.proton.me ONCE in that browser; agents then drive the live session over "
                    "CDP. No IMAP, no Bridge, no daemon."),
            "works_on": "★ ANY plan, including Proton FREE — this is the only approach that does",
            "needs_secret": "example-account",
            "needs_persistent": "the agent Chromium must stay open, and its profile must survive reboots",
            "min_rank": "Adept",
            "territory": "pa",
            "known_risks": ("The profile currently lives under /tmp, so every login is one cleanup or "
                            "reboot away from being lost. Move it somewhere durable BEFORE logging in, "
                            "or the work is done twice."),
        },
        {
            "id": "proton-bridge-imap",
            "name": "Proton Bridge, then himalaya over IMAP",
            "how": "Install the Bridge package, run it as a daemon, use the app password it issues.",
            "works_on": "PAID plans only (Plus / Unlimited / Business). Impossible on Free.",
            "needs_secret": "proton-bridge-app-password",
            "needs_persistent": "Bridge runs as a daemon that must stay up",
            "min_rank": "Sage",
            "territory": "pa",
            "known_risks": ("~100MB download on a metered link and a privileged system install with no "
                            "user-space alternative. Fails at the LAST step if the plan is Free — which "
                            "is what happened on 2026-09-03."),
        },
    ],
    "gmail": [
        {
            "id": "gmail-app-password-imap",
            "name": "himalaya over IMAP with a Google app password",
            "how": "The config already exists and parses; both backends resolve. One credential missing.",
            "works_on": "any Google account with 2-Step Verification enabled",
            "needs_secret": "gmail-app-password",
            "needs_persistent": None,
            "min_rank": "Adept",
            "territory": "pa",
            "known_risks": "Google refuses normal account passwords for IMAP; it must be a 16-letter app password.",
        },
    ],
}


def _held_secrets() -> dict:
    """Which secrets the registry ALREADY HOLDS — names and backends only, never values."""
    try:
        out = subprocess.run([sys.executable, str(SECRETS_CLI), "list"],
                             capture_output=True, timeout=25).stdout.decode(errors="replace")
    except Exception:
        return {}
    held, name = {}, None
    for line in out.splitlines():
        st = line.strip()
        if st and not st.startswith(("#", "backend=", "consumer=", "cap=", "created=")) and "=" not in st:
            name = st
        elif name and st.startswith("backend="):
            held[name] = st.split()[0].split("=", 1)[1]
    return held


def _rank_of(agent_id: str) -> str | None:
    try:
        d = json.loads(ACCESS.read_text())
        a = (d.get("agents") or {}).get(agent_id)
        return (a or {}).get("rank")
    except Exception:
        return None


def check_first(service: str, agent_id: str | None = None) -> dict:
    """★ THE ONE CALL TO MAKE BEFORE CHOOSING A PATH.
    Returns every known strategy with what already exists for it, so nobody starts from scratch or
    walks into a wall someone already found."""
    held = _held_secrets()
    rank = _rank_of(agent_id) if agent_id else None
    rank_i = RANK_ORDER.index(rank) if rank in RANK_ORDER else None
    rows = []
    for s in STRATEGIES.get(service, []):
        need = s.get("needs_secret")
        # ★★ TWO CONFIDENCES, BECAUSE ONE WORD IS NOT A MATCH. The first version matched any secret
        #    sharing a token with the requirement, so `example-account` was reported HELD for
        #    `proton-bridge-app-password` — they share "proton" and nothing else. A registry that says
        #    "you already have this" when you do not sends an agent down a path that fails at the
        #    credential, which is precisely the failure it exists to prevent, inverted.
        #    EXACT: the name matches, or every distinguishing word is present.
        #    POSSIBLE: something related exists — offered as "verify", never as held.
        exact, possible = [], []
        if need:
            words = [w for w in need.lower().split("-") if len(w) > 3]
            for k in held:
                kl = k.lower().replace(" ", "-")
                if need.lower() == kl or all(w in kl for w in words):
                    exact.append(k)
                elif any(w in kl for w in words):
                    possible.append(k)
        matches = exact
        blockers = []
        if need and not exact:
            blockers.append(f"no secret matching {need!r} is held"
                            + (f" (possibly related, verify before relying on it: {', '.join(possible)})"
                               if possible else ""))
        if rank_i is not None and RANK_ORDER.index(s["min_rank"]) > rank_i:
            blockers.append(f"needs rank {s['min_rank']}; this agent is {rank}")
        rows.append({**s, "secret_held": matches or None, "secret_possible": possible or None,
                     "blockers": blockers,
                     "status": "ready" if not blockers else "blocked"})
    return {"service": service, "agent_rank": rank, "strategies": rows,
            "held_secrets_matching": {k: v for k, v in held.items()
                                      if service.split("-")[0] in k.lower()},
            "note": ("Check this BEFORE starting. Every row here was learned the expensive way once; "
                     "the point is that it is only expensive once.")}


def self_test() -> int:
    """★ NEGATIVE CONTROL. The failure that matters is telling an agent a path is available when the
    secret it needs is not held, or when its rank cannot execute it."""
    fails = []
    r = check_first("proton-mail")
    if not r["strategies"]:
        fails.append("no strategies for a service that has two — the registry read nothing")
    web = next((s for s in r["strategies"] if s["id"] == "proton-web-session"), None)
    if not web:
        fails.append("the web-session strategy is missing")
    elif not web.get("secret_held"):
        fails.append("★ a Proton secret IS held in the registry but the matcher did not find it — this "
                     "is exactly the miss that caused the incident")
    # a rank that cannot do it must be told so
    import types
    saved = globals()["_rank_of"]
    try:
        globals()["_rank_of"] = lambda a: "Initiate"
        low = check_first("proton-mail", agent_id="x")
        if all(s["status"] == "ready" for s in low["strategies"]):
            fails.append("an Initiate was told every strategy was ready — clearance is not being applied")
    finally:
        globals()["_rank_of"] = saved
    unknown = check_first("no-such-service")
    if unknown["strategies"]:
        fails.append("invented strategies for a service with none")
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ strategy_registry self_test: finds the held Proton secret, applies rank, and invents "
          "nothing for an unknown service" if not fails else "  ✗ strategy_registry self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test()
    svc = a[0] if a and not a[0].startswith("-") else "proton-mail"
    agent = a[1] if len(a) > 1 else None
    r = check_first(svc, agent)
    print(f"\n  STRATEGIES FOR {svc}" + (f"  (agent rank: {r['agent_rank']})" if r["agent_rank"] else ""))
    for s in r["strategies"]:
        mark = "✓" if s["status"] == "ready" else "✗"
        print(f"\n    {mark} {s['name']}")
        print(f"        how        {s['how']}")
        print(f"        works on   {s['works_on']}")
        print(f"        secret     {s['needs_secret']}  →  "
              + ("HELD: " + ", ".join(s["secret_held"]) if s["secret_held"] else "NOT HELD"))
        if s.get("needs_persistent"):
            print(f"        persistent {s['needs_persistent']}")
        print(f"        rank       {s['min_rank']} · territory {s['territory']}")
        print(f"        risks      {s['known_risks']}")
        if s["blockers"]:
            print(f"        ★ BLOCKED  {'; '.join(s['blockers'])}")
    print(f"\n  {r['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
