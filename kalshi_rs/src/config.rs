//! Session config + global constants — mirrors `DEFAULT_CONFIG` (paper_trading.py §9.1)
//! and the live `f6_wait270` config (README "Live identity").

use crate::signal::TauMode;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TradeSide {
    Yes,
    No,
    Both,
}

/// One paper/shadow session's strategy parameters. Only the keys the classic
/// `pmodel` path needs are kept (the live `third`/f6 strategy is `strategy_type='pmodel'`).
#[derive(Clone, Debug)]
pub struct SessionConfig {
    pub name: String,
    pub kappa: f64,
    pub delta_threshold: f64,
    pub p_model_threshold: f64,
    pub sigma_type: String,
    pub tau_mode: TauMode,
    pub trade_side: TradeSide,
    pub max_entry_price: f64,
    pub stake: f64,
    pub entry_wait_min: f64,
    /// per-session liquidity gate ('on' → spread/depth check). Polymarket-tuned thresholds;
    /// re-tune for Kalshi before relying on it (see LIQ_* below).
    pub liq_filter: bool,
    pub vol_imb_kill: f64,
    pub kill_hours: Vec<i32>,
    pub ofi_align: bool,
    /// optional sigma cap → 'SIGMA-HIGH' (the s_sigfilt knob). None = no cap.
    pub sigma_max: Option<f64>,
}

impl SessionConfig {
    /// The exact live config the best PROD performer runs (README "Live identity: F6 wait270").
    /// `third` P-model with entry delayed to 270 s into the 15-min window.
    pub fn f6_wait270() -> Self {
        SessionConfig {
            name: "F6 wait270".to_string(),
            kappa: 0.5,
            delta_threshold: 20.0,
            p_model_threshold: 0.60,
            sigma_type: "max30".to_string(),
            tau_mode: TauMode::Linear,
            trade_side: TradeSide::Both,
            max_entry_price: 0.92,
            stake: 100.0,
            entry_wait_min: 4.5, // 270 s
            liq_filter: false,   // OFF for the shadow run until LIQ thresholds are re-tuned for Kalshi
            vol_imb_kill: 0.0,
            kill_hours: vec![],
            ofi_align: false,
            sigma_max: None,
        }
    }
}

// ---- global constants (config.py §10.1 + paper_trading.py §10.2) ----

/// Live `f6_wait270` runs on the 15-minute KXBTC15M series — NOT the spec's local 5.
pub const WINDOW_MINUTES: i64 = 15;

/// BTC price fallback chain literal (compute_p_model): binance → chainlink → 70000.
pub const BTC_FALLBACK: f64 = 70000.0;

/// Polymarket fee constants (paper engine). Kalshi has a DIFFERENT fee schedule — see
/// kalshi::fees. Kept here only to document the paper engine's accounting.
pub const PM_FEE_RATE: f64 = 0.25;
pub const PM_FEE_EXPONENT: i32 = 2;

/// Global liquidity gate thresholds (paper_trading.py) — POLYMARKET-tuned. Kalshi books are
/// quoted in cents/contracts; re-tune before enabling `liq_filter`.
pub const LIQ_SPREAD_KILL: f64 = 0.0142;
pub const LIQ_DEPTH_KILL: f64 = 424.0;

/// state_updater cadence — the engine recomputes sigma/ofi/delta/signal + evaluates
/// sessions every 0.3 s. We sample the price history at this cadence to match `_price_history`.
pub const STATE_TICK_SECS: f64 = 0.3;

/// `_price_history` retention (35 min) and trade_buffer retention (5 min).
pub const PRICE_HISTORY_RETENTION_SECS: f64 = 2100.0;
pub const TRADE_BUFFER_RETENTION_SECS: f64 = 300.0;
