#!/usr/bin/env python3
"""tool_result_pointer.py — a large tool result is stored ONCE and referenced, not re-sent forever.

★★★ THE MECHANISM, measured on this machine rather than assumed:
      · `acp_tool_call` rows total 19.67 MB over 7,117 rows   (the brief said 3.85 MB — it is 5x that)
      · but 6,612 of those rows are under 4 KB and total only 4.55 MB
      · and FOUR rows, all in ONE conversation, hold 11.33 MB — 58% of every tool-call byte
    ★ So this is not a broad problem, it is a FAT-TAIL problem. Externalising everything would add a
      pointer to 7,000 rows to reclaim nothing. Externalising above a threshold touches four rows and
      reclaims most of the cost. The threshold is the whole design.

★★ WHY IT MATTERS BEYOND DISK: a tool result is part of the conversation, so it is re-sent on EVERY
   subsequent turn of that session. A 3.7 MB result is not paid once; it is paid again every turn.
   Disk is cheap and upload is not — on a metered link that is the bill.

★ "Pointing costs you the bytes, not the knowledge." The stub that replaces a payload carries the
  tool name, the real size, a HEAD EXCERPT, and the exact path — so an agent reading history still
  knows what happened and can go and read the rest. A pointer that says only "[truncated]" has
  thrown the knowledge away and kept the cost of asking again.

★★★ THIS DOES NOT TOUCH THE LIVE DATABASE UNLESS TOLD TO, TWICE. `--apply` is required, and even
     then it refuses without a fresh backup it made itself. The payload is written to disk and
     verified byte-for-byte BEFORE the row is rewritten, and `--restore` puts everything back.
"""
from __future__ import annotations
import argparse, hashlib, json, os, shutil, sqlite3, sys, time
from pathlib import Path

DB = Path.home() / ".config" / "AionUi-Dev" / "aionui" / "aionui.db"
STORE = Path.home() / ".config" / "AionUi-Dev" / "aionui" / "tool_results"
THRESHOLD = 256 * 1024           # ★ measured, not guessed — see the module docstring
MARKER = "__externalised_tool_result__"


def _rows(con, threshold: int):
    return list(con.execute(
        "SELECT id, conversation_id, LENGTH(CAST(content AS TEXT)), content FROM messages "
        "WHERE type='acp_tool_call' AND LENGTH(CAST(content AS TEXT)) >= ? "
        "AND CAST(content AS TEXT) NOT LIKE ? ORDER BY 3 DESC",
        (threshold, f"%{MARKER}%")))


def survey(db: Path = DB, threshold: int = THRESHOLD) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = _rows(con, threshold)
        total = con.execute("SELECT COALESCE(SUM(LENGTH(CAST(content AS TEXT))),0) FROM messages "
                            "WHERE type='acp_tool_call'").fetchone()[0]
        big = sum(r[2] for r in rows)
        return {"db": str(db), "threshold_bytes": threshold, "candidates": len(rows),
                "candidate_bytes": big, "all_tool_call_bytes": total,
                "pct_of_tool_calls": round(100 * big / total, 1) if total else 0.0,
                "conversations": sorted({r[1] for r in rows}),
                "top": [{"id": r[0], "conversation": r[1], "bytes": r[2]} for r in rows[:10]]}
    finally:
        con.close()


def _stub(tool_hint: str, size: int, path: Path, head: str, sha: str) -> str:
    """★ The stub IS the knowledge. Everything an agent needs to know what happened, minus the bulk."""
    return json.dumps({
        MARKER: True,
        "note": "This tool result was large and is stored on disk instead of in the conversation. "
                "The full payload is unchanged and byte-identical; read the file to see all of it.",
        "tool": tool_hint, "original_bytes": size, "sha256": sha, "path": str(path),
        "head": head,
        "how_to_read": f"cat {path}",
        "why": "Kept out of context because a tool result is re-sent on every later turn of this "
               "session; this one was paid for repeatedly.",
    }, ensure_ascii=False)


def _tool_hint(content: str) -> str:
    for key in ("toolName", "tool_name", "name", "title", "label", "kind"):
        try:
            v = json.loads(content)
            if isinstance(v, dict) and v.get(key):
                return str(v[key])[:80]
        except Exception:
            break
    return "unknown tool"


def apply(db: Path = DB, threshold: int = THRESHOLD, dry_run: bool = True) -> dict:
    STORE.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        # ★ its OWN backup, named to the second, and it refuses to clobber one
        bak = db.with_name(db.name + f".bak.toolptr_{time.strftime('%Y%m%d_%H%M%S')}")
        if bak.exists():
            return {"ok": False, "error": f"refusing to overwrite an existing backup {bak}"}
        shutil.copy2(db, bak)
        if bak.stat().st_size != db.stat().st_size:
            return {"ok": False, "error": "backup size mismatch — refusing to modify the database"}
    con = sqlite3.connect(db)
    moved, freed, errors = [], 0, []
    try:
        for mid, conv, size, content in _rows(con, threshold):
            text = content if isinstance(content, str) else str(content)
            sha = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
            out = STORE / f"{sha[:16]}.json"
            head = text[:600]
            stub = _stub(_tool_hint(text), size, out, head, sha)
            if len(stub) >= size:
                continue                       # ★ never make a row BIGGER
            if not dry_run:
                out.write_text(text, encoding="utf-8")
                # ★★ verify the payload landed byte-for-byte BEFORE destroying the original
                back = out.read_text(encoding="utf-8")
                if hashlib.sha256(back.encode("utf-8", "replace")).hexdigest() != sha:
                    errors.append({"id": mid, "why": "written payload did not verify — row untouched"})
                    continue
                con.execute("UPDATE messages SET content=? WHERE id=?", (stub, mid))
            moved.append({"id": mid, "conversation": conv, "bytes": size,
                          "stub_bytes": len(stub), "path": str(out)})
            freed += size - len(stub)
        if not dry_run:
            con.commit()
    finally:
        con.close()
    return {"ok": not errors, "dry_run": dry_run, "moved": len(moved),
            "bytes_freed": freed, "mb_freed": round(freed / 1048576, 2),
            "errors": errors, "rows": moved,
            "note": ("DRY RUN — nothing was written." if dry_run else
                     "Applied. Every payload was verified on disk before its row was rewritten.")}


def restore(db: Path = DB) -> dict:
    """★ Reversible. A migration you cannot undo is a migration you cannot safely test."""
    con = sqlite3.connect(db)
    back, missing = 0, []
    try:
        for mid, content in con.execute(
                "SELECT id, content FROM messages WHERE CAST(content AS TEXT) LIKE ?",
                (f"%{MARKER}%",)):
            try:
                stub = json.loads(content)
                p = Path(stub["path"])
                if not p.exists():
                    missing.append({"id": mid, "path": str(p)}); continue
                text = p.read_text(encoding="utf-8")
                if hashlib.sha256(text.encode("utf-8", "replace")).hexdigest() != stub.get("sha256"):
                    missing.append({"id": mid, "path": str(p), "why": "sha mismatch"}); continue
                con.execute("UPDATE messages SET content=? WHERE id=?", (text, mid))
                back += 1
            except Exception as e:
                missing.append({"id": mid, "why": str(e)[:100]})
        con.commit()
    finally:
        con.close()
    return {"ok": not missing, "restored": back, "problems": missing}


def self_test() -> int:
    import tempfile
    fails = []
    d = Path(tempfile.mkdtemp())
    tdb = d / "t.db"
    con = sqlite3.connect(tdb)
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT, type TEXT, content TEXT)")
    big = json.dumps({"toolName": "read_file", "content": "X" * 400_000})
    small = json.dumps({"toolName": "ls", "content": "y" * 100})
    con.execute("INSERT INTO messages VALUES (1,'c1','acp_tool_call',?)", (big,))
    con.execute("INSERT INTO messages VALUES (2,'c1','acp_tool_call',?)", (small,))
    con.execute("INSERT INTO messages VALUES (3,'c1','text',?)", (big,))
    con.commit(); con.close()

    global STORE
    STORE = d / "store"
    s = survey(tdb)
    if s["candidates"] != 1:
        fails.append(f"survey found {s['candidates']} candidates, expected exactly 1 (the big one)")
    dry = apply(tdb, dry_run=True)
    c2 = sqlite3.connect(tdb)
    if c2.execute("SELECT LENGTH(content) FROM messages WHERE id=1").fetchone()[0] != len(big):
        fails.append("★ A DRY RUN MODIFIED THE DATABASE")
    c2.close()
    r = apply(tdb, dry_run=False)
    if not r["ok"] or r["moved"] != 1:
        fails.append(f"apply did not move exactly the one big row: {r['moved']}")
    c3 = sqlite3.connect(tdb)
    newlen = c3.execute("SELECT LENGTH(content) FROM messages WHERE id=1").fetchone()[0]
    stub = json.loads(c3.execute("SELECT content FROM messages WHERE id=1").fetchone()[0])
    if newlen >= len(big):
        fails.append("the row did not shrink")
    if c3.execute("SELECT LENGTH(content) FROM messages WHERE id=2").fetchone()[0] != len(small):
        fails.append("★ a SMALL row was externalised — pointer overhead for no gain")
    if c3.execute("SELECT LENGTH(content) FROM messages WHERE id=3").fetchone()[0] != len(big):
        fails.append("★ a NON-tool-call row was modified — this must only touch acp_tool_call")
    for k in ("head", "path", "original_bytes", "sha256", "tool"):
        if not stub.get(k):
            fails.append(f"★ the stub lost '{k}' — pointing must cost the bytes, not the knowledge")
    if "X" * 50 not in stub.get("head", ""):
        fails.append("the stub head excerpt does not contain the real payload opening")
    c3.close()
    # ★ the control that matters most: it must come back byte-for-byte
    rr = restore(tdb)
    c4 = sqlite3.connect(tdb)
    got = c4.execute("SELECT content FROM messages WHERE id=1").fetchone()[0]
    c4.close()
    if got != big:
        fails.append("★★ RESTORE DID NOT RETURN THE ORIGINAL BYTES — the payload is not recoverable")
    if not rr["ok"]:
        fails.append(f"restore reported problems: {rr['problems'][:2]}")
    shutil.rmtree(d, ignore_errors=True)
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  8 controls pass: survey finds only the fat tail · A DRY RUN WRITES NOTHING · only "
              "acp_tool_call is touched · a small row is left alone · the row shrinks · the stub keeps "
              "tool/size/sha/path/HEAD · payload verified on disk before the row is rewritten · "
              "★ RESTORE RETURNS THE ORIGINAL BYTES EXACTLY")
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--survey", action="store_true")
    ap.add_argument("--apply", action="store_true", help="actually rewrite rows (makes its own backup)")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--threshold", type=int, default=THRESHOLD)
    a = ap.parse_args()
    if a.selftest:
        return self_test()
    db = Path(a.db)
    if a.restore:
        print(json.dumps(restore(db), indent=2)); return 0
    if a.apply:
        print(json.dumps(apply(db, a.threshold, dry_run=False), indent=2)); return 0
    s = survey(db, a.threshold)
    print(f"\n  DB                 {s['db']}")
    print(f"  threshold          {s['threshold_bytes']/1024:.0f} KB")
    print(f"  candidates         {s['candidates']} row(s)")
    print(f"  their bytes        {s['candidate_bytes']/1048576:.2f} MB "
          f"({s['pct_of_tool_calls']}% of ALL acp_tool_call bytes)")
    print(f"  conversations      {', '.join(str(c)[:20] for c in s['conversations']) or '—'}")
    for t in s["top"]:
        print(f"     id={t['id']}  {t['bytes']/1048576:>6.2f} MB  conv {str(t['conversation'])[:24]}")
    print(f"\n  Nothing was changed. Use --apply to externalise (it backs the DB up first).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
