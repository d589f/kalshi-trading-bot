//! MIRROR mode — execute the prod Kalshi paper engine's EXACT signal (1:1).
//!
//! The live executor's divergence from paper came from computing an INDEPENDENT
//! signal (its own window_open / Δ / σ off a separate Binance feed): on near-zero-Δ
//! choppy windows the two processes sample different instants → opposite sides
//! (the 13:00 window that cost ~77% of one day's gap). Here we instead read the
//! paper's live state (port 8893, localhost on the EU box) and feed its EXACT
//! window_open / Δ / σ / book into our already-validated f6 filter, so the side +
//! entry are byte-identical to the paper. Only the real fill (slippage/fee) differs.

use std::time::Duration;

use chrono::{DateTime, Utc};
use serde_json::Value;

/// A snapshot of the paper's live signal — enough to rebuild `shared`/`win`/`book`.
#[derive(Clone, Debug)]
pub struct MirrorSnap {
    pub window_start: DateTime<Utc>,
    pub window_end: DateTime<Utc>,
    pub window_open: f64,
    pub binance_price: f64,
    pub delta_from_open: f64,
    pub delta_from_prev: f64,
    /// The selected session's `live_sigma` (f6 → max30, f1 → max10). Field NAME is kept
    /// for wire/struct compatibility — it is NOT always a max30 value.
    pub sigma_max30: f64,
    /// The reference-shadow session's `live_sigma` (f6's max30) when a shadow session
    /// key was requested and the paper exposes a sane value. None never blocks the
    /// tick — it only disables the f6 shadow evaluation for that tick.
    pub sigma_shadow: Option<f64>,
    pub ticker: String,
    pub yes_bid: Option<f64>,
    pub yes_ask: Option<f64>,
    pub yes_bid_vol: Option<f64>,
    pub yes_ask_vol: Option<f64>,
    pub no_bid: Option<f64>,
    pub no_ask: Option<f64>,
    pub no_bid_vol: Option<f64>,
    pub no_ask_vol: Option<f64>,
    pub last_trade_expensive: Option<f64>,
    /// seconds since the paper last refreshed this state (freshness guard)
    pub age_secs: f64,
}

fn parse_ts(v: Option<&Value>) -> Option<DateTime<Utc>> {
    let s = v?.as_str()?;
    DateTime::parse_from_rfc3339(s)
        .ok()
        .map(|d| d.with_timezone(&Utc))
}

/// σ cache for the fast mirror tick: /api/sessions_state carries EVERY session with
/// history (~30KB of Python serialization) — polling it at 10Hz would load the paper
/// engine, the signal source for everything. σ is a 10/30-min window statistic: a
/// sub-second-stale value is identical for the gate. Refresh every SIGMA_REFRESH_SECS;
/// a value older than SIGMA_MAX_AGE_SECS fails CLOSED (tick skipped), preserving the
/// no-blind-trades guarantee even if the sessions endpoint dies while /api/state lives.
#[derive(Default)]
pub struct SigmaCache {
    pub primary: Option<f64>,
    pub shadow: Option<f64>,
    pub at: Option<DateTime<Utc>>,
}

pub const SIGMA_REFRESH_SECS: f64 = 0.3;
pub const SIGMA_MAX_AGE_SECS: f64 = 3.0;

/// Age of the cache in seconds (infinity when never filled).
pub fn cache_age_secs(at: Option<DateTime<Utc>>, now: DateTime<Utc>) -> f64 {
    at.map(|t| (now - t).num_milliseconds() as f64 / 1000.0)
        .unwrap_or(f64::INFINITY)
}
/// Is a refresh due? (>= so the cache refreshes on the paper's own update cadence)
pub fn cache_due(age: f64) -> bool {
    age >= SIGMA_REFRESH_SECS
}
/// FAIL-CLOSED read: a too-stale cache yields None regardless of the stored value.
pub fn sigma_from_cache(v: Option<f64>, age: f64) -> Option<f64> {
    if age <= SIGMA_MAX_AGE_SECS {
        v
    } else {
        None
    }
}

/// Extract the mirrored session's `live_sigma` — FAIL-CLOSED. Returns Some only when
/// the session key exists, the field is numeric, finite and strictly > 0.0. A real
/// 0/negative sigma must NOT reach the gate: engine.rs's `.or(s.sigma)` fallback would
/// silently swap in the LOCAL realized5 sigma — a non-F1 formula on real money.
/// (Rejecting here keeps engine.rs untouched, per the architecture review.)
pub fn extract_live_sigma(sessions_state: &Value, session_key: &str) -> Option<f64> {
    let v = sessions_state.get(session_key)?.get("live_sigma")?.as_f64()?;
    // Sanity band (security audit): real max10/max30 values live around 1e-5..1e-2.
    // A corrupt-but-positive tiny σ would push p→1 and defeat the confidence gate
    // entirely (snr→∞), so wildly out-of-band values fail closed too.
    if v.is_finite() && (1e-7..=1e-1).contains(&v) {
        Some(v)
    } else {
        None
    }
}

/// Fetch the selected session's live signal from the paper engine.
/// `base` e.g. "http://127.0.0.1:8893"; `session_key` e.g. "f6_wait270" / "f1_d50cap75".
/// `shadow_session_key` optionally names the reference-shadow session (f6) whose sigma
/// rides along for the dashboard's shadow line — its absence never fails the fetch.
/// Returns None on any error — the caller then SKIPS the tick rather than trade blind.
pub async fn fetch(
    client: &reqwest::Client,
    base: &str,
    now_utc: DateTime<Utc>,
    session_key: &str,
    shadow_session_key: Option<&str>,
    cache: &mut SigmaCache,
) -> Option<MirrorSnap> {
    // Light state poll — every tick (this is what carries Δ/book/window, the fast data).
    let st: Value = client
        .get(format!("{base}/api/state"))
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;
    // Heavy sessions poll — only when the σ cache is due; on failure the old cache
    // survives until SIGMA_MAX_AGE_SECS, then the tick fails closed below.
    if cache_due(cache_age_secs(cache.at, now_utc)) {
        if let Ok(resp) = client
            .get(format!("{base}/api/sessions_state"))
            .timeout(Duration::from_secs(3))
            .send()
            .await
        {
            if let Ok(ss) = resp.json::<Value>().await {
                cache.primary = extract_live_sigma(&ss, session_key);
                cache.shadow = shadow_session_key.and_then(|k| extract_live_sigma(&ss, k));
                cache.at = Some(now_utc);
            }
        }
    }
    let age = cache_age_secs(cache.at, now_utc);
    // The σ is the SELECTED session's per-session value: f6 → max30, f1 → max10.
    // Fail-closed on missing/non-positive/too-stale.
    let sigma = sigma_from_cache(cache.primary, age)?;
    let sigma_shadow = sigma_from_cache(cache.shadow, age);
    let num = |k: &str| st.get(k).and_then(Value::as_f64);

    let last_update = parse_ts(st.get("last_update"))?;
    let age_secs = (now_utc - last_update).num_milliseconds() as f64 / 1000.0;

    Some(MirrorSnap {
        window_start: parse_ts(st.get("window_start_utc"))?,
        window_end: parse_ts(st.get("window_end_utc"))?,
        window_open: num("window_open_price")?,
        binance_price: num("binance_price")?,
        delta_from_open: num("delta_from_open").unwrap_or(0.0),
        delta_from_prev: num("delta_from_prev").unwrap_or(0.0),
        sigma_max30: sigma,
        sigma_shadow,
        ticker: st.get("market_slug")?.as_str()?.to_string(),
        yes_bid: num("poly_yes_bid"),
        yes_ask: num("poly_yes_ask"),
        yes_bid_vol: num("poly_yes_bid_vol"),
        yes_ask_vol: num("poly_yes_ask_vol"),
        no_bid: num("poly_no_bid"),
        no_ask: num("poly_no_ask"),
        no_bid_vol: num("poly_no_bid_vol"),
        no_ask_vol: num("poly_no_ask_vol"),
        last_trade_expensive: num("poly_last_trade_expensive"),
        age_secs,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ss() -> Value {
        json!({
            "f6_wait270":  {"live_sigma": 0.00056},
            "f1_d50cap75": {"live_sigma": 0.00031}
        })
    }

    // TC-3.1: happy path — the requested session's own sigma, no cross-talk.
    #[test]
    fn extract_happy_path_per_session() {
        assert_eq!(extract_live_sigma(&ss(), "f1_d50cap75"), Some(0.00031));
        assert_eq!(extract_live_sigma(&ss(), "f6_wait270"), Some(0.00056));
    }

    // TC-11.1: absent session key -> None, and NO fallback to another session that
    // IS present in the payload (trading on the wrong strategy's sigma is fail-open).
    #[test]
    fn extract_missing_key_no_fallback() {
        let s = json!({"f6_wait270": {"live_sigma": 0.00056}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
    }

    // TC-3.5: session present, live_sigma field absent -> None.
    #[test]
    fn extract_missing_field() {
        let s = json!({"f1_d50cap75": {"balance": 10000.0}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
    }

    // TC-3.6 / TC-14.1: null / string-typed sigma -> None.
    #[test]
    fn extract_non_numeric() {
        let s = json!({"f1_d50cap75": {"live_sigma": null}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
        let s = json!({"f1_d50cap75": {"live_sigma": "0.0003"}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
    }

    // TC-14.2 / TC-14.5: FAIL-CLOSED on non-positive — exactly 0.0 and negative both
    // rejected so the engine's .or(s.sigma) local fallback can never be reached.
    #[test]
    fn extract_non_positive_fail_closed() {
        let s = json!({"f1_d50cap75": {"live_sigma": 0.0}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
        let s = json!({"f1_d50cap75": {"live_sigma": -3.1}});
        assert_eq!(extract_live_sigma(&s, "f1_d50cap75"), None);
    }

    // Sanity band: realistic σ passes; out-of-band positive values (corrupt feed —
    // a tiny σ would defeat the p-gate with p→1) fail closed on both edges.
    #[test]
    fn extract_sanity_band() {
        let ok = |v: f64| json!({"f1_d50cap75": {"live_sigma": v}});
        assert_eq!(extract_live_sigma(&ok(1e-5), "f1_d50cap75"), Some(1e-5));
        assert_eq!(extract_live_sigma(&ok(1e-7), "f1_d50cap75"), Some(1e-7)); // band edges inclusive
        assert_eq!(extract_live_sigma(&ok(1e-1), "f1_d50cap75"), Some(1e-1));
        assert_eq!(extract_live_sigma(&ok(1e-9), "f1_d50cap75"), None); // p→1 exploit
        assert_eq!(extract_live_sigma(&ok(1e300), "f1_d50cap75"), None);
        assert_eq!(extract_live_sigma(&ok(0.5), "f1_d50cap75"), None);
    }
    // Fast-tick σ cache: refresh due at the paper's own cadence; stale reads fail CLOSED.
    #[test]
    fn sigma_cache_bounds() {
        assert!(cache_due(f64::INFINITY)); // never filled -> refresh
        assert!(cache_due(0.3));
        assert!(!cache_due(0.1));
        assert_eq!(sigma_from_cache(Some(0.0003), 0.1), Some(0.0003));
        assert_eq!(sigma_from_cache(Some(0.0003), 2.9), Some(0.0003));
        assert_eq!(sigma_from_cache(Some(0.0003), 3.1), None); // too stale -> skip tick
        assert_eq!(sigma_from_cache(None, 0.0), None);
        let now = Utc::now();
        assert_eq!(cache_age_secs(None, now), f64::INFINITY);
        assert!(cache_age_secs(Some(now - chrono::Duration::milliseconds(500)), now) - 0.5 < 0.01);
    }
}

