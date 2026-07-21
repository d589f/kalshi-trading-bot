#!/usr/bin/env python3
"""EXECUTION REALISM lens on the ETH15M momentum edge. Adversarial: try to kill it."""
import sys, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
import asset_momentum as am

TE = am.TRAIN_END

CONFIGS = {
    "min6_0.12%": (5, 0.0012),
    "min6_0.18%": (5, 0.0018),
    "min4_0.08%": (3, 0.0008),
}

def run_detail(ci, thr_ret):
    """Replicate am.run but keep full per-trade detail incl spread + next-candle ask."""
    em = ci + 1
    rows = []
    for t, tk, won_up, rets in am.wins:
        r = rets.get(em)
        if r is None or abs(r) < thr_ret: continue
        side = "UP" if r > 0 else "DOWN"
        e = am.entry_px(tk, ci, side)
        if e is None or e <= 0.02 or e >= 0.99: continue
        won = won_up if side == "UP" else (not won_up)
        ent = am.ENTRY.get((tk, ci))
        _, ya, yb = ent
        spread = (ya - yb) if (ya is not None and yb is not None) else None
        # next candle ask on the SAME (mover) side
        e_next = am.entry_px(tk, ci + 1, side)
        rows.append(dict(t=t, tk=tk, side=side, won=won, e=e, ya=ya, yb=yb,
                         spread=spread, e_next=e_next, r=r))
    return rows

def pcts(xs):
    xs = sorted(xs)
    n = len(xs)
    def q(p):
        if n == 0: return None
        i = min(n-1, int(p*n))
        return xs[i]
    return dict(mean=st.mean(xs) if xs else None, median=q(0.5), p90=q(0.9), p10=q(0.1), mn=xs[0] if xs else None, mx=xs[-1] if xs else None, n=n)

def ev_of(rows, price_fn):
    """price_fn(row)->entry price (or None to drop). returns (n_kept, n_dropped, ev, tot)."""
    pnls = []
    dropped = 0
    for row in rows:
        e = price_fn(row)
        if e is None:
            dropped += 1; continue
        p = am.pnl5(e, row['won'])
        if p is None:
            dropped += 1; continue
        pnls.append(p)
    if not pnls:
        return (0, dropped, None, 0.0)
    return (len(pnls), dropped, sum(pnls)/len(pnls), sum(pnls))

def split(rows):
    tr = [r for r in rows if r['t'] < TE]
    te = [r for r in rows if r['t'] >= TE]
    return tr, te

print("="*90)
for name, (ci, thr) in CONFIGS.items():
    rows = run_detail(ci, thr)
    tr, te = split(rows)
    print(f"\n### {name}  (ci={ci}, entry_minute={ci+1}, thr={thr*100:.3f}%)  n_all={len(rows)}  train={len(tr)} test={len(te)}")

    # baseline EV (ask) — reproduce am.run headline
    for lab, sub in (("ALL", rows), ("TRAIN", tr), ("TEST", te)):
        n,d,ev,tot = ev_of(sub, lambda r: r['e'])
        wr = 100*sum(1 for r in sub if r['won'])/len(sub) if sub else 0
        ae = st.mean(r['e'] for r in sub) if sub else 0
        print(f"  BASELINE ask   {lab:5s} n={n:4d} wr={wr:5.1f}% avg_ask={ae:.3f} EV/tr={ev if ev is None else round(ev,4)!s:>8} tot={tot:+.2f}")

    # ---- (1) SPREAD ----
    sp = pcts([r['spread'] for r in rows if r['spread'] is not None])
    print(f"  SPREAD (yes_ask-yes_bid) c: mean={sp['mean']*100:.2f} median={sp['median']*100:.2f} p90={sp['p90']*100:.2f} max={sp['mx']*100:.2f} (n={sp['n']})")

    # ---- (2) DRIFT next-candle ask on mover side ----
    drifts = [(r['e_next']-r['e']) for r in rows if r['e_next'] is not None]
    dd = pcts(drifts)
    print(f"  DRIFT ask(ci+1)-ask(ci) c: mean={dd['mean']*100:+.2f} median={dd['median']*100:+.2f} p90={dd['p90']*100:+.2f} p10={dd['p10']*100:+.2f} (n={dd['n']}/{len(rows)})")

    # ---- Re-priced EVs ----
    print("  RE-PRICED EV/tr  (n kept / dropped):")
    def show(lab, price_fn):
        for pl, sub in (("ALL",rows),("TRAIN",tr),("TEST",te)):
            n,dr,ev,tot = ev_of(sub, price_fn)
            evs = "None " if ev is None else f"{ev:+.4f}"
            print(f"      {lab:18s} {pl:5s} EV/tr={evs} tot={tot:+7.2f} n={n:4d} drop={dr}")
    show("ask+half_spread", lambda r: (r['e'] + (r['spread'] or 0)/2))
    show("ask+1c", lambda r: r['e'] + 0.01)
    show("ask+2c", lambda r: r['e'] + 0.02)
    show("fill@next_candle", lambda r: r['e_next'])

    # ---- (4) liquidity: trades/day and $ deployed ----
    if rows:
        days = max((rows[-1]['t']-rows[0]['t']).total_seconds()/86400, 1)
        tpd = len(rows)/days
        avg_ask = st.mean(r['e'] for r in rows)
        # contracts sized like pnl5: n = min(15, round(5/e))
        ns = [min(15, round(5.0/r['e'])) or 1 for r in rows]
        depl = st.mean(n*r['e'] for n,r in zip(ns,rows))
        print(f"  LIQUIDITY: {tpd:.2f} trades/day, avg_ask={avg_ask:.3f}, avg contracts={st.mean(ns):.1f}, avg $ deployed/trade=${depl:.2f}")
print("="*90)
