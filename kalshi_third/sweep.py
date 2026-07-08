"""Comprehensive parameter sweep on the Kalshi third backtest harness, WITH the real Kalshi
taker fee and a train/test split, to find the highest-edge config and flag overfitting."""
import math, datetime as dt
import kalshi_third_bt as bt   # importing loads markets + entry prices + BTC path (once)

def kfee(stake, e):
    return math.ceil(0.07 * stake * (1 - e) * 100) / 100  # Kalshi taker fee, ceil to cent

def evalset(trades, stake=100.0):
    if not trades:
        return None
    gross = 0.0; fee = 0.0; wins = 0
    for tr in trades:
        e = tr["entry"]; won = tr["won"]
        g = (stake * (1 - e) / e) if won else -stake
        gross += g; fee += kfee(stake, e); wins += 1 if won else 0
    n = len(trades); net = gross - fee
    return dict(n=n, wr=100 * wins / n, gross_pct=gross / n / stake * 100,
                net_pct=net / n / stake * 100, net_total5=net * 0.05)

def split(trades):
    trades = sorted(trades, key=lambda x: x["t"])
    if not trades: return [], []
    days = [(t["t"] - trades[0]["t"]).total_seconds() / 86400 for t in trades]
    span = days[-1] or 1
    cut = span * 0.70
    tr = [t for t, d in zip(trades, days) if d <= cut]
    te = [t for t, d in zip(trades, days) if d > cut]
    return tr, te

GRID = []
for ci in (3, 4, 5):                       # entry candle -> entry minute em=ci+1 (4,5,6 = 240/300/360s)
    for dthr in (10, 15, 20, 25, 30, 40):
        for me in (0.75, 0.80, 0.85, 0.90):
            for pg in (False, True):
                GRID.append((ci, dthr, me, pg))

rows = []
for ci, dthr, me, pg in GRID:
    trades = bt.run(entry_ci=ci, max_entry=me, delta_thr=dthr, pgate=pg)
    tr, te = split(trades)
    A = evalset(trades); TR = evalset(tr); TE = evalset(te)
    if not A or A["n"] < 60:   # need a minimum sample
        continue
    rows.append(dict(ci=ci, em=ci+1, dthr=dthr, me=me, pg=pg, A=A, TR=TR, TE=TE))

def line(r):
    A, TE = r["A"], r["TE"]
    te_net = TE["net_pct"] if TE else float("nan")
    te_n = TE["n"] if TE else 0
    return (f"em={r['em']} Δ>={r['dthr']:>2} maxE={r['me']:.2f} p={'Y' if r['pg'] else 'N'} | "
            f"ALL n={A['n']:>4} WR={A['wr']:.1f}% netEV={A['net_pct']:+.2f}% | "
            f"TEST n={te_n:>4} netEV={te_net:+.2f}% | net@$5={A['net_total5']:+.0f}")

print(f"=== swept {len(rows)} configs (min 60 trades). Kalshi fee included. ===\n")
print(">>> CURRENT-ish live config (em=5≈270s, Δ20, maxE 0.92→use 0.90, p-gate on):")
for r in rows:
    if r["em"] in (5,) and r["dthr"]==20 and r["me"]==0.90 and r["pg"]:
        print("   ", line(r))
print("\n>>> TOP 15 by ALL-period net EV% (after Kalshi fee):")
for r in sorted(rows, key=lambda x: -x["A"]["net_pct"])[:15]:
    print("   ", line(r))
print("\n>>> TOP 15 by TEST (out-of-sample) net EV% — robustness check:")
for r in sorted(rows, key=lambda x: -(x["TE"]["net_pct"] if x["TE"] and x["TE"]["n"]>=40 else -99))[:15]:
    print("   ", line(r))
print("\n>>> entry-minute effect (Δ20, maxE 0.85, p-gate on):")
for r in rows:
    if r["dthr"]==20 and r["me"]==0.85 and r["pg"]:
        print("   ", line(r))
print("\n>>> delta-threshold effect (em=5, maxE 0.85, p-gate on):")
for r in rows:
    if r["em"]==5 and r["me"]==0.85 and r["pg"]:
        print("   ", line(r))
