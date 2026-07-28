#!/usr/bin/env python3
"""Is the right entry band a function of market regime, and which number tells us?

The entry price IS the market's probability estimate, and that estimate is driven
by how big the triggering move looks against current noise. So volatility does not
sit next to the band as an independent knob — it largely DETERMINES which price
bucket a signal lands in. If that is true, filtering on price is already filtering
on regime, and the useful question becomes which of the two actually carries the
edge.

Tested on the real 82-day Kalshi tape with the production F1 signal and bt_core's
honest economics (real ask, integer $5 sizing, taker fee both ways, train/test
split). Regime candidates: realised sigma at signal time, the raw move size, the
move measured in sigmas (the surprise), and hour of day.

  python regime_band.py
"""
import sys, math, datetime as dt
import statistics as st
import bt_core as B
sys.stdout.reconfigure(encoding="utf-8")

EM, CI = 3, 2                      # F1 fires 3 min in
DELTA_THR, KAPPA, P_THR = 50.0, 0.4, 0.65

ws = B.windows()
sig = []
for w in ws:
    d = w.deltas.get(EM)
    if d is None or CI not in w.entries or abs(d) < DELTA_THR:
        continue
    side = "UP" if d > 0 else "DOWN"
    e = B.entry_px(w, CI, side)
    if e is None:
        continue
    t_e = w.t + dt.timedelta(minutes=EM)
    s10 = B.sigma_n(t_e, 10)
    bo = B.btc_at(w.t)
    btc_e = (bo + d) if bo is not None else None
    if s10 is None or not btc_e:
        continue
    tleft = max((15 - EM) / 5, 0.1)
    snr = abs(d) / (s10 * btc_e * tleft)
    if B.Phi(KAPPA * snr) < P_THR:
        continue
    won = w.won_up if side == "UP" else (not w.won_up)
    sig.append(dict(t=w.t, e=e, won=won, sigma=s10, dollars=abs(d),
                    sig_usd=s10 * btc_e, snr=snr, hour=w.t.hour))
sig.sort(key=lambda r: r["t"])
print("F1 signals on the 82d tape: %d | span %s..%s" % (len(sig), sig[0]["t"].date(), sig[-1]["t"].date()))

def ev(rows):
    p = [B.pnl5(r["e"], r["won"]) for r in rows]
    p = [x for x in p if x is not None]
    if not p: return None
    return dict(n=len(p), tot=sum(p), ev=sum(p) / len(p),
                wr=100 * sum(1 for r in rows if r["won"]) / len(rows),
                ae=st.mean(r["e"] for r in rows))

# ---------- 1. does regime DETERMINE the entry price? ----------
print("\n" + "=" * 78)
print("1. Does volatility decide which price bucket a signal lands in?")
print("=" * 78)
def corr(a, b):
    ma, mb = st.mean(a), st.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
    return num / den if den else 0
for key, lbl in (("sig_usd", "sigma at signal ($/min)"), ("dollars", "move size |delta| ($)"),
                 ("snr", "move in sigmas (surprise)")):
    print("   corr(%-26s, entry price) = %+0.3f" % (lbl, corr([r[key] for r in sig], [r["e"] for r in sig])))
print("\n   median entry price by sigma tercile:")
srt = sorted(sig, key=lambda r: r["sig_usd"]); k = len(srt) // 3
for lbl, part in (("low sigma  (calm)", srt[:k]), ("mid sigma", srt[k:2 * k]), ("high sigma (fast)", srt[2 * k:])):
    print("      %-20s sigma %5.1f $/min -> median entry %.3f  (n=%d)" %
          (lbl, st.median(r["sig_usd"] for r in part), st.median(r["e"] for r in part), len(part)))

# ---------- 2. where is the edge: in the price, or in the regime? ----------
print("\n" + "=" * 78)
print("2. Same question as a grid: EV per trade by sigma x entry bucket")
print("=" * 78)
EB = [(0.02, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.92)]
print("   %-14s" % "sigma tercile" + "".join("%16s" % ("entry %.2f-%.2f" % b) for b in EB))
for lbl, part in (("low  (calm)", srt[:k]), ("mid", srt[k:2 * k]), ("high (fast)", srt[2 * k:])):
    cells = []
    for lo, hi in EB:
        s = ev([r for r in part if lo < r["e"] <= hi])
        cells.append("%16s" % ("%+0.3f (n=%d)" % (s["ev"], s["n"]) if s and s["n"] >= 15 else "-"))
    print("   %-14s" % lbl + "".join(cells))

# ---------- 3. each regime variable on its own, train/test ----------
print("\n" + "=" * 78)
print("3. Does any regime number split winners from losers on its own?")
print("=" * 78)
TR = B.TRAIN_END
def show(rows, lbl):
    a, tr, te = ev(rows), ev([r for r in rows if r["t"] < TR]), ev([r for r in rows if r["t"] >= TR])
    f = lambda s: ("%5d %6.1f %+7.3f" % (s["n"], s["wr"], s["ev"])) if s and s["n"] >= 15 else "%20s" % "-"
    print("   %-26s | %s | %s | %s" % (lbl, f(a), f(tr), f(te)))
print("   %-26s | %-20s | %-20s | %-20s" % ("", "------ ALL ------", "----- TRAIN -----", "----- TEST ------"))
print("   %-26s | %5s %6s %7s | %5s %6s %7s | %5s %6s %7s" %
      ("split", "n", "WR%", "EV", "n", "WR%", "EV", "n", "WR%", "EV"))
for key, lbl in (("sig_usd", "sigma"), ("dollars", "move $"), ("snr", "surprise")):
    v = sorted(r[key] for r in sig); t1, t2 = v[len(v) // 3], v[2 * len(v) // 3]
    show([r for r in sig if r[key] <= t1], "%s: LOW  (<=%.1f)" % (lbl, t1))
    show([r for r in sig if t1 < r[key] <= t2], "%s: MID" % lbl)
    show([r for r in sig if r[key] > t2], "%s: HIGH (>%.1f)" % (lbl, t2))
