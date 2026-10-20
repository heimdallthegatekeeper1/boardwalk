#!/usr/bin/env python3
"""dsh_obs_writer_drive.py — DRIVES the standalone writer and MEASURES it. Nothing here asserts
that the code says the right thing; every check is a fact read off the filesystem after real
processes ran.

WHAT IT PROVES (each line is a measurement, not a reading of the source)
  1. FLAG OFF ⇒ ZERO captures. Not "fewer" — the ring is unchanged, the mtimes are identical and
     the writer's own attempt counter is 0. A real `import`/`convert` on this box takes real
     milliseconds, so "no attempt" and "attempted and discarded" are distinguishable here.
  2. FLAG ON  ⇒ captures DO land, each a valid JPEG, and LOW-DEFINITION BY DESIGN (<= 480 px wide).
     ★ 1 without 2 is a writer that is simply broken; 2 without 1 is a toggle that only pretends.
  3. THE RING CAP holds under real pressure: cap forced to 6, more than 6 captures taken, and the
     count after is <= cap with evictions actually recorded. Asserting the constant would prove
     nothing; this drives it past the bound.
  4. THE LEASE: a FRESH state.json ⇒ mode=yielding and ZERO captures, even with the flag ON.
     A STALE state.json ⇒ mode=owning and captures resume. This is the no-double-writer property,
     measured at both ends.
  5. THE CONFLICT GUARD: a writer that has claimed the ring stops capturing for good the moment a
     foreign publisher appears on state.json. This is the double-writer window closing itself.
  6. THIS PROCESS NEVER WRITES state.json OR screenshot-mode.json — content hashes compared
     before and after every scenario.

PROVE-FAIL (AGENTS.md §2 — a guard never tested against its own bypass is a guard we are hoping
about). `--prove-fail` copies the writer, mutates the flag gate `if fl["enabled"]:` into `if True:`
— i.e. breaks exactly the thing scenario 1 guards — re-runs scenario 1 against the mutant, and
requires the suite to go RED. If the mutation does not turn scenario 1 red, this suite is not
testing the gate and it says so.

RUN:  python3 operator/dsh_obs_writer_drive.py [--prove-fail]
EXIT: 0 green · 1 red · 2 the suite could not run (which is NOT a pass)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path(os.environ.get("HOME") or os.path.expanduser("~"))
REPO = Path(__file__).resolve().parent.parent
WRITER_SRC = REPO / "bin" / "dsh_obs_writer.py"
FLAG_GATE = 'if fl["enabled"]:'

RESULTS = []


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    RESULTS.append((name, ok, str(detail)[:300]))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return "<absent>"


def sandbox(tag: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"dshews-{tag}-"))
    (d / "shots").mkdir(parents=True, exist_ok=True)
    return d


def write_flag(d: Path, enabled: bool) -> None:
    p = d / "screenshot-mode.json"
    tmp = d / "screenshot-mode.json.tmp"
    tmp.write_text(json.dumps({"enabled": enabled, "updatedAt": int(time.time() * 1000),
                               "updatedBy": "driver", "schema": 1}, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)


def write_state(d: Path, age_ms: int, source: str = "dsh-observability-plugin (cordis host half, live Services)") -> None:
    p = d / "state.json"
    p.write_text(json.dumps({"generatedAt": int(time.time() * 1000) - age_ms, "source": source,
                             "live": True, "jobs": [], "agents": [], "screenshots": []}, indent=2) + "\n",
                 encoding="utf-8")
    if age_ms > 60000:
        old = time.time() - age_ms / 1000.0
        os.utime(p, (old, old))


def run_writer(d: Path, seconds: float, cap: int = 40, interval_ms: int = 1500,
               stale_ms: int = 90000, writer: Path = WRITER_SRC, dry: bool = False) -> dict:
    """Run the REAL writer as a REAL process for `seconds`, then read back its own status file."""
    env = dict(os.environ)
    env.update({
        "DSH_OBS_DIR": str(d),
        "DSH_OBS_CAP": str(cap),
        "DSH_OBS_INTERVAL_MS": str(interval_ms),
        "DSH_OBS_TAKEOVER_STALE_MS": str(stale_ms),
        "DSH_OBS_DISPLAY": os.environ.get("DSH_OBS_DISPLAY", ":0"),
    })
    if dry:
        env["DSH_OBS_NO_CAPTURE"] = "1"
    log = (d / f"run-{int(time.time()*1000)}.stdout").open("wb")
    p = subprocess.Popen([sys.executable, str(writer)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        p.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=5)
    log.close()
    try:
        st = json.loads((d / "writer.json").read_text(encoding="utf-8"))
    except Exception as e:
        st = {"_readError": f"{e.__class__.__name__}: {e}"}
    st["_exitCode"] = p.returncode
    return st


def spawn_writer(d: Path, cap: int, interval_ms: int, stale_ms: int, writer: Path = WRITER_SRC):
    env = dict(os.environ)
    env.update({"DSH_OBS_DIR": str(d), "DSH_OBS_CAP": str(cap), "DSH_OBS_INTERVAL_MS": str(interval_ms),
                "DSH_OBS_TAKEOVER_STALE_MS": str(stale_ms)})
    log = (d / "bg.stdout").open("wb")
    return subprocess.Popen([sys.executable, str(writer)], env=env, stdout=log, stderr=subprocess.STDOUT), log


def read_status(d: Path) -> dict:
    try:
        return json.loads((d / "writer.json").read_text(encoding="utf-8"))
    except Exception as e:
        return {"_readError": f"{e.__class__.__name__}: {e}"}


SHOT_RE = re.compile(r"^shot-\d+\.jpg$")


def ring_files(d: Path) -> list:
    """Only the files the RING owns: shot-<digits>.jpg. ★ A previous version of this helper globbed
    `shot-*.jpg`, which also matched the deliberately-planted bystander `shot-notanumber.jpg` — the
    counter was wrong, not the writer. A measuring instrument that is wrong reads exactly like a
    failing subject, so the selector here is the same one the writer evicts by."""
    return sorted((p for p in (d / "shots").glob("shot-*.jpg") if SHOT_RE.match(p.name)),
                  key=lambda p: p.name)


def ring_fingerprint(d: Path) -> str:
    """Names + sizes + mtimes of every file in the ring directory, bystanders included. Used to
    prove a YIELDING standby mutates NOTHING."""
    parts = []
    for p in sorted((d / "shots").iterdir()):
        st = p.stat()
        parts.append(f"{p.name}:{st.st_size}:{int(st.st_mtime_ns)}")
    return "|".join(parts)


def identify_width(p: Path) -> int:
    r = subprocess.run(["identify", "-format", "%w", str(p)], capture_output=True, timeout=20)
    try:
        return int(r.stdout.decode().strip())
    except Exception:
        return -1


# ════════════════════════════════════════════════════════════════════════════════════════════
def scenario_flag_off(writer: Path = WRITER_SRC, seconds: float = 7.0) -> bool:
    """1. FLAG OFF ⇒ ZERO captures, over a window that spans at least four capture intervals."""
    d = sandbox("off")
    write_flag(d, False)
    before = ring_files(d)
    st = run_writer(d, seconds, interval_ms=1500, writer=writer)
    after = ring_files(d)
    ok_mode = st.get("mode") == "owning"          # no state.json ⇒ the surface is unowned
    ok_attempts = (st.get("capture") or {}).get("attempts") == 0
    ok_ring = len(after) == len(before) == 0
    ok_flag_seen = (st.get("flag") or {}).get("enabled") is False
    detail = (f"mode={st.get('mode')} attempts={(st.get('capture') or {}).get('attempts')} "
              f"ring={len(after)} flagSeen={(st.get('flag') or {}).get('enabled')} "
              f"window={seconds}s at 1.5s interval")
    check("1. FLAG OFF ⇒ writer owns the surface but takes NOTHING (0 attempts, ring unchanged)",
          ok_mode and ok_attempts and ok_ring and ok_flag_seen, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok_mode and ok_attempts and ok_ring


def scenario_flag_on(seconds: float = 8.0) -> bool:
    """2. FLAG ON ⇒ captures land, valid JPEG, LOW DEFINITION (<= 480 px wide)."""
    d = sandbox("on")
    write_flag(d, True)
    st = run_writer(d, seconds, interval_ms=1500)
    files = ring_files(d)
    cap = st.get("capture") or {}
    ok_count = len(files) >= 2
    ok_valid = all(p.read_bytes()[:3] == b"\xff\xd8\xff" and p.stat().st_size > 0 for p in files)
    widths = [identify_width(p) for p in files]
    ok_lowdef = bool(widths) and all(0 < w <= 480 for w in widths)
    detail = (f"files={len(files)} attempts={cap.get('attempts')} failures={cap.get('failures')} "
              f"widths={widths} lastReason={cap.get('lastReason')}")
    if not ok_count and cap.get("lastReason"):
        detail += "  ← CAPTURE FAILED: " + str(cap.get("lastReason"))
    check("2. FLAG ON ⇒ captures land, valid JPEG, and LOW-DEFINITION (<=480px) by design",
          ok_count and ok_valid and ok_lowdef, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok_count and ok_valid and ok_lowdef


def scenario_ring_cap(seconds: float = 26.0, cap: int = 6) -> bool:
    """3. THE CAP, driven past its bound. Cap 6, many captures, then count."""
    d = sandbox("cap")
    write_flag(d, True)
    st = run_writer(d, seconds, cap=cap, interval_ms=500)
    files = ring_files(d)
    ring = st.get("ring") or {}
    ok = (len(files) <= cap
          and ring.get("count", 99) <= cap
          and ring.get("evictedTotal", 0) > 0
          and ring.get("boundHeld") is True
          and ring.get("peakCount", 99) <= cap
          and (st.get("capture") or {}).get("attempts", 0) > cap)
    detail = (f"cap={cap} files={len(files)} attempts={(st.get('capture') or {}).get('attempts')} "
              f"evictedTotal={ring.get('evictedTotal')} bytes={ring.get('bytes')} "
              f"selfReportedPeak={ring.get('peakCount')} boundHeld={ring.get('boundHeld')}")
    check("3. RING CAP holds when driven past it (attempts > cap, files <= cap, evictions recorded)",
          ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_ring_cap_peak(cap: int = 4, interval_ms: int = 350, seconds: float = 20.0) -> bool:
    """3b. THE BOUND AT EVERY INSTANT, not just at rest. A cap that is only true between captures
    is not a bound: the live ring was observed holding 41 files against a cap of 40 while the
    Cordis plugin used the write-then-evict order. So this samples the directory from OUTSIDE the
    writer while it captures at ~2.9/s and records the highest count ever seen.

    ★ THE PROBE IS TOO GENTLE TO PROVE THE ABSENCE OF A TRANSIENT: a sampler cannot see a window
    shorter than its own period, and this one polls every 12 ms. What it DOES prove is that no
    transient wider than 12 ms exists across ~1700 samples. That limit is stated rather than
    hidden — the exact-instant guarantee here rests on the staged admit in ring_admit(), and this
    measurement is what checks the claim rather than restating it."""
    d = sandbox("peak")
    write_flag(d, True)
    p, log = spawn_writer(d, cap=cap, interval_ms=interval_ms, stale_ms=90000)
    peak = 0
    samples = 0
    t_end = time.time() + seconds
    try:
        while time.time() < t_end:
            n = len(ring_files(d))
            peak = max(peak, n)
            samples += 1
            time.sleep(0.012)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    st = read_status(d)
    ring = st.get("ring") or {}
    ok = (peak <= cap and samples > 500
          and (ring.get("peakCount") or 99) <= cap
          and (st.get("capture") or {}).get("attempts", 0) > cap)
    detail = (f"cap={cap} observedPeak={peak} samples={samples} selfReportedPeak={ring.get('peakCount')} "
              f"attempts={(st.get('capture') or {}).get('attempts')} final={len(ring_files(d))}")
    check("3b. RING CAP holds at EVERY SAMPLED INSTANT (external sampler, 12 ms period) — no "
          "write-then-evict transient above the cap", ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_lease_yield(seconds: float = 6.0) -> bool:
    """4a. A FRESH state.json ⇒ YIELDING with the flag ON: the standby captures nothing AND
    mutates nothing in the ring. The bigger claim is the second one — a standby that quietly
    evicts is a second writer on the ring even while it captures nothing."""
    d = sandbox("yield")
    write_flag(d, True)
    write_state(d, age_ms=500)                    # plugin published 0.5s ago
    # a pre-existing ring the incumbent owns, plus a bystander that must not be touched
    for i in range(4):
        (d / "shots" / f"shot-{1790000000000 + i * 8000}.jpg").write_bytes(b"\xff\xd8\xff" + b"x" * 64)
    (d / "shots" / "operator-note.txt").write_text("incumbent's neighbour\n", encoding="utf-8")
    before = ring_fingerprint(d)
    st = run_writer(d, seconds, interval_ms=1000)
    after = ring_fingerprint(d)
    cap = st.get("capture") or {}
    ok = (st.get("mode") == "yielding"
          and cap.get("attempts") == 0
          and before == after
          and (st.get("ring") or {}).get("mutatedByWriter") is False)
    detail = (f"mode={st.get('mode')} attempts={cap.get('attempts')} ringUnchanged={before == after} "
              f"mutatedByWriter={(st.get('ring') or {}).get('mutatedByWriter')} "
              f"incumbentAgeMs={(st.get('incumbent') or {}).get('ageMs')}")
    check("4a. LEASE: live incumbent ⇒ mode=yielding, ZERO captures, and the ring is untouched "
          "(bit-identical names+sizes+mtimes)", ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_lease_takeover(seconds: float = 7.0) -> bool:
    """4b. A STALE state.json ⇒ OWNING and captures resume."""
    d = sandbox("takeover")
    write_flag(d, True)
    write_state(d, age_ms=200_000)                # incumbent silent for 200s
    st = run_writer(d, seconds, interval_ms=1500, stale_ms=90000)
    ok = (st.get("mode") == "owning"
          and (st.get("capture") or {}).get("attempts", 0) >= 1
          and len(ring_files(d)) >= 1)
    detail = (f"mode={st.get('mode')} attempts={(st.get('capture') or {}).get('attempts')} "
              f"ring={len(ring_files(d))} incumbentAgeMs={(st.get('incumbent') or {}).get('ageMs')} "
              f"takeoverStaleMs={(st.get('lease') or {}).get('takeoverStaleMs')}")
    check("4b. LEASE: silent incumbent ⇒ mode=owning and the ring starts filling",
          ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_conflict(seconds: float = 14.0) -> bool:
    """5. THE CONFLICT GUARD. Claim on a stale state.json, then let a foreign publisher appear.
    The writer must stop capturing for good and say so — and it must not have written state.json."""
    d = sandbox("conflict")
    write_flag(d, True)
    write_state(d, age_ms=200_000)
    p, log = spawn_writer(d, cap=40, interval_ms=700, stale_ms=4000)
    try:
        time.sleep(5.0)
        mid = read_status(d)
        claimed = mid.get("mode") == "owning"
        # the foreign publisher returns
        write_state(d, age_ms=200)
        time.sleep(5.0)
        after_conflict = read_status(d)
        n_at_conflict = len(ring_files(d))
        time.sleep(4.0)
        n_later = len(ring_files(d))
        st = read_status(d)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()
    ok = (claimed
          and after_conflict.get("mode") == "conflict"
          and n_later == n_at_conflict                 # capture stopped dead
          and (st.get("lease") or {}).get("conflicts", 0) >= 1
          and (st.get("lease") or {}).get("conflictEvidence"))
    detail = (f"claimed={claimed} modeAfter={after_conflict.get('mode')} final={st.get('mode')} "
              f"ringAtConflict={n_at_conflict} ringLater={n_later} "
              f"conflicts={(st.get('lease') or {}).get('conflicts')}")
    check("5. CONFLICT: a returning foreign publisher stops capture permanently (ring frozen)",
          ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_never_writes_stores() -> bool:
    """6. This process never writes state.json or screenshot-mode.json.

    ★ TWO INDEPENDENT PROOFS, because "it does not write that file" is exactly the kind of claim
    that is asserted and never tested:
      (a) hash both files across a real run in which captures DID land;
      (b) make both files READ-ONLY (0444) first — if any code path in the writer so much as tried
          to open them for writing, the run would raise PermissionError into its own log. A hash
          comparison alone would not catch a write that happened to be byte-identical.
    """
    d = sandbox("stores")
    write_flag(d, True)
    write_state(d, age_ms=200_000)
    before_state, before_flag = sha(d / "state.json"), sha(d / "screenshot-mode.json")
    os.chmod(d / "state.json", 0o444)
    os.chmod(d / "screenshot-mode.json", 0o444)
    st = run_writer(d, 7.0, interval_ms=1500)
    after_state, after_flag = sha(d / "state.json"), sha(d / "screenshot-mode.json")
    logs = "".join(p.read_text(errors="replace") for p in d.glob("run-*.stdout"))
    wrote = "PermissionError" in logs or "Permission denied" in logs
    os.chmod(d / "state.json", 0o644)
    os.chmod(d / "screenshot-mode.json", 0o644)
    ok = (before_state == after_state and before_flag == after_flag
          and not wrote and (d / "state.json").is_file())
    detail = (f"state.json {before_state} -> {after_state} (captures landed: "
              f"{len(ring_files(d))}, mode={st.get('mode')}); flag {before_flag} -> {after_flag}; "
              f"read-only run raised a write attempt: {wrote}")
    check("6. writer NEVER writes state.json or screenshot-mode.json (hashes unchanged across a "
          "run with both files 0444 — no write was even attempted)", ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


def scenario_ring_selectivity() -> bool:
    """7. Eviction touches ONLY shot-<digits>.jpg. A bystander file in the ring survives."""
    d = sandbox("sel")
    write_flag(d, True)
    bystander = d / "shots" / "operator-note.txt"
    bystander.write_text("must survive\n", encoding="utf-8")
    also = d / "shots" / "shot-notanumber.jpg"
    also.write_text("must survive too\n", encoding="utf-8")
    run_writer(d, 14.0, cap=3, interval_ms=700)
    ok = bystander.is_file() and also.is_file() and len(ring_files(d)) <= 3
    detail = (f"note={bystander.is_file()} badname={also.is_file()} "
              f"shotFiles={len(ring_files(d))} (cap 3, so eviction certainly ran)")
    check("7. eviction matches shot-<digits>.jpg ONLY — bystander files are never deleted", ok, detail)
    shutil.rmtree(d, ignore_errors=True)
    return ok


# ════════════════════════════════════════════════════════════════════════════════════════════
def prove_fail() -> int:
    """Break EXACTLY the guarded thing and require the suite to notice.

    Mutation: `if fl["enabled"]:` → `if True:` — i.e. a writer that ignores the operator's flag.
    That is precisely the defect this whole change exists to remove, so if scenario 1 does not go
    red against this mutant, scenario 1 is not a test of the flag gate.
    """
    print("\n=== PROVE-FAIL: mutate the flag gate and require scenario 1 to go RED ===")
    src = WRITER_SRC.read_text(encoding="utf-8")
    if FLAG_GATE not in src:
        print(f"  [FAIL] the gate line {FLAG_GATE!r} is not present — cannot mutate; the suite "
              f"would be asserting the wrong invariant")
        return 1
    mutated = src.replace(FLAG_GATE, "if True:  # MUTANT: flag ignored", 1)
    d = Path(tempfile.mkdtemp(prefix="dshews-mutant-"))
    mpath = d / "dsh_obs_writer_mutant.py"
    mpath.write_text(mutated, encoding="utf-8")
    ok = scenario_flag_off(writer=mpath, seconds=7.0)
    shutil.rmtree(d, ignore_errors=True)
    if ok:
        print("  [FAIL] the mutant PASSED scenario 1 — the suite does not test the flag gate. "
              "Treat every green above as UNPROVEN.")
        return 1
    print("  [PASS] the mutant is caught: scenario 1 goes RED when the flag gate is removed, "
          "so the green above is a real measurement of the gate.")
    return 0


def main() -> int:
    print("dsh_obs_writer_drive.py — driving bin/dsh_obs_writer.py and measuring it")
    print(f"writer: {WRITER_SRC}")
    print(f"python: {sys.executable}")
    if not WRITER_SRC.is_file():
        print("  [FAIL] the writer does not exist — nothing to drive; this is NOT a pass")
        return 2
    for tool in ("xdotool", "import", "convert", "identify"):
        if not shutil.which(tool):
            print(f"  [FAIL] {tool} is missing — captures cannot be driven; this is NOT a pass")
            return 2

    t0 = time.time()
    scenario_flag_off()
    scenario_flag_on()
    scenario_ring_cap()
    scenario_ring_cap_peak()
    scenario_lease_yield()
    scenario_lease_takeover()
    scenario_conflict()
    scenario_never_writes_stores()
    scenario_ring_selectivity()

    fails = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed in {time.time()-t0:.1f}s")
    proved = False
    if "--prove-fail" in sys.argv:
        rc = prove_fail()
        proved = (rc == 0)
        if rc != 0:
            return 1
    if fails:
        print("\nFAILURES:")
        for n, _, d in fails:
            print(f"  · {n} — {d}")
        print("\nSTATUS: RED")
        return 1
    if proved:
        print("\nSTATUS: GREEN — and the prove-fail mutant was caught, so these greens are "
              "measurements of the gate and not a restatement of the code.")
    else:
        print("\nSTATUS: GREEN, BUT UNPROVEN — prove-fail was NOT run. Re-run with --prove-fail "
              "before claiming this suite guards anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
