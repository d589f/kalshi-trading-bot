"""Standalone Kalshi sub-second ORDERBOOK + TRADE collector — the data we never
had. Logs raw `orderbook_delta` + `trade` WS messages (each stamped with a local
receive time) for the currently-open KXBTC15M / KXETH15M window, rotating markets
as windows roll. Reconstruct the book & fills OFFLINE from the log.

Purpose: measure (a) maker fill-rate + adverse selection (post a bid, does a
taker sell into it, does that side then win?) and (b) how fast the ask runs
within the entry window (the phantom/latency question). Both need the depth +
tape that the `ticker` channel and 1-min candles cannot give.

Run:  python3 kalshi_book_collector.py [KXBTC15M KXETH15M]
Auth: same RSA-PSS signed headers as the engine (KEY_PATH below).
"""
import asyncio, base64, json, sys, time, urllib.request
import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
REST = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = "f3894322-a419-4a42-8663-c056abb6dcc0"
KEY_PATH = "/home/dmitrii/.kalshi_live.pem"
SERIES = sys.argv[1:] or ["KXBTC15M", "KXETH15M"]
LOGP = "/tmp/kalshi_book_%s.jsonl" % time.strftime("%Y%m%d")
_KEY = load_pem_private_key(open(KEY_PATH, "rb").read(), password=None)


def _signed():
    ts = str(int(time.time() * 1000))
    sig = _KEY.sign((ts + "GET" + WS_PATH).encode(),
                    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                    hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}


def current_markets():
    """Ticker of the currently-open market for each series (window covering now)."""
    now = time.time()
    out = {}
    for s in SERIES:
        try:
            u = "%s/markets?series_ticker=%s&status=open&limit=20" % (REST, s)
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "col"}), timeout=15) as r:
                ms = json.load(r).get("markets", [])
            # pick the one whose window covers now (open<=now<close), else nearest open
            import datetime as dt
            def ts(x):
                return dt.datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()
            live = [m for m in ms if ts(m["open_time"]) <= now < ts(m["close_time"])]
            if live:
                out[s] = min(live, key=lambda m: ts(m["close_time"]))["ticker"]
        except Exception as e:
            print("[col] discover %s err: %s" % (s, str(e)[:80]), flush=True)
    return out


def _log(f, obj):
    obj["rx"] = time.time()
    f.write(json.dumps(obj) + "\n")
    f.flush()


async def run():
    f = open(LOGP, "a")
    print("[col] logging to %s | series %s" % (LOGP, SERIES), flush=True)
    backoff = 1
    while True:
        subbed = {}          # ticker -> sid
        cmd_id = 1
        try:
            async with websockets.connect(WS_URL, additional_headers=_signed(),
                                           open_timeout=15, max_size=None,
                                           ping_interval=10, ping_timeout=10) as ws:
                print("[col] connected", flush=True)
                backoff = 1
                last_disc = 0
                while True:
                    # (re)discover current markets every 20s; (un)subscribe on rotation
                    if time.time() - last_disc > 20:
                        last_disc = time.time()
                        cur = set(current_markets().values())
                        for tk in list(subbed):
                            if tk not in cur:
                                await ws.send(json.dumps({"id": cmd_id, "cmd": "unsubscribe",
                                              "params": {"sids": [subbed[tk]]}}))
                                cmd_id += 1
                                del subbed[tk]
                        for tk in cur:
                            if tk not in subbed:
                                await ws.send(json.dumps({"id": cmd_id, "cmd": "subscribe",
                                    "params": {"channels": ["orderbook_delta", "trade"],
                                               "market_tickers": [tk]}}))
                                cmd_id += 1
                                subbed[tk] = None
                                print("[col] subscribing %s" % tk, flush=True)
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "subscribed":
                        sid = (d.get("msg") or {}).get("sid")
                        mt = (d.get("msg") or {}).get("market_ticker")
                        if mt in subbed:
                            subbed[mt] = sid
                        continue
                    if t in ("orderbook_snapshot", "orderbook_delta", "trade"):
                        _log(f, d)
        except Exception as e:
            print("[col] error: %s: %s" % (type(e).__name__, e), file=sys.stderr, flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    asyncio.run(run())
