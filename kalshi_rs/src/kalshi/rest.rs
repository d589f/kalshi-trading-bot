//! Kalshi Trade API v2 REST client — market discovery, orderbook, settlement result.
//! Market-data endpoints are PUBLIC (no auth needed); confirmed live HTTP 200.

use anyhow::{Context, Result};
use serde::Deserialize;

pub const PROD_BASE: &str = "https://api.elections.kalshi.com/trade-api/v2";
pub const DEMO_BASE: &str = "https://demo-api.kalshi.co/trade-api/v2";
pub const SERIES_KXBTC15M: &str = "KXBTC15M";

/// A Kalshi market (only the fields we use). Price fields are FixedPointDollars STRINGS.
#[derive(Debug, Clone, Deserialize)]
pub struct Market {
    pub ticker: String,
    #[serde(default)]
    pub event_ticker: String,
    #[serde(default)]
    pub open_time: String,
    #[serde(default)]
    pub close_time: String,
    #[serde(default)]
    pub status: String,
    /// "" until settled, then "yes" | "no".
    #[serde(default)]
    pub result: String,
    #[serde(default)]
    pub floor_strike: Option<f64>,
    #[serde(default)]
    pub last_price_dollars: Option<String>,
}

impl Market {
    pub fn last_price(&self) -> Option<f64> {
        self.last_price_dollars
            .as_deref()
            .and_then(|s| s.parse::<f64>().ok())
    }
}

#[derive(Debug, Deserialize)]
struct MarketsResp {
    #[serde(default)]
    markets: Vec<Market>,
    #[serde(default)]
    cursor: String,
}

#[derive(Debug, Deserialize)]
struct MarketResp {
    market: Market,
}

#[derive(Debug, Deserialize)]
struct OrderbookResp {
    #[serde(default)]
    orderbook_fp: Option<OrderbookFp>,
}

#[derive(Debug, Deserialize, Default)]
struct OrderbookFp {
    /// resting YES bids: [price_dollars, count] pairs (strings)
    #[serde(default)]
    yes_dollars: Vec<[String; 2]>,
    /// resting NO bids
    #[serde(default)]
    no_dollars: Vec<[String; 2]>,
}

/// One price level (price in [0,1] dollars, count in contracts).
#[derive(Debug, Clone, Copy)]
pub struct Level {
    pub price: f64,
    pub count: f64,
}

/// Raw two-ladder book (bids only, both sides), as Kalshi returns it.
#[derive(Debug, Clone, Default)]
pub struct RawBook {
    pub yes_bids: Vec<Level>,
    pub no_bids: Vec<Level>,
}

fn parse_levels(raw: &[[String; 2]]) -> Vec<Level> {
    raw.iter()
        .filter_map(|p| {
            let price = p[0].parse::<f64>().ok()?;
            let count = p[1].parse::<f64>().ok()?;
            if count > 0.0 {
                Some(Level { price, count })
            } else {
                None
            }
        })
        .collect()
}

pub struct KalshiRest {
    base: String,
    http: reqwest::Client,
}

impl KalshiRest {
    pub fn new(base: impl Into<String>) -> Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent("kalshi_bot/0.1")
            .timeout(std::time::Duration::from_secs(8))
            .build()?;
        Ok(Self {
            base: base.into(),
            http,
        })
    }

    /// GET /markets?series_ticker=...&status=...
    pub async fn get_markets(&self, series: &str, status: &str) -> Result<Vec<Market>> {
        let url = format!("{}/markets", self.base);
        let resp = self
            .http
            .get(&url)
            .query(&[
                ("series_ticker", series),
                ("status", status),
                ("limit", "100"),
            ])
            .send()
            .await
            .context("GET /markets")?
            .error_for_status()?
            .json::<MarketsResp>()
            .await?;
        let _ = resp.cursor;
        Ok(resp.markets)
    }

    /// GET /markets/{ticker}
    pub async fn get_market(&self, ticker: &str) -> Result<Market> {
        let url = format!("{}/markets/{}", self.base, ticker);
        let resp = self
            .http
            .get(&url)
            .send()
            .await
            .context("GET /markets/{ticker}")?
            .error_for_status()?
            .json::<MarketResp>()
            .await?;
        Ok(resp.market)
    }

    /// GET /markets/{ticker}/orderbook?depth=N  → bids-only two ladders.
    pub async fn get_orderbook(&self, ticker: &str, depth: u32) -> Result<RawBook> {
        let url = format!("{}/markets/{}/orderbook", self.base, ticker);
        let resp = self
            .http
            .get(&url)
            .query(&[("depth", depth.to_string())])
            .send()
            .await
            .context("GET orderbook")?
            .error_for_status()?
            .json::<OrderbookResp>()
            .await?;
        let ob = resp.orderbook_fp.unwrap_or_default();
        Ok(RawBook {
            yes_bids: parse_levels(&ob.yes_dollars),
            no_bids: parse_levels(&ob.no_dollars),
        })
    }
}
