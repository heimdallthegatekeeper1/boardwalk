#!/usr/bin/env python3
"""TRADING OPERATOR NEEDS — a VIEW over the one board, never a second board.

★★★ WHY A VIEW AND NOT A BOARD. The operator asked for "your specific Trading Operator Needs
  board". The tempting build is a trading-local store. That is precisely the two-stores failure
  this board was created to end: an item outstanding in one store and invisible in another, with
  nothing able to tell you which is stale. So this READS blockers.json and writes nothing. There
  is one store; this is a lens on it.

★ It also prints what it is EXCLUDING, by count. A filtered view that silently hides the rest
  teaches its reader that the filtered set is the whole set — which is how a board stops being a
  board.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.path.join(HERE, "blockers.json")
SEV = {"stop": ("BLOCKING", "\033[91m"), "hold": ("WAITING ON YOU", "\033[93m"),
       "info": ("YOUR CALL", "\033[96m")}
B, D, R = "\033[1m", "\033[2m", "\033[0m"


# ★★★ THE COMPLETE BOARD IS THE LOOPBACK ENDPOINT, NOT THE FILE. Corrected 2026-08-29.
#   blockers.json holds only the HAND-WRITTEN items (21 at the time of writing). The panel's
#   /operator/board.json returns those PLUS the DERIVED ones — requirements, findings, invariants,
#   approval requests — 67 in total. This tool originally read the file, so it reported "14 open in
#   other lanes" when the true figure was 60.
#   ★★ That is the exact defect this tool was built to prevent, committed by the tool itself: a
#   filtered view teaching its reader that the filtered set is the whole set. It printed an
#   excluded-count that was itself wrong by a factor of four.
#   So: prefer the endpoint, fall back to the file, and ALWAYS SAY WHICH ONE WAS USED — a count
#   whose provenance is unstated is how two surfaces disagree without anyone noticing.
def load_board():
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:7842/operator/board.json", timeout=6) as r:
            items = json.load(r).get("items", [])
        # ★★★ MEASURE THE CLAIM, DO NOT ASSERT IT. Flagged by the KHAOSS seat about their own
        #   provenance line, and it cut here too: "COMPLETE" was static text. It was true the day
        #   it was written and could go false silently the next time a source is added to one
        #   surface and not another — which is precisely the 21-vs-67 defect that started this,
        #   re-created inside the fix for it. So the agreement is CHECKED at print time and the
        #   result is reported either way. A claim of completeness that cannot fail is not a claim.
        n_live = len([i for i in items if i.get("state") == "open"])
        try:
            store = json.load(open(STORE))["items"]
            n_hand = len([i for i in store if i.get("state") == "open"])
            note = (f"loopback board.json — {n_live} open, of which {n_hand} hand-written and "
                    f"{n_live - n_hand} derived (counted now, not assumed)")
            if n_live < n_hand:
                note = (f"★★ DISAGREEMENT: endpoint reports {n_live} open but the store holds "
                        f"{n_hand}. The endpoint is MISSING items the file has. Do not trust "
                        f"either count until this is explained.")
                globals()["_DISAGREE"] = True
        except Exception as e:
            note = (f"loopback board.json — {n_live} open. ★ Could not read {STORE} to verify the "
                    f"hand-written subset ({type(e).__name__}), so completeness is UNVERIFIED here.")
        return items, note
    except Exception as e:
        d = json.load(open(STORE))
        return d["items"], (f"blockers.json FALLBACK — the panel was unreachable ({type(e).__name__}). "
                            "★ This is the HAND-WRITTEN SUBSET; derived items are NOT shown.")


def main() -> None:
    lane = sys.argv[1] if len(sys.argv) > 1 else "trading"
    all_items, source = load_board()
    items = [i for i in all_items if i.get("state") == "open"]
    mine = [i for i in items if i.get("lane") == lane]
    other = len(items) - len(mine)
    order = ["stop", "hold", "info"]
    mine.sort(key=lambda x: order.index(x.get("sev", "hold")))

    print("\n" + "━" * 78)
    print(f"  {B}{lane.upper()} — OPERATOR NEEDS{R}   "
          f"{len(mine)} open in this lane   {D}({other} open in other lanes, not shown){R}")
    print(f"  {D}source: {source}{R}")
    print("━" * 78)
    if not mine:
        # ★ the liveness rule again: an empty view must not read as "all clear".
        print(f"  ★ No OPEN items tagged lane={lane!r}. That is not the same as 'nothing "
              f"outstanding'\n    — {other} item(s) are open in other lanes. Run blockers.py for all.")
        return _exit_code(0)
    for i in mine:
        label, col = SEV.get(i.get("sev", "hold"), ("WAITING ON YOU", ""))
        print(f"\n  {col}[{label}]{R} {B}{i['title']}{R}")
        one = i.get("one") or "⚠ no action recorded — this item cannot be acted on"
        print(f"      → {one}")
        if i.get("blocks"):
            print(f"      {D}blocks: {i['blocks']}{R}")
    print(f"\n{'━'*78}")
    print(f"  Nothing here can be unblocked by an agent. Mark done ONLY yourself:")
    print(f"    python3 {os.path.join(HERE,'blockers.py')} --set <id> done|later")
    print(f"  Full board (all lanes): python3 {os.path.join(HERE,'blockers.py')}")
    print("━" * 78 + "\n")
    return _exit_code(len(mine))


# ★★★ AN EXIT CONTRACT, BECAUSE I SHIPPED THIS WITH NONE. Added 2026-08-29 after the KHAOSS seat
#   ranked unproven detectors by WHAT READS THEM and this came second. It had NO sys.exit anywhere:
#   it printed 7 open items in the trading lane and RETURNED 0. A caller branching on it got
#   "clear" while the lane had seven outstanding.
#   ★★ Theirs was the same class and LATENT — every hand row was open, so both the wrong and the
#   right exit code returned 1. MINE RETURNED THE WRONG ONE TODAY, on every run.
#   ★ And my own AGENTS.md v2 block names this tool as a way for trading leads to check the board.
#   I documented a false-clear-by-omission in the block I wrote to fix someone else's block.
#   0 = lane genuinely clear · 1 = something open · 2 = counts disagree, trust neither
def _exit_code(open_in_lane: int) -> int:
    if globals().get("_DISAGREE"):
        return 2
    return 1 if open_in_lane else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
