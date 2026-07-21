"""Polymarket btc-updown-15m ORDERBOOK analysis core (Predexon snapshots, cached).

The accurate executable book the trade-tape round-2 lacked: per-window best
bid/ask time series at sub-second resolution + top-of-book depth for an
imbalance signal. Cache keys mirror px.orderbooks exactly (ob:{token}:{st}:{en}).

A taker BUY of the UP side hits the UP ask; a BUY of the DOWN side hits the DOWN
ask (real dn book if cached, else the 1-UP-bid complement). Zero Polymarket fee.
"""
import datetime as dt
import hashlib, json, os
import px

D = os.path.dirname(os.path.abspath(__file__))


def _ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _ob_cache(token, st, en):
    h = hashlib.md5(("ob:%s:%s:%s" % (token, st, en)).encode()).hexdigest()[:16]
    p = os.path.join(px.CACHE, "ob_%s.json" % h)
    if os.path.exists(p) and os.path.getsize(p) > 2:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def _best(levels, side):
    """side 'ask' -> lowest price with size; 'bid' -> highest price with size.
    Returns (price, size_at_that_price, total_size)."""
    xs = [(x["price"], x["size"]) for x in (levels or []) if x.get("size", 0) > 0]
    if not xs:
        return (None, 0.0, 0.0)
    tot = sum(s for _, s in xs)
    p = min(xs)[0] if side == "ask" else max(xs)[0]
    sz = sum(s for pr, s in xs if pr == p)
    return (p, sz, tot)


class OW:
    """One window's UP-token book as a time series."""
    __slots__ = ("slug", "t", "won_up", "snaps", "dn_snaps")
    def __init__(self, slug, t, won_up):
        self.slug, self.t, self.won_up = slug, t, won_up
        self.snaps = []      # (sec, bid, ask, bid_tot, ask_tot) for UP token
        self.dn_snaps = []   # same for DOWN token (may be empty)


def _series(ob, open_ts):
    out = []
    for s in ob or []:
        sec = int(s["timestamp"]) // 1000 - open_ts   # ob ts always ms
        if not (-30 <= sec <= 915):
            continue
        bp, _, bt = _best(s.get("bids"), "bid")
        ap, _, at = _best(s.get("asks"), "ask")
        out.append((sec, bp, ap, bt, at))
    out.sort(key=lambda x: x[0])
    return out


def load_ob_windows(limit=None):
    mm = json.load(open(os.path.join(px.CACHE, "poly15m_markets.json"), encoding="utf-8"))
    mm = [m for m in mm if m.get("winning_side") in ("Up", "Down")]
    out = []
    for m in mm:
        if not m.get("start_time") or not m.get("end_time"):
            continue
        try:
            open_ts = int(m["market_slug"].rsplit("-", 1)[1])
        except (ValueError, IndexError):
            continue
        st, en = _ms(m["start_time"]), _ms(m["end_time"])
        up = _ob_cache(m["up_token_id"], st, en)
        if up is None:
            continue
        w = OW(m["market_slug"], dt.datetime.fromtimestamp(open_ts, dt.timezone.utc),
               m["winning_side"] == "Up")
        w.snaps = _series(up, open_ts)
        if not w.snaps:
            continue
        dn = _ob_cache(m["down_token_id"], st, en)
        if dn:
            w.dn_snaps = _series(dn, open_ts)
        out.append(w)
        if limit and len(out) >= limit:
            break
    out.sort(key=lambda w: w.t)
    return out


def snap_at(snaps, sec, max_stale=8):
    """Last snapshot at/before sec within max_stale seconds -> (bid, ask, bid_tot, ask_tot)."""
    best = None
    for s in snaps:
        if s[0] <= sec:
            if sec - s[0] <= max_stale:
                best = s
        else:
            break
    return (best[1], best[2], best[3], best[4]) if best else (None, None, None, None)


def ask_side(w, side, sec, max_stale=8):
    """Real executable ask for the given side at `sec`.
    UP -> UP ask; DOWN -> DOWN ask (real dn book, else 1 - UP bid complement)."""
    if side == "UP":
        _, a, _, _ = snap_at(w.snaps, sec, max_stale)
        return a
    # DOWN
    if w.dn_snaps:
        _, a, _, _ = snap_at(w.dn_snaps, sec, max_stale)
        return a
    b, _, _, _ = snap_at(w.snaps, sec, max_stale)   # complement: DOWN ask = 1 - UP bid
    return (round(1 - b, 4) if b is not None else None)


def imbalance_at(w, sec, max_stale=8):
    """Top-of-book UP imbalance (bid_tot - ask_tot)/(bid_tot + ask_tot) in [-1,1].
    >0 = more UP-buy pressure. Uses full stored side totals."""
    _, _, bt, at = snap_at(w.snaps, sec, max_stale)
    if bt is None or (bt + at) <= 0:
        return None
    return (bt - at) / (bt + at)


def pnl_poly(entry, won, stake=5.0):
    """Polymarket $ PnL. CORRECTION 2026-07-15: Poly is NOT zero-fee — it charges
    a taker fee measured at 0.07·shares·p·(1-p) = 0.07·stake·(1-p), the SAME
    schedule as Kalshi (empirically identical across 221k real trades). Charged
    on entry regardless of outcome."""
    if entry is None or entry <= 0.02 or entry >= 0.99:
        return None
    shares = stake / entry
    fee = 0.07 * shares * entry * (1 - entry)
    return (shares * (1 - entry) - fee) if won else (-stake - fee)


if __name__ == "__main__":
    ws = load_ob_windows()
    print("windows with cached UP orderbook:", len(ws))
    if ws:
        print("span %s .. %s" % (ws[0].t.strftime("%m-%d %H:%M"), ws[-1].t.strftime("%m-%d %H:%M")))
        both = sum(1 for w in ws if w.dn_snaps)
        print("with DOWN book too:", both)
        cov = [w.snaps[-1][0] for w in ws if w.snaps]
        cov.sort()
        print("in-window coverage last-sec: median %ds, min %ds" % (cov[len(cov)//2], min(cov)))
        n = [len(w.snaps) for w in ws]
        print("snaps/window: median %d" % sorted(n)[len(n)//2])
