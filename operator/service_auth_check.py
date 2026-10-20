#!/usr/bin/env python3
"""service_auth_check.py — for each service the agents need, MEASURE whether access actually works.

★★★ WHY THIS EXISTS. The PA login audit (2026-09-01) was measured the right way — it loaded pages and
    read what came back — and it correctly stated its own scope: the browser was a FRESH profile, so
    "0 of 11 logged in" means AGENTS HAVE NO SESSIONS, not that the operator is logged out anywhere.
    That distinction is the whole reason this file exists: after he logs something in, somebody has to
    establish that the AGENT can now use it, which is a different claim from "he typed a password".

★★ A LOGIN IS THE ARCHETYPAL THING THAT LOOKS DONE AND IS NOT. Sessions expire, tokens are scoped too
    narrowly, a CLI authenticates a different account than the browser, 2FA half-completes. So every
    service here declares a PROBE — a command whose output distinguishes working from not — and a
    service with no probe is reported as UNPROVEN, never as fine.

★ THREE OUTCOMES, AND THE THIRD IS THE POINT:
      works    — the probe ran and showed authenticated access
      no       — the probe ran and showed no access (this is useful; it is a measurement)
      unproven — no probe exists, or the tool is not installed. NOT a pass. Absence of a check is not
                 evidence of health, and reporting it as green is the failure this house keeps finding.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# ★ Each probe must be able to say BOTH things. A command that exits 0 whether or not you are
#   authenticated proves nothing — the `expect` string is what makes the answer discriminating.
SERVICES = [
    {# A capability the guided run can SHOW, with its exact one command. It deliberately does NOT
     # perform the grant itself: `settle()` records decisions, it does not wield root, and the
     # existing rows guide the operator rather than acting for him ("gh auth login ...").
     # ★ The probe is `reconcile`, the truth-teller: it reports whether the ledger and sudo AGREE.
     #   A status that only counted ledger grants would print "blocked by default" while three
     #   permanent root grants sat on disk — the exact lie this whole tool exists to kill.
     "id": "capability-root-grants", "label": "Root capabilities (timed grants)",
     "tool": "capability-grant",
     "probe": ["/usr/bin/python3", "str(Path(__file__).resolve().parent.parent)/bin/capability-grant.py", "reconcile"],
     "expect_ok": "agree exactly", "expect_fail": "UNMANAGED",
     "unlocks": "Root for a NAMED script, for a set number of minutes, recorded with provenance and "
                "withdrawn mechanically at expiry — instead of standing sudoers entries that never die.",
     "how": "bin/capability-grant.py propose drive.phase1.finish --ttl 60   "
            "then: capability-grant.py issue <cap>  ·  sudo capability-grant-apply.sh"},

    {"id": "github", "label": "GitHub", "tool": "gh",
     "probe": ["gh", "auth", "status"], "expect_ok": "Logged in to",
     "unlocks": "issues, PRs, releases, repo and CI management, the full GitHub API",
     "how": "gh auth login   (browser or token; it prints the URL and a one-time code)"},
    {# ★★★ THE LABEL SAID PROTON AND THE CONFIG WAS GMAIL, AND THAT MISMATCH STARTED A REAL DETOUR.
     # On 2026-09-03 the operator launched a guided Proton login. The row he was working from read
     # "Proton Mail (via himalaya)" — but ~/.config/himalaya/config.toml has targeted
     # operator@example.invalid since May, and the advice under it was entirely about GOOGLE app passwords.
     # The lead spent twenty minutes downloading Proton Bridge before anyone checked the plan.
     # ★ The id stays `email-proton` so existing advice and PA's edits keep resolving; only the LABEL
     #   is corrected, because the label is what a human reads and acts on.
     "id": "email-proton", "label": "Gmail (via himalaya)", "tool": "himalaya",
     "probe": ["himalaya", "envelope", "list"], "expect_ok": None, "expect_fail": "authentication failed",
     "proves": "absence-of-failure",
     "unlocks": "read, search, send, archive — the single biggest unlock for the least effort",
     # ★ The precise diagnosis, measured 2026-09-01, so this row is both self-clearing AND specific.
     #   Everything except the credential is done: himalaya v1.2.0 installed, config parses (it failed
     #   on one word — `type = "passwd"` had to become `"password"`, a schema change from an older
     #   himalaya), IMAP and SMTP both resolve. ~/.hermes/profiles/<your-profile>/.env holds
     #   GMAIL_APP_PASSWORD at mode 600, but the stored value is 13 characters with punctuation and a
     #   Google app password is 16 letters — so it is almost certainly the normal account password,
     #   which Google has not accepted for IMAP since 2022.
     #   ★★ The probe here is `envelope list` — it actually reaches the IMAP server and prints
     #   "authentication failed" on a bad credential, rather than `account list` which merely proves
     #   the config PARSES. Parsing is not fetching. The expect_fail string is the measured error.
     "how": "Everything except the credential is done. 1) Turn on 2-Step Verification for "
            "operator@example.invalid — Google will not offer app passwords without it. "
            "2) Create one at myaccount.google.com/apppasswords for 'himalaya'. "
            "3) Replace GMAIL_APP_PASSWORD in ~/.hermes/profiles/<your-profile>/.env with the 16-character "
            "value. Nothing else needs touching — the config and backends are already proven."},
    {"id": "keyring", "label": "Desktop keyring (libsecret)", "tool": "secret-tool",
     "probe": ["secret-tool", "search", "--all", "service", "khaoss-probe-nonexistent"],
     "expect_ok": None, "proves": "reachability",   # ★ declared: the store answering IS the claim          # exit 1 with no result IS the healthy answer for a keyring query
     "unlocks": "a 0700 store the desktop already unlocks at login — no new dependency",
     "how": "nothing to do; it is present and round-trips"},
    {"id": "age-backend", "label": "age secrets backend", "tool": "age",
     "probe": ["age", "--version"], "expect_ok": "v1", "proves": "reachability",
     "unlocks": "the registry's SHIPPED DEFAULT backend — encrypted secrets at rest, one static binary",
     "how": "installed 2026-09-01 to ~/.local/bin from the release tarball; identity at "
            "~/.config/khaoss/age/identity.key (0600)"},
    # ── Probes added 2026-09-01 — CLIs installed via user-space routes (npm/pipx). Two of them turned
    #    out to be squats/stubs, measured not assumed — see the NO_PROBE notes below. ──
    {"id": "gws", "label": "Google Workspace (gws)", "tool": "gws",
     "probe": ["gws", "auth", "status"], "expect_ok": None, "expect_fail": '"auth_method": "none"',
     "proves": "absence-of-failure",
     "unlocks": "Gmail read/send, Calendar read/create, Drive read/write, Docs/Sheets",
     "how": "gws v0.22.5 installed via npm (real tool, verified). Needs a Google Cloud OAuth client: "
            "`gws auth setup` (creates client_secret.json) then `gws auth login` (browser consent). "
            "The google-workspace skill exists."},
    {"id": "spotify", "label": "Spotify (spotifycli)", "tool": "spotifycli",
     "probe": ["spotifycli", "--status"], "expect_ok": None, "expect_fail": "No mpris clients",
     "proves": "absence-of-failure",
     "unlocks": "control a running Spotify desktop client (MPRIS) — play/pause/skip/status",
     "how": "spotifycli v2.1.0 installed via pipx. ★ It is an MPRIS controller, NOT a Web API client — "
            "it controls the Spotify desktop app when it is running. No API credentials needed; just "
            "launch the Spotify desktop client and this probe goes green."},
]

# Services with no non-interactive probe. Listed so their absence is VISIBLE rather than implied.
NO_PROBE = [
    ("slack", "Slack", "OAuth app or user token; no probe until one exists"),
    ("discord", "Discord", "no supported non-interactive path; browser session only"),
    ("youtube", "YouTube", "browser session, or Data API key for read-only"),
    ("linkedin", "LinkedIn", "no sanctioned automation path — browser session only"),
    # ── ★ TWO SQUAT/STUB INSTALLS, MEASURED 2026-09-01. The npm packages `@linear/cli` and `notion-cli`
    #    are NOT the real tools — `lin issues list` prints a Qwen2.5-Coder model card (a hijacked
    #    package) and `notion` is a one-line stub that only echoes "Welcome to notion-cli". Neither can
    #    reach its named service, so a probe over them would report `no` while implying the tool exists.
    #    The real paths are REST + token (curl), already documented in the linear and notion skills. ──
    ("linear", "Linear", "★ npm @linear/cli is a hijacked package (prints a model card, no API). Real "
     "path: REST GraphQL + personal API key via curl — the linear skill documents it"),
    ("notion", "Notion", "★ npm notion-cli is a one-line stub (echoes 'Welcome to notion-cli'). Real "
     "path: REST API + integration token via curl — the notion skill documents it"),

    ("proton-mail", "Proton Mail (via Bridge)", "requires a PAID Proton plan — Bridge is unavailable on Free, and Proton offers no IMAP there at all. Verify the subscription before spending a download or a privileged install."),
]


def _run(cmd, timeout=8):
    # ★ 8s, not 20. A cold pass runs every probe in series; at 20s each, one hanging network call held
    #   the whole block for a quarter of a minute. A probe that cannot answer in 8 seconds is telling
    #   us something too, and it is recorded as a timeout rather than waited on.
    exe = shutil.which(cmd[0])
    if not exe:
        return None, f"{cmd[0]} is not on PATH"
    try:
        r = subprocess.run([exe] + cmd[1:], capture_output=True, timeout=timeout)
        out = (r.stdout + r.stderr).decode(errors="replace")
        # ★ Strip ANSI. Tools colour their errors, and the escape codes rendered in the panel as
        #   "□[2m2026-09-02T04:19□[0m…" — turning the one line that explains a failure into noise. The
        #   operator reads these; a diagnostic he cannot read is not a diagnostic.
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", out)
        return r, out
    except subprocess.TimeoutExpired:
        return None, f"{cmd[0]} did not answer within {timeout}s"
    except Exception as e:
        return None, f"{cmd[0]} failed to run: {e.__class__.__name__}"


def _ensure_path() -> None:
    """★ The probe runner must see the same tool surface the agents do. Without this, `gh` and
    `himalaya` — both installed and verified — report UNPROVEN because shutil.which() cannot find
    them. Not a tooling gap: a PATH gap."""
    for pfx in [os.path.expanduser("~/.local/bin"),
                os.path.expanduser("~/.cargo/bin"),
                os.path.expanduser("~/.nvm/versions/node/v20.19.6/bin")]:
        if os.path.isdir(pfx) and pfx not in os.environ.get("PATH", ""):
            os.environ["PATH"] = pfx + os.pathsep + os.environ.get("PATH", "")


_CACHE = {"at": 0.0, "data": None}
CACHE_TTL = 90          # ★ empirical and labelled: long enough that opening the board is instant,
                        #   short enough that a login he just did shows up on the next look.


def check(force: bool = False) -> dict:
    """★★ THE PROBES ARE NETWORK CALLS COSTING ~5 SECONDS. Running them on every page load made the
    services block appear five seconds late, which reads as broken — and on a surface built for parity,
    "his button feels broken" IS a parity failure, not a performance note.
    So the result is cached briefly and the page ALWAYS SAYS HOW OLD IT IS. A cached answer presented as
    current would be the staleness defect this board already carries an invariant against; an honest
    timestamp costs one line and keeps the speed."""
    import time as _t, json as _j
    # ★★★ THE CACHE MUST LIVE OUTSIDE MODULE STATE. My first version held it in a module-level dict —
    #   and the panel calls importlib.reload() on every request, which wipes module state. So the cache
    #   never survived a single HTTP call and the endpoint stayed at ~1.4s while REPORTING a cache hit.
    #   Measured, not assumed: two consecutive requests both took the full time.
    #   ★ A cache whose hit rate you have not measured is a comment, not a cache.
    cf = HERE / "service_check_cache.json"
    if not force and cf.exists():
        try:
            blob = _j.loads(cf.read_text())
            age = _t.time() - blob.get("at", 0)
            if age < CACHE_TTL:
                d = dict(blob["data"]); d["age_seconds"] = int(age); d["from_cache"] = True
                return d
        except Exception:
            pass
    d = _check_uncached()
    try:
        cf.write_text(_j.dumps({"at": _t.time(), "data": d}))
    except Exception:
        pass
    d = dict(d); d["age_seconds"] = 0; d["from_cache"] = False
    return d


def _check_uncached() -> dict:
    _ensure_path()
    rows = []
    for s in SERVICES:
        r, out = _run(s["probe"])
        if r is None:
            rows.append({**s, "state": "unproven", "detail": out})
            continue
        # ★ An expect_fail string is the DISCRIMINATING half for auth probes: the command produces
        #   output in BOTH states (a table on success, an error on failure), so "the tool answered"
        #   cannot mean "works". expect_fail names the failure signature; when present, the answer is
        #   `no` even though the tool ran. Measured 2026-09-01: himalaya prints "authentication
        #   failed", spotifycli prints "No mpris clients" — both are errors that a bare expect_ok
        #   would have misread as health.
        if s.get("expect_fail") and s["expect_fail"].lower() in out.lower():
            rows.append({**s, "state": "no", "detail": " ".join(out.split())[:180] or "(no output)"})
            continue
        # ★★ ABSENCE-OF-FAILURE: a probe that names its failure signature and DECLARES that the
        #   absence of it is the success signal (proves: "absence-of-failure"). This is distinct from
        #   reachability (presence of the tool is the claim) and from expect_ok (a positive marker must
        #   appear). Legitimate only for status-style commands whose output is genuinely
        #   discriminating — `gws auth status` reports auth_method:none only when signed out, and
        #   `spotifycli --status` reports "No mpris clients" only when the client is not running.
        #   Without the declaration this would fall through to the expect_ok guard and report
        #   UNPROVEN on the healthy path, so a fixed service would never clear its board row.
        if (s.get("expect_fail") and s.get("expect_ok") is None
                and s.get("proves") == "absence-of-failure"):
            rows.append({**s, "state": "works",
                         "detail": ("the named failure signature is absent — and this service declares "
                                    "that absence as its success signal")})
            continue
        if s["expect_ok"] is None:
            # ★★★ `expect_ok: None` WAS A FOOTGUN AND IT FIRED WITHIN THE HOUR.
            #   I wrote it for the keyring, where "the tool answered" genuinely IS the evidence. It
            #   reads as "no expectation needed", so the next contributor — the PA lead, adding probes
            #   exactly as asked — used it for Linear and Spotify. Those then reported `works` on the
            #   strength of the binary existing, which says nothing about authentication. Three false
            #   greens, in the one file whose whole job is to refuse them.
            #   ★ A probe that cannot say NO is not a probe. So reachability-only is now legal ONLY
            #     when the service DECLARES that reachability is the claim it is making.
            if s.get("proves") == "reachability":
                ok = True
                detail = "the tool answered — and this service declares reachability as its claim"
            else:
                rows_state = ("unproven", "this probe has no authenticated marker to look for, so it "
                              "passes whenever the binary exists — it cannot distinguish signed-in "
                              "from signed-out. Give it an `expect_ok` string that only appears when "
                              "authenticated, or declare `proves: \"reachability\"` if presence really "
                              "is the claim.")
                rows.append({**s, "state": rows_state[0], "detail": rows_state[1]})
                continue
        else:
            # ★★★ A VERSION CHECK CANNOT PROVE AUTHENTICATION, whatever string it is asked to match.
            #   `notion --version` expecting "notion" passes whenever the binary prints its own name.
            #   That is the same false green as the reachability case wearing an expect_ok, so the
            #   guard is on the PROBE SHAPE rather than on the one service that hit it: if the command
            #   is a version/help invocation and the service has not declared that presence is its
            #   claim, the answer is UNPROVEN — never `works`.
            versiony = any(a in ("--version", "-V", "-v", "--help", "-h", "version")
                           for a in s["probe"][1:])
            if versiony and s.get("proves") != "reachability":
                rows.append({**s, "state": "unproven",
                             "detail": ("this probe only asks the tool for its version, which it will "
                                        "print whether or not anyone is signed in. It cannot say NO, so "
                                        "its YES means nothing. Point it at a command that requires "
                                        "auth — a whoami, an account list, a fetch — or declare "
                                        "`proves: \"reachability\"` if presence really is the claim.")})
                continue
            ok = s["expect_ok"].lower() in out.lower()
            detail = " ".join(out.split())[:180] or "(no output)"
        rows.append({**s, "state": "works" if ok else "no", "detail": detail})
    for sid, label, why in NO_PROBE:
        rows.append({"id": sid, "label": label, "tool": None, "state": "unproven",
                     "detail": why, "unlocks": "", "how": ""})
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("works", "no", "unproven")}
    return {"checked_at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
            "services": rows, "counts": counts,
            "note": ("`unproven` is NOT a pass — it means nothing here established whether that service "
                     "works. Of the two, `no` is the more useful answer, because it was measured.")}


def self_test() -> int:
    """★ NEGATIVE CONTROL.
    ★★★ IT CALLS _check_uncached(), NEVER check(). This test plants SYNTHETIC services ("a probe that
       must fail", "definitely absent") and, when it called check(force=True), those results were
       WRITTEN TO THE CACHE FILE — which the panel reads. A test artefact appeared on the operator's
       live board as a real service, and the real services disappeared with it. A negative control that
       can write to the surface it is testing is not isolated; it is a fixture with side effects.
    ★★ EVERY CALL HERE FORCES A LIVE RUN. When the file-backed cache was added, this test began
       silently passing a CACHED result while the mutated SERVICES list it had just planted was never
       exercised — so the control proved nothing and said it had. A control that can be served a cache
       is not a control; caching is exactly the mechanism that makes a stale yes look like a fresh one. The failure that matters is a probe that returns `works` regardless — a
    check that cannot say no. So: a service whose tool is absent must come back `unproven` (never
    `works`), and a probe pointed at a certainly-unauthenticated command must be able to say `no`."""
    fails = []
    d = _check_uncached()
    if not d["services"]:
        fails.append("no services checked — an empty population, and any verdict over it is vacuous")
    for r in d["services"]:
        if r["state"] == "works" and not r.get("tool"):
            fails.append(f"{r['id']} reports works with no tool behind it")
    saved = SERVICES[:]
    try:
        SERVICES.clear()
        SERVICES.append({"id": "ghost", "label": "definitely absent", "tool": "khaoss-no-such-binary",
                         "probe": ["khaoss-no-such-binary", "--version"], "expect_ok": "x",
                         "unlocks": "", "how": ""})
        g = _check_uncached()["services"][0]
        if g["state"] != "unproven":
            fails.append("a service whose tool does not exist was not reported UNPROVEN — a missing "
                         "check would read as a healthy one")
        SERVICES.clear()
        SERVICES.append({"id": "cannot-match", "label": "probe that must fail", "tool": "echo",
                         "probe": ["echo", "definitely-not-authenticated"],
                         "expect_ok": "Logged in to", "unlocks": "", "how": ""})
        n = _check_uncached()["services"][0]
        if n["state"] != "no":
            fails.append("a probe whose expectation was absent did not report `no` — it cannot "
                         "distinguish authenticated from not, so its `works` would mean nothing")
    finally:
        SERVICES.clear(); SERVICES.extend(saved)
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ service_auth_check self_test: a missing tool reports UNPROVEN, and a probe that cannot "
          "find its expectation reports NO" if not fails else "  ✗ service_auth_check self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    d = check()
    if "--json" in sys.argv:
        print(json.dumps(d, indent=2)); return 0
    mark = {"works": "✓", "no": "✗", "unproven": "·"}
    print("\n  SERVICE ACCESS — measured, not assumed")
    for r in d["services"]:
        print(f"    {mark[r['state']]} {r['label']:34s} {r['state']}")
        if r.get("detail"):
            print(f"        {str(r['detail'])[:150]}")
        if r["state"] != "works" and r.get("how"):
            print(f"        → {r['how']}")
    c = d["counts"]
    print(f"\n  works {c['works']} · no {c['no']} · unproven {c['unproven']}")
    print(f"  {d['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
