"""Fetch Polymarket btc-updown-15m ORDERBOOK snapshots (Predexon) into px's cache.
Sub-second best bid/ask depth per window — the accurate executable book the
trade-tape round-2 lacked. UP token for the most recent N windows; DOWN token
too for the newest RECENT_DN (so a real down-side fill can be checked without
the 1-UP approximation). Run: PREDEXON_API_KEY=... python fetch_poly_ob.py [N]"""
import datetime as dt
import json, os, sys, time
import px

N = int(sys.argv[1]) if len(sys.argv) > 1 else 500
RECENT_DN = 500

def ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

mm = json.load(open(os.path.join(px.CACHE, "poly15m_markets.json"), encoding="utf-8"))
# newest first, keep those with a real outcome
mm = [m for m in mm if m.get("winning_side") in ("Up", "Down")][:N]
print(f"windows to fetch orderbooks: {len(mm)} ({mm[-1]['market_slug']} .. {mm[0]['market_slug']})", flush=True)

t0 = time.time(); done = err = 0
for i, m in enumerate(mm):
    try:
        st, en = ms(m["start_time"]), ms(m["end_time"])
        px.orderbooks(m["up_token_id"], st, en, max_pages=12)
        if i < RECENT_DN:
            px.orderbooks(m["down_token_id"], st, en, max_pages=12)
        done += 1
    except Exception as e:
        err += 1
        if err <= 5:
            print("ERR %s: %s" % (m["market_slug"], str(e)[:120]), flush=True)
    if i % 50 == 0:
        print("%d/%d done=%d err=%d elapsed=%.1fm" % (i, len(mm), done, err, (time.time()-t0)/60), flush=True)
print("DONE ob windows=%d err=%d elapsed=%.1fm" % (done, err, (time.time()-t0)/60), flush=True)
