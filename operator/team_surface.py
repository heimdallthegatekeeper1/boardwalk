#!/usr/bin/env python3
"""★ OPERATOR BOARD → THE TEAM CHATS. Install and verify the operator surface in every team.

★ WHY THIS EXISTS: `operator/blockers.py` made outstanding items visible to ONE lane — the KHAOSS dev
  chat, where it is run. Every other team chat was structurally blind to them, which is the failure the
  board was built to end: an item can be outstanding in one lane and invisible in another. The trading
  lane hit this first — four operator-blocking items, correctly refusing to write into another tree, with
  nowhere to put them. `--add` fixed the WRITE side. This fixes the READ side.

★ THE MECHANISM, MEASURED (hermes v0.14, `agent/prompt_builder.py::build_context_files_prompt`):
  a hermes ACP agent loads exactly ONE project-context file from its cwd, first match wins —
      1. .hermes.md / HERMES.md  (walks up to the git root, or to / when there is no repo)
      2. AGENTS.md / agents.md   (cwd only)
      3. CLAUDE.md / claude.md   (cwd only)
      4. .cursorrules
  AionUi spawns hermes with cwd = the team workspace (acpConnectors.ts). So a marker block in
  `<workspace>/AGENTS.md` reaches that team's lead at session start, and only that team.
  ★ PRECEDENCE IS THE FOOTGUN: one `.hermes.md` dropped anywhere above a workspace silently wins and
  every AGENTS.md below it stops being read. The checker asserts that, because the failure is invisible.

★ AND ONE STORE: the block carries NO copy of the items — a pasted digest is stale the moment he defers
  one. It points at `blockers.py` (live) and `OPERATOR_BOARD.md` (generated), and nothing else.

USE:
    team_surface.py --check        # verify every target (exit 1 on any FAIL)
    team_surface.py --install      # install/update the block in every eligible target
    team_surface.py --install --team <id-prefix>
    team_surface.py --list         # resolve targets against the live DB and print them
"""
from __future__ import annotations
import ast, json, os, re, sqlite3, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGETS = HERE / "team_surface_targets.json"
BLOCKERS = HERE / "blockers.json"
BOARD_MD = HERE / "OPERATOR_BOARD.md"
AIONUI_DB = Path.home() / ".config/AionUi-Dev/aionui/aionui.db"
HERMES_PROMPT_BUILDER = Path.home() / ".hermes/hermes-agent/agent/prompt_builder.py"

BEGIN = "<!-- KHAOSS:OPERATOR-BOARD:BEGIN v2 -->"
END = "<!-- KHAOSS:OPERATOR-BOARD:END -->"
CTX_NAMES_HERMES = (".hermes.md", "HERMES.md")
CTX_NAMES_EARLIER = ()  # nothing outranks AGENTS.md except the hermes.md pair, which walks


def block(board_url: str) -> str:
    """The text every team lead loads at session start. Kept SHORT on purpose — every team pays these
    tokens on every session, so it carries the rule and the pointers, never the items.

    ★ v2, 2026-08-29. v1 ENDED WITH A LINK TO A CLOUD ARTIFACT, three lines under its own instruction to
      read the board live and never from a copy. That artifact was a snapshot: it showed 10 of the items
      then on the board and had missed every one added since — including the only item with a live
      deadline and the live write-path-to-money row. A block that forbids copies and then hands out a
      stale one teaches the opposite of what it says. Found by the trading lane.
    ★★ The pointer is now LOCAL and LIVE. Whether the cloud artifact is unpublished is the operator's
      call; this only stops seven files pointing at it.
    ★★★ And it names WHICH SURFACE IS COMPLETE, because a count with no provenance is how two surfaces
      disagree without anyone noticing (FC-12's provenance half).
    """
    return f"""{BEGIN}
## OPERATOR BOARD — read this before you start work

Some things can only be done by the operator . They are tracked in one place for every team, so
that an item outstanding in one lane is not invisible in this one.

**The rule:** if anything is outstanding, **say so before doing other work.** Not refuse — say so, in one
short list, then carry on with what you were asked. He decides what to do about it.

**Read it live. Never from memory, and never from a copy pasted into a chat:**

    /usr/bin/python3 str(Path(__file__).resolve().parent.parent)/operator/blockers.py

It exits 1 while anything is open, and stays quiet on a clear board. The same items, rendered, are at
`{board_url}` — that page is generated per request, so it cannot go stale.

**★ SAY WHERE YOUR NUMBER CAME FROM.** The board draws from five sources: hand-written rows plus items
derived from the findings ledger, invariant FAILs, approval requests and the requirements register. If you
quote a count, name the surface you read it from — *"blockers.py reports N"* — not a bare total.

**★★ ABSENCE OF ACCESS IS NOT ABSENCE OF CONTENT.** If a source cannot be read, say **UNREADABLE**, never
"none". The board does this itself: the approval-request row says *invisible to the board — not absent,
unreadable*, because the directory is root-owned. A lead that reports "nothing outstanding" after failing
to look has told the operator he is free when nobody knows.

**★ IF YOU FIND A SERVICE HE MUST LOG INTO, WRITE THE ADVICE, NOT JUST THE ROW.** Add an entry to
`operator/credential_advice.json` — what it unlocks, the narrowest scope that works, and the pitfall
before he hits it. The guided run reads that file and hands him a burst at a time. A row tells him
something is needed; the advice is the part he cannot look up faster than you can. See
`operator/HOW_TO_WRITE_CREDENTIAL_ADVICE.md`.

**Never mark an item done on his behalf**, and never add one silently — an agent may ADD an item it
discovered (`blockers.py --add`, JSON on stdin), but only he closes one. If you believe an item is
already resolved, say why and leave it open.
{END}"""


def load_targets() -> dict:
    with open(TARGETS) as f:
        return json.load(f)


def board_url() -> str:
    with open(BLOCKERS) as f:
        # ★ LOCAL AND LIVE. `board_url` in the store points at a cloud artifact — a snapshot that had
        #   drifted to 10 of 21 items. A block telling leads never to use a copy must not end in one.
        return json.load(f).get("panel_url_local", "http://127.0.0.1:7842/operator/board")


def resolve(team_id: str):
    """Workspace comes from the LIVE db, never from this file — two of the targets sit in
    `hermes-temp-*` directories that change whenever the team is rebuilt."""
    if not AIONUI_DB.exists():
        return None, None
    con = sqlite3.connect(f"file:{AIONUI_DB}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT name, workspace FROM teams WHERE id LIKE ?", (team_id + "%",)).fetchone()
    finally:
        con.close()
    return (row[0], row[1]) if row else (None, None)


def hermes_threat_patterns():
    """★ Verify with the REAL scanner, read out of hermes's own source — not a copy of its rules kept
    here, which would pass forever after hermes tightened them."""
    if not HERMES_PROMPT_BUILDER.exists():
        return None
    tree = ast.parse(HERMES_PROMPT_BUILDER.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_CONTEXT_THREAT_PATTERNS" for t in node.targets
        ):
            return [p for p, _ in ast.literal_eval(node.value)]
    return None


def shadowing_hermes_md(ws: Path):
    """Any .hermes.md / HERMES.md at or above the workspace outranks AGENTS.md and silences it."""
    cur = ws.resolve()
    hits = []
    for d in [cur, *cur.parents]:
        for n in CTX_NAMES_HERMES:
            if (d / n).is_file():
                hits.append(str(d / n))
        if (d / ".git").exists():
            break
    return hits



def live_pinned_ids():
    """★ THE DRIFT THIS CATCHES: he pins a new team in the sider and it silently has no surface.
    The pin list lives in renderer localStorage, so it can only be read while the app is up — a
    check that cannot run says so; it does not pass by default."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "cdp"))
        from cdp import C  # noqa: E402
        c = C()
        try:
            raw = c.ev("localStorage.getItem('team-pinned-ids')")
        finally:
            c.close()
        return json.loads(raw) if raw else []
    except Exception as e:
        return None if "refus" in str(e).lower() or "Connection" in str(e) else None


# ★ MATCH ON THE STABLE PREFIX, NEVER THE VERSIONED LITERAL.
#   `BEGIN` used to be the exact string "<!-- ...BEGIN v1 -->" and install_one searched for it verbatim.
#   The trading lane bumped its own marker to "BEGIN v2 (trading lane, CC 2026-08-29)". The verbatim
#   search then MISSED a block that was plainly there, fell through to the append branch, and wrote a
#   SECOND board block into a file that already had one — silently, reported as success.
#   An idempotent writer keyed on a mutable marker is not idempotent, and its failure mode here was
#   duplication rather than an error, which is the worse of the two. Prefix-match, and count.
MARK = re.compile(r"<!--\s*KHAOSS:OPERATOR-BOARD:BEGIN.*?<!--\s*KHAOSS:OPERATOR-BOARD:END\s*-->", re.S)
OWNED = re.compile(r"<!--\s*KHAOSS:OPERATOR-BOARD:BEGIN\s+v\d+\s*\(([^)]*)\)")


def install_one(ws: Path, url: str) -> str:
    f = ws / "AGENTS.md"
    new = block(url)
    if not f.exists():
        f.write_text(new + "\n")
        return "created"

    cur = f.read_text()
    found = MARK.findall(cur)

    if not found:
        # A team's own instructions come FIRST; the board block is appended so it never displaces them.
        f.write_text(cur.rstrip() + "\n\n" + new + "\n")
        return "appended to existing AGENTS.md"

    # ★★ A BLOCK ANOTHER LANE ANNOTATED IS NOT MINE TO OVERWRITE.
    #    A marker of the form "BEGIN v2 (trading lane, CC ...)" is a signature: someone tailored this
    #    block for their own lane and owns it. Rolling my generic text over it would delete work I did
    #    not do, in a workspace I do not own. Report it and leave it — the operator owns the rollout.
    owner = OWNED.search(cur)
    if owner:
        return f"SKIPPED — lane-owned block ({owner.group(1).strip()}); not mine to overwrite"

    out = MARK.sub(lambda m: new, cur)
    if len(found) > 1:
        # Collapse: keep the first, drop the rest. Say the number out loud — a duplicate that gets
        # quietly tidied teaches nobody that a writer was appending behind their back.
        first = True
        def keep(m):
            nonlocal first
            if first:
                first = False
                return new
            return ""
        out = MARK.sub(keep, cur)
        out = re.sub(r"\n{3,}", "\n\n", out)
        f.write_text(out)
        return f"updated + COLLAPSED {len(found) - 1} DUPLICATE BLOCK(S) — a previous run had been appending"
    if out == cur:
        return "unchanged"
    f.write_text(out)
    return "updated"


def main() -> int:
    a = sys.argv[1:]
    mode = "check"
    only = None
    if "--install" in a:
        mode = "install"
    if "--list" in a:
        mode = "list"
    if "--team" in a:
        only = a[a.index("--team") + 1]

    cfg = load_targets()
    url = board_url()
    pats = hermes_threat_patterns()
    rows, fails = [], 0

    for t in cfg["targets"]:
        if only and not t["team_id"].startswith(only):
            continue
        # ★ A TARGET MAY BE A WORKSPACE WITH NO TEAM ROW.
        #   Targets were team_id-only, so str(Path(__file__).resolve().parent.parent) — the dev workspace this board is
        #   BUILT in, and which carries an AGENTS.md of its own — was never a target. Its block was
        #   written by hand once and then nothing maintained it: when the cloud pointer was replaced
        #   everywhere else, that file kept the stale link, and it was the last one still carrying it.
        #   A file the generator does not own is a file that silently rots.
        if t.get("workspace"):
            name, ws = t["label"], t["workspace"]
        else:
            name, ws = resolve(t["team_id"])
        tag = f"{t['label'][:28]:30s}"
        # ★ A deliberate exclusion is reported BEFORE any failure check — an excluded target with a
        #   named reason is a decision on the record, not a broken row, and must not read as one.
        if not t.get("install", True):
            rows.append(f"  [SKIP] {tag} {t.get('skip_reason', 'excluded')}")
            continue
        if not name:
            rows.append(f"  [FAIL] {tag} no team row for {t['team_id'][:8]} — pinned or named, but gone")
            fails += 1
            continue
        if ws is None or not Path(ws).is_dir():
            rows.append(f"  [FAIL] {tag} workspace missing: {ws}")
            fails += 1
            continue
        W = Path(ws)
        if mode == "install":
            rows.append(f"  [ OK ] {tag} {install_one(W, url)} → {W}/AGENTS.md")
            continue
        if mode == "list":
            rows.append(f"  {tag} {W}")
            continue
        # --- check ---
        shadow = shadowing_hermes_md(W)
        f = W / "AGENTS.md"
        if shadow:
            rows.append(f"  [FAIL] {tag} SILENCED — {shadow[0]} outranks AGENTS.md (hermes loads one file)")
            fails += 1
            continue
        if not f.exists():
            rows.append(f"  [FAIL] {tag} no AGENTS.md in {W}")
            fails += 1
            continue
        body = f.read_text()
        # ★★★ THE WRITER LEARNED AND THE READER DID NOT — FC-15 surviving in the verifier.
        #   install_one() was fixed to find blocks by the MARK prefix regex, precisely so a lane that
        #   annotates its own marker ("BEGIN v2 (trading lane, CC 2026-08-29)") is recognised and its
        #   file protected. This check kept doing `BEGIN not in body` against the UNANNOTATED literal.
        #   Measured: the trading lane's correctly-owned file reported "carries no board block".
        #   ★ So the ownership protection PRODUCED the failure — the more correctly a lane owns its
        #     block, the more certainly this said it had none.
        #   ★★ And the hazard is not cosmetic: it surfaces a BLOCKING row that the sanctioned repair
        #     CANNOT clear, because --install refuses an owned file by design. The only way to silence
        #     it is to force, which destroys the thing the signature exists to protect. A false FAIL
        #     that only a destructive action can silence is worse than a false PASS.
        #   Writer and reader now share ONE definition of "a block is present". Found by the trading
        #   lane applying the audience rule to its own operator-facing surface.
        found = MARK.findall(body)
        if not found:
            rows.append(f"  [FAIL] {tag} AGENTS.md present but carries no board block")
            fails += 1
            continue
        if len(found) > 1:
            rows.append(f"  [FAIL] {tag} {len(found)} board blocks — a writer has been appending (FC-15)")
            fails += 1
            continue
        owner = OWNED.search(body)
        if owner:
            # ★ A lane that signed its block MAINTAINS it. Comparing it to this template would report
            #   STALE forever and send the operator to a repair that is correctly refused.
            rows.append(f"  [ OK ] {tag} lane-owned block ({owner.group(1).strip()}) — "
                        f"maintained by that lane, not compared to this template")
            continue
        cur = found[0]
        if cur.strip() != block(url).strip():
            rows.append(f"  [FAIL] {tag} board block is STALE (template or board_url moved) — rerun --install")
            fails += 1
            continue
        if pats:
            hit = [p for p in pats if re.search(p, body, re.IGNORECASE)]
            if hit:
                rows.append(f"  [FAIL] {tag} hermes would BLOCK this file as injection: {hit[0]}")
                fails += 1
                continue
        rows.append(f"  [ OK ] {tag} loaded from {W}")

    print(f"\n★ OPERATOR SURFACE — {mode} ({len([t for t in cfg['targets']])} targets)")
    print("\n".join(rows))

    if mode == "check":
        # the generated copy must not lag the store it renders
        if not BOARD_MD.exists():
            print("  [FAIL] OPERATOR_BOARD.md has never been generated — run blockers.py --board")
            fails += 1
        elif BOARD_MD.stat().st_mtime < BLOCKERS.stat().st_mtime:
            age = int(BLOCKERS.stat().st_mtime - BOARD_MD.stat().st_mtime)
            print(f"  [FAIL] OPERATOR_BOARD.md is {age}s older than blockers.json — regenerate (--board)")
            fails += 1
        else:
            print("  [ OK ] OPERATOR_BOARD.md is current with blockers.json")
        if pats is None:
            print("  [WARN] hermes prompt_builder.py not found — injection check was NOT run")
        pinned = live_pinned_ids()
        if pinned is None:
            print("  [WARN] AionUi is not reachable on CDP — pin drift NOT checked (not a pass)")
        else:
            known = {t["team_id"] for t in cfg["targets"]}
            missing = [p for p in pinned if p not in known]
            if missing:
                for m in missing:
                    nm, _ = resolve(m)
                    print(f"  [FAIL] a PINNED team has no operator surface: {nm or m[:8]} ({m[:8]})")
                fails += len(missing)
            else:
                print(f"  [ OK ] every pinned team ({len(pinned)}) is a known target")
        print(f"\n  {'FAIL' if fails else 'PASS'} — {fails} problem(s)")
    if mode == "install":
        print("\n  ★ A running lead does NOT pick this up mid-session: context files load at session start.")
        print("    It reaches each team on its next session (or after acp.reset-session on that seat).")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
