#!/usr/bin/env python3
"""★ THE DERIVED BOARD — items come from the four sources, not from someone remembering to type them.

★ WHY THIS IS THE ITEM THAT MAKES IT A WORKFLOW: while the board is hand-authored, CLEARING IT MEANS
  NOTHING. A new operator-blocking finding does not reach it, so he can empty the board and still be
  blocked. A queue you can empty that means nothing is a chore list, not an instrument.

★★ AND THE THING THE SPEC DID NOT KNOW: two of the four sources cannot contribute today and a third
  lacks the field the filter keys on. A derivation that silently reads zero from three sources produces
  AN EMPTY BOARD THAT MEANS NOTHING — the same false clear as a board that crashes (FC-4), arriving by
  omission instead of by exception. **So every source reports its own health, and an unavailable source
  becomes a VISIBLE ROW rather than a silent zero.**

THE FOUR, as they actually are (measured 2026-08-28):
  requests   /var/lib/khaoss/requests/     → PERMISSION DENIED (root-owned; needs the privileged broker)
  findings   docs/findings_rows.json       → 58 rows, 25 open — but NO `owner` field exists, so
                                             "rows whose owner is the operator" cannot be filtered
  red_rows   docs/red_rows.json            → EMPTY (0 rows) — empty is not broken, and is reported as empty
  invariants reports/invariant_board_history.jsonl → verdicts recorded; FAILs derivable
"""
from __future__ import annotations
import json, os, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUESTS = Path("/var/lib/khaoss/requests")
FINDINGS = ROOT / "docs" / "findings_rows.json"
RED_ROWS = ROOT / "docs" / "red_rows.json"
INV_HIST = ROOT / "reports" / "invariant_board_history.jsonl"


def _rows(p: Path):
    d = json.loads(p.read_text())
    return d if isinstance(d, list) else (d.get("rows") or d.get("findings") or d.get("items") or [])


def from_requests():
    """Pending REQUIRE_APPROVAL requests. Every request he has ever received arrived inside a report."""
    if not REQUESTS.exists():
        return [], {"source": "requests", "status": "absent", "reason": f"{REQUESTS} does not exist", "count": 0}
    # ★ TEST READABILITY EXPLICITLY. glob() on an unreadable directory yields NOTHING WITHOUT RAISING,
    #   so relying on the exception reported "ok, 0 items" for a directory this process cannot open —
    #   a silent zero on an unreadable source, which is the single failure this module exists to prevent.
    #   Caught because the health line contradicted a plain `ls` two minutes earlier.
    try:
        if not os.access(REQUESTS, os.R_OK | os.X_OK):
            raise PermissionError(f"cannot read {REQUESTS}")
        pend = [f for f in REQUESTS.iterdir() if f.suffix == ".json"]
    except PermissionError:
        # ★ VISIBLE, NOT SILENT. An unreadable source is not an empty one, and the difference is the
        #   whole reason this board exists.
        return ([{
            "id": "derived:requests:unreadable", "sev": "hold", "derived": True, "source": "requests",
            "title": "The approval-request channel cannot be read from here",
            "one": (f"{REQUESTS} is root-owned and this process cannot list it (permission denied). Any "
                    f"pending REQUIRE_APPROVAL request is therefore INVISIBLE to the board — not absent, "
                    f"unreadable. Reading it needs the privileged broker."),
            "blocks": "the board cannot show you a request that is waiting; it can only tell you it cannot look",
            "origin": {"what": "the approval-request queue could not be READ",
                       "where": str(REQUESTS), "kind": "unreadable",
                       "raised_by": "the board itself, refusing to report a silent zero",
                       "how_to_clear": "make the directory readable, then this row is replaced by whatever it actually contains"}
        }], {"source": "requests", "status": "unavailable", "reason": "permission denied", "count": 0})
    items = []
    for f in sorted(pend):
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        if str(r.get("state", "pending")).lower() != "pending":
            continue
        items.append({
            "id": f"derived:request:{f.stem}", "sev": "hold", "derived": True, "source": "requests",
            "origin": {"what": "an approval request waiting in the request queue",
                       "where": str(REQUESTS / f.name), "kind": "file",
                       "raised_by": "whatever asked for the permission",
                       "how_to_clear": "grant or refuse it; the row disappears when the file does"},
            "title": f"Approval requested: {str(r.get('what') or f.stem)[:80]}",
            "one": str(r.get("reason") or "(no reason recorded)")[:400],
            "blocks": str(r.get("blocks") or "the agent that filed it"),
        })
    return items, {"source": "requests", "status": "ok", "reason": "", "count": len(items)}


def from_findings():
    if not FINDINGS.exists():
        return [], {"source": "findings", "status": "absent", "reason": "no findings_rows.json", "count": 0}
    rows = _rows(FINDINGS)
    has_owner = any("owner" in r for r in rows)
    open_broken = [r for r in rows if str(r.get("status")).lower() == "open"
                   and str(r.get("class")).lower() == "broken"]
    items = [{
        "id": f"derived:finding:{r.get('id')}", "sev": "hold", "derived": True, "source": "findings",
        "origin": {"what": f"finding {r.get('id')} in the findings ledger",
                   "where": str(FINDINGS), "kind": "ledger", "row": r.get("id"),
                   "raised_by": r.get("owner") or r.get("by") or "not recorded in the ledger row",
                   "how_to_clear": "resolve the finding in the ledger; this row follows it"},
        "title": f"{r.get('id')} — {str(r.get('title'))[:90]}",
        "one": (f"An OPEN finding of class '{r.get('class')}' from cohort {r.get('cohort')}. "
                f"Evidence: {r.get('evidence') or '(none recorded)'}"),
        "blocks": "nothing mechanically — it is an open defect nobody has closed",
    } for r in open_broken]
    health = {"source": "findings", "status": "ok" if has_owner else "degraded",
              "reason": "" if has_owner else
                        "NO `owner` field exists on any row, so 'findings the operator owns' cannot be "
                        "filtered — every OPEN+BROKEN row is offered instead, which is wider than asked for",
              "count": len(items)}
    return items, health


def from_red_rows():
    if not RED_ROWS.exists():
        return [], {"source": "red_rows", "status": "absent", "reason": "no red_rows.json", "count": 0}
    rows = _rows(RED_ROWS)
    if not rows:
        return [], {"source": "red_rows", "status": "empty",
                    "reason": "0 rows — EMPTY IS NOT BROKEN, and it is reported as empty so a quiet "
                              "source cannot be mistaken for a healthy one", "count": 0}
    items = [{
        "id": f"derived:red:{r.get('id') or i}", "sev": "stop", "derived": True, "source": "red_rows",
        "title": str(r.get("title") or r.get("what") or f"red row {i}")[:90],
        "one": str(r.get("detail") or r.get("one") or "")[:400],
        "blocks": str(r.get("blocks") or ""),
    } for i, r in enumerate(rows)]
    return items, {"source": "red_rows", "status": "ok", "reason": "", "count": len(items)}


def from_invariants():
    """Outstanding invariant FAILs, on BOTH freshness axes.

    ★ FAILs from the most recent run, PLUS any previously-failing invariant the most recent run did
      not evaluate (carried forward as NOT RE-EVALUATED), PLUS a row when the suite's coverage
      shrank. An old FAIL that has since PASSED is not outstanding; an old FAIL that was never
      re-checked very much is."""
    if not INV_HIST.exists():
        return [], {"source": "invariants", "status": "absent", "reason": "no board history", "count": 0}
    rows = []
    for line in INV_HIST.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    if not rows:
        return [], {"source": "invariants", "status": "empty", "reason": "history file is empty", "count": 0}
    last_run = rows[-1].get("run")
    fails = [r for r in rows if r.get("run") == last_run and r.get("verdict") == "FAIL"]

    # ★★★ COVERAGE IS THE SECOND FRESHNESS AXIS, AND IT WAS COMPLETELY UNGUARDED.
    #   The staleness block below asks "how OLD is the last run". It never asks "how MUCH did the
    #   last run actually check". Those are different questions and only one was being answered.
    #   ★★ WHAT HAPPENED: a 66-check run followed a 100-check run. Keeping only the newest run's
    #      FAILs silently dropped SEVEN failing invariants — INV-94, 95, 100, 101, 111, 112, 114 —
    #      none fixed, none re-evaluated, no row anywhere. The 66-check run was not a smaller suite;
    #      its ids are an exact PREFIX of the 100-check run ending at INV-77, and it finished in 25s
    #      against 269s. It was a TRUNCATED run, and truncation rendered as a cleaner board.
    #   ★★★ THE RULE THIS ENCODES: ABSENCE OF A CHECK MUST NOT RENDER AS ABSENCE OF A PROBLEM.
    #      A previously-FAILING invariant that the latest run did not evaluate carries forward as
    #      NOT RE-EVALUATED. Its true state is unknown, and unknown is not clear.
    #   ★ This is purely ADDITIVE — it can only surface rows, never hide one. That is deliberate:
    #     a bug in this block should make the board noisier, never quieter.
    _evaluated_now = {r.get("id") for r in rows if r.get("run") == last_run}
    _last_verdict = {}                      # id -> (run, verdict, row) from the newest run naming it
    for r in rows:
        if r.get("id"):
            _last_verdict[r["id"]] = r
    _carried = [r for _id, r in sorted(_last_verdict.items())
                if r.get("verdict") == "FAIL" and _id not in _evaluated_now]
    for r in _carried:
        fails = fails + [{
            "id": r.get("id"), "verdict": "FAIL",
            "desc": f"NOT RE-EVALUATED — {str(r.get('desc') or '')[:70]}",
            "evidence": (f"This invariant FAILED on run {r.get('run')} and the most recent run "
                         f"({last_run}) DID NOT EVALUATE IT. It was not fixed and it did not pass — "
                         f"it was not checked. Its true state is UNKNOWN. "
                         f"Re-run the full suite: bash check_invariants.sh"),
        }]

    # ★ AND THE REGRESSION ITSELF GETS A ROW. Without this, the seven carried rows above appear with
    #   no explanation of WHY the suite stopped covering them, and the next short run does it again.
    _runs_seen, _counts = [], {}
    for r in rows:
        if r.get("run") not in _counts:
            _runs_seen.append(r.get("run"))
        _counts[r.get("run")] = _counts.get(r.get("run"), 0) + 1
    if len(_runs_seen) >= 2:
        _now_n, _prev_n = _counts.get(last_run, 0), _counts.get(_runs_seen[-2], 0)
        if _now_n < _prev_n:
            fails = fails + [{
                "id": "INV-COVERAGE-REGRESSION", "verdict": "FAIL",
                "desc": "the last invariant run checked FEWER things than the run before it",
                "evidence": (f"run {last_run} evaluated {_now_n} checks; the previous run "
                             f"({_runs_seen[-2]}) evaluated {_prev_n}. {_prev_n - _now_n} check(s) "
                             f"were not run. A shorter run makes the board look cleaner without "
                             f"anything having been fixed — which is why this row exists. "
                             f"Re-run the full suite and confirm the count returns to {_prev_n}."),
            }]

    # ★★★ A DERIVED INVARIANT ROW IS ONLY AS FRESH AS THE LAST SUITE RUN, AND NOTHING SAID SO.
    #   Two directions, and the second is the dangerous one:
    #     · a FAIL since FIXED keeps appearing until someone re-runs — annoying, visible, self-correcting
    #     · a PASS since BROKEN does not appear AT ALL — a false clear with a timestamp nobody can see
    #   The board renders "FAILs from the most recent run", which reads as "current" and is not. Raised
    #   by the trading lane after my INV-110 fix passed while their operator page still showed the FAIL
    #   from a run twenty minutes older than the fix.
    #   ★ STALE_AFTER is an EMPIRICAL LINE, not a derived one: the suite takes >2 min so it is not run
    #     casually, and a working day is the interval over which "nobody has looked" starts to matter.
    #     Labelled so nobody mistakes it for a computed threshold.
    import time as _t
    last_ts = max((r.get("ts") or 0) for r in rows if r.get("run") == last_run)
    age_s = int(_t.time()) - int(last_ts or 0)
    as_of = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(last_ts)) if last_ts else "unknown"
    STALE_AFTER = 4 * 3600
    age_txt = f"{age_s // 3600}h{(age_s % 3600) // 60:02d}m" if age_s >= 3600 else f"{age_s // 60}m"

    _AS_OF = f"  ·  as of {as_of} ({age_txt} ago)"

    if age_s > STALE_AFTER:
        # The false clear, made into a visible row. Without this, "no invariant FAILs on the board"
        # is indistinguishable from "nobody has checked since yesterday".
        fails = fails + [{"id": "INV-SOURCE-STALE", "verdict": "FAIL",
                          "desc": "the invariant suite has not run recently",
                          "evidence": (f"last run {as_of} ({age_txt} ago). Invariant rows on this board "
                                       f"are from THAT run. Anything that has broken since is NOT here — "
                                       f"this is a clear board on that axis only because nobody looked. "
                                       f"Run: bash check_invariants.sh")}]
    age_h = round((time.time() - (rows[-1].get("ts") or 0)) / 3600.0, 1)
    items = [{
        "id": f"derived:inv:{r.get('id')}", "sev": "stop", "derived": True, "source": "invariants",
        "origin": {"what": f"{r.get('id')} failed on the last invariant run",
                   "where": "check_invariants.sh  ·  history: reports/invariant_board_history.jsonl",
                   "kind": "check", "row": r.get("id"), "as_of": as_of,
                   "raised_by": "the invariant suite",
                   "how_to_clear": f"fix what {r.get('id')} guards, then re-run: bash check_invariants.sh"},
        "title": f"{r.get('id')} FAIL — {str(r.get('desc'))[:80]}",
        # ★ THE STAMP GOES ON THE ROW, not only in the health dict. A health field is read by the
        #   panel's source-health box; the ROW is what the operator actually looks at, and without a
        #   date on it a twenty-minute-old verdict and a two-day-old one look identical.
        "one": str(r.get("evidence") or "(no evidence recorded)")[:400] + _AS_OF,
        "blocks": "an invariant is failing; whatever it guards is unguarded",
        "as_of": as_of, "age_seconds": age_s,
    } for r in fails]
    return items, {"source": "invariants",
                   # ★ 48h was far too lax for the direction that matters. A break introduced this
                   #   morning is invisible until the next run, so the honest line is hours not days.
                   "status": "ok" if age_s <= STALE_AFTER else "stale",
                   "reason": "" if age_s <= STALE_AFTER else
                             f"the last recorded run is {age_txt} old — these FAILs may be neither "
                             f"current nor complete, and anything broken since is NOT listed",
                   "count": len(items), "run": last_run, "age_hours": age_h}


REGISTER = ROOT / "operator" / "requirements_register.json"


def from_requirements():
    """★ THE FIFTH SOURCE — the board's mirror image. The board lists what needs HIS attention; the
    register lists what HE ASKED FOR that needs the system's attention. It exists because his feature
    requests were prose in dispatches: nothing enumerated them, nothing asserted them, and nothing failed
    when one went missing — the only detector was him looking, and he looked three times.
    ★★ Only asks NOT YET verified BY HIM surface. `verified-by-machine` is not done: a machine driving a
    control proves the control reacts, not that the feature is what he wanted."""
    if not REGISTER.exists():
        return [], {"source": "requirements", "status": "absent", "reason": "no register", "count": 0}
    reqs = json.loads(REGISTER.read_text()).get("requirements", [])
    openish = [r for r in reqs if r.get("state") != "verified-by-him"]
    sev = {"asked": "hold", "built": "info", "verified-by-machine": "info"}
    items = [{
        "id": f"derived:req:{r['id']}", "sev": sev.get(r.get("state"), "info"),
        "origin": {"what": f"{r['id']} — something YOU asked for, tracked until you say it works",
                   "where": str(REGISTER), "kind": "register", "row": r["id"],
                   "raised_by": "you",
                   "your_words": (r.get("verbatim") or "")[:300],
                   "how_to_clear": "rule on it in the board's test panel — only your verdict closes it"},
        "derived": True, "source": "requirements",
        "title": f"{r['id']} · {r.get('state')} — \u201c{r.get('verbatim','')[:70]}\u2026\u201d",
        "one": (f"HIS WORDS: \u201c{r.get('verbatim','')}\u201d\n\nHOW IT IS PROVEN: {r.get('test','(no test)')}"
                + (f"\n\n\u2605 {r['note']}" if r.get("note") else "")),
        "blocks": ("not built" if r.get("state") == "asked"
                   else "nothing — but only HE can move it to verified-by-him"),
    } for r in openish]
    byhim = len([r for r in reqs if r.get("state") == "verified-by-him"])
    return items, {"source": "requirements", "status": "ok" if byhim else "degraded",
                   "reason": "" if byhim else
                             f"{len(reqs)} asks recorded and NONE is verified-by-him — a machine driving a "
                             f"control proves it reacts, not that it is what he wanted",
                   "count": len(items)}



ROADMAP_DIR = ROOT / "docs"


def _yields_rows(path) -> bool:
    """Does this roadmap file actually parse to any items? Used to detect a newest-file that would
    silently empty the source."""
    try:
        txt = path.read_text(errors="replace")
    except Exception:
        return False
    section = ""
    for line in txt.splitlines():
        t = line.strip()
        if t.startswith("#"):
            section = t.lstrip("# ").strip(); continue
        if "►" not in t and "OPERATOR" not in section.upper():
            continue
        if not t.startswith(("-", "|", "*")) or len(t) < 25:
            continue
        return True
    return False


def from_roadmap():
    """★★★ THE SIXTH SOURCE: work that was DISPLACED, with how long ago.

    Every report this week has ended with a displacement paragraph — Queue B is 10 days old, JARVIS is a
    week, R-09 was called especially important and is unbuilt — written by hand, in prose, in a document
    that is read once. None of it was on the board, so none of it competed for his attention against the
    thing whoever wrote the report happened to be working on.
    ★ A roadmap that lives only in a file is a promise nobody is holding. Surfacing it here makes the
      displacement paragraph unnecessary rather than better-written: the queue competes on the board.
    ★★ AGE IS THE WHOLE POINT. A queued item with no age reads as 'planned'. The same item marked
      '10 days queued' reads as 'this is not happening', which is the true statement and the one that
      changes a decision.
    ★ These are `info` — they are not blocking, and inflating them to blocking would be the noise that
      makes a board unreadable. They are here to be SEEN, not to shout."""
    import re as _re, datetime as _dt
    files = sorted(ROADMAP_DIR.glob("ROADMAP_2*.md"), reverse=True)
    if not files:
        return [], {"source": "roadmap", "status": "absent",
                    "reason": f"no ROADMAP_*.md in {ROADMAP_DIR} — queued work is invisible to the board",
                    "count": 0}
    # ★★★ A NEW ROADMAP FILE MUST NEVER SILENTLY EMPTY THIS SOURCE.
    #   This read files[0] — the newest — and nothing else. On 2026-09-06 a new ROADMAP file was added
    #   whose sections do not carry the "►" marker this parser looks for, so it yielded ZERO rows and
    #   FOURTEEN roadmap items left the operator's board without a word. The board went 114 -> 103 and
    #   every surface agreed with itself, because they all read the same empty answer.
    #   ★★ Zero from a file that EXISTS is the suspicious case, and it is the one the old code treated
    #     as normal. So: if the newest yields nothing, fall back to the newest file that DOES yield
    #     something, and report the fallback loudly rather than quietly succeeding.
    #   ★ It cannot be fixed by "remember to use the right format" — that is a rule, and every rule
    #     that stayed a rule here drifted (L6). The parser now refuses to lose rows.
    f = files[0]
    _fallback = None
    if len(files) > 1 and not _yields_rows(files[0]):
        for cand in files[1:]:
            if _yields_rows(cand):
                _fallback = (files[0].name, cand.name)
                f = cand
                break
    m = _re.search(r"(\d{4})(\d{2})(\d{2})", f.name)
    try:
        dated = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except Exception:
        dated = None
    age_days = (_dt.date.today() - dated).days if dated else None

    rows, section = [], ""
    for line in f.read_text(errors="replace").splitlines():
        t = line.strip()
        if t.startswith("#"):
            section = t.lstrip("# ").strip()
            continue
        # A queue marker (►N) or a line in the operator's own short list.
        if "►" not in t and "OPERATOR" not in section.upper():
            continue
        if not t.startswith(("-", "|", "*")) or len(t) < 25:
            continue
        # ★ A COARSE PARSER PUTS RUBBISH ON HIS BOARD, which is worse than putting nothing there —
        #   noise trains him to skim the source that carries it. The first run surfaced the document's
        #   own header ("From: ⛬6 orchestrator") as queued work. Metadata lines are refused explicitly.
        if _re.match(r"^[-*|\s]*(from|doc-id|report|to|date|status|v\d)\s*[:·]", t, _re.I):
            continue
        if "►" not in t and not _re.search(r"\b(ask|queued|unbuilt|not built|waiting|outstanding|pending)\b", t, _re.I):
            continue
        text = _re.sub(r"^[-*|\s]+", "", t).strip(" |")
        text = _re.sub(r"\*\*|\*|`", "", text)
        if not text:
            continue
        # ★ Strip the queue marker BEFORE splitting, or the title becomes the marker: the first pass
        #   produced rows literally titled "►1", "►2", "►3". A row whose title is its own bullet number
        #   tells him nothing and makes the source look broken.
        text = _re.sub(r"^►\s*\d+\s*[·:.\-]?\s*", "", text).strip(" |")
        parts = [x.strip() for x in _re.split(r"—|·|\|", text) if x.strip()]
        title = (parts[0] if parts else text)[:95]
        if len(title) < 8 and len(parts) > 1:
            title = (parts[0] + " — " + parts[1])[:95]
        if len(title) < 8:
            continue
        rid = "derived:roadmap:" + _re.sub(r"[^a-z0-9]+", "-", title.lower())[:44].strip("-")
        if any(r["id"] == rid for r in rows):
            continue
        age_txt = (f"{age_days} day(s) since this roadmap was written"
                   if age_days is not None else "age unknown — the roadmap file carries no date")
        rows.append({
            "id": rid, "sev": "info", "derived": True, "source": "roadmap",
            "title": title,
            "one": (text[:360] + ("…" if len(text) > 360 else "")
                    + f"  ·  QUEUED, NOT BLOCKED — {age_txt}. From {f.name}, section “{section[:60]}”."),
            "blocks": "nothing is stopped by this — it is here so queued work competes for your "
                      "attention instead of living in a file nobody opens",
            "origin": {"what": f"queued work from the roadmap, section “{section[:70]}”",
                       "where": str(f), "kind": "roadmap", "as_of": f"roadmap written {age_days} day(s) ago"
                       if age_days is not None else "roadmap undated",
                       "raised_by": "whoever last wrote the roadmap",
                       "how_to_clear": "start it, drop it, or move it — it clears when the roadmap stops "
                                       "listing it"},
        })
    _hs = {"source": "roadmap", "status": "ok" if rows else "empty",
           "count": len(rows[:14]), "file": f.name,
           "reason": (f"read from {f.name}"
                      + (f" (age {age_days}d)" if age_days is not None else ""))}
    if _fallback:
        # ★ SAY IT LOUDLY. A fallback that reports "ok" is how the next new roadmap silently empties
        #   this again — the whole point is that the newest file did NOT work.
        _hs["status"] = "degraded"
        _hs["reason"] = (f"THE NEWEST ROADMAP YIELDED NOTHING. {_fallback[0]} parsed to zero items — "
                         f"its sections carry no '►' marker and no OPERATOR heading — so these rows "
                         f"come from {_fallback[1]} instead. Without this fallback the roadmap source "
                         f"would have gone silently empty and taken its rows off the board.")
    _hs["age_days"] = age_days
    return rows[:14], _hs


def from_services():
    """★★★ THE SEVENTH SOURCE — service access, MEASURED, self-clearing.

    This is the answer to "do we build a wizard". A wizard is a UI over a decision tree, and it would be
    a SECOND surface for 'things only the operator can do'. This house has been bitten twice by two
    surfaces disagreeing about the same facts — the board that displayed 70 while its exit code counted
    22, and the two launcher entries that showed different boards. Building a parallel wizard on purpose
    recreates that defect.

    So the wizard is the board: each service that cannot be reached becomes a ROW, with the exact step
    and a VERIFY command, and the row DISAPPEARS when the verify starts passing. No state to keep in
    sync, because there is no second store — the check is run fresh each time and the board renders it.

    ★★ `unproven` is not reported as a blocker. A service with no probe is a gap in our CHECKING, not a
       gap in his access, and dressing one as the other would fill his board with rows he cannot act on.
       They are summarised in source health instead, where the honest statement is 'we cannot tell'.
    """
    try:
        import service_auth_check as sac
        # ★ reload removed 2026-09-17 — same import-lock convoy as from_persistent() below.
        #   from_services() is on the operator-board path, which is polled continuously.
        d = sac.check()
    except Exception as e:
        return [], {"source": "services", "status": "error", "reason": str(e)[:140], "count": 0}

    rows = []
    for r in d["services"]:
        if r["state"] != "no":
            continue          # `works` needs nothing; `unproven` is our gap, not his
        rows.append({
            "id": f"derived:svc:{r['id']}", "sev": "hold", "derived": True, "source": "services",
            "title": f"{r['label']}: reachable once you authenticate it",
            "one": (f"A probe just ran and it is NOT authenticated. {r.get('unlocks','')} "
                    f"What the probe saw: {str(r.get('detail',''))[:200]}"),
            "blocks": r.get("unlocks") or "the capabilities this service provides",
            "todo": r.get("how", ""),
            "verify": " ".join(r.get("probe", [])),
            "origin": {"what": f"a live probe of {r['label']} found no authenticated access",
                       "where": "operator/service_auth_check.py", "kind": "measured", "row": r["id"],
                       "raised_by": "the service prober, run fresh at render time",
                       "how_to_clear": "authenticate it; this row disappears when the probe passes — "
                                       "there is no state to update and nothing to mark done"},
        })
    unproven = [r["label"] for r in d["services"] if r["state"] == "unproven"]
    return rows, {"source": "services", "status": "ok" if not unproven else "partial",
                  "reason": ("" if not unproven else
                             f"{len(unproven)} service(s) have NO probe, so nothing here establishes "
                             f"whether they work: {', '.join(unproven[:6])}"
                             f"{'…' if len(unproven) > 6 else ''}. That is a gap in our checking, not a "
                             f"claim that they are fine."),
                  "count": len(rows)}


def from_persistent():
    """★★★ THE EIGHTH SOURCE — things that must stay open, and are not.

    A team declares a process it depends on; this turns two faults into board rows:
      · DECLARED BUT NOT RUNNING — something a team needs has stopped. Somebody must restart it, and
        the declaration says which steward owns that.
      · DECLARED BUT NOT DURABLE — it is running, but its STATE lives somewhere volatile. A browser
        session in /tmp is not persistent; it merely has not been cleaned yet. That is an operator
        decision (move it) rather than a restart, and it is invisible until the day it is gone.
    ★ `running_unclaimed` is deliberately NOT surfaced as rows. Nobody declaring a process is a gap in
      OUR bookkeeping, not a thing waiting on him, and filling his board with it would be noise."""
    try:
        import persistent_registry as pr
        # ★★★ NO importlib.reload HERE — 2026-09-17.
        #   reload() takes the per-module IMPORT LOCK and re-executes the module body. This runs on
        #   /operator/board.json, which seats, hooks and the panel poll continuously. Measured 312
        #   concurrent request threads in this call path: one lock, 311 waiters, median 161 s blocked
        #   (the futex the operator-needs seat measured). It also reset persistent_registry's
        #   single-flight scan cache on every request, so the cache could never once be hit.
        #   The panel restarts on edit; picking up source changes was never worth a convoy.
        st = pr.status()
    except Exception as e:
        return [], {"source": "persistent", "status": "error", "reason": str(e)[:140], "count": 0}
    rows = []
    for r in st.get("marked_dead", []):
        rows.append({
            "id": f"derived:persist:{r['name']}", "sev": "stop", "derived": True, "source": "persistent",
            "title": f"{r['name']} must stay open and is NOT running",
            "one": (f"{r['why']} It has stopped. {r.get('breaks_if_stopped') or ''}").strip(),
            "blocks": r.get("breaks_if_stopped") or "whatever depends on it",
            "todo": f"restart it — steward {r.get('steward')} owns this; owning team is {r.get('owner')}",
            "origin": {"what": f"{r['name']} was declared as must-stay-open and is not running",
                       "where": "operator/persistent_processes.json", "kind": "declared",
                       "row": r["name"], "raised_by": f"the {r.get('owner')} team's declaration",
                       "how_to_clear": "start it again; the row clears when it is seen running"},
        })
    for r in st.get("marked_running", []):
        d = r.get("durable_state") or ""
        if d and ("/tmp" in d or d.startswith("/run")):
            rows.append({
                "id": f"derived:persist-fragile:{r['name']}", "sev": "hold", "derived": True,
                "source": "persistent",
                "title": f"{r['name']} is running, but its state will not survive a reboot",
                "one": (f"Its state lives at {d}, which is volatile. {r['why']} Anything established in "
                        f"it — a login, a session — is one reboot or one /tmp clean away from gone. It "
                        f"is not persistent; it just has not been cleared yet."),
                "blocks": r.get("breaks_if_stopped") or "the work done inside it, silently, at some "
                                                        "future restart",
                "todo": ("move the state somewhere durable (e.g. ~/.config/khaoss/) and relaunch "
                         "against it BEFORE doing the work again, or the work is done twice"),
                "origin": {"what": "a declared-persistent process whose state is in a volatile path",
                           "where": "operator/persistent_processes.json", "kind": "declared",
                           "row": r["name"], "raised_by": f"the {r.get('owner')} team's declaration",
                           "how_to_clear": "point it at a durable path and re-register"},
            })
    return rows, {"source": "persistent", "status": "ok",
                  "reason": (f"{st['counts']['unclaimed']} long-lived process(es) are undeclared — a gap "
                             f"in our bookkeeping, not something waiting on him, so they are not rows"),
                  "count": len(rows)}



def from_paused():
    """★★★ A DELIBERATE PAUSE MUST BE VISIBLE WHILE IT IS PAUSED.

    khaoss-kb-capture.timer was stopped and disabled on 2026-09-13 because one run cost $0.78.
    Correct decision, no resume condition recorded, nothing surfacing it — and five days later the
    operator asked why his captures had stopped. He was not missing captures; there had been none
    since 09-13, by our own hand.
    ★ A stopped service that nothing surfaces is INDISTINGUISHABLE FROM A BROKEN ONE. This source
      is the difference. An entry past its review date becomes a row he can see.
    """
    try:
        import paused as _p
        rows = _p.board_rows()
        st = _p.status()
        if st.get("error"):
            # ★ "could not look" is not "nothing is paused" — say which.
            return [], {"source": "paused", "status": "error",
                        "reason": st["error"][:140], "count": 0}
        n = len(st["paused"]) + len(st["overdue"]) + len(st["resumed_without_record"])
        return rows, {"source": "paused", "status": "ok", "count": n}
    except Exception as e:
        return [], {"source": "paused", "status": "error",
                    "reason": f"{e.__class__.__name__}: {str(e)[:120]}", "count": 0}


def derive():
    items, health = [], []
    for fn in (from_requests, from_findings, from_red_rows, from_invariants, from_requirements, from_roadmap, from_services, from_persistent, from_paused):
        try:
            i, h = fn()
        except Exception as e:
            i, h = [], {"source": fn.__name__, "status": "error", "reason": str(e)[:120], "count": 0}
        items += i
        health.append(h)
    return items, health


def self_test() -> int:
    """★ NEGATIVE CONTROL. This module once reported a silent ZERO: the approval-requests directory is
    root-owned, iterdir() raised PermissionError, the except swallowed it, and the board showed no
    pending requests — indistinguishable from 'none pending'. Absence of ACCESS is not absence of
    CONTENT. So the test that matters is not 'does it find rows', it is 'when it CANNOT look, does it
    say so instead of saying zero'. Fails if an unreadable source yields a clean empty result.
    """
    import tempfile, os, pathlib as _pl
    failures = []

    d = tempfile.mkdtemp()
    unreadable = os.path.join(d, "locked")
    os.mkdir(unreadable)
    os.chmod(unreadable, 0o000)
    try:
        try:
            os.listdir(unreadable)
            # running as root, or an fs that ignores the mode — the probe itself is invalid
            print("  SKIP self_test: directory still readable at mode 000 (root?) — probe invalid")
            return 0
        except PermissionError:
            pass

        global REQUESTS
        saved = REQUESTS
        REQUESTS = _pl.Path(unreadable)
        try:
            out = from_requests()
        finally:
            REQUESTS = saved

        blob = repr(out).lower()
        if "unreadable" not in blob and "permission" not in blob:
            failures.append("an UNREADABLE source produced no unreadable/permission signal — "
                            "the caller cannot tell 'nothing pending' from 'could not look'")
    finally:
        os.chmod(unreadable, 0o755)

    # ★ SECOND CONTROL: the staleness row must FIRE on an old history, and NOT on a fresh one.
    #   The dangerous direction is a PASS that has since broken and therefore appears nowhere — that is
    #   a false clear whose only visible symptom is a date nobody prints. A branch that has only ever
    #   been not-taken is not a guard.
    import tempfile as _tf, json as _j, time as _tm
    global INV_HIST
    _saved_hist = INV_HIST
    try:
        old = _pl.Path(_tf.mkdtemp()) / "hist.jsonl"
        stamp = int(_tm.time()) - 30 * 3600          # 30h ago, well past STALE_AFTER
        old.write_text(_j.dumps({"run": "OLD", "ts": stamp, "id": "INV-1",
                                 "verdict": "PASS", "desc": "x", "evidence": "y"}) + "\n")
        INV_HIST = old
        rows_old, health_old = from_invariants()
        if not any(r["id"] == "derived:inv:INV-SOURCE-STALE" for r in rows_old):
            failures.append("a 30h-old invariant history produced NO staleness row — the board would "
                            "present those verdicts as current, and anything broken since is invisible")
        if health_old.get("status") != "stale":
            failures.append("a 30h-old history is not reported stale in source health")

        fresh = _pl.Path(_tf.mkdtemp()) / "hist.jsonl"
        fresh.write_text(_j.dumps({"run": "NEW", "ts": int(_tm.time()), "id": "INV-1",
                                   "verdict": "PASS", "desc": "x", "evidence": "y"}) + "\n")
        INV_HIST = fresh
        rows_new, _ = from_invariants()
        if any(r["id"] == "derived:inv:INV-SOURCE-STALE" for r in rows_new):
            failures.append("a FRESH history produced a staleness row — the guard cries wolf")
    finally:
        INV_HIST = _saved_hist

    for f in failures:
        print(f"  ✗ {f}")
    print("  ✓ derive_items self_test: unreadable source reported not zeroed; staleness fires on old "
          "history and not on fresh"
          if not failures else "  ✗ derive_items self_test FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys as _s
    if "--prove-fail" in _s.argv:
        _s.exit(self_test())
    items, health = derive()
    print(f"\n★ DERIVED ITEMS — {len(items)} from 4 sources\n")
    for h in health:
        mark = {"ok": "✓", "degraded": "!", "empty": "·", "unavailable": "✗", "absent": "✗",
                "stale": "!", "error": "✗"}.get(h["status"], "?")
        print(f"  {mark} {h['source']:11s} {h['status']:12s} {h['count']:3d} item(s)"
              + (f"  — {h['reason'][:88]}" if h.get("reason") else ""))
    print()
    for i in items[:12]:
        print(f"    [{i['sev']}] {i['title'][:88]}")
    if len(items) > 12:
        print(f"    … {len(items)-12} more")
    print("\n  ★ A source that cannot be read becomes a VISIBLE ROW, never a silent zero.\n")
