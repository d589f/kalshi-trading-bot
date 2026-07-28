#!/usr/bin/env python3
"""What does the live entry band actually buy us, and is a CHEAP-side cap better?

Live currently trades (0.65, 0.75] — moderate favourites. The opposite hypothesis
is that the cheap entries are the good ones: at 0.60 you risk 60c to win 40c, so
the win rate needed to break even is far lower than at 0.80. This sweeps the band
over the real Kalshi tape with the production F1 signal and the honest economics
from bt_core (real ask, integer $5 sizing, taker fee on win AND loss), split
train/test so a band that only works in-sample is visible as such.

Break-even win rate for a taker at entry e is (e + fee)/1, so the bar rises with
price — that is the whole reason a band matters at all.

  python band_sweep.py
"""
import sys, datetime as dt
import bt_core as B
sys.stdout.reconfigure(encoding="utf-8")

# production F1: fires at 3 min in, |Δ|>=$50, p>=0.65 via Phi(kappa*snr), sigma=max10
EM = int(__import__("sys").argv[1]) if len(__import__("sys").argv)>1 else 3   # minutes into the window when the signal fires
CI = EM - 1            # candle whose close is the price at minute EM
DELTA_THR = 50.0
KAPPA, P_THR = 0.4, 0.65

ws = B.windows()
print("windows: %d | span %s .. %s" % (len(ws), ws[0].t.date(), ws[-1].t.date()))

# ---- precompute the signal once; the band is applied afterwards ----
sig = []          # (t, entry, won)  for every window where F1 would fire
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
    if B.Phi(KAPPA * abs(d) / (s10 * btc_e * tleft)) < P_THR:
        continue
    won = w.won_up if side == "UP" else (not w.won_up)
    sig.append((w.t, e, won))
sig.sort()
print("F1 signals (before any band): %d | train/test split at %s\n" % (len(sig), B.TRAIN_END.date()))


def band(lo, hi):
    return [(t, e, won) for t, e, won in sig if lo < e <= hi]


def line(label, lo, hi):
    rows = band(lo, hi)
    ev = B.evaluate(rows)
    a, tr, te = ev["all"], ev["train"], ev["test"]
    if not a:
        print("  %-18s %5s" % (label, "-")); return
    be = 100 * (sum(e for _, e, _ in rows) / len(rows) + 0.0175)   # rough breakeven WR at avg entry
    f = lambda s: ("%6d %6.1f %+8.3f %+9.2f" % (s["n"], s["wr"], s["ev"], s["total"])) if s else " " * 32
    print("  %-18s %s | %s | %s  | be~%.0f%%" % (label, f(a), f(tr), f(te), be))


print("%-20s %s | %s | %s" % ("", "-------- ALL --------", "------- TRAIN -------", "------- TEST --------"))
print("  %-18s %6s %6s %8s %9s | %6s %6s %8s %9s | %6s %6s %8s %9s" %
      ("band (lo,hi]", "n", "WR%", "EV/tr", "total$", "n", "WR%", "EV/tr", "total$", "n", "WR%", "EV/tr", "total$"))

print("\n-- the question: cap on the CHEAP side --")
for hi in (0.55, 0.60, 0.64, 0.70):
    line("(0.02, %.2f]" % hi, 0.02, hi)

print("\n-- what live runs today, and its neighbours --")
for lo, hi in ((0.65, 0.75), (0.64, 0.75), (0.65, 0.80), (0.60, 0.75), (0.55, 0.80)):
    line("(%.2f, %.2f]" % (lo, hi), lo, hi)

print("\n-- no band at all / expensive side only --")
line("(0.02, 0.92]", 0.02, 0.92)
line("(0.75, 0.92]", 0.75, 0.92)

print("\n-- shape:by decile of entry price --")
print("  %-18s %6s %6s %8s %9s   %s" % ("bucket", "n", "WR%", "EV/tr", "total$", "breakeven WR"))
lo = 0.02
for hi in (0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.92):
    rows = band(lo, hi)
    if rows:
        s = B.evaluate(rows)["all"]
        ae = sum(e for _, e, _ in rows) / len(rows)
        bewr = 100 * (ae + 0.07 * ae * (1 - ae))
        print("  (%.2f, %.2f]      %6d %6.1f %+8.3f %+9.2f   %.1f%%  %s" %
              (lo, hi, s["n"], s["wr"], s["ev"], s["total"], bewr,
               "EDGE" if s["wr"] > bewr else ""))
    lo = hi
