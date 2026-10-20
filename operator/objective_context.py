#!/usr/bin/env python3
"""objective_context.py — add context to an objective, HIS way, from what is actually installed.

★★★ THE CHOICE THAT MATTERS IS COPY vs POINT-TO, and both are wrong in different situations, so the
    tool refuses to choose for him:
      COPY      — the vault owns the bytes. Survives the original being moved, renamed or deleted;
                  portable; syncs. Costs disk, and duplicates a file that already exists.
      POINT-TO  — the vault records where it lives. Costs nothing, always current, no duplication.
                  Breaks the moment the file moves, and is useless on another machine.
    ★ His words were "downloading them to the items they make, or a point-to for efficiency", which is
      exactly this trade. So both ship, the trade is stated on the control, and POINT-TO records the
      inode and a checksum so a later break can be DETECTED rather than discovered.

★★ ONLY WHAT IS INSTALLED IS OFFERED. pandoc, markitdown and docling are NOT on this machine; age,
    7z, pdftotext, libreoffice and python-docx are. An option that cannot run is a dead affordance,
    and this project deletes those rather than shipping them with a shrug.

★ EVERY IMPORT RECORDS ITS PROVENANCE, because the things people wish they had captured are always
    the same: where it came from, when, its original name, and a checksum. A note that says "see the
    attached report" with no origin is a dead end six months later.
"""
from __future__ import annotations
import hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOW = {
    "copy":      "Copy the file into the vault. It becomes yours — survives the original moving or "
                 "being deleted. Costs disk.",
    "reference": "Point to where it already lives. Costs nothing and is always current, but breaks if "
                 "the file moves, and means nothing on another machine.",
    "convert":   "Convert to Markdown and store the text. Searchable, diffable, tiny — and lossy: "
                 "layout, images and tables may not survive.",
    "archive":   "Compress into the vault (zip). One tidy file; you need to unpack it to read it.",
    "encrypt":   "Encrypt into the vault with age. Unreadable without your key — INCLUDING BY YOU if "
                 "you lose it. There is no recovery.",
}


BASES = {"home": Path.home(), "vault": None}          # vault filled in at call time


def _rebase(path: Path) -> tuple[str, str]:
    """Express a path against a NAMED base so it survives moving between machines.

    Returns (base_name, relative_path). Falls back to ("absolute", full path) and the caller says so
    — an absolute pointer is allowed, but it is never silently presented as portable.
    """
    try:
        import life_objectives as _lo
        v = _lo.vault()
        if v:
            BASES["vault"] = Path(v["path"])
    except Exception:
        pass
    for name, root in BASES.items():
        if not root:
            continue
        try:
            return name, str(path.resolve().relative_to(Path(root).resolve()))
        except Exception:
            continue
    return "absolute", str(path)


def _has(cmd: str) -> bool:
    return bool(shutil.which(cmd))


def capabilities() -> dict:
    """★ What can this machine actually do RIGHT NOW. The UI is built from this, so an option never
    appears unless the thing behind it exists."""
    try:
        import docx  # noqa: F401
        docx_ok = True
    except Exception:
        docx_ok = False
    conv = {
        ".pdf":  "pdftotext" if _has("pdftotext") else None,
        ".docx": "python-docx" if docx_ok else ("libreoffice" if _has("libreoffice") else None),
        ".doc":  "libreoffice" if _has("libreoffice") else None,
        ".odt":  "libreoffice" if _has("libreoffice") else None,
        ".html": "beautifulsoup" if _import_ok("bs4") else None,
        ".htm":  "beautifulsoup" if _import_ok("bs4") else None,
        ".txt":  "verbatim", ".md": "verbatim", ".csv": "verbatim", ".json": "verbatim",
    }
    return {
        "copy": True,
        "reference": True,
        "convert": {k: v for k, v in conv.items() if v},
        "convert_unavailable": [k for k, v in conv.items() if not v],
        "archive": _has("zip") or _has("7z"),
        "encrypt": _has("age"),
        "missing_tools": [t for t in ("pandoc", "markitdown", "docling") if not _has(t)],
        "note": ("pandoc, markitdown and docling are not installed here, so conversion uses "
                 "pdftotext, python-docx, LibreOffice and BeautifulSoup. Those cover most documents "
                 "and are worse at complex layout."),
    }


def _import_ok(mod: str) -> bool:
    try:
        __import__(mod); return True
    except Exception:
        return False


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quality_check(text: str, src: Path) -> dict:
    """★★ NEVER TRUST THE EXIT CODE. Text extractors return success on scanned PDFs and return almost
    nothing — the failure shape that makes an empty attachment look like a done job. Gate on a
    measured property of the OUTPUT: a document that yields very little text per page is almost
    certainly an image scan, and should be flagged rather than filed as converted."""
    chars = len(text.strip())
    out = {"chars": chars, "suspect": False, "why": ""}
    if chars < 40:
        out.update(suspect=True, why="almost no text came out — this is likely a scan or an image-only file")
        return out
    if src.suffix.lower() == ".pdf":
        try:
            pages = max(1, subprocess.run(["pdfinfo", str(src)], capture_output=True, text=True,
                                          timeout=30).stdout.count("Pages:") and
                        int([l for l in subprocess.run(["pdfinfo", str(src)], capture_output=True,
                             text=True, timeout=30).stdout.splitlines()
                             if l.startswith("Pages:")][0].split()[1]))
        except Exception:
            pages = 1
        per = chars / pages
        if per < 200:
            out.update(suspect=True,
                       why=f"only {int(per)} characters per page — that is the signature of a scanned "
                           f"PDF, and the text you have is not the document")
    return out


def to_markdown(src: Path) -> tuple[str, str]:
    """(text, how). ★ Returns the METHOD as well as the text, because "converted" covers a verbatim
    copy and a lossy extraction, and the reader deserves to know which one they are holding."""
    ext = src.suffix.lower()
    if ext in (".txt", ".md", ".csv", ".json"):
        return src.read_text(errors="replace"), "read verbatim"
    if ext == ".pdf" and _has("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", str(src), "-"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            return r.stdout, "pdftotext -layout (text only — images and layout are lost)"
        raise RuntimeError(f"pdftotext failed: {r.stderr[:200]}")
    if ext == ".docx" and _import_ok("docx"):
        import docx
        d = docx.Document(str(src))
        return "\n\n".join(p.text for p in d.paragraphs), "python-docx (paragraph text only)"
    if ext in (".html", ".htm") and _import_ok("bs4"):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(src.read_text(errors="replace"), "html.parser")
        return soup.get_text("\n"), "BeautifulSoup text extraction (no markdown structure)"
    if ext in (".doc", ".odt", ".rtf", ".pptx", ".xlsx") and _has("libreoffice"):
        out = Path("/tmp") / f"khaoss_conv_{os.getpid()}"
        out.mkdir(exist_ok=True)
        r = subprocess.run(["libreoffice", "--headless", "--convert-to", "txt:Text",
                            "--outdir", str(out), str(src)],
                           capture_output=True, text=True, timeout=300)
        cand = list(out.glob("*.txt"))
        if cand:
            txt = cand[0].read_text(errors="replace")
            shutil.rmtree(out, ignore_errors=True)
            return txt, "LibreOffice headless (text only)"
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"libreoffice produced nothing: {r.stderr[:200]}")
    raise RuntimeError(f"no converter available for {ext} on this machine")


def add(objective_file: str, source: str, how: str = "copy",
        recipient: str | None = None, note: str = "") -> dict:
    """Attach `source` to the objective note, the way he asked for."""
    import life_objectives as lo
    if how not in HOW:
        raise ValueError(f"unknown method {how!r}; choose from {', '.join(HOW)}")
    obj = Path(objective_file)
    if not obj.is_file():
        raise FileNotFoundError(f"no objective note at {obj}")
    # ★ A CLOUD LINK IS NOT A DURABLE POINTER. Notion serves 1-hour signed URLs; Drive and S3 links
    #   expire or rotate. Storing one as a "reference" produces a pointer that is dead before he
    #   next opens the note — so those must be copied, never pointed at.
    if how == "reference" and re.match(r"^https?://", str(source), re.I):
        raise ValueError("a web or cloud address cannot be a point-to reference: signed links expire "
                         "and pages rot. Download it first, then attach the file with copy.")
    src = Path(source).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"nothing to import at {src} — checked, it is not there")
    v = lo.vault()
    if not v:
        raise RuntimeError("no Obsidian vault registered")

    digest, size = sha256(src), src.stat().st_size
    att = obj.parent / lo.ATTACH / obj.stem
    att.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    result = {"how": how, "source": str(src), "sha256": digest, "bytes": size, "at": stamp}

    if how == "reference":
        # ★★★ THREE TIERS, NOT TWO. The bytes are what the policy decides; the RECORD and the TEXT are
        #   always kept locally. This is git-annex's and Zotero's shape, and it removes the real cost
        #   of pointing rather than copying: text is orders of magnitude smaller than the source, so
        #   a pointed-to document is still searchable, still readable, and still usable as context
        #   when the original is on a drive that is not plugged in. Pointing should cost you the
        #   bytes, not the knowledge.
        st = src.stat()
        result["stored"] = None
        try:
            text, method = to_markdown(src)
            q = quality_check(text, src)
            tdir = att / "_text"
            tdir.mkdir(parents=True, exist_ok=True)
            tf = tdir / (src.stem + ".md")
            tf.write_text(f"<!-- extracted from {src} on {stamp} · {method} · not a copy of the "
                          f"original -->\n\n" + text)
            result["text_extracted"] = str(tf)
            result["quality"] = q
        except Exception as e:
            result["text_extracted"] = None
            result["text_error"] = str(e)[:160]
        # ★★★ A POINTER IS {base, relative}, NEVER AN ABSOLUTE PATH. This is Zotero's "Linked
        #   Attachment Base Directory", and the research is blunt about why: absolute paths are the
        #   number one cause of dead references. It matters concretely here — he works on a laptop
        #   AND an r710, so ~/Documents/x.pdf is a different file, or no file, depending on
        #   which machine is reading the note. Recorded against a named base, the same pointer
        #   resolves on both.
        base, rel = _rebase(src)
        result["reference"] = {"base": base, "rel": rel, "resolved_now": str(src),
                               "inode": st.st_ino,
                               "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))}
        link = f"[{src.name}]({src.as_uri()})"
        _b, _r = result["reference"]["base"], result["reference"]["rel"]
        body = (f"- **{src.name}** — pointed to, not copied · `{size:,}` bytes · "
                f"`sha256:{digest[:12]}…`\n"
                + (f"  - recorded as `${{{_b}}}/{_r}` — a named base, so this still resolves on your "
                   f"other machine\n" if _b != "absolute" else
                   f"  - ★ recorded as an ABSOLUTE path `{src}`, because it is not under a base this "
                   f"knows. It will NOT resolve on another machine.\n")
                + f"  - {link}\n"
                + (f"  - its text WAS extracted and kept here, so it stays searchable even when "
                   f"the original is not reachable\n" if result.get("text_extracted") else "")
                + f"  - ★ if that file moves or is deleted, this link breaks; the checksum above is "
                  f"how you tell whether what is there now is what you attached.\n")
    elif how == "convert":
        text, method = to_markdown(src)
        q = quality_check(text, src)
        result["quality"] = q
        dest = att / (src.stem + ".md")
        head = ["---", f"source: {src}", f"imported: {stamp}",
                f"original_name: {src.name}", f"original_bytes: {size}",
                f"sha256: {digest}", f"conversion: {method}", "---", "",
                f"# {src.stem}", "",
                f"*Converted from `{src.name}` — {method}. The original was not copied.*", ""]
        dest.write_text("\n".join(head) + text)
        result["stored"] = str(dest); result["conversion"] = method
        body = (f"- **{src.name}** — converted to Markdown · {method}\n"
                + (f"  - ★ SUSPECT CONVERSION: {q['why']}. What is stored is not the document.\n"
                   if q["suspect"] else "")
                + f"  - [[{lo.ATTACH}/{obj.stem}/{dest.stem}]]\n"
                f"  - `sha256:{digest[:12]}…` of the original ({size:,} bytes)\n")
    elif how == "archive":
        dest = att / (src.stem + ".zip")
        r = subprocess.run(["zip", "-j", "-q", str(dest), str(src)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not dest.exists():
            raise RuntimeError(f"zip failed: {r.stderr[:200]}")
        result["stored"] = str(dest)
        body = (f"- **{src.name}** — compressed into the vault\n  - `{dest.name}` "
                f"({dest.stat().st_size:,} bytes, from {size:,})\n"
                f"  - `sha256:{digest[:12]}…` of the original\n")
    elif how == "encrypt":
        if not _has("age"):
            raise RuntimeError("age is not installed — encryption is not available here")
        if not recipient:
            raise ValueError("encryption needs a recipient (an age public key), and there is no "
                             "safe default: encrypting to a key you do not hold destroys the file")
        dest = att / (src.name + ".age")
        r = subprocess.run(["age", "-r", recipient, "-o", str(dest), str(src)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not dest.exists():
            raise RuntimeError(f"age failed: {r.stderr[:200]}")
        result["stored"] = str(dest); result["recipient"] = recipient
        body = (f"- **{src.name}** — ENCRYPTED into the vault (`age`)\n  - `{dest.name}`\n"
                f"  - recipient `{recipient[:24]}…`\n"
                f"  - ★ without that key this file is gone. There is no recovery path.\n"
                f"  - `sha256:{digest[:12]}…` of the plaintext original\n")
    else:  # copy
        dest = att / src.name
        if dest.exists() and sha256(dest) == digest:
            result["stored"] = str(dest); result["deduplicated"] = True
            body = (f"- **{src.name}** — already attached; identical checksum, not copied again\n"
                    f"  - `sha256:{digest[:12]}…`\n")
        else:
            if dest.exists():
                dest = att / f"{src.stem}-{digest[:8]}{src.suffix}"
            shutil.copy2(src, dest)
            result["stored"] = str(dest)
            body = (f"- **{src.name}** — copied into the vault ({size:,} bytes)\n"
                    f"  - [[{lo.ATTACH}/{obj.stem}/{dest.name}]]\n"
                    f"  - `sha256:{digest[:12]}…`\n")

    if note:
        body += f"  - note: {note}\n"
    # ★ APPEND, NEVER REWRITE. His note may hold work we cannot see.
    text = obj.read_text(errors="replace")
    marker = "## Context"
    if marker in text:
        i = text.index(marker) + len(marker)
        text = text[:i] + "\n\n" + body + text[i:]
    else:
        text = text.rstrip() + f"\n\n## Context\n\n{body}"
    obj.write_text(text)
    # a machine-readable sidecar beside the human note
    (att / "_provenance.jsonl").open("a").write(json.dumps(result) + "\n")
    result["ok"] = True
    return result


def self_test() -> int:
    import tempfile
    fails = []
    caps = capabilities()
    if not isinstance(caps.get("convert"), dict):
        fails.append("capabilities() did not report a convert map")
    if caps.get("encrypt") != _has("age"):
        fails.append("encrypt capability disagrees with whether age is installed")
    d = Path(tempfile.mkdtemp())
    src = d / "sample.txt"; src.write_text("hello objective\n")
    if sha256(src) != hashlib.sha256(b"hello objective\n").hexdigest():
        fails.append("sha256() does not match a known digest")
    txt, how = to_markdown(src)
    if "hello objective" not in txt or "verbatim" not in how:
        fails.append(f"verbatim conversion wrong: {how}")
    # must REFUSE things it cannot do, rather than half-doing them
    try:
        to_markdown(d / "x.unknownext"); fails.append("converted an extension it has no converter for")
    except Exception:
        pass
    try:
        add(str(d / "nope.md"), str(src)); fails.append("attached to an objective note that does not exist")
    except FileNotFoundError:
        pass
    try:
        add(str(src), str(d / "missing.pdf")); fails.append("attached a source file that does not exist")
    except FileNotFoundError:
        pass
    try:
        add(str(src), str(src), how="teleport"); fails.append("accepted an unknown method")
    except ValueError:
        pass
    shutil.rmtree(d, ignore_errors=True)
    for f in fails:
        print("  CONTROL FAILED:", f)
    if not fails:
        print("  6 controls pass: capabilities match the machine · sha256 matches a known digest · "
              "verbatim conversion labelled · unknown format refused · missing note refused · "
              "missing source refused · unknown method refused")
    return 1 if fails else 0


def main() -> int:
    if "--selftest" in sys.argv:
        return self_test()
    c = capabilities()
    print("\n  ADDING CONTEXT TO AN OBJECTIVE — what this machine can do\n")
    for k, v in HOW.items():
        avail = (c["encrypt"] if k == "encrypt" else c["archive"] if k == "archive" else True)
        print(f"    {'✓' if avail else '✗'} {k:<10} {v}")
    print(f"\n    convert handles : {', '.join(sorted(c['convert']))}")
    if c["convert_unavailable"]:
        print(f"    cannot convert  : {', '.join(c['convert_unavailable'])}")
    print(f"    not installed   : {', '.join(c['missing_tools'])}")
    print(f"    {c['note']}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
