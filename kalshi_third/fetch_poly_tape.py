"""Bulk-fetch Polymarket btc-updown-15m history via Predexon into px's disk cache.
UP-token trade tape for ALL closed markets; DOWN-token tape additionally for the
most recent RECENT_BOTH markets (test period needs both sides honestly).
Every px.trades() call is disk-cached, so downstream analyses (and workflow
agents) hit the cache, not the API. Run: PREDEXON_API_KEY=... python fetch_poly_tape.py"""
import datetime as dt
import json, os, sys, time
import px

RECENT_BOTH = 1000

def ms(iso):
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

mm = px.list_updown("btc", "15m", status="closed", pages=40)
print(f"closed 15m markets: {len(mm)} ({mm[-1]['market_slug']} .. {mm[0]['market_slug']})", flush=True)
json.dump(mm, open(os.path.join(px.CACHE, "poly15m_markets.json"), "w"))

t0 = time.time()
done = err = 0
for i, m in enumerate(mm):
    try:
        st, en = ms(m["start_time"]), ms(m["end_time"])
        px.trades(m["up_token_id"], st, en)
        if i < RECENT_BOTH:
            px.trades(m["down_token_id"], st, en)
        done += 1
    except Exception as e:
        err += 1
        if err <= 5: print(f"ERR {m['market_slug']}: {str(e)[:120]}", flush=True)
    if i % 100 == 0:
        el = time.time() - t0
        print(f"{i}/{len(mm)} done={done} err={err} elapsed={el/60:.1f}m", flush=True)
print(f"DONE markets={done} err={err} elapsed={(time.time()-t0)/60:.1f}m", flush=True)
