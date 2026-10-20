#!/usr/bin/env python3
"""gev_option.py — THE ONE STORE of the GODS EYE VIEW recommendation in the setup walk.

★★★★★ WHY THIS FILE EXISTS, AND WHY IT IS A STORE AND NOT A CHECKBOX.

    The operator, verbatim: *"include gods eye view download and install as a recommended
    suggestion/option in the installation and let's make sure we accommodate it in this build.
    We want to give a good experience."*

    ★ THE EXPERIENCE IS THE DELIVERABLE, NOT THE INSTALL. So this module does NOT install anything,
      does NOT fetch models, and does NOT launch the app. It answers three questions and refuses the
      fourth:
        · WHAT IS IT?          `TERMS["gives"]`      — in words he can read before choosing.
        · WHAT DOES IT COST?   `TERMS["requires"]`   — including the Google key WITH BILLING.
        · WHERE DOES IT PHONE? `TERMS["egress"]`     — the third parties, named.
        · WHAT ARE ITS TERMS?  `TERMS["licence"]`    — MIT code, third-party data, TeleGeography NC.
      And the fourth thing — INSTALL IT — is not here. The estate's rule, quoted on every grant
      surface: **"It may ASK. It may not TAKE."**

★★★ AN INFORMED NO IS THE FEATURE. A "recommended" option that does not state its cost and its
    egress is not a good experience, it is a SURPRISE. So `DECLINED` is a FIRST-CLASS SETTLED STATE
    here — not a failure, not a nag, not folded into "done". He can decline once and never see it
    again, and he can change his mind from the same row.

★★★★ "RECOMMENDED" MUST NEVER RENDER AS DONE WHEN NOTHING WAS FETCHED.
    This estate found that defect nine times in one day. So the mapping from measurement to walk
    state is one function, `walk_state()`, it is TOTAL, and `NOT-INSTALLED` / `INSTALLED-NO-KEY`
    are PROVED to be incapable of reaching `DONE` — see `_g3_fails()` and its prove-fail, which
    plants a liar that maps them to DONE and REQUIRES the guard to go red.

★★ AND A KEY IS NEVER TYPED INTO ANYTHING THAT LOGS (`AGENTS.md`: *"Never log key material —
    fingerprints and last-4 only"*). This store never emits a key value. `key_report()` emits
    PRESENCE plus a last-4 tail plus a sha256 prefix, and `_g1_fails()` proves the raw cannot appear
    in any output it produces — with a prove-fail that PLANTS a distinctive key and requires the raw
    substring to be ABSENT. The key itself is entered in GEV's OWN provider-settings surface, which
    writes it 0600 to GEV's own `pinokio/ENVIRONMENT`. There is no field on our surface for it.

★ WHAT IS MEASURED, AND HOW. Read-only, bounded, no filesystem walk:
    · the checkout            — a FIXED tuple of candidate paths, each identified by its
                                `package.json` `"name": "gods-eye-view"` (a directory NAME is not
                                proof; the port-9000 trap is what happens when a name is taken as
                                proof of identity).
    · installed               — `node_modules/` present.
    · keys                    — GEV's OWN config files, uncommented non-empty `KEY=` lines only.
    · running                 — a loopback port from a FIXED candidate set that IDENTIFIES ITSELF as
                                GEV by title. Something listening is not GEV; Portainer owns :9000 on
                                this box and is exactly the false positive this avoids.
    · node                    — `node --version`, parsed against GEV's own `engines` field.

REACH: new-only. This module writes exactly one small file — its own choice ledger under `var/`.
       It installs nothing, launches nothing, and reads no key value.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "var" / "gev_choice.json"

# ── the option's OWN closed state set. Six words. DECLINED is the one that makes it a choice, and
#    UNKNOWN is the one that keeps a probe we could not run from being folded into a pass.
OPTION_STATES = ("NOT-INSTALLED", "INSTALLED-NO-KEY", "INSTALLED-UNRUNNABLE", "INSTALLED-KEYED",
                 "RUNNING", "DECLINED", "UNKNOWN")

# ★ THE MAPPING IS THE HONESTY. One function, total over OPTION_STATES, and the states that mean
#   "this cannot work right now" are incapable of reaching DONE. I3's sibling in `setup_blockers.py`
#   still holds because DECLINED is added there as a deliberate fifth word, not invented here.
_WALK_STATE = {
    "NOT-INSTALLED":         "BLOCKED",
    "INSTALLED-NO-KEY":      "BLOCKED",
    # ★ MEASURED 2026-10-12, AND IT IS WHY THIS WORD EXISTS: the panel's own PATH resolves `node` to
    #   /usr/bin/node = v22.22.2, and GEV declares `engines.node: ">=24.14.0 <25 || >=26 <27"`. So a
    #   GEV that IS fully installed, WITH a key, still cannot start in that context. Before this word
    #   existed the probe said "satisfies engines" WITHOUT EVER CHECKING — a claim, printed as a
    #   measurement. That is the estate's oldest defect and it was in this file for one hour.
    "INSTALLED-UNRUNNABLE":  "BLOCKED",
    "INSTALLED-KEYED":       "DONE",
    "RUNNING":               "DONE",
    "DECLINED":              "DECLINED",
    # ★ "I COULD NOT LOOK" IS NOT "IT IS FINE" (COWORK_PROTOCOL §2.7). A probe that raised lands
    #   here, and walk_state() refuses to carry it anywhere but UNKNOWN.
    "UNKNOWN":               "UNKNOWN",
}

# ── WHERE A CHECKOUT MAY LIVE. A fixed tuple, because a full-filesystem walk is forbidden on this box
#    (the handoff measured load above 30 when something did that). Both known checkouts, newest first.
CHECKOUTS = (
    HOME / "gods-eye-view-v021",
    HOME / "gods-eye-view",
    HOME / "Downloads" / "gods-eye-view",
)

# ── IDENTITY: a directory named "gods-eye-view" is not proof. Its own package.json name is.
PACKAGE_NAME = "gods-eye-view"
TITLE_MARKER = "God's Eye View"

# ── THE PORTS GEV CAN BE ON. Pinokio injects `PORT`; without Pinokio it is Vite's own default. A
#    fixed set, probed by IDENTITY rather than by "something answered".
PORT_CANDIDATES = (5173, 4173)

# ── GEV'S OWN KEY NAMES, read from its `pinokio/_ENVIRONMENT`. Names only; this store never reads
#    any other value, and never emits one.
KNOWN_KEYS = (
    "GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_API_KEY", "CESIUM_ION_TOKEN",
    "OPENAI_API_KEY", "AISSTREAM_API_KEY", "FIRMS_MAP_KEY", "TOMTOM_API_KEY",
    "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "LL2_API_TOKEN",
)
# ★ THE GLOBE NEEDS THIS ONE, AND IT NEEDS BILLING. Named separately because "installed but no key"
#   is a different state from "installed", and collapsing them is the defect class this file guards.
GLOBE_KEYS = ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_SERVER_API_KEY")
# ★ AND `OPENAI_API_KEY` IS THE ONE THAT CHANGES THE DOCTRINE — see VOICE below.
VOICE_KEY = "OPENAI_API_KEY"

# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  ★★★★★ THE DISCLOSURE. This is THE ONE STORE of it. A page that restates any line here has made a
#  second copy of a fact, and the second copy is the one that goes stale.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

TERMS = {
    "gives": (
        "A photorealistic 3D globe in your browser with live aircraft, ships, satellites, "
        "earthquakes, wildfire perimeters, traffic, weather radar and public cameras — plus a "
        "hands-free voice agent. Its own words: \u201ca spy-satellite simulator in your browser — "
        "then you realize the sources are public and the data is real.\u201d"
    ),
    "requires": (
        "A Google Maps Platform key WITH BILLING ENABLED — the 3D globe does not render without it, "
        "and it is a paid, metered account, not a free tier you can forget about. "
        "Node.js 24.14+ (measured on this box: present). "
        "Several GB of disk if the 3D model assets are fetched — we do NOT fetch them for you. "
        "A microphone and a browser permission, if you want the voice agent."
    ),
    # ★ NAMED, NOT SUMMARISED. "It makes network requests" is not a disclosure.
    "egress": (
        "It makes OUTBOUND REQUESTS TO THIRD PARTIES while it runs. At minimum: "
        "Google (Maps Platform: 3D tiles, geocoding, Places, Street View) · OpenSky Network "
        "(live flights) · adsb.lol (flights) · AISStream.io (ships) · CelesTrak (orbital elements) · "
        "The Space Devs (launches). Also, depending on which layers you switch on: NASA FIRMS "
        "(fire), TomTom (traffic), Open-Meteo and NOAA nowCOAST (weather), and USGS (earthquakes). "
        "You choose the layers, so you choose which of these it talks to — but it talks to Google "
        "the moment the globe renders."
    ),
    "licence": (
        "MIT for the CODE ONLY. The bundled DATA keeps its own terms and several sources are "
        "NON-COMMERCIAL: \u2605 TeleGeography submarine-cable data is CC BY-NC-SA 3.0 \u2014 "
        "NONCOMMERCIAL, so a commercial deployment must delete that one folder or license it from "
        "TeleGeography. OpenSky Network is non-commercial research/education, and Google News RSS "
        "is restricted to personal noncommercial use. No warranty, no support, no liability."
    ),
    "attribution": (
        "In-app attribution is REQUIRED and must stay intact. The Google Maps / Cesium credit line "
        "is pinned on-screen at the bottom-left and remains visible even in clean-view and recording "
        "modes, and every layer registers its own credit in the expandable \u201cData "
        "attribution\u201d popover. If you remove the credits you are outside the licence, not just "
        "outside the convention."
    ),
    "voice": (
        "\u2605\u2605 ITS VOICE AGENT IS CLOUD-BACKED AND THIS IS A DOCTRINAL CONFLICT. GEV's "
        "hands-free control connects the browser DIRECTLY to OpenAI's Realtime API over WebRTC "
        "(api.openai.com) and streams RAW MICROPHONE AUDIO there. This estate's hard gate is "
        "\u201cRaw audio never leaves the box. Not a setting.\u201d So the voice half of GEV is "
        "UNAVAILABLE on this box by doctrine, and the globe half is not affected by that. Choosing "
        "GEV here does not enable its voice agent."
    ),
    # \u2605\u2605\u2605 THE POSITIVE HALF, ADDED 2026-10-12, AND WHY IT BELONGS ON THIS CARD.
    #     The paragraph above is what GEV WOULD do if you armed it. On its own that reads as a
    #     refusal, and it is not one -- it is the reason we do not use that path. Under this design
    #     you give GEV TEXT, not your microphone: you speak to the seat, the seat transcribes on this
    #     box, and the sentence is what GEV acts on. \u2605 THE STATEMENT ITSELF IS NOT RESTATED HERE.
    #     It lives once in `operator/setup_blockers.VOICE_BOUNDARY` and this card POINTS AT it, for
    #     the same reason this module does not keep a second copy of the cost of a key: the second
    #     copy is the one that goes stale, and a stale disclosure misinforms the person reading it.
    "voice_boundary": (
        "The path we DO use: you speak to this seat, the seat transcribes locally, and what reaches "
        "God's Eye View \u2014 or any other integration \u2014 is TEXT. Its own cloud voice path is "
        "never armed, and this is a statement about the design rather than a promise about GEV's "
        "behaviour: nothing in GEV is patched or disabled, so what keeps it off is that the "
        "microphone is the seat's and the credential its voice path needs is not on this box."
    ),
    "cost_note": (
        "Billing is YOURS and it is metered. GEV ships rate limits as app-level guards, and says "
        "plainly that provider-side budgets remain authoritative — meaning the cap that actually "
        "protects you is the one you set in the Google Cloud console, not the one in the app."
    ),
}

# ★ THE SIX THE OPERATOR NAMED, so a later edit cannot quietly drop one. `_g5_fails` asserts SET
#   INCLUSION against TERMS["egress"], not against a re-typed list.
EGRESS_NAMED = ("Google", "OpenSky Network", "adsb.lol", "AISStream.io", "CelesTrak",
                "The Space Devs")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  MEASUREMENT. Every probe is read-only and bounded. None of them raises into a passing state.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, timeout: int = 8) -> tuple:
    """(rc, stdout, stderr). Never raises — a probe that dies is UNKNOWN, never DONE."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except FileNotFoundError as e:
        return -1, "", f"not installed: {e.filename}"
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return -1, "", f"{type(e).__name__}: {e}"


def find_checkout(candidates=None) -> tuple:
    """(path, why) for the first candidate whose OWN package.json says it is gods-eye-view.

    ★ IDENTITY IS THE MANIFEST, NOT THE FOLDER NAME. `~/Downloads/gods-eye-view` and
      `~/gods-eye-view-v021` are two checkouts of the same project and only the second has run; a
      folder named for a program is not evidence that the program is in it."""
    for c in (candidates if candidates is not None else CHECKOUTS):
        p = Path(c)
        pj = p / "package.json"
        if not pj.is_file():
            continue
        try:
            if json.loads(pj.read_text(encoding="utf-8", errors="replace")).get("name") == PACKAGE_NAME:
                return p, f"package.json name={PACKAGE_NAME!r} at {p}"
        except Exception:  # noqa: BLE001 — an unreadable manifest is not a match
            continue
    return None, f"no checkout whose package.json names {PACKAGE_NAME!r} in {len(candidates or CHECKOUTS)} candidate path(s)"


def node_candidates() -> list:
    """Absolute paths only. ★★★ NEVER `shutil.which`, AND THIS IS A MEASURED RULING, NOT A STYLE.

    ★ The panel is ONE LONG-LIVED PROCESS. Other modules mutate its global `os.environ["PATH"]` at
      runtime — measured on this box at `operator/guide_backends.py:712` and
      `operator/service_auth_check.py:153`, both of which prepend a node directory to the process
      PATH. So a `which("node")` evaluated inside a route returns whatever some EARLIER REQUEST left
      behind, and this probe's answer changed between two reads of the same unchanged box:
      /usr/bin/node v22.22.2 on the first, nvm v20.19.6 on the second. A measurement that depends on
      which pages were opened first is not a measurement.

    ★ SO NODE IS RESOLVED FROM THE FILESYSTEM, deterministically, highest version first, and the
      answer is the same on every read regardless of what the process has done to its own PATH."""
    out = []
    nvm = HOME / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        try:
            for d in sorted(nvm.iterdir(), reverse=True):
                p = d / "bin" / "node"
                if p.is_file() and os.access(p, os.X_OK):
                    out.append(p)
        except Exception:  # noqa: BLE001 — an unreadable nvm is simply not a candidate
            pass
    for p in (Path("/usr/local/bin/node"), Path("/usr/bin/node"), HOME / ".local" / "bin" / "node"):
        if p.is_file() and os.access(p, os.X_OK):
            out.append(p)
    return out


def node_report(eng_range) -> dict:
    """Measure EVERY node on the box and decide, deterministically.

    Returns {found, best, best_ok, any_ok}. `best` is the highest version that SATISFIES the range if
    one exists, else the highest version at all — so the caller can always name the best thing the
    box actually has, whether or not it is good enough."""
    found = []
    for p in node_candidates():
        rc, out, _ = _run([str(p), "--version"])
        if rc == 0 and out.strip():
            v = out.strip().lstrip("v")
            ok, _why = engines_ok(v, eng_range)
            found.append({"version": v, "path": str(p), "ok": ok})
    found.sort(key=lambda f: (_vt(f["version"]), f["ok"] is True), reverse=True)
    ok_ones = [f for f in found if f["ok"] is True]
    return {
        "found": found,
        "best": (ok_ones[0] if ok_ones else (found[0] if found else None)),
        "any_ok": bool(ok_ones),
        "best_ok": bool(ok_ones),
    }


def node_version() -> tuple:
    """(version, resolved_binary, why) for the HIGHEST node on the box — deterministic.

    ★ Kept as a name because other code reads it; it no longer consults PATH."""
    cands = node_candidates()
    if not cands:
        return None, None, "no node binary found at any known absolute path"
    p = cands[0]
    rc, out, err = _run([str(p), "--version"])
    if rc != 0:
        return None, str(p), f"{p} --version failed: {err.strip() or rc}"
    return out.strip().lstrip("v"), str(p), f"{p} --version"


# ── A REAL engines CHECK, BECAUSE THE ALTERNATIVE IS A CLAIM. GEV's range is `||` of
#    space-separated comparator clauses; that is the whole grammar this needs, and it is implemented
#    rather than eyeballed. An unparseable range answers None ("could not look"), never True.
_CLAUSE = re.compile(r"^(>=|<=|>|<|=)?\s*v?(\d+(?:\.\d+)*)$")


def _vt(s: str) -> tuple:
    n = [int(x) for x in re.findall(r"\d+", s or "")]
    while len(n) < 3:
        n.append(0)
    return tuple(n[:3])


def _sat_clause(ver: tuple, clause: str):
    """True/False, or None when the clause is not something this checker understands."""
    m = _CLAUSE.match(clause.strip())
    if not m:
        return None
    op, raw = (m.group(1) or "="), m.group(2)
    ref = _vt(raw)
    if op == "=":
        # npm-style: a partial like `26` or `24.14` is a PREFIX range; a full x.y.z is exact.
        n = len(raw.split("."))
        return ver[:n] == ref[:n]
    if op == ">=":
        return ver >= ref
    if op == "<=":
        return ver <= ref
    if op == ">":
        return ver > ref
    if op == "<":
        return ver < ref
    return None


def engines_ok(node_ver, range_str) -> tuple:
    """(True|False|None, why). None means COULD NOT DETERMINE — never folded into True."""
    if not node_ver:
        return None, "no node version to compare"
    if not str(range_str or "").strip():
        return None, "the package declares no engines.node, so nothing is required of it"
    v = _vt(str(node_ver))
    verdicts = []
    for or_part in str(range_str).split("||"):
        clauses = or_part.split()
        if not clauses:
            continue
        vals = [_sat_clause(v, c) for c in clauses]
        if any(x is None for x in vals):
            verdicts.append(None)
            continue
        verdicts.append(all(vals))
    if not verdicts or any(x is None for x in verdicts):
        return None, f"could not parse engines.node {range_str!r}"
    ok = any(verdicts)
    return ok, (f"node {node_ver} {'satisfies' if ok else 'DOES NOT SATISFY'} "
                f"engines.node {range_str!r}")


def _fingerprint(value: str) -> str:
    """★★★ LAST-4 AND A FINGERPRINT. NEVER THE VALUE. (`AGENTS.md` §6)

    A short value is NOT partially disclosed — under 8 characters the last-4 would be the whole
    secret for a short one, so it is withheld entirely. This function is the ONLY place in this
    module that touches a key value, and its output is the only thing that can escape."""
    v = (value or "").strip()
    if not v:
        return "absent"
    if len(v) < 8:
        return "set \u00b7 (too short to fingerprint safely)"
    return f"set \u00b7 \u2026{v[-4:]} \u00b7 sha256:{hashlib.sha256(v.encode()).hexdigest()[:8]}"


def key_report(env_files=None) -> dict:
    """{key_name: fingerprint-or-'absent'} for GEV's OWN config files. Values never leave here.

    ★ GEV'S CONFIG, GEV'S FORMAT. We do not read `.env` conventions we invented; we read the file
      GEV's own `pinokio/_ENVIRONMENT` documents, and only UNCOMMENTED non-empty assignments count.
      A commented `# GOOGLE_MAPS_API_KEY=` is `absent`, which is exactly what it is."""
    out = {k: "absent" for k in KNOWN_KEYS}
    # ★ DERIVED FROM `CHECKOUTS`, SO THE CHECKOUT LIST EXISTS ONCE. This was a hardcoded copy of two of
    #   the three candidates, and it therefore missed `~/gods-eye-view/.env` — a key saved by THAT
    #   checkout's dev server (`server/standalone/key-setup.js`: the non-Pinokio path writes the
    #   repo-root `.env`; the Pinokio path writes `pinokio/ENVIRONMENT`) would have been reported
    #   ABSENT while it was set. One store per fact, pointed at rather than copied.
    #   ★ `pinokio/_ENVIRONMENT` IS DELIBERATELY NOT READ: it is GEV's TEMPLATE, so reading it would
    #     report placeholder text as a key that is set — a false PRESENT, which is worse than a false
    #     absent because it would have the row claim a globe that cannot render.
    files = env_files if env_files is not None else [
        p for c in CHECKOUTS for p in (c / "pinokio" / "ENVIRONMENT", c / ".env")
    ]
    for f in files:
        p = Path(f)
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                k = k.strip()
                if k in out:
                    out[k] = _fingerprint(v.strip().strip('"').strip("'"))
        except Exception:  # noqa: BLE001 — an unreadable config is absent keys, not a crash
            continue
    return out


def identify_gev(port: int, timeout: float = 2.0) -> tuple:
    """(True, why) only if something on `port` IDENTIFIES ITSELF as GEV.

    ★★ SOMETHING LISTENING IS NOT GEV. :9000 on this box answers 200 and is Portainer. A probe that
       counted "a socket is open" would report GEV running because Docker has a UI."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as r:
            body = r.read(65_536).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}"
    if TITLE_MARKER in body:
        return True, f"http://127.0.0.1:{port}/ served {TITLE_MARKER!r}"
    return False, f"port {port} answered but is NOT {TITLE_MARKER!r}"


def probe(choice=None, candidates=None, env_files=None, ports=None, identify=None) -> tuple:
    """(OPTION_STATE, evidence). Read-only. Returns one of OPTION_STATES.

    ★ EVERY PARAMETER IS A NAMED SEAM, on the same reasoning as `setup_blockers._probe_state`: the
      guards in `_g1_fails`..`_g6_fails` must be drivable to RED against a planted input, or they are
      guards we are hoping about.

    ★ AND DECLINED IS CHECKED FIRST. Once he has said no, the row reports `DECLINED` and STOPS — it
      does not re-measure and re-recommend at him. That is the whole difference between an offer and
      a nag. Declining is reversible from the same row, which re-measures only after he asks."""
    dec = get_choice() if choice is None else choice
    if (dec or {}).get("decision") == "declined":
        at = (dec or {}).get("at") or "an unrecorded time"
        return "DECLINED", (f"you declined this on {at} — nothing was fetched and nothing is "
                            f"running. Press \u201cReconsider\u201d, or re-measure, to take it back.")

    path, why = find_checkout(candidates)
    if path is None:
        nv, np_, _w = node_version()
        return "NOT-INSTALLED", (f"{why}. Best node on the box: {nv or 'NONE FOUND'}"
                                 f"{f' at {np_}' if np_ else ''}. "
                                 f"No dependency was installed and nothing was fetched by this probe.")

    installed = (path / "node_modules").is_dir()
    engines = {}
    manifest_version = None
    try:
        # ★ THE VERSION IS READ HERE AND CARRIED, NOT REMEMBERED. `step()["technical"]` named
        #   "gods-eye-view 0.2.1" as a LITERAL while this manifest was already open two lines away —
        #   a claim printed as a measurement, which is this file's own oldest defect class. The
        #   checkout list holds a second candidate (`~/gods-eye-view`, version 0.1.0), so the literal
        #   would have gone on saying 0.2.1 about a 0.1.0 build the moment -v021 moved.
        _manifest = json.loads((path / "package.json").read_text(
            encoding="utf-8", errors="replace"))
        engines = _manifest.get("engines", {}) or {}
        manifest_version = _manifest.get("version")
    except Exception:  # noqa: BLE001
        engines = {}
    eng_range = engines.get("node")
    rep = node_report(eng_range)
    best = rep["best"] or {}
    nv, npath = best.get("version"), best.get("path")
    found_note = " · ".join(f"v{f['version']} at {f['path']}" for f in rep["found"]) or "none found"
    # ★ WHICH BUILD, NOT ONLY WHERE. Derived from the manifest read above, so every piece of evidence
    #   this probe publishes names the version that was actually MEASURED. A missing `version` field is
    #   said out loud rather than silently omitted, for the same reason a missing key is `absent`.
    where = (f"{path} (package.json version {manifest_version})" if manifest_version
             else f"{path} (package.json carries no version)")

    if not installed:
        # ★ THE WORD MUST MATCH ITS OWN EVIDENCE. This returned "INSTALLED-NO-KEY" while the sentence
        #   beside it read "That is not installed" — so the served `option_state` told the operator the
        #   option was INSTALLED and merely unkeyed, which sends him to buy a Google key to fix a
        #   missing `npm install`. That is the same mis-direction the node-vs-key ordering below exists
        #   to prevent, one branch earlier. `NOT-INSTALLED` is also the word `setup_blockers._i7_fails`
        #   already classifies as NOTHING FETCHED, so this stays inside the existing cross-lane contract
        #   and needs no edit in a file this lane does not own.
        return "NOT-INSTALLED", (
            f"checkout at {where} but node_modules/ is ABSENT — the code is here and its "
            f"dependencies are NOT. That is not installed; nothing was fetched.")

    # ★★ THE NODE CHECK COMES BEFORE THE KEY CHECK, AND THE ORDER IS THE POINT. A missing key and a
    #    missing runtime are different problems with different fixes, and reporting "no key" for a box
    #    whose node cannot run GEV at all would send him to buy a Google key to fix a PATH.
    #    ★ AND IT IS ONLY A BLOCKER IF **NO** NODE ON THE BOX QUALIFIES. This box HAS a qualifying
    #      node (nvm v24.15.0); calling that unrunnable would be a false alarm, and a false alarm is
    #      the same lie as a false pass — it just costs a different afternoon.
    if rep["found"] and not rep["any_ok"]:
        return "INSTALLED-UNRUNNABLE", (
            f"installed at {where} (node_modules present) but NO node on this box satisfies "
            f"engines.node {eng_range!r}. Found: {found_note}. GEV cannot be started here until a "
            f"qualifying runtime exists. NOT counted as done: this cannot start as the box is "
            f"configured.")

    keys = key_report(env_files)
    globe = [k for k in GLOBE_KEYS if keys.get(k, "absent") != "absent"]
    voice = keys.get(VOICE_KEY, "absent") != "absent"
    # ★★★ LATENT vs ARMED, MEASURED, AND THE DISTINCTION IS THE WHOLE POINT OF SAYING IT AT ALL.
    #     "It would do this if you gave it a key" and "it does this" are different claims, and only
    #     the first is true here. The word is derived from the same read that produces the
    #     fingerprint, so it cannot drift from the key's actual presence -- and a voice half reported
    #     as ARMED is a voice half that could start today.
    voice_half = ("ARMED \u2014 a key for its cloud voice path IS set on this box"
                  if voice else
                  "LATENT \u2014 the credential its cloud voice path needs is ABSENT, so it "
                  "cannot start")
    keyline = (f"Google key: {keys.get('GOOGLE_MAPS_API_KEY', 'absent')} · "
               f"OpenAI key: {keys.get(VOICE_KEY, 'absent')} · voice half: {voice_half}")
    if rep["found"]:
        nodeline = f"node v{nv} at {npath} satisfies engines.node {eng_range!r}"
        if len(rep["found"]) > 1:
            nodeline += (f" \u2014 and it matters WHICH one: found {found_note}. Launch it with "
                         f"{npath}, not with whatever `node` happens to resolve to.")
    else:
        nodeline = ("\u2605 no node found at any known absolute path, so the runtime requirement "
                    "could NOT be determined \u2014 not being called a pass")
    if not globe:
        return "INSTALLED-NO-KEY", (
            f"installed at {where} (node_modules present · {nodeline}) and NO Google Maps key is "
            f"set — the globe will not render. {keyline}. Not counted as done: nothing is fetched "
            f"until a key exists.")

    ident = identify if identify is not None else identify_gev
    for port in (ports if ports is not None else PORT_CANDIDATES):
        ok, why2 = ident(port)
        if ok:
            return "RUNNING", (f"installed at {where} · {why2} · {keyline} · {nodeline}"
                               + ("  \u2605 its voice half is cloud-backed and stays OFF here." if voice else ""))

    return "INSTALLED-KEYED", (
        f"installed at {where} with a Google key and NOT running — "
        f"probed {list(ports if ports is not None else PORT_CANDIDATES)} by identity, none answered "
        f"as {TITLE_MARKER!r}. {keyline} · {nodeline}")


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE RUNNER. ★ A NAMED SEAM, for the same reason `setup_blockers._probe_state` is one: the
#  anti-overclaim rule has to be a thing that can be BROKEN and caught, not an inline `try` that
#  nothing can reach. `prove_fail` points G6 at a runner that swallows the exception into DONE.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def option_state(probe_fn=None, **kw) -> tuple:
    """Run the probe and return (OPTION_STATE, evidence). NEVER raises, NEVER answers DONE for a
    probe that died. A probe that raises is UNKNOWN carrying its reason — the option equivalent of
    `setup_blockers`' I5."""
    try:
        st, ev = (probe_fn or probe)(**kw)
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", f"probe raised {type(e).__name__}: {e} — this option was NOT measured"
    if st not in OPTION_STATES:
        return "UNKNOWN", f"probe returned {st!r}, outside {OPTION_STATES}"
    return st, ev


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE CHOICE. Small, reversible, append-a-decision — his, not ours.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def get_choice() -> dict:
    """The recorded decision, or {}. An unreadable ledger is NO DECISION, never a silent 'accepted'."""
    try:
        if LEDGER.is_file():
            d = json.loads(LEDGER.read_text(encoding="utf-8") or "{}")
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def set_choice(decision, note: str = "") -> dict:
    """Record `accepted` · `declined` · or None to clear. ★ This is the ONLY writer in this module.

    ★ IT IS REVERSIBLE ON PURPOSE. A recommendation you cannot withdraw is not a recommendation, it
      is a default. Clearing the decision returns the row to measurement."""
    d = (decision or "").strip().lower() or None
    if d not in (None, "accepted", "declined"):
        raise ValueError(f"decision must be accepted/declined/None, not {decision!r}")
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    rec = {} if d is None else {
        "decision": d,
        "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "note": note or "",
    }
    tmp = LEDGER.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    os.replace(tmp, LEDGER)
    return rec


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE GUARDS. Each one is a function of an injectable input, so prove-fail can aim at it.
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def _g1_fails(texts, planted: str) -> list:
    """G1 — NO KEY VALUE ESCAPES. Given the probes' published output and a value that was planted in
    the config, the raw value must appear NOWHERE. Runs against a PLANTED key in prove-fail, which is
    the only way to test an absence."""
    if planted and any(planted in (t or "") for t in texts):
        return [f"G1 a raw key value appeared in the option's output — this is the exact defect "
                f"`AGENTS.md` §6 forbids ('never log key material — fingerprints and last-4 only')"]
    return []


def _g1_short_fails(fingerprint_fn=None) -> list:
    """G1's second teeth — A SHORT SECRET IS NOT PARTIALLY DISCLOSED.

    ★ `last-4` is the sanctioned disclosure for a REAL key, and it is a leak for a short one: the
      last four characters of a three-character value are the whole value. So the seam is the
      helper itself, and prove-fail aims this at a helper that discloses. Without this the guard
      would only ever test the long case, which is the case that was never at risk."""
    fn = fingerprint_fn or _fingerprint
    fails = []
    # ★ DISTINCTIVE PROBE VALUES, NOT LETTERS. A substring check against "a" matches the word
    #   "absent" and reports a leak that is not there — a guard that cries wolf is a guard that gets
    #   switched off. These values cannot occur in the safe message by accident.
    short_vals = ("9", "9Z", "9Zq", "9Zq7", "9Zq7x", "9Zq7x2")
    # (a) THE OUTPUT MUST NOT DEPEND ON THE VALUE. This is the strong form: a fingerprint of a short
    #     secret that varies at all carries length, prefix, or the value itself.
    outs = {str(fn(v)) for v in short_vals}
    if len(outs) != 1:
        fails.append(f"G1 the fingerprint of a short value VARIES with the value ({len(outs)} "
                     f"distinct outputs across {len(short_vals)} short secrets) — a fingerprint "
                     f"that changes with its input leaks that input")
    # (b) and the value must not appear literally either
    for probe_val in short_vals:
        got = str(fn(probe_val))
        if probe_val in got:
            fails.append(f"G1 a {len(probe_val)}-character secret was disclosed by the fingerprint "
                         f"helper as {got!r} — under 8 characters last-4 IS the secret")
    if "absent" not in str(fn("")):
        fails.append("G1 an empty value is not reported as absent")
    # (c) and the long case must still work, or (a) and (b) could be satisfied by disclosing nothing
    #     — a helper that reports nothing is not a safe helper, it is a blind one.
    long_out = str(fn("gevPROVEFAILlongvalue0123456789"))
    if not long_out.startswith("set") or "6789" not in long_out:
        fails.append(f"G1 a real-length value is not fingerprinted ({long_out!r}) — the probe must "
                     f"still be able to say WHICH key is set without saying what it is")
    return fails


def _g7_fails(checker=None, probe_fn=None) -> list:
    """G7 — ★★★ THE RUNTIME CLAIM IS MEASURED, NOT ASSERTED.

    ★ THIS GUARD EXISTS BECAUSE THE BUG WAS REAL AND IT WAS IN THIS FILE. The first version of the
      probe printed `node 22.22.2 satisfies engines {"node": ">=24.14.0 <25 || >=26 <27"}` — the word
      "satisfies" hard-coded, the comparison never performed. Node 22.22.2 does not satisfy that
      range. It was caught by reading the live output, not by a test, which is exactly the argument
      for this guard existing.

    It requires, by name:
      · a version INSIDE the declared range to be reported as satisfying
      · a version OUTSIDE it to be reported as NOT satisfying — the half that was missing
      · an unparseable range to answer None ("could not determine"), never True
      · and the probe itself to reach INSTALLED-UNRUNNABLE when node fails engines, so the row is
        BLOCKED rather than DONE. A claim in a string is not a state.
    """
    ck = checker or engines_ok
    fails = []
    rng = ">=24.14.0 <25 || >=26 <27"
    for ver, want in (("24.15.0", True), ("24.14.0", True), ("26.1.0", True),
                      ("22.22.2", False), ("24.12.0", False), ("20.19.6", False), ("25.0.0", False)):
        got, _why = ck(ver, rng)
        if got is not want:
            fails.append(f"G7 engines check said {got!r} for node {ver} against {rng!r}, expected "
                         f"{want!r} — the runtime claim must be COMPUTED, not printed")
    if ck("24.15.0", "some nonsense range")[0] is not None:
        fails.append("G7 an unparseable engines range did not answer 'could not determine' — "
                     "not-understood must never become True")
    if ck("24.15.0", None)[0] is not None:
        fails.append("G7 an absent engines range did not answer 'could not determine'")

    # and the STATE must move: a node outside the range, with everything else installed, is BLOCKED.
    try:
        st, ev = (probe_fn or probe)(choice={}, ports=(), identify=lambda p: (False, "not probed"))
    except Exception as e:  # noqa: BLE001
        return fails + [f"G7 the probe raised {type(e).__name__}: {e}"]
    if st == "INSTALLED-UNRUNNABLE" and walk_state(st) != "BLOCKED":
        fails.append(f"G7 a box whose node cannot run GEV maps to {walk_state(st)!r}, not BLOCKED")
    if st in ("INSTALLED-KEYED", "RUNNING") and "DOES NOT SATISFY" in str(ev):
        fails.append("G7 the evidence says node DOES NOT SATISFY engines while the state claims the "
                     "option is ready — the string and the state disagree, and the state is what "
                     "renders")
    return fails


def _g8_fails(report_fn=None, range_str=">=24.14.0 <25 || >=26 <27") -> list:
    """G8 — ★★★ THE NODE MEASUREMENT DOES NOT DEPEND ON THE AMBIENT `PATH`.

    ★ THIS GUARD EXISTS BECAUSE THE BUG WAS REAL AND IT WAS MEASURED, NOT IMAGINED. The first version
      of this probe used `shutil.which("node")`. The panel is ONE LONG-LIVED PROCESS and other modules
      mutate its global `os.environ["PATH"]` at runtime, so the SAME UNCHANGED BOX answered
      `/usr/bin/node v22.22.2` on one read and `nvm v20.19.6` on the next — depending on which pages
      had been opened in between. A measurement that changes when nothing changed is worse than no
      measurement, because it is believed.

    The guard drives exactly that: it reads the report, replaces PATH with a path that has no node on
    it at all, reads again, and REQUIRES the two answers to be identical. `prove_fail` aims it at a
    PATH-consulting implementation and requires it to go red."""
    fn = report_fn or (lambda r: node_report(r))
    saved = os.environ.get("PATH", "")
    fails = []
    try:
        a = fn(range_str)
        os.environ["PATH"] = "/nonexistent/gev/provefail"
        b = fn(range_str)
    except Exception as e:  # noqa: BLE001
        return [f"G8 the node report raised {type(e).__name__}: {e}"]
    finally:
        os.environ["PATH"] = saved
    if a != b:
        fails.append(f"G8 the node measurement CHANGED when PATH changed and nothing else did "
                     f"({(a or {}).get('best')} -> {(b or {}).get('best')}) — a probe whose answer "
                     f"depends on ambient PATH cannot be reproduced, and will be believed anyway")
    if not (a or {}).get("found"):
        fails.append("G8 the node report found NO node at all on a box with four of them — the "
                     "absolute-path resolution is broken, and 'none found' would read as a real gap")
    return fails


def _g2_fails(page_text: str) -> list:
    """G2 — NO CREDENTIAL INPUT ON OUR SURFACE. The option may POINT at GEV's own provider-settings
    surface; it may not grow a field of its own, because a field on a page the agent can read is a
    field in a log."""
    fails = []
    lowered = (page_text or "").lower()
    for bad in ('type="password"', "type='password'", 'name="api_key"', 'name="apikey"',
                'id="api-key"', 'placeholder="sk-'):
        if bad in lowered:
            fails.append(f"G2 the GEV surface carries a credential input ({bad}) — a key must be "
                         f"entered in GEV's own provider settings, on the box, 0600")
    return fails


def _g3_fails(walk_state=_WALK_STATE) -> list:
    """G3 — 'RECOMMENDED' NEVER RENDERS AS DONE WHEN NOTHING WAS FETCHED.

    ★★ This is the invariant the operator's estate has broken NINE TIMES IN ONE DAY. The guard is a
       function of the mapping so prove-fail can point it at a LYING mapping and require red."""
    fails = []
    for s in ("NOT-INSTALLED", "INSTALLED-NO-KEY"):
        got = walk_state.get(s)
        if got != "BLOCKED":
            fails.append(f"G3 option state {s} maps to walk state {got!r}, not 'BLOCKED' — a "
                         f"recommendation with nothing fetched must never read as done")
    if walk_state.get("DECLINED") != "DECLINED":
        fails.append("G3 DECLINED is folded into another state — declining must be its own settled "
                     "answer, or the option nags him forever")
    # and the mapping must be TOTAL over the closed set, so a new state cannot fall through to DONE
    missing = [s for s in OPTION_STATES if s not in walk_state]
    if missing:
        fails.append(f"G3 the option->walk mapping is not total; unmapped: {missing}")
    return fails


def _g4_fails() -> list:
    """G4 — DECLINING WORKS, AND IS REVERSIBLE. Driven against a THROWAWAY ledger path, because a
    guard that proves itself by writing the operator's real decision is a guard that changes the
    thing it is measuring."""
    global LEDGER
    real, fails = LEDGER, []
    try:
        LEDGER = REPO / "var" / "gev_choice.PROVEFAIL-throwaway.json"
        if LEDGER.exists():
            LEDGER.unlink()
        set_choice("declined", "prove-fail")
        st, ev = probe()
        if st != "DECLINED":
            fails.append(f"G4 a recorded decline produced option state {st!r}, not DECLINED")
        if "declined" not in ev.lower():
            fails.append("G4 the decline evidence does not say it was declined")
        set_choice(None)
        st2, _ = probe(candidates=[HOME / "nonexistent-gev-provefail"])
        if st2 == "DECLINED":
            fails.append("G4 the decline was NOT reversible — clearing the decision still reads "
                         "DECLINED")
    except Exception as e:  # noqa: BLE001
        fails.append(f"G4 the decline round trip raised {type(e).__name__}: {e}")
    finally:
        try:
            if LEDGER.exists():
                LEDGER.unlink()
        except Exception:  # noqa: BLE001
            pass
        LEDGER = real
    return fails


def _g5_fails(terms=None) -> list:
    """G5 — THE DISCLOSURE IS COMPLETE. A 'recommended' option is only informed consent if all five
    parts are present AND the six named egress parties survive. Set inclusion, not presence."""
    t = terms if terms is not None else TERMS
    fails = []
    for part in ("gives", "requires", "egress", "licence", "attribution", "voice",
                 "voice_boundary", "cost_note"):
        if not str(t.get(part) or "").strip():
            fails.append(f"G5 TERMS[{part!r}] is empty — the option cannot be chosen informed")
    req = str(t.get("requires") or "").lower()
    if "billing" not in req:
        fails.append("G5 `requires` does not name BILLING — the Google key is metered and paid")
    lic = str(t.get("licence") or "").lower()
    if "mit" not in lic:
        fails.append("G5 `licence` does not name MIT for the code")
    if "noncommercial" not in lic.replace("-", "").replace(" ", ""):
        fails.append("G5 `licence` does not name the TeleGeography NONCOMMERCIAL carve-out")
    missing = [n for n in EGRESS_NAMED if n not in str(t.get("egress") or "")]
    if missing:
        fails.append(f"G5 `egress` has dropped named third parties: {missing}")
    return fails


def _g6_fails(runner=option_state) -> list:
    """G6 — THE ANTI-OVERCLAIM RULE, AIMED AT THIS MODULE'S RUNNER.

    ★ Handed something that RAISES, the runner must answer UNKNOWN (or at least carry the failure in
      its evidence) and must NEVER answer DONE or RUNNING. A probe we could not run is not a pass.
      `prove_fail` hands it a runner that swallows the exception into DONE and requires this to go
      red — an absence only bites when the thing that breaks it is planted."""
    def _boom():
        raise RuntimeError("planted")
    try:
        res = runner(_boom)
    except Exception as e:  # noqa: BLE001
        return [f"G6 a raising probe ESCAPED as {type(e).__name__}: {e} — it must be contained "
                f"into UNKNOWN with its reason, because an uncaught probe takes the whole walk down"]
    st, ev = (res if isinstance(res, tuple) and len(res) == 2 else (res, ""))
    if st in ("DONE", "RUNNING"):
        return [f"G6 a raising probe produced {st!r} — this is the exact shape that reports a broken "
                f"thing as working"]
    if st not in OPTION_STATES:
        return [f"G6 a raising probe produced {st!r}, outside the closed option vocabulary "
                f"{OPTION_STATES} — it must be UNKNOWN carrying the reason"]
    if st == "UNKNOWN" and not str(ev).strip():
        return ["G6 a probe that could not be measured answered UNKNOWN with NO reason — "
                "'I could not look' must say why, or it is indistinguishable from a shrug"]
    return []


# ══════════════════════════════════════════════════════════════════════════════════════════════════
#  THE ROW. Shaped for `setup_blockers.STEPS`, which owns the walk and its invariants (I1..I6).
# ══════════════════════════════════════════════════════════════════════════════════════════════════

def walk_state(option_state: str) -> str:
    """The ONE mapping. Total, and proved incapable of sending an unfetched option to DONE."""
    return _WALK_STATE.get(option_state, "UNKNOWN")


def walk_probe() -> tuple:
    """(WALK_STATE, evidence, extra) — the shape `setup_blockers._probe_state` accepts.

    ★★ WHY THE OPTION STATE RIDES ALONGSIDE AND IS NOT THROWN AWAY. The walk's own vocabulary has
       no word for 'installed but no key'; it has BLOCKED. That is the right thing to SHOW, but it is
       the wrong thing to TEST — I7 has to be able to see that the option measured NOT-INSTALLED
       while some row claimed DONE, and once the mapping has run, that fact is gone. So the option's
       own state travels in `extra` and lands on the row, where the invariant can still reach it.

    ★ AND IT IS ONE MEASUREMENT, NOT TWO. Re-probing inside a `row_extra` hook would run node,
      re-read the manifest and re-probe every port a second time, on a box whose load the operator
      has asked us to watch — for a fact we already had."""
    st, ev = option_state()
    return walk_state(st), ev, {"option_state": st}


def _row_extra() -> dict:
    """The keys that must be read LIVE at plan time rather than frozen at import time.

    ★ THE CHOICE IS LIVE BY CONSTRUCTION. If his decision were captured when the module was
      imported, a decline would not take effect until something restarted — and the whole point of
      the row is that pressing the button changes what the next read says."""
    return {"choice": get_choice(), "terms": TERMS}


def step() -> dict:
    """The step as it is handed to `setup_blockers.STEPS` — ★ NO PROBE IS RUN HERE.

    `setup_blockers.plan()` owns WHEN to measure; a store that measures on import would probe the
    box every time anything imported it, which is how a wizard starts costing more than it saves.
    So this returns the row with the probe CALLABLE attached, plus `row_extra` for the live keys."""
    return {
        "id": "gev_option",
        "felt": "God's Eye View — the recommended globe (optional)",
        # ★ NO VERSION LITERAL HERE, AND THAT IS THE FIX. `step()` runs no probe by design, so a
        #   version written on this line could only ever be one this file REMEMBERS — while `probe()`
        #   reads the real one out of the manifest. The measured version rides in the evidence, so
        #   dropping the number is the only form of this line that cannot go stale. The package name
        #   stays: it is what `find_checkout` finds the checkout BY.
        "technical": ("gods-eye-view · a browser 3D globe over live public data · launched by its own "
                      "Node/Vite server"),
        # ★ NOT 'ENFORCING'. Read `why` for what refuses and what does not.
        "status": "ENFORCING",
        "owner": "operator",
        "why": (
            "YOU CAN SAY NO. This is offered because it is the best thing of its kind on this box "
            "and it cost you a question to get it. \u2605 It is NOT installed by this walk: the "
            "walk MEASURES, and installing it is your act. \u2605 WHAT REFUSES HERE: this surface "
            "renders no credential field at all, so a key cannot be typed into anything that "
            "logs, and a key posted to this route is refused rather than stored. \u2605 WHAT DOES "
            "NOT: nothing stops GEV's own outbound requests, and nothing stops its cloud voice "
            "agent \u2014 those are NAMED in the terms, not blocked. \u2605 AND ITS VOICE HALF IS A "
            "DOCTRINAL CONFLICT: it streams raw microphone audio to OpenAI. \u201cRaw audio never "
            "leaves the box\u201d is not a setting, so that half stays off here."
        ),
        "typing": "set up gods eye view",
        "button": {"label": "\u25c7 God's Eye View (recommended)", "action": "gev:offer"},
        "say": (
            "There is one more thing I can offer you, and it is optional. God's Eye View is a 3D "
            "globe with live aircraft, ships, satellites, earthquakes and cameras \u2014 it is "
            "already downloaded on this machine. Before you decide, hear the cost: it needs a "
            "Google Maps key with BILLING enabled, it makes outbound requests to Google, OpenSky, "
            "adsb.lol, AISStream, CelesTrak and The Space Devs, and its cable data is "
            "NONCOMMERCIAL. Say \u201cset up gods eye view\u201d to take it, or say \u201cdecline "
            "gods eye view\u201d to say no \u2014 and no is a real answer, it just stops coming up. "
            "I will not install it for you and I will never ask you to type a key into this chat: "
            "the key goes into the app's own settings, on this box. Tell me either way and I will "
            "record it."
        ),
        "next": (
            "Saying no records DECLINED and the row settles \u2014 it stops appearing in the "
            "default view and stays reversible from this same row. Saying yes records your "
            "decision and re-measures; installing and the Google key stay yours, and no key value "
            "ever passes through this surface."
        ),
        "capability": "launch.gev",
        "probe": walk_probe,
        "row_extra": _row_extra,
    }


def row() -> dict:
    """The step WITH a live measurement, for the CLI and for any caller that wants one row.

    ★ The terms are POINTED AT, not copied: the returned dict carries `terms` by reference to TERMS,
      so a page rendering it holds no second copy that can go stale."""
    r = step()
    r.pop("probe", None)
    r.pop("row_extra", None)
    state, evidence = option_state()
    r = {k: v for k, v in r.items()}
    r["state"] = walk_state(state)
    r["option_state"] = state
    r["evidence"] = evidence
    r["choice"] = get_choice()
    r["terms"] = TERMS
    return r


# ══════════════════════════════════════════════════════════════════════════════════════════════════

def self_test() -> int:
    """G1..G6, each of which has been violated somewhere in this estate's record.

      G1  no key value escapes this module's output            (AGENTS.md §6)
      G2  no credential input on our surface                   (credential endpoints stay loopback)
      G3  an unfetched recommendation never maps to DONE        (the nine-times defect)
      G4  declining works and is reversible                     (an informed NO is the feature)
      G5  the disclosure is complete: billing, egress, licence  (a surprise is not an experience)
      G6  the option->walk mapping is total and in-vocabulary   (the renderer cannot invent)
      G7  the runtime (node engines) claim is COMPUTED, not printed   (a bug that was here)
      G8  the node measurement is PATH-INDEPENDENT and reproducible    (a bug that was here too)
    """
    fails, notes = [], []

    # G1 — plant a distinctive key in a THROWAWAY env file, run the real key_report, and require the
    #      raw to appear in NO output. An absence can only be tested by planting the thing.
    planted = "gevPROVEFAILplantedKEY0123456789"
    tmp_env = REPO / "var" / "gev_option.PROVEFAIL.env"
    try:
        tmp_env.parent.mkdir(parents=True, exist_ok=True)
        tmp_env.write_text(f"GOOGLE_MAPS_API_KEY={planted}\n# OPENAI_API_KEY=nope\n", encoding="utf-8")
        rep = key_report(env_files=[tmp_env])
        st, ev = probe(choice={}, candidates=[HOME / "nonexistent-gev-provefail"], env_files=[tmp_env])
        fails.extend(_g1_fails([json.dumps(rep), ev, json.dumps(TERMS), st], planted))
        if rep.get("GOOGLE_MAPS_API_KEY", "").startswith("absent"):
            fails.append("G1 the planted key was not DETECTED — a probe that cannot see a key "
                         "cannot honestly report that one is missing")
        if not rep.get("OPENAI_API_KEY", "").startswith("absent"):
            fails.append("G1 a COMMENTED-OUT key was counted as set — `# KEY=` is absent")
    finally:
        try:
            tmp_env.unlink()
        except Exception:  # noqa: BLE001
            pass

    # G2 — the shipped page must carry no credential input
    page = REPO / "panel" / "setup-wizard-popout.html"
    if page.is_file():
        fails.extend(_g2_fails(page.read_text(encoding="utf-8", errors="replace")))
    else:
        fails.append(f"G2 the setup wizard page is missing at {page} — cannot check it")

    # G1b — the fingerprint helper must not disclose a short value
    fails.extend(_g1_short_fails())

    # G7 — the runtime claim must be COMPUTED, not printed (this bug was real, and was here)
    fails.extend(_g7_fails())

    # G8 — and it must not depend on the ambient PATH (this one was real too)
    fails.extend(_g8_fails())

    # G3 — over the real mapping AND against a liar
    fails.extend(_g3_fails())

    # G4 — the decline round trip, on a throwaway ledger
    fails.extend(_g4_fails())

    # G5 — the disclosure
    fails.extend(_g5_fails())

    # G6 — a raising probe must not answer DONE. The HONEST runner must survive it...
    fails.extend(_g6_fails())
    fails.extend(_g6_fails(option_state))
    # ...and the seam must be reachable by prove-fail, which aims a LYING runner at the same guard.
    if not _g6_fails(lambda fn: ("DONE", "swallowed")):
        fails.append("G6 the guard did NOT bite a runner that swallows a raising probe into DONE — "
                     "this guard cannot fail, which is the same as no guard at all")

    st, ev = probe()
    notes.append(f"measured now: {st} — {ev[:150]}")
    notes.append(f"option states: {' | '.join(OPTION_STATES)}")
    _ch = get_choice()
    notes.append("choice: " + (f"{_ch.get('decision')} on {_ch.get('at')}" if _ch.get("decision")
                               else "no decision recorded"))
    for f in fails:
        print(f"  \u2717 {f}")
    if not fails:
        print("  \u2713 gev_option self_test: G1..G8 hold")
    for n in notes:
        print(f"  \u00b7\u00b7 {n}")
    return 1 if fails else 0


def prove_fail() -> int:
    """★★ EVERY GUARD BROKEN ON PURPOSE AND REQUIRED TO GO RED (`AGENTS.md` §2).
    A mutation that leaves the suite green makes the WHOLE RUN INVALID."""
    print("=== PROVE-FAIL \u2014 break each guarded thing, require RED ===")
    planted = "gevPROVEFAILplantedKEY0123456789"
    muts = []

    # M1/G1 — a leaky probe: the raw key IS published
    muts.append(("M1 a probe that PUBLISHES the raw key",
                 lambda: _g1_fails([f"GOOGLE_MAPS_API_KEY={planted}", "clean"], planted), "G1"))

    # M2/G2 — a page that grew a credential field
    muts.append(("M2 a page with a credential input",
                 lambda: _g2_fails('<input type="password" name="api_key">'), "G2"))

    # M3/G3 — a LYING mapping: nothing fetched reports as done
    liar = dict(_WALK_STATE)
    liar["NOT-INSTALLED"] = "DONE"
    muts.append(("M3 a mapping that sends NOT-INSTALLED to DONE",
                 lambda: _g3_fails(liar), "G3"))

    # M4/G3 — DECLINED folded away, so the option nags forever
    folded = dict(_WALK_STATE)
    folded["DECLINED"] = "BLOCKED"
    muts.append(("M4 DECLINED folded into BLOCKED",
                 lambda: _g3_fails(folded), "G3"))

    # M5/G5 — the disclosure silently loses the billing requirement
    thin = dict(TERMS)
    thin["requires"] = "It needs a Google Maps key."
    muts.append(("M5 `requires` with the billing warning dropped",
                 lambda: _g5_fails(thin), "G5"))

    # M6/G5 — an egress list that quietly drops a third party
    short = dict(TERMS)
    short["egress"] = "It talks to Google and a few public APIs."
    muts.append(("M6 `egress` with five of six parties dropped",
                 lambda: _g5_fails(short), "G5"))

    # M7/G1 — a fingerprint helper that discloses a SHORT secret whole. The mutation replaces the
    #         helper, so the guard is proved against a liar rather than asserted against the truth.
    muts.append(("M7 a fingerprint helper disclosing the short value",
                 lambda: _g1_short_fails(lambda v: f"set \u00b7 \u2026{v[-4:]}" if v else "absent"),
                 "G1"))

    # M8/G6 — a runner that swallows a RAISING probe into DONE
    muts.append(("M8 a runner that swallows a raising probe into DONE",
                 lambda: _g6_fails(lambda fn: ("DONE", "swallowed")), "G6"))

    # M9/G6 — a runner that answers UNKNOWN but names no reason, so "could not look" is a shrug
    muts.append(("M9 a runner answering UNKNOWN with no reason",
                 lambda: _g6_fails(lambda fn: ("UNKNOWN", "  ")), "G6"))

    # M10/G7 — THE ORIGINAL BUG, REPRODUCED: an engines checker that always says yes.
    muts.append(("M10 an engines checker that always answers 'satisfies'",
                 lambda: _g7_fails(checker=lambda v, r: (True, "trust me")), "G7"))

    # M11/G7 — a checker that gets the boundary wrong (>= read as >), so 24.14.0 is refused
    def _offbyone(v, r):
        base = engines_ok(v, r)
        if _vt(str(v)) == (24, 14, 0):
            return False, "off by one on the lower bound"
        return base
    muts.append(("M11 an engines checker with the lower bound off by one",
                 lambda: _g7_fails(checker=_offbyone), "G7"))

    # M12/G7 — an unparseable range reported as a pass
    muts.append(("M12 an engine checker calling an unparseable range a pass",
                 lambda: _g7_fails(checker=lambda v, r: (True, "assumed")), "G7"))

    # M13/G8 — THE ORIGINAL PATH BUG, REPRODUCED: a node report that consults `which`. It returns a
    #          different answer once PATH is emptied, which is exactly what the live panel did.
    def _path_consulting(rng):
        import shutil as _sh
        p = _sh.which("node")
        if not p:
            return {"found": [], "best": None, "any_ok": False, "best_ok": False}
        v = _run([p, "--version"])[1].strip().lstrip("v")
        ok, _ = engines_ok(v, rng)
        return {"found": [{"version": v, "path": p, "ok": ok}], "best": {"version": v, "path": p},
                "any_ok": ok, "best_ok": ok}
    muts.append(("M13 a node report that consults PATH (`which node`)",
                 lambda: _g8_fails(report_fn=_path_consulting), "G8"))

    bad = 0
    for name, fn, inv in muts:
        try:
            fails = fn()
        except Exception as e:  # noqa: BLE001
            fails = [f"raised {type(e).__name__}: {e}"]
        red = bool(fails)
        print(f"  {'RED  ' if red else 'GREEN'}  {name}  -> expected {inv} red: {'yes' if red else 'NO'}")
        if not red:
            print(f"         nothing failed — the guard does not bite")
            bad += 1
    print()
    print(f"  {'\u2717 INVALID' if bad else '\u2713 VALID'} \u2014 {len(muts) - bad}/{len(muts)} "
          f"mutations reddened their own invariant")
    return 1 if bad else 0


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
        st, ev = probe()
        print(json.dumps({"option_state": st, "walk_state": walk_state(st), "evidence": ev,
                          "terms": TERMS, "choice": get_choice()}, indent=2))
        return 0
    if "--accept" in a:
        print(json.dumps(set_choice("accepted"), indent=2)); return 0
    if "--decline" in a:
        print(json.dumps(set_choice("declined"), indent=2)); return 0
    if "--clear" in a:
        print(json.dumps(set_choice(None), indent=2)); return 0
    st, ev = probe()
    print(f"GODS EYE VIEW OPTION \u2014 {st}  (walk state: {walk_state(st)})")
    print(f"  measured: {ev}")
    print(f"  choice:   {get_choice() or 'no decision recorded'}")
    print()
    print("  WHAT IT GIVES YOU")
    print(f"    {TERMS['gives']}")
    print("  WHAT IT REQUIRES")
    print(f"    {TERMS['requires']}")
    print("  WHERE IT PHONES OUT")
    print(f"    {TERMS['egress']}")
    print("  LICENCE")
    print(f"    {TERMS['licence']}")
    print("  ATTRIBUTION")
    print(f"    {TERMS['attribution']}")
    print("  VOICE")
    print(f"    {TERMS['voice']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
