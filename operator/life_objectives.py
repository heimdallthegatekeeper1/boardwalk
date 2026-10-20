#!/usr/bin/env python3
"""life_objectives.py — the objectives live in HIS Obsidian vault, not in our database.

★★★ THE ONE DESIGN DECISION, AND EVERYTHING ELSE FOLLOWS FROM IT. An objective is his, it outlives
    this project, and it must be readable and editable when KHAOSS is not running. So the file of
    record is a markdown note inside his real Obsidian vault. We read it, we render it, we can help
    him write it -- we do not own it. The board shows objectives; the vault HOLDS them.
    ★ The opposite arrangement (ours, with an export button) is how personal data gets stranded in a
      tool. It also fails his own criterion: he must be able to do everything without us, and a note
      in a vault he already opens is the only version of this where that is true.

★★ THE VAULT IS DETECTED, NEVER HARD-CODED. Obsidian records its vaults in obsidian.json, and this
    reads that. Prior notes in this project asserted the vault was ~/vault or ~/obsidian-vault; the
    registered, open vault is neither of those -- it is under ~/boardwalk-project. Hard-coding either
    guess would have written his objectives into a directory Obsidian never opens, where he would
    never see them and would reasonably conclude the feature did nothing.

★ AND IT REFUSES RATHER THAN GUESSING. If no vault is registered, every call says so instead of
    falling back to a plausible directory. A silently-wrong location is worse than an error here:
    the error is visible in a second, the wrong directory is discovered in a month.
"""
from __future__ import annotations
import hashlib, json, os, re, shutil, sys, time
from pathlib import Path
from urllib.parse import quote

HOME = Path.home()
OBSIDIAN_CONFIGS = [
    HOME / ".config" / "obsidian" / "obsidian.json",
    HOME / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",
]
FOLDER = "Objectives"                 # where objectives live INSIDE his vault
ATTACH = "Attachments"                # where imported files land, per objective


# ── the vault ────────────────────────────────────────────────────────────────────────────────────
def vaults() -> list:
    for cfg in OBSIDIAN_CONFIGS:
        if not cfg.exists():
            continue
        try:
            d = json.loads(cfg.read_text())
        except Exception:
            continue
        out = []
        for vid, v in (d.get("vaults") or {}).items():
            p = v.get("path")
            if p:
                out.append({"id": vid, "path": p, "name": Path(p).name,
                            "open": bool(v.get("open")), "exists": Path(p).is_dir(),
                            "config": str(cfg)})
        if out:
            return out
    return []


def vault() -> dict | None:
    """The vault to use: the open one, else the only one, else None."""
    vs = [v for v in vaults() if v["exists"]]
    if not vs:
        return None
    opened = [v for v in vs if v["open"]]
    return opened[0] if opened else (vs[0] if len(vs) == 1 else None)


def obsidian_installed() -> dict:
    """★★★ Is the obsidian:// handler REAL, and does it actually CARRY the URI?

    My first version answered "does the Exec target exist?" and reported usable=True. That is not the
    question. The entry here reads:

        Exec=~/Downloads/Obsidian.AppImage

    with NO %u field code. A .desktop entry without %u or %U does not receive the URI at all: GLib
    reports supports_uris() == False and DROPS the argument. Obsidian launches, reports success, opens
    whatever vault was last open, and never navigates to the note.

    ★★ That is the worst shape a control can have — it does something, so it looks like it worked, and
       the thing you asked for silently did not happen. Measured by a peer with a replica handler:
       launch_uris returned True while the receiving script logged ARGC=0.
    ★ So this reports THREE states, not two: no handler / handler that drops the URI / handler that
      carries it. The middle one is the one that lies.
    """
    desk = HOME / ".local/share/applications/obsidian.desktop"
    line, exec_path, carries = None, None, False
    if desk.exists():
        for ln in desk.read_text(errors="replace").splitlines():
            if ln.startswith("Exec="):
                line = ln[5:].strip()
                exec_path = line.split(" %")[0].strip()
                carries = ("%u" in line) or ("%U" in line)
                break
    exists = bool(exec_path) and (Path(exec_path).exists() or bool(shutil.which(exec_path)))
    if not desk.exists() or not exists:
        state, why = "absent", "no usable obsidian:// handler is registered"
    elif not carries:
        state, why = "drops_uri", ("the handler is registered and Obsidian will open, but its Exec "
                                   "line has no %u field code, so the note address is discarded. It "
                                   "opens the last-used vault and reports success.")
    else:
        state, why = "ok", "the handler carries the note address"
    return {"desktop_entry": desk.exists(), "exec": exec_path, "exec_line": line,
            "exec_exists": exists, "carries_uri": carries, "state": state, "why": why,
            "usable": state == "ok"}


def obsidian_uri(rel_path: str) -> str | None:
    v = vault()
    if not v:
        return None
    return f"obsidian://open?vault={quote(v['name'])}&file={quote(rel_path)}"


# ── objectives ───────────────────────────────────────────────────────────────────────────────────
FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse(text: str) -> tuple[dict, str]:
    m = FM.match(text)
    if not m:
        return {}, text
    meta = {}
    for ln in m.group(1).splitlines():
        if ":" in ln:
            k, _, v = ln.partition(":")
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def objectives() -> tuple[list, dict]:
    """Every objective note in the vault, plus a health report — never a silent empty list."""
    v = vault()
    if not v:
        return [], {"source": "objectives", "status": "unavailable",
                    "reason": ("no Obsidian vault is registered, so there is nowhere to read from. "
                               "Open a vault in Obsidian once and this starts working."),
                    "count": 0}
    root = Path(v["path"]) / FOLDER
    if not root.is_dir():
        return [], {"source": "objectives", "status": "empty",
                    "reason": (f"no {FOLDER}/ folder in the vault '{v['name']}' yet. EMPTY IS NOT "
                               f"BROKEN — it means none have been written, not that they could not "
                               f"be read."), "count": 0, "vault": v["path"]}
    rows = []
    for f in sorted(root.rglob("*.md")):
        try:
            raw = f.read_text(errors="replace")
        except Exception as e:
            rows.append({"id": f.stem, "title": f.stem, "unreadable": str(e), "file": str(f)})
            continue
        meta, body = _parse(raw)
        rel = str(f.relative_to(Path(v["path"])))
        first = next((l.strip() for l in body.splitlines() if l.strip() and not l.startswith("#")), "")
        att_dir = f.parent / ATTACH / f.stem
        rows.append({
            "id": "objective:" + re.sub(r"[^a-z0-9]+", "-", f.stem.lower()).strip("-"),
            "title": meta.get("title") or f.stem,
            "status": meta.get("status", "open"),
            "horizon": meta.get("horizon", ""),
            "why": meta.get("why", ""),
            "one": first[:400],
            "file": str(f), "rel": rel, "vault": v["name"],
            "obsidian": obsidian_uri(rel),
            # ★ _provenance.jsonl is our bookkeeping sidecar, not something he attached. Counting it
            #   made a note with one file report two, which is a small lie that erodes the count.
            "attachments": sorted([a.name for a in att_dir.glob("*")
                                   if not a.name.startswith("_")]) if att_dir.is_dir() else [],
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
        })
    return rows, {"source": "objectives", "status": "ok", "count": len(rows), "vault": v["path"]}


def create(title: str, why: str = "", horizon: str = "") -> dict:
    """Write a new objective INTO his vault. ★ It refuses to clobber: an existing note is never
    overwritten, because the note may already hold work we cannot see."""
    v = vault()
    if not v:
        raise RuntimeError("no Obsidian vault is registered — open one in Obsidian first")
    title = title.strip()
    if not title:
        raise ValueError("an objective needs a title")
    safe = re.sub(r'[\\/:*?"<>|]+', "-", title).strip()[:80]
    root = Path(v["path"]) / FOLDER
    root.mkdir(parents=True, exist_ok=True)
    f = root / f"{safe}.md"
    if f.exists():
        raise FileExistsError(f"{f} already exists — not overwriting a note that may hold your work")
    fm = ["---", f"title: {title}", "status: open"]
    if horizon:
        fm.append(f"horizon: {horizon}")
    if why:
        fm.append(f"why: {why}")
    fm += [f"created: {time.strftime('%Y-%m-%d')}", "source: operator-needs-board", "---", ""]
    body = [f"# {title}", ""]
    if why:
        body += [why, ""]
    body += ["## Why this matters", "", "## What done looks like", "", "## Context", "",
             "<!-- Files added from the board are linked here. -->", ""]
    f.write_text("\n".join(fm + body))
    rel = str(f.relative_to(Path(v["path"])))
    return {"ok": True, "file": str(f), "rel": rel, "vault": v["name"],
            "obsidian": obsidian_uri(rel)}


def self_test() -> int:
    """INV-114 negative control."""
    fails = []
    v = vault()
    if v and not Path(v["path"]).is_dir():
        fails.append("vault() returned a path that does not exist")
    meta, body = _parse("---\ntitle: T\nstatus: open\n---\nhello\n")
    if meta.get("title") != "T" or "hello" not in body:
        fails.append(f"frontmatter parse is wrong: {meta}")
    meta2, body2 = _parse("no frontmatter here")
    if meta2 or body2 != "no frontmatter here":
        fails.append("a note without frontmatter was mangled")
    rows, health = objectives()
    if health.get("status") not in ("ok", "empty", "unavailable"):
        fails.append(f"health status is not one of the three honest states: {health}")
    if health.get("status") == "ok" and not isinstance(rows, list):
        fails.append("ok status with no rows list")
    # the important one: an absent vault must REFUSE, not invent a directory
    real = globals()["vaults"]
    try:
        globals()["vaults"] = lambda: []
        if vault() is not None:
            fails.append("with no vaults registered, vault() still returned one")
        _, h = objectives()
        if h.get("status") != "unavailable":
            fails.append("with no vault, objectives() did not report unavailable — it must never "
                         "fall back to a plausible directory")
        try:
            create("x"); fails.append("create() invented a location with no vault registered")
        except RuntimeError:
            pass
    finally:
        globals()["vaults"] = real
    ob = obsidian_installed()
    if ob["state"] not in ("absent", "drops_uri", "ok"):
        fails.append(f"handler state is not one of the three: {ob['state']}")
    if ob.get("exec_line") and ("%u" in ob["exec_line"]) != ob["carries_uri"]:
        fails.append("carries_uri disagrees with the Exec line it read")
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  5 controls pass: vault path real · frontmatter parsed · plain note untouched · "
              "health is one of three honest states · no vault means REFUSE, never guess")
    return 1 if fails else 0


def main() -> int:
    a = sys.argv[1:]
    if "--selftest" in a:
        return self_test()
    if a and a[0] == "--create":
        print(json.dumps(create(" ".join(a[1:])), indent=2)); return 0
    rows, health = objectives()
    vs = vaults(); ob = obsidian_installed()
    print("\n  LIFE OBJECTIVES — the vault holds them, this board shows them\n")
    print(f"    obsidian vaults registered : {len(vs)}")
    for v in vs:
        print(f"      {'* ' if v['open'] else '  '}{v['path']}  {'(missing!)' if not v['exists'] else ''}")
    # ★ Report the MEASURED reason, not a guessed one. The first version printed "not found" for a
    #   file that exists — the verdict was right and the explanation was invented, which sends you
    #   looking for a missing file instead of a missing field code.
    print(f"    obsidian:// links usable   : {ob['usable']}  [{ob['state']}]")
    if not ob["usable"]:
        print(f"      {ob['why']}")
    print(f"    objectives                 : {health['count']}  [{health['status']}]")
    if health.get("reason"):
        print(f"      {health['reason']}")
    for r in rows:
        print(f"      · {r['title']}  ({r['status']}) — {len(r['attachments'])} attachment(s)")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
