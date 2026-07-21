"""WS-fed paper simulator: XO 5m + Poly 5m via live WebSocket books (sub-second),
Kalshi 15m via signed REST (its WS needs auth+delta reconstruction = crash surface;
sampled rarely). Same rule menu {mom, rev, fade, flatUP} on each venue, settle on
binance. WS books cached + REST fallback on staleness. Heartbeat file for the
watchdog. Every WS task / thread has its own try/reconnect so it never dies quietly.
  python3 -u xo_paper.py   -> /home/dmitrii/xo_paper/trades.csv  (+ .../heartbeat)"""
import os, csv, json, time, base64, threading, asyncio, urllib.request, datetime as dt
import statistics as stx
from math import erf, sqrt
import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

PX = os.getenv("PREDEXON_API_KEY", "")          # export PREDEXON_API_KEY=...
KID = os.getenv("KALSHI_ACCESS_KEY_ID", "")     # export KALSHI_ACCESS_KEY_ID=...
KK = load_pem_private_key(open(os.getenv("KALSHI_PEM", "/home/dmitrii/.kalshi_live.pem"), "rb").read(), password=None)
XO_META = "https://api-mainnet.xo.market"
XO_WS_URL = "wss://orderbooks.xo.market/ws/market"
PO_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
STATE_URL = "http://127.0.0.1:8893/api/state"
OUTDIR = "/home/dmitrii/xo_paper"; os.makedirs(OUTDIR, exist_ok=True)
CSVP = os.path.join(OUTDIR, "trades.csv")
HBP = os.path.join(OUTDIR, "heartbeat")
COLS = ["t", "venue", "window", "rule", "side", "entry", "n", "outcome", "won", "pnl", "cum",
        "open_px", "close_px", "dec_disp", "fair", "mid", "note"]

XO_DEC, K_DEC = 60, 180
MOM_THR = 50.0
FADE_THR = 0.06
REV_K = 5
REV_THR = 0.001
RULES = ["mom", "rev", "fade", "flatUP"]
STALE = 5.0                       # seconds: WS cache older than this -> REST fallback

def jget(u, hdr=None, to=6):
    try:
        with urllib.request.urlopen(urllib.request.Request(u, headers=hdr or {"User-Agent": "xp"}), timeout=to) as r:
            return json.load(r)
    except Exception:
        return None

def ksign(path):
    ts = str(int(time.time() * 1000))
    sig = KK.sign((ts + "GET" + path).encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KID, "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts, "User-Agent": "xp"}

def iso(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return 0.0

def Phi(z): return 0.5 * (1 + erf(z / sqrt(2)))

def lvl(x):
    if isinstance(x, dict): return float(x.get("price")), float(x.get("size", 0))
    return float(x[0]), (float(x[1]) if len(x) > 1 else 0.0)

def top(bids, asks):
    bl = [lvl(x) for x in (bids or [])]; al = [lvl(x) for x in (asks or [])]
    bb = max((p for p, _ in bl), default=None); ba = min((p for p, _ in al), default=None)
    dep = sum(p * s for p, s in bl) + sum(p * s for p, s in al)
    return bb, ba, len(bl), len(al), round(dep, 1)

# ---------------- shared state (plain dicts, GIL-safe for our access pattern) ----------------
BOOKS = {}          # token -> (bb, ba, nb, na, dep, ts)
XO_TOK = {}         # slug -> {s, e, up, dn}
PO_TOK = {}         # epoch -> {s, e, up, dn}
STATE = {"btc": None, "ktk": None}
PXHIST = []
WSOK = {"xo": 0.0, "po": 0.0}      # last time each WS delivered a frame (for status)
PO_STATE = {}                       # token -> {"bids": {price: size}, "asks": {price: size}} (Poly live book)

# ---------------- discovery thread ----------------
def discover():
    while True:
        try:
            j = jget(XO_META + "/api/markets?take=100&marketScope=onlyClob&statuses=ACTIVE&sortOrder=DESC")
            for m in (j.get("data") if j else []) or []:
                sl = str(m.get("slug", ""))
                if "pulse" not in sl.lower() or "btc" not in sl.lower(): continue
                if sl[-9:] in XO_TOK: continue
                det = jget(XO_META + "/api/markets/%d" % m["id"]); det = (det.get("data") or det) if det else None
                if not det or not det.get("startsAt"): continue
                outs = det.get("outcomes", [])
                if len(outs) < 2: continue
                up = str(outs[0].get("outcomeTokenId") or outs[0].get("id") or "")
                dn = str(outs[1].get("outcomeTokenId") or outs[1].get("id") or "")
                XO_TOK[sl[-9:]] = {"s": iso(det["startsAt"]), "e": iso(det.get("expiresAt")) or iso(det["startsAt"]) + 300,
                                   "up": up, "dn": dn, "mid": m["id"], "up_id": outs[0].get("id")}
        except Exception as ex:
            print("[disc xo]", str(ex)[:80], flush=True)
        try:
            now = time.time(); base = int(now // 300 * 300)
            for ep in (base, base + 300):
                if str(ep) in PO_TOK: continue
                g = jget("https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-%d" % ep)
                if not (isinstance(g, list) and g): continue
                m = g[0]; toks = m.get("clobTokenIds"); tl = json.loads(toks) if isinstance(toks, str) else toks
                if not tl or len(tl) < 2: continue
                PO_TOK[str(ep)] = {"s": iso(m.get("eventStartTime")), "e": iso(m.get("endDate")),
                                   "up": str(tl[0]), "dn": str(tl[1])}
        except Exception as ex:
            print("[disc po]", str(ex)[:80], flush=True)
        try:
            path = "/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1"
            k = jget("https://api.elections.kalshi.com" + path, hdr=ksign(path))
            ms = k.get("markets") if k else None
            if ms: STATE["ktk"] = ms[0].get("ticker")
        except Exception as ex:
            print("[disc k]", str(ex)[:80], flush=True)
        try:
            s = jget(STATE_URL, to=3)
            if s and (s.get("chainlink_price") or s.get("binance_price")):
                STATE["btc"] = s.get("chainlink_price") or s.get("binance_price")
        except Exception as ex:
            print("[disc st]", str(ex)[:80], flush=True)
        # prune windows that ended > 10 min ago (keep memory bounded)
        try:
            cut = time.time() - 700
            for d in (XO_TOK, PO_TOK):
                for k in [k for k, v in d.items() if v.get("e") and v["e"] < cut]:
                    d.pop(k, None)
            active = set(all_tokens(XO_TOK)) | set(all_tokens(PO_TOK))
            for cache in (BOOKS, PO_STATE):
                for k in [k for k in list(cache) if k not in active]:
                    cache.pop(k, None)
        except Exception: pass
        time.sleep(15)

# ---------------- WS feeds ----------------
def all_tokens(d):
    out = []
    for w in list(d.values()):
        for t in (w.get("up"), w.get("dn")):
            if t: out.append(t)
    return out

async def xo_ws():
    while True:
        subbed = set()
        try:
            async with websockets.connect(XO_WS_URL, open_timeout=15, max_size=None,
                                           ping_interval=15, ping_timeout=15, close_timeout=5) as ws:
                print("[xo_ws] connected", flush=True)
                while True:
                    pend = [t for t in all_tokens(XO_TOK) if t not in subbed]
                    if pend:
                        await ws.send(json.dumps({"type": "market", "assets_ids": pend, "initial_dump": True}))
                        subbed.update(pend)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(d, dict) and d.get("event_type") == "book":
                        tid = str(d.get("asset_id") or "")
                        if tid:
                            BOOKS[tid] = top(d.get("bids"), d.get("asks")) + (time.time(),)
                            WSOK["xo"] = time.time()
        except Exception as e:
            print("[xo_ws] reconnect:", str(e)[:80], flush=True)
            await asyncio.sleep(3)

async def po_ws():
    while True:
        subbed = set()
        try:
            async with websockets.connect(PO_WS_URL, open_timeout=15, max_size=None,
                                           ping_interval=15, ping_timeout=15, close_timeout=5) as ws:
                print("[po_ws] connected", flush=True)
                while True:
                    pend = [t for t in all_tokens(PO_TOK) if t not in subbed]
                    if pend:
                        await ws.send(json.dumps({"assets_ids": pend, "type": "market"}))
                        subbed.update(pend)
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:
                        continue
                    for e in (d if isinstance(d, list) else [d]):
                        if not isinstance(e, dict): continue
                        et = e.get("event_type"); tid = str(e.get("asset_id") or "")
                        if not tid: continue
                        if et == "book":
                            try:
                                PO_STATE[tid] = {"bids": {float(x["price"]): float(x["size"]) for x in (e.get("bids") or [])},
                                                 "asks": {float(x["price"]): float(x["size"]) for x in (e.get("asks") or [])}}
                            except Exception:
                                continue
                        elif et == "price_change":
                            stt = PO_STATE.setdefault(tid, {"bids": {}, "asks": {}})
                            for ch in (e.get("changes") or [e]):
                                try:
                                    pr = float(ch["price"]); sz = float(ch["size"]); sd = str(ch.get("side", "")).lower()
                                except Exception:
                                    continue
                                bk = stt["bids"] if sd in ("buy", "bid") else stt["asks"]
                                if sz <= 0: bk.pop(pr, None)
                                else: bk[pr] = sz
                        else:
                            continue
                        stt = PO_STATE.get(tid)
                        if stt is not None:
                            bb = max(stt["bids"], default=None); ba = min(stt["asks"], default=None)
                            dep = sum(p * s for p, s in stt["bids"].items()) + sum(p * s for p, s in stt["asks"].items())
                            BOOKS[tid] = (bb, ba, len(stt["bids"]), len(stt["asks"]), round(dep, 1), time.time())
                            WSOK["po"] = time.time()
        except Exception as e:
            print("[po_ws] reconnect:", str(e)[:80], flush=True)
            await asyncio.sleep(3)

# ---------------- book access: WS cache, REST fallback on staleness ----------------
def book_rest(venue, tok):
    if venue == "xo":
        b = jget("https://orderbooks.xo.market/book?token_id=" + tok, to=5)
    else:
        b = jget("https://clob.polymarket.com/book?token_id=" + tok, to=5)
    return top(b.get("bids"), b.get("asks")) if b else (None, None, 0, 0, 0)

def get_ba(venue, tok):
    """return (best_bid, best_ask) for a token, freshest available."""
    e = BOOKS.get(tok)
    if e and time.time() - e[5] < STALE:
        return e[0], e[1]
    r = book_rest(venue, tok)
    if r and (r[0] is not None or r[1] is not None):
        BOOKS[tok] = r + (time.time(),)
        return r[0], r[1]
    return (e[0], e[1]) if e else (None, None)

def kalshi_quote():
    tk = STATE.get("ktk")
    if not tk: return None
    p = "/trade-api/v2/markets/%s/orderbook" % tk
    ob = jget("https://api.elections.kalshi.com" + p, hdr=ksign(p))
    fp = (ob or {}).get("orderbook_fp", (ob or {}).get("orderbook", {})) or {}
    yes = [(float(a), float(s)) for a, s in fp.get("yes_dollars", [])]
    no = [(float(a), float(s)) for a, s in fp.get("no_dollars", [])]
    yb = max((p2 for p2, _ in yes), default=None)
    nb = max((p2 for p2, _ in no), default=None)
    up_ask = round(1 - nb, 4) if nb is not None else None
    dn_ask = round(1 - yb, 4) if yb is not None else None
    up_mid = (yb + up_ask) / 2 if yb is not None and up_ask is not None else None
    return up_ask, dn_ask, up_mid

# ---------------- P&L ----------------
def kfee(n, e): return round(0.07 * n * e * (1 - e) * 10000) / 10000.0
def pnl(entry, won, fee):
    if entry is None or entry <= 0.02 or entry >= 0.98: return None, 0
    n = max(1, min(15, round(5.0 / entry)))
    f = fee(n, entry)
    return ((n * (1 - entry) - f) if won else -(n * entry + f)), n

CUM = {}
def load_cum():
    if not os.path.exists(CSVP): return
    for r in csv.DictReader(open(CSVP)):
        k = (r["venue"], r["rule"])
        try: CUM[k] = float(r["cum"])
        except Exception: pass

_wlock = threading.Lock()
def log_trade(t, venue, win, rule, side, entry, n, outcome, won, p, opx, cpx, disp, fair, mid, note):
    k = (venue, rule); CUM[k] = CUM.get(k, 0.0) + (p or 0.0)
    row = dict(t=round(t, 1), venue=venue, window=win, rule=rule, side=side, entry=entry, n=n,
               outcome=outcome, won=int(won) if won is not None else "",
               pnl=round(p, 4) if p is not None else "", cum=round(CUM[k], 4), open_px=opx, close_px=cpx,
               dec_disp=round(disp, 1) if disp is not None else "", fair=round(fair, 3) if fair is not None else "",
               mid=round(mid, 3) if mid is not None else "", note=note)
    with _wlock:
        newf = not os.path.exists(CSVP)
        with open(CSVP, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=COLS)
            if newf: w.writeheader()
            w.writerow(row)
    print("[%s] %-6s %-7s %-6s %-4s e=%s -> won=%s pnl=%s cum=%.2f (%s)" %
          (time.strftime("%H:%M:%S"), venue, win, rule, str(side), str(entry), str(won),
           ("%.3f" % p) if p is not None else "-", CUM[k], note), flush=True)

def px_at(target):
    if not PXHIST: return None
    best = min(PXHIST, key=lambda x: abs(x[0] - target))
    return best[1] if abs(best[0] - target) <= 120 else None

# ---- venue-native settlement (the venue's OWN oracle/resolution, not binance) ----
def xo_outcome(mid, up_id):
    d = jget(XO_META + "/api/markets/%d" % int(mid), to=8)
    d = (d.get("data") or d) if d else None
    if not d or not d.get("resolvedAt"): return None
    wid = d.get("winningOutcomeId")
    if wid is None and isinstance(d.get("winningOutcome"), dict): wid = d["winningOutcome"].get("id")
    if wid is None or up_id is None: return None
    return int(wid) == int(up_id)

def poly_outcome(ep):
    for extra in ("", "&closed=true"):
        g = jget("https://gamma-api.polymarket.com/markets?slug=btc-updown-5m-%d%s" % (int(ep), extra), to=8)
        if not (isinstance(g, list) and g): continue
        m = g[0]
        if not m.get("closed"): continue
        op = m.get("outcomePrices")
        if isinstance(op, str):
            try: op = json.loads(op)
            except Exception: op = None
        if op and len(op) >= 2:
            try:
                a, b = float(op[0]), float(op[1])
                if a >= 0.99 and b <= 0.01: return True      # outcomePrices[0] == UP
                if b >= 0.99 and a <= 0.01: return False
            except Exception: pass
    return None

def kalshi_outcome(ticker):
    p = "/trade-api/v2/markets/%s" % ticker
    d = jget("https://api.elections.kalshi.com" + p, hdr=ksign(p), to=8)
    m = (d or {}).get("market") or {}
    res = str(m.get("result", "")).lower()
    if res == "yes": return True
    if res == "no": return False
    return None

PENDING = []
def queue_settle(venue, win, buf, wlen, dec_sec, fee, wstart, ident):
    PENDING.append({"venue": venue, "win": win, "buf": buf, "wlen": wlen, "dec": dec_sec,
                    "fee": fee, "wstart": wstart, "ident": ident, "q": time.time()})

def settle_loop():
    """poll each venue's own resolution API; fall back to binance only after 45 min."""
    while True:
        try:
            for it in list(PENDING):
                v = it["venue"]; out = None
                try:
                    if v == "XO":     out = xo_outcome(it["ident"][0], it["ident"][1])
                    elif v == "POLY": out = poly_outcome(it["ident"])
                    else:             out = kalshi_outcome(it["ident"])
                except Exception:
                    out = None
                src = "venue"
                if out is None and time.time() - it["q"] > 2700:
                    live = [x for x in it["buf"] if x.get("sec", -1) >= 0 and x.get("spx")]
                    if len(live) >= 3:
                        o2 = min(live, key=lambda x: x["sec"]); c2 = max(live, key=lambda x: x["sec"])
                        out = c2["spx"] > o2["spx"]; src = "binance-fb"
                    else:
                        PENDING.remove(it); continue
                if out is None: continue
                try:
                    finalize(it["venue"], it["win"], it["buf"], it["wlen"], it["dec"], it["fee"], it["wstart"], out, src)
                except Exception as ex:
                    print("[settle fin]", str(ex)[:90], flush=True)
                PENDING.remove(it)
        except Exception as e:
            print("[settle]", str(e)[:100], flush=True)
        time.sleep(20)

def finalize(venue, win, buf, wlen, dec_sec, fee, wstart, outcome_up, src="venue"):
    live = [x for x in buf if x["sec"] is not None and x["sec"] >= 0 and x["spx"]]
    if len(live) < 3: return
    op = min(live, key=lambda x: x["sec"]); cl = max(live, key=lambda x: x["sec"])
    if op["sec"] > 20 or cl["sec"] < wlen - 40:
        print("[skip %s %s] joined-late op=%.0f cl=%.0f" % (venue, win, op["sec"], cl["sec"]), flush=True)
        return
    open_px, close_px = op["spx"], cl["spx"]
    outcome = "UP" if outcome_up else "DOWN"
    dec = min(live, key=lambda x: abs(x["sec"] - dec_sec)); disp = dec["spx"] - open_px
    difs = [live[i + 1]["spx"] - live[i]["spx"] for i in range(len(live) - 1)]
    dts = [max(0.5, live[i + 1]["sec"] - live[i]["sec"]) for i in range(len(live) - 1)]
    sig_ps = (stx.pstdev([d / sqrt(t) for d, t in zip(difs, dts)]) if len(difs) > 3 else 2.5) or 2.5
    tau = max(5, wlen - dec_sec); fair = Phi(disp / (sig_ps * sqrt(tau))); mid = dec.get("up_mid")
    for rule in RULES:
        side = entry = None; note = ""
        if rule == "mom":
            if abs(disp) < MOM_THR:
                log_trade(time.time(), venue, win, rule, "-", "", "", outcome, None, None, open_px, close_px, disp, fair, mid, "no-trigger"); continue
            side = "UP" if disp > 0 else "DOWN"; entry = dec["up_ask"] if side == "UP" else dec["dn_ask"]; note = "|d|=%.0f" % abs(disp)
        elif rule == "rev":
            p0 = px_at(wstart - REV_K * 60)
            if p0 is None or abs(open_px - p0) / p0 < REV_THR:
                log_trade(time.time(), venue, win, rule, "-", "", "", outcome, None, None, open_px, close_px, disp, fair, mid, "no-trigger" if p0 else "no-hist"); continue
            side = "DOWN" if open_px > p0 else "UP"; entry = op["dn_ask"] if side == "DOWN" else op["up_ask"]; note = "trail=%+.2f%%" % (100 * (open_px - p0) / p0)
        elif rule == "fade":
            if mid is None or abs(mid - fair) < FADE_THR:
                log_trade(time.time(), venue, win, rule, "-", "", "", outcome, None, None, open_px, close_px, disp, fair, mid, "no-trigger"); continue
            side = "DOWN" if mid > fair else "UP"; entry = dec["up_ask"] if side == "UP" else dec["dn_ask"]; note = "dev=%.2f" % (mid - fair)
        elif rule == "flatUP":
            side = "UP"; entry = op["up_ask"]; note = "open"
        won = (outcome_up if side == "UP" else (not outcome_up))
        p, n = pnl(entry, won, fee)
        if p is None:
            log_trade(time.time(), venue, win, rule, side, entry, "", outcome, None, None, open_px, close_px, disp, fair, mid, "bad-entry"); continue
        log_trade(time.time(), venue, win, rule, side, entry, n, outcome, won, p, open_px, close_px, disp, fair, mid, note + "|" + src)

# ---------------- paper loop (thread; blocking REST fallback + kalshi is fine here) ----------------
def paper_loop():
    xo_buf = {}; xo_done = set(); po_buf = {}; po_done = set(); k_buf = []; k_win = None; hb = 0.0; lastk = 0.0
    while True:
        try:
            now = time.time()
            open(HBP, "w").write(str(now))                  # heartbeat for the watchdog
            spx = STATE.get("btc")
            if spx:
                PXHIST.append((now, spx))
                if len(PXHIST) > 1400: del PXHIST[:len(PXHIST) - 1400]
            if now - hb > 60:
                hb = now
                print("[hb %s] xo_tok=%d po_tok=%d books=%d ws_xo=%.0fs ws_po=%.0fs pend=%d cum=%s" %
                      (time.strftime("%H:%M:%S"), len(XO_TOK), len(PO_TOK), len(BOOKS),
                       now - WSOK["xo"] if WSOK["xo"] else -1, now - WSOK["po"] if WSOK["po"] else -1, len(PENDING),
                       {("%s/%s" % k): round(v, 2) for k, v in CUM.items()}), flush=True)
            # ----- XO 5m -----
            for slug, w in list(XO_TOK.items()):
                if slug in xo_done: continue
                sec = now - w["s"]
                if -10 <= sec < (w["e"] - w["s"]) + 20:
                    ub, ua = get_ba("xo", w["up"]); db, da = get_ba("xo", w["dn"])
                    up_mid = ((ub + ua) / 2) if ub is not None and ua is not None else None
                    xo_buf.setdefault(slug, []).append({"sec": sec, "spx": spx, "up_ask": ua, "dn_ask": da, "up_mid": up_mid})
                if sec > (w["e"] - w["s"]) + 8:
                    queue_settle("XO", slug, xo_buf.get(slug, []), 300, XO_DEC, lambda n, e: 0.0, w["s"], (w.get("mid"), w.get("up_id")))
                    xo_done.add(slug); xo_buf.pop(slug, None)
            # ----- Poly 5m -----
            for ep, w in list(PO_TOK.items()):
                if ep in po_done or not w["s"] or not w["e"]: continue
                sec = now - w["s"]
                if -10 <= sec < (w["e"] - w["s"]) + 20:
                    ub, ua = get_ba("po", w["up"]); db, da = get_ba("po", w["dn"])
                    up_mid = ((ub + ua) / 2) if ub is not None and ua is not None else None
                    po_buf.setdefault(ep, []).append({"sec": sec, "spx": spx, "up_ask": ua, "dn_ask": da, "up_mid": up_mid})
                if sec > (w["e"] - w["s"]) + 8:
                    queue_settle("POLY", ep[-6:], po_buf.get(ep, []), 300, XO_DEC, lambda n, e: 0.0, w["s"], int(ep))
                    po_done.add(ep); po_buf.pop(ep, None)
            # ----- Kalshi 15m (REST, every ~2s) -----
            s = jget(STATE_URL, to=3) or {}
            ws_, we_ = iso(s.get("window_start_utc")), iso(s.get("window_end_utc"))
            wl = (s.get("window_minutes") or 15) * 60
            if ws_:
                if k_win and k_win["s"] != ws_:
                    queue_settle("KALSHI", k_win["id"], k_buf, k_win["wl"], K_DEC, kfee, k_win["s"], k_win.get("tk")); k_buf = []
                if not k_win or k_win["s"] != ws_:
                    k_win = {"s": ws_, "e": we_, "wl": wl, "id": time.strftime("%H%M", time.gmtime(ws_)), "tk": STATE.get("ktk")}
                if now - lastk > 2:
                    lastk = now
                    q = kalshi_quote()
                    if q:
                        ua, da, umid = q
                        k_buf.append({"sec": now - ws_, "spx": spx, "up_ask": ua, "dn_ask": da, "up_mid": umid})
            time.sleep(1.0)
        except Exception as e:
            print("[loop err]", str(e)[:120], flush=True)
            time.sleep(1.0)

async def ws_main():
    await asyncio.gather(xo_ws(), po_ws())

def main():
    load_cum()
    print("xo_paper (WS) start | cum seeded: %s" % {("%s/%s" % k): round(v, 2) for k, v in CUM.items()}, flush=True)
    threading.Thread(target=discover, daemon=True).start()
    threading.Thread(target=paper_loop, daemon=True).start()
    threading.Thread(target=settle_loop, daemon=True).start()
    while True:
        try:
            asyncio.run(ws_main())
        except Exception as e:
            print("[ws_main restart]", str(e)[:120], flush=True)
            time.sleep(3)

if __name__ == "__main__":
    main()
