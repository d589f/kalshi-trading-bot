"""Periodic async tasks: window tracker, state updater, data recorder, orderbook reader."""
import asyncio
import sys
import json

from .state import (state, state_lock, orderbook, ob_lock,
                    _market_rediscovery_requested)
from .helpers import (_now_utc, _current_window, compute_ofi,
                      update_delta, update_signal, update_sigma,
                      _sigma_cache)
from .database import db_insert_tick, db_insert_signal, db_insert_orderbook, db_insert_window
from .market import find_current_market
from .orderbook import fetch_deep_orderbook
from .paper_trading import check_all_sessions, resolve_all_positions, resolve_pending_via_gamma


async def orderbook_reader():
    read_count = 0
    while True:
        try:
            await asyncio.to_thread(fetch_deep_orderbook)
            read_count += 1
            if read_count % 20 == 1:
                with ob_lock:
                    no_lvl = orderbook["no"].get("levels", 0)
                    yes_lvl = orderbook["yes"].get("levels", 0)
                    no_src = orderbook["no"].get("src", "?")
                    yes_src = orderbook["yes"].get("src", "?")
                print(f"[ob-rest] #{read_count} NO:{no_lvl}lvl({no_src}) YES:{yes_lvl}lvl({yes_src})",
                      flush=True)
        except Exception as e:
            print(f"[ob-rest] error: {e}", file=sys.stderr, flush=True)
        await asyncio.sleep(1.5)


async def window_tracker():
    last_window = None
    while True:
        w_start, w_end = _current_window()
        new_window = (w_start != last_window)
        rediscovery = _market_rediscovery_requested[0]

        if new_window or rediscovery:
            if rediscovery and not new_window:
                # Market resolved but window hasn't changed yet — just wait
                _market_rediscovery_requested[0] = False
                await asyncio.sleep(2)
                continue

            if rediscovery:
                _market_rediscovery_requested[0] = False
                print("[dashboard] Rediscovery triggered (new window)", flush=True)

            if new_window:
                # Resolve paper positions — PRIMARY: Polymarket Gamma API (ground
                # truth, matches what live prod sees). FALLBACK: local orderbook
                # heuristic → BTC delta.
                with state_lock:
                    yes_bid = state.get("poly_yes_bid")
                    no_bid = state.get("poly_no_bid")
                    delta_close = state.get("delta_from_open") or state.get("delta_from_prev") or 0
                    was_resolved = state.get("market_resolved", False)
                    prev_slug = state.get("market_slug")

                outcome_up = None
                resolve_src = "?"
                gamma_closed = False
                gamma_net_ok = False
                if prev_slug:
                    try:
                        import requests as _rq
                        r = _rq.get("https://gamma-api.polymarket.com/events",
                                     params={"slug": prev_slug}, timeout=5)
                        gamma_net_ok = True
                        evs = r.json() or []
                        if evs:
                            m = (evs[0].get("markets") or [None])[0]
                            if m:
                                gamma_closed = bool(m.get("closed", False))
                                op = m.get("outcomePrices")
                                if isinstance(op, str):
                                    import json as _j
                                    op = _j.loads(op)
                                # Only trust binary ("1"/"0") outcomes that
                                # also have closed=True — op=["0.5","0.5"] on
                                # a pending market must NOT trigger resolve.
                                if gamma_closed and op and len(op) == 2:
                                    if op[0] == "1":
                                        outcome_up = True; resolve_src = "gamma"
                                    elif op[1] == "1":
                                        outcome_up = False; resolve_src = "gamma"
                    except Exception as e:
                        print(f"[paper] Gamma resolve failed: {e}", flush=True)

                if outcome_up is None and gamma_net_ok and not gamma_closed:
                    # Gamma reachable but market not yet closed on-chain.
                    # DEFER — leave positions active, retry next window transition.
                    print(f"[paper] DEFER {prev_slug} — Gamma says not closed yet", flush=True)
                elif outcome_up is None:
                    # Gamma unreachable → last-resort local heuristic.
                    if was_resolved and yes_bid is not None and no_bid is not None:
                        outcome_up = (yes_bid > no_bid)
                        resolve_src = "orderbook"
                    else:
                        outcome_up = (delta_close > 0)
                        resolve_src = "binance_delta"
                    print(f"[paper] Resolve {prev_slug} via FALLBACK {resolve_src}: "
                          f"{'UP' if outcome_up else 'DOWN'}  "
                          f"(Gamma unreachable)", flush=True)
                    resolve_all_positions(outcome_up)
                else:
                    print(f"[paper] Resolve {prev_slug} via {resolve_src}: "
                          f"{'UP' if outcome_up else 'DOWN'}", flush=True)
                    resolve_all_positions(outcome_up)
                with state_lock:
                    cl = state["chainlink_price"]
                    bn = state["binance_price"]
                    # Binance as primary — more reliable than Chainlink RTDS
                    price = bn or cl
                    if state["window_open_price"] is not None:
                        state["prev_close_price"] = price
                    state["window_start_utc"] = w_start.isoformat()
                    state["window_end_utc"] = w_end.isoformat()
                    state["window_open_price"] = price

            with ob_lock:
                for s in ("yes", "no"):
                    orderbook[s] = {"bids": [], "asks": [], "ts": None}
            # Clear last trade prices (new market = new tokens)
            with state_lock:
                state["poly_no_last_trade"] = None
                state["poly_no_last_trade_size"] = None
                state["poly_no_last_trade_ts"] = None
                state["poly_yes_last_trade"] = None
                state["poly_yes_last_trade_size"] = None
                state["poly_yes_last_trade_ts"] = None
                state["poly_last_trade_expensive"] = None
                state["poly_last_trade_cheap"] = None
            print("[dashboard] Cleared orderbook + last trades", flush=True)

            with state_lock:
                state["market_resolved"] = False
                state["market_resolved_ts"] = None

            slug, token_ids = await asyncio.to_thread(find_current_market)
            with state_lock:
                if slug:
                    state["market_slug"] = slug
                if token_ids:
                    state["market_token_ids"] = token_ids

            if new_window:
                with state_lock:
                    cl = state["chainlink_price"]
                    bn = state["binance_price"]
                open_p = (bn or cl)
                await asyncio.to_thread(db_insert_window,
                    w_start.isoformat(), w_end.isoformat(), slug, open_p, token_ids)

            last_window = w_start
            print(f"[dashboard] Window: {w_start} - {w_end}, market: {slug}", flush=True)

        await asyncio.sleep(2)


async def state_updater():
    while True:
        update_sigma()
        compute_ofi()
        update_delta()
        update_signal()

        # Paper trading — evaluate all sessions with shared market data
        with state_lock:
            no_ask = state.get("poly_no_ask")
            yes_ask = state.get("poly_yes_ask")
            last_exp = state.get("poly_last_trade_expensive")
            ob_entry = max(yes_ask or 0, no_ask or 0) if (yes_ask or no_ask) else None
            yes_ask_vol = state.get("poly_yes_ask_vol") or 0
            no_ask_vol = state.get("poly_no_ask_vol") or 0
            w_start_str = state.get("window_start_utc")
            w_end_str = state.get("window_end_utc")

            # Compute elapsed and tau
            elapsed_min = 0
            tau = 2
            if w_start_str:
                try:
                    from datetime import datetime, timezone
                    w_s = datetime.fromisoformat(w_start_str)
                    elapsed_min = (datetime.now(timezone.utc) - w_s).total_seconds() / 60
                except Exception:
                    pass
            if w_end_str:
                try:
                    from datetime import datetime, timezone
                    w_e = datetime.fromisoformat(w_end_str)
                    tau = max((w_e - datetime.now(timezone.utc)).total_seconds() / 60, 0.1)
                except Exception:
                    pass

            from datetime import datetime as _dt_cls, timezone as _tz_cls
            _now = _dt_cls.now(_tz_cls.utc)

            shared = {
                "delta_from_prev": state.get("delta_from_prev"),
                "sigma": state.get("sigma"),
                "sigmas": dict(_sigma_cache),  # all sigma variants for per-session use
                "binance_price": state.get("binance_price"),
                "last_trade_expensive": last_exp,
                "orderbook_entry": ob_entry,
                "elapsed_min": elapsed_min,
                "tau": tau,
                "window_start": w_start_str,
                "ob_shares": max(yes_ask_vol, no_ask_vol),
                "market_slug": state.get("market_slug"),
                "uptime_s": state.get("uptime_s", 0),
                # Extra fields for weighted model
                "ofi_1min": state.get("ofi_1min", 0),
                "ofi_5min": state.get("ofi_5min", 0),
                "buy_vol_1min": state.get("buy_vol_1min", 0),
                "sell_vol_1min": state.get("sell_vol_1min", 0),
                "trade_count_1min": state.get("trade_count_1min", 0),
                "hour": _now.hour,
                "dow": _now.weekday(),
                # ── Fields needed by noob_fader (shared dict contract) ──
                "poly_yes_bid": state.get("poly_yes_bid"),
                "poly_yes_ask": state.get("poly_yes_ask"),
                "poly_no_bid": state.get("poly_no_bid"),
                "poly_no_ask": state.get("poly_no_ask"),
                "poly_yes_bid_vol": state.get("poly_yes_bid_vol"),
                "poly_yes_ask_vol": state.get("poly_yes_ask_vol"),
                "poly_no_bid_vol": state.get("poly_no_bid_vol"),
                "poly_no_ask_vol": state.get("poly_no_ask_vol"),
                "chainlink_price": state.get("chainlink_price"),
                "delta_from_open": state.get("delta_from_open"),
                "ofi_3min": state.get("ofi_3min", 0),
                "p_model": state.get("p_model"),
                "snr": state.get("snr"),
                "bn_spread": state.get("bn_spread"),
                "window_start_utc": w_start_str,
            }

        from .helpers import _warmup_ready
        warmup_time = 60 if _warmup_ready else 1800
        if shared.get("uptime_s", 0) >= warmup_time:  # 30-min warmup (or 60s if DB has data)
            check_all_sessions(shared)

        await asyncio.sleep(0.3)


async def data_recorder():
    last_signal = None
    ob_snap_interval = 0
    while True:
        try:
            with state_lock:
                snap = dict(state)
            snap['ts'] = _now_utc().isoformat()
            snap['window_start'] = snap.get('window_start_utc')
            snap['window_end'] = snap.get('window_end_utc')
            snap['no_bid'] = snap.get('poly_no_bid')
            snap['no_ask'] = snap.get('poly_no_ask')
            snap['no_bid_vol'] = snap.get('poly_no_bid_vol')
            snap['no_ask_vol'] = snap.get('poly_no_ask_vol')
            snap['yes_bid'] = snap.get('poly_yes_bid')
            snap['yes_ask'] = snap.get('poly_yes_ask')
            snap['yes_bid_vol'] = snap.get('poly_yes_bid_vol')
            snap['yes_ask_vol'] = snap.get('poly_yes_ask_vol')


            # Fetch Deribit data from collector
            try:
                import urllib.request, json as _json
                _dresp = urllib.request.urlopen('http://127.0.0.1:9999/snapshot', timeout=2)
                _dsnap = _json.loads(_dresp.read().decode())
                _der = _dsnap.get('deribit', {})
                snap['btc_dvol'] = _dsnap.get('btc_dvol')
                snap['dvol_change_5m'] = _dsnap.get('dvol_change_5m')
                snap['btc_index'] = _der.get('btc_index')
                snap['funding_rate'] = _der.get('funding_rate')
                snap['perp_mark'] = _der.get('perp_mark')
                snap['perp_oi'] = _der.get('perp_oi')
                snap['perp_volume_24h'] = _der.get('perp_volume_24h')
                snap['perp_ofi_1m'] = _der.get('perp_ofi_1m')
                snap['perp_buy_vol_1m'] = _der.get('perp_buy_vol_1m')
                snap['perp_sell_vol_1m'] = _der.get('perp_sell_vol_1m')
                snap['perp_trade_count_1m'] = _der.get('perp_trade_count_1m')
                snap['atm_iv'] = _der.get('atm_iv')
                snap['atm_strike'] = _der.get('atm_strike')
                _pb = _der.get('perp_bid') or 0
                _pa = _der.get('perp_ask') or 0
                snap['perp_spread'] = round(_pa - _pb, 2) if _pa and _pb else None
                snap['put_call_ratio'] = _der.get('put_call_ratio')
                snap['iv_skew'] = _der.get('iv_skew')
                snap['gamma_exposure'] = _der.get('gamma_exposure')
                _flow = _der.get('options_flow_1m') or {}
                snap['options_flow_ratio'] = _flow.get('flow_ratio')
                snap['options_flow_net_puts'] = _flow.get('net_puts')
                snap['options_flow_net_calls'] = _flow.get('net_calls')
                snap['options_flow_count'] = _flow.get('trade_count')
                snap['liq_buy_vol_1m'] = _der.get('liq_buy_vol_1m')
                snap['liq_sell_vol_1m'] = _der.get('liq_sell_vol_1m')
                snap['liq_count_1m'] = _der.get('liq_count_1m')
                snap['liq_net_1m'] = _der.get('liq_net_1m')
                snap['long_short_ratio'] = _der.get('long_short_ratio')
                snap['long_account'] = _der.get('long_account')
                snap['short_account'] = _der.get('short_account')
                snap['top_trader_ls_ratio'] = _der.get('top_trader_ls_ratio')
                snap['taker_buy_vol'] = _der.get('taker_buy_vol')
                snap['taker_sell_vol'] = _der.get('taker_sell_vol')
                snap['taker_buy_ratio'] = _der.get('taker_buy_ratio')
                snap['bn_futures_oi'] = _der.get('bn_futures_oi')
                snap['bn_futures_oi_change'] = _der.get('bn_futures_oi_change')
                snap['futures_basis_1m'] = _der.get('futures_basis_1m')
                snap['futures_basis_3m'] = _der.get('futures_basis_3m')
                snap['term_spread'] = _der.get('term_spread')
                snap['iv_term_1d'] = _der.get('iv_term_1d')
                snap['iv_term_7d'] = _der.get('iv_term_7d')
                snap['iv_term_30d'] = _der.get('iv_term_30d')
                snap['iv_term_spread'] = _der.get('iv_term_spread')
                snap['max_pain_strike'] = _der.get('max_pain_strike')
                snap['max_pain_dist'] = _der.get('max_pain_dist')
                snap['block_trades_1m'] = _der.get('block_trades_1m')
                snap['block_net_direction'] = _der.get('block_net_direction')
                snap['rv_iv_spread'] = _der.get('rv_iv_spread')
            except Exception:
                pass

            await asyncio.to_thread(db_insert_tick, snap)

            sig = snap.get('signal', '')
            if sig and 'BUY' in sig and sig != last_signal:
                await asyncio.to_thread(db_insert_signal, snap)
                print(f"[db] Signal recorded: {sig}", flush=True)
                last_signal = sig
            elif 'BUY' not in (sig or ''):
                last_signal = None

            ob_snap_interval += 1
            if ob_snap_interval >= 5:
                ob_snap_interval = 0
                slug = snap.get('market_slug')
                with ob_lock:
                    for side in ("yes", "no"):
                        b = orderbook[side]["bids"]
                        a = orderbook[side]["asks"]
                        if b or a:
                            await asyncio.to_thread(db_insert_orderbook,
                                snap['ts'], slug, side, b, a)
        except Exception as e:
            print(f"[db] recorder error: {e}", file=sys.stderr, flush=True)

        await asyncio.sleep(1)



async def paper_gamma_resolver_loop():
    """Periodic Gamma-based per-position resolver. Every 20s."""
    while True:
        try:
            await asyncio.to_thread(resolve_pending_via_gamma)
        except Exception as e:
            print(f"[paper] gamma_resolver loop error: {e}", flush=True)
        await asyncio.sleep(20)
