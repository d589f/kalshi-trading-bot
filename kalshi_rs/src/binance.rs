//! Binance public feed — `@aggTrade` for the primary BTC price + the trade buffer
//! (OFI). Mirrors ws_consumers.py §2.1. Reconnecting loop with exponential backoff.
//! No auth (public stream).

use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use futures_util::StreamExt;
use serde::Deserialize;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{info, warn};

use crate::config::TRADE_BUFFER_RETENTION_SECS;
use crate::state::AppState;

const AGGTRADE_URL: &str = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade";

#[derive(Deserialize)]
struct AggTrade {
    #[serde(rename = "p")]
    price: String,
    #[serde(rename = "q")]
    qty: String,
    /// buyer-is-maker flag: m=true ⇒ aggressor SOLD (taker sell)
    #[serde(rename = "m")]
    is_sell: bool,
    #[serde(rename = "T", default)]
    _trade_time: i64,
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Run the Binance feed forever, updating `state`. Returns only on unrecoverable setup error.
/// `AGGTRADE_URL` (binance.com) is the default; on a US/Kalshi-compliant host binance.com is
/// geo-blocked (HTTP 451), so the systemd unit sets `BINANCE_WS_URL` to the Binance.US stream.
/// The delta-based signal only uses price CHANGE, so the .us↔.com basis is irrelevant.
pub async fn run(state: Arc<Mutex<AppState>>) -> Result<()> {
    let url = std::env::var("BINANCE_WS_URL").unwrap_or_else(|_| AGGTRADE_URL.to_string());
    info!("binance feed = {url}");
    let mut backoff = 1.0f64;
    loop {
        match connect_async(&url).await {
            Ok((mut ws, _resp)) => {
                info!("binance @aggTrade connected");
                backoff = 1.0;
                while let Some(msg) = ws.next().await {
                    match msg {
                        Ok(Message::Text(txt)) => {
                            if let Ok(t) = serde_json::from_str::<AggTrade>(&txt) {
                                let price: f64 = match t.price.parse() {
                                    Ok(p) => p,
                                    Err(_) => continue,
                                };
                                let qty: f64 = t.qty.parse().unwrap_or(0.0);
                                let notional = price * qty;
                                let ts = now_secs();
                                if let Ok(mut s) = state.lock() {
                                    s.binance_price = Some(price);
                                    s.binance_ts = ts;
                                    s.trade_buffer.push((ts, notional, t.is_sell));
                                    // light prune to bound memory between signal-loop prunes
                                    if s.trade_buffer.len() > 20_000 {
                                        let cutoff = ts - TRADE_BUFFER_RETENTION_SECS;
                                        s.trade_buffer.retain(|(t, _, _)| *t >= cutoff);
                                    }
                                }
                            }
                        }
                        Ok(Message::Ping(_)) | Ok(Message::Pong(_)) => {}
                        Ok(Message::Close(_)) => {
                            warn!("binance closed frame");
                            break;
                        }
                        Ok(_) => {}
                        Err(e) => {
                            warn!("binance ws error: {e}");
                            break;
                        }
                    }
                }
            }
            Err(e) => warn!("binance connect failed: {e}"),
        }
        warn!("binance reconnecting in {backoff:.1}s");
        tokio::time::sleep(std::time::Duration::from_secs_f64(backoff)).await;
        backoff = (backoff * 2.0).min(30.0);
    }
}
