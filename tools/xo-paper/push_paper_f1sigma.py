#!/usr/bin/env python3
"""Push a REGIME-FILTERED F1 series to the dashboard, taking the retired f6 slot.

F1 fires on a fixed $50 move, so how expensive the resulting contract is depends on
how big that move looks against current noise. Measured over the 82-day tape the win
rate barely moves with volatility (~80.6% across three of four regimes) but EV falls
from +0.062 to -0.121, because rising sigma pushes the signal into dearer buckets
where the break-even bar climbs 64% -> 89% while our hit rate does not. So the edge
is not lost to being wrong more often, it is lost to paying more for being right.

This series applies the two filters that follow from that:
  sigma <= SIGMA_MAX     skip fast regimes outright (whole row is negative)
  entry <= ENTRY_MAX     inside calm regimes only the cheaper buckets clear their bar

Sigma is not stored, but it is recoverable: the engine records p_model = Phi(k*|d|/
(sigma*tau)), so sigma = C * |delta| / Phi^-1(p_model). C was calibrated against an
independently computed sigma over 396 shared windows (median residual 16%).

On the tape: n=197, WR 74.6%, EV +0.173/trade, +$34.07 over 82 days, positive in BOTH
halves (train +20.92 / test +13.15) — unlike the unfiltered F1 (-$50.31) and unlike
the live band alone (-$15.04).

CAVEAT worth keeping in view: the sigma threshold is NOT monotone across the sweep
(20 -> +$26, 25 -> -$5, 30 -> +$40, 35 -> +$17), so the exact number is partly fitted.
Treat this line as a forward experiment, not a settled result.
"""
import sqlite3, json, math, urllib.request

DB = "/root/paper_compare_kalshi_15m/live_data.db"
TOKEN = "f6shadow_7c1d"
URL = "http://23.95.217.78:8890/paper"     # the slot f6 used to occupy
RESET_FROM = "2026-06-25T16:00"
SESSION = "f1_d50cap75"
SIGMA_MAX = 30.0        # $/min of BTC noise at signal time
ENTRY_MAX = 0.75        # calm regimes: dearer buckets do not clear their break-even
SIGMA_C = 0.4896        # calibrated sigma = C * |delta| / Phi^-1(p_model)

COLS = ["window_start", "side", "entry_price", "delta", "p_model", "pnl", "result", "market_slug"]


def ppf(p):
    """inverse normal CDF (Acklam), enough precision for a regime cut"""
    if not (0.0 < p < 1.0):
        return None
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl = 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - pl:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def sigma_usd(delta, p_model):
    """recover BTC noise at signal time from what the engine already stored"""
    try:
        d = abs(float(delta)); p = float(p_model)
    except (TypeError, ValueError):
        return None
    z = ppf(min(max(p, 1e-6), 1 - 1e-6))
    if z is None or z <= 0.05 or d <= 0:
        return None
    return SIGMA_C * d / z


c = sqlite3.connect(DB)
raw = list(c.execute(
    "select " + ",".join(COLS) + " from paper_trades where session_id=? "
    "and window_start >= ? order by window_start desc limit 6000", (SESSION, RESET_FROM)))

rows, skipped_sigma, skipped_entry = [], 0, 0
for r in raw:
    rec = dict(zip(COLS, r))
    s = sigma_usd(rec.get("delta"), rec.get("p_model"))
    if s is None or s > SIGMA_MAX:
        skipped_sigma += 1
        continue
    try:
        if float(rec["entry_price"]) > ENTRY_MAX:
            skipped_entry += 1
            continue
    except (TypeError, ValueError):
        continue
    rows.append(rec)

data = json.dumps(rows).encode()
req = urllib.request.Request(URL, data=data,
                             headers={"Content-Type": "application/json", "X-Token": TOKEN},
                             method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("pushed %d of %d (skipped: sigma>%.0f %d, entry>%.2f %d) -> %s"
              % (len(rows), len(raw), SIGMA_MAX, skipped_sigma, ENTRY_MAX, skipped_entry,
                 resp.read().decode()))
except Exception as e:
    print("push err", e)
