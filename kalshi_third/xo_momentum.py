#!/usr/bin/env python3
"""Does short-horizon BTC momentum predict a 5-min up/down window — the XO
'BTC 5min Pulse' horizon? XO is a PRE-START auction (you bet before the 5-min
window starts; book dies ~60s after), so the only signal at bet time is trailing
momentum. HIT RATE is the decision variable: on a naive ~0.50 auction the mover
side has EV/$ = 2*hit-1 (minus fee). We report hit rate + 95% CI so the reader
can apply any fee. Two feeds (Binance path & Coinbase/BRTI-proxy). Lookahead-safe:
signal uses only prices up to the decision minute; outcome uses a strictly-later
price. Non-overlapping aligned 5-min grid so CIs are honest (no window overlap).

  python xo_momentum.py            # last 90 days, both feeds
"""
import sys, datetime as dt
import statistics as st
import px, coinbase_px
sys.stdout.reconfigure(encoding="utf-8")

END = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
START = END - dt.timedelta(days=DAYS)
SMS, EMS = int(START.timestamp() * 1000), int(END.timestamp() * 1000)
W = 5  # window length (minutes) = XO pulse horizon

def kfee(q):  # Kalshi taker fee as a fraction of $1 payout, per contract at price q
    return 0.07 * q * (1 - q)

def load():
    feeds = {}
    try:
        b = px.btc_path(SMS, EMS, symbol="BTCUSDT")
        if len(b) > 1000:
            feeds["binance"] = b
    except Exception as e:
        print("binance load failed:", str(e)[:80])
    try:
        c = {int(k): v for k, v in coinbase_px.btc_path(SMS, EMS).items()}
        if len(c) > 1000:
            feeds["coinbase"] = c
    except Exception as e:
        print("coinbase load failed:", str(e)[:80])
    return feeds

def ci95(win, n):
    h = win / n
    se = (h * (1 - h) / n) ** 0.5
    return h, h - 1.96 * se, h + 1.96 * se

# ---- Exp A: PRE-START trailing momentum (XO-tradable). signal at window start m0 =
#      trailing K-min return; bet the mover side; outcome = P[m0+W] vs P[m0]. -------
def exp_prestart(P, anti=False):
    mins = sorted(P)
    rows = {}
    for K in (1, 2, 3, 5, 10):
        for thr in (0.0, 0.0003, 0.0006, 0.0010, 0.0015, 0.0020):
            n = win = flat = 0
            for m0 in range(mins[0], mins[-1] + 1, W):     # non-overlapping aligned grid
                ps, pp, pe = P.get(m0), P.get(m0 - K), P.get(m0 + W)
                if ps is None or pp is None or pe is None:
                    continue
                r = (ps - pp) / pp
                if abs(r) < thr:
                    continue
                if pe == ps:
                    flat += 1
                    continue
                up_moved = r > 0
                side_up = (not up_moved) if anti else up_moved
                won = (pe > ps) if side_up else (pe < ps)
                n += 1; win += 1 if won else 0
            if n >= 50:
                h, lo, hi = ci95(win, n)
                rows[(K, thr)] = dict(n=n, hit=h, lo=lo, hi=hi, flat=flat,
                                      ev050=2 * h - 1, evk=(2 * h - 1) - 2 * kfee(0.50))
    return rows

# ---- Exp B: POST-START peek (the true F1 analog: observe a move, bet continuation).
#      Marginally XO-tradable only in the first ~60s. signal = first D-min move inside
#      the window; bet remaining W-D min; outcome = P[m0+W] vs P[m0+D]. --------------
def exp_continuation(P):
    mins = sorted(P)
    rows = {}
    for D in (1, 2):                       # minutes waited into the 5-min window
        for thr in (0.0, 0.0003, 0.0006, 0.0010, 0.0015):
            n = win = 0
            for m0 in range(mins[0], mins[-1] + 1, W):
                ps, pd, pe = P.get(m0), P.get(m0 + D), P.get(m0 + W)
                if ps is None or pd is None or pe is None or pe == pd:
                    continue
                r = (pd - ps) / ps
                if abs(r) < thr:
                    continue
                side_up = r > 0
                won = (pe > pd) if side_up else (pe < pd)
                n += 1; win += 1 if won else 0
            if n >= 50:
                h, lo, hi = ci95(win, n)
                rows[(D, thr)] = dict(n=n, hit=h, lo=lo, hi=hi,
                                      ev050=2 * h - 1, evk=(2 * h - 1) - 2 * kfee(0.50))
    return rows

def base_rate_move(P, usd=50.0):
    """How often does BTC actually move >= $usd over W minutes (F1 uses $50)."""
    mins = sorted(P); n = big = 0
    for m0 in range(mins[0], mins[-1] + 1, W):
        ps, pe = P.get(m0), P.get(m0 + W)
        if ps is None or pe is None:
            continue
        n += 1; big += 1 if abs(pe - ps) >= usd else 0
    return big / n if n else 0, n

def show(title, rows, keyname):
    print("\n" + title)
    print("%-10s %5s %6s %7s %7s %8s %9s" % (keyname, "n", "hit%", "ci_lo", "ci_hi", "EV@.50", "EV_kfee"))
    for k in sorted(rows):
        d = rows[k]
        print("%-10s %5d %6.2f %7.2f %7.2f %+8.3f %+9.3f" %
              (str(k), d["n"], 100 * d["hit"], 100 * d["lo"], 100 * d["hi"], d["ev050"], d["evk"]))

if __name__ == "__main__":
    feeds = load()
    print("span %s..%s (%dd) | feeds: %s" %
          (START.strftime("%m-%d"), END.strftime("%m-%d"), DAYS,
           {k: len(v) for k, v in feeds.items()}))
    for name, P in feeds.items():
        br, nb = base_rate_move(P, 50)
        print("\n================= FEED: %s (base rate |Δ|>=$50 in 5m = %.1f%% of %d) =================" %
              (name.upper(), 100 * br, nb))
        show("A) PRE-START momentum (bet WITH trailing move) [XO-tradable]", exp_prestart(P, anti=False), "(K,thr)")
        show("A') PRE-START anti-momentum (bet AGAINST) [mean-reversion check]", exp_prestart(P, anti=True), "(K,thr)")
        show("B) POST-START continuation (F1 analog; peek D min, bet rest)", exp_continuation(P), "(D,thr)")
