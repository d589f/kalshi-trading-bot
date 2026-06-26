//! Kalshi venue: REST client, request signing, and the derived top-of-book.

pub mod auth;
pub mod rest;

use rest::{Level, Market, RawBook};

/// Top-of-book derived from Kalshi's bids-only two-ladder book.
/// Kalshi returns only resting YES bids and NO bids, so the ask on each side is
/// synthesized: `yes_ask = 1 - best_no_bid`, `no_ask = 1 - best_yes_bid`.
#[derive(Debug, Clone, Default)]
pub struct KalshiBook {
    pub ticker: String,
    pub yes_bid: Option<f64>,
    pub yes_bid_vol: Option<f64>,
    pub yes_ask: Option<f64>,
    pub yes_ask_vol: Option<f64>,
    pub no_bid: Option<f64>,
    pub no_bid_vol: Option<f64>,
    pub no_ask: Option<f64>,
    pub no_ask_vol: Option<f64>,
    pub last_price: Option<f64>,
    /// the window pivot (BRTI 60s-avg at open); from the market object
    pub floor_strike: Option<f64>,
    /// wall-clock seconds when this snapshot was taken
    pub ts: f64,
}

/// Kalshi trading fee in USD: `ceil(0.07 · C · P · (1−P))` to the next cent.
/// C = contracts, P = price in dollars. Peaks ~1.75c/contract at P=0.50.
pub fn kalshi_fee_usd(contracts: f64, price: f64) -> f64 {
    if price <= 0.0 || price >= 1.0 || contracts <= 0.0 {
        return 0.0;
    }
    let cents = 0.07 * contracts * price * (1.0 - price) * 100.0;
    cents.ceil() / 100.0
}

fn best(levels: &[Level]) -> Option<Level> {
    levels
        .iter()
        .copied()
        .max_by(|a, b| a.price.partial_cmp(&b.price).unwrap_or(std::cmp::Ordering::Equal))
}

impl KalshiBook {
    pub fn derive(ticker: &str, raw: &RawBook, last_price: Option<f64>, now: f64) -> Self {
        let by = best(&raw.yes_bids);
        let bn = best(&raw.no_bids);
        KalshiBook {
            ticker: ticker.to_string(),
            yes_bid: by.map(|l| l.price),
            yes_bid_vol: by.map(|l| l.count),
            // to BUY yes you lift the best no-bid: yes_ask = 1 - best_no_bid
            yes_ask: bn.map(|l| round2(1.0 - l.price)),
            yes_ask_vol: bn.map(|l| l.count),
            no_bid: bn.map(|l| l.price),
            no_bid_vol: bn.map(|l| l.count),
            no_ask: by.map(|l| round2(1.0 - l.price)),
            no_ask_vol: by.map(|l| l.count),
            last_price,
            floor_strike: None,
            ts: now,
        }
    }
}

#[inline]
fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}

/// Pick the KXBTC15M market for a given window-end (RFC3339 close_time match).
pub fn pick_market_for_close<'a>(markets: &'a [Market], close_time_rfc3339: &str) -> Option<&'a Market> {
    markets.iter().find(|m| m.close_time == close_time_rfc3339)
}
