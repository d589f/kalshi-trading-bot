#!/usr/bin/env python3
"""Does the 5-min reversion edge survive TWAP settlement?

The edge was measured with a spot outcome (P[open+5] vs P[open]). Polymarket has
announced TWAP resolution for its 5-min crypto markets, and XO's resolver may do
the same, so the outcome that actually pays is an AVERAGE over the tail of the
window rather than the last print. A TWAP damps late moves, which is exactly the
part of the move a reversion signal is betting on — so the edge has to be
re-measured against the outcome definition that settles it.

Neither venue has published its window or sample count, so this sweeps the
averaging length instead of guessing one, and reports how often the TWAP verdict
disagrees with the spot verdict (the mechanism by which the edge can move).

Outcome definitions, per non-overlapping 5-min window opening at minute m0:
  spot        close P[m0+5]                  vs open P[m0]
  twapC(k)    mean(P[m0+6-k .. m0+5])        vs open P[m0]             (close-only TWAP)
  twapB(k)    mean(P[m0+6-k .. m0+5])        vs mean(P[m0+1-k .. m0])  (both ends)

The opening leg averages INTO the open rather than out of it: XO publishes its
"Opening reference price" at window start, so it can only be built from prices at
or before m0. Averaging forward from m0 would also collide with the closing leg
once k reaches the window length and silently compare a value to itself.

Signal is unchanged and lookahead-safe: the trailing K-min return strictly before
m0. Reversion bets against it, momentum with it.

  python xo_twap.py [days]
"""
import sys, datetime as dt
import statistics as st
import px, coinbase_px
sys.stdout.reconfigure(encoding="utf-8")

END = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
START = END - dt.timedelta(days=DAYS)
SMS, EMS = int(START.timestamp() * 1000), int(END.timestamp() * 1000)
W = int(sys.argv[2]) if len(sys.argv) > 2 else 5   # window length (min): 5 = XO/Poly pulse, 15 = Kalshi
REV_K = (1, 3, 5)          # trailing lookbacks to test
REV_THR = (0.0006, 0.0010, 0.0020)


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


def mean_or_none(P, lo, hi):
    """mean of P over the inclusive minute range, None if any sample is missing"""
    vals = []
    for m in range(lo, hi + 1):
        v = P.get(m)
        if v is None:
            return None
        vals.append(v)
    return sum(vals) / len(vals) if vals else None


def outcomes_for(P, m0):
    """{label: is_up} for every settlement definition, None where data is missing"""
    o = P.get(m0)
    c = P.get(m0 + W)
    out = {}
    out["spot"] = (c > o) if (o is not None and c is not None) else None
    for k in (2, 3, min(5, W)):
        cl = mean_or_none(P, m0 + W - k + 1, m0 + W)
        out["twapC%d" % k] = (cl > o) if (cl is not None and o is not None) else None
        op = mean_or_none(P, m0 - k + 1, m0)      # averages INTO the open, never past it
        out["twapB%d" % k] = (cl > op) if (cl is not None and op is not None) else None
    return out


DEFS = ["spot", "twapC2", "twapC3", "twapC5", "twapB2", "twapB3", "twapB5"]


def run(P, label):
    mins = sorted(P)
    rows = []
    for m0 in range(mins[0], mins[-1] + 1, W):        # non-overlapping grid
        oc = outcomes_for(P, m0)
        if oc["spot"] is None:
            continue
        base = P.get(m0)
        trail = {}
        for K in REV_K:
            p0 = P.get(m0 - K)
            trail[K] = ((base - p0) / p0) if (p0 not in (None, 0)) else None
        rows.append((m0, trail, oc))
    print("\n================= FEED: %s  (%d windows) =================" % (label.upper(), len(rows)))

    # 1) how much does the settlement definition actually change the verdict?
    print("\nA) verdict disagreement vs spot  (this is the whole mechanism)")
    print("   %-9s %7s %9s %9s" % ("defn", "n", "flip%", "ties(flat)"))
    for d in DEFS:
        pair = [(r[2]["spot"], r[2][d]) for r in rows if r[2].get(d) is not None]
        if not pair:
            continue
        flip = sum(1 for a, b in pair if a != b)
        print("   %-9s %7d %8.2f%%" % (d, len(pair), 100 * flip / len(pair)))

    # 2) does the reversion edge survive each definition?
    print("\nB) REVERSION hit rate by settlement definition  (bet AGAINST the trailing move)")
    hdr = "   %-14s" % "(lookback,thr)" + "".join("%10s" % d for d in DEFS)
    print(hdr)
    best = {}
    for K in REV_K:
        for thr in REV_THR:
            cells = []
            for d in DEFS:
                n = win = 0
                for m0, trail, oc in rows:
                    r = trail.get(K)
                    if r is None or abs(r) < thr or oc.get(d) is None:
                        continue
                    side_up = not (r > 0)                    # reversion = fade
                    won = (oc[d] == side_up)
                    n += 1; win += 1 if won else 0
                if n >= 50:
                    h, lo, hi = ci95(win, n)
                    cells.append("%9.2f%%" % (100 * h))
                    best.setdefault(d, []).append((h, lo, n, K, thr))
                else:
                    cells.append("%10s" % "-")
            print("   %-14s" % ("K=%d thr=%.2f%%" % (K, 100 * thr)) + "".join(cells))

    # 3) the honest bottom line per definition: does ANY cell clear 50% with CI?
    print("\nC) survival check — best cell per definition, and whether its 95%% CI clears 50%%")
    print("   %-9s %8s %8s %9s %9s   %s" % ("defn", "hit%", "ci_lo", "n", "cell", "verdict"))
    for d in DEFS:
        if d not in best:
            continue
        h, lo, n, K, thr = max(best[d], key=lambda x: x[0])
        verdict = "SURVIVES" if lo > 0.50 else ("marginal" if h > 0.50 else "GONE")
        print("   %-9s %7.2f%% %7.2f%% %9d  K=%d/%.2f%%   %s" % (d, 100 * h, 100 * lo, n, K, 100 * thr, verdict))


if __name__ == "__main__":
    feeds = load()
    print("span %s..%s (%dd) | feeds: %s" %
          (START.strftime("%m-%d"), END.strftime("%m-%d"), DAYS,
           {k: len(v) for k, v in feeds.items()}))
    print("\nNOTE: price data is 1-minute bars, so a TWAP here is the mean of the last k")
    print("      MINUTE closes. A venue sampling every second over 60s is finer than")
    print("      twapC2; treat these as the shape of the effect, not the exact number.")
    for name, P in feeds.items():
        run(P, name)
