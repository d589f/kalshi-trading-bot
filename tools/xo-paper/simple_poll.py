"""Dead-simple synchronous 3-venue 5-min orderbook poller. No WS, no threads.
Every ~1.5s: pick the live XO pulse window + live Poly 5m window + Kalshi 15m,
GET each book (XO/Poly REST public, Kalshi signed REST), write one CSV row.
Every piece verified by hand. Run on EU: python3 -u /tmp/simple_poll.py <sec>"""
import sys, json, time, base64, urllib.request, datetime as dt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

PX = os.getenv("PREDEXON_API_KEY", "")          # export PREDEXON_API_KEY=...
KID = os.getenv("KALSHI_ACCESS_KEY_ID", "")     # export KALSHI_ACCESS_KEY_ID=...
KK = load_pem_private_key(open(os.getenv("KALSHI_PEM", "/home/dmitrii/.kalshi_live.pem"), "rb").read(), password=None)
XO_META = "https://api-mainnet.xo.market"
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 900

CSV = open("/tmp/books5m.csv", "w")
CSV.write("t,secXO,xo_bid,xo_ask,xo_nb,xo_na,xo_dep,secPO,po_bid,po_ask,po_nb,po_na,po_dep,"
          "k_bid,k_ask,k_nb,k_na,k_dep,btc,xo_slug,po_slug,k_tk\n"); CSV.flush()

def jget(u, hdr=None, to=6):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr or {"User-Agent": "p"}), timeout=to) as r:
            return json.load(r)
    except Exception:
        return None

def ksign(path):
    ts = str(int(time.time() * 1000))
    sig = KK.sign((ts + "GET" + path).encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KID, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts, "User-Agent": "p"}

def iso(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return 0.0

def lvl(x):
    if isinstance(x, dict): return float(x.get("price")), float(x.get("size", 0))
    return float(x[0]), (float(x[1]) if len(x) > 1 else 0.0)

def top(bids, asks):
    bl = [lvl(x) for x in (bids or [])]; al = [lvl(x) for x in (asks or [])]
    bb = max((p for p, _ in bl), default=None); ba = min((p for p, _ in al), default=None)
    dep = sum(p * s for p, s in bl) + sum(p * s for p, s in al)
    return bb, ba, len(bl), len(al), round(dep, 1)

D = {"t": 0.0, "xo": {}, "po": {}, "ktk": None}   # xo/po: token -> (s, e, slug)

def refresh():
    now = time.time()
    if now - D["t"] < 18: return
    D["t"] = now
    try:
        j = jget(XO_META + "/api/markets?take=100&marketScope=onlyClob&statuses=ACTIVE&sortOrder=DESC")
        xo = {}
        for m in (j.get("data") if j else []) or []:
            sl = str(m.get("slug", ""))
            if "pulse" not in sl.lower() or "btc" not in sl.lower(): continue
            det = jget(XO_META + "/api/markets/%d" % m["id"]); det = (det.get("data") or det) if det else None
            if not det or not det.get("startsAt"): continue
            outs = det.get("outcomes", [])
            up = None
            for o in outs:
                if str(o.get("name", "")).upper().startswith("UP"):
                    up = str(o.get("outcomeTokenId") or o.get("id") or "")
            if up is None and outs:              # XO outcome names are null -> UP = index 0
                up = str(outs[0].get("outcomeTokenId") or outs[0].get("id") or "")
            if up:
                s = iso(det["startsAt"]); e = iso(det.get("expiresAt")) or (s + 300)
                xo[up] = (s, e, sl[-9:])
        if xo: D["xo"] = xo
    except Exception as ex:
        print("disc xo err", str(ex)[:80], flush=True)
    try:
        j = jget("https://api.predexon.com/v2/polymarket/crypto-updown?asset=btc&timeframe=5m&status=open&limit=6",
                 hdr={"x-api-key": PX, "User-Agent": "p"})
        po = {}
        for m in (j.get("markets") if j else []) or []:
            up = str(m.get("up_token_id") or "")
            if up: po[up] = (iso(m.get("start_time")), iso(m.get("end_time")), str(m.get("market_slug", ""))[-10:])
        if po: D["po"] = po
    except Exception as ex:
        print("disc po err", str(ex)[:80], flush=True)
    try:
        path = "/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1"
        k = jget("https://api.elections.kalshi.com" + path, hdr=ksign(path))
        ms = k.get("markets") if k else None
        if ms: D["ktk"] = ms[0].get("ticker")
    except Exception as ex:
        print("disc k err", str(ex)[:80], flush=True)

def pick(d):
    now = time.time()
    live = [(tok, v) for tok, v in d.items() if v[0] and v[0] - 5 <= now < v[1]]
    if live: return sorted(live, key=lambda x: x[1][0])[-1]
    up = [(tok, v) for tok, v in d.items() if v[0] and v[0] > now]
    return sorted(up, key=lambda x: x[1][0])[0] if up else (None, None)

def xo_book(tok):
    b = jget("https://orderbooks.xo.market/book?token_id=" + tok)
    return top(b.get("bids"), b.get("asks")) if b else (None, None, 0, 0, 0)

def po_book(tok):
    b = jget("https://clob.polymarket.com/book?token_id=" + tok)
    return top(b.get("bids"), b.get("asks")) if b else (None, None, 0, 0, 0)

def k_book():
    tk = D["ktk"]
    if not tk: return (None, None, 0, 0, 0, "")
    path = "/trade-api/v2/markets/%s/orderbook" % tk
    ob = jget("https://api.elections.kalshi.com" + path, hdr=ksign(path))
    if not ob: return (None, None, 0, 0, 0, tk[-8:])
    fp = ob.get("orderbook_fp", ob.get("orderbook", {})) or {}
    yes = [(float(a), float(s)) for a, s in fp.get("yes_dollars", [])]
    no = [(float(a), float(s)) for a, s in fp.get("no_dollars", [])]
    yb = max((p for p, _ in yes), default=None)
    ya = round(1 - max((p for p, _ in no), default=1), 4) if no else None
    dep = sum(p * s for p, s in yes) + sum((1 - p) * s for p, s in no)
    return (yb, ya, len(yes), len(no), round(dep, 1), tk[-8:])

def btc():
    s = jget("http://127.0.0.1:8893/api/state", to=3)
    return s.get("binance_price") if s else None

refresh()
print("startup discovered: XO=%d Poly=%d Kalshi_tk=%s" % (len(D["xo"]), len(D["po"]), D["ktk"]), flush=True)
end = time.time() + DUR
while time.time() < end:
    now = time.time(); refresh()
    xt, xv = pick(D["xo"]); pt, pv = pick(D["po"])
    xo = xo_book(xt) if xt else (None, None, 0, 0, 0)
    po = po_book(pt) if pt else (None, None, 0, 0, 0)
    k = k_book(); b = btc()
    secXO = int(now - xv[0]) if xv else ""
    secPO = int(now - pv[0]) if pv else ""
    row = [round(now, 1), secXO, xo[0], xo[1], xo[2], xo[3], xo[4],
           secPO, po[0], po[1], po[2], po[3], po[4],
           k[0], k[1], k[2], k[3], k[4], b,
           xv[2] if xv else "", pv[2] if pv else "", k[5]]
    CSV.write(",".join("" if v is None else str(v) for v in row) + "\n"); CSV.flush()
    time.sleep(max(0, 1.5 - (time.time() - now)))
CSV.close()
