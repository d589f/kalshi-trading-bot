//! Kalshi V2 order client — signed `POST /trade-api/v2/portfolio/events/orders`.
//! V2 schema (live-verified): side="bid"|"ask" (YES-perspective: bid=buy YES, ask=sell YES=buy NO),
//! count & price are STRING fixed-point, price in DOLLARS, time_in_force, self_trade_prevention_type.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use tracing::warn;

use crate::kalshi::auth::Signer;

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

#[derive(Serialize)]
struct V2Order<'a> {
    ticker: &'a str,
    side: &'a str, // "bid" | "ask"
    count: String, // "7.00"
    price: String, // "0.70" (dollars)
    time_in_force: &'a str,
    self_trade_prevention_type: &'a str,
    client_order_id: String,
}

#[derive(Deserialize, Debug, Default, Clone)]
pub struct OrderResp {
    #[serde(default)]
    pub order_id: String,
    #[serde(default)]
    pub fill_count: String,
    #[serde(default)]
    pub remaining_count: String,
    #[serde(default)]
    pub average_fill_price: Option<String>,
    #[serde(default)]
    pub average_fee_paid: Option<String>,
}

impl OrderResp {
    pub fn fill(&self) -> f64 {
        self.fill_count.parse().unwrap_or(0.0)
    }
    pub fn avg_price(&self) -> Option<f64> {
        self.average_fill_price.as_ref().and_then(|s| s.parse().ok())
    }
    pub fn fee(&self) -> f64 {
        self.average_fee_paid
            .as_ref()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0.0)
    }
}

pub struct OrderClient {
    base: String,
    http: reqwest::Client,
    signer: Signer,
}

impl OrderClient {
    pub fn new(base: impl Into<String>, signer: Signer) -> Result<Self> {
        // Orders fire ~once/15min — far past the idle window — so a cold order pays a fresh
        // TCP+TLS handshake (~280ms vs ~105ms warm, measured EU→Kalshi). HTTP/2 holds ONE
        // multiplexed connection per host, kept alive by PING frames even while idle, so the
        // order POST reuses the warm connection. (HTTP/1.1 pooling did NOT reliably reuse it.)
        // The warm-ping task in main.rs establishes/refreshes the connection; h2 PINGs keep it.
        let http = reqwest::Client::builder()
            .timeout(std::time::Duration::from_secs(10))
            .pool_idle_timeout(None)
            .tcp_keepalive(std::time::Duration::from_secs(30))
            .http2_keep_alive_interval(std::time::Duration::from_secs(15))
            .http2_keep_alive_timeout(std::time::Duration::from_secs(10))
            .http2_keep_alive_while_idle(true)
            .build()?;
        Ok(Self {
            base: base.into(),
            http,
            signer,
        })
    }

    fn signed(&self, rb: reqwest::RequestBuilder, method: &str, path: &str) -> reqwest::RequestBuilder {
        let mut rb = rb;
        for (k, v) in self.signer.headers(now_ms(), method, path) {
            rb = rb.header(k, v);
        }
        rb
    }

    /// Place an immediate-or-cancel order. Returns (http_status, OrderResp, latency_ms).
    pub async fn create_ioc(
        &self,
        ticker: &str,
        side: &str,
        count: f64,
        price: f64,
        client_order_id: String,
    ) -> Result<(u16, OrderResp, u128)> {
        let body = V2Order {
            ticker,
            side,
            count: format!("{:.2}", count),
            price: format!("{:.2}", price),
            time_in_force: "immediate_or_cancel",
            self_trade_prevention_type: "taker_at_cross",
            client_order_id,
        };
        let url = format!("{}/portfolio/events/orders", self.base);
        let rb = self.signed(self.http.post(&url).json(&body), "POST", "/trade-api/v2/portfolio/events/orders");
        let t0 = Instant::now();
        let resp = rb.send().await.context("send order")?;
        let status = resp.status().as_u16();
        let lat = t0.elapsed().as_millis();
        let txt = resp.text().await.unwrap_or_default();
        if status != 201 {
            warn!("order HTTP {}: {}", status, &txt[..txt.len().min(220)]);
        }
        let parsed = serde_json::from_str::<OrderResp>(&txt).unwrap_or_default();
        Ok((status, parsed, lat))
    }

    /// Account cash balance in dollars (read-only, for logging / sanity).
    pub async fn balance_dollars(&self) -> Result<f64> {
        let url = format!("{}/portfolio/balance", self.base);
        let rb = self.signed(self.http.get(&url), "GET", "/trade-api/v2/portfolio/balance");
        let v = rb.send().await?.json::<serde_json::Value>().await?;
        Ok(v.get("balance").and_then(|b| b.as_f64()).unwrap_or(0.0) / 100.0)
    }
}
