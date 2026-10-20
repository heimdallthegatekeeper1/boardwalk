#!/usr/bin/env python3
"""test_voice_boundary.py -- THE VOICE BOUNDARY: THE REQUIREMENT, THE WARNING, AND THE PAGE.

MODEL/MODE: see the lane report. This suite guards the claims the 2026-10-12 voice-boundary lane made:

  G1  THE REQUIREMENT IS IN THE INSTALL. A `voice_boundary` step exists in the walk, carries BOTH
      approval paths, and states BOTH what crosses and what never crosses.
  G2  IT IS STATED WHERE IT IS READ. `plan()` publishes the block AND the page renders it above the
      step list, outside every filter -- with an honest branch for a walk that arrives without one.
  G3  QUIET ON A HEALTHY BOX. Driven against the LIVE plan, the warning is quiet and nothing is
      detected. A warning that fires here has taught the operator to ignore the real one.
  G4  LOUD ON A FIXTURE. The same parser, fed a synthetic capture client and connection table,
      detects it -- SET EQUALITY on who was named, not "something appeared".
  G5  IT DOES NOT CRY WOLF. Outbound connections ALONE are not a warning: this box has many, and a
      detector that fired on them would be noise. A connection to this box's OWN address is not an
      egress either.
  G6  IT NEVER BLOCKS. A loud warning leaves the row non-blocking, keeps it out of
      `next_actionable`, and changes nothing on the box.
  G7  THE LIMITS ARE SERVED. `cannot_detect` is non-empty, reaches the plan, and the page prints it.
  G8  THE PROBES DO NOT REACH OUT. AST scan: the only subprocesses the boundary code runs are
      `pactl` and `ss`, and there is no urlopen/socket/requests anywhere in it.
  G9  THE GEV CARD CARRIES IT. The card states the positive half (text, not the microphone) and
      keeps the LATENT-vs-ACTIVE precision.
  G10 THE WARNING CANNOT BE FILTERED AWAY. The default "what is not done" view keeps a row that is
      carrying a warning -- driven in `node`, against the shape the route actually serves.

★★★ THE PROVE-FAIL NEVER WRITES A FILE YOU CARE ABOUT, AND THAT IS A CORRECTION, NOT A STYLE CHOICE.

  MEASURED 2026-10-12: the first version of this file mutated the SHIPPED sources and restored them
  in a `finally:`. A timeout killed it between a mutation and its restore, and
  `panel/setup-wizard-popout.html` was left on disk with `/* swBoundary(); */` -- the served page
  silently stopped rendering the voice boundary, and the suite that would have caught it was the
  suite doing the mutating. The estate had already lost a morning to this exact shape.

  SO THE MUTATIONS NOW RUN AGAINST COPIES IN A SCRATCH DIRECTORY. The real files are read, hashed,
  copied, mutated AS COPIES, and are asserted BYTE-IDENTICAL at the end of the run. That assertion is
  the point: it makes "this suite did not touch your working tree" a proved claim rather than a
  promise, and it is safe on a repo where five other lanes are live -- including in
  `operator/setup_blockers.py`, which another lane owns.

Usage:
    python3 operator/test_voice_boundary.py --self-test      exit 0 green, 1 red
    python3 operator/test_voice_boundary.py --prove-fail     mutations on copies, exit 0 iff all red
"""
from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
STORE = HERE / "setup_blockers.py"
GEV = HERE / "gev_option.py"
PAGE = REPO / "panel" / "setup-wizard-popout.html"
COPYABLE = (STORE, GEV, PAGE)
sys.path.insert(0, str(HERE))

# ── THE FIXTURES. * IN THE REAL `ss -tnp state established` FORMAT, WHICH HAS NO `State` COLUMN.
#    The first draft of this lane's own fixture invented one, and the parser then read the LOCAL
#    address as the peer -- an "outbound connection" that was this box talking to itself. The fixture
#    below is copied from the live output, and `_outbound_peers` now derives its columns from the
#    header so the two cannot drift again.
CONN_FIXTURE = """Recv-Q Send-Q                             Local Address:Port           Peer Address:Port Process
0      0                                   203.0.113.44:59426         155.102.176.82:443   users:(("firefox-esr",pid=9498,fd=200))
0      0                                   203.0.113.44:59436         155.102.176.82:443   users:(("firefox-esr",pid=9498,fd=174))
0      0                                   203.0.113.44:38960           203.0.113.44:8780
0      0                                   127.0.0.1:53542                  127.0.0.1:8912  users:(("firefox-esr",pid=9498,fd=61))
0      0      [2605:59c0:697a:cc10:c207:144d:7ee7:caca]:57070  [2606:4700::6812:373]:443  users:(("hermes",pid=1345480,fd=27))
"""
MIC_HELD = """Source Output #37
\tDriver: PipeWire
\tOwner Module: n/a
\tClient: 91
\tSource: 55
\tProperties:
\t\tapplication.name = "Firefox"
\t\tapplication.process.binary = "firefox-esr"
\t\tmedia.name = "AudioCallbackDriver"
"""
MIC_HELD_OTHER = """Source Output #41
\tDriver: PipeWire
\tClient: 77
\tProperties:
\t\tapplication.name = "some-voice-agent"
\t\tapplication.process.binary = "voice-agent-bin"
"""
MIC_FREE = ""


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE GUARDS. Each takes (store_module, page_path, store_path) and returns a list of failures.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def guard_g1(store, page, store_path=STORE) -> list:
    """THE REQUIREMENT IS IN THE INSTALL."""
    fails = []
    step = next((s for s in store.STEPS if s.get("id") == "voice_boundary"), None)
    if not step:
        return ["G1 no `voice_boundary` step in STEPS -- the requirement is not part of the install"]
    b = step.get("button") or {}
    if not str(step.get("typing") or "").strip():
        fails.append("G1 the boundary row has no typed approval path")
    if not (b.get("label") and b.get("action")):
        fails.append("G1 the boundary row has no button")
    st = str(store.VOICE_BOUNDARY.get("statement") or "")
    if "text" not in st.lower() or "audio" not in st.lower():
        fails.append("G1 the statement does not name BOTH sides (text crosses, audio does not)")
    # * SET EQUALITY, NOT PRESENCE: the step must be in STEPS, in the plan, AND in the renderer.
    live = store.plan()
    ids = {r["id"] for r in live["steps"]}
    if ids != {s["id"] for s in store.STEPS}:
        fails.append(f"G1 plan() ids != STEPS ids: {sorted(ids ^ {s['id'] for s in store.STEPS})}")
    if "voice_boundary" not in ids:
        fails.append("G1 the step is in STEPS but plan() dropped it")
    if step["felt"] not in store.render(live):
        fails.append("G1 the text render does not carry the boundary step")
    return fails


def guard_g2(store, page, store_path=STORE) -> list:
    """IT IS STATED WHERE IT IS READ."""
    fails = []
    blk = store.plan().get("voice_boundary")
    if not blk:
        return ["G2 plan() publishes no voice_boundary block -- the page has nothing to render"]
    if not blk.get("statement"):
        fails.append("G2 the plan-level block carries no statement")
    txt = page.read_text(encoding="utf-8", errors="replace")
    if 'id="sw-boundary"' not in txt:
        fails.append("G2 the page has no mount for the boundary block")
    if "function swBoundaryBlock(" not in txt or "function swBoundary(" not in txt:
        fails.append("G2 the page defines no boundary renderer")
    if "\n      swBoundary();" not in txt:
        fails.append("G2 swBoundary() is never CALLED -- a renderer nobody runs is not a surface")
    if "SW.plan && SW.plan.voice_boundary" not in txt:
        fails.append("G2 the page does not read the block from the served plan")
    # * THE MOUNT MUST PRECEDE THE STEP LIST, or "above the walk" is a claim about nothing. This is
    #   the one ordering assertion in the file, and it is done on the SERVED page source.
    if txt.find('id="sw-boundary"') > txt.find('id="sw-steps"'):
        fails.append("G2 the boundary mount comes AFTER the step list in the DOM -- a requirement "
                     "below the fold of a filtered list is not read first")
    return fails


def guard_g3(store, page, store_path=STORE) -> list:
    """QUIET ON THE LIVE, HEALTHY BOX -- AND 'COULD NOT LOOK' IS NOT 'CLEAN'.

    * THE SECOND HALF WAS ADDED BECAUSE THE PROVE-FAIL SAID SO. Mutation V2 (`... else "quiet"`, which
      folds a blind detector into a clean answer) came back GREEN against the first version of this
      guard, because on this box every detector DOES run -- so a mutation that only misbehaves when
      one does not was unreachable, and an unreachable mutation is an unguarded rule. The blind path
      is now DRIVEN, with a detector forced to raise."""
    fails = []
    blk = store.plan().get("voice_boundary") or {}
    if blk.get("state") != "quiet":
        fails.append(f"G3 the boundary measures {blk.get('state')!r} on this box, not 'quiet' -- "
                     f"either something real was found (look at it) or the warning cries wolf. "
                     f"line: {blk.get('line')!r}")
    if blk.get("detected"):
        fails.append(f"G3 detections on a healthy box: {[d.get('who') for d in blk['detected']]}")
    if blk.get("unmeasured"):
        fails.append(f"G3 a detector could not look: {blk['unmeasured']}")

    class _Exploding:
        """A cloud-voice agent store that CANNOT be read. D2 must answer 'could not look'."""
        VOICE_KEY = "OPENAI_API_KEY"

        def key_report(self, *a, **k):
            raise OSError("the option store is unreadable")

        def find_checkout(self, *a, **k):
            raise OSError("the option store is unreadable")

    blind = store.detect_audio_egress(capture_text=MIC_FREE, conn_text=CONN_FIXTURE,
                                      gev=_Exploding())
    if blind["state"] != "unknown":
        fails.append(f"G3 a detector that RAISED produced state {blind['state']!r}, not 'unknown' -- "
                     f"'I could not look' has been folded into 'I looked and it was clean' "
                     f"(unmeasured={blind['unmeasured']})")
    if not blind["unmeasured"]:
        fails.append("G3 a detector that raised left no reason behind -- the operator is told the "
                     "boundary is unmeasured and not WHY")
    if blind["detected"]:
        fails.append("G3 a blind detector reported a detection it could not have made")
    return fails


def guard_g4(store, page, store_path=STORE) -> list:
    """LOUD ON A FIXTURE, BY SET EQUALITY ON WHO WAS NAMED."""
    fails = []
    d = store.detect_audio_egress(capture_text=MIC_HELD, conn_text=CONN_FIXTURE)
    if d["state"] != "warn":
        return [f"G4 the fixture did NOT fire -- state {d['state']!r}, so the loud branch is dead"]
    named = {x["who"] for x in d["detected"]}
    if named != {"Firefox (firefox-esr)"}:
        fails.append(f"G4 the fixture named {sorted(named)}, expected exactly "
                     f"['Firefox (firefox-esr)'] -- SET equality, not 'something appeared'")
    kinds = {x["what"] for x in d["detected"]}
    if kinds != {"holds the microphone right now",
                 "is HOLDING THE MICROPHONE and holds an outbound connection"}:
        fails.append(f"G4 the detectors that fired are {sorted(kinds)} -- expected both D1 and D3")
    # * AND THE PEER MUST BE THE REAL REMOTE. The bug this asserts against: a fixture with a `State`
    #   column made the parser read the LOCAL address as the peer, so D3 "proved" an egress that was
    #   this box talking to itself.
    ev = " ".join(x["evidence"] for x in d["detected"] if "outbound" in x["what"])
    if "155.102.176.82:443" not in ev:
        fails.append(f"G4 D3's evidence does not name the real remote peer: {ev!r}")
    if "203.0.113.44:59426" in ev:
        fails.append("G4 D3's evidence names the LOCAL address as the peer -- the column offset is "
                     "wrong again")
    # a second, differently-named holder, so the guard cannot pass by hard-coding one app name
    d2 = store.detect_audio_egress(capture_text=MIC_HELD_OTHER, conn_text=CONN_FIXTURE)
    n2 = {x["who"] for x in d2["detected"]}
    if d2["state"] != "warn" or n2 != {"some-voice-agent (voice-agent-bin)"}:
        fails.append(f"G4 a differently-named holder produced {d2['state']!r}/{sorted(n2)} -- the "
                     f"detector is keyed to the real capture client, or it is keyed to nothing")
    return fails


def guard_g5(store, page, store_path=STORE) -> list:
    """IT DOES NOT CRY WOLF: outbound connections alone are not a warning."""
    fails = []
    d = store.detect_audio_egress(capture_text=MIC_FREE, conn_text=CONN_FIXTURE)
    if d["state"] != "quiet":
        fails.append(f"G5 {len(CONN_FIXTURE.splitlines()) - 1} outbound connections and NO capture "
                     f"stream produced {d['state']!r} -- a healthy box would be warned about")
    if d["detected"]:
        fails.append(f"G5 detections with no microphone holder: {[x['who'] for x in d['detected']]}")
    # * AND OUR OWN ADDRESS IS NOT A PEER. The fixture's `203.0.113.44:8780` row is this box talking
    #   to its own headscale over its LAN address; counting it would be the same false alarm.
    rows, local, why = store._outbound_peers(CONN_FIXTURE)
    peers = {r["peer"] for r in rows}
    if "203.0.113.44:8780" in peers:
        fails.append("G5 a connection to this box's OWN address was counted as outbound")
    if "127.0.0.1:8912" in peers:
        fails.append("G5 a loopback connection was counted as outbound")
    if peers != {"155.102.176.82:443", "[2606:4700::6812:373]:443"}:
        fails.append(f"G5 the outbound peer set is {sorted(peers)} -- expected exactly the two "
                     f"genuinely remote peers")
    # * AND A BLIND DETECTOR IS NOT A CLEAN ONE.
    blind = store._outbound_peers("some header nobody has ever seen\n")
    if blind[0] is not None:
        fails.append("G5 an unrecognised `ss` header was parsed anyway instead of answering UNKNOWN")
    return fails


def guard_g6(store, page, store_path=STORE) -> list:
    """A WARNING THAT REFUSES WOULD VIOLATE 'IT MAY ASK. IT MAY NOT TAKE.'"""
    fails = []
    live = store.plan()
    # 1. the LIVE row, whatever the detection says, is never a blocking word
    row = next((r for r in live["steps"] if r.get("id") == "voice_boundary"), None)
    if row is None:
        return ["G6 no boundary row in the plan"]
    if row["state"] in ("BLOCKED", "STILL-BLOCKED-LATER"):
        fails.append(f"G6 the boundary row reports {row['state']!r} -- the warning blocks")
    if live.get("next_actionable") == "voice_boundary" and row["state"] == "DONE":
        fails.append("G6 a satisfied boundary is next_actionable -- the walk nags him about a rule "
                     "that is already holding")
    b = row.get("button") or {}
    if not (b.get("label") and b.get("action")) or not str(row.get("typing") or "").strip():
        fails.append("G6 the boundary row is a dead end -- no typed path and no button")
    # 2. a LOUD plan must not become a block either: I8 is the same rule over a plan
    loud_plan = store._lied_boundary(
        live, block={"state": "warn", "detected": [{"who": "X", "what": "y", "evidence": "z"}],
                     "line": "one thing may route audio off it", "unmeasured": []})
    i8 = store._i8_fails(loud_plan, store.STEPS)
    if i8:
        fails.append(f"G6 I8 rejects a legitimate LOUD plan: {i8}")
    # 3. and the page must SAY it is not a block
    txt = page.read_text(encoding="utf-8", errors="replace")
    if "NOT A BLOCK" not in txt:
        fails.append("G6 the page never tells him he may proceed -- a warning that does not say so "
                     "reads as a refusal he cannot argue with")
    return fails


def guard_g7(store, page, store_path=STORE) -> list:
    """THE LIMITS OF THE WARNING ARE SERVED, NOT HIDDEN."""
    fails = []
    cd = list(store.VOICE_BOUNDARY.get("cannot_detect") or ())
    if len(cd) < 5:
        fails.append(f"G7 only {len(cd)} limit(s) disclosed -- this warning has real blind spots "
                     f"and they are part of the disclosure")
    blk = store.plan().get("voice_boundary") or {}
    if not blk.get("cannot_detect"):
        fails.append("G7 the limits did not survive the trip into the served plan")
    txt = page.read_text(encoding="utf-8", errors="replace")
    if "b.cannot_detect" not in txt:
        fails.append("G7 the page does not render `cannot_detect` -- the limits are in the JSON and "
                     "nowhere the operator reads")
    if "b.checked" not in txt:
        fails.append("G7 the page does not render what was actually measured")
    d = store.detect_audio_egress(capture_text=MIC_FREE, conn_text=CONN_FIXTURE)
    if not d["checked"]:
        fails.append("G7 the detection reports no checks at all")
    return fails


def guard_g8(store, page, store_path=STORE) -> list:
    """THE BOUNDARY PROBES DO NOT REACH OUT. AST, not grep -- a grep once matched its own constant."""
    fails = []
    tree = ast.parse(pathlib.Path(store_path).read_text(encoding="utf-8"))
    want = {"_mic_holders", "_outbound_peers", "_same_app", "detect_audio_egress",
            "probe_voice_boundary", "voice_boundary_block"}
    found = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = want - found
    if missing:
        return [f"G8 the boundary functions named in this guard no longer exist: {sorted(missing)} "
                f"-- the guard would be checking nothing"]
    banned = {"urlopen", "urlretrieve", "socket", "requests", "httpx", "urlsplit", "getaddrinfo",
              "create_connection", "Popen", "system", "call", "check_output", "run"}
    argv0 = set()
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name not in want:
            continue
        for c in ast.walk(n):
            if isinstance(c, ast.Call):
                fn = c.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name in banned:
                    fails.append(f"G8 {n.name} calls {name!r} -- the boundary probes must not reach "
                                 f"out, and must not shell out beyond `_run`")
                if name == "_run" and c.args and isinstance(c.args[0], ast.List) and c.args[0].elts:
                    a0 = c.args[0].elts[0]
                    if isinstance(a0, ast.Constant):
                        argv0.add(str(a0.value))
    if argv0 - {"pactl", "ss"}:
        fails.append(f"G8 the boundary code runs {sorted(argv0 - {'pactl', 'ss'})} -- only the two "
                     f"read-only local commands are declared")
    if not {"pactl", "ss"} <= argv0:
        fails.append(f"G8 the declared commands are {sorted(argv0)}; the detector claims to run "
                     f"`pactl` and `ss` and does not name at least one of them")
    return fails


def guard_g9(store, page, store_path=STORE) -> list:
    """THE GEV CARD CARRIES THE BOUNDARY, AND KEEPS THE LATENT PRECISION."""
    fails = []
    import gev_option as GEV_MOD
    t = GEV_MOD.TERMS
    if not str(t.get("voice") or "").strip():
        fails.append("G9 the GEV card dropped its voice disclosure")
    vb = str(t.get("voice_boundary") or "")
    if not vb.strip():
        fails.append("G9 the GEV card carries no positive half -- he is told what GEV WOULD do and "
                     "never what he would actually give it")
    if "TEXT" not in vb.upper():
        fails.append("G9 the positive half does not say TEXT")
    if "microphone" not in vb.lower():
        fails.append("G9 the positive half does not name the microphone it replaces")
    # * THE LATENT/ARMED WORD IS DERIVED FROM THE SAME MEASUREMENT AS THE KEY, AND IT IS ON THE ROW.
    st, ev = GEV_MOD.probe()
    keyed = GEV_MOD.key_report().get(GEV_MOD.VOICE_KEY, "absent") != "absent"
    want = "ARMED" if keyed else "LATENT"
    if f"voice half: {want}" not in ev:
        fails.append(f"G9 the GEV evidence does not carry 'voice half: {want}' -- the row says "
                     f"neither that it would nor that it could: {ev[:160]!r}")
    # and the GEV row in the served walk must still carry the term (I7 enforces the same set)
    live = store.plan()
    g = next((r for r in live["steps"] if r.get("id") == "gev_option"), None)
    if not g or not (g.get("terms") or {}).get("voice_boundary"):
        fails.append("G9 the GEV row in the served walk dropped the positive half in transit")
    txt = page.read_text(encoding="utf-8", errors="replace")
    if "vb.gevs_own_voice" not in txt:
        fails.append("G9 the page's GEV card does not render the boundary's own words")
    return fails


def guard_g10(store, page, store_path=STORE) -> list:
    """THE WARNING CANNOT BE FILTERED AWAY -- and the rule is DRIVEN, not read."""
    fails = []
    txt = page.read_text(encoding="utf-8", errors="replace")
    if "r.voice_warning" not in txt:
        fails.append("G10 the default filter does not exempt a row carrying a warning -- on a "
                     "healthy box the boundary measures DONE and would be HIDDEN in the very view a "
                     "first-install user sees")
    # * DRIVEN IN `node`, AGAINST THE SHAPE THE ROUTE SERVES. The rule below is the page's own
    #   expression, transcribed; the rows are the live plan's. If `node` is absent this is UNKNOWN and
    #   says so -- it is never folded into a pass.
    js = ("var rows=%s;"
          "var open=rows.filter(function(r){return (r.state!=='DONE'&&r.state!=='DECLINED')"
          "||!!r.voice_warning;});"
          "console.log(JSON.stringify(open.map(function(r){return r.id;})));")
    live = store.plan()
    rows = [{"id": r["id"], "state": r["state"], "voice_warning": r.get("voice_warning")}
            for r in live["steps"]]
    try:
        p = subprocess.run(["node", "-e", js % json.dumps(rows)], capture_output=True, text=True,
                           timeout=20)
        if p.returncode != 0:
            fails.append(f"G10 could not drive the filter rule: {p.stderr.strip()[:120]}")
        else:
            kept = set(json.loads(p.stdout or "[]"))
            quiet_kept = {"voice_boundary"} & kept
            if live.get("voice_boundary", {}).get("state") == "quiet" and quiet_kept:
                fails.append("G10 a QUIET boundary row is shown in 'what is not done' -- on a "
                             "healthy box the row should be hidden, or the filter is not filtering")
    except FileNotFoundError:
        fails.append("G10 NOT RUN: `node` is not on PATH, so the filter rule could not be driven "
                     "-- this is UNKNOWN, not a pass")
    return fails


GUARDS = (("G1", guard_g1), ("G2", guard_g2), ("G3", guard_g3), ("G4", guard_g4), ("G5", guard_g5),
          ("G6", guard_g6), ("G7", guard_g7), ("G8", guard_g8), ("G9", guard_g9), ("G10", guard_g10))


def run_guards(verbose: bool = True, scratch: pathlib.Path | None = None) -> list:
    """Every guard, over ONE measurement of the box.

    * THE PLAN IS MEASURED ONCE AND MEMOISED, AND THAT IS NOT ONLY SPEED. `plan()` runs fifteen live
      probes -- one of which (the secrets keeper's driven value path) took 14.65 s when this was
      written -- so six guards each calling it separately is both slow and six DIFFERENT measurements
      of a box that moves underneath them. One measurement, many assertions.
    * `scratch` POINTS THE WHOLE SUITE AT A MUTATED COPY, so no guard and no mutation can read or
      write the working tree. sys.path and sys.modules are restored afterwards."""
    import importlib
    if scratch:
        sys.path.insert(0, str(scratch))
        for m in ("setup_blockers", "gev_option"):
            sys.modules.pop(m, None)
    imported = []
    try:
        import setup_blockers as store
        import gev_option as _gev
        imported = [store, _gev]
        importlib.reload(_gev)
        importlib.reload(store)
        page = (scratch / PAGE.name) if scratch else PAGE
        store_path = (scratch / STORE.name) if scratch else STORE
        real_plan = store.plan
        memo = {}

        def _memo(*a, **k):
            if "p" not in memo:
                memo["p"] = real_plan(*a, **k)
            return memo["p"]

        store.plan = _memo
        fails = []
        try:
            for name, fn in GUARDS:
                try:
                    f = fn(store, page, store_path)
                except Exception as e:  # noqa: BLE001 -- a guard that dies is a failure, not a pass
                    f = [f"{name} raised {type(e).__name__}: {e}"]
                if verbose:
                    print(f"  {'x' if f else 'v'} {name}: {'FAIL' if f else 'ok'}")
                fails.extend(f)
        finally:
            store.plan = real_plan
        return fails
    finally:
        if scratch:
            sys.path.remove(str(scratch))
            for m in ("setup_blockers", "gev_option"):
                sys.modules.pop(m, None)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  PROVE-FAIL -- ON COPIES. THE WORKING TREE IS HASHED BEFORE AND AFTER AND MUST NOT MOVE.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


MUTATIONS = (
    ("V1 the detector always warns (cry wolf)",
     "setup_blockers.py",
     'state = "warn" if detected else ("unknown" if unmeasured else "quiet")',
     'state = "warn"',
     "G3"),
    ("V2 a blind detector is folded into 'clean'",
     "setup_blockers.py",
     'state = "warn" if detected else ("unknown" if unmeasured else "quiet")',
     'state = "warn" if detected else "quiet"',
     "G3"),
    ("V3 the requirement is dropped from the served plan",
     "setup_blockers.py",
     '"voice_boundary": voice_boundary_block(rows),',
     '"_voice_boundary_dropped": voice_boundary_block(rows),',
     "G2"),
    ("V4 the page stops rendering the boundary",
     "setup-wizard-popout.html",
     "      swBoundary();\n      swRender();",
     "      /* swBoundary(); */\n      swRender();",
     "G2"),
    ("V5 the default filter may hide a warning",
     "setup-wizard-popout.html",
     "|| !!r.voice_warning;",
     ";",
     "G10"),
    ("V6 the GEV card loses the positive half",
     "gev_option.py",
     '"voice_boundary": (',
     '"_voice_boundary_disabled": (',
     "G9"),
)


def prove_fail() -> int:
    print("=== PROVE-FAIL -- mutate COPIES, require the named guard RED, working tree must not move ===")
    before = {p: _md5(p) for p in COPYABLE}
    bad = 0
    for name, fname, old, new, expect in MUTATIONS:
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="khaoss_vboundary_pf_"))
        try:
            for src in COPYABLE:
                shutil.copy2(src, scratch / src.name)
            target = scratch / fname
            src_text = target.read_text(encoding="utf-8")
            if src_text.count(old) != 1:
                # * A MUTATION THAT DID NOT APPLY PROVES NOTHING, and counting it as a pass is how a
                #   guard quietly stops being tested. It is a FAILURE here, not a skip.
                print(f"  SKIP  {name}: anchor appears {src_text.count(old)}x in {fname} -- mutation "
                      f"NOT applied, so this mutation proves NOTHING")
                bad += 1
                continue
            target.write_text(src_text.replace(old, new), encoding="utf-8")
            fails = run_guards(verbose=False, scratch=scratch)
            red = [f for f in fails if f.startswith(expect)]
            got = "RED" if red else "GREEN"
            print(f"  {got:<5} {name}  -> expected {expect} red: {'yes' if red else 'NO'}")
            if not red:
                print(f"        failures seen: {fails[:3] or 'none at all'}")
                bad += 1
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    moved = [p for p in COPYABLE if _md5(p) != before[p]]
    mine = []
    for p in moved:
        txt = p.read_text(encoding="utf-8", errors="replace")
        # * A MUTATION SIGNATURE IS THE ONLY HONEST WAY TO TELL THE TWO APART. Five lanes are live in
        #   this repo; `operator/setup_blockers.py` is owned by another one and it wrote to it DURING
        #   this run (measured: 18:25:37, its `probe_value_substitution` rewrite). A concurrent write
        #   is not this suite's damage, and reporting it as such would be a false alarm that gets a
        #   real assertion switched off. But a file that CONTAINS MY MUTATED TEXT is mine, and fails.
        sigs = [new for _n, fname, _o, new, _g in MUTATIONS if fname == p.name]
        hits = [s for s in sigs if s in txt]
        if hits:
            print(f"  ** THE WORKING TREE CARRIES MY MUTATION: {p} :: {hits!r}")
            mine.append(str(p))
        else:
            print(f"  !! CONCURRENT WRITE by another lane while this ran: {p.name} "
                  f"({before[p][:8]} -> {_md5(p)[:8]}) -- reported, not counted against this suite, "
                  f"and NOT one of my mutations (no mutation signature present)")
    if mine:
        bad += 1
    elif not moved:
        print("  -- working tree byte-identical after every mutation (3 files hashed before/after)")

    print()
    print(f"  {'INVALID' if bad else 'VALID'} -- {len(MUTATIONS) - bad}/{len(MUTATIONS)} mutations "
          f"reddened their own guard without touching a file you care about")
    return 1 if bad else 0


def main() -> int:
    a = sys.argv[1:]
    if "--prove-fail" in a:
        return prove_fail()
    if "--self-test" in a or not a:
        fails = run_guards()
        for f in fails:
            print(f"  x {f}")
        n_bad = len({f[:2] for f in fails})
        print(f"\n  {'FAIL' if fails else 'PASS'} -- test_voice_boundary: "
              f"{len(GUARDS) - n_bad}/{len(GUARDS)} guards green")
        return 1 if fails else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
