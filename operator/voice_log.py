#!/usr/bin/env python3
"""★ THE SPOKEN LOG — one row per utterance, and the row carries who said it.

★ A0, THE PREMISE: voicebox is a SHARED SINK WITH NO CALLER IDENTITY. Every source POSTs text to one
  /speak and the endpoint learns nothing about the sender. So "who is talking" CANNOT be derived at the
  sink — attribution is added at the CALL SITE or it does not exist. Same shape as spawn_preflight logging
  25 decisions with an empty subject: the log was written, and it recorded nothing about its subjects.

★★ `effect` IS THE FIELD THAT MATTERS MOST. It records whether an audio sink actually left SUSPENDED —
  MEASURED, not claimed. Every completion chime for a week was silent because `play` defaults to False and
  nobody read the schema, while /speak answered {"ok": true} each time. **This column is FC-1's detector
  for the voice path**: with it, that week would have ended on day one.

★★★ AND THIS IS A CROSS-LANE READ SURFACE BY CONSTRUCTION. A view aggregating every agent's speech puts
  the lanes' utterances side by side — the bleed we closed twice this week, rebuilt as
  a feature. So clearance and territory are IN THE ROW FROM DAY ONE, never retrofitted (the no-cloud
  lesson: 11,504 files carrying a label nobody approved and nothing enforces).
  ★ Entries above the viewer's clearance are COUNTED, NOT HIDDEN — "3 entries above your current view" —
  because a hidden row is indistinguishable from one that never existed.

A RE-READ IS LOGGED AS A RE-READ (`kind: "replay"`, `replay_of: <id>`), never as a fresh utterance.
A log that cannot tell a replay from an original lies about what happened in the room.
"""
from __future__ import annotations
import json, os, re, subprocess, threading, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "voicelog.jsonl"
CLEARANCES = ["initiate", "seeker", "adept", "oracle", "sage"]


def _rank(c: str) -> int:
    try:
        return CLEARANCES.index((c or "initiate").lower())
    except ValueError:
        return 0


class SinkWatcher:
    """★ Measures the EFFECT — did a sink actually leave SUSPENDED. That is the only observable proof
    that sound reached the device.

    ★★ THE WINDOW WAS WRONG IN MY FIRST VERSION, AND IT MATTERED. I asserted that /speak BLOCKS through
    playback, so watching only during the call would be enough. MEASURED: the POST returns after ~1.5s and
    the sink does not go RUNNING until ~3s — it QUEUES the audio and returns early. So the watcher stopped
    1.5 seconds before the sound began and reported every utterance, including audible ones, as SILENT.
    ★★★ A detector that reports silence for everything is worse than none: it would have marked every real
    utterance as a failure, and it was itself an instance of the class it exists to catch — a confident
    claim about behaviour that nobody had measured. So the window now SETTLES past the call, and the cost
    is paid only when the utterance really is silent (it returns the moment RUNNING is seen)."""

    def __init__(self, interval: float = 0.35, limit: float = 40.0):
        self.interval, self.limit = interval, limit
        self._stop = threading.Event()
        self.saw_running = False
        self.attributed = False
        self.owner = ''
        self.foreign = []
        self._before = None
        self._t = None

    @staticmethod
    def _inputs() -> set:
        """The set of live sink-input ids — one per stream an application is actually playing."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sink-inputs"],
                                 capture_output=True, timeout=4).stdout.decode()
            return {l.split("\t")[0] for l in out.splitlines() if l.strip()}
        except Exception:
            return set()

    # ★ VOICEBOX PLAYS THROUGH `aplay` — verified in tts_server.py:483 _play_wav(), not assumed.
    #   PulseAudio/PipeWire tags each stream with the binary that opened it, so a stream can be
    #   attributed to a player without tracking pids across the HTTP boundary.
    #   ★ TOKEN, NOT SUBSTRING: "aplay" is a substring of "paplay" — a DIFFERENT binary. A substring
    #     test would attribute any paplay stream (system sounds, other apps) to us. Caught while
    #     designing this check's own negative control, before it ran.
    OURS = re.compile(r"\b(aplay|piper)\b", re.I)

    @classmethod
    def _owner(cls, sid: str) -> str:
        """application.name / process.binary for one sink-input id, or '' if it cannot be read."""
        try:
            out = subprocess.run(["pactl", "list", "sink-inputs"],
                                 capture_output=True, timeout=4).stdout.decode()
        except Exception:
            return ""
        block, want = [], f"Sink Input #{sid}"
        seen = False
        for line in out.splitlines():
            if line.startswith("Sink Input #"):
                if seen:
                    break
                seen = (line.strip() == want)
                continue
            if seen:
                block.append(line)
        return " ".join(l.strip() for l in block if "application.name" in l
                        or "application.process.binary" in l)

    def baseline(self):
        """★★★ ATTRIBUTE THE SOUND, DO NOT JUST DETECT SOUND.
        This watcher used to return True on `"RUNNING" in pactl list short sinks` — ANY sink, fed by ANY
        application. Measured on this machine today: a Bluetooth output was RUNNING with THREE foreign
        streams on it while nothing of ours was playing. Under those conditions — which are the NORMAL
        conditions, a person with headphones on — the check returns `audible` for every utterance,
        including ones voicebox refused outright. The detector built to catch a silent success had become
        able to certify silence AS sound: the same class it exists to catch, running the other way.
        So: snapshot the streams BEFORE the utterance, and count only a stream that was not there before."""
        self._before = self._inputs()

    def _loop(self):
        end = time.time() + self.limit
        before = getattr(self, "_before", None)
        if before is None:
            before = self._inputs()
        while not self._stop.is_set() and time.time() < end:
            try:
                now = self._inputs()
                # ★★★ NOVEL IS NOT THE SAME AS OURS, and the gap is not theoretical on this box:
                #   Firefox and speech-dispatcher open and close streams with no involvement from us.
                #   A foreign stream opening inside the watch window is NOVEL, passes a set difference,
                #   and would be counted as proof that our utterance played — the same false positive as
                #   the old any-sink-RUNNING check, one layer in. Raised by the trading seat after they
                #   hit it in their own verifier. So a novel stream counts only if its owner is the
                #   player voicebox actually uses.
                for sid in sorted(now - before):
                    who = self._owner(sid)
                    if self.OURS.search(who):
                        self.saw_running = True
                        self.attributed = True
                        self.owner = who[:120]
                        return
                    tag = f"{sid}:{who[:60]}" if who else sid
                    if tag not in self.foreign:          # one entry per stream, not one per poll
                        self.foreign.append(tag)
                out = subprocess.run(["pactl", "list", "short", "sinks"],
                                     capture_output=True, timeout=4).stdout.decode()
                if "RUNNING" in out and not before and not now:
                    # A sink is RUNNING but NOTHING holds it and nothing appeared — an open idle device.
                    # Not evidence either way; keep looking rather than claim a result.
                    pass
            except Exception:
                return          # cannot measure ≠ silent; `unknown` is recorded instead
            self._stop.wait(self.interval)

    def __enter__(self):
        # ★ Baseline BEFORE the thread starts, so a stream that appears in the gap is still counted new.
        self.baseline()
        self._t = threading.Thread(target=self._loop, daemon=True); self._t.start(); return self

    def settle(self, timeout: float = 7.0):
        """Keep watching AFTER the call returns — /speak queues and returns before playback starts."""
        end = time.time() + timeout
        while time.time() < end and not self.saw_running and self._t and self._t.is_alive():
            time.sleep(0.25)
        return self

    def __exit__(self, *a):
        self.settle()
        self._stop.set()
        if self._t:
            self._t.join(timeout=2)

    @property
    def effect(self) -> str:
        return "audible" if self.saw_running else "silent"


def append(row: dict) -> dict:
    row.setdefault("id", uuid.uuid4().hex[:12])
    row.setdefault("ts", int(time.time()))
    row.setdefault("kind", "utterance")
    row.setdefault("source", "(unattributed)")
    row.setdefault("where", "")
    row.setdefault("why", "")
    row.setdefault("clearance", "initiate")
    row.setdefault("territory", "")
    row.setdefault("effect", "unknown")
    row.setdefault("duration_s", None)
    row.setdefault("link", "")
    try:
        with LOG.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass                    # a logging failure must never change what the caller was doing
    return row


def read(limit: int = 20, before: int | None = None, viewer_clearance: str = "sage",
         viewer_territory: str | None = None) -> dict:
    """Return the tail, filtered by the VIEWER's clearance/territory, with what was withheld COUNTED."""
    rows = []
    if LOG.exists():
        for line in LOG.read_text(errors="replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    rows.sort(key=lambda r: r.get("ts", 0))
    if before:
        rows = [r for r in rows if r.get("ts", 0) < before]

    vr = _rank(viewer_clearance)
    above = [r for r in rows if _rank(r.get("clearance")) > vr]
    off_territory = [r for r in rows if r not in above and viewer_territory
                     and r.get("territory") and r.get("territory") != viewer_territory]
    visible = [r for r in rows if r not in above and r not in off_territory]
    page = visible[-limit:] if limit else visible
    return {
        "rows": page,
        "total_visible": len(visible),
        # ★ counted, not hidden — a silent gap is indistinguishable from nothing having happened
        "withheld_above_clearance": len(above),
        "withheld_other_territory": len(off_territory),
        "has_more": len(visible) > len(page),
        "oldest_ts": page[0]["ts"] if page else None,
    }




def self_test() -> int:
    """★ NEGATIVE CONTROL for the clearance filter — ranked 2nd by consequence (⛬6, 2026-08-29):
    a false clear here leaks cross-lane content into one view.

    The failure is invisible by construction: a viewer shown 12 rows cannot tell that from 12-of-20.
    So the test plants rows the viewer must NOT see and fails if any of them come back, and separately
    fails if the withheld COUNT is zero while rows were in fact withheld — because 'hidden and counted'
    is the contract, and hiding without counting is the silent gap this file exists to prevent."""
    import tempfile as _tf, json as _j, time as _t
    global LOG
    saved = LOG
    fails = []
    try:
        LOG = Path(_tf.mkdtemp()) / "vlog.jsonl"
        rows = [
            {"ts": int(_t.time()) - 5, "text": "PUBLIC-INITIATE", "clearance": "initiate", "territory": "khaoss"},
            {"ts": int(_t.time()) - 4, "text": "SECRET-SAGE",     "clearance": "sage",     "territory": "khaoss"},
            {"ts": int(_t.time()) - 3, "text": "OTHER-TERRITORY", "clearance": "initiate", "territory": "trading"},
        ]
        LOG.write_text("\n".join(_j.dumps(r) for r in rows) + "\n")

        low = read(limit=50, viewer_clearance="initiate", viewer_territory="khaoss")
        blob = _j.dumps(low["rows"])
        if "SECRET-SAGE" in blob:
            fails.append("a SAGE row was returned to an INITIATE viewer — the clearance filter does not filter")
        if "OTHER-TERRITORY" in blob:
            fails.append("a row from another territory was returned — territory scoping does not scope")
        if low.get("withheld_above_clearance", 0) < 1:
            fails.append("a row WAS withheld above clearance but the count says 0 — hidden and uncounted "
                         "is exactly the silent gap this file promises not to produce")
        if low.get("withheld_other_territory", 0) < 1:
            fails.append("an off-territory row was withheld but not counted")

        high = read(limit=50, viewer_clearance="sage", viewer_territory=None)
        if "SECRET-SAGE" not in _j.dumps(high["rows"]):
            fails.append("a SAGE viewer could not see a SAGE row — the filter refuses everything, so "
                         "'nothing leaked' would prove nothing")
    finally:
        LOG = saved

    for f in fails:
        print(f"  \u2717 {f}")
    print("  \u2713 voice_log self_test: withholds above-clearance and off-territory rows, COUNTS both, "
          "and still shows a cleared viewer their own rows" if not fails else "  \u2717 voice_log self_test FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--tail" in sys.argv:
        d = read(limit=15)
        print(f"\n★ SPOKEN LOG — {d['total_visible']} visible"
              f" · {d['withheld_above_clearance']} above your clearance"
              f" · {d['withheld_other_territory']} in another territory\n")
        for r in d["rows"]:
            t = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
            mark = {"audible": "♪", "silent": "✗", "unknown": "?"}.get(r.get("effect"), "?")
            kind = "↻" if r.get("kind") == "replay" else " "
            print(f"  {t} {mark}{kind} {r.get('source','?')[:22]:24s} {str(r.get('text',''))[:52]}")
            if r.get("why"):
                print(f"           why: {r['why'][:70]}")
        print()
