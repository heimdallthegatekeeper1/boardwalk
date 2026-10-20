#!/usr/bin/env python3
"""voice_in.py — the other half of the voice. Push to talk, and the words land where you are typing.

★★★ NOTHING WAS BOUGHT AND NOTHING WAS INSTALLED. Both halves were already on this machine and the
    only missing piece was the wire between them:
      · whisper.cpp v1.8.2 at ~/whisper.cpp/build/bin/whisper-cli, models tiny / base / base.en,
        measured at 174 MB and 282 MB peak against a ~1,776 MB budget.
      · CDP `Input.insertText` — the app's OWN debug protocol. No portal, no consent dialog, no
        uinput, no root, no keystroke synthesis at all.

★★ WHY NOT THE OBVIOUS TOOLS.
    · `wtype` and everything built on it is DEAD here: mutter does not implement
      virtual-keyboard-unstable-v1 (measured: zero occurrences in libmutter) and the maintainers
      refused the protocol in May 2025. Most "Wayland dictation" tools mean exactly this.
    · `xdotool` is not blocked, it is GATED: mutter launches Xwayland with -enable-ei-portal, so XTEST
      becomes an org.freedesktop.portal.RemoteDesktop consent request. It fires and dies unanswered.
    · `ydotool` would work and would not even need root here (a Steam udev rule left a uinput ACL) —
      and it is refused anyway: layout-blind, focus-blind, needs a resident daemon, and it defeats
      every consent mechanism GNOME has. A dictation tool should not be a keylogger in reverse.

★ THE FAILURE THIS IS BUILT AGAINST: a transcription of silence inserted as text. That is the
  empty-success class with a keyboard — the same shape as the delivery check that printed "0 CRITICAL"
  while looking at nothing. So silence must produce NOTHING and SAY SO, and that is the prove-fail.
"""
from __future__ import annotations
import json, os, subprocess, sys, time, wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
WHISPER = Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli"
MODELS = Path.home() / ".local" / "share" / "whisper"
LOG = HERE / "voiceinlog.jsonl"
CDP_TARGETS = {"aionui": 9230, "panel": 9222}

# ★★★ MATCH THE SHAPE, NOT A LIST OF LITERALS — and a live capture is what taught this.
#   The first version held a list: [BLANK_AUDIO], [SILENCE], [MUSIC], [NOISE]... A real 3-second
#   capture of a quiet room returned "[MUSIC PLAYING]", which contains none of them as a substring
#   ("[MUSIC]" has a closing bracket "[MUSIC PLAYING]" does not share), so nothing fired and that
#   string would have been TYPED INTO HIS COMPOSER. The synthetic prove-fail passed because digital
#   silence tripped the loudness guard instead — the tag path was never exercised with a real tag.
#   ★ whisper emits every non-speech annotation the same way: the WHOLE transcript wrapped in
#     brackets or parentheses. That is a shape, and a shape does not need maintaining.
import re as _re
NON_SPEECH_SHAPE = _re.compile(r"^\s*(?:[\[(<][^\])>]*[\])>]\s*)+$")
NON_SPEECH = ("[BLANK_AUDIO]", "[SILENCE]", "(silence)", "[INAUDIBLE]")   # kept as belt and braces


def is_non_speech(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if NON_SPEECH_SHAPE.match(t):        # the whole utterance is one or more bracketed tags
        return True
    return any(x.lower() in t.lower() for x in NON_SPEECH)


def _ss():
    import importlib
    sys.path.insert(0, str(HERE))
    return importlib.import_module("safe_store")


def available() -> dict:
    """What this machine can actually do — the UI is built from this, so an option never appears
    unless the thing behind it exists."""
    models = sorted(p.name for p in MODELS.glob("ggml-*.bin")) if MODELS.is_dir() else []
    try:
        srcs = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                              text=True, timeout=10).stdout.splitlines()
    except Exception:
        srcs = []
    # ★ a .monitor source is loopback of the SPEAKERS, not a microphone. Offering one as an input
    #   would transcribe whatever is playing — which is not dictation, it is eavesdropping on itself.
    mics = [l.split("\t")[1] for l in srcs if "\t" in l and ".monitor" not in l.split("\t")[1]]
    return {"whisper": WHISPER.exists(), "whisper_path": str(WHISPER),
            "models": models, "microphones": mics,
            "monitors_excluded": len([l for l in srcs if ".monitor" in l]),
            "insert_targets": CDP_TARGETS,
            "ready": bool(WHISPER.exists() and models and mics),
            "note": ("A `.monitor` source is speaker loopback, not a microphone — excluded, because "
                     "dictating from one would transcribe whatever is playing.")}


def record(seconds: float = 5.0, device: str = "") -> dict:
    out = Path("/tmp") / f"khaoss_voicein_{os.getpid()}.wav"
    cmd = ["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1",
           "-d", str(max(1, int(seconds))), str(out)]
    if device:
        cmd[1:1] = ["-D", device]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 20)
    if r.returncode != 0 or not out.exists():
        return {"ok": False, "error": f"arecord failed: {r.stderr[:160]}"}
    return {"ok": True, "wav": str(out), "bytes": out.stat().st_size}


def _rms(path: str) -> float:
    """Loudness of the capture, BEFORE transcription. ★ This is the honest silence check: whisper
    will happily hallucinate a caption for a silent file, so the audio itself has to be measured."""
    try:
        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
        if not frames:
            return 0.0
        import array, math
        a = array.array("h")
        a.frombytes(frames[: len(frames) // 2 * 2])
        if not len(a):
            return 0.0
        return math.sqrt(sum(float(x) * x for x in a) / len(a))
    except Exception:
        return -1.0


SILENCE_RMS = 120.0     # measured floor; below this the mic is muted or the room is empty


def transcribe(wav: str, model: str = "ggml-base.en.bin") -> dict:
    m = MODELS / model
    if not WHISPER.exists():
        return {"ok": False, "error": f"whisper-cli not at {WHISPER}"}
    if not m.exists():
        return {"ok": False, "error": f"model {model} not in {MODELS}"}
    loud = _rms(wav)
    t0 = time.time()
    r = subprocess.run([str(WHISPER), "-m", str(m), "-f", wav, "-nt", "-np"],
                       capture_output=True, text=True, timeout=300)
    took = round(time.time() - t0, 2)
    if r.returncode != 0:
        return {"ok": False, "error": f"whisper failed: {r.stderr[-200:]}", "seconds": took}
    text = " ".join(r.stdout.split()).strip()
    # ★★ THE GUARD. Two independent reasons to refuse: the audio was silent, or the transcript is a
    #    non-speech tag. Either one means there is nothing to insert, and inserting anything would be
    #    the empty-success failure with a keyboard.
    silent = loud >= 0 and loud < SILENCE_RMS
    tagged = is_non_speech(text)
    return {"ok": True, "text": "" if (silent or tagged) else text,
            "raw": text, "seconds": took, "rms": round(loud, 1), "model": model,
            "refused": bool(silent or tagged),
            "why": ("the microphone was silent (rms %.0f, floor %.0f) — nothing was inserted"
                    % (loud, SILENCE_RMS) if silent else
                    ("whisper returned a non-speech annotation (%r), not words — nothing "
                     "was inserted" % text
                     if tagged else ""))}


def insert(text: str, target: str = "aionui") -> dict:
    """Put the words where he is typing, using the app's own debug protocol."""
    if not text.strip():
        return {"ok": False, "refused": True,
                "why": "nothing to insert — refusing rather than sending an empty keystroke"}
    port = CDP_TARGETS.get(target)
    if not port:
        return {"ok": False, "error": f"unknown target {target!r}; choose from {list(CDP_TARGETS)}"}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pg = b.contexts[0].pages[0]
            before = pg.evaluate("()=>((document.activeElement||{}).value||'')")
            s = pg.context.new_cdp_session(pg)
            s.send("Input.insertText", {"text": text})
            after = pg.evaluate("()=>((document.activeElement||{}).value||'')")
            landed = text in after and after != before
            return {"ok": landed, "target": target, "landed": landed,
                    "focused": pg.evaluate("()=>(document.activeElement||{}).tagName"),
                    "why": "" if landed else
                           ("the text was sent but did not appear — nothing had keyboard focus, or "
                            "the focused element does not take text")}
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:160]}"}


def dictate(seconds: float = 5.0, target: str = "aionui", model: str = "ggml-base.en.bin",
            device: str = "", insert_it: bool = True) -> dict:
    """★ Push-to-talk, end to end, with the voice-OUT envelope in reverse: every transcription
    recorded with its source, where it went, and its EFFECT — the column that catches a silent
    success."""
    env = {"kind": "transcription", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "source": "microphone", "where": f"cdp:{target}", "model": model,
           "seconds_requested": seconds}
    rec = record(seconds, device)
    if not rec.get("ok"):
        env.update(effect="NOTHING — capture failed", error=rec.get("error"))
        _log(env); return env
    tr = transcribe(rec["wav"], model)
    try:
        os.unlink(rec["wav"])
    except Exception:
        pass
    if not tr.get("ok"):
        env.update(effect="NOTHING — transcription failed", error=tr.get("error"))
        _log(env); return env
    env.update(rms=tr["rms"], transcribe_seconds=tr["seconds"], raw=tr["raw"])
    if tr["refused"]:
        env.update(text="", effect="NOTHING INSERTED — refused", why=tr["why"])
        _log(env); return env
    env["text"] = tr["text"]
    if not insert_it:
        env.update(effect="transcribed only, not inserted (asked not to)")
        _log(env); return env
    ins = insert(tr["text"], target)
    env.update(effect=("inserted into " + target) if ins.get("landed") else
               "NOTHING INSERTED — " + str(ins.get("why") or ins.get("error")),
               landed=bool(ins.get("landed")), focused=ins.get("focused"))
    _log(env)
    return env


def _log(env: dict) -> None:
    try:
        with LOG.open("a") as f:
            f.write(json.dumps(env) + "\n")
    except Exception:
        pass


def self_test() -> int:
    """★★ THE PROVE-FAIL THE DISPATCH ASKED FOR: silence must produce NOTHING and say so."""
    import tempfile, struct
    fails = []
    a = available()
    if a["whisper"] != WHISPER.exists():
        fails.append("availability disagrees with the filesystem")

    # a genuinely silent wav — the exact input that must not become text
    d = Path(tempfile.mkdtemp()); wav = d / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))
    if _rms(str(wav)) >= SILENCE_RMS:
        fails.append("a digitally silent file measured as loud")
    if a["whisper"] and a["models"]:
        t = transcribe(str(wav), a["models"][0] if a["models"] else "ggml-base.en.bin")
        if t.get("ok") and not t.get("refused"):
            fails.append(f"SILENCE WAS TRANSCRIBED AS TEXT {t.get('text')!r} — the empty-success "
                         f"failure with a keyboard, which is the whole thing this guards")
        if t.get("ok") and not t.get("why"):
            fails.append("silence was refused but gave no reason — a refusal must say why")
    else:
        print("  (whisper or models absent — the silence transcription leg could not run)")

    # ★ THE CASE A LIVE CAPTURE FOUND, now pinned so it cannot come back. Every one of these is a
    #   real whisper non-speech annotation and none is in the literal list.
    for tag in ("[MUSIC PLAYING]", "[BIRDS CHIRPING]", "(drill whirring)", "[ Silence ]",
                "[SOUND]", "[typing]", "(wind blowing)", "[BLANK_AUDIO]", "  ", ""):
        if not is_non_speech(tag):
            fails.append(f"a non-speech annotation {tag!r} would have been INSERTED AS TEXT")
    # and real speech must still get through — a guard that refuses everything is not a guard
    for real in ("open the operator board", "close item twenty eight", "yes"):
        if is_non_speech(real):
            fails.append(f"real speech {real!r} was refused as non-speech — the guard is too greedy")

    # insert must refuse empty text rather than sending an empty keystroke
    r = insert("   ")
    if not r.get("refused"):
        fails.append("insert() accepted empty text")
    r2 = insert("x", "nonexistent-target")
    if r2.get("ok"):
        fails.append("insert() accepted an unknown target")
    # a monitor source must never be offered as a microphone
    if any(".monitor" in m for m in a["microphones"]):
        fails.append("a .monitor loopback source was offered as a microphone")
    import shutil; shutil.rmtree(d, ignore_errors=True)
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  5 controls pass: availability matches the filesystem · digital silence measures "
              "silent · SILENCE IS NEVER INSERTED AS TEXT and says why · empty text refused · "
              "unknown target refused · speaker-loopback sources are not offered as microphones")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a or "--prove-fail" in a:
        return self_test()
    if "--status" in a or not a:
        print(json.dumps(available(), indent=2)); return 0
    if a[0] == "--dictate":
        secs = float(a[1]) if len(a) > 1 else 5.0
        tgt = a[2] if len(a) > 2 else "aionui"
        print(json.dumps(dictate(secs, tgt), indent=2)); return 0
    print("usage: voice_in.py [--status | --dictate SECONDS [target] | --prove-fail]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
