"""Predexon API client — REAL Polymarket orderbook/trade history + Binance underlying.
Free & unlimited data endpoints; per-second rate-limited -> backoff. Disk-cached.

Confirmed endpoints (docs.predexon.com, 2026):
  GET /v2/polymarket/crypto-updown            list BTC/ETH/.. up-down markets (asset,timeframe,status,limit,offset)
  GET /v2/polymarket/orderbooks               historical L2 snapshots (token_id,start_time,end_time ms,limit<=200,pagination_key) — data from Jan 1 2026
  GET /v2/polymarket/trades                    historical trade tape (token_id,start_time,end_time,order,limit<=500)
  GET /v2/binance/candles/{symbol}             OHLCV (interval 1s/1m/.., start_time,end_time,limit<=1500)
"""
import json, os, time, hashlib
import requests

API_KEY = os.getenv("PREDEXON_API_KEY", "")  # required: export PREDEXON_API_KEY=...
ROOT = "https://api.predexon.com/v2"
H = {"x-api-key": API_KEY}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
os.makedirs(CACHE, exist_ok=True)


def _get(path, params, tries=8):
    for i in range(tries):
        r = requests.get(ROOT + path, params=params, headers=H, timeout=60)
        if r.status_code == 429:
            time.sleep(min(r.json().get("retryAfterMs", 600) / 1000 + 0.25, 4)); continue
        if r.status_code >= 500:
            time.sleep(0.6 + i * 0.4); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status(); return r.json()


def _cache_get(key, fn):
    p = os.path.join(CACHE, key + ".json")
    if os.path.exists(p) and os.path.getsize(p) > 2:
        return json.load(open(p, encoding="utf-8"))
    v = fn(); json.dump(v, open(p, "w", encoding="utf-8")); return v


# ---- market discovery ----
def list_updown(asset, timeframe, status="closed", pages=6):
    """Return closed up/down markets (paginates offset, newest-first)."""
    out = []
    for pg in range(pages):
        j = _get("/polymarket/crypto-updown",
                 {"asset": asset, "timeframe": timeframe, "status": status,
                  "limit": 100, "offset": pg * 100})
        arr = j.get("markets", [])
        if not arr: break
        out += arr
    return out


# ---- orderbook snapshots (cursor paginated) ----
def orderbooks(token_id, start_ms, end_ms, max_pages=40):
    def fn():
        out = []; key = None
        for _ in range(max_pages):
            p = {"token_id": token_id, "start_time": start_ms, "end_time": end_ms, "limit": 200}
            if key: p["pagination_key"] = key
            j = _get("/polymarket/orderbooks", p)
            sn = j.get("snapshots", [])
            out += sn
            pg = j.get("pagination", {})
            if not pg.get("has_more") or not sn: break
            key = pg.get("pagination_key")
        return out
    h = hashlib.md5(f"ob:{token_id}:{start_ms}:{end_ms}".encode()).hexdigest()[:16]
    return _cache_get(f"ob_{h}", fn)


# ---- trade tape (time params are Unix SECONDS for this endpoint) ----
def trades(token_id, start_ms, end_ms, max_pages=80):
    def fn():
        out = []; key = None
        for _ in range(max_pages):
            p = {"token_id": token_id, "start_time": start_ms // 1000, "end_time": end_ms // 1000 + 1,
                 "order": "asc", "limit": 500}
            if key: p["pagination_key"] = key
            j = _get("/polymarket/trades", p)
            arr = j.get("trades", j.get("data", []))
            out += arr
            pg = j.get("pagination", {})
            if not pg.get("has_more") or not arr: break
            key = pg.get("pagination_key")
        return out
    h = hashlib.md5(f"tr:{token_id}:{start_ms}:{end_ms}".encode()).hexdigest()[:16]
    return _cache_get(f"tr_{h}", fn)


# ---- BTC underlying from public data.binance.vision (1-min daily klines, free) ----
import io, zipfile, datetime as _dt
_BV = os.path.join(CACHE, "binance"); os.makedirs(_BV, exist_ok=True)

def btc_path(start_ms, end_ms, symbol="BTCUSDT"):
    """Return dict {minute_index -> close} covering [start_ms, end_ms] from data.binance.vision."""
    d0 = _dt.datetime.utcfromtimestamp(start_ms / 1000).date()
    d1 = _dt.datetime.utcfromtimestamp(end_ms / 1000).date()
    P = {}; day = d0
    while day <= d1:
        ds = day.isoformat(); zp = os.path.join(_BV, f"{symbol}-1m-{ds}.zip")
        if not (os.path.exists(zp) and os.path.getsize(zp) > 400):
            url = f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{ds}.zip"
            try:
                r = requests.get(url, timeout=90)
                if r.status_code == 200: open(zp, "wb").write(r.content)
            except Exception: pass
        if os.path.exists(zp) and os.path.getsize(zp) > 400:
            try:
                with zipfile.ZipFile(zp) as z:
                    with z.open(z.namelist()[0]) as fh:
                        for line in io.TextIOWrapper(fh, encoding="utf-8"):
                            c = line.split(",")
                            ts = int(c[0])
                            while ts > 2_000_000_000_000: ts //= 1000   # us -> ms
                            # kline keyed by OPEN time but c[4] is the CLOSE = price at the END
                            # of the minute -> store under minute+1 so P[i] = price AT minute i.
                            # (The old P[ts//60000]=close gave every caller a 60s LOOKAHEAD.)
                            P[ts // 60000 + 1] = float(c[4])
            except Exception: pass
        day += _dt.timedelta(days=1)
    return P


if __name__ == "__main__":
    import datetime as dt
    def ms(iso): return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    m = [x for x in list_updown("btc", "daily", pages=1) if "march-16" in x["market_slug"]][0]
    tid = m["up_token_id"]; st = ms(m["start_time"]); en = ms(m["end_time"])
    print("market:", m["market_slug"], "| win", m["winning_side"], "| vol", round(float(m["total_volume_usd"])))
    tr = trades(tid, st, en)
    print("TRADES:", len(tr), "| sides:", {s: sum(1 for t in tr if t.get('side') == s) for s in ('BUY', 'SELL')})
    ob = orderbooks(tid, st, en)
    print("ORDERBOOK snaps:", len(ob))
    P = btc_path(st, en)
    mn = sorted(P); print("BTC 1m points:", len(P), "| range", round(min(P.values())) if P else 0, "-", round(max(P.values())) if P else 0)
