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
    pub sigma_max30: f64,
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

/// Fetch the f6 live signal from the paper engine. `base` e.g. "http://127.0.0.1:8893".
/// Returns None on any error — the caller then SKIPS the tick rather than trade blind.
pub async fn fetch(
    client: &reqwest::Client,
    base: &str,
    now_utc: DateTime<Utc>,
) -> Option<MirrorSnap> {
    let st: Value = client
        .get(format!("{base}/api/state"))
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;
    let ss: Value = client
        .get(format!("{base}/api/sessions_state"))
        .timeout(Duration::from_secs(3))
        .send()
        .await
        .ok()?
        .json()
        .await
        .ok()?;

    // f6's σ is the per-session max30 (top-level state['sigma'] is a different type).
    let sigma = ss.get("f6_wait270")?.get("live_sigma")?.as_f64()?;
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
