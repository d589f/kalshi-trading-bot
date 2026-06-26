"""Equity-curve: REFIT-gated third (pfit>entry, walk-forward retrain) vs BASELINE third.
Same universe (featured trades 2026-05-25..06-22). Shows what the refit would have made."""
import csv, datetime as dt, os
import numpy as np
from sklearn.linear_model import LogisticRegression
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
import matplotlib.dates as mdates

D = os.path.dirname(os.path.abspath(__file__)); FEATS = ['absd', 'delta', 'dfo', 'sigma', 'tau', 'snr', 'ofi1', 'ofi5']
def fnum(x):
    try: return float(x)
    except: return None
R = []
for r in csv.DictReader(open(f"{D}/third_refit.csv", encoding="utf-8")):
    d = {k: fnum(r[k]) for k in ('entry_price', 'delta', 'pnl', 'sigma', 'dfo', 'snr', 'ofi1', 'ofi5', 'p_model')}
    if any(d[k] is None for k in ('entry_price', 'delta', 'pnl', 'sigma', 'dfo')): continue
    try:
        ets = dt.datetime.fromisoformat(r['ts']); ws = dt.datetime.fromisoformat(r['window_start']); d['t'] = dt.datetime.fromisoformat(r['resolved_ts'])
    except Exception: continue
    d['tau'] = (ws + dt.timedelta(seconds=300) - ets).total_seconds(); d['win'] = 1 if d['pnl'] > 0 else 0
    d['absd'] = abs(d['delta']); d['entry'] = d['entry_price']
    for k in ('snr', 'ofi1', 'ofi5'): d[k] = d[k] or 0.0
    R.append(d)
R.sort(key=lambda x: x['t'])

# walk-forward: retrain each fold, tag pfit on test trades
edges = [dt.datetime(2026, m, dd, tzinfo=dt.timezone.utc) for m, dd in [(6, 2), (6, 8), (6, 13), (6, 18), (6, 23)]]
tested = []
for i in range(len(edges) - 1):
    a, b = edges[i], edges[i + 1]
    tr = [x for x in R if x['t'] < a]; te = [x for x in R if a <= x['t'] < b]
    if len(tr) < 300 or not te: continue
    Xtr = np.array([[x[k] for k in FEATS] for x in tr]); ytr = np.array([x['win'] for x in tr])
    mu = Xtr.mean(0); sd = Xtr.std(0) + 1e-9
    m = LogisticRegression(max_iter=1000).fit((Xtr - mu) / sd, ytr)
    P = m.predict_proba((np.array([[x[k] for k in FEATS] for x in te]) - mu) / sd)[:, 1]
    for j, x in enumerate(te): x['pfit'] = P[j]; tested.append(x)
tested.sort(key=lambda x: x['t'])

# equity curves over the OOS (walk-forward) span — both start at 0
base_t, base_c, ref_t, ref_c = [], [], [], []
cb = cr = 0.0; nb = nr = 0; wr_b = wr_r = 0
for x in tested:
    cb += x['pnl']; nb += 1; wr_b += x['win']; base_t.append(x['t']); base_c.append(cb)
    if x['pfit'] > x['entry']:
        cr += x['pnl']; nr += 1; wr_r += x['win']; ref_t.append(x['t']); ref_c.append(cr)

fig, ax = plt.subplots(figsize=(13, 6.2))
ax.plot(base_t, base_c, color="#999", lw=2.0, label=f"БАЗОВЫЙ third (все сделки): {cb:+,.0f}$  n={nb}, WR {100*wr_b/nb:.0f}%")
ax.plot(ref_t, ref_c, color="#1f77b4", lw=2.4, label=f"РЕФИТ pfit>entry (walk-forward): {cr:+,.0f}$  n={nr}, WR {100*wr_r/nr:.0f}%")
ax.axhline(0, color="#333", lw=0.8)
ax.set_ylabel("кумулятивный PnL, $  (обе с нуля, общее OOS-окно)")
ax.set_title("PnL: РЕФИТ-third (pfit>entry, переобучение каждый фолд) vs БАЗОВЫЙ third\nОдин универсум, out-of-sample (PROD ticks+signals, 06-02..06-22)", fontweight="bold")
ax.legend(fontsize=10, loc="upper left"); ax.grid(alpha=0.25)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
for lab in ax.get_xticklabels(): lab.set_rotation(30)
fig.tight_layout(); p = f"{D}/refit_vs_third_pnl.png"; fig.savefig(p, dpi=140, bbox_inches="tight"); plt.close(fig)
print("saved", p)
print(f"baseline {cb:+.0f}$ ({nb} tr) | refit {cr:+.0f}$ ({nr} tr) | refit EV {cr/nr:+.3f}/tr vs base {cb/nb:+.3f}/tr")
