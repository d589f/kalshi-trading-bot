"""Polymarket btc-updown-15m analysis core (Predexon tape, disk-cached by px.py).

Loads whatever markets have cached trade tapes and builds per-window structures:
UP-token prints timeline + best-effort top-of-book from prints. Zero fees on
Polymarket; PnL at $5 = shares*(1-p) win / -shares*p loss, shares = 5/p.

Methodology note: signal params frozen from the 82-day Kalshi sweep are PURE
out-of-sample here (different venue, later selection) — no in-Poly re-selection.
"""
import datetime as dt
import json, math, os
import px

D = os.path.dirname(os.path.abspath(__file__))

def _ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

def _cached_tape(token, st, en):
    import hashlib
    h = hashlib.md5(f"tr:{token}:{st}:{en}".encode()).hexdigest()[:16]
    p = os.path.join(px.CACHE, f"tr_{h}.json")
    if os.path.exists(p) and os.path.getsize(p) > 2:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:   # in-flight partial write by the background fetcher
            return None
    return None

class PW:
    """One 15-min Polymarket window."""
    __slots__ = ("slug", "t", "hour", "won_up", "up_trades", "dn_trades", "vol")
    def __init__(self, slug, t, won_up, vol):
        self.slug, self.t, self.won_up, self.vol = slug, t, won_up, vol
        self.hour = t.hour
        self.up_trades = []   # (sec_into_window, price, size_usd, side)  side=BUY/SELL taker
        self.dn_trades = []

def load_windows(require_dn=False):
    mm = json.load(open(os.path.join(px.CACHE, "poly15m_markets.json"), encoding="utf-8"))
    out = []
    for m in mm:
        if m.get("winning_side") not in ("Up", "Down"): continue
        if not m.get("start_time") or not m.get("end_time"): continue
        # metadata start_time = market CREATION (hours before the window);
        # the actual 15-min window open is the unix ts in the slug. Trading
        # happens pre-open too (prints at ~0.50) — offsets below are relative
        # to the WINDOW OPEN, so pre-open prints get negative sec.
        try:
            open_ts = int(m["market_slug"].rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        st, en = _ms(m["start_time"]), _ms(m["end_time"])   # cache keys use fetch range
        up = _cached_tape(m["up_token_id"], st, en)
        if up is None: continue
        dn = _cached_tape(m["down_token_id"], st, en)
        if require_dn and dn is None: continue
        t = dt.datetime.fromtimestamp(open_ts, dt.timezone.utc)
        w = PW(m["market_slug"], t, m["winning_side"] == "Up", float(m.get("total_volume_usd") or 0))
        for arr, dest in ((up, w.up_trades), (dn or [], w.dn_trades)):
            for tr in arr:
                try:
                    ts = int(tr["timestamp"]); pr = float(tr["price"])
                    if ts > 2_000_000_000: ts //= 1000
                    dest.append((ts - open_ts, pr, float(tr.get("amount_usd") or 0), tr.get("side", "")))
                except Exception:
                    continue
        w.up_trades.sort(); w.dn_trades.sort()
        out.append(w)
    out.sort(key=lambda w: w.t)
    return out

def up_price_at(w, sec, lookback=90):
    """Last UP print at/before sec (within lookback); None if no print."""
    best = None
    for ts, pr, _, _ in w.up_trades:
        if ts <= sec:
            if sec - ts <= lookback: best = pr
        else: break
    return best

def buy_cost(w, side, sec, budget=5.0, horizon=45):
    """Honest taker entry: VWAP of the NEXT prints on that side's token after `sec`
    (we can only get what actually traded). Uses BUY-taker prints (someone lifted
    the ask) within `horizon` sec. Returns (vwap, usd_filled)."""
    tape = w.up_trades if side == "UP" else w.dn_trades
    got = 0.0; cost = 0.0
    for ts, pr, usd, sd in tape:
        if ts < sec or sd != "BUY": continue
        if ts > sec + horizon: break
        take = min(usd, budget - got)
        if take <= 0: break
        got += take; cost += take * pr
    if got < budget * 0.6: return None, got
    return cost / got, got

def pnl_poly(entry, won, stake=5.0):
    """Zero-fee Polymarket PnL at $ stake."""
    if entry is None or entry <= 0.02 or entry >= 0.99: return None
    sh = stake / entry
    return sh * (1 - entry) if won else -stake

if __name__ == "__main__":
    ws = load_windows()
    print(f"windows with cached UP tape: {len(ws)}")
    if ws:
        print(f"span {ws[0].t:%m-%d %H:%M} .. {ws[-1].t:%m-%d %H:%M}")
        both = sum(1 for w in ws if w.dn_trades)
        print(f"with DOWN tape too: {both}")
        tp = [len(w.up_trades) for w in ws]
        print(f"up prints/window: median {sorted(tp)[len(tp)//2]}, min {min(tp)}, max {max(tp)}")
