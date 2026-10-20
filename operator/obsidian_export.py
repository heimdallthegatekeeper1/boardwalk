#!/usr/bin/env python3
"""★ THE BOARD, IN THE VAULT — with the one property that makes labels honest.

★ THE CAUTIONARY CASE IS LIVE AND IT IS ON THIS VERY BOARD: 11,504 knowledge files carry a `no-cloud`
  tag nobody approved, nothing enforces, and a cloud model wrote every file it appears on. A clearance
  label in a markdown vault is exactly that shape again — **Obsidian enforces NOTHING**.

★★ SO EVERY EXPORTED NOTE SAYS SO, IN THE FILE. The frontmatter carries `enforcement: none` and
  `advisory: true`, and the body carries one line the reader cannot miss. The difference between a
  property that is ENFORCED and one that is merely WRITTEN DOWN is visible in the file itself — which
  is the whole of Topic 4, and the only way this does not become no-cloud a second time.

★★★ AND THE VAULT IS A MIRROR, NEVER A SOURCE. blockers.json remains the store; these notes are
  regenerated and carry `generated: true`. Editing a note does not change the board — the note says that
  too, because a file that looks editable and silently is not is worse than a read-only one.
"""
from __future__ import annotations
import json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
VAULT = Path.home() / "obsidian-vault" / "operator-board"


def slug(s: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " -_") else "" for c in str(s))
    return "-".join(keep.split())[:70] or "item"


def board():
    """Read the SAME store and the SAME derivation the panel renders — never a second reader."""
    import importlib.util
    d = json.loads((HERE / "blockers.json").read_text())
    spec = importlib.util.spec_from_file_location("khaoss_derive", HERE / "derive_items.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    derived, health = m.derive()
    known = {i.get("id") for i in d.get("items", [])}
    for i in d.get("items", []):
        i.setdefault("source", "hand")
    items = d.get("items", []) + [i for i in derived if i["id"] not in known]
    return items, health


ADVISORY = (
    "> [!warning] These labels are ADVISORY, not enforced\n"
    "> `clearance` and `territory` below are **written down, not applied**. Obsidian enforces nothing,\n"
    "> and nothing else reads this file to decide access. They describe intent so a reader knows the\n"
    "> lane an item belongs to — they do **not** restrict who can open it.\n"
    "> *This warning exists because a label nobody enforces already went wrong here once: 11,504 files\n"
    "> carry a `no-cloud` tag nobody approved and nothing applies.*\n"
)


def enforcement_receipt(target: Path) -> dict:
    """★ THE CIRCLE'S VERDICT, IMPLEMENTED: a note carries TWO kinds of metadata and they must not be
    confused. The ADVISORY block is what the operator asked for. The ENFORCED block is a RECEIPT — written
    by the system, derived from reality, never declared.

    ★★ "A file tagged no-cloud that is already in iCloud is a lie that was told before the tag was
    written." So nothing here is asserted: the mode comes from stat(), the path from resolve(), and
    no_cloud is only claimed when no sync directory is found ABOVE the vault. Where it cannot be
    established it says `unverified` — never `true`.
    """
    import stat as _stat
    SYNC_MARKERS = ("Dropbox", "iCloud", "Nextcloud", "OneDrive", "Google Drive", "Syncthing", ".obsidian/sync")
    p_ = target.resolve()
    synced = [m for m in SYNC_MARKERS if m.lower() in str(p_).lower()]
    try:
        mode = oct(_stat.S_IMODE(p_.stat().st_mode))
    except Exception:
        mode = "unverified"
    if synced:
        no_cloud = "false"          # it is demonstrably inside a sync path
    else:
        # ★ absence of a marker is not proof of absence of sync — say so rather than claim it
        no_cloud = "unverified (no sync directory found above this path; nothing checks egress here)"
    return {
        "no_cloud": no_cloud,
        "mode": mode,
        "vault_path": str(p_),
        "sync_markers_found": ", ".join(synced) if synced else "none",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "checked_by": "operator/obsidian_export.py — derived, not declared",
    }


def note_for(i: dict) -> str:
    lane = i.get("lane") or ""
    fm = {
        "advisory": True,
        "id": i.get("id"), "title": i.get("title"), "severity": i.get("sev"),
        "state": i.get("state", "open"), "territory": lane or "unassigned",
        "clearance": "adept" if i.get("sev") == "stop" else "seeker",
        "source": i.get("source", "hand"), "derived": i.get("source", "hand") != "hand",
        "enforcement": "none", "advisory": True, "generated": True,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "store": "~/boardwalk/operator/blockers.json",
    }
    lines = ["---", "# ADVISORY — for human readers and search. NOT enforced by anything."]
    for k, v in fm.items():
        lines.append(f"{k}: {json.dumps(v) if isinstance(v, (bool, dict, list)) or v is None else v}")
    lines += ["", "# ENFORCED — a RECEIPT written by the system from what it could actually observe.",
              "# If a value here disagrees with reality, this note is in breach and the exporter is wrong.",
              "enforced:"]
    for k, v in enforcement_receipt(VAULT).items():
        lines.append(f"  {k}: {v}")
    lines += ["tags:", "  - operator-board", f"  - severity/{i.get('sev')}"]
    if lane:
        lines.append(f"  - lane/{slug(lane)}")
    lines += ["---", "", f"# {i.get('title')}", "", ADVISORY, ""]
    lines += ["## What this is", "", str(i.get("what") or i.get("one") or "").strip() or "_not written_", ""]
    cost = i.get("if_left") or i.get("blocks")
    if cost and cost != "nothing":
        lines += ["## If left", "", str(cost).strip(), ""]
    if i.get("diagram"):
        lines += ["## What is actually going on", "", "```", str(i["diagram"]).rstrip(), "```", ""]
    lines += ["## What to do", "", str(i.get("todo") or "").strip() or
              ("_Derived from `" + str(i.get("source")) + "` — it clears when its source stops reporting it._"
               if i.get("source", "hand") != "hand" else
               f"_Not written. `blockers.py --set {i.get('id')} done` clears it once decided._"), ""]
    if i.get("note"):
        lines += ["## Operator's note", "", str(i["note"]).strip(), ""]
    lines += ["---", "",
              "*Mirror, not source. Regenerated from the board — **editing this note does not change the "
              "board**. Rule on it in the operator popout, or with `blockers.py --set`.*",
              "", "[[Operator Board]]"]
    return "\n".join(lines) + "\n"


def main() -> int:
    items, health = board()
    VAULT.mkdir(parents=True, exist_ok=True)
    written, kept = [], set()
    for i in items:
        name = f"{slug(i.get('id'))}.md"
        kept.add(name)
        (VAULT / name).write_text(note_for(i))
        written.append(name)

    # index
    by_sev = {"stop": [], "hold": [], "info": []}
    for i in items:
        by_sev.setdefault(i.get("sev", "hold"), []).append(i)
    idx = ["---", "title: Operator Board", "tags:", "  - operator-board", "generated: true",
           "enforcement: none", "advisory: true", "---", "", "# Operator Board", "", ADVISORY, "",
           f"*{len([i for i in items if i.get('state','open')=='open'])} open · generated "
           f"{time.strftime('%Y-%m-%d %H:%M')} from `blockers.json` + 3 derived sources.*", ""]
    for sev, label in (("stop", "Blocking"), ("hold", "Waiting on you"), ("info", "Your call")):
        rows = [i for i in by_sev.get(sev, []) if i.get("state", "open") == "open"]
        if not rows:
            continue
        idx += [f"## {label} ({len(rows)})", ""]
        for i in rows:
            src = "" if i.get("source", "hand") == "hand" else f" · _derived · {i.get('source')}_"
            idx.append(f"- [[{slug(i.get('id'))}|{i.get('title')}]]{src}")
        idx.append("")
    idx += ["## Source health", ""]
    for h in health:
        idx.append(f"- **{h['source']}** — {h['status']} ({h['count']})" + (f" — {h['reason']}" if h.get("reason") else ""))
    (VAULT / "Operator Board.md").write_text("\n".join(idx) + "\n")

    # ★ remove notes for items that no longer exist, so the mirror cannot outlive its source
    stale = [f for f in VAULT.glob("*.md") if f.name not in kept and f.name != "Operator Board.md"]
    for f in stale:
        f.unlink()

    print(f"  wrote {len(written)} note(s) + index → {VAULT}")
    print(f"  removed {len(stale)} stale note(s) (a mirror must not outlive its source)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
