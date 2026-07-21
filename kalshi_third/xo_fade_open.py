#!/usr/bin/env python3
"""XO 'BTC 5min Pulse' fade-the-distance-from-open backtest.

The pulse settles UP/DOWN vs the window OPEN ("Price to Beat"). It is tradable
the WHOLE 5-min window (unlike a pre-start auction), so the real signal is:
at minute D into the window, BTC sits X away from the open. If the book prices
the current side rich, and BTC tends to mean-revert toward open, fade it: buy
the CHEAP (toward-open) side.

Signal at m0+D (uses only prices <= m0+D):
  dist = (P[m0+D] - P[m0]) / P[m0]
  ABOVE open by >= thr  -> bet DOWN (fade)
  BELOW open by >= thr  -> bet UP   (fade)
Outcome (strictly later): P[m0+5] vs OPEN P[m0].
  win = window ended on the BET side of the open
        (fade-DOWN wins if P[m0+5] < P[m0]; fade-UP wins if P[m0+5] > P[m0])

Non-overlapping aligned 5-min grid so CIs are honest. Two feeds: Binance path
(px.btc_path) and Coinbase/BRTI-proxy (coinbase_px). EV reported at flat 0.50
entry and after Kalshi taker fee 0.07*p*(1-p). CHASE (don't fade) shown for
contrast: same trigger, opposite side.

  python xo_fade_open.py            # last 90 days, both feeds
"""
import sys, datetime as dt
import px, coinbase_px
sys.stdout.reconfigure(encoding="utf-8")

END = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
START = END - dt.timedelta(days=DAYS)
SMS, EMS = int(START.timestamp() * 1000), int(END.timestamp() * 1000)
W = 5                                   # XO pulse horizon (minutes)
DS = (1, 2, 3)                          # minutes into window at decision
THRS = (0.0002, 0.0004, 0.0006, 0.0010)  # distance-from-open thresholds

def kfee(q):   # Kalshi taker fee as a fraction of $1 payout, per contract at price q
    return 0.07 * q * (1 - q)

def ci95(win, n):
    h = win / n
    se = (h * (1 - h) / n) ** 0.5
    return h, h - 1.96 * se, h + 1.96 * se

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

def run(P, fade=True):
    """rows[(D,thr)] -> stats. fade=True buys toward-open side; fade=False chases."""
    mins = sorted(P)
    rows = {}
    for D in DS:
        for thr in THRS:
            n = win = flat = 0
            for m0 in range(mins[0], mins[-1] + 1, W):   # non-overlapping aligned grid
                po, pd, pe = P.get(m0), P.get(m0 + D), P.get(m0 + W)
                if po is None or pd is None or pe is None:
                    continue
                dist = (pd - po) / po
                if abs(dist) < thr:
                    continue
                if pe == po:                              # settled exactly at open -> tie
                    flat += 1
                    continue
                above = dist > 0
                # fade: above open -> bet DOWN; below -> bet UP. chase: opposite.
                bet_down = above if fade else (not above)
                ended_below = pe < po
                won = (ended_below == bet_down)
                n += 1; win += 1 if won else 0
            if n >= 50:
                h, lo, hi = ci95(win, n)
                rows[(D, thr)] = dict(n=n, hit=h, lo=lo, hi=hi, flat=flat,
                                      ev050=2 * h - 1,
                                      evk=(2 * h - 1) - 2 * kfee(0.50))
    return rows

def show(title, rows):
    print("\n" + title)
    print("%-11s %6s %6s %7s %7s %8s %9s" %
          ("(D,thr)", "n", "hit%", "ci_lo", "ci_hi", "EV@.50", "EV_kfee"))
    for k in sorted(rows):
        d = rows[k]
        kd = "(%d,%.2f%%)" % (k[0], 100 * k[1])
        print("%-11s %6d %6.2f %7.2f %7.2f %+8.3f %+9.3f" %
              (kd, d["n"], 100 * d["hit"], 100 * d["lo"], 100 * d["hi"],
               d["ev050"], d["evk"]))

if __name__ == "__main__":
    feeds = load()
    print("span %s..%s (%dd) | feeds: %s" %
          (START.strftime("%m-%d"), END.strftime("%m-%d"), DAYS,
           {k: len(v) for k, v in feeds.items()}))
    for name, P in feeds.items():
        print("\n================= FEED: %s =================" % name.upper())
        show("FADE distance-from-open (bet toward open / mean-reversion)", run(P, True))
        show("CHASE distance-from-open (bet away from open / continuation)", run(P, False))
