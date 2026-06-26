"""Backtest engine — NEWUPDATEDDATA P-model backtester."""
import os
import math

from .config import BASE

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False
    print("[dashboard] WARNING: pandas/numpy not installed, backtest disabled")

# Loaded dataframes
_df5 = None
_df15 = None
_data_info = {'windows_5min': 0, 'windows_15min': 0}

SIGMA_STATIC = 0.0005

# Sigma name -> CSV column mapping
# For intra-window sigmas, {e} is replaced with entry minute (1/2/3)
SIGMA_MAP = {
    'static':           '__static__',
    'trail5':           'sigma_trail5',
    'trail10':          'sigma_trail10',
    'trail30':          'sigma_trail30',
    'trail5_pmin':      'sigma_per_min_trail5',
    'trail10_pmin':     'sigma_per_min_trail10',
    'trail30_pmin':     'sigma_per_min_trail30',
    'max5':             'sigma_max_trail5',
    'max10':            'sigma_max_trail10',
    'max30':            'sigma_max_trail30',
    'max5_pmin':        'sigma_max_pmin_trail5',
    'max10_pmin':       'sigma_max_pmin_trail10',
    'max30_pmin':       'sigma_max_pmin_trail30',
    'range5':           'sigma_range_trail5',
    'range10':          'sigma_range_trail10',
    'range30':          'sigma_range_trail30',
    'range5_pmin':      'sigma_range_pmin_trail5',
    'range10_pmin':     'sigma_range_pmin_trail10',
    'range30_pmin':     'sigma_range_pmin_trail30',
    'realized5':        'sigma_realized_trail5',
    'realized10':       'sigma_realized_trail10',
    'realized5_pmin':   'sigma_realized_pmin_trail5',
    'realized10_pmin':  'sigma_realized_pmin_trail10',
    'median5':          'sigma_median_trail5',
    'median10':         'sigma_median_trail10',
    'median5_pmin':     'sigma_median_pmin_trail5',
    'median10_pmin':    'sigma_median_pmin_trail10',
    'mad5':             'sigma_mad_trail5',
    'mad10':            'sigma_mad_trail10',
    'intra':            'sigma_intra_{e}min',
    'intra_pmin':       'sigma_intra_per_min_{e}min',
    'intra_max':        'sigma_intra_max_{e}min',
    'intra_range':      'sigma_intra_range_{e}min',
}


def _phi(x):
    """Standard normal CDF."""
    return 0.5 * (1 + math.erf(x / 1.4142135623730951))


def load_backtest_data():
    """Load NEWUPDATEDDATA windows CSVs."""
    global _df5, _df15, _data_info
    if not HAS_PANDAS:
        return

    out_dir = os.path.join(BASE, "analysis_output", "newdata_full")
    f5 = os.path.join(out_dir, "windows_5min.csv")
    f15 = os.path.join(out_dir, "windows_15min.csv")

    if os.path.exists(f5):
        _df5 = pd.read_csv(f5)
        _df5['window_start'] = pd.to_datetime(_df5['window_start'])
        _data_info['windows_5min'] = len(_df5)
        print(f"[backtest] Loaded {len(_df5)} 5-min windows", flush=True)
    else:
        print(f"[backtest] No 5-min data at {f5}", flush=True)

    if os.path.exists(f15):
        _df15 = pd.read_csv(f15)
        _df15['window_start'] = pd.to_datetime(_df15['window_start'])
        _data_info['windows_15min'] = len(_df15)
        print(f"[backtest] Loaded {len(_df15)} 15-min windows", flush=True)
    else:
        print(f"[backtest] No 15-min data at {f15}", flush=True)


def _get_sigma_col(sigma_name, entry_min):
    """Resolve sigma column name for given entry minute."""
    template = SIGMA_MAP.get(sigma_name)
    if template is None:
        return None
    if template == '__static__':
        return '__static__'
    return template.replace('{e}', str(entry_min))


def run_backtest(params):
    """Run P-model backtest with custom parameters. Returns trades + equity curve."""
    market = params.get('market', '5min')
    df = _df5 if market == '5min' else _df15
    if df is None or df.empty:
        return {'error': f'No {market} data loaded'}

    # Parameters
    sigma_name = params.get('sigma', 'static')
    tau_mode = params.get('tau_mode', 'sqrt')
    kappa = float(params.get('kappa', 1.5))
    entry_min = int(params.get('entry_min', 2))
    delta_thresh = float(params.get('delta_thresh', 50))
    p_thresh = float(params.get('p_thresh', 0))
    ofi_filter = params.get('ofi_filter', 'off')
    side = params.get('side', 'NO')
    stake = float(params.get('stake', 100))
    edge_thresh = float(params.get('edge_thresh', 0))
    max_entry = float(params.get('max_entry', 0.95))
    min_stake = float(params.get('min_stake', 5))
    delta_source = params.get('delta_source', 'binance')

    # Resolve columns
    if delta_source == 'binance':
        delta_col = f'bn_delta_at_{entry_min}min'
        delta_abs_col = f'bn_delta_abs_at_{entry_min}min'
    else:
        delta_col = f'delta_at_{entry_min}min'
        delta_abs_col = f'delta_abs_at_{entry_min}min'
    tau_col = f'tau_remaining_{entry_min}min'
    entry_price_col = f'poly_no_ask_at_{entry_min}min'
    entry_bid_col = f'poly_no_bid_at_{entry_min}min'
    l1_col = f'poly_l1_shares_{entry_min}min'
    ofi_col = f'ofi_at_{entry_min}min'
    sigma_col = _get_sigma_col(sigma_name, entry_min)

    if sigma_col is None:
        return {'error': f'Unknown sigma: {sigma_name}'}

    # Check columns exist
    needed = [delta_col, 'resolve']
    for c in needed:
        if c not in df.columns:
            return {'error': f'Column {c} not found'}

    trades = []
    balance = 10000.0
    equity_curve = [{'x': 0, 'y': balance, 'ts': ''}]

    for idx, row in df.iterrows():
        # Get delta
        delta = row.get(delta_col)
        if pd.isna(delta):
            continue
        abs_delta = abs(delta)

        # Delta threshold filter
        if abs_delta < delta_thresh:
            continue

        # Determine direction
        if delta < 0:
            trade_side = 'NO'
        else:
            trade_side = 'YES'

        if side == 'NO' and trade_side != 'NO':
            continue
        if side == 'YES' and trade_side != 'YES':
            continue

        # Get entry price
        # IMPORTANT: poly_no_ask/bid columns use bid<0.5 heuristic, which picks
        # the CHEAP token. The cheap token is the LOSING side for the direction
        # BTC has already moved. The WINNING token's ask ≈ 1 - cheap_token_bid.
        #
        # So for BOTH sides (NO and YES bets), the correct entry price
        # for the winning-direction token is: 1 - poly_no_bid (cheap token bid).
        cheap_bid = row.get(entry_bid_col)
        cheap_ask = row.get(entry_price_col)
        if pd.isna(cheap_bid) or cheap_bid <= 0:
            continue
        # Entry = ask of the expensive (correct) token ≈ 1 - cheap_bid
        entry_price = 1 - cheap_bid
        if entry_price <= 0.01 or entry_price >= 1:
            continue
        if entry_price > max_entry:
            continue

        # Get sigma
        if sigma_col == '__static__':
            sigma = SIGMA_STATIC
        else:
            sigma = row.get(sigma_col)
            if pd.isna(sigma) or sigma <= 0:
                continue

        # Get tau
        tau = row.get(tau_col, entry_min)
        if pd.isna(tau) or tau <= 0:
            tau = 5 - entry_min if market == '5min' else 15 - entry_min

        # Compute f(tau)
        if tau_mode == 'sqrt':
            f_tau = math.sqrt(tau)
        elif tau_mode == 'linear':
            f_tau = tau
        else:  # none
            f_tau = 1.0

        # Compute p-model (sigma is fractional, delta is in USD — convert sigma to USD)
        ref_price = row.get('cl_start_price') or row.get('bn_price_start') or 70000
        sigma_usd = sigma * ref_price
        denom = sigma_usd * f_tau
        if denom <= 0:
            continue
        snr = abs_delta / denom
        p_model = _phi(kappa * snr)

        # P-model threshold filter
        if p_thresh > 0 and p_model < p_thresh:
            continue

        # Edge filter
        edge = p_model - entry_price
        if edge_thresh > 0 and edge < edge_thresh:
            continue

        # OFI filter
        ofi_val = row.get(ofi_col, 0)
        if pd.isna(ofi_val):
            ofi_val = 0

        if ofi_filter == 'agree':
            if trade_side == 'NO' and ofi_val > 0:
                continue
            if trade_side == 'YES' and ofi_val < 0:
                continue
        elif ofi_filter == 'strong':
            if trade_side == 'NO' and ofi_val > -1_000_000:
                continue
            if trade_side == 'YES' and ofi_val < 1_000_000:
                continue
        elif ofi_filter == 'vstrong':
            if trade_side == 'NO' and ofi_val > -2_000_000:
                continue
            if trade_side == 'YES' and ofi_val < 2_000_000:
                continue

        # L1 liquidity
        l1 = row.get(l1_col, 0)
        if pd.isna(l1):
            l1 = 0
        max_buy = l1 * entry_price if l1 > 0 else stake
        real_stake = min(stake, max_buy) if max_buy > 0 else stake
        if real_stake < min_stake:
            continue

        # Resolve
        resolve = row['resolve']
        win = (trade_side == 'NO' and resolve == 'Down') or \
              (trade_side == 'YES' and resolve == 'Up')

        if win:
            pnl = real_stake / entry_price * (1 - entry_price)
        else:
            pnl = -real_stake

        balance += pnl
        window_ts = str(row['window_start'])

        # Polymarket orderbook details at entry
        # cheap_ask/bid = the cheap token (bid<0.5 in data)
        # real entry = 1 - cheap_bid = expensive token ask
        no_ask = cheap_ask   # cheap token ask (for display)
        no_bid = cheap_bid   # cheap token bid (for display)
        no_ask_vol = row.get(f'poly_no_ask_vol_at_{entry_min}min', 0)
        # End-of-window prices (resolve prices)
        no_ask_end = row.get('poly_no_ask_end')
        no_bid_end = row.get('poly_no_bid_end')
        # Chainlink prices
        cl_start = row.get('cl_start_price', 0)
        cl_end = row.get('cl_end_price', 0)
        cl_delta_full = row.get('cl_delta', 0)
        # Binance
        bn_start = row.get('bn_price_start', 0)
        bn_end = row.get('bn_price_end', 0)
        # Poly liquidity
        total_bid_vol = row.get('poly_avg_total_bid_vol', 0)
        total_ask_vol = row.get('poly_avg_total_ask_vol', 0)
        n_bid_levels = row.get('poly_n_bid_levels', 0)
        n_ask_levels = row.get('poly_n_ask_levels', 0)
        # Shares bought
        shares_bought = real_stake / entry_price if entry_price > 0 else 0

        trades.append({
            'n': len(trades) + 1,
            'window': window_ts[:16],
            'side': trade_side,
            'delta': round(delta, 1),
            'abs_delta': round(abs_delta, 1),
            'entry': round(entry_price, 4),
            'sigma': round(sigma, 8),
            'tau': round(tau, 1),
            'p_model': round(p_model, 4),
            'edge': round(p_model - entry_price, 4),
            'ofi': round(ofi_val, 0),
            'l1_shares': round(l1, 0),
            'stake': round(real_stake, 2),
            'shares': round(shares_bought, 1),
            'resolve': resolve,
            'win': win,
            'pnl': round(pnl, 2),
            'balance': round(balance, 2),
            # Polymarket orderbook
            'no_ask': round(no_ask, 4) if pd.notna(no_ask) else None,
            'no_bid': round(no_bid, 4) if pd.notna(no_bid) else None,
            'no_ask_vol': round(no_ask_vol, 0) if pd.notna(no_ask_vol) else 0,
            'no_ask_end': round(no_ask_end, 4) if pd.notna(no_ask_end) else None,
            'no_bid_end': round(no_bid_end, 4) if pd.notna(no_bid_end) else None,
            'total_bid_vol': round(total_bid_vol, 0) if pd.notna(total_bid_vol) else 0,
            'total_ask_vol': round(total_ask_vol, 0) if pd.notna(total_ask_vol) else 0,
            # Prices
            'cl_start': round(cl_start, 2) if pd.notna(cl_start) else 0,
            'cl_end': round(cl_end, 2) if pd.notna(cl_end) else 0,
            'cl_delta_full': round(cl_delta_full, 1) if pd.notna(cl_delta_full) else 0,
            'bn_start': round(bn_start, 2) if pd.notna(bn_start) else 0,
            'bn_end': round(bn_end, 2) if pd.notna(bn_end) else 0,
        })

        equity_curve.append({
            'x': len(trades),
            'y': round(balance, 2),
            'ts': window_ts[:16],
        })

    # Summary
    n = len(trades)
    wins = sum(1 for t in trades if t['win'])
    losses = n - wins
    total_pnl = sum(t['pnl'] for t in trades)
    total_staked = sum(t['stake'] for t in trades)
    avg_entry = sum(t['entry'] for t in trades) / n if n > 0 else 0
    avg_edge = sum(t['edge'] for t in trades) / n if n > 0 else 0
    max_dd = 0
    peak = 10000.0
    for t in trades:
        bal = t['balance']
        if bal > peak:
            peak = bal
        dd = peak - bal
        if dd > max_dd:
            max_dd = dd

    ev = (wins / n * (1 - avg_entry) - losses / n * avg_entry) if n > 0 else 0

    return {
        'summary': {
            'trades': n,
            'wins': wins,
            'losses': losses,
            'wr': round(wins / n, 4) if n > 0 else 0,
            'total_pnl': round(total_pnl, 2),
            'total_staked': round(total_staked, 2),
            'roi': round(total_pnl / total_staked * 100, 2) if total_staked > 0 else 0,
            'ev_per_dollar': round(ev, 4),
            'avg_entry': round(avg_entry, 4),
            'avg_edge': round(avg_edge, 4),
            'max_drawdown': round(max_dd, 2),
            'final_balance': round(balance, 2),
        },
        'trades': trades,
        'equity_curve': equity_curve,
        'data_info': _data_info,
        'params': params,
    }


def run_paper_backtest(params):
    """Legacy paper backtest — returns empty."""
    return {'error': 'Use the new backtest page instead'}
