"""WebSocket consumers: Binance trades/depth, Polymarket RTDS/CLOB."""
import asyncio
import json
import sys
import time
import threading

import websockets

from .config import (BINANCE_TRADES_WS, BINANCE_DEPTH_WS,
                     POLY_RTDS_WS, POLY_CLOB_WS)
from .state import (state, state_lock, trade_buffer, trade_buffer_lock,
                    conn_status, conn_lock, orderbook, ob_lock, _clob_msg_count)
from .orderbook import process_clob_msg, flush_pending_changes


async def consume_binance_trades():
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(BINANCE_TRADES_WS, ping_interval=20, close_timeout=5) as ws:
                print("[dashboard] Connected to Binance trades", flush=True)
                with conn_lock:
                    conn_status["binance_trades"]["connected"] = True
                    conn_status["binance_trades"]["last_error"] = None
                backoff = 1.0
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        price = float(d.get("p", 0))
                        qty = float(d.get("q", 0))
                        notional = price * qty
                        is_sell = d.get("m", False)
                        ts = time.time()

                        with trade_buffer_lock:
                            trade_buffer.append((ts, notional, is_sell))

                        with state_lock:
                            state["binance_price"] = price
                            state["binance_ts"] = d.get("T", "")
                        with conn_lock:
                            conn_status["binance_trades"]["last_msg_time"] = time.time()
                            conn_status["binance_trades"]["msg_count"] += 1
                    except Exception:
                        pass
        except Exception as e:
            err_str = f"{type(e).__name__}: {e}"
            print(f"[dashboard] Binance trades error: {err_str}", file=sys.stderr, flush=True)
            with conn_lock:
                conn_status["binance_trades"]["connected"] = False
                conn_status["binance_trades"]["last_error"] = err_str
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def consume_binance_depth():
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(BINANCE_DEPTH_WS, ping_interval=20, close_timeout=5) as ws:
                print("[dashboard] Connected to Binance depth", flush=True)
                with conn_lock:
                    conn_status["binance_depth"]["connected"] = True
                    conn_status["binance_depth"]["last_error"] = None
                backoff = 1.0
                async for msg in ws:
                    try:
                        d = json.loads(msg)
                        bids = d.get("b") or d.get("bids") or []
                        asks = d.get("a") or d.get("asks") or []
                        if bids and asks:
                            b1p = float(bids[0][0])
                            b1v = float(bids[0][1])
                            a1p = float(asks[0][0])
                            a1v = float(asks[0][1])
                            with state_lock:
                                state["bn_bid1_p"] = b1p
                                state["bn_bid1_v"] = b1v
                                state["bn_ask1_p"] = a1p
                                state["bn_ask1_v"] = a1v
                                state["bn_spread"] = round(a1p - b1p, 2)
                        with conn_lock:
                            conn_status["binance_depth"]["last_msg_time"] = time.time()
                            conn_status["binance_depth"]["msg_count"] += 1
                    except Exception:
                        pass
        except Exception as e:
            err_str = f"{type(e).__name__}: {e}"
            print(f"[dashboard] Binance depth error: {err_str}", file=sys.stderr, flush=True)
            with conn_lock:
                conn_status["binance_depth"]["connected"] = False
                conn_status["binance_depth"]["last_error"] = err_str
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def consume_poly_rtds():
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(POLY_RTDS_WS, ping_interval=10, close_timeout=5) as ws:
                print("[dashboard] Connected to Polymarket RTDS", flush=True)
                with conn_lock:
                    conn_status["poly_rtds"]["connected"] = True
                    conn_status["poly_rtds"]["last_error"] = None
                sub = {
                    "action": "subscribe",
                    "subscriptions": [
                        {
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": '{"symbol":"btc/usd"}',
                        }
                    ],
                }
                await ws.send(json.dumps(sub))
                backoff = 1.0

                async def rtds_ping():
                    while True:
                        await asyncio.sleep(5)
                        try:
                            await ws.send("PING")
                        except Exception:
                            break
                asyncio.create_task(rtds_ping())

                async for msg in ws:
                    try:
                        if msg.strip() in ("PONG", "pong", ""):
                            continue
                        d = json.loads(msg)
                        topic = d.get("topic", "")
                        msg_type = d.get("type", "")
                        payload = d.get("payload") or {}

                        if isinstance(payload.get("data"), list):
                            for item in payload["data"]:
                                ts_p = item.get("timestamp")
                                val = item.get("value")
                                if val is not None:
                                    with state_lock:
                                        state["chainlink_price"] = float(val)
                                        state["chainlink_ts"] = ts_p or ""

                        if topic == "crypto_prices_chainlink" and msg_type == "update":
                            sym = (payload.get("symbol") or "").lower()
                            val = payload.get("value")
                            if "btc" in sym and val is not None:
                                with state_lock:
                                    state["chainlink_price"] = float(val)
                                    state["chainlink_ts"] = payload.get("timestamp", "")
                        with conn_lock:
                            conn_status["poly_rtds"]["last_msg_time"] = time.time()
                            conn_status["poly_rtds"]["msg_count"] += 1
                    except json.JSONDecodeError:
                        pass
                    except Exception:
                        pass
        except Exception as e:
            err_str = f"{type(e).__name__}: {e}"
            print(f"[dashboard] RTDS error: {err_str}", file=sys.stderr, flush=True)
            with conn_lock:
                conn_status["poly_rtds"]["connected"] = False
                conn_status["poly_rtds"]["last_error"] = err_str
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _clob_thread_loop():
    """Sync CLOB WS consumer — runs in its own thread, never blocks event loop."""
    from websockets.sync.client import connect as ws_connect
    backoff = 1.0
    msg_local = 0

    while True:
        with state_lock:
            token_ids = list(state.get("market_token_ids") or [])

        if not token_ids:
            time.sleep(5)
            continue

        try:
            with ws_connect(POLY_CLOB_WS, close_timeout=5, ping_interval=None) as ws:
                print(f"[clob-thread] Connected, tokens: {len(token_ids)}", flush=True)
                with conn_lock:
                    conn_status["poly_clob"]["connected"] = True
                    conn_status["poly_clob"]["last_error"] = None
                sub = {"assets_ids": token_ids, "type": "market"}
                ws.send(json.dumps(sub))
                backoff = 1.0
                last_flush = time.monotonic()
                last_token_check = time.monotonic()

                while True:
                    try:
                        msg = ws.recv(timeout=1.0)
                    except TimeoutError:
                        # Flush and check tokens
                        flush_pending_changes(force=True)
                        with state_lock:
                            cur = list(state.get("market_token_ids") or [])
                        if cur and cur != token_ids:
                            print("[clob-thread] Tokens changed, reconnecting...", flush=True)
                            with ob_lock:
                                orderbook["yes"] = {"bids": [], "asks": [], "ts": None}
                                orderbook["no"] = {"bids": [], "asks": [], "ts": None}
                            break
                        continue
                    except Exception:
                        break

                    try:
                        data = json.loads(msg)
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict):
                                    process_clob_msg(item)
                        elif isinstance(data, dict):
                            process_clob_msg(data)

                        msg_local += 1
                        with conn_lock:
                            conn_status["poly_clob"]["last_msg_time"] = time.time()
                            conn_status["poly_clob"]["msg_count"] += 1
                            cnt = conn_status["poly_clob"]["msg_count"]
                        if cnt <= 3 or cnt % 2000 == 0:
                            print(f"[clob-thread] msg#{cnt}", flush=True)
                    except json.JSONDecodeError:
                        pass
                    except Exception as exc:
                        if msg_local < 10:
                            print(f"[clob-thread] error: {exc}", file=sys.stderr, flush=True)

                    # Flush batched changes every 200ms
                    now = time.monotonic()
                    if (now - last_flush) >= 0.2:
                        flush_pending_changes()
                        last_flush = now

                    # Check tokens every 2s
                    if (now - last_token_check) >= 2.0:
                        last_token_check = now
                        with state_lock:
                            cur = list(state.get("market_token_ids") or [])
                        if cur and cur != token_ids:
                            print("[clob-thread] Tokens changed, reconnecting...", flush=True)
                            with ob_lock:
                                orderbook["yes"] = {"bids": [], "asks": [], "ts": None}
                                orderbook["no"] = {"bids": [], "asks": [], "ts": None}
                            break

        except Exception as e:
            err_str = f"{type(e).__name__}: {e}"
            print(f"[clob-thread] error: {err_str}", file=sys.stderr, flush=True)
            with conn_lock:
                conn_status["poly_clob"]["connected"] = False
                conn_status["poly_clob"]["last_error"] = err_str
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def consume_poly_clob():
    """Wrapper: launch sync CLOB consumer in a daemon thread."""
    t = threading.Thread(target=_clob_thread_loop, daemon=True, name="clob-ws")
    t.start()
    print("[clob-thread] Started CLOB WS thread", flush=True)
    # Keep the async task alive so it doesn't get restarted
    while True:
        await asyncio.sleep(60)
