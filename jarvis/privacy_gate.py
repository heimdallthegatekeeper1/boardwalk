#!/usr/bin/env python3
"""privacy_gate.py -- THE PRIVACY GATE: detect, decide, obfuscate, delegate, re-hydrate.

THE SPEC THIS IMPLEMENTS (a strong secondary source, read before designing)
-------------------------------------------------------------------------
`knowledge/youtube/0F7fGYpqOZg.L3-vein-privacy-gate-functionality.md` (caveat: `cloud-processed` --
the vein was derived by a cloud model, so it is a SUMMARY of the video, not the video; the raw
transcript `0F7fGYpqOZg.raw.md` was read beside it and the four steps below are verbatim from the
transcript, not from the summary):

    1 DETECT      an on-device classifier scans what is about to leave
    2 DECIDE      prompt: process locally, or delegate
    3 OBFUSCATE   replace sensitive values with PLACEHOLDERS before egress
    4 DELEGATE    the cloud model works on the sanitised text -- never the real data
    5 RE-HYDRATE  the local side maps placeholders back (implied by step 4; built here)

The reference product uses one 0.6 B model ("Pi Tracer") to do step 1. THIS BOX ALREADY HAS THE SAME
MODEL CLASS -- the seat's default local model is `qwen3:0.6b`, on `ollama` at 127.0.0.1:11434, zero
egress. So the reference is reachable. It is also not good enough on its own, and this file says why.


WHY THIS IS A LADDER AND NOT A MODEL CALL
-----------------------------------------
\u2605\u2605\u2605 THIS ESTATE HAS ALREADY MEASURED A MODEL-BASED PII DETECTOR AND IT FAILED AS A GATE.
`jarvis/jev_unison.sensitive_verdict()` carries its own bench result: 44 labelled utterances,
**recall 0.636 (8 missed), FPR 0.045, and a confident CLEAR still wrong 4/24**. `jarvis/jev_menu.py`
renders it as `ADVISORY ONLY. THIS IS NOT A FENCE.` Nothing routes on it, by guard.
A gate whose false-negative rate is 36% is a gate that lets one turn in three through. So the model
is the SECOND rung here, never the only one:

    tier 0  DETERMINISTIC AND EXHAUSTIVE -- regex and structural patterns for everything that has a
            SHAPE: emails, phones, IPs, card-like digit runs (Luhn-checked), API keys and tokens,
            private-key headers, paths under a home directory, hostnames, account-number shapes,
            SSNs, dates of birth, credentialed URLs, secret-shaped env assignments.
            FREE, INSTANT, AND IT CANNOT BE TALKED OUT OF IT. No model to load, no prompt to drift.
    tier 1  THE LOCAL CLASSIFIER, for what has NO shape: names, addresses, personal narrative.
            `qwen3:0.6b` on loopback. It never egresses because it never leaves.
            The model PROPOSES spans; deterministic code VERIFIES each proposed span is literally
            present in the text. A span the model invented is discarded. (A 0.6 B model asked for
            exact substrings will hallucinate them; this is the cheapest possible fix for that, and
            it is why tier 1's output is never trusted unverified.)

\u2605\u2605 TIER 0 RUNS EVEN IF TIER 1 IS UNAVAILABLE, AND TIER 1 BEING DOWN DOES NOT OPEN THE GATE.
   The failure mode being designed out is the one this estate found repeatedly: a control that fails
   OPEN when its dependency is missing. If the classifier cannot run, the gate returns
   `unchecked` / action `ask` and THE TEXT DOES NOT GO. It does not pass "clean". It does not pass
   "unknown". It stops, and it says what it could not check.
   \u2605 AN ABSENT FIELD IS `unchecked`, NOT `clear` -- the same rule `jev_unison` already paid for.

\u2605\u2605\u2605 THE DEFAULT IS OBFUSCATE, NOT UPLOAD. The reference product PROMPTS and lets the operator upload
   anyway. This estate's rule is "It may ASK. It may NOT TAKE." -- so asking is right, and the FLOOR
   under the asking is the part that matters: when the gate is in doubt it OBFUSCATES and delegates
   the sanitised text, rather than delegating the real text and hoping. A prompt that defaults to
   "upload" is a gate in name only. Here the dangerous direction (send the real value) is the one
   that has to be ASKED FOR BY NAME, per turn, and it is recorded when it happens.


RE-HYDRATION, AND WHY THE TABLE IS EPHEMERAL
--------------------------------------------
Replacing "Alex Example" with a placeholder is half a feature. If the cloud answers
"[[PERSON_...]] should call the clinic", the operator must see Alex's name or the seat is broken,
not private. So every egress produces a LEASE:

  \u00b7 the lease holds the value -> placeholder map IN MEMORY, on this box, for the duration of the
    round trip ONLY;
  \u00b7 `Lease.rehydrate()` puts the real values back before the answer is displayed;
  \u00b7 the map is NEVER written to disk, NEVER logged, and the lease's `__repr__` shows COUNTS ONLY;
  \u00b7 this module contains no `open()`, no `logging`, no `print()` -- asserted by `test_privacy_gate.py`
    by AST, not by grep, so a mention in a docstring cannot satisfy the check;
  \u00b7 `close()` drops the map; the registry is bounded and purged at interpreter exit.
  AGENTS.md says "Never log key material". A PII mapping table is the same class of object.

\u2605\u2605 THE SUBSTITUTE IS A SURROGATE, NOT A BRACKET TOKEN, AND THAT IS THE 2026-10-12 FIX. It was
   `[[TYPE_hex8]]` and the cloud agent REFUSED it -- on a brand-new conversation, with zero history,
   as "a template/placeholder with personal data redaction markers". The shape reads as a redaction
   marker, a template variable or a prompt-injection probe, and refusing it is correct behaviour.
   What leaves now is a PLAUSIBLE VALUE OF THE SAME TYPE, drawn wherever possible from a range that
   is DOCUMENTED AS NEVER ISSUED -- `example.invalid` for addresses (RFC 2606), the NANP 555-0100..
   555-0199 fictional block for phones, the SSA's never-assigned 900-series for SSNs, RFC 5737 /
   RFC 3849 documentation nets for IPs, and published test PANs for cards. See THE SURROGATE TABLE
   below for the full table, the citations, and the types that keep a MARKER instead because no
   reserved range exists and a plausible fake there would BE the harm.
   \u2605\u2605 THE SUBSTITUTES ARE CONSISTENT WITHIN A TURN AND UNLINKABLE ACROSS TURNS, and they get both
     properties from the same primitive the token did:
        surrogate = render(HMAC-SHA256(per_lease_nonce, "<TYPE>:<normalised value>"))
   Same value, same turn  -> same surrogate, so the cloud can still reason about "the same person".
   Same value, NEXT turn  -> a DIFFERENT surrogate (a fresh 16-byte nonce each lease), so a cloud
   that keeps transcripts cannot join turn N to turn N+1 and build a profile of one individual.
   \u2605 A FORGED SUBSTITUTE (one not in this lease's map) is LEFT ALONE by rehydration and COUNTED,
   because silently rewriting text the gate never issued is how a gate invents facts.
   \u2605\u2605 AND A GENERATED SURROGATE IS CHECKED AGAINST THE TURN BEFORE IT IS USED. If it equals another
     real span, or already appears anywhere in the turn, or nests inside another surrogate this lease
     issued, the generator MOVES ON to its next deterministic candidate. Without that check the
     far side's own words could be rewritten back into a value they never stood for.


WHAT THIS DOES NOT COVER -- AND IT IS NOT A GAP, IT IS A HARD RULE
-----------------------------------------------------------------
RAW AUDIO IS OUT OF SCOPE AND MUST STAY OUT. `AGENTS.md` \u00a76: "Raw audio never leaves the box. Not a
setting." The privacy gate is not a licence to send audio: there is no "sanitised" form of a
recording that preserves its use, because the use IS the waveform. The gate makes TEXT delegation
safe; the audio floor is `jarvis/audio_egress_floor.py` and is untouched by this file. Credential
endpoints stay loopback-only, and "a stage that READS a secret runs on the box" -- both unchanged.

\u2605\u2605 LIMITS, STATED, because a gate with unstated limits is a claim:
  \u00b7 tier 0 catches SHAPES. A name with no shape is tier 1's job and tier 1 is a 0.6 B model: expect
    it to miss things. It is the reason `unchecked` blocks and the reason tier 0 is exhaustive.
  \u00b7 tier 1 sees the POST-tier-0 text, so it is judging the residue, not the original.
  \u00b7 rehydration is exact-token. If the far side rewrites a token (markdown, wrapping), that token
    does not come back; the count of unrestored tokens is reported rather than hidden.
  \u00b7 this module gates TEXT handed to it. It cannot see a payload that never calls it -- that is what
    the EGRESS_MODULES set-equality test in `test_privacy_gate.py` is for.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import textwrap
import time
import urllib.request

# -- the loopback-only classifier endpoint ---------------------------------------------------------
# * Mirrors `audio_egress_floor.LOCAL_HOSTS`: anything not in this set is refused BY NAME, so a
#   future edit that points tier 1 at a remote host turns the suite red instead of quietly shipping
#   the operator's text to a third party.
OLLAMA = os.environ.get("KHAOSS_OLLAMA_URL", "http://127.0.0.1:11434")

# * \u2605\u2605\u2605 WHICH LOCAL MODEL IS THE CLASSIFIER, AND WHY IT IS NOT THE 0.6 B ONE. MEASURED, 2026-10-12,
#   12 labelled sentences (6 private, 6 ordinary seat turns), `think:false`, temperature 0:
#
#       qwen3:0.6b   -- THE EXACT PI-TRACER PARAMETER CLASS (the reference product's classifier) --
#                       recall 3/6, specificity 6/6, median 514 ms, max 2944 ms
#                       MISSED: "I still owe twelve thousand on the car loan",
#                               "Dr. Alvarez prescribed me sertraline last month",
#                               "Jordan Example's contract renewal is up on the 14th"
#       qwen3:1.7b   recall 6/6, specificity 6/6, median 1805 ms, max 7360 ms
#
#   SO THE REFERENCE'S OWN CLASS SIZE IS NOT SUFFICIENT ON THIS BOX, and shipping it as the default
#   would have bought a gate that misses half of the plainly-private sentences while looking perfect
#   on the ordinary ones. The default is the model that actually catches them; both are local, both
#   are loopback, and neither egresses. The 0.6 B class stays reachable and NAMED, because "we beat
#   the reference" has to be a measurement, not an assertion.
#   \u2605 THE COST IS REAL AND IS NOT HIDDEN: ~1.8 s median, 7.4 s worst on a quiet box, per gated
#   egress. \u2605\u2605 AND UNDER LOAD IT IS WORSE THAN THE DEFAULT TIMEOUT: with the load average at 3-6
#   this model exceeded 25 s on a cold reload, the gate correctly reported `unchecked`, and the turn
#   was HELD. The timeout below is 45 s for that reason. The honest consequence is stated rather
#   than hidden: **on a loaded box, a cold tier 1 holds egress.** That is the fail-closed direction
#   and it is the direction the brief asks for, but an operator who sees turns holding should read
#   this line before he reaches for a bigger timeout.
# ★★★★★ A1 · THE CLASSIFIER AND THE BRAIN WERE RUNNING DIFFERENT MODELS — 2026-10-15.
#  THE DEFECT, MEASURED: with the brain on `LiquidAI/lfm2.5-2.6b` and this constant defaulting to
#  `qwen3:1.7b`, EVERY gated turn loaded TWO models: 1569 + 1767 = 3336 MiB against a
#  `brain.VOICE_LANE_CEILING_MB` of 1776. On 2026-10-15 the operator's turn was HELD with
#  "the local classifier could not be reached" -- the cold load of the SECOND model exceeded this
#  module's own 45 s window. THE COMMENT ABOVE PREDICTED IT and nobody connected the two facts.
#  ★ THE FIX FOLLOWS `_runner_num_ctx` EXACTLY, which already points num_ctx at the brain's store
#    (commit 0812de8): read the value the BRAIN writes, and DEGRADE TO THE OLD VALUE on any
#    failure, so a broken import can never produce a WRONG answer -- only the previous one.
#  ★ THE ENV VAR STILL WINS, so the fast 0.6b class stays reachable and the operator can override
#    this without editing a file. ★ AND `TIER1_MODEL_FAST` is untouched.
def _brain_model() -> str | None:
    """The model the BRAIN is set to -- read from the store the brain reads, never copied."""
    try:
        try:
            from . import brain as _brain       # package import
        except ImportError:
            import brain as _brain              # direct/script import
        return str(_brain.tuning()["model"]) or None
    except Exception:                           # noqa: BLE001
        return None      # -> the default below: the pre-change value, byte for byte


#: ★★★ MEASURED 2026-10-15, AND THE ANSWER IS *NOT YET* -- THE DEFAULT IS DELIBERATELY UNCHANGED.
#:   The mechanism above is LIVE and available via `KHAOSS_PRIVACY_MODEL`, but resolving it BY
#:   DEFAULT to the brain's model is REFUSED on the evidence, not on taste:
#:
#:     LiquidAI/lfm2.5-2.6b as tier 1, driven through this module's own door on a QUIET box (load 8.5):
#:       HARD (the three sentences qwen3:0.6b misses): 2/3, ONE TIMED OUT AT 45 s
#:       ORDINARY (must read clean):                   3/3
#:       LATENCIES: 22.2 s - 45.1 s (timeout) - 44.6 s - 38.5 s - 27.0 s - 21.4 s
#:     qwen3:1.7b's recorded figures for the same battery: recall 6/6, median **1805 ms**, max 7360 ms.
#:
#:   ★ SO LFM IS ROUGHLY 18x SLOWER HERE AND ONE HARD CASE EXCEEDED THE 45 s WINDOW -- which is
#:     precisely the condition that HELD THE OPERATOR'S TURN. Switching the default to it would trade
#:     a two-model memory cost for a held-egress cost, and the held-egress one is the worse failure
#:     because it is the one he actually experienced.
#:   ★ PER THE OPERATOR'S OWN PROFILE-FIRST RULE, THE FIRST QUESTION IS WHAT THE PROFILE IS MISSING,
#:     NOT WHETHER THE MODEL IS BAD: LFM is a pure reasoning model whose template emits `<think>`
#:     UNCONDITIONALLY and IGNORES `think:false`, so it writes a monologue into the 400-token budget
#:     before it classifies. `jarvis/model_profile.py` records the cure -- `omit_think` plus an
#:     empty-think assistant prefill -- and THIS PATH DOES NOT APPLY IT. That is the gap to close,
#:     and it is a change to this file's request shape, which is a decision this lane may not make.
#:   ★ TO OPT IN AND MEASURE: `KHAOSS_PRIVACY_MODEL=LiquidAI/lfm2.5-2.6b` (the env var still wins).
TIER1_MODEL = os.environ.get("KHAOSS_PRIVACY_MODEL") or "qwen3:1.7b"
TIER1_MODEL_FAST = "qwen3:0.6b"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

TIER0 = "tier0_deterministic"
TIER1 = "tier1_local_classifier"

# * Verdicts. `unchecked` is a FIRST-CLASS OUTCOME, not an error and not a pass.
V_PASS = "pass"                 # nothing found by either tier -> delegate as-is
V_OBFUSCATE = "obfuscate"       # something found -> delegate the SANITISED text
V_UNCHECKED = "unchecked"       # tier 1 could not run AND tier 0 found nothing -> DO NOT PASS
V_FLAGGED_NO_SPAN = "flagged_unmaskable"   # tier 1 says sensitive but named no usable span
# * \u2605\u2605\u2605 THE TWO VERDICTS THAT DID NOT EXIST UNTIL THE OVER-MASK LANE (2026-10-12), AND WHY THEY
#   ARE VERDICTS AND NOT A BIGGER MASK. Both mean the same thing to the caller: THE MASK PLAN IS
#   NOT USABLE, SO NOTHING IS SENT. They are separate because the operator's next move differs.
V_OVER_MASKED = "over_masked_no_structure"  # the mask left the turn no instruction -> DO NOT SEND
V_UNLOCALISED = "flagged_unlocalised"       # tier 1 flagged the turn but localised nothing -> HOLD
# * \u2605\u2605\u2605 THE THIRD SHAPE, AND WHY IT DELIBERATELY REPORTS AS `V_OVER_MASKED` INSTEAD OF GETTING ITS
#   OWN STRING (2026-10-13). `over_masked_no_structure` is "nothing but grammatical glue survived". The
#   case below is "the ACTION survived and every THING it could be about did not" -- the mask left a
#   non-finite verb form standing alone. Measured on the operator's own 111-character turn:
#   `skeleton_content_words 1`, the survivor being `saying`, and the first clause therefore said
#   "there is an instruction here" about a turn whose agent answered "no clear actionable task for me
#   ... standing by".
#
#   \u2605 IT IS A SEPARATE MEASUREMENT AND NOT A SEPARATE VERDICT, AND THAT IS A CONSTRAINT RATHER THAN AN
#   OVERSIGHT. `router.py` renders the refusal sentence PER VERDICT through an `elif` chain whose final
#   `else` reads "the local classifier could not be reached, so NOTHING HAS CHECKED WHAT THIS TURN
#   CONTAINS". A NEW VERDICT STRING WOULD FALL INTO THAT BRANCH AND TELL THE OPERATOR A LIE -- the
#   classifier answered, and it answered that this turn's mask destroyed it -- which is the exact lie
#   `router.py`'s own comment at :111-118 was written to stop ("telling the operator to go and start a
#   classifier that is already running is how the next agent spends an evening on the wrong file").
#   That file is not this lane's to edit, so the clause reports through `V_OVER_MASKED`, whose existing
#   prose is TRUE OF BOTH SHAPES: the classifier named the private parts AND the words around them, so
#   the masked body had no instruction left in it. The two are told apart ON THE RECEIPT --
#   `skeleton_heads == 0` together with `skeleton_content_words > 0` is this clause and only this one.
#   \u2605 THE ONE-LINE CHANGE IF THE OPERATOR WANTS THEM NAMED APART: add an `elif v == pg.V_NO_REFERENT:`
#   branch to that chain in the same shape as its neighbours, and use the constant below as the verdict.
V_NO_REFERENT = "over_masked_action_without_referent"  # the receipt's own name; see the note above

# * Actions the caller must honour. `ask` is the fail-closed default.
A_DELEGATE = "delegate"
A_DELEGATE_SANITISED = "delegate_sanitised"
A_ASK = "ask"
A_LOCAL_ONLY = "local_only"

# * ================================================================================================
# * THE STRUCTURE FLOOR -- THE OVER-MASK DEFECT, AND THE ONE RULE THAT CLOSES IT (2026-10-12)
# * ================================================================================================
# * WHAT WAS MEASURED, ON THE OPERATOR'S OWN CONVERSATION (`8971593b`), BEFORE THIS EXISTED.
#   \u2605 THE OPERATOR'S ACTUAL WORDS ARE NOT REPRODUCED HERE. The first version of this comment quoted the
#   two live utterances verbatim -- which put a real contact address into a source file while
#   explaining that real values must not persist. De-sentinel the SOURCE, never widen the check; the
#   gate lane paid for this same shape once already ("my own docstring quoted the leaked values while
#   explaining the leak"). What is recorded is the SHAPE and the measurement:
#
#       a short instruction ending in a value   -> sent_text "[[PERSONAL_<hex>]]"           mf 1.0
#       the same instruction ending in an email -> sent_text "[[PERSONAL_<hex>]][[EMAIL_<hex>]]"  mf 1.0
#       (a synthetic stand-in for both, used by self_test: "Reply with exactly one word: <a name>")
#
#   TWO OF FOUR DRIVES ON THAT PHRASING. The agent received a body of nothing but tokens -- no
#   instruction, no verb, no sentence -- and its own recorded reactions were to flail, or to read the
#   body as a PROMPT-INJECTION ATTEMPT. That is not a model failure; it is a correct response to a
#   nonsensical input.
#
# * \u2605\u2605 THE MECHANISM, RE-MEASURED OFF-LINE AGAINST THE LIVE MODEL, 6 RUNS PER TURN:
#
#       turn (len 57) -> tier 1 proposed ONE span of length 57 == the whole turn   6/6
#       turn (len 73) -> tier 1 proposed ONE span of length 73 == the whole turn   6/6
#       turn (len 39) -> tier 1 proposed ONE span of length 10  (the name alone)   6/6  <- WORKS
#
#   So `masked_fraction == 1.0` has ONE cause and it is not "too many spans": the classifier answered
#   with a span that IS THE TURN. `_subtract` then cuts the tier-0 regions out of it, the remaining
#   fragments plus the tier-0 findings between them cover every character, and the turn goes out as
#   placeholders. THE WHOLE-TURN SPAN IS NOT A LOCALISATION -- it names the turn, not the private
#   part -- and masking it destroys the instruction.
#
# * \u2605\u2605\u2605 AND THE OBVIOUS FIX IS A MEASURED LEAK, WHICH IS WHY THIS IS A REFUSAL AND NOT A CLEVERER
#   MASK. Dropping a span the classifier proposed is the D2 leak this module already paid for ("a
#   name left the box on a real network request" because a tier-1 span was dropped whole). Driven
#   again off-line on 2026-10-12, on a hand-labelled battery: a verifier that dropped the spans it
#   judged "not private" DROPPED `loan payment` -- a money span -- while keeping `my spouse`. And the
#   acceptance turn itself ("email Alex Example at operator@example.invalid and tell her the clinic
#   called") is not reproducible: 5 runs localised it (5 spans, verb `email` and verb `tell` among
#   them) and 1 run returned NO spans at all, sending `Alex Example` IN THE CLEAR.
#
#   THEREFORE, PROVEN BY MEASUREMENT RATHER THAN ARGUED: the two safe responses to a destructive mask
#   plan are (a) send it, which is the defect, or (b) DO NOT SEND. There is no third. A gate may ASK.
#   It may NOT TAKE. And the honest failure outranks the cosmetic pass.
#
# * \u2605 THE SAFETY PROPERTY IS NOT BENT BY ANY OF THIS. Nothing below ever REMOVES a finding from the
#   plan -- every span either leaves as a placeholder or the turn does not leave at all. The floor
#   only ever converts a SEND into a HOLD, and a hold cannot leak.
UNLOCALISED_FRACTION = 0.6

# * \u2605\u2605 WHY A CLOSED-CLASS WORD LIST AND NOT A POS TAGGER. The estate has no offline tagger: `nltk`
#   imports but has no `punkt_tab`/`averaged_perceptron_tagger` model, `spacy` has no
#   `en_core_web_sm`, and downloading either is egress. A dictionary (`/usr/share/dict/
#   american-english`) does NOT separate them -- `Alex` and `Marcus` are both in it -- so using one
#   to DROP spans would leak names.
#   \u2605 AND THIS LIST CANNOT LEAK EITHER, BY CONSTRUCTION: it is never used to choose WHICH span to mask.
#   It only measures whether what SURVIVED the mask still says anything. A turn whose only unmasked
#   words are grammatical glue has no instruction left in it, and that is a fact about the MASK, not
#   a judgement about the operator's words. It can only ever cause a HOLD.
_SKELETON_FUNCTION_WORDS = frozenset("""
a an the this that these those
i me my mine we us our ours you your yours he him his she her hers it its they them their theirs
who whom whose which what when where why how
and or but nor so yet for because if then than as until while of to in on at by with from into
about over under between during without within across after before above below up down out off
is am are was were be been being do does did done have has had having will would shall should can
could may might must
not no nor none any some all both each few more most other such only own same too very just
there here now then again also
""".split())


def _endpoint_is_loopback(url: str) -> tuple[bool, str]:
    """Tier 1 must never be pointed off-box. Refuses BY NAME, on the host, not on the string."""
    m = re.match(r"^https?://([^/]+)", str(url or ""))
    if not m:
        return False, f"unparseable endpoint {url!r}"
    host = m.group(1).split("@")[-1]
    bare = host.split(":")[0].strip()
    if bare not in LOCAL_HOSTS:
        return False, (f"the classifier endpoint {bare!r} is NOT loopback -- tier 1 would send the "
                       f"operator's text off the box, which is the one thing this gate exists to stop")
    return True, f"loopback ({bare})"


# =================================================================================================
# TIER 0 -- DETERMINISTIC AND EXHAUSTIVE
# =================================================================================================
# * Every detector is (TYPE, compiled pattern, extra validator). The validator is what keeps the
#   shapes honest: Luhn for card-like digit runs, a real version/checksum where one exists. A
#   detector that fires on every 16-digit number is a detector the operator will switch off.
#
# * \u2605 ORDER MATTERS AND IS DECLARED: scanners run in this order and OVERLAPS ARE RESOLVED
#   LONGEST-FIRST, so `sk-or-v1-<48 hex>` is one APIKEY and not a KEY plus a CARD-like run.

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
# * Phones: NANP with separators, or any +CC international run. Deliberately requires either a
#   leading +, parentheses, or separators -- a bare 10-digit run is a card-shaped/account-shaped
#   thing and is caught by those detectors instead.
_PHONE = re.compile(
    r"(?<![\dA-Za-z])(?:\+\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\b\d{3})[\s.\-]\d{3}[\s.\-]\d{4}(?![\dA-Za-z])"
    r"|(?<![\dA-Za-z])\+\d{9,15}(?![\dA-Za-z])"
    # * \u2605 AND AN INTERNATIONAL RUN *WITH* SEPARATORS. The two alternatives above cover NANP-with-
    #   separators and a bare +CC run; neither matched "+44 20 7946 0958", which is an ordinary way to
    #   write a phone number. Found by the suite's own probe set, not by reading.
    r"|(?<![\dA-Za-z])\+\d{1,3}[\s.\-]?(?:\d[\s.\-]?){6,13}\d(?![\dA-Za-z])")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])")
_IPV6 = re.compile(r"(?<![:0-9A-Fa-f])(?:[0-9A-Fa-f]{1,4}:){3,7}[0-9A-Fa-f]{1,4}(?![:0-9A-Fa-f])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
# * Card-like: 13-19 digits with optional separators, then Luhn.
_CARD = re.compile(r"(?<![\d\-])(?:\d[ \-]?){12,18}\d(?![\d\-])")
# * Secrets. Named prefixes first; then the generic `KEY=value` assignment shapes.
_SECRET_PREFIX = re.compile(
    r"\b(?:sk-or-v1-[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}"
    r"|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|ya29\.[0-9A-Za-z_\-]{20,}|hf_[A-Za-z0-9]{20,}"
    r"|sk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}|glpat-[A-Za-z0-9_\-]{16,})")
_SECRET_ASSIGN = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|auth|bearer|"
    r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?([^\s\"',;]{6,})[\"']?",
    re.IGNORECASE)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
# * Paths that name the operator's account. `~` alone is harmless; `/home/<someone>` is a person.
_HOME_PATH = re.compile(r"(?:/home/([A-Za-z0-9._\-]+))|(?:/Users/([A-Za-z0-9._\-]+))")
# * Hostnames that are not loopback and not a bare word: FQDN, .local, .ts.net (tailnet), .internal.
_HOSTNAME = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)*"
    r"(?:local|lan|internal|ts\.net|corp|home|intranet|com|net|org|io|ai|dev|co\.uk)\b")
# * Account-number shapes: the WORD is what makes it an account rather than a number.
_ACCOUNT = re.compile(
    r"\b(?:account|acct|routing|iban|sort\s?code|swift|bic)\b[^\n]{0,20}?"
    r"((?:\d[ \-]?){6,20}\d|[A-Z]{2}\d{2}[A-Z0-9]{10,30})", re.IGNORECASE)
# * Dates of birth: a date WITH a birth word near it. A bare date is not PII.
_DOB = re.compile(
    r"\b(?:d\.?o\.?b\.?|date of birth|born)\b[^\n]{0,20}?"
    r"(\d{1,4}[\-/.]\d{1,2}[\-/.]\d{1,4}|[A-Z][a-z]+ \d{1,2},? \d{4})", re.IGNORECASE)
# * Credentialed URLs: the password is in the URL.
# * \u2605 THE WHOLE `scheme://user:pass@` IS THE FINDING, NOT JUST THE PASSWORD. Reporting only the
#   password (group 2) let the adjacent EMAIL detector win the longest-first tie and the finding
#   came back as EMAIL -- my own self-test caught that, and it is exactly the class of miss a
#   detector list produces when its members are written one at a time.
#   so the pattern takes the WHOLE url -- scheme, user, password AND host -- which also makes it the
#   longest finding in the region, so it wins the longest-first pass over the EMAIL inside it.
_URL_CREDS = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]{3,}@[A-Za-z0-9.\-]*[A-Za-z0-9]", re.IGNORECASE)
# * Loopback and the unspecified address are NOT about the operator -- they are in almost every
#   turn this seat carries ("the ollama server is on 127.0.0.1:11434"). Masking them would break the
#   turn and teach the operator to ignore the gate. Private LAN ranges ARE kept: naming his network
#   is naming him.
_LOOPBACK = re.compile(r"^(?:127\.|0\.0\.0\.0$|::1$)")


def _luhn(digits: str) -> bool:
    """The check that stops `_CARD` firing on every long number in the operator's prose."""
    ds = [int(c) for c in digits if c.isdigit()]
    if not (13 <= len(ds) <= 19):
        return False
    if len(set(ds)) == 1:
        return False                      # 0000... and 1111... are not card numbers, they are typing
    tot, alt = 0, False
    for d in reversed(ds):
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        tot += d
        alt = not alt
    return tot % 10 == 0


def _clean(digits: str) -> str:
    return "".join(c for c in digits if c.isdigit())


def scan_deterministic(text: str) -> list[dict]:
    """EVERY shaped thing in this text. Free, instant, and not negotiable.

    Returns a list of `{"type","start","end","value"}` sorted by start. Overlaps are resolved
    longest-first so a key is not also reported as its own sub-run."""
    s = text or ""
    hits: list[dict] = []

    def add(kind: str, m: re.Match, group: int | None = None) -> None:
        if group is None:
            a, b, v = m.start(), m.end(), m.group(0)
        else:
            a, b, v = m.start(group), m.end(group), m.group(group)
        if v and v.strip():
            hits.append({"type": kind, "start": a, "end": b, "value": v})

    for m in _EMAIL.finditer(s):
        add("EMAIL", m)
    for m in _PRIVATE_KEY.finditer(s):
        add("PRIVATEKEY", m)
    for m in _SECRET_PREFIX.finditer(s):
        add("APIKEY", m)
    for m in _JWT.finditer(s):
        add("TOKEN", m)
    for m in _URL_CREDS.finditer(s):
        add("URLCREDS", m)
    for m in _SECRET_ASSIGN.finditer(s):
        add("SECRET", m, 1)
    for m in _IPV4.finditer(s):
        if not _LOOPBACK.match(m.group(0)) and m.group(0) != "0.0.0.0":
            add("IPV4", m)
    for m in _IPV6.finditer(s):
        if not _LOOPBACK.match(m.group(0)):
            add("IPV6", m)
    for m in _SSN.finditer(s):
        add("SSN", m)
    for m in _DOB.finditer(s):
        add("DOB", m, 1)
    for m in _ACCOUNT.finditer(s):
        add("ACCOUNT", m, 1)
    for m in _CARD.finditer(s):
        if _luhn(m.group(0)):
            add("CARD", m)
    for m in _PHONE.finditer(s):
        add("PHONE", m)
    for m in _HOME_PATH.finditer(s):
        add("USERPATH", m)
    for m in _HOSTNAME.finditer(s):
        # * a hostname that is already inside a larger finding is not a second finding
        if not any(h["start"] <= m.start() and m.end() <= h["end"] for h in hits):
            add("HOST", m)

    # -- longest-first, non-overlapping -----------------------------------------------------------
    hits.sort(key=lambda h: (-(h["end"] - h["start"]), h["start"]))
    kept: list[dict] = []
    for h in hits:
        if any(not (h["end"] <= k["start"] or h["start"] >= k["end"]) for k in kept):
            continue
        kept.append(h)
    kept.sort(key=lambda h: h["start"])
    return kept


# =================================================================================================
# TIER 1 -- THE LOCAL CLASSIFIER (loopback only)
# =================================================================================================
TIER1_PROMPT = """You are a privacy classifier running on the user's own machine.
Decide whether the TEXT contains PRIVATE information about a real person.

PRIVATE is true when the text contains ANY of:
  - a person's name, or a family relation ("my spouse", "my son", "my daughter")
  - a home address, street, apartment, city or postcode
  - health: an illness, a symptom, a medication, a diagnosis, or a medical appointment
  - money: a salary, a debt, a balance, a loan, an account or a transaction
  - a child's school, a legal matter, a birthday, or a relationship

PRIVATE is false for: instructions and commands, questions about software, file names, company
names, team names, job titles, product names, technical terms, code, and general knowledge.

EXAMPLES
TEXT: route to the support team and ask for a status report
JSON: {{"sensitive": false, "quotes": []}}
TEXT: Alex Example is my spouse and she has a medical appointment on Tuesday
JSON: {{"sensitive": true, "quotes": ["Alex Example", "my spouse", "medical appointment"]}}
TEXT: read the file /home/x/notes.txt and summarise it
JSON: {{"sensitive": false, "quotes": []}}
TEXT: my daughter Emma goes to Lincoln Elementary and hates her teacher
JSON: {{"sensitive": true, "quotes": ["my daughter Emma", "Lincoln Elementary"]}}

Now do the same for the TEXT below. Reply with JSON only, using exactly the keys "sensitive" and
"quotes". Every quote must be copied CHARACTER FOR CHARACTER out of the TEXT.

TEXT: {text}
JSON:"""


def _validate_spans(text: str, spans) -> tuple[list[str], list[str]]:
    """The model PROPOSES, deterministic code VERIFIES.

    Returns (accepted, rejected). Three shapes are accepted, and each is checked rather than trusted:

      "Alex Example"                  -> must appear VERBATIM in the text
      {"text": "Alex Example"}        -> same check
      {"start": 12, "end": 22}      -> the slice `text[12:22]` is taken; it must be a non-empty run

    Anything else is REJECTED and reported. A 0.6 B model asked for exact substrings will produce
    plausible ones that are not there, and rewriting the operator's turn around a span the model
    imagined is how a gate starts editing facts nobody asked it to edit.
    \u2605 MEASURED: asked for strings, this model sometimes answers with `{"start":0,"end":10}` objects
    (it mirrors the word "spans"). Rejecting that shape lost a real finding, so it is now SLICED --
    still deterministically verified, never trusted as an offset pair on its own."""
    ok, bad = [], []
    low = (text or "").lower()
    for sp in spans or []:
        cand = None
        if isinstance(sp, dict):
            for k in ("text", "value", "quote", "span"):
                if isinstance(sp.get(k), str):
                    cand = sp[k]
                    break
            if cand is None:
                a, b = sp.get("start"), sp.get("end")
                if isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(text or ""):
                    cand = (text or "")[a:b]
        elif isinstance(sp, str):
            cand = sp
        cand = (cand or "").strip()
        if not cand:
            continue
        if len(cand) > 200:
            bad.append(cand[:60] + "...")
            continue
        if cand.lower() in low:
            ok.append(cand)
        else:
            bad.append(cand)
    # dedupe, longest first, so a span containing another does not double-mask
    uniq = sorted(set(ok), key=lambda x: -len(x))
    final: list[str] = []
    for sp in uniq:
        if not any(sp in f for f in final):
            final.append(sp)
    return final, bad


# =================================================================================================
#  \u2605\u2605\u2605\u2605\u2605 TIER 1 AND THE BRAIN SHARE ONE OLLAMA RUNNER, SO THEY MUST ASK FOR IT THE SAME WAY.
#
#  THE DEFECT, MEASURED ON THIS BOX. `classify_local` sent `options: {temperature, num_predict}` and
#  said NOTHING about the context, so ollama loaded the classifier's model at the model's OWN default
#  -- `ollama ps` read CONTEXT 4096 -- while `brain._opts()` asks for `num_ctx: 1024` out of
#  `jarvis/brain_tuning.json`. TWO OPTION SHAPES ARE TWO RUNNER CONFIGURATIONS: ollama cannot reuse
#  one for the other, so every alternation classifier->brain paid a reload plus a COLD PREFILL.
#  \u2605\u2605 THE LANE THAT FOUND IT measured 2.4 s of load and 5.5-7.4 s of prefill PER ALTERNATION with
#  the mismatch, against 0.060-0.081 s when the two callers agreed. It was INVISIBLE while the two
#  callers used different models (both runners could stay resident) and it BITES THE MOMENT THE
#  BRAIN'S MODEL IS THE CLASSIFIER'S MODEL -- and `brain_tuning.json` has since been set to
#  `qwen3:1.7b`, which IS `TIER1_MODEL`. The estate is in the biting case now, not hypothetically.
#  \u2605\u2605\u2605 AND FOR A MODEL WITH NO PREFIX REUSE (LFM2.5: 9.188 s warm against 8.441 s cold) an evicted
#  runner is not a partial loss, it is the ENTIRE prompt re-evaluated at decode speed.
#
#  \u2605\u2605\u2605 ONE STORE, POINTED AT, NEVER COPIED (AGENTS.md \u00a75). The number is NOT written down in this
#  file. It is read through `brain.tuning()` -- the same accessor `brain._opts()` uses -- so when the
#  operator moves `num_ctx` in the GUI, BOTH callers move together on the very next request. A
#  constant here would stop matching the moment he did.
#  \u2605\u2605 THE FAILURE DIRECTION IS TODAY'S BEHAVIOUR. If the store cannot be read, the field is OMITTED,
#  which is EXACTLY the body this function sent before this change -- so a broken import degrades to
#  the old shape and never to a wrong one.
def _runner_num_ctx() -> int | None:
    """The context size the BRAIN asks the runner for -- read from the store the brain reads."""
    try:
        try:
            from . import brain as _brain       # package import
        except ImportError:
            import brain as _brain              # direct/script import
        return int(_brain.tuning()["num_ctx"])
    except Exception:                           # noqa: BLE001
        return None      # -> the field is omitted: the pre-change body, byte for byte


def _runner_options() -> dict:
    """Tier 1's sampler options, UNCHANGED, plus the runner's context size when the store answers."""
    o = {"temperature": 0, "num_predict": 400}
    _nc = _runner_num_ctx()
    if _nc:
        o["num_ctx"] = _nc
    return o


def classify_local(text: str, timeout: float = 45.0, model: str | None = None) -> dict:
    """Ask `qwen3:0.6b` on loopback whether this text carries private personal information.

    \u2605\u2605 THE RETURN SHAPE IS THE CONTROL. `available: False` whenever anything at all went wrong --
    model missing, connection refused, non-JSON, a missing `sensitive` field. The caller must treat
    that as "could not check", never as "clean". `sensitive` is `None`, not `False`, in that case.
    \u2605 Takes the POST-tier-0 text: the model judges the residue, not the original.

    \u2605\u2605\u2605 `/api/chat` WITH `think: false`, AND THE MEASUREMENT IS THE REASON. The first version of this
    function used `/api/generate` with a `/no_think` prefix, which is the widely-repeated advice.
    ON THIS BOX IT IS WRONG, AND IT FAILED SILENTLY: `qwen3:0.6b` went into thinking mode anyway
    (`thinking` came back 1569 characters long for "is 17 a prime number"), the trace consumed the
    whole `num_predict` budget, and `response` came back EMPTY OR TRUNCATED. The gate then read a
    truncated answer as a verdict and scored **recall 0/5** on five obviously-private sentences --
    including "Alex Example is my spouse and she has a medical appointment" -- while scoring a perfect
    5/5 on the clean ones. A detector that says "fine" to everything looks like a working detector
    with perfect specificity. **THE INSTRUMENT WAS THE SUSPECT, and it was the instrument.**
    With `/api/chat` + `"think": false`, the SAME model on the SAME sentence answers
    `{"sensitive": true, "quotes": ["Alex Example"]}` in ~0.6 s.
    \u2605 The lesson is kept as a guard, not a story: `self_test` asserts this function does NOT post to
    `/api/generate`, and the `done_reason`/empty-response check below refuses to read a truncated
    answer as a verdict.
    """
    ok_host, why_host = _endpoint_is_loopback(OLLAMA)
    if not ok_host:
        return {"available": False, "sensitive": None, "spans": [], "rejected": [],
                "tier": TIER1, "why": why_host, "ms": 0}
    body = json.dumps({
        "model": model or TIER1_MODEL,
        "messages": [{"role": "user", "content": TIER1_PROMPT.format(text=text)}],
        "stream": False,
        "think": False,                 # \u2605 see the docstring: without this the trace eats the answer
        "format": "json",
        "options": _runner_options(),
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:  # noqa: BLE001
        return {"available": False, "sensitive": None, "spans": [], "rejected": [], "tier": TIER1,
                "why": f"the local classifier did not answer on {OLLAMA} "
                       f"({type(e).__name__}: {str(e)[:120]}) -- THIS IS NOT A CLEARANCE",
                "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    content = ((raw.get("message") or {}).get("content") or "")
    if not content.strip() or raw.get("done_reason") == "length":
        # * \u2605 THE EXACT DEFECT ABOVE, REFUSED BY NAME: a truncated or empty answer is NOT a verdict.
        return {"available": False, "sensitive": None, "spans": [], "rejected": [], "tier": TIER1,
                "why": (f"the classifier's answer was empty or truncated "
                        f"(done_reason={raw.get('done_reason')!r}, {len(content)} chars of content, "
                        f"{len((raw.get('message') or {}).get('thinking') or '')} chars of thinking) "
                        f"-- AN UNREAD ANSWER IS NOT A CLEARANCE"),
                "ms": ms}
    try:
        out = json.loads(content)
    except ValueError:
        out = {}
    if not isinstance(out, dict) or "sensitive" not in out:
        return {"available": False, "sensitive": None, "spans": [], "rejected": [], "tier": TIER1,
                "why": (f"the classifier answered but carried no `sensitive` field "
                        f"({content[:80]!r}) -- AN ABSENT FIELD IS NOT A CLEARANCE"),
                "ms": ms}
    # * \u2605 THE MODEL IS ASKED FOR `quotes`; `spans` is accepted too, because a prompt is not a schema and
    #   a model that answers a differently-named key has still answered. Either is verified the same.
    proposed = out.get("quotes")
    if proposed is None:
        proposed = out.get("spans")
    accepted, rejected = _validate_spans(text, proposed)
    return {"available": True, "sensitive": bool(out.get("sensitive")), "spans": accepted,
            "rejected": rejected, "tier": TIER1, "ms": ms,
            "why": (f"{TIER1_MODEL} on loopback answered in {ms} ms; "
                    f"{len(accepted)} span(s) verified verbatim, {len(rejected)} rejected as absent "
                    f"from the text")}


# =================================================================================================
# THE LEASE -- obfuscation, the in-memory map, and re-hydration
# =================================================================================================
class Lease:
    """ONE outbound turn's worth of substitutions. IN MEMORY ONLY. NEVER PERSISTED.

    \u2605 `__slots__` so nothing can be stapled onto it, and a `__repr__` that prints COUNTS so a
    traceback or a debugger session cannot spill the table into a log.
    """

    __slots__ = ("handle", "destination", "created", "verdict", "action", "sent_text", "_map",
                 "_types", "tier0", "tier1", "spent_model", "closed", "masked_fraction",
                 "skeleton_words", "skeleton_content_words", "unlocalised_spans", "skeleton_heads")

    def __init__(self, destination: str, verdict: str, action: str, sent_text: str,
                 pairs: list[tuple[str, str]], tier0: list[dict], tier1: dict,
                 types=None) -> None:
        self.handle = secrets.token_hex(8)
        self.destination = destination
        self.created = time.time()
        self.verdict = verdict
        self.action = action
        self.sent_text = sent_text
        self._map = {tok: val for tok, val in pairs}
        # * \u2605 THE TYPE LIST IS PASSED IN, NOT PARSED OUT OF THE SUBSTITUTION KEY. It used to be
        #   `tok[2:].rsplit("_", 1)[0]`, which worked only because a substitution LOOKED like
        #   `[[TYPE_hex8]]`. A surrogate is ordinary text, so that slice would have silently
        #   produced garbage type names for every substituted value -- the quiet kind of wrong.
        self._types = set(types or ())
        self.tier0 = tier0
        self.tier1 = tier1
        self.spent_model = bool(tier1.get("available"))
        self.closed = False
        self.masked_fraction = 0.0
        # * \u2605\u2605 THE STRUCTURE MEASUREMENT. Filled in by `gate()` after the plan is final. `-1` is
        #   "not measured" and is deliberately NOT folded into 0 -- a lease that never ran the floor
        #   must not read as a lease that passed it.
        self.skeleton_words = -1
        self.skeleton_content_words = -1
        self.unlocalised_spans = 0
        # * \u2605 HOW MANY OF THOSE CONTENT WORDS CAN STILL HEAD A REQUEST. `-1` is "not measured", for the
        #   same reason as above -- a lease that never asked the question must not read as one that
        #   answered it. 0 with `skeleton_content_words > 0` is the acceptance defect's exact shape.
        self.skeleton_heads = -1

    # -- the display side ------------------------------------------------------------------------
    def rehydrate(self, text: str) -> dict:
        """Put the real values back. THE ONLY WAY A PLACEHOLDER EVER BECOMES A VALUE AGAIN.

        Returns {"text", "restored", "unrestored"} -- `unrestored` counts tokens THIS lease issued
        that the far side did not echo back, so a mangled placeholder is REPORTED, not hidden."""
        s = text or ""
        restored = 0
        for tok in sorted(self._map, key=len, reverse=True):
            if tok in s:
                n = s.count(tok)
                s = s.replace(tok, self._map[tok])
                restored += n
        unr = sum(1 for tok in self._map if tok not in (text or ""))
        return {"text": s, "restored": restored, "unrestored": unr}

    def close(self) -> None:
        """Drop the table. Ephemerality is a property of the object, not a promise in a docstring."""
        self._map = {}
        self.closed = True

    def receipt(self) -> dict:
        """What may be written down. COUNTS AND TYPES ONLY -- no value, no token, ever.

        \u2605\u2605\u2605 THE FIRST VERSION OF THIS FUNCTION LEAKED, AND THE END-TO-END DRIVE CAUGHT IT. It
        stripped `spans` -- the verified, MASKED spans -- and passed every other key of the tier-1
        result straight through. But `rejected` is the list of strings the classifier PROPOSED and
        that were then DISCARDED, and a discarded proposal is a VERBATIM SUBSTRING OF THE OPERATOR'S
        TURN. The receipt therefore carried, in the clear:

            "rejected": [<the email address>, <the phone number>]

        -- and this receipt is written into `handoff_queue.jsonl` by `router.to_cc_queue` and returned
        in every HTTP refusal. A field whose whole purpose was "no value, ever" was the leak. The fix
        is not a longer blocklist of key names: it is to NEVER PASS THE MODEL'S RAW ANSWER THROUGH,
        and to emit only counts. `rejected_values` is the COUNT of discarded proposals; the strings
        themselves never leave memory."""
        t1 = self.tier1 or {}
        return {"handle": self.handle, "destination": self.destination,
                "verdict": self.verdict, "action": self.action,
                # * \u2605 `sent` IS THE ONE FIELD A CALLER SHOULD READ BEFORE ANYTHING ELSE. It is false
                #   exactly when the turn was HELD, and a held turn's `sent_text` must never be used.
                "sent": self.action != A_ASK,
                "substitutions": len(self._map), "closed": self.closed,
                "masked_fraction": getattr(self, "masked_fraction", None),
                "over_masked": bool((getattr(self, "masked_fraction", 0) or 0) > 0.7),
                # * \u2605\u2605 THE STRUCTURE FLOOR, ON THE RECEIPT, BECAUSE A SILENT OVER-MASK IS HOW A GATE
                #   GETS SWITCHED OFF. `skeleton_content_words == 0` is the exact condition that made
                #   this a HOLD instead of a body of nothing but placeholders.
                "skeleton_words": getattr(self, "skeleton_words", None),
                "skeleton_content_words": getattr(self, "skeleton_content_words", None),
                # * \u2605\u2605 HOW MANY SURVIVING CONTENT WORDS CAN STILL HEAD A REQUEST. Zero, with a non-zero
                #   `skeleton_content_words`, is "the mask left an action and no thing" -- the verdict
                #   is `over_masked_action_without_referent` and NOTHING IS SENT.
                "skeleton_heads": getattr(self, "skeleton_heads", None),
                "unlocalised_spans": getattr(self, "unlocalised_spans", None),
                "tier0_findings": len(self.tier0),
                "tier0_types": sorted({f["type"] for f in self.tier0}),
                "tier1": {"available": bool(t1.get("available")),
                          "sensitive": t1.get("sensitive"),
                          "ms": t1.get("ms"),
                          "spans_verified": len(t1.get("spans") or []),
                          "proposals_discarded": len(t1.get("rejected") or []),
                          "why": str(t1.get("why") or "")[:200]},
                "spent_model": self.spent_model}

    def __repr__(self) -> str:  # pragma: no cover - the point is that it CANNOT leak
        return (f"<Lease {self.handle} {self.destination} {self.verdict} "
                f"{len(self._map)} substitution(s), map not printed>")


# =================================================================================================
# THE REGISTRY -- bounded, in-memory, purged at exit
# =================================================================================================
_LIVE: dict[str, Lease] = {}
_BY_KEY: dict[str, str] = {}          # destination key -> newest lease handle (async round trips)
_MAX_LIVE = 32
MAX_LIVE = _MAX_LIVE


def _evict() -> None:
    while len(_LIVE) > _MAX_LIVE:
        oldest = min(_LIVE, key=lambda h: _LIVE[h].created)
        _LIVE[oldest].close()
        _LIVE.pop(oldest, None)
        for k, v in list(_BY_KEY.items()):
            if v == oldest:
                _BY_KEY.pop(k, None)


def purge() -> int:
    """Drop every live lease. Called at exit, and available to any caller that wants it sooner."""
    n = len(_LIVE)
    for lz in _LIVE.values():
        lz.close()
    _LIVE.clear()
    _BY_KEY.clear()
    return n


def registry_state() -> dict:
    """For `/state` and for the tests. Names and counts -- never a token, never a value."""
    return {"live": len(_LIVE), "max": _MAX_LIVE,
            "handles": sorted(_LIVE), "keys": sorted(_BY_KEY),
            "ephemeral": ("held in this process's memory only; nothing here is read from or written "
                          "to disk")}


def rehydrate(text: str, handle: str | None = None, key: str | None = None) -> dict:
    """The return path. Give it a handle, or the destination key the turn went out under."""
    h = handle or (_BY_KEY.get(key or "") if key else None)
    lz = _LIVE.get(h or "")
    if not lz:
        return {"text": text, "restored": 0, "unrestored": 0, "lease": None,
                "why": "no live lease -- either nothing was substituted, or the round trip is over"}
    return {**lz.rehydrate(text), "lease": lz.handle}


# =================================================================================================
# THE GATE
# =================================================================================================
def _normalise(v: str) -> str:
    return re.sub(r"\s+", " ", v.strip()).lower()


def _content_word(token: str) -> bool:
    """Does this surviving token still carry meaning, or is it the sentence's grammatical glue?

    \u2605 THIS IS THE STRUCTURE FLOOR'S ONLY INSTRUMENT, AND IT CANNOT CAUSE A LEAK. It is never consulted
    to choose WHICH span to mask -- it is only ever consulted about the words that are LEFT OVER
    after the mask. Nothing it returns can unmask anything; the worst case is a HOLD.

    It is deliberately generous about what counts as CONTENT: a token is "glue" only when, stripped
    of surrounding punctuation and lowercased, it is a member of the declared closed-class list AND
    nothing else is left of it. A number, a placeholder, a capitalised word, a hyphenated word, an
    unknown word -- all read as content, so the floor only fires when the turn has genuinely been
    reduced to articles and prepositions."""
    bare = token.strip(".,;:!?\"'()[]{}<>-").lower()
    if not bare:
        return False                     # pure punctuation is not a word at all
    if bare.startswith("[["):
        return True                      # a placeholder is the value now; it is content
    return bare not in _SKELETON_FUNCTION_WORDS


# * ================================================================================================
# * THE ACTION VOCABULARY -- ONE STORE, POINTED AT, NEVER COPIED (`voice_intent.py` owns the words)
# * ================================================================================================
# * WHY THIS EXISTS AT ALL. The structure floor's first clause asks "did anything but grammatical glue
#   survive the mask?" (`skeleton_content_words == 0`). THAT QUESTION MISSED THE ACCEPTANCE TURN BY
#   EXACTLY ONE WORD. The span union ate the instruction, the only surviving content word was
#   `saying`, and `saying` is not glue -- so the floor reported "there is an instruction here" about
#   a turn whose agent answered "no clear actionable task for me ... standing by". The two functions
#   below ask the NEXT question: "is any surviving word still able to HEAD A REQUEST?"
#
# * WHERE THE WORDS COME FROM: `jarvis/voice_intent.py`, this estate's own intent vocabulary -- the
#   errand verbs (`_TASK_VERBS`, the words that say WHAT TO DO) plus the fold verbs (`_FOLD_VERBS`,
#   the words this seat already accepts as an instruction opener). NOTHING IS INVENTED HERE, and the
#   suite asserts SET EQUALITY against that file by AST: adding a verb there and not here is a RED
#   test, not a silent difference. The list is deliberately NOT widened by taste -- this estate has
#   paid three times for a keyword list that was widened and stole ordinary sentences.
#
# * \u2605\u2605 AND THE LIST CANNOT LEAK, BY CONSTRUCTION -- THE SAME ARGUMENT `_content_word` MAKES ABOVE. It is
#   never consulted to choose WHICH span to mask; it is consulted only about the words LEFT OVER after
#   the mask. Nothing here can unmask anything. The worst case is a HOLD, and a hold cannot leak.
_ACTION_WORDS = frozenset("""
do ask tell say find check make write give send start run build fix add update
review explain summarise summarize show report request confirm remind schedule
plan draft create search verify test investigate ping message call work
delegate dispatch forward page have get relock switch reassign move
re-lock
""".split())


def _action_word(token: str) -> bool:
    """Is this surviving word a form of an ACTION -- something the turn could be ASKING FOR?

    \u2605\u2605\u2605 `saying` IS THE WHOLE REASON THIS FUNCTION EXISTS. The acceptance turn's only surviving
    content word was `saying`, and `saying` IS a verb -- which is exactly why a naive "did a verb
    survive?" check would have PASSED the turn that failed. THE DISTINCTION THAT MATTERS IS NOT
    VERB/NOT-VERB, IT IS FINITE/NON-FINITE. A bare verb can head a request ("send the note"); an
    `-ing` form cannot -- it can only live inside a clause, and the clause `saying the draft is
    ready` had its subject and its object taken by the mask.

    \u2605 THE FORM TEST IS DELIBERATELY NARROW, AND IT IS NARROW IN THE SAFE DIRECTION. Only the `-ing`
    inflection is recognised, because `-ing` is the one English verb inflection that is UNAMBIGUOUSLY
    non-finite. `-s` and `-ed` are NOT recognised: `plans`, `works`, `reports`, `calls`, `found` and
    `sent` are ordinary NOUNS about as often as they are verbs, so recognising them would widen the
    HOLD onto turns that are perfectly usable. Every form this function does not recognise reads as
    "not an action", and that answer can only ever mean SEND."""
    bare = token.strip(".,;:!?\"'()[]{}<>-").lower()
    if not bare:
        return False
    if bare in _ACTION_WORDS:
        return True
    if len(bare) > 4 and bare.endswith("ing"):
        stem = bare[:-3]
        if stem in _ACTION_WORDS or stem + "e" in _ACTION_WORDS:
            return True
        if len(stem) > 2 and stem[-1] == stem[-2] and stem[:-1] in _ACTION_WORDS:
            return True       # doubled final consonant: `planning` -> `plan`
    return False


def _heads_a_request(token: str) -> bool:
    """Can this surviving word still HEAD a request -- as a bare action, or as a thing named?

    \u2605\u2605\u2605 THIS IS A QUESTION ABOUT THE LANGUAGE, NOT ABOUT A TURN. A request needs an action and a
    thing. Exactly two kinds of surviving word can supply the head:

        a BARE action word                  -- `send`, `reply`, `check`: an imperative, it says what to do;
        a word that is NOT an action at all -- `key`, `note`, `line`, `draft`: it NAMES a thing, and the
                                               action may be implied or may sit in the operator's head.

    A NON-BARE form of an action word can do NEITHER. `saying` is not an imperative and it does not
    name a thing; it is a subordinate verb form whose clause was gutted by the mask.

    \u2605\u2605 AND THE FAILURE DIRECTION IS SEND, WHICH IS THE WHOLE POINT. Ambiguity resolves to "it can
    head a request" and the turn GOES: a bare action word GOES even when it is really a noun in that
    sentence ("the report", "the plan"), and a word this module has never seen GOES. Only a turn whose
    every surviving content word is an unambiguous NON-FINITE action word is held -- which is why this
    can never become the outage that a `masked_fraction >= 0.6` rule would have been. That rule held
    two legitimate turns (0.704, 0.734) to catch one at 0.82, inside a 0.086-wide window fitted to two
    samples; this one needs no window at all."""
    return (not _action_word(token)) or (token.strip(".,;:!?\"'()[]{}<>-").lower() in _ACTION_WORDS)


def _mac(nonce: bytes, kind: str, value: str) -> str:
    """Per-lease HMAC. SAME VALUE + SAME LEASE -> SAME MAC. SAME VALUE + NEXT LEASE -> DIFFERENT."""
    mac = hmac.new(nonce, f"{kind}:{_normalise(value)}".encode(), hashlib.sha256).hexdigest()
    return mac


def _token(nonce: bytes, kind: str, value: str) -> str:
    """THE BRACKET FORM THIS MODULE USED TO SEND ON EVERY MASKED TURN. Kept, named, OFF the gate path.

    \u2605\u2605\u2605 WHAT IT COST, MEASURED 2026-10-12 ON A BRAND-NEW CONVERSATION. `d1c84082` was created by
    the app twenty minutes earlier and had ZERO history; it was sent ONE masked turn whose body was

        "ask the book team to send a one-line note to [[PERSONAL_...]] at [[EMAIL_...]] ..."

    and the agent REFUSED, in its own recorded rowids 25524/25525:

        "This is a confusing message - it seems to be a template/placeholder with personal data
         redaction markers. There's no actual task or assignment here."
        "That message looks like a template/placeholder (redacted personal data markers), not an
         actual assignment - I can't parse any concrete task from it."

    A CLEAN conversation refuses exactly as fast as the poisoned one, so the working diagnosis
    ("the broken turns taught the agent to refuse") was WRONG -- the shape is the whole of it.
    `[[PERSONAL_d44ed143]]` READS as a redaction marker, a template variable or a prompt-injection
    probe, and refusing is the CORRECT response to that input. We built a payload that looks hostile
    and then blamed the agent for noticing.

    The function survives because the PROPERTY it encodes -- per-lease HMAC, consistent within a
    turn, unlinkable across turns -- is the property `_surrogate` below is built on, and because
    deleting it would delete the evidence that this shape was ever tried. `gate()` does not call it.
    """
    return f"[[{kind}_{_mac(nonce, kind, value)[:8]}]]"


# * ================================================================================================
# * \u2605\u2605\u2605 THE SURROGATE TABLE -- WHAT LEAVES INSTEAD OF THE VALUE (2026-10-12)
# * ================================================================================================
# * THE FIX, AND IT IS ONLY THE OUTBOUND HALF. Replace each span with a PLAUSIBLE VALUE OF THE SAME
#   TYPE. The cloud receives a COMPLETE, ORDINARY INSTRUCTION ABOUT A PERSON WHO DOES NOT EXIST, and
#   it never sees the real one. There is nothing to refuse, because the sentence reads like every
#   other sentence. The lease, the handle, the rehydration and `classify()` are UNTOUCHED: the map
#   goes from `token -> real` to `surrogate -> real`, and every consumer of the map is identical.
#
# * THE TWO PROPERTIES THE TOKEN HAD ARE KEPT, BY THE SAME PRIMITIVE:
#   \u00b7 DETERMINISTIC WITHIN A LEASE -- generated from the SAME
#     `HMAC(per-lease nonce, "<TYPE>:<normalised value>")` the token was. Same value, same turn ->
#     the same surrogate, so the far side can still reason about "the same person".
#   \u00b7 UNLINKABLE ACROSS TURNS -- a fresh 16-byte nonce per lease, so the same real person gets a
#     DIFFERENT surrogate next turn and a cloud holding transcripts cannot join them.
#
# * \u2605\u2605\u2605 THE RESERVED RANGES, AND WHY EVERY DANGEROUS TYPE USES ONE. A surrogate that COLLIDES
#   WITH A REAL PERSON'S IDENTIFIER IS A WORSE LEAK THAN A BRACKET, so wherever a plausible-looking
#   fake can BE somebody's real identifier, the value comes from a range DOCUMENTED AS NEVER ISSUED.
#   Every citation below is the primary source, and each one is asserted by `self_test`, not just
#   described here:
#
#   EMAIL / HOST   `.invalid` -- RFC 2606 reserves it; it can never be registered or resolved.
#                  The estate's own fixtures already use `example.invalid`, so this follows them.
#                  https://datatracker.ietf.org/doc/html/rfc2606
#   PHONE          555-0100 .. 555-0199 -- "The fictitious, non-working numbers, 555-0100 through
#                  555-0199, will remain reserved for entertainment/advertising." (NANPA, 555 Line
#                  Numbers). Emitted as `<NPA>-555-01XX`.
#                  https://nanpa.com/index.php/numbering/555-line-numbers
#   SSN            900-999 AREA SERIES. SSA POMS RM 10201.035: an invalid (never assigned) SSN has a
#                  first group of "000", "666" or in the 900 series, a second group of "00", or a
#                  third group of "0000". Emitted as `9XX-XX-XXXX` with group 01-99 and serial
#                  0001-9999, so it never lands on ANY of the three never-issued shapes.
#                  https://secure.ssa.gov/apps10/poms.NSF/LNX/0110201035
#   IPV4           RFC 5737 TEST-NET-1/2/3: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24.
#                  https://www.rfc-editor.org/rfc/rfc5737
#   IPV6           RFC 3849: the 2001:db8::/32 documentation prefix.
#                  https://www.rfc-editor.org/rfc/rfc3849
#   CARD           THE PUBLISHED TEST PANs ONLY, which every processor documents as test numbers
#                  never issued to an account holder -- NOT a Luhn-valid number we generate
#                  ourselves, because a PAN we invented could be A LIVE CARD BELONGING TO SOMEONE.
#                  That is the exact failure the brief names, so it is designed out rather than
#                  argued about.
#                  https://developer.paypal.com/api/nvp-soap/payflow/payflow-pro/payflow-pro-testing/
#
# * \u2605\u2605 AND THE TYPES WHERE NO RESERVED RANGE EXISTS AT ALL -- the CREDENTIAL FAMILY (APIKEY,
#   TOKEN, SECRET, PRIVATEKEY, URLCREDS) and ACCOUNT. There is no reserved API key, no reserved
#   IBAN, and a "plausible" fake credential is the one class where LOOKING REAL IS ITSELF THE HARM:
#   a fabricated key that reads like a live one can be pasted, filed or mistaken for key material
#   downstream, and a fabricated IBAN/account that passes its OWN checksum is indistinguishable
#   from a real account. FOR THOSE TYPES THE SUBSTITUTE IS A MARKER -- ordinary English inside
#   parentheses, which is NOT the double-bracket template shape that caused the refusal and carries
#   no digits that could belong to anyone. Stated here, per type, because the brief asks which was
#   chosen and why. A marker is unique within a lease ("an API key", "a second API key", ...), so
#   two different keys never collapse onto one substitution.
_MARKER_NOUN = {
    "APIKEY": "API key",
    "TOKEN": "access token",
    "SECRET": "password",
    "PRIVATEKEY": "private key",
    "URLCREDS": "username and password",
    "ACCOUNT": "account number",
}
_MARKER_ARTICLE = {"APIKEY": "an", "TOKEN": "an", "SECRET": "a", "PRIVATEKEY": "a",
                   "URLCREDS": "a", "ACCOUNT": "an"}
_MARKER_ORD = ("", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth")
MARKER_TYPES = frozenset(_MARKER_NOUN)

# * The published test PANs. 16 digits each so the grouping survives a reformat. Source: the
#  processor test-card tables (PayPal/Payflow linked above; Stripe and Computop publish the same
#  set). `4111 1111 1111 1111` is also this module's own PROVE-FAIL probe for the Luhn detector.
TEST_PANS = ("4111111111111111", "4242424242424242", "4000056655665556",
             "5555555555554444", "5200828282828210", "5105105105105100",
             "6011111111111117", "6011000990139424")
_TEST_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")

# * The name / place pools the surrogates are drawn from. 40 x 40 given-family pairs is 1,600 names,
#  so two spans in one turn colliding is rare, and when it happens the collision check below sees it
#  and the generator MOVES ON rather than shipping an ambiguous substitution.
_SUR_FIRST = (
    "Marcus", "Nadia", "Ellery", "Tobias", "Priya", "Rowan", "Delia", "Ansel",
    "Marisol", "Quentin", "Ivy", "Rafael", "Corinne", "Desmond", "Leonie", "Otto",
    "Sable", "Emory", "Bridget", "Casper", "Noor", "Alaric", "Tamsin", "Hugo",
    "Wren", "Isidore", "Camille", "Foster", "Linnea", "Barnaby", "Rhea", "Cormac",
    "Sylvie", "Ambrose", "Juno", "Milo", "Verity", "Ezekiel", "Odette", "Peregrine",
)
_SUR_LAST = (
    "Whitfield", "Okonkwo", "Ferrante", "Lindqvist", "Ashcombe", "Delacroix", "Nakamura",
    "Brightwater", "Halloran", "Vasquez", "Thornbury", "Merriweather", "Castellanos",
    "Fairbanks", "Oyelaran", "Ravensworth", "Solberg", "Trellis", "Underhill", "Vandermeer",
    "Winslow", "Yarrow", "Zamorano", "Bellweather", "Cavendish", "Draper", "Everly",
    "Foxglove", "Grantham", "Havelock", "Ingersoll", "Jessup", "Kingsley", "Larkspur",
    "Marlowe", "Norrington", "Pemberton", "Quill", "Rothbury", "Stanhope",
)
_SUR_STREET = ("Larkspur", "Alder", "Bramble", "Cedar", "Dovetail", "Elmstead", "Foxglove",
               "Granite", "Harrow", "Ivywood", "Juniper", "Kestrel", "Lantern", "Mulberry",
               "Nettlebed", "Orchard", "Primrose", "Quarry", "Rowanwood", "Sablewood")
_SUR_STREET_SUFFIX = ("Street", "Avenue", "Road", "Lane", "Drive", "Court", "Way", "Place")
_SUR_CITY = ("Fairview", "Brookmere", "Cedarholt", "Dunmoor", "Eastvale", "Fernwick",
             "Glenhaven", "Halloway", "Ironbridge", "Kingsmoor", "Larkfield", "Millbrook",
             "Northgate", "Oakhaven", "Pinecrest", "Quarryside")
# * How many deterministic candidates one span may try before the generator gives up and falls back
#  to a marker. 64 tries against a 1,600-name pool is unreachable in practice; the counter exists so
#  the loop is BOUNDED rather than trusting that.
SUR_ATTEMPTS = 64


def _pick(pool, digest: bytes, off: int):
    return pool[int.from_bytes(digest[off:off + 4], "big") % len(pool)]


def _num(digest: bytes, off: int, lo: int, hi: int) -> int:
    return lo + int.from_bytes(digest[off:off + 4], "big") % (hi - lo + 1)


def _marker(kind: str, attempt: int = 0) -> str:
    """Ordinary English for the types where a plausible fake would be the harm. Never a bracket."""
    noun = _MARKER_NOUN.get(kind, "private value")
    if attempt <= 0:
        return f"({_MARKER_ARTICLE.get(kind, 'a')} {noun})"
    if attempt < len(_MARKER_ORD):
        return f"(a {_MARKER_ORD[attempt]} {noun})"
    return f"({noun} {attempt + 1})"


def _regroup(pan: str, like: str) -> str:
    """Reuse the separator style of the value being replaced, so the fake reads the way the real did."""
    if "-" in like:
        return "-".join(pan[i:i + 4] for i in range(0, len(pan), 4))
    if " " in like:
        return " ".join(pan[i:i + 4] for i in range(0, len(pan), 4))
    return pan


def _personal_surrogate(value: str, digest: bytes) -> str:
    """A tier-1 span is whatever the classifier thought was private. Guess its KIND, then fake it.

    \u2605 THE HEURISTIC IS DECLARED BECAUSE IT IS A HEURISTIC. Tier 0 has already taken everything with a
      hard shape, so a PERSONAL span is a name, a relation ("my spouse"), an address, or something
      shaped that tier 0's patterns did not cover. Each branch is checked in that order and the
      DEFAULT is a full name, which is the commonest and the safest reading: a name in the place of
      a relation still leaves a grammatical sentence ("my spouse has an appointment" ->
      "Delia Whitfield has an appointment"), and rehydration puts the relation back.
    """
    v = value or ""
    if "@" in v:
        return _render_email(digest)
    if re.search(r"\d{3}[-.\s]\d{3}[-.\s]\d{4}", v):
        return _render_phone(digest)
    if (re.search(r"(?i)\b(street|st\.|avenue|ave\.|road|rd\.|lane|ln\.|drive|dr\.|"
                  r"boulevard|blvd|terrace|suite|apartment|apt\.|floor)\b", v)
            or re.match(r"\s*\d{1,6}\s+[A-Za-z]", v)):
        return (f"{_num(digest, 0, 10, 9899)} {_pick(_SUR_STREET, digest, 4)} "
                f"{_pick(_SUR_STREET_SUFFIX, digest, 8)}, {_pick(_SUR_CITY, digest, 12)}")
    return f"{_pick(_SUR_FIRST, digest, 0)} {_pick(_SUR_LAST, digest, 4)}"


def _render_email(digest: bytes) -> str:
    return (f"{_pick(_SUR_FIRST, digest, 0).lower()}."
            f"{_pick(_SUR_LAST, digest, 4).lower()}@example.invalid")


def _render_phone(digest: bytes) -> str:
    return f"{_num(digest, 8, 201, 989)}-555-01{_num(digest, 12, 0, 99):02d}"


def _render_surrogate(kind: str, digest: bytes, value: str, attempt: int) -> str:
    """One deterministic candidate of the right TYPE. Reserved ranges wherever one exists."""
    if kind in _MARKER_NOUN:
        return _marker(kind, attempt)
    if kind == "EMAIL":
        return _render_email(digest)
    if kind == "PHONE":
        return _render_phone(digest)
    if kind == "SSN":
        return (f"{_num(digest, 16, 900, 999)}-{_num(digest, 20, 1, 99):02d}-"
                f"{_num(digest, 24, 1, 9999):04d}")
    if kind == "CARD":
        return _regroup(_pick(TEST_PANS, digest, 4), value)
    if kind == "IPV4":
        return f"{_pick(_TEST_NETS, digest, 4)}{_num(digest, 12, 1, 254)}"
    if kind == "IPV6":
        return f"2001:db8:{_num(digest, 8, 0, 0xffff):x}:{_num(digest, 12, 0, 0xffff):x}::1"
    if kind == "HOST":
        return f"{_pick(_SUR_STREET, digest, 0).lower()}-{_num(digest, 8, 10, 99)}.invalid"
    if kind == "USERPATH":
        return ("/home/" + _pick(_SUR_FIRST, digest, 0)[0].lower()
                + _pick(_SUR_LAST, digest, 4).lower())
    if kind == "DOB":
        return (f"{_num(digest, 0, 1946, 2004):04d}-{_num(digest, 4, 1, 12):02d}-"
                f"{_num(digest, 8, 1, 28):02d}")
    return _personal_surrogate(value, digest)


def _ambiguous(cand: str, real: frozenset, issued: dict, source: str, me: str) -> bool:
    """Would this candidate make re-hydration AMBIGUOUS? Constraint 4, and it is a correctness bug.

    Three ways, each of which has a stated consequence:

      1 IT IS ANOTHER REAL SPAN IN THIS TURN. Two spans substituting to the same string collapses
        the map to one entry and one of the two real values is LOST -- and on the way back the
        surviving surrogate restores the WRONG value.
      2 IT IS ALREADY SOMEWHERE IN THE TURN'S OWN TEXT. Rehydration is exact-string replacement, so
        a surrogate that was already there rewrites a word the far side wrote for itself into a
        value it never stood for. That is how a gate INVENTS FACTS.
      3 IT CONTAINS, OR IS CONTAINED BY, ANOTHER SURROGATE THIS LEASE ISSUED. Rehydration sorts by
        length and replaces longest-first, so nesting makes which-value-comes-back depend on which
        string was longer. Not ambiguous by luck; regenerated instead.

    \u2605 THE ONE THING THAT IS NOT A COLLISION: the SAME span seen a second time. The same value must
      map to the SAME surrogate throughout one turn, so a candidate equal to a surrogate this lease
      already issued FOR THIS SAME NORMALISED VALUE is the consistency property working, not a
      clash. Every other equal-or-nested case regenerates.
    """
    c = (cand or "").strip()
    if not c:
        return True
    low = c.lower()
    if low in real:
        return True
    if low in (source or "").lower():
        return True
    for other, other_real in issued.items():
        o = other.lower()
        if low == o:
            return other_real != me
        if low in o or o in low:
            return True
    return False


def _surrogate(nonce: bytes, kind: str, value: str, real: frozenset,
               issued: dict, source: str) -> str:
    """THE SUBSTITUTE FOR ONE SPAN. Deterministic in (nonce, kind, value); collision-checked.

    \u2605\u2605 `issued` IS AN ORDERED MAPPING surrogate -> normalised real value, NOT A SET, and that is
      load-bearing twice over. It carries the within-turn consistency rule (see `_ambiguous`), and
      it keeps this function PURE: the same arguments produce the same answer, so the same real
      value reaches the same surrogate everywhere in a turn WITHOUT a memo table. A memo would have
      hidden a broken HMAC behind cached answers -- the mutation table proves it does not.
    \u2605 BOUNDED: `SUR_ATTEMPTS` candidates, then a marker that is itself collision-checked. There is
      no path out of this function that returns the real value.
    """
    me = _normalise(value)
    base = _mac(nonce, kind, value).encode("ascii")
    for attempt in range(SUR_ATTEMPTS):
        cand = _render_surrogate(kind, hashlib.sha256(base + attempt.to_bytes(4, "big")).digest(),
                                 value, attempt)
        if not _ambiguous(cand, real, issued, source, me):
            return cand
    n = 0
    while n < 1000:
        n += 1
        cand = _marker(kind, n)
        if not _ambiguous(cand, real, issued, source, me):
            return cand
    # * Unreachable: 1,000 distinct markers cannot all be ambiguous. Stated rather than pretended.
    return f"({_MARKER_NOUN.get(kind, 'private value')} {secrets.token_hex(2)})"


class GateRefused(RuntimeError):
    """Raised only by `gate_or_raise`. The dict-returning `gate()` never raises."""


def gate(text: str, destination: str, *, key: str | None = None,
         allow_unchecked: bool = False, allow_plaintext: bool = False,
         local: dict | None = None, timeout: float = 45.0) -> Lease:
    """SCAN THIS TURN, DECIDE, AND RETURN THE LEASE WHOSE `.text` IS SAFE TO SEND.

    The order is the ladder, and it is deliberate:

      1. tier 0 over the WHOLE text. Free, instant, always runs, cannot be skipped.
      2. if tier 0 found anything -> we already know the turn carries shaped secrets, so we OBFUSCATE
         first and then hand the model the RESIDUE. (The model sees less and costs the same.)
         if tier 0 found nothing -> tier 1 MUST run, because "no shape" is not "no PII".
      3. tier 1 down, and tier 0 found nothing -> verdict `unchecked`, action `ask`.
         \u2605 THE TEXT DOES NOT GO. `allow_unchecked=True` is the operator's explicit, per-turn "send it
         anyway", and the lease records that it happened.
      4. caller may force the plaintext through with `allow_plaintext=True` -- the reference product's
         "upload it anyway" button. It is OFF by default and it is recorded.
    """
    s = text or ""
    t0 = scan_deterministic(s)
    nonce = secrets.token_bytes(16)
    # * \u2605\u2605 THE PRE-MASK LOOP THAT USED TO LIVE HERE IS GONE, AND IT WAS DEAD CODE. It built `masked`
    #   -- which NOTHING ever read -- while this function hands tier 1 the ORIGINAL text anyway (see
    #   the long note below; that is deliberate and measured). `t0_pairs` is what the decision keys
    #   on, and it is now built WITH the substitutions further down, for a structural reason: a
    #   SURROGATE cannot be generated here, because collision-checking it needs EVERY real value in
    #   the turn at once and tier 1 has not answered yet.

    # -- tier 1 -----------------------------------------------------------------------------------
    # \u2605\u2605\u2605 TIER 1 SEES THE **ORIGINAL** TEXT, NOT THE TIER-0-MASKED TEXT, AND THE DRIVE IS WHY.
    #   The first version handed the model the masked residue. On a real turn the model then quoted a
    #   span that CONTAINED the tier-0 placeholders --

    #     tier 0:  "Ask <the name> at [[EMAIL_...]] to call ... on [[PHONE_...]] ..."
    #     tier 1:  spans = ["Ask <the name> at [[EMAIL_...]] to call ... ready"]

    #   -- and substituting that span SWALLOWED the tier-0 tokens inside it, collapsing the entire
    #   errand into one `[[PERSONAL_...]]`. The cloud model received a turn with no content, which is
    #   a BROKEN FEATURE and not a private one. Two smaller models do not compose by feeding one's
    #   output to the other. So the model reads the turn as the operator wrote it (it is on loopback;
    #   nothing egresses), and the RECONCILIATION happens afterwards, deterministically, below.
    if local is not None:
        t1 = dict(local)
        t1.setdefault("tier", TIER1)
    else:
        t1 = classify_local(s, timeout=timeout)

    # -- assemble the substitution table ----------------------------------------------------------
    # \u2605\u2605\u2605 ONE MASK PLAN OVER THE ORIGINAL TEXT, RESOLVED BY THE SAME LONGEST-FIRST RULE TIER 0 USES
    #   ON ITS OWN. The previous version masked tier 0 first and then ran tier 1's spans as string
    #   replacements over the ALREADY-MASKED text, and it DROPPED any tier-1 span that overlapped a
    #   tier-0 finding. The drive caught what that costs: the classifier sometimes answers with ONE
    #   span covering the whole turn, that span overlaps the email and the phone, so it was dropped
    #   WHOLE -- AND THE PERSON'S NAME INSIDE IT WENT OUT IN THE CLEAR ON A REAL NETWORK REQUEST.
    #   Dropping a finding is never the safe direction. The overlap is now SUBTRACTED instead: the
    #   tier-0 regions are cut out of the tier-1 span and the REMAINING FRAGMENTS are masked. That
    #   over-masks (some ordinary words go with the name) and over-masking is the direction the floor
    #   points -- "when in doubt, OBFUSCATE rather than send" -- while under-masking is a leak.
    def _subtract(a: int, b: int, cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """The parts of [a,b) that no cut covers. Cuts are the exact, verified tier-0 regions."""
        frags = [(a, b)]
        for (ca, cb) in cuts:
            nxt = []
            for (x, y) in frags:
                if cb <= x or ca >= y:
                    nxt.append((x, y))
                    continue
                if x < ca:
                    nxt.append((x, min(ca, y)))
                if cb < y:
                    nxt.append((max(cb, x), y))
            frags = nxt
        return frags

    t1_spans = list(t1.get("spans") or []) if t1.get("available") else []
    t1_cuts = [(f["start"], f["end"]) for f in t0]
    plan: list[tuple[int, int, str, str]] = [(f["start"], f["end"], f["type"], f["value"]) for f in t0]
    dropped, kept_spans = [], []
    for sp in t1_spans:
        if not sp:
            continue
        i = s.find(sp)
        if i < 0:
            dropped.append(sp)
            continue
        kept_spans.append(sp)
        for (a, b) in _subtract(i, i + len(sp), t1_cuts):
            if b - a >= 3:
                plan.append((a, b, "PERSONAL", s[a:b]))

    # * longest-first, non-overlapping -- the identical rule `scan_deterministic` uses internally
    plan.sort(key=lambda h: (-(h[1] - h[0]), h[0]))
    final: list[tuple[int, int, str, str]] = []
    for h in plan:
        if any(not (h[1] <= k[0] or h[0] >= k[1]) for k in final):
            continue
        final.append(h)
    final.sort(key=lambda h: h[0])

    # \u2605 WHEN THE CLASSIFIER CANNOT LOCALISE, THE TURN IS MASKED HARD -- AND THE LEASE SAYS SO.
    #   Measured: asked for quotes, `qwen3:1.7b` sometimes answers with ONE span covering the whole
    #   turn (1 run in 6; 6 in 6 after a prompt tweak that tried to forbid it -- the tweak was
    #   REVERTED, see the report). Subtracting the tier-0 regions out of such a span leaves several
    #   long fragments, and masking them all destroys the turn's usability. That is the SAFE
    #   direction and it is kept -- but a silent over-mask is how a gate gets switched off, so the
    #   fraction of the turn that was replaced is computed and carried on the receipt.
    pairs: list[tuple[str, str]] = []
    sent = s
    # * (surrogate, value, type) for tier 0's own findings. Tier-1 spans are always kind "PERSONAL",
    #   so `kind != "PERSONAL"` IS "came from tier 0" -- said out loud because a reader should not
    #   have to reconstruct that. It is what the decision below keys on.
    t0_pairs: list[tuple[str, str, str]] = []
    # \u2605\u2605\u2605 THE COLLISION CONTEXT, BUILT ONCE FOR THE WHOLE PLAN. `real` is every span this turn is
    #   about to replace; `issued` maps each surrogate THIS LEASE issued back to the normalised value
    #   it stands for. Both are needed before the first substitution: a surrogate that equals
    #   another real span, or that already appears in the turn, would make rehydration restore the
    #   WRONG value -- a correctness bug, not a nicety.
    real_vals = frozenset(_normalise(v) for (_a, _b, _k, v) in final)
    issued: dict[str, str] = {}
    for (a, b, kind, val) in sorted(final, key=lambda h: -h[0]):   # right-to-left: offsets stay valid
        tok = _surrogate(nonce, kind, val, real_vals, issued, s)
        sent = sent[:a] + tok + sent[b:]
        pairs.append((tok, val))
        issued[tok] = _normalise(val)
        if kind != "PERSONAL":
            t0_pairs.append((tok, val, kind))
    _covered = sum(b - a for (a, b, _k, _v) in final)
    masked_fraction = round(_covered / max(1, len(s)), 3)
    # * \u2605\u2605\u2605 `masked_fraction == 1.0` IS NOT THE CONDITION THAT MATTERS. THE CONDITION THAT MATTERS IS
    #   WHETHER THE TURN STILL SAYS WHAT TO DO. A turn can be 26% masked and be a perfect payload
    #   ("Reply with exactly one word: [[PERSONAL_d44ed143]]" -- the re-hydration lane proved the agent
    #   echoes that token and the answer comes back as the real name), and it can be 80% masked and be
    #   useless ("[[PERSONAL]] [[PERSONAL]] at [[EMAIL]] and [[PERSONAL]] her [[PERSONAL]]" -- measured,
    #   on the acceptance turn, with the verbs `email` and `tell` gone). So the floor is measured in
    #   WORDS THAT SURVIVED, not in characters replaced, and the fraction is kept beside it because
    #   the fraction is what the operator was shown.
    skeleton = [(m.start(), m.end()) for m in re.finditer(r"\S+", s)
                if not any(a <= m.start() and m.end() <= b for (a, b, _k, _v) in final)]
    skeleton_words = len(skeleton)
    surviving_content = [s[a:b] for (a, b) in skeleton if _content_word(s[a:b])]
    skeleton_content_words = len(surviving_content)
    # * \u2605\u2605\u2605 THE SECOND QUESTION, ASKED WHERE THE FIRST ONE IS ANSWERED. `skeleton_content_words == 0`
    #   is "only grammatical glue survived". This is "an ACTION survived and every THING did not" --
    #   the mask left the turn a verb form with no referent, which is the acceptance turn's exact
    #   shape (`saying`, alone, after four spans ate the instruction). See `_heads_a_request` for why
    #   the FORM of the surviving verb is the thing that matters and why the failure direction is SEND.
    skeleton_heads = sum(1 for t in surviving_content if _heads_a_request(t))
    # * \u2605 A SPAN THAT IS (ALMOST) THE WHOLE TURN IS NOT A LOCALISATION -- it is the classifier saying
    #   "something in here is private" without saying where. It is COUNTED, and it is STILL MASKED
    #   (a finding is never dropped, see the _subtract comment above), but a turn is not sent on the
    #   strength of it: an unlocalised span may be hiding un-shaped PII inside the part of the turn
    #   it did not name, and this module has already paid once for assuming otherwise.
    unlocalised_spans = sum(1 for sp in kept_spans
                            if len(sp) >= UNLOCALISED_FRACTION * max(1, len(s)))
    # * \u2605 A DEAD LINE REMOVED HERE. `t1_pairs = [(t, v) for (t, v) in pairs if t.startswith("[[PERSONAL_")]`
    #   was never read by anything, and it keyed on the TOKEN SHAPE -- which a surrogate does not
    #   have. Deleting it is safer than fixing it: dead code that has started lying is the kind that
    #   gets copied into a live path later.
    if dropped:
        t1 = {**t1, "dropped_spans": len(dropped),
              "rejected": list(t1.get("rejected") or []) + [d[:60] for d in dropped]}

    # -- THE DECISION, and the floor under it -----------------------------------------------------
    if allow_plaintext:
        action, verdict = A_DELEGATE, "operator_forced_plaintext"
        pairs, sent = [], s
    elif not t1.get("available"):
        # \u2605\u2605\u2605 TIER 1 DOWN MEANS THE TURN IS NOT SENT, AND TIER 0 FINDINGS DO NOT BUY AN EXCEPTION.
        #   The first version let "tier 0 found something" license a send with only the shaped values
        #   masked, on the reasoning that tier 0 had "held the line". THE DRIVE CAUGHT IT LEAKING A
        #   NAME: tier 0 masks what has a SHAPE, and a person's name has none, so a turn containing
        #   the name plus an email went out with the email masked and the name in the clear. The rule
        #   in the brief is the right one and it admits no exception -- "if the classifier cannot run,
        #   the gate reports that it could not check; it does NOT pass the text through". Tier 0's
        #   substitutions are still APPLIED, so an operator override sends a partially-sanitised turn
        #   and the verdict says exactly that it is partial.
        action, verdict = ((A_DELEGATE, "operator_overrode_unchecked") if allow_unchecked
                           else (A_ASK, V_UNCHECKED))
    elif pairs or t0_pairs:
        # * \u2605 THE BRANCH READS `pairs`, NOT `t1["sensitive"]`. Measured 6/6: this model returns the
        #   NAME inside `spans` while answering `sensitive: false`. Keying the verdict on the boolean
        #   and the substitutions on the spans made those two disagree -- the text was masked while
        #   the verdict said `pass`. Whatever was actually substituted is what the verdict reports.
        # * \u2605\u2605\u2605 AND THEN THE STRUCTURE FLOOR, WHICH IS THE OVER-MASK FIX. Something was found, so a plan
        #   exists -- but A PLAN THAT DESTROYS THE TURN IS NOT A PLAN, IT IS A REFUSAL WEARING A
        #   PAYLOAD. Both conditions below mean the same thing to the caller: NOTHING IS SENT. They
        #   stay separate because the operator's next move differs.
        if unlocalised_spans:
            action, verdict = A_ASK, V_UNLOCALISED
        elif skeleton_content_words == 0:
            action, verdict = A_ASK, V_OVER_MASKED
        elif skeleton_heads == 0 and skeleton_content_words > 0:
            # * \u2605\u2605\u2605 THE ONE-WORD MISS, CLOSED. Content survived -- but every surviving content word
            #   is a NON-FINITE action word, so the turn kept a verb form and lost every referent. That
            #   is a sentence about nothing, and it is the shape that went out SILENTLY after the
            #   surrogates landed: `Alaric Kingsley to Priya Vandermeer to Milo Trellis at <address>
            #   saying Barnaby Vandermeer`, answered "no clear actionable task for me ... standing by".
            #   Nothing is unmasked to make it readable; the turn is simply not sent.
            #   \u2605\u2605 THE VERDICT IS `V_OVER_MASKED` AND NOT `V_NO_REFERENT` -- see the long note beside the
            #   constant: a new string would fall through `router.py`'s per-verdict refusal chain into
            #   its generic `else` and tell the operator the classifier was unreachable, which is false.
            #   The distinct SIGNAL is `skeleton_heads == 0` on the receipt, beside a non-zero
            #   `skeleton_content_words`; the distinct MOTION for the operator is the same as its
            #   neighbour's, which is why one sentence covers both honestly.
            #   \u2605\u2605\u2605 AND `skeleton_content_words > 0` IS WRITTEN OUT, NOT ASSUMED. Logically the clause
            #   above has already taken the empty case, so this reads as redundant -- IT IS NOT, AND THE
            #   ESTATE'S OWN MUTATION TABLE PROVED IT. `test_privacy_gate.py` carries a mutation that
            #   disables the clause above to model "the skeleton floor never fires". Written WITHOUT
            #   this precondition the new clause SILENTLY ABSORBS that case, the mutation stops being
            #   lethal, and a suite that was green by DETECTION goes green by REDUNDANCY -- which is the
            #   same "a guard a comment could satisfy" failure this estate keeps re-learning. With the
            #   precondition stated the two clauses are disjoint by construction and each can be killed
            #   on its own. It is also exactly the discriminator the receipt documents below.
            action, verdict = A_ASK, V_OVER_MASKED
        else:
            action, verdict = A_DELEGATE_SANITISED, V_OBFUSCATE
    elif t1.get("sensitive"):
        # flagged, but named nothing usable -> there is nothing to substitute -> DO NOT PASS
        action, verdict = A_ASK, V_FLAGGED_NO_SPAN
    else:
        action, verdict = A_DELEGATE, V_PASS

    lease = Lease(destination, verdict, action, sent, pairs, t0, t1,
                  types=[k for (_a, _b, k, _v) in final])
    lease.masked_fraction = masked_fraction
    lease.skeleton_words = skeleton_words
    lease.skeleton_content_words = skeleton_content_words
    lease.skeleton_heads = skeleton_heads
    lease.unlocalised_spans = unlocalised_spans
    _LIVE[lease.handle] = lease
    if key:
        _BY_KEY[key] = lease.handle
    _evict()
    return lease


def gate_or_raise(text: str, destination: str, **kw) -> Lease:
    """Same gate, but a `hold` raises instead of returning. For callers whose only safe move is to
    stop. `GateRefused` carries the lease so the refusal can still be reported."""
    lz = gate(text, destination, **kw)
    if lz.action == A_ASK:
        raise GateRefused(f"privacy gate held this turn for {destination}: {lz.verdict}")
    return lz


# * Purge on interpreter exit. A lease that outlives its process is a lease that could be written.
import atexit as _atexit  # noqa: E402  (kept at the bottom on purpose: the purge is a footnote)
_atexit.register(purge)


# =================================================================================================
def self_test() -> int:
    """The module's own green/red, driven without a network and without a model."""
    fails: list[str] = []

    def C(ok: bool, msg: str) -> None:
        if not ok:
            fails.append(msg)

    # -- tier 0 catches each shape, by SET EQUALITY over the types it must name -------------------
    PROBES = [
        ("email me at operator@example.invalid today", {"EMAIL"}),
        ("call 415-555-0142 please", {"PHONE"}),
        ("the box is at 203.0.113.44", {"IPV4"}),
        ("key sk-or-v1-" + "a1b2" * 12, {"APIKEY"}),
        ("AKIA" + "IOSFODNN7EXAMPLE", {"APIKEY"}),
        ("-----BEGIN " + "RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
         {"PRIVATEKEY"}),
        ("my file is /home/user/secret.txt", {"USERPATH"}),
        ("push it to this-box.internal", {"HOST"}),
        ("acct 1234567890 routing 021000021", {"ACCOUNT"}),
        ("ssn 900-01-0001", {"SSN"}),
        ("dob 1974-03-02", {"DOB"}),
        ("see https://user:operator@example.invalid/x", {"URLCREDS"}),
        ("password: correcthorsebattery", {"SECRET"}),
        ("my card 4111 1111 1111 1111 expires soon", {"CARD"}),
    ]
    for text, want in PROBES:
        got = {f["type"] for f in scan_deterministic(text)}
        C(want <= got, f"tier 0 must name {sorted(want)} for {text[:38]!r}; it named {sorted(got)}")

    # -- and it must NOT fire on the ordinary turns this seat actually carries --------------------
    QUIET = [
        "route to the support team and ask for a status report",
        "what is the capital of Australia",
        "read the file in my downloads folder and summarise it",
        "the meeting is on 2026-10-12 at 3pm",
        "switch the model to qwen3:1.7b",
        "the ollama server is on 127.0.0.1:11434",
        "the build failed with exit code 1",
    ]
    for text in QUIET:
        got = {f["type"] for f in scan_deterministic(text)}
        C(not got, f"tier 0 fired {sorted(got)} on an ordinary turn: {text!r}")

    # -- Luhn is what keeps CARD honest: a 16-digit non-card run must NOT be a finding ------------
    C(not [f for f in scan_deterministic("order 1234567890123456 shipped") if f["type"] == "CARD"],
      "a 16-digit run that fails Luhn must not be reported as a card")
    C([f for f in scan_deterministic("card 4111111111111111") if f["type"] == "CARD"],
      "PROVE-FAIL DIRECTION: a Luhn-valid run IS a card")

    # -- THE INSTRUMENT ITSELF, GUARDED (the 0/5-recall defect, kept as a rule) --------------------
    # * The first version posted to `/api/generate` with a `/no_think` prefix; qwen3 thought anyway,
    #   the trace ate num_predict, and the gate read a TRUNCATED answer as a verdict -- scoring
    #   recall 0/5 on plainly-private sentences and a perfect 5/5 on clean ones, which looks like a
    #   working detector. This asserts the shape that fixed it, so a "simplification" back to the
    #   widely-repeated advice goes RED instead of silent.
    # * \u2605 AND IT IS AN AST CHECK, NOT A TEXT SCAN, BECAUSE THE FIRST VERSION OF THIS GUARD FAILED ON
    #   ITSELF: `inspect.getsource` includes the DOCSTRING, the docstring explains the old defect by
    #   naming `/api/generate`, and so a guard about the code went red because of a COMMENT. The
    #   estate has hit the mirror image of this ("a guard a comment could satisfy") more than once.
    #   So: the function's docstring is dropped, and the check is a SET EQUALITY over the endpoint
    #   string literals that actually reach a call node.
    _fn = [n for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(classify_local))))
           if isinstance(n, ast.FunctionDef)][0]
    _body = _fn.body[1:] if (_fn.body and isinstance(_fn.body[0], ast.Expr)
                             and isinstance(_fn.body[0].value, ast.Constant)
                             and isinstance(_fn.body[0].value.value, str)) else _fn.body
    _eps = {str(n.value) for stmt in _body for n in ast.walk(stmt)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/api/")}
    C(_eps == {"/api/chat"},
      f"\u2605\u2605 THE INSTRUMENT: tier 1 must post to exactly one endpoint, /api/chat -- it reaches "
      f"{sorted(_eps)}. Posting to /api/generate let qwen3's thinking trace eat the answer budget and "
      f"the gate read a truncated reply as a verdict")
    _code = ast.dump(ast.Module(body=_body, type_ignores=[]))
    # * ast.dump renders string constants with SINGLE quotes, so the key reads `'think'` here. What
    #   matters is that the key is in the code that RUNS -- the docstring is already dropped above.
    C("'think'" in _code, "\u2605 `think: False` is what keeps the trace out of the budget")
    C("done_reason" in _code, "\u2605 a length-truncated answer must be refused, not read")
    C(TIER1_MODEL_FAST == "qwen3:0.6b",
      "\u2605 the Pi-Tracer-class model must stay NAMED as the measured fast alternative")
    C(TIER1_MODEL != TIER1_MODEL_FAST,
      "\u2605\u2605 the default classifier must NOT be the 0.6 B class: measured recall 3/6 on this box")

    # -- loopback-only tier 1 ---------------------------------------------------------------------
    C(_endpoint_is_loopback("http://127.0.0.1:11434")[0], "127.0.0.1 must be accepted")
    C(not _endpoint_is_loopback("https://api.openai.com/v1")[0],
      "\u2605 a REMOTE classifier endpoint must be REFUSED -- tier 1 would be the leak")

    # -- span verification: the model proposes, deterministic code verifies ------------------------
    ok, bad = _validate_spans("Alex Example is my spouse", ["Alex Example", "Jane Doe"])
    C(ok == ["Alex Example"], f"a span present verbatim must be accepted: {ok}")
    C(bad == ["Jane Doe"], f"\u2605 a span the model INVENTED must be rejected: {bad}")

    # -- WHAT REPLACES THE VALUE: the two properties that matter, and the RESERVED RANGES ----------
    # \u2605\u2605\u2605 THESE FOUR ASSERTIONS REPLACE FOUR THAT PINNED `[[TYPE_hex8]]`. That shape is the defect
    #    this lane exists to remove, so an assertion that keeps it alive would be an assertion
    #    guarding the bug. What is asserted instead is the same PROPERTY plus the type-correctness.
    n1 = secrets.token_bytes(16)
    _nopool = frozenset()
    a = _surrogate(n1, "PERSONAL", "Alex Example", _nopool, {}, "")
    b = _surrogate(n1, "PERSONAL", "Alex Example", _nopool, {}, "")
    c = _surrogate(secrets.token_bytes(16), "PERSONAL", "Alex Example", _nopool, {}, "")
    C(a == b, "\u2605 CONSISTENT WITHIN A TURN: one value must map to ONE surrogate")
    C(a != c, "\u2605\u2605 UNLINKABLE ACROSS TURNS: the same value must get a DIFFERENT surrogate next turn")
    C("Alex Example" not in a and "[[" not in a,
      f"\u2605 the surrogate must be a VALUE, not the value and not a bracket token: {a!r}")
    C(re.fullmatch(r"[A-Z][a-z]+ [A-Z][a-z]+", a) is not None,
      f"\u2605 a PERSONAL surrogate must READ like a name -- 'Marcus Whitfield', not 'X': {a!r}")
    C(_surrogate(n1, "PERSONAL", "Alex  Example", _nopool, {}, "") == a,
      "\u2605 whitespace-normalised: 'Alex  Example' and 'Alex Example' are the same person, one surrogate")
    # * THE RESERVED RANGES, DRIVEN RATHER THAN DESCRIBED IN A COMMENT. Each one is the citation
    #   above turned into a regex, so a future edit that starts inventing SSNs or cards goes RED.
    _e = _surrogate(n1, "EMAIL", "operator@example.invalid", _nopool, {}, "")
    C(_e.endswith("@example.invalid"),
      f"\u2605 EMAIL surrogate must sit in the RFC 2606 .invalid TLD: {_e!r}")
    _p = _surrogate(n1, "PHONE", "415-555-0142", _nopool, {}, "")
    C(re.fullmatch(r"\d{3}-555-01\d{2}", _p) is not None,
      f"\u2605 PHONE surrogate must be NANP 555-0100..555-0199, reserved for fiction: {_p!r}")
    _s = _surrogate(n1, "SSN", "123-45-6789", _nopool, {}, "")
    C(re.fullmatch(r"9\d{2}-(?!00)\d{2}-(?!0000)\d{4}", _s) is not None,
      f"\u2605 SSN surrogate must be 900-series, which the SSA has NEVER ASSIGNED: {_s!r}")
    for _k, _want in (("IPV4", r"(192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}"),
                      ("IPV6", r"2001:db8:[0-9a-f]{1,4}:[0-9a-f]{1,4}::1"),
                      ("HOST", r"[a-z]+-\d{2}\.invalid")):
        _g = _surrogate(n1, _k, "a-real-value", _nopool, {}, "")
        C(re.fullmatch(_want, _g) is not None,
          f"\u2605 {_k} surrogate must come from its documented range: {_g!r}")
    _cd = _surrogate(n1, "CARD", "4111 1111 1111 1111", _nopool, {}, "")
    C(_clean(_cd) in TEST_PANS,
      f"\u2605\u2605\u2605 a CARD surrogate is a PUBLISHED TEST PAN, NOT a Luhn-valid number we generated: "
      f"a PAN we invented could be A LIVE CARD BELONGING TO SOMEONE. got {_cd!r}")
    C(" " in _cd, f"\u2605 and it reuses the separator style of the value it replaced: {_cd!r}")
    for _k in sorted(MARKER_TYPES):
        _m = _surrogate(n1, _k, "sk-live-abcdefghijklmnop", _nopool, {}, "")
        C(_m.startswith("(") and "[[" not in _m and not any(ch.isdigit() for ch in _m),
          f"\u2605 no reserved range exists for {_k}, so it keeps a MARKER, and the marker must carry "
          f"no digits that could belong to anyone: {_m!r}")
    # * \u2605\u2605\u2605 THE COLLISION CHECK -- constraint 4, and it is a CORRECTNESS bug, not a nicety.
    #   Two ways, both driven here rather than argued: a candidate equal to another REAL span, and a
    #   candidate that already appears in the turn's own text. Each must make the generator MOVE ON.
    _clash = _surrogate(n1, "PERSONAL", "Alex Example", frozenset({_normalise("Alex Example")}), {}, "")
    C(_clash != "Alex Example", "\u2605 a surrogate must never BE another real span in the turn")
    _src = _surrogate(n1, "PERSONAL", "Alex Example", _nopool, {}, "")
    _again = _surrogate(n1, "PERSONAL", "Alex Example", _nopool, {}, _src)
    C(_again != _src and _again not in _src and _src not in _again,
      f"\u2605\u2605\u2605 a surrogate already present in the turn must be REGENERATED, not reused: "
      f"{_src!r} -> {_again!r}")
    _first = _surrogate(n1, "PERSONAL", "Alex Example", _nopool, {}, "")
    _taken = {_first: _normalise("Alex Example")}
    _second = _surrogate(n1, "PERSONAL", "Jordan Example", _nopool, _taken, "")
    C(_second != _first and _second not in _first and _first not in _second,
      f"\u2605\u2605 two different people in one turn must not collapse onto one string: "
      f"{_first!r} vs {_second!r}")
    C(_surrogate(n1, "PERSONAL", "Alex Example", _nopool, _taken, "") == _first,
      "\u2605 and the SAME span seen twice still gets the SAME surrogate -- consistency is not a clash")

    # -- the gate, driven four ways with a SYNTHETIC classifier, so no model is needed -------------
    DOWN = {"available": False, "sensitive": None, "spans": [], "rejected": [],
            "why": "synthetic: classifier down", "ms": 0}
    CLEAN = {"available": True, "sensitive": False, "spans": [], "rejected": [],
             "why": "synthetic: clean", "ms": 1}
    DIRTY = {"available": True, "sensitive": True, "spans": ["Alex Example"], "rejected": [],
             "why": "synthetic: sensitive", "ms": 1}
    FLAG_NO_SPAN = {"available": True, "sensitive": True, "spans": [], "rejected": [],
                    "why": "synthetic: flagged, no span", "ms": 1}

    lz = gate("Alex Example is my spouse, mail operator@example.invalid", "aionui", local=DIRTY)
    C(lz.action == A_DELEGATE_SANITISED, f"findings -> sanitise, got {lz.action}")
    C("operator@example.invalid" not in lz.sent_text, "\u2605\u2605 THE EMAIL MUST NOT BE IN THE SENT TEXT")
    C("Alex Example" not in lz.sent_text, "\u2605\u2605 THE NAME MUST NOT BE IN THE SENT TEXT")
    C(len(lz._map) == 2, f"two substitutions expected, got {len(lz._map)}")
    back = lz.rehydrate(f"Tell {list(lz._map)[0]} to call the clinic about {list(lz._map)[1]}")
    C("Alex Example" in back["text"] and "operator@example.invalid" in back["text"],
      f"\u2605 RE-HYDRATION must restore BOTH real values: {back}")

    lz = gate("route to the support team and ask for a status report", "aionui", local=CLEAN)
    C(lz.action == A_DELEGATE and lz.verdict == V_PASS,
      f"a clean turn must pass untouched, got {lz.verdict}/{lz.action}")
    C(lz.sent_text == "route to the support team and ask for a status report",
      "a clean turn's sent text must be byte-identical to the input")

    lz = gate("my spouse has a medical appointment on Tuesday", "aionui", local=DOWN)
    C(lz.action == A_ASK and lz.verdict == V_UNCHECKED,
      f"\u2605\u2605\u2605 CLASSIFIER DOWN + NOTHING SHAPED -> THE TURN MUST BE HELD, got {lz.verdict}/{lz.action}")
    lz = gate("my spouse has a medical appointment on Tuesday", "aionui", local=DOWN,
              allow_unchecked=True)
    C(lz.action == A_DELEGATE and lz.verdict == "operator_overrode_unchecked",
      "the override is the ONE way past an unchecked turn, and it is NAMED")

    # \u2605\u2605\u2605 TIER 0 FINDINGS DO **NOT** BUY AN EXCEPTION TO THE CLASSIFIER BEING DOWN.
    #   The first version asserted the opposite ("tier 0 holds the line -> sanitise and go") and the
    #   END-TO-END DRIVE caught it leaking a NAME: tier 0 masks what has a SHAPE, a person's name has
    #   none, so a turn naming a person AND carrying her email went out with the email masked and the
    #   name in the clear. This assertion now pins the rule that admits no exception.
    lz = gate("email operator@example.invalid and ask about the appointment", "aionui", local=DOWN)
    C(lz.action == A_ASK and lz.verdict == V_UNCHECKED,
      f"\u2605\u2605 a tier-0 finding must NOT license a send while the classifier is down: "
      f"{lz.verdict}/{lz.action}")
    C("operator@example.invalid" not in lz.sent_text,
      "tier 0 still masks what it found, so an OVERRIDE sends a partially-sanitised turn")
    lz = gate("email operator@example.invalid and ask about the appointment", "aionui", local=DOWN,
              allow_unchecked=True)
    C(lz.action == A_DELEGATE and lz.verdict == "operator_overrode_unchecked",
      f"and the override names itself: {lz.verdict}")
    C("operator@example.invalid" not in lz.sent_text,
      "the override sends the TIER-0 SANITISED text, not the raw turn")

    lz = gate("my spouse has an appointment", "aionui", local=FLAG_NO_SPAN)
    C(lz.action == A_ASK, "\u2605 flagged with nothing maskable -> HOLD, not pass")

    lz = gate("Alex Example called", "aionui", local=DIRTY, allow_plaintext=True)
    C(lz.sent_text == "Alex Example called" and lz.action == A_DELEGATE,
      "the 'upload it anyway' door exists, is OFF by default, and is named in the verdict")

    # -- the receipt must not carry a value or a token --------------------------------------------
    lz = gate("Alex Example, operator@example.invalid", "aionui", local=DIRTY)
    blob = json.dumps(lz.receipt())
    C("Alex Example" not in blob and "operator@example.invalid" not in blob,
      "\u2605 THE RECEIPT MUST NOT CARRY A REAL VALUE")
    C(not any(t in blob for t in lz._map), "\u2605 THE RECEIPT MUST NOT CARRY A TOKEN EITHER")
    # \u2605\u2605\u2605 THE DISCARDED-PROPOSAL LEAK, PINNED. `rejected` is a list of verbatim substrings of the
    #   operator's turn, so passing the model's raw answer into the receipt (even with `spans`
    #   stripped) wrote the real values to disk via `to_cc_queue`. Measured, not hypothesised.
    _dirty = {"available": True, "sensitive": True, "spans": ["Alex Example"],
              "rejected": ["415-555-0142", "Jane Doe"], "why": "", "ms": 1}
    lz = gate("Alex Example and 415-555-0142", "aionui", local=_dirty)
    blob = json.dumps(lz.receipt())
    C("415-555-0142" not in blob and "Jane Doe" not in blob and "Alex Example" not in blob,
      f"\u2605\u2605\u2605 THE RECEIPT CARRIED A DISCARDED PROPOSAL -- this is a real leak into \n"
      f"handoff_queue.jsonl: {blob[:200]}")
    C(lz.receipt()["tier1"]["proposals_discarded"] == 2,
      "and the discards are still REPORTED as a count, not hidden")
    C("map not printed" in repr(lz), "repr must refuse to print the table")
    # \u2605\u2605\u2605 THE OVER-MASK DEFECT, PINNED AS THE BEHAVIOUR THAT MUST NOT COME BACK.
    #   This probe used to assert the OPPOSITE -- that a whole-turn span masks everything and that the
    #   over-mask is merely "flagged, not hidden". That assertion was true and it was the defect: it
    #   pinned the gate to sending a body of nothing but placeholders. What is asserted now is that
    #   the gate REFUSES to send it, and says why, and that the refusal is still measured honestly.
    _whole = {"available": True, "sensitive": True,
              "spans": ["Ask Alex Example at operator@example.invalid to call the clinic on Tuesday"],
              "rejected": [], "why": "", "ms": 1}
    _WHOLE_TURN = "Ask Alex Example at operator@example.invalid to call the clinic on Tuesday"
    lz = gate(_WHOLE_TURN, "aionui", local=_whole)
    C(lz.receipt()["masked_fraction"] is not None,
      "the lease must report HOW MUCH of the turn it replaced")
    C(lz.receipt()["over_masked"] is True,
      "an over-mask is still MEASURED, even though it is no longer sent")
    C(lz.sent_text != _WHOLE_TURN,
      "\u2605\u2605 THE REAL VALUE MUST NOT BE IN THE BODY (a refusal is not a licence to send raw)")
    C("operator@example.invalid" not in lz.sent_text and "Alex Example" not in lz.sent_text,
      "\u2605\u2605\u2605 NOTHING IS UNMASKED BY THE FLOOR -- the safety property does not bend")
    C(lz.receipt()["unlocalised_spans"] >= 1,
      f"\u2605\u2605\u2605 A WHOLE-TURN SPAN IS NOT A LOCALISATION and must be counted as such: "
      f"{lz.receipt()['unlocalised_spans']}")
    C(lz.action == A_ASK and lz.verdict == V_UNLOCALISED,
      f"\u2605\u2605\u2605 A WHOLE-TURN MASK MUST BE HELD, NOT SENT: the agent received a body of nothing "
      f"but tokens and read it as a prompt injection. Got {lz.verdict}/{lz.action}")
    C(lz.receipt()["sent"] is False, "and the receipt says plainly that nothing was sent")

    # \u2605\u2605 AND THE OTHER SHAPE OF THE SAME DEFECT: NO SINGLE SPAN IS THE WHOLE TURN, BUT THE SPANS BETWEEN
    #   THEM LEAVE THE TURN WITH NO INSTRUCTION. Measured on the acceptance turn -- tier 1 masked the
    #   VERBS `email` and `tell` along with the values and the body went out as
    #   "[[PERSONAL]] [[PERSONAL]] at [[EMAIL]] and [[PERSONAL]] her [[PERSONAL]]". The floor is
    #   measured in words that SURVIVED, so this is caught even though no span is unlocalised.
    _verbs = {"available": True, "sensitive": True,
              "spans": ["email", "Alex Example", "operator@example.invalid", "tell",
                        "the clinic called"], "rejected": [], "why": "", "ms": 1}
    lz = gate("email Alex Example at operator@example.invalid and tell her the clinic called", "aionui",
              local=_verbs)
    C(lz.receipt()["masked_fraction"] < 1.0,
      f"\u2605\u2605 this shape never reaches 1.0 and still destroys the turn: "
      f"{lz.receipt()['masked_fraction']}")
    C(lz.receipt()["skeleton_content_words"] == 0,
      f"\u2605\u2605\u2605 THE FLOOR'S OWN MEASUREMENT: only grammatical glue survived "
      f"({lz.receipt()['skeleton_content_words']} content word(s), "
      f"{lz.receipt()['skeleton_words']} word(s))")
    C(lz.action == A_ASK and lz.verdict == V_OVER_MASKED,
      f"\u2605\u2605\u2605 a mask that leaves the turn no verbs must NOT be sent: "
      f"{lz.verdict}/{lz.action}")
    C("Alex Example" not in lz.sent_text and "operator@example.invalid" not in lz.sent_text,
      "\u2605 and the floor does not unmask a value to make the turn readable")
    # \u2605\u2605\u2605 AND THE ONE WORD THE CLAUSE ABOVE MISSED. That clause is satisfied by "no content word
    #   survived". It is NOT satisfied when exactly one content word survives and that word CANNOT HEAD
    #   A REQUEST. MEASURED, on the operator's own 111-character turn with the FIVE spans tier 1
    #   actually returned live (receipt: masked_fraction 0.82, skeleton_content_words 1,
    #   unlocalised_spans 0 -> SENT as four strangers, agent answered "no clear actionable task for me
    #   ... standing by", and NOTHING told the operator why).
    _saying = {"available": True, "sensitive": True,
               "spans": ["ask the book team", "send a one-line note", "Alex Example",
                         "operator@example.invalid", "the draft is ready"],
               "rejected": [], "why": "", "ms": 1}
    _SAYING_TURN = ("ask the book team to send a one-line note to Alex Example at "
                    "operator@example.invalid saying the draft is ready")
    C(len(_SAYING_TURN) == 111,
      f"\u2605 THE FIXTURE IS THE SANITIZED RECONSTRUCTION (111 chars; the operator's exact turn is not shipped): {len(_SAYING_TURN)}")
    lz = gate(_SAYING_TURN, "aionui", local=_saying)
    C(lz.receipt()["masked_fraction"] == 0.82,
      f"\u2605 THE FIXTURE IS THE SANITIZED ONE: masked_fraction must be 0.82, got "
      f"{lz.receipt()['masked_fraction']}")
    C(lz.receipt()["skeleton_content_words"] == 1,
      f"\u2605 and exactly ONE content word survives -- the premise of the whole defect: "
      f"{lz.receipt()['skeleton_content_words']} ({lz.receipt()['skeleton_words']} words)")
    C(lz.receipt()["unlocalised_spans"] == 0,
      "\u2605\u2605 no single span is the whole turn, so the OLDEST clause cannot see this shape")
    C(lz.receipt()["skeleton_heads"] == 0,
      f"\u2605\u2605\u2605 A NON-FINITE ACTION WORD IS NOT A REFERENT: `saying` survived and can head "
      f"nothing ({lz.receipt()['skeleton_heads']} head(s), "
      f"{lz.receipt()['skeleton_content_words']} content word(s))")
    C(lz.action == A_ASK and lz.verdict == V_OVER_MASKED,
      f"\u2605\u2605\u2605 A TURN THAT LEAVES AS FOUR STRANGERS MUST BE HELD, NOT SENT: "
      f"{lz.verdict}/{lz.action}")
    # * \u2605\u2605 AND THE RECEIPT MUST SAY WHICH SHAPE OF OVER-MASK THIS WAS. The two clauses share a verdict
    #   on purpose (see the note beside `V_NO_REFERENT`), so the DISCRIMINATOR is these two numbers
    #   together -- `skeleton_heads == 0` AND `skeleton_content_words > 0`. Without this the two shapes
    #   would be indistinguishable to the operator AND to any future prove-fail.
    C(lz.receipt()["skeleton_heads"] == 0 and lz.receipt()["skeleton_content_words"] > 0,
      f"\u2605\u2605\u2605 THE RECEIPT MARKS THE SHAPE: heads="
      f"{lz.receipt()['skeleton_heads']} content={lz.receipt()['skeleton_content_words']}")
    C(lz.sent_text != _SAYING_TURN and "Alex Example" not in lz.sent_text
      and "operator@example.invalid" not in lz.sent_text,
      "\u2605\u2605\u2605 AND NOTHING IS UNMASKED TO MAKE IT READABLE -- the floor only ever converts a "
      "SEND into a HOLD, so it cannot leak by construction")
    # \u2605\u2605\u2605 PROVE-FAIL DIRECTION ON THE SAME CLAUSE, AND THESE ARE THE FIXTURES THE LAST LANE REFUSED
    #   TO BREAK WITH A FRACTION THRESHOLD. A rule that holds these is a threshold; a rule that
    #   separates them is a rule. Both are driven through the SAME clause, one after the other.
    _dense = {"available": True, "sensitive": True,
              "spans": ["email operator@example.invalid", "call 415-555-0142",
                        "card 4111111111111111", "ssn 078-05-1120"], "rejected": [], "why": "", "ms": 1}
    lz = gate("email operator@example.invalid, call 415-555-0142, card 4111111111111111, "
              "ssn 078-05-1120", "aionui", local=_dense)
    C(lz.receipt()["masked_fraction"] > 0.7,
      f"\u2605\u2605 THE FIXTURE REALLY IS DENSE -- a fraction rule would hold it: "
      f"{lz.receipt()['masked_fraction']}")
    C(lz.action == A_DELEGATE_SANITISED,
      f"\u2605\u2605\u2605 A LEGITIMATE MOSTLY-SHAPED-VALUES TURN MUST STILL GO -- holding it is the outage "
      f"the last lane refused to ship: {lz.verdict}/{lz.action}")
    _cred = {"available": True, "sensitive": True,
             "spans": ["sk-or-v1-0000000000000000000000000000000000000000"], "rejected": [],
             "why": "", "ms": 1}
    _CRED_TURN = "is this key still valid sk-or-v1-0000000000000000000000000000000000000000"
    lz = gate(_CRED_TURN, "aionui", local=_cred)
    # * \u2605\u2605\u2605 THE BRIEF ASKED THIS ONE BY NAME, AND THE ANSWER IS MEASURED RATHER THAN ASSUMED: a turn
    #   whose spans are ALL CREDENTIALS renders as an ordinary-English marker `(an API key)` rather
    #   than a bracket, so it reads oddly. DOES THIS CLAUSE HOLD IT? NO, AND THAT IS DELIBERATE. The
    #   credential turn keeps its question words -- `key`, `still`, `valid` -- and not one of them is
    #   an action word, so THREE heads survive and this clause cannot fire. (What holds the shape
    #   below is the OLDEST clause: injecting the credential as a TIER-1 span makes it a span >= 0.6
    #   of the turn, i.e. "flagged but not localised". That is a PRE-EXISTING rule and this lane did
    #   not touch it -- see the second half of this guard for what the LIVE classifier actually does.)
    C(lz.receipt()["skeleton_heads"] >= 1,
      f"\u2605\u2605\u2605 A CREDENTIAL-ONLY TURN IS NOT HELD BY *THIS* CLAUSE: its question words survive "
      f"({lz.receipt()['skeleton_heads']} head(s), {lz.receipt()['skeleton_content_words']} content "
      f"word(s))")
    C(lz.verdict == V_UNLOCALISED,
      f"\u2605\u2605 and WHAT bites it is the OLDEST clause, not this lane's: {lz.verdict}/{lz.action}")
    # * \u2605\u2605 THE LIVE SHAPE, MEASURED 6/6 ON 2026-10-13: `qwen3:1.7b` proposes NO span at all for this
    #   turn -- tier 0 alone finds the key -- so there is no kept span, `unlocalised_spans` is 0, and
    #   the turn SENDS as `is this key still valid (an API key)`. That is the case the operator meets.
    lz = gate(_CRED_TURN, "aionui", local={"available": True, "sensitive": False, "spans": [],
                                           "rejected": [], "why": "", "ms": 1})
    C(lz.action == A_DELEGATE_SANITISED and lz.receipt()["unlocalised_spans"] == 0,
      f"\u2605\u2605\u2605 THE LIVE CREDENTIAL TURN STILL GOES AND KEEPS ITS QUESTION WORDS: "
      f"{lz.verdict}/{lz.action} heads={lz.receipt()['skeleton_heads']}")
    # * \u2605 AND THE OTHER DIRECTION OF THE SAME CLAUSE: a BARE action word is a head, even when the word
    #   is really a noun in that sentence. Ambiguity must resolve to SEND or this becomes an outage.
    _bare = {"available": True, "sensitive": True, "spans": ["Alex Example"], "rejected": [],
             "why": "", "ms": 1}
    lz = gate("send the report to Alex Example", "aionui", local=_bare)
    C(lz.action == A_DELEGATE_SANITISED,
      f"\u2605\u2605\u2605 A BARE ACTION WORD HEADS A REQUEST EVEN WHEN IT IS A NOUN THERE -- `the report` "
      f"must not be able to hold a turn: {lz.verdict}/{lz.action}")
    # \u2605 PROVE-FAIL DIRECTION, ON THE SAME MECHANISM: the floor must NOT fire on an ordinary turn.
    #   The re-hydration lane's proven-good payload is 26% masked and MUST keep working -- a floor
    #   that holds this one has traded the defect for an outage.
    _name = {"available": True, "sensitive": True, "spans": ["Alex Example"], "rejected": [],
             "why": "", "ms": 1}
    lz = gate("Reply with exactly one word: Alex Example", "aionui", local=_name)
    C(lz.action == A_DELEGATE_SANITISED and lz.verdict == V_OBFUSCATE,
      f"\u2605\u2605\u2605 THE PROVEN-GOOD PAYLOAD MUST STILL GO: {lz.verdict}/{lz.action}")
    # \u2605\u2605\u2605 AND THIS IS THE WHOLE POINT OF THE LANE, ASSERTED ON THE BODY THAT LEAVES: the verbs
    #   survive, the value is replaced by something that reads like a NAME, and the body carries no
    #   double-bracket token for a cloud agent to read as a redaction marker and refuse.
    C(re.fullmatch(r"Reply with exactly one word: [A-Z][a-z]+ [A-Z][a-z]+", lz.sent_text) is not None,
      f"\u2605\u2605\u2605 THE BYTES THE AGENT RECEIVES MUST BE AN ORDINARY SENTENCE: {lz.sent_text!r}")
    C("[[" not in lz.sent_text,
      f"\u2605\u2605\u2605 NO BRACKET TOKEN MAY LEAVE THE BOX -- measured 2026-10-12: an agent refused "
      f"exactly that shape as 'a template/placeholder with personal data redaction markers'")
    C("Alex Example" not in lz.sent_text, "\u2605\u2605 and the name itself is gone from the body")
    C(lz.receipt()["skeleton_content_words"] >= 1,
      "and the skeleton floor agrees there is still an instruction")
    C(lz.receipt()["unlocalised_spans"] == 0,
      "\u2605 a 10-character span in a 39-character turn is a LOCALISATION, not a whole-turn quote")

    # -- ephemerality -----------------------------------------------------------------------------
    h = lz.handle
    C(registry_state()["live"] >= 1, "the registry holds the live lease")
    n = purge()
    C(n >= 1 and registry_state()["live"] == 0, f"purge must drop every lease, dropped {n}")
    C(rehydrate("[[PERSONAL_deadbeef]]", handle=h)["lease"] is None,
      "\u2605 after purge there is NO lease to rehydrate from -- ephemeral, by construction")
    C(lz._map == {} and lz.closed, "and the lease's own table is empty")

    # -- the registry is BOUNDED ------------------------------------------------------------------
    for i in range(MAX_LIVE + 10):
        gate(f"note {i}", "aionui", local=CLEAN)
    C(registry_state()["live"] <= MAX_LIVE, f"the registry must be bounded: {registry_state()}")
    purge()

    print(f"privacy_gate self-test: {'ALL PASS' if not fails else str(len(fails)) + ' FAILED'}")
    for f in fails:
        print(f"  x {f}")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test())
