"""Utility functions: time, P-model, OFI, delta, signal."""
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta

import numpy as np
from collections import deque
from . import config as cfg
from .config import WINDOW_MINUTES
from .state import state, state_lock, trade_buffer, trade_buffer_lock, start_time

# Rolling price buffer for sigma calculation (5-min window for realized5_pmin)
_price_history = deque()  # (timestamp, price)


_warmup_ready = False  # True if DB warmup loaded enough data for 30-min sigma


def warmup_price_history():
    """Load recent prices from DB so sigma works immediately after restart."""
    global _warmup_ready
    from .database import db_load_recent_prices
    prices = db_load_recent_prices(35)  # load 35 min for 30-min sigma windows
    if prices:
        now = time.time()
        cutoff = now - 2100  # keep last 35 min
        for ts, price in prices:
            if ts >= cutoff and price > 0:
                _price_history.append((ts, price))
        print(f"[sigma] Warmed up with {len(_price_history)} prices from DB (35 min)", flush=True)
        # Compute initial sigma immediately
        _compute_sigma_now()
        # If we have enough data for 30-min windows, skip long warmup
        if len(_price_history) >= 600:  # ~10 prices/sec for 1 min = plenty for 30 min
            _warmup_ready = True
            print(f"[sigma] DB warmup sufficient — sigma ready, short warmup only", flush=True)


SIGMA_STATIC = 0.0005

# Cache of all computed sigma variants: {"trail5": 0.001, "realized5_pmin": 0.0003, ...}
_sigma_cache = {}


def _get_prices_window(minutes):
    """Get prices from last N minutes of _price_history."""
    now = time.time()
    cutoff = now - minutes * 60
    return [(t, p) for t, p in _price_history if t >= cutoff]


def _std(values):
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) if var > 0 else 0


def _returns(prices):
    ret = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0:
            ret.append((prices[i] - prices[i - 1]) / prices[i - 1])
    return ret


def _duration_min(ts_list):
    if len(ts_list) < 2:
        return 1
    d = (ts_list[-1] - ts_list[0]) / 60
    return max(d, 0.01)


def _compute_all_sigmas():
    """Compute all sigma variants from price history. Updates _sigma_cache."""
    global _sigma_cache
    if len(_price_history) < 20:
        return

    result = {"static": SIGMA_STATIC}

    for win in (5, 10, 30):
        pts = _get_prices_window(win)
        if len(pts) < 10:
            continue
        prices = [p for _, p in pts]
        ts_list = [t for t, _ in pts]
        dur = _duration_min(ts_list)
        avg = sum(prices) / len(prices)
        if avg <= 0:
            continue

        # trail: std of fractional prices (std/mean)
        trail = _std(prices) / avg
        result[f"trail{win}"] = trail
        result[f"trail{win}_pmin"] = trail / dur

        # realized: std of returns
        rets = _returns(prices)
        if len(rets) >= 3:
            realized = _std(rets)
            result[f"realized{win}"] = realized
            result[f"realized{win}_pmin"] = realized / dur

            # median: median absolute deviation of returns
            sorted_rets = sorted(rets)
            med = sorted_rets[len(sorted_rets) // 2]
            mad_vals = sorted(abs(r - med) for r in rets)
            mad = mad_vals[len(mad_vals) // 2]
            result[f"median{win}"] = med if med > 0 else realized
            result[f"median{win}_pmin"] = (med if med > 0 else realized) / dur
            result[f"mad{win}"] = mad if mad > 0 else realized

        # range: (high-low)/avg
        hi, lo = max(prices), min(prices)
        rng = (hi - lo) / avg if avg > 0 else 0
        result[f"range{win}"] = rng
        result[f"range{win}_pmin"] = rng / dur

        # max: max of rolling 1-min std within window
        max_std = 0
        for i in range(len(pts)):
            chunk_prices = []
            t0 = pts[i][0]
            for t, p in pts:
                if t0 - 60 <= t <= t0:
                    chunk_prices.append(p)
            if len(chunk_prices) >= 3:
                cavg = sum(chunk_prices) / len(chunk_prices)
                if cavg > 0:
                    s = _std(chunk_prices) / cavg
                    if s > max_std:
                        max_std = s
        result[f"max{win}"] = max_std
        result[f"max{win}_pmin"] = max_std  # max is already a peak measure

    _sigma_cache.clear()
    _sigma_cache.update(result)
    # Log sigma cache periodically (every ~60 calls = ~60s)
    if not hasattr(_compute_all_sigmas, '_log_cnt'):
        _compute_all_sigmas._log_cnt = 0
    _compute_all_sigmas._log_cnt += 1
    if _compute_all_sigmas._log_cnt % 60 == 1:
        keys = sorted(result.keys())
        vals = {k: f"{result[k]:.8f}" for k in keys[:5]}
        print(f"[sigma] cache updated: {len(result)} types, sample: {vals}", flush=True)

    # Update global state with the default sigma (for backward compat)
    sigma_key = cfg.SIGMA_TYPE
    sigma_val = result.get(sigma_key)
    if sigma_val is None:
        # Fallback to realized5_pmin
        sigma_val = result.get("realized5_pmin")
    if sigma_val and sigma_val > 0:
        with state_lock:
            state["sigma"] = sigma_val
            state["sigma_type"] = cfg.SIGMA_TYPE


def get_sigma_for_type(sigma_type):
    """Get sigma value for a specific sigma_type. Used by per-session paper trading."""
    if sigma_type == "static":
        return SIGMA_STATIC
    val = _sigma_cache.get(sigma_type)
    if val and val > 0:
        return val
    # Fallback to realized5_pmin
    return _sigma_cache.get("realized5_pmin") or _sigma_cache.get("realized5")


def _compute_sigma_now():
    """Compute sigma from current price history buffer."""
    _compute_all_sigmas()


def update_sigma():
    """Update sigma from rolling Binance price history."""
    now = time.time()
    cutoff = now - 2100  # keep 35 min buffer (for 30-min sigma windows)

    # Add current price
    with state_lock:
        price = state.get("binance_price")
    if price and price > 0:
        _price_history.append((now, price))

    # Trim old entries
    while _price_history and _price_history[0][0] < cutoff:
        _price_history.popleft()

    _compute_sigma_now()


def _now_utc():
    return datetime.now(timezone.utc)


def _current_window():
    now = _now_utc()
    slot = (now.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    start = now.replace(minute=slot, second=0, microsecond=0)
    end = start + timedelta(minutes=WINDOW_MINUTES)
    return start, end


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def compute_p_model(delta, sigma, tau_minutes):
    if delta is None or sigma is None or tau_minutes is None or tau_minutes <= 0:
        return None
    sigma_usd = sigma * (state.get("binance_price") or state.get("chainlink_price") or 70000)
    if sigma_usd <= 0:
        return None

    # Tau mode: linear or sqrt
    if cfg.TAU_MODE == "linear":
        f_tau = tau_minutes
    elif cfg.TAU_MODE == "sqrt":
        f_tau = math.sqrt(tau_minutes)
    else:
        f_tau = 1.0

    if f_tau <= 0:
        f_tau = 0.1

    snr = abs(delta) / (sigma_usd * f_tau)
    p = _phi(cfg.P_MODEL_KAPPA * snr)
    with state_lock:
        state["snr"] = round(snr, 3)
    return round(p, 4)


def get_strategy_formula():
    """Return human-readable formula string for display on dashboard."""
    tau_str = "τ" if cfg.TAU_MODE == "linear" else "√τ"
    return (f"p = Φ({cfg.P_MODEL_KAPPA} · |Δ| / ({cfg.SIGMA_TYPE} · {tau_str}))"
            f"  |  Δ≥${cfg.DELTA_THRESHOLD}  p≥{cfg.P_MODEL_THRESHOLD}  side={cfg.TRADE_SIDE}")


def compute_ofi():
    now = time.time()
    with trade_buffer_lock:
        cutoff = now - 300
        while trade_buffer and trade_buffer[0][0] < cutoff:
            trade_buffer.pop(0)

        ofi_1 = ofi_3 = ofi_5 = 0
        buy_1 = sell_1 = 0
        count_1 = 0
        for ts, notional, is_sell in trade_buffer:
            age = now - ts
            val = -notional if is_sell else notional
            if age <= 60:
                ofi_1 += val
                count_1 += 1
                if is_sell:
                    sell_1 += notional
                else:
                    buy_1 += notional
            if age <= 180:
                ofi_3 += val
            ofi_5 += val

    with state_lock:
        state["ofi_1min"] = round(ofi_1, 0)
        state["ofi_3min"] = round(ofi_3, 0)
        state["ofi_5min"] = round(ofi_5, 0)
        state["buy_vol_1min"] = round(buy_1, 0)
        state["sell_vol_1min"] = round(sell_1, 0)
        state["trade_count_1min"] = count_1


def update_signal():
    with state_lock:
        delta = state["delta_from_prev"]
        no_ask = state["poly_no_ask"]
        yes_ask = state["poly_yes_ask"]
        ofi = state["ofi_1min"]
        p = state.get("p_model")
        w_start_str = state.get("window_start_utc")
        uptime = state.get("uptime_s", 0)
        # Last trade prices from WS (real market prices)
        last_trade_expensive = state.get("poly_last_trade_expensive")
        no_last = state.get("poly_no_last_trade")
        yes_last = state.get("poly_yes_last_trade")

    if delta is None:
        with state_lock:
            state["signal"] = "WAITING FOR DATA"
            state["signal_side"] = None
            state["signal_entry"] = None
            state["signal_confidence"] = None
        return

    # Paused — don't generate signals
    with state_lock:
        if state.get("paper_paused"):
            state["signal"] = "PAUSED"
            state["signal_side"] = None
            state["signal_entry"] = None
            state["signal_confidence"] = None
            return

    # Warmup: need 30-min sigma data. If DB warmup loaded enough, only wait 60s for streams
    warmup_time = 60 if _warmup_ready else 1800
    if uptime < warmup_time:
        remaining = warmup_time - uptime
        with state_lock:
            state["signal"] = f"WARMUP ({remaining}s remaining)"
            state["signal_side"] = None
            state["signal_entry"] = None
            state["signal_confidence"] = None
        return

    # Strategy: entry_min=3 — wait until 3 minutes into the window
    elapsed_min = 0
    if w_start_str:
        try:
            w_start = datetime.fromisoformat(w_start_str)
            elapsed_min = (_now_utc() - w_start).total_seconds() / 60
        except Exception:
            pass

    signal = "NO SIGNAL"
    side = None
    entry = None
    confidence = None

    if elapsed_min < 3:
        signal = f"WAITING (min {elapsed_min:.1f}/3)"
    elif abs(delta) < cfg.DELTA_THRESHOLD:
        signal = f"DELTA LOW ({delta:+.0f} < {cfg.DELTA_THRESHOLD})"
    elif p is not None and p < cfg.P_MODEL_THRESHOLD:
        signal = f"P-MODEL LOW ({p:.2%} < {cfg.P_MODEL_THRESHOLD:.0%})"
    else:
        # Entry price priority:
        # 1. Last trade price from WS (real market price)
        # 2. Orderbook ask (fallback)
        entry_price = None
        price_src = "none"

        if last_trade_expensive and last_trade_expensive > 0.5:
            entry_price = last_trade_expensive
            price_src = "last_trade"
        else:
            if yes_ask and no_ask:
                entry_price = max(yes_ask, no_ask)
            elif yes_ask:
                entry_price = yes_ask
            elif no_ask:
                entry_price = no_ask
            price_src = "orderbook"

        print(f"[signal] CHECK: delta={delta:+.1f} elapsed={elapsed_min:.1f}min "
              f"entry={entry_price} src={price_src} p={p}", flush=True)

        # Side filter
        if cfg.TRADE_SIDE == "NO":
            # Only take NO (DOWN) bets
            if delta <= -cfg.DELTA_THRESHOLD:
                signal = "BUY NO (DOWN)"
                side = "NO"
                entry = entry_price
                confidence = p
            else:
                signal = f"WRONG SIDE (UP, need DOWN)"
        elif cfg.TRADE_SIDE == "YES":
            if delta >= cfg.DELTA_THRESHOLD:
                signal = "BUY YES (UP)"
                side = "YES"
                entry = entry_price
                confidence = p
            else:
                signal = f"WRONG SIDE (DOWN, need UP)"
        else:  # BOTH
            if delta <= -cfg.DELTA_THRESHOLD:
                signal = "BUY NO (DOWN)"
                side = "NO"
                entry = entry_price
                confidence = p
            elif delta >= cfg.DELTA_THRESHOLD:
                signal = "BUY YES (UP)"
                side = "YES"
                entry = entry_price
                confidence = p

        # Sanity: entry should be > 0.5 (we're buying the winning side)
        if entry is not None and entry < 0.5:
            signal = f"PRICE ERROR ({entry:.2f}<0.50)"
            side = None
            entry = None
            confidence = None

        # Max entry price filter
        if entry is not None and entry > cfg.MAX_ENTRY_PRICE:
            signal = f"ENTRY TOO HIGH ({entry:.2f}>{cfg.MAX_ENTRY_PRICE})"
            side = None
            entry = None
            confidence = None

    with state_lock:
        state["signal"] = signal
        state["signal_side"] = side
        state["signal_entry"] = entry
        state["signal_confidence"] = confidence


def update_delta():
    with state_lock:
        cl = state["chainlink_price"]
        bn = state["binance_price"]
        # Binance as primary price source — more reliable than Chainlink RTDS oracle
        price = bn or cl
        w_open = state["window_open_price"]
        prev = state["prev_close_price"]

        if price and w_open is None:
            state["window_open_price"] = price
            w_open = price
        if price and prev is None and w_open is not None:
            state["prev_close_price"] = w_open
            prev = w_open

        if price and w_open:
            state["delta_from_open"] = round(price - w_open, 2)
        if price and prev:
            state["delta_from_prev"] = round(price - prev, 2)

        delta = state["delta_from_prev"]
        if delta is not None:
            now = _now_utc()
            w_end_str = state.get("window_end_utc")
            if w_end_str:
                try:
                    w_end = datetime.fromisoformat(w_end_str)
                    tau = max((w_end - now).total_seconds() / 60, 0.1)
                except Exception:
                    tau = 7.5
            else:
                tau = 7.5
            p = compute_p_model(delta, state["sigma"], tau)
            state["p_model"] = p

        state["last_update"] = _now_utc().isoformat()
        state["uptime_s"] = int(time.time() - start_time)


# ── Weighted scoring model ──
_weighted_model = None  # loaded once from optimized_weights.json


def load_weighted_model():
    """Load optimized weights from JSON file."""
    global _weighted_model
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "optimized_weights.json")
    if not os.path.exists(path):
        print(f"[weighted] optimized_weights.json not found at {path}", flush=True)
        return False
    with open(path) as f:
        data = json.load(f)
    _weighted_model = {
        "feature_names": data["feature_names"],
        "weights": np.array(data["weights"]),
        "threshold": data["threshold"],
        "max_entry": data["max_entry"],
        "means": np.array(data["train_means"]),
        "stds": np.array(data["train_stds"]),
    }
    print(f"[weighted] Loaded {len(data['feature_names'])} weights, "
          f"threshold={data['threshold']:.3f}, max_entry={data['max_entry']:.3f}", flush=True)
    return True


def compute_weighted_score(shared):
    """Compute weighted score from shared market data.
    Returns (score, threshold, max_entry) or (None, None, None) if model not loaded.
    """
    if _weighted_model is None:
        return None, None, None

    delta = shared.get("delta_from_prev")
    btc = shared.get("binance_price", 0)
    tau = shared.get("tau", 2)
    sigmas = shared.get("sigmas", {})
    if delta is None or btc <= 0:
        return None, None, None

    ofi_1 = shared.get("ofi_1min", 0) or 0
    ofi_5 = shared.get("ofi_5min", 0) or 0
    buy_vol = shared.get("buy_vol_1min", 0) or 0
    sell_vol = shared.get("sell_vol_1min", 0) or 0
    trade_count = shared.get("trade_count_1min", 0) or 0
    hour = shared.get("hour", 0)

    # SNR variants
    snr_vals = {}
    for st in ["static", "realized5_pmin", "trail5_pmin", "range5_pmin"]:
        sv = sigmas.get(st, 0)
        if sv > 0 and btc > 0:
            snr_vals[st] = abs(delta) / (sv * btc * math.sqrt(max(tau, 0.1)))
        else:
            snr_vals[st] = 0

    # Build feature dict matching FEATURE_NAMES order
    fdict = {
        "abs_delta": abs(delta),
        "delta_pct": delta / btc if btc > 0 else 0,
        "snr_static": snr_vals.get("static", 0),
        "snr_realized5_pmin": snr_vals.get("realized5_pmin", 0),
        "snr_trail5_pmin": snr_vals.get("trail5_pmin", 0),
        "snr_range5_pmin": snr_vals.get("range5_pmin", 0),
        "p_k0.7_static": _phi(0.7 * snr_vals.get("static", 0)),
        "p_k1.0_static": _phi(1.0 * snr_vals.get("static", 0)),
        "p_k0.7_realized5_pmin": _phi(0.7 * snr_vals.get("realized5_pmin", 0)),
        "ofi_1min": ofi_1,
        "ofi_5min": ofi_5,
        "vol_imbalance": (buy_vol - sell_vol) / max(buy_vol + sell_vol, 1),
        "trade_count_1min": trade_count,
        "is_night": 1 if 0 <= hour < 6 else 0,
        "is_weekend": 1 if shared.get("dow", 0) >= 5 else 0,
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "sigma_static": sigmas.get("static", SIGMA_STATIC),
        "sigma_realized5_pmin": sigmas.get("realized5_pmin", 0),
        "sigma_trail5_pmin": sigmas.get("trail5_pmin", 0),
        "entry_expensive": shared.get("orderbook_entry", 0) or 0,
        "tau": tau,
    }

    # Build feature vector in correct order
    fvec = np.array([float(fdict.get(fn, 0)) for fn in _weighted_model["feature_names"]])
    fvec = np.nan_to_num(fvec)

    # Normalize and score — clamp z-scores to [-5, 5] to prevent
    # near-zero stds (e.g. sigma_static is constant) from exploding
    stds_safe = np.where(_weighted_model["stds"] > 1e-10, _weighted_model["stds"], 1.0)
    fvec_norm = (fvec - _weighted_model["means"]) / stds_safe
    fvec_norm = np.clip(fvec_norm, -5.0, 5.0)
    score = float(np.dot(_weighted_model["weights"], fvec_norm))

    return score, _weighted_model["threshold"], _weighted_model["max_entry"]
