//! Signal math — a VERBATIM port of `dash_src/helpers.py` §5 of PAPER_ENGINE_SPEC.md.
//!
//! Everything here mirrors the Python engine bit-for-bit so the Rust shadow runner
//! produces the SAME signal values as the live paper engine (the whole point of the
//! paper-vs-live comparison). Where Python uses `float`, we use `f64`. `_phi` uses
//! `libm::erf`, which is the same libm routine CPython's `math.erf` calls.

use std::collections::HashMap;

/// `SIGMA_STATIC` / `SIGMA_DEFAULT` — two literals with the same value in the source.
pub const SIGMA_STATIC: f64 = 0.0005;

/// Standard-normal CDF, `_phi` in helpers.py / paper_trading.py.
/// `0.5 * (1 + erf(x / sqrt(2)))` — exact libm erf, NOT an approximation.
#[inline]
pub fn phi(x: f64) -> f64 {
    0.5 * (1.0 + libm::erf(x / std::f64::consts::SQRT_2))
}

/// Population std (÷ N, not N-1). Two-pass, matching `_std` exactly for numerical parity.
fn std_pop(values: &[f64]) -> f64 {
    if values.len() < 2 {
        return 0.0;
    }
    let n = values.len() as f64;
    let mean = values.iter().sum::<f64>() / n;
    let var = values.iter().map(|v| (v - mean) * (v - mean)).sum::<f64>() / n;
    if var > 0.0 {
        var.sqrt()
    } else {
        0.0
    }
}

/// Consecutive fractional returns, `_returns`.
fn returns(prices: &[f64]) -> Vec<f64> {
    let mut ret = Vec::with_capacity(prices.len().saturating_sub(1));
    for i in 1..prices.len() {
        if prices[i - 1] > 0.0 {
            ret.push((prices[i] - prices[i - 1]) / prices[i - 1]);
        }
    }
    ret
}

/// Actual span in minutes (floored at 0.01), `_duration_min`.
fn duration_min(ts: &[f64]) -> f64 {
    if ts.len() < 2 {
        return 1.0;
    }
    let d = (ts[ts.len() - 1] - ts[0]) / 60.0;
    d.max(0.01)
}

/// Median of a slice by the Python "middle element" rule: `sorted[len/2]`
/// (biased high on even N — replicated intentionally).
fn middle_sorted(mut v: Vec<f64>) -> f64 {
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    v[v.len() / 2]
}

/// Compute every sigma variant over win ∈ {5, 10, 30} minutes from the price history.
///
/// `history` is `(ts_seconds, price)` in append order (oldest→newest), the engine's
/// `_price_history` trimmed to the last 2100 s. Returns the `sigmas{}` dict.
///
/// No-ops (returns just `{"static": SIGMA_STATIC}`) if `history.len() < 20`, matching
/// "Sigma computation no-ops entirely if len(_price_history) < 20".
pub fn compute_all_sigmas(history: &[(f64, f64)], now: f64) -> HashMap<String, f64> {
    let mut result: HashMap<String, f64> = HashMap::new();
    result.insert("static".to_string(), SIGMA_STATIC);
    if history.len() < 20 {
        return result;
    }

    for win in [5usize, 10, 30] {
        let cutoff = now - (win as f64) * 60.0;
        // pts within the window, preserving order (history is time-ordered).
        let pts: Vec<(f64, f64)> = history
            .iter()
            .copied()
            .filter(|(t, _)| *t >= cutoff)
            .collect();
        if pts.len() < 10 {
            continue;
        }
        let prices: Vec<f64> = pts.iter().map(|(_, p)| *p).collect();
        let ts: Vec<f64> = pts.iter().map(|(t, _)| *t).collect();
        let avg = prices.iter().sum::<f64>() / prices.len() as f64;
        let dur = duration_min(&ts);

        // trail{win}: coefficient of variation
        let trail = if avg != 0.0 { std_pop(&prices) / avg } else { 0.0 };
        result.insert(format!("trail{win}"), trail);
        result.insert(format!("trail{win}_pmin"), trail / dur);

        // realized{win}: std of returns (+ median/mad, which depend on `realized`)
        let rets = returns(&prices);
        if rets.len() >= 3 {
            let realized = std_pop(&rets);
            result.insert(format!("realized{win}"), realized);
            result.insert(format!("realized{win}_pmin"), realized / dur);

            // median stores the median RETURN; mad the median abs deviation.
            let med = middle_sorted(rets.clone());
            let mad_vals: Vec<f64> = rets.iter().map(|r| (r - med).abs()).collect();
            let mad = middle_sorted(mad_vals);
            let med_or = if med > 0.0 { med } else { realized };
            result.insert(format!("median{win}"), med_or);
            result.insert(format!("median{win}_pmin"), med_or / dur);
            result.insert(format!("mad{win}"), if mad > 0.0 { mad } else { realized });
        }

        // range{win}: (high-low)/mean
        let hi = prices.iter().cloned().fold(f64::MIN, f64::max);
        let lo = prices.iter().cloned().fold(f64::MAX, f64::min);
        let rng = if avg > 0.0 { (hi - lo) / avg } else { 0.0 };
        result.insert(format!("range{win}"), rng);
        result.insert(format!("range{win}_pmin"), rng / dur);

        // max{win}: MAX over the window of trailing-60s std/mean ("max30" — the third sigma).
        // Two-pointer over time-ordered pts gives the exact same chunk {t: t0-60<=t<=t0}.
        let mut max_std = 0.0f64;
        let mut left = 0usize;
        for i in 0..pts.len() {
            let t0 = ts[i];
            while ts[left] < t0 - 60.0 {
                left += 1;
            }
            let chunk = &prices[left..=i];
            if chunk.len() >= 3 {
                let cavg = chunk.iter().sum::<f64>() / chunk.len() as f64;
                if cavg > 0.0 {
                    let s = std_pop(chunk) / cavg;
                    if s > max_std {
                        max_std = s;
                    }
                }
            }
        }
        result.insert(format!("max{win}"), max_std);
        result.insert(format!("max{win}_pmin"), max_std); // NO /dur — already per-minute
    }

    result
}

/// `get_sigma_for_type` — lookup with the realized5_pmin fallback.
pub fn get_sigma_for_type(sigmas: &HashMap<String, f64>, sigma_type: &str) -> Option<f64> {
    if sigma_type == "static" {
        return Some(SIGMA_STATIC);
    }
    if let Some(&v) = sigmas.get(sigma_type) {
        if v > 0.0 {
            return Some(v);
        }
    }
    sigmas
        .get("realized5_pmin")
        .or_else(|| sigmas.get("realized5"))
        .copied()
}

/// Tau mode for the classic p-model.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TauMode {
    Linear,
    Sqrt,
    None,
}

/// Classic P-model exactly as `_evaluate_signal` computes it (paper_trading.py §5.4):
///   sigma_usd = sigma * price_ref
///   f_tau     = max(tau,0.1) [linear] | max(sqrt(tau),0.1) [sqrt] | 1.0 [else]
///   snr       = |delta| / (sigma_usd * f_tau)
///   p         = phi(kappa * snr)
///
/// NB: delta source is `delta_from_open`; f_tau is floored at 0.1 via `max(tau,0.1)`.
/// Returns `(p, snr)` or `None` when sigma/price invalid (caller treats None-p as PASS).
pub fn p_model_classic(
    delta_from_open: f64,
    sigma: f64,
    tau: f64,
    price_ref: f64,
    kappa: f64,
    tau_mode: TauMode,
) -> Option<(f64, f64)> {
    if !(sigma > 0.0) || !(price_ref > 0.0) {
        return None;
    }
    let sigma_usd = sigma * price_ref;
    let f_tau = match tau_mode {
        TauMode::Linear => tau.max(0.1),
        TauMode::Sqrt => tau.sqrt().max(0.1),
        TauMode::None => 1.0,
    };
    let snr = delta_from_open.abs() / (sigma_usd * f_tau);
    let p = phi(kappa * snr);
    Some((p, snr))
}

/// OFI over the trade buffer, `compute_ofi`. `buffer` is `(ts, notional, is_sell)`.
/// Returns `(ofi_1min, ofi_3min, ofi_5min, buy_vol_1min, sell_vol_1min, trade_count_1min)`.
/// (Pruning of entries older than 300 s is the caller's responsibility on the live buffer.)
pub fn compute_ofi(buffer: &[(f64, f64, bool)], now: f64) -> (f64, f64, f64, f64, f64, u64) {
    let (mut ofi_1, mut ofi_3, mut ofi_5) = (0.0, 0.0, 0.0);
    let (mut buy_1, mut sell_1) = (0.0, 0.0);
    let mut count_1: u64 = 0;
    for &(ts, notional, is_sell) in buffer {
        let age = now - ts;
        let val = if is_sell { -notional } else { notional };
        if age <= 60.0 {
            ofi_1 += val;
            count_1 += 1;
            if is_sell {
                sell_1 += notional;
            } else {
                buy_1 += notional;
            }
        }
        if age <= 180.0 {
            ofi_3 += val;
        }
        ofi_5 += val;
    }
    (
        ofi_1.round(),
        ofi_3.round(),
        ofi_5.round(),
        buy_1.round(),
        sell_1.round(),
        count_1,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn phi_matches_known_values() {
        assert!((phi(0.0) - 0.5).abs() < 1e-12);
        assert!((phi(1.0) - 0.8413447460685429).abs() < 1e-12);
        assert!((phi(-1.0) - 0.15865525393145707).abs() < 1e-12);
    }

    #[test]
    fn sigma_noop_under_20_points() {
        let hist: Vec<(f64, f64)> = (0..10).map(|i| (i as f64, 63000.0)).collect();
        let s = compute_all_sigmas(&hist, 10.0);
        assert_eq!(s.len(), 1);
        assert_eq!(s["static"], SIGMA_STATIC);
    }

    #[test]
    fn p_model_none_when_sigma_zero() {
        assert!(p_model_classic(50.0, 0.0, 2.0, 63000.0, 0.5, TauMode::Linear).is_none());
    }
}
