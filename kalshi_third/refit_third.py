"""Refit the third signal on the RAW feature log (PROD ticks+signals), not the paper table.
The decayed formula p_model is broken (AUC~0.53). We refit P(win) on the raw features the
formula needs (|delta|, sigma, delta_from_open, tau, + OFI/snr) and gate on my_P > market_entry.

Data: third_refit.csv = s_third resolved trades joined to ticks.sigma + signals.{snr,ofi,btc}
(2026-05-25..06-22, the decay window; ~4300 fully-featured trades).

Walk-forward: expanding train, retrain each fold, test next ~5-day block. Honest OOS only.
"""
import csv, datetime as dt, os
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

D = os.path.dirname(os.path.abspath(__file__))
FEATS = ['absd', 'delta', 'dfo', 'sigma', 'tau', 'snr', 'ofi1', 'ofi5']

def fnum(x):
    try: return float(x)
    except: return None

def load():
    R = []
    for r in csv.DictReader(open(f"{D}/third_refit.csv", encoding="utf-8")):
        d = {k: fnum(r[k]) for k in ('entry_price', 'delta', 'pnl', 'sigma', 'dfo', 'snr', 'ofi1', 'ofi5', 'p_model')}
        if any(d[k] is None for k in ('entry_price', 'delta', 'pnl', 'sigma', 'dfo')): continue
        try:
            ets = dt.datetime.fromisoformat(r['ts']); ws = dt.datetime.fromisoformat(r['window_start'])
            d['t'] = dt.datetime.fromisoformat(r['resolved_ts'])
        except Exception: continue
        d['tau'] = (ws + dt.timedelta(seconds=300) - ets).total_seconds()
        d['win'] = 1 if d['pnl'] > 0 else 0; d['absd'] = abs(d['delta'])
        d['entry'] = d['entry_price']
        for k in ('snr', 'ofi1', 'ofi5'): d[k] = d[k] or 0.0
        R.append(d)
    R.sort(key=lambda x: x['t']); return R

def fit_predict(tr, te, feats=FEATS):
    Xtr = np.array([[x[k] for k in feats] for x in tr]); ytr = np.array([x['win'] for x in tr])
    mu = Xtr.mean(0); sd = Xtr.std(0) + 1e-9
    m = LogisticRegression(max_iter=1000).fit((Xtr - mu) / sd, ytr)
    Xte = np.array([[x[k] for k in feats] for x in te])
    return m.predict_proba((Xte - mu) / sd)[:, 1], dict(zip(feats, m.coef_[0]))

def ev(rows):
    n = len(rows); return (sum(x['pnl'] for x in rows) / n if n else 0, n, 100 * sum(x['win'] for x in rows) / n if n else 0)

if __name__ == "__main__":
    R = load()
    print(f"refit set n={len(R)}  span {R[0]['t']:%m-%d}..{R[-1]['t']:%m-%d}")
    edges = [dt.datetime(2026, m, d, tzinfo=dt.timezone.utc) for m, d in [(6, 2), (6, 8), (6, 13), (6, 18), (6, 23)]]
    folds = []; pooled = []
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        tr = [x for x in R if x['t'] < a]; te = [x for x in R if a <= x['t'] < b]
        if len(tr) < 300 or len(te) < 30: continue
        P, coef = fit_predict(tr, te)
        for j, x in enumerate(te): x['pfit'] = P[j]
        g = [x for x in te if x['pfit'] > x['entry']]; pooled += g
        e80 = [x for x in te if x['entry'] <= 0.80]
        folds.append((f"{a:%m-%d}..{b:%m-%d}", ev(te)[0], ev(g)[0], len(g), ev(e80)[0], roc_auc_score([x['win'] for x in te], P)))
    print(f"\n{'fold':>14}{'base':>8}{'pfit>entry':>12}{'n':>6}{'entry<=.80':>12}{'AUC':>7}")
    for f in folds: print(f"{f[0]:>14}{f[1]:>+8.2f}{f[2]:>+12.2f}{f[3]:>6}{f[4]:>+12.2f}{f[5]:>7.3f}")
    pe = ev(pooled); pe80 = ev([x for x in pooled if x['entry'] > 0.80])
    print(f"\nPOOLED pfit>entry: EV {pe[0]:+.3f} n={pe[1]} WR {pe[2]:.0f}%  |  on entry>0.80: EV {pe80[0]:+.3f} n={pe80[1]}")

    # ---- chart ----
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(14, 5.6))
    labs = [f[0] for f in folds]; x = np.arange(len(folds)); w = 0.27
    a1.bar(x - w, [f[1] for f in folds], w, label="база (no gate)", color="#999")
    a1.bar(x, [f[2] for f in folds], w, label="РЕФИТ pfit>entry", color="#1f77b4")
    a1.bar(x + w, [f[4] for f in folds], w, label="фильтр entry<=0.80", color="#ff7f0e")
    a1.axhline(0, color="#333"); a1.set_xticks(x); a1.set_xticklabels(labs, fontsize=8)
    a1.set_ylabel("EV $/сделку"); a1.legend(fontsize=8)
    a1.set_title(f"Walk-forward OOS (переобучение каждый фолд)\nрефит-гейт + во ВСЕХ окнах; пул {pe[0]:+.2f} (n={pe[1]})", fontweight="bold")
    a1.grid(alpha=0.2, axis="y")
    # AUC panel
    teall = [x for x in R if x.get('pfit') is not None]
    aucs = {"p_model\n(сломан)": roc_auc_score([x['win'] for x in pooled], [x['p_model'] for x in pooled]) if pooled else 0,
            "РЕФИТ pfit": roc_auc_score([x['win'] for x in pooled], [x['pfit'] for x in pooled]) if pooled else 0,
            "entry\n(рынок)": roc_auc_score([x['win'] for x in pooled], [x['entry'] for x in pooled]) if pooled else 0}
    a2.bar(list(aucs), list(aucs.values()), color=["#d62728", "#1f77b4", "#2ca02c"])
    a2.axhline(0.5, color="#333", ls="--", label="0.5 = монетка")
    for i, v in enumerate(aucs.values()): a2.text(i, v + 0.005, f"{v:.3f}", ha="center", fontweight="bold")
    a2.set_ylim(0.45, 0.70); a2.set_ylabel("AUC (предсказание исхода)")
    a2.set_title("Кто предсказывает исход\nрынок (entry) всё ещё лучший единичный предиктор", fontweight="bold")
    a2.legend(fontsize=8); a2.grid(alpha=0.2, axis="y")
    fig.suptitle("Рефит third на сырых фичах (PROD ticks+signals) — единственный выживший OOS edge", fontweight="bold")
    fig.tight_layout(); p = f"{D}/refit_third.png"; fig.savefig(p, dpi=140, bbox_inches="tight"); plt.close(fig)
    print("saved", p)
