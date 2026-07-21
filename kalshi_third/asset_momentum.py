#!/usr/bin/env python3
"""Momentum sweep on ANY Kalshi up/down 15m series with the honest bt_core
economics (real ask entry, $5 integer contracts, Kalshi taker fee both ways,
train/test split). Asset-agnostic: threshold is expressed as a RETURN
(|price(open+em) - open| / open) so BTC and ETH are directly comparable.

  python asset_momentum.py <markets_csv> <entry_csv> <BINANCE_SYMBOL> [label]
e.g. python asset_momentum.py eth15m_markets.csv eth15m_entry.csv ETHUSDT ETH15M
"""
import csv, math, os, sys, datetime as dt
import statistics as st
import px
sys.stdout.reconfigure(encoding="utf-8")

MK_CSV = sys.argv[1] if len(sys.argv) > 1 else "eth15m_markets.csv"
EN_CSV = sys.argv[2] if len(sys.argv) > 2 else "eth15m_entry.csv"
SYMBOL = sys.argv[3] if len(sys.argv) > 3 else "ETHUSDT"
LABEL = sys.argv[4] if len(sys.argv) > 4 else SYMBOL
D = os.path.dirname(os.path.abspath(__file__))
TRAIN_END = dt.datetime(2026, 6, 30, tzinfo=dt.timezone.utc)

def tparse(iso): return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))

MK = {r["ticker"]: r for r in csv.DictReader(open(os.path.join(D, MK_CSV), encoding="utf-8"))}
ENTRY = {}
for r in csv.DictReader(open(os.path.join(D, EN_CSV), encoding="utf-8")):
    def _f(x): return float(x) if x not in ("", "None", None) else None
    ENTRY[(r["ticker"], int(r["minute"]))] = (_f(r["price"]), _f(r["yes_ask"]), _f(r["yes_bid"]))

_times = [tparse(m["open_time"]) for m in MK.values() if m["result"] in ("yes", "no")]
P = px.btc_path(int(min(_times).timestamp()*1000), int(max(_times).timestamp()*1000)+16*60000, symbol=SYMBOL)

def price_at(t):
    mi = int(t.timestamp()) // 60
    for d in (0, -1, 1, -2, 2):
        if mi + d in P: return P[mi + d]
    return None

def entry_px(tk, ci, side):
    e = ENTRY.get((tk, ci))
    if not e: return None
    _, ya, yb = e
    return ya if side == "UP" else ((1 - yb) if yb is not None else None)

def kfee(n, e): return math.ceil(7 * n * e * (1 - e)) / 100.0
def pnl5(e, won):
    if e is None or e <= 0.02 or e >= 0.99: return None
    n = min(15, round(5.0 / e)) or 1
    fee = kfee(n, e)
    return (n * (1 - e) - fee) if won else -(n * e + fee)

# build windows: per market, open price + delta(return) at each minute + entries
wins = []
for tk, m in MK.items():
    if m["result"] not in ("yes", "no"): continue
    t = tparse(m["open_time"]); o = price_at(t)
    if o is None or o <= 0: continue
    rets = {}
    for em in range(1, 15):
        pe = price_at(t + dt.timedelta(minutes=em))
        if pe is not None: rets[em] = (pe - o) / o
    if not rets: continue
    wins.append((t, tk, m["result"] == "yes", rets))
wins.sort()
print("%s windows: %d | span %s..%s" % (LABEL, len(wins),
      wins[0][0].strftime("%m-%d"), wins[-1][0].strftime("%m-%d")))

def run(ci, thr_ret):
    em = ci + 1
    rows = []
    for t, tk, won_up, rets in wins:
        r = rets.get(em)
        if r is None or abs(r) < thr_ret: continue
        side = "UP" if r > 0 else "DOWN"
        e = entry_px(tk, ci, side)
        if e is None or e <= 0.02 or e >= 0.99: continue
        won = won_up if side == "UP" else (not won_up)
        p = pnl5(e, won)
        if p is not None: rows.append((t, e, won, p))
    return rows

def stats(rows):
    if len(rows) < 30: return None
    tot = sum(r[3] for r in rows)
    days = max((rows[-1][0] - rows[0][0]).total_seconds() / 86400, 1)
    return dict(n=len(rows), wr=100*sum(1 for r in rows if r[2])/len(rows),
                ae=st.mean(r[1] for r in rows), ev=tot/len(rows), tot=tot, pd=tot/days)

print("\nmomentum sweep (honest $5 + Kalshi fee). thr = |return| at entry minute")
print("%-6s %-7s %5s %6s %8s %8s %9s" % ("entry", "thr%", "n", "WR%", "avg_ask", "EV/tr", "$/day"))
for thr in (0.0005, 0.0008, 0.0012, 0.0018, 0.0025):   # 0.05%..0.25% return
    for ci in (2, 3, 4, 5):
        rows = run(ci, thr)
        # test-period only (out of sample)
        te = [r for r in rows if r[0] >= TRAIN_END]
        s = stats(te)
        if not s: continue
        print("min%-3d %-7.3f %5d %6.1f %8.3f %+8.3f %+9.2f"
              % (ci+1, thr*100, s["n"], s["wr"], s["ae"], s["ev"], s["pd"]))
    print()
