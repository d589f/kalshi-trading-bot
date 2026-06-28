//! Append-only JSONL ledger — the record of every would-be order ("empty order")
//! and its settlement, for paper-vs-live comparison. One JSON object per line.

use std::fs::OpenOptions;
use std::io::Write;
use std::sync::Mutex;

use serde::{Deserialize, Serialize};
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

/// What actually happened when `place_live` tried to send a live order. Serialized
/// as the snake_case `outcome` discriminator on a `LiveTriggerRecord`. Records with
/// NO `outcome` field are legacy (written only on a fill) → treated as `Filled`.
#[derive(Serialize, Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    /// IOC fully filled (a real position) — counts on the dashboard.
    Filled,
    /// IOC filled some but `remaining_count > 0` (still a real position).
    Partial,
    /// order accepted (HTTP 201) but zero fill — no position.
    Nofill,
    /// skipped before any order: daily trade cap reached.
    SkipDailyCap,
    /// skipped before any order: daily loss-stop reached.
    SkipLossStop,
    /// skipped before any order: signal entry out of (0.50, max_entry].
    SkipBand,
    /// `create_ioc` returned a transport error (no order placed).
    OrderError,
    /// order returned a non-201 HTTP status (rejected).
    Rejected,
}

impl Outcome {
    /// True only for outcomes that represent a real position (or legacy fills).
    /// Wired into the dashboard loader filter in a later slice.
    #[allow(dead_code)]
    pub fn is_position(self) -> bool {
        matches!(self, Outcome::Filled | Outcome::Partial)
    }
}

/// A live order attempt's full record, replacing the ad-hoc `json!` row in
/// `place_live`. Carries the fields needed to decompose the live-vs-paper entry gap
/// (`gap = eff - signal_entry`, `drift = exec_entry - signal_entry`, `walk = eff - exec_entry`).
///
/// Schema is ADDITIVE over the legacy live row: `kind`/`live`/`entry` keep their old
/// meaning (`entry` carries the effective fill price `eff`, as the resolver/dashboard
/// expect) and every new field is either always-present or `Option` + skipped-when-none,
/// so old loaders still parse and old records (no `outcome`) still read as fills.
#[derive(Serialize, Debug, Clone)]
#[allow(dead_code)] // wired into place_live in slice 3
pub struct LiveTriggerRecord {
    pub kind: &'static str, // always "trigger"
    pub live: bool,         // always true
    pub outcome: Outcome,
    pub ts: f64,
    pub ts_iso: String,
    pub session: String,
    pub window_start: String,
    pub window_end: String,
    pub market_ticker: String,
    pub side: String,
    /// effective per-contract entry of the side bought (legacy `entry` key = `eff`).
    pub entry: f64,
    /// the paper/signal entry the gate used (`fire.entry`) — additive.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signal_entry: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub p: Option<f64>,
    pub delta_from_open: f64,
    // ---- order/fill fields: present only when an order was actually built/sent ----
    #[serde(skip_serializing_if = "Option::is_none")]
    pub exec_entry: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub orderbook_entry: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_limit_price: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requote_limit_price: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub requote: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub remaining_count: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fill: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub eff: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fee: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latency_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub order_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub count: Option<i64>,
}

impl LiveTriggerRecord {
    /// Base record with the always-present context fields set and every order/fill
    /// field empty. Callers fill in the order fields on the paths where they apply.
    #[allow(clippy::too_many_arguments)]
    #[allow(dead_code)] // wired into place_live in slice 3
    pub fn new(
        outcome: Outcome,
        ts: f64,
        ts_iso: String,
        session: String,
        window_start: String,
        window_end: String,
        market_ticker: String,
        side: String,
        entry: f64,
        signal_entry: Option<f64>,
        p: Option<f64>,
        delta_from_open: f64,
    ) -> Self {
        Self {
            kind: "trigger",
            live: true,
            outcome,
            ts,
            ts_iso,
            session,
            window_start,
            window_end,
            market_ticker,
            side,
            entry,
            signal_entry,
            p,
            delta_from_open,
            exec_entry: None,
            orderbook_entry: None,
            first_limit_price: None,
            requote_limit_price: None,
            requote: None,
            remaining_count: None,
            fill: None,
            eff: None,
            fee: None,
            latency_ms: None,
            order_id: None,
            count: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn outcome_tags_are_snake_case() {
        assert_eq!(serde_json::to_string(&Outcome::Filled).unwrap(), "\"filled\"");
        assert_eq!(serde_json::to_string(&Outcome::Partial).unwrap(), "\"partial\"");
        assert_eq!(serde_json::to_string(&Outcome::Nofill).unwrap(), "\"nofill\"");
        assert_eq!(
            serde_json::to_string(&Outcome::SkipDailyCap).unwrap(),
            "\"skip_daily_cap\""
        );
        assert_eq!(
            serde_json::to_string(&Outcome::SkipLossStop).unwrap(),
            "\"skip_loss_stop\""
        );
        assert_eq!(serde_json::to_string(&Outcome::SkipBand).unwrap(), "\"skip_band\"");
        assert_eq!(
            serde_json::to_string(&Outcome::OrderError).unwrap(),
            "\"order_error\""
        );
        assert_eq!(serde_json::to_string(&Outcome::Rejected).unwrap(), "\"rejected\"");
    }

    #[test]
    fn outcome_roundtrip_and_unknown_rejected() {
        for o in [
            Outcome::Filled,
            Outcome::Partial,
            Outcome::Nofill,
            Outcome::SkipDailyCap,
            Outcome::SkipLossStop,
            Outcome::SkipBand,
            Outcome::OrderError,
            Outcome::Rejected,
        ] {
            let s = serde_json::to_string(&o).unwrap();
            let back: Outcome = serde_json::from_str(&s).unwrap();
            assert_eq!(o, back);
        }
        assert!(serde_json::from_str::<Outcome>("\"bogus\"").is_err());
    }

    fn sample(outcome: Outcome) -> LiveTriggerRecord {
        LiveTriggerRecord::new(
            outcome,
            1.0,
            "2026-06-28T12:00:00+00:00".into(),
            "f6_wait270_shadow".into(),
            "2026-06-28T11:45:00+00:00".into(),
            "2026-06-28T12:00:00+00:00".into(),
            "KXBTC15M-26JUN281145-45".into(),
            "yes".into(),
            0.70,
            Some(0.64),
            Some(0.73),
            113.34,
        )
    }

    #[test]
    fn filled_record_serializes_all_fields() {
        let mut r = sample(Outcome::Filled);
        r.exec_entry = Some(0.66);
        r.orderbook_entry = Some(0.66);
        r.first_limit_price = Some(0.72);
        r.requote = Some(false);
        r.remaining_count = Some(0);
        r.fill = Some(7.0);
        r.eff = Some(0.70);
        r.fee = Some(0.0147);
        r.latency_ms = Some(119);
        r.order_id = Some("abc".into());
        r.count = Some(7);
        let v: serde_json::Value = serde_json::to_value(&r).unwrap();
        assert_eq!(v["kind"], "trigger");
        assert_eq!(v["live"], true);
        assert_eq!(v["outcome"], "filled");
        // legacy `entry` key carries the effective fill (resolver/dashboard read this).
        assert_eq!(v["entry"], 0.70);
        assert_eq!(v["signal_entry"], 0.64);
        assert_eq!(v["exec_entry"], 0.66);
        assert_eq!(v["latency_ms"], 119);
    }

    #[test]
    fn skip_record_omits_order_fields() {
        let r = sample(Outcome::SkipDailyCap);
        let s = serde_json::to_string(&r).unwrap();
        assert!(s.contains("\"outcome\":\"skip_daily_cap\""));
        // none of the order/fill fields should appear when unset
        for k in [
            "exec_entry",
            "first_limit_price",
            "requote_limit_price",
            "requote",
            "remaining_count",
            "fill",
            "eff",
            "fee",
            "latency_ms",
            "order_id",
            "count",
            "orderbook_entry",
        ] {
            assert!(!s.contains(k), "skip record must omit `{k}` when None: {s}");
        }
        // additive context fields still present
        assert!(s.contains("\"signal_entry\":0.64"));
    }

    #[test]
    fn requote_record_has_both_prices() {
        let mut r = sample(Outcome::Filled);
        r.exec_entry = Some(0.66);
        r.first_limit_price = Some(0.72);
        r.requote = Some(true);
        r.requote_limit_price = Some(0.78);
        let v: serde_json::Value = serde_json::to_value(&r).unwrap();
        assert_eq!(v["requote"], true);
        assert_eq!(v["first_limit_price"], 0.72);
        assert_eq!(v["requote_limit_price"], 0.78);
        assert_ne!(v["first_limit_price"], v["requote_limit_price"]);
    }
}
