#!/usr/bin/env python3
"""full_sweep.py — point an agent at an area and have it look, systematically.

★★★ WHAT A SWEEP IS FOR. Everything on this board arrives because something raised it. Nothing goes
    looking. So the failures that survive here are the ones nobody's checker happens to cover: a row
    that has been true for three weeks and is now false, two rows that contradict each other, a
    control that was built and never wired, a claim written down and never measured. A sweep is the
    deliberate act of going and looking, in a named area, with a named lens.

★★ THE DEFAULT IS DELIBERATELY TIMID: report only, no writes. A sweep that edits the board on its own
    is an agent grading its own homework at scale — and the one thing this board is for is that he
    decides. `--act` exists and has to be asked for.

★ AREAS AND LENSES ARE ENUMERATED, NOT FREE TEXT. "Sweep everything" is how you get a sweep that
  looks at nothing in particular and reports that things seem fine. Each area names WHAT WILL BE READ
  and each lens names WHAT WOULD COUNT AS A FINDING, so a clean result means something.
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path
from urllib.request import urlopen, Request

HERE = Path(__file__).resolve().parent
PANEL = "http://127.0.0.1:7842"
HISTORY = HERE / "sweep_history.jsonl"

AREAS = {
    "board": {
        "label": "The board itself",
        "reads": "every open item: title, detail, origin, lane, priority, and how long it has been open",
        "why": "rows go stale silently — the thing they describe changes and the row does not",
    },
    "invariants": {
        "label": "Invariants",
        "reads": "check_invariants.sh results and the invariant registry",
        "why": "a red invariant nobody reads is the same as no invariant; a phantom one is worse",
    },
    "services": {
        "label": "Services and access",
        "reads": "the service probe results, and what each probe actually proves",
        "why": "'unproven' quietly reads as 'fine' on a glance",
    },
    "completions": {
        "label": "What has left the board",
        "reads": "the completion log — settled, source-cleared, hidden, and unexplained departures",
        "why": "a row leaving because its source broke looks exactly like a row being finished",
    },
    "vault": {
        "label": "The Obsidian vault",
        "reads": "objectives, their attachments, and whether their provenance still resolves",
        "why": "pointed-to files move; attachments outlive the paths that found them",
    },
    "code": {
        "label": "This project's own code",
        "reads": "operator/*.py and panel_server.py — routes, controls and their self-tests",
        "why": "controls get built and never wired; self-tests get written and never run",
    },
}

LENSES = {
    "stale": "Claims that were true when written and may not be now. Anything with a date, a count, "
             "or a 'currently' in it.",
    "contradiction": "Two things on the board, or in the code, that cannot both be true.",
    "completions": "Work that is actually finished and still occupying space, or departures that were "
                   "filed as done when their source merely broke.",
    "dead": "Affordances that exist and cannot work — buttons without handlers, routes without "
            "callers, links to things that are not installed.",
    "unproven": "Assertions with no measurement behind them. Especially ones phrased as findings.",
    "duplicates": "The same problem written twice under different names.",
}

DEPTHS = {
    "quick":    "Read what is already recorded. No commands run. Fast, and blind to anything the "
                "records do not already say.",
    "standard": "Read the records AND run the cheap checks that confirm them. The default.",
    "deep":     "Drive the actual controls and re-measure. Slow, and the only depth that catches a "
                "record which is confidently wrong.",
}

DEFAULT = {
    # ★ MY CHOSEN DEFAULT, and the reasoning is in the docstring above: the three areas where this
    #   project has actually been bitten, the three lenses that caught those bites, standard depth,
    #   and no writes.
    "areas": ["board", "invariants", "completions"],
    "lenses": ["stale", "contradiction", "completions"],
    "depth": "standard",
    "act": False,
    "route": None,
}


def options() -> dict:
    return {"areas": AREAS, "lenses": LENSES, "depths": DEPTHS, "default": DEFAULT,
            "note": ("The default is report-only. A sweep that writes to the board on its own is an "
                     "agent grading its own work at scale.")}


def _get(path: str, timeout: int = 120):
    with urlopen(f"{PANEL}{path}", timeout=timeout) as r:
        return json.load(r)


def gather(areas: list, depth: str) -> dict:
    """★ Collect the EVIDENCE first, in this process, so the agent is reading measurements rather than
    being asked to go and find some. An agent told to 'sweep the board' will read three rows and
    generalise; an agent handed 117 rows and a completion log will not."""
    out, problems = {}, []
    if "board" in areas:
        try:
            d = _get("/operator/board.json")
            out["board"] = {
                "count": len(d.get("items", [])),
                "health": d.get("source_health", []),
                "items": [{"n": i.get("n"), "id": i["id"], "title": i.get("title"),
                           "one": (i.get("one") or "")[:400], "source": i.get("source"),
                           "lane": i.get("lane"), "priority": i.get("priority"),
                           "origin_as_of": (i.get("origin") or {}).get("as_of")}
                          for i in d.get("items", [])],
            }
        except Exception as e:
            problems.append(f"board unreadable: {e}")
    if "invariants" in areas:
        try:
            d = _get("/operator/board.json")
            out["invariants"] = [i for i in d.get("items", []) if i.get("source") == "invariants"]
        except Exception as e:
            problems.append(f"invariants unreadable: {e}")
    if "services" in areas:
        try:
            out["services"] = _get("/operator/services", timeout=180)
        except Exception as e:
            problems.append(f"services unreadable: {e}")
    if "completions" in areas:
        try:
            sys.path.insert(0, str(HERE))
            import completion_log as cl
            out["completions"] = {"recent": cl.entries()[-40:], "now": cl.run(write=False)}
        except Exception as e:
            problems.append(f"completion log unreadable: {e}")
    if "vault" in areas:
        try:
            out["vault"] = _get("/operator/objectives")
        except Exception as e:
            problems.append(f"vault unreadable: {e}")
    if "code" in areas and depth != "quick":
        try:
            mods = sorted(p.name for p in HERE.glob("*.py"))
            selftests = []
            for m in mods:
                txt = (HERE / m).read_text(errors="replace")
                selftests.append({"module": m, "has_selftest": "def self_test" in txt,
                                  "lines": txt.count("\n")})
            out["code"] = {"modules": selftests}
        except Exception as e:
            problems.append(f"code scan failed: {e}")
    # ★ A source that could not be read is a VISIBLE hole, never a silent omission.
    out["_problems"] = problems
    return out


def compose(areas: list, lenses: list, depth: str, evidence: dict) -> str:
    L = ["You are running a FULL SWEEP over the KHAOSS operator board. This is a deliberate act of "
         "going and looking — nothing here raised a flag by itself.", ""]
    L.append("AREAS SWEPT:")
    for a in areas:
        L.append(f"  · {AREAS[a]['label']} — reads {AREAS[a]['reads']}")
        L.append(f"    why it is worth looking: {AREAS[a]['why']}")
    L.append("")
    L.append("WHAT WOULD COUNT AS A FINDING:")
    for l in lenses:
        L.append(f"  · {l}: {LENSES[l]}")
    L.append("")
    L.append(f"DEPTH: {depth} — {DEPTHS[depth]}")
    L.append("")
    L.append("RULES:")
    L.append("  · Report only. Do not change the board, the code, or any file.")
    L.append("  · A finding must name the specific row, file or claim. 'Several items look stale' is "
             "not a finding; it is an impression.")
    L.append("  · If a lens finds nothing, say so explicitly for that lens. A sweep that reports only "
             "hits cannot be distinguished from a sweep that did not look.")
    L.append("  · Distinguish measured from inferred, every time.")
    L.append("  · Rank findings by what it costs to leave them alone, not by how interesting they are.")
    if evidence.get("_problems"):
        L.append("")
        L.append("★ SOURCES THAT COULD NOT BE READ (these are holes in this sweep, not clean results):")
        for p in evidence["_problems"]:
            L.append(f"  · {p}")
    L.append("")
    L.append("EVIDENCE (this is what was gathered for you — read it, do not re-fetch it):")
    blob = json.dumps({k: v for k, v in evidence.items() if k != "_problems"}, default=str)
    CAP = 90000
    if len(blob) > CAP:
        # ★ SAY WHAT WAS CUT. A silently truncated prompt produces a sweep that confidently reports on
        #   the part it happened to receive, and "I read the board" becomes false without anyone
        #   noticing. The agent is told, in the prompt, that it is not seeing everything.
        L.append(f"★ THIS EVIDENCE WAS TRUNCATED: {len(blob):,} bytes gathered, {CAP:,} sent. You are "
                 f"NOT seeing the whole board. Say so in your report, and do not describe your "
                 f"coverage as complete.")
        blob = blob[:CAP]
    L.append(blob)
    return "\n".join(L)


def run(areas=None, lenses=None, depth=None, route=None, act=False, dry_run=False) -> dict:
    cfg = dict(DEFAULT)
    if areas:  cfg["areas"] = [a for a in areas if a in AREAS]
    if lenses: cfg["lenses"] = [l for l in lenses if l in LENSES]
    if depth:  cfg["depth"] = depth if depth in DEPTHS else DEFAULT["depth"]
    if route:  cfg["route"] = route
    cfg["act"] = bool(act)
    if not cfg["areas"]:
        raise ValueError("no valid area selected — choose from: " + ", ".join(AREAS))
    if not cfg["lenses"]:
        raise ValueError("no valid lens selected — choose from: " + ", ".join(LENSES))
    t0 = time.time()
    ev = gather(cfg["areas"], cfg["depth"])
    prompt = compose(cfg["areas"], cfg["lenses"], cfg["depth"], ev)
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "config": cfg,
           "evidence_bytes": len(prompt), "gathered_seconds": round(time.time() - t0, 1),
           "problems": ev.get("_problems", [])}
    if dry_run:
        rec["dry_run"] = True
        rec["prompt_head"] = prompt[:1200]
        return rec
    # ★ ONE DISPATCH PATH, shared with every per-item question. item_assist.ask_raw() is the same
    #   local / openrouter / agent routing that the per-row "AI assist" button uses; the sweep does
    #   not get its own copy, because two implementations of "how a question reaches a model" drift
    #   and the sweep would quietly stop honouring routing rules the button still obeyed.
    try:
        sys.path.insert(0, str(HERE))
        import item_assist as ia
        route = cfg["route"]
        if not route:
            # ★ THE ROUTES ARE GROUPED (local / cloud / agents), not a flat list. My first version
            #   read a "routes" key that does not exist and concluded there were none available —
            #   a sweep that reports "nothing to ask" while 53 routes sit there. Read the real shape.
            # ★★ LOCAL FIRST, deliberately: a sweep sends the whole board, so it is the most
            #   expensive question this board can ask, and the one most worth keeping on the machine.
            r = ia.routes()
            local = [x["id"] for x in (r.get("local") or [])]
            cloud = [x["id"] for x in (r.get("cloud") or [])]
            route = (local[0] if local else (cloud[0] if cloud else None))
            cfg["route"] = route
            cfg["route_chosen_because"] = ("no route was specified; picked a local model so the whole "
                                           "board does not leave the machine" if local else
                                           "no local model available; fell back to a paid cloud route")
        if not route:
            rec["ok"] = False
            rec["error"] = ("no model or agent route is available — the evidence was gathered but "
                            "there is nothing to ask")
        else:
            rec["answer"] = ia.ask_raw(route, prompt)
            rec["ok"] = bool(rec["answer"].get("ok"))
            if not rec["ok"]:
                rec["error"] = ("the sweep gathered its evidence but the route failed: "
                                + str(rec["answer"].get("error"))[:200])
    except Exception as e:
        rec["ok"] = False
        rec["error"] = f"the sweep gathered its evidence but dispatch failed: {e}"
    with HISTORY.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    return rec


def self_test() -> int:
    fails = []
    o = options()
    if o["default"]["act"] is not False:
        fails.append("the default is not report-only — a sweep must not write unless asked")
    for a in o["default"]["areas"]:
        if a not in AREAS: fails.append(f"default area {a} is not a real area")
    for l in o["default"]["lenses"]:
        if l not in LENSES: fails.append(f"default lens {l} is not a real lens")
    try:
        run(areas=["nonsense"], dry_run=True); fails.append("accepted an unknown area")
    except ValueError:
        pass
    try:
        run(areas=["board"], lenses=["nonsense"], dry_run=True); fails.append("accepted an unknown lens")
    except ValueError:
        pass
    p = compose(["board"], ["stale"], "quick", {"board": {"count": 1}, "_problems": ["x unreadable"]})
    if "not a clean result" not in p and "holes in this sweep" not in p:
        fails.append("a source that could not be read is not surfaced in the prompt")
    if "Report only" not in p:
        fails.append("the prompt does not tell the agent it is report-only")
    if "say so explicitly for that lens" not in p:
        fails.append("the prompt does not require a lens to report finding nothing")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: default is report-only · default areas and lenses are real · "
              "unknown area refused · unknown lens refused · unreadable sources surfaced as holes · "
              "empty lenses must report themselves")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if "--options" in a:
        print(json.dumps(options(), indent=2)); return 0
    def val(flag, default=None):
        return a[a.index(flag) + 1] if flag in a and len(a) > a.index(flag) + 1 else default
    areas = (val("--areas") or "").split(",") if val("--areas") else None
    lenses = (val("--lenses") or "").split(",") if val("--lenses") else None
    r = run(areas=areas, lenses=lenses, depth=val("--depth"), route=val("--route"),
            act="--act" in a, dry_run="--dry-run" in a)
    if "--json" in a:
        print(json.dumps(r, indent=2, default=str)); return 0
    print(f"\n  FULL SWEEP · {r['at']}")
    print(f"    areas  : {', '.join(r['config']['areas'])}")
    print(f"    lenses : {', '.join(r['config']['lenses'])}")
    print(f"    depth  : {r['config']['depth']}   writes: {'YES' if r['config']['act'] else 'no'}")
    print(f"    gathered {r['evidence_bytes']:,} bytes in {r['gathered_seconds']}s")
    for p in r.get("problems", []):
        print(f"    ★ hole: {p}")
    if r.get("dry_run"):
        print("\n  --- prompt head ---")
        print("  " + r["prompt_head"][:900].replace("\n", "\n  ")); print()
        return 0
    if r.get("ok"):
        print("\n  --- what it found ---")
        print("  " + json.dumps(r.get("answer"), default=str)[:2500].replace("\\n", "\n  "))
    else:
        print(f"\n  {r.get('error')}")
    print()
    return 0 if r.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
