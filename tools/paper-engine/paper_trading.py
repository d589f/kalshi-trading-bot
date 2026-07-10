"""Paper trading emulator — multi-session. Each session has its own strategy config,
balance, and trade history. All persisted to SQLite.
"""
import threading
import time
import math
import json as _json
import os as _os
from datetime import datetime, timezone

from .config import WINDOW_MINUTES
from . import config as _cfg
from .state import state as _live_state
from .database import (db_insert_paper_trade, db_resolve_paper_trade,
                       db_save_paper_config, db_load_paper_config,
                       db_load_paper_trades, db_load_active_paper_trades)

# ── NOOB fader strategy: lazy-loaded LightGBM model + isotonic calibrator.
# Predicts P(UP) from 25 non-orderbook features (Binance micro + time), then
# fades the market when |p_cal - yes_mid| > threshold in decision zone [60,180).
_NOOB_MODEL_PATH = "/root/doubletrade2notactual/models/new_noob_fresh.lgb"
_NOOB_CAL_PATH = "/root/doubletrade2notactual/models/new_noob_fresh_cal.json"
_noob_model = None
_noob_cal = None
_noob_load_error = None

_NOOB_FEATURES = [
    "binance_price", "chainlink_price",
    "delta_from_prev", "delta_from_prev",
    "abs_delta", "sign_delta",
    "ofi_1min", "ofi_3min", "ofi_5min",
    "buy_vol_1min", "sell_vol_1min", "trade_count_1min",
    "vol_imbalance",
    "sigma", "p_model", "snr",
    "bn_spread",
    "hour_utc", "hour_sin", "hour_cos", "day_of_week", "is_weekend",
    "sec_in_window", "frac_in_window", "tau_norm",
]

import os as _os
_UNCERTAINTY_EPS = float(_os.environ.get("UNCERTAINTY_EPS", "0.0"))



def _load_noob_model():
    """Lazy-load model + calibrator on first use. Cached thereafter."""
    global _noob_model, _noob_cal, _noob_load_error
    if _noob_model is not None or _noob_load_error is not None:
        return _noob_model, _noob_cal
    try:
        import lightgbm as lgb
        if not _os.path.exists(_NOOB_MODEL_PATH):
            _noob_load_error = f"model file missing: {_NOOB_MODEL_PATH}"
            return None, None
        if not _os.path.exists(_NOOB_CAL_PATH):
            _noob_load_error = f"calibrator missing: {_NOOB_CAL_PATH}"
            return None, None
        _noob_model = lgb.Booster(model_file=_NOOB_MODEL_PATH)
        _noob_cal = _json.load(open(_NOOB_CAL_PATH))
        print(f"[paper-noob] loaded model ({_noob_model.num_feature()} features) + "
              f"calibrator ({len(_noob_cal.get('X_thresholds', []))} pivots)",
              flush=True)
        return _noob_model, _noob_cal
    except Exception as e:
        _noob_load_error = f"load failed: {type(e).__name__}: {e}"
        print(f"[paper-noob] {_noob_load_error}", flush=True)
        return None, None

# ── Polymarket crypto fee (effective 2026-03-06) ──
FEE_RATE = 0.25
FEE_EXPONENT = 2


def calc_fee(shares, price):
    if price <= 0 or price >= 1:
        return 0.0
    fee = shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT
    return max(fee, 0.0001)


def _phi(x):
    import math
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


paper_lock = threading.RLock()

# ── Sessions: {session_id: {config: {...}, state: {...}, positions: [...], history: [...]}} ──
_sessions = {}

DEFAULT_CONFIG = {
    "name": "Default",
    "kappa": 0.5,
    "delta_threshold": 20,
    "p_model_threshold": 0.60,
    "sigma_type": "realized5_pmin",
    "tau_mode": "linear",
    "trade_side": "NO",
    "max_entry_price": 0.95,
    "stake": 10,
    "min_stake": 5,
    "paused": False,
    "strategy_type": "pmodel",
    "threshold_gap": 0.10,
    # PAPER_VARIANTS_PATCH_V1 — per-session knobs (defaults = legacy behavior)
    "entry_wait_min": 3.0,
    "liq_filter": "on",
    "vol_imb_kill": 0.0,
    "kill_hours": "",
    "ofi_align": "off",
    "ev_thr": 0.0,
    # MAKER_PAPER_PATCH_V1 — virtual resting-limit execution (Kalshi maker, fee $0)
    "exec_mode": "taker",          # "taker" (legacy) | "maker"
    "maker_ttl_min": 5.0,          # cancel unfilled after N minutes
    "maker_offset": 0.0,           # limit = own-side bid + offset
    "maker_fill_rule": "cons",     # "cons": print <= L-1c | "opt": print <= L
}

DEFAULT_STATE = {
    "balance": 10000.0,
    "starting_balance": 10000.0,
    "total_pnl": 0.0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_staked": 0.0,
    "buys_this_window": 0,
    "current_window": None,
    "last_buy_ts": 0,
    "last_signal": "",
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _save_session(sid):
    """Persist session config + state to DB."""
    s = _sessions.get(sid)
    if not s:
        return
    data = {}
    for k, v in s["config"].items():
        data[f"cfg_{k}"] = str(v)
    for k, v in s["state"].items():
        data[f"st_{k}"] = str(v)
    db_save_paper_config(data, session_id=sid)


def create_session(sid, config=None, balance=10000.0):
    """Create a new paper trading session."""
    with paper_lock:
        cfg = dict(DEFAULT_CONFIG)
        if config:
            cfg.update(config)
        st = dict(DEFAULT_STATE)
        st["balance"] = balance
        st["starting_balance"] = balance
        _sessions[sid] = {
            "config": cfg,
            "state": st,
            "positions": [],
            "history": [],
        }
        _save_session(sid)
        print(f"[paper] Created session '{sid}': {cfg['name']}", flush=True)
        return get_session_info(sid)


def delete_session(sid):
    """Delete a session."""
    with paper_lock:
        if sid in _sessions:
            del _sessions[sid]
            db_save_paper_config({"__deleted__": "true"}, session_id=sid)
            print(f"[paper] Deleted session '{sid}'", flush=True)
            return True
    return False


def update_session_config(sid, config_updates):
    """Update session config."""
    with paper_lock:
        s = _sessions.get(sid)
        if not s:
            return {"error": f"Session '{sid}' not found"}
        for k, v in config_updates.items():
            if k in s["config"]:
                # Type conversion
                if k in ("kappa", "delta_threshold", "p_model_threshold", "max_entry_price", "stake", "min_stake", "threshold_gap", "entry_wait_min", "vol_imb_kill", "ev_thr", "maker_ttl_min", "maker_offset"):
                    s["config"][k] = float(v)
                elif k == "paused":
                    s["config"][k] = bool(v)
                else:
                    s["config"][k] = v
        _save_session(sid)
        return {"status": "ok", "config": s["config"]}


def reset_session(sid, balance=None):
    """Reset session state (keep config)."""
    with paper_lock:
        s = _sessions.get(sid)
        if not s:
            return {"error": f"Session '{sid}' not found"}
        bal = balance or s["state"]["starting_balance"]
        s["state"] = dict(DEFAULT_STATE)
        s["state"]["balance"] = bal
        s["state"]["starting_balance"] = bal
        s["positions"] = []
        s["history"] = []
        _save_session(sid)
        return {"status": "reset", "balance": bal}


def restore_from_db():
    """Restore all sessions from DB on startup."""
    with paper_lock:
        all_configs = db_load_paper_config(session_id=None)  # load all
        # Group by session
        session_data = {}
        for key, val, sid in all_configs:
            if sid not in session_data:
                session_data[sid] = {}
            session_data[sid][key] = val

        for sid, data in session_data.items():
            if data.get("__deleted__") == "true":
                continue
            cfg = dict(DEFAULT_CONFIG)
            st = dict(DEFAULT_STATE)
            for k, v in data.items():
                if k.startswith("cfg_"):
                    real_key = k[4:]
                    if real_key in cfg:
                        if real_key in ("kappa", "delta_threshold", "p_model_threshold",
                                        "max_entry_price", "stake", "min_stake", "threshold_gap", "entry_wait_min", "vol_imb_kill", "ev_thr", "maker_ttl_min", "maker_offset"):
                            cfg[real_key] = float(v)
                        elif real_key == "paused":
                            cfg[real_key] = v.lower() == "true"
                        else:
                            cfg[real_key] = v
                elif k.startswith("st_"):
                    real_key = k[3:]
                    if real_key in st:
                        try:
                            if real_key in ("total_trades", "wins", "losses", "buys_this_window"):
                                st[real_key] = int(float(v))
                            elif real_key in ("balance", "starting_balance", "total_pnl",
                                              "total_staked", "last_buy_ts"):
                                st[real_key] = float(v)
                            else:
                                st[real_key] = v
                        except (ValueError, TypeError):
                            pass

            _sessions[sid] = {"config": cfg, "state": st, "positions": [], "history": []}

            # Load trades
            history_rows = db_load_paper_trades(200, session_id=sid)
            history = []
            for r in history_rows:
                history.append({
                    "side": r["side"], "entry": r["entry_price"],
                    "stake": r["stake"], "shares": r.get("shares") or 0,
                    "ob_shares": r.get("ob_shares") or 0, "ts": r["ts"],
                    "signal": r.get("signal"), "confidence": r.get("confidence"),
                    "delta": r.get("delta"), "window": r.get("window_start"),
                    "db_id": r["id"], "market_slug": r.get("market_slug"), "pnl": r.get("pnl", 0),
                    "result": r.get("result"), "resolved_ts": r.get("resolved_ts"),
                    "outcome": r.get("outcome"),
                })
            _sessions[sid]["history"] = history

            active_rows = db_load_active_paper_trades(session_id=sid)
            active = []
            for r in active_rows:
                active.append({
                    "side": r["side"], "entry": r["entry_price"],
                    "stake": r["stake"], "shares": r.get("shares") or 0,
                    "ob_shares": r.get("ob_shares") or 0, "ts": r["ts"],
                    "signal": r.get("signal"), "confidence": r.get("confidence"),
                    "delta": r.get("delta"), "window": r.get("window_start"),
                    "db_id": r["id"], "market_slug": r.get("market_slug"),
                })
            _sessions[sid]["positions"] = active

            print(f"[paper] Restored session '{sid}': {cfg['name']} "
                  f"bal=${st['balance']:.0f} trades={st['total_trades']} "
                  f"pnl=${st['total_pnl']:+.2f} active={len(active)}", flush=True)

        # Create default session if none exist
        if not _sessions:
            create_session("s1", {
                "name": "NO realized5",
                "kappa": 0.5, "delta_threshold": 20, "p_model_threshold": 0.60,
                "sigma_type": "realized5_pmin", "tau_mode": "linear",
                "trade_side": "NO", "max_entry_price": 0.95, "stake": 10,
            }, balance=10000.0)



# Liquidity kill-filter — added 2026-06-08 per workflow analysis
# 2026-06-10 CORRECTION: the "+$6,871 OOS lift" claim did NOT reproduce; filter kept
# for execution value only (see notes/2026-06-10_cap_liq_deep_research.md)
LIQ_SPREAD_KILL = 0.0142
LIQ_DEPTH_KILL  = 424.0
LIQ_FILTER_ENABLED = False  # disabled for LL 2026-06-16: PM-calibrated 0.0142 spread rejects all LL (natural 5-15c spread)

def _check_liquidity_paper(side, shared):
    """Returns (skip: bool, reason: str). FAIL-CLOSED on missing data."""
    if not LIQ_FILTER_ENABLED:
        return False, ""
    if side == "YES":
        ask = shared.get("poly_yes_ask")
        bid = shared.get("poly_yes_bid")
        ask_vol = shared.get("poly_yes_ask_vol")
        bid_vol = shared.get("poly_yes_bid_vol")
    else:
        ask = shared.get("poly_no_ask")
        bid = shared.get("poly_no_bid")
        ask_vol = shared.get("poly_no_ask_vol")
        bid_vol = shared.get("poly_no_bid_vol")
    if ask is None or bid is None:
        return True, "LIQ:no_quotes"
    spread = ask - bid
    if spread > LIQ_SPREAD_KILL:
        return True, f"LIQ:wide_spread {spread:.4f}>{LIQ_SPREAD_KILL}"
    # depth check is OPTIONAL — vol fields may be missing in shared dict
    if ask_vol is not None and bid_vol is not None:
        depth = (ask_vol or 0) + (bid_vol or 0)
        if depth <= LIQ_DEPTH_KILL:
            return True, f"LIQ:thin_book {depth:.0f}<={LIQ_DEPTH_KILL:.0f}"
    return False, ""


def _evaluate_signal(session_cfg, shared):
    """Evaluate entry signal for a session given shared market data.
    Returns (signal_text, side, entry_price, confidence) or None."""
    delta = shared.get("delta_from_open")  # ALIGNED to LIVE collector (window_open_price)
    if delta is None:
        return "WAITING", None, None, None

    elapsed_min = shared.get("elapsed_min", 0)
    _ew = float(session_cfg.get("entry_wait_min", 3) or 3)
    if elapsed_min < _ew:
        return f"WAIT ({elapsed_min:.1f}/{_ew:.1f})", None, None, None

    # ── Weighted model path ──
    if session_cfg.get("sigma_type") == "weighted":
        from .helpers import compute_weighted_score
        score, threshold, max_entry = compute_weighted_score(shared)
        if score is None:
            return "NO SCORE", None, None, None
        if score <= threshold:
            return f"SCORE LOW ({score:.2f})", None, None, None

        # Entry price
        entry_price = shared.get("last_trade_expensive")
        if not entry_price or entry_price <= 0.5:
            entry_price = shared.get("orderbook_entry")
        if not entry_price or entry_price <= 0.5:
            return "NO PRICE", None, None, None
        if entry_price > max_entry:
            return f"HIGH ({entry_price:.2f}>{max_entry:.2f})", None, None, None

        # Side by delta direction
        if delta < 0:
            _side = "NO"
            _skip, _reason = _check_liquidity_paper(_side, shared)
            if _skip:
                print(f"[paper:liq-skip] {_side} {_reason}", flush=True)
                return _reason, None, None, None
            return f"BUY NO (s={score:.2f})", "NO", entry_price, score
        elif delta > 0:
            _side = "YES"
            _skip, _reason = _check_liquidity_paper(_side, shared)
            if _skip:
                print(f"[paper:liq-skip] {_side} {_reason}", flush=True)
                return _reason, None, None, None
            return f"BUY YES (s={score:.2f})", "YES", entry_price, score
        return "NO SIGNAL", None, None, None

    # ── Classic P-model path ──
    dt = session_cfg["delta_threshold"]
    if abs(delta) < dt:
        return f"DELTA LOW ({delta:+.0f})", None, None, None

    # ── Solution #1: dual-confirmation gate (matches LIVE) ──
    _d_open = shared.get("delta_from_open")
    _d_prev = shared.get("delta_from_prev")
    if _d_open is not None and _d_prev is not None:
        if (_d_open >= 0) != (_d_prev >= 0):
            return f"AMBIGUOUS (open={_d_open:+.0f} prev={_d_prev:+.0f})", None, None, None

    _rb = _regime_block(session_cfg, delta, shared)
    if _rb:
        return _rb, None, None, None

    # Compute session-specific p-model using per-session sigma_type
    sigma_type = session_cfg.get("sigma_type", "realized5_pmin")
    sigma = shared.get("sigmas", {}).get(sigma_type) or shared.get("sigma")
    tau = shared.get("tau", 2)
    price_ref = shared.get("binance_price", 70000)

    if sigma and sigma > 0 and price_ref > 0:
        sigma_usd = sigma * price_ref
        if session_cfg["tau_mode"] == "linear":
            f_tau = max(tau, 0.1)
        elif session_cfg["tau_mode"] == "sqrt":
            f_tau = max(math.sqrt(tau), 0.1)
        else:
            f_tau = 1.0
        snr = abs(delta) / (sigma_usd * f_tau)
        p = _phi(session_cfg["kappa"] * snr)
    else:
        p = None

    pt = session_cfg["p_model_threshold"]
    if p is not None and p < pt:
        return f"P-LOW ({p:.1%}<{pt:.0%})", None, None, None

    # Entry price
    entry_price = shared.get("last_trade_expensive")
    if not entry_price or entry_price <= 0.5:
        entry_price = shared.get("orderbook_entry")
    if not entry_price or entry_price <= 0.5:
        return "NO PRICE", None, None, None
    if entry_price > session_cfg["max_entry_price"]:
        return f"HIGH ({entry_price:.2f})", None, None, None

    # Side filter
    side_cfg = session_cfg["trade_side"]
    if side_cfg == "NO":
        if delta <= -dt:
            _skip, _reason = _session_liq(session_cfg, "NO", shared)
            if _skip:
                print(f"[paper:liq-skip] NO {_reason}", flush=True)
                return _reason, None, None, None
            return "BUY NO", "NO", entry_price, p
        return "WRONG SIDE", None, None, None
    elif side_cfg == "YES":
        if delta >= dt:
            _skip, _reason = _session_liq(session_cfg, "YES", shared)
            if _skip:
                print(f"[paper:liq-skip] YES {_reason}", flush=True)
                return _reason, None, None, None
            return "BUY YES", "YES", entry_price, p
        return "WRONG SIDE", None, None, None
    else:  # BOTH
        if delta <= -dt:
            _skip, _reason = _session_liq(session_cfg, "NO", shared)
            if _skip:
                print(f"[paper:liq-skip] NO {_reason}", flush=True)
                return _reason, None, None, None
            return "BUY NO", "NO", entry_price, p
        elif delta >= dt:
            _skip, _reason = _session_liq(session_cfg, "YES", shared)
            if _skip:
                print(f"[paper:liq-skip] YES {_reason}", flush=True)
                return _reason, None, None, None
            return "BUY YES", "YES", entry_price, p
    return "NO SIGNAL", None, None, None


def _evaluate_signal_noob_fader(session_cfg, shared):
    """NOOB fader signal evaluator.

    Runs the 25-feature LightGBM model on live shared data to get p_cal,
    compares with market yes_mid. Fires in decision zone [60, 180)s:
      p_cal > market + TH   -> BUY YES (fade DOWN overpricing)
      p_cal < market - TH   -> BUY NO  (fade UP overpricing)
    Same return signature as _evaluate_signal so _try_open works unchanged.
    """
    model, cal = _load_noob_model()
    if model is None:
        return (f"NOOB_LOAD_ERR: {_noob_load_error}", None, None, None)

    # ── 1. Gate: only fire in decision zone [60, 180)s ──
    win_start_iso = shared.get("window_start_utc") or shared.get("window_start")
    if not win_start_iso:
        return ("NO WINDOW", None, None, None)
    try:
        ws_dt = datetime.fromisoformat(win_start_iso.replace("Z", "+00:00"))
    except Exception:
        return ("BAD WINDOW TS", None, None, None)
    now_dt = datetime.now(timezone.utc)
    sec_in_window = (now_dt - ws_dt).total_seconds()
    if sec_in_window < 60:
        return (f"EARLY (sec={sec_in_window:.0f})", None, None, None)
    if sec_in_window >= 180:
        return (f"LATE (sec={sec_in_window:.0f})", None, None, None)

    # ── 2. Market price ──
    yb = shared.get("poly_yes_bid")
    ya = shared.get("poly_yes_ask")
    no_a = shared.get("poly_no_ask")
    if yb is None or ya is None or yb <= 0 or ya >= 1 or ya <= yb:
        return ("NO BOOK", None, None, None)
    market = (yb + ya) / 2.0

    # ── 3. Build 25-feature vector ──
    try:
        import numpy as _np
        def g(k, default=0.0):
            v = shared.get(k)
            return float(v) if v is not None else float(default)

        delta_open = g("delta_from_prev")
        abs_delta = abs(delta_open)
        sign_delta = 1.0 if delta_open > 0 else (-1.0 if delta_open < 0 else 0.0)

        buy_v = g("buy_vol_1min"); sell_v = g("sell_vol_1min")
        vol_imb = buy_v / (buy_v + sell_v) if (buy_v + sell_v) > 0 else 0.5

        hour_utc = ws_dt.hour
        hour_sin = math.sin(2 * math.pi * hour_utc / 24)
        hour_cos = math.cos(2 * math.pi * hour_utc / 24)
        dow = ws_dt.weekday()
        is_weekend = 1 if dow >= 5 else 0

        frac = max(0.0, min(1.0, sec_in_window / 300.0))
        tau_norm = 1.0 - frac

        feats = _np.array([[
            g("binance_price"), g("chainlink_price"),
            delta_open, g("delta_from_prev"),
            abs_delta, sign_delta,
            g("ofi_1min"), g("ofi_3min"), g("ofi_5min"),
            buy_v, sell_v, g("trade_count_1min"),
            vol_imb,
            g("sigma"), g("p_model"), g("snr"),
            g("bn_spread"),
            float(hour_utc), hour_sin, hour_cos, float(dow), float(is_weekend),
            float(sec_in_window), frac, tau_norm,
        ]], dtype=_np.float32)
        _np.nan_to_num(feats, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    except Exception as e:
        return (f"FEAT_ERR: {type(e).__name__}", None, None, None)

    # ── 4. Predict + calibrate ──
    try:
        p_raw = float(model.predict(feats)[0])
        xs = cal.get("X_thresholds") or cal.get("x_thresholds") or []
        ys = cal.get("y_thresholds") or cal.get("y") or []
        if xs and ys:
            p_cal = float(_np.interp(p_raw, xs, ys))
        else:
            p_cal = p_raw
    except Exception as e:
        return (f"PREDICT_ERR: {type(e).__name__}", None, None, None)

    gap = p_cal - market
    TH = float(session_cfg.get("threshold_gap", 0.10))
    max_entry = float(session_cfg.get("max_entry_price", 0.95))

    # Uncertainty gate: skip entries where model is not confident enough
    # (reduces flips between paper and prod when p_cal is near 0.5)
    # Per-session uncertainty gate: session_cfg.uncertainty_eps overrides env var
    _session_eps = float(session_cfg.get("uncertainty_eps", _UNCERTAINTY_EPS))
    if _session_eps > 0 and abs(p_cal - 0.5) < _session_eps:
        return (f"UNCERTAIN (|p-0.5|={abs(p_cal-0.5):.3f}<{_session_eps})", None, None, None)


    # ── 5. Trigger direction based on gap ──
    if gap > TH:
        # Model thinks UP more likely than market -> buy YES at yes_ask
        entry = float(ya)
        if entry > max_entry:
            return (f"YES HIGH ({entry:.2f}>{max_entry})", None, None, None)
        return (f"BUY YES (gap={gap:+.3f} p={p_cal:.2f} mkt={market:.2f})",
                "YES", entry, p_cal)
    elif gap < -TH:
        # Model thinks DOWN more likely -> buy NO at no_ask
        if no_a is None or no_a <= 0 or no_a >= 1:
            # fall back to 1 - yes_bid
            no_a = 1.0 - float(yb)
        entry = float(no_a)
        if entry > max_entry:
            return (f"NO HIGH ({entry:.2f}>{max_entry})", None, None, None)
        return (f"BUY NO (gap={gap:+.3f} p={p_cal:.2f} mkt={market:.2f})",
                "NO", entry, 1.0 - p_cal)

    return (f"NO EDGE (gap={gap:+.3f})", None, None, None)



# ── PAPER_VARIANTS_PATCH_V1 helpers ───────────────────────────────────────
def _session_liq(cfg, side, shared):
    """Per-session liquidity gate: spread/depth (if liq_filter on) + vol-imbalance.
    The spread/depth check is a Polymarket-microstructure filter (~1.4c threshold)
    that is miscalibrated for Limitless's inherently wider books, so it defaults
    OFF on the Limitless venue (the formula's own gates still apply)."""
    _is_ll = getattr(_cfg, "VENUE", "polymarket") == "limitless"
    if (not _is_ll) and str(cfg.get("liq_filter", "on")) == "on":
        skip, reason = _check_liquidity_paper(side, shared)
        if skip:
            return skip, reason
    vk = float(cfg.get("vol_imb_kill", 0) or 0)
    if vk > 0:
        bv = shared.get("buy_vol_1min") or 0
        sv = shared.get("sell_vol_1min") or 0
        tot = bv + sv
        if tot > 0:
            imb = (bv - sv) / tot
            if side == "YES" and imb < -vk:
                return True, f"VOLIMB {imb:+.2f}<-{vk} vs YES"
            if side == "NO" and imb > vk:
                return True, f"VOLIMB {imb:+.2f}>{vk} vs NO"
    return False, ""


def _regime_block(cfg, delta, shared):
    """F5 kill-filters: block configured UTC hours + require OFI agree with side."""
    kh = str(cfg.get("kill_hours", "") or "")
    if kh:
        ws = shared.get("window_start") or shared.get("window_start_utc") or ""
        hr = -1
        try:
            hr = int(ws[11:13])
        except Exception:
            hr = shared.get("hour", -1)
        blocked = set()
        for x in kh.split(","):
            x = x.strip()
            if x:
                try:
                    blocked.add(int(x))
                except Exception:
                    pass
        if hr in blocked:
            return f"HOUR-BLOCK ({hr})"
    if str(cfg.get("ofi_align", "off")) == "on":
        ofi1 = shared.get("ofi_1min") or 0
        sgn = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if ofi1 * sgn < 0:
            return f"OFI-MISALIGN ({ofi1:+.0f} vs {'YES' if delta > 0 else 'NO'})"
    return None


def _merged_entry(side, shared):
    """Best executable BUY price = min(side ask, 1 - opposite bid). Matches research."""
    ya, yb = shared.get("poly_yes_ask"), shared.get("poly_yes_bid")
    na, nb = shared.get("poly_no_ask"), shared.get("poly_no_bid")
    def v(x):
        return x if (x is not None and 0.01 < x < 0.99) else None
    if side == "YES":
        cands = [c for c in (v(ya), (round(1 - nb, 2) if v(nb) else None)) if c is not None]
    else:
        cands = [c for c in (v(na), (round(1 - yb, 2) if v(yb) else None)) if c is not None]
    return min(cands) if cands else None


# ── F2 (caley): isotonic-calibrated p_re -> EV gate ──
_F2_ISO_PATH = "/root/paper_compare/dashboard/artifacts/f2_iso.json"
_f2_iso = None
_f2_err = None

def _load_f2():
    global _f2_iso, _f2_err
    if _f2_iso is not None or _f2_err is not None:
        return _f2_iso
    try:
        _f2_iso = _json.load(open(_F2_ISO_PATH))
    except Exception as e:
        _f2_err = f"{type(e).__name__}: {e}"
    return _f2_iso


def _evaluate_signal_caley(session_cfg, shared):
    iso = _load_f2()
    if iso is None:
        return (f"F2_LOAD_ERR: {_f2_err}", None, None, None)
    delta = shared.get("delta_from_open")
    if delta is None:
        return ("WAITING", None, None, None)
    elapsed_min = shared.get("elapsed_min", 0)
    _ew = float(session_cfg.get("entry_wait_min", 3) or 3)
    if elapsed_min < _ew:
        return (f"WAIT ({elapsed_min:.1f}/{_ew:.1f})", None, None, None)
    dfloor = float(session_cfg.get("delta_threshold", 20))
    if abs(delta) < dfloor:
        return (f"DELTA LOW ({delta:+.0f})", None, None, None)
    _rb = _regime_block(session_cfg, delta, shared)
    if _rb:
        return (_rb, None, None, None)
    sigma = (shared.get("sigmas", {}) or {}).get(session_cfg.get("sigma_type", "max30")) or shared.get("sigma")
    tau = shared.get("tau", 2)
    btc = shared.get("binance_price", 70000)
    if not sigma or sigma <= 0 or not btc or btc <= 0:
        return ("NO SIGMA", None, None, None)
    snr = abs(delta) / (sigma * btc * max(tau, 0.1))
    p_re = _phi(0.5 * snr)
    xs = iso.get("X_thresholds") or []
    ys = iso.get("y_thresholds") or []
    if not xs:
        return ("NO ISO", None, None, None)
    import numpy as _np
    p_cal = float(_np.interp(p_re, xs, ys))
    side = "YES" if delta > 0 else "NO"
    entry = _merged_entry(side, shared)
    if not entry or entry <= 0.50:
        return ("NO PRICE", None, None, None)
    if entry > float(session_cfg.get("max_entry_price", 0.80)):
        return (f"HIGH ({entry:.2f})", None, None, None)
    skip, reason = _session_liq(session_cfg, side, shared)
    if skip:
        return (reason, None, None, None)
    ev = p_cal * (1 - entry) / entry - (1 - p_cal)
    ev_thr = float(session_cfg.get("ev_thr", 0.15))
    if ev < ev_thr:
        return (f"EV LOW ({ev:+.3f}<{ev_thr})", None, None, None)
    return (f"BUY {side} (EV={ev:+.3f} pc={p_cal:.2f})", side, entry, p_cal if side == "YES" else 1 - p_cal)


# ── F4 (ml_evgate): core8 LightGBM P(win) -> isotonic -> EV gate ──
_F4_MODEL_PATH = "/root/paper_compare/dashboard/artifacts/f4_model.lgb"
_F4_META_PATH = "/root/paper_compare/dashboard/artifacts/f4_meta.json"
_f4_model = None
_f4_meta = None
_f4_err = None

def _load_f4():
    global _f4_model, _f4_meta, _f4_err
    if _f4_model is not None or _f4_err is not None:
        return _f4_model, _f4_meta
    try:
        import lightgbm as lgb
        _f4_model = lgb.Booster(model_file=_F4_MODEL_PATH)
        _f4_meta = _json.load(open(_F4_META_PATH))
    except Exception as e:
        _f4_err = f"{type(e).__name__}: {e}"
    return _f4_model, _f4_meta


def _evaluate_signal_ml_evgate(session_cfg, shared):
    model, meta = _load_f4()
    if model is None:
        return (f"F4_LOAD_ERR: {_f4_err}", None, None, None)
    delta = shared.get("delta_from_open")
    if delta is None:
        return ("WAITING", None, None, None)
    elapsed_min = shared.get("elapsed_min", 0)
    _ew = float(session_cfg.get("entry_wait_min", 3) or 3)
    if elapsed_min < _ew:
        return (f"WAIT ({elapsed_min:.1f}/{_ew:.1f})", None, None, None)
    dfloor = float(session_cfg.get("delta_threshold", 15))
    if abs(delta) < dfloor:
        return (f"DELTA LOW ({delta:+.0f})", None, None, None)
    _rb = _regime_block(session_cfg, delta, shared)
    if _rb:
        return (_rb, None, None, None)
    sigma = (shared.get("sigmas", {}) or {}).get(session_cfg.get("sigma_type", "max30")) or shared.get("sigma")
    tau = shared.get("tau", 2)
    btc = shared.get("binance_price", 70000)
    if not sigma or sigma <= 0 or not btc or btc <= 0:
        return ("NO SIGMA", None, None, None)
    side = "YES" if delta > 0 else "NO"
    entry = _merged_entry(side, shared)
    if not entry or entry <= 0.50:
        return ("NO PRICE", None, None, None)
    if entry > float(session_cfg.get("max_entry_price", 0.82)):
        return (f"HIGH ({entry:.2f})", None, None, None)
    snr = abs(delta) / (sigma * btc * max(tau, 0.1))
    p_re = _phi(0.5 * snr)
    bv = shared.get("buy_vol_1min") or 0
    sv = shared.get("sell_vol_1min") or 0
    tot = bv + sv
    sgn = 1.0 if delta > 0 else -1.0
    vol_imb_s = ((bv - sv) / tot if tot > 0 else 0.0) * sgn
    if side == "YES":
        spread = (shared.get("poly_yes_ask") or 0) - (shared.get("poly_yes_bid") or 0)
    else:
        spread = (shared.get("poly_no_ask") or 0) - (shared.get("poly_no_bid") or 0)
    import numpy as _np
    feats = _np.array([[snr, p_re, entry, abs(delta), sigma, max(tau, 0.1), spread, vol_imb_s]], dtype=_np.float64)
    _np.nan_to_num(feats, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    raw = float(model.predict(feats)[0])
    ix = meta.get("iso_x") or []
    iy = meta.get("iso_y") or []
    p_cal = float(_np.interp(raw, ix, iy)) if ix else raw
    skip, reason = _session_liq(session_cfg, side, shared)
    if skip:
        return (reason, None, None, None)
    ev = p_cal * (1 - entry) / entry - (1 - p_cal)
    ev_thr = float(session_cfg.get("ev_thr", 0.18))
    if ev < ev_thr:
        return (f"EV LOW ({ev:+.3f}<{ev_thr})", None, None, None)
    return (f"BUY {side} (EV={ev:+.3f} pc={p_cal:.2f})", side, entry, p_cal if side == "YES" else 1 - p_cal)


_LOGIT_BAKES = {}
def _load_logit_bake(path):
    import json as _j
    if path not in _LOGIT_BAKES:
        try:
            _LOGIT_BAKES[path] = _j.load(open(path))
            print(f"[paper:logit_ev] loaded bake {path}", flush=True)
        except Exception as e:
            _LOGIT_BAKES[path] = None
            print(f"[paper:logit_ev] bake load fail {path}: {e}", flush=True)
    return _LOGIT_BAKES[path]

def _evaluate_signal_logit_ev(session_cfg, shared, bake_path):
    """Kalshi-refit logistic P(win) -> EV gate. ev = P/entry - 1; trade iff ev >= ev_thr (0 => P>entry).
    Same entry timing / merged-entry / regime / liq filters as ml_evgate, but allows cheap entries."""
    import math as _m
    bake = _load_logit_bake(bake_path)
    if not bake:
        return ("LOGIT_BAKE_ERR", None, None, None)
    delta = shared.get("delta_from_open")
    if delta is None:
        return ("WAITING", None, None, None)
    elapsed_min = shared.get("elapsed_min", 0)
    _ew = float(session_cfg.get("entry_wait_min", 4.5) or 4.5)
    if elapsed_min < _ew:
        return (f"WAIT ({elapsed_min:.1f}/{_ew:.1f})", None, None, None)
    dfloor = float(session_cfg.get("delta_threshold", 0) or 0)
    if abs(delta) < dfloor:
        return (f"DELTA LOW ({delta:+.0f})", None, None, None)
    _rb = _regime_block(session_cfg, delta, shared)
    if _rb:
        return (_rb, None, None, None)
    sigma = (shared.get("sigmas", {}) or {}).get(session_cfg.get("sigma_type", "max30")) or shared.get("sigma")
    tau = shared.get("tau", 2)
    btc = shared.get("binance_price", 70000)
    if not sigma or sigma <= 0 or not btc or btc <= 0:
        return ("NO SIGMA", None, None, None)
    side = "YES" if delta > 0 else "NO"
    entry = _merged_entry(side, shared)
    if not entry or entry <= 0.01 or entry >= 0.99:
        return ("NO PRICE", None, None, None)
    if entry > float(session_cfg.get("max_entry_price", 0.99)):
        return (f"HIGH ({entry:.2f})", None, None, None)
    snr = abs(delta) / (sigma * btc * max(tau, 0.1))
    p_re = _phi(0.5 * snr)
    bv = shared.get("buy_vol_1min") or 0
    sv = shared.get("sell_vol_1min") or 0
    tot = bv + sv
    sgn = 1.0 if delta > 0 else -1.0
    vol_imb = ((bv - sv) / tot if tot > 0 else 0.0) * sgn
    if side == "YES":
        spread = (shared.get("poly_yes_ask") or 0) - (shared.get("poly_yes_bid") or 0)
    else:
        spread = (shared.get("poly_no_ask") or 0) - (shared.get("poly_no_bid") or 0)
    fmap = {"p_model": p_re, "snr": snr, "entry": entry, "absd": abs(delta),
            "sigma": sigma, "tau": max(tau, 0.1), "spread": spread, "vol_imb": vol_imb,
            "hour_utc": float(shared.get("hour_utc", 12) or 12)}
    z = float(bake["intercept"])
    for i, f in enumerate(bake["feats"]):
        x = float(fmap.get(f, 0.0))
        z += float(bake["coef"][i]) * (x - float(bake["mu"][i])) / float(bake["sd"][i])
    z = max(-30.0, min(30.0, z))
    P = 1.0 / (1.0 + _m.exp(-z))
    _skip, _reason = _session_liq(session_cfg, side, shared)
    if _skip:
        return (_reason, None, None, None)
    ev = P / entry - 1.0
    ev_thr = float(session_cfg.get("ev_thr", 0.0))
    if ev < ev_thr:
        return (f"EV LOW ({ev:+.3f}<{ev_thr})", None, None, None)
    return (f"BUY {side} (EV={ev:+.3f} P={P:.2f})", side, entry, P if side == "YES" else 1 - P)


def check_all_sessions(shared):
    """Evaluate and try to open positions for all sessions."""
    with paper_lock:
        for sid, s in _sessions.items():
            cfg = s["config"]
            st = s["state"]

            if cfg.get("paused"):
                st["last_signal"] = "PAUSED"
                continue

            # Dispatch by strategy_type. Default = "pmodel" (legacy).
            strat = cfg.get("strategy_type", "pmodel")
            if strat == "noob_fader":
                sig_text, side, entry, conf = _evaluate_signal_noob_fader(cfg, shared)
            elif strat == "ml_evgate":
                sig_text, side, entry, conf = _evaluate_signal_ml_evgate(cfg, shared)
            elif strat == "logit_ev":
                sig_text, side, entry, conf = _evaluate_signal_logit_ev(cfg, shared, "/root/paper_compare_kalshi_15m/dashboard/evgate_bake.json")
            elif strat == "logit_ev_full":
                sig_text, side, entry, conf = _evaluate_signal_logit_ev(cfg, shared, "/root/paper_compare_kalshi_15m/dashboard/evgate_full_bake.json")
            elif strat == "caley":
                sig_text, side, entry, conf = _evaluate_signal_caley(cfg, shared)
            elif cfg.get("exec_mode", "taker") == "maker":
                _cfg_eval = dict(cfg)
                _cfg_eval["max_entry_price"] = 0.99
                sig_text, side, entry, conf = _evaluate_signal(_cfg_eval, shared)
            else:
                sig_text, side, entry, conf = _evaluate_signal(cfg, shared)
            st["last_signal"] = sig_text
            # Store live sigma for display
            sigma_type = cfg.get("sigma_type", "realized5_pmin")
            st["live_sigma"] = shared.get("sigmas", {}).get(sigma_type) or shared.get("sigma") or 0

            if side and entry:
                if cfg.get("exec_mode", "taker") == "maker":
                    _try_post_maker(sid, sig_text, side, conf or 0,
                                    shared.get("delta_from_prev", 0),
                                    shared.get("window_start"), shared)
                else:
                    _try_open(sid, sig_text, side, entry, conf or 0,
                              shared.get("delta_from_prev", 0),
                              shared.get("window_start"),
                              shared.get("ob_shares", 0),
                              shared.get("market_slug"))


BOOK_STALE_SECS = float(_os.getenv("BOOK_STALE_SECS", "4.0"))


def _try_open(sid, signal, side, entry_price, confidence, delta, window_start, ob_shares=0, market_slug=None):
    """Try to open position for a session. Must be called inside paper_lock."""
    s = _sessions.get(sid)
    if not s:
        return False

    # Fill-time freshness gate: refuse to fill on a stale book. The WS ticker
    # feed drops periodically (keepalive timeouts, 9x on 2026-07-08) and the
    # REST fallback is 3s-cadence behind a 4s hand-back gap; in violent windows
    # a seconds-old quote is tens of cents from reality (measured: paper 0.54
    # vs real ask 0.80 within 300ms). Sessions only fire in MOVING markets,
    # where the ticker prints many times per second - so ws age > threshold
    # means the feed is broken, not the market quiet. Lock-free read is fine
    # for a float under the GIL.
    _ws_age = time.time() - (_live_state.get("kalshi_ws_ts") or 0)
    if _ws_age > BOOK_STALE_SECS:
        print(f"[paper] STALE-BOOK skip {sid} {side} entry={entry_price} ws_age={_ws_age:.1f}s", flush=True)
        return False

    cfg = s["config"]
    st = s["state"]
    stake = cfg["stake"]

    if window_start != st["current_window"]:
        st["current_window"] = window_start
        st["buys_this_window"] = 0

    if st["buys_this_window"] >= 1:  # 1 per window
        return False
    if st["balance"] < stake:
        return False

    raw_shares = stake / entry_price
    buy_fee_shares = calc_fee(raw_shares, entry_price)
    shares = raw_shares - buy_fee_shares

    trade_dict = {
        "ts": _now_iso(), "side": side, "entry": entry_price,
        "stake": stake, "shares": round(shares, 2),
        "ob_shares": round(ob_shares, 0), "signal": signal,
        "confidence": confidence, "delta": delta,
        "window": window_start, "market_slug": market_slug,
        "p_model": confidence, "session_id": sid,
    }
    db_id = db_insert_paper_trade(trade_dict)
    trade_dict["db_id"] = db_id

    s["positions"].append(trade_dict)
    st["buys_this_window"] += 1
    st["last_buy_ts"] = time.time()
    st["balance"] -= stake
    st["total_staked"] += stake
    _save_session(sid)

    print(f"[paper:{sid}] OPEN {side} @ {entry_price:.3f} stake=${stake} "
          f"shares={shares:.1f} delta={delta}", flush=True)
    return True


def _try_post_maker(sid, signal, side, confidence, delta, window_start, shared):
    """MAKER paper: post a virtual resting limit at own-side best bid + offset.
    Balance is NOT deducted until fill; 1 post per window; pending orders live
    in memory only (crash -> attempt uncounted, never resurrected as position).
    Must be called inside paper_lock."""
    s = _sessions.get(sid)
    if not s:
        return False
    cfg = s["config"]; st = s["state"]
    if window_start != st["current_window"]:
        st["current_window"] = window_start
        st["buys_this_window"] = 0
    if st["buys_this_window"] >= 1:
        return False
    if st["balance"] < cfg["stake"]:
        return False
    bid = shared.get("poly_yes_bid") if side == "YES" else shared.get("poly_no_bid")
    ask = shared.get("poly_yes_ask") if side == "YES" else shared.get("poly_no_ask")
    if not bid or not ask:
        return False
    L = round(bid + float(cfg.get("maker_offset", 0.0)), 2)
    if L < 0.03 or L > float(cfg.get("max_entry_price", 0.95)) or L > ask - 0.0099:
        st["last_signal"] = f"MKR NO-POST L={L:.2f} ask={ask:.2f}"
        return False
    now = time.time()
    ttl = float(cfg.get("maker_ttl_min", 5.0)) * 60.0
    s.setdefault("pending_orders", []).append({
        "side": side, "L": L, "posted_ts": now, "expiry_ts": now + ttl,
        "window": window_start, "signal": signal, "confidence": confidence,
        "delta": delta, "market_slug": shared.get("market_slug"),
    })
    st["buys_this_window"] += 1
    st["last_signal"] = f"MKR POSTED {side} @ {L:.2f}"
    print(f"[paper:{sid}] MAKER POST {side} @ {L:.3f} ttl={int(ttl)}s "
          f"delta={delta}", flush=True)
    return True



_MKR_PRINTS = {"ticker": None, "ts": 0.0, "prints": []}

def _kalshi_prints(ticker):
    """Recent trade prints [(epoch_ts, yes_price_dollars), ...] for a market.
    Throttled to 1 request / 2.5 s; empty list on any failure (fill check just
    retries next loop). Public endpoint, no auth."""
    now = time.time()
    if _MKR_PRINTS["ticker"] == ticker and now - _MKR_PRINTS["ts"] < 2.5:
        return _MKR_PRINTS["prints"]
    prints = _MKR_PRINTS["prints"] if _MKR_PRINTS["ticker"] == ticker else []
    try:
        import json as _j, urllib.request as _u, datetime as _dt
        url = ("https://api.elections.kalshi.com/trade-api/v2/markets/trades?ticker=%s&limit=100"
               % ticker)
        with _u.urlopen(_u.Request(url, headers={"User-Agent": "paper"}), timeout=1.5) as r:
            d = _j.loads(r.read().decode())
        out = []
        for t in d.get("trades", []):
            ct = t.get("created_time") or t.get("ts")
            yp = t.get("yes_price_dollars")
            if yp is None and t.get("yes_price") is not None:
                yp = float(t["yes_price"]) / 100.0
            if ct is None or yp is None:
                continue
            try:
                ets = _dt.datetime.fromisoformat(str(ct).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            out.append((ets, float(yp)))
        prints = out
    except Exception:
        pass
    _MKR_PRINTS.update(ticker=ticker, ts=now, prints=prints)
    return prints


def check_maker_orders(shared):
    """Fill or expire pending maker orders. Fill = a FRESH own-side trade print
    (ts > posted_ts) at <= L-1c ("cons", queue-safe) or <= L ("opt"). Prints come
    from the book poller's last-trade fields — prints between polls are missed,
    which UNDERcounts fills (conservative for paper honesty).
    Unfilled expiry -> SKIP/UNFILLED row (per-attempt accounting), $0."""
    with paper_lock:
        for sid, s in _sessions.items():
            pend = s.get("pending_orders")
            if not pend:
                continue
            cfg = s["config"]; st = s["state"]
            rule = cfg.get("maker_fill_rule", "cons")
            keep = []
            for o in pend:
                thr = o["L"] - 0.01 if rule == "cons" else o["L"]
                lt = None
                if o.get("market_slug"):
                    for (p_ts, p_yes) in _kalshi_prints(o["market_slug"]):
                        if p_ts <= o["posted_ts"]:
                            continue
                        own_px = p_yes if o["side"] == "YES" else (1.0 - p_yes)
                        if own_px <= thr + 1e-9:
                            lt = own_px
                            break
                if lt is not None:
                    stake = cfg["stake"]
                    shares = stake / o["L"]            # Kalshi maker fee = $0
                    trade_dict = {
                        "ts": _now_iso(), "side": o["side"], "entry": o["L"],
                        "stake": stake, "shares": round(shares, 2),
                        "ob_shares": 0, "signal": "MKRFILL " + (o["signal"] or ""),
                        "confidence": o["confidence"], "delta": o["delta"],
                        "window": o["window"], "market_slug": o["market_slug"],
                        "p_model": o["confidence"], "session_id": sid,
                    }
                    db_id = db_insert_paper_trade(trade_dict)
                    trade_dict["db_id"] = db_id
                    s["positions"].append(trade_dict)
                    st["last_buy_ts"] = time.time()
                    st["balance"] -= stake
                    st["total_staked"] += stake
                    _save_session(sid)
                    print(f"[paper:{sid}] MAKER FILL {o['side']} @ {o['L']:.3f} "
                          f"(print={lt})", flush=True)
                    continue
                if time.time() > o["expiry_ts"] or shared.get("window_start") != o["window"]:
                    row = {
                        "ts": _now_iso(), "side": o["side"], "entry": o["L"],
                        "stake": 0, "shares": 0, "ob_shares": 0,
                        "signal": "MKRSKIP " + (o["signal"] or ""),
                        "confidence": o["confidence"], "delta": o["delta"],
                        "window": o["window"], "market_slug": o["market_slug"],
                        "p_model": o["confidence"], "session_id": sid,
                    }
                    db_id = db_insert_paper_trade(row)
                    db_resolve_paper_trade(db_id, 0.0, "SKIP", "UNFILLED", _now_iso())
                    print(f"[paper:{sid}] MAKER EXPIRE {o['side']} @ {o['L']:.3f}", flush=True)
                    continue
                keep.append(o)
            s["pending_orders"] = keep


def resolve_all_positions(outcome_up):
    """Resolve all positions for all sessions."""
    with paper_lock:
        for sid, s in _sessions.items():
            if not s["positions"]:
                continue

            resolved = []
            for pos in s["positions"]:
                won = (pos["side"] == "YES" and outcome_up) or \
                      (pos["side"] == "NO" and not outcome_up)

                if won:
                    payout = pos["shares"] * 1.0
                    pnl = payout - pos["stake"]
                else:
                    pnl = -pos["stake"]
                    payout = 0

                resolved_ts = _now_iso()
                result_str = "WIN" if won else "LOSS"
                outcome_str = "UP" if outcome_up else "DOWN"

                result = {**pos, "pnl": round(pnl, 2), "payout": round(payout, 2),
                          "result": result_str, "resolved_ts": resolved_ts,
                          "outcome": outcome_str}
                resolved.append(result)

                if pos.get("db_id"):
                    db_resolve_paper_trade(pos["db_id"], round(pnl, 2),
                                           result_str, outcome_str, resolved_ts)

                s["state"]["total_pnl"] += pnl
                s["state"]["balance"] += payout
                s["state"]["total_trades"] += 1
                if won:
                    s["state"]["wins"] += 1
                else:
                    s["state"]["losses"] += 1

            s["history"] = (resolved + s["history"])[:200]
            s["positions"] = []
            _save_session(sid)

            w = sum(1 for r in resolved if r["result"] == "WIN")
            l = sum(1 for r in resolved if r["result"] == "LOSS")
            tp = sum(r["pnl"] for r in resolved)
            print(f"[paper:{sid}] RESOLVED {len(resolved)}: {w}W/{l}L "
                  f"pnl=${tp:+.2f} bal=${s['state']['balance']:.2f}", flush=True)


def resolve_pending_via_gamma():
    """Per-position resolve via Polymarket Gamma API.

    Iterates all pending positions, queries Gamma for each position's
    market_slug, and resolves ONLY ones Gamma marks closed=True. Safe to
    call repeatedly — unresolved positions stay active, resolved ones
    move to history.
    """
    try:
        import requests as _rq
        import json as _j
    except ImportError:
        return 0

    resolved_count = 0
    with paper_lock:
        for sid, s in _sessions.items():
            if not s["positions"]:
                continue
            still_pending = []
            resolved_for_session = []
            for pos in list(s["positions"]):
                slug = pos.get("market_slug")
                if not slug:
                    still_pending.append(pos)
                    continue
                outcome_up = None
                if getattr(_cfg, "VENUE", "polymarket") == "limitless":
                    from . import ll_feed
                    outcome_up = ll_feed.resolve_ll_market(slug)
                else:
                    try:
                        r = _rq.get("https://gamma-api.polymarket.com/events",
                                    params={"slug": slug}, timeout=5)
                        evs = r.json() or []
                        if evs:
                            m = (evs[0].get("markets") or [None])[0]
                            if m and m.get("closed"):
                                op = m.get("outcomePrices")
                                if isinstance(op, str):
                                    op = _j.loads(op)
                                if op and len(op) == 2:
                                    if op[0] == "1":
                                        outcome_up = True
                                    elif op[1] == "1":
                                        outcome_up = False
                    except Exception:
                        pass

                if outcome_up is None:
                    still_pending.append(pos)
                    continue

                won = (pos["side"] == "YES" and outcome_up) or \
                      (pos["side"] == "NO" and not outcome_up)
                if won:
                    payout = pos["shares"] * 1.0
                    pnl = payout - pos["stake"]
                else:
                    pnl = -pos["stake"]
                    payout = 0
                resolved_ts = _now_iso()
                result_str = "WIN" if won else "LOSS"
                outcome_str = "UP" if outcome_up else "DOWN"
                result = {**pos, "pnl": round(pnl, 2), "payout": round(payout, 2),
                          "result": result_str, "resolved_ts": resolved_ts,
                          "outcome": outcome_str}
                resolved_for_session.append(result)
                if pos.get("db_id"):
                    db_resolve_paper_trade(pos["db_id"], round(pnl, 2),
                                           result_str, outcome_str, resolved_ts)
                s["state"]["total_pnl"] += pnl
                s["state"]["balance"] += payout
                s["state"]["total_trades"] += 1
                if won:
                    s["state"]["wins"] += 1
                else:
                    s["state"]["losses"] += 1
                resolved_count += 1

            if resolved_for_session:
                s["history"] = (resolved_for_session + s["history"])[:200]
                s["positions"] = still_pending
                _save_session(sid)
                w = sum(1 for r in resolved_for_session if r["result"] == "WIN")
                l = sum(1 for r in resolved_for_session if r["result"] == "LOSS")
                tp = sum(r["pnl"] for r in resolved_for_session)
                print(f"[paper:{sid}] GAMMA-RESOLVED {len(resolved_for_session)}: "
                      f"{w}W/{l}L pnl=${tp:+.2f} bal=${s['state']['balance']:.2f} "
                      f"(still_pending={len(still_pending)})", flush=True)
    return resolved_count


def get_all_sessions():
    """Return all sessions info for API."""
    with paper_lock:
        result = {}
        for sid, s in _sessions.items():
            result[sid] = get_session_info(sid)
        return result


def get_session_info(sid):
    """Return single session info."""
    s = _sessions.get(sid)
    if not s:
        return None
    cfg = s["config"]
    st = s["state"]
    if cfg.get("strategy_type") == "ml_evgate":
        formula = (f"F4 ML-EVgate core8 LGBM->iso EV>={cfg.get('ev_thr',0.18)} "
                   f"|D|>={cfg.get('delta_threshold')} max_entry={cfg.get('max_entry_price')}")
    elif cfg.get("strategy_type") == "caley":
        formula = (f"F2 calEV iso(p_re) EV>={cfg.get('ev_thr',0.15)} "
                   f"|D|>={cfg.get('delta_threshold')} max_entry={cfg.get('max_entry_price')}")
    elif cfg.get("strategy_type") == "noob_fader":
        th = cfg.get("threshold_gap", 0.05)
        formula = (f"NOOB_FADER gap=p_cal-mkt  |gap|>{th}  "
                   f"decision_zone=[60,180)s  max_entry={cfg.get('max_entry_price',0.95)}")
    elif cfg.get("sigma_type") == "weighted":
        formula = "WEIGHTED score=Σ(w×f) → threshold+max_entry from JSON"
    else:
        tau_str = "t" if cfg["tau_mode"] == "linear" else "sqrt(t)"
        formula = (f"p=F({cfg['kappa']}*|D|/({cfg['sigma_type']}*{tau_str})) "
                   f"D>=${cfg['delta_threshold']} p>={cfg['p_model_threshold']} "
                   f"side={cfg['trade_side']}")
    return {
        "id": sid,
        "config": cfg,
        "formula": formula,
        "balance": round(st["balance"], 2),
        "starting_balance": st["starting_balance"],
        "total_pnl": round(st["total_pnl"], 2),
        "total_trades": st["total_trades"],
        "wins": st["wins"],
        "losses": st["losses"],
        "win_rate": round(st["wins"] / max(st["total_trades"], 1), 3),
        "total_staked": round(st["total_staked"], 2),
        "buys_this_window": st["buys_this_window"],
        "last_signal": st.get("last_signal", ""),
        "live_sigma": st.get("live_sigma", 0),
        "active_positions": list(s["positions"]),
        "history": s["history"][:30],
    }


# Legacy compatibility
def configure_paper(starting_balance=None, stake_per_trade=None, reset=False):
    """Legacy API — operates on first session."""
    with paper_lock:
        if not _sessions:
            return {"error": "No sessions"}
        sid = list(_sessions.keys())[0]
    if reset:
        return reset_session(sid, starting_balance)
    updates = {}
    if starting_balance is not None:
        with paper_lock:
            s = _sessions.get(sid)
            if s:
                s["state"]["balance"] = starting_balance
                s["state"]["starting_balance"] = starting_balance
                _save_session(sid)
    if stake_per_trade is not None:
        updates["stake"] = stake_per_trade
    if updates:
        return update_session_config(sid, updates)
    return {"status": "ok"}


def get_paper_state():
    """Legacy API — return first session state for backward compat."""
    with paper_lock:
        if not _sessions:
            return {"active_positions": [], "history": [], "total_pnl": 0,
                    "total_trades": 0, "wins": 0, "losses": 0, "balance": 10000,
                    "starting_balance": 10000, "stake_per_trade": 10,
                    "buys_this_window": 0, "max_per_window": 1, "win_rate": 0,
                    "total_staked": 0, "active_count": 0, "active_stake": 0}
        sid = list(_sessions.keys())[0]
    info = get_session_info(sid)
    if not info:
        return {}
    return {
        "active_positions": info["active_positions"],
        "active_count": len(info["active_positions"]),
        "active_stake": sum(p["stake"] for p in info["active_positions"]),
        "history": info["history"],
        "total_pnl": info["total_pnl"],
        "total_trades": info["total_trades"],
        "wins": info["wins"],
        "losses": info["losses"],
        "win_rate": info["win_rate"],
        "total_staked": info["total_staked"],
        "balance": info["balance"],
        "starting_balance": info["starting_balance"],
        "buys_this_window": info["buys_this_window"],
        "max_per_window": 1,
        "stake_per_trade": info["config"]["stake"],
        "cooldown": 0,
    }
