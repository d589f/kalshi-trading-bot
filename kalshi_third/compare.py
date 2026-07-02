"""Head-to-head TOTAL PnL: live f6_wait270 vs the best new strategy (WF logistic p+hour, EV>0 gate).
Same engine/data as sweep.py. Compares on the identical OOS window (walk-forward folds 1..5) so it is
an honest 'how much money would each have made over the same period'. Idealized + live-haircut (/2.5)."""
import numpy as np, datetime as dt, math
import kalshi_third_bt as bt
from sklearn.linear_model import LogisticRegression

SEED=42; np.random.seed(SEED)
CI=3; EM=CI+1; FEE_K=0.0349; STAKE=100.0; NFOLDS=6; HAIRCUT=2.5

R=[]
for tk,m in bt.MK.items():
    if m["result"] not in ("yes","no"): continue
    o=bt.tparse(m["open_time"]); bo=bt.btc_at(o); be=bt.btc_at(o+dt.timedelta(minutes=EM))
    if bo is None or be is None: continue
    e=bt.ENTRY.get((tk,CI));
    if not e: continue
    price,ya,yb=e; delta=be-bo; up=delta>0
    ep=ya if up else ((1-yb) if yb is not None else None)
    if ep is None: continue
    sig=bt.sigma30(o+dt.timedelta(minutes=EM)); tau=(15-EM)/5.0
    snr=abs(delta)/(sig*be*max(tau,0.1)) if sig>0 else 0.0
    won=(up and m["result"]=="yes") or ((not up) and m["result"]=="no")
    R.append((o.timestamp(),abs(delta),bt.Phi(0.5*snr),o.hour,ep,1 if won else 0))
R.sort(key=lambda x:x[0]); a=np.array(R,float)
ts,absd,p,hour,ep,won=a[:,0],a[:,1],a[:,2],a[:,3],a[:,4],a[:,5].astype(bool)
N=len(a); fold=np.minimum(np.argsort(np.argsort(ts))*NFOLDS//N,NFOLDS-1)
PNL=np.where(won,STAKE/ep-STAKE-FEE_K*STAKE*(1-ep),-STAKE)
def d(t): return dt.datetime.utcfromtimestamp(t)
print(f"{N} markets | {d(ts.min()):%Y-%m-%d}..{d(ts.max()):%Y-%m-%d} | {NFOLDS} folds")

# --- f6 mask (delta>=20, band (0.50,0.92], p>=0.60) ---
f6=(absd>=20)&(ep>0.50)&(ep<=0.92)&(p>=0.60)

# --- new strategy: walk-forward logistic on [p,hour], EV>0 gate. Trades only folds 1..5 (fold0=warmup) ---
X=np.column_stack([p,hour]); new_sel=np.zeros(N,bool)
for f in range(1,NFOLDS):
    tr=fold<f; te=fold==f
    mu,sd=X[tr].mean(0),X[tr].std(0)+1e-9
    clf=LogisticRegression(max_iter=400).fit((X[tr]-mu)/sd,won[tr])
    pw=clf.predict_proba((X[te]-mu)/sd)[:,1]
    new_sel[te]=pw>ep[te]

def stats(mask):
    n=int(mask.sum()); pl=PNL[mask]
    return dict(n=n,total=float(pl.sum()),ev=float(pl.mean()) if n else 0,
                wr=100*float(won[mask].mean()) if n else 0)

oos=fold>=1                       # the fair comparison window (where the new strat operates)
print("\n=================  HEAD-TO-HEAD TOTAL PnL  (idealized $ / ~live $ = ÷2.5)  =================")
def line(name,s):
    print(f"  {name:34s} n={s['n']:5d}  EV ${s['ev']:+6.2f}  WR {s['wr']:4.1f}%   TOTAL ${s['total']:+8,.0f}  (~live ${s['total']/HAIRCUT:+8,.0f})")

print("\n--- FULL PERIOD (all 6 folds, 17.04–23.06) ---")
line("f6_wait270 (current live)", stats(f6))
print("\n--- SAME OOS WINDOW (folds 1–5, ~55 days) — the fair money comparison ---")
sf6=stats(f6&oos); snew=stats(new_sel&oos)
line("f6_wait270 (current)", sf6)
line("NEW: WF logistic p+hour EV-gate", snew)
extra=snew['total']-sf6['total']
print(f"\n  >> NEW makes ${extra:+,.0f} more idealized  (~${extra/HAIRCUT:+,.0f} live) over the same ~55 days")
print(f"  >> per-day: f6 ${sf6['total']/55/HAIRCUT:+.0f}/day live  vs  new ${snew['total']/55/HAIRCUT:+.0f}/day live")

print("\n--- per-fold TOTAL $ (idealized) ---")
print(f"  {'fold':>4} {'period':>21} {'f6 $':>9} {'new $':>9}")
for f in range(NFOLDS):
    fm=fold==f; per=f"{d(ts[fm].min()):%m-%d}..{d(ts[fm].max()):%m-%d}"
    f6t=PNL[f6&fm].sum(); nwt=PNL[new_sel&fm].sum() if f>=1 else float('nan')
    print(f"  {f:>4} {per:>21} {f6t:+9,.0f} {'' if f==0 else f'{nwt:+9,.0f}'}")

# cumulative equity (idealized)
import itertools
print("\n--- cumulative equity at end (idealized) ---")
print(f"  f6 (folds1-5): ${PNL[f6&oos].cumsum()[-1]:+,.0f}   new (folds1-5): ${PNL[new_sel&oos].cumsum()[-1]:+,.0f}")
