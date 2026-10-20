#!/usr/bin/env python3
"""test_secrets_gate.py — the clearance gate on the ONE value path. DRIVEN, not counted.

★★★ WHAT THIS GUARDS. `khaoss-secrets resolve-for-inject` used to emit a secret VALUE for any process
    running as this user, consulting NO clearance; file mode 0600 was the only control. The estate's
    own installer said so in the operator's words. This suite holds the replacement property:

        a value is emitted ONLY to a caller the clearance engine allows,
        every other request REFUSES, and a refusal NEVER writes to the value channel.

★★ HERMETIC BY CONSTRUCTION. Every case runs with `HOME` pointed at a throwaway directory containing
   a SYNTHETIC secret invented by this file. **No real secret is read, listed or opened** — not even
   to test. `shaman_ledger` and `secret_meta` both resolve their paths through `$HOME`, so redirecting
   HOME moves the store, the registry and the access ledger together.
   The synthetic value is `SYNTHETIC-…`, it exists only under the temp dir, and the temp dir is
   removed at the end. Nothing is written into `~/.khaoss-secrets`.

★ THE PROVE-FAIL MUTATES **COPIES**, NEVER THE SHIPPED FILES. A security gate is the worst possible
  thing to leave half-restored, so the mutation harness copies the repo to a temp dir, breaks the copy,
  and requires THIS suite to go red there — naming the guard that was supposed to catch it. The shipped
  tree is not written to at all, which is a stronger guarantee than mutating-and-restoring.

Usage:
  python3 operator/test_secrets_gate.py            # the suite
  python3 operator/test_secrets_gate.py --prove-fail   # break it four ways; require RED each time
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(os.environ.get("KHAOSS_SECRETS_REPO") or Path(__file__).resolve().parent.parent).resolve()
CLI = REPO / "khaoss-secrets"

VALUE = "SYNTHETIC-LANE-FIXTURE-NOT-A-REAL-SECRET-0001"
UND_VALUE = "SYNTHETIC-LANE-UNDESCRIBED-0002"
SECRET = "lane-fixture-secret"
UNDESCRIBED = "lane-fixture-undescribed"

# The axes this suite DELIBERATELY drives. The ledger must record exactly this set — no more, no less.
EXPECTED_AXES = {"no-caller", "rank", "territory", "undescribed", "engine-unavailable", "engine",
                 "consumer"}

_fails: list[str] = []
_passes: list[str] = []


def chk(label: str, ok: bool, detail: str = "") -> bool:
    (_passes if ok else _fails).append(label + (f"  [{detail}]" if detail and not ok else ""))
    print(f"  {'✓' if ok else '✗'} {label}" + (f"   {detail}" if detail and not ok else ""))
    return ok


# ──────────────────────────────────────────────────────────────────────────────────────────────
#  THE FIXTURE — a throwaway HOME. Nothing here is real.
# ──────────────────────────────────────────────────────────────────────────────────────────────
ACCESS = {
    "_shaman": {
        "ranks": {"Initiate": 0, "Seeker": 1, "Adept": 2, "Oracle": 3, "Sage": 4},
        "zones": {"stack": ["~/.khaoss-secrets"], "knowledge": ["~/vault"]},
        "groups": {},
    },
    "surfaces": [],
    "agents": {
        # rank Oracle + territory stack  → ALLOWED
        "fixture-reader": {"rank": "Oracle",
                           "capabilities": [{"tool": "fs", "op": "read", "params_scope": "",
                                             "territory": ["stack"]}]},
        # rank Initiate                  → REFUSED on rank
        "fixture-lowy": {"rank": "Initiate",
                         "capabilities": [{"tool": "fs", "op": "read", "params_scope": "",
                                           "territory": ["stack"]}]},
        # rank Oracle but wrong territory → REFUSED on territory
        "fixture-outsider": {"rank": "Oracle",
                             "capabilities": [{"tool": "fs", "op": "read", "params_scope": "",
                                               "territory": ["knowledge"]}]},
    },
}


def build_home(broken_ledger: bool = False) -> Path:
    home = Path(tempfile.mkdtemp(prefix="secrets-gate-fx-"))
    store = home / ".khaoss-secrets"
    store.mkdir(parents=True)
    store.chmod(0o700)
    for name, val in ((SECRET, VALUE), (UNDESCRIBED, UND_VALUE)):
        p = store / name
        p.write_text(val)
        p.chmod(0o600)
    # the clearance rows. `lane-fixture-secret` IS described; `lane-fixture-undescribed` deliberately
    # has no row — the "nobody said" case, which must never resolve to "everybody".
    (store / ".meta.json").write_text(json.dumps({
        SECRET: {"rank_required": "Oracle", "territory": "stack", "ring": 0,
                 "owner": "lane-secrets", "kind": "synthetic-fixture"}
    }))
    (store / ".meta.json").chmod(0o600)

    cfg = home / ".config" / "AionUi-Dev"
    cfg.mkdir(parents=True)
    (cfg / "khaoss-secrets.json").write_text(json.dumps({"secrets": {
        SECRET: {"consumer": ["fixture-reader"], "backend": "key_file",
                 "ref": f"~/.khaoss-secrets/{SECRET}", "spend_cap_note": "", "created": "2026-10-12",
                 "last_rotated": None, "exemption": None},
        UNDESCRIBED: {"consumer": ["fixture-reader"], "backend": "key_file",
                      "ref": f"~/.khaoss-secrets/{UNDESCRIBED}", "spend_cap_note": "",
                      "created": "2026-10-12", "last_rotated": None, "exemption": None},
        # consumer DECLARED and the caller is NOT on it → the second, narrower axis
        "lane-fixture-consumed": {"consumer": ["someone-else"], "backend": "key_file",
                                  "ref": f"~/.khaoss-secrets/{SECRET}", "spend_cap_note": "",
                                  "created": "2026-10-12", "last_rotated": None, "exemption": None},
    }}))
    if broken_ledger:
        (cfg / "khaoss-access.json").write_text("{ this is not json")
    else:
        (cfg / "khaoss-access.json").write_text(json.dumps(ACCESS))
    return home


def run(home: Path, *args: str, repo: Path | None = None) -> tuple:
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run([sys.executable, str((repo or REPO) / "khaoss-secrets"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, r.stdout, r.stderr


def audit_rows(home: Path) -> list:
    p = home / ".local" / "state" / "khaoss" / "secrets_resolve_audit.jsonl"
    if not p.is_file():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


# ──────────────────────────────────────────────────────────────────────────────────────────────
#  THE SUITE
# ──────────────────────────────────────────────────────────────────────────────────────────────
def suite() -> int:
    print("═" * 100)
    print("  THE DECISION POINT — clearance gate on the one value path")
    print(f"  repo: {REPO}")
    print("═" * 100)
    # ★★★ HERMETICITY IS ASSERTED, NOT ASSUMED (added after this suite leaked once). `~` is taken
    #     from the PASSWD DATABASE, not `$HOME`, so this still finds the real home when the mutation
    #     harness has redirected HOME. What is compared is the ALLOWED rows — see G15 for why a raw
    #     line count is the wrong instrument here.
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    real_ledger = real_home / ".local" / "state" / "khaoss" / "secrets_resolve_audit.jsonl"
    try:
        real_rows_before = ([json.loads(l) for l in real_ledger.read_text().splitlines() if l.strip()]
                            if real_ledger.is_file() else [])
    except Exception:  # noqa: BLE001 — an unreadable ledger must not stop the suite; G15 will say so
        real_rows_before = []
    home = build_home()
    try:
        # ── G1 · ALLOW: the engine permits, so the value IS returned, byte for byte ──
        rc, out, err = run(home, "resolve-for-inject", SECRET, "--as", "fixture-reader")
        chk("G1 the engine ALLOWS a declared, in-rank, in-territory caller → the VALUE is returned",
            rc == 0 and out == VALUE, f"rc={rc} out={out!r} err={err[:120]!r}")

        # ── G2..G6 · REFUSE, each naming its axis, each leaving stdout EMPTY ──
        refuses = [
            ("G2 an UNDECLARED caller is refused", ("resolve-for-inject", SECRET), "no-caller"),
            ("G3 a caller BELOW the required rank is refused",
             ("resolve-for-inject", SECRET, "--as", "fixture-lowy"), "rank"),
            ("G4 a caller OUTSIDE the territory is refused",
             ("resolve-for-inject", SECRET, "--as", "fixture-outsider"), "territory"),
            ("G5 an agent NOT IN THE LEDGER is refused",
             ("resolve-for-inject", SECRET, "--as", "nobody-at-all"), "rank"),
            ("G6 an UNDESCRIBED secret is refused ('nobody said' ≠ 'everybody')",
             ("resolve-for-inject", UNDESCRIBED, "--as", "fixture-reader"), "undescribed"),
            ("G7 a caller NOT on a DECLARED consumer list is refused",
             ("resolve-for-inject", "lane-fixture-consumed", "--as", "fixture-reader"), "consumer"),
        ]
        for label, args, axis in refuses:
            rc, out, err = run(home, *args)
            chk(label, rc == 3 and out == "" and f"(axis: {axis})" in err,
                f"rc={rc} stdout_bytes={len(out)} axis_in_stderr={'yes' if axis in err else 'NO'}")
            # ★ THE VALUE CHANNEL IS THE POINT. A refusal that writes a sentence to STDOUT would be
            #   injected as the secret by any caller doing $(khaoss-secrets resolve-for-inject …).
            chk(f"   ↳ and it wrote NOTHING to stdout ({axis})", out == "",
                f"stdout={out[:60]!r}")

        # ── G8 · ENGINE UNAVAILABLE → REFUSE, and NO fall-through to 0600 ──
        #     ★★ THE NEGATIVE THAT MATTERS. The fixture file is still mode 0600 and still readable;
        #        only the engine is gone. A gate that failed open here would still print the value.
        broken = Path(tempfile.mkdtemp(prefix="secrets-gate-broken-"))
        try:
            (broken / "operator").mkdir()
            (broken / "jarvis").mkdir()
            shutil.copy2(CLI, broken / "khaoss-secrets")
            for f in ("secret_meta.py", "secrets_authority_apply.py"):
                src = REPO / "operator" / f
                if src.is_file():
                    shutil.copy2(src, broken / "operator" / f)
            for f in (REPO / "shaman_ledger.py", REPO / "jarvis" / "launch_activity.py"):
                shutil.copy2(f, broken / f.name)
            # ★ REMOVE THE ENGINE. mode is untouched: the file below is still 0600 and readable.
            (broken / "operator" / "secret_meta.py").unlink()
            mode = stat.S_IMODE((home / ".khaoss-secrets" / SECRET).stat().st_mode)
            rc, out, err = run(home, "resolve-for-inject", SECRET, "--as", "fixture-reader",
                               repo=broken)
            chk("G8 ★★ with the ENGINE UNAVAILABLE the value path REFUSES (no 0600 fall-through)",
                rc == 3 and out == "" and "engine" in err.lower(),
                f"rc={rc} stdout_bytes={len(out)} mode_was={oct(mode)}")
            chk("   ↳ and the refusal NAMES the engine as the missing thing",
                "could not be imported" in err, err[:160])
        finally:
            shutil.rmtree(broken, ignore_errors=True)

        # ── G9 · the engine is importable but the LEDGER is unreadable → REFUSE ──
        h2 = build_home(broken_ledger=True)
        try:
            rc, out, err = run(h2, "resolve-for-inject", SECRET, "--as", "fixture-reader")
            chk("G9 a CORRUPT ledger refuses rather than reading as 'no restriction'",
                rc == 3 and out == "" and "(axis: engine)" in err,
                f"rc={rc} stdout_bytes={len(out)}")
        finally:
            shutil.rmtree(h2, ignore_errors=True)

        # ── G10 · 0600 is still enforced — it is now the SECOND control, not the only one ──
        h3 = build_home()
        try:
            f = h3 / ".khaoss-secrets" / SECRET
            f.chmod(0o644)
            rc, out, err = run(h3, "resolve-for-inject", SECRET, "--as", "fixture-reader")
            chk("G10 the pre-existing 0600 control still bites (defence in depth, not a replacement)",
                rc != 0 and out == "" and "0600" in err, f"rc={rc} err={err[:80]!r}")
        finally:
            shutil.rmtree(h3, ignore_errors=True)

        # ── G11 · THE LEDGER: set equality over the axes, and NO VALUE IN IT ──
        rows = audit_rows(home)
        recorded = {r.get("axis") for r in rows if r.get("outcome") == "refused"}
        # ★ THE HOME IS SHARED WITH THE ENGINE-UNAVAILABLE CASE (G8), so its refusal is in this
        #   ledger too. The corrupt-ledger case (G9) runs under its OWN home and is checked there.
        drove = {a for _, _, a in refuses} | {"no-caller", "engine-unavailable"}
        chk("G11 ★ SET EQUALITY — the axes recorded equal the axes this suite drove",
            recorded == drove, f"recorded={sorted(recorded)} drove={sorted(drove)}")
        chk("   ↳ and the suite's own axis vocabulary is exactly the one it claims to cover",
            drove <= EXPECTED_AXES, f"unexpected={sorted(drove - EXPECTED_AXES)}")
        allowed = [(r.get("secret"), r.get("caller")) for r in rows if r.get("outcome") == "allowed"]
        chk("   ↳ and EXACTLY ONE row is an allowed read, by the one caller that should have it",
            allowed == [(SECRET, "fixture-reader")], f"allowed={allowed}")
        blob = (home / ".local" / "state" / "khaoss" / "secrets_resolve_audit.jsonl").read_text()
        chk("   ↳ NEGATIVE: the ledger holds NEITHER synthetic value",
            VALUE not in blob and UND_VALUE not in blob, "a value reached the ledger")
        chk("   ↳ and the ledger is mode 0600",
            stat.S_IMODE((home / ".local" / "state" / "khaoss"
                          / "secrets_resolve_audit.jsonl").stat().st_mode) == 0o600)
        chk("   ↳ and an ALLOWED row carries a FINGERPRINT, never a value",
            all(r.get("fingerprint", "").startswith("sha256:") for r in rows
                if r.get("outcome") == "allowed"))

        # ── G12 · the refusal object is the ESTATE'S shape, not a second vocabulary ──
        rc, out, err = run(home, "resolve-for-inject", SECRET, "--as", "fixture-lowy")
        nc = None
        for line in err.splitlines():
            if line.startswith("[resolve] needs_capability: "):
                nc = json.loads(line.split(": ", 1)[1])
        keys = {"id", "act", "state", "in_catalogue", "granted", "label", "propose", "ttl_minutes"}
        chk("G12 a refusal carries the ESTATE'S needs_capability object (set-equal key set)",
            isinstance(nc, dict) and set(nc) == keys, f"keys={sorted(nc) if nc else None}")
        chk("   ↳ state is one of the two the contract allows, and propose is null when nothing exists",
            bool(nc) and nc.get("state") in ("not-granted", "does-not-exist")
            and (nc.get("propose") is None or nc.get("state") == "not-granted"),
            f"state={nc.get('state') if nc else None}")
        chk("   ↳ and the label is the ENGINE'S own sentence, naming both ranks",
            bool(nc) and "Initiate" in str(nc.get("label")) and "Oracle" in str(nc.get("label")),
            str(nc.get("label") if nc else "")[:120])

        # ── G13 · the enforcement detector SEES this value path (the blind spot) ──
        # ★★ HOME IS FORCED TO THE FIXTURE HERE, AND THAT IS A SAFETY FIX, NOT TIDINESS.
        #    `enforcement_status()` DRIVES the CLI (`value_path_probe`), and this check used to run it
        #    with the inherited real HOME. Under the prove-fail that meant a MUTATED CLI — one with
        #    the gate deliberately removed — was pointed at the REAL store, and it did what a mutated
        #    gate does: it emitted a real value and recorded an `allowed` row. The value never
        #    reached a screen or a file, but the read happened, and a lane whose whole subject is a
        #    gate must not be the thing that walks around it. With HOME pinned, the mutation resolves
        #    the SYNTHETIC fixture instead. The grep roots inside the detector stay absolute, so the
        #    blind-spot mutation this check exists to catch still bites exactly as before.
        p = subprocess.run(
            [sys.executable, "-c",
             "import sys;sys.path.insert(0,'operator');import secret_meta,json;"
             "d=secret_meta.enforcement_status();"
             "print(json.dumps({'readers':d['readers_calling_may_read'],"
             "'gate':d['value_path']}))"],
            capture_output=True, text=True, cwd=str(REPO), timeout=180,
            env=dict(os.environ, HOME=str(home)))
        try:
            d = json.loads((p.stdout or "{}").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            d = {}
        readers = [r for r in (d.get("readers") or [])]
        chk("G13 ★ the reader scan COUNTS the value path (it has no .py extension — that was the bug)",
            any(r.endswith("khaoss-secrets") for r in readers), f"readers={readers}")
        chk("   ↳ and no verification file is miscounted as a runtime reader",
            all("_assert.py" not in r and "test_" not in r for r in readers), f"readers={readers}")

        # ── G14 · metadata subcommands did NOT become value paths ──
        rc, out, err = run(home, "list")
        chk("G14 `list` still emits metadata only — neither synthetic value appears",
            rc == 0 and VALUE not in out and UND_VALUE not in out, f"rc={rc}")

        # ── G16 · ★★★ THE PROBE MUST BE SERVABLE — a truthful row nobody can load is not a row ──
        # ★ WHY THIS GUARD EXISTS. `probe_secrets_keeper` is called on EVERY `/operator/setup/walk`
        #   request. The first version of it called `enforcement_status()` with the repo-wide scan on,
        #   which walks 131,901 files and cost **14.65 s** — and it took the whole installer route
        #   down. The operator could not read the row that was supposed to tell him the truth about
        #   the gate, because the probe that made it truthful was too slow to serve. A gate is not
        #   delivered by a page that times out.
        # ★ AND THE OBVIOUS EXPLANATION WAS WRONG, which is why the bound is on the WHOLE probe and
        #   not on the spawn: the two `khaoss-secrets` process spawns cost 0.12 s. The grep was the
        #   entire cost. A guard written against the guess would have measured the wrong thing.
        # ★ THE BOUND IS GENEROUS ON PURPOSE (1.5 s against a measured 0.16 s) — this box runs at load
        #   8-14 with other lanes live, and a timing guard that flakes gets muted, which is worse than
        #   no guard. It catches the 100x regression class, not jitter.
        # ★ RUN IN A SUBPROCESS WITH HOME PINNED TO THE FIXTURE, like every other check here — so the
        #   probe drives the SYNTHETIC store and cannot wander onto the real one.
        p2 = subprocess.run(
            [sys.executable, "-c",
             "import json,sys,time;sys.path.insert(0,'operator');import setup_blockers as sb;"
             "t=time.time();s,e=sb.probe_secrets_keeper();print(json.dumps({'cost':time.time()-t,"
             "'state':s,'ev':e}))"],
            capture_output=True, text=True, cwd=str(REPO), timeout=120,
            env=dict(os.environ, HOME=str(home)))
        try:
            g = json.loads((p2.stdout or "{}").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001
            g = {}
        cost = float(g.get("cost") or 99.0)
        chk("G16 ★★★ the step-2 probe is SERVABLE (costs < 1.5 s, or the walk route dies)",
            cost < 1.5, f"probe_secrets_keeper took {cost:.2f}s — the walk renders on every request")
        chk("   ↳ and it still decides the row by DRIVING the gate",
            g.get("state") == "DONE" and "REFUSED an undeclared caller" in str(g.get("ev")),
            f"state={g.get('state')} ev={str(g.get('ev'))[:120]}")
        chk("   ↳ ★ and it does NOT print '0 readers' when the scan was skipped",
            "readers consulting the clearance engine: 0" not in str(g.get("ev")),
            "a skipped scan was reported as zero — the exact false-negative this lane fixed")

        # ── G15 · ★★★ HERMETICITY: this run did NOT leak into the real store ──
        # ★★ WHAT THIS ASSERTS, AND WHY IT IS NOT "THE FILE DID NOT GROW". The first version of this
        #    guard compared raw line COUNTS and went red — but 30 seconds of IDLE time also added
        #    three rows, because the setup WALK itself drives the value path on every render and this
        #    box has other lanes on it. A guard that reddens on another process's legitimate refusal
        #    is a false positive, and a false positive in a security suite is how a real one gets
        #    ignored. So the assertion is what is actually ATTRIBUTABLE to this suite:
        #      · an ALLOWED read of a real secret must not appear  ← the signature of the leak I made
        #      · no FIXTURE id may ever appear in the real ledger  ← the suite's own fingerprints
        def _real_rows() -> list:
            if not real_ledger.is_file():
                return []
            return [json.loads(l) for l in real_ledger.read_text().splitlines() if l.strip()]

        real_now = _real_rows()
        allowed_before = [r for r in real_rows_before if r.get("outcome") == "allowed"]
        allowed_after = [r for r in real_now if r.get("outcome") == "allowed"]
        chk("G15 ★★★ HERMETIC — no ALLOWED read of a REAL secret appeared during this run",
            len(allowed_after) == len(allowed_before),
            f"real allowed rows {len(allowed_before)} -> {len(allowed_after)}: a real value was read")
        chk("   ↳ and no FIXTURE id ever reached the real ledger (the synthetic store stayed synthetic)",
            not any("lane-fixture" in str(r.get("secret", "")) for r in real_now),
            "a fixture id leaked into the real audit ledger")
    finally:
        shutil.rmtree(home, ignore_errors=True)

    print()
    print(f"  {len(_passes)} passed · {len(_fails)} FAILED")
    for f in _fails:
        print(f"    ✗ {f}")
    return 1 if _fails else 0


# ──────────────────────────────────────────────────────────────────────────────────────────────
#  PROVE-FAIL — break the guarded thing on a COPY and require THIS suite to go red
# ──────────────────────────────────────────────────────────────────────────────────────────────
MUTATIONS = [
    ("M1 the gate is removed from resolve-for-inject (the original defect restored)",
     "gate_removed",
     ["G2 an UNDECLARED caller is refused", "G3 a caller BELOW the required rank is refused",
      "G4 a caller OUTSIDE the territory is refused", "G5 an agent NOT IN THE LEDGER is refused",
      "G6 an UNDESCRIBED secret is refused"],
     "G2"),
    ("M2 the ENGINE-UNAVAILABLE branch falls through to the file-mode check",
     "engine_falls_through",
     ["G8 ★★ with the ENGINE UNAVAILABLE the value path REFUSES (no 0600 fall-through)"],
     "G8"),
    ("M3 a refusal is written to STDOUT (the value channel)",
     "refuse_to_stdout",
     ["G2 an UNDECLARED caller is refused", "G3 a caller BELOW the required rank is refused"],
     "G2"),
    ("M4 the reader scan goes back to `--include=*.py` (the blind spot)",
     "blind_spot",
     ["G13 ★ the reader scan COUNTS the value path (it has no .py extension — that was the bug)"],
     "G13"),
    ("M5 the step-2 probe goes back to the 14 s repo-wide scan (takes the walk route down)",
     "slow_probe",
     ["G16 ★★★ the step-2 probe is SERVABLE (costs < 1.5 s, or the walk route dies)"],
     "G16"),
]


def _mutate(root: Path, how: str) -> None:
    """Apply one mutation to a COPY. ★ RAISES IF THE MUTATION DID NOT LAND.

    ★ WHY THAT RAISE MATTERS. `str.replace` that matches nothing is a silent no-op, and a no-op
      mutation makes the suite go GREEN — which the harness would then report as "the guard did not
      bite". Three lanes today reported reds that were actually crashes, and the inverse error is
      worse: a mutation that never applied looks exactly like a guard that works. So the file must
      CHANGE, or this is infrastructure failure and not a result."""
    cli = root / "khaoss-secrets"
    sm = root / "operator" / "secret_meta.py"

    def _sub(path: Path, old: str, new: str) -> None:
        before = path.read_text()
        after = before.replace(old, new)
        if after == before:
            raise RuntimeError(f"MUTATION DID NOT APPLY to {path.name}: pattern not found: {old[:60]!r}")
        path.write_text(after)

    if how == "gate_removed":
        _sub(cli, "    path, verdict, declared = _gate(a.name, rec, a.caller)",
             "    path, verdict, declared = _ref_path(rec), {'ring': None, "
             "'rank_required': None, 'territory': None}, False  # MUTATED")
    elif how == "engine_falls_through":
        _sub(cli, '        _refuse(f"{why}. Refused, because could-not-look is not permission — this path does NOT "\n'
                  '                f"fall back to file mode alone.",',
             '        return path, {"ring": None, "rank_required": None, "territory": None}, False  # MUTATED')
    elif how == "refuse_to_stdout":
        _sub(cli, '    sys.stderr.write(f"[resolve] REFUSED — {reason} (axis: {axis})\\n")',
             '    sys.stdout.write(f"[resolve] REFUSED — {reason} (axis: {axis})\\n")  # MUTATED')
    elif how == "blind_spot":
        _sub(sm, 'subprocess.run(["grep", "-rl", "secret_meta", r]',
             'subprocess.run(["grep", "-rl", "--include=*.py", "secret_meta", r]')
    elif how == "slow_probe":
        _sub(root / "operator" / "setup_blockers.py",
             "enf = secret_meta.enforcement_status(scan=False)",
             "enf = secret_meta.enforcement_status()  # MUTATED")
    else:
        raise RuntimeError(f"unknown mutation {how!r}")


def prove_fail() -> int:
    print("═" * 100)
    print("  PROVE-FAIL — break the guarded thing on a COPY; require THIS suite to go RED")
    print("  (the shipped tree is never written — copies only, which is stronger than mutate-and-restore)")
    print("═" * 100)
    bad = 0
    for label, how, must_fail, expected_biter in MUTATIONS:
        tmp = Path(tempfile.mkdtemp(prefix="secrets-gate-mut-"))
        try:
            (tmp / "operator").mkdir()
            (tmp / "jarvis").mkdir()
            shutil.copy2(CLI, tmp / "khaoss-secrets")
            for f in ("secret_meta.py", "secrets_authority_apply.py", "test_secrets_gate.py",
                      "setup_blockers.py"):
                src = REPO / "operator" / f
                if src.is_file():
                    shutil.copy2(src, tmp / "operator" / f)
            shutil.copy2(REPO / "shaman_ledger.py", tmp / "shaman_ledger.py")
            shutil.copy2(REPO / "jarvis" / "launch_activity.py", tmp / "jarvis" / "launch_activity.py")
            _mutate(tmp, how)
            # ★★ THE MUTATED SUITE RUNS UNDER A SCRATCH HOME. This is the second lock on the same
            #    door: the copied suite already pins HOME for every CLI it drives, and this makes the
            #    INHERITED home scratch too, so a mutated CLI cannot reach the real store from any
            #    code path — including one added later by someone who has not read this comment.
            scratch = Path(tempfile.mkdtemp(prefix="secrets-gate-scratch-"))
            env = dict(os.environ, KHAOSS_SECRETS_REPO=str(tmp), HOME=str(scratch))
            try:
                r = subprocess.run([sys.executable, str(tmp / "operator" / "test_secrets_gate.py")],
                                   capture_output=True, text=True, env=env, timeout=900)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
            red = r.returncode != 0
            tail = r.stdout[-4000:]
            # ★ ASSERT THE **EXPECTED** GUARD IS THE ONE THAT BIT. A mutation that reddens an
            #   unrelated check (or dies on infrastructure) is not a red — three lanes reported
            #   crash-reds today and were corrected. So the named guard must appear in the failures.
            named = [g for g in must_fail if g in tail and "✗" in tail]
            infrastructure = ("Traceback (most recent call last)" in tail
                              and "SyntaxError" in tail)
            ok = red and named and not infrastructure
            bad += (not ok)
            print(f"  {'RED ' if ok else '★NOT-RED'} {label}")
            print(f"          exit={r.returncode}  crash={infrastructure}  "
                  f"biting guard present: {expected_biter in tail}")
            if not ok:
                print("          ---- tail ----")
                print("\n".join("          " + l for l in tail.splitlines()[-14:]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print()
    print(f"  {'✓ VALID' if not bad else '✗ ' + str(bad) + ' MUTATION(S) DID NOT REDDEN THE NAMED GUARD'}"
          f" — {len(MUTATIONS) - bad}/{len(MUTATIONS)} mutated the right thing")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--prove-fail" in sys.argv:
        sys.exit(prove_fail())
    sys.exit(suite())
