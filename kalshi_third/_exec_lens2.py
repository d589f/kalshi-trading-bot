#!/usr/bin/env python3
import sys, statistics as st, math
sys.stdout.reconfigure(encoding="utf-8")
import asset_momentum as am
TE = am.TRAIN_END
CFG = {"min6_0.12%": (5,0.0012), "min6_0.18%": (5,0.0018), "min4_0.08%": (3,0.0008)}

def run_detail(ci, thr):
    em=ci+1; rows=[]
    for t,tk,wu,rets in am.wins:
        r=rets.get(em)
        if r is None or abs(r)<thr: continue
        side="UP" if r>0 else "DOWN"
        e=am.entry_px(tk,ci,side)
        if e is None or e<=0.02 or e>=0.99: continue
        won=wu if side=="UP" else (not wu)
        en=am.ENTRY.get((tk,ci)); _,ya,yb=en
        sp=(ya-yb) if (ya is not None and yb is not None) else None
        e_next=am.entry_px(tk,ci+1,side)
        rows.append(dict(t=t,side=side,won=won,e=e,spread=sp,e_next=e_next))
    return rows

def ev(rows, pf):
    ps=[]
    for r in rows:
        e=pf(r)
        if e is None: continue
        p=am.pnl5(e,r['won'])
        if p is not None: ps.append(p)
    return (sum(ps)/len(ps) if ps else None, len(ps))

def sp(rows): return [r for r in rows if r['t']<TE],[r for r in rows if r['t']>=TE]

# fee magnitude sanity: kfee is included in pnl5 both ways
print("FEE CHECK: pnl5 at e=0.87 win vs a NO-FEE version")
e=0.87; n=min(15,round(5/e)); fee=am.kfee(n,e)
print(f"  e={e} n={n} kfee={fee:.4f}  pnl5_win={am.pnl5(e,True):+.4f}  nofee_win={n*(1-e):+.4f}  (fee drag/trade≈{fee:+.4f})")

print("\nADVERSARIAL worst-of fill = max(ask_ci, ask_ci+1) [you fill at ci+1 only when it moved AGAINST you]")
for name,(ci,thr) in CFG.items():
    rows=run_detail(ci,thr); tr,te=sp(rows)
    def worst(r):
        return max(r['e'], r['e_next']) if r['e_next'] is not None else r['e']
    for lab,sub in (("ALL",rows),("TRAIN",tr),("TEST",te)):
        m,n=ev(sub,worst); m2,_=ev(sub,lambda r:r['e'])
        print(f"  {name:11s} {lab:5s} worst-of EV/tr={m if m is None else round(m,4)!s:>8} (baseline {round(m2,4)})  n={n}")
    # depth proxy: we have NO size column — state it, and show how many entries sit >=0.90 (thin, pricey)
    hi=sum(1 for r in rows if r['e']>=0.90); print(f"    entries with ask>=0.90: {hi}/{len(rows)} ({100*hi/len(rows):.0f}%)  | avg spread {100*st.mean(r['spread'] for r in rows if r['spread'] is not None):.2f}c")
