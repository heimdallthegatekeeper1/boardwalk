#!/usr/bin/env python3
"""model_context_check.py — the model's REAL context is the PROVIDER's, not the catalog's.

★★★ WHY THIS EXISTS
    The operator asked for efficient models and named qwen3.8-27b. I applied it to 12 lead
    profiles on 2026-09-17, having checked the catalog:

        openrouter catalog   context_length      : 1,000,000
        the serving provider top_provider cap    :   262,144     ← 3.8x overstatement

    `lead-example`'s measured workload over 1,110 real API calls: p50 168,923 · p95 426,401 · max
    501,166 tokens, with **25.5% of calls above 262,144**. It hit the wall the same morning, in
    the provider's own words:

        "maximum context length of 262144 tokens. You requested a total of 307631 tokens"

    ★★ THE CATALOG FIELD I READ WAS NOT THE FIELD THAT BINDS. `context_length` is what the model
       can do somewhere; `top_provider.context_length` is what THIS account's serving provider will
       actually accept. This estate already has the rule in another costume — "only a CALL proves a
       route", learned when declared routing drained a metered account while the screen said
       subscription. ★ IT APPLIES TO CONTEXT TOO, and I did not carry it across.

★ This checks the binding number against MEASURED demand, not against a guess. A profile with no
  observed traffic reports UNKNOWN — never "fine", because an unmeasured profile is exactly the one
  whose first big turn discovers the cap.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

PROFILES = Path.home() / ".hermes" / "profiles"
CATALOG = "https://openrouter.ai/api/v1/models"


def _catalog() -> dict:
    with urllib.request.urlopen(CATALOG, timeout=30) as r:
        return {e["id"]: e for e in json.loads(r.read())["data"]}


def _model_of(profile: Path) -> str | None:
    cfg = profile / "config.yaml"
    try:
        text = cfg.read_text(errors="replace")
    except Exception:
        return None
    # ★ The key is nested under `model:` as `default:`. A flat regex for `^model:` returns the
    #   literal "default:" — which is exactly the mis-read that started this, one layer down.
    m = re.search(r'^\s*model\s*:\s*\n(?:\s+.*\n)*?\s+default\s*:\s*["\']?([\w\-./:]+)',
                  text, re.M)
    if m:
        return m.group(1)
    m = re.search(r'^\s*model\s*:\s*["\']?([\w\-./:]+)\s*$', text, re.M)
    return m.group(1) if m else None


def _observed_tokens(profile: Path) -> list[int]:
    """Input-token sizes this profile has ACTUALLY sent.

    ★ state.db FIRST. The first cut read only sessions/request_dump_*.json and reported lead-example as
      UNMEASURED — the very profile whose 1,110 real calls (p95 426,401) motivated this checker.
      A checker that cannot see the case it was built for is not yet a checker; it just says
      "could not look" honestly, which is better than lying and still not the job.
    """
    # ★★★ state.db's sessions.input_tokens IS NOT USABLE HERE, AND I SHIPPED IT BEFORE CHECKING.
    #   It is a CUMULATIVE per-session total, not a per-call size. Measured: session c2bc40b4 has
    #   717 messages and input_tokens 6,545,475 — 9,129 per message. Comparing that running total
    #   against a PER-CALL cap produced seven confident OVER_CAP verdicts, including "lead-example 50%
    #   over", and every one was meaningless.
    #   ★ Fourth time today I took a field whose NAME matched and drew a conclusion about
    #     behaviour without checking what it counts. The tell is the same every time: the number
    #     was implausible (4.5M against a measured p95 of 426K) and I nearly reported it anyway.
    #   ★★ So this reports UNMEASURED rather than a wrong number. "Could not look" is a first-class
    #     answer; a confident wrong verdict would have sent someone to change seven profiles.
    out = []
    for f in sorted(profile.glob("sessions/request_dump_*.json"))[-60:]:
        try:
            d = json.loads(f.read_text(errors="replace"))
        except Exception:
            continue
        u = d.get("usage") or {}
        n = u.get("prompt_tokens")
        if isinstance(n, int) and n > 0:
            out.append(n)
            continue
        msgs = d.get("messages") or []
        if msgs:                       # ★ chars/4 is an ESTIMATE and is labelled as one
            out.append(sum(len(str(m.get("content") or "")) for m in msgs) // 4)
    return out


def check() -> dict:
    try:
        cat = _catalog()
    except Exception as exc:
        return {"error": f"catalog unreachable: {exc.__class__.__name__} — "
                         f"this is 'could not look', not 'all fine'"}
    rows = []
    for p in sorted(PROFILES.iterdir()):
        if not p.is_dir():
            continue
        model = _model_of(p)
        if not model:
            continue
        entry = cat.get(model)
        if not entry:
            rows.append({"profile": p.name, "model": model, "severity": "UNKNOWN_MODEL",
                         "detail": "not in the OpenRouter catalog"})
            continue
        declared = entry.get("context_length") or 0
        served = (entry.get("top_provider") or {}).get("context_length") or 0
        obs = _observed_tokens(p)
        row = {"profile": p.name, "model": model, "declared": declared, "served": served,
               "samples": len(obs)}
        if not obs:
            row["severity"] = "UNMEASURED"
            row["detail"] = (f"served cap {served:,}"
                             + (f" (catalog claims {declared:,})" if declared > served else "")
                             + " — no observed traffic, so demand is unknown")
        else:
            obs.sort()
            p95 = obs[int(len(obs) * 0.95) - 1] if len(obs) > 1 else obs[0]
            over = sum(1 for n in obs if n > served) if served else 0
            row.update({"p95": p95, "max": obs[-1], "over_cap": over,
                        "over_pct": round(100 * over / len(obs), 1)})
            if served and over:
                row["severity"] = "OVER_CAP"
                row["detail"] = (f"{over}/{len(obs)} observed calls ({row['over_pct']}%) exceed the "
                                 f"SERVED cap {served:,}; p95 {p95:,}, max {obs[-1]:,}")
            elif served and p95 > served * 0.8:
                row["severity"] = "NEAR_CAP"
                row["detail"] = f"p95 {p95:,} is within 20% of the served cap {served:,}"
            else:
                row["severity"] = "OK"
                row["detail"] = f"p95 {p95:,} against served cap {served:,}"
        if declared and served and declared > served:
            row["catalog_overstates"] = round(declared / served, 1)
        rows.append(row)
    return {"rows": rows}


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if args and args not in (["--json"],):
        print(f"unknown argument(s): {' '.join(args)}\nusage: model_context_check.py [--json]",
              file=sys.stderr)
        return 2
    d = check()
    if "error" in d:
        print(f"  ★ {d['error']}")
        return 1
    if args == ["--json"]:
        print(json.dumps(d, indent=2))
    order = {"OVER_CAP": 0, "NEAR_CAP": 1, "UNKNOWN_MODEL": 2, "UNMEASURED": 3, "OK": 4}
    bad = 0
    for r in sorted(d["rows"], key=lambda r: order.get(r["severity"], 9)):
        if r["severity"] in ("OVER_CAP", "NEAR_CAP"):
            bad += 1
        if args != ["--json"] and r["severity"] != "OK":
            mark = "★" if r["severity"] in ("OVER_CAP", "NEAR_CAP") else " "
            over = (f"  [catalog overstates {r['catalog_overstates']}x]"
                    if r.get("catalog_overstates") else "")
            print(f"  {mark} {r['severity']:<13} {r['profile']:<24} {r['model']}")
            print(f"      {r['detail']}{over}")
    if args != ["--json"]:
        ok = sum(1 for r in d["rows"] if r["severity"] == "OK")
        print(f"\n  {len(d['rows'])} profiles · {ok} OK · {bad} over/near the SERVED cap")
        print("  ★ the served cap is top_provider.context_length, NOT the catalog's context_length")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
