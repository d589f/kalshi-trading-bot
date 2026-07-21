#!/usr/bin/env python3
"""Fetch ANY Kalshi settled series (markets + per-minute candles) from the public
API. Generalises fetch_kalshi_hist.py. Run on a US box (CloudFront geo-blocks).
  python fetch_kalshi_series.py <SERIES> <CUTOFF_ISO> [out_prefix]
e.g. python fetch_kalshi_series.py KXETH15M 2026-06-15T00:00:00Z eth15m
Writes /root/<prefix>_markets_new.csv + /root/<prefix>_entry_new.csv."""
import json, csv, sys, time, urllib.request, datetime as dt

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = sys.argv[1] if len(sys.argv) > 1 else "KXETH15M"
CUTOFF = sys.argv[2] if len(sys.argv) > 2 else "2026-06-15T00:00:00Z"
PREFIX = sys.argv[3] if len(sys.argv) > 3 else SERIES.lower()

def get(url, tries=8):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "hist-fetch"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if "429" in str(e):
                time.sleep(1.5 + i); continue
            if i == tries - 1: raise
            time.sleep(1.0 + i)

# 1) settled markets, newest-first, back to CUTOFF
mkts, cursor = [], ""
while True:
    u = "%s/markets?series_ticker=%s&status=settled&limit=1000" % (BASE, SERIES)
    if cursor: u += "&cursor=%s" % cursor
    j = get(u)
    arr = j.get("markets", [])
    if not arr: break
    stop = False
    for m in arr:
        if m["open_time"] < CUTOFF: stop = True; break
        mkts.append(m)
    cursor = j.get("cursor", "")
    if stop or not cursor: break
print("%s settled markets since %s: %d" % (SERIES, CUTOFF, len(mkts)), flush=True)

with open("/root/%s_markets_new.csv" % PREFIX, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["ticker", "open_time", "close_time", "floor_strike", "result"])
    for m in mkts:
        w.writerow([m["ticker"], m["open_time"], m["close_time"], m.get("floor_strike", ""), m.get("result", "")])

def iso2ts(s): return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
rows = err = 0
with open("/root/%s_entry_new.csv" % PREFIX, "w", newline="") as f:
    w = csv.writer(f); w.writerow(["ticker", "minute", "price", "yes_ask", "yes_bid"])
    for i, m in enumerate(mkts):
        ot, ct = iso2ts(m["open_time"]), iso2ts(m["close_time"])
        try:
            j = get("%s/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=1"
                    % (BASE, SERIES, m["ticker"], ot, ct))
            for c in j.get("candlesticks", []):
                ci = (c["end_period_ts"] - ot) // 60 - 1
                if ci < 0 or ci > 14: continue
                p = (c.get("price") or {}).get("close_dollars")
                ya = (c.get("yes_ask") or {}).get("close_dollars")
                yb = (c.get("yes_bid") or {}).get("close_dollars")
                w.writerow([m["ticker"], ci, p or "", ya or "", yb or ""]); rows += 1
        except Exception:
            err += 1
        if i % 100 == 0: print("%d/%d candle-rows=%d err=%d" % (i, len(mkts), rows, err), flush=True)
        time.sleep(0.12)
print("DONE %s markets=%d entry_rows=%d errors=%d" % (SERIES, len(mkts), rows, err), flush=True)
