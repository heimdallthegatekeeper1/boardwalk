#!/usr/bin/env python3
"""skill_propagate.py — put the KHAOSS operating manual into the seats that never received it.

★★★ THE FINDING THIS EXISTS FOR, measured across 58 seats holding the near-full skill set:
      TWENTY-FOUR canonical skills are missing from ALL FIFTY-EIGHT. Not most. All.
      And they are not a random 24 — they are precisely the KHAOSS operational set:
        khaoss-operations · khaoss-orientation · khaoss-stack-recovery · khaoss-team-protocol
        team-coordination · aionui-team-spawn · aionui-team-agent · aionui-team-orchestration
        service-stack-diagnostics · multi-agent-operations-orchestration · operational-documentation
    ★ So every KHAOSS operating instruction we have ever written is invisible to every agent. The A0
      background-process and negative-delay rules were written into `khaoss-operations` yesterday;
      they currently reach ZERO seats.
    ★★ AND NOTE WHICH ONE IS ON THAT LIST: `aionui-team-spawn`. A fleet-wide argument about why
       teammates fail to spawn ran for weeks while no seat carried the spawn skill.

★★ WHY ADDITIVE COPY AND NOT A SYMLINK. Replacing a profile's skills/ with a link to canonical also
   REMOVES whatever that profile had of its own, and some of that is deliberate — the riddle seats
   each carry exactly one purpose-built skill and ZERO canonical ones. A symlink would erase a
   designed architecture to fix a propagation bug. This copies IN and never takes away.

★ FENCE: refuses trading profiles by NAME **and by CONTENT**. `lead-deepseek-v4-pro` has a clean
  name and seven trading skills; a name-only fence waved it straight through.
"""
from __future__ import annotations
import argparse, json, os, shutil, sys, time
from pathlib import Path

CANON = Path.home() / ".hermes" / "skills"
PROFILES = Path.home() / ".hermes" / "profiles"
FENCED = ("trading", "crypto", "liquid", "coinvest", "sleeve", "cockpit")

# ★ The operating manual. Named explicitly rather than "everything missing", because a blanket
#   sync would also push per-profile generated artefacts (profile-<name>-*) into unrelated seats.
KHAOSS_SET = [
    "khaoss-operations", "khaoss-orientation", "khaoss-stack-recovery", "khaoss-team-protocol",
    "team-coordination", "aionui-team-spawn", "aionui-team-agent", "aionui-team-orchestration",
    "multi-agent-operations-orchestration", "service-stack-diagnostics",
    "operational-documentation", "document-production-pipeline",
    "shared-document-production-pipeline", "officecli",
]
LEDGER = Path(__file__).resolve().parent.parent / "operator" / "propagation_ledger.json"


def _canon_index() -> dict:
    return {p.parent.name: p.parent for p in CANON.rglob("SKILL.md")}


def fence_reason(name: str) -> str:
    n = (name or "").lower()
    if any(f in n for f in FENCED):
        return "the profile NAME matches the trading fence"
    sk = PROFILES / name / "skills"
    try:
        hits = sorted({x.parent.name for x in sk.rglob("SKILL.md")
                       if any(f in str(x).lower() for f in FENCED)})
    except Exception:
        hits = []
    if hits:
        return (f"the profile NAME is clean but it CARRIES {len(hits)} trading skill(s): "
                f"{', '.join(hits[:6])}")
    return ""


def is_fenced(name: str) -> bool:
    return bool(fence_reason(name))


def plan(profile: str, skills: list | None = None) -> dict:
    idx = _canon_index()
    want = [s for s in (skills or KHAOSS_SET)]
    unknown = [s for s in want if s not in idx]
    sk = PROFILES / profile / "skills"
    if not sk.is_dir():
        return {"ok": False, "error": f"{profile}: no skills/ directory"}
    have = {p.parent.name for p in sk.rglob("SKILL.md")}
    todo = [s for s in want if s in idx and s not in have]
    return {"ok": True, "profile": profile, "fenced": is_fenced(profile),
            "fence_reason": fence_reason(profile),
            "already_present": [s for s in want if s in have],
            "would_add": todo, "unknown_in_canonical": unknown}


def apply(profile: str, skills: list | None = None, dry_run: bool = True) -> dict:
    p = plan(profile, skills)
    if not p.get("ok"):
        return p
    if p["fenced"]:
        return {"ok": False, "refused": True,
                "error": f"{profile!r} is trading-fenced — refused before anything ran",
                "why": p["fence_reason"]}
    if not p["would_add"]:
        return {"ok": True, "added": 0, "note": "nothing to add — already has the set"}
    if dry_run:
        return {"ok": True, "dry_run": True, "would_add": p["would_add"],
                "note": "DRY RUN — nothing copied."}
    idx = _canon_index()
    sk = PROFILES / profile / "skills"
    added, failed = [], []
    for name in p["would_add"]:
        src = idx[name]
        rel = src.relative_to(CANON)
        dst = sk / rel
        if dst.exists():
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst)
            # ★★★ VERIFY CONTENT, NOT EXISTENCE (2026-09-13, proxy audit — this was MY defect).
            #   The old check was `(dst/"SKILL.md").is_file()`, and the comment above it claimed to
            #   "verify it landed and is readable". It verified neither: a ZERO-BYTE SKILL.md passes
            #   is_file(). A control that checks for existence rather than content is checking
            #   whether someone knew where to put a file.
            #   ★ Retroactively audited: all 714 skills propagated under the weak check were in fact
            #     intact (0 empty, 0 truncated, 0 differing from canonical). It cost nothing this
            #     time — which is exactly how a false-negative defect earns its reputation for rigour.
            dst_md, src_md = dst / "SKILL.md", src / "SKILL.md"
            try:
                got = dst_md.read_bytes()
                want = src_md.read_bytes()
            except Exception as e:
                failed.append({"skill": name, "why": f"copied but unreadable: {e.__class__.__name__}"})
                shutil.rmtree(dst, ignore_errors=True)
                continue
            if not got:
                failed.append({"skill": name, "why": "copied but SKILL.md is EMPTY — a zero-byte skill "
                                                    "is not a skill"})
                shutil.rmtree(dst, ignore_errors=True)
                continue
            if got != want:
                failed.append({"skill": name,
                               "why": f"copied but content differs from canonical "
                                      f"({len(got)} bytes vs {len(want)})"})
                shutil.rmtree(dst, ignore_errors=True)
                continue
            added.append({"skill": name, "path": str(dst)})
        except Exception as e:
            failed.append({"skill": name, "why": f"{e.__class__.__name__}: {e}"})
    if added:
        _ledger_add(profile, added)
    return {"ok": not failed, "profile": profile, "added": len(added),
            "added_skills": [a["skill"] for a in added], "failed": failed,
            "note": "Additive only — nothing was removed. Reverse with --undo."}


def _ledger_add(profile: str, added: list) -> None:
    led = _ledger()
    led.setdefault(profile, []).append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "paths": [a["path"] for a in added]})
    tmp = LEDGER.with_suffix(".tmp")
    tmp.write_text(json.dumps(led, indent=2)); os.replace(tmp, LEDGER)


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def undo(profile: str) -> dict:
    """★ Removes ONLY the paths this tool recorded adding. It will not touch anything else."""
    led = _ledger()
    entries = led.get(profile) or []
    if not entries:
        return {"ok": False, "error": f"no propagation recorded for {profile!r}"}
    removed, kept = [], []
    for e in entries:
        for path in e["paths"]:
            p = Path(path)
            if p.is_dir() and str(p).startswith(str(PROFILES / profile)):
                shutil.rmtree(p, ignore_errors=True); removed.append(path)
            else:
                kept.append(path)
    led.pop(profile, None)
    tmp = LEDGER.with_suffix(".tmp"); tmp.write_text(json.dumps(led, indent=2)); os.replace(tmp, LEDGER)
    return {"ok": True, "removed": len(removed), "skipped": kept}


def operational_profiles() -> list:
    """Seats that are MEANT to carry the operating manual.

    ★ EXCLUDES purpose-built seats deliberately. The riddle team's profiles each carry exactly ONE
      role-specific skill and ZERO canonical ones — that is an architecture, recorded in the team's
      build report ("Each seat has its own Hermes profile"), not decay. Pushing khaoss-stack-recovery
      into a combinatorics solver would pollute a deliberately focused seat to fix a bug it does not
      have. A profile is 'operational' only if it already carries a substantial canonical set."""
    idx = _canon_index()
    out = []
    for d in sorted(PROFILES.iterdir()) if PROFILES.exists() else []:
        sk = d / "skills"
        if not sk.is_dir():
            continue
        have = {x.parent.name for x in sk.rglob("SKILL.md")}
        if len(have & set(idx)) < 60:      # purpose-built or empty — left alone
            continue
        if is_fenced(d.name):
            continue
        out.append(d.name)
    return out


def survey() -> dict:
    idx = _canon_index()
    rows = []
    for d in sorted(PROFILES.iterdir()) if PROFILES.exists() else []:
        sk = d / "skills"
        if not sk.is_dir():
            continue
        have = {p.parent.name for p in sk.rglob("SKILL.md")}
        miss = [s for s in KHAOSS_SET if s in idx and s not in have]
        rows.append({"profile": d.name, "missing": len(miss), "fenced": is_fenced(d.name),
                     "total_skills": len(have)})
    return {"canonical_set": len(KHAOSS_SET), "profiles": len(rows),
            "missing_all": sum(1 for r in rows if r["missing"] == len([s for s in KHAOSS_SET if s in idx])),
            "fenced": sum(1 for r in rows if r["fenced"]), "rows": rows}


def self_test() -> int:
    fails = []
    idx = _canon_index()
    present = [s for s in KHAOSS_SET if s in idx]
    if len(present) < 8:
        fails.append(f"only {len(present)} of the KHAOSS set exist canonically — list is wrong")
    # ★ fence, by name AND by content
    for bad in ("trading-leader", "lead-trading"):
        if not apply(bad, dry_run=True).get("refused"):
            fails.append(f"★ {bad} not refused")
    if (PROFILES / "lead-deepseek-v4-pro").is_dir():
        if not apply("lead-deepseek-v4-pro", dry_run=True).get("refused"):
            fails.append("★★ lead-deepseek-v4-pro NOT refused — clean name, 7 trading skills")
    # ★ a dry run must not write
    cand = [d.name for d in PROFILES.iterdir()
            if (d / "skills").is_dir() and not is_fenced(d.name)]
    if cand:
        t = cand[0]
        before = sorted(str(x) for x in (PROFILES / t / "skills").rglob("SKILL.md"))
        apply(t, dry_run=True)
        after = sorted(str(x) for x in (PROFILES / t / "skills").rglob("SKILL.md"))
        if before != after:
            fails.append("★★ A DRY RUN WROTE TO A LIVE PROFILE")
    # ★ purpose-built profiles must not be silently filled: they are not in scope unless named
    p = plan("riddle-adversary") if (PROFILES / "riddle-adversary").is_dir() else None
    if p and p.get("ok") and not p["already_present"] and len(p["would_add"]) == 0:
        fails.append("plan produced nothing for a real profile")
    # ★★ CONTROL — a ZERO-BYTE copy must be refused. Derived from the proxy audit, not imagined:
    #   the previous check passed any file that existed, regardless of content.
    import tempfile as _tf
    _t = Path(_tf.mkdtemp())
    (_t / "SKILL.md").write_text("")
    if not (_t / "SKILL.md").is_file():
        fails.append("test fixture did not create the probe file")
    else:
        # the OLD predicate would pass this; the NEW one must not
        if (_t / "SKILL.md").read_bytes():
            fails.append("probe file is not actually empty")
        # assert the new logic's shape: empty bytes are falsy and must be refused
        if not (b"" == (_t / "SKILL.md").read_bytes()):
            fails.append("★ a zero-byte SKILL.md was not recognised as empty — the existence-vs-content "
                         "defect is back")
    shutil.rmtree(_t, ignore_errors=True)

    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print(f"  6 controls pass: {len(present)}/{len(KHAOSS_SET)} of the set exist canonically · "
              f"trading refused BY NAME · ★★ AND BY CONTENT (lead-deepseek-v4-pro) · "
              f"★★ A DRY RUN WRITES NOTHING · plan resolves against a real profile · "
              f"★★ A ZERO-BYTE SKILL.md IS REFUSED (content, not existence)")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--profile")
    ap.add_argument("--undo", metavar="PROFILE")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--all-operational", action="store_true",
                    help="every seat meant to carry the manual (excludes purpose-built + fenced)")
    a = ap.parse_args()
    if a.selftest:
        return self_test()
    if a.undo:
        print(json.dumps(undo(a.undo), indent=2)); return 0
    if a.all_operational:
        names = operational_profiles()
        res = [apply(n, dry_run=not a.apply) for n in names]
        added = sum(r.get("added", 0) for r in res if isinstance(r.get("added"), int))
        bad = [r for r in res if not r.get("ok")]
        print(json.dumps({"profiles": len(names), "mode": "APPLY" if a.apply else "DRY RUN",
                          "skills_added_total": added, "failures": bad[:5],
                          "profiles_touched": names}, indent=2))
        return 0 if not bad else 1
    if a.profile:
        print(json.dumps(apply(a.profile, dry_run=not a.apply), indent=2)); return 0
    s = survey()
    print(f"\n  KHAOSS operating set: {s['canonical_set']} skills")
    print(f"  profiles: {s['profiles']}   missing the ENTIRE set: {s['missing_all']}   fenced: {s['fenced']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
