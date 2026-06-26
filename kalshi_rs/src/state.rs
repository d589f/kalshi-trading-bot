//! Shared application state — the Rust analogue of `state.py`'s module globals, but
//! owned behind a single `Arc<Mutex<AppState>>`. Feed tasks write; the 0.3s signal
//! loop reads + maintains the derived series.

use crate::kalshi::KalshiBook;
use crate::window::WindowState;

#[derive(Default)]
pub struct AppState {
    /// Primary BTC price (Binance @aggTrade) and its wall-clock receipt time.
    pub binance_price: Option<f64>,
    pub binance_ts: f64,
    /// (ts, notional_usd, is_sell) — pruned to last 300 s for OFI.
    pub trade_buffer: Vec<(f64, f64, bool)>,
    /// (ts, price) sampled by the signal loop at 0.3 s, trimmed to last 2100 s.
    pub price_history: Vec<(f64, f64)>,
    /// Latest Kalshi top-of-book for the current window's market.
    pub kalshi: Option<KalshiBook>,
    /// Window model (15-min KXBTC15M).
    pub window: WindowState,
}

impl AppState {
    pub fn new() -> Self {
        Self::default()
    }
}
