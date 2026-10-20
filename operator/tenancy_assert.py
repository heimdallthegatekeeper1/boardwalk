#!/usr/bin/env python3
"""★ ONE TEAM, ONE PROFILE — the guard that stops the memory bleed being rebuilt a third time.

★ WHY: a hermes profile owns a `memories/` directory, and that directory is the memory boundary. So two
  teams sharing a profile share a memory, and everything one lane writes the other reads at session start.
  We have now built that bleed TWICE BY ACCIDENT — once by letting five leads default onto
  `lead-deepseek-v4-pro`, and once nearly again by proposing to bind teams to CLEARANCE-TIER profiles,
  which would have put any two teams of the same RANK back into one store. The third time is refused here.

★★ RANK IS NOT TENANCY. The tier profiles (initiate/seeker/adept/oracle/sage) are TEMPLATES to clone FROM
  at mint time — rank is inherited once, tenancy is forever. A team bound directly to a tier profile is a
  tenancy violation wearing a clearance label, so the tiers are named below and refused as bindings.

★★★ AND THE OLD NAME IS GUARDED BY NAME. `lead-deepseek-v4-pro` now has exactly one tenant (trading) and
  empty memory. Any NEW binding to it re-opens the bleed with the most sensitive lane on the box, so a
  second tenant on that name is a FAIL even though the same rule would allow it for any other profile.

USE:  tenancy_assert.py              # check (exit 1 on any violation)
      tenancy_assert.py --self-test  # prove it refuses the shape it exists to catch
"""
from __future__ import annotations
import json, pathlib, sqlite3, sys
from collections import defaultdict

DB = "__NEEDS_OS_HOME__/.config/AionUi-Dev/aionui/aionui.db"
TIER_PROFILES = {"lead-initiate", "lead-seeker", "lead-adept", "lead-oracle", "lead-sage"}
GUARDED_BY_NAME = "lead-deepseek-v4-pro"      # one tenant only: the trading lane
GUARDED_ALLOWED_TENANT = "Example Team"
LANES_FILE = pathlib.Path(__file__).resolve().parent / "team_lanes.json"


def lane_of(team: str) -> str:
    """★ SEVERITY DEPENDS ON WHETHER THE CO-TENANTS ARE IN DIFFERENT LANES. Two trading teams sharing a
    memory is a tenancy question for one lane; a trading team and a book-writing team sharing one is a
    breach. An invariant that scores both the same convicts every dev/test pair and teaches the operator
    to ignore it — the alarm-fatigue failure already named for INV-89.
    ★★ 'unknown' is NOT a lane: two unknowns are not assumed different, because assuming would MANUFACTURE
    severity out of a gap in the map."""
    try:
        return json.loads(LANES_FILE.read_text())["lanes"].get(team, "unknown")
    except Exception:
        return "unknown"


def team_profiles(rows=None):
    """[(team_name, profile)] for every team whose lead declares one."""
    if rows is not None:
        return rows
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    out = []
    try:
        for tid, name, agents in con.execute("SELECT id, name, agents FROM teams"):
            try:
                A = json.loads(agents or "[]")
            except Exception:
                continue
            lead = next((a for a in A if a.get("role") == "leader"), A[0] if A else None)
            if not lead or not lead.get("conversationId"):
                continue
            r = con.execute("SELECT extra FROM conversations WHERE id LIKE ?",
                            (lead["conversationId"] + "%",)).fetchone()
            if not r:
                continue
            prof = (json.loads(r[0] or "{}") or {}).get("agentProfile")
            if prof:
                out.append((name, prof))
    finally:
        con.close()
    return out


def check(rows=None):
    rows = team_profiles(rows)
    # ★ LIVENESS PRECONDITION (trading lane, 2026-08-29). This check is INVARIANCE-SHAPED — it asserts a
    #   property holds across a population — and an EMPTY population satisfies every such property. With
    #   zero teams it reported PASS, which reads identically to "every team is correctly separated".
    #   ★★ "Nothing changed" and "nothing happened" are not the same verdict.
    if not rows:
        return rows, {}, ["SAW NOTHING: no team declares a lead profile, so this check examined an EMPTY "
                          "population. That is not a pass — an invariance holds trivially over nothing. "
                          "Either the DB is unreadable, the schema moved, or there are genuinely no teams; "
                          "this cannot tell which, and says so rather than reporting green."], []
    by_profile = defaultdict(list)
    for name, prof in rows:
        by_profile[prof].append(name)
    fails = []
    notices = []

    for prof, teams in sorted(by_profile.items()):
        if len(teams) > 1:
            lanes = {lane_of(t) for t in teams}
            known = {l for l in lanes if l != "unknown"}
            cross = len(known) > 1
            detail = ", ".join(f"{t} [{lane_of(t)}]" for t in teams)
            if cross:
                fails.append(f"SEVERE · CROSS-LANE CO-TENANCY: {prof!r} is the memory boundary for "
                             f"{len(teams)} teams in DIFFERENT lanes ({detail}) — each opens with the "
                             f"others' lane in context at every session start")
            elif not known:
                # ★ UNKNOWN IS NOT A LANE, AND IT IS ALSO NOT AN ALL-CLEAR. Two unmapped teams sharing a
                #   memory might be same-lane housekeeping or a breach; this cannot tell. Silently calling
                #   it a NOTICE would let an unmapped pair sit un-alarmed forever, so the GAP is the
                #   finding: it fails, and it names the missing map rather than the teams.
                fails.append(f"SEVERITY UNDETERMINABLE: {prof!r} is shared by {len(teams)} teams whose "
                             f"lanes are NOT IN operator/team_lanes.json ({detail}) — add them and re-run. "
                             f"This is a gap in the map, not a verdict about the teams")
            else:
                notices.append(f"NOTICE · SAME-LANE CO-TENANCY: {prof!r} is shared by {len(teams)} teams "
                               f"in one lane ({detail}) — a tenancy question for that lane to rule on, "
                               f"not a breach, and not ours to fix")
        if prof in TIER_PROFILES:
            fails.append(f"RANK BOUND AS TENANCY: {teams[0]!r} is bound directly to the tier profile "
                         f"{prof!r}. Tiers are TEMPLATES to clone from, never profiles to share — binding by "
                         f"rank puts any two teams of the same rank into one memory store")
        if prof == GUARDED_BY_NAME:
            others = [t for t in teams if t != GUARDED_ALLOWED_TENANT]
            if others:
                fails.append(f"NEW BINDING TO THE OLD SHARED NAME: {', '.join(others)} bound to "
                             f"{GUARDED_BY_NAME!r}, which is reserved to {GUARDED_ALLOWED_TENANT!r}. A second "
                             f"tenant here re-opens the bleed with the most sensitive lane on the box")
    return rows, by_profile, fails, notices


def main() -> int:
    rows, by_profile, fails, notices = check()
    print(f"\n★ TENANCY — {len(rows)} team(s) with a declared lead profile, {len(by_profile)} distinct profile(s)")
    for prof, teams in sorted(by_profile.items()):
        mark = "✗" if len(teams) > 1 or prof in TIER_PROFILES else "✓"
        print(f"  {mark} {prof:24s} {', '.join(teams)}")
    if notices:
        print()
        for n in notices:
            print(f"  · {n}")
    if fails:
        print()
        for f in fails:
            print(f"  ✗ {f}")
    print(f"\n  {'FAIL' if fails else 'PASS'} — {len(fails)} violation(s)\n")
    return 1 if fails else 0


def self_test() -> int:
    """★ The check must refuse the shape it exists to catch, or its green is worthless."""
    cases = [
        ("★ two teams, DIFFERENT lanes — the breach", [("Example Team", "lead-x"), ("Example Team", "lead-x")], True),
        ("two teams, SAME lane — a notice, not a breach", [("Crypto trading", "lead-x"), ("Shoulder strike bots", "lead-x")], False),
        ("★ two teams with UNMAPPED lanes — the gap must fail, not pass", [("Alpha", "lead-x"), ("Beta", "lead-x")], True),
        ("one team per profile — healthy", [("Alpha", "lead-a"), ("Beta", "lead-b")], False),
        ("★ a team bound to a TIER profile — rank as tenancy", [("Alpha", "lead-adept")], True),
        ("★ a second tenant on the old shared name", [("Example Team", GUARDED_BY_NAME), ("Alpha", GUARDED_BY_NAME)], True),
        ("trading alone on the old name — allowed", [("Example Team", GUARDED_BY_NAME)], False),
    ]
    bad = 0
    print("═══ TENANCY SELF-TEST — it must refuse what it exists to catch ═══")
    for label, rows, want_fail in cases:
        _, _, fails, _n = check(rows)
        got = bool(fails)
        ok = got == want_fail
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {label:52s} → {'RED' if got else 'GREEN'} (want {'RED' if want_fail else 'GREEN'})")
    print("★ SELF-TEST PASSED\n" if not bad else "✗ SELF-TEST FAILED\n")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
