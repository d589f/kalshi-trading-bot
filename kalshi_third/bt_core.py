"""Shared honest-backtest core for the strategy-search iteration (Jul 2026).

Loads the merged Kalshi KXBTC15M history (markets + per-minute entry prices),
the BTC 1-min path, and precomputes per-window features. Every strategy family
evaluates through the SAME economics: real Kalshi ask entry, $5 integer-contract
sizing, Kalshi taker fee on win AND loss, time-ordered train/test split.

Data files (this directory):
  kalshi_markets.csv      ticker,open_time,close_time,floor_strike,result
  kalshi_entry.csv        ticker,minute(candle ci),price,yes_ask,yes_bid
                          (candle ci covers minute ci..ci+1 -> its close ~ price at minute ci+1)
"""
import csv, math, os, datetime as dt
import statistics as st
import px

D = os.path.dirname(os.path.abspath(__file__))
TRAIN_END = dt.datetime(2026, 6, 24, tzinfo=dt.timezone.utc)   # ~70/30 by span Apr17..Jul8

def tparse(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))

def Phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))

# ---------- raw data ----------
MK = {r["ticker"]: r for r in csv.DictReader(open(f"{D}/kalshi_markets.csv", encoding="utf-8"))}
ENTRY = {}
for r in csv.DictReader(open(f"{D}/kalshi_entry.csv", encoding="utf-8")):
    def _f(x): return float(x) if x not in ("", "None", None) else None
    ENTRY[(r["ticker"], int(r["minute"]))] = (_f(r["price"]), _f(r["yes_ask"]), _f(r["yes_bid"]))

_times = [tparse(m["open_time"]) for m in MK.values()]
_st_ms = int(min(_times).timestamp() * 1000)
_en_ms = int(max(_times).timestamp() * 1000) + 16 * 60000
P = px.btc_path(_st_ms, _en_ms)          # {minute_index: close}

def btc_at(t):
    mi = int(t.timestamp()) // 60
    for d in (0, -1, 1, -2, 2, -3, 3):
        if mi + d in P: return P[mi + d]
    return None

def sigma_n(t, n=30):
    """Stdev of 1m log returns over the n minutes ENDING at t (P[i] = price AT
    minute i after the px.py lookahead fix, so every return here is <= t).
    The old `> max(10, n//3)` bar made n=10 structurally impossible (always None)."""
    mi = int(t.timestamp()) // 60
    rr = [math.log(P[k + 1] / P[k]) for k in range(mi - n, mi) if k in P and (k + 1) in P and P[k] > 0]
    return st.pstdev(rr) if len(rr) >= max(5, (2 * n) // 3) else None

# ---------- per-window feature table ----------
class W:
    __slots__ = ("tk", "t", "hour", "dow", "result", "deltas", "entries", "won_up")
    def __init__(self, tk, t, result):
        self.tk, self.t, self.result = tk, t, result
        self.hour, self.dow = t.hour, t.weekday()
        self.deltas = {}    # minute m (1..14) -> BTC(open+m) - BTC(open)
        self.entries = {}   # candle ci -> (price, yes_ask, yes_bid)
        self.won_up = (result == "yes")

def windows():
    """All windows with BTC path + result, time-ordered."""
    out = []
    for tk, m in MK.items():
        if m["result"] not in ("yes", "no"): continue
        t = tparse(m["open_time"])
        bo = btc_at(t)
        if bo is None: continue
        w = W(tk, t, m["result"])
        ok = False
        for mm in range(1, 15):
            be = btc_at(t + dt.timedelta(minutes=mm))
            if be is not None:
                w.deltas[mm] = be - bo; ok = True
        if not ok: continue
        for ci in range(0, 15):
            e = ENTRY.get((tk, ci))
            if e: w.entries[ci] = e
        out.append(w)
    out.sort(key=lambda w: w.t)
    return out

def entry_px(w, ci, side):
    """Real entry price for side at candle ci: ask for UP, 1-bid for DOWN."""
    e = w.entries.get(ci)
    if not e: return None
    _, ya, yb = e
    if side == "UP": return ya
    return (1 - yb) if yb is not None else None

# ---------- honest economics ----------
def kfee(n_contracts, e):
    return math.ceil(7 * n_contracts * e * (1 - e)) / 100.0

def pnl5(e, won):
    """$5 stake, integer contracts (cap 15), Kalshi taker fee both ways."""
    if e is None or e <= 0.02 or e >= 0.99: return None
    n = min(15, round(5.0 / e)) or 1
    fee = kfee(n, e)
    return (n * (1 - e) - fee) if won else -(n * e + fee)

def evaluate(trades, label=""):
    """trades: list of (t, entry, won). Returns dict with train/test stats."""
    def stats(rows):
        if not rows: return None
        pn = [pnl5(e, w) for _, e, w in rows]
        pn = [p for p in pn if p is not None]
        if not pn: return None
        days = max((rows[-1][0] - rows[0][0]).total_seconds() / 86400, 1)
        return dict(n=len(pn), total=round(sum(pn), 2), ev=round(sum(pn) / len(pn), 4),
                    wr=round(100 * sum(1 for _, _, w in rows if w) / len(rows), 1),
                    perday=round(sum(pn) / days, 2), days=round(days, 1))
    tr = [x for x in trades if x[0] < TRAIN_END]
    te = [x for x in trades if x[0] >= TRAIN_END]
    return dict(label=label, all=stats(trades), train=stats(tr), test=stats(te))

if __name__ == "__main__":
    ws = windows()
    print(f"windows: {len(ws)} | span {ws[0].t:%Y-%m-%d} .. {ws[-1].t:%Y-%m-%d}")
    print(f"with entry data: {sum(1 for w in ws if w.entries)}")
    cis = {}
    for w in ws:
        for ci in w.entries: cis[ci] = cis.get(ci, 0) + 1
    print("entry coverage by candle:", dict(sorted(cis.items())))
