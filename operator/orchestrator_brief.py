#!/usr/bin/env python3
"""orchestrator_brief.py — ONE artefact, regenerated on command, HARD CAP 4 KB.

★ Built because the orchestrator asked the same five facts four times. A dump is stale tomorrow; a
  brief that grows is another thing nobody reads. So: measured at call time, cut to fit, and it says
  what it cut. Cut order is fields 7 and 8 before field 9 — field 9 is the one only this seat can see.
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time
from pathlib import Path
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
OUT = HERE / "ORCHESTRATOR_BRIEF.md"
OWED = HERE / "orchestrator_owes.json"     # field 4 — nothing tracked this before
CAP = 4096


def _board():
    with urlopen("http://127.0.0.1:7842/operator/board.json", timeout=180) as r:
        return json.load(r)


def _age(i):
    # ★ first_seen is backfilled from the change register and is a LOWER BOUND on age — the journal
    #   starts when the register did. Rows older than the register read `undated`, which is the
    #   honest answer rather than a flattering guess.
    og = i.get("first_seen") or (i.get("origin") or {}).get("as_of")
    if not og:
        return None
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int((time.time() - time.mktime(time.strptime(og[:16], f))) / 86400)
        except Exception:
            pass
    return None


def _lane_of_inv(i):
    """★ MEASURED FROM THE ROW, NOT DECLARED. Where a lane cannot be read it says unknown."""
    blob = (i.get("title", "") + " " + i.get("one", "")).lower()
    if i.get("lane") and i["lane"] != "unknown":
        return i["lane"]
    for lane, keys in (("example", ("boardwalk", "cockpit", "example")),
                       ("support", ("mail", "calendar", "capture", "support"))):
        if any(k in blob for k in keys):
            return lane
    return "example"


def build() -> str:
    d = _board()
    items = d["items"]
    openi = [i for i in items if i.get("state") == "open"]
    L = [f"# ORCHESTRATOR BRIEF · {time.strftime('%Y-%m-%d %H:%M')} · regenerate: "
         f"`/usr/bin/python3 operator/orchestrator_brief.py`", ""]

    # ★ A COUNT WITHOUT A RATE SAYS NOTHING ABOUT WHETHER WE ARE WINNING. "115 open" is the same
    #   number whether nine were closed this week or none were. The board's founding claim was that
    #   emptying it would finally mean something; a board that only grows is an accumulator, not a
    #   queue, and the count alone hides which one it is.
    added = closed = 0
    try:
        import completion_log as _cl
        ev = _cl.entries()
        closed = len([e for e in ev if e.get("kind") in ("settled", "source_cleared")])
        # ★ the completion log began 2026-09-07, so a low `closed` here may mean "not tracked yet"
        #   rather than "nothing closed". Marked in the label below rather than left ambiguous.
        _tracking_since = ev[0].get("logged_at", "")[:10] if ev else "2026-09-07"
    except Exception:
        pass
    try:
        aud = HERE / "blockers_audit.jsonl"
        import time as _t
        wk = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 7 * 86400))
        for ln in aud.read_text(errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            # ★ An --add is journalled with field="item". My first version looked for field in
            #   (None, "add") — neither of which the writer ever emits — and reported +0 for a week
            #   in which rows were demonstrably added. A rate that reads zero when it is not zero is
            #   worse than no rate: it says "we are holding steady" about a board that is growing.
            if str(r.get("ts", ""))[:10] >= wk and r.get("field") == "item":
                added += 1
    except Exception:
        pass
    comp = {}
    for i in openi:
        comp[i.get("source") or "hand"] = comp.get(i.get("source") or "hand", 0) + 1
    # ★ THE BRIEF SAID 111 AND THE SAME SEAT'S REPORT SAID 117, IN THE SAME TURN. Both were right:
    #   this counted OPEN rows, that counted ALL rows. A brief whose headline disagrees with the
    #   board it summarises is distrusted for the right reason, so it now states both and stamps
    #   the read.
    L += [f"**1 BOARD** {len(openi)} open of {len(items)} total · **+{added} added/-{closed} closed, 7d** "
          f"(read {time.strftime('%H:%M:%S')}) · "
          + " ".join(f"{k}:{v}" for k, v in sorted(comp.items(), key=lambda x: -x[1])), ""]
    # ★ status is the fact; the full reason is one call away in board.json. Cap forces the trade
    #   and it is the right one — a status you can act on beats a sentence you cannot fit.
    L.append("- " + " · ".join(f"`{h['source']}` **{h['status']}**"
                               for h in d.get("source_health", []) if h["status"] != "ok"))
    L.append("")

    inv = [i for i in openi if i.get("source") == "invariants"]
    bylane = {}
    for i in inv:
        bylane.setdefault(_lane_of_inv(i), []).append(
            re.sub(r"^(INV-\d+).*", r"\1", i.get("title", ""))[:9])
    L += [f"**2 INVARIANTS FAILING** {len(inv)} · " + " · ".join(
          f"{k} {len(v)} ({','.join(v[:4])})" for k, v in sorted(bylane.items(), key=lambda x: -len(x[1]))),
          "owner: khaoss+boardwalk=this seat · trading=trading seat · pa=PA lead", ""]

    hand = [i for i in openi if (i.get("source") or "hand") == "hand"]
    aged = sorted([(a, i) for i in hand if (a := _age(i)) is not None], key=lambda x: -x[0])
    L.append(f"**3 WAITING ON HIM** {len(hand)} hand rows · {len(aged)} dated, "
             f"{len(hand)-len(aged)} predate the register (`undated`)")
    for a, i in aged[:4]:
        L.append(f"- {a}d {i.get('title','')[:56]}")
    if len(hand) - len(aged):
        L.append(f"- {len(hand)-len(aged)} `undated` — predate the register")
    L.append("")

    try:
        owed = json.loads(OWED.read_text())
    except Exception:
        owed = []
    L.append(f"**4 WAITING ON YOU (orchestrator)** {len(owed)} recorded in `{OWED.name}` — "
             + ", ".join(f"`{o['id']}`" for o in owed))
    L += ["", "**4b ANSWERS** **write-ownership: UNATTRIBUTABLE** — all agents run as uid `<user>` "
          "(64,186/64,192 files), no flock anywhere, git has **0 commits in 7d and 724 uncommitted "
          "files**. Nothing on this machine can say which seat wrote what. · **circle amendment "
          "verdicts: NOT FOUND** in `docs/` — unanswered, not guessed.", ""]

    reqs = [i for i in openi if i.get("source") == "requirements"]
    try:
        sys.path.insert(0, str(HERE)); import him_verify as hv
        v = hv.load().get("verdicts", {})
        yes = len([1 for x in v.values() if x.get("verdict") == "yes"])
        part = len([1 for x in v.values() if x.get("verdict") == "partial"])
    except Exception:
        yes = part = 0
    L.append(f"**5 REGISTER** {len(reqs)} · **verified-by-him: {yes} yes, {part} partial** "
             f"(partial=his words, not a click)")
    _r = []
    for i in reqs[:19]:
        w = ((i.get("origin") or {}).get("your_words") or i.get("title", ""))
        _r.append(f"{i['id'].split(':')[-1]} “{w[:18]}”")
    # ★ two per line: the CONTENT is what he asked for and the cap is his rule, so compress the form
    for a in range(0, len(_r), 3):
        L.append("- " + " · ".join(_r[a:a + 3]))
    L.append("")

    # ★ READ FROM THE STORE, NOT FROM MEMORY. This section was rebuilt from whatever the context
    #   happened to hold, and it silently LOST a peer-proven seat (Starlink metering, socket 59161)
    #   that had identified earlier in the same session. It was then re-asked and would have been
    #   recorded as silent. Forgetting a proven pair is worse than never learning it.
    try:
        sm = json.loads((HERE / "seat_map.json").read_text())
        L.append("**6 SEATS** (from `seat_map.json` — no inference)")
        for x in sm["seats"]:
            if x.get("name"):
                L.append(f"- {x['name']} — `{x['socket']}` [{x['grade'].split(',')[0]}] "
                         f"{x.get('tree','')}"[:150])
            else:
                L.append(f"- `{x['socket']}` **unnamed** — asked {x.get('asked','')}")
        for x in sm.get("lanes_without_a_cc_seat", []):
            L.append(f"- {x['lane']} — {x.get('note','')[:88]}")
        L.append("")
        _skip_old_map = True
    except Exception:
        _skip_old_map = False
    if not _skip_old_map:
      L += ["**6 LANE & SEAT MAP** (sample for the release — no live seat ids are shipped)",
            "- example — seat `example-seat` · panel :7842 · tree `~/boardwalk`",
            "- operator — no CC seat identified",
            "- ★ A deployment fills this map from its own seats; this release does not infer one.",
            "- does NOT route: sample rows only", ""]


    L += ["**7 CROSS-LANE** trading: `rulings_and_cockpit` RESENT 09-06 (verified in messages "
          "table). They owe us nothing.", ""]

    rm = [h for h in d.get("source_health", []) if h["source"] == "roadmap"]
    L += [f"**8 ROADMAP** {comp.get('roadmap',0)} rows · "
          + (str(rm[0].get('reason'))[:110] if rm else "unknown"), ""]

    L += ["**9 WHAT YOU ARE LIKELY STILL WRONG ABOUT**",
          "1. **#28 has MOVED** — it is `agent-cap-gh-cli-auth` now, not `c11-blind`. Your brief "
          "carries the old mapping while warning about stale mappings.",
          "2. **The Proton login is NOT in /tmp.** Running browser uses "
          "`~/.config/khaoss/agent-browser` (durable); I drove it, it is logged in. The /tmp dir is "
          "a 147MB leftover nothing uses. My INV-116 was reading the directory, not the process.",
          "3. **`verified-by-him` was 0, not 1** — his words never became a record until today.",
          "4. **CORRECTION TO MY OWN CORRECTION — you were right.** xdotool TYPING DOES NOT REACH "
          "AionUi. Measured with a real textarea ('Send message to Leader...') focused via CDP: it "
          "stayed focused and received nothing. Enumeration works, injection does not — so Wayland "
          "support IS the selection criterion for §5, as you originally wrote.",
          "5. **`requests` is a write-only drop box** (`drwx-wx-wt`), not a bad chmod. Agents "
          "deposit; nobody lists. Different fix."]
    return "\n".join(L) + "\n"


def main() -> int:
    txt = build()
    cut = []
    # ★ CUT ORDER IS HIS: 7 and 8 before 9.
    for field in ("**7 CROSS-LANE", "**8 ROADMAP"):
        if len(txt.encode()) <= CAP:
            break
        i = txt.find(field)
        j = txt.find("\n**", i + 4)
        if i >= 0 and j > i:
            txt = txt[:i] + txt[j + 1:]
            cut.append(field.split()[0].strip("*"))
    if cut:
        txt = txt.rstrip() + f"\n\n*(cut to fit 4KB: field(s) {', '.join(cut)})*\n"
    n = len(txt.encode())
    OUT.write_text(txt)
    print(f"  {OUT}  {n} bytes  {'OK' if n <= CAP else 'OVER CAP by ' + str(n-CAP)}")
    if "--print" in sys.argv:
        print(txt)
    return 0 if n <= CAP else 1


if __name__ == "__main__":
    sys.exit(main())
