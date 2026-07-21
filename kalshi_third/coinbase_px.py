"""Coinbase Exchange 1-min BTC-USD path — a proxy for CF Benchmarks BRTI, the
index Kalshi actually settles KXBTC15M on (Coinbase is BRTI's largest, deepest
US-legal constituent; Binance is NOT in BRTI). Returns {minute_index: close}
where P[i] = price AT minute i (close of the kline covering [i-1, i)), matching
px.btc_path's lookahead-fixed convention exactly. Disk-cached per day."""
import urllib.request, json, os, time, datetime as dt

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "coinbase")
os.makedirs(CACHE, exist_ok=True)
BASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def _get(start, end, tries=6):
    u = "%s?granularity=60&start=%s&end=%s" % (BASE, start.isoformat(), end.isoformat())
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "px"}), timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(0.5 + i)


def _day(day):
    """One UTC day of 1-min candles -> {minute_index: close}. Cached."""
    ds = day.isoformat()
    p = os.path.join(CACHE, "%s.json" % ds)
    if os.path.exists(p) and os.path.getsize(p) > 2:
        return {int(k): v for k, v in json.load(open(p)).items()}
    out = {}
    t = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    end_day = t + dt.timedelta(days=1)
    cur = t
    while cur < end_day:
        chunk_end = min(cur + dt.timedelta(minutes=290), end_day)
        rows = _get(cur, chunk_end)
        for row in rows:
            ts, _lo, _hi, _op, close, _v = row[:6]
            # Coinbase ts = kline OPEN time; close realized AT ts+60 -> store minute+1,
            # so P[i] = price AT minute i (no 60s lookahead, matches px.btc_path).
            out[ts // 60 + 1] = float(close)
        cur = chunk_end
        time.sleep(0.25)
    json.dump(out, open(p, "w"))
    return out


def btc_path(start_ms, end_ms):
    d0 = dt.datetime.utcfromtimestamp(start_ms / 1000).date()
    d1 = dt.datetime.utcfromtimestamp(end_ms / 1000).date()
    P = {}
    day = d0
    while day <= d1:
        try:
            P.update(_day(day))
        except Exception as e:
            print("coinbase day %s failed: %s" % (day, str(e)[:80]), flush=True)
        day += dt.timedelta(days=1)
    return P


if __name__ == "__main__":
    import sys
    a = int(dt.datetime(2026, 4, 17).timestamp() * 1000)
    b = int(dt.datetime(2026, 7, 9).timestamp() * 1000)
    P = btc_path(a, b)
    print("coinbase 1m points:", len(P))
    if P:
        vals = list(P.values())
        print("range %.0f .. %.0f" % (min(vals), max(vals)))
