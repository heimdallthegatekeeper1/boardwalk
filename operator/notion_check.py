#!/usr/bin/env python3
"""notion_check.py — does the Notion token AUTHENTICATE, and can it SEE anything?

★★★ TWO DIFFERENT QUESTIONS, AND ONLY ONE OF THEM IS THE ONE THAT MATTERS.
    /v1/users/me returns 200 for a valid token with ZERO shared pages. So it proves the token
    authenticates and proves NOTHING about whether an agent can read a single word. Notion is
    share-by-page: an integration sees only pages explicitly connected to it.

★★ An empty result set from a valid token is exactly the state that READS AS WORKING AND DOES
   NOTHING — the shape of every defect found on this box tonight: a check passing on an artefact
   adjacent to the thing anyone cares about. So this asks both, and reports them separately.

★ The token is read from the store and NEVER printed.
"""
import json
import pathlib
import sys
import urllib.request

TOK = pathlib.Path.home() / ".khaoss-secrets" / "notion-token"
V = "2022-06-28"


def _call(url, payload=None):
    tok = TOK.read_text().strip()
    h = {"Authorization": "Bearer " + tok, "Notion-Version": V}
    data = None
    if payload is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=25).read().decode())


def main() -> int:
    if not TOK.exists():
        print("  no notion-token is stored — nothing to check")
        print("  ★ note: a stored `notion-login` is the LOGIN METHOD note, not the integration token")
        return 2
    try:
        me = _call("https://api.notion.com/v1/users/me")
    except Exception as e:
        body = ""
        try:
            body = e.read().decode()[:160]
        except Exception:
            pass
        print("  AUTHENTICATION: ✗ refused —", e.__class__.__name__, body)
        return 1
    print("  AUTHENTICATION: ★ works — Notion answered as", me.get("name") or "(unnamed bot)")
    try:
        s = _call("https://api.notion.com/v1/search", {"page_size": 5})
    except Exception as e:
        print("  VISIBILITY   : could not check —", e.__class__.__name__)
        return 1
    n = len(s.get("results") or [])
    if n:
        print(f"  VISIBILITY   : ★★ it can see {n} page(s)/database(s) — genuinely usable")
        return 0
    print("  VISIBILITY   : ★★★ SEES NOTHING. The token is valid and the workspace is empty to it.")
    print("                 Open a page in Notion, press ..., Connections, add the khaoss")
    print("                 integration. Authenticating and being useful are different things.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
