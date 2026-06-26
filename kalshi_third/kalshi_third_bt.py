"""Backtest the THIRD P-model on Kalshi KXBTC15M history (real Kalshi entry prices + settlement).
Signal (third): at entry_min into the 15-min window, Δ = BTC(entry)-BTC(open); if |Δ|>=20 bet the
direction (continuation). Enter at the REAL Kalshi price (yes_ask for UP, no_ask=1-yes_bid for DOWN),
apply MAX_ENTRY, resolve at Kalshi result. BTC signal from Binance (data.binance.vision)."""
import csv, math, datetime as dt, statistics as st, os
import px

D = os.path.dirname(os.path.abspath(__file__))
def tparse(iso): return dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
def Phi(z): return 0.5 * (1 + math.erf(z / math.sqrt(2)))

MK = {r["ticker"]: r for r in csv.DictReader(open(f"{D}/kalshi_markets.csv", encoding="utf-8"))}
ENTRY = {}
if os.path.exists(f"{D}/kalshi_entry.csv"):
    for r in csv.DictReader(open(f"{D}/kalshi_entry.csv", encoding="utf-8")):
        def fv(x): return float(x) if x not in ("", "None", None) else None
        ENTRY[(r["ticker"], int(r["minute"]))] = (fv(r["price"]), fv(r["yes_ask"]), fv(r["yes_bid"]))

times = [tparse(m["open_time"]) for m in MK.values()]
st_ms = int(min(times).timestamp() * 1000); en_ms = int(max(times).timestamp() * 1000) + 16 * 60000
print(f"markets {len(MK)} | span {min(times):%Y-%m-%d} .. {max(times):%Y-%m-%d} | loading Binance...")
P = px.btc_path(st_ms, en_ms)
print(f"BTC 1m points: {len(P)}")

def btc_at(t):
    mi = int(t.timestamp()) // 60
    for d in (0, -1, 1, -2, 2, -3, 3):
        if mi + d in P: return P[mi + d]
    return None
def sigma30(t):
    mi = int(t.timestamp()) // 60
    rr = [math.log(P[k + 1] / P[k]) for k in range(mi - 30, mi) if k in P and (k + 1) in P and P[k] > 0]
    return st.pstdev(rr) if len(rr) > 10 else 0.0005

def run(entry_ci=3, max_entry=0.82, min_entry=0.50, delta_thr=20.0, pgate=False, kappa=0.5):
    """entry_ci = candle index (~minute) for entry. Returns list of trade dicts."""
    tr = []
    em = entry_ci + 1  # candle ci covers minute ci..ci+1; entry price ~ minute ci+1
    for tk, m in MK.items():
        o = tparse(m["open_time"])
        bo = btc_at(o); be = btc_at(o + dt.timedelta(minutes=em))
        if bo is None or be is None: continue
        delta = be - bo
        if abs(delta) < delta_thr: continue
        side = "UP" if delta > 0 else "DOWN"
        if pgate:
            sig = sigma30(o + dt.timedelta(minutes=em)); tau = (15 - em) / 5.0
            z = kappa * abs(delta) / (sig * be * max(tau, 0.1))
            if Phi(z) < 0.60: continue
        e = ENTRY.get((tk, entry_ci))
        if not e: continue
        price, ya, yb = e
        entry_px = ya if side == "UP" else ((1 - yb) if yb is not None else None)
        if entry_px is None or entry_px < min_entry or entry_px > max_entry: continue
        won = (side == "UP" and m["result"] == "yes") or (side == "DOWN" and m["result"] == "no")
        stake = 100.0; pnl = (stake / entry_px - stake) if won else -stake
        tr.append(dict(t=o, side=side, entry=entry_px, delta=delta, pnl=pnl, won=won))
    return tr

def report(tr, label):
    n = len(tr)
    if not n: print(f"{label}: no trades"); return
    tot = sum(x["pnl"] for x in tr); wr = 100 * sum(x["won"] for x in tr) / n
    days = (tr[-1]["t"] - tr[0]["t"]).total_seconds() / 86400 if n > 1 else 1
    print(f"\n{label}: EV ${tot/n:+.3f}/tr | WR {wr:.1f}% | n={n} | total ${tot:+,.0f} | {days:.0f}d | {n/days:.0f} tr/day")
    import collections
    mon = collections.defaultdict(lambda: [0, 0.0])
    for x in tr: k = x["t"].strftime("%Y-%m"); mon[k][0] += 1; mon[k][1] += x["pnl"]
    for k in sorted(mon): print(f"    {k}: n={mon[k][0]:4d} EV ${mon[k][1]/mon[k][0]:+.3f} total ${mon[k][1]:+,.0f}")

if __name__ == "__main__":
    print(f"entry prices loaded: {len(ENTRY)} (markets with entry data: {len(set(t for t,_ in ENTRY))})")
    for me in (0.82, 0.92):
        report(run(entry_ci=3, max_entry=me, pgate=False), f"third on Kalshi (entry~4min, MAX_ENTRY {me}, no p-gate)")
    report(run(entry_ci=3, max_entry=0.82, pgate=True), "third on Kalshi (entry~4min, MAX_ENTRY 0.82, +p-gate)")
