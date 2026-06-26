"""Orderbook: REST fetching, WS message processing, resolved detection."""
import json
import sys
import time as _time

try:
    import requests as req_lib
except ImportError:
    req_lib = None

from .config import CLOB_BOOK_URL, OB_MAX_LEVELS
from .state import (state, state_lock, orderbook, ob_lock,
                    _market_rediscovery_requested, _clob_msg_count)
from .helpers import _now_utc

_rest_hashes = {}
_rest_fetch_count = 0

# Throttle: batch price_change deltas, apply every 200ms max
_pending_changes = []  # list of (ob_side, price, size, is_bid)
_last_apply_time = 0.0
_APPLY_INTERVAL = 0.2  # seconds
_price_change_skip_count = 0


def flush_pending_changes(force=False):
    """Apply batched price_change deltas to orderbook. Called periodically."""
    global _last_apply_time, _price_change_skip_count
    now = _time.monotonic()
    if not force and (now - _last_apply_time) < _APPLY_INTERVAL:
        return  # too soon
    if not _pending_changes:
        _last_apply_time = now
        return

    # Deduplicate: keep last value per (ob_side, price, is_bid)
    latest = {}
    for ob_side, price, size, is_bid in _pending_changes:
        latest[(ob_side, round(price, 4), is_bid)] = (ob_side, price, size, is_bid)
    _pending_changes.clear()
    _last_apply_time = now

    updates = list(latest.values())

    # Group by ob_side to minimize lock acquisitions
    sides_changed = set()
    with ob_lock:
        for ob_side, price, size, is_bid in updates:
            levels = orderbook[ob_side]["bids"] if is_bid else orderbook[ob_side]["asks"]
            found = False
            for i, lv in enumerate(levels):
                if abs(lv[0] - price) < 0.0001:
                    if size <= 0:
                        levels.pop(i)
                    else:
                        lv[1] = size
                    found = True
                    break
            if not found and size > 0:
                levels.append([price, size])
            sides_changed.add((ob_side, is_bid))

        now_ts = _now_utc().isoformat()
        for ob_side in ("yes", "no"):
            if any(s == ob_side for s, _ in sides_changed):
                orderbook[ob_side]["bids"].sort(key=lambda x: -x[0])
                orderbook[ob_side]["bids"] = orderbook[ob_side]["bids"][:OB_MAX_LEVELS]
                orderbook[ob_side]["asks"].sort(key=lambda x: x[0])
                orderbook[ob_side]["asks"] = orderbook[ob_side]["asks"][:OB_MAX_LEVELS]
                orderbook[ob_side]["ts"] = now_ts
                orderbook[ob_side]["levels"] = max(
                    len(orderbook[ob_side]["bids"]), len(orderbook[ob_side]["asks"]))
                orderbook[ob_side]["total_bid_vol"] = sum(b[1] for b in orderbook[ob_side]["bids"])
                orderbook[ob_side]["total_ask_vol"] = sum(a[1] for a in orderbook[ob_side]["asks"])
                orderbook[ob_side]["src"] = "ws_delta"

    for ob_side in set(s for s, _ in sides_changed):
        _update_ob_tob(ob_side)


def _resolve_ob_side(asset_id):
    with state_lock:
        yes_tid = state.get("yes_token_id")
        no_tid = state.get("no_token_id")
    if asset_id == no_tid:
        return "no"
    elif asset_id == yes_tid:
        return "yes"
    return None


def _update_ob_tob(side):
    with ob_lock:
        bids = orderbook[side].get("bids", [])
        asks = orderbook[side].get("asks", [])
        bid_p = bids[0][0] if bids else None
        bid_v = bids[0][1] if bids else None
        ask_p = asks[0][0] if asks else None
        ask_v = asks[0][1] if asks else None
    with state_lock:
        pfx = "poly_" + side + "_"
        state[pfx + "bid"] = bid_p
        state[pfx + "bid_vol"] = bid_v
        state[pfx + "ask"] = ask_p
        state[pfx + "ask_vol"] = ask_v


def _is_resolved_book(bids, asks):
    if not bids and not asks:
        return False
    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 1
    if best_bid <= 0.05 or best_ask >= 0.95:
        return True
    if best_ask - best_bid > 0.85:
        return True
    return False


def fetch_orderbook_rest(token_id, side_name):
    if not req_lib or not token_id:
        return None, None
    try:
        resp = req_lib.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=5)
        if resp.status_code != 200:
            print(f"[ob-rest] {side_name} HTTP {resp.status_code}", file=sys.stderr, flush=True)
            return None, None
        data = resp.json()

        api_hash = data.get("hash", "")
        prev_hash = _rest_hashes.get(side_name)
        if api_hash and api_hash != prev_hash:
            if prev_hash is not None:
                print(f"[ob-rest] {side_name} CHANGED! hash={api_hash[:16]}", flush=True)
            _rest_hashes[side_name] = api_hash

        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []
        bids = sorted(
            [[float(b["price"]), float(b["size"])] for b in bids_raw if b.get("price") and float(b.get("size", 0)) > 0],
            key=lambda x: -x[0]
        )[:OB_MAX_LEVELS]
        asks = sorted(
            [[float(a["price"]), float(a["size"])] for a in asks_raw if a.get("price") and float(a.get("size", 0)) > 0],
            key=lambda x: x[0]
        )[:OB_MAX_LEVELS]
        return bids, asks
    except Exception as e:
        print(f"[ob-rest] {side_name} error: {e}", file=sys.stderr, flush=True)
        return None, None


def fetch_deep_orderbook():
    global _rest_fetch_count
    with state_lock:
        yes_tid = state.get("yes_token_id")
        no_tid = state.get("no_token_id")

    now_ts = _now_utc().isoformat()
    resolved_detected = False
    _rest_fetch_count += 1

    for side_name, tid in [("no", no_tid), ("yes", yes_tid)]:
        bids, asks = fetch_orderbook_rest(tid, side_name)
        if bids is None and asks is None:
            continue

        if _is_resolved_book(bids or [], asks or []):
            resolved_detected = True

        # Debug: log actual prices every 20th fetch
        if _rest_fetch_count % 20 == 1 and bids and asks:
            print(f"[ob-debug] {side_name.upper()} tid=...{str(tid)[-8:]} "
                  f"best_bid={bids[0][0]:.4f} best_ask={asks[0][0]:.4f} "
                  f"levels={len(bids)}b/{len(asks)}a", flush=True)

        with ob_lock:
            orderbook[side_name]["bids"] = bids or []
            orderbook[side_name]["asks"] = asks or []
            orderbook[side_name]["ts"] = now_ts
            orderbook[side_name]["levels"] = max(len(bids or []), len(asks or []))
            orderbook[side_name]["total_bid_vol"] = sum(b[1] for b in (bids or []))
            orderbook[side_name]["total_ask_vol"] = sum(a[1] for a in (asks or []))
            orderbook[side_name]["src"] = "rest"

        with state_lock:
            if side_name == "no":
                state["poly_no_bid"] = bids[0][0] if bids else None
                state["poly_no_bid_vol"] = bids[0][1] if bids else None
                state["poly_no_ask"] = asks[0][0] if asks else None
                state["poly_no_ask_vol"] = asks[0][1] if asks else None
            else:
                state["poly_yes_bid"] = bids[0][0] if bids else None
                state["poly_yes_bid_vol"] = bids[0][1] if bids else None
                state["poly_yes_ask"] = asks[0][0] if asks else None
                state["poly_yes_ask_vol"] = asks[0][1] if asks else None

    with state_lock:
        was_resolved = state.get("market_resolved", False)
        if resolved_detected and not was_resolved:
            state["market_resolved"] = True
            state["market_resolved_ts"] = now_ts
            _market_rediscovery_requested[0] = True
            print(f"[ob-rest] MARKET RESOLVED detected at {now_ts}! Requesting rediscovery.", flush=True)
        elif not resolved_detected and was_resolved:
            state["market_resolved"] = False
            state["market_resolved_ts"] = None
            print(f"[ob-rest] Market active again.", flush=True)


def process_clob_msg(data):
    ev = data.get("event_type")
    _clob_msg_count[0] += 1

    if ev == "book":
        asset_id = data.get("asset_id", "")
        side = _resolve_ob_side(asset_id)
        if not side:
            bids_raw = data.get("bids") or []
            bid1 = float(bids_raw[0]["price"]) if bids_raw else 0.5
            side = "no" if bid1 < 0.5 else "yes"

        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []
        bids = sorted(
            [[float(b["price"]), float(b["size"])] for b in bids_raw if b.get("price") and float(b.get("size", 0)) > 0],
            key=lambda x: -x[0]
        )[:OB_MAX_LEVELS]
        asks = sorted(
            [[float(a["price"]), float(a["size"])] for a in asks_raw if a.get("price") and float(a.get("size", 0)) > 0],
            key=lambda x: x[0]
        )[:OB_MAX_LEVELS]

        now_ts = _now_utc().isoformat()
        with ob_lock:
            orderbook[side]["bids"] = bids
            orderbook[side]["asks"] = asks
            orderbook[side]["ts"] = now_ts
            orderbook[side]["levels"] = max(len(bids), len(asks))
            orderbook[side]["total_bid_vol"] = sum(b[1] for b in bids)
            orderbook[side]["total_ask_vol"] = sum(a[1] for a in asks)
            orderbook[side]["src"] = "ws_book"

        _update_ob_tob(side)

        if _clob_msg_count[0] <= 3 or _clob_msg_count[0] % 500 == 0:
            print(f"[clob-ws] BOOK snapshot {side}: {len(bids)}b/{len(asks)}a", flush=True)

    elif ev == "price_change":
        # Batch price changes — don't process inline, just queue them
        changes = data.get("price_changes") or data.get("changes") or []

        if _clob_msg_count[0] <= 5:
            print(f"[clob-ws] price_change: {len(changes)} changes, keys={list(data.keys())}", flush=True)

        for chg in changes:
            asset_id = chg.get("asset_id", data.get("asset_id", ""))
            ob_side = _resolve_ob_side(asset_id)
            if not ob_side:
                continue
            try:
                price = float(chg["price"])
                size = float(chg.get("size", 0))
            except (KeyError, ValueError, TypeError):
                continue
            chg_side = (chg.get("side") or "").upper()
            if chg_side in ("BUY", "BID"):
                is_bid = True
            elif chg_side in ("SELL", "ASK"):
                is_bid = False
            else:
                is_bid = price <= 0.5  # fast heuristic, no lock
            _pending_changes.append((ob_side, price, size, is_bid))

    elif ev == "last_trade_price":
        asset_id = data.get("asset_id", "")
        ob_side = _resolve_ob_side(asset_id)
        try:
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
        except (ValueError, TypeError):
            return
        trade_side = data.get("side", "?")

        # Save last trade price to state — this is the REAL market price
        if ob_side and price > 0:
            with state_lock:
                state[f"poly_{ob_side}_last_trade"] = price
                state[f"poly_{ob_side}_last_trade_size"] = size
                state[f"poly_{ob_side}_last_trade_ts"] = _now_utc().isoformat()
                # Determine which side is winning (expensive)
                other = "yes" if ob_side == "no" else "no"
                other_last = state.get(f"poly_{other}_last_trade")
                if other_last:
                    expensive_price = max(price, other_last)
                    cheap_price = min(price, other_last)
                else:
                    expensive_price = price if price > 0.5 else 1 - price
                    cheap_price = 1 - expensive_price
                state["poly_last_trade_expensive"] = round(expensive_price, 4)
                state["poly_last_trade_cheap"] = round(cheap_price, 4)

        if _clob_msg_count[0] <= 3 or _clob_msg_count[0] % 500 == 0:
            print(f"[clob-ws] TRADE {ob_side}: {trade_side} {size}@{price:.3f}", flush=True)
