#!/usr/bin/env python3
"""secret_meta.py — a secret's CLEARANCE, TERRITORY and RING, and who may read it.

★★★ WHAT HE ASKED FOR: "All the secrets and including these oaths need to be factored into the
    access levels and territories criteria too."

★★ WHAT WAS ACTUALLY THERE. Secrets were BARE FILES under ~/.khaoss-secrets — no registry, no owner,
   no clearance, no territory, no expiry. Ten of them. The only thing standing between any process
   running as this user and every credential on the box was the file mode. Meanwhile a complete
   two-axis access engine already existed in shaman_ledger.py (rank AND territory, with a
   rank-independent strike) and NOTHING connected the two.

★ SO THIS DOES NOT INVENT AN ACCESS MODEL. It attaches the EXISTING one to secrets:
      rank      from shaman_ledger.RANK_ORDER  — Initiate < Seeker < Adept < Oracle < Sage
      territory resolved by shaman_ledger against the secret's PATH, which is what that engine
                already does for every other resource
      ring      the orthogonal DATA class (0 = never leaves, 4 = public)
   Writing a third vocabulary here is how two answers to one question start disagreeing, which is
   most of what went wrong on this box tonight.

★★★ AND THE HONEST PART, STATED IN THE CODE BECAUSE IT IS THE WHOLE RISK: attaching a label to a
   secret DOES NOT ENFORCE ANYTHING BY ITSELF. Agents read these files directly from disk; a Python
   function cannot stop them. This estate has already paid for that exact shape — 13,653 files carry
   a `no-cloud` caveat that nothing reads, and the operator was told they were protected.
   So this module is explicit about its own status:
     · `may_read()` is the AUTHORITY — the one place that answers "is this agent allowed this secret"
     · `enforcement_status()` reports HOW MANY readers actually call it, by measuring, not claiming
   A clearance that no reader consults is reported as DECLARED, never as PROTECTED.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import pathlib
from pathlib import Path

STORE = Path.home() / ".khaoss-secrets"
META = STORE / ".meta.json"
RINGS = {
    0: "never leaves this machine — not to any model, not to any network",
    1: "local agents only, on-box models only",
    2: "trusted cloud models under contract",
    3: "any model we pay for",
    4: "public — no restriction",
}


def _ledger():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import shaman_ledger as L
    return L


def ranks() -> list:
    """★ CONSULTED, never re-declared. If the ledger grows a rank, this grows with it."""
    try:
        return list(_ledger().RANK_ORDER)
    except Exception:
        return ["Initiate", "Seeker", "Adept", "Oracle", "Sage"]


def _load() -> dict:
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    META.parent.mkdir(parents=True, exist_ok=True)
    tmp = META.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(META)
    os.chmod(META, 0o600)


def describe(secret_id: str, rank_required: str = "", territory: str = "",
             ring: int | None = None, owner: str = "", kind: str = "",
             account: str = "", expires: str = "") -> dict:
    """Attach (or update) the access facts for one secret. Never touches the VALUE."""
    # ★★ THE ID MUST BE THE STORE'S FILENAME, ALWAYS. `_secure_write_secret` writes the file at the
    #   SLUGIFIED id (panel_server.py:763) but the /secrets/set route described it with the RAW typed
    #   id — so "Deepseek API Key" landed on disk as `deepseek-api-key` and was classified as
    #   "Deepseek API Key", a key no reader can name. Every reader keys on the FILENAME (`may_read`
    #   below, `_secrets_list`), so 8 of the 9 clearances in the store were unreachable by
    #   construction: the gate answered `undescribed` while the clearance sat in the file under a
    #   pretty name. Canonicalising HERE means no caller can recreate that split again.
    sid = re.sub(r"[^a-z0-9._-]", "-", (secret_id or "").strip().lower()).strip("-")
    if not sid:
        return {"ok": False, "error": "which secret?"}
    if rank_required and rank_required not in ranks():
        return {"ok": False, "error": f"rank must be one of {ranks()}"}
    if ring is not None and int(ring) not in RINGS:
        return {"ok": False, "error": f"ring must be 0-4; {RINGS}"}
    d = _load()
    row = d.get(sid, {})
    for k, v in (("rank_required", rank_required), ("territory", territory),
                 ("owner", owner), ("kind", kind), ("account", account), ("expires", expires)):
        if v:
            row[k] = v
    if ring is not None:
        row["ring"] = int(ring)
    row.setdefault("first_described", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    d[sid] = row
    _save(d)
    return {"ok": True, "id": sid, **row}


def _resolve_meta(secret_id: str, ref_path: str = "") -> tuple:
    """WHICH clearance row describes this secret? ONE row per secret, whichever way it is addressed.

    ★ The registry NAMES a secret; the engine keys on the store FILENAME. For a store-backed secret
      those are the same string. For one whose ref lives elsewhere (`~/.mammouth/api_key`, an age
      ciphertext) they are not — so the lookup tries the declared id first and the file's basename
      second. If neither has a row the secret is UNDESCRIBED, and undescribed is a refusal.
    """
    d = _load()
    for key in (secret_id, os.path.basename(ref_path or "")):
        if key and key in d:
            return key, d[key]
    return (secret_id or os.path.basename(ref_path or "")), None


def may_read_path(agent_id: str, ref_path: str, secret_id: str = "") -> dict:
    """★★★ THE AUTHORITY, for a secret named by the PATH the value actually lives at.

    ★ WHY THIS EXISTS. `may_read()` answered only for secrets sitting IN the store, because it
      fabricated the path as `STORE/<sid>`. But `shaman_ledger.check_access` is itself PATH-based —
      it resolves territory against a real path for every other resource on this box — and the
      keeper's registry points at credentials that live outside the store. Judging those against a
      fabricated store path would have been the wrong question, answered confidently.

    Resolution order is unchanged and still the ledger's: a STRIKE beats everything, then rank, then
    territory. An UNDESCRIBED secret is refused — not because it is dangerous, but because nobody
    has said who it is for, and 'nobody said' must never resolve to 'everybody'.
    """
    path = os.path.expanduser(ref_path or "")
    if not path or not os.path.exists(path):
        return {"allowed": False, "reason": f"no such secret {secret_id or ref_path!r}"}
    key, meta = _resolve_meta(secret_id, path)
    if not meta:
        return {"allowed": False, "denied_axis": "undescribed",
                "reason": "this secret has no clearance or territory attached, so nobody has said "
                          "who it is for. Refused by default — 'nobody said' is not 'everybody'."}
    try:
        L = _ledger()
        verdict = L.check_access(agent_id, path, "read")
    except Exception as e:
        return {"allowed": False, "denied_axis": "engine",
                "reason": f"could not consult the access ledger ({e.__class__.__name__}) — "
                          f"refused, because could-not-look is not permission"}
    need = meta.get("rank_required")
    if verdict.get("allowed") and need:
        # ★★ THIS USED TO END IN `except Exception: pass`. If the rank lookup had thrown, the gate
        #   would have silently skipped the rank check and returned ALLOWED — a failure that opens
        #   the door and says nothing, which is the single worst shape a security check can have.
        #   Now a failure REFUSES and names itself: could-not-check is not permission.
        try:
            L = _ledger()
            # ★★★ THE RANK COMES FROM THE LEDGER'S OWN RESOLVER, NOT FROM A SECOND LOOKUP HERE.
            #     MEASURED 2026-10-13: this line used to be
            #         have = (L.agents().get(agent_id) or {}).get("rank", "Initiate")
            #     which is a SECOND, independent answer to a question `check_access` had already
            #     answered one screen up. Once the ledger grew a declared default posture for agents
            #     with no row, this line kept returning "Initiate" and the gate kept refusing -- so
            #     the ledger would have said "capable" while the door said no. That is two truths
            #     about one fact inside a security check, which is the defect this surface exists to
            #     end. `L.rank_of` is the ONE resolver; both callers use it, so they cannot disagree.
            have = L.rank_of(agent_id)
            if L.rank_index(have) < L.rank_index(need):
                return {"allowed": False, "denied_axis": "rank",
                        "reason": f"{agent_id} is {have}; this secret requires {need}"}
        except Exception as e:
            return {"allowed": False, "denied_axis": "rank-check-failed",
                    "reason": f"could not establish {agent_id}'s rank ({e.__class__.__name__}) — "
                              f"refused, because a rank check that fails must not read as a pass"}
    return {**verdict, "secret": key, "ring": meta.get("ring"),
            "rank_required": need, "territory": meta.get("territory")}


def may_read(agent_id: str, secret_id: str) -> dict:
    """★★ THE AUTHORITY for a secret that LIVES IN THE STORE, addressed by its filename.

    ★ DELEGATES, so there is ONE resolution order on this box rather than two that can drift into
      giving different answers about the same secret."""
    sid = (secret_id or "").strip()
    if not (STORE / sid).exists():
        return {"allowed": False, "reason": f"no such secret {sid!r}"}
    out = may_read_path(agent_id, str(STORE / sid), sid)
    if "secret" in out:
        out["secret"] = sid          # `may_read` has always named the secret by the id it was asked
    return out


def enforcement_status(scan: bool = True) -> dict:
    """★★★ DOES ANY READER ACTUALLY CALL may_read()? Measured, not claimed.

    If the answer is zero, every clearance in this file is DECLARED and nothing more — which is
    exactly the no-cloud tag, and the operator must be told in those words.

    ★★★ `scan=False` — THE REPO-WIDE SCAN IS NOT A PAGE FACT (added 2026-10-12, after this function
        took the installer route down). MEASURED: the two recursive greps below walk **131,901 files
        and cost 14.0 s**, while the DRIVEN probe that actually decides the row costs **0.12 s**. The
        setup walk renders on every request, so a 14 s probe is a status page nobody can load — and a
        row that cannot be SERVED is a row the operator cannot read before he decides.

        ★ WHAT WAS NOT SCANNED MUST NEVER BE REPORTED AS ZERO. With `scan=False` this returns
          `readers_calling_may_read: None` and a status that says the scan was NOT RUN — because
          "0 readers" and "not looked" are different facts, and collapsing them is the exact
          false-negative this module exists to prevent. The count is a CODE fact that changes when a
          FILE changes, not once per request; it belongs on demand (`--status`), not on a page.

        ★ AND THE COMMON GUESS ABOUT WHY IT WAS SLOW WAS WRONG, so it is written down here: it is NOT
          the process spawn. `value_path_probe()` spawns `khaoss-secrets` twice and costs 0.12 s. The
          cost is entirely the grep."""
    # ★★★ THE FIRST VERSION OF THIS FUNCTION GOT IT WRONG, IN THE EXACT WAY THIS MODULE WARNS ABOUT.
    #   It grepped for the STRING "may_read" and reported "ENFORCED", naming port_registry.py — where
    #   `may_read_ring0` is a PARAMETER NAME. A substring is not a call. The honest answer was zero,
    #   and the checker built to detect decorative protection was itself decorative for one run.
    # ★ So a caller must BOTH import this module AND invoke the function. Two conditions, because
    #   either alone is satisfied by coincidence.
    # ★★ A VERIFICATION FILE IS NOT A READER (added 2026-09-20, because this function went green on
    #   my own test and I watched it happen). The secrets-authority lane added
    #   `operator/secrets_authority_assert.py`, which imports this module and calls `may_read` — so
    #   the detector flipped from "DECLARED ONLY" to "ENFORCED at the callers listed" naming a TEST.
    #   That is the same failure this function already documents once (a substring matched a
    #   parameter name) and it is the estate's named defect class: an instrument that reports
    #   protection it does not have. A test proving the gate CAN refuse is not a reader consulting it.
    #   The honest shape is to keep the test VISIBLE and refuse to count it.
    def _is_verification_file(f: str) -> bool:
        b = os.path.basename(f).lower()
        return (b.startswith("test_") or b.endswith("_test.py") or b.endswith("_assert.py")
                or "prove_fail" in b or "selftest" in b or "self_test" in b
                or "/tests/" in f or "/test/" in f)
    import re as _re
    import subprocess
    roots = [str(Path(__file__).resolve().parent.parent), str(Path.home() / ".hermes")]

    # ★★★ THE DETECTOR HAD ITS OWN BLIND SPOT, AND IT WAS THE WHOLE VERDICT (found + fixed
    #   2026-10-12). The scan ran `grep -rl --include=*.py`. **The ONE value path on this box is
    #   `khaoss-secrets` — an executable with NO `.py` EXTENSION.** So the moment the gate was
    #   actually wired in, this function would still have reported "zero runtime readers", and the
    #   operator would have been told his boundary was one chmod deep *by the instrument that was
    #   supposed to notice it had been fixed*. A check that cannot see the thing it is checking for
    #   is the failure class this module already documents twice. Python sources AND extensionless
    #   executables are therefore both readers; nothing else is.
    def _is_scannable_source(f: str) -> bool:
        b = os.path.basename(f)
        if b.endswith((".bak", ".json", ".md", ".txt", ".log", ".jsonl")) or ".bak." in b:
            return False
        if "/reports/" in f or "/archive/" in f or "__pycache__" in f or "/node_modules/" in f:
            return False
        return os.path.splitext(b)[1] in ("", ".py")

    callers = []
    verification = []
    for r in (roots if scan else []):
        try:
            out = subprocess.run(["grep", "-rl", "secret_meta", r],
                                 capture_output=True, text=True, timeout=45).stdout
        except Exception:
            continue
        for f in out.splitlines():
            if not f.strip() or f.endswith("secret_meta.py") or not _is_scannable_source(f):
                continue
            try:
                body = pathlib.Path(f).read_text(errors="replace")
            except Exception:
                continue
            imports = _re.search(r"(?:^|\n)\s*(?:import\s+secret_meta|from\s+secret_meta\s+import)", body)
            # ★ THE LOOKBEHIND USED TO BE `(?<![\w.])`, WHICH REJECTED A CALL THROUGH A MODULE ALIAS.
            #   The value path imports the engine as `sm` and calls `sm.may_read_path(...)` — a real
            #   call the detector would have missed for the SECOND time in the same function. What
            #   actually prevents the original false positive (`may_read_ring0`, a parameter NAME) is
            #   requiring `(` immediately after, NOT the dot. Blocking word chars keeps that; the dot
            #   is a call, so it is allowed.
            calls = _re.search(r"(?:secret_meta\s*\.\s*may_read_path|secret_meta\s*\.\s*may_read"
                               r"|(?<![\w])may_read(?:_path)?\s*\()", body)
            if imports and calls:
                (verification if _is_verification_file(f) else callers).append(f)
    described = len(_load())
    total = len([p for p in STORE.iterdir() if p.is_file() and not p.name.startswith(".")]) \
        if STORE.is_dir() else 0
    gate = value_path_probe()
    if not scan:
        # ★ NOT SCANNED IS NOT ZERO. The reader count is reported as UNKNOWN, and the status says so
        #   in those words — a page that printed "0 readers" here would be the same lie this lane was
        #   sent to fix, wearing a faster font.
        return {"described": described, "secrets_total": total,
                "readers_calling_may_read": None,
                "reader_scan": "NOT RUN on this path (scan=False) — the count is a code fact, it "
                               "changes when a FILE changes, not once per request",
                "verification_callers_not_counted_as_readers": None,
                "value_path": gate,
                "status": ("ENFORCEMENT IS DECIDED BY THE DRIVEN PROBE BELOW, not by a reader count "
                           "that was not taken here. Run `secret_meta.py --status` for the full scan."
                           if gate.get("gated") else
                           "★ the value path is NOT gated — see the driven probe below"),
                "note": (gate.get("sentence")
                         or "file mode 0600 remains the only thing actually stopping a read today")}
    return {"described": described, "secrets_total": total,
            "readers_calling_may_read": sorted(set(callers)),
            "reader_scan": "full repo scan completed",
            "verification_callers_not_counted_as_readers": sorted(set(verification)),
            "value_path": gate,
            "status": ("ENFORCED at the callers listed" if callers else
                       "DECLARED ONLY — no runtime reader consults it yet, so these labels describe "
                       "intent and protect nothing on their own"
                       + (f" ({len(set(verification))} verification caller(s) exercise may_read and "
                          f"are correctly NOT counted as readers)" if verification else "")),
            "note": (gate.get("sentence")
                     or "file mode 0600 remains the only thing actually stopping a read today")}


def value_path_probe(secret_name: str = "") -> dict:
    """★★★ DOES THE VALUE PATH ACTUALLY REFUSE, OR DOES IT ONLY LOOK LIKE IT DOES?

    ★ WHY THIS IS NOT A GREP. The reader count above is a TEXT measurement, and this module has
      already been burned twice by instruments that reported protection they did not have — once by
      matching a parameter NAME (`may_read_ring0`), once by counting a TEST as a reader. A third
      text-shaped answer would be a third chance to be wrong the same way. So this DRIVES the value
      path and reads its actual answer.

    ★ IT NEVER SEES A VALUE. Two refusals are driven, both of which must emit ZERO bytes to stdout —
      the caller is UNDECLARED and the caller is a NONEXISTENT agent. Neither can ever be allowed, so
      no value can be produced even by accident, and `stdout_bytes` is asserted to be 0 rather than
      merely reported. The secret is addressed by NAME only; nothing here opens the file.

    ★ AND IT FAILS CLOSED. If the CLI is missing or unreadable the answer is GATED=False with the
      reason, because could-not-look is not permission."""
    cli = pathlib.Path(__file__).resolve().parent.parent / "khaoss-secrets"
    out = {"driven": False, "gated": False, "no_caller_refused": None, "unknown_caller_refused": None,
           "stdout_bytes": None, "returncode": None, "sentence": ""}
    if not cli.is_file():
        out["sentence"] = f"the value path {cli} is absent — cannot be measured, not called protected"
        return out
    # A real store filename, used as a NAME. Never opened. If the store is empty use a placeholder,
    # because the refusal under test happens BEFORE resolution either way.
    names = sorted(p.name for p in STORE.iterdir()
                   if p.is_file() and not p.name.startswith(".")) if STORE.is_dir() else []
    name = secret_name or (names[0] if names else "no-such-secret-here")
    import subprocess
    try:
        r1 = subprocess.run([sys.executable, str(cli), "resolve-for-inject", name],
                            capture_output=True, timeout=30)
        r2 = subprocess.run([sys.executable, str(cli), "resolve-for-inject", name,
                             "--as", "nobody-at-all-not-in-the-ledger"],
                            capture_output=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        out["sentence"] = (f"could not drive the value path ({e.__class__.__name__}) — "
                           f"unmeasured, which is not the same as safe")
        return out
    out["driven"] = True
    out["no_caller_refused"] = r1.returncode != 0
    out["unknown_caller_refused"] = r2.returncode != 0
    out["stdout_bytes"] = len(r1.stdout) + len(r2.stdout)
    out["returncode"] = [r1.returncode, r2.returncode]
    gated = (out["no_caller_refused"] and out["unknown_caller_refused"]
             and out["stdout_bytes"] == 0)
    out["gated"] = gated
    if gated:
        out["sentence"] = (
            f"value path `khaoss-secrets resolve-for-inject` REFUSED an undeclared caller and an "
            f"unknown caller (exit {r1.returncode}/{r2.returncode}, {out['stdout_bytes']} bytes on "
            f"stdout) — the clearance engine is the decision point, DRIVEN not counted.")
    else:
        bad = []
        if not out["no_caller_refused"]:
            bad.append("it emitted a value for a caller that declared NO identity")
        if not out["unknown_caller_refused"]:
            bad.append("it emitted a value for an agent that is not in the ledger")
        if out["stdout_bytes"]:
            bad.append(f"a refusal wrote {out['stdout_bytes']} byte(s) to STDOUT, which is the "
                       f"value channel")
        out["sentence"] = ("★ NOT GATED — " + "; ".join(bad) + ". File mode 0600 is the only thing "
                           "stopping a read.")
    return out


def direct_readers() -> dict:
    """★★★ WHO READS A SECRET'S BYTES WITHOUT GOING THROUGH THE KEEPER? Measured, not assumed.

    ★ WHY THIS EXISTS. Gating the one SANCTIONED value path fixes the door everyone was told about.
      It does not touch the windows. A module that opens `~/.khaoss-secrets/<id>` itself has bypassed
      the keeper, the clearance engine, and the audit ledger in one line — and because it never
      mentions `resolve-for-inject`, a report that only audits the keeper would call the estate clean.

      This counts them so the gap is a NUMBER rather than a hope. It is deliberately a census and not
      a gate: routing each of these through the keeper needs a clearance row, a rank and a territory
      per credential, and those are the OPERATOR'S to grant — inventing them here would be widening a
      governance gate on the agent's own judgement, which is the thing this module refuses to do.

    ★ A DIRECTORY LISTING OR A `stat` IS NOT A READ. The file must both name the store AND carry a
      read primitive, because `panel_server.py` legitimately stats these files to report mode and
      size, and counting that as a leak would make the number useless.

    ★★ AND A MENTION IS NOT A READ EITHER — the first version of this function grepped for the string
       and reported ELEVEN files, of which nine only name the store in a comment, a docstring, a
       sensitive-name list, or a subprocess argv (`khaoss-secrets list`). An instrument that cries
       wolf is the same defect as one that stays silent: nobody can act on the number. So the read is
       resolved through the AST — a NAME bound to an expression containing the store path, and a real
       `.read_text()` / `.read()` / `.read_bytes()` / `open()` ON THAT NAME. Nothing else counts.
    """
    import ast
    import re as _re
    import subprocess
    found = {}
    roots = [str(Path(__file__).resolve().parent.parent), str(Path.home() / ".hermes")]
    readers = {"read_text", "read_bytes", "read", "readlines"}
    for r in roots:
        try:
            out = subprocess.run(["grep", "-rl", ".khaoss-secrets", r],
                                 capture_output=True, text=True, timeout=45).stdout
        except Exception:
            continue
        for f in out.splitlines():
            b = os.path.basename(f)
            if not f.strip() or ".bak." in b or "/reports/" in f or "/archive/" in f \
                    or "__pycache__" in b or "/node_modules/" in f:
                continue
            if os.path.splitext(b)[1] not in ("", ".py"):
                continue
            if b in ("secret_meta.py", "khaoss-secrets", "setup_blockers.py", "test_secrets_gate.py",
                     "secrets_authority_assert.py", "secrets_authority_apply.py"):
                continue          # the keeper, its probe and its verification are not bypasses
            try:
                src = pathlib.Path(f).read_text(errors="replace")
                tree = ast.parse(src)
            except Exception:
                continue
            # 1 — every NAME bound to an expression that mentions the store. A NAME BOUND MORE THAN
            #     ONCE IS DISCARDED: this analysis is file-scoped, so a single `f = <store path>`
            #     anywhere would otherwise turn every `f.read_text()` in a 15,000-line module into a
            #     finding. An ambiguous name is not evidence, and a census that over-reports is the
            #     same defect as one that stays silent.
            binds: dict = {}
            where = {}
            # ★ COUNT **EVERY** BINDING OF THE NAME, not just the store-path ones. Counting only the
            #   store-path bindings still left `p` and `f` looking "unambiguous" in `panel_server.py`,
            #   where they are reassigned in dozens of unrelated helpers — and one `.khaoss-secrets`
            #   assignment was enough to make sixteen innocent reads look like findings.
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for t in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Name):
                                binds[sub.id] = binds.get(sub.id, 0) + 1
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    val = getattr(node, "value", None)
                    tgt = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if val is None or ".khaoss-secrets" not in ast.dump(val):
                        continue
                    for t in tgt:
                        if isinstance(t, ast.Name):
                            where[t.id] = getattr(val, "lineno", 0)
            names = {n for n in where if binds.get(n, 0) == 1}
            # 2 — a real READ on one of those names (or on a literal store path)
            for node in ast.walk(tree):
                fn = node.func if isinstance(node, ast.Call) else None
                hit = None
                if isinstance(fn, ast.Attribute) and fn.attr in readers \
                        and isinstance(fn.value, ast.Name) and fn.value.id in names:
                    hit = f"{fn.value.id}.{fn.attr}()"
                elif isinstance(fn, ast.Name) and fn.id == "open" and node.args:
                    a0 = node.args[0]
                    if ".khaoss-secrets" in ast.dump(a0):
                        hit = "open(<store path>)"
                    elif isinstance(a0, ast.Name) and a0.id in names:
                        hit = f"open({a0.id})"
                if hit:
                    found.setdefault(f, []).append(f"line {node.lineno}: {hit}")
    return {"count": len(found), "files": {k: sorted(set(v)) for k, v in sorted(found.items())},
            "note": ("these read a secret's bytes WITHOUT the clearance engine, the consumer list or "
                     "the audit ledger. Gating the keeper does not close them." if found else
                     "no module outside the keeper reads the store directly")}


def self_test() -> int:
    """★★★ PROVE IT CAN SAY NO. The only agent in the ledger is a Sage, so every real query this gate
    has ever answered came back ALLOWED — and a control that has only ever been seen allowing has not
    been shown to refuse. These plant the conditions instead of waiting for them."""
    fails = 0

    def chk(label, ok):
        nonlocal fails
        fails += (not ok)
        print(f"  {'✓' if ok else '✗'} {label}")

    # ★★ THIS CONTROL USED TO BORROW `starlink` AS ITS EXAMPLE OF AN UNDESCRIBED SECRET. On
    #   2026-09-20 the secrets-authority lane classified every entry in the store, so `starlink` is
    #   now described and this control went RED — correctly, because the premise it was standing on
    #   had been removed. The property it guards is real, so it now PLANTS its own undescribed secret
    #   instead of borrowing one. A control that depends on some store entry staying unclassified
    #   dies the day the store is finally classified, and that is precisely what happened here.
    _und = STORE / "__undescribed_probe"
    try:
        _und.write_text("x")
        os.chmod(_und, 0o600)
        chk("★ an UNDESCRIBED secret is refused, never waved through",
            may_read("pa-lead-de63a495", "__undescribed_probe").get("denied_axis") == "undescribed")
    finally:
        _und.unlink(missing_ok=True)
    chk("a nonexistent secret is refused and says so",
        may_read("pa-lead-de63a495", "no-such-secret-here").get("allowed") is False)
    chk("★ an UNKNOWN agent is refused (not treated as the default agent)",
        may_read("nobody-at-all", "starlink").get("allowed") is False)

    # plant a described secret that outranks a planted low-rank agent
    real_ledger = _ledger
    tmp = STORE / "__gate_probe"
    try:
        tmp.write_text("x"); os.chmod(tmp, 0o600)
        describe("__gate_probe", rank_required="Oracle", territory="khaoss", ring=0)

        class _Fake:
            RANK_ORDER = ["Initiate", "Seeker", "Adept", "Oracle", "Sage"]
            @staticmethod
            def rank_index(r): return _Fake.RANK_ORDER.index(r) if r in _Fake.RANK_ORDER else -1
            @staticmethod
            def check_access(a, r, o): return {"allowed": True}
            @staticmethod
            def agents(cfg=None): return {"lowly": {"rank": "Seeker"}}
        globals()["_ledger"] = lambda: _Fake
        v = may_read("lowly", "__gate_probe")
        chk("★★★ a SEEKER is REFUSED a secret that requires ORACLE",
            v.get("allowed") is False and v.get("denied_axis") == "rank")
        chk("  and the refusal names both ranks",
            "Seeker" in str(v.get("reason")) and "Oracle" in str(v.get("reason")))

        class _Broken(_Fake):
            @staticmethod
            def agents(cfg=None): raise RuntimeError("ledger unreadable")
        globals()["_ledger"] = lambda: _Broken
        v2 = may_read("lowly", "__gate_probe")
        chk("★★ a rank check that FAILS refuses, rather than passing",
            v2.get("allowed") is False and v2.get("denied_axis") == "rank-check-failed")
    finally:
        globals()["_ledger"] = real_ledger
        tmp.unlink(missing_ok=True)
        d = _load(); d.pop("__gate_probe", None); _save(d)
    chk("  the probe left nothing behind", not (STORE / "__gate_probe").exists()
        and "__gate_probe" not in _load())
    print(f"\n  {'ALL PASS' if not fails else str(fails) + ' FAILED'} — the gate can refuse, "
          f"and a failed check refuses too.")
    return 1 if fails else 0


def main() -> int:
    a = [x for x in sys.argv[1:] if x.strip()]
    if a and a[0] == "--self-test":
        return self_test()
    if a and a[0] == "--status":
        d = enforcement_status()
        print(f"  {d['described']} of {d['secrets_total']} secret(s) have clearance attached")
        print(f"  readers calling may_read(): {d['readers_calling_may_read'] or 'NONE'}")
        if d.get("verification_callers_not_counted_as_readers"):
            print(f"  verification callers (NOT readers): "
                  f"{d['verification_callers_not_counted_as_readers']}")
        print(f"  ★ {d['status']}")
        print(f"    {d['note']}")
        # ★ ON DEMAND ONLY, AND DELIBERATELY NOT INSIDE `enforcement_status()`. This scan is a second
        #   pair of greps over the whole tree (~19 s here), and `enforcement_status()` is on the setup
        #   WALK's hot path — it is re-run every time a row is rendered. Paying 47 s to render a step
        #   would make the walk unusable on a box already at load 9-14, and a measurement nobody can
        #   afford to take is a measurement nobody takes. So the number lives here, where a human or a
        #   lane asks for it on purpose.
        dr = direct_readers()
        print(f"  ★ DIRECT readers of the store that bypass the keeper: {dr['count']}")
        for f, hits in (dr.get("files") or {}).items():
            print(f"      {f}: {', '.join(hits)}")
        print(f"    {dr['note']}")
        return 0
    if a and a[0] == "--check" and len(a) >= 3:
        print(json.dumps(may_read(a[1], a[2]), indent=2))
        return 0
    if a and a[0] == "--json":
        print(json.dumps({"meta": _load(), "ranks": ranks(), "rings": RINGS}, indent=2))
        return 0
    if a:
        print("usage: secret_meta.py [--status | --json | --check <agent> <secret>]", file=sys.stderr)
        return 2
    print(json.dumps({"ranks": ranks(), "rings": RINGS, "described": len(_load())}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
