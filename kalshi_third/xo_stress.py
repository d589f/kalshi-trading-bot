#!/usr/bin/env python3
"""Adversarial stress-test of the BTC 5-min mean-reversion finding.
Three attacks:
  1) Roll bid-ask-bounce artifact (magnitude + threshold-monotonicity signature)
  2) Sub-period (monthly) robustness
  3) Realistic FADE entry EV (you buy the beaten-down side, cross ~1c spread)

  python xo_stress.py            # last 90 days, both feeds
"""
import sys, datetime as dt
import statistics as st
import px, coinbase_px
sys.stdout.reconfigure(encoding="utf-8")

END = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
START = END - dt.timedelta(days=DAYS)
SMS, EMS = int(START.timestamp() * 1000), int(END.timestamp() * 1000)
W = 5

def ci95(win, n):
    h = win / n
    se = (h * (1 - h) / n) ** 0.5
    return h, h - 1.96 * se, h + 1.96 * se

def load():
    feeds = {}
    try:
        b = px.btc_path(SMS, EMS, symbol="BTCUSDT")
        if len(b) > 1000: feeds["binance"] = b
    except Exception as e:
        print("binance load failed:", str(e)[:80])
    try:
        c = {int(k): v for k, v in coinbase_px.btc_path(SMS, EMS).items()}
        if len(c) > 1000: feeds["coinbase"] = c
    except Exception as e:
        print("coinbase load failed:", str(e)[:80])
    return feeds

# minute index -> approximate UTC datetime (minute index = ms/60000 + 1 convention)
def midx_to_dt(m):
    return dt.datetime.utcfromtimestamp((m - 1) * 60)

# ---------- collect qualifying anti-momentum windows with metadata ----------
def collect(P, K, thr):
    """Return list of dicts per qualifying window: r (trailing ret), won (anti-momentum
    hit i.e. reverted), absmove_usd (|P[m0+W]-P[m0]|), ps (open px), month key."""
    mins = sorted(P)
    out = []
    for m0 in range(mins[0], mins[-1] + 1, W):
        ps, pp, pe = P.get(m0), P.get(m0 - K), P.get(m0 + W)
        if ps is None or pp is None or pe is None: continue
        r = (ps - pp) / pp
        if abs(r) < thr: continue
        if pe == ps: continue
        up_moved = r > 0
        side_up = not up_moved           # anti-momentum: bet against trailing move
        won = (pe > ps) if side_up else (pe < ps)
        d = midx_to_dt(m0)
        out.append(dict(r=r, won=won, ps=ps, pe=pe,
                        move_usd=abs(pe - ps), mkey="%04d-%02d" % (d.year, d.month)))
    return out

# ============================ TASK 1: BOUNCE ARTIFACT ========================
def task1_bounce(P, name):
    print("\n########## TASK 1  BID-ASK-BOUNCE ARTIFACT — feed=%s ##########" % name)
    # (a) magnitude: typical price, a $1 spread, vs the 0.1% threshold in $.
    px_mean = st.mean(P.values())
    spread = 1.0            # generous BTC top-of-book spread, USD
    half = spread / 2
    thr_usd = 0.001 * px_mean
    print("  mean BTC px            = $%.0f" % px_mean)
    print("  assumed spread         = $%.2f (half=$%.2f)  [~%.4f%% of px]" %
          (spread, half, 100 * spread / px_mean))
    print("  0.1%% move threshold    = $%.1f" % thr_usd)
    print("  half-spread / threshold= %.4f  -> a bounce is %.0fx too small to fake a 0.1%% move"
          % (half / thr_usd, thr_usd / half))
    # Roll: max spurious reversion in *price* from bounce is bounded by the spread.
    # Fraction of a threshold move that a full bounce could account for:
    print("  Roll spurious neg-autocov = -(s/2)^2 = -$%.3f^2; a $%.0f move dwarfs $%.2f bounce"
          % (half, thr_usd, half))

    # (b) signature test: does reversion strength INCREASE with threshold?
    #     bounce artifact -> reversion should DECREASE as move grows (bounce fixed size,
    #     move grows). Real reversion -> INCREASE. Report the slope.
    print("  --- anti-momentum hit%% vs threshold (K=1 lookback) ---")
    print("  %-9s %6s %7s %7s" % ("thr", "n", "hit%", "d_ret(1)"))
    hits = []
    for thr in (0.0003, 0.0006, 0.0010, 0.0015, 0.0020, 0.0030):
        w = collect(P, 1, thr)
        if len(w) < 40: continue
        win = sum(1 for x in w if x["won"])
        h = win / len(w)
        hits.append((thr, h))
        print("  %-9.4f %6d %7.2f" % (thr, len(w), 100 * h))
    incr = all(hits[i][1] <= hits[i + 1][1] + 0.01 for i in range(len(hits) - 1))
    if len(hits) >= 2:
        slope = (hits[-1][1] - hits[0][1]) / (hits[-1][0] - hits[0][0])
        print("  reversion vs threshold: %s (hit %.1f%%@%.2f%% -> %.1f%%@%.2f%%, slope=%+.1f hit-frac/ret)"
              % ("INCREASES (real reversion, NOT bounce)" if hits[-1][1] > hits[0][1] else "DECREASES (bounce-like)",
                 100 * hits[0][1], 100 * hits[0][0], 100 * hits[-1][1], 100 * hits[-1][0], slope))
    return hits

# ============================ TASK 2: SUB-PERIOD =============================
def task2_subperiods(P, name):
    print("\n########## TASK 2  SUB-PERIOD ROBUSTNESS — feed=%s ##########" % name)
    for K in (1, 5):
        w = collect(P, K, 0.0010)   # |ret|>=0.1%
        by = {}
        for x in w:
            by.setdefault(x["mkey"], []).append(x["won"])
        print("  --- lookback K=%dmin, |ret|>=0.1%% ---" % K)
        print("  %-9s %6s %7s %7s %7s" % ("month", "n", "hit%", "ci_lo", "ci_hi"))
        allpos = True
        for mk in sorted(by):
            arr = by[mk]; n = len(arr); win = sum(arr)
            if n < 20:
                print("  %-9s %6d   (thin, skipped)" % (mk, n)); continue
            h, lo, hi = ci95(win, n)
            flag = "" if h > 0.50 else "  <-- <=50%"
            if h <= 0.50: allpos = False
            print("  %-9s %6d %7.2f %7.2f %7.2f%s" % (mk, n, 100 * h, 100 * lo, 100 * hi, flag))
        print("  ALL sub-periods >50%%: %s" % ("YES" if allpos else "NO"))

# ============================ TASK 3: REALISTIC ENTRY =======================
def task3_entry(P, name):
    print("\n########## TASK 3  REALISTIC FADE ENTRY EV — feed=%s ##########" % name)
    SPREAD = 0.01   # cross ~1c
    for K, thr in ((1, 0.0010), (3, 0.0010), (5, 0.0020)):
        w = collect(P, K, thr)
        n = len(w)
        if n < 40: continue
        hit = sum(1 for x in w if x["won"]) / n
        mean_abs_r = st.mean(abs(x["r"]) for x in w)
        print("\n  (K=%dmin, |ret|>=%.2f%%)  n=%d  hit=%.2f%%  mean|ret|=%.3f%%"
              % (K, 100 * thr, n, 100 * hit, 100 * mean_abs_r))
        # EV per contract = hit - entry_price   (XO ~0% fee, $1 payout)
        # Model the mover side priced up proportionally: mover_px = 0.5 + alpha*|r|.
        # We fade -> buy the OTHER (beaten) side at (1 - mover_px) + SPREAD.
        # alpha in price-cents per unit return. alpha=50 => 0.1% move -> +5c on mover.
        print("    %-30s %8s %8s" % ("entry model", "entry$", "EV/contract"))
        # (a) market ignores the move: buy beaten side ~0.50, still cross spread
        for label, alpha in (("flat 0.50 (+1c spread)", 0.0),
                             ("overreact a=25 (2.5c/0.1%)", 25.0),
                             ("overreact a=50 (5c/0.1%)", 50.0),
                             ("overreact a=100(10c/0.1%)", 100.0)):
            # per-window entry then EV, so heterogenous |r| handled honestly
            tot_ev = 0.0; tot_entry = 0.0
            for x in w:
                mover = 0.5 + alpha * abs(x["r"])
                mover = min(mover, 0.98)
                fade_entry = (1 - mover) + SPREAD
                fade_entry = min(max(fade_entry, 0.02), 0.99)
                tot_entry += fade_entry
                tot_ev += (1.0 if x["won"] else 0.0) - fade_entry
            print("    %-30s %8.3f %+8.4f" % (label, tot_entry / n, tot_ev / n))
        # (b) EFFICIENT-market adversarial case: beaten side already priced at TRUE prob
        #     (=hit) so no free discount; you still pay spread. EV = -spread.
        eff_entry = hit + SPREAD
        print("    %-30s %8.3f %+8.4f  <-- adversarial: XO prices reversion in"
              % ("efficient (fade@truep+1c)", eff_entry, hit - eff_entry))
        # break-even mover markup needed to overcome spread at this hit:
        # EV=0 => (1-mover)+spread = hit => mover = 1 - hit + spread
        be_mover = 1 - hit + SPREAD
        print("    break-even mover price = %.3f (needs mover >%.1fc over 0.50, i.e. alpha>%.0f)"
              % (be_mover, 100 * (be_mover - 0.5) * -1 if be_mover < 0.5 else 100*(be_mover-0.5),
                 max(0.0, (be_mover - 0.5)) / mean_abs_r if mean_abs_r else 0))

if __name__ == "__main__":
    feeds = load()
    print("span %s..%s (%dd) | feeds: %s" %
          (START.strftime("%m-%d"), END.strftime("%m-%d"), DAYS,
           {k: len(v) for k, v in feeds.items()}))
    for name, P in feeds.items():
        print("\n=================================== FEED: %s ===================================" % name.upper())
        task1_bounce(P, name)
        task2_subperiods(P, name)
        task3_entry(P, name)
