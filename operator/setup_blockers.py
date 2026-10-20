#!/usr/bin/env python3
"""setup_blockers.py — THE ONE STORE of what a first install is blocked on.

★★★★★ WHY THIS FILE EXISTS, AND WHY IT IS A STORE RATHER THAN A WIZARD.

    The operator has asked for a first-install path repeatedly. Each time, the answer was a
    *document* — `docs/CLOUDFLARE_STEPS_FOR_OPERATOR.md`, `docs/GRANT_SPEC_WHAT_TO_SET.md`,
    `docs/TRANSPORT_LADDER_PHONE.md`. Three good documents, each correct, each **unaware of the
    others**, and none of them able to say *"you have finished two of my four steps."* That is the
    estate's recorded disease (`OPEN_ASKS.md`: *"It fails to remember what it has not built yet."*)
    wearing a new coat: a checklist that lives in prose cannot be resumed, counted, or checked.

    So the blockers are DATA here, each carrying its own LIVE PROBE, and the wizard is a renderer.
    Delete every UI and this file still answers the question. That is the test of a store.

★★ THE HONESTY VOCABULARY IS NOT DECORATION, IT IS THE OUTPUT. Every row carries BOTH:

      `state`   DONE · BLOCKED · STILL-BLOCKED-LATER · UNKNOWN     — is it done right now?
      `status`  ENFORCING · NOT ENFORCING YET · DESIGN-ONLY          — the estate's words for
                                                                      "does anything actually
                                                                      refuse on this box?"

    They are different axes and collapsing them is how this estate has twice reported a thing as
    working because a file existed. A step can be DONE and DESIGN-ONLY (the ollama fix is applied;
    the mechanism that would keep it applied is a one-shot script). A step can be BLOCKED and
    ENFORCING (the mic gate refuses in core today, and the always-on lock is holding).

    ★★ `UNKNOWN` IS A FIRST-CLASS STATE AND IS NEVER FOLDED INTO `DONE`. A probe that could not
       run returns UNKNOWN with the reason. *"I could not look"* and *"I looked and it was clean"*
       must never be the same answer (COWORK_PROTOCOL.md §2.7).

★★★ EVERY BLOCKED ROW CARRIES BOTH APPROVAL PATHS, AND THE ROW IS INVALID WITHOUT THEM.
     The operator, verbatim: *"there must be a natural typed way to approve or change and also a
     button click for changing those things from within the chat output."* So each row carries:

        `typing`   the exact sentence he can TYPE, resolved deterministically and FREE by
                   `jarvis/voice_intent.py`'s own rule — a regex/keyword parse that imports no
                   model client and opens no socket. NOT a second parser: this field is the
                   ARGUMENT that parser will be handed, one registry, one parse.
        `button`   the label and action id of the control that appears BESIDE the chat message
                   that reported the block.
        `say`      the sentence the seat SPEAKS when the action is blocked — naming the blocker,
                   why it is his, the exact command, the failure branch he actually hits, and
                   what happens next. §7 of `docs/SETUP_WIZARD_DESIGN.md` is the standard.

★ IT NEVER ACTS. There is no function in this file that grants, installs, writes outside its own
  cache, or runs a privileged command. The privileged half stays where it already is:
  `bin/capability-grant.py` issues and `bin/capability-grant-apply.sh` applies. This module READS.
  The estate's rule, quoted on every grant surface: **"It may ASK. It may not TAKE."**

REACH: new-only. This file writes nothing. `plan()` spawns read-only probes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# ★ THE GEV OPTION IS A STORE OF ITS OWN, AND THIS FILE POINTS AT IT RATHER THAN RESTATING IT.
#   `operator/gev_option.py` owns the recommendation's terms, its five-plus-one measured states, and
#   its accept/decline ledger. Copying any of that here would make two copies of the cost of a paid
#   API key, and the second copy is the one that goes stale — which, for a disclosure, means the one
#   that starts lying to him about money. ONE STORE PER FACT.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import gev_option as GEV  # noqa: E402
except Exception as _gev_e:  # noqa: BLE001 — the walk must still run without the option
    GEV = None
    _GEV_IMPORT_ERROR = f"{type(_gev_e).__name__}: {_gev_e}"

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
PANEL = "http://127.0.0.1:7842"
DAEMON = "http://127.0.0.1:8771"

# ── the closed state set. Adding a word is a deliberate act, not a typo. ───────────────────────────
# ★★★ `DECLINED` ADDED 2026-10-12, AND HERE IS WHY IT IS A WORD AND NOT A `DONE`.
#     The setup walk now carries a RECOMMENDED, OPTIONAL step (God's Eye View — see
#     `operator/gev_option.py`). A recommendation the operator can refuse needs a state that means
#     "he considered it and said no", and there were only two dishonest places to put it:
#       · `DONE`      — reports a thing as present when NOTHING WAS FETCHED. That is the defect this
#                       estate found NINE TIMES IN ONE DAY. It is the exact lie this file exists to
#                       prevent, and shipping a tenth one in the file that guards against it would be
#                       the worst possible place for it.
#       · `BLOCKED`   — nags him forever about a decision he already made. An offer that cannot be
#                       refused is not an offer, it is a default.
#     So DECLINED is SETTLED: it is not done, it is not blocked, it is not waiting, and it is not
#     unknown. It is excluded from `next_actionable` and from the page's default "what is not done"
#     view, and it stays REVERSIBLE from its own row. I7 in `_fails_for` enforces all of that.
STATES = ("DONE", "BLOCKED", "STILL-BLOCKED-LATER", "UNKNOWN", "DECLINED")
# ★ AND THE ONES THAT ARE NOT SETTLED. A renderer that counts DECLINED as outstanding work is the
#   nag this word exists to avoid; a renderer that counts it as finished is the lie. Named here so
#   neither can happen by accident.
OPEN_STATES = ("BLOCKED", "STILL-BLOCKED-LATER", "UNKNOWN")
STATUSES = ("ENFORCING", "NOT ENFORCING YET", "DESIGN-ONLY")
OWNERS = ("operator", "agent", "system")


def _enum(status: str) -> str:
    """The honesty vocabulary, normalised to the three words this estate uses. Nothing else passes.
    ★ Pointed at, not re-declared: `bubble.py:_enum` is the same rule. Duplicated here ONLY because
      this module must import with no display and no `jarvis/` on the path; the selftest asserts the
      two agree on every value that appears in this file."""
    s = str(status or "").strip().upper()
    return s if s in STATUSES else "DESIGN-ONLY"


def _run(cmd: list, timeout: int = 8) -> tuple:
    """(rc, stdout, stderr) or (-1, '', reason). Never raises — a probe that dies is UNKNOWN."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except FileNotFoundError as e:
        return -1, "", f"not installed: {e.filename}"
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return -1, "", f"{type(e).__name__}: {e}"


def _http(path: str, timeout: int = 6):
    """GET a loopback JSON endpoint. Returns (ok, obj_or_reason). Loopback only, by construction."""
    import urllib.request
    try:
        with urllib.request.urlopen(path, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _listening(port: int) -> tuple:
    """(True, 'bind address') if something LISTENs on `port`, else (False, reason).

    ★ THE BIND ADDRESS IS THE WHOLE POINT for the ollama row: `*:11434` and `127.0.0.1:11434` are
      the same port and OPPOSITE security postures. A probe that only checked "is it up" would have
      reported the defect as a pass."""
    rc, out, _ = _run(["ss", "-ltn"])
    if rc != 0:
        return None, "ss unavailable — could not look"
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3].endswith(":" + str(port)):
            return True, parts[3]
    return False, "nothing listening"


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE PROBES. Each returns (state, evidence). Read-only. No probe may write.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def probe_headscale_control() -> tuple:
    """Is our own headscale control plane up, and on WHICH interface?"""
    rc, out, err = _run(["systemctl", "--user", "is-active", "khaoss-headscale.service"])
    unit = (out or err).strip()
    listening, where = _listening(8780)          # control plane
    _, api_where = _listening(8781)              # metrics/API
    if listening is None:
        return "UNKNOWN", "ss unavailable; unit=%s" % unit
    if unit != "active":
        return "BLOCKED", f"khaoss-headscale.service is {unit!r}"
    if not listening:
        return "BLOCKED", "unit active but nothing listening on 8780"
    # ★ `*:8780` IS ALL INTERFACES, and this is worth saying out loud rather than passing.
    wide = where.startswith("*:") or where.startswith("0.0.0.0")
    note = f"unit active · control {where}" + (f" · api {api_where}" if api_where else "")
    if wide:
        note += "  ★ LISTENING ON ALL INTERFACES (*:8780) — every other seat service is loopback-only"
    return "DONE", note


HS_CLI = HOME / ".local" / "bin" / "headscale"
HS_CFG = HOME / ".config" / "headscale" / "config.yaml"


def _headscale(*args, timeout: int = 12) -> tuple:
    """headscale, ALWAYS with `--config`.

    ★★ MEASURED 2026-10-10, AND IT IS A TRAP WORTH WRITING DOWN. Run bare, the CLI reaches for
    `/var/run/headscale/headscale.sock` and dies with `context deadline exceeded` — while the server
    is healthy and listening on 8780. That reads as "headscale is broken" and it is not; the config
    is what tells the client where the server actually is. A probe that skipped the flag would have
    reported a working control plane as a blocker, so the flag is not optional here."""
    if not HS_CLI.exists():
        return -1, "", f"{HS_CLI} not present"
    return _run([str(HS_CLI), "--config", str(HS_CFG), *args], timeout=timeout)


def probe_headscale_nodes() -> tuple:
    """Can this box READ its own headscale registry?

    ★ MEASURED 2026-10-10: **yes, with `--config`** — and the bare invocation dies with
    `connecting to /var/run/headscale/headscale.sock: context deadline exceeded` while the server is
    healthy on 8780. The first version of this probe ran it bare and would have reported a working
    control plane as a blocker. The flag is the fix and this docstring is the scar."""
    rc, out, err = _headscale("nodes", "list")
    if rc != 0:
        tail = (err or out).strip().splitlines()[-1:] or [""]
        return "UNKNOWN", f"CLI cannot reach the server: {tail[0][:150]}"
    rows = [r for r in out.splitlines() if r.strip()]
    return "DONE", f"{len(rows)} node row(s) readable"


def probe_preauth_keys() -> tuple:
    """Are there UNEXPIRED preauth keys for the phone? ★ 'Five staged' was true once and is not a
    number to trust — key #4 expired 2026-09-22 and nothing said so."""
    rc, out, err = _headscale("preauthkeys", "list")
    if rc != 0:
        return "UNKNOWN", f"preauthkeys unreadable: {(err or out).strip()[:120]}"
    total = len([l for l in out.splitlines() if l.strip()])
    expired = len([l for l in out.splitlines() if "Expired" in l or "2026-09-22" in l])
    if total == 0:
        return "BLOCKED", "no preauth keys at all — one must be minted before a phone can enrol"
    return "DONE", f"{total} preauth key row(s); {expired} look expired — mint fresh at enrol time"


def probe_phone_enrolled() -> tuple:
    """★ THE STEP THAT CANNOT BE DONE FROM HERE. A phone must physically exist and be touched."""
    rc, out, err = _headscale("nodes", "list", "-o", "json")
    if rc != 0:
        return "STILL-BLOCKED-LATER", "node list unreadable — cannot count peers"
    try:
        nodes = json.loads(out or "[]")
    except ValueError:
        return "UNKNOWN", "node list was not JSON"
    peers = [n for n in nodes if str(n.get("given_name") or "").lower() not in ("this-box",)]
    if not peers:
        return ("STILL-BLOCKED-LATER",
                f"{len(nodes)} node(s), ALL of them this box — the mesh has never carried a "
                f"second device (peer count 0)")
    return "DONE", f"{len(peers)} peer(s) enrolled: " + ", ".join(
        str(p.get("given_name")) for p in peers[:4])


def probe_phone_step() -> tuple:
    """The phone step: how many peers exist AND whether a key is ready to hand him.

    ★ TWO FACTS, ONE ROW, because they are one decision — and the second is the one that rots. The
    record says *"headscale has 5 unused ones staged"*; measured now, **key #4 expired 2026-09-22**
    and nothing announced it. So the count is re-read at the moment of asking, never quoted."""
    state, ev = probe_phone_enrolled()
    ks, kev = probe_preauth_keys()
    if ks == "UNKNOWN":
        return state, f"{ev} · preauth keys: {kev}"
    return state, f"{ev} · {kev}"


def probe_cf_cert() -> tuple:
    """`cloudflared tunnel login` — the browser act. THE OPERATOR'S, AND IT HAS NO SUBSTITUTE."""
    cert = HOME / ".cloudflared" / "cert.pem"
    d = HOME / ".cloudflared"
    if cert.is_file():
        return "DONE", f"{cert} present ({cert.stat().st_size} bytes)"
    if not d.exists():
        return "BLOCKED", f"{d} does not exist — login has never been attempted"
    # ★ THE FAILURE HE ACTUALLY HIT. Firefox took the download because the CLI's loopback callback
    #   could not receive it. The folder then EXISTS AND IS EMPTY, which reads as "done" to anything
    #   that only checks the folder. So the empty folder is reported as the blocker it is.
    strays = sorted(d.iterdir()) if d.is_dir() else []
    extra = f" folder exists but holds {len(strays)} item(s): {[p.name for p in strays][:4]}" if strays else \
            " ★ folder exists and is EMPTY — a login was attempted and the certificate never landed"
    return "BLOCKED", extra.strip()


def probe_cf_tunnel() -> tuple:
    """Is there a named tunnel (credentials JSON)? Needs the cert, not root."""
    d = HOME / ".cloudflared"
    if not d.is_dir():
        return "BLOCKED", "no ~/.cloudflared at all"
    creds = sorted(d.glob("*.json"))
    if not creds:
        return "BLOCKED", "no tunnel credentials (*.json) — `tunnel create` has not run"
    return "DONE", f"{len(creds)} credential file(s): {[c.name[:8] for c in creds]}"


def probe_cf_route() -> tuple:
    """Is a hostname routed? Quick tunnels are RETIRED (the panel answers HTTP 410)."""
    hostfile = HOME / ".khaoss-tunnel-hostname"
    want = hostfile.read_text().strip() if hostfile.is_file() else "seat.example.invalid"
    cf = shutil.which("cloudflared")
    if not cf:
        return "UNKNOWN", "cloudflared not on PATH"
    rc, out, err = _run([cf, "tunnel", "list"], timeout=12)
    if rc != 0:
        return "BLOCKED", f"`tunnel list` failed: {(err or out).strip().splitlines()[-1:][0][:120]}"
    if not out.strip() or "No tunnels were found" in out:
        return "BLOCKED", "no tunnels exist"
    return "DONE", f"hostname target {want} · {len([l for l in out.splitlines() if l.strip()])-1} tunnel row(s)"


def probe_cf_service() -> tuple:
    """Is the cloudflared SYSTEM service installed and reporting a connector?"""
    rc, out, err = _run(["systemctl", "is-active", "cloudflared"])
    act = (out or err).strip()
    cfg = Path("/etc/cloudflared/config.yml")
    if act == "active":
        cf = shutil.which("cloudflared")
        if cf:
            rc2, out2, _ = _run([cf, "tunnel", "info", "khaoss"], timeout=12)
            if re.search(r"CONNECTOR", out2, re.I):
                return "DONE", "service active and reporting a connector"
            return "BLOCKED", "service active but NO connector reported — do not record as applied"
        return "UNKNOWN", "service active; cannot verify a connector"
    if not cfg.exists():
        return "BLOCKED", f"service {act!r} and no /etc/cloudflared/config.yml"
    return "BLOCKED", f"service {act!r} with a config present"


def probe_grant(capability: str) -> tuple:
    """Is a capability CURRENTLY granted? Composed from the ledger's own tool — never re-derived."""
    tool = REPO / "bin" / "capability-grant.py"
    if not tool.is_file():
        return "UNKNOWN", f"{tool} absent"
    rc, out, err = _run(["/usr/bin/python3", str(tool), "status"], timeout=12)
    if rc != 0:
        return "UNKNOWN", f"status failed: {(err or out)[:120]}"
    for line in out.splitlines():
        if line.strip().startswith(capability):
            cols = line.split()
            state = cols[2] if len(cols) > 2 else "?"
            return ("DONE" if state == "active" else "BLOCKED"), f"grant {capability} is {state}"
    return "BLOCKED", f"{capability} is not in the grant ledger at all"


def probe_ollama_loopback() -> tuple:
    """★ THE ROW THAT PROVES WHY BIND ADDRESS MATTERS. *:11434 is the defect; 127.0.0.1 is the fix."""
    up, where = _listening(11434)
    if up is None:
        return "UNKNOWN", where
    if not up:
        return "BLOCKED", "nothing listening on 11434 — the model server is not running"
    drop = Path("/etc/systemd/system/ollama.service.d/override.conf")
    line = ""
    try:
        for l in drop.read_text().splitlines():
            if "OLLAMA_HOST" in l:
                line = l.strip()
    except OSError:
        line = "(drop-in unreadable without privilege)"
    loop = where.startswith("127.0.0.1") or where.startswith("[::1]") or where.startswith("localhost")
    if loop:
        return "DONE", f"socket {where} · {line}"
    return "BLOCKED", f"socket {where} — ON ALL INTERFACES · {line}"


def probe_mic_gate() -> tuple:
    """The mic gate reads from the daemon's own state — never from a guess about the widget."""
    ok, st = _http(DAEMON + "/state")
    if not ok:
        return "UNKNOWN", f"daemon /state unreachable: {st}"
    mic = st.get("mic") if isinstance(st.get("mic"), dict) else {}
    engaged = bool(mic.get("engaged"))
    locked = bool(st.get("always_on_locked"))
    always = bool(st.get("opt_in_always_on"))
    mode = str(st.get("mode") or "?")
    phrase = ((st.get("always_on_unlock") or {}).get("phrase") or "").strip()
    ev = (f"mic {'engaged' if engaged else 'released'} · mode {mode} · indicator "
          f"{st.get('indicator')!r} · always-on {'ARMED' if always else 'off'} · "
          f"lock {'HOLDING' if locked else 'OPEN'}")
    if locked and phrase:
        ev += f" · release phrase: {phrase!r}"
    # ★ ENFORCING means something REFUSES. The lock refuses in core, so this is ENFORCING whatever
    #   the mic's current engagement is — and the row says which, because they are different facts.
    return "DONE", ev


def probe_speaker() -> tuple:
    """The sink. `pactl` needs no privilege; the seat is deliberately READ-ONLY on audio."""
    rc, out, err = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if rc != 0:
        return "UNKNOWN", f"pactl unavailable: {(err or out)[:100]}"
    m = re.search(r"(\d+)%", out)
    vol = int(m.group(1)) if m else None
    rc2, out2, _ = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    muted = "yes" in (out2 or "").lower()
    if muted:
        return "BLOCKED", f"sink MUTED at {vol}% — the seat cannot unmute it (read-only on audio)"
    if vol is not None and vol < 30:
        return "BLOCKED", f"sink at {vol}% — quiet enough to read as 'the assistant said nothing'"
    return "DONE", f"sink {vol}% · unmuted"


def probe_model_tier() -> tuple:
    """★ THE TERRITORY OF THE BRAIN: which tier is loaded, and against what ceiling?"""
    ok, b = _http(DAEMON + "/budget")
    if not ok:
        return "UNKNOWN", f"daemon /budget unreachable: {b}"
    tier = str(b.get("current") or "?")
    ceil = b.get("ceiling_mb")
    unison = bool(b.get("unison"))
    engine = "?"
    ok2, st = _http(DAEMON + "/state")
    if ok2:
        engine = str(st.get("engine") or "?")
    return "DONE", f"tier {tier} · engine {engine} · ceiling {ceil} MiB · unison {'ON' if unison else 'off'}"


def probe_territory() -> tuple:
    """The territory grant made AT SETUP. Reads the ONE access store; writes nothing."""
    p = HOME / ".config" / "AionUi-Dev" / "khaoss-access.json"
    if not p.is_file():
        return "BLOCKED", f"{p} absent — no territory has ever been declared"
    try:
        d = json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"unreadable: {type(e).__name__}: {e}"
    zones = ((d.get("_shaman") or {}).get("zones") or {})
    named = [k for k in zones if not str(k).startswith("_")]
    agents = d.get("agents") or {}
    if not named:
        return "BLOCKED", "no firewalls/zones declared"
    return "DONE", (f"{len(named)} zone(s): {', '.join(named[:6])} · {len(agents)} agent record(s) · "
                    f"★ zones are DECLARED, not ENFORCED — nothing consults them on a read path")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE LEAD AGENT — ★★★★ ADDED 2026-10-14 ON THE OPERATOR'S OWN REQUIREMENT.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  HIS WORDS: "The support team needs full access and capabilities. It is near the top, almost or all
#  the way for development purposes. It should be suggested to new installers/new users to have one."
#
#  ★ WHY THIS IS A STEP AND NOT A PARAGRAPH IN A DOC. A requirement that is only visible to someone
#    who goes looking for it is not in the install (the same ruling `VOICE_BOUNDARY` was built under).
#    It sits IMMEDIATELY AFTER `territory`, because that is the decision it belongs to: territory says
#    WHICH parts of the machine an assistant may reach, and this says WHO holds that reach and acts on
#    it. A zone nobody holds is a zone nothing can use.
#
#  ★★★ AND THAT LAST CLAUSE IS MEASURED, NOT RHETORICAL — WHICH IS THE WHOLE REASON THIS ROW EXISTS AS
#      A PROBE RATHER THAN A SENTENCE. An agent the ledger has no row for resolves against the
#      DECLARED default posture (`shaman_ledger.check_access` step 1). That posture is INSTALL-SCOPED
#      (`shaman_posture.active` ignores a record whose `install` is not this host), so on a fresh
#      install of this repo it is not in force at all, and the resolver returns
#      `denied_axis="default-posture"` — a REFUSAL. The new user's first experience is an assistant
#      that will not act for them, saying nothing about why.
#
#  ★ THE TWO SENTENCES THE ROW MAY NOT RENDER WITHOUT. Held ONCE, here, and required VERBATIM by I9,
#    so a later edit cannot quietly drop the load-bearing clause or the limit and still render a
#    green pill over it. ONE STORE PER FACT — the row points at these, it does not restate them.
LEAD_AGENT = {
    "clause": (
        "A fresh install has NO agent row, so nothing holds the reach this walk just configured: an "
        "agent the ledger does not list resolves against the declared default posture, that posture "
        "is INSTALL-SCOPED, and on any install but the one that declared it the resolver REFUSES "
        "outright (denied_axis=default-posture) — the first experience is an assistant that will not "
        "act, with no explanation."),
    "limit": (
        "WHAT THIS ROW DOES NOT ENFORCE, stated on the row rather than left to be discovered: a "
        "ledger row decides what an agent may be GRANTED. It does not contain an agent that already "
        "has a shell. The bwrap jail and the pre-tool guard are separate mechanisms, and a file on "
        "disk is readable by any process running as you whatever this ledger says."),
}

# ★ ONE REPRESENTATIVE PATH PER DECLARED ZONE, PLUS ONE THAT IS IN NO ZONE AT ALL.
#   The last entry is the point. `_expand_zones([])` returns None and None means UNRESTRICTED, so
#   "no territory declared" and "no reach" are DIFFERENT ANSWERS and a report that conflates them is
#   the one thing this probe must never do. Measuring a path outside every zone tells them apart on
#   the live store instead of assuming which one it is.
LEAD_ZONE_PROBES = (
    ("personal", "~/.ssh"),
    ("knowledge", "~/boardwalk/knowledge"),
    ("trading", "~/vault/trading"),
    ("stack", "~/boardwalk"),
    ("conversations", "~/.config/AionUi-Dev/aionui/aionui.db"),
    ("outside-every-zone", "/etc/shadow"),
)
LEAD_AGENT_ID = "pa-lead-de63a495"  # the row this box actually carries; the probe prefers ANY top-rank row


def _ledger_mod():
    """The clearance engine, IMPORTED from the repo — never re-declared here.

    ★ A SECOND ANSWER TO "WHAT MAY THIS AGENT REACH" IS THE TWO-TRUTHS DEFECT THIS ESTATE KEEPS
      PAYING FOR. `shaman_ledger` owns the resolution order (strike, then rank, then territory);
      this probe only asks it questions."""
    import importlib
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    return importlib.import_module("shaman_ledger")


def _read_access(path=None) -> dict:
    """The ONE access store, read fresh. RAISES when it cannot be read.

    ★ IT RAISES ON PURPOSE. The failure mode this file exists to prevent is a probe that cannot look
      and renders a ZERO instead — "0 agents" reads as a measurement, and it is not one. An
      exception here reaches `_probe_state`, which answers UNKNOWN and carries the reason."""
    p = Path(path) if path else (HOME / ".config" / "AionUi-Dev" / "khaoss-access.json")
    if not p.is_file():
        raise FileNotFoundError(f"no access store at {p}")
    return json.loads(p.read_text())


def _reach_of(SL, agent_id: str, cfg: dict) -> tuple:
    """(list of 'zone(op+op)', sorted ops) — the SET this agent id actually resolves to.

    Measured through the REAL resolver for all three ops, over one path per zone plus one path that
    is in no zone at all. Nothing here is inferred from the row's own text."""
    reached, ops = [], set()
    for label, raw in LEAD_ZONE_PROBES:
        got = [op for op in ("read", "execute", "write")
               if SL.check_access(agent_id, os.path.expanduser(raw), op, cfg).get("allowed")]
        if got:
            ops.update(got)
            reached.append(f"{label}({'+'.join(o[0] for o in got)})")
    return reached, sorted(ops)


def probe_lead_agent(reader=None) -> tuple:
    """Is there an agent row that can act for this user — and what does it actually reach?

    ★★ `reader` IS A NAMED SEAM, for the same reason `_probe_state` is one: the probe's INPUT can be
       replaced, so "the ledger could not be read" and "the ledger is empty" are branches that can be
       DRIVEN to red by a test rather than hoped about. The default is the real store.

    ★ IT NEVER RENDERS A BARE ZERO. Every path that cannot look returns its REASON in the evidence.
    """
    read = reader or _read_access
    cfg = read()
    SL = _ledger_mod()
    if not isinstance(cfg, dict):
        raise TypeError(f"the access store did not parse to an object ({type(cfg).__name__})")

    rows = SL.agents(cfg)
    # ── THE FRESH-INSTALL CASE: no row at all. This is the case the step exists for. ──────────────
    if not rows:
        nop = SL.check_access("_setup_walk_no_row_probe_", os.path.expanduser("~/boardwalk"),
                              "read", _no_posture(cfg))
        return "BLOCKED", (
            "no agent row in the access ledger's `agents` — NOTHING here can act. Measured on a copy "
            "of this store with the default posture removed (which is the fresh-install condition, "
            "the posture being install-scoped): a no-row agent resolving at "
            f"~/boardwalk -> allowed={nop.get('allowed')}, "
            f"denied_axis={nop.get('denied_axis', '-')}")

    # ── THERE IS A ROW. Which one holds the reach, and what is the SET it resolves to? ────────────
    rank_of = {aid: SL.rank_index(SL.rank_of(aid, cfg)) for aid in rows}
    top = max(rank_of, key=lambda a: (rank_of[a], a == LEAD_AGENT_ID))
    reached, ops = _reach_of(SL, top, cfg)
    # ★ "NO TERRITORY DECLARED" AND "NO REACH" ARE DIFFERENT ANSWERS. If a path in NO declared zone
    #   came back allowed, the row reaches the WHOLE BOX, and the evidence says so in those words
    #   rather than leaving the reader to infer it from a zone list that looks merely complete.
    everything = any(r.startswith("outside-every-zone") for r in reached)
    # ── AND THE OTHER HALF, MEASURED IN THE SAME BREATH: what a no-row agent gets HERE. ───────────
    try:
        import shaman_posture as SP
        prec, pwhy = SP.active(cfg)
    except Exception as e:  # noqa: BLE001 — could-not-look must be said, never counted as absent
        prec, pwhy = None, f"the posture module could not be read ({type(e).__name__})"
    if prec:
        nop = SL.check_access("_setup_walk_no_row_probe_", os.path.expanduser("~/boardwalk"),
                              "read", cfg)
        norow = (f"a no-row agent here: read allowed={nop.get('allowed')} via the declared posture "
                 f"{prec.get('_default_posture')} — and REFUSED on any install that posture does not "
                 f"name (install-scoped)")
    else:
        nop = SL.check_access("_setup_walk_no_row_probe_", os.path.expanduser("~/boardwalk"),
                              "read", cfg)
        norow = (f"a no-row agent: REFUSED, allowed={nop.get('allowed')} "
                 f"denied_axis={nop.get('denied_axis', '-')} ({pwhy})")
    return "DONE", (
        f"{len(rows)} agent row(s); lead {top} rank={SL.rank_of(top, cfg)} reaches "
        f"{len(reached)}/{len(LEAD_ZONE_PROBES)} probe path(s) [{', '.join(reached)}] "
        f"ops={'+'.join(ops)}"
        f"{' incl. a path in NO declared zone' if everything else ''} · {norow}")


def _no_posture(cfg: dict) -> dict:
    """A copy of the store with the declared default posture removed — the FRESH-INSTALL condition.

    ★ THIS IS HOW THE CLAUSE IS MEASURED RATHER THAN ASSERTED. The posture is install-scoped, so a
      new install of this repo has none in force; removing the record reproduces that condition
      in memory, and the resolver's own answer is what the row reports. Nothing is written."""
    import copy as _copy
    c = _copy.deepcopy(cfg)
    sh = c.get("_shaman")
    if isinstance(sh, dict):
        sh.pop("default_posture", None)
    return c


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  STEP 13 — THE PRIVACY GATE. ★ MEASURED BY DRIVING IT, NOT BY REMEMBERING IT.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# ★★★ 2026-10-12 — THIS PROBE USED TO REMEMBER, AND THAT IS EXACTLY WHY IT LIED. It read
#     `scrubber.py` for the words `placeholder|rehydrat|substitut` and, finding none, reported "no
#     placeholder to rehydrate round trip exists". That sentence was TRUE when it was written and
#     became FALSE the moment `jarvis/privacy_gate.py` landed — and NOTHING TOLD THIS ROW. It is the
#     tenth instance today of this estate's signature failure: **a status that describes the system
#     as it was, not as it is.** The lesson is not "update the string". It is that a probe which
#     greps an OLD file for the ABSENCE of a capability CANNOT NOTICE THE CAPABILITY ARRIVING
#     somewhere it was never looking. So this probe now DRIVES the gate and reports what it measured,
#     and it is built to go RED on the next read if the gate disappears, if the round trip stops
#     restoring, or if the running seat predates the gate module.
GATE_PY = REPO / "jarvis" / "privacy_gate.py"
ROUTER_PY = REPO / "jarvis" / "router.py"

# ★★ THE ROUND TRIP IS DRIVEN IN A SEPARATE INTERPRETER, AND EVERY REASON FOR THAT IS A SCAR:
#    · `privacy_gate.self_test()` PRINTS to stdout, and this probe runs inside the LONG-LIVED panel
#      process. A probe that writes to the served process's stdout is a side effect this walk's own
#      charter forbids ("Read-only. No probe may write").
#    · it gets a HARD TIMEOUT. `plan()` runs on every page read; one wedged probe must not wedge the
#      whole walk.
#    · the gate keeps an in-memory lease registry. Measuring it must not disturb the registry of the
#      process that is actually gating the operator's turns.
#    · IT IS HERMETIC. The classifier is handed in as `local=` — a synthetic verdict — so NO MODEL
#      and NO SOCKET are used. The values are FABRICATED (`example.invalid`): the operator's real
#      name, mail and phone are never part of this measurement and are never printed.
_GATE_DRIVER = r'''
import importlib.util, json, re, sys
spec = importlib.util.spec_from_file_location("pg_probe", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NAME, MAIL = "Probe Person", "operator@example.invalid"
original = "Ask %s at %s to call the clinic on Tuesday" % (NAME, MAIL)
DIRTY = {"available": True, "sensitive": True, "spans": [NAME], "rejected": [],
         "why": "synthetic classifier, handed in by the setup probe", "ms": 1}
lz = m.gate(original, "setup-probe", local=DIRTY)
sent = lz.sent_text or ""
back = lz.rehydrate(sent)
print("@@RESULT@@" + json.dumps({
    "substitutions": len(lz._map),
    "action": lz.action,
    "verdict": lz.verdict,
    "name_left_in_sent": NAME in sent,
    "mail_left_in_sent": MAIL in sent,
    "placeholder_in_sent": bool(re.search(r"\[\[[A-Z]+_[0-9a-f]{8}\]\]", sent)),
    "name_restored": NAME in back["text"],
    "mail_restored": MAIL in back["text"],
    "restored_count": back["restored"],
}))
'''
_DRIVER_MARK = "@@RESULT@@"


def _fingerprint(path: Path) -> str:
    """First 8 hex of a file's sha256 — a value to COMPARE across reads, never a secret."""
    try:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    except Exception:  # noqa: BLE001
        return "unreadable"


def _egress_call_sites() -> int:
    """How many places in `router.py` actually CALL the gate. ★ AN AST COUNT, NOT A LINE COUNT.

    ★★★ THE FIRST VERSION OF THIS FUNCTION COUNTED LINES CONTAINING THE CALL AND RETURNED 7 WHERE
      THERE ARE 6 — because `router.py`'s own comment above the definition MENTIONS the call while
      explaining it. That is this estate's recorded "a guard a comment could satisfy" defect, and it
      was about to overstate the wiring on the operator's own card. (`privacy_gate.self_test` moved
      the same kind of check to AST for the same reason: its first version went red because a
      DOCSTRING named the endpoint it was guarding against.) Counted from the parse tree, a comment
      cannot vote. ★ AND THE FIX WAS FOUND BY READING THE EVIDENCE STRING, not by a test — 7 was
      implausible against the 6 call sites `grep` showed, which is the only reason it was caught."""
    try:
        import ast
        tree = ast.parse(ROUTER_PY.read_text(errors="replace"))
    except Exception:  # noqa: BLE001
        return 0
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name)
                    else fn.attr if isinstance(fn, ast.Attribute) else None)
            if name == "_gate_egress":
                n += 1
    return n


def _drive_gate_round_trip(timeout: int = 30) -> tuple:
    """(True, facts) or (False, reason). ★ THE MEASUREMENT ITSELF: a real round trip, hermetic.

    ★ THE FAILURE THAT MATTERS IS THE LEAK, so it is named FIRST and it is not softened: if a real
      value survives into the text that would be sent, this returns False carrying THAT sentence."""
    rc, out, err = _run([sys.executable, "-c", _GATE_DRIVER, str(GATE_PY)], timeout=timeout)
    if rc != 0:
        tail = ((err or out).strip().splitlines() or [""])[-1]
        return False, f"the driver exited {rc}: {tail[:160]}"
    line = next((l for l in reversed(out.splitlines()) if l.startswith(_DRIVER_MARK)), None)
    if line is None:
        return False, "the driver printed no result line"
    try:
        facts = json.loads(line[len(_DRIVER_MARK):])
    except ValueError as e:
        return False, f"the driver's result was not JSON: {e}"
    if facts.get("name_left_in_sent") or facts.get("mail_left_in_sent"):
        return False, ("the REAL VALUE SURVIVED into the sent text — that is the leak this whole "
                       "step exists to prevent, not a cosmetic failure")
    if not facts.get("placeholder_in_sent"):
        return False, ("nothing was substituted — no placeholder of the gate's own shape reached "
                       "the sent text")
    if not (facts.get("name_restored") and facts.get("mail_restored")):
        return False, ("re-hydration did NOT restore both values — a gate without the return path "
                       "is a broken feature, not a private one")
    return True, facts


def _proc_start_epoch(pid: int) -> tuple:
    """(start_epoch, argv) read from /proc, or (None, reason). ★ NEVER `pgrep -f` on this box."""
    try:
        fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        btime = None
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None, "no btime in /proc/stat"
        ticks = float(os.sysconf("SC_CLK_TCK") or 100)
        argv = [a for a in Path("/proc/%d/cmdline" % pid).read_bytes()
                .decode("utf-8", "replace").split("\0") if a]
        return btime + start_ticks / ticks, argv
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def _running_daemons() -> list:
    """[(pid, start_epoch)] for every LIVE `jarvis/daemon.py`.

    ★★★ THE FILE IS NOT THE PROCESS. A previous lane learned this the hard way: `brain.py` was
      edited, the daemon was NOT respawned, and the running seat did not contain the change while
      every file-based check said it did. So the daemon is found by reading `/proc` and matching its
      ARGV EXACTLY, and its START TIME is compared against the source mtimes — a process that began
      after the file landed is a process that would load THIS build. `pgrep -f`/`pkill -f` are
      banned on this box (five shells died to a pattern matching their own command line) and neither
      appears anywhere in this file."""
    found, me = [], os.getpid()
    try:
        entries = list(Path("/proc").iterdir())
    except Exception:  # noqa: BLE001
        return found
    for ent in entries:
        if not ent.name.isdigit():
            continue
        pid = int(ent.name)
        if pid == me:
            continue
        started, argv = _proc_start_epoch(pid)
        if started is None or not argv:
            continue
        if any(a.endswith("jarvis/daemon.py") or a.endswith("/daemon.py") for a in argv[1:]):
            found.append((pid, started))
    return found


def _hhmmss(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def probe_value_substitution() -> tuple:
    """A5 — reversible substitution for egress legs. ★ MEASURED BY DRIVING THE GATE.

    ★★ THE RUNGS, NAMED, BECAUSE AN UNQUALIFIED "DONE" IS THE OVERCLAIM THIS ROW WAS REWRITTEN FOR:
        EXISTS  `jarvis/privacy_gate.py` is present and its public surface imports.
        WORKS   a synthetic turn carrying a NAME and an EMAIL is driven through `gate()`: the real
                values are asserted ABSENT from the sent text, a placeholder is asserted PRESENT,
                and re-hydration is asserted to restore BOTH. Plus the module's own suite, run in a
                second interpreter as an independent second signal.
        live    whether the RUNNING daemon postdates the source — because the file is not the
                process.
        USABLE  ★ NOT REACHED, AND NOT CLAIMED. No turn has been sent to a real cloud agent and no
                gated turn has gone through the daemon's HTTP surface, so nothing here says the
                round trip survives a REAL far side. An agent that rewrites a placeholder —
                markdown, wrapping, a smart quote — gets no value back; the gate COUNTS those rather
                than hiding them, and so does this row.
    """
    if not GATE_PY.is_file():
        return "STILL-BLOCKED-LATER", (
            f"jarvis/privacy_gate.py is absent from {REPO} — there is no placeholder to rehydrate "
            f"round trip, so nothing that crosses the Cloudflare rung can be recovered on the far "
            f"side. ★ Until it exists, rung 3 carries NO content the local rungs would not have "
            f"carried. Refuse, never degrade.")
    try:
        gate_size = GATE_PY.stat().st_size
        gate_mtime = GATE_PY.stat().st_mtime
    except OSError as e:
        return "UNKNOWN", f"privacy_gate.py is present but unreadable: {type(e).__name__}: {e}"

    ok, facts = _drive_gate_round_trip()
    if not ok:
        return "BLOCKED", (
            f"jarvis/privacy_gate.py is present ({gate_size} B) but THE ROUND TRIP DID NOT PASS: "
            f"{facts}. ★ This is BLOCKED rather than STILL-BLOCKED-LATER on purpose: the work is "
            f"HERE and it is not WORKING, and those are different sentences.")

    # -- the gate's OWN suite, as a second and independent signal ---------------------------------
    st_rc, st_out, st_err = _run([sys.executable, str(GATE_PY)], timeout=60)
    st_line = ((st_out or st_err).strip().splitlines() or [""])[-1][:110]
    if st_rc != 0:
        return "BLOCKED", (
            f"privacy_gate.py is present and its round trip passes, but THE GATE'S OWN SUITE IS RED "
            f"(exit {st_rc}: {st_line}). ★ A round trip that passes while the suite guarding it "
            f"fails means the suite is measuring something this probe is not — read the suite before "
            f"believing either.")

    sites = _egress_call_sites()
    if sites <= 0:
        return "BLOCKED", (
            f"jarvis/privacy_gate.py exists ({gate_size} B) and its round trip passes, but NOTHING "
            f"CALLS IT: an AST count finds 0 calls to the gate in router.py. A gate no egress path "
            f"calls is a library, not a gate — and this is the exact check that would have caught "
            f"`brain.py`-class wiring drift before a turn went out unguarded.")

    # -- the RUNNING process, not the file --------------------------------------------------------
    # ★★★ TWO FILES, TWO CONSEQUENCES, AND CONFLATING THEM WOULD BE A CRY-WOLF BUG.
    #   · `privacy_gate.py` IS the capability, and `router` imports it LAZILY (inside `_pg()`, on
    #     the first gated egress). A daemon that started before the module landed will still load
    #     the CURRENT file at its first egress — so this is the milder staleness, and it is the one
    #     this row turns RED on, because it is rare and it is exactly the named risk.
    #   · `router.py` holds the WIRING and IS imported eagerly at daemon start, so a router.py edit
    #     after the daemon started is the `brain.py` class precisely. But router.py is edited by a
    #     whole fleet of lanes and most of those edits do not touch the gate. Turning this row red on
    #     every unrelated router edit would teach the operator to ignore it — the "a warning that
    #     fires here has taught him to ignore the real one" defect that `test_voice_boundary` G3
    #     names. So it is REPORTED IN FULL, and it does not by itself turn the row red.
    daemons = _running_daemons()
    try:
        router_mtime = ROUTER_PY.stat().st_mtime if ROUTER_PY.is_file() else 0.0
    except OSError:
        router_mtime = 0.0

    if not daemons:
        live_ok = True
        live = ("no daemon is running, so no process can be carrying a stale gate — and none is "
                "claimed to be resident either")
    else:
        pid, started = min(daemons, key=lambda d: d[1])
        many = f" ({len(daemons)} daemon processes alive)" if len(daemons) > 1 else ""
        gate_age = started - gate_mtime
        bits = []
        if gate_age >= 0:
            live_ok = True
            bits.append(f"running daemon pid {pid}{many} started {_hhmmss(started)}, "
                        f"{int(gate_age)} s AFTER the gate module ({_hhmmss(gate_mtime)})")
        else:
            live_ok = False
            bits.append(f"★ THE RUNNING DAEMON PREDATES THE GATE MODULE: pid {pid}{many} started "
                        f"{_hhmmss(started)} but privacy_gate.py landed {_hhmmss(gate_mtime)}, "
                        f"{int(-gate_age)} s later — the file is not the process")
        router_age = started - router_mtime
        if router_age < 0:
            bits.append(f"★ AND router.py, which holds the wiring, was edited {int(-router_age)} s "
                        f"AFTER that daemon started ({_hhmmss(router_mtime)}), so the RUNNING seat "
                        f"does not hold the current router.py. Whether THAT edit touched the gate "
                        f"wiring is NOT measurable from here and this row does NOT claim it did — "
                        f"router.py is edited by many lanes, the {sites} gate call site(s) are "
                        f"present on disk now, and a respawn is what settles it")
        live = " · ".join(bits)

    measured = (
        f"★ rung WORKS: the round trip was DRIVEN, not remembered — a synthetic turn carrying a "
        f"name and an email produced {facts['substitutions']} placeholder(s), NEITHER real value "
        f"was in the sent text, a placeholder of the gate's own shape was, and re-hydration "
        f"restored BOTH ({facts['restored_count']} value(s))")
    selftest = f"the module's own suite: {'ALL PASS' if st_rc == 0 else 'FAILED'}"
    wired = (f"wired: {sites} call site(s) in router.py, counted from the parse tree "
             f"({_fingerprint(ROUTER_PY)}); gate {_fingerprint(GATE_PY)}")
    notmeasured = (
        "★ WHAT THIS DOES NOT MEASURE: no turn has been sent to a real cloud agent and no gated "
        "turn has gone through the daemon's HTTP surface, so rung USABLE is NOT reached and is not "
        "claimed — this row says the round trip works ON THIS BOX, not that it has survived a real "
        "far side")
    ev = " · ".join([measured, selftest, live, wired, notmeasured])

    if not live_ok:
        return "STILL-BLOCKED-LATER", (
            f"{ev}. ★ Built and passing, but THE SEAT THAT WOULD RUN IT PREDATES IT — the "
            f"capability is on disk and NOT in the running process until it is respawned.")
    return "DONE", ev


def probe_secrets_keeper() -> tuple:
    """The in-house keeper, reduced to the one question that decides ship/no-ship.

    ★★ THE QUESTION IS NOT 'does it store secrets'. It is **'can anything read a VALUE, and does
       anything consult a clearance before it does?'** Measured, not opined.

    ★★★ 2026-10-12 — THE PROBE NOW DRIVES THE VALUE PATH INSTEAD OF COUNTING READERS. It used to ask
       `enforcement_status()` for the number of runtime readers, and that number was a TEXT
       measurement whose blind spot made the whole verdict wrong: the scan ran `grep --include=*.py`,
       and the one value path on this box is `khaoss-secrets` — an executable with NO `.py`
       extension. So the gate could be WIRED IN and this row would STILL have told the operator his
       boundary was one chmod deep. He re-reads this step before he decides; a row that measures the
       wrong thing is worse than a row that says nothing. It now DRIVES two refusals and asserts the
       value channel stayed empty.

    ★★★ AND IT WAS 14.65 s, WHICH TOOK THE WHOLE WALK ROUTE DOWN (fixed same day). The walk renders on
       EVERY `/operator/setup/walk` request, so a probe costing fourteen seconds is a status page
       nobody can load — the operator could not read the row that was supposed to tell him the truth.
       MEASURED, the cost was NOT the obvious thing: the two `khaoss-secrets` spawns are **0.12 s**;
       the `enforcement_status()` repo-wide grep over **131,901 files** was the entire 14 s. So the
       scan is now requested OFF here (`scan=False`, 0.09 s) and the row is decided by the DRIVEN
       probe — which is the stronger evidence anyway.
    ★★ `scan=False` reports the reader count as UNKNOWN, NEVER as zero. If this row ever prints
       "readers: 0" it is because the scan ran and found none — not because it did not run."""
    store = HOME / ".khaoss-secrets"
    cli = REPO / "khaoss-secrets"
    if not store.is_dir():
        return "UNKNOWN", f"{store} absent"
    n = len([f for f in store.iterdir() if f.is_file() and not f.name.startswith(".")])
    mode_bad = [f.name for f in store.iterdir()
                if f.is_file() and not f.name.startswith(".") and (f.stat().st_mode & 0o077)]
    try:
        sys.path.insert(0, str(REPO / "operator"))
        import secret_meta  # noqa: PLC0415
        # ★ scan=False: the reader count is a CODE fact that changes when a FILE changes, not once
        #   per request. The DRIVEN gate probe below is what decides this row, and it is 0.09 s.
        enf = secret_meta.enforcement_status(scan=False)
        readers = enf.get("readers_calling_may_read")
        gate = enf.get("value_path") or {}
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"secret_meta unreadable: {type(e).__name__}: {e}"
    # ★ A COUNT OF `None` PRINTS AS "not scanned", NEVER AS 0.
    reader_txt = ("not scanned here (code fact; see `secret_meta.py --status`)"
                  if readers is None else str(len(readers)))
    ev = (f"{n} secret file(s); {len(mode_bad)} not 0600; runtime readers consulting the clearance "
          f"engine: {reader_txt}")
    if mode_bad:
        return "BLOCKED", ev + f" · LEAKY MODES on {mode_bad[:3]}"
    # ★★★ THE DECISION IS THE DRIVEN RESULT, NOT THE READER COUNT. An undriven gate is unproven, and
    #     a gate that refused while writing to stdout has leaked into the value channel while looking
    #     brave — so `stdout_bytes == 0` is asserted by the probe, not merely reported.
    if not gate.get("driven"):
        return ("UNKNOWN",
                ev + "  ★ the value path could NOT BE DRIVEN, so its gate is unmeasured. "
                     "Unmeasured is not safe. " + str(gate.get("sentence") or ""))
    if not gate.get("gated"):
        return ("BLOCKED",
                ev + "  ★ DECLARED ONLY — " + str(gate.get("sentence") or
                "the value path did not refuse an undeclared caller; file mode 0600 is the only thing "
                "stopping a read. See docs/UTILITIES_SUITE.md § THE SECRETS VERDICT."))
    return ("DONE", ev + "  ★ GATED — " + str(gate.get("sentence") or ""))


def _secrets_keeper_row_extra() -> dict:
    """★ THE ENFORCEMENT WORD ON THE STEP-2 ROW, MEASURED AT THE MOMENT THE ROW IS BUILT.

    ★ WHY THIS IS A HOOK AND NOT A STRING. `status: "NOT ENFORCING YET"` was correct when the value
      path consulted nothing. It is a claim about the WORLD, and every claim about the world on this
      walk has to be re-read when the row is rendered — that is what `row_extra` is for ("a step may
      carry a `row_extra()` returning keys that must not be frozen at import time").
    ★ IT FAILS CLOSED TO THE OLD WORD. If the engine cannot be reached the row says NOT ENFORCING YET,
      which is the pessimistic answer — never the flattering one."""
    try:
        sys.path.insert(0, str(REPO / "operator"))
        import secret_meta  # noqa: PLC0415
        g = secret_meta.value_path_probe()
        gated = bool(g.get("gated"))
        return {"status": "ENFORCING" if gated else "NOT ENFORCING YET",
                "value_path_gated": gated,
                "value_path_driven": bool(g.get("driven")),
                "value_path_stdout_bytes": g.get("stdout_bytes")}
    except Exception as e:  # noqa: BLE001
        return {"status": "NOT ENFORCING YET", "value_path_gated": False,
                "value_path_error": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE VOICE BOUNDARY -- the requirement, stated ONCE, plus the WARNING THAT IS NOT A BLOCK.
#
#  THE OPERATOR, 2026-10-12, VERBATIM:
#    "if we work with gev we need to make sure audio doesn't leave. good catch and let's at least
#     make the requirements a part of the install. This is for people who may have agents working
#     already that compromise voice. If they do that, or choose to do that, we just need a warning
#     for them really. It's their choice but ill advised. We want our system to do the voice stuff
#     and spit it out as TEXT for the integration, no matter what it is."
#
#  THE DESIGN -- and this is the thing that makes the requirement SATISFIABLE rather than
#  aspirational:
#
#      microphone --> THE SEAT (STT happens HERE, on this box) --> TEXT
#                                                                   |
#                                        +--------------------------+---------------------+
#                                        v                                                v
#                                  local model                                 whatever integration
#                                  (ollama)                                    (GEV, your agents,
#                                                                               any cloud model,
#                                                                               a tool nobody has
#                                                                               written yet)
#                                  * ALL of these receive TEXT and only TEXT
#
#  WHAT THAT MEANS CONCRETELY: *GEV's own voice feature is NOT the path we use.* GEV's hands-free
#  control connects the browser DIRECTLY to OpenAI's Realtime API over WebRTC and streams raw
#  microphone audio there. Our path is: the operator speaks to OUR seat, the seat transcribes
#  locally, and the resulting TEXT is what GEV -- or anything else -- acts on. That is the difference
#  between a requirement this estate can KEEP and one it can only STATE.
#
#  "NO MATTER WHAT IT IS" -- so this is NOT a GEV rule. Every downstream integration is a TEXT
#  consumer. This constant is the ONE home of that statement: the walk renders it, and the GEV card
#  (`operator/gev_option.py`) POINTS AT IT rather than keeping a second copy that goes stale.
#
#  AND THE WARNING. A first-install user may already have agents on the box that route audio off it.
#  They may keep them. The estate's rule is "It may ASK. It may NOT TAKE." -- so this WARNs and never
#  refuses, and a warning that is not SHOWN is not a warning, which is why the page renders the block
#  below ABOVE the step list, outside every filter.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

VOICE_BOUNDARY = {
    "statement": (
        "Voice never leaves this machine. Your seat transcribes locally and hands TEXT to every "
        "integration \u2014 GEV, your agents, any cloud model. Nothing downstream ever receives audio."
    ),
    "stt_runs": (
        "The speech-to-text itself runs ON THIS BOX: `jarvis/speech.py` transcribes locally (the "
        "resident whisper model, or voicebox on 127.0.0.1:8770, or whisper-server on 127.0.0.1:8772), "
        "and the daemon logs the resulting SENTENCE. The audio's only journey is into this machine."
    ),
    "crosses": (
        "TEXT. When the seat hands a turn to an integration it sends a string and nothing else \u2014 "
        "measured at the transport, not asserted: the delivery tool builds its request body as "
        "{\"conversation_id\": ..., \"input\": <the sentence>} and writes a plain prose row into the "
        "conversation. GEV, a Hermes agent, a cloud model and a tool that does not exist yet all sit "
        "on the same side of this line, because all four receive the same shape."
    ),
    "never_crosses": (
        "AUDIO \u2014 and it is not a setting. `jarvis_seat.egress_allowed(\"audio\")` answers "
        "allowed:False, hard:True, unconditionally, with no override parameter in existence, and the "
        "seat's own self-test refuses with C4 BREACH if that ever changes. The seat is also READ-ONLY "
        "on audio devices by recorded ruling. What is KEPT on this box is a WAV per deliberate "
        "utterance under var/heard/ (owner-only, ring of 20) so that a claim about the ears can be "
        "replayed instead of argued about \u2014 a file on disk, never a payload on a wire."
    ),
    "gevs_own_voice": (
        "GEV's own hands-free voice would go browser to OpenAI over WebRTC, which is exactly the "
        "thing this rule forbids \u2014 so we do not use it. Under this design you give GEV TEXT, not "
        "your microphone. Its voice half is LATENT on this box (the credential it needs is absent), "
        "and the doctrine would keep it off even if the credential were there."
    ),
    "why_satisfiable": (
        "A rule you cannot keep is worse than no rule, because it is a promise the product breaks. "
        "This one is keepable because the seat OWNS the microphone and the transcription, and every "
        "integration is downstream of the text. Nothing has to be trusted to behave: there is no code "
        "path on this box that carries a microphone buffer to a third party."
    ),
    # * THE HONEST LIMITS, AND THEY ARE PART OF THE DISCLOSURE RATHER THAN A FOOTNOTE. An
    #   undetectable compromise reported as detected would be worse than silence, so the warning
    #   below states what it cannot see in the same breath as what it can.
    "cannot_detect": (
        "Audio captured and sent BETWEEN two polls. This reads sockets and capture clients at one "
        "instant; it does not sniff packets, so a voice agent that opens the microphone for 200 ms "
        "and posts it is NOT caught.",
        "WHICH peer a TLS connection is talking to. We never resolve or inspect traffic, so all we "
        "can say is that an outbound connection exists \u2014 which is why an outbound connection "
        "ALONE is not a warning here (a healthy box has many).",
        "A cloud voice agent that is installed but is not running and holds no credential we know "
        "about. That is exactly the state God's Eye View is in, and it is why its row says LATENT "
        "rather than ACTIVE.",
        "A browser that has been GRANTED microphone permission. Firefox keeps that in "
        "permissions.sqlite and we do not read it: a granted permission is not a capture, and "
        "reading it would fire on the operator's own browser.",
        "An agent inside a container, a namespace, or another user's session. We can see our own "
        "process's sockets and the capture clients the sound server reports, and nothing else.",
        "A SECOND MACHINE. This measures THIS box; it cannot see a phone or another computer with "
        "its own microphone.",
    ),
}

# ── THE DETECTORS. Each is read-only, bounded, and INJECTABLE so its loud branch can be driven
#    without opening the microphone (which this lane is forbidden to do). -------------------------

def _mic_holders(capture_text: str | None = None) -> tuple:
    """(names, reason) -- which applications are CAPTURING the microphone RIGHT NOW.

    * WHAT THIS MEASURES: the sound server's own list of active capture streams. It is the one
      question about the microphone that can be asked without touching the microphone.
    * WHAT IT CANNOT: a permission, a capability, or an intention. It sees a stream that EXISTS.
    * `capture_text` IS THE FIXTURE SEAM. Every parser in this file that can only be exercised by a
      state the box is not in has one, because a loud branch nobody ever ran is a branch nobody knows
      works. It takes the same text `pactl` prints, so the fixture and the live read go through ONE
      parse."""
    if capture_text is None:
        rc, out, err = _run(["pactl", "list", "source-outputs"], timeout=6)
        if rc != 0:
            return None, f"pactl could not list capture streams: {(err or out).strip()[:120]}"
        capture_text = out
    names = []
    for block in capture_text.split("Source Output #")[1:]:
        app = binary = ""
        for line in block.splitlines():
            m = re.match(r'\s*application\.name\s*=\s*"?([^"]*)"?\s*$', line)
            if m and not app:
                app = m.group(1).strip()
            m2 = re.match(r'\s*application\.process\.binary\s*=\s*"?([^"]*)"?\s*$', line)
            if m2 and not binary:
                binary = m2.group(1).strip()
        names.append(f"{app} ({binary})" if app and binary else (app or binary or "unnameable client"))
    return names, ""


def _outbound_peers(conn_text: str | None = None) -> tuple:
    """(rows, local_hosts, reason) -- ESTABLISHED connections, and which of this box's own addresses
    they terminate on.

    * THE COLUMNS ARE READ FROM THE HEADER, NOT ASSUMED, AND THAT IS A MEASURED SCAR. `ss -tnp state
      established` prints NO `State` column (the filter already said it) while plain `ss -tnp` prints
      one, so the local/peer columns are 2/3 in the first case and 3/4 in the second. Hard-coding
      either one silently reads the LOCAL address as the peer -- which is exactly what this function's
      own first fixture did, and it produced an "outbound connection" whose peer was this box talking
      to itself. The header names the columns, so the header decides. An unrecognised header is
      UNKNOWN, never a guess.
    * THE LOCAL-ADDRESS SET COMES FROM THE SAME READ, and that is deliberate: a peer that is one of
      our own addresses has not left the box (this box talks to its own headscale on its LAN
      address), and calling that a leak would be the false alarm that teaches the operator to ignore
      the warning. Deriving the set from the connection table costs no second command."""
    if conn_text is None:
        rc, out, err = _run(["ss", "-tnp", "state", "established"], timeout=8)
        if rc != 0:
            return None, set(), f"ss could not list connections: {(err or out).strip()[:120]}"
        conn_text = out
    lines = [l for l in conn_text.splitlines() if l.strip()]
    if not lines:
        return [], set(), ""
    toks = lines[0].split()
    # * THE HEADER'S TOKENS ARE NOT THE DATA'S COLUMNS, AND THAT IS THE SECOND HALF OF THE SCAR:
    #   `Local Address:Port` is ONE column in the data and TWO whitespace-separated tokens in the
    #   header. Counting header tokens would put the peer one column to the left of the truth. So the
    #   pairs are MERGED first, and only then are the columns named.
    cols, i = [], 0
    while i < len(toks):
        if toks[i] in ("Local", "Peer") and i + 1 < len(toks) and toks[i + 1].startswith("Address:Port"):
            cols.append(toks[i] + " " + toks[i + 1])
            i += 2
        else:
            cols.append(toks[i])
            i += 1
    local_col = next((n for n, c in enumerate(cols) if c.startswith("Local")), None)
    peer_col = next((n for n, c in enumerate(cols) if c.startswith("Peer")), None)
    if local_col is None or peer_col is None:
        return None, set(), (f"ss printed a header this parser does not recognise ({lines[0][:80]!r}) "
                             f"-- refusing to guess which column is the peer")
    rows, local = [], set()
    for line in lines[1:]:
        parts = line.split()
        if len(parts) <= max(local_col, peer_col):
            continue
        local.add(parts[local_col].rsplit(":", 1)[0])
        peer = parts[peer_col]
        host = peer.rsplit(":", 1)[0]
        if host in local or host.startswith("127.") or host in ("[::1]", "::1"):
            continue
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        rows.append({"peer": peer, "process": (m.group(1) if m else "unowned"),
                     "pid": (m.group(2) if m else "")})
    # a peer whose address we only meet AFTER it was recorded is still ours if it appears as a local
    # address anywhere in the same table; re-filter once so row ordering cannot decide the answer
    return [r for r in rows if r["peer"].rsplit(":", 1)[0] not in local], local, ""


def _same_app(a: str, b: str) -> bool:
    """Is capture-client `a` the same application as socket-owner `b`?

    * THE MATCHING RULE IS NAMED BECAUSE IT IS THE WEAK LINK. The sound server reports a display name
      ("Firefox") and the kernel reports a process name ("firefox-esr"), so equality does not work
      and never will. The rule is: normalise both, and call them the same app when one contains the
      other AND the shared part is at least 3 characters. It is deliberately CONSERVATIVE -- a
      mismatch under-warns by one row, while a loose match invents a leak and costs the warning its
      credibility."""
    def _n(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", str(s or "").lower())
    na, nb = _n(a), _n(b)
    if not na or not nb:
        return False
    for long_, short_ in ((na, nb), (nb, na)):
        if len(short_) >= 3 and short_ in long_:
            return True
    return False


def detect_audio_egress(capture_text: str | None = None, conn_text: str | None = None,
                        gev=None) -> dict:
    """Can something on this box carry audio off it? A WARNING, never a refusal.

    * THREE DETECTORS, and they are a CONJUNCTION where it matters:
        D1  a process HOLDING THE MICROPHONE right now.
        D2  a known cloud-voice agent installed WITH a credential for a cloud speech API. Installed
            alone is not a detection -- that is the LATENT state, and reporting it as a leak would be
            the false alarm this whole design is trying not to be.
        D3  an application that is BOTH holding the microphone AND holding an outbound connection.
            That is the shape of a live leak, and it is the one that fires on the ACT rather than on
            the potential.
    * SILENCE IS NOT THE DEFAULT. `state` is "quiet" ONLY when every detector actually ran. A
      detector that could not look yields "unknown" with the reason, because "I could not look" and
      "I looked and it was clean" must never be the same answer (COWORK_PROTOCOL.md 2.7)."""
    detected, checked, unmeasured = [], [], []

    checked.append("D1 microphone capture holders (pactl list source-outputs)")
    holders, why = _mic_holders(capture_text)
    if holders is None:
        unmeasured.append(f"D1 could not look: {why}")
    elif holders:
        detected.append({"who": ", ".join(holders), "what": "holds the microphone right now",
                         "evidence": f"{len(holders)} active capture stream(s): {holders}"})

    checked.append("D3 microphone holder with an outbound connection (D1 x ss -tnp state established)")
    peers, local, why2 = _outbound_peers(conn_text)
    if peers is None:
        unmeasured.append(f"D3 could not look: {why2}")
    elif holders:
        remote = [p for p in peers if p["peer"].rsplit(":", 1)[0] not in local]
        for h in holders:
            hit = [p for p in remote if _same_app(h, p["process"])]
            if hit:
                detected.append({
                    "who": h, "what": "is HOLDING THE MICROPHONE and holds an outbound connection",
                    "evidence": (f"{h} owns {len(hit)} established connection(s) off this box, e.g. "
                                 f"{hit[0]['process']} (pid {hit[0]['pid']}) to {hit[0]['peer']}")})

    checked.append("D2 a known cloud-voice agent installed WITH a cloud speech credential "
                   "(God's Eye View, read through operator/gev_option.py)")
    g = GEV if gev is None else gev
    if g is None:
        unmeasured.append("D2 could not look: operator/gev_option.py did not import "
                          f"({_GEV_IMPORT_ERROR})")
    else:
        try:
            keys = g.key_report()
            repo = g.find_checkout()
            installed = bool(repo and repo[0])
            armed = keys.get(g.VOICE_KEY, "absent") != "absent"
            if installed and armed:
                detected.append({
                    "who": "God's Eye View", "what": ("is installed and holds a credential for a "
                                                      "cloud speech API"),
                    "evidence": (f"checkout present and {g.VOICE_KEY}: "
                                 f"{keys.get(g.VOICE_KEY)} -- its voice path could start")})
            elif installed:
                checked.append("D2 looked: God's Eye View is present and its voice credential is "
                               f"ABSENT ({g.VOICE_KEY}: {keys.get(g.VOICE_KEY)}) -- LATENT, not active")
        except Exception as e:  # noqa: BLE001 -- a detector that dies is UNKNOWN, never clean
            unmeasured.append(f"D2 could not look: {type(e).__name__}: {e}")

    state = "warn" if detected else ("unknown" if unmeasured else "quiet")
    if state == "warn":
        line = (f"{len(detected)} thing(s) on this box may route audio off it: "
                + "; ".join(d["who"] for d in detected)
                + ". THIS IS A WARNING, NOT A BLOCK \u2014 you may proceed, and it is your call. "
                  "The seat's own voice path is unaffected: your microphone still becomes text here.")
    elif state == "unknown":
        line = ("the boundary could not be fully measured on this box: " + "; ".join(unmeasured)
                + ". Nothing was found, but NOTHING WAS RULED OUT either.")
    else:
        line = ("measured quiet: no process is holding the microphone, and no known cloud-voice "
                "agent on this box is installed with a credential that would let it reach a speech "
                "API.")
    return {"state": state, "line": line, "detected": detected, "checked": checked,
            "unmeasured": unmeasured}


def probe_voice_boundary() -> tuple:
    """(state, evidence, extra) for the walk row -- TWO FACTS, ONE ROW, like `probe_phone_step`.

    The first fact is the BOUNDARY (where STT runs, and that audio has no outbound path). The second
    is the WARNING about everything else on the box. They are one row because they are one decision
    for the person reading it, and the second is the one that changes without this install doing
    anything.

    * THE WALK STATE IS DONE WHEN THE WARNING IS LOUD, AND THAT IS NOT A BUG. The requirement is
      about OUR seat, and our seat is what it says it is; the warning is about something ELSE the
      operator installed. Reporting BLOCKED would assert the install is blocked, which is false, and
      an advisory that blocks is exactly the thing he ruled out. The warning rides on the row so the
      page can shout without the walk refusing anything."""
    ok, st = _http(DAEMON + "/state")
    if ok:
        # ★★★ MEASURED 2026-10-12: THE BACKEND IS NOT AT THE TOP LEVEL. `/state.asr_backend` is
        #     **None** on this box; the live value is at `/state.phrases.asr_backend` (measured:
        #     `'auto'`). The old line read the top level, got None, and fell back to `'?'` — so the
        #     row served `asr backend '?'` while the daemon had a real answer. THAT IS THE SAME
        #     DEFECT CLASS AS THE STEP-13 PROBE THIS LANE FIXED: reading the value from the wrong
        #     place and reporting a shape that looks plausible instead of the measurement. It was
        #     found by the `voice_boundary` lane, which correctly refused to edit a file it did not
        #     own. `G11` in `test_step13_probe.py` now fails if this reads the wrong place again.
        asr = str((st.get("phrases") or {}).get("asr_backend") or "?")
        boundary = f"STT on this box (asr backend {asr!r}) \u00b7 audio egress refused hard"
    else:
        asr = "?"
        boundary = ("the daemon's /state was unreachable, so the live STT backend could NOT be read "
                    "\u2014 the rule is the seat's own constant, and a constant is not a measurement")
    listening, where = _listening(8770)
    if listening:
        loop = str(where).startswith("127.") or str(where).startswith("[::1]")
        boundary += f" \u00b7 voicebox {where}" + ("" if loop else "  ** NOT LOOPBACK")
    det = detect_audio_egress()
    ev = (f"{boundary} \u00b7 crossing the boundary: TEXT only \u00b7 "
          f"warning: {det['state']} \u2014 {det['line']}")
    state = "DONE" if det["state"] in ("quiet", "warn") else "UNKNOWN"
    return state, ev, {"voice_detection": det,
                       "voice_warning": det if det["state"] == "warn" else None,
                       "voice_boundary_state": det["state"]}


def voice_boundary_block(rows) -> dict:
    """The requirement as the PAGE renders it: the static statement plus the LIVE detection.

    * IT IS BUILT FROM THE ROW THAT WAS ALREADY MEASURED, never by probing a second time. Two reads
      of one fact drift, and the page could then show a warning the walk row does not carry.
      `plan()` calls this after the probes have run, so there is exactly ONE measurement."""
    det = None
    for r in rows:
        if r.get("id") == "voice_boundary":
            det = r.get("voice_detection")
            break
    if det is None:
        det = {"state": "unknown", "line": "the boundary step did not run, so nothing was measured",
               "detected": [], "checked": [], "unmeasured": ["the voice_boundary step is absent"]}
    return {
        "statement": VOICE_BOUNDARY["statement"],
        "stt_runs": VOICE_BOUNDARY["stt_runs"],
        "crosses": VOICE_BOUNDARY["crosses"],
        "never_crosses": VOICE_BOUNDARY["never_crosses"],
        "gevs_own_voice": VOICE_BOUNDARY["gevs_own_voice"],
        "why_satisfiable": VOICE_BOUNDARY["why_satisfiable"],
        "cannot_detect": list(VOICE_BOUNDARY["cannot_detect"]),
        "state": det["state"], "line": det["line"], "detected": det["detected"],
        "checked": det["checked"], "unmeasured": det["unmeasured"],
    }


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE STEPS, IN WIZARD ORDER. Order is a deliverable, not a preference.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
# ★ Every row carries BOTH approval paths (`typing`, `button`) and a `say`. A row without them is a
#   dead end, and a dead end is the thing he keeps finding. The selftest REFUSES such a row by name.

STEPS: tuple[dict, ...] = (
    {
        "id": "territory",
        "felt": "What this assistant is allowed to touch",
        "technical": "the territory grant — zones in khaoss-access.json `_shaman.zones`",
        "status": "DESIGN-ONLY",
        "owner": "operator",
        "probe": probe_territory,
        "why": ("Chosen AT SETUP, not discovered later. Five zones exist — personal, knowledge, "
                "trading, stack, conversations — and an agent needs an explicit rule (rank + "
                "territory) to enter one; there is no ambient cross-zone access. ★ Declaring a zone "
                "is NOT enforcing it: nothing consults these labels on a read path today, and this "
                "row says so rather than implying protection it cannot demonstrate."),
        "typing": "set my territory to stack and knowledge",
        "button": {"label": "⚖ Choose territory", "action": "open:access"},
        "say": ("Your territory is not set yet. Territory is which parts of the machine this "
                "assistant may reach — personal, knowledge, trading, stack or conversations — and "
                "you pick it now rather than later, because an assistant that inherits everything "
                "has already been given everything. Say \u201cset my territory to stack and "
                "knowledge\u201d, or press Choose territory beside this message. Tell me the zones "
                "and I will write them to the access registry, then read the page back to you so "
                "you can see exactly what you granted."),
        "next": "The zones you pick are written to the access registry and shown on the access page.",
        "capability": None,
    },
    # ★★★★★ THE LEAD AGENT — ADDED 2026-10-14. THE SUGGESTION THE OPERATOR ASKED FOR, AND WHY IT IS
    #     A SECOND ROW RATHER THAN A SENTENCE ON THE TERRITORY ROW.
    #       His words: "Support Team needs full access and capabilities. ... It should be suggested to new
    #       installers/new users to have one."
    #     Territory and the lead agent are two halves of ONE decision — which parts of the machine may
    #     be reached, and WHO holds that reach. Putting the second half inside the first row would put
    #     two approvals on one card, which is the defect `swTerms` and the GEV row already record.
    #     ★ IT IS INSERTED HERE AND NOT AT THE END, and the proof that this is safe is in `prove_fail`:
    #       M3/M4/M6 used to address rows BY INDEX (0, 1, 4). They now address them BY ID, so a row
    #       inserted anywhere can no longer silently re-aim a guard written for a different row — the
    #       hazard the `voice_boundary` comment warned about. M3's `[:-1]` still drops the LAST step
    #       (`gev_option`), which is why this row is NOT appended at the end: appending a DONE row
    #       there would have made M3's set-equality mutation pass for the wrong reason.
    #     ★ IT IS A SUGGESTION. `state` (what the measurement found on this box) and `status` (whether
    #       the mechanism behind it enforces anything) are TWO AXES, and both are labelled wherever
    #       they render — a row showing `BLOCKED` beside `ENFORCING` with neither axis named is honest
    #       and unreadable, and this file will not add another one.
    {
        "id": "lead_agent",
        "felt": "Who is allowed to act for you",
        "technical": ("a lead agent row in khaoss-access.json `agents` — rank + capability, resolved "
                      "by shaman_ledger.check_access (strike, then rank, then territory)"),
        "status": "ENFORCING",
        "owner": "operator",
        "probe": probe_lead_agent,
        "why": (LEAD_AGENT["clause"] + " " + LEAD_AGENT["limit"] +
                " ★ Measured on this box rather than asserted: {value}"),
        "typing": "create a lead agent with full territory",
        "button": {"label": "⚖ Create a lead agent", "action": "open:access"},
        "say": ("Give this assistant a LEAD AGENT — one agent row that holds broad capability — and "
                "route your work through it. You want one because " + LEAD_AGENT["clause"] +
                " I measured it here rather than assuming: {value} Say \u201ccreate a lead agent "
                "with full territory\u201d, or press Create a lead agent, and I will show you the "
                "row, its rank and the exact set of zones it reaches before anything is written — "
                "it may ask, it may not take."),
        "next": ("The row appears on the access page with its rank and the set of zones it resolves "
                 "to, and every agent you add afterwards can be delegated FROM it rather than "
                 "granted from nothing."),
        "capability": None,
    },
    {
        "id": "secrets_keeper",
        "felt": "Where your secrets live",
        "technical": "the in-house keeper (~/.khaoss-secrets + khaoss-secrets registry)",
        "status": "NOT ENFORCING YET",
        "owner": "operator",
        "probe": probe_secrets_keeper,
        "row_extra": _secrets_keeper_row_extra,
        # ★★★ THE STATUS WORD IS MEASURED, NOT WRITTEN DOWN (2026-10-12). This row used to assert in
        #     prose that the value path "does NOT consult the clearance engine ... so file mode 0600
        #     is the only thing stopping a read". That was TRUE and it was the finding. It is not true
        #     now, and the operator re-reads this step before he decides — so the claim lives in
        #     `{value}`, filled from the DRIVEN probe, and the row cannot go stale without the
        #     measurement going stale with it. The static prose states only what the row is ABOUT.
        "why": ("You asked for either your own keeper or the in-house one \u201cso long as it's safe "
                "to use\u201d. The in-house keeper stores REFERENCES AND METADATA in every surface "
                "you can see, and one value path exists: `khaoss-secrets resolve-for-inject`. "
                "Whether that path consults the clearance engine BEFORE it emits, and whether a "
                "refusal keeps the value channel empty, is driven and measured here rather than "
                "asserted: {value} Verdict and conditions are in docs/UTILITIES_SUITE.md."),
        "typing": "use my own secret keeper",
        "button": {"label": "🔑 Choose your keeper", "action": "open:utilities#secrets"},
        "say": ("For secrets you have two choices: the in-house keeper, or your own. Whether the "
                "in-house one is safe to use as a broker turns on one question \u2014 does its value "
                "path ask the clearance engine before it hands a value out, and does it refuse when "
                "the engine is not there to ask? I measured it rather than assuming: {value} "
                "If you would rather use your own keeper, say \u201cuse my own secret keeper\u201d "
                "and I will wire the reference and never store the value."),
        "next": "Whatever you choose, no value is ever typed into the chat and no value is logged.",
        "capability": None,
    },
    {
        "id": "model_tier",
        "felt": "How much brain to load",
        "technical": "the tier is applied through POST /model; ceiling is VOICE_LANE_CEILING_MB",
        "status": "ENFORCING",
        "owner": "operator",
        "probe": probe_model_tier,
        "why": ("A model is admitted only against a MEASURED resident figure under the lane ceiling; "
                "an unmeasured model fails CLOSED rather than being admitted on a guess. Choosing "
                "the tier is a setup decision because it is the one that decides what the box can "
                "still do while the assistant is thinking."),
        "typing": "switch to the minimal model tier",
        "button": {"label": "🧠 Choose model tier", "action": "open:models"},
        "say": ("Your model tier is {value}. Say \u201cswitch to the minimal tier\u201d for the "
                "smallest footprint, or \u201cstandard\u201d for more capability, and I will show "
                "you the measured resident cost against the ceiling before it loads."),
        "next": "The picker shows refusals with the daemon's own reason when a model will not fit.",
        "capability": None,
    },
    {
        "id": "mic_gate",
        "felt": "The microphone gate",
        "technical": "core.set_always_on_lock — one setter, arm-then-confirm release",
        "status": "ENFORCING",
        "owner": "operator",
        "probe": probe_mic_gate,
        "why": ("The lock refuses IN CORE, not in the widget, so the hotkeys and the button cannot "
                "disagree with it. ★ Recorded defect, still open (OPEN_ASKS A17): the seat can "
                "transcribe its OWN speech, because this box reports aec:false. The gate is real; "
                "the acoustic separation is not."),
        "typing": "unlock always-on listening",
        "button": {"label": "🎙 Release always-on", "action": "open:capture#mic"},
        "say": ("Always-on listening is locked, and the release is deliberately two keys: arm it, "
                "then type the words \u201cI accept always-on\u201d. Say \u201cunlock always-on "
                "listening\u201d and I will arm the case — but I will not type the confirmation for "
                "you, because that phrase exists so that a person says it. While it is locked, "
                "push-to-talk is the mode and there is zero standing exposure."),
        "next": "The mic indicator never disappears, in any state — a mic you cannot check is a defect.",
        "capability": None,
    },
    {
        "id": "speaker",
        "felt": "Whether you can hear it",
        "technical": "the default sink — the seat is READ-ONLY on audio by recorded ruling",
        "status": "ENFORCING",
        "owner": "operator",
        "probe": probe_speaker,
        "why": ("A muted or very quiet sink is indistinguishable from a broken assistant, and this "
                "has cost the estate time before. ★ The seat deliberately does NOT set the sink: "
                "pinning audio devices broke Bluetooth takeover on 2026-09-11, and that ruling "
                "stands. It reads, it reports, and it tells you the one command."),
        "typing": "turn the volume up",
        "button": {"label": "🔊 Volume & voice", "action": "open:settings#speaker"},
        "say": ("Your speaker is {value}. I do not change it myself — pinning the audio device "
                "broke Bluetooth takeover here once and that ruling still stands — but the fix is "
                "one command you can run: `pactl set-sink-mute @DEFAULT_SINK@ 0` and "
                "`pactl set-sink-volume @DEFAULT_SINK@ 100%`. Tell me when you have, and I will "
                "re-read the sink and say so out loud."),
        "next": "The volume and the voice list live together, so a silent assistant is diagnosable in one place.",
        "capability": None,
    },
    # ★★★★★ THE REQUIREMENT, AS A STEP -- ADDED 2026-10-12, AND WHY IT SITS EXACTLY HERE.
    #
    #     IT IS ATTACHED TO THIS POSITION: immediately after `mic_gate` (the microphone) and `speaker`
    #     (the reverse direction), and BEFORE every step that opens a channel (`ollama_loopback`,
    #     `headscale_control`, the cloudflared rung, and the GEV offer). The reason is the reader, not
    #     the taxonomy: the three voice steps are the only place a first-install user is already
    #     thinking about the microphone, and the rule has to be READ BEFORE he is invited to open a
    #     tunnel or add an integration -- a boundary stated after the doors is a boundary nobody
    #     reads. It also closes the voice cluster rather than starting a new one.
    #     ★ IT IS INSERTED AFTER `speaker` AND NOT BEFORE IT deliberately: the prove-fail's mutations
    #       M3/M4/M6 address rows 0, 1 and 4 BY INDEX, so inserting earlier would silently re-aim
    #       three existing guards at my row instead of the row they were written for.
    #     ★ THE STATEMENT ITSELF DOES NOT LIVE IN THIS DICT. It lives once in `VOICE_BOUNDARY` above
    #       and reaches the page through `plan()["voice_boundary"]`, so the page can render it in two
    #       places (above the walk, and on the GEV card) without a second copy existing.
    {
        "id": "voice_boundary",
        "felt": "Voice never leaves this machine",
        "technical": ("STT on this box (jarvis/speech.py -> local whisper/voicebox) and TEXT to every "
                      "integration -- GEV, agents, any cloud model"),
        "status": "ENFORCING",
        "owner": "system",
        "probe": probe_voice_boundary,
        "why": (
            "This is the requirement the whole voice lane is built on: audio enters the seat and "
            "never leaves it; only TEXT crosses. It is ENFORCING, and here is precisely WHAT refuses "
            "-- `jarvis_seat.egress_allowed('audio')` answers allowed:False, hard:True with no "
            "override parameter, and the seat's own self-test refuses the build with C4 BREACH if "
            "that ever changes; voicebox binds 127.0.0.1 and refuses a non-loopback stop channel by "
            "construction; and the seat is READ-ONLY on audio devices. \u2605 BE CLEAR ABOUT THE "
            "KIND OF GUARANTEE THIS IS: that constant is consulted on the SURFACES and asserted by "
            "the self-test, not by a transport -- because there is no transport carrying audio to "
            "refuse. The boundary holds by CONSTRUCTION (no code path carries a microphone buffer "
            "off this box), which is stronger than a gate, but it is a different claim and it is "
            "said here rather than blurred. \u2605 WHAT DOES NOT REFUSE: anything ELSE you install. "
            "An agent you already run, or add later, can open the microphone and post it, and this "
            "walk will not stop it \u2014 it will SAY SO. That is the whole design of the warning.")
        ,
        "typing": "where does my voice go",
        "button": {"label": "\u25b6 Where voice goes", "action": "recheck:voice_boundary"},
        "say": (
            "Your voice never leaves this machine. I transcribe here, on this box, and what I hand "
            "to anything downstream \u2014 GEV, an agent, a cloud model \u2014 is text, never audio. "
            "That is not a setting you can flip; the rule refuses unconditionally and my own "
            "self-test fails the build if it ever stops refusing. Say \u201cwhere does my voice "
            "go\u201d and I will re-measure it and read you the answer, including anything else on "
            "this machine that looks like it could carry audio off it. If something does, I will "
            "tell you plainly and leave it alone \u2014 it is your machine and your call."),
        "next": ("Pressing the button re-MEASURES; it never turns green because you pressed it. If the "
                 "warning is loud, it names what it saw and you may proceed \u2014 a warning that "
                 "refused would be taking the decision, and this walk only asks."),
        "capability": None,
    },
    {
        "id": "ollama_loopback",
        "felt": "The model server is not exposed to the room",
        "technical": "OLLAMA_HOST — *:11434 is the defect, 127.0.0.1:11434 is the fix",
        "status": "ENFORCING",
        "owner": "agent",
        "probe": probe_ollama_loopback,
        "why": ("Ollama was the ONE service on this box listening on every interface while voicebox "
                "(8770), the daemon (8771), whisper (8772) and Jev (8096) were all loopback-only. "
                "With `*:11434`, anything on the LAN — including a guest device — can reach your "
                "model server. ★ MEASURED 2026-10-10: the drop-in now reads 127.0.0.1 and the "
                "socket has moved. The fix is a one-shot script, so this is DONE and not ENFORCING "
                "BY CONSTRUCTION — nothing would notice if it drifted back."),
        "typing": "put the model server back on loopback",
        "button": {"label": "🔒 Fix loopback", "action": "grant:network.ollama-loopback"},
        "say": ("The model server is reachable from every interface on your network, which nothing "
                "else on this box is. Fixing it is one line in one file, and I need a timed grant "
                "for exactly that: say \u201cput the model server back on loopback\u201d and I will "
                "ask for the grant, show you the sudoers line before it is issued, and verify the "
                "socket actually moved afterwards rather than reporting success."),
        "next": "Verification is the socket state, not the file's contents — a fix that does not verify is a silent no-op.",
        "capability": "network.ollama-loopback",
    },
    {
        "id": "headscale_control",
        "felt": "Your own private network exists",
        "technical": "headscale v0.29.3 control plane — khaoss-headscale.service (systemd user)",
        "status": "ENFORCING",
        "owner": "system",
        "probe": probe_headscale_control,
        "why": ("Rung 2 of the transport ladder. The Tailscale SaaS tailnet was RETIRED on "
                "2026-09-21 and replaced with our own control plane, which means the mesh is yours "
                "and nobody else's — but a self-hosted control server is also a thing you must keep "
                "alive, and it is listening on every interface rather than loopback."),
        "typing": "check the private network",
        "button": {"label": "🕸 Check headscale", "action": "open:transport"},
        "say": ("Your private network control plane is running. Nothing is blocked here — I am "
                "showing it because the next step cannot work without it, and because it is "
                "listening on every interface while every other service on this box is loopback "
                "only. Say \u201ccheck the private network\u201d any time and I will re-read it."),
        "next": "The control plane being up is not the mesh being used; the next step is the device.",
        "capability": None,
    },
    {
        "id": "headscale_phone",
        "felt": "Your phone, on your own network",
        "technical": "a second headscale node — the Pixel 7 over WireGuard",
        "status": "NOT ENFORCING YET",
        "owner": "operator",
        "probe": probe_phone_step,
        "why": ("★ THE MESH HAS EXACTLY ONE NODE: this box, talking to itself. Measured, peer count "
                "0. So rungs 1 and 2 of the transport ladder are configured and have never carried "
                "a second device — everything about them is from configuration, not from a phone "
                "that has ever connected. This is why it is STILL-BLOCKED-LATER rather than DONE: "
                "no amount of work on this box moves it."),
        "typing": "how do I add my phone",
        "button": {"label": "📱 Add my phone", "action": "open:transport#phone"},
        "say": ("This one needs you and the phone, and there is nothing I can do from here. Install "
                "the Tailscale app on the phone, choose \u201cUse an alternate server\u201d, and "
                "enter your headscale URL. I will mint you a preauth key — say \u201cgive me a "
                "preauth key\u201d — and the key is read from the vault and shown to you, never "
                "printed into this conversation. When the phone appears, tell me and I will re-read "
                "the node list and confirm it."),
        "next": "Once one peer exists, the ladder's first two rungs can finally be tested end to end.",
        "capability": None,
    },
    {
        "id": "cf_cert",
        "felt": "Telling Cloudflare who you are",
        "technical": "cloudflared tunnel login → ~/.cloudflared/cert.pem (browser act)",
        "status": "ENFORCING",
        "owner": "operator",
        "probe": probe_cf_cert,
        "why": ("★ THIS IS A CREDENTIAL ACT AND IT IS YOURS. `tunnel login` authenticates as you "
                "against your Cloudflare account in a browser; the estate's hard gate is \u201cnever "
                "log key material\u201d, and a tunnel credential is not a value, it is authority to "
                "publish a route into this machine. I will not hold it, read it, or read your "
                "browser's password store. Three commands need this certificate; none of them need "
                "root."),
        "typing": "I ran the tunnel login",
        "button": {"label": "▶ Show me the login steps", "action": "open:utils#cf"},
        "say": ("The tunnel is not authorised yet, and I cannot do this part for you. In a "
                "terminal, run `cloudflared tunnel login` and pick example.invalid. If your "
                "browser downloads the certificate instead of handing it back — which is what "
                "happened last time — save it to `~/.cloudflared/cert.pem`; the folder already "
                "exists. Then say \u201cI ran the tunnel login\u201d and I will check for the "
                "certificate, create the tunnel, route the name, and verify a connector."),
        "next": "With cert.pem present, `tunnel create` and `route dns` need no root and I can run them.",
        "capability": None,
    },
    {
        "id": "cf_tunnel",
        "felt": "Creating the way in",
        "technical": "cloudflared tunnel create khaoss → ~/.cloudflared/<UUID>.json",
        "status": "NOT ENFORCING YET",
        "owner": "agent",
        "probe": probe_cf_tunnel,
        "why": ("Quick tunnels are RETIRED on this panel (it answers HTTP 410), so the NAMED tunnel "
                "is the only path. This step needs the certificate and needs no root, so it is mine "
                "to run the moment you authorise the machine — it is not another thing for you to "
                "type."),
        "typing": "create the tunnel",
        "button": {"label": "▶ Create tunnel", "action": "step:cf_tunnel"},
        "say": ("No tunnel exists yet. Create the machine authorisation first by running "
                "`cloudflared tunnel login`; once `~/.cloudflared/cert.pem` exists I will run "
                "`cloudflared tunnel create khaoss` myself, because it needs no privilege and no "
                "browser. Say \u201ccreate the tunnel\u201d when the certificate is in place."),
        "next": "The credentials file is written to ~/.cloudflared/<UUID>.json and stays on this box.",
        "capability": None,
    },
    {
        "id": "cf_route",
        "felt": "Giving it a name",
        "technical": "cloudflared tunnel route dns khaoss seat.example.invalid",
        "status": "NOT ENFORCING YET",
        "owner": "agent",
        "probe": probe_cf_route,
        "why": ("The step people miss. A tunnel with no DNS name cannot be reached by anything. The "
                "name must stay under example.invalid, and the ingress publishes exactly ONE "
                "hostname to http://127.0.0.1:8771 with a 404 catch-all — never the panel, never an "
                "admin surface, never a credential endpoint."),
        "typing": "route the name",
        "button": {"label": "▶ Route the name", "action": "step:cf_route"},
        "say": ("The tunnel has no hostname yet, so nothing could reach it. Once it exists I will "
                "run `cloudflared tunnel route dns khaoss seat.example.invalid`. If you would "
                "rather use a different name, write it to `~/.khaoss-tunnel-hostname` first — that "
                "file exists because sudo sanitises the environment, so an environment variable "
                "would have silently refused every time."),
        "next": "Only one hostname is published, and a 404 catch-all refuses anything unlisted.",
        "capability": None,
    },
    {
        "id": "cf_grant",
        "felt": "Permission to install the tunnel service",
        "technical": "capability network.cloudflare-tunnel → /etc/sudoers.d/khaoss-cloudflare-tunnel",
        "status": "ENFORCING",
        "owner": "operator",
        "probe": lambda: probe_grant("network.cloudflare-tunnel"),
        "why": ("The privileged half: render /etc/cloudflared/config.yml from the tunnel's OWN "
                "credentials, install the cloudflared system service, start it, and verify a "
                "connector is actually reporting. ★ The grant is TIMED AND EXPIRING, and it had "
                "already expired — a grant that outlives its purpose is a standing key. This is the "
                "estate's rule in practice: root BLOCKS by default and a grant is an exception with "
                "a clock on it."),
        "typing": "grant the cloudflare tunnel capability",
        "button": {"label": "⛨ Grant (timed)", "action": "grant:network.cloudflare-tunnel"},
        "say": ("I need a timed grant to install the tunnel service. It is ONE script, no arguments, "
                "its own sudoers file — the entire permission is the line "
                "`<user> ALL=(root) NOPASSWD: str(Path(__file__).resolve().parent.parent)/bin/cloudflare-tunnel-service.sh`. "
                "It refuses without your certificate, refuses any hostname outside "
                "example.invalid, and refuses to report success if no connector appears. Say "
                "\u201cgrant the cloudflare tunnel capability\u201d and I will queue it and show you "
                "that exact line before it is issued. It expires on its own."),
        "next": "Expiry is mechanical: `sweep` removes the sudoers file whether or not anyone polled it.",
        "capability": "network.cloudflare-tunnel",
    },
    {
        "id": "value_substitution",
        "felt": "What may cross the tunnel",
        "technical": "reversible placeholder→rehydrate substitution for egress legs (OPEN_ASKS A5)",
        "status": "ENFORCING",
        "owner": "system",
        "probe": probe_value_substitution,
        "why": ("★ THIS IS IMPLEMENTED NOW, AND THE ROW SAYS SO PLAINLY — then says exactly how far "
                "the evidence goes, because the previous version of this paragraph was honest when "
                "it said 'not implemented' and became a lie the moment the gate landed. "
                "`jarvis/privacy_gate.py` exists and THE ROUND TRIP WAS DRIVEN ON THIS BOX: a "
                "synthetic turn carrying a name and an email went in, [[TYPE_hex8]] placeholders "
                "came out, NEITHER real value was in the sent text, and re-hydration put BOTH back. "
                "It is called from SIX egress points in `router.py`, counted from the parse tree "
                "rather than from a grep. ★★ THE RUNG IS **WORKS**, NOT **USABLE**, and the "
                "difference is the entire point of this paragraph: no turn has been sent to a real "
                "cloud agent and no gated turn has gone through the running daemon's HTTP surface, "
                "so what is proven is that substitution and the return path work HERE — not that "
                "they have survived a real far side. ★ AND WHAT STILL BITES: re-hydration is "
                "EXACT-TOKEN. A far side that rewrites a placeholder — markdown, wrapping, a smart "
                "quote — never gets its value back, and the gate COUNTS those instead of hiding "
                "them. ★★ THE OLD RULE IS RETIRED AND REPLACED BY A NARROWER ONE: rung 3 may now "
                "carry turns whose DETECTED values crossed as placeholders, but detection is not "
                "total — tier 1 is a local model with a measured miss rate — so this is a "
                "REDUCTION, not an absolution. The refusal that remains is the `unchecked` branch: "
                "if the classifier is down and nothing shaped was found, the turn is HELD and does "
                "not go. ★ And this row re-measures the round trip on every single read instead of "
                "quoting this paragraph, so the day it stops being true it says so by itself."),
        "typing": "what can cross the tunnel",
        "button": {"label": "▤ What may cross", "action": "open:transport#egress"},
        "say": ("Value substitution is built now, and you should hear exactly how far that goes "
                "before you trust it. A name and an email went into the gate on this box, "
                "placeholders came out, and the real values came back on the return path — so the "
                "round trip works here, and six egress points in the router call it. What I have "
                "NOT done is send a real turn to a real cloud agent, so I will not tell you it has "
                "survived a far side; that would be me guessing. Say \u201cwhat can cross the "
                "tunnel\u201d and I will re-read the egress rule and tell you which rung you are "
                "on; press What may cross to see the rule itself."),
        "next": ("The next rung is USABLE and it is one real turn: a gated turn through the daemon "
                 "to a real cloud agent, with the answer re-hydrated on the way back. Until that "
                 "run exists this row stays WORKS — and if privacy_gate.py disappears or stops "
                 "restoring, the probe goes red on its next read without anyone editing this text."),
        "capability": None,
    },
)

# ★★★ THE RECOMMENDED, OPTIONAL ONE. Appended LAST on purpose: it is an offer, and an offer that
#     jumps the queue ahead of a blocked step is a recommendation behaving like a requirement.
#     ★ AND IT IS APPENDED AS A STEP DICT FROM THE OTHER STORE, not re-typed here — `gev_option.step()`
#       carries the probe CALLABLE (so nothing is measured at import) and a `row_extra` hook that
#       reads his accept/decline decision LIVE, so a decline takes effect on the very next read.
#     ★ IF THE MODULE IS MISSING, THE STEP IS MISSING AND THE WALK SAYS SO. It is not silently
#       skipped: a recommended option that vanishes without a word is indistinguishable from one
#       that was never offered, and that is the false-absence class this estate keeps paying for.
if GEV is not None:
    STEPS = STEPS + (GEV.step(),)
else:
    STEPS = STEPS + ({
        "id": "gev_option",
        "felt": "God's Eye View — the recommended globe (option store MISSING)",
        "technical": "operator/gev_option.py could not be imported",
        "status": "DESIGN-ONLY",
        "owner": "agent",
        "probe": lambda: ("UNKNOWN", f"the option store did not import: {_GEV_IMPORT_ERROR}"),
        "why": ("The recommended option exists but its store could not be loaded, so NOTHING about "
                "it can be measured or disclosed here. It is shown as a broken row rather than "
                "omitted, because a missing option and an unoffered option must not look the same."),
        "typing": "why is gods eye view not offered",
        "button": {"label": "↻ Re-measure", "action": "step:gev_option"},
        "say": ("The Gods Eye View option could not be loaded, so I cannot tell you what it costs "
                "or whether it is installed — and I would rather say that than describe it from "
                "memory. Say \u201cwhy is gods eye view not offered\u201d and I will re-read the "
                "store. The rest of the walk is unaffected."),
        "next": "Repairing operator/gev_option.py brings the row back with its real measurement.",
        "capability": None,
    },)


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE RUNNER
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _fill(template: str, value: str) -> str:
    """`{value}` in a `say`/`why` is filled from the MEASURED evidence, not from a guess."""
    return template.replace("{value}", value or "unknown")


def _probe_state(fn) -> tuple:
    """Run one probe. Returns (state, evidence, extra). ★ THIS IS A NAMED SEAM ON PURPOSE.

    It exists so the anti-overclaim rule (I5: *a probe that raises answers UNKNOWN, never DONE*) is a
    thing that can be BROKEN and caught, rather than an inline `try` that nothing can reach. The
    prove-fail replaces this function with one that swallows the exception into DONE and requires the
    I5 check to go red. A guard that cannot be driven to red is a guard we are hoping about.

    ★ AND A PROBE MAY RETURN A THIRD ELEMENT. `(state, evidence, extra)` lets a step keep a fact the
      walk's five words cannot hold — the GEV option's own measured state is the first, because
      'installed but no key' collapses to BLOCKED for DISPLAY and must not collapse for TESTING
      (I7 has to see what was actually measured). Two-element probes are unchanged, so this is
      new-only and nothing already here had to move."""
    try:
        res = fn()
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"probe raised {type(e).__name__}: {e}", {}
    if isinstance(res, tuple) and len(res) == 3:
        st, ev, extra = res
    else:
        st, ev = res
        extra = {}
    return (st if st in STATES else "UNKNOWN"), ev, (extra if isinstance(extra, dict) else {})


def _i5_fails(probe_state=_probe_state) -> list:
    """I5, as a function of the runner — so it can be pointed at a LYING runner in prove-fail."""
    def _boom():
        raise RuntimeError("planted")
    res = probe_state(_boom)
    st = res[0] if isinstance(res, tuple) else res
    if st != "UNKNOWN":
        return [f"I5 a raising probe produced {st!r}, not UNKNOWN — this is the exact shape that "
                f"reports a broken thing as working"]
    return []


def plan(steps=None, timeout: int = 10) -> dict:
    """Run every probe and return the whole walk as data. Read-only, no side effects.

    ★ Probe failures are UNKNOWN rows carrying their reason, NEVER `DONE`. This is the single most
      important line in the file: a wizard that silently passes an unmeasurable step is worse than
      no wizard, because it manufactures confidence."""
    rows, counts = [], {s: 0 for s in STATES}
    for s in (steps if steps is not None else STEPS):
        # ★ ANY CALLABLE IS A HOOK, NOT A FACT. `probe` is the measurement and `row_extra` is the
        #   per-step live-key hook; neither is JSON. Filtering on `callable` rather than on the two
        #   names means a future step can add a hook without this line having to learn its name.
        row = {k: v for k, v in s.items() if not callable(v)}
        state, evidence, extra = _probe_state(s["probe"])
        row.update(extra or {})
        row["state"] = state
        row["evidence"] = evidence
        row["status"] = _enum(row.get("status"))
        row["why"] = _fill(row.get("why", ""), evidence)
        row["say"] = _fill(row.get("say", ""), evidence)
        # ★ LIVE KEYS, READ NOW. A step may carry a `row_extra()` returning keys that must not be
        #   frozen at import time — the GEV row's accept/decline decision is the first of them, and a
        #   decline that only took effect after a restart would be a button that appears not to work.
        extra = s.get("row_extra")
        if callable(extra):
            try:
                row.update(extra() or {})
            except Exception as e:  # noqa: BLE001 — a dead hook must not take the walk down
                row["row_extra_error"] = f"{type(e).__name__}: {e}"
        counts[state] += 1
        rows.append(row)
    blocked = [r for r in rows if r["state"] == "BLOCKED"]
    later = [r for r in rows if r["state"] == "STILL-BLOCKED-LATER"]
    unknown = [r for r in rows if r["state"] == "UNKNOWN"]
    done = [r for r in rows if r["state"] == "DONE"]
    # ★ SETTLED IS NOT OUTSTANDING. A declined recommendation is his decision, not our backlog.
    declined = [r for r in rows if r["state"] == "DECLINED"]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "steps": rows,
        "counts": counts,
        "done": len(done),
        "blocked_now": len(blocked),
        "blocked_later": len(later),
        "unknown": len(unknown),
        "declined": len(declined),
        "total": len(rows),
        # ★ THE ONE LINE HE READS. Blocked-now before still-blocked-later, because the first is
        #   actionable today and the second is not — collapsing them is how "waiting on hardware"
        #   gets mistaken for "broken". ★ AND `declined` is reported SEPARATELY AND LAST, never
        #   inside `done`: a refused offer is not a completed step, and it is not a problem either.
        "headline": (f"{len(done)} of {len(rows)} steps done · {len(blocked)} blocked now · "
                     f"{len(later)} waiting on you or hardware · {len(unknown)} could not be "
                     f"measured" + (f" · {len(declined)} declined" if declined else "")),
        # ★ DECLINED IS NEVER `next_actionable`. `next_actionable` drives what the seat raises at him;
        #   putting a refused offer there would make "no" mean "ask me again", which is the opposite
        #   of what a decline is for.
        "next_actionable": (blocked[0]["id"] if blocked else
                            (later[0]["id"] if later else None)),
        # ★★★★★ THE VOICE BOUNDARY, AT PLAN LEVEL, AND WHY IT IS NOT ONLY A ROW.
        #     The page's DEFAULT filter is "what is not done". A step that measures DONE is therefore
        #     HIDDEN by default -- correct for a completed step, and fatal for a requirement that is
        #     supposed to be READ by someone who has not finished anything yet. So the requirement
        #     also rides here, at the top of the plan, where the page can render it ABOVE the step
        #     list and outside every filter. The measurement is NOT taken twice: this reads the
        #     `voice_detection` the row's own probe already produced.
        "voice_boundary": voice_boundary_block(rows),
    }


def render(plan_d: dict, width: int = 96) -> str:
    """The walk as text, in the boardwalk's own shape: one step at a time, blocker first, with the
    way forward ON the row rather than in a document somewhere else."""
    icons = {"DONE": "\u2713", "BLOCKED": "\u2717", "STILL-BLOCKED-LATER": "\u25cc",
             "UNKNOWN": "?", "DECLINED": "\u2298", "": " "}
    out = []
    out.append("=" * width)
    out.append("  THE SETUP WALK — %s on %s" % (plan_d["generated"], plan_d["host"]))
    out.append("  " + plan_d["headline"])
    # ★★★ THE BOUNDARY RIDES THE TEXT RENDER TOO, AND FOR THE SAME REASON IT RIDES THE PAGE: a
    #     requirement that is only visible to someone who filters for it is not in the install. It is
    #     printed here UNCONDITIONALLY, above the steps, exactly as the page prints it above the list.
    _vb = plan_d.get("voice_boundary") or {}
    if _vb.get("statement"):
        out.append("")
        out.append("  \u2605 THE VOICE BOUNDARY \u2014 " + str(_vb["statement"]))
        out.append("    measured: " + str(_vb.get("line") or ""))
    out.append("=" * width)
    for i, r in enumerate(plan_d["steps"], 1):
        ic = icons.get(r["state"], "?")
        out.append("")
        # ★★ BOTH AXES ARE LABELLED, AND THAT IS A FIX RATHER THAN A FLOURISH. `state` (what the
        #    measurement found) and `status` (whether the mechanism behind the row enforces anything)
        #    are DIFFERENT AXES, and a row printing `BLOCKED + ENFORCING` side by side with neither
        #    named is honest and reads as a contradiction. Naming them costs no width and removes the
        #    only way this line can be misread.
        out.append(f"{ic} {i:>2}. {r['felt']}   [state: {r['state']}]  "
                   f"(status: {r['status']}, owner: {r['owner']})")
        out.append(f"      {r['technical']}")
        out.append(f"      measured: {r['evidence']}")
        if r["state"] != "DONE":
            out.append(f"      why:  {r['why'][:width - 12]}")
            out.append(f"      ask:  \"{r['typing']}\"")
            out.append(f"      or:   [ {r['button']['label']} ]  -> {r['button']['action']}")
            out.append(f"      then: {r['next']}")
    out.append("")
    out.append("-" * width)
    if plan_d["next_actionable"]:
        nxt = next(r for r in plan_d["steps"] if r["id"] == plan_d["next_actionable"])
        out.append(f"NEXT ACTIONABLE STEP: {nxt['felt']}")
        out.append(f"  {nxt['say']}")
    else:
        out.append("NEXT ACTIONABLE STEP: none — every step is done or waiting on you.")
    out.append("-" * width)
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _fails_for(steps) -> list:
    """The invariants, over ANY step list. ★ THE SUITE AND THE PROVE-FAIL CALL THIS SAME FUNCTION,
    so the guard that is proved red and the guard that runs live are the same line of code."""
    fails = []
    # I4
    ids = [s["id"] for s in steps]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        fails.append(f"I4 duplicate step ids: {dupes}")

    for s in steps:
        sid = s.get("id", "?")
        t = str(s.get("typing") or "").strip()
        b = s.get("button") or {}
        if not t:
            fails.append(f"I1 {sid}: no `typing` — the typed approval path is missing")
        if not (b.get("label") or "").strip() or not (b.get("action") or "").strip():
            fails.append(f"I1 {sid}: no `button` label/action — the click path is missing")
        if s.get("status") not in STATUSES:
            fails.append(f"I3 {sid}: status {s.get('status')!r} is outside the vocabulary")
        say = str(s.get("say") or "")
        if len(say) < 80:
            fails.append(f"I2 {sid}: `say` is {len(say)} chars — too short to name, explain, "
                         f"instruct and promise")
        # ★ INSTRUCTIONS, NOT PROSE. §7's standard is that a blocked action gives him something to
        #   DO. That is one of four forms and the check accepts all of them because they are all real
        #   instructions: a backticked command, a "say …" phrase (the typed path), a "press …" or a
        #   "run …". A sentence with none of them is an explanation, not a way forward.
        has_instruction = bool(
            re.search(r"`[^`]+`", say) or re.search(r"\bsay\b", say, re.I)
            or re.search(r"\bpress\b", say, re.I) or re.search(r"\brun\b", say, re.I))
        if not has_instruction:
            fails.append(f"I2 {sid}: `say` contains no instruction — no backticked command, no "
                         f"'say …', no 'press …'. An explanation is not a way forward.")
        # ★ AND A PROMISE. The operator's standard ends with what happens NEXT, so the sentence must
        #   say what the system will do once he acts — otherwise acting teaches him nothing.
        has_promise = bool(re.search(r"\bI will\b|\bwill then\b|\btell me\b|\bwhen you\b|\bthen I\b",
                                     say, re.I))
        if not has_promise:
            fails.append(f"I2 {sid}: `say` makes no promise about what happens after he acts — the "
                         f"standard is: name it, say why it is his, give the steps, say what is next")
        if not str(s.get("next") or "").strip():
            fails.append(f"I2 {sid}: no `next` — a blocked action with no 'what happens after' is "
                         f"exactly the dead end this file exists to prevent")
    return fails


def _i7_fails(plan_d) -> list:
    """I7 — ★★★ 'RECOMMENDED' NEVER RENDERS AS DONE WHEN NOTHING WAS FETCHED, AND A DECLINE SETTLES.

    ★ THE DEFECT THIS EXISTS FOR WAS FOUND NINE TIMES IN ONE DAY on this estate: a thing reported as
      present because a row existed. The setup walk now carries a RECOMMENDED OPTION, which is
      precisely the shape that invites it — "recommended" reads as "assumed", and an assumed install
      reports green with nothing on disk. So this invariant is a function of a PLAN, aimed at the
      live one and at planted liars alike.

    It refuses, by name:
      · a GEV row whose option state says nothing was fetched while its walk state says DONE
      · a declined option that is counted as done, or as blocked, or raised as next_actionable
      · a plan that reports a `declined` count which does not match the rows
      · a GEV row missing the disclosure it is supposed to carry
    """
    fails = []
    rows = plan_d.get("steps") or []
    gev = [r for r in rows if r.get("id") == "gev_option"]

    if not gev:
        fails.append("I7 the recommended option is absent from the walk entirely — an unoffered "
                     "option and a missing one must not look the same")
        return fails

    g = gev[0]
    unfetched = ("NOT-INSTALLED", "INSTALLED-NO-KEY")
    if g.get("option_state") in unfetched and g.get("state") == "DONE":
        fails.append(f"I7 the option reports {g.get('option_state')!r} (NOTHING FETCHED) while its "
                     f"walk state is 'DONE' — this is the nine-times defect; 'recommended' must "
                     f"never render as 'done'")

    if g.get("state") == "DECLINED":
        if plan_d.get("next_actionable") == "gev_option":
            fails.append("I7 a DECLINED option is next_actionable — a refusal that comes back as a "
                         "prompt means 'no' did not mean no")
        if g.get("id") in [r["id"] for r in rows if r.get("state") == "DONE"]:
            fails.append("I7 a DECLINED option is also reported DONE")

    counted = plan_d.get("declined")
    actual = len([r for r in rows if r.get("state") == "DECLINED"])
    if counted != actual:
        fails.append(f"I7 plan reports declined={counted!r} but {actual} row(s) are DECLINED")

    # ★ AND THE DISCLOSURE MUST SURVIVE THE TRIP. The row he is asked to judge must carry the cost.
    t = g.get("terms") or {}
    if not t:
        fails.append("I7 the recommended row carries NO terms — he is being asked to choose "
                     "without the cost, the egress, or the licence")
    else:
        for part in ("gives", "requires", "egress", "licence", "attribution", "voice",
                     "voice_boundary"):
            if not str(t.get(part) or "").strip():
                fails.append(f"I7 the recommended row's terms dropped {part!r} in transit")
    return fails


def _i8_fails(plan_d=None, steps=None) -> list:
    """I8 -- THE VOICE BOUNDARY IS STATED IN THE INSTALL, AND ITS WARNING NEVER TAKES THE DECISION.

    * THE FOUR DEFECTS THIS EXISTS FOR, each of which is a way a "warning" becomes a lie:
        - the requirement is ABSENT from the walk. A rule that is not in the install is a hope, and
          the operator asked for it to be PART OF THE INSTALL.
        - the requirement is stated ONE-SIDED (says text crosses but never says audio does not), or
          gutted. The statement is the deliverable; an empty one renders green and says nothing.
        - the warning is LOUD WITH NOTHING BEHIND IT. A red light that names nobody teaches the
          operator to ignore the next one, which costs more than not warning at all.
        - the warning is QUIET WHEN A DETECTOR COULD NOT LOOK, or it BLOCKS. The first folds "I could
          not see" into "it is clean" (COWORK_PROTOCOL 2.7); the second TAKES a decision he reserved
          for himself -- "It's their choice but ill advised" is the ruling, and "It may ASK. It may
          NOT TAKE." is the estate's.
    * IT IS CALLED WITH THE RAW STORE (step-level checks) AND WITH THE LIVE PLAN (block-level checks),
      so the guard that is proved red is the same line of code that runs on the served walk."""
    fails = []
    steps = list(STEPS if steps is None else steps)
    step = next((s for s in steps if s.get("id") == "voice_boundary"), None)
    if not step:
        fails.append("I8 the voice-boundary requirement is ABSENT from the walk -- 'make the "
                     "requirements a part of the install' is not satisfied by a file on disk")
    else:
        b = step.get("button") or {}
        if not str(step.get("typing") or "").strip() or not (b.get("label") and b.get("action")):
            fails.append("I8 the boundary row carries only one approval path -- every row needs a "
                         "typed sentence AND a button (his standing rule)")

    st = str(VOICE_BOUNDARY.get("statement") or "")
    if len(st) < 120:
        fails.append(f"I8 the boundary statement is {len(st)} chars -- too short to state a boundary")
    if "text" not in st.lower() or "audio" not in st.lower():
        fails.append("I8 the boundary statement is ONE-SIDED -- it must name both what crosses "
                     "(text) and what does not (audio)")

    cd = list(VOICE_BOUNDARY.get("cannot_detect") or ())
    if len(cd) < 3:
        fails.append(f"I8 the boundary discloses only {len(cd)} limit(s) -- a warning with no stated "
                     f"limits reads as 'we can see everything', which is the overclaim that makes an "
                     f"undetectable compromise worse than silence")

    if plan_d:
        blk = plan_d.get("voice_boundary") or {}
        if not blk.get("statement"):
            fails.append("I8 plan() published no voice_boundary block -- the page renders it above "
                         "the walk, and a requirement missing from the top of the page is not read")
        state = str(blk.get("state") or "")
        if state == "warn":
            if not (blk.get("detected") or []):
                fails.append("I8 the warning is LOUD with NOTHING detected -- a red light behind "
                             "which there is nothing")
            if not str(blk.get("line") or "").strip():
                fails.append("I8 the warning is LOUD with no sentence to show him")
        if state == "quiet" and (blk.get("unmeasured") or []):
            fails.append(f"I8 the boundary reports QUIET while {len(blk['unmeasured'])} detector(s) "
                         f"COULD NOT LOOK -- 'I could not look' folded into 'I looked and it was clean'")
        rows = plan_d.get("steps") or []
        row = next((r for r in rows if r.get("id") == "voice_boundary"), None)
        if row is None:
            fails.append("I8 the plan has a boundary block but no boundary ROW -- the statement and "
                         "the measurement must travel together")
        elif row.get("state") in ("BLOCKED", "STILL-BLOCKED-LATER"):
            fails.append(f"I8 the boundary row reports {row.get('state')!r} -- the warning has turned "
                         f"into a block, and he ruled that out: 'It's their choice but ill advised'")
        elif row.get("state") == "DONE" and plan_d.get("next_actionable") == "voice_boundary":
            fails.append("I8 a satisfied boundary is next_actionable -- the walk is nagging him about "
                         "a rule that is already holding")
    return fails


def _lied_boundary(plan_d, block=None, row_state=None, next_actionable=None) -> dict:
    """A COPY of a live plan with one lie planted in the VOICE BOUNDARY. Same reasoning as
    `_lied_plan`: I8 has to be aimed at a specific defect rather than hoped about."""
    import copy as _copy
    p = _copy.deepcopy(plan_d)
    if block:
        p["voice_boundary"] = {**(p.get("voice_boundary") or {}), **block}
    if row_state is not None:
        for r in p.get("steps") or []:
            if r.get("id") == "voice_boundary":
                r["state"] = row_state
    if next_actionable is not None:
        p["next_actionable"] = next_actionable
    return p


def _i9_fails(plan_d=None, steps=None) -> list:
    """I9 — ★★★★ THE LEAD-AGENT SUGGESTION IS IN THE INSTALL, IT MEASURES, AND IT DOES NOT TAKE.

    * WHY THIS IS A GUARD AND NOT A SENTENCE. The operator's requirement is that a NEW installer be
      TOLD to create a lead agent. Every way that telling can rot is a way this estate has already
      been burned, so each one is refused here, by name:
        · the requirement is ABSENT from the walk. "It should be suggested to new installers" is not
          satisfied by a page in a document — the same ruling the voice boundary was built under.
        · the row carries only ONE approval path. His standing rule is a typed sentence AND a button,
          on every row, checked per row and not in aggregate.
        · the row renders `ENFORCING` WITHOUT ITS LIMIT. The limit is not a hedge: it is the whole
          difference between "the ledger decides what an agent may be GRANTED" and the overclaim that
          the ledger contains an agent that already has a shell. A guard against overclaiming that
          itself overclaims is worse than no guard.
        · the row's load-bearing clause is dropped. The clause — a no-row agent is REFUSED, because
          the default posture is install-scoped — is the REASON the suggestion exists. A suggestion
          that keeps the instruction and loses the reason is one nobody can act on.
        · THE ROW RENDERS A ZERO WHERE IT COULD NOT LOOK. This is the file's most important clause:
          "0 agents" reads as a measurement and is not one. A probe that cannot read the store must
          answer UNKNOWN and carry the REASON.
        · the suggestion TAKES instead of ASKING. "It may ASK. It may NOT TAKE." A button on this row
          that performed the grant itself would be the walk granting capability on his behalf — so
          the action must OPEN the surface where he decides, never write.
    * IT IS CALLED WITH THE RAW STORE (step-level checks) AND WITH THE LIVE PLAN (row-level checks),
      so the guard that is proved red is the same line of code that runs on the served walk."""
    fails = []
    steps = list(STEPS if steps is None else steps)
    step = next((s for s in steps if s.get("id") == "lead_agent"), None)
    if not step:
        fails.append("I9 the LEAD-AGENT suggestion is ABSENT from the walk -- 'it should be suggested "
                     "to new installers' is not satisfied by a file on disk")
        return fails

    b = step.get("button") or {}
    if not str(step.get("typing") or "").strip() or not (b.get("label") and b.get("action")):
        fails.append("I9 the lead-agent row carries only ONE approval path -- every row needs a typed "
                     "sentence AND a button (his standing rule)")
    act = str(b.get("action") or "")
    if not act.startswith("open:"):
        fails.append(f"I9 the lead-agent row's button is {act!r} -- it must OPEN the surface where he "
                     f"decides, because it may ASK and it may NOT TAKE")

    clause, limit = str(LEAD_AGENT.get("clause") or ""), str(LEAD_AGENT.get("limit") or "")
    if "agent row" not in clause.lower() or "REFUSES" not in clause:
        fails.append("I9 the lead-agent clause is ONE-SIDED -- it must name BOTH the empty state (no "
                     "agent row) and the refusal a no-row agent gets")
    why = str(step.get("why") or "")
    if clause and clause not in why:
        fails.append("I9 the lead-agent row dropped its LOAD-BEARING CLAUSE -- the instruction "
                     "survived and the reason it matters did not")
    if _enum(step.get("status")) == "ENFORCING" and limit and limit not in why:
        fails.append("I9 the lead-agent row claims ENFORCING without stating WHAT IT DOES NOT "
                     "ENFORCE -- the exact overclaim this estate records nine times a day")
    if "{value}" not in why and "{value}" not in str(step.get("say") or ""):
        fails.append("I9 the lead-agent row states no MEASUREMENT -- the row must render a measured "
                     "value, not a claim")

    # ── DRIVEN. Both "could not look" branches are exercised here, not described. ─────────────────
    def _boom():
        raise OSError("I9 planted: the store cannot be read")

    st, ev, _x = _probe_state(lambda: probe_lead_agent(reader=_boom))
    if st != "UNKNOWN":
        fails.append(f"I9 a lead-agent probe that CANNOT LOOK answered {st!r}, not UNKNOWN -- a "
                     f"broken read must never render as a measurement")
    if not ev.strip() or ev.strip().strip("0.") == "":
        fails.append(f"I9 a lead-agent probe that cannot look rendered {ev!r} -- a ZERO is not a "
                     f"reason. Could-not-look is its own state and it must say so.")

    st_e, ev_e = probe_lead_agent(reader=lambda: {"_shaman": {"zones": {}}, "agents": {}})
    if st_e != "BLOCKED":
        fails.append(f"I9 an EMPTY ledger produced {st_e!r}, not BLOCKED -- a fresh install with no "
                     f"agent row is the case this step exists for")
    if "denied_axis" not in ev_e:
        fails.append("I9 the empty-ledger evidence does not carry the REFUSAL it is closing -- the "
                     "reason the suggestion matters must be measured on the row")

    if plan_d:
        rows = plan_d.get("steps") or []
        row = next((r for r in rows if r.get("id") == "lead_agent"), None)
        if row is None:
            fails.append("I9 plan() carries no lead-agent row -- the store has it and the renderer "
                         "does not")
        else:
            ev = str(row.get("evidence") or "").strip()
            if not ev or ev.strip("0.") == "":
                fails.append(f"I9 the rendered lead-agent row shows {ev!r} as its measurement -- a "
                             f"zero where it could not look")
            if row.get("state") == "DONE" and not any(x in ev for x in ("agent row", "rank=")):
                fails.append(f"I9 the row reports DONE on evidence that names no row and no rank: "
                             f"{ev!r} -- 'done' must be the measurement, not the intention")
            # ★ THE CLAUSE AND THE LIMIT MUST SURVIVE THE TRIP, not merely exist in the store. The
            #   step can be correct in source and the rendered row still lose it downstream, which is
            #   the class `_i7_fails` catches for the recommended row's terms.
            rwhy = str(row.get("why") or "")
            if clause and clause not in rwhy:
                fails.append("I9 the RENDERED lead-agent row lost its load-bearing clause in transit")
            if _enum(row.get("status")) == "ENFORCING" and limit and limit not in rwhy:
                fails.append("I9 the RENDERED lead-agent row claims ENFORCING without its stated limit")
    return fails


def _lied_lead(plan_d, evidence=None, state=None, why=None) -> dict:
    """A COPY of a live plan with one lie planted in the LEAD-AGENT row. Same reasoning as
    `_lied_plan` and `_lied_boundary`: I9 has to be aimed at a specific defect rather than hoped
    about."""
    import copy as _copy
    p = _copy.deepcopy(plan_d)
    for r in p.get("steps") or []:
        if r.get("id") != "lead_agent":
            continue
        if evidence is not None:
            r["evidence"] = evidence
        if state is not None:
            r["state"] = state
        if why is not None:
            r["why"] = why
    return p


def _lied_plan(plan_d, state=None, option_state=None, next_actionable=None, drop_terms=False) -> dict:
    """A COPY of a plan with one lie planted in the GEV row. ★ It exists so I7 can be aimed at a
    specific defect rather than hoped about — the same reasoning as `_lied_runner` in prove-fail.
    It deep-enough-copies the parts it mutates so the caller's plan is never touched."""
    import copy as _copy
    p = _copy.deepcopy(plan_d)
    for r in p.get("steps") or []:
        if r.get("id") != "gev_option":
            continue
        if state is not None:
            r["state"] = state
        if option_state is not None:
            r["option_state"] = option_state
        if drop_terms:
            r["terms"] = {}
    if next_actionable is not None:
        p["next_actionable"] = next_actionable
    if state is not None:
        p["declined"] = len([r for r in p.get("steps") or [] if r.get("state") == "DECLINED"])
    return p


def self_test() -> int:
    """★ SET EQUALITY ON THE THING THAT MAKES A WIZARD HONEST, not on the words in it.

    The invariants, each of which has been violated somewhere in this estate's record:
      I1  every row carries BOTH approval paths, non-empty            (his standing rule)
      I2  every row's `say` INSTRUCTS and PROMISES, and it has a `next` (§7's standard)
      I3  `status` is in the closed vocabulary                          (honesty vocabulary)
      I4  ids are unique                                                (one row per fact)
      I5  a probe failure produces UNKNOWN, never DONE                  (the anti-overclaim rule)
      I6  `plan()` and `STEPS` agree as SETS of ids                     (the renderer cannot invent)
      I7  a RECOMMENDED option never renders as DONE with nothing fetched, and a DECLINE settles
                                                                        (the nine-times defect)
      I8  the VOICE BOUNDARY is stated in the install, and its warning never blocks, never cries
          wolf and never folds "could not look" into "clean"                (added 2026-10-12)
      I9  the LEAD-AGENT suggestion is stated in the install, it MEASURES rather than asserts, it
          renders a REASON and never a zero when it cannot look, and it ASKS rather than TAKES
                                                                             (added 2026-10-14)
    """
    fails, notes = [], []

    fails.extend(_fails_for(STEPS))

    # I5 — drive a probe that raises, and require UNKNOWN
    fails.extend(_i5_fails())

    # I6 — set equality between the store and the renderer
    live = plan()
    ids = [s["id"] for s in STEPS]
    if {r["id"] for r in live["steps"]} != set(ids):
        fails.append("I6 plan() ids != STEPS ids")
    if live["total"] != len(STEPS):
        fails.append(f"I6 plan() counted {live['total']} rows for {len(STEPS)} steps")
    # and the renderer must mention every step, by felt name
    txt = render(live)
    for s in STEPS:
        if s["felt"] not in txt:
            fails.append(f"I6 the render dropped step {s['id']!r}")

    # I7 — the recommended option, against the LIVE plan and against planted liars
    fails.extend(_i7_fails(live))
    if not _i7_fails(_lied_plan(live, state="DONE", option_state="NOT-INSTALLED")):
        fails.append("I7 the guard did NOT bite a plan reporting the option DONE with nothing "
                     "fetched — this guard cannot fail, which is the same as no guard at all")
    if not _i7_fails(_lied_plan(live, state="DECLINED", next_actionable="gev_option")):
        fails.append("I7 the guard did NOT bite a DECLINED option raised as next_actionable")
    if not _i7_fails(_lied_plan(live, drop_terms=True)):
        fails.append("I7 the guard did NOT bite a recommended row that lost its terms")

    # I8 — the voice boundary, against the RAW store and against the LIVE plan and against liars
    fails.extend(_i8_fails(live, STEPS))
    if not _i8_fails(_lied_boundary(live, block={"state": "warn", "detected": []}), STEPS):
        fails.append("I8 the guard did NOT bite a LOUD warning with nothing detected")
    if not _i8_fails(_lied_boundary(live, block={"state": "quiet", "unmeasured": ["D1 blind"]}), STEPS):
        fails.append("I8 the guard did NOT bite a QUIET boundary with a blind detector")
    if not _i8_fails(_lied_boundary(live, row_state="BLOCKED", next_actionable="voice_boundary"),
                     STEPS):
        fails.append("I8 the guard did NOT bite a warning that turned into a block")

    # I9 — the lead-agent suggestion, against the RAW store and against the LIVE plan and against liars
    fails.extend(_i9_fails(live, STEPS))
    if not _i9_fails(_lied_lead(live, evidence="0", state="DONE"), STEPS):
        fails.append("I9 the guard did NOT bite a row that rendered a ZERO where it could not look")
    if not _i9_fails(_lied_lead(live, state="DONE"), [s for s in STEPS if s["id"] != "lead_agent"]):
        fails.append("I9 the guard did NOT bite a walk with the suggestion MISSING from it")
    if not _i9_fails(_lied_lead(live, why="Give the assistant a lead agent."), STEPS):
        fails.append("I9 the guard did NOT bite a row that lost its load-bearing clause AND its limit")

    n_blocked = live["blocked_now"] + live["blocked_later"]
    notes.append(f"{live['headline']}")
    notes.append(f"{n_blocked} non-done row(s) each carry a typed sentence AND a button")
    notes.append(f"{len(STEPS)} steps · the recommended option measures "
                 f"{[r.get('option_state') for r in live['steps'] if r.get('id') == 'gev_option']}")
    _la = next((r for r in live["steps"] if r.get("id") == "lead_agent"), None)
    if _la:
        notes.append(f"lead agent measures {_la['state']!r} — {_la['evidence'][:150]}")
    _vb = live.get("voice_boundary") or {}
    notes.append(f"voice boundary measures {_vb.get('state')!r} — {_vb.get('line')}")
    for f in fails:
        print(f"  \u2717 {f}")
    if not fails:
        print(f"  \u2713 setup_blockers self_test: {len(STEPS)} steps · I1..I9 hold")
    for n in notes:
        print(f"  \u00b7\u00b7 {n}")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--self-test" in a:
        rc = self_test()
        if "--prove-fail" in a:
            rc = prove_fail() or rc
        return rc
    if "--prove-fail" in a:
        return prove_fail()
    if "--json" in a:
        print(json.dumps(plan(), indent=2))
        return 0
    if "--blocked" in a:
        p = plan()
        rows = [r for r in p["steps"] if r["state"] in ("BLOCKED", "STILL-BLOCKED-LATER")]
        print(json.dumps([{k: r[k] for k in ("id", "felt", "state", "owner", "evidence",
                                             "typing", "button", "say", "next")} for r in rows],
                         indent=2))
        return 0
    print(render(plan()))
    return 0


def prove_fail() -> int:
    """★ EVERY GUARD IS BROKEN ON PURPOSE AND MUST GO RED. A guard never tested against its own
    bypass is a guard we are hoping about (AGENTS.md §2).

    Six mutations, one per invariant, each applied to an in-memory copy of `STEPS` and each REQUIRED
    to produce a failure naming its own invariant. A mutation that leaves the suite green makes the
    WHOLE RUN INVALID and this returns non-zero — because the failure mode being guarded against is
    a check that cannot fail, which is the same thing as no check at all.
    """
    print("=== PROVE-FAIL — break each guarded thing, require RED ===")
    muts = []

    # M1 — remove the typed approval path from a row that will measure BLOCKED (cf_cert: the
    #      certificate is absent on this box, measured). Indexed by ID, not by position, so this
    #      mutation cannot silently aim at the wrong row when the store is reordered.
    tgt = [i for i, s in enumerate(STEPS) if s["id"] == "cf_cert"][0]
    m = [dict(s) for s in STEPS]
    m[tgt] = dict(m[tgt]); m[tgt]["typing"] = ""
    muts.append(("M1 drop `typing`", m, "I1", "no `typing`"))

    # M2 — remove the button
    m = [dict(s) for s in STEPS]
    m[tgt] = dict(m[tgt]); m[tgt]["button"] = {"label": "", "action": ""}
    muts.append(("M2 drop `button`", m, "I1", "no `button`"))

    # M3 — put a status outside the closed vocabulary.
    #      ★ ADDRESSED BY ID, NOT BY INDEX (2026-10-14). M3/M4/M6 used to say `m[0]`, `m[1]`, `m[4]`,
    #      which meant any row inserted before them silently RE-AIMED three guards at rows they were
    #      not written for — the hazard the `voice_boundary` comment recorded. The `lead_agent` row is
    #      inserted at index 1, so they are named now. Each still aims at the SAME row it did before:
    #      index 0 was `territory`, index 1 was `secrets_keeper`, index 4 was `speaker`.
    _at = lambda sid: [i for i, s in enumerate(STEPS) if s["id"] == sid][0]  # noqa: E731
    m = [dict(s) for s in STEPS]
    _t = _at("territory")
    m[_t] = dict(m[_t]); m[_t]["status"] = "PROBABLY FINE"
    muts.append(("M3 status outside vocabulary", m, "I3", "outside the vocabulary"))

    # M4 — a duplicate id (two rows claiming one fact)
    m = [dict(s) for s in STEPS]
    _s = _at("secrets_keeper")
    m[_s] = dict(m[_s]); m[_s]["id"] = m[_t]["id"]
    muts.append(("M4 duplicate id", m, "I4", "duplicate step ids"))

    # M5 — the anti-overclaim rule. This one attacks the RUNNER, not the data: a probe that RAISES
    #      must answer UNKNOWN. The mutation is a runner that SWALLOWS the exception into "DONE",
    #      which is exactly the defect this invariant exists for, and I5 must catch it.
    def _lying_probe_state(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — the lie
            return "DONE", "swallowed"
    muts.append(("M5 a runner that swallows a RAISING probe into DONE", None, "I5", "not UNKNOWN"))

    # M6 — a `say` with no instruction and no promise. Addressed BY ID for the same reason as M3/M4;
    #      it still aims at the row that used to sit at index 4 (`speaker`).
    m = [dict(s) for s in STEPS]
    _sp = _at("speaker")
    m[_sp] = dict(m[_sp]); m[_sp]["say"] = "This is blocked."
    muts.append(("M6 a bare-prose `say`", m, "I2", "no instruction"))

    # ── I7's THREE MUTATIONS, AT PLAN LEVEL RATHER THAN STEP LEVEL ────────────────────────────────
    #    They are separate from the loop below because they attack a PLAN, not a step list — and
    #    because the defect they guard is the one this estate found nine times in a day, so it gets
    #    its own proof rather than riding along on someone else's.
    live_plan = plan()
    i7_muts = [
        ("M7 the recommended option reported DONE with NOTHING FETCHED",
         _lied_plan(live_plan, state="DONE", option_state="NOT-INSTALLED"), "I7"),
        ("M8 a DECLINED option raised as next_actionable",
         _lied_plan(live_plan, state="DECLINED", next_actionable="gev_option"), "I7"),
        ("M9 a recommended row that lost its terms",
         _lied_plan(live_plan, drop_terms=True), "I7"),
        ("M10 a decline counted as done",
         {**_lied_plan(live_plan, state="DONE"), "declined": 1}, "I7"),
    ]

    bad = 0
    for name, steps, inv, expect in muts:
        if steps is None:
            # a runner-level mutation: point the invariant at the LYING runner
            fails = _i5_fails(_lying_probe_state)
        else:
            fails = _fails_for(steps)
        red = any(expect in f for f in fails)
        print(f"  {'RED  ' if red else 'GREEN'}  {name}  -> expected {inv} red: {'yes' if red else 'NO'}")
        if not red:
            print(f"         failures seen: {fails[:3]}")
            bad += 1

    for name, lied, inv in i7_muts:
        fails = _i7_fails(lied)
        red = bool(fails)
        print(f"  {'RED  ' if red else 'GREEN'}  {name}  -> expected {inv} red: {'yes' if red else 'NO'}")
        if not red:
            print("         nothing failed — the guard does not bite this defect")
            bad += 1

    # ── I8's FIVE MUTATIONS. The voice boundary is a WARNING, and every way a warning lies gets its
    #    own planted defect -- including the two that leave the walk green while saying nothing.
    vsteps = [dict(s) for s in STEPS]
    _vi = [i for i, s in enumerate(vsteps) if s["id"] == "voice_boundary"]
    i8_muts = []
    if _vi:
        i = _vi[0]
        m = [dict(s) for s in vsteps]
        m[i]["typing"] = ""; m[i]["button"] = {"label": "", "action": ""}
        i8_muts.append(("M11 the boundary row loses BOTH approval paths", None, m, None))
        m = [dict(s) for s in vsteps]
        m[i]["id"] = "not_the_boundary"
        i8_muts.append(("M12 the requirement is ABSENT from the walk", None, m, None))
    else:
        i8_muts.append(("M12 the requirement is ABSENT from the walk (already true!)", None, vsteps, None))
    i8_muts.append(("M13 a LOUD warning with NOTHING detected", None, None,
                    {"state": "warn", "detected": [], "line": "something may be leaking"}))
    i8_muts.append(("M14 a QUIET boundary with a BLIND detector", None, None,
                    {"state": "quiet", "unmeasured": ["D1 could not look"]}))
    i8_muts.append(("M15 the warning turned into a BLOCK", None, None, None))

    for name, _unused, steps, block in i8_muts:
        if steps is not None:
            fails = _i8_fails(None, steps)
        elif name.startswith("M15"):
            fails = _i8_fails(_lied_boundary(live_plan, row_state="BLOCKED",
                                             next_actionable="voice_boundary"), STEPS)
        else:
            fails = _i8_fails(_lied_boundary(live_plan, block=block), STEPS)
        red = bool(fails)
        print(f"  {'RED  ' if red else 'GREEN'}  {name}  -> expected I8 red: {'yes' if red else 'NO'}")
        if not red:
            print("         nothing failed — the guard does not bite this defect")
            bad += 1

    # ── I9's FOUR MUTATIONS. The lead-agent suggestion is the operator's requirement to the NEW
    #    installer, and every way that suggestion can lie gets its own planted defect -- including
    #    the two that leave the walk green while telling the new user nothing.
    i9_muts = [
        ("M16 a row that rendered a ZERO where it could not look",
         _lied_lead(live_plan, evidence="0", state="DONE")),
        ("M17 the suggestion ABSENT from the walk",
         None),
        ("M18 a row that claims ENFORCING and states no limit",
         _lied_lead(live_plan, why="Give the assistant a lead agent.")),
    ]

    for name, lied in i9_muts:
        if lied is None:
            fails = _i9_fails(None, [s for s in STEPS if s["id"] != "lead_agent"])
        else:
            fails = _i9_fails(lied, STEPS)
        red = bool(fails)
        print(f"  {'RED  ' if red else 'GREEN'}  {name}  -> expected I9 red: {'yes' if red else 'NO'}")
        if not red:
            print("         nothing failed — the guard does not bite this defect")
            bad += 1

    # M19 — the suggestion TAKES instead of ASKING, planted at STEP level (a source edit, which the
    #       plan-level mutations above cannot see).
    m = [dict(s) for s in STEPS]
    _la = _at("lead_agent")
    m[_la] = dict(m[_la]); m[_la]["button"] = {"label": "Grant it", "action": "grant:lead_agent"}
    fails = _i9_fails(None, m)
    red = bool(fails)
    print(f"  {'RED  ' if red else 'GREEN'}  M19 the suggestion TAKES instead of ASKS  "
          f"-> expected I9 red: {'yes' if red else 'NO'}")
    if not red:
        print("         nothing failed — the guard does not bite this defect")
        bad += 1
    i9_total = len(i9_muts) + 1

    total_muts = len(muts) + len(i7_muts) + len(i8_muts) + i9_total
    print()
    print(f"  {'\u2717 INVALID' if bad else '\u2713 VALID'} — {total_muts - bad}/{total_muts} "
          f"mutations reddened their own invariant")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
