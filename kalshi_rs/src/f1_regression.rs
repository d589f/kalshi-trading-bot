//! Slice 8 regression guard for live-f1-strategy: F1 gate behavior, F1-vs-f6
//! divergence, boundary semantics, and the f6 byte-identity guarantee with SESSION
//! unset. engine.rs is UNCHANGED by the feature — these tests drive the existing
//! gate with the two configs. (Lives in src/ because the crate is a binary — an
//! external tests/ target cannot import its modules; the module is declared
//! `#[cfg(test)]` in main.rs.)

use crate::config::{
    resolve_mirror_session_key, SessionConfig, SessionSel,
};
use crate::engine::{evaluate, Shared, Side};
use crate::{display_sigma, insert_mirror_sigma, select_session};

/// Shared with BOTH sigma keys present (as the mirror would populate for the selected
/// strategy) so one fixture can drive f6 and F1 side by side.
fn shared(delta: f64, elapsed_min: f64, sigma: f64, ask: f64) -> Shared {
    let mut s = Shared {
        binance_price: Some(100_000.0),
        delta_from_open: Some(delta),
        delta_from_prev: Some(delta / 2.0), // same sign — dual-confirmation passes
        tau: 2.0,
        elapsed_min,
        yes_ask: Some(ask),
        no_ask: Some(0.10),
        ..Default::default()
    };
    s.sigmas.insert("max10".to_string(), sigma);
    s.sigmas.insert("max30".to_string(), sigma);
    s
}

// TC-5.1: the F1 gate fires when ALL F1 conditions hold (Δ≥50 @180s+, p≥0.65,
// entry ≤0.92) — and computes p off max10.
#[test]
fn f1_gate_fires_on_full_conditions() {
    let cfg = SessionConfig::f1_d50cap75();
    // snr = 60/(0.0002*100000*2) = 1.5 → p = Φ(0.4*1.5) ≈ 0.726 ≥ 0.65
    let r = evaluate(&cfg, &shared(60.0, 3.5, 0.0002, 0.85));
    assert_eq!(r.reason, "BUY YES");
    let f = r.fire.expect("must fire");
    assert_eq!(f.side, Side::Yes);
    assert_eq!(f.entry, 0.85);
    assert_eq!(f.sigma_used, 0.0002);
}

// TC-9.1/9.2: divergence by delta — Δ=30 fires f6 (Δ≥20, p≈0.65≥0.60) but F1 skips
// (DELTA LOW): proof the two configs gate differently on the same signal.
#[test]
fn divergence_delta_30_fires_f6_not_f1() {
    let s = shared(30.0, 5.0, 0.0002, 0.85);
    let f6 = evaluate(&SessionConfig::f6_wait270(), &s);
    assert_eq!(f6.reason, "BUY YES");
    let f1 = evaluate(&SessionConfig::f1_d50cap75(), &s);
    assert!(f1.reason.starts_with("DELTA LOW"), "{}", f1.reason);
    assert!(f1.fire.is_none());
}

// TC-9.3: divergence by timing — at 210s (3.5 min) F1 already trades, f6 still WAITs.
#[test]
fn divergence_elapsed_210s_fires_f1_not_f6() {
    let s = shared(60.0, 3.5, 0.0002, 0.85);
    assert_eq!(evaluate(&SessionConfig::f1_d50cap75(), &s).reason, "BUY YES");
    let f6 = evaluate(&SessionConfig::f6_wait270(), &s);
    assert!(f6.reason.starts_with("WAIT"), "{}", f6.reason);
}

// TC-17.1: entry_wait boundary — strict `<` means exactly 180.0s (3.0 min) fires.
#[test]
fn boundary_entry_wait_180s_inclusive() {
    let cfg = SessionConfig::f1_d50cap75();
    let r = evaluate(&cfg, &shared(60.0, 2.99, 0.0002, 0.85));
    assert!(r.reason.starts_with("WAIT"), "{}", r.reason);
    assert_eq!(evaluate(&cfg, &shared(60.0, 3.0, 0.0002, 0.85)).reason, "BUY YES");
}

// TC-17.2: delta boundary — |Δ| < 50 skips, exactly 50.0 passes.
#[test]
fn boundary_delta_50_inclusive() {
    let cfg = SessionConfig::f1_d50cap75();
    let r = evaluate(&cfg, &shared(49.99, 3.5, 0.0002, 0.85));
    assert!(r.reason.starts_with("DELTA LOW"), "{}", r.reason);
    // snr = 50/40 = 1.25 → p = Φ(0.5) ≈ 0.691 ≥ 0.65
    assert_eq!(evaluate(&cfg, &shared(50.0, 3.5, 0.0002, 0.85)).reason, "BUY YES");
    // NO side symmetric
    assert_eq!(evaluate(&cfg, &shared(-50.0, 3.5, 0.0002, 0.85)).reason, "BUY NO");
}

// TC-17.3: p-threshold — p below 0.65 skips P-LOW; higher sigma = lower p.
#[test]
fn boundary_p_threshold() {
    let cfg = SessionConfig::f1_d50cap75();
    // snr = 60/(0.00034*100000*2) ≈ 0.882 → p = Φ(0.353) ≈ 0.638 < 0.65
    let r = evaluate(&cfg, &shared(60.0, 3.5, 0.00034, 0.85));
    assert!(r.reason.starts_with("P-LOW"), "{}", r.reason);
}

// TC-17.4/17.5: entry band — 0.92 inclusive fires, above rejects HIGH, ≤0.50 has
// no tradable price.
#[test]
fn boundary_max_entry_band() {
    let cfg = SessionConfig::f1_d50cap75();
    assert_eq!(evaluate(&cfg, &shared(60.0, 3.5, 0.0002, 0.92)).reason, "BUY YES");
    let r = evaluate(&cfg, &shared(60.0, 3.5, 0.0002, 0.925));
    assert!(r.reason.starts_with("HIGH"), "{}", r.reason);
    let r = evaluate(&cfg, &shared(60.0, 3.5, 0.0002, 0.50));
    assert_eq!(r.reason, "NO PRICE");
}

// TC-16.x: boot mid-window later than 180s — F1 may still fire (same semantics f6
// always had at 270s); the gate has no upper elapsed bound, tau handles decay.
#[test]
fn mid_window_late_entry_allowed() {
    let cfg = SessionConfig::f1_d50cap75();
    assert_eq!(evaluate(&cfg, &shared(60.0, 10.0, 0.0002, 0.85)).reason, "BUY YES");
}

// TC-2.3/2.7 (byte-identity guard): with SESSION unset every f6 observable is
// exactly today's behavior — config, sigma insert key, mirror key, tag, coid.
#[test]
fn f6_default_byte_identity() {
    let (sel, warn) = select_session(None);
    assert_eq!(sel, SessionSel::F6);
    assert!(warn.is_none());
    let cfg = sel.config();
    let base = SessionConfig::f6_wait270();
    assert_eq!(cfg.kappa, base.kappa);
    assert_eq!(cfg.delta_threshold, base.delta_threshold);
    assert_eq!(cfg.p_model_threshold, base.p_model_threshold);
    assert_eq!(cfg.sigma_type, base.sigma_type);
    assert_eq!(cfg.max_entry_price, base.max_entry_price);
    assert_eq!(cfg.entry_wait_min, base.entry_wait_min);
    let mut m = std::collections::HashMap::new();
    insert_mirror_sigma(&mut m, &cfg, 0.0005);
    assert_eq!(m.get("max30"), Some(&0.0005)); // legacy insert key
    assert_eq!(resolve_mirror_session_key(&sel, None), "f6_wait270");
    assert_eq!(sel.session_tag(), "f6_wait270_shadow");
    assert_eq!(sel.coid_prefix(), "f6-");
    assert_eq!(display_sigma(&m, &cfg), 0.0005);
}
