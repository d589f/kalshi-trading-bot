"""Market-making analysis on the collected Kalshi sub-second book+trade log.
Reconstructs the per-window book from orderbook_snapshot+delta, replays the
trade tape, and simulates a resting MAKER quote to measure the two things the
1-min candles could never give: (1) maker FILL RATE and (2) ADVERSE SELECTION
(do filled maker orders skew toward the losing side?). Kalshi maker fee ~= 0,
so if adverse selection is small the spread is kept — the fee-dodging lever.

Feed the collector log (download from EU /tmp/kalshi_book_YYYYMMDD.jsonl) and a
{ticker: result} map (from the Kalshi API). Usage:
  python mm_analyze.py <book_log.jsonl> <results.json>
"""
import json, sys, collections, statistics as st
sys.stdout.reconfigure(encoding="utf-8")

LOG, RES = sys.argv[1], sys.argv[2]
results = json.load(open(RES))            # {ticker: "yes"/"no"}


def open_ts(tk):
    return int(tk.rsplit("-", 1)[0].rsplit("-", 1)[1][-4:])  # placeholder, unused


# ---- load & group by market ----
by_mkt = collections.defaultdict(list)
for line in open(LOG, encoding="utf-8"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    m = d.get("msg") or {}
    tk = m.get("market_ticker")
    if tk:
        by_mkt[tk].append(d)
print("markets in log: %d | with result: %d" %
      (len(by_mkt), sum(1 for tk in by_mkt if results.get(tk) in ("yes", "no"))))


def replay(events):
    """Yield (rx, kind, payload) and maintain the YES-side book {price: size}.
    Book is in YES-price space; NO levels map to yes_price = 1 - no_price."""
    yes = {}  # price(float) -> size
    def apply_level(side, price, size, absolute):
        p = round(price if side == "yes" else 1 - price, 4)
        if absolute:
            yes[p] = size
        else:
            yes[p] = yes.get(p, 0.0) + size
        if yes.get(p, 0) <= 0:
            yes.pop(p, None)
    out = []
    for d in sorted(events, key=lambda x: x.get("rx", 0)):
        t = d["type"]; m = d["msg"]
        if t == "orderbook_snapshot":
            yes.clear()
            for pr, sz in m.get("yes_dollars_fp", []):
                apply_level("yes", float(pr), float(sz), True)
            for pr, sz in m.get("no_dollars_fp", []):
                apply_level("no", float(pr), float(sz), True)
        elif t == "orderbook_delta":
            apply_level(m["side"], float(m["price_dollars"]), float(m["delta_fp"]), False)
        elif t == "trade":
            out.append((d.get("rx", 0), "trade", m, dict(yes)))
    return out


def best_bid_ask(book):
    ps = [p for p, s in book.items() if s > 0]
    if not ps:
        return (None, None)
    # book is YES-side sizes; YES bid = highest price with size on the bid... but our
    # reconstructed `yes` mixes both sides' resting orders keyed by yes-price. For a
    # crude top-of-book we take min/max populated prices as ask/bid proxies.
    return (max(ps), min(ps))


# ---- MM simulation: post a YES/NO bid at the mover side at ~minute 3, measure fill ----
def simulate(offset_ticks=0, ttl_s=600):
    posted = filled = 0
    fill_win = fill_lose = unfill_win = unfill_lose = 0
    pnls = []
    for tk, ev in by_mkt.items():
        r = results.get(tk)
        if r not in ("yes", "no"):
            continue
        stream = replay(ev)
        if not stream:
            continue
        # crude: post to BUY the side that is currently the favorite (>0.5) at t0=first trade
        # and see if a taker sells into us before resolution.
        t0 = stream[0][0]
        # find book near t0
        book0 = stream[0][3]
        bid, ask = best_bid_ask(book0)
        if bid is None or ask is None:
            continue
        mid = (bid + ask) / 2
        side = "yes" if mid >= 0.5 else "no"
        my_px = round((mid) - 0.01 * (1 + offset_ticks), 2)  # post 1c+ under mid on the mover side
        my_px = min(max(my_px, 0.02), 0.98)
        # fill if a later trade prints at/below my_px on my side with taker selling (book_side bid)
        got = False; fill_price = None
        for rx, kind, m, book in stream:
            if rx - t0 > ttl_s:
                break
            # trade yes-price
            yp = float(m["yes_price_dollars"])
            price_myside = yp if side == "yes" else round(1 - yp, 4)
            if m.get("taker_book_side") == "bid" and price_myside <= my_px + 1e-9:
                got = True; fill_price = my_px; break
        posted += 1
        won = (side == "yes" and r == "yes") or (side == "no" and r == "no")
        if got:
            filled += 1
            (fill_win if won else fill_lose)
            if won: fill_win += 1
            else: fill_lose += 1
            sh = 5.0 / fill_price
            pnls.append(sh * (1 - fill_price) if won else -5.0)   # maker fee ~0
        else:
            if won: unfill_win += 1
            else: unfill_lose += 1
    print("\n=== MM sim (post mover-side bid ~1c under mid, TTL %ds) ===" % ttl_s)
    print("posted %d | filled %d (%.0f%%)" % (posted, filled, 100*filled/max(posted, 1)))
    if filled:
        print("  filled WR   %.1f%% (%d/%d)" % (100*fill_win/filled, fill_win, filled))
    if (unfill_win+unfill_lose):
        print("  UNFILLED WR %.1f%% (%d/%d)  <- if >> filled WR, adverse selection" %
              (100*unfill_win/(unfill_win+unfill_lose), unfill_win, unfill_win+unfill_lose))
    if pnls:
        print("  maker PnL $%.2f over %d fills (EV $%.3f/fill, zero fee)" %
              (sum(pnls), len(pnls), sum(pnls)/len(pnls)))


if __name__ == "__main__":
    simulate()
