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
