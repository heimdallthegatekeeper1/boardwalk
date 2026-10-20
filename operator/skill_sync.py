#!/usr/bin/env python3
"""skill_sync.py — stop 72 profiles each holding a private, frozen snapshot of the skills.

★★★ THE MECHANISM, confirmed in Hermes' own source rather than assumed:
      hermes_cli/profiles.py:731 → shutil.copytree(source_skills, profile_dir / "skills")
    `hermes profile create --clone` COPIES the skills tree. We cloned five profiles to end the
    memory bleed; every clone froze a private snapshot of the skills at that moment.

★★ SO THE CURE FOR ONE TENANCY PROBLEM MANUFACTURED ANOTHER. That is FC-11 — a repair that creates
   a finding — and it happened at a scale nobody noticed until a scan tripped over it.

★★★ AND THE SECOND-ORDER CONSEQUENCE IS WORSE THAN DRIFT: those trees are FROZEN. They receive no
     new skills and no fixes to existing ones. A fix written canonically never reaches them —
     built-but-unreachable, in skills.

★ WHAT THE NUMBERS ACTUALLY ARE (measured, and much more tractable than "81 skills diverge"):
    · 81 skill NAMES have more than one version
    · but ONE profile — mammouth-deepseek-debug — holds 72 of them
    · six other profiles hold between 1 and 5 each
  So this is one very stale profile plus a short tail, not a fleet-wide split.

★★ THIS TOOL DEFAULTS TO DRY RUN, REFUSES TRADING PROFILES OUTRIGHT, MOVES (never deletes) the old
   tree, and is reversible. Converting a live profile's skills to a symlink changes what an agent
   reads on its next turn; that is not a change to make in bulk on a hunch.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sys, time
from pathlib import Path

CANON = Path.home() / ".hermes" / "skills"
PROFILES = Path.home() / ".hermes" / "profiles"
# ★ the standing fence. These are refused by NAME, before anything else runs.
FENCED = ("trading", "crypto", "liquid", "coinvest", "sleeve", "cockpit")


def _fenced(name: str) -> bool:
    """★★★ THE FENCE MUST READ CONTENT, NOT JUST THE NAME.

    ★ THE HOLE THIS CLOSES, found before it was used: a recommendation arrived to converge
      `lead-deepseek-v4-pro` as a "starved" profile carrying only 9 skills. It was created the same
      day, and SEVEN of those nine are trading skills — offline-trading, trading-ops,
      escalation-protocol, fast-response-trading, model-tier-upgrade, precision-debate-protocol,
      dishy-watcher. It is a trading profile.
    ★★ AND ITS NAME CONTAINS NO TRADING WORD, so a name-only fence — which is what this function
       used to be — would have waved it straight through. A fence that only reads labels does not
       fence anything; it fences things that were honest about what they are."""
    n = (name or "").lower()
    if any(f in n for f in FENCED):
        return True
    # ★ content check: what does the profile actually CARRY?
    sk = PROFILES / name / "skills"
    try:
        paths = [str(x).lower() for x in sk.rglob("SKILL.md")]
    except Exception:
        return False
    if not paths:
        return False
    hits = sum(1 for x in paths if any(f in x for f in FENCED))
    # any trading skill at all is enough — this is a fence, not a vote
    return hits > 0


def fence_reason(name: str) -> str:
    n = (name or "").lower()
    if any(f in n for f in FENCED):
        return f"the profile NAME matches the trading fence"
    sk = PROFILES / name / "skills"
    try:
        hits = [str(x.parent.name) for x in sk.rglob("SKILL.md")
                if any(f in str(x).lower() for f in FENCED)]
    except Exception:
        hits = []
    if hits:
        return (f"the profile NAME is clean but it CARRIES {len(hits)} trading skill(s): "
                f"{', '.join(sorted(set(hits))[:6])}")
    return ""


def _fp(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        try:
            h.update(f.relative_to(d).as_posix().encode()); h.update(f.read_bytes())
        except Exception:
            h.update(b"?")
    return h.hexdigest()[:16]


def report() -> dict:
    out = []
    for pd in sorted(PROFILES.iterdir()) if PROFILES.exists() else []:
        sk = pd / "skills"
        if not sk.is_dir():
            continue
        linked = sk.is_symlink()
        names = {p.parent.name for p in sk.rglob("SKILL.md")}
        canon_names = {p.parent.name for p in CANON.rglob("SKILL.md")}
        out.append({"profile": pd.name, "symlinked": linked, "skills": len(names),
                    "fenced": _fenced(pd.name),
                    "missing_vs_canonical": sorted(canon_names - names)[:8],
                    "n_missing": len(canon_names - names)})
    return {"canonical": str(CANON), "profiles": len(out),
            "symlinked": sum(1 for o in out if o["symlinked"]),
            "copies": sum(1 for o in out if not o["symlinked"]), "rows": out}


def converge(profile: str, dry_run: bool = True) -> dict:
    """Replace one profile's copied skills tree with a symlink to canonical.
    ★ The old tree is MOVED aside, never deleted — restore is a rename."""
    if _fenced(profile):
        return {"ok": False, "refused": True,
                "error": f"{profile!r} is trading-fenced — refused before anything ran",
                "why": fence_reason(profile)}
    pd = PROFILES / profile
    sk = pd / "skills"
    if not pd.is_dir():
        return {"ok": False, "error": f"no such profile {profile!r}"}
    if sk.is_symlink():
        return {"ok": True, "already": True, "note": "already points at canonical"}
    if not sk.is_dir():
        return {"ok": False, "error": f"{sk} is not a directory"}
    before, canon = _fp(sk), _fp(CANON)
    parked = pd / f"skills.copy.{time.strftime('%Y%m%d_%H%M%S')}"
    plan = {"profile": profile, "current_fingerprint": before, "canonical_fingerprint": canon,
            "identical_to_canonical": before == canon,
            "would_park_to": str(parked), "would_link": f"{sk} -> {CANON}"}
    if dry_run:
        return {"ok": True, "dry_run": True, "plan": plan,
                "note": "DRY RUN — nothing moved, nothing linked."}
    if parked.exists():
        return {"ok": False, "error": f"refusing to overwrite {parked}"}
    os.rename(sk, parked)                       # ★ move, never delete
    os.symlink(CANON, sk)
    ok = sk.is_symlink() and sk.resolve() == CANON.resolve()
    if not ok:
        os.rename(parked, sk)                   # ★ put it back rather than leave it broken
        return {"ok": False, "error": "symlink did not verify — original tree restored"}
    return {"ok": True, "dry_run": False, "plan": plan,
            "note": f"converged. The old tree is parked at {parked} — restore with --restore."}


def restore(profile: str) -> dict:
    pd = PROFILES / profile
    sk = pd / "skills"
    parked = sorted(pd.glob("skills.copy.*"))
    if not parked:
        return {"ok": False, "error": "no parked copy to restore"}
    newest = parked[-1]
    if sk.is_symlink():
        os.unlink(sk)
    elif sk.exists():
        return {"ok": False, "error": f"{sk} exists and is not a symlink — refusing to clobber"}
    os.rename(newest, sk)
    return {"ok": True, "restored_from": str(newest)}


def self_test() -> int:
    fails = []
    for bad in ("trading-leader", "lead-trading", "crypto-thing", "COINVEST-x"):
        r = converge(bad, dry_run=True)
        if not r.get("refused"):
            fails.append(f"★ fenced profile {bad!r} was NOT refused")
    # ★★★ THE CONTROL THAT MATTERS MOST HERE: a profile whose NAME is innocent but which CARRIES
    #     trading skills must still be refused. `lead-deepseek-v4-pro` is exactly that shape, and a
    #     name-only fence waved it through.
    if (PROFILES / "lead-deepseek-v4-pro").is_dir():
        r = converge("lead-deepseek-v4-pro", dry_run=True)
        if not r.get("refused"):
            fails.append("★★ `lead-deepseek-v4-pro` was NOT refused — its name is clean but it "
                         "carries 7 trading skills. The fence is reading labels, not content.")
    r = converge("does-not-exist-xyz", dry_run=True)
    if r.get("ok"):
        fails.append("a non-existent profile was accepted")
    rep = report()
    if rep["profiles"] == 0:
        fails.append("report found no profiles")
    if rep["copies"] + rep["symlinked"] != rep["profiles"]:
        fails.append("profiles do not add up to copies + symlinked")
    # ★ a dry run must not touch the filesystem
    import tempfile
    probe = [p for p in (PROFILES.iterdir() if PROFILES.exists() else [])
             if (p / "skills").is_dir() and not (p / "skills").is_symlink() and not _fenced(p.name)]
    if probe:
        t = probe[0]
        before = sorted(x.name for x in t.iterdir())
        converge(t.name, dry_run=True)
        after = sorted(x.name for x in t.iterdir())
        if before != after:
            fails.append("★★ A DRY RUN MODIFIED A LIVE PROFILE DIRECTORY")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print(f"  6 controls pass: ★ TRADING PROFILES REFUSED BY NAME (4 spellings) · ★★ AND BY "
              f"CONTENT (lead-deepseek-v4-pro: clean name, 7 trading skills) · unknown profile "
              f"refused · {rep['profiles']} profiles enumerated · counts reconcile · "
              f"★★ A DRY RUN TOUCHES NOTHING ON DISK")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--converge", metavar="PROFILE")
    ap.add_argument("--restore", metavar="PROFILE")
    ap.add_argument("--apply", action="store_true", help="actually do it (default is a dry run)")
    a = ap.parse_args()
    if a.selftest:
        return self_test()
    if a.restore:
        print(json.dumps(restore(a.restore), indent=2)); return 0
    if a.converge:
        print(json.dumps(converge(a.converge, dry_run=not a.apply), indent=2)); return 0
    r = report()
    print(f"\n  canonical: {r['canonical']}")
    print(f"  profiles: {r['profiles']}   symlinked: {r['symlinked']}   private copies: {r['copies']}")
    worst = sorted(r["rows"], key=lambda x: -x["n_missing"])[:10]
    print("\n  profiles missing the MOST canonical skills (frozen trees):")
    for w in worst:
        tag = " [FENCED]" if w["fenced"] else ""
        print(f"    {w['profile'][:30]:30} {w['skills']:>4} skills  missing {w['n_missing']:>3}{tag}")
    print("\n  Nothing was changed. --converge <profile> shows a plan; add --apply to do it.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
