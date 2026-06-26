//! Signal evaluation — the classic P-model filter chain (`_evaluate_signal`, §6.2),
//! specialized to the live `third`/f6 strategy (`strategy_type='pmodel'`, not weighted).
//!
//! Returns the exact same reason strings the dashboard shows ("WAITING", "WAIT",
//! "DELTA LOW", "AMBIGUOUS", ...) so shadow logs line up with paper logs.

use std::collections::HashMap;

use crate::config::{SessionConfig, TradeSide, BTC_FALLBACK, LIQ_DEPTH_KILL, LIQ_SPREAD_KILL};
use crate::signal::{get_sigma_for_type, p_model_classic};

/// The subset of `shared` the classic evaluator reads, plus the venue book fields
/// (named generically — `poly_*` in the source is "the venue book", §11.1).
#[derive(Clone, Debug, Default)]
pub struct Shared {
    pub binance_price: Option<f64>,
    pub delta_from_open: Option<f64>,
    pub delta_from_prev: Option<f64>,
    pub tau: f64,
    pub elapsed_min: f64,
    pub sigmas: HashMap<String, f64>,
    pub sigma: Option<f64>, // state['sigma'] default
    pub ofi_1min: f64,
    pub buy_vol_1min: f64,
    pub sell_vol_1min: f64,
    pub hour: i32,            // UTC hour of window_start
    // venue book (Kalshi), prices in [0,1]
    pub yes_bid: Option<f64>,
    pub yes_ask: Option<f64>,
    pub yes_ask_vol: Option<f64>,
    pub yes_bid_vol: Option<f64>,
    pub no_bid: Option<f64>,
    pub no_ask: Option<f64>,
    pub no_ask_vol: Option<f64>,
    pub no_bid_vol: Option<f64>,
    pub last_trade_expensive: Option<f64>,
}

/// A firing decision (the order we WOULD place).
#[derive(Clone, Debug)]
pub struct Fire {
    pub side: Side,
    pub entry: f64,
    pub conf: Option<f64>, // p
    pub snr: f64,
    pub p: Option<f64>,
    pub sigma_used: f64,
    pub orderbook_entry: f64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Yes,
    No,
}

impl Side {
    pub fn as_str(&self) -> &'static str {
        match self {
            Side::Yes => "YES",
            Side::No => "NO",
        }
    }
}

/// Result of one evaluation: always a reason; `fire` is Some on a trigger.
#[derive(Clone, Debug)]
pub struct EvalResult {
    pub reason: String,
    pub fire: Option<Fire>,
}

#[inline]
fn sign(x: f64) -> i32 {
    if x >= 0.0 {
        1
    } else {
        -1
    }
}

/// `_evaluate_signal` (classic pmodel) for the f6/third config. Filter order is EXACT.
pub fn evaluate(cfg: &SessionConfig, s: &Shared) -> EvalResult {
    macro_rules! skip {
        ($($arg:tt)*) => {
            return EvalResult { reason: format!($($arg)*), fire: None }
        };
    }

    // 1. delta_from_open
    let delta = match s.delta_from_open {
        Some(d) => d,
        None => skip!("WAITING"),
    };

    // 2. entry_wait
    if s.elapsed_min < cfg.entry_wait_min {
        skip!("WAIT ({:.1}/{:.1})", s.elapsed_min, cfg.entry_wait_min);
    }

    // (3. weighted branch — not used by f6)

    // 4. delta threshold
    if delta.abs() < cfg.delta_threshold {
        skip!("DELTA LOW ({:.1}<{:.0})", delta, cfg.delta_threshold);
    }

    // 5. dual-confirmation (classic only): sign(open) must match sign(prev)
    if let Some(dprev) = s.delta_from_prev {
        if sign(delta) != sign(dprev) {
            skip!("AMBIGUOUS (open {:+.1} vs prev {:+.1})", delta, dprev);
        }
    }

    // 6. regime block
    if let Some(r) = regime_block(cfg, delta, s) {
        skip!("{}", r);
    }

    // 7. sigma + sigma_max cap
    let mut sigma = s
        .sigmas
        .get(&cfg.sigma_type)
        .copied()
        .filter(|v| *v > 0.0)
        .or(s.sigma);
    if let Some(sm) = cfg.sigma_max {
        if let Some(sg) = sigma {
            if sg > sm {
                skip!("SIGMA-HIGH ({:.5}>{:.5})", sg, sm);
            }
        }
    }
    // if sigmas[type] present, prefer it (step 7 redundant assignment)
    if let Some(&v) = s.sigmas.get(&cfg.sigma_type) {
        if v > 0.0 {
            sigma = Some(v);
        }
    }
    let _ = get_sigma_for_type; // keep helper referenced for other strategies

    // 8. p-model
    let price_ref = s.binance_price.unwrap_or(BTC_FALLBACK);
    let (p, snr, sigma_used) = match sigma {
        Some(sg) => match p_model_classic(delta, sg, s.tau, price_ref, cfg.kappa, cfg.tau_mode) {
            Some((p, snr)) => (Some(p), snr, sg),
            None => (None, 0.0, sg),
        },
        None => (None, 0.0, 0.0),
    };

    // 9. p-threshold — NB: None-p PASSES.
    if let Some(pv) = p {
        if pv < cfg.p_model_threshold {
            skip!("P-LOW ({:.3}<{:.2})", pv, cfg.p_model_threshold);
        }
    }

    // determine side now (BOTH → by delta sign; YES/NO need the matching direction)
    let side = match cfg.trade_side {
        TradeSide::Both => {
            if delta >= 0.0 {
                Side::Yes
            } else {
                Side::No
            }
        }
        TradeSide::Yes => {
            if delta >= cfg.delta_threshold {
                Side::Yes
            } else {
                skip!("SIDE (need YES move)");
            }
        }
        TradeSide::No => {
            if delta <= -cfg.delta_threshold {
                Side::No
            } else {
                skip!("SIDE (need NO move)");
            }
        }
    };

    // 10. entry sourcing — EXACTLY as the prod Kalshi paper (tasks.py:179 + paper_trading.py:419):
    //   orderbook_entry = max(yes_ask or 0, no_ask or 0)   (the expensive side, same for both)
    //   entry = last_trade_expensive (>0.5) else orderbook_entry; reject if <= 0.5.
    let orderbook_entry = s.yes_ask.unwrap_or(0.0).max(s.no_ask.unwrap_or(0.0));
    let mut entry = s.last_trade_expensive.unwrap_or(0.0);
    if entry <= 0.5 {
        entry = orderbook_entry;
    }
    if entry <= 0.5 {
        skip!("NO PRICE");
    }

    // 11. max_entry
    if entry > cfg.max_entry_price {
        skip!("HIGH ({:.2}>{:.2})", entry, cfg.max_entry_price);
    }

    // (12. refit gate — not used by f6)

    // 13. per-session liquidity
    if let Some(r) = session_liq(cfg, side, s) {
        skip!("{}", r);
    }

    EvalResult {
        reason: format!("BUY {}", side.as_str()),
        fire: Some(Fire {
            side,
            entry,
            conf: p,
            snr,
            p,
            sigma_used,
            orderbook_entry,
        }),
    }
}

/// `_regime_block` (§6.6): kill_hours then ofi_align.
fn regime_block(cfg: &SessionConfig, delta: f64, s: &Shared) -> Option<String> {
    if !cfg.kill_hours.is_empty() && cfg.kill_hours.contains(&s.hour) {
        return Some(format!("HOUR-BLOCK ({})", s.hour));
    }
    if cfg.ofi_align {
        let sgn = if delta > 0.0 {
            1.0
        } else if delta < 0.0 {
            -1.0
        } else {
            0.0
        };
        if s.ofi_1min * sgn < 0.0 {
            return Some(format!(
                "OFI-MISALIGN ({:+.0} vs {})",
                s.ofi_1min,
                if delta > 0.0 { "YES" } else { "NO" }
            ));
        }
    }
    None
}

/// `_session_liq` (§6.5): optional spread/depth gate + vol-imbalance kill.
fn session_liq(cfg: &SessionConfig, side: Side, s: &Shared) -> Option<String> {
    if cfg.liq_filter {
        if let Some(r) = check_liquidity(side, s) {
            return Some(r);
        }
    }
    let vk = cfg.vol_imb_kill;
    if vk > 0.0 {
        let tot = s.buy_vol_1min + s.sell_vol_1min;
        if tot > 0.0 {
            let imb = (s.buy_vol_1min - s.sell_vol_1min) / tot;
            match side {
                Side::Yes if imb < -vk => {
                    return Some(format!("VOLIMB {:+.2}<-{} vs YES", imb, vk))
                }
                Side::No if imb > vk => return Some(format!("VOLIMB {:+.2}>{} vs NO", imb, vk)),
                _ => {}
            }
        }
    }
    None
}

/// `_check_liquidity_paper` (§6.4) — FAIL-CLOSED. POLYMARKET-tuned thresholds.
fn check_liquidity(side: Side, s: &Shared) -> Option<String> {
    let (ask, bid, ask_vol, bid_vol) = match side {
        Side::Yes => (s.yes_ask, s.yes_bid, s.yes_ask_vol, s.yes_bid_vol),
        Side::No => (s.no_ask, s.no_bid, s.no_ask_vol, s.no_bid_vol),
    };
    let (ask, bid) = match (ask, bid) {
        (Some(a), Some(b)) => (a, b),
        _ => return Some("LIQ:no_quotes".to_string()),
    };
    let spread = ask - bid;
    if spread > LIQ_SPREAD_KILL {
        return Some(format!("LIQ:wide_spread {:.4}>{}", spread, LIQ_SPREAD_KILL));
    }
    if let (Some(av), Some(bv)) = (ask_vol, bid_vol) {
        let depth = av + bv;
        if depth <= LIQ_DEPTH_KILL {
            return Some(format!("LIQ:thin_book {:.0}<={:.0}", depth, LIQ_DEPTH_KILL));
        }
    }
    None
}
