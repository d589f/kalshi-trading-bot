"""Step 1: fold in FRESH data and re-run head-to-head + a genuine forward test.
 - Dataset A (Apr17-Jun23): kalshi_third_bt engine (Binance features @ci=3 + real Kalshi entry CSV + result).
 - FRESH (Jun24-30): prod kalshi_15m signals (entry_price + delta + p_model @~entry) JOIN Predexon result.
Reports: (1) combined-period walk-forward, (2) FORWARD test = train logit on A, apply on FRESH (true OOS).
Idealized $; ~live = /2.5 (haircut validated on f6: +7.40 idealized -> +2.96 ~ actual +2.74)."""
import numpy as np, csv, datetime as dt, math
import kalshi_third_bt as bt
from sklearn.linear_model import LogisticRegression
SEED=42; np.random.seed(SEED); CI=3; EM=CI+1; FEE_K=0.0349; STAKE=100.0; HAIRCUT=2.5
def pnl(ep,won): return (STAKE/ep-STAKE-FEE_K*STAKE*(1-ep)) if won else -STAKE

# ---- dataset A (clean ci=3) ----
A=[]
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
    A.append((o.timestamp(),abs(delta),bt.Phi(0.5*snr),o.hour,ep,1 if won else 0))

# ---- FRESH from prod signals JOIN Predexon result ----
DD="sweep_data/"
res={r["ticker"]:r["result"] for r in csv.DictReader(open(DD+"fresh_markets.csv"))}
def T(s): return dt.datetime.fromisoformat(s)
best={}   # window -> nearest-270s signal row
for r in csv.DictReader(open(DD+"k_signals.csv")):
    w=r["market_slug"]
    if w not in res or not r.get("entry_price"): continue
    if not ("2026-06-23"< r["window_start"][:10] <= "2026-06-30"): continue
    try: sec=(T(r["ts"])-T(r["window_start"])).total_seconds(); d=float(r["delta"]); ep=float(r["entry_price"]); p=float(r["p_model"])
    except: continue
    if not (0<ep<1): continue
    if w not in best or abs(sec-270)<best[w][0]: best[w]=(abs(sec-270),r,d,ep,p)
F=[]
for w,(_,r,d,ep,p) in best.items():
    up=d>0; won=(up and res[w]=="yes") or ((not up) and res[w]=="no")
    F.append((T(r["window_start"]).timestamp(),abs(d),p,T(r["window_start"]).hour,ep,1 if won else 0))

def arr(L):
    a=np.array(sorted(L),float); return dict(ts=a[:,0],absd=a[:,1],p=a[:,2],hour=a[:,3],ep=a[:,4],won=a[:,5].astype(bool))
Ad=arr(A); Fd=arr(F)
print(f"dataset A: {len(A)} ({dt.datetime.utcfromtimestamp(Ad['ts'].min()):%m-%d}..{dt.datetime.utcfromtimestamp(Ad['ts'].max()):%m-%d})  |  FRESH: {len(F)} ({dt.datetime.utcfromtimestamp(Fd['ts'].min()):%m-%d}..{dt.datetime.utcfromtimestamp(Fd['ts'].max()):%m-%d})")

def f6mask(D): return (D["absd"]>=20)&(D["ep"]>0.50)&(D["ep"]<=0.92)&(D["p"]>=0.60)
def summ(D,mask):
    n=int(mask.sum()); pl=np.array([pnl(D["ep"][i],D["won"][i]) for i in np.where(mask)[0]])
    return dict(n=n,total=float(pl.sum()) if n else 0,ev=float(pl.mean()) if n else 0,wr=100*float(D["won"][mask].mean()) if n else 0)

# ---- FORWARD TEST: train logit [p,hour] on A, apply on FRESH (EV>0 gate) ----
Xtr=np.column_stack([Ad["p"],Ad["hour"]]); mu,sd=Xtr.mean(0),Xtr.std(0)+1e-9
clf=LogisticRegression(max_iter=400).fit((Xtr-mu)/sd,Ad["won"])
Xte=np.column_stack([Fd["p"],Fd["hour"]]); pw=clf.predict_proba((Xte-mu)/sd)[:,1]
new_fresh=pw>Fd["ep"]
f6_fresh=f6mask(Fd)
sf6=summ(Fd,f6_fresh); snew=summ(Fd,new_fresh)

def line(nm,s): print(f"  {nm:32s} n={s['n']:4d} EV ${s['ev']:+6.2f} WR {s['wr']:4.1f}%  TOTAL ${s['total']:+7,.0f} (~live ${s['total']/HAIRCUT:+6,.0f})")
print("\n===== FORWARD TEST on FRESH data (train on A, apply on Jun24-30) — the real 'does it still work' =====")
line("f6_wait270 (current)",sf6)
line("NEW p+hour EV-gate",snew)
print(f"  >> on fresh {len(F)} windows: NEW ${snew['total']-sf6['total']:+,.0f} vs f6 (idealized), ~${(snew['total']-sf6['total'])/HAIRCUT:+,.0f} live")

# ---- combined full-period baselines (context) ----
print("\n===== FULL PERIOD combined (A + fresh), f6 baseline for context =====")
Cd=arr(A+F); line("f6 (all data Apr17-Jun30)",summ(Cd,f6mask(Cd)))
