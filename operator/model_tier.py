#!/usr/bin/env python3
"""model_tier.py — decide what each seat's DEFAULT model costs, deliberately rather than by accident.

★★★ WHY THIS EXISTS — the measured root cause of the $199 burn, 2026-09-13.
    It was never mainly "too many calls". It was the DEFAULTS. Priced against the live OpenRouter
    catalog, across 74 runnable profiles:

        25 profiles  anthropic/claude-haiku-4.5     $1.000/M in
        19 profiles  deepseek/deepseek-v4-pro       $1.600/M in
         6 profiles  moonshotai/kimi-k3             $2.648/M in
         4 profiles  openai/gpt-5.5                 $5.000/M in
         1 profile   anthropic/claude-opus-5        $5.000/M in

    ★ FORTY-FOUR OF SEVENTY-FOUR SEATS DEFAULTED TO >= $1.00/M — while qwen/qwen3.8-27b is $0.214/M
      and deepseek/deepseek-v4-flash is $0.047/M. Nobody chose that fleet-wide; it accumulated.

    ★★ AND THE SHARPEST ONE IS AN ID SUFFIX. `deepseek/deepseek-v4-pro` is $1.600/M;
       `deepseek/deepseek-v4-pro-0813` — the SAME family, pinned — is $0.579/M. **2.8x, for a date.**
       Nineteen seats carry the expensive spelling.

    ★★★ THE ARITHMETIC THAT MATTERS AT $1.00 REMAINING: one 300K-token turn costs $1.50 on gpt-5.5
        and $0.0142 on deepseek-v4-flash. A single turn on the wrong default exceeds the balance.

★ THIS IS THE SAME SHAPE AS THE OLD "VERBOSE AIONUI DEFAULT" — a setting nobody picked, applied
  everywhere, paid for on every turn. The cure is the same: make the choice explicit and give it tiers.

★★ IT NEVER WRITES WITHOUT --apply, REFUSES the trading lane and the two GPT seats (a different
   account pays those), and every change is reversible from the backup it takes.
"""
from __future__ import annotations
import argparse, json, os, re, shutil, sys, time, urllib.request
from pathlib import Path

PROFILES = Path.home() / ".hermes" / "profiles"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
FENCE = ("trading", "crypto", "liquid", "coinvest", "sleeve", "cockpit")
# ★ NEVER REPOINT: a different account pays for these. Operator instruction, standing.
NEVER = ("openai-codex", "custom:gpt-5", "gpt-5")

# ★ The operator's architecture, 2026-09-13, in his words: leads on Qwen 3.8 27b; the ASSISTANTS to
#   leads on the larger, more capable models. Cheap coordination, expensive thinking — and the
#   expensive tier is entered deliberately rather than inherited.
TIERS = {
    "lead":      {"model": "qwen/qwen3.8-27b",             "why": "coordinates; many turns, little depth"},
    "assistant": {"model": "deepseek/deepseek-v4-pro-0813", "why": "does the hard thinking; fewer, deeper turns"},
    "worker":    {"model": "deepseek/deepseek-v4.1-flash",  "why": "bulk/mechanical work; cheapest served tier"},
    "local":     {"model": "",                              "why": "local ollama; $0.00 — set by hand"},
}


def classify(name: str) -> str:
    """Which tier does a seat belong to? ★ Name-based and therefore a GUESS — it is reported as a
    proposal, never applied without --apply, because a name is a label and labels have lied here."""
    n = name.lower()
    if any(f in n for f in FENCE):
        return "fenced"
    if n.startswith("lead-") or n in ("example-a", "example-leader") or "leader" in n:
        return "lead"
    # ★ bakeoff-* seats are DELIBERATELY pinned to specific models — that is their entire purpose
    #   (comparing them). Tiering them would overwrite a considered choice with a generic one, and
    #   for four of them it would have made the seat MORE expensive. Caught by asking "which seats
    #   does my own proposal make worse?" — a question worth asking of every bulk change.
    if n.startswith("bakeoff"):
        return "pinned"
    if n.startswith("riddle") or n in ("researcher",):
        return "assistant"
    if re.match(r"^swarm\d+$", n):
        return "worker"
    return "unclassified"


CACHE = Path(__file__).resolve().parent.parent / "operator" / "model_catalog_cache.json"
CACHE_TTL = 900   # 15 min — the catalog moves rarely; a stale price is still the right order of magnitude


def catalog(max_age: int = CACHE_TTL) -> dict:
    """★ CACHED. The panel card calling this took ~20s to paint because it re-fetched 445 models on
    every open — a surface that slow is a surface nobody uses, which is how the money question stayed
    invisible in the first place. Cached to disk with a TTL, and the cache's AGE is reported so a
    stale price is never mistaken for a fresh one."""
    try:
        if CACHE.is_file() and (time.time() - CACHE.stat().st_mtime) < max_age:
            d = json.loads(CACHE.read_text())
            if d:
                return d
    except Exception:
        pass
    d = _catalog_fetch()
    if d:
        try:
            tmp = CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, CACHE)
        except Exception:
            pass
    return d


def catalog_age() -> float | None:
    try:
        return round(time.time() - CACHE.stat().st_mtime, 1)
    except Exception:
        return None


def _catalog_fetch() -> dict:
    key = ""
    for f in PROFILES.glob("*/.env"):
        try:
            m = re.search(r"sk-or-[A-Za-z0-9._-]{20,}", f.read_text(errors="replace"))
            if m:
                key = m.group(0); break
        except Exception:
            pass
    if not key:
        return {}
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return {m["id"]: m for m in json.loads(r.read().decode()).get("data", [])}
    except Exception:
        return {}


def current_default(p: Path) -> str:
    cfg = p / "config.yaml"
    if not cfg.is_file():
        return ""
    t = cfg.read_text(errors="replace")
    m = re.search(r"^model:\s*\n((?:[ \t]+.*\n?)*)", t, re.M)
    d = re.search(r"default:\s*(\S+)", m.group(1) if m else "")
    return d.group(1).strip("'\"") if d else ""


def price(cat: dict, mid: str) -> float | None:
    e = cat.get(mid)
    if not e:
        return None
    try:
        return float(e.get("pricing", {}).get("prompt") or 0) * 1e6
    except Exception:
        return None


def survey(cat: dict | None = None) -> dict:
    cat = cat if cat is not None else catalog()
    rows = []
    for d in sorted(PROFILES.iterdir()):
        if not (d / "config.yaml").is_file():
            continue
        cur = current_default(d)
        tier = classify(d.name)
        prop = TIERS.get(tier, {}).get("model", "")
        rows.append({
            "profile": d.name, "tier": tier, "current": cur,
            "current_price": price(cat, cur),
            "current_served": cur in cat if cat else None,
            "proposed": prop, "proposed_price": price(cat, prop) if prop else None,
            "protected": any(x in d.name.lower() for x in NEVER) or tier == "fenced",
        })
    unroutable = [r for r in rows if cat and not r["current_served"]]
    return {"catalog_size": len(cat), "profiles": len(rows), "rows": rows,
            "unroutable": unroutable}


# ★★★ REPAIRS vs POLICY — kept apart deliberately.
#   A REPAIR fixes something that is broken on its own terms: a model id the endpoint does not serve,
#   or an id that is 2.8x dearer than the identical pinned model. Nobody chose those.
#   A POLICY change (the tiering) is the operator's architecture decision and needs his word.
#   Measured 2026-09-13 from 210 saved failure dumps: bare `gpt-5.5` 404'd 14 times
#   ("Invalid model name passed in model=gpt-5"), `inflection/inflection-3-pi` 9 times and
#   `nousresearch/hermes-4-70b` 3 times ("No endpoints found") — all retried to exhaustion.
PINNED_SWAP = {
    # dearer spelling -> identical family, pinned, 2.8x cheaper (measured against the live catalog)
    "deepseek/deepseek-v4-pro": "deepseek/deepseek-v4-pro-0813",
}


def repair(dry_run: bool = True) -> dict:
    """Fix only what is BROKEN or accidentally dearer. Never changes a deliberate choice."""
    cat = catalog()
    if not cat:
        return {"ok": False, "error": "no catalog — refusing to touch any id unverified"}
    fixed, skipped = [], []
    for d in sorted(PROFILES.iterdir()):
        if not (d / "config.yaml").is_file():
            continue
        if classify(d.name) == "fenced" or any(x in d.name.lower() for x in NEVER):
            skipped.append({"profile": d.name, "why": "fenced or another account's seat"}); continue
        # ★ an ARCHIVED_ profile is deliberately retired — repairing it would quietly resurrect a
        #   choice someone made about it. Leave it exactly as it is and say so.
        if d.name.startswith("ARCHIVED_"):
            skipped.append({"profile": d.name, "why": "archived — deliberately retired, not repaired"})
            continue
        cur = current_default(d)
        new_id = None
        if cur in PINNED_SWAP and PINNED_SWAP[cur] in cat:
            new_id, why = PINNED_SWAP[cur], "same family, pinned, 2.8x cheaper"
        elif cur and cur not in cat:
            # ★ an id the endpoint does not serve. Try the vendor-scoped spelling before giving up —
            #   the bare-id class is the single most common failure on this machine.
            cand = [m for m in cat if m.endswith("/" + cur) or m == cur]
            if len(cand) == 1:
                new_id, why = cand[0], f"unroutable bare id -> the served scoped spelling"
            else:
                skipped.append({"profile": d.name, "current": cur,
                                "why": f"NOT SERVED and no unambiguous replacement ({len(cand)} candidates)"})
                continue
        # ★ CHAIN THE REPAIRS. Fixing an unroutable bare id must not land on the DEARER spelling of
        #   the same family — that would trade one defect for a 2.8x bill. Apply the pinned swap to
        #   the result of the scope fix.
        if new_id in PINNED_SWAP and PINNED_SWAP[new_id] in cat:
            new_id = PINNED_SWAP[new_id]
            why += " (then pinned to the cheaper identical version)"
        if not new_id or new_id == cur:
            continue
        e = {"profile": d.name, "from": cur, "to": new_id, "why": why,
             "from_price": price(cat, cur), "to_price": price(cat, new_id)}
        if not dry_run:
            cfg = d / "config.yaml"
            bak = cfg.with_name(f"config.yaml.bak.{time.strftime('%Y%m%d_%H%M%S')}_repair")
            shutil.copy2(cfg, bak)
            t = cfg.read_text()
            t2 = re.sub(r"(^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+default:\s*)\S+",
                        lambda m: m.group(1) + new_id, t, count=1, flags=re.M)
            t2 = re.sub(r"(^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*)\S+",
                        lambda m: m.group(1) + new_id, t2, count=1, flags=re.M)
            cfg.write_text(t2)
            if current_default(d) != new_id:      # ★ read back CONTENT, never trust the write
                shutil.copy2(bak, cfg); e["failed"] = "write did not take — restored"
            else:
                e["backup"] = str(bak)
        fixed.append(e)
    return {"ok": True, "dry_run": dry_run, "repaired": len(fixed), "rows": fixed,
            "skipped": skipped}


def apply(dry_run: bool = True, only_tier: str = "") -> dict:
    cat = catalog()
    if not cat:
        # ★ fail closed: without a catalog we cannot know a model is served
        return {"ok": False, "error": "could not read the model catalog — refusing to change any "
                                      "default, because an unverified model id is how two teams died"}
    s = survey(cat)
    changed, skipped = [], []
    for r in s["rows"]:
        if r["protected"]:
            skipped.append({"profile": r["profile"], "why": "protected (trading-fenced or a GPT seat "
                                                            "billed to another account)"}); continue
        if r["tier"] not in TIERS or not r["proposed"]:
            skipped.append({"profile": r["profile"], "why": f"tier {r['tier']!r} has no proposal"}); continue
        if only_tier and r["tier"] != only_tier:
            continue
        if r["current"] == r["proposed"]:
            continue
        if r["proposed"] not in cat:
            skipped.append({"profile": r["profile"], "why": f"{r['proposed']} not in catalog"}); continue
        entry = {"profile": r["profile"], "tier": r["tier"], "from": r["current"],
                 "to": r["proposed"], "from_price": r["current_price"], "to_price": r["proposed_price"]}
        if not dry_run:
            cfg = PROFILES / r["profile"] / "config.yaml"
            bak = cfg.with_name(f"config.yaml.bak.{time.strftime('%Y%m%d_%H%M%S')}_tier")
            shutil.copy2(cfg, bak)
            t = cfg.read_text()
            new = re.sub(r"(^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+default:\s*)\S+",
                         lambda m: m.group(1) + r["proposed"], t, count=1, flags=re.M)
            new = re.sub(r"(^model:\s*\n(?:[ \t]+.*\n)*?[ \t]+name:\s*)\S+",
                         lambda m: m.group(1) + r["proposed"], new, count=1, flags=re.M)
            cfg.write_text(new)
            # ★ verify the write by READING IT BACK — content, not existence
            if current_default(PROFILES / r["profile"]) != r["proposed"]:
                shutil.copy2(bak, cfg)
                entry["failed"] = "write did not take — restored from backup"
            else:
                entry["backup"] = str(bak)
        changed.append(entry)
    return {"ok": True, "dry_run": dry_run, "changed": len(changed), "rows": changed,
            "skipped": skipped[:12], "skipped_total": len(skipped)}


def self_test() -> int:
    fails = []
    for n in ("trading-leader", "lead-trading", "crypto-x"):
        if classify(n) != "fenced":
            fails.append(f"★ {n} not fenced")
    if classify("lead-example") != "lead":
        fails.append("lead-example not classed as a lead")
    if classify("example-agent") != "worker":
        fails.append("example-agent not classed as a worker")
    s = survey({})
    if s["profiles"] < 40:
        fails.append(f"only {s['profiles']} profiles surveyed")
    # ★ FAIL CLOSED: no catalog -> refuse to write anything
    import unittest.mock as m
    with m.patch.object(sys.modules[__name__], "catalog", lambda: {}):
        r = apply(dry_run=False)
    if r.get("ok"):
        fails.append("★★ with NO CATALOG it did not refuse — it would set an unverified model id, "
                     "which is exactly how the Bee and Computer Maintenance teams died")
    # ★ a dry run must write nothing
    before = {d.name: current_default(d) for d in PROFILES.iterdir() if (d / "config.yaml").is_file()}
    apply(dry_run=True)
    after = {d.name: current_default(d) for d in PROFILES.iterdir() if (d / "config.yaml").is_file()}
    if before != after:
        fails.append("★★ A DRY RUN CHANGED A PROFILE")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: trading fenced · leads classed · workers classed · profiles surveyed · "
              "★★ NO CATALOG = REFUSE TO WRITE (never set an unverified id) · ★★ A DRY RUN CHANGES NOTHING")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repair", action="store_true",
                    help="fix only unroutable ids and the dearer-spelling swap (not policy)")
    ap.add_argument("--tier", default="", help="restrict to one tier: lead|assistant|worker")
    a = ap.parse_args()
    if a.selftest:
        return self_test()
    if a.survey:
        cat = catalog(); s = survey(cat)
        print(f"\n  catalog: {s['catalog_size']} models · profiles: {s['profiles']}\n")
        print(f"  {'PROFILE':26}{'TIER':13}{'CURRENT':34}{'$/M':>7}  {'PROPOSED':28}{'$/M':>7}")
        for r in s["rows"]:
            cp = f"{r['current_price']:.3f}" if r['current_price'] is not None else "  ?"
            pp = f"{r['proposed_price']:.3f}" if r['proposed_price'] is not None else "   —"
            mark = " ★" if r["protected"] else "  "
            print(f" {mark}{r['profile'][:24]:26}{r['tier']:13}{r['current'][:32]:34}{cp:>7}  {r['proposed'][:26]:28}{pp:>7}")
        if s["unroutable"]:
            print(f"\n  ★ {len(s['unroutable'])} profiles carry a default NOT IN THE CATALOG (would fail at spawn):")
            for r in s["unroutable"]:
                print(f"     {r['profile']:26} {r['current']}")
        return 0
    if a.repair:
        print(json.dumps(repair(dry_run=not a.apply), indent=2)[:5000]); return 0
    r = apply(dry_run=not a.apply, only_tier=a.tier)
    print(json.dumps(r, indent=2)[:4000])
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
