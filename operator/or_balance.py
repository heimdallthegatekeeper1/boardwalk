#!/usr/bin/env python3
"""or_balance.py — the OpenRouter balance, MEASURED, stamped, and fingerprinted.

★★★ WHY THIS EXISTS
    On 2026-09-13 the balance was measured properly — twice, stable, independently confirmed by a
    second seat: $1.0034. Correct at the time. It was then CARRIED FORWARD IN REPORT AFTER REPORT
    for five days, by both seats, and became the premise for a standing constraint: no paid
    delegation, CC-only work, no OpenRouter spend.

    Measured again 2026-09-18: **$26.21**. Someone topped the account up from $200 to $240 and
    nobody looked again.

    ★★ A WEEK OF SELF-IMPOSED AUSTERITY WAS BOUGHT WITH A STALE READING.
       Not a wrong measurement — a correct measurement that outlived its moment. The estate's own
       phrasing: *a figure carried between documents is a rumour until something counts it again.*

★ THE OTHER HYPOTHESIS WAS REFUTED, AND THAT MATTERS TOO. The likely cause was "more than one key"
  — it has a precedent here (26 profiles were found on a different, exhausted Anthropic key). It is
  FALSE for OpenRouter: 90 profiles, **exactly one distinct key**. Reported so nobody re-opens it.

RULE THIS ENFORCES: a balance may never be quoted without its TIMESTAMP and KEY FINGERPRINT.
An unstamped number becomes a fact within one document. This prints all three or nothing.

★ Never prints, logs or returns the key itself — only a sha256 prefix and the last four characters.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import sys
import urllib.request

KEY_RE = re.compile(
    r'^\s*(?:OPENROUTER_API_KEY|OPENAI_API_KEY)\s*=\s*["\']?(sk-or-[A-Za-z0-9_\-]+)', re.M)
ENV_GLOB = os.path.expanduser("~/.hermes/**/.env")


def _keys() -> dict[str, dict]:
    """{fingerprint: {"key": k, "profiles": [...]}} — every DISTINCT key in use."""
    found: dict[str, dict] = {}
    for f in glob.glob(ENV_GLOB, recursive=True):
        try:
            text = open(f, errors="replace").read()
        except Exception:
            continue
        for k in KEY_RE.findall(text):
            fp = hashlib.sha256(k.encode()).hexdigest()[:12] + "…" + k[-4:]
            prof = (f.split("/profiles/")[-1].split("/")[0]
                    if "/profiles/" in f else os.path.basename(os.path.dirname(f)))
            found.setdefault(fp, {"key": k, "profiles": []})["profiles"].append(prof)
    return found


def _balance(key: str) -> dict:
    req = urllib.request.Request("https://openrouter.ai/api/v1/credits",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read()).get("data", {})
    total, used = d.get("total_credits"), d.get("total_usage")
    if total is None or used is None:
        raise RuntimeError("credits endpoint returned no totals")
    return {"total_credits": total, "total_usage": used, "remaining": round(total - used, 4)}


def report() -> dict:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    keys = _keys()
    out = {"read_at": stamp, "distinct_keys": len(keys), "keys": []}
    for fp, info in keys.items():
        row = {"fingerprint": fp, "profiles": len(info["profiles"])}
        try:
            row.update(_balance(info["key"]))
        except Exception as exc:
            # ★ FAIL LOUD, NEVER SILENTLY REUSE A PRIOR NUMBER. An unreadable balance is
            #   "I could not look", which must never render identically to a figure.
            row["error"] = f"{exc.__class__.__name__}: {str(exc)[:80]}"
        out["keys"].append(row)
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if a.strip()]
    if args and args != ["--json"]:
        print(f"unknown argument(s): {' '.join(args)}\nusage: or_balance.py [--json]",
              file=sys.stderr)
        return 2
    d = report()
    if args == ["--json"]:
        print(json.dumps(d, indent=2))
        return 0 if all("error" not in k for k in d["keys"]) else 1
    print(f"  read at        : {d['read_at']}")
    print(f"  distinct keys  : {d['distinct_keys']}")
    bad = 0
    for k in d["keys"]:
        print(f"    {k['fingerprint']}  ({k['profiles']} profiles)")
        if "error" in k:
            bad += 1
            print(f"      ★ COULD NOT READ: {k['error']}  (this is not $0 — it is unknown)")
        else:
            print(f"      credits {k['total_credits']} · usage {k['total_usage']:.2f} "
                  f"· ★ REMAINING ${k['remaining']}")
    print("\n  ★ Quote this ONLY with the timestamp and fingerprint above. A figure carried "
          "between\n    documents is a rumour until something counts it again.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
