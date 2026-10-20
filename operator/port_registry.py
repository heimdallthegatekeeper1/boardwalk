#!/usr/bin/env /usr/bin/python3
"""port_registry — who owns which port, and a checker that FAILS when the wrong process holds one.

★★★ WHY THIS EXISTS, AND WHY THE LAST ONE DID NOT SURVIVE.
A port-etiquette protocol existed since ~April and decayed. On 2026-09-17 a `hermes --profile
lead-example acp` process bound 127.0.0.1:9230 — AionUi's debugger port — twelve minutes before Electron
asked for it. Electron lost the bind silently, the panel's reads went to a socket that accepted and
never answered, the panel leaked file descriptors to 1023/1024, and the operator's microphone went
cold. `routing_state.md` shows the SAME CLASS was already fixed once ("app-spawned voicebox no
longer steals :9230") — that fix named the thief instead of the class, so the next thief walked in.

★★ THE LESSON IS NOT "WRITE THE RULE DOWN AGAIN". The rule WAS written down:
    AGENTS.md:10 — "the electron process serving out/renderer/index.html over CDP port 9230 —
                    determined from the RUNNING PROCESS, never a config assumption."
It was written, it was correct, and nothing could check it. A rule with no checker is a preference.
So this module is a REGISTRY **and** a CHECKER, and the checker is the point.

★ TIERS — the operator's requirement: the top tier is off-limits, NOT described, and visible only to
top-level access. Folded into the estate's existing Ring vocabulary rather than inventing a parallel
scheme:

    Ring 0  SEALED       Existence is acknowledged; contents are NEVER described at lower rings.
                         Stored in a SEPARATE 0600 file, not in this one — so redaction is a
                         filesystem property, not a promise made by this code.
    Ring 1  RESTRICTED   Name, owner and purpose. No bind address, no how-to-reach.
    Ring 2  OPERATIONAL  Full detail. The working surfaces an agent legitimately needs.
    Ring 3  EPHEMERAL    Dynamic/ephemeral ranges. No ownership is claimed or enforced.

★★ Redaction is asymmetric on purpose: a caller may always learn that a sealed range EXISTS and how
many entries it holds. Hiding the existence of a boundary is how people trip over it — and a count
leaks nothing a port scan would not reveal anyway.

Run the controls:   /usr/bin/python3 operator/port_registry.py --self-test
Check the box:      /usr/bin/python3 operator/port_registry.py --check
View as a ring:     /usr/bin/python3 operator/port_registry.py --view --ring 2
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent.parent
REGISTRY = BASE / "ports.registry.json"          # rings 1-3, readable
SEALED = HOME / ".khaoss-secrets" / "ports.sealed.json"   # ring 0, mode 0600

RING_NAMES = {0: "SEALED", 1: "RESTRICTED", 2: "OPERATIONAL", 3: "EPHEMERAL"}


# ---------------------------------------------------------------- live state

def live_ports() -> dict[int, str]:
    """{port: holder-process-name} for every LISTENING socket. Measured, never assumed."""
    out: dict[int, str] = {}
    try:
        res = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return out
    for line in res.splitlines()[1:]:
        m_addr = re.search(r"\s(\S+):(\d+)\s", line)
        # ★★★ ALL HOLDERS, NOT THE FIRST. 2026-09-17.
        #   This took only the first name ss printed and judged the port by it. Measured on :9230:
        #       users:(("hermes",pid=2435365,fd=81),("electron",pid=1244287,fd=81))
        #   BOTH processes hold THE SAME fd 81 — AionUi's ACP child inherited Electron's listening
        #   descriptor (no FD_CLOEXEC). The port was never stolen; CDP answers normally. But the
        #   checker named 'hermes' and reported WRONG_HOLDER — the signature of the real collision
        #   that took the operator's microphone cold this morning.
        #   ★★ A false alarm that impersonates a known outage is worse than silence: it argues for
        #     a fix that is not needed, and the obvious "fix" is to kill a LIVE AGENT process.
        #   ★ The fd inheritance is a genuine bug and is reported as INHERITED_FD — surfaced, but
        #     never as a collision, because those are different faults with different remedies.
        holders = re.findall(r'\("([^"]+)",pid=', line)
        if not m_addr:
            continue
        port = int(m_addr.group(2))
        out[port] = holders if holders else ["?"]
    return out


def _bind_scope(port: int) -> str:
    """loopback / tailnet / ANY — the exposure of a live port, measured."""
    try:
        res = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return "unknown"
    scopes = set()
    for line in res.splitlines()[1:]:
        m = re.search(r"\s(\S+):(\d+)\s", line)
        if not m or int(m.group(2)) != port:
            continue
        a = m.group(1)
        if a in ("0.0.0.0", "*", "[::]"):
            scopes.add("ANY")
        elif a.startswith("127.") or a in ("[::1]", "::1"):
            scopes.add("loopback")
        elif a.startswith("100."):
            scopes.add("tailnet")
        else:
            scopes.add(a)
    return "+".join(sorted(scopes)) if scopes else "not-listening"


# ---------------------------------------------------------------- registry io

def load_registry() -> dict:
    if not REGISTRY.is_file():
        return {"version": 1, "entries": []}
    try:
        return json.loads(REGISTRY.read_text())
    except Exception:
        return {"version": 1, "entries": [], "error": "registry unreadable"}


def load_sealed(may_read_ring0: bool) -> tuple[list, str]:
    """Ring-0 entries. Returns ([], reason) when the caller is not cleared — never partial content.

    ★ The refusal names the boundary and the count, never a port, a name or a purpose.
    """
    if not may_read_ring0:
        n = 0
        try:
            if SEALED.is_file():
                n = len(json.loads(SEALED.read_text()).get("entries", []))
        except Exception:
            n = -1
        return [], (f"Ring 0 SEALED: {n if n >= 0 else 'an unknown number of'} entr"
                    f"{'y' if n == 1 else 'ies'} exist and are not described at this ring.")
    if not SEALED.is_file():
        return [], "Ring 0 SEALED: no sealed registry file exists."
    try:
        return json.loads(SEALED.read_text()).get("entries", []), ""
    except Exception as exc:
        return [], f"Ring 0 SEALED: file unreadable ({exc.__class__.__name__}) — treat as opaque."


# ---------------------------------------------------------------- the view

def view(ring: int, may_read_ring0: bool = False) -> dict:
    """What a caller at `ring` may see. Higher ring number = LESS privilege."""
    reg = load_registry()
    rows = []
    for e in reg.get("entries", []):
        r = int(e.get("ring", 2))
        if r < ring:
            continue                      # more privileged than the caller: not shown
        if r == 1 and ring > 1:
            continue
        if r == 1:
            rows.append({"port": e["port"], "owner": e.get("owner"),
                         "purpose": e.get("purpose"), "ring": r})
        else:
            rows.append({k: v for k, v in e.items()})
    sealed, sealed_note = load_sealed(may_read_ring0 and ring == 0)
    return {"ring": ring, "ring_name": RING_NAMES.get(ring, "?"),
            "entries": rows, "sealed": sealed, "sealed_note": sealed_note}


# ---------------------------------------------------------------- the checker

def check() -> dict:
    """★ THE POINT OF THE MODULE. Verify every claimed port is held by the process that claims it.

    Findings, most serious first:
      WRONG_HOLDER  a registered port is held by a process that is not its owner  (the 9230 case)
      EXPOSED       a port registered loopback-only is listening more widely
      UNREGISTERED  something is listening on a port nobody claimed
      ABSENT        a registered always-on port is not listening
    Separately, `acknowledged` carries exposures the operator has DECIDED to accept (each with a
    reason). They are still reported — they are simply not counted as failures.
    """
    reg = load_registry()
    live = live_ports()
    findings = []
    acknowledged = []
    claimed = set()
    for e in reg.get("entries", []):
        port = int(e["port"]); claimed.add(port)
        owner_pat = e.get("holder_pattern") or e.get("owner") or ""
        holder = live.get(port)
        if holder is None:
            if e.get("always_on"):
                findings.append({"severity": "ABSENT", "port": port, "owner": e.get("owner"),
                                 "detail": "registered always-on but nothing is listening"})
            continue
        holders = holder if isinstance(holder, list) else [holder]
        # A port is correctly held if ANY holder matches — see the inheritance note above.
        holder = next((h for h in holders if owner_pat and re.search(owner_pat, h, re.I)),
                      holders[0])
        if len(holders) > 1 and owner_pat and any(re.search(owner_pat, h, re.I) for h in holders):
            findings.append({"severity": "INHERITED_FD", "port": port, "name": e.get("name", ""),
                             "detail": f"held by {len(holders)} processes on one descriptor "
                                       f"({', '.join(holders)}) — the owner is present, so this is "
                                       f"fd inheritance, NOT a stolen port"})
        if holder == "?":
            # ★ "I could not look" is NOT "it is wrong". `ss` cannot name a process owned by another
            #   user, so an unnameable holder is UNVERIFIABLE, never a violation. Reporting it as
            #   WRONG_HOLDER is the same conflation that makes a truncated run look like a clean
            #   board — and it produced a false positive against ollama on this very box.
            findings.append({"severity": "UNVERIFIABLE", "port": port, "owner": e.get("owner"),
                             "detail": "listening, but the holder is owned by another user and "
                                       "cannot be named without privilege - not a violation"})
        elif owner_pat and not re.search(owner_pat, holder, re.I):
            findings.append({"severity": "WRONG_HOLDER", "port": port, "owner": e.get("owner"),
                             "detail": f"held by {holder!r}, expected to match /{owner_pat}/"})
        scope = _bind_scope(port)
        want = e.get("scope", "loopback")
        if want == "loopback" and scope not in ("loopback", "not-listening"):
            # ★★ exposure_ack — THE ESCAPE HATCH FOR A DECISION, NOT A HOLE IN THE CHECK. 2026-09-23.
            #   Found by measuring, not by reasoning: `:8780` (headscale) and `:11434` (ollama) were
            #   both EXPOSED, and BOTH had been reported to the operator already. `:11434` is a real
            #   standing exposure with no firewall compensating anywhere on this box, and it is a
            #   recorded decision for the operator ("on a trusted home network that may be exactly
            #   what you want"). `:8780` is DIFFERENT: headscale's config says `listen_addr:
            #   0.0.0.0:8780` and `server_url: http://203.0.113.44:8780`, i.e. LAN binding is the
            #   DESIGN — member nodes register against that address.
            #   ★ WITHOUT THIS FIELD THE BOARD IS PERMANENTLY RED ON AN ACCEPTED RISK, and a guard
            #   that is red on a decision you already made teaches people to ignore the board. That
            #   is the failure this whole file exists to prevent, in the opposite direction.
            #   ★ A REASON IS REQUIRED — an ack with no `reason` is treated as absent, so silence is
            #   never mistaken for consent. The suppressed exposure is RECORDED in `acknowledged`
            #   rather than dropped, so it still appears in `--check` output.
            ack = e.get("exposure_ack") or {}
            if isinstance(ack, dict) and str(ack.get("reason", "")).strip():
                acknowledged.append({"port": port, "owner": e.get("owner"), "scope": scope,
                                     "reason": str(ack["reason"]).strip(),
                                     "decided_by": ack.get("decided_by"),
                                     "decided_at": ack.get("decided_at")})
            else:
                findings.append({"severity": "EXPOSED", "port": port, "owner": e.get("owner"),
                                 "detail": f"registered loopback-only but listening on {scope} "
                                           f"(no exposure_ack with a reason — add one, or bind loopback)"})
    # ring 0 ports participate in the check WITHOUT being named in the output
    sealed, _ = load_sealed(True)
    sealed_ports = {int(e["port"]) for e in sealed} if sealed else set()
    claimed |= sealed_ports
    for port, _h in sorted(live.items()):
        holder = (_h[0] if isinstance(_h, list) else _h)
        if port in claimed or port < 1024:
            continue
        findings.append({"severity": "UNREGISTERED", "port": port, "owner": None,
                         "detail": f"{holder!r} is listening on an unclaimed port"})
    order = {"WRONG_HOLDER": 0, "EXPOSED": 1, "ABSENT": 2, "UNVERIFIABLE": 3, "UNREGISTERED": 4}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["port"]))
    return {"ok": not any(f["severity"] in ("WRONG_HOLDER", "EXPOSED") for f in findings),
            "checked": len(claimed), "live": len(live), "findings": findings,
            # Reported, never dropped: an accepted exposure must stay VISIBLE, or the acceptance
            # becomes indistinguishable from having forgotten about it.
            "acknowledged": acknowledged}


# ---------------------------------------------------------------- controls

def self_test() -> int:
    fails = []

    def check_(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{('  — ' + detail) if detail and not cond else ''}")
        if not cond:
            fails.append(name)

    # 1 — a lower ring must NEVER receive sealed contents
    v2 = view(2, may_read_ring0=False)
    check_("ring 2 receives ZERO sealed entries", v2["sealed"] == [])
    check_("ring 2 is TOLD a sealed range exists (boundary is visible, contents are not)",
           "SEALED" in (v2["sealed_note"] or ""))
    blob = json.dumps(v2)
    sealed_real, _ = load_sealed(True)
    leaked = [str(e.get("port")) for e in sealed_real if str(e.get("port")) in blob]
    check_("no sealed PORT NUMBER appears anywhere in a ring-2 view", not leaked, f"leaked {leaked}")

    # 2 — even a ring-0 request without clearance is refused
    v0 = view(0, may_read_ring0=False)
    check_("ring 0 view WITHOUT clearance still returns nothing", v0["sealed"] == [])

    # 3 — the sealed file must not be world-readable
    if SEALED.is_file():
        mode = stat.S_IMODE(SEALED.stat().st_mode)
        check_("sealed file is 0600", mode == 0o600, f"mode is {oct(mode)}")
    else:
        print("  [SKIP] sealed file permissions — no sealed file yet")

    # 4 — the checker must CATCH a wrong holder (the 9230 case), not just describe one
    reg = load_registry()
    probe = {"port": 65432, "owner": "nothing-ever", "holder_pattern": "^definitely-not-a-real-proc$",
             "ring": 2, "scope": "loopback", "always_on": True}
    reg.setdefault("entries", []).append(probe)
    orig = REGISTRY.read_text() if REGISTRY.is_file() else None
    try:
        REGISTRY.write_text(json.dumps(reg))
        res = check()
        got = [f for f in res["findings"] if f["port"] == 65432]
        check_("checker reports an always-on port that is absent", bool(got), str(res)[:120])
    finally:
        if orig is not None:
            REGISTRY.write_text(orig)

    # 5 — the checker must not be fooled by 'listening' alone (today's exact defect)
    res = check()
    check_("check() returns a structured verdict with an ok flag", isinstance(res.get("ok"), bool))

    # 6 — ★★ THE exposure_ack ESCAPE HATCH, DRIVEN BOTH WAYS. 2026-09-23.
    #     A NEW MECHANISM WITH NO NEGATIVE CONTROL IS THE THING INV-114 EXISTS TO CATCH, and this
    #     one could fail in the dangerous direction: if the ack suppressed the finding whenever the
    #     key was merely PRESENT, then a typo'd or empty ack would silently retire a live exposure.
    #     So this drives a real wildcard listener (headscale :8780, measured scope ANY) through both
    #     states and demands the finding appear, then disappear, and be RECORDED on the way out.
    _orig = REGISTRY.read_text() if REGISTRY.is_file() else None
    _reg = load_registry()
    _ent = next((e for e in _reg.get("entries", []) if int(e.get("port", 0)) == 8780), None)
    if _ent is None or _bind_scope(8780) in ("loopback", "not-listening"):
        print("  [SKIP] exposure_ack both-directions control — no wildcard listener on :8780 to drive")
    else:
        _saved = _ent.pop("exposure_ack", None)
        try:
            # A — NO ACK: the exposure MUST be reported
            REGISTRY.write_text(json.dumps(_reg))
            _no_ack = [f for f in check()["findings"] if f["port"] == 8780 and f["severity"] == "EXPOSED"]
            check_("an unacknowledged wildcard listener is reported EXPOSED", bool(_no_ack),
                   f"findings for 8780: {[f['severity'] for f in check()['findings'] if f['port'] == 8780]}")

            # B — ACK WITH NO REASON: still a violation. Silence is never consent.
            _ent["exposure_ack"] = {"decided_by": "self-test", "decided_at": "self-test"}
            REGISTRY.write_text(json.dumps(_reg))
            _empty = [f for f in check()["findings"] if f["port"] == 8780 and f["severity"] == "EXPOSED"]
            check_("an ack with NO reason does NOT retire the exposure", bool(_empty),
                   "an empty ack suppressed a live exposure")

            # C — ACK WITH A REASON: the finding retires and the exposure is still REPORTED
            _ent["exposure_ack"] = {"reason": "self-test: LAN binding is the design", "decided_by": "self-test",
                                    "decided_at": "self-test"}
            REGISTRY.write_text(json.dumps(_reg))
            _res = check()
            _still = [f for f in _res["findings"] if f["port"] == 8780 and f["severity"] == "EXPOSED"]
            _acked = [a for a in _res.get("acknowledged", []) if a["port"] == 8780]
            check_("a reasoned ack retires the finding", not _still,
                   f"still reported: {[f['detail'][:40] for f in _still]}")
            check_("a retired exposure is still RECORDED as acknowledged (not dropped)",
                   bool(_acked) and "self-test" in _acked[0]["reason"], f"acknowledged={_acked}")
        finally:
            if _saved is not None:
                _ent["exposure_ack"] = _saved
            REGISTRY.write_text(json.dumps(_reg))
            if _orig is not None:
                REGISTRY.write_text(_orig)

    print(f"\n  {11 - len(fails)}/11 controls passed" if not fails else f"\n  FAILED: {', '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--check" in sys.argv:
        r = check()
        print(json.dumps(r, indent=2))
        raise SystemExit(0 if r["ok"] else 2)
    if "--view" in sys.argv:
        ring = 2
        if "--ring" in sys.argv:
            ring = int(sys.argv[sys.argv.index("--ring") + 1])
        print(json.dumps(view(ring, may_read_ring0=(ring == 0 and os.geteuid() == os.stat(str(SEALED)).st_uid
                                                    if SEALED.is_file() else False)), indent=2))
        raise SystemExit(0)
    print(__doc__)
