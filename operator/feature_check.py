#!/usr/bin/env python3
"""feature_check.py — does the board actually DO what has been claimed for it?

★★★ WHY THIS EXISTS. He asked to "run a check to see if features landed properly", and the honest
    answer needed a tool rather than a paragraph: over several sessions this board has accumulated
    claims — a search, category chips, a per-item AI assist, a finished-work page, resolvable row
    numbers — and each was verified once, by hand, at the moment it shipped. Nothing re-checked them
    afterwards, and two of them had already regressed in ways only pressing them would reveal.

★★ IT DRIVES THE REAL PAGE. A route returning 200 is not a feature; a DOM node existing is not a
    feature. Every check here loads the served panel in a browser, finds the control a person would
    click, clicks it, and asserts on what changed. The one thing it cannot do is judge whether a
    control is FINDABLE — he could not find the search for two days while it worked perfectly — so
    position is asserted explicitly where it matters.

★ VERDICTS ARE THREE, NOT TWO. PASS means observed working. FAIL means observed broken. UNPROVEN
    means the check could not run (no browser, panel down) — never silently a pass. A checker that
    cannot distinguish "fine" from "did not look" is the failure this whole board exists to end.
"""
from __future__ import annotations
import json, sys, time
from urllib.request import urlopen, Request

PANEL = "http://127.0.0.1:7842"
RESULTS: list[dict] = []


def record(name: str, verdict: str, detail: str, group: str = "") -> None:
    RESULTS.append({"name": name, "verdict": verdict, "detail": detail, "group": group})


def http(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(PANEL + path, data=data,
                  headers={"Content-Type": "application/json"} if data else {})
    if method != "GET":
        req.get_method = lambda: method
    with urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def check_routes() -> None:
    """The data behind each feature. A feature whose route is dead is dead however it renders."""
    for name, path, want in [
        ("board.json carries row numbers",  "/operator/board.json", "n"),
        ("facets (clickable categories)",   "/operator/facets",     "facets"),
        ("finished record",                 "/operator/settled",    "counts"),
        ("agent browser status",            "/operator/browser",    "durable_profile"),
        ("assist routes (per-item AI)",     "/operator/assist/routes", "order"),
        ("dispatch targets",                "/operator/dispatch/targets", "groups"),
    ]:
        try:
            st, d = http(path)
            if st != 200:
                record(name, "FAIL", f"HTTP {st}", "routes"); continue
            if want == "n":
                items = d.get("items", [])
                withn = [i for i in items if isinstance(i.get("n"), int)]
                if not withn:
                    record(name, "FAIL", "no item carries `n` — agents cannot resolve his numbers", "routes")
                else:
                    record(name, "PASS", f"{len(withn)}/{len(items)} rows numbered", "routes")
            elif want in d:
                extra = ""
                if want == "facets":
                    extra = f" ({len(d['facets'])} facets, {sum(len(f['values']) for f in d['facets'])} chips)"
                record(name, "PASS", f"200{extra}", "routes")
            else:
                record(name, "FAIL", f"200 but no `{want}` in the payload", "routes")
        except Exception as e:
            record(name, "UNPROVEN", f"could not reach it: {e}", "routes")


def check_number_resolution() -> None:
    """★ The near-miss check. He rules by number; a wrong mapping acted on is the worst outcome
    this board can produce, so both the resolver and the words-vs-number cross-check are exercised."""
    try:
        st, d = http("/operator/resolve?n=1,2,99999")
        ok = st == 200 and d.get("resolved")
        oor = [p for p in d.get("problems", []) if "out of range" in str(p)]
        if ok and oor:
            record("resolve numbers -> rows", "PASS",
                   f"{len(d['resolved'])} resolved, and 99999 was REPORTED out of range, not dropped",
                   "rulings")
        elif ok:
            record("resolve numbers -> rows", "FAIL",
                   "resolved, but an impossible number was NOT reported — silent drops mean an agent "
                   "believes it acted on everything he asked", "rulings")
        else:
            record("resolve numbers -> rows", "FAIL", f"HTTP {st}", "rulings")
    except Exception as e:
        record("resolve numbers -> rows", "UNPROVEN", str(e), "rulings")

    try:
        # ★ BUILD THE CASE FROM TODAY'S BOARD. The first version hard-coded "12. the Sleeve is now
        #   set to $1000" — and failed the day the board renumbered and 12 became a different row.
        #   A test for the number-instability hazard that is itself broken by number instability is
        #   not a test. Resolve a row, then quote that row's OWN words back at it.
        st0, d0 = http("/operator/resolve?n=1")
        row = (d0.get("resolved") or [{}])[0]
        words = " ".join(str(row.get("title", "")).split()[:6]) or "the board"
        st, d = http("/operator/ruling", "POST", {
            "ruling": f"1. {words}. 2. Do as recommended."})
        rs = {r["n"]: r["verdict"] for r in d.get("rulings", [])}
        good = rs.get(1) == "CONFIRMED"
        flagged = rs.get(2) in ("MISMATCH", "NO-DESCRIPTION")
        if good and flagged:
            record("ruling cross-check (number vs his words)", "PASS",
                   "described ruling CONFIRMED; a bare 'do as recommended' is refused, not guessed",
                   "rulings")
        else:
            record("ruling cross-check (number vs his words)", "FAIL",
                   f"described={rs.get(1)} bare={rs.get(2)} — a bare number must never confirm", "rulings")
    except Exception as e:
        record("ruling cross-check (number vs his words)", "UNPROVEN", str(e), "rulings")


def check_ui() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        record("browser checks", "UNPROVEN", f"playwright unavailable: {e}", "ui")
        return
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1580, "height": 1400})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)[:160]))
        try:
            pg.goto(PANEL + "/", wait_until="domcontentloaded", timeout=45000)
            pg.evaluate("()=>{const e=document.getElementById('operator-needs-panel');"
                        "if(e) e.style.display='block';}")
            # ★ The panel is single-threaded and every board payload re-derives eight sources, so
            #   the route phase above can leave a queue this page waits behind. 90s was not enough
            #   and the checker reported "no rows rendered" about a board that renders perfectly.
            #   A timeout that fires on a slow server is a FALSE FAILURE, and on a verification
            #   surface a false failure costs exactly as much trust as a missed one.
            pg.wait_for_function(
                "()=>document.querySelectorAll('#on-items article.bd-card').length>0", timeout=240000)
        except Exception as e:
            record("board renders", "FAIL", f"no rows rendered: {str(e)[:90]}", "ui")
            b.close(); return
        record("board renders", "PASS",
               f"{pg.evaluate('()=>document.querySelectorAll(\"#on-items article.bd-card\").length')} rows", "ui")

        # ── POSITION. He could not find the search for two days while it worked. ──
        pos = pg.evaluate("""()=>{
          const q=document.getElementById('bd-q'), inc=document.getElementById('bd-incomplete'),
                fac=document.getElementById('bd-facets'), items=document.getElementById('on-items');
          const top=e=>e?e.getBoundingClientRect().top+window.scrollY:null;
          return {q:top(q), inc:top(inc), fac:top(fac), items:top(items)};
        }""")
        if pos["q"] is None:
            record("search is present", "FAIL", "no #bd-q on the page", "position")
        else:
            record("search is present", "PASS", "found", "position")
            if pos["inc"] is not None and pos["q"] < pos["inc"]:
                record("search ABOVE 'board is incomplete'", "PASS",
                       f"search at {int(pos['q'])}px, warning at {int(pos['inc'])}px", "position")
            else:
                record("search ABOVE 'board is incomplete'", "FAIL",
                       f"search at {pos['q']}px is BELOW the warning at {pos['inc']}px — "
                       "he asked for it above", "position")
            if pos["fac"] is not None and pos["inc"] is not None and pos["fac"] < pos["inc"]:
                record("categories ABOVE 'board is incomplete'", "PASS", "yes", "position")
            else:
                record("categories ABOVE 'board is incomplete'", "FAIL",
                       f"chips at {pos['fac']}px vs warning at {pos['inc']}px", "position")
            if pos["q"] < 1200:
                record("search near the top of the page", "PASS", f"{int(pos['q'])}px from the top", "position")
            else:
                record("search near the top of the page", "FAIL",
                       f"{int(pos['q'])}px down — that is below the fold on most screens", "position")

        # ── THE PRIMARY ACTIONS. ★ Each of these was a control buried inside a block half a page
        #   down. "Near the top" is the requirement, so position is asserted, not just presence —
        #   presence was already true when he could not find them.
        prim = pg.evaluate("""()=>{
          const top=e=>e?Math.round(e.getBoundingClientRect().top+scrollY):null;
          const b=[...document.querySelectorAll('.btn-primary-op')].map(x=>x.innerText.trim());
          return {labels:b, first:top(document.querySelector('.btn-primary-op')),
                  inc:top(document.getElementById('bd-incomplete')),
                  float:[...document.querySelectorAll('.btn-float-op')].map(x=>x.innerText.trim())};
        }""")
        want = ["Guided", "Assist", "Add"]
        have = [w for w in want if any(w in l for l in prim["labels"])]
        record("Guided Run / AI Assist / Add are at the top", "PASS" if len(have) == 3 else "FAIL",
               f"found {prim['labels']}", "position")
        if prim["first"] is not None and prim["inc"] is not None:
            record("primary actions ABOVE 'board is incomplete'",
                   "PASS" if prim["first"] < prim["inc"] else "FAIL",
                   f"actions at {prim['first']}px, warning at {prim['inc']}px", "position")
        fhave = [w for w in want if any(w in l for l in prim["float"])]
        record("the same three actions in the floating box", "PASS" if len(fhave) == 3 else "FAIL",
               f"found {prim['float']}", "float")

        # ── the outbox folds away, and still warns while folded ──
        try:
            # ★ WAIT FOR THE RE-RENDER, NOT A DURATION. bdOutboxToggle() re-fetches, so a fixed
            #   800ms measured the block before it had folded and reported the control broken. L85,
            #   in my own checker, three checks after I wrote the lesson.
            H = "()=>{const e=document.getElementById('bd-outbox');return e?e.offsetHeight:0;}"
            h0 = pg.evaluate(H)
            pg.evaluate("()=>bdOutboxToggle()")
            try:
                pg.wait_for_function(f"()=>({H})() < {h0}", timeout=15000)
            except Exception:
                pass
            h1 = pg.evaluate(H)
            txt = pg.evaluate("()=>{const e=document.getElementById('bd-outbox');return e?e.innerText:'';}")
            pg.evaluate("()=>bdOutboxToggle()")
            try:
                pg.wait_for_function(f"()=>({H})() > {h1}", timeout=15000)
            except Exception:
                pass
            h2 = pg.evaluate(H)
            if h0 == 0:
                record("'what you sent' folds away", "UNPROVEN",
                       "nothing has been sent, so the block is not rendered", "outbox")
            else:
                record("'what you sent' folds away", "PASS" if h1 < h0 and h2 > h1 else "FAIL",
                       f"{h0}px -> {h1}px folded -> {h2}px reopened", "outbox")
                # ★ A folded block that hides a warning is a hidden warning, not a folded block.
                record("folded, it still says how many are unanswered",
                       "PASS" if "WHAT YOU SENT" in txt else "FAIL",
                       f"collapsed text: {txt.replace(chr(10), ' / ')[:80]!r}", "outbox")
        except Exception as e:
            record("'what you sent' folds away", "UNPROVEN", str(e)[:80], "outbox")

        # ── the floating search ──
        fl = pg.evaluate("""()=>{
          const f=document.getElementById('bd-float');
          if(!f) return null;
          const cs=getComputedStyle(f), r=f.getBoundingClientRect();
          return {display:cs.display, position:cs.position, right:r.right, top:r.top,
                  vw:window.innerWidth, hasInput:!!document.getElementById('bd-float-q')};
        }""")
        if not fl:
            record("floating search exists", "FAIL", "#bd-float not in the page", "float")
        elif fl["display"] == "none":
            record("floating search visible", "FAIL", "present but display:none while the board is open", "float")
        elif fl["position"] != "fixed":
            record("floating search is pinned", "FAIL", f"position:{fl['position']} — it will scroll away", "float")
        elif fl["right"] < fl["vw"] * 0.6 or fl["top"] > 160:
            record("floating search is top-right", "FAIL",
                   f"right edge {int(fl['right'])} of {fl['vw']}, top {int(fl['top'])}", "float")
        else:
            record("floating search top-right, pinned", "PASS",
                   f"fixed, right edge {int(fl['right'])}/{fl['vw']}px, top {int(fl['top'])}px", "float")
            # it must be the SAME search, not a second one
            # ★ DERIVE THE SEARCH TERM FROM THE LIVE BOARD. This hard-coded "cockpit" and started
            #   failing the day he settled that row — the checker reporting a broken search because
            #   its test data had moved. Same class as the ruling check that hard-coded item 12.
            term = pg.evaluate("""()=>{
              const t=document.querySelector('#on-items .bd-title, #bd-results .bd-title');
              if(!t) return 'the';
              const w=(t.innerText||'').split(/\\s+/).filter(x=>x.length>5 && /^[a-z]+$/i.test(x));
              return w[0]||'the';
            }""")
            pg.fill("#bd-float-q", term); pg.wait_for_timeout(1800)
            sync = pg.evaluate("""()=>({main:(document.getElementById('bd-q')||{}).value,
                                        rows:document.querySelectorAll('#on-items article.bd-card, #bd-results article.bd-card').length,
                                        count:(document.getElementById('bd-float-n')||{}).innerText})""")
            if sync["main"] == term and sync["rows"] > 0:
                record("floating search drives the board", "PASS",
                       f"typing {term!r} filtered to {sync['rows']} row(s); main box mirrored; "
                       f"count reads {sync['count'][:40]!r}", "float")
            else:
                record("floating search drives the board", "FAIL",
                       f"main box={sync['main']!r}, rows={sync['rows']}", "float")
            pg.evaluate("()=>bdFloatClear()"); pg.wait_for_timeout(900)

        # ── OVERLAP. ★ He found this before the checker did: the floating box was sitting on top of
        #   "Open the docket →" and the board's Refresh / Read all. A covered control is worse than a
        #   missing one, because you click where you remember it and hit whatever is on top. Position
        #   was asserted; OCCLUSION was not, and those are different questions.
        ov = pg.evaluate("""()=>{
          const f=document.getElementById('bd-float');
          if(!f || getComputedStyle(f).display==='none') return [];
          const r=f.getBoundingClientRect();
          const hits=[];
          document.querySelectorAll('button,a,input,select,summary').forEach(function(e){
            if(f.contains(e)) return;
            const cs=getComputedStyle(e);
            if(cs.display==='none'||cs.visibility==='hidden') return;
            const b=e.getBoundingClientRect();
            if(b.width===0||b.height===0) return;
            const inter = !(b.right<r.left || b.left>r.right || b.bottom<r.top || b.top>r.bottom);
            if(inter) hits.push((e.innerText||e.id||e.tagName).trim().slice(0,40));
          });
          return hits;
        }""")
        record("floating search covers no control", "PASS" if not ov else "FAIL",
               "nothing underneath it" if not ov
               else f"it is sitting on top of: {', '.join(ov[:4])}", "float")

        # ── search behaviour ──
        def rows() -> int:
            # ★ Filtered rows render in #bd-results (moved there so a search result is reachable
            #   without scrolling past ten blocks). Counting only #on-items reported 0 for every
            #   working filter — the checker asserting on WHERE the rows used to be rather than on
            #   whether filtering works.
            return pg.evaluate("()=>document.querySelectorAll("
                               "'#on-items article.bd-card, #bd-results article.bd-card').length")
        base = rows()
        pg.fill("#bd-q", "Trade"); pg.wait_for_timeout(1400)
        trade = rows()
        marks = pg.evaluate("()=>document.querySelectorAll('mark.bd-hit').length")
        pg.fill("#bd-q", "trading"); pg.wait_for_timeout(1300)
        trading = rows()
        pg.fill("#bd-q", "zzzznope"); pg.wait_for_timeout(1200)
        nope = rows()
        pg.fill("#bd-q", ""); pg.wait_for_timeout(1100)
        if trade > 2 and trade == trading:
            record("search finds word forms ('Trade' == 'trading')", "PASS",
                   f"both return {trade} of {base}", "search")
        else:
            record("search finds word forms ('Trade' == 'trading')", "FAIL",
                   f"Trade={trade}, trading={trading} — they must agree", "search")
        record("matches are highlighted", "PASS" if marks else "FAIL",
               f"{marks} highlight(s) on the 'Trade' result", "search")
        record("a nonsense term returns nothing (negative control)",
               "PASS" if nope == 0 else "FAIL",
               f"{nope} rows for 'zzzznope' — must be 0, or the filter is not filtering", "search")
        record("clearing restores the board", "PASS" if rows() == base else "FAIL",
               f"{rows()} vs {base} before", "search")

        # ── per-row controls, on EVERY row ──
        per = pg.evaluate("""()=>{
          const r=[...document.querySelectorAll('#on-items article.bd-card')];
          const has=(x,t)=>[...x.querySelectorAll('button')].some(b=>b.innerText.includes(t));
          return {n:r.length, assist:r.filter(x=>has(x,'AI assist')).length,
                  guide:r.filter(x=>has(x,'guide me here')).length,
                  hist:r.filter(x=>has(x,'history')).length,
                  ids:r.filter(x=>x.querySelector('code')).length,
                  jump:r.filter(x=>has(x,'jump to that agent')).length};
        }""")
        for label, key in [("AI assist on EVERY row", "assist"), ("guide on every row", "guide"),
                           ("history on every row", "hist"), ("stable id shown on every row", "ids")]:
            record(label, "PASS" if per[key] == per["n"] else "FAIL",
                   f"{per[key]} of {per['n']}", "per-row")
        record("jump offered only where it can work", "PASS" if 0 < per["jump"] < per["n"] else "FAIL",
               f"{per['jump']} of {per['n']} rows have a destination", "per-row")

        # ── the per-item assist actually opens, bound to that item ──
        try:
            wanted = pg.evaluate("""()=>{
              const b=[...document.querySelectorAll('#on-items button')].find(x=>x.innerText.includes('AI assist'));
              const id=b.closest('article').getAttribute('data-item-id'); b.click(); return id;}""")
            pg.wait_for_function("()=>document.getElementById('bd-ask-sel')!==null", timeout=30000)
            got = pg.evaluate("()=>document.getElementById('bd-ask-sel').value")
            record("per-item assist opens for THAT item", "PASS" if got == wanted else "FAIL",
                   f"clicked {wanted!r}, panel opened on {got!r}", "per-row")
        except Exception as e:
            record("per-item assist opens for THAT item", "FAIL", f"never opened: {str(e)[:80]}", "per-row")

        # ── category chips filter ──
        try:
            chip = pg.evaluate("""()=>{
              const b=[...document.querySelectorAll('#bd-facets button')].find(x=>/\\d+$/.test(x.innerText.trim()));
              if(!b) return null; const t=b.innerText.trim(); b.click(); return t;}""")
            pg.wait_for_timeout(1500)
            after = rows()
            if chip and 0 < after < base:
                record("category chips filter the board", "PASS",
                       f"chip {chip!r} narrowed {base} -> {after}", "categories")
            else:
                record("category chips filter the board", "FAIL",
                       f"chip={chip!r}, {base} -> {after}", "categories")
            pg.evaluate("()=>bdFacetClear()"); pg.wait_for_timeout(1200)
            record("clearing chips restores the board", "PASS" if rows() == base else "FAIL",
                   f"{rows()} vs {base}", "categories")
        except Exception as e:
            record("category chips filter the board", "UNPROVEN", str(e)[:90], "categories")

        # ── finished record page ──
        try:
            pg.evaluate("()=>bdOpenFinished()")
            pg.wait_for_function(
                "()=>{const e=document.getElementById('bd-fin-body');"
                "return e && !/reading the record/.test(e.innerText);}", timeout=30000)
            txt = pg.evaluate("()=>document.getElementById('bd-fin-body').innerText")
            record("finished record page opens", "PASS", f"{len(txt)} chars rendered", "finished")
            # ★ THIS CHECK WAS A FALSE PASS ONCE THE RECORD FILLED UP. It matched the words
            #   "never"/"nothing" anywhere in the page and reported "it states nothing has been
            #   finished" — on a page listing six finished items. A check whose name stops matching
            #   what it asserts is worse than no check: it reports confidently about the wrong thing.
            #   Assert the branch that actually applies.
            st, sd = http("/operator/settled")
            fin = (sd.get("counts") or {}).get("finished_now", 0)
            ev = len(sd.get("history") or [])
            if fin == 0 and ev == 0:
                record("finished record is honest when nothing is finished",
                       "PASS" if ("never" in txt.lower() or "nothing" in txt.lower()) else "FAIL",
                       "it says so in words rather than showing an empty box", "finished")
            else:
                shows = any(w in txt for w in ("COMPLETED", "FINISHED", "DEFERRED"))
                record("finished record lists the work that is actually done",
                       "PASS" if shows else "FAIL",
                       f"{fin} finished now, {ev} settle event(s) in the journal; the page renders "
                       f"{'them' if shows else 'NOTHING — the record has content and the page hides it'}",
                       "finished")
        except Exception as e:
            record("finished record page opens", "FAIL", str(e)[:90], "finished")

        record("no javascript errors on the page", "PASS" if not errs else "FAIL",
               "; ".join(errs[:2]) if errs else "none", "ui")
        b.close()


def self_test() -> int:
    """INV-114 negative control: the checker must be able to FAIL."""
    global RESULTS
    keep = RESULTS
    RESULTS = []
    record("x", "PASS", "y")
    ok_shape = RESULTS and RESULTS[0]["verdict"] == "PASS"
    RESULTS = []
    global PANEL
    real, PANEL = PANEL, "http://127.0.0.1:1"      # a port nothing listens on
    check_routes()
    caught = [r for r in RESULTS if r["verdict"] in ("FAIL", "UNPROVEN")]
    PANEL = real
    RESULTS = keep
    if not ok_shape:
        print("  CONTROL FAILED: record() does not store verdicts"); return 1
    if not caught:
        print("  CONTROL FAILED: pointed at a dead port, every route still reported OK — "
              "this checker cannot detect a broken panel"); return 1
    print(f"  controls pass: pointed at a dead port it reported {len(caught)} problem(s), not silence")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return self_test()
    t0 = time.time()
    check_routes()
    check_number_resolution()
    if "--routes-only" not in sys.argv:
        check_ui()
    # ★ MACHINE-READABLE, because the panel button was PARSING THE PRETTY OUTPUT and silently lost
    #   three checks: the table pads names to 46 columns, so a name exactly that long left one space
    #   instead of two and the row did not match. The button reported 27 of 30 and looked entirely
    #   healthy -- a verification surface under-reporting its own checks, which is the failure it
    #   exists to catch, one level up. Never parse your own display format.
    if "--json" in sys.argv:
        n = {v: len([r for r in RESULTS if r["verdict"] == v]) for v in ("PASS", "FAIL", "UNPROVEN")}
        print(json.dumps({"checks": RESULTS, "counts": n,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
        return 1 if n["FAIL"] else 0
    groups: dict[str, list] = {}
    for r in RESULTS:
        groups.setdefault(r["group"] or "other", []).append(r)
    mark = {"PASS": "\033[32m✓\033[0m", "FAIL": "\033[31m✗\033[0m", "UNPROVEN": "\033[33m?\033[0m"}
    print("\n  ═══ OPERATOR BOARD — DID THE FEATURES LAND? ═══\n")
    for g, rs in groups.items():
        print(f"  {g.upper()}")
        for r in rs:
            print(f"    {mark[r['verdict']]} {r['name']:<46} {r['detail'][:86]}")
        print()
    n = {v: len([r for r in RESULTS if r["verdict"] == v]) for v in ("PASS", "FAIL", "UNPROVEN")}
    print(f"  {n['PASS']} pass · {n['FAIL']} fail · {n['UNPROVEN']} unproven   ({time.time()-t0:.0f}s)")
    if n["UNPROVEN"]:
        print("  ★ UNPROVEN is not a pass — those checks could not run.")
    if n["FAIL"]:
        print("  ★ A FAIL here is a feature that was claimed and is not working.")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
