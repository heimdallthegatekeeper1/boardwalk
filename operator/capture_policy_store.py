#!/usr/bin/env python3
"""capture_policy_store.py — the microphone permission as a FILE, not a network call.

★★★ WHY THIS EXISTS
    The capture policy is the boolean that decides whether the operator's microphone may open.
    Its truth lives in AionUi's localStorage, so reading it meant a CDP websocket round-trip to
    Electron on EVERY read. Measured on a quiet box, 2026-09-17:
        /state/capture-policy — min 975 ms · median 3699 ms · p90 8578 ms · 3 of 15 timing out
        35 panel threads simultaneously inside that CDP call
    The voice seat is on the path of /ptt, /listen and every mic tick, so it cached the answer with
    a 2 s staleness bound — and the other seat found the killer:

        ★★ A 2-SECOND STALENESS BOUND IS SHORTER THAN ONE REFRESH AT THAT LATENCY.
           The cache was stale more often than fresh, so the operator's microphone tore down
           SECONDS AFTER HE ENGAGED IT. Nothing failed. The contract was unachievable.

    No implementation can bound revocation to 2 s when a fresh read costs 3.7 s. The fix is not a
    better cache — it is to stop making permission a network call. One writer polls CDP on a
    cadence it already pays for; every reader gets a local file in microseconds. Staleness stops
    being a race and becomes a number you can actually enforce.

★ FAIL CLOSED, ALWAYS. Missing file, unparseable file, stale file, clock skew — every one of them
  returns mic_allowed=False with a reason. An unreadable permission is NEVER an opening. This
  matches the seat's existing posture and must not be relaxed: "could not look" and "you may" must
  never share a code path.

★ tmpfs on purpose (/run/user/<uid>): microsecond reads, and it is CLEARED ON REBOOT — which is
  correct, because a permission should be re-derived after a reboot, never resurrected from disk.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

_RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
STORE_DIR = _RUNTIME / "khaoss"
STORE = STORE_DIR / "capture-policy.json"

# ★★★ THIS BOUND IS DERIVED FROM A MEASUREMENT, NOT DECLARED.
#   First attempt hung the publish off the panel's steward sweep, whose declared interval was 3 s.
#   Measured cadence was 15 s — because that loop AUTO-PAUSES when panel RSS crosses a ceiling
#   (the panel sits at ~725MB against a 700MB ceiling), waits interval*4 and skips the publish.
#   A bound EQUAL to the real cadence means the file is stale for roughly half of every cycle by
#   construction: measured 57% usable, and the operator's gate still flapped. That is the same
#   arithmetic error as the 2 s bound this whole store was built to replace, one layer up.
#   Now: a DEDICATED publisher that never pauses. Measured after the split —
#       age min 0.0s · median 1.9s · max 4.3s · one write every ~4.3s · readable 30/30 = 100%
#   So the real cadence is ~4.3 s (3 s wait + one bounded CDP read), and 15 s is ~3.5x that:
#   wide enough to ride out three consecutive missed writes, tight enough to notice a dead writer.
#   ★ Revocation does NOT depend on this bound: the panel republishes IMMEDIATELY when the policy
#     is written, so an operator switching capture off takes effect at once. This bound is the
#     dead-writer detector, nothing else.
#   ★★ Re-measure before changing it. "Declared, never measured" is how the first version shipped.
#   ★★★ RE-DERIVED 15.0 -> 45.0 after a LONGER measurement. A 30-sample window said 100% usable
#     and I reported that; the 60-sample window said 92% with max age 19.2 s. The short window was
#     the lucky one. For anything intermittent, N=1 is not a measurement and a GOOD short window is
#     the most dangerous kind — it is exactly when you stop looking.
#     Legitimate gap = tick (3 s) + worst publish duration under CDP contention, observed to ~15 s.
#     A 15 s bound against a 14.5 s worst observed age is 1.0x headroom — at the cliff, and a FALSE
#     DENIAL *is* the gate flap this store exists to stop. 45 s is ~3x the worst legitimate gap.
#   ★★ 45 s is safe only because REVOCATION DOES NOT DEPEND ON THIS BOUND. The POST path
#     republishes BEFORE it answers (proven: mic_allowed True->False, file rewritten by the POST,
#     age 0.318 s), so an operator switching capture off takes effect at once. This bound is purely
#     a DEAD-WRITER detector. If the panel dies while the policy says ON, a 45 s-old ON still
#     reflects the operator's last stated intent; the dangerous case — he switches OFF and the
#     panel dies before publishing — cannot happen, because a successful OFF is on disk before he
#     receives his response.
MAX_AGE_S = 45.0


def _denied(reason: str, **extra) -> dict:
    """The one shape every failure returns. ★ mic_allowed is False on every path through here."""
    out = {
        "ts": int(time.time() * 1000),
        "readable": False,
        "master": False,
        "paused": False,
        "mic_allowed": False,
        "reason": reason,
        "source": f"file:{STORE}",
        "degraded": [reason],
    }
    out.update(extra)
    return out


def write(policy: dict) -> bool:
    """Publish a policy atomically. Readers never observe a partial file.

    ★ tmp + os.replace, not open('w') — a reader hitting a half-written permission file would be
      reading a torn boolean, and this estate has already shipped 'the setting died with its
      socket'. os.replace is atomic within a filesystem.
    """
    try:
        STORE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        body = dict(policy)
        body["written_at"] = time.time()
        body["writer_pid"] = os.getpid()
        fd, tmp = tempfile.mkstemp(dir=str(STORE_DIR), prefix=".cp-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(body, fh)
                fh.flush()
                os.fsync(fh.fileno())          # survive a panel crash mid-write
            os.chmod(tmp, 0o600)
            os.replace(tmp, STORE)             # atomic
            return True
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    except Exception:
        return False


def read(max_age_s: float = MAX_AGE_S) -> dict:
    """Read the published policy. Fails CLOSED on every abnormal path."""
    try:
        raw = STORE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _denied("no capture policy published — the panel has not written one "
                       "(mic stays closed until it does)")
    except Exception as exc:
        return _denied(f"capture policy unreadable: {exc.__class__.__name__}")

    try:
        p = json.loads(raw)
    except Exception:
        return _denied("capture policy file is not valid JSON")

    written = p.get("written_at")
    if not isinstance(written, (int, float)):
        return _denied("capture policy carries no write timestamp")

    age = time.time() - written
    if age < -2.0:
        # Clock moved backwards. Refusing is the only safe answer.
        return _denied(f"capture policy timestamp is in the future by {-age:.1f}s (clock skew)",
                       age_s=round(age, 3))
    if age > max_age_s:
        return _denied(f"capture policy is STALE ({age:.1f}s old, bound {max_age_s:.0f}s) — "
                       f"the writer may be dead", age_s=round(age, 3), stale=True)

    p["age_s"] = round(age, 3)
    p["stale"] = False
    p["source"] = f"file:{STORE} (published; truth is localStorage via the panel)"
    # ★ Never trust a published True blindly: re-derive the boolean from its parts, so a file that
    #   carries mic_allowed=True with master=False can never open the microphone.
    p["mic_allowed"] = bool(p.get("readable") and p.get("master") and not p.get("paused"))
    return p


def self_test() -> int:
    fails = 0

    def check(label: str, ok: bool):
        nonlocal fails
        fails += (not ok)
        print(f"  {'✓' if ok else '✗'} {label}")

    original = STORE.read_text(encoding="utf-8") if STORE.exists() else None
    try:
        write({"readable": True, "master": True, "paused": False, "mic_allowed": True,
               "reason": "capture master is ON"})
        r = read()
        check("a fresh ON policy reads back as mic_allowed", r["mic_allowed"] is True)
        check("mode is 0600", oct(STORE.stat().st_mode & 0o777) == "0o600")

        write({"readable": True, "master": False, "paused": False, "mic_allowed": False,
               "reason": "capture master is OFF"})
        check("master OFF denies", read()["mic_allowed"] is False)

        write({"readable": True, "master": True, "paused": True, "mic_allowed": True,
               "reason": "paused"})
        check("★ PAUSED denies even when the file claims mic_allowed=True",
              read()["mic_allowed"] is False)

        write({"readable": False, "master": True, "paused": False, "mic_allowed": True})
        check("★ unreadable-upstream denies even when the file claims True",
              read()["mic_allowed"] is False)

        write({"readable": True, "master": True, "paused": False})
        check("★ STALE denies (bound exceeded)", read(max_age_s=-1)["mic_allowed"] is False)
        check("  and says so", read(max_age_s=-1).get("stale") is True)

        STORE.write_text("{not json", encoding="utf-8")
        check("★ corrupt file denies", read()["mic_allowed"] is False)

        STORE.unlink()
        check("★ missing file denies", read()["mic_allowed"] is False)
    finally:
        if original is not None:
            STORE.write_text(original, encoding="utf-8")
            os.chmod(STORE, 0o600)
        elif STORE.exists():
            STORE.unlink()

    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILED'} — every abnormal path denies.")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    # ★★★ AN UNRECOGNISED ARGUMENT MUST NOT EXIT 0. 2026-09-17.
    #   The voice seat lost TWELVE HOURS to this exact shape: its module dispatched on
    #   "--selftest" while every invocation used "selftest", so the wrong form fell through to the
    #   default path, printed something plausible and exited 0. NINE CONTROLS RAN ZERO TIMES and
    #   the suite reported a pass the whole while.
    #   ★ Worse than unprotected: two of those controls encoded a policy that had since been
    #     DELIBERATELY REVERSED, so anyone who ran them and "made them pass" would have restored a
    #     known defect — a dead test still argues, with the authority of a control.
    #   So: exactly one accepted flag, and anything else is a loud non-zero exit.
    args = [a for a in sys.argv[1:] if a.strip()]
    if args == ["--self-test"]:
        raise SystemExit(self_test())
    if args:
        print(f"unknown argument(s): {' '.join(args)}\n"
              f"usage: capture_policy_store.py [--self-test]", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(read(), indent=2))
