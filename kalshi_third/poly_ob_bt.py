"""Orderbook-accurate backtest on Polymarket btc-updown-15m (Predexon snapshots).

Answers three things the trade-tape round-2 could not:
  (A) F1 momentum filled at the REAL best ask at the signal instant, zero fee —
      the most favourable honest taker fill (immediate, no 45s VWAP proxy).
  (B) Latency sensitivity: ask at 180s vs +1/+3/+5/+10s — does it run in seconds?
  (C) Book-imbalance signal (orderbook-only, no Binance): does top-of-book
      pressure at minute N predict the settlement?
"""
import sys, statistics as stt
sys.stdout.reconfigure(encoding="utf-8")
import px, poly_ob_core as ob

ws = ob.load_ob_windows()
print("OB windows: %d | span %s..%s | with DOWN book: %d"
      % (len(ws), ws[0].t.strftime("%m-%d"), ws[-1].t.strftime("%m-%d"),
         sum(1 for w in ws if w.dn_snaps)))

# BTC path over the span
st_ms = int(ws[0].t.timestamp() * 1000) - 3600_000
en_ms = int(ws[-1].t.timestamp() * 1000) + 16 * 60_000
P = px.btc_path(st_ms, en_ms)
def btc(sec_ts):
    mi = sec_ts // 60
    for d in (0, -1, 1, -2, 2):
        if mi + d in P:
            return P[mi + d]
    return None

def days(rows):
    return max((rows[-1][0] - rows[0][0]).total_seconds() / 86400, 1) if len(rows) > 1 else 1

# ---------- (A) + (B) momentum at real ask, latency sweep ----------
print("\n=== (A/B) F1 momentum @ real Poly ask, ZERO fee, latency sweep ===")
print("thr  lat   n   WR%  avg_ask  EV/tr   total   $/day")
for thr in (50, 35):
    for lat in (0, 1, 3, 5, 10):
        rows = []
        for w in ws:
            ot = int(w.t.timestamp())
            b0 = btc(ot); b3 = btc(ot + 180)
            if b0 is None or b3 is None:
                continue
            d = b3 - b0
            if abs(d) < thr:
                continue
            side = "UP" if d > 0 else "DOWN"
            e = ob.ask_side(w, side, 180 + lat)
            if e is None or e <= 0.02 or e >= 0.99:
                continue
            won = w.won_up if side == "UP" else (not w.won_up)
            p = ob.pnl_poly(e, won)
            if p is not None:
                rows.append((w.t, e, won, p))
        if len(rows) < 30:
            continue
        wr = 100 * sum(1 for r in rows if r[2]) / len(rows)
        tot = sum(r[3] for r in rows)
        ae = stt.mean(r[1] for r in rows)
        print("%3d  +%2ds %4d %5.1f  %6.3f  %+6.3f %+7.1f  %+6.2f"
              % (thr, lat, len(rows), wr, ae, tot/len(rows), tot, tot/days(rows)))
    print()

# ---------- (C) book imbalance as a predictor ----------
print("=== (C) UP book-imbalance @ minute m -> predicts won_up? (orderbook only) ===")
for m in (3, 5, 8):
    sec = m * 60
    obs = []
    for w in ws:
        im = ob.imbalance_at(w, sec)
        if im is None:
            continue
        obs.append((im, w.won_up))
    if len(obs) < 50:
        continue
    obs.sort()
    # decile buckets: is realized P(up) monotone in imbalance?
    q = len(obs) // 5
    print("  minute %d (n=%d):" % (m, len(obs)))
    for i in range(5):
        chunk = obs[i*q:(i+1)*q] if i < 4 else obs[i*q:]
        if not chunk:
            continue
        mi = stt.mean(x[0] for x in chunk)
        wr = 100 * sum(1 for x in chunk if x[1]) / len(chunk)
        print("    imb quintile %d: mean_imb %+.3f -> P(up) %.1f%% (n=%d)" % (i+1, mi, wr, len(chunk)))
    # strategy: buy the side imbalance favors, at real ask, zero fee
    for edge in (0.2, 0.4):
        rows = []
        for w in ws:
            im = ob.imbalance_at(w, sec)
            if im is None or abs(im) < edge:
                continue
            side = "UP" if im > 0 else "DOWN"
            e = ob.ask_side(w, side, sec)
            if e is None or e <= 0.02 or e >= 0.99:
                continue
            won = w.won_up if side == "UP" else (not w.won_up)
            p = ob.pnl_poly(e, won)
            if p is not None:
                rows.append((w.t, e, won, p))
        if len(rows) < 30:
            continue
        wr = 100 * sum(1 for r in rows if r[2]) / len(rows)
        tot = sum(r[3] for r in rows)
        print("    STRAT imb>|%.1f| buy-favored: n=%d WR=%.1f%% avg_e=%.3f EV=%+.3f total=%+.1f $/day=%+.2f"
              % (edge, len(rows), wr, stt.mean(r[1] for r in rows), tot/len(rows), tot, tot/days(rows)))
