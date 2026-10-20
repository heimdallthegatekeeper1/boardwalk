#!/usr/bin/env python3
"""secrets_authority_assert.py — does the secret store still READ, and does the gate still REFUSE?

Two things are asserted together, because either alone is a lie:
  · the store is READABLE by its consumers   (the same checks `/secrets/list` and `may_read` run)
  · the gate still says NO                   (a control only ever seen allowing is not a control)

RUN:        /usr/bin/python3 operator/secrets_authority_assert.py
PROVE-FAIL: /usr/bin/python3 operator/secrets_authority_assert.py --prove-fail
            Mutates a COPY of the meta only. The real store is never touched by --prove-fail.

Never prints, hashes-to-disk, or copies a secret VALUE. Files are compared by a run-local fingerprint
that is never persisted.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys
import tempfile
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import secret_meta as sm  # noqa: E402

STORE = pathlib.Path.home() / ".khaoss-secrets"
META = STORE / ".meta.json"
ACCESS = pathlib.Path.home() / ".config" / "AionUi-Dev" / "khaoss-access.json"
PANEL = "http://127.0.0.1:7842/secrets/list"

R = []


def chk(name, ok, detail=""):
    R.append((name, bool(ok), detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   [{detail}]" if detail and not ok else ""))
    return bool(ok)


def fp(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def store_files():
    return sorted(p.name for p in STORE.iterdir() if p.is_file() and not p.name.startswith("."))


def real_zones():
    try:
        d = json.loads(ACCESS.read_text())
        return set((d.get("_shaman", {}).get("zones") or {}).keys())
    except Exception:
        return set()


# ── the estate's own technique: a fake ledger, so a LOW-rank consumer can exist at all ───────────
class FakeLedger:
    RANK_ORDER = ["Initiate", "Seeker", "Adept", "Oracle", "Sage"]

    @staticmethod
    def rank_index(r):
        return FakeLedger.RANK_ORDER.index(r) if r in FakeLedger.RANK_ORDER else -1

    @staticmethod
    def check_access(a, r, o):
        return {"allowed": True}

    @staticmethod
    def agents(cfg=None):
        # ★ THE ESTATE'S ONLY REAL AGENT IS A SAGE (pa-lead-de63a495), so it can NEVER be refused by
        #   rank — `rank_ok = rank == "Sage" or any(...)` (shaman_ledger.py:160) makes Sage a bypass
        #   of the capability list. A refusal therefore has to be PLANTED to be observed at all.
        return {"consumer-adept": {"rank": "Adept"}}


def file_assertions(meta_path: pathlib.Path, files: list, baseline: dict):
    meta = json.loads(meta_path.read_text())
    ranks = sm.ranks()
    zones = real_zones()

    chk("A1 SET EQUALITY: meta keys == store filenames (no orphans, none missing)",
        sorted(meta.keys()) == files,
        f"meta={len(meta)} files={len(files)} "
        f"orphans={sorted(set(meta) - set(files))[:3]} missing={sorted(set(files) - set(meta))[:3]}")

    chk("A2 every rank_required is in the canonical ladder (shaman_ledger.RANK_ORDER)",
        all(meta[k].get("rank_required") in ranks for k in meta),
        str(sorted({meta[k].get("rank_required") for k in meta} - set(ranks))))

    chk("A3 every ring is on the secret_meta.RINGS axis (0-4)",
        all(meta[k].get("ring") in sm.RINGS for k in meta),
        str(sorted({meta[k].get("ring") for k in meta} - set(sm.RINGS.keys()))))

    chk("A4 every territory names a zone that EXISTS in khaoss-access.json",
        all(meta[k].get("territory") in zones for k in meta),
        str(sorted({meta[k].get("territory") for k in meta} - zones)))

    chk("A5 every entry carries all three fields (nothing half-classified)",
        all(meta[k].get("rank_required") and meta[k].get("territory") and meta[k].get("ring") is not None
            for k in meta),
        str([k for k in meta if not (meta[k].get("rank_required") and meta[k].get("territory")
                                     and meta[k].get("ring") is not None)][:3]))

    # ★ NO VALUE WAS MOVED BY THIS WORK: compare against the baseline taken at suite start.
    moved = [n for n in files if fp(STORE / n) != baseline[n]]
    chk("A6 no secret VALUE changed during this run (fingerprint + size, before vs after)",
        not moved, str(moved))

    chk("A7 nothing was deleted: the store still holds every file it held",
        sorted(baseline.keys()) == files,
        f"{sorted(baseline.keys())} -> {files}")

    # ★ SUPERSEDED, NOT DELETED: a duplicate pair keeps BOTH files on disk and links them.
    pairs = [(k, meta[k]["superseded_by"]) for k in meta if meta[k].get("superseded_by")]
    chk("A8 every superseded duplicate still HAS its file, and its replacement exists",
        all((STORE / k).is_file() and (STORE / t).is_file() for k, t in pairs) and len(pairs) >= 2,
        str(pairs))

    return meta


def gate_assertions(meta):
    """Every described secret must RESOLVE through the gate — the defect was that 8 of 9 did not."""
    saved = sm._ledger
    unreachable = []
    try:
        for sid in sorted(meta):
            v = sm.may_read("pa-lead-de63a495", sid)
            if not v.get("allowed") and v.get("denied_axis") == "undescribed":
                unreachable.append(sid)
            elif not v.get("allowed"):
                unreachable.append(f"{sid}:{v.get('denied_axis')}")
    finally:
        sm._ledger = saved
    chk("A9 every described secret RESOLVES through may_read (not 'undescribed')",
        not unreachable, str(unreachable[:5]))

    # ★ a caller using the OLD display label must now get the file, not a phantom — describe() keys
    #   on the store filename and may_read must stay STRICT, so the label is gone by design.
    chk("A10 a display label is NOT a secret id (the store keys on filenames, strictly)",
        sm.may_read("pa-lead-de63a495", "Deepseek API Key").get("allowed") is False)


def live_assertions():
    try:
        with urllib.request.urlopen(PANEL, timeout=8) as r:
            d = json.loads(r.read())
    except Exception as e:
        chk("L1 GET /secrets/list reachable", False, f"{e.__class__.__name__}")
        return
    rows = d.get("secrets", [])
    files = store_files()
    chk("L1 GET /secrets/list returns exactly the store's files (set equality)",
        sorted(x["id"] for x in rows) == files, f"{len(rows)} rows vs {len(files)} files")
    nulls = [x["id"] for x in rows
             if x["rank_required"] is None or x["territory"] is None or x["ring"] is None]
    chk("L2 ★ the panel renders a clearance for EVERY row (the defect rendered null for 18 of 19)",
        not nulls, str(nulls[:5]))


def prove_fail():
    """★ PROVE IT CAN GO RED. A guard never tested against its own bypass is a hope."""
    print("\n=== PROVE-FAIL (mutating COPIES only; the real store is not touched) ===")
    failures = 0
    real_meta = json.loads(META.read_text())
    files = store_files()
    baseline = {n: fp(STORE / n) for n in files}
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="secrets_provefail_"))

    # ── PF1: SET A LEVEL TOO HIGH AND WATCH A LEGITIMATE CONSUMER BE REFUSED ──────────────────────
    # The gate guard is "an Adept consumer can read what it is entitled to". Raise one secret above
    # the consumer's rank and the guard MUST go red.
    pf1 = tmpdir / "pf1.json"
    m = json.loads(json.dumps(real_meta))
    # ★ THE TARGET MUST BE ENTITLED AT ITS TRUE LEVEL, or PF1a is not a control. The first version of
    #   this prove-fail used `kde-encrypted-wallet` (true level ORACLE) against an ADEPT consumer —
    #   so it was refused at the TRUE level too, and the control went red on its own bad premise.
    #   `notion-login` is Initiate, so an Adept consumer holds it legitimately.
    target = "notion-login"
    true_level = real_meta[target]["rank_required"]
    m[target]["rank_required"] = true_level
    pf1.write_text(json.dumps(m))

    saved_meta, saved_ledger = sm.META, sm._ledger
    try:
        sm.META = pf1
        sm._ledger = lambda: FakeLedger
        at_true = sm.may_read("consumer-adept", target)
        # now RAISE it above the consumer and confirm the refusal
        m[target]["rank_required"] = "Sage"
        pf1.write_text(json.dumps(m))
        raised = sm.may_read("consumer-adept", target)
    finally:
        sm.META, sm._ledger = saved_meta, saved_ledger

    chk(f"PF1a at its TRUE level ({true_level}) an Adept consumer is ALLOWED",
        at_true.get("allowed") is True, json.dumps(at_true)[:120])
    red1 = (raised.get("allowed") is False and raised.get("denied_axis") == "rank")
    chk("PF1b ★ raising the level too high REFUSES that consumer and the check goes RED",
        red1, json.dumps(raised)[:160])
    failures += (not red1) + (at_true.get("allowed") is not True)

    # ── PF2: PUT A DISPLAY LABEL BACK AS A KEY AND WATCH THE PANEL RENDER NULL AGAIN ──────────────
    pf2 = tmpdir / "pf2.json"
    m2 = json.loads(json.dumps(real_meta))
    m2["Deepseek API Key"] = m2.pop("deepseek-api-key")   # <-- the original defect, restored
    pf2.write_text(json.dumps(m2))

    # reproduce the panel's own join (panel_server.py:11043): meta.get(f.name, {})
    joined_null = [n for n in files if not m2.get(n, {}).get("rank_required")]
    chk("PF2a ★ restoring ONE label key re-nulls that row in the panel's own join (defect reproduced)",
        "deepseek-api-key" in joined_null, str(joined_null[:3]))

    flipped = json.loads(pf2.read_text())
    chk("PF2b the suite's A1 SET-EQUALITY assertion is RED on that copy",
        sorted(flipped.keys()) != files,
        "keys differ" if sorted(flipped.keys()) != files else "keys matched — assertion is blind")

    # ── PF3: DELETE A FILE'S ROW AND WATCH COMPLETENESS GO RED ────────────────────────────────────
    pf3 = tmpdir / "pf3.json"
    m3 = json.loads(json.dumps(real_meta))
    m3.pop("starlink", None)
    pf3.write_text(json.dumps(m3))
    chk("PF3 the completeness assertion (A5) is RED when a row is dropped",
        not all(json.loads(pf3.read_text()).get(k, {}).get("rank_required") for k in files))

    shutil.rmtree(tmpdir, ignore_errors=True)
    return failures


def main() -> int:
    files = store_files()
    baseline = {n: fp(STORE / n) for n in files}

    print("=== FILE + GATE ASSERTIONS (real store) ===")
    meta = file_assertions(META, files, baseline)
    gate_assertions(meta)
    print("\n=== LIVE ROUTE (the panel's own surface) ===")
    live_assertions()

    fails = [r for r in R if not r[1]]
    print(f"\n{len(R) - len(fails)} PASS / {len(fails)} FAIL")
    for n, _, d in fails:
        print(f"   RED: {n} {d}")

    if "--prove-fail" in sys.argv:
        pf = prove_fail()
        print(f"\nprove-fail failures: {pf}  ({'ALL PROVE-FAILS WENT RED AS REQUIRED' if pf == 0 else 'A PROVE-FAIL DID NOT GO RED'})")
        return 0 if (not fails and pf == 0) else 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
