#!/usr/bin/env python3
"""cc_session_names.py — give a running Claude Code session a NAME a human recognises.

★★★ THE PROBLEM. The Send-to picker listed "Claude Code session 2403068". A process id is not an
    identity: five sessions share one working directory, all five read as interchangeable, and there is
    no way to tell the seat that runs the trading lane from the one reading Starlink telemetry. He
    cannot route an item to a chat he cannot recognise.

★★ WHERE THE NAME ACTUALLY IS. Not on the process — no session id in its environment, cmdline or open
    descriptors. It is in the TRANSCRIPT: ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl, whose first
    real user message is, in practice, what the chat is about. "You're 2KHAOSS dev chat." is a better
    name than any id.

★ MATCHING IS BY START TIME AND IT IS STATED AS AN INFERENCE, NOT A FACT. A transcript is created when
  its session starts, so the transcript whose first record is nearest the process's start time is
  almost certainly that session's. Almost. So every resolved name carries a confidence, and a match
  outside a tolerance window is reported as UNMATCHED rather than guessed — a picker that confidently
  mislabels a destination is worse than one that admits it does not know, because he would send to the
  wrong seat and never find out.
"""
from __future__ import annotations
import json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
ALIASES = Path(__file__).resolve().parent / "cc_session_aliases.json"
SOCKS = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "cc-socks"
TOLERANCE_S = 180          # ★ empirical: a transcript's first record lands within ~3 min of process start


def _proc_start_epoch(pid: str):
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        boot = datetime.now().timestamp() - uptime
        return boot + starttime_ticks / hz
    except Exception:
        return None


def _proc_cwd(pid: str):
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except Exception:
        return None


def _transcripts():
    """Every session transcript with its first timestamp and its first real user line."""
    out = []
    if not PROJECTS.exists():
        return out
    for d in PROJECTS.iterdir():
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            first_ts, subject = None, None
            try:
                with f.open(errors="replace") as fh:
                    for n, line in enumerate(fh):
                        if n > 400 or subject:
                            break
                        if not line.strip():
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        if first_ts is None and r.get("timestamp"):
                            first_ts = r["timestamp"]
                        if r.get("type") == "user":
                            c = r.get("message", {}).get("content")
                            txt = None
                            if isinstance(c, str):
                                txt = c
                            elif isinstance(c, list):
                                for p in c:
                                    if isinstance(p, dict) and p.get("type") == "text":
                                        txt = p.get("text"); break
                            if txt and txt.strip() and not txt.strip().startswith(("<", "[")):
                                subject = " ".join(txt.split())[:78]
            except Exception:
                continue
            if first_ts:
                try:
                    ep = datetime.fromisoformat(first_ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ep = None
                out.append({"uuid": f.stem, "dir": d.name, "first_epoch": ep,
                            "subject": subject, "path": str(f),
                            "mtime": f.stat().st_mtime})
    return out


PEERS = Path(__file__).resolve().parent / "cc_peer_sockets.json"


def _peers() -> dict:
    """★★ SOCKET → ADDRESSABLE PEER NAME, PROVEN BY AN ARRIVING MESSAGE.
    A cross-session message carries BOTH its socket path and the sender's name:
        <cross-session-message from="uds:$XDG_RUNTIME_DIR/cc-socks/12345.sock" from-name="example-seat">
    That pairing is a FACT, not an inference — the transport asserted both halves at once. It is a
    weaker name than one a person chose ("Example Team seat"), but it is infinitely better than a pid,
    and unlike the transcript matching it cannot be a coincidence of file density.
    ★ Recorded only when observed. Nothing here is guessed."""
    if PEERS.exists():
        try:
            return json.loads(PEERS.read_text()).get("sockets", {})
        except Exception:
            return {}
    return {}


def record_peer(pid: str, peer_name: str, seen: str = "") -> dict:
    d = {"doc": "socket -> addressable peer name, each PROVEN by a message that arrived carrying both.",
         "sockets": _peers()}
    d["sockets"][str(pid)] = {"peer": peer_name, "proven_by": seen or "a cross-session message from that socket",
                              "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}
    PEERS.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "pid": str(pid), "peer": peer_name}


def _aliases() -> dict:
    """★★ NAMES A HUMAN ASSERTED, and they win over anything inferred.
    Start-time matching only works for a FRESH session: a resumed one keeps its original transcript
    while its process is days younger, so four of five sessions here cannot be matched that way at all.
    Rather than guess harder, the picker lets him NAME a seat once and remembers it — which is also the
    only source that can know a thing no file records, like which chat he thinks of as 'the trading
    seat'. An asserted name is evidence; an inferred one is a hypothesis; the file says which is which."""
    if ALIASES.exists():
        try:
            return json.loads(ALIASES.read_text()).get("aliases", {})
        except Exception:
            return {}
    return {}


def set_alias(pid: str, name: str, by: str = "operator", purpose: str = "") -> dict:
    d = {"doc": "Names for Claude Code sessions, asserted by a person. These OVERRIDE inference.",
         "aliases": _aliases()}
    name = " ".join(str(name).split())[:80]
    if not name:
        d["aliases"].pop(str(pid), None)
    else:
        d["aliases"][str(pid)] = {"name": name, "by": by, "purpose": purpose,
                                  "at": datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")}
    ALIASES.write_text(json.dumps(d, indent=2) + "\n")
    return {"ok": True, "pid": str(pid), "name": name or None}


def sessions():
    """Every LIVE session socket, named as well as the evidence allows."""
    rows = []
    alias = _aliases()
    peers = _peers()
    trs = _transcripts()
    try:
        socks = sorted(SOCKS.glob("*.sock"))
    except Exception:
        socks = []
    for s in socks:
        pid = s.stem
        alive = Path(f"/proc/{pid}").exists()
        start = _proc_start_epoch(pid) if alive else None
        cwd = _proc_cwd(pid) if alive else None
        best, gap = None, None
        if start:
            for t in trs:
                if not t["first_epoch"]:
                    continue
                g = abs(t["first_epoch"] - start)
                if gap is None or g < gap:
                    best, gap = t, g
        # ★ CONFIDENCE, NOT A GUESS DRESSED AS A FACT.
        a = alias.get(pid)
        if a:
            label = a["name"]
            conf = "named"
            detail = f"named by {a.get('by','a person')} on {str(a.get('at'))[:10]} — asserted, not inferred"
        # ★★★ TRANSCRIPT INFERENCE IS NO LONGER ALLOWED TO PRODUCE A NAME. It is kept as a HINT only.
        #   Measured against ground truth from the seats themselves, it went 1 for 3: it assigned
        #   c2a61e31 to pid 2671527 when that transcript belongs to 59161, and it assigned a
        #   35,906-record file to the socket at "+7.2s" — a gap that measured the file's DENSITY, since a
        #   transcript that busy has a neighbour within seconds of any timestamp.
        #   ★ Two seats independently confirmed the deeper reason: the claude processes hold no open
        #     handle on their .jsonl, all five share one parent (the VS Code node service) and one cwd,
        #     and three have been alive since 23–27 August — so start time, parentage, cwd and age are
        #     all non-discriminating from the outside. A seat can identify itself; nothing outside can.
        #   So a wrong-but-plausible label can no longer reach a picker he routes work from.
        elif False:
            pass
        elif peers.get(pid):
            # ★ The addressable name, proven by the transport. Not what he calls it — what it IS.
            pr = peers[pid]
            label = pr["peer"]
            conf = "peer-proven"
            detail = (f"identified itself as {pr['peer']} in a message from this socket "
                      f"({str(pr.get('at'))[:10]}) — the transport asserted the socket and the name "
                      f"together, so this is observed, not inferred. You can still rename it.")
        else:
            label = f"unnamed session {pid}"
            conf = "unmatched"
            # ★ THE HINT IS GONE. Left in, it offered "You're 2KHAOSS dev chat." — THIS session's own
            #   transcript — as the possibility for BOTH remaining seats, because that file is the one
            #   being written right now. A wrong suggestion labelled unreliable is still an anchor: he
            #   would read it, half-believe it, and the label would do work it has not earned. An
            #   inference that has been measured at 1-for-3 is not worth showing at any confidence.
            hint = ""
            detail = ("nothing outside this seat can identify it — no session id on the process, one "
                      "shared parent, one shared cwd, and no open transcript handle. Ask it, or name "
                      "it here." + hint)
        rows.append({
            "pid": pid, "alive": alive, "label": label, "confidence": conf, "why": detail,
            # ★★★ cwd IS ACTIVELY MISLEADING AND MUST NOT BE SHOWN AS IDENTITY.
            #   All five sockets report cwd=~/example-project. — but the LIQUID-100
            #   seat has never worked in that directory; its lane is ~/example-project and
            #   every file it touched today was there. So cwd does not merely fail to DISTINGUISH the
            #   seats (they are identical); for at least one it asserts the wrong lane with total
            #   confidence. Reported by that seat itself. Kept in the payload for diagnostics, flagged
            #   so no renderer treats it as a label.
            "cwd": cwd, "cwd_is_identity": False,
            "cwd_warning": ("the working directory does NOT indicate what a seat works on — at least "
                            "one seat's lane differs from its cwd"),
            "purpose": (alias.get(pid) or {}).get("purpose", ""), "uuid": (best or {}).get("uuid") if conf == "matched" else None,
            "started": datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M") if start else None,
            "last_active": (datetime.fromtimestamp(best["mtime"]).strftime("%Y-%m-%d %H:%M")
                            if conf == "matched" else None),
        })
    return rows


def self_test() -> int:
    """★ NEGATIVE CONTROL: the failure that matters is a CONFIDENT WRONG NAME. So the test is that an
    impossible start time produces `unmatched`, not the nearest transcript anyway."""
    fails = []
    rows = sessions()
    if not rows:
        fails.append("no sessions found at all — the resolver examined an EMPTY population and any "
                     "conclusion from it is vacuous")
    for r in rows:
        if r["confidence"] == "matched" and not r.get("uuid"):
            fails.append(f"{r['pid']} claims a match with no transcript behind it")
        if r["confidence"] == "unmatched" and not r["label"].startswith("unnamed"):
            fails.append(f"{r['pid']} is unmatched but carries a confident-looking name")
    saved = globals()["_proc_start_epoch"]
    try:
        globals()["_proc_start_epoch"] = lambda pid: 1.0        # 1970 — nothing can be near it
        far = sessions()
        if any(r["confidence"] == "matched" for r in far):
            fails.append("with an impossible start time the resolver STILL matched — it takes the "
                         "nearest transcript regardless of distance, which is how a picker confidently "
                         "sends an item to the wrong seat")
    finally:
        globals()["_proc_start_epoch"] = saved
    for f in fails:
        print(f"  ✗ {f}")
    print(f"  ✓ cc_session_names self_test: {len(rows)} session(s); an impossible start time yields "
          f"UNMATCHED rather than a confident wrong name" if not fails
          else "  ✗ cc_session_names self_test FAILED")
    return 1 if fails else 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    rows = sessions()
    print(f"\n  RUNNING CLAUDE CODE SESSIONS ({len(rows)})")
    for r in rows:
        mark = "·" if r["alive"] else "✗"
        print(f"    {mark} [{r['confidence']:9s}] {r['label']}")
        print(f"        pid {r['pid']} · started {r['started']} · {r['why']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
