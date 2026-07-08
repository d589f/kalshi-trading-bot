"""Momentum out-of-sample re-validation sweep (family: momentum_oos).

Grid: ci {3,4,5} x delta_thr {10,15,20,25,30,40,50,75} x max_entry {0.75,0.82,0.88,0.92}
      x p-gate {off, (kappa,p_thr) in {0.4,0.5}x{0.60,0.65}}  -> 480 configs
Plus 2 named analogs (F1-live: sigma window 10; f6: in-grid config).

Honesty: signal delta = w.deltas[ci+1] (known at entry candle ci close); entry at real
ask via bt_core.entry_px; sigma computed at entry time only; every window iterated,
missing-data windows counted as skips. Headline = TEST period (>= 2026-06-24).
"""
import sys, os, math, json, datetime as dt
sys.path.insert(0, 'C:/Users/User/Desktop/kalshi-trading-bot/kalshi_third')
import bt_core

print("loading windows...", flush=True)
ws = bt_core.windows()
print(f"windows: {len(ws)} | span {ws[0].t:%Y-%m-%d}..{ws[-1].t:%Y-%m-%d}", flush=True)

CIS = (3, 4, 5)
THRS = (10, 15, 20, 25, 30, 40, 50, 75)
MAXES = (0.75, 0.82, 0.88, 0.92)
GATES = [None, (0.4, 0.60), (0.4, 0.65), (0.5, 0.60), (0.5, 0.65)]

def sigma10(t):
    """10-min pre-entry sigma. bt_core.sigma_n(t,10) is ALWAYS None (needs >10 of
    max 10 returns), so compute locally: >=8 of the 10 1-min log returns before t."""
    P = bt_core.P
    mi = int(t.timestamp()) // 60
    rr = [math.log(P[k + 1] / P[k]) for k in range(mi - 10, mi)
          if k in P and (k + 1) in P and P[k] > 0]
    import statistics as _st
    return _st.pstdev(rr) if len(rr) >= 8 else None

# ---------- precompute per (window, ci): signal, entry, sigma, btc ----------
print("precomputing features...", flush=True)
pre = []  # (w, {ci: (d, side, entry, sigma30, sigma10_or_None, btc_at_entry)})
cov = {ci: 0 for ci in CIS}
for w in ws:
    row = {}
    bo = bt_core.btc_at(w.t)
    for ci in CIS:
        em = ci + 1
        d = w.deltas.get(em)
        if d is None or ci not in w.entries:
            row[ci] = None
            continue
        side = 'UP' if d > 0 else 'DOWN'
        e = bt_core.entry_px(w, ci, side)
        if e is None:
            row[ci] = None
            continue
        t_e = w.t + dt.timedelta(minutes=em)
        s30 = bt_core.sigma_n(t_e, 30)
        s10 = sigma10(t_e) if ci == 3 else None
        btc_e = (bo + d) if bo is not None else None
        row[ci] = (d, side, e, s30, s10, btc_e)
        cov[ci] += 1
    pre.append((w, row))
print("entry coverage by ci:", cov, flush=True)


def run_cfg(ci, thr, maxe, gate, sigma_win=30):
    """gate = None or (kappa, p_thr). Returns (trades, skips_missing, n_signal)."""
    em = ci + 1
    tleft = max((15 - em) / 5, 0.1)
    trades, skips, n_sig = [], 0, 0
    for w, row in pre:
        r = row[ci]
        if r is None:
            skips += 1
            continue
        d, side, e, s30, s10, btc_e = r
        if abs(d) < thr:
            continue
        n_sig += 1
        if gate is not None:
            sig = s30 if sigma_win == 30 else s10
            if sig is None or not btc_e:
                skips += 1
                continue
            kappa, pthr = gate
            p = bt_core.Phi(kappa * abs(d) / (sig * btc_e * tleft))
            if p < pthr:
                continue
        if e > maxe:
            continue
        won = w.won_up if side == 'UP' else (not w.won_up)
        trades.append((w.t, e, won))
    return trades, skips, n_sig


print("sweeping grid...", flush=True)
results = []
for ci in CIS:
    for thr in THRS:
        for maxe in MAXES:
            for gate in GATES:
                trades, skips, n_sig = run_cfg(ci, thr, maxe, gate)
                ev = bt_core.evaluate(trades)
                params = dict(ci=ci, delta_thr=thr, max_entry=maxe,
                              gate=('off' if gate is None else
                                    dict(kappa=gate[0], p_thr=gate[1], sigma_win=30)))
                results.append(dict(params=params, ev=ev, skips=skips, n_signal=n_sig))
print(f"grid configs evaluated: {len(results)}", flush=True)

# ---------- named analogs ----------
f1_trades, f1_sk, _ = run_cfg(3, 50, 0.92, (0.4, 0.65), sigma_win=10)
f1_ev = bt_core.evaluate(f1_trades, "F1-live analog")
f6_trades, f6_sk, _ = run_cfg(4, 20, 0.92, (0.5, 0.60), sigma_win=30)
f6_ev = bt_core.evaluate(f6_trades, "f6 analog")


def avg_entry(trades, period):
    sel = [e for t, e, _ in trades
           if (t < bt_core.TRAIN_END) == (period == 'train')]
    return round(sum(sel) / len(sel), 3) if sel else None


def weekly(trades):
    """Per-ISO-week PnL for TEST-period trades (regime-consistency check)."""
    wk = {}
    for t, e, won in trades:
        if t < bt_core.TRAIN_END:
            continue
        p = bt_core.pnl5(e, won)
        if p is None:
            continue
        k = f"{t.isocalendar()[0]}-W{t.isocalendar()[1]:02d}"
        a = wk.setdefault(k, [0, 0.0])
        a[0] += 1
        a[1] += p
    return {k: (v[0], round(v[1], 2)) for k, v in sorted(wk.items())}


def fmt(s):
    if s is None:
        return "n=0"
    return (f"n={s['n']} ev={s['ev']:+.4f} perday={s['perday']:+.2f} "
            f"total={s['total']:+.2f} wr={s['wr']}%")


def brief(s):
    if s is None:
        return None
    return dict(n=s['n'], ev=s['ev'], perday=s['perday'], total=s['total'], wr=s['wr'])


print("\n=== NAMED ANALOGS (report even if negative) ===")
print(f"F1-live (ci=3 thr=50 k=0.4 p>=0.65 sig10 max<=0.92, skips={f1_sk}):")
print(f"  train {fmt(f1_ev['train'])} avg_e={avg_entry(f1_trades,'train')}")
print(f"  test  {fmt(f1_ev['test'])} avg_e={avg_entry(f1_trades,'test')}")
print(f"  test weekly: {weekly(f1_trades)}")
print(f"f6 (ci=4 thr=20 k=0.5 p>=0.60 sig30 max<=0.92, skips={f6_sk}):")
print(f"  train {fmt(f6_ev['train'])} avg_e={avg_entry(f6_trades,'train')}")
print(f"  test  {fmt(f6_ev['test'])} avg_e={avg_entry(f6_trades,'test')}")
print(f"  test weekly: {weekly(f6_trades)}")

# ---------- landscape stats ----------
def tst(r): return r['ev']['test']
def trn(r): return r['ev']['train']

valid = [r for r in results if tst(r) and tst(r)['n'] >= 40]
pos_test = [r for r in valid if tst(r)['ev'] > 0]
pos_both = [r for r in pos_test if trn(r) and trn(r)['ev'] > 0]
small_pos = [r for r in results if tst(r) and tst(r)['n'] < 40 and tst(r)['ev'] > 0]
print(f"\n=== LANDSCAPE ({len(results)} grid configs) ===")
print(f"test n>=40: {len(valid)} | test ev>0 (n>=40): {len(pos_test)} "
      f"| positive train AND test: {len(pos_both)} | test ev>0 but n<40 (insufficient): {len(small_pos)}")

# honest check: select on TRAIN, look at TEST
by_train = sorted([r for r in results if trn(r) and trn(r)['n'] >= 100],
                  key=lambda r: trn(r)['perday'], reverse=True)
print("\n=== TOP 10 BY TRAIN perday -> their TEST (train-selection honesty check) ===")
for r in by_train[:10]:
    p = r['params']
    print(f"ci={p['ci']} thr={p['delta_thr']} max={p['max_entry']} gate={p['gate']}")
    print(f"  train {fmt(trn(r))}\n  test  {fmt(tst(r))}")

# deliverable ranking: TEST perday, test ev>0, test n>=40
top = sorted(pos_test, key=lambda r: tst(r)['perday'], reverse=True)
print("\n=== TOP BY TEST perday (test ev>0, test n>=40) ===")
top_detail = []
for r in top[:5]:
    p = r['params']
    g = p['gate']
    gate = None if g == 'off' else (g['kappa'], g['p_thr'])
    trades, _, _ = run_cfg(p['ci'], p['delta_thr'], p['max_entry'], gate)
    ae_tr, ae_te = avg_entry(trades, 'train'), avg_entry(trades, 'test')
    wk = weekly(trades)
    print(f"ci={p['ci']} thr={p['delta_thr']} max={p['max_entry']} gate={p['gate']} "
          f"skips={r['skips']} signals={r['n_signal']}")
    print(f"  train {fmt(trn(r))} avg_e={ae_tr}\n  test  {fmt(tst(r))} avg_e={ae_te}")
    print(f"  test weekly: {wk}")
    top_detail.append(dict(params=p, train=brief(trn(r)), test=brief(tst(r)),
                           avg_entry_train=ae_tr, avg_entry_test=ae_te,
                           test_weekly=wk, skips=r['skips'], n_signal=r['n_signal']))

out = dict(
    configs_tested=len(results) + 2,
    f1=dict(train=brief(f1_ev['train']), test=brief(f1_ev['test']),
            avg_e_test=avg_entry(f1_trades, 'test'), test_weekly=weekly(f1_trades)),
    f6=dict(train=brief(f6_ev['train']), test=brief(f6_ev['test']),
            avg_e_test=avg_entry(f6_trades, 'test'), test_weekly=weekly(f6_trades)),
    n_valid=len(valid), n_pos_test=len(pos_test), n_pos_both=len(pos_both),
    top=top_detail,
    by_train=[dict(params=r['params'], train=brief(trn(r)), test=brief(tst(r)))
              for r in by_train[:5]],
)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'momentum_oos_results.json'), 'w') as f:
    json.dump(out, f, indent=1, default=str)
print("\nJSON written next to script.")
