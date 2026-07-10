"""Kalshi real-time top-of-book feed (WebSocket `ticker` channel) — PRIMARY book source.

Why: the 3s REST poll lags the real book by seconds (measured 8-16c on fast dumps),
so paper "fills" happened at prices that no longer existed. The WS `ticker` channel
delivers best yes bid/ask on every change — no orderbook reconstruction, no seq
fragility.

Contract: updates ONLY the price fields (poly_yes_bid/ask + no-side complements,
exactly the mapping ll_ws._apply uses) and the ll_ob_ts heartbeat. Volumes are NOT
touched — the ticker channel carries no depth, and zeroing vols would trip any
session running the liq filter; they keep their last REST-seeded values.

Fallback: poll_ll_orderbook_rest keeps running for market discovery and skips its
own _apply only while this feed is fresh (kalshi_ws_ts guard) — WS dies => the
engine degrades to exactly the old REST behavior within ~4s.

Auth: same signed headers as REST on the HTTP upgrade
(RSA-PSS-SHA256 over "{ts_ms}GET/trade-api/ws/v2").
"""
import asyncio
import base64
import json
import sys
import time

import websockets
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from .state import state, state_lock

WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"
KEY_ID = "f3894322-a419-4a42-8663-c056abb6dcc0"
KEY_PATH = "/home/dmitrii/.kalshi_live.pem"


def _signed_headers():
    ts = str(int(time.time() * 1000))
    msg = (ts + "GET" + WS_PATH).encode()
    with open(KEY_PATH, "rb") as f:
        key = load_pem_private_key(f.read(), password=None)
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _apply_top(yes_bid, yes_ask):
    """Price-only state update — same complement mapping as ll_ws._apply
    (no_bid = 1 - yes_ask, no_ask = 1 - yes_bid); vols left untouched."""
    now = time.time()
    with state_lock:
        state["poly_yes_bid"] = yes_bid
        state["poly_yes_ask"] = yes_ask
        state["poly_no_bid"] = round(1 - yes_ask, 4) if yes_ask is not None else None
        state["poly_no_ask"] = round(1 - yes_bid, 4) if yes_bid is not None else None
        state["ll_ob_ts"] = now        # heartbeat (keepalive watches this)
        state["kalshi_ws_ts"] = now    # freshness guard for the REST fallback


async def consume_kalshi_ticker_ws():
    """Keep top-of-book fresh from the Kalshi WS `ticker` channel. Reconnects with
    backoff; re-subscribes when the active market rotates (market_slug is kept
    current by the existing 3s REST discovery loop)."""
    backoff = 1
    while True:
        subbed = None
        sid = None
        cmd_id = 1
        try:
            async with websockets.connect(
                WS_URL,
                additional_headers=_signed_headers(),
                open_timeout=15,
                max_size=None,
                ping_interval=10,
                ping_timeout=10,
            ) as ws:
                print("[kalshi-ws] connected", flush=True)
                backoff = 1
                while True:
                    with state_lock:
                        cur = state.get("market_slug")
                    if cur and cur != subbed and str(cur).startswith("KXBTC"):
                        if sid is not None:
                            await ws.send(json.dumps(
                                {"id": cmd_id, "cmd": "unsubscribe",
                                 "params": {"sids": [sid]}}))
                            cmd_id += 1
                            sid = None
                        await ws.send(json.dumps(
                            {"id": cmd_id, "cmd": "subscribe",
                             "params": {"channels": ["ticker"],
                                        "market_tickers": [cur]}}))
                        cmd_id += 1
                        subbed = cur
                        print("[kalshi-ws] subscribing %s" % cur, flush=True)

                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        # any inbound frame proves the connection is alive
                        with state_lock:
                            if state.get("kalshi_ws_ts"):
                                state["kalshi_ws_ts"] = time.time()
                    except asyncio.TimeoutError:
                        # ticker is CHANGE-based: silence on a live connection
                        # means "no quote change", not staleness. Keep the
                        # freshness stamp alive so the fill gate only fires on
                        # real disconnects (reconnect loop stops stamping).
                        with state_lock:
                            if state.get("kalshi_ws_ts"):
                                state["kalshi_ws_ts"] = time.time()
                        continue
                    try:
                        d = json.loads(msg)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "subscribed":
                        sid = (d.get("msg") or {}).get("sid", sid)
                        continue
                    if t == "error":
                        print("[kalshi-ws] server error: %s" % d,
                              file=sys.stderr, flush=True)
                        continue
                    if t != "ticker":
                        continue
                    m = d.get("msg") or {}
                    if m.get("market_ticker") != subbed:
                        continue
                    yb = _fnum(m.get("yes_bid_dollars", m.get("yes_bid")))
                    ya = _fnum(m.get("yes_ask_dollars", m.get("yes_ask")))
                    # yes_bid may come in cents under the legacy key
                    if yb is not None and yb > 1:
                        yb = yb / 100.0
                    if ya is not None and ya > 1:
                        ya = ya / 100.0
                    if yb is None and ya is None:
                        continue
                    _apply_top(yb, ya)
        except Exception as e:
            print("[kalshi-ws] error: %s: %s" % (type(e).__name__, e),
                  file=sys.stderr, flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)
