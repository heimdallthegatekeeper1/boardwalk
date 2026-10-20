#!/usr/bin/env python3
"""★ FC-11 — DRY-RUN A REPAIR AGAINST THE BOARD BEFORE IT FIRES.

★ THE CASE THAT PRODUCED THIS (the trading lane's, and the sharpest thing in the patrol report):
  re-minting 94b01ce0 would put a SECOND TENANT on `trading-leader` — the profile flagged for co-tenancy
  by THE SAME PATROL RUN. **A repair that creates a finding the same instrument just raised is not a
  repair.** The operator was one approval away from resolving one class by instantiating another.

★★ SO: shadow mode, applied to REPAIRS. Before firing, say which checks move and IN WHICH DIRECTION.
  This is the same instinct as running P1's gate log-only first — decide, record, change nothing — and it
  generalises past this case: binding the seven trading seats, splitting a profile, retiring a lead.

★★★ IT PREDICTS, IT DOES NOT SIMULATE. It reasons from the CURRENT board plus what the remediation is
  documented to do. A predicted delta is a claim, and it is labelled one — never presented as an observed
  outcome. Where it cannot predict, it says so rather than reporting "no change".

USE:  remediation_preview.py --remediation remint-acp-session --target <conversation-id>
"""
from __future__ import annotations
import argparse, json, pathlib, sqlite3, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
DB = "__NEEDS_OS_HOME__/.config/AionUi-Dev/aionui/aionui.db"


def team_of(conv: str):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT extra, name FROM conversations WHERE id LIKE ?", (conv + "%",)).fetchone()
        if not r:
            return None, None, None
        e = json.loads(r[0] or "{}")
        prof = e.get("agentProfile")
        team = None
        for tid, tname, agents in con.execute("SELECT id,name,agents FROM teams"):
            try:
                if any((a.get("conversationId") or "").startswith(conv[:8]) for a in json.loads(agents or "[]")):
                    team = tname; break
            except Exception:
                continue
        return prof, team, r[1]
    finally:
        con.close()


def tenants_of(profile: str):
    """Which teams already treat this profile as their memory boundary."""
    out = []
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        for tid, tname, agents in con.execute("SELECT id,name,agents FROM teams"):
            try:
                A = json.loads(agents or "[]")
            except Exception:
                continue
            lead = next((a for a in A if a.get("role") == "leader"), A[0] if A else None)
            if not lead or not lead.get("conversationId"):
                continue
            r = con.execute("SELECT extra FROM conversations WHERE id LIKE ?",
                            (lead["conversationId"] + "%",)).fetchone()
            if r and (json.loads(r[0] or "{}") or {}).get("agentProfile") == profile:
                out.append(tname)
    finally:
        con.close()
    return out


def board_now():
    r = subprocess.run(["/usr/bin/python3", str(HERE / "tenancy_assert.py")], capture_output=True, timeout=60)
    return r.returncode, r.stdout.decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--remediation", required=True)
    ap.add_argument("--target", required=True, help="conversation id")
    a = ap.parse_args()

    prof, team, name = team_of(a.target)
    if prof is None:
        print(f"  unknown conversation {a.target!r}"); return 2

    rc, out = board_now()
    print(f"\n★ DRY RUN — {a.remediation} on {a.target} ({name})")
    print(f"  target profile      : {prof}")
    print(f"  board BEFORE        : tenancy {'FAIL' if rc else 'PASS'}")

    deltas = []
    if a.remediation == "remint-acp-session":
        others = [t for t in tenants_of(prof) if t != team]
        # ★ re-minting makes this conversation a LIVE tenant of `prof` again.
        if others:
            deltas.append(("WORSENS", f"INV-111 tenancy — re-minting makes {name!r} a live tenant of "
                                      f"{prof!r}, which {len(others)} other team(s) already hold "
                                      f"({', '.join(others)}). The repair CREATES the co-tenancy finding."))
        else:
            deltas.append(("neutral", f"INV-111 tenancy — {prof!r} has no other team bound; re-minting "
                                      f"adds no co-tenancy"))
        deltas.append(("IMPROVES", "INV-112 patrol — this conversation stops appearing as an unresolvable "
                                   "ACP session"))
    else:
        deltas.append(("UNKNOWN", f"no predicted impact is recorded for {a.remediation!r} — this tool "
                                  f"cannot say, and says so rather than reporting 'no change'"))

    print("\n  PREDICTED DELTA (a claim, not an observation):")
    for d, why in deltas:
        mark = {"WORSENS": "✗", "IMPROVES": "✓", "neutral": "·", "UNKNOWN": "?"}[d]
        print(f"    {mark} {d:8s} {why}")
    worsens = [d for d in deltas if d[0] == "WORSENS"]
    print(f"\n  {'★ THIS REPAIR MANUFACTURES A FINDING — the operator rules on the trade, with both sides named.' if worsens else 'no check is predicted to worsen.'}\n")
    return 1 if worsens else 0


if __name__ == "__main__":
    sys.exit(main())
