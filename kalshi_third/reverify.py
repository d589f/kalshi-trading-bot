"""ADVERSARIAL re-verification of the f6 vs EV-gate finding. Tries to BREAK the prior conclusion.
Tests: pure time holdout (not walk-forward), calibration, entry-price buckets, slippage stress,
f6 decay audit, reconciliation of backtest(+7.40) vs prod-paper(+2.74). Saves a PnL chart.
Data: dataset A = kalshi_third_bt engine (clean ci=3 entry). Fresh = k_trades (real prod paper records).
"""
import numpy as np, csv, datetime as dt, math, json
import kalshi_third_bt as bt
from sklearn.linear_model import LogisticRegression
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

np.random.seed(42); CI=3; EM=CI+1; FEE_K=0.0349; STAKE=100.0
def pnl(ep,won,slip=0.0):
    eff=min(ep+slip,0.99)
    return (STAKE/eff-STAKE-FEE_K*STAKE*(1-eff)) if won else -STAKE

# ---------- build clean dataset A ----------
A=[]
for tk,m in bt.MK.items():
    if m["result"] not in ("yes","no"): continue
    o=bt.tparse(m["open_time"]); bo=bt.btc_at(o); be=bt.btc_at(o+dt.timedelta(minutes=EM))
    if bo is None or be is None: continue
    e=bt.ENTRY.get((tk,CI))
    if not e: continue
    price,ya,yb=e; delta=be-bo; up=delta>0
    ep=ya if up else ((1-yb) if yb is not None else None)
    if ep is None: continue
    sig=bt.sigma30(o+dt.timedelta(minutes=EM)); tau=(15-EM)/5.0
    snr=abs(delta)/(sig*be*max(tau,0.1)) if sig>0 else 0.0
    won=(up and m["result"]=="yes") or ((not up) and m["result"]=="no")
    A.append(dict(ts=o.timestamp(), o=o, absd=abs(delta), p=bt.Phi(0.5*snr), hour=o.hour, ep=ep, won=1 if won else 0))
A.sort(key=lambda r:r["ts"])
ts=np.array([r["ts"] for r in A]); absd=np.array([r["absd"] for r in A]); P=np.array([r["p"] for r in A])
hour=np.array([r["hour"] for r in A]); EP=np.array([r["ep"] for r in A]); WON=np.array([r["won"] for r in A]).astype(bool)
N=len(A); print(f"dataset A: {N} markets  {A[0]['o']:%Y-%m-%d}..{A[-1]['o']:%Y-%m-%d}")
def f6mask(): return (absd>=20)&(EP>0.50)&(EP<=0.92)&(P>=0.60)

# ================= PART 1: f6 AUDIT =================
print("\n"+"="*70+"\n PART 1 — f6 AUDIT (what's actually going on)\n"+"="*70)
f6=f6mask()
# weekly decay
wk={}
for i in np.where(f6)[0]:
    k=A[i]["o"].strftime("%Y-W%W"); wk.setdefault(k,[]).append(pnl(EP[i],WON[i]))
print("  f6 EV/trade by week (decay check):")
for k in sorted(wk):
    v=wk[k]; print(f"    {k}: n={len(v):4d} EV ${np.mean(v):+6.2f}")
# WR & EV by entry-price bucket
print("  f6 by entry-price bucket:")
for lo,hi in [(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.92)]:
    mm=f6&(EP>lo)&(EP<=hi); n=mm.sum()
    if n: print(f"    ep ({lo},{hi}]: n={n:4d} WR {100*WON[mm].mean():4.1f}% EV ${np.mean([pnl(EP[i],WON[i]) for i in np.where(mm)[0]]):+6.2f}")

# ================= PART 2: PURE TIME HOLDOUT (train 60% / test last 40%) =================
print("\n"+"="*70+"\n PART 2 — PURE HOLDOUT: train first 60%, test LAST 40% (hardest honest test)\n"+"="*70)
cut=int(N*0.6); tr=np.arange(N)<cut; te=np.arange(N)>=cut
print(f"  train {tr.sum()} ({A[0]['o']:%m-%d}..{A[cut-1]['o']:%m-%d})  |  test {te.sum()} ({A[cut]['o']:%m-%d}..{A[-1]['o']:%m-%d})")
def fit_gate(feat_cols):
    X=np.column_stack([{"p":P,"hour":hour,"absd":absd,"ep":EP}[c] for c in feat_cols])
    mu,sd=X[tr].mean(0),X[tr].std(0)+1e-9
    clf=LogisticRegression(max_iter=500).fit((X[tr]-mu)/sd,WON[tr])
    pw=np.zeros(N); pw[te]=clf.predict_proba((X[te]-mu)/sd)[:,1]
    return pw
pw=fit_gate(["p","hour"])
def summ(mask,slip=0.0):
    idx=np.where(mask)[0]; pl=np.array([pnl(EP[i],WON[i],slip) for i in idx])
    return dict(n=len(idx),ev=float(pl.mean()) if len(idx) else 0,tot=float(pl.sum()),
                wr=100*float(WON[mask].mean()) if len(idx) else 0)
f6_te=f6&te; ev_te=(pw>EP)&te
for s in [0.0,0.02,0.04]:
    a=summ(f6_te,s); b=summ(ev_te,s)
    print(f"  slip +{s:.2f}:  f6 n={a['n']} EV ${a['ev']:+6.2f} WR {a['wr']:.0f}% tot ${a['tot']:+7,.0f}   |   EV-gate n={b['n']} EV ${b['ev']:+6.2f} WR {b['wr']:.0f}% tot ${b['tot']:+7,.0f}")

# ================= PART 3: calibration + price-bucket of EV-gate =================
print("\n"+"="*70+"\n PART 3 — is EV-gate edge a cheap-contract / calibration artifact?\n"+"="*70)
idx=np.where(ev_te)[0]
print("  EV-gate trades by entry-price bucket (test set):")
for lo,hi in [(0.0,0.4),(0.4,0.5),(0.5,0.6),(0.6,0.75),(0.75,1.0)]:
    b=ev_te&(EP>lo)&(EP<=hi); n=b.sum()
    if n: print(f"    ep ({lo},{hi}]: n={n:4d} WR {100*WON[b].mean():4.1f}% EV ${summ(b)['ev']:+6.2f}")
print("  calibration (predicted P(win) vs realized, test set, EV-gate universe):")
for lo,hi in [(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,1.0)]:
    b=te&(pw>lo)&(pw<=hi); n=b.sum()
    if n: print(f"    pred ({lo},{hi}]: n={n:4d} predP {pw[b].mean():.3f} realized {WON[b].mean():.3f}")

# ================= PART 4: FRESH real prod data (k_trades) =================
print("\n"+"="*70+"\n PART 4 — REAL prod-paper records (k_trades, Jun16-Jul2): what actually happened\n"+"="*70)
def rows(f):
    import os; p="sweep_data/"+f
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []
kt=rows("k_trades.csv")
from collections import defaultdict
# f6 weekly on REAL prod data
f6r=[r for r in kt if r["session_id"]=="f6_wait270"]
wkr=defaultdict(list)
for r in f6r:
    try: wkr[dt.datetime.fromisoformat(r["window_start"]).strftime("%Y-W%W")].append(float(r["pnl"]))
    except: pass
print("  REAL f6 prod-paper EV/trade by week:")
for k in sorted(wkr): v=wkr[k]; print(f"    {k}: n={len(v):4d} EV ${np.mean(v):+6.2f}")

# ================= CHART =================
fig,ax=plt.subplots(1,2,figsize=(14,5.2))
# left: holdout cumulative equity f6 vs EV-gate (idealized + slip)
for mask,lbl,c in [(f6_te,"f6 (holdout)","#e15759"),(ev_te,"EV-gate (holdout)","#4e79a7")]:
    idx=np.where(mask)[0];
    for s,ls,al in [(0.0,"-",1.0),(0.03,"--",0.6)]:
        cum=np.cumsum([pnl(EP[i],WON[i],s) for i in idx])
        ax[0].plot(range(len(cum)),cum,ls,color=c,alpha=al,label=f"{lbl}{' +3c slip' if s else ''}")
ax[0].axhline(0,color="#888",lw=.7); ax[0].set_title("Holdout (last 40%): cumulative $ — idealized vs +3c slippage")
ax[0].set_xlabel("trade #"); ax[0].set_ylabel("cumulative PnL $"); ax[0].legend(fontsize=8)
# right: REAL prod f6 weekly EV
ks=sorted(wkr); vals=[np.mean(wkr[k]) for k in ks]
ax[1].bar(range(len(ks)),vals,color=["#59a14f" if v>0 else "#e15759" for v in vals])
ax[1].axhline(0,color="#888",lw=.7); ax[1].set_title("REAL f6 prod-paper: EV $/trade by week")
ax[1].set_xticks(range(len(ks))); ax[1].set_xticklabels(ks,rotation=45,ha="right",fontsize=8); ax[1].set_ylabel("EV $/trade")
plt.tight_layout(); plt.savefig("reverify_pnl.png",dpi=130); print("\n  chart -> reverify_pnl.png")
