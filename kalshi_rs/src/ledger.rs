//! Append-only JSONL ledger — the record of every would-be order ("empty order")
//! and its settlement, for paper-vs-live comparison. One JSON object per line.

use std::fs::OpenOptions;
use std::io::Write;
use std::sync::Mutex;

use serde::Serialize;
use tracing::error;

pub struct Ledger {
    path: String,
    lock: Mutex<()>,
}

impl Ledger {
    pub fn new(path: impl Into<String>) -> Self {
        Self {
            path: path.into(),
            lock: Mutex::new(()),
        }
    }

    pub fn append<T: Serialize>(&self, rec: &T) {
        let line = match serde_json::to_string(rec) {
            Ok(s) => s,
            Err(e) => {
                error!("ledger serialize: {e}");
                return;
            }
        };
        let _g = self.lock.lock().unwrap_or_else(|p| p.into_inner());
        match OpenOptions::new().create(true).append(true).open(&self.path) {
            Ok(mut f) => {
                if let Err(e) = writeln!(f, "{line}") {
                    error!("ledger write: {e}");
                }
            }
            Err(e) => error!("ledger open {}: {e}", self.path),
        }
    }
}

/// A would-be order emitted on a trigger (the "empty order"). Captures everything
/// needed to (a) reconstruct the exact order we'd send live and (b) diff vs the
/// paper engine's decision at the same instant.
#[derive(Serialize)]
pub struct TriggerRecord<'a> {
    pub kind: &'a str, // "trigger"
    pub ts: f64,
    pub ts_iso: String,
    pub session: &'a str,
    pub window_start: String,
    pub window_end: String,
    pub market_ticker: &'a str,
    // the order we WOULD place
    pub action: &'a str, // "buy"
    pub order_type: &'a str, // "limit" (FAK when live)
    pub side: &'a str,   // "yes" | "no"
    pub count: i64,
    pub limit_price_cents: i64,
    pub stake_usd: f64,
    // signal context (so triggers can be matched to paper)
    pub entry: f64,
    pub orderbook_entry: f64,
    pub last_trade_expensive: Option<f64>,
    pub delta_from_open: f64,
    pub delta_from_prev: Option<f64>,
    pub binance_price: f64,
    pub sigma_type: &'a str,
    pub sigma_used: f64,
    pub snr: f64,
    pub p: Option<f64>,
    pub tau: f64,
    pub elapsed_min: f64,
    // book snapshot at trigger
    pub yes_bid: Option<f64>,
    pub yes_ask: Option<f64>,
    pub no_bid: Option<f64>,
    pub no_ask: Option<f64>,
    pub floor_strike: Option<f64>,
}

/// Settlement of a previously-fired trigger, computed on Kalshi's real result.
#[derive(Serialize)]
pub struct ResolveRecord<'a> {
    pub kind: &'a str, // "resolve"
    pub ts: f64,
    pub ts_iso: String,
    pub session: &'a str,
    pub market_ticker: &'a str,
    pub side: &'a str,
    pub entry: f64,
    pub stake_usd: f64,
    pub result: &'a str, // "yes" | "no"
    pub won: bool,
    pub pnl_usd: f64,
}
