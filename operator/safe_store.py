#!/usr/bin/env python3
"""safe_store.py — write a JSON store so a crash or a second writer cannot destroy it.

★★★ TWO SEPARATE FAILURES, both live on this machine until 2026-09-07:

  1. `open(path, "w")` TRUNCATES BEFORE IT WRITES. A crash, a kill or a full disk between those two
     moments leaves the store empty or half-written. For the operator board that meant every blocker
     and every ruling gone, with all surfaces agreeing about it because they read one file.
  2. NOTHING SERIALISED CONCURRENT WRITERS. Every agent here runs as the same uid, no code used flock,
     and git had no commits for six weeks — so two seats writing at once was possible, undetectable
     AND unattributable. Three properties that make a corruption impossible to investigate afterwards.

★ The fix is not "be careful": temp file → fsync → atomic rename, under an exclusive lock. rename()
  within a filesystem is atomic, so a reader sees the old file or the new one and never a partial one.

★★ AND IT REFUSES TO WRITE SOMETHING IT CANNOT READ BACK. The payload is serialised BEFORE the old
  file is touched, so a store is never destroyed on the way to failing.
"""
from __future__ import annotations
import fcntl, json, os, tempfile
from pathlib import Path


def write_json(path, obj, indent: int = 2) -> dict:
    p = Path(path)
    # ★ Serialise FIRST. If the object cannot be encoded, nothing has been touched yet.
    payload = json.dumps(obj, indent=indent)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = p.parent / (p.name + ".lock")
    lf = open(lock, "a+")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name + "-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(p))
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
    finally:
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
        finally:
            lf.close()
    return {"ok": True, "path": str(p), "bytes": len(payload)}


def read_json(path, default=None):
    """Shared lock on read, so a reader never catches a writer mid-rename on a filesystem that
    would otherwise allow it."""
    p = Path(path)
    if not p.exists():
        return default
    lock = p.parent / (p.name + ".lock")
    try:
        lf = open(lock, "a+")
    except Exception:
        lf = None
    try:
        if lf:
            fcntl.flock(lf, fcntl.LOCK_SH)
        return json.loads(p.read_text())
    except Exception:
        return default
    finally:
        if lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()


def self_test() -> int:
    import shutil, subprocess, sys, time
    fails = []
    d = Path(tempfile.mkdtemp())
    f = d / "store.json"
    write_json(f, {"n": 1})
    if read_json(f) != {"n": 1}:
        fails.append("round trip failed")

    # ★ THE CONTROL THAT MATTERS: an unserialisable payload must leave the OLD store intact.
    #   A writer that destroys the file on its way to failing is worse than one that never ran.
    try:
        write_json(f, {"bad": object()})
        fails.append("accepted an unserialisable payload")
    except TypeError:
        pass
    if read_json(f) != {"n": 1}:
        fails.append("a FAILED write destroyed the existing store — the exact thing this prevents")

    # concurrent writers must leave valid JSON, never a truncated file
    code = ("import sys;sys.path.insert(0,%r);import safe_store as s\n"
            "[s.write_json(%r, {'n': i}) for i in range(40)]" % (str(Path(__file__).parent), str(f)))
    ps = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(4)]
    [x.wait() for x in ps]
    if read_json(f) is None:
        fails.append("160 concurrent writes across 4 processes left an unreadable store")

    # no temp files left behind
    leftovers = [x for x in d.iterdir() if x.name.startswith(".") and x.name.endswith(".tmp")]
    if leftovers:
        fails.append(f"left {len(leftovers)} temp file(s) behind")
    shutil.rmtree(d, ignore_errors=True)
    for x in fails:
        print("  CONTROL FAILED:", x)
    if not fails:
        print("  5 controls pass: round trip · unserialisable payload refused · a failed write leaves "
              "the old store intact · 160 concurrent writes stay valid JSON · no temp files left")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(self_test() if "--selftest" in sys.argv else 0)
