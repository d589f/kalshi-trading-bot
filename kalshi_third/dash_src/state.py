"""Global shared state, locks, and connection tracking."""
import threading
import time

from .config import SIGMA_DEFAULT, WINDOW_MINUTES

state = {
    "chainlink_price": None,
    "chainlink_ts": None,
    "binance_price": None,
    "binance_ts": None,

    "window_start_utc": None,
    "window_end_utc": None,
    "window_open_price": None,
    "prev_close_price": None,

    "delta_from_open": None,
    "delta_from_prev": None,

    "ofi_1min": 0,
    "ofi_3min": 0,
    "ofi_5min": 0,
    "buy_vol_1min": 0,
    "sell_vol_1min": 0,
    "trade_count_1min": 0,

    "bn_bid1_p": None, "bn_bid1_v": None,
    "bn_ask1_p": None, "bn_ask1_v": None,
    "bn_spread": None,

    "poly_no_bid": None, "poly_no_ask": None,
    "poly_no_bid_vol": None, "poly_no_ask_vol": None,
    "poly_yes_bid": None, "poly_yes_ask": None,
    "poly_yes_bid_vol": None, "poly_yes_ask_vol": None,

    # Last trade prices from WS (real market prices)
    "poly_no_last_trade": None,
    "poly_no_last_trade_size": None,
    "poly_no_last_trade_ts": None,
    "poly_yes_last_trade": None,
    "poly_yes_last_trade_size": None,
    "poly_yes_last_trade_ts": None,
    "poly_last_trade_expensive": None,
    "poly_last_trade_cheap": None,

    "yes_token_id": None,
    "no_token_id": None,
    "market_resolved": False,
    "market_resolved_ts": None,

    "p_model": None,
    "snr": None,
    "sigma": SIGMA_DEFAULT,

    "signal": "NO SIGNAL",
    "signal_side": None,
    "signal_entry": None,
    "signal_confidence": None,

    "market_slug": None,
    "market_token_ids": [],

    "last_update": None,
    "uptime_s": 0,
    "window_minutes": WINDOW_MINUTES,
    "paper_paused": False,
    "errors": [],
}

state_lock = threading.RLock()
trade_buffer = []  # (timestamp, notional, is_sell)
trade_buffer_lock = threading.Lock()
start_time = time.time()

conn_status = {
    "binance_trades": {"connected": False, "last_error": None, "last_msg_time": None, "msg_count": 0},
    "binance_depth": {"connected": False, "last_error": None, "last_msg_time": None, "msg_count": 0},
    "poly_rtds": {"connected": False, "last_error": None, "last_msg_time": None, "msg_count": 0},
    "poly_clob": {"connected": False, "last_error": None, "last_msg_time": None, "msg_count": 0},
}
conn_lock = threading.Lock()

orderbook = {
    "yes": {"bids": [], "asks": [], "ts": None},
    "no": {"bids": [], "asks": [], "ts": None},
}
ob_lock = threading.Lock()

# Mutable flags for cross-module signaling
_market_rediscovery_requested = [False]
_clob_msg_count = [0]
