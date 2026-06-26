"""SQLite database operations."""
import json
import sys
import sqlite3
import threading

from .config import DB_PATH

_db_local = threading.local()


def _get_db():
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(DB_PATH, timeout=5)
        _db_local.conn.execute("PRAGMA journal_mode=WAL")
        _db_local.conn.execute("PRAGMA synchronous=NORMAL")
    return _db_local.conn


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ticks (
        ts TEXT NOT NULL,
        chainlink_price REAL, binance_price REAL,
        delta_from_prev REAL, delta_from_open REAL,
        ofi_1min REAL, ofi_3min REAL, ofi_5min REAL,
        buy_vol_1min REAL, sell_vol_1min REAL, trade_count_1min INTEGER,
        p_model REAL, snr REAL, sigma REAL,
        signal TEXT, signal_side TEXT, signal_confidence REAL,
        window_start TEXT, window_end TEXT, market_slug TEXT,
        no_bid REAL, no_ask REAL, no_bid_vol REAL, no_ask_vol REAL,
        yes_bid REAL, yes_ask REAL, yes_bid_vol REAL, yes_ask_vol REAL,
        bn_spread REAL
    );
    CREATE TABLE IF NOT EXISTS signals (
        ts TEXT NOT NULL,
        signal TEXT, side TEXT,
        delta REAL, p_model REAL, snr REAL,
        ofi_1min REAL, ofi_5min REAL,
        entry_price REAL, confidence REAL,
        chainlink_price REAL, binance_price REAL,
        market_slug TEXT, window_start TEXT, window_end TEXT
    );
    CREATE TABLE IF NOT EXISTS orderbook_snapshots (
        ts TEXT NOT NULL,
        market_slug TEXT, side TEXT,
        bids TEXT, asks TEXT
    );
    CREATE TABLE IF NOT EXISTS windows (
        window_start TEXT NOT NULL,
        window_end TEXT,
        market_slug TEXT,
        open_price REAL, close_price REAL,
        token_ids TEXT,
        PRIMARY KEY (window_start)
    );
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        side TEXT,
        entry_price REAL,
        stake REAL,
        shares REAL,
        ob_shares REAL,
        signal TEXT,
        delta REAL, p_model REAL,
        confidence REAL,
        window_start TEXT,
        market_slug TEXT,
        pnl REAL,
        result TEXT,
        outcome TEXT,
        resolved_ts TEXT,
        session_id TEXT DEFAULT 's1'
    );
    CREATE TABLE IF NOT EXISTS paper_config (
        session_id TEXT DEFAULT 's1',
        key TEXT,
        value TEXT,
        PRIMARY KEY (session_id, key)
    );
    CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(ts);
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
    CREATE INDEX IF NOT EXISTS idx_paper_ts ON paper_trades(ts);
    CREATE INDEX IF NOT EXISTS idx_paper_session ON paper_trades(session_id);
    """)
    conn.commit()
    conn.close()
    print(f"[db] Database initialized: {DB_PATH}", flush=True)


def db_insert_tick(snap):
    try:
        conn = _get_db()
        cols = [
            'ts', 'chainlink_price', 'binance_price',
            'delta_from_prev', 'delta_from_open',
            'ofi_1min', 'ofi_3min', 'ofi_5min',
            'buy_vol_1min', 'sell_vol_1min', 'trade_count_1min',
            'p_model', 'snr', 'sigma',
            'signal', 'signal_side', 'signal_confidence',
            'window_start', 'window_end', 'market_slug',
            'no_bid', 'no_ask', 'no_bid_vol', 'no_ask_vol',
            'yes_bid', 'yes_ask', 'yes_bid_vol', 'yes_ask_vol',
            'bn_spread',
            'btc_dvol', 'dvol_change_5m', 'btc_index', 'funding_rate',
            'perp_mark', 'perp_oi', 'perp_volume_24h',
            'perp_ofi_1m', 'perp_buy_vol_1m', 'perp_sell_vol_1m',
            'perp_trade_count_1m', 'atm_iv', 'atm_strike', 'perp_spread',
            'put_call_ratio', 'iv_skew', 'gamma_exposure',
            'options_flow_ratio', 'options_flow_net_puts', 'options_flow_net_calls', 'options_flow_count',
            'liq_buy_vol_1m', 'liq_sell_vol_1m', 'liq_count_1m', 'liq_net_1m',
            'long_short_ratio', 'long_account', 'short_account',
            'top_trader_ls_ratio', 'taker_buy_vol', 'taker_sell_vol', 'taker_buy_ratio',
            'bn_futures_oi', 'bn_futures_oi_change',
            'futures_basis_1m', 'futures_basis_3m', 'term_spread',
            'iv_term_1d', 'iv_term_7d', 'iv_term_30d', 'iv_term_spread',
            'max_pain_strike', 'max_pain_dist',
            'block_trades_1m', 'block_net_direction',
            'rv_iv_spread'
        ]
        vals = [snap.get(c) if c != 'ts' else snap['ts'] for c in cols]
        placeholders = ','.join(['?'] * len(cols))
        col_names = ','.join(cols)
        conn.execute(f"INSERT INTO ticks ({col_names}) VALUES ({placeholders})", vals)
        conn.commit()
    except Exception as e:
        if 'tick_err_cnt' not in db_insert_tick.__dict__:
            db_insert_tick.tick_err_cnt = 0
        db_insert_tick.tick_err_cnt += 1
        if db_insert_tick.tick_err_cnt <= 3:
            print(f"[db] tick error: {e}", file=sys.stderr, flush=True)


def db_insert_signal(snap):
    try:
        conn = _get_db()
        conn.execute("""INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (snap['ts'], snap.get('signal'), snap.get('signal_side'),
             snap.get('delta_from_prev'), snap.get('p_model'), snap.get('snr'),
             snap.get('ofi_1min'), snap.get('ofi_5min'),
             snap.get('signal_entry'), snap.get('signal_confidence'),
             snap.get('chainlink_price'), snap.get('binance_price'),
             snap.get('market_slug'), snap.get('window_start'), snap.get('window_end')))
        conn.commit()
    except Exception as e:
        print(f"[db] signal error: {e}", file=sys.stderr, flush=True)


def db_insert_orderbook(ts, slug, side, bids, asks):
    try:
        conn = _get_db()
        conn.execute("INSERT INTO orderbook_snapshots VALUES (?,?,?,?,?)",
            (ts, slug, side, json.dumps(bids), json.dumps(asks)))
        conn.commit()
    except Exception as e:
        print(f"[db] ob error: {e}", file=sys.stderr, flush=True)


def db_insert_paper_trade(trade_dict):
    """Insert a paper trade and return its DB id."""
    try:
        conn = _get_db()
        cur = conn.execute(
            "INSERT INTO paper_trades (ts,side,entry_price,stake,shares,ob_shares,signal,delta,p_model,confidence,window_start,market_slug,session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_dict['ts'], trade_dict['side'], trade_dict['entry'],
             trade_dict['stake'], trade_dict.get('shares'),
             trade_dict.get('ob_shares'), trade_dict.get('signal'),
             trade_dict.get('delta'), trade_dict.get('p_model'),
             trade_dict.get('confidence'), trade_dict.get('window'),
             trade_dict.get('market_slug'), trade_dict.get('session_id', 's1')))
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        print(f"[db] paper_trade insert error: {e}", file=sys.stderr, flush=True)
        return None


def db_resolve_paper_trade(trade_id, pnl, result, outcome, resolved_ts):
    """Update a paper trade with resolution data."""
    try:
        conn = _get_db()
        conn.execute(
            "UPDATE paper_trades SET pnl=?, result=?, outcome=?, resolved_ts=? WHERE id=?",
            (pnl, result, outcome, resolved_ts, trade_id))
        conn.commit()
    except Exception as e:
        print(f"[db] paper_trade resolve error: {e}", file=sys.stderr, flush=True)


def db_save_paper_config(config, session_id="s1"):
    """Save paper trading config as key-value pairs for a session."""
    try:
        conn = _get_db()
        for k, v in config.items():
            conn.execute(
                "INSERT OR REPLACE INTO paper_config (session_id, key, value) VALUES (?, ?, ?)",
                (session_id, k, str(v)))
        conn.commit()
    except Exception as e:
        print(f"[db] paper_config save error: {e}", file=sys.stderr, flush=True)


def db_load_paper_config(session_id=None):
    """Load paper config. If session_id=None, load ALL sessions as (key, value, session_id) tuples."""
    try:
        conn = _get_db()
        if session_id is None:
            rows = conn.execute("SELECT key, value, session_id FROM paper_config").fetchall()
            return rows  # [(key, value, session_id), ...]
        else:
            rows = conn.execute(
                "SELECT key, value FROM paper_config WHERE session_id=?", (session_id,)).fetchall()
            return {k: v for k, v in rows}
    except Exception:
        return [] if session_id is None else {}


def db_load_paper_trades(limit=200, session_id=None):
    """Load resolved paper trades for history restore."""
    try:
        conn = _get_db()
        conn.row_factory = sqlite3.Row
        if session_id:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE result IS NOT NULL AND session_id=? ORDER BY resolved_ts DESC LIMIT ?",
                (session_id, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE result IS NOT NULL ORDER BY resolved_ts DESC LIMIT ?",
                (limit,)).fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[db] paper_trades load error: {e}", file=sys.stderr, flush=True)
        return []


def db_load_active_paper_trades(session_id=None):
    """Load unresolved (active) paper trades."""
    try:
        conn = _get_db()
        conn.row_factory = sqlite3.Row
        if session_id:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE result IS NULL AND session_id=? ORDER BY ts DESC",
                (session_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE result IS NULL ORDER BY ts DESC").fetchall()
        conn.row_factory = None
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[db] active paper_trades load error: {e}", file=sys.stderr, flush=True)
        return []


def db_load_recent_prices(minutes=30):
    """Load binance_price from ticks for last N minutes (for range30 warmup)."""
    try:
        conn = _get_db()
        rows = conn.execute(
            "SELECT ts, binance_price FROM ticks WHERE binance_price IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?", (minutes * 60,)).fetchall()
        # Return as [(unix_ts, price), ...] oldest first
        result = []
        for ts_str, price in reversed(rows):
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(ts_str)
                result.append((dt.timestamp(), price))
            except Exception:
                pass
        return result
    except Exception as e:
        print(f"[db] load_recent_prices error: {e}", file=sys.stderr, flush=True)
        return []


def db_insert_window(w_start, w_end, slug, open_p, token_ids):
    try:
        conn = _get_db()
        conn.execute("INSERT OR REPLACE INTO windows VALUES (?,?,?,?,?,?)",
            (w_start, w_end, slug, open_p, None, json.dumps(token_ids)))
        conn.commit()
    except Exception as e:
        print(f"[db] window error: {e}", file=sys.stderr, flush=True)
