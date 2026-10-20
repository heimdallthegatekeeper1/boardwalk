#!/usr/bin/env python3
"""★ DOES THIS URL PARAMETER DO ANYTHING? — fetch with it and without it, and diff.

★ CONTRIBUTED BY THE TRADING LANE, 2026-08-29, after I published a lane-scoped URL onto a surface that
  ignores lanes. Their one-liner would have caught it in one command: /operator/board returned the SAME
  38,367 bytes with and without ?lane=trading. **A 200 with plausible-looking content is a very convincing
  false positive**, and the only reason it was caught is that they verified the URL instead of pasting it.

★★ AND ITS BLIND SPOT, WHICH I FOUND BY RUNNING IT: identical bytes DO NOT mean the parameter did nothing
  when the parameter is read CLIENT-SIDE. The root panel is byte-identical with and without ?lane, and it
  filters correctly at runtime — 67 rows down to 7. So this check can prove a parameter IGNORED by a
  server, and can never prove one HONOURED. It says which, rather than reporting a verdict it cannot hold.
  *The detector that catches "verified X, claimed it for Y" must not commit the same substitution itself.*

USE:  param_effect_check.py <url> <param=value> [...]
      param_effect_check.py --demo
"""
from __future__ import annotations
import hashlib, sys, urllib.request


def fetch(url: str) -> bytes:
    return urllib.request.urlopen(url, timeout=15).read()


def check(base: str, param: str) -> dict:
    sep = "&" if "?" in base else "?"
    with_p = base + sep + param
    a, b = fetch(base), fetch(with_p)
    same = hashlib.md5(a).hexdigest() == hashlib.md5(b).hexdigest()
    client_side = b"URLSearchParams" in b or b"location.search" in b
    return {
        "base_bytes": len(a), "with_param_bytes": len(b), "identical": same,
        "reads_params_client_side": client_side,
        "verdict": (
            "SERVER IGNORES IT — identical bytes and no client-side param reader; this URL is a lie"
            if same and not client_side else
            "CANNOT TELL FROM BYTES — identical, but the page reads parameters in JS. "
            "Render it and count what changes; this check cannot see runtime filtering"
            if same else
            "the server responded differently — the parameter reaches something"
        ),
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--demo"]
    if "--demo" in sys.argv:
        args = ["http://127.0.0.1:7842/operator/board", "lane=trading",
                "http://127.0.0.1:7842/?section=operator-needs", "lane=trading"]
    if len(args) < 2 or len(args) % 2:
        print(__doc__); return 2
    bad = 0
    print("\n★ PARAMETER-EFFECT CHECK\n")
    for i in range(0, len(args), 2):
        base, param = args[i], args[i + 1]
        try:
            r = check(base, param)
        except Exception as e:
            print(f"  [SKIP] {base} — {str(e)[:70]}"); continue
        mark = "✗" if r["verdict"].startswith("SERVER IGNORES") else ("?" if r["identical"] else "✓")
        if mark == "✗":
            bad += 1
        print(f"  {mark} {base}  ?{param}")
        print(f"      {r['base_bytes']}b vs {r['with_param_bytes']}b · identical={r['identical']} · "
              f"client-side reader={r['reads_params_client_side']}")
        print(f"      {r['verdict']}\n")
    print(f"  {bad} URL(s) proven to ignore their parameter\n")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
