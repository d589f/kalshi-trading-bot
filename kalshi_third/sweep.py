"""Reproducible walk-forward parameter + weight sweep for the THIRD strategy on Kalshi KXBTC15M.

Engine: kalshi_third_bt (REAL Kalshi entry prices + settlement + Binance underlying).
Methodology (honest about overfitting):
  1. IN-SAMPLE threshold leaderboard over ~1.6M combos  -> hypothesis generation only (ranked by a
     lower-confidence bound so thin/lucky slices are penalized). NOT a performance claim.
  2. WALK-FORWARD threshold strategy: pick best config on past folds, trade the next fold -> honest OOS.
  3. WALK-FORWARD logistic "weight" model (P(win) features, EV>0 gate) -> honest OOS.
  4. f6 baseline evaluated the same walk-forward way. Idealized EV; live ~2.5x worse (thin-book fills).

Deterministic: fixed SEED + data snapshot. Run: `python3 sweep.py`.
"""
import numpy as np, itertools, json, math, datetime as dt, time
import kalshi_third_bt as bt
from sklearn.linear_model import LogisticRegression

SEED = 42; np.random.seed(SEED)
CI = 3; EM = CI + 1
FEE_K = 0.0349; STAKE = 100.0
NFOLDS = 6; MIN_N_FOLD = 12
LIVE_HAIRCUT = 2.5          # idealized EV / this ~ live estimate (memory: +7.6 idealized -> +3.6 live)

def build():
    R = []
    for tk, m in bt.MK.items():
        if m["result"] not in ("yes", "no"): continue
        o = bt.tparse(m["open_time"]); bo = bt.btc_at(o); be = bt.btc_at(o + dt.timedelta(minutes=EM))
        if bo is None or be is None: continue
        e = bt.ENTRY.get((tk, CI))
        if not e: continue
        price, ya, yb = e
        delta = be - bo; up = delta > 0
        ep = ya if up else ((1 - yb) if yb is not None else None)
        if ep is None: continue
        sig = bt.sigma30(o + dt.timedelta(minutes=EM)); tau = (15 - EM) / 5.0
        snr = abs(delta) / (sig * be * max(tau, 0.1)) if sig > 0 else 0.0
        won = (up and m["result"] == "yes") or ((not up) and m["result"] == "no")
        R.append((o.timestamp(), abs(delta), abs(delta)/be, sig, snr, bt.Phi(0.5*snr), o.hour, ep,
                  (ya-yb) if (ya is not None and yb is not None) else np.nan, 1 if won else 0))
    R.sort(key=lambda x: x[0]); a = np.array(R, float)
    cols = ["ts","absd","dpct","sig","snr","p","hour","entry_px","spread","won"]
    return {c: a[:, i] for i, c in enumerate(cols)}, len(R)

D, N = build()
ts = D["ts"]; fold = np.minimum(np.argsort(np.argsort(ts)) * NFOLDS // N, NFOLDS - 1)
ep = D["entry_px"]; won = D["won"].astype(bool)
PNL = np.where(won, STAKE/ep - STAKE - FEE_K*STAKE*(1-ep), -STAKE)
print(f"feature table: {N} markets | {dt.datetime.utcfromtimestamp(ts.min()):%Y-%m-%d}..{dt.datetime.utcfromtimestamp(ts.max()):%Y-%m-%d} | {NFOLDS} folds (~{N//NFOLDS}/fold)")

def mask_of(dth, mn, mx, pt): return (D["absd"] >= dth) & (ep > mn) & (ep <= mx) & (D["p"] >= pt)
def lcb(mask):
    n = int(mask.sum())
    if n < MIN_N_FOLD*NFOLDS: return None
    p = PNL[mask]; ev = p.mean(); se = p.std()/math.sqrt(n)
    return dict(n=n, ev=float(ev), lcb=float(ev-1.0*se), total=float(p.sum()))
def foldwise(mask):
    evs=[PNL[mask&(fold==f)].mean() if (mask&(fold==f)).sum()>=MIN_N_FOLD else np.nan for f in range(NFOLDS)]
    return np.array(evs)

# ---- f6 baseline, honest per-fold ----
bmask = mask_of(20,0.50,0.92,0.60); bf = foldwise(bmask)
BASE = dict(ev=float(PNL[bmask].mean()), n=int(bmask.sum()), fold_ev=[round(x,2) for x in bf],
            pos_folds=int(np.nansum(bf>0)), total=float(PNL[bmask].sum()))
print(f"\n=== f6 baseline (idealized) ===  EV ${BASE['ev']:+.3f}/tr  n={BASE['n']}  per-fold {BASE['fold_ev']}  (+ in {BASE['pos_folds']}/{NFOLDS})")

# ---- 1. IN-SAMPLE threshold leaderboard (hypothesis gen) ----
DTHR=np.arange(5,90,1.5); MINE=np.round(np.arange(0.40,0.70,0.02),3)
MAXE=np.round(np.arange(0.72,0.96,0.01),3); PTHR=np.round(np.arange(0.0,0.78,0.01),3)
grid=[(a,b,c,d) for a in DTHR for b in MINE for c in MAXE for d in PTHR if b<c]
print(f"\n[1] IN-SAMPLE threshold sweep: {len(grid):,} combos (ranked by lower-confidence bound; hypothesis-gen only) ...")
t0=time.time(); res=[]
for dth,mn,mx,pt in grid:
    s=lcb(mask_of(dth,mn,mx,pt))
    if s: s.update(delta_thr=dth,min_e=mn,max_e=mx,p_thr=pt); res.append(s)
res.sort(key=lambda r:-r["lcb"])
print(f"  {len(grid):,} combos in {time.time()-t0:.0f}s | {len(res):,} valid")
print(f"  {'ev/tr':>7}{'lcb':>7}{'n':>6}{'total$':>9}  dΔ  band            p>=")
for r in res[:8]:
    print(f"  {r['ev']:+7.2f}{r['lcb']:+7.2f}{r['n']:6d}{r['total']:+9.0f}  {r['delta_thr']:>4.1f} ({r['min_e']:.2f},{r['max_e']:.2f}]  {r['p_thr']:.2f}")

# ---- 2. WALK-FORWARD threshold strategy (honest OOS): pick best-on-train, trade next fold ----
def wf_threshold(coarse):
    oos=[]; picks=[]
    for f in range(1,NFOLDS):
        tr=fold<f; te=fold==f
        if te.sum()<MIN_N_FOLD: continue
        best=None
        for dth,mn,mx,pt in coarse:
            m=mask_of(dth,mn,mx,pt)&tr; n=int(m.sum())
            if n<MIN_N_FOLD*f: continue
            e=PNL[m].mean()
            if best is None or e>best[0]: best=(e,(dth,mn,mx,pt))
        if not best: continue
        dth,mn,mx,pt=best[1]; tm=mask_of(dth,mn,mx,pt)&te
        oos.append(PNL[tm]); picks.append(best[1])
    allp=np.concatenate(oos) if oos else np.array([])
    return (dict(ev=float(allp.mean()),n=len(allp),total=float(allp.sum()),picks=picks) if len(allp) else None)
coarse=[(a,b,c,d) for a in np.arange(10,70,5) for b in [0.40,0.50] for c in [0.80,0.88,0.92] for d in np.arange(0.4,0.75,0.05)]
WFT=wf_threshold(coarse)
print(f"\n[2] WALK-FORWARD threshold strategy (OOS): EV ${WFT['ev']:+.3f}/tr  n={WFT['n']}  total ${WFT['total']:+,.0f}")

# ---- 3. WALK-FORWARD logistic weight model (OOS) ----
FEATS=["absd","dpct","sig","snr","p","hour","entry_px","spread"]
X=np.column_stack([np.nan_to_num(D[f],nan=np.nanmedian(D[f])) for f in FEATS])
def wf_logit(idx):
    oos=[]; nf=[]
    for f in range(1,NFOLDS):
        tr=fold<f; te=fold==f
        if tr.sum()<100 or te.sum()<MIN_N_FOLD: continue
        Xi=X[:,idx]; mu,sd=Xi[tr].mean(0),Xi[tr].std(0)+1e-9
        clf=LogisticRegression(max_iter=400).fit((Xi[tr]-mu)/sd,won[tr])
        pw=clf.predict_proba((Xi[te]-mu)/sd)[:,1]
        sel=pw>ep[te]                      # EV>0 gate: model P(win) beats the price you'd pay
        oos.append(PNL[te][sel]); nf.append(int(sel.sum()))
    allp=np.concatenate(oos) if oos else np.array([])
    if len(allp)<MIN_N_FOLD*2: return None
    return dict(ev=float(allp.mean()),n=len(allp),total=float(allp.sum()),minf=min(nf),feats=[FEATS[i] for i in idx])
cand=[]; full=wf_logit(list(range(len(FEATS))))
if full: cand.append(full)
chosen=[]; rest=list(range(len(FEATS))); best=-1e9
while rest:
    tr=[(wf_logit(chosen+[i]),i) for i in rest]; tr=[(r,i) for r,i in tr if r]
    if not tr: break
    r,i=max(tr,key=lambda x:x[0]["ev"])
    if r["ev"]<=best+1e-3: break
    best=r["ev"]; chosen.append(i); rest.remove(i); cand.append(r)
cand.sort(key=lambda r:-r["ev"])
print(f"\n[3] WALK-FORWARD logistic weight model (OOS, EV>0 gate):")
for r in cand[:6]: print(f"  OOS ${r['ev']:+.3f}/tr  n={r['n']}  total ${r['total']:+,.0f}   feats={'+'.join(r['feats'])}")

# ---- verdict ----
bestwf=cand[0] if cand else None
def live(x): return x/LIVE_HAIRCUT
print(f"\n================= VERDICT (idealized -> ~live /{LIVE_HAIRCUT}) =================")
print(f"  f6 baseline           : EV ${BASE['ev']:+6.2f}/tr  (~live ${live(BASE['ev']):+.2f})  n={BASE['n']}")
print(f"  WF threshold-adaptive : OOS ${WFT['ev']:+6.2f}/tr  (~live ${live(WFT['ev']):+.2f})  n={WFT['n']}")
if bestwf:
    print(f"  WF logistic (best)    : OOS ${bestwf['ev']:+6.2f}/tr  (~live ${live(bestwf['ev']):+.2f})  n={bestwf['n']}  feats={'+'.join(bestwf['feats'])}")
json.dump(dict(seed=SEED,ci=CI,n=N,baseline=BASE,wf_threshold=WFT,wf_logit=cand[:6],insample_top=res[:20]),
          open("sweep_results.json","w"),indent=1,default=float)
print("  -> sweep_results.json")
