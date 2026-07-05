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

    /// The `f1_d50cap75` paper session (params live only in the paper DB, mirrored here).
    /// Earlier entry (180 s) on 2.5x bigger moves with a stricter confidence gate than f6.
    /// NOTE: the "cap75" in the name is a legacy Polymarket-era label — the live paper
    /// config runs max_entry_price 0.92, identical to f6.
    pub fn f1_d50cap75() -> Self {
        SessionConfig {
            name: "F1 d50cap75".to_string(),
            kappa: 0.4,
            delta_threshold: 50.0,
            p_model_threshold: 0.65,
            sigma_type: "max10".to_string(),
            tau_mode: TauMode::Linear,
            trade_side: TradeSide::Both,
            max_entry_price: 0.92,
            stake: 100.0,
            entry_wait_min: 3.0, // 180 s
            liq_filter: false, // off in the paper F1 config too
            vol_imb_kill: 0.0,
            kill_hours: vec![],
            ofi_align: false,
            sigma_max: None,
        }
    }
}

/// The closed set of strategies the live bot can run. Selected at startup via the
/// `SESSION` env; everything derived from the choice (params, ledger tag, coid prefix,
/// mirror session key) hangs off this enum so a strategy switch cannot half-apply.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SessionSel {
    F6,
    F1,
}

impl SessionSel {
    /// The paper engine's session id — also the default mirror key.
    pub fn key(&self) -> &'static str {
        match self {
            SessionSel::F6 => "f6_wait270",
            SessionSel::F1 => "f1_d50cap75",
        }
    }

    pub fn config(&self) -> SessionConfig {
        match self {
            SessionSel::F6 => SessionConfig::f6_wait270(),
            SessionSel::F1 => SessionConfig::f1_d50cap75(),
        }
    }

    /// Ledger `session` tag. For F6 this must stay byte-identical to the legacy
    /// `SESSION_NAME` const ("f6_wait270_shadow") so existing ledger replay/filters hold.
    pub fn session_tag(&self) -> String {
        format!("{}_shadow", self.key())
    }

    /// Client-order-id prefix (Kalshi coid must attribute fills to the strategy).
    pub fn coid_prefix(&self) -> &'static str {
        match self {
            SessionSel::F6 => "f6-",
            SessionSel::F1 => "f1-",
        }
    }
}

/// Pure resolver: trimmed, case-sensitive exact match against the known session keys.
/// Empty/unknown → None. Fail-loud logging and the f6 fallback are main()'s job —
/// this module stays pure (no logging, no exit).
pub fn resolve_session_sel(name: &str) -> Option<SessionSel> {
    match name.trim() {
        "f6_wait270" => Some(SessionSel::F6),
        "f1_d50cap75" => Some(SessionSel::F1),
        _ => None,
    }
}

/// (Convenience form of the resolver — kept as the stable API named by the
/// architecture review; prod wiring goes through `resolve_session_sel`.)
#[allow(dead_code)]
pub fn resolve_session(name: &str) -> Option<SessionConfig> {
    resolve_session_sel(name).map(|s| s.config())
}

/// The paper-engine session key the mirror reads `live_sigma` from: an explicit
/// non-empty MIRROR_SESSION env wins, otherwise the selected strategy's own key.
pub fn resolve_mirror_session_key(sel: &SessionSel, mirror_env: Option<&str>) -> String {
    match mirror_env.map(str::trim).filter(|s| !s.is_empty()) {
        Some(k) => k.to_string(),
        None => sel.key().to_string(),
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

#[cfg(test)]
mod tests {
    use super::*;

    // TC-1.2: every F1 field exactly as the paper DB config (kappa 0.4, d50, p 0.65,
    // max10, 180s, cap 0.92 — NOT the legacy 0.75 the name implies).
    #[test]
    fn f1_factory_params_exact() {
        let c = SessionConfig::f1_d50cap75();
        assert_eq!(c.kappa, 0.4);
        assert_eq!(c.delta_threshold, 50.0);
        assert_eq!(c.p_model_threshold, 0.65);
        assert_eq!(c.sigma_type, "max10");
        assert_eq!(c.tau_mode, TauMode::Linear);
        assert_eq!(c.trade_side, TradeSide::Both);
        assert_eq!(c.max_entry_price, 0.92);
        assert_eq!(c.entry_wait_min, 3.0);
        assert!(!c.liq_filter);
        assert_eq!(c.sigma_max, None);
        assert_eq!(c.vol_imb_kill, 0.0);
        assert!(c.kill_hours.is_empty());
        assert!(!c.ofi_align);
    }

    // TC-2.1: f6 factory unchanged (regression pin for the byte-identity guarantee).
    #[test]
    fn f6_factory_params_exact() {
        let c = SessionConfig::f6_wait270();
        assert_eq!(c.kappa, 0.5);
        assert_eq!(c.delta_threshold, 20.0);
        assert_eq!(c.p_model_threshold, 0.60);
        assert_eq!(c.sigma_type, "max30");
        assert_eq!(c.max_entry_price, 0.92);
        assert_eq!(c.entry_wait_min, 4.5);
        assert!(!c.liq_filter);
    }

    // TC-1.1 / TC-1.11 / TC-2.2 / TC-10.1 / TC-10.4 / TC-10.5: resolver matrix —
    // trim yes, case-sensitivity yes, empty/garbage/near-miss → None.
    #[test]
    fn resolver_matrix() {
        assert_eq!(resolve_session_sel("f1_d50cap75"), Some(SessionSel::F1));
        assert_eq!(resolve_session_sel("f6_wait270"), Some(SessionSel::F6));
        assert_eq!(resolve_session_sel(" f1_d50cap75 "), Some(SessionSel::F1));
        assert_eq!(resolve_session_sel(""), None);
        assert_eq!(resolve_session_sel("   "), None);
        assert_eq!(resolve_session_sel("F1_D50CAP75"), None);
        assert_eq!(resolve_session_sel("f7_foo"), None);
        assert_eq!(resolve_session_sel("f1_d50cap7"), None);
        assert_eq!(resolve_session_sel("f1_d50cap75x"), None);
    }

    #[test]
    fn resolve_session_maps_to_config() {
        assert_eq!(resolve_session("f1_d50cap75").unwrap().sigma_type, "max10");
        assert_eq!(resolve_session("f6_wait270").unwrap().sigma_type, "max30");
        assert!(resolve_session("nope").is_none());
    }

    // TC-1.3: attribution derivations. F6's tag MUST equal the legacy SESSION_NAME
    // const byte-for-byte or old ledger replay/filters break.
    #[test]
    fn session_derivations() {
        assert_eq!(SessionSel::F6.key(), "f6_wait270");
        assert_eq!(SessionSel::F1.key(), "f1_d50cap75");
        assert_eq!(SessionSel::F6.session_tag(), "f6_wait270_shadow");
        assert_eq!(SessionSel::F1.session_tag(), "f1_d50cap75_shadow");
        assert_eq!(SessionSel::F6.coid_prefix(), "f6-");
        assert_eq!(SessionSel::F1.coid_prefix(), "f1-");
        assert_eq!(SessionSel::F1.config().name, "F1 d50cap75");
    }

    // TC-3.3: mirror key = explicit env override wins, else the session's own key;
    // whitespace-only env is treated as unset.
    #[test]
    fn mirror_key_resolution() {
        assert_eq!(resolve_mirror_session_key(&SessionSel::F1, None), "f1_d50cap75");
        assert_eq!(resolve_mirror_session_key(&SessionSel::F6, None), "f6_wait270");
        assert_eq!(
            resolve_mirror_session_key(&SessionSel::F1, Some("f6_wait270")),
            "f6_wait270"
        );
        assert_eq!(
            resolve_mirror_session_key(&SessionSel::F1, Some("  ")),
            "f1_d50cap75"
        );
        assert_eq!(
            resolve_mirror_session_key(&SessionSel::F6, Some(" f1_d50cap75 ")),
            "f1_d50cap75"
        );
    }
}
