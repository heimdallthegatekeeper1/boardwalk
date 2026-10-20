#!/usr/bin/env python3
"""guide_focus.py — an agent points at a place on the board; the panel takes him there and lights it up.

★★★ WHAT HE ASKED FOR (2026-09-03): "a visual guided process, where I can get flashy things as the
    agent moves me to the areas I need to work with it on. This needs to be as interactive as possible."

★★ THE SHAPE: an agent sets a FOCUS — an item, a control, a section — with one instruction and a step
    number. The panel scrolls there, rings it, and shows the instruction beside it. He does the thing,
    presses done, and the agent's next step lights up. It is a conversation conducted in the interface
    rather than about it.

★ WHY THIS IS NOT A SECOND SURFACE. It carries no content of its own — it points AT the board and the
  controls that already exist. Everything it highlights is a thing he could have found himself; the
  agent is supplying the ORDER and the reason, which is exactly what a person coming to 110 items
  cannot supply for themselves.

★★ AND IT IS ALWAYS DISMISSIBLE. A guided flow he cannot leave is a captor. `dismiss()` ends it, and the
   panel offers that on every step — because the moment a guide cannot be closed, the surface belongs to
   the agent rather than to him.
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime
from pathlib import Path


def _ss():
    """★ One atomic+locked writer. write_text(json.dumps(...)) truncates before it writes, so a crash
    between those two moments empties the store; and nothing serialised concurrent writers on a box
    where every agent runs as the same uid. Proven through 160 concurrent writes."""
    import importlib, sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent))
    return importlib.import_module("safe_store")

HERE = Path(__file__).resolve().parent
STATE = HERE / "guide_focus.json"
# ★ The message journal and a runtime wake-up feed are different facts.  The old CC watcher tails
# guide_replies.jsonl and cannot filter by selected backend.  If every guide message stayed there,
# choosing Codex would still wake CC.  New messages therefore live in one canonical journal; the
# backend worker copies a transport-only envelope to the legacy feed only when that CC seat is the
# selected destination.  Historical rows remain readable below and nothing is deleted.
REPLIES = HERE / "guide_messages.jsonl"
LEGACY_REPLIES = HERE / "guide_replies.jsonl"
ANSWERS = HERE / "guide_answers.json"

# What a focus may point at. Closed on purpose: an arbitrary selector would let an agent aim the
# operator's attention anywhere, including at things the panel does not own.
KINDS = {
    "item": "a board row — scrolls to it, opens it, and rings it",
    "control": "a named control in the panel (search, group, walkthrough, assist, services, wizard)",
    "section": "one of the board's own blocks (services, wizard, verify, delta, assist, yours)",
    "none": "no target — just say something, without moving him",
}
CONTROLS = {
    "search": "#bd-q", "group": "#bd-group", "sort": "#bd-sort",
    "assist": "#bd-assist", "services": "#bd-services", "wizard": "#bd-wizard",
    "verify": "#bd-verify", "delta": "#bd-delta", "yours": "#bd-yours",
    "walkthrough": "#on-walk-btn", "items": "#on-items",
}


def _load() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def set_focus(kind: str, target: str, say: str, step: int = 1, of: int = 1,
              by: str = "an agent", why: str = "", replace: bool = False,
              dry_run: bool = False) -> dict:
    """★★★ AN ACTIVE FOCUS IS PROTECTED, because it is on his screen while he is reading it.
    On 2026-09-04 a peer probing this endpoint's field contract POSTed {"say":"contract probe"} and
    REPLACED a live step-1-of-3 guide with the word "probe" — on the panel he was reading at that
    moment. It restored it within a minute, correctly and with the provenance intact, but nothing in
    this file stopped it: ANY well-formed call was a silent takeover of whatever he was being guided by.
    ★ So overwriting another agent's ACTIVE focus now requires `replace: true`. A guide is a thing he is
      in the middle of; taking it over should be a decision, not a side effect of a probe.
    ★★ And `dry_run` exists here for the same reason it now exists on dispatch/send — the peer said it
      would use one and there was none to use. An endpoint an agent is expected to drive needs a way to
      ask what would happen."""
    if kind not in KINDS:
        return {"ok": False, "error": f"kind must be one of {sorted(KINDS)}"}
    if kind == "control" and target not in CONTROLS:
        return {"ok": False, "error": f"unknown control {target!r}; known: {sorted(CONTROLS)}"}
    if not say.strip():
        return {"ok": False, "error": "a focus with no instruction just moves him for no reason — "
                                      "say what to do"}
    live = current()
    if live.get("active") and not replace:
        if live.get("by") != by:
            return {"ok": False, "error": "refused — another agent's guide is ACTIVE and he may be "
                                          "reading it right now",
                    "active_by": live.get("by"), "active_step": f"{live.get('step')} of {live.get('of')}",
                    "active_target": live.get("target"),
                    "how": "pass replace:true to take it over deliberately, or wait until he advances it"}
    if dry_run:
        return {"ok": True, "dry_run": True, "changed": False,
                "would_point_at": f"{kind}:{target}", "would_say": say.strip()[:200],
                "would_replace": (f"{live.get('by')} step {live.get('step')} of {live.get('of')}"
                                  if live.get("active") else None),
                "note": "NOTHING changed. Remove dry_run to set it."}
    d = {"kind": kind, "target": target, "say": say.strip(), "why": why.strip(),
         "step": int(step), "of": int(of), "by": by,
         "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
         "selector": CONTROLS.get(target) if kind == "control" else None,
         "done": False}
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, **d}


def _step_view(d: dict) -> dict:
    """The one step he is on, out of a sequence. ★ Old single-focus files still load: a file with no
    `steps` key IS a one-step sequence, so nothing written before this change becomes unreadable."""
    steps = d.get("steps")
    if not steps:
        return d
    i = int(d.get("i") or 0)
    if i >= len(steps):
        return {}
    st = dict(steps[i])
    st.update({"step": i + 1, "of": len(steps), "by": d.get("by", "an agent"),
               "at": d.get("last_at") or d.get("at"), "done": False,
               "selector": CONTROLS.get(st.get("target")) if st.get("kind") == "control" else None,
               "queued": True})
    return st


def current() -> dict:
    d = _load()
    if not d or d.get("done"):
        return {"active": False}
    is_walk = bool(d.get("steps"))      # ★ captured BEFORE d is replaced by the step view below
    if d.get("steps"):
        st = _step_view(d)
        if not st:
            return {"active": False, "finished": True,
                    "note": "the sequence is finished"}
        # ★ THE TASK FLAG LIVES ON THE SEQUENCE, NOT ON THE STEP — so it has to be carried across
        #   explicitly, or the panel cannot tell a guided task from a walk and renders "ROW None OF
        #   None". Same shape as the whitelist that ate `title` and `row_n` an hour ago: a value that
        #   exists is not a value that arrives.
        if d.get("task"):
            st["task"] = True
            st["returns_to_row"] = ((d.get("resume") or {}).get("steps") or [{}])[
                int((d.get("resume") or {}).get("i") or 0)].get("row_n") if d.get("resume") else None
        ans = _answers().get(st.get("target") or "")
        if ans:
            st["agent_says"] = ans.get("text")
            st["agent_says_at"] = ans.get("at")
            st["agent_says_by"] = ans.get("by")
        d = st
    # ★ A focus older than 30 minutes is stale — he has moved on, and lighting something up because an
    #   agent said so half an hour ago is a haunting, not a guide.
    try:
        age = time.time() - datetime.strptime(d["at"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except Exception:
        age = 0
    # ★★ FOR A SEQUENCE THIS IS MEASURED FROM HIS LAST ADVANCE, not from when the walk was pushed —
    #    `at` above is already `last_at` for a queue. The single-focus rule ("30 minutes on from an
    #    agent's instruction is a haunting, not a guide") is about how long he has been LEFT ALONE.
    #    Measured against the set time it would have killed a 20-row walk at minute 30 — which is
    #    precisely the end condition the walk plan recommends, so the guide would expire exactly as
    #    he reached the finish. Inactivity is the thing that means he moved on; elapsed time is not.
    # ★★★ A WALK HE STARTED MUST NOT TIME OUT WHILE HE IS DOING THE WORK.
    #   The 30-minute rule was written for a SINGLE focus an agent set: half an hour on, that
    #   instruction is a haunting and should vanish. It is exactly wrong for a QUEUED WALK. He spent
    #   36 minutes in the Google Cloud Console doing what row 7 asked him to do, came back, and the
    #   guide had dropped itself — punishing him for completing a long step, and leaving him asking
    #   which button to press because there was nothing to press.
    #   ★★ A walk has an explicit exit: "leave the walk". Something with a door does not also need a
    #     timer. So a sequence persists, and only a lone agent-set focus still expires.
    #   ★ AND THIS TESTED THE WRONG VARIABLE ON ITS FIRST ATTEMPT. By the time it ran, `d` had been
    #     reassigned to the single-step view, which has no "steps" key — so the guard never fired and
    #     the walk stayed expired. Exactly the class of bug this evening keeps producing: a condition
    #     that reads a name whose meaning changed three lines earlier.
    if is_walk:
        return {"active": True, **d, "age_s": int(age)}
    if age > 1800:
        return {"active": False, "expired": True,
                "note": "the last focus was set more than 30 minutes ago and has been dropped"}
    return {"active": True, **d, "age_s": int(age)}


def set_sequence(steps: list, by: str = "an agent", why: str = "",
                 replace: bool = False) -> dict:
    """★★★ PUSH THE WHOLE WALK ONCE; THE BOARD THEN DRIVES ITSELF.

    The single-focus shape required the guiding agent to set the next step every time he pressed
    done — so a 20-row walk cost 20 round trips through an agent, and every one of them was a pause
    he sat through while a model thought. The operator asked for this to happen IN THE BOARD ("ideally
    in the future it will all be done through the board") and to be cheap. A queue is both: the steps
    are written once, and advancing is a local click.

    ★ It is the SAME mechanism, not a second one — same store, same poll, same dismiss. `current()`
      simply returns the step he is on."""
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "a sequence needs at least one step"}
    clean = []
    for n, raw in enumerate(steps, 1):
        if not isinstance(raw, dict):
            return {"ok": False, "error": f"step {n} is not an object"}
        kind = str(raw.get("kind") or "item")
        if kind not in KINDS:
            return {"ok": False, "error": f"step {n}: kind must be one of {sorted(KINDS)}"}
        if kind == "control" and str(raw.get("target")) not in CONTROLS:
            return {"ok": False, "error": f"step {n}: unknown control {raw.get('target')!r}"}
        if not str(raw.get("say") or "").strip():
            return {"ok": False, "error": f"step {n}: a step with no instruction just moves him "
                                          f"for no reason — say what to do"}
        st = {"kind": kind, "target": str(raw.get("target") or ""),
              "say": str(raw["say"]).strip(), "why": str(raw.get("why") or "").strip()}
        # ★★★ THIS WAS A WHITELIST AND IT SILENTLY ATE EVERY FIELD ADDED AFTER IT WAS WRITTEN.
        #   `title`, `row_n` and `row_total` were added to the composer to fix the very confusion
        #   this walk exists to avoid — two different counters both called "row" — and this line
        #   dropped all three without a word. The next walk would have rendered "STEP 21 OF 22"
        #   again, and I would have gone hunting for the off-by-one a third time.
        # ★★ A filter that discards what it does not recognise, silently, is the shape of every
        #   instrument defect found on this box tonight. So it now PASSES EVERYTHING THROUGH except
        #   the keys it has already validated — a new field on the composer just works, and nothing
        #   disappears between two files that were written months apart.
        for extra, val in raw.items():
            if extra in ("kind", "target", "say", "why") or val in (None, ""):
                continue
            st[extra] = val
        clean.append(st)
    live = current()
    if live.get("active") and not replace:
        return {"ok": False, "error": "refused — a guide is ACTIVE and he may be reading it right now",
                "active_by": live.get("by"), "active_step": f"{live.get('step')} of {live.get('of')}",
                "how": "pass replace:true to take it over deliberately"}
    now = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    d = {"steps": clean, "i": 0, "by": by, "why": why.strip(), "at": now, "last_at": now,
         "done": False}
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "steps": len(clean), "by": by, "first": clean[0]["say"][:120]}


MOVES = HERE / "guide_moves.jsonl"


def _journal(action: str, frm, to, of):
    """★ EVERY MOVE IS RECORDED. He reported the walk 'popping back a row' three times and every
    diagnosis was a guess, because nothing wrote down what moved it or when. A position that changes
    with no record is indistinguishable from one that changed by itself."""
    try:
        with MOVES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                                 "action": action, "from": frm, "to": to, "of": of}) + "\n")
    except OSError:
        pass


def advance(done_step: int | None = None) -> dict:
    """He pressed done. Moves to the next queued step, or clears if this was a single focus."""
    d = _load()
    if not d:
        return {"ok": True, "note": "nothing was focused"}
    if d.get("steps"):
        was = int(d.get("i") or 0)
        i = was + 1
        d["i"] = i
        d["last_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        finished = i >= len(d["steps"])
        if finished and d.get("task") and d.get("resume"):
            # ★ HAND HIM BACK HIS WALK. Not re-derived, not recomposed — the exact object we set aside.
            back = d["resume"]
            back["last_at"] = d["last_at"]
            STATE.write_text(json.dumps(back, indent=2) + "\n")
            st = _step_view(back) or {}
            _journal("task-done", i, int(back.get("i") or 0), len(back.get("steps") or []))
            return {"ok": True, "finished": True, "returned_to_walk": True,
                    "row": st.get("row_n"), "of": st.get("row_total"),
                    "note": "task complete — you are back on your walk, exactly where you were"}
        d["done"] = finished
        STATE.write_text(json.dumps(d, indent=2) + "\n")
        # ★★★ THE JOURNAL WAS OFF BY ONE, AND IT IS THE INSTRUMENT I BUILT TO STOP GUESSING.
        #   It logged (i, i+1) AFTER i had already been incremented, so an advance from 16 to 17
        #   recorded itself as "17 -> 18". I then read that log, saw a move to 18 while the file said
        #   17, and started hunting a phantom writer that reverts positions. A wrong instrument does
        #   not merely fail to help — it manufactures a bug to chase. That is the ninth time today.
        _journal("advance", was, i, len(d["steps"]))
        return {"ok": True, "step": i + 1, "of": len(d["steps"]), "finished": finished,
                "note": "walk complete" if finished else "next step is live"}
    d["done"] = True
    d["done_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "cleared_step": d.get("step"),
            "note": "focus cleared — the guiding agent sets the next step"}


KINDS_OF_REPLY = {
    # ★★★ A DIRECTIVE AND A NOTE ARE NOT THE SAME SPEECH ACT, in his words: "We need to be able to
    #   submit it as a directive or as a note with that input." A note is context that stays with the
    #   row and expects nothing. A directive expects an agent to GO AND DO SOMETHING. Storing both in
    #   one undifferentiated stream means the agent has to guess which sentences were orders — and a
    #   guess in that direction either ignores an instruction or acts on a musing.
    "directive": "an instruction to the agent — it is expected to act on this",
    "note": "context that belongs with the row — nobody is being asked to act",
    "answer": "a reply to what the guide just asked him",
    # ★★★ "I need a button that pings the agent that they are confused and need more context." A
    #   person who does not understand a row cannot rule on it, and the walk's whole premise is that
    #   he is a non-expert on some of this. Without this, confusion has only two exits — guess, or
    #   abandon the walk — and both look identical in the data afterwards.
    "question": "he does not understand this and wants it explained before he rules on it",
}

# ★★ HOW SOON, decided by him and not inferred from his tone. "Do this right now" and "shelve it for
#   a longer coding session" are different instructions with the same words in them, and an agent
#   guessing between them either interrupts his walk to start a two-hour job, or quietly files an
#   urgent thing for next week.
URGENCY = {
    "now": "do it now — interrupt what you are doing",
    "session": "shelve it for a longer working session",
    "none": "no timing stated",
}


def reply(text: str, step=None, target: str = "", kind: str = "answer",
          urgency: str = "none") -> dict:
    """★★★ HE MUST BE ABLE TO ANSWER THE AGENT FROM INSIDE THE GUIDE, in his words:
        "I want to be able to do that to answer you here too (or any agent for that matter that's
         running the guided pathway)."

    Until now the guide was one-directional — an agent could point and speak, and the only way back
    was to leave the board and type at a terminal. A guided flow he cannot answer inside is a
    lecture, not a conversation.

    ★★ It is append-only and SEPARATE from the row's note. The note belongs to the row and stays on
       the board forever; this is what he said TO THE AGENT while standing on that step, which is a
       different fact. Writing both into one field would lose which was which.
    ★ Append-only also means a reply cannot be overwritten by the next one, and fsync means the
      agent reading it a second later sees it."""
    text = (text or "").strip()
    if not text:
        # ★ A QUESTION NEEDS NO WORDS. Pressing "I do not understand this" while a row is on screen
        #   already says everything: this row, this person, right now. Demanding he articulate the
        #   confusion first is asking the one thing he cannot do yet.
        if (kind or "").strip().lower() == "question":
            text = "(no words — pressed the I-do-not-understand button on this row)"
        else:
            return {"ok": False, "error": "nothing typed"}
    kind = (kind or "answer").strip().lower()
    if kind not in KINDS_OF_REPLY:
        return {"ok": False, "error": f"kind must be one of {sorted(KINDS_OF_REPLY)}"}
    urgency = (urgency or "none").strip().lower()
    if urgency not in URGENCY:
        return {"ok": False, "error": f"urgency must be one of {sorted(URGENCY)}"}
    row = {"at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
           "step": step, "target": target or None, "kind": kind, "urgency": urgency,
           "text": text[:4000], "read": False,
           # ★ A directive is not done when it is recorded. It is OPEN until an agent acts, and the
           #   agent must be able to find the ones still owed — otherwise "I told you to" and "you
           #   never did" have no shared record to settle them.
           "acted": (None if kind in ("directive", "question") else "n/a")}
    try:
        with REPLIES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            import os as _os
            _os.fsync(fh.fileno())
    except OSError as e:
        return {"ok": False, "error": f"could not record your answer: {e}"}
    # ★★★ THE NOTE CONFIRMATION USED TO READ "nobody is being asked to act", AND HE PRESSED SEND
    #   FIVE TIMES IN THIRTY-FOUR SECONDS. He was telling me something that mattered — that a job
    #   was finished — and the system answered with a phrase that reads as "ignored". The wording was
    #   written to distinguish a note from a directive; it landed as a dismissal.
    # ★ A confirmation's job is to prove RECEIPT. Saying what will NOT happen is a footnote, not the
    #   headline, and a person repeating themselves is a user interface telling you something.
    said = {"directive": ("recorded as a DIRECTIVE — "
                          + URGENCY.get(urgency, "the agent is expected to act on it")),
            "note": "got it — saved, and the agent will see it (no action requested)",
            "question": "the agent has been asked to explain this one — it will answer before you rule",
            "answer": "the agent guiding you will see this"}
    return {"ok": True, "recorded": True, "at": row["at"], "kind": kind, "urgency": urgency,
            "note": said.get(kind, "recorded")}


def replies(unread_only: bool = False, n: int = 50) -> list:
    out = []
    # Legacy rows are history; `_transport_only` rows exist solely to wake the old CC watcher and
    # must never become a second copy of the operator's instruction in the shelf.
    for source in (LEGACY_REPLIES, REPLIES):
        try:
            for ln in source.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                    if not row.get("_transport_only"):
                        out.append(row)
                except ValueError:
                    continue
        except OSError:
            continue
    return out[-n:]


def answer(target: str, text: str, by: str = "the agent") -> dict:
    """★★★ THE LOOP WAS ONE AND A HALF DIRECTIONS. He could press "I don't understand", and it
    reached the agent — and then the agent's explanation had nowhere to land except a terminal he is
    not looking at. He is standing in front of the row, asking about THAT row; the answer belongs
    beside it, not in another window.

    ★ Keyed by the row, not by the step, deliberately: the walk can be re-ordered or re-run, and an
      explanation of `nocloud` is true about `nocloud` whenever he next meets it. It persists, so the
      second walk is cheaper than the first — which is the entire argument for writing these down.
    ★★ It NEVER touches the row's own words. An agent explaining a row is not the same as an agent
      editing his board, and conflating them is how a helpful gloss becomes an unreviewed rewrite."""
    target = (target or "").strip()
    text = (text or "").strip()
    if not target or not text:
        return {"ok": False, "error": "answer needs a row and something to say"}
    try:
        store = json.loads(ANSWERS.read_text(encoding="utf-8")) if ANSWERS.exists() else {}
    except Exception:
        store = {}
    store[target] = {"text": text[:6000], "by": by,
                     "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}
    tmp = ANSWERS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    tmp.replace(ANSWERS)
    # ★★★ APPEND THE SETTLEMENT, NEVER REWRITE THE LOG. The first version rewrote guide_replies.jsonl
    #   in place to flip `acted` on the answered question — and that truncate-and-rewrite made the
    #   watcher tailing this file see it as a NEW file and REPLAY EVERY REPLY as if it had just
    #   arrived. Fifteen old directives came back at once. Nothing was lost, but an agent reading
    #   that stream would have acted on each of them a second time, and "do it now" twice is not the
    #   same as once.
    # ★ An append-only log must stay append-only all the way down, including its own bookkeeping.
    #   The debt is settled by a later line, not by editing an earlier one.
    try:
        with REPLIES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": store[target]["at"], "kind": "acted",
                                 "target": target, "settles": "question", "by": by}) + "\n")
            fh.flush()
            import os as _os
            _os.fsync(fh.fileno())
    except OSError:
        pass
    return {"ok": True, "target": target, "at": store[target]["at"]}


def _answers() -> dict:
    try:
        return json.loads(ANSWERS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def push_task(steps: list, by: str = "an agent", why: str = "") -> dict:
    """★★★ A GUIDED RUN ON ONE ROW, THAT GIVES HIM BACK HIS PLACE AFTERWARDS.

    He asked: "Give me a guided run on this with the agent." Pushing a new sequence would have
    REPLACED the walk he is seven rows into — the guide would have helped him with one row by
    throwing away his progress on twenty. He has lost his place twice tonight already and it cost
    real time both times.

    ★★ So a task is a sequence WITH A RETURN ADDRESS. The walk is set aside intact, the task runs,
    and finishing the last step puts him back exactly where he was — same row, same position, same
    remaining count. Leaving the task early does the same thing.
    ★ It is the same store and the same poll: no second mechanism, no second surface, nothing new to
      learn. Only the return address is added."""
    live = _load() or {}
    if live.get("task"):
        return {"ok": False, "error": "a guided task is already running — finish or leave that one first"}
    resume = live if live.get("steps") else None
    r = set_sequence(steps, by=by, why=why, replace=True)
    if not r.get("ok"):
        return r
    d = _load()
    d["task"] = True
    d["resume"] = resume          # ★ the walk, preserved byte-for-byte, not a reconstruction
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "steps": r.get("steps"),
            "will_return_to": (f"row {resume['steps'][int(resume.get('i') or 0)].get('row_n')}"
                               if resume and resume.get("steps") else "no walk was running")}


def recheck(target: str) -> dict:
    """★★★ IS THIS ROW STILL TRUE? — asked of the board, not of memory.

    He asked for "a button that re-assesses something if it's stale". Half the rows he has walked
    tonight turned out to be already done, impossible, or duplicated — the board is weeks old in
    places and nothing re-measures it.

    ★★ THE STRONGEST SIGNAL IS FREE AND NOBODY WAS READING IT: a `derived:` row is COMPUTED at render
    time from a check. It is not stored. So if it is no longer on the live board, THE CHECK NOW
    PASSES — the thing resolved itself and the row evaporated. That is a real verdict, obtained by
    looking, in one request.
    ★ For a hand-written row the board cannot answer it: the row is a human claim about the world and
    only a measurement settles it. So this does NOT pretend — it records a directive so an agent
    re-investigates, and says plainly that is what it did. A button that returned a cheerful
    "still true!" without checking would be the decorative-green defect in a new place.
    """
    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "which row?"}
    try:
        import urllib.request
        board = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:7842/operator/board.json", timeout=30).read())
        items = {i.get("id"): i for i in board.get("items", []) if isinstance(i, dict)}
    except Exception as e:
        return {"ok": False, "error": f"could not read the board: {e.__class__.__name__}",
                "note": "this is 'could not look', not 'still true'"}
    row = items.get(target)
    if target.startswith("derived:"):
        if row is None:
            return {"ok": True, "verdict": "RESOLVED",
                    "says": "This row is GONE from the live board. Derived rows are recomputed from "
                            "their check every time the board is built, so its absence means the "
                            "check now PASSES. Nothing to do — it fixed itself."}
        return {"ok": True, "verdict": "STILL_FAILING",
                "says": "Still present on the live board, so its check is still failing as of the "
                        "last invariant run. The row's own text carries when that run was.",
                "state": row.get("state")}
    if row is None:
        return {"ok": True, "verdict": "GONE",
                "says": "This row is no longer on the board at all."}
    if row.get("state") != "open":
        return {"ok": True, "verdict": "SETTLED",
                "says": f"You already ruled on this one — it is {row.get('state')}.",
                "state": row.get("state")}
    directive = ("RE-CHECK REQUESTED from the guided walk: is this row still true? "
                 "It is hand-written, so the board cannot answer it — it needs measuring.")
    r = reply(directive, target=target, kind="directive", urgency="now")
    return {"ok": True, "verdict": "NEEDS_MEASURING",
            "says": "This is a hand-written row, so the board cannot tell you whether it is still "
                    "true — only a measurement can. I have asked the agent to re-measure it now, "
                    "and it is on the shelf marked DO NOW until it reports back.",
            "recorded": r.get("ok"), "reply": r, "directive": directive}


def shelf() -> dict:
    """★★★ THE SHELF — everything he told an agent to do, that an agent has not done yet.

    He said: "We'll need a place/button for working on the shelved items somewhere, both in the
    guided box and in the general GUI." By the end of the first walk this was the LARGEST thing on
    his board — a dozen instructions he had given — and it lived in a JSONL file only an agent could
    read. Work he has authorised, invisible to him, is the same defect as a board row nobody can see.

    ★ It is a list of MY debts, not his tasks. Each row is something he said and nobody has done.
    ★★ An item leaves this list only two ways: an agent records it DONE, or he WITHDRAWS it. It never
       ages out quietly — a debt that expires on its own is a debt nobody ever had to admit to.
    """
    rows = replies()
    settled = {}
    for r in rows:
        if r.get("kind") == "settled" and r.get("settles"):
            settled[r["settles"]] = r
    ans = _answers()
    out = []
    for r in rows:
        if r.get("kind") not in ("directive", "question"):
            continue
        key = r.get("at")
        if key in settled:
            continue
        a = ans.get(r.get("target") or "")
        out.append({"at": key, "kind": r.get("kind"),
                    "urgency": r.get("urgency") or "none",
                    "target": r.get("target"), "text": r.get("text"),
                    "answered": bool(a),
                    "answer_at": (a or {}).get("at"),
                    "answer": ((a or {}).get("text") or "")[:1800]})
    order = {"now": 0, "none": 1, "session": 2}
    out.sort(key=lambda r: (order.get(r["urgency"], 3), r["at"]))
    return {"count": len(out), "items": out,
            "by_urgency": {u: sum(1 for r in out if r["urgency"] == u) for u in ("now", "session", "none")}}


def settle(at: str, how: str = "done", note: str = "") -> dict:
    """Take one item off the shelf. ★ Append-only, like everything else in this log."""
    if not at:
        return {"ok": False, "error": "which item? its timestamp identifies it"}
    how = (how or "done").strip().lower()
    if how not in ("done", "withdrawn"):
        return {"ok": False, "error": "how must be 'done' or 'withdrawn'"}
    live = {r.get("at") for r in replies() if r.get("kind") in ("directive", "question")}
    if at not in live:
        return {"ok": False, "error": f"no shelved item recorded at {at}"}
    try:
        with REPLIES.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z"),
                                 "kind": "settled", "settles": at, "how": how,
                                 "note": (note or "").strip()[:500]}) + "\n")
            fh.flush()
            import os as _os
            _os.fsync(fh.fileno())
    except OSError as e:
        return {"ok": False, "error": f"could not record it: {e}"}
    return {"ok": True, "settled": at, "how": how}


def goto(row_n: int) -> dict:
    """★ JUMP STRAIGHT TO A ROW BY ITS OWN NUMBER — never by arithmetic on the step index.

    He asked for this after losing his place twice: "I can't see row 16, need a button for a jump-to
    to current spot selected." A walk you can only step through one at a time is a walk you cannot
    recover from once you are lost, and being lost is exactly when you need to move.
    ★★ It matches on the step's OWN row_n. Computing `i = row_n + 1` would bake in an assumption
    about how many opening steps exist — the same off-by-one that put 'STEP 15' and 'Row 14' on one
    screen and made him lose his place in the first place."""
    d = _load()
    if not d or not d.get("steps"):
        return {"ok": False, "error": "no walk is running"}
    try:
        want = int(row_n)
    except (TypeError, ValueError):
        return {"ok": False, "error": "which row? a number, please"}
    idx = next((k for k, st in enumerate(d["steps"]) if st.get("row_n") == want), None)
    if idx is None:
        rows = [st.get("row_n") for st in d["steps"] if st.get("row_n")]
        return {"ok": False,
                "error": f"this walk has rows {min(rows)}-{max(rows)}" if rows else "this walk has no numbered rows",
                "asked_for": want}
    was = int(d.get("i") or 0)
    d["i"] = idx
    d["done"] = False
    d["last_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    _journal("goto", was, idx, len(d["steps"]))
    return {"ok": True, "row": want, "of": d["steps"][idx].get("row_total"),
            "target": d["steps"][idx].get("target")}


def back() -> dict:
    """★ A WALK YOU CANNOT REVERSE IS A CONVEYOR BELT. One stray Enter and the row is behind him with
    no way to look again — and Enter is the advance key, pressed by reflex. The step he just left is
    the single most likely thing he wants back."""
    d = _load()
    if not d or not d.get("steps"):
        return {"ok": False, "error": "nothing to go back to"}
    i = int(d.get("i") or 0)
    if i <= 0:
        return {"ok": False, "error": "you are on the first step", "step": 1}
    d["i"] = i - 1
    d["done"] = False           # ★ stepping back out of a finished walk re-opens it, correctly
    d["last_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    STATE.write_text(json.dumps(d, indent=2) + "\n")
    _journal("back", i + 1, i, len(d["steps"]))
    return {"ok": True, "step": i, "of": len(d["steps"])}


def leave_task() -> dict:
    """★ Leaving a task must never cost him the walk either — same restore, no completion required."""
    d = _load() or {}
    if not d.get("task"):
        return {"ok": False, "error": "no guided task is running"}
    back = d.get("resume")
    if not back:
        return dismiss()
    back["last_at"] = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    STATE.write_text(json.dumps(back, indent=2) + "\n")
    st = _step_view(back) or {}
    return {"ok": True, "returned_to_walk": True, "row": st.get("row_n"), "of": st.get("row_total")}


def dismiss() -> dict:
    """★ Always available. A guide he cannot leave is a captor."""
    if STATE.exists():
        STATE.unlink()
    return {"ok": True, "note": "guide dismissed. Nothing was recorded as done."}


def self_test(prove_fail: bool = False) -> int:
    """★ NEGATIVE CONTROL. Three failures matter: pointing at something that does not exist, moving him
    with nothing to say, and a focus that cannot be dismissed."""
    fails = []
    import tempfile
    global STATE, REPLIES, LEGACY_REPLIES
    saved = STATE, REPLIES, LEGACY_REPLIES
    try:
        td = Path(tempfile.mkdtemp())
        STATE, REPLIES, LEGACY_REPLIES = td / "g.json", td / "messages.jsonl", td / "legacy.jsonl"
        expected_kinds = {"item", "control", "section", "none"}
        if prove_fail:
            expected_kinds.add("deliberately-missing-negative-control")
        if set(KINDS) != expected_kinds:
            fails.append(f"focus-kind SET differs: expected {sorted(expected_kinds)}, got {sorted(KINDS)}")
        if set_focus("nonsense", "x", "do it").get("ok"):
            fails.append("accepted an unknown focus kind")
        if set_focus("control", "no-such-control", "do it").get("ok"):
            fails.append("accepted a control that does not exist — he would be sent to nothing")
        if set_focus("item", "abc", "   ").get("ok"):
            fails.append("★ accepted a focus with no instruction — moving him for no reason is the "
                         "flashiness without the guidance")
        r = set_focus("item", "abc", "press Done on this row", 2, 5, by="test")
        if not r.get("ok"):
            fails.append("a valid focus was refused")
        if not current().get("active"):
            fails.append("a focus that was just set is not active")
        dismiss()
        if current().get("active"):
            fails.append("★ a dismissed guide is still active — he cannot leave it")
        # ★ an active focus set by someone ELSE must not be silently replaced
        set_focus("item", "abc", "his live guide", 1, 3, by="agent-A")
        clobber = set_focus("item", "zzz", "probe", by="agent-B")
        if clobber.get("ok"):
            fails.append("★ another agent's ACTIVE guide was silently replaced — that happened for "
                         "real, on the panel he was reading")
        if not set_focus("item", "zzz", "probe", by="agent-B", replace=True).get("ok"):
            fails.append("an explicit replace:true takeover was refused")
        dismiss()
        set_focus("item", "abc", "his live guide", 1, 3, by="agent-A")
        dr = set_focus("item", "zzz", "probe", by="agent-B", replace=True, dry_run=True)
        if not dr.get("dry_run") or dr.get("changed"):
            fails.append("dry_run on focus changed something or did not report itself")
        if current().get("target") != "abc":
            fails.append("★ a dry_run overwrote the live focus — the one thing it must not do")
        dismiss()
        # staleness
        set_focus("item", "abc", "x")
        d = json.loads(STATE.read_text()); d["at"] = "2020-01-01T00:00:00+0000"
        _ss().write_json(STATE, d)
        if current().get("active"):
            fails.append("a focus set years ago is still lighting something up")
        # The canonical message and the CC wake-up envelope must not be two visible instructions.
        LEGACY_REPLIES.write_text(json.dumps({"at": "old", "kind": "note", "text": "history"}) + "\n")
        reply("canonical", target="abc", kind="note")
        with LEGACY_REPLIES.open("a") as fh:
            fh.write(json.dumps({"at": "wake", "kind": "note", "text": "canonical",
                                 "_transport_only": True}) + "\n")
        texts = [r.get("text") for r in replies(n=20)]
        if texts != ["history", "canonical"]:
            fails.append(f"message/transport SET differs: expected one history + one canonical, got {texts}")
    finally:
        STATE, REPLIES, LEGACY_REPLIES = saved
    for f in fails:
        print(f"  ✗ {f}")
    print("  ✓ guide_focus self_test: refuses unknown targets and empty instructions, can be dismissed, "
          "and lets a stale focus go" if not fails else "  ✗ guide_focus self_test FAILED")
    if not fails:
        print(f"  PROVE-FAIL: {Path(__file__).name} --self-test --prove-fail (must exit 1)")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        return self_test("--prove-fail" in a)
    if a and a[0] == "--set" and len(a) >= 4:
        print(json.dumps(set_focus(a[1], a[2], " ".join(a[3:])), indent=2)); return 0
    if a and a[0] == "--answer" and len(a) >= 3:
        print(json.dumps(answer(a[1], " ".join(a[2:])), indent=2)); return 0
    if a and a[0] == "--dismiss":
        print(json.dumps(dismiss(), indent=2)); return 0
    if a and a[0] in ("--replies", "--directives"):
        # ★ THE AGENT MUST BE ABLE TO FIND WHAT HE ASKED FOR. A reply channel nobody can read back is
        #   a suggestion box nailed shut — and an UNACTED DIRECTIVE is the one thing here with a debt
        #   attached to it, so it is listed separately and loudly.
        want_directives = (a[0] == "--directives")
        rows = replies()
        settled = {r.get("target") for r in rows if r.get("kind") == "acted" and r.get("target")}
        done_at = {r.get("settles") for r in rows if r.get("kind") == "settled" and r.get("settles")}
        rows = [r for r in rows if r.get("kind") not in ("acted", "settled")
                and r.get("at") not in done_at]
        if want_directives:
            rows = [r for r in rows
                    if r.get("kind") in ("directive", "question")
                    and r.get("acted") in (None, False)
                    and not (r.get("kind") == "question" and r.get("target") in settled)]
        if not rows:
            print("  nothing outstanding" if want_directives else "  no replies yet")
            return 0
        for r in rows:
            k = (r.get("kind") or "answer").upper()
            owed = r.get("acted") in (None, False) and k in ("DIRECTIVE", "QUESTION")
            u = (r.get("urgency") or "none")
            tag = f"{k}{'/' + u.upper() if u != 'none' else ''}"
            mark = "\u2605 " if owed else "  "
            where = str(r.get("target") or "(no row)")[:36]
            said = " ".join(str(r.get("text", "")).split())[:140]
            print(f"{mark}{r.get('at','')[11:19]}  {tag:<18}{where:38}{said}")
        return 0
    print(json.dumps(current(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
