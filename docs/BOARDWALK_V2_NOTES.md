# BOARDWALK v2 NOTES — the vision, and what Space Agent actually contributes

**Written 2026-10-18 for the v1 handoff.** v1 is `bin/point-at.py` + `jarvis/point_to.py`
(see `docs/POINT_AT_PROTOCOL.md`). This file is the design direction for what was explicitly left
out: the game feel, in-walk search with a source pocket, and time-travel across decisions.

---

## 1 · "SPACE AGENT" — CONFIRMED, AND MISLEADINGLY NAMED

**It is real:** <https://github.com/agent0ai/space-agent> — README says "Created by Agent Zero",
MIT, published under the Agent Zero org (not `frdel/space-agent`, which does not exist). Agent Zero
itself now lives at <https://github.com/agent0ai/agent-zero> (the old `frdel/agent-zero` redirects).

**It is not a desktop screen-guidance tool.** It is an Electron agent workspace that reshapes its
own browser UI. Its "overlay" is a chat panel inside its own page; it has no OS-level capture, no
OCR, no desktop coordinates, and cannot point at a third-party app. Its "Time Travel" is **Git
history of its writable layer** (rollback + revert-as-new-commit behind a preview), not decision
branching. **There is no desktop-grounding mechanism to copy.** What transfers is protocol grammar,
and it is good:

| borrow | why it fits BoardWalk | status in v1 |
|---|---|---|
| **Typed/numbered refs over coordinates** (`[button 18] Continue`) | A citation the model can make and the runtime can check — which is exactly `point-at`'s line ids + phrase resolution | **implemented** (line ids + text, resolution by phrase) |
| **Ref invalidation** — a new `content()` invalidates old ids | Stops a stale target from pointing at last screen's button | **implemented**: `point` re-reads and matches text/anchors; ids are not targets |
| **Post-action telemetry** (`noObservedEffect`, `validationTextAdded`, `valueChanged`) + "if no effect, re-read" | The verification primitive for "did the operator actually get there?" | **partial**: `point-at verify` answers present/absent; diffing the before/after digest is v2 |
| **Escalation ladder** `content` → `detail(ref)` → evaluate last | Match confidence should degrade honestly, not silently | **not needed yet**; our ladder is phrase → anchor → explicit rect |
| **Evidence ≠ instruction** prompt clause | OCR text is attacker-controlled; a page can say "ignore your instructions" | **implemented**: repeated in every `see` result and the protocol doc |
| **Prompt as a tested artifact** (recorded cases, response-shape assertions) | Before tuning a guide-controller prompt, build the transcript suite | **v2** — do this before any controller prompt |
| **Time-travel grammar**: preview before a destructive fork; revert as a NEW point; keep forward refs | Directly maps to "go back and take a different fork" without erasing the walk that happened | **v2 design below** |
| **SKILL.md packs as shareable walkthroughs** | A walk becomes portable, reviewable text | **v2** |
| **Its privacy posture** | **Do NOT copy.** It blanks password values and truncates labels; no PII/token redaction, no allowlist. BoardWalk's filter is the stronger asset. | — |
| **Its in-app DOM overlay / html2canvas** | Cannot point at another app; DOM pixels only | — |

**License:** MIT, "Copyright (c) 2026 Space Agent". No runtime code is used in v1. If any is ever
copied, the copyright and permission notice must ship with it. Treat the project as a design
reference, not a dependency (created 2026-03-27; paths are moving).

---

## 2 · GAME FEEL — what "like a video game" should mean here

Not points and confetti. The qualities that make a tutorial feel like play:

1. **One thing at a time, always visible.** v1 has this: one ring, one card, a step counter.
2. **A map of where you are.** A compact progress rail (chapter / step / branches taken) in the
   card header. It must show *visited forks*, not only a percentage — the percentage lies when you
   backtrack.
3. **Reversibility as a mechanic.** Back/forward must be a first-class control, and a branch change
   must be previewed ("you are about to take a different path from step 4; the old path stays").
4. **Small, honest wins.** When a step's completion is *verified* on screen, mark it verified; when
   it is only "operator pressed Next", mark it implied. v1 already records the difference in the
   feedback store; the v2 UI should show it.
5. **No dead ends.** Every "not found" state names the door (open it / search / skip / leave), as
   `point_to` already does for unresolved steps.

---

## 3 · SEARCH AT A STAGE, AND THE POCKET

**The shape:** a step can carry **questions**, not just instructions. At a decision stage the card
offers `search`; the search happens in a visible surface (not a hidden fetch), and every page the
operator actually opened is stored in the walk's **pocket** with:

```json
{"at":"…","step_id":"pick-a-region","url":"https://…","title":"…","why":"cost per hour",
 "retrieved_at":"…","quote":"…","used_in":"proposal"}
```

- **One store per fact:** `operator/walk_sources.jsonl`, append-only, keyed by walk id + step id.
- **Citations, not scraped dumps.** Store title, URL, retrieval time, and the operator-chosen quote.
  The page content is not copied into the model's context; the operator's selection is.
- **The pocket is visible.** A step's card shows "3 sources kept"; the end of the walk can export
  the pocket as the decision's bibliography.
- **Search is an egress act.** It is announced, one query at a time, and the URL is shown before it
  is opened — the estate's existing rule for `xdg-open`.
- **Privacy:** a search query typed by the operator is theirs; the walk must not auto-fill it from a
  digested screen. The privacy gate applies to whatever text is sent to a model, not to the search
  engine the operator chose to use.

---

## 4 · TIME TRAVEL ACROSS FORKS (the hard one)

A linear walk with Back is already in v1. A **decision walk** needs a graph.

### 4.1 The store

```json
{"walk_id":"…","nodes":[
  {"id":"n7","kind":"decision","title":"Choose a region","say":"…",
   "options":[{"label":"us-east","next":"n8","why":"cheapest"},
              {"label":"eu-west","next":"n9","why":"latency"}]},
  {"id":"n8","kind":"step","screen":{"rect":[…]}, "say":"…", "next":"n10"}],
 "cursor":"n7", "history":[{"at":"…","from":"n6","to":"n7","how":"advance"}]}
```

- `cursor` is the one fact: where the operator is.
- `history` is append-only: every advance/back/branch is a row (the existing `guide_moves.jsonl`
  already records moves; this extends it with `branch`).
- **A fork change does not delete the old path.** It appends a `branch` row and moves the cursor.
  The old path remains reachable ("return to the other branch") — Space Agent's revert-as-new-point,
  applied to decisions.
- **Preview before a destructive change.** Leaving a branch that has completed steps shows what will
  be marked abandoned before it happens; nothing is erased.
- **Forward refs survive.** A node reached from two branches is one node, not two copies.

### 4.2 The UI

- The card's step counter becomes `node 7 · path A` with a small map of the forks taken.
- Back/forward are cursor moves, not array indices.
- "Change the fork" opens the decision node's options with the current choice marked; choosing
  another appends a branch and moves the cursor.
- The end of a path shows the path taken AND the paths not taken, with the reasons — that is the
  decision record the operator actually wants.

### 4.3 Why not build it in v1

The current `guide_focus` sequence is a list; every existing surface (board, panel, voice seat,
overlay) reads `steps` + `i`. A graph is a new store shape and a migration for all four. Doing it
before the point-at loop is driven would be building the map before the road.

---

## 5 · ORDER OF WORK

1. **Drive the v1 loop** with a real agent and a real screen (the WORKS rung). Record what breaks.
2. **Post-step telemetry**: diff the before/after digest; `noObservedEffect` → do not advance, re-read.
3. **Pocket**: `walk_sources.jsonl` + the card's `search` control + the end-of-walk bibliography.
4. **Game rail**: verified vs implied marks, visited-fork map.
5. **Fork graph + time travel**: migrate `guide_focus` to nodes; keep the list shape readable for
   old walks (a list is a graph with one edge per node — a compatibility path, not a second store).
6. Only then tune a controller prompt, with a recorded-transcript suite first (Space Agent's lesson).
