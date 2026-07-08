#!/usr/bin/env python3
"""Fetch KXBTC15M settled markets + per-minute candles (price/yes_ask/yes_bid closes)
from the public Kalshi API. Writes kalshi_markets_new.csv / kalshi_entry_new.csv.
Run on a US box (CloudFront blocks non-US). Stdlib only."""
import json, csv, sys, time, urllib.request, datetime as dt

BASE = "https://api.elections.kalshi.com/trade-api/v2"
CUTOFF = sys.argv[1] if len(sys.argv) > 1 else "2026-06-28T00:00:00Z"

def get(url, tries=6):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hist-fetch"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1: raise
            time.sleep(1.0 + i)

# 1) settled markets, newest-first, back to CUTOFF
mkts, cursor = [], ""
while True:
    u = f"{BASE}/markets?series_ticker=KXBTC15M&status=settled&limit=1000"
    if cursor: u += f"&cursor={cursor}"
    j = get(u)
    arr = j.get("markets", [])
    if not arr: break
    stop = False
    for m in arr:
        if m["open_time"] < CUTOFF: stop = True; break
        mkts.append(m)
    cursor = j.get("cursor", "")
    if stop or not cursor: break
print(f"settled markets since {CUTOFF}: {len(mkts)}", flush=True)

with open("/root/kalshi_markets_new.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["ticker", "open_time", "close_time", "floor_strike", "result"])
    for m in mkts:
        w.writerow([m["ticker"], m["open_time"], m["close_time"], m.get("floor_strike", ""), m.get("result", "")])

# 2) per-minute candles for each market
def iso2ts(s): return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
rows, err = 0, 0
with open("/root/kalshi_entry_new.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["ticker", "minute", "price", "yes_ask", "yes_bid"])
    for i, m in enumerate(mkts):
        ot, ct = iso2ts(m["open_time"]), iso2ts(m["close_time"])
        try:
            j = get(f"{BASE}/series/KXBTC15M/markets/{m['ticker']}/candlesticks?start_ts={ot}&end_ts={ct}&period_interval=1")
            for c in j.get("candlesticks", []):
                ci = (c["end_period_ts"] - ot) // 60 - 1
                if ci < 0 or ci > 14: continue
                p = (c.get("price") or {}).get("close_dollars")
                ya = (c.get("yes_ask") or {}).get("close_dollars")
                yb = (c.get("yes_bid") or {}).get("close_dollars")
                w.writerow([m["ticker"], ci, p or "", ya or "", yb or ""]); rows += 1
        except Exception:
            err += 1
        if i % 100 == 0: print(f"{i}/{len(mkts)} candles rows={rows} err={err}", flush=True)
        time.sleep(0.1)
print(f"DONE markets={len(mkts)} entry_rows={rows} errors={err}", flush=True)
