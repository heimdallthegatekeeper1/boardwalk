#!/usr/bin/env python3
"""did_this.py — "I did this" stops being a single bit and becomes a RECORD.

★★★ HIS ASK: "the 'I did this' button needs to pop up to show a comprehensive set of fields on notes
    of how the operator did what they did with checkboxes, whether they did with an existing agent,
    who, or alone, or partially finished with a percentage estimate, what they did (bigger field) —
    think comprehensive. This means the 'I did this' needs to change to 'I did this. 0.#%-100%'."

★★ WHY A PERCENTAGE CHANGES THE MEANING OF THE BUTTON. Today the button writes one fact: he asserted
   it. That is the SAME record whether he finished the thing or glanced at it, so the board cannot
   tell a finished row from a nearly-finished one, and neither can any agent reading it later. A row
   at 60% is not done and must not vanish; a row at 100% is finished and should. Those are different
   outcomes and until now they wrote the same bit.

★ WHAT THIS REFUSES TO DO: it does not VERIFY anything. A record here says "the operator states he did
  X" — it is his assertion, stamped and attributed, and it is stored as an assertion. Per the
  verification canon, an assertion is EVIDENCE, never a verified claim, and the record says so in
  every entry so that nothing downstream can quietly promote it.
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "did_this_log.json"

# ★ checkboxes, in his words: "with an existing agent, who, or alone, or partially finished"
HOW = [
    {"key": "alone", "label": "I did it myself"},
    {"key": "with_agent", "label": "With one of my agents", "needs": "who"},
    {"key": "with_person", "label": "With another person", "needs": "who"},
    {"key": "already_done", "label": "It turned out it was already done"},
    {"key": "not_needed", "label": "It turned out it did not need doing"},
    {"key": "worked_around", "label": "I worked around it rather than fixing it"},
    {"key": "partial", "label": "I got part of the way"},
]

EVIDENCE = [
    {"key": "saw_it", "label": "I saw it work with my own eyes"},
    {"key": "ran_cmd", "label": "I ran a command and read the output"},
    {"key": "someone_told", "label": "Someone/something told me it was done"},
    {"key": "assumed", "label": "I am assuming — I have not checked"},
]


def fields() -> dict:
    """The form. ★ Every field says what it is FOR, because a blank box teaches nobody what to write."""
    return {
      "percent": {"type": "number", "min": 0, "max": 100, "step": 0.1, "required": True,
                  "label": "How far did you get?",
                  "help": "0.1% to 100%. Anything under 100 keeps the row OPEN with your note "
                          "attached — it does not disappear. 100 settles it."},
      "how": {"type": "checkboxes", "options": HOW, "required": True,
              "label": "How did you do it?",
              "help": "Tick everything that applies. 'With an agent' asks who, because a record that "
                      "cannot name the agent cannot be checked later."},
      "who": {"type": "text", "required": False, "label": "Who with?",
              "help": "The agent, seat or person. Required if you ticked an option that needs it."},
      "what": {"type": "textarea", "rows": 6, "required": False,
               "label": "What did you actually do?",
               "help": "The big field. Write it for yourself in three months, not for me. What you "
                       "changed, what you tried that failed, and anything you'd want to know if this "
                       "comes back."},
      "evidence": {"type": "checkboxes", "options": EVIDENCE, "required": False,
                   "label": "How do you know it worked?",
                   "help": "★ This is the honest bit. 'I am assuming' is a legitimate answer and is "
                           "recorded as such — it is far better than a silent claim of done."},
      "time_spent": {"type": "text", "required": False, "label": "Roughly how long did it take?",
                     "help": "Free text — '10 min', 'most of an afternoon'. Used to estimate the "
                             "next one like it. Leave blank if you'd rather not."},
      "blocked_by": {"type": "text", "required": False, "label": "If you did NOT finish — what stopped you?",
                     "help": "The single most useful field on this form when the percentage is under "
                             "100. Whatever stopped you will stop you again."},
      "followup": {"type": "text", "required": False, "label": "Should this leave a follow-up row?",
                   "help": "Text here opens a NEW board row so the leftover does not get lost when "
                           "this one settles."},
    }


def validate(rec: dict) -> dict:
    """★ Refuses rather than guesses. A record that fabricates the percentage or drops the 'who' is
    worse than no record — it reads as fact later."""
    errs = []
    try:
        pct = float(rec.get("percent"))
    except (TypeError, ValueError):
        return {"ok": False, "errors": ["how far you got is required — 0.1 to 100"]}
    if not (0 < pct <= 100):
        errs.append(f"{pct} is not between 0.1 and 100")
    how = [h for h in (rec.get("how") or []) if h in {x["key"] for x in HOW}]
    if not how:
        errs.append("tick at least one 'how did you do it'")
    needs_who = {x["key"] for x in HOW if x.get("needs") == "who"}
    if set(how) & needs_who and not str(rec.get("who") or "").strip():
        errs.append("you ticked an option that needs a name — say who")
    # ★ the contradiction check: 100% while ticking "I got part of the way"
    if pct >= 100 and "partial" in how:
        errs.append("you said 100% and also 'I got part of the way' — which is it? "
                    "(that contradiction would be recorded as fact, so it is refused here)")
    if pct < 100 and not str(rec.get("blocked_by") or "").strip() and not str(rec.get("what") or "").strip():
        errs.append("under 100% with no note — say what you did or what stopped you, "
                    "otherwise the leftover is unrecoverable")
    return {"ok": not errs, "errors": errs, "percent": pct, "how": how}


def record(item_id: str, rec: dict, source: str = "board") -> dict:
    v = validate(rec)
    if not v["ok"]:
        return {"ok": False, "errors": v["errors"], "stored": False}
    entry = {
        "item": item_id, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source,
        "percent": v["percent"], "how": v["how"],
        "who": str(rec.get("who") or "").strip(),
        "what": str(rec.get("what") or "").strip(),
        "evidence": [e for e in (rec.get("evidence") or []) if e in {x["key"] for x in EVIDENCE}],
        "time_spent": str(rec.get("time_spent") or "").strip(),
        "blocked_by": str(rec.get("blocked_by") or "").strip(),
        "followup": str(rec.get("followup") or "").strip(),
        # ★ the canon line. Written into EVERY entry so nothing downstream can promote an assertion.
        "claim_class": "OPERATOR ASSERTION — not verified by anything here",
        "settles": v["percent"] >= 100,
    }
    log = _load()
    log.append(entry)
    _save(log)
    return {"ok": True, "stored": True, "entry": entry,
            "settles": entry["settles"],
            "note": ("Recorded and the row is settled." if entry["settles"] else
                     f"Recorded at {v['percent']}%. ★ The row stays OPEN — your note is attached to it "
                     f"so you pick up where you left off.")}


def for_item(item_id: str) -> list:
    return [e for e in _load() if e.get("item") == item_id]


def _load() -> list:
    try:
        d = json.loads(LOG.read_text())
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(log: list) -> None:
    tmp = LOG.with_suffix(".tmp")
    tmp.write_text(json.dumps(log, indent=2))
    os.replace(tmp, LOG)


def self_test() -> int:
    fails = []
    f = fields()
    for k, v in f.items():
        if not v.get("help"):
            fails.append(f"field {k} has no help text — a blank box teaches nobody what to write")
    if not validate({"percent": 50, "how": ["partial"], "what": "got halfway"})["ok"]:
        fails.append("a legitimate partial record was refused")
    if validate({"percent": 0, "how": ["alone"]})["ok"]:
        fails.append("0% was accepted — that is not a completion")
    if validate({"percent": 101, "how": ["alone"]})["ok"]:
        fails.append("101% was accepted")
    if validate({"percent": 100, "how": []})["ok"]:
        fails.append("a record with no 'how' was accepted")
    if validate({"percent": 100, "how": ["with_agent"]})["ok"]:
        fails.append("★ 'with an agent' was accepted WITHOUT naming the agent")
    if validate({"percent": 100, "how": ["partial"], "what": "x"})["ok"]:
        fails.append("★ 100% AND 'part of the way' was accepted — a contradiction got stored as fact")
    if validate({"percent": 40, "how": ["alone"]})["ok"]:
        fails.append("under-100% with no note at all was accepted — the leftover is unrecoverable")
    r = record("__selftest__", {"percent": 100, "how": ["alone"], "what": "test",
                                "evidence": ["assumed"]})
    if r.get("ok") and r["entry"].get("claim_class", "").startswith("OPERATOR ASSERTION") is False:
        fails.append("★ an entry was stored without the assertion-not-verified class")
    if r.get("ok"):
        log = [e for e in _load() if e.get("item") != "__selftest__"]
        _save(log)
    for x in fails:
        print("  CONTROL FAILED:", x)
    if not fails:
        print("  8 controls pass: every field explains itself · partial accepted · 0%/101% refused · "
              "no-'how' refused · AGENT NAMED OR REFUSED · 100%-and-partial CONTRADICTION refused · "
              "under-100% with no note refused · every entry stamped OPERATOR ASSERTION, NOT VERIFIED")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(self_test())
    print(json.dumps(fields(), indent=2))
