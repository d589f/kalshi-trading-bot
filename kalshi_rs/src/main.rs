//! Kalshi KXBTC15M `third`/f6_wait270 — live SHADOW runner.
//!
//! Runs the exact paper-engine signal against live feeds (Binance @aggTrade for BTC,
//! Kalshi public REST for the KXBTC15M orderbook). On each trigger it emits a would-be
//! ("empty") order to a JSONL ledger — NOTHING is sent to Kalshi. A resolver then scores
//! each would-be order on Kalshi's real settlement so we can compare paper vs (future) live.
//!
//! No API credentials required: Kalshi market data is public. Auth (rsa) is wired for the
//! later WS feed / real orders.

mod binance;
mod config;
mod dashboard;
mod engine;
mod kalshi;
mod ledger;
mod mirror;
mod signal;
mod state;
mod window;

#[cfg(test)]
mod f1_regression;

use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use chrono::{DateTime, SecondsFormat, Utc};
use tracing::{error, info, warn};
use tracing_subscriber::EnvFilter;

use config::{resolve_mirror_session_key, ExecAnchor, SessionConfig, SessionSel, STATE_TICK_SECS};
use dashboard::{Dash, TrigSummary};
use engine::{evaluate, Shared, Side};
use kalshi::auth::Signer;
use kalshi::orders::OrderClient;
use kalshi::rest::{KalshiRest, PROD_BASE, SERIES_KXBTC15M};
use kalshi::{kalshi_fee_usd, pick_market_for_close, KalshiBook};
use ledger::{Ledger, LiveTriggerRecord, Outcome, ResolveRecord, TriggerRecord};
use signal::{compute_all_sigmas, compute_ofi, p_model_classic};
use state::AppState;

fn env_f64(k: &str, d: f64) -> f64 {
    std::env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}
fn env_i64(k: &str, d: i64) -> i64 {
    std::env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}

/// Resolve the entry-price cap: an env `MAX_ENTRY` override (clamped to a sane (0.5, 0.99])
/// wins over the session default, so the live gate can be tightened without a rebuild.
fn resolve_max_entry(default: f64, env_val: Option<String>) -> f64 {
    env_val
        .and_then(|s| s.parse::<f64>().ok())
        .filter(|v| *v > 0.5 && *v <= 0.99)
        .unwrap_or(default)
}

/// Resolve the strategy from the SESSION env. Unset/empty → F6 (today's default).
/// A non-empty UNKNOWN value also falls back to F6 but returns a warning for main()
/// to log FAIL-LOUD — a silent typo running the wrong strategy on real money is the
/// failure mode this guards against. Pure (no logging/exit) so it stays testable.
fn select_session(env_val: Option<&str>) -> (SessionSel, Option<String>) {
    let raw = env_val.map(str::trim).unwrap_or("");
    if raw.is_empty() {
        return (SessionSel::F6, None);
    }
    match config::resolve_session_sel(raw) {
        Some(sel) => (sel, None),
        None => (
            SessionSel::F6,
            Some(format!(
                "SESSION='{raw}' is not a known strategy (accepted: f6_wait270, f1_d50cap75) — falling back to f6_wait270"
            )),
        ),
    }
}

/// Resolve EXEC_ANCHOR (how the live IOC limit is priced). Unset/empty → Ask (today's
/// default). Non-empty UNKNOWN → Ask + a warning for main() to log FAIL-LOUD — a typo
/// must NEVER flip real-money execution to Signal. Pure, testable.
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
fn select_exec_anchor(env_val: Option<&str>) -> (ExecAnchor, Option<String>) {
    let raw = env_val.map(str::trim).unwrap_or("");
    if raw.is_empty() {
        return (ExecAnchor::Ask, None);
    }
    match config::resolve_exec_anchor(raw) {
        Some(a) => (a, None),
        None => (
            ExecAnchor::Ask,
            Some(format!(
                "EXEC_ANCHOR='{raw}' is not a known anchor (accepted: ask, signal) — falling back to ask"
            )),
        ),
    }
}

/// Telemetry-fetch bound in signal mode: the concurrent orderbook GET inside the order
/// join! is cut off here so a slow book (client timeout 8s) can never stall place_live's
/// return — the order is already gone; only exec_entry degrades to None.
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
const TELEMETRY_TIMEOUT_MS: u64 = 500;

// ---- signal-anchor pricing/sizing (pure; wired into place_live's Signal arm) ----
// The exec_* twins mirror the legacy ask-arm formulas so byte-identity is testable.

/// Signal mode, YES side: limit = signal entry + crossing allowance, API-clamped.
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
fn signal_yes_limit(signal_entry: f64, price_buf: f64) -> f64 {
    (signal_entry + price_buf).min(0.99)
}
/// Signal mode, NO side (NO book prices = 1 - yes price).
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
fn signal_no_limit(signal_entry: f64, price_buf: f64) -> f64 {
    ((1.0 - signal_entry) - price_buf).max(0.01)
}
/// Sizing off the SIGNAL price (legacy sizes off the drifted exec price).
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
fn signal_count(stake: f64, signal_entry: f64, max_count: i64) -> f64 {
    (stake / signal_entry).round().clamp(1.0, max_count as f64)
}
/// Legacy formulas keyed on the exec (fresh-ask) price — for byte-identity assertions.
#[cfg_attr(not(test), allow(dead_code))]
fn exec_yes_limit(exec_entry: f64, price_buf: f64) -> f64 {
    (exec_entry + price_buf).min(0.99)
}
#[cfg_attr(not(test), allow(dead_code))]
fn exec_no_limit(exec_entry: f64, price_buf: f64) -> f64 {
    ((1.0 - exec_entry) - price_buf).max(0.01)
}
#[cfg_attr(not(test), allow(dead_code))]
fn exec_count(stake: f64, exec_entry: f64, max_count: i64) -> f64 {
    (stake / exec_entry).round().clamp(1.0, max_count as f64)
}
/// Fail-open telemetry filter — same sanity band the legacy ask path applies.
#[cfg_attr(not(test), allow(dead_code))] // wired in slice 2
fn filtered_ask(ask: Option<f64>) -> Option<f64> {
    ask.filter(|v| *v > 0.50 && *v <= 0.98)
}

/// Strict SUBACCOUNT parse: a present-but-garbage value REFUSES to start — the old
/// silent 0 default would route real orders to the PRIMARY account, defeating the
/// subaccount isolation (security audit, MEDIUM).
fn parse_subaccount(env_val: Option<&str>) -> Result<u32, String> {
    match env_val.map(str::trim) {
        None | Some("") => Ok(0),
        Some(s) => s
            .parse::<u32>()
            .map_err(|_| format!("SUBACCOUNT='{s}' is not a valid subaccount number")),
    }
}

/// Insert the mirrored paper sigma under the key the gate actually reads
/// (`cfg.sigma_type`). Hardcoding "max30" here was the H2 bug: under F1
/// (sigma_type="max10") the gate's lookup would MISS and silently fall back to the
/// LOCAL realized5 sigma — trading a formula that is not the selected strategy's.
fn insert_mirror_sigma(
    sigmas: &mut std::collections::HashMap<String, f64>,
    cfg: &SessionConfig,
    val: f64,
) {
    sigmas.insert(cfg.sigma_type.clone(), val);
}

/// σ for the operator displays (status log + dashboard card), keyed by the SELECTED
/// session's sigma_type — a hardcoded "max30" lookup would show 0.0 under F1.
fn display_sigma(sigmas: &std::collections::HashMap<String, f64>, cfg: &SessionConfig) -> f64 {
    sigmas.get(&cfg.sigma_type).copied().unwrap_or(0.0)
}

const LEDGER_PATH: &str = "shadow_ledger.jsonl";

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Kalshi close_time format: "2026-06-25T10:15:00Z"
fn kalshi_rfc3339(dt: DateTime<Utc>) -> String {
    dt.to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// Inputs to the live-order outcome decision, mirroring `place_live`'s gate order
/// exactly (daily-cap → loss-stop → band → order-error → rejected → no-fill → partial
/// → filled). Pure so the classification can be unit-tested without sending an order.
#[derive(Clone, Copy, Debug)]
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) struct GateSnapshot {
    pub trades_today: i64,
    pub max_trades_day: i64,
    pub day_pnl: f64,
    pub daily_loss_stop: f64,
    pub entry: f64,
    pub max_entry_price: f64,
    pub order_err: bool,
    pub status: u16,
    pub fill: f64,
    pub remaining_count: i64,
}

/// Classify a live-order attempt into an [`Outcome`] in EXACTLY `place_live`'s gate
/// order. The band predicate is byte-identical to the `place_live` band check
/// (`!(0.50 < entry && entry <= max_entry_price)`). Wired into `place_live` in a later slice.
#[cfg_attr(not(test), allow(dead_code))]
// band check kept byte-identical to place_live's `!(0.50 < entry && entry <= max)`.
#[allow(clippy::neg_cmp_op_on_partial_ord)]
pub(crate) fn classify_outcome(g: &GateSnapshot) -> Outcome {
    if g.trades_today >= g.max_trades_day {
        return Outcome::SkipDailyCap;
    }
    if g.day_pnl <= -g.daily_loss_stop {
        return Outcome::SkipLossStop;
    }
    if !(0.50 < g.entry && g.entry <= g.max_entry_price) {
        return Outcome::SkipBand;
    }
    if g.order_err {
        return Outcome::OrderError;
    }
    if g.status != 201 {
        return Outcome::Rejected;
    }
    if g.fill <= 0.0 {
        return Outcome::Nofill;
    }
    if g.remaining_count > 0 {
        return Outcome::Partial;
    }
    Outcome::Filled
}

/// Decompose the entry gap of a filled live order:
/// `gap = eff - signal_entry`, `drift = exec_entry - signal_entry`, `walk = eff - exec_entry`.
/// By construction `drift + walk == gap`.
#[cfg_attr(not(test), allow(dead_code))]
pub(crate) fn decompose_gap(signal_entry: f64, exec_entry: f64, eff: f64) -> (f64, f64, f64) {
    let gap = eff - signal_entry;
    let drift = exec_entry - signal_entry;
    let walk = eff - exec_entry;
    (gap, drift, walk)
}

/// Whether a ledger "trigger" row with this `outcome` represents a real position and
/// should be counted as a dashboard trigger. `None` = legacy record (live rows were
/// only ever written on a fill) → counts.
pub(crate) fn is_dashboard_trigger(outcome: Option<Outcome>) -> bool {
    outcome.is_none_or(|o| o.is_position())
}

/// P0b retry policy. A no-fill / order-error / rejected is a *retryable* miss — it consumes a
/// bounded attempt but does NOT latch the window. Everything else (filled, partial, or a
/// deliberate skip) latches the window so we never re-fire it (and skips never spam).
pub(crate) fn counts_as_attempt(o: Outcome) -> bool {
    matches!(o, Outcome::Nofill | Outcome::OrderError | Outcome::Rejected)
}

/// Latch the window on any non-retryable outcome (filled/partial = got a position; skip_* =
/// a deliberate no-trade). The window is NOT latched on a retryable miss, so it can re-attempt.
pub(crate) fn latch_decision(o: Outcome) -> bool {
    !counts_as_attempt(o)
}

/// May we place another live attempt this window? Bounded by max attempts and a cooldown since
/// the last attempt. A backwards clock (`now < last`) yields false (stay in the safe direction).
pub(crate) fn retry_gate(attempt_count: i64, last_attempt_ts: f64, now: f64, n: i64, c: f64) -> bool {
    attempt_count < n && (now - last_attempt_ts) >= c
}

/// Build the typed fill/partial ledger record from `place_live`'s fill-path locals.
/// The legacy JSON `entry` key carries `eff` (resolver/dashboard read it); the paper/signal
/// entry goes into the additive `signal_entry` field so `gap = eff - signal_entry`.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_fill_record(
    session: &str,
    outcome: Outcome,
    ts: f64,
    ts_iso: String,
    window_start: String,
    window_end: String,
    market_ticker: String,
    side: &str,
    signal_entry: f64,
    orderbook_entry: f64,
    p: Option<f64>,
    delta_from_open: f64,
    exec_entry: f64,
    first_limit_price: f64,
    requote: bool,
    requote_limit_price: Option<f64>,
    remaining_count: i64,
    fill: f64,
    eff: f64,
    fee: f64,
    latency_ms: i64,
    order_id: &str,
) -> LiveTriggerRecord {
    let mut r = LiveTriggerRecord::new(
        outcome,
        ts,
        ts_iso,
        session.to_string(),
        window_start,
        window_end,
        market_ticker,
        side.to_string(),
        eff, // legacy `entry` key = effective fill
        Some(signal_entry),
        p,
        delta_from_open,
    );
    r.exec_entry = Some(exec_entry);
    r.orderbook_entry = Some(orderbook_entry);
    r.first_limit_price = Some(first_limit_price);
    r.requote = Some(requote);
    r.requote_limit_price = requote_limit_price;
    r.remaining_count = Some(remaining_count);
    r.fill = Some(fill);
    r.eff = Some(eff);
    r.fee = Some(fee);
    r.latency_ms = Some(latency_ms);
    r.order_id = if order_id.is_empty() {
        None
    } else {
        Some(order_id.to_string())
    };
    r.count = Some(fill as i64);
    r
}

/// Build a context-only record (order/fill fields unset) for a skip / error / no-fill
/// path. There is no effective fill, so the legacy `entry` key carries `signal_entry`.
/// Callers set whatever order fields apply to their path before appending.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_context_record(
    session: &str,
    outcome: Outcome,
    ts: f64,
    ts_iso: String,
    window_start: String,
    window_end: String,
    market_ticker: String,
    side: &str,
    signal_entry: f64,
    orderbook_entry: f64,
    p: Option<f64>,
    delta_from_open: f64,
) -> LiveTriggerRecord {
    let mut r = LiveTriggerRecord::new(
        outcome,
        ts,
        ts_iso,
        session.to_string(),
        window_start,
        window_end,
        market_ticker,
        side.to_string(),
        signal_entry,
        Some(signal_entry),
        p,
        delta_from_open,
    );
    r.orderbook_entry = Some(orderbook_entry);
    r
}

/// Replay the JSONL ledger into the dashboard so shadow history survives restarts.
fn load_ledger_into_dash(path: &str, dash: &Arc<Mutex<Dash>>) {
    let content = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(_) => return,
    };
    let mut d = dash.lock().unwrap();
    for line in content.lines() {
        replay_line_into(&mut d, line);
    }
    info!("loaded {} shadow triggers from ledger", d.triggers.len());
}

/// Parse a ledger `outcome` tag. Returns `None` for an unrecognized tag (callers treat
/// an unrecognized tag conservatively — not a confirmed position).
fn outcome_from_str(s: &str) -> Option<Outcome> {
    serde_json::from_value::<Outcome>(serde_json::Value::String(s.to_string())).ok()
}

/// Replay one JSONL line into the dashboard. A `trigger` row is counted as a dashboard
/// trigger only when it is a real position: a filled/partial outcome, OR a legacy row
/// with no `outcome` field (live rows were historically written only on fills). nofill/
/// skip/error rows — and rows with an unrecognized `outcome` — are NOT counted. Malformed
/// lines are skipped without aborting the replay.
fn replay_line_into(d: &mut Dash, line: &str) {
    let v: serde_json::Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(_) => return,
    };
    match v.get("kind").and_then(|k| k.as_str()) {
        Some("trigger") => {
            let push = match v.get("outcome") {
                None => is_dashboard_trigger(None), // legacy fill row
                Some(val) => match val.as_str().and_then(outcome_from_str) {
                    Some(o) => is_dashboard_trigger(Some(o)),
                    None => false, // present but unrecognized → conservatively skip
                },
            };
            if push {
                d.triggers.push(TrigSummary {
                    ts_iso: v["ts_iso"].as_str().unwrap_or("").to_string(),
                    window_start: v["window_start"].as_str().unwrap_or("").to_string(),
                    window_end: v["window_end"].as_str().unwrap_or("").to_string(),
                    ticker: v["market_ticker"].as_str().unwrap_or("").to_string(),
                    side: v["side"].as_str().unwrap_or("").to_string(),
                    entry: v["entry"].as_f64().unwrap_or(0.0),
                    count: v["count"].as_i64().unwrap_or(0),
                    delta: v["delta_from_open"].as_f64().unwrap_or(0.0),
                    p: v["p"].as_f64(),
                    result: None,
                    won: None,
                    pnl: None,
                    // LiveTriggerRecord rows carry "live":true; legacy shadow rows omit it
                    live: v["live"].as_bool().unwrap_or(false),
                });
            }
        }
        Some("resolve") => {
            let (t, s, e) = (
                v["market_ticker"].as_str().unwrap_or("").to_string(),
                v["side"].as_str().unwrap_or("").to_string(),
                v["entry"].as_f64().unwrap_or(0.0),
            );
            let (res, won, pnl) = (
                v["result"].as_str().unwrap_or("").to_string(),
                v["won"].as_bool().unwrap_or(false),
                v["pnl_usd"].as_f64().unwrap_or(0.0),
            );
            match v.get("live").and_then(|x| x.as_bool()) {
                Some(b) => {
                    d.resolve(&t, &s, e, &res, won, pnl, b);
                }
                None => {
                    // LEGACY resolve (pre-feature, no flag). Pre-feature history is NOT
                    // all-shadow: live fills were LiveTriggerRecord{live:true} while
                    // their resolves had no flag — try shadow first (majority), then
                    // fall back to the live row so historical live fills stay resolved.
                    if !d.resolve(&t, &s, e, &res, won, pnl, false) {
                        d.resolve(&t, &s, e, &res, won, pnl, true);
                    }
                }
            }
        }
        _ => {}
    }
}

/// A fired order awaiting Kalshi settlement (shadow would-be, or a real live fill).
#[derive(Clone)]
struct Pending {
    ticker: String,
    /// ledger session tag for the ResolveRecord (f6 shadow and the live strategy
    /// resolve under their OWN tags)
    session: String,
    side: &'static str, // "yes" | "no"
    entry: f64,
    count: f64,
    stake: f64,
    window_end: DateTime<Utc>,
    live: bool, // true = real money trade (resolver updates daily PnL)
}

/// Live-trading config (env). Disabled by default — shadow stays log-only unless LIVE_TRADING=1.
#[derive(Clone)]
struct LiveCfg {
    enabled: bool,
    stake: f64,
    max_trades_day: i64,
    daily_loss_stop: f64,
    max_count: i64,
    price_buf: f64,
    /// on a no-fill, re-quote ONE deeper IOC at entry±requote_buf to catch a moved book.
    /// Applies only to the failed order (normal fills keep price_buf). 0 disables re-quote.
    requote_buf: f64,
    /// P0b: a no-fill no longer burns the window — retry up to this many attempts/window,
    /// spaced by retry_cooldown_secs. retry_max_attempts=1 + cooldown=0 == legacy (one shot).
    retry_max_attempts: i64,
    retry_cooldown_secs: f64,
}

/// MIRROR config (env). When `url` is set, the f6 signal is taken straight from the
/// prod paper engine (window_open / Δ / σ / book) instead of our own Binance feed,
/// making the side + entry 1:1 with the paper. `max_age_secs` guards a stale/dead paper:
/// if the paper's last_update is older than this, we SKIP the tick (never trade blind).
#[derive(Clone)]
struct MirrorCfg {
    url: Option<String>,
    max_age_secs: f64,
    /// paper-engine session whose live_sigma we mirror (f6_wait270 / f1_d50cap75) —
    /// explicit MIRROR_SESSION env wins, else the selected strategy's own key.
    session_key: String,
}

const LIVE_STATE_PATH: &str = "live_state.json";

#[derive(Default, Clone, serde::Serialize, serde::Deserialize)]
struct LiveState {
    day: String,
    trades_today: i64,
    day_pnl: f64,
    /// authoritative all-time real PnL across every resolved live trade (fees included)
    #[serde(default)]
    total_pnl: f64,
    /// window_start (rfc3339) of the last CONFIRMED live fill — persists the
    /// 1-trade-per-window latch across restarts, so a redeploy right after a fill
    /// cannot double-order the same still-open window. Old state files parse to None.
    #[serde(default)]
    last_filled_window: Option<String>,
}

/// Mark a confirmed fill in the persistent live state: bump the day counter and
/// remember the filled window for the restart latch.
fn record_fill_state(s: &mut LiveState, win_key: Option<String>) {
    s.trades_today += 1;
    s.last_filled_window = win_key;
}

/// Sum the real PnL of every `resolve` row in a ledger string. Telemetry rows
/// (`kind:"trigger"`, including nofill/skip) are ignored — only settlements count.
fn sum_resolve_pnl(content: &str) -> f64 {
    let mut tot = 0.0;
    for line in content.lines() {
        if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
            if v.get("kind").and_then(|k| k.as_str()) == Some("resolve") {
                if let Some(p) = v.get("pnl_usd").and_then(|p| p.as_f64()) {
                    tot += p;
                }
            }
        }
    }
    (tot * 100.0).round() / 100.0
}

/// Sum the real PnL of every resolved trade in the ledger — the authoritative all-time total.
fn total_pnl_from_ledger() -> f64 {
    std::fs::read_to_string(LEDGER_PATH)
        .map(|c| sum_resolve_pnl(&c))
        .unwrap_or(0.0)
}

fn load_live_state() -> LiveState {
    std::fs::read_to_string(LIVE_STATE_PATH)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

fn save_live_state(s: &LiveState) {
    if let Ok(j) = serde_json::to_string(s) {
        let _ = std::fs::write(LIVE_STATE_PATH, j);
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();

    let base = std::env::var("KALSHI_BASE").unwrap_or_else(|_| PROD_BASE.to_string());
    info!("kalshi base = {base}");

    // Strategy selection (SESSION env; default f6). Unknown value = fail-loud + f6
    // fallback so a typo can never silently run the wrong formula on real money.
    let session_env = std::env::var("SESSION").ok();
    let (session_sel, session_warn) = select_session(session_env.as_deref());
    if let Some(w) = &session_warn {
        error!("{w}");
    }
    // Execution anchor (EXEC_ANCHOR env; default ask = legacy fresh-ask pricing).
    let anchor_env = std::env::var("EXEC_ANCHOR").ok();
    let (exec_anchor, anchor_warn) = select_exec_anchor(anchor_env.as_deref());
    if let Some(w) = &anchor_warn {
        error!("{w}");
    }
    let mut cfg = session_sel.config();
    cfg.max_entry_price = resolve_max_entry(cfg.max_entry_price, std::env::var("MAX_ENTRY").ok());
    info!(
        "session '{}': entry_wait={}min delta>={} p>={} sigma={} max_entry={} stake=${}",
        cfg.name,
        cfg.entry_wait_min,
        cfg.delta_threshold,
        cfg.p_model_threshold,
        cfg.sigma_type,
        cfg.max_entry_price,
        cfg.stake
    );

    let state = Arc::new(Mutex::new(AppState::new()));
    let ledger = Arc::new(Ledger::new(LEDGER_PATH));
    let pending: Arc<Mutex<Vec<Pending>>> = Arc::new(Mutex::new(Vec::new()));
    let rest = Arc::new(KalshiRest::new(base.clone())?);

    // ---- live trading setup (REAL orders; OFF unless LIVE_TRADING=1) ----
    let lcfg = LiveCfg {
        enabled: std::env::var("LIVE_TRADING").map(|v| v == "1").unwrap_or(false),
        stake: env_f64("STAKE", 5.0),
        max_trades_day: env_i64("MAX_TRADES_DAY", 20),
        daily_loss_stop: env_f64("DAILY_LOSS_STOP", 30.0),
        max_count: env_i64("MAX_COUNT", 15),
        price_buf: env_f64("PRICE_BUF", 0.02),
        requote_buf: env_f64("REQUOTE_BUF", 0.12),
        retry_max_attempts: env_i64("RETRY_MAX_ATTEMPTS", 2),
        retry_cooldown_secs: env_f64("RETRY_COOLDOWN_SECS", 3.0),
    };
    // MIRROR: take the signal from the paper engine (1:1). Off unless MIRROR_STATE_URL set.
    let mcfg = MirrorCfg {
        url: std::env::var("MIRROR_STATE_URL").ok().filter(|s| !s.is_empty()),
        max_age_secs: env_f64("MIRROR_MAX_AGE_SECS", 5.0),
        session_key: resolve_mirror_session_key(
            &session_sel,
            std::env::var("MIRROR_SESSION").ok().as_deref(),
        ),
    };
    if let Some(u) = &mcfg.url {
        info!(
            "MIRROR mode ON — signal sourced from paper engine {u} (max_age={}s, session={})",
            mcfg.max_age_secs, mcfg.session_key
        );
        // An unknown key isn't fatal (MIRROR_SESSION may point at any paper session),
        // but if the paper engine doesn't expose it every tick will skip — say so loudly
        // instead of leaving only the generic per-tick skip warning.
        if config::resolve_session_sel(&mcfg.session_key).is_none() {
            warn!(
                "MIRROR_SESSION='{}' is not a known strategy key — if the paper engine does not expose this session, the bot will skip EVERY tick (no trades)",
                mcfg.session_key
            );
        }
    }
    let http = reqwest::Client::new();
    let live_state = Arc::new(Mutex::new(load_live_state()));
    // backfill the authoritative all-time total from the ledger if it's not tracked yet
    {
        let mut s = live_state.lock().unwrap();
        if s.total_pnl == 0.0 {
            s.total_pnl = total_pnl_from_ledger();
            save_live_state(&s);
            info!("total real PnL (all-time, from ledger): ${:+.2}", s.total_pnl);
        }
    }
    // Route live orders to this subaccount (0 = primary). Set SUBACCOUNT=1 to isolate the bot
    // on a funded subaccount instead of the main balance. Garbage value = refuse to start.
    let subaccount = match parse_subaccount(std::env::var("SUBACCOUNT").ok().as_deref()) {
        Ok(n) => n,
        Err(e) => {
            error!("{e} — refusing to start (a silent default would route REAL orders to the primary account)");
            anyhow::bail!("invalid SUBACCOUNT env");
        }
    };
    let order_client: Option<Arc<OrderClient>> =
        match (std::env::var("KALSHI_KEY_ID"), std::env::var("KALSHI_KEY_PATH")) {
            (Ok(kid), Ok(kpath)) => match std::fs::read_to_string(&kpath) {
                Ok(pem) => match Signer::from_pem(kid, &pem).and_then(|s| OrderClient::new(base.clone(), s, subaccount)) {
                    Ok(oc) => {
                        info!(
                            "order client ready | LIVE_TRADING={} stake=${} max/day={} loss_stop=${} subaccount={} exec_anchor={:?}",
                            lcfg.enabled, lcfg.stake, lcfg.max_trades_day, lcfg.daily_loss_stop, subaccount, exec_anchor
                        );
                        Some(Arc::new(oc))
                    }
                    Err(e) => {
                        warn!("order client init failed: {e}");
                        None
                    }
                },
                Err(e) => {
                    warn!("key file read failed: {e}");
                    None
                }
            },
            _ => {
                if lcfg.enabled {
                    warn!("LIVE_TRADING=1 but KALSHI_KEY_ID/KALSHI_KEY_PATH unset — staying shadow");
                }
                None
            }
        };
    // optional $0 self-test of the Rust order path (1c bid + 99c ask IOC, no fill)
    if std::env::var("SELF_TEST_ORDER").ok().as_deref() == Some("1") {
        if let Some(oc) = &order_client {
            self_test_orders(oc, &rest).await;
        }
    }

    // Keep the order connection HOT. Orders fire ~once/15min — far past reqwest's idle window —
    // so a cold order pays a fresh TCP+TLS handshake (~250-400ms measured from the EU box vs
    // ~10ms warm). A light signed GET /portfolio/balance every ORDER_WARM_SECS exercises the
    // pooled connection so the real order POST is ~1 RTT. Read-only; never touches trading logic.
    if lcfg.enabled {
        if let Some(oc) = &order_client {
            let oc = oc.clone();
            let warm_secs = env_f64("ORDER_WARM_SECS", 30.0).max(5.0);
            info!("order connection keep-warm every {warm_secs:.0}s (GET /portfolio/balance)");
            tokio::spawn(async move {
                let mut tick = tokio::time::interval(Duration::from_secs_f64(warm_secs));
                loop {
                    tick.tick().await;
                    if let Err(e) = oc.balance_dollars().await {
                        warn!("order keep-warm ping failed: {e}");
                    }
                }
            });
        }
    }

    // Persistent comparison cutoff: survives restarts (so deploys don't wipe the dashboard
    // history). Stored in `.cutoff` in the working dir; first run stamps now.
    let cutoff = match std::fs::read_to_string(".cutoff") {
        Ok(s) if !s.trim().is_empty() => s.trim().to_string(),
        _ => {
            let n = Utc::now().to_rfc3339();
            let _ = std::fs::write(".cutoff", &n);
            n
        }
    };
    let dash = Arc::new(Mutex::new(Dash::default()));
    {
        let mut d = dash.lock().unwrap();
        d.started_iso = cutoff;
        d.live.binance_feed = std::env::var("BINANCE_WS_URL")
            .unwrap_or_else(|_| "binance.us (default)".to_string());
        d.live.order_mode = if lcfg.enabled {
            format!("LIVE ${} (real orders)", lcfg.stake)
        } else {
            "log-only (shadow)".to_string()
        };
    }
    load_ledger_into_dash(LEDGER_PATH, &dash);
    let dash_port: u16 = std::env::var("DASH_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8890);
    {
        let d = dash.clone();
        tokio::spawn(async move { dashboard::serve(d, dash_port).await });
    }

    // optional push mode: the EU/binance.com instance POSTs its triggers to the Buffalo
    // dashboard's /shadow_com endpoint so both feeds appear side-by-side vs paper.
    if let Ok(push_url) = std::env::var("PUSH_TRIGGERS_URL") {
        let d = dash.clone();
        let token = std::env::var("DASH_TOKEN").unwrap_or_default();
        // also push the live signal snapshot so the dashboard shows OUR real binance.com signal,
        // not the host's low-volume binance.US shadow.
        let live_url = push_url.replace("/shadow_com", "/live_com");
        info!("push mode: triggers → {push_url} · live signal → {live_url}");
        tokio::spawn(async move {
            let client = reqwest::Client::new();
            loop {
                tokio::time::sleep(Duration::from_secs(3)).await;
                let (trig_body, live_body) = {
                    let dd = d.lock().unwrap();
                    (
                        serde_json::to_string(&dd.triggers).unwrap_or_default(),
                        serde_json::to_string(&dd.live).unwrap_or_default(),
                    )
                };
                let _ = client
                    .post(&push_url)
                    .header("X-Token", &token)
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(8))
                    .body(trig_body)
                    .send()
                    .await;
                let _ = client
                    .post(&live_url)
                    .header("X-Token", &token)
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(8))
                    .body(live_body)
                    .send()
                    .await;
            }
        });
    }

    // 1) Binance price/trade feed
    {
        let st = state.clone();
        tokio::spawn(async move {
            if let Err(e) = binance::run(st).await {
                warn!("binance task ended: {e}");
            }
        });
    }

    // 2) Kalshi orderbook poller (current window's KXBTC15M market)
    {
        let st = state.clone();
        let rc = rest.clone();
        tokio::spawn(async move { kalshi_poller(st, rc).await });
    }

    // 3) Settlement resolver
    {
        let rc = rest.clone();
        let pend = pending.clone();
        let led = ledger.clone();
        let dsh = dash.clone();
        let ls = live_state.clone();
        tokio::spawn(async move { resolver(rc, pend, led, dsh, ls).await });
    }

    // 4) Signal loop @ 0.3s
    signal_loop(
        state, ledger, pending, cfg, dash, order_client, lcfg, live_state, mcfg, http, rest,
        session_sel, exec_anchor,
    )
    .await;
    Ok(())
}

/// $0 self-test of the order path: a 1c bid (buy YES) and a 99c ask (sell YES) IOC on the
/// current mid-market — both must be accepted (201) with 0 fill, proving signing + V2 schema.
async fn self_test_orders(oc: &OrderClient, rest: &KalshiRest) {
    info!("SELF_TEST: probing order path ($0, no fill)...");
    let markets = match rest.get_markets(SERIES_KXBTC15M, "open").await {
        Ok(m) => m,
        Err(e) => {
            warn!("self-test discovery failed: {e}");
            return;
        }
    };
    let Some(m) = markets.first() else {
        warn!("self-test: no open market");
        return;
    };
    let raw = match rest.get_orderbook(&m.ticker, 5).await {
        Ok(r) => r,
        Err(e) => {
            warn!("self-test orderbook: {e}");
            return;
        }
    };
    let book = KalshiBook::derive(&m.ticker, &raw, None, now_secs());
    let byb = book.yes_bid.unwrap_or(0.0);
    let bnb = book.no_bid.unwrap_or(0.0);
    if bnb < 0.99 {
        match oc.create_ioc(&m.ticker, "bid", 1.0, 0.01, format!("selftest-bid-{}", now_secs() as i64)).await {
            Ok((s, r, lat)) => info!("SELF_TEST bid@$0.01 -> HTTP {} fill={} ({}ms)", s, r.fill_count, lat),
            Err(e) => warn!("self-test bid err: {e}"),
        }
    }
    if byb < 0.99 {
        match oc.create_ioc(&m.ticker, "ask", 1.0, 0.99, format!("selftest-ask-{}", now_secs() as i64)).await {
            Ok((s, r, lat)) => info!("SELF_TEST ask@$0.99 -> HTTP {} fill={} ({}ms)", s, r.fill_count, lat),
            Err(e) => warn!("self-test ask err: {e}"),
        }
    }
    if let Ok(b) = oc.balance_dollars().await {
        info!("SELF_TEST balance ${:.2} (should be unchanged)", b);
    }
}

/// Poll the current window's KXBTC15M orderbook into `state.kalshi`.
async fn kalshi_poller(state: Arc<Mutex<AppState>>, rest: Arc<KalshiRest>) {
    let mut cur_ticker: Option<String> = None;
    let mut cur_floor: Option<f64> = None;
    let mut cur_close: Option<String> = None;
    loop {
        let now = Utc::now();
        let (_w_start, w_end) = window::current_window(now);
        let close_iso = kalshi_rfc3339(w_end);

        // (re)discover the market when the window rolls or we don't know it yet
        if cur_close.as_deref() != Some(close_iso.as_str()) || cur_ticker.is_none() {
            match rest.get_markets(SERIES_KXBTC15M, "open").await {
                Ok(markets) => {
                    if let Some(m) = pick_market_for_close(&markets, &close_iso) {
                        cur_ticker = Some(m.ticker.clone());
                        cur_floor = m.floor_strike;
                        cur_close = Some(close_iso.clone());
                        info!("market for window {} = {}", close_iso, m.ticker);
                    } else {
                        warn!("no open KXBTC15M market with close_time {close_iso}");
                    }
                }
                Err(e) => warn!("get_markets: {e}"),
            }
        }

        if let Some(ticker) = cur_ticker.clone() {
            match rest.get_orderbook(&ticker, 10).await {
                Ok(raw) => {
                    let mut book = KalshiBook::derive(&ticker, &raw, None, now_secs());
                    book.floor_strike = cur_floor;
                    if let Ok(mut s) = state.lock() {
                        s.kalshi = Some(book);
                    }
                }
                Err(e) => warn!("get_orderbook {ticker}: {e}"),
            }
        }

        tokio::time::sleep(Duration::from_millis(1000)).await;
    }
}

/// Twin latch decision: emit the shadow would-be once per window key; never emit
/// without a window key.
fn twin_should_emit(twin_window: Option<&str>, win_key: Option<&str>) -> bool {
    win_key.is_some() && twin_window != win_key
}

/// Keep-predicate for the pending list after `pd` resolves: remove only rows matching
/// ticker/side/entry AND the live flag — a twin resolve must never evict the live
/// pending (they can share ticker/side/entry when eff == signal_entry).
fn retain_after_resolve(x: &Pending, pd: &Pending) -> bool {
    !(x.ticker == pd.ticker && x.side == pd.side && x.entry == pd.entry && x.live == pd.live)
}

/// Score fired would-be orders on Kalshi's real settlement.
async fn resolver(
    rest: Arc<KalshiRest>,
    pending: Arc<Mutex<Vec<Pending>>>,
    ledger: Arc<Ledger>,
    dash: Arc<Mutex<Dash>>,
    live_state: Arc<Mutex<LiveState>>,
) {
    loop {
        tokio::time::sleep(Duration::from_secs(20)).await;
        let now = Utc::now();
        // snapshot the ones whose window has closed (+45s grace for settlement data)
        let due: Vec<Pending> = {
            let p = pending.lock().unwrap();
            p.iter()
                .filter(|x| now > x.window_end + chrono::Duration::seconds(45))
                .cloned()
                .collect()
        };
        for pd in due {
            match rest.get_market(&pd.ticker).await {
                Ok(m) => {
                    if (m.status == "settled" || m.status == "finalized") && !m.result.is_empty() {
                        let won = (pd.side == "yes" && m.result == "yes")
                            || (pd.side == "no" && m.result == "no");
                        let cost = pd.count * pd.entry + kalshi_fee_usd(pd.count, pd.entry);
                        let pnl = if won { pd.count * 1.0 - cost } else { -cost };
                        ledger.append(&ResolveRecord {
                            kind: "resolve",
                            ts: now_secs(),
                            ts_iso: now.to_rfc3339(),
                            session: &pd.session,
                            market_ticker: &pd.ticker,
                            side: pd.side,
                            entry: pd.entry,
                            stake_usd: pd.stake,
                            result: &m.result,
                            won,
                            pnl_usd: (pnl * 100.0).round() / 100.0,
                            live: pd.live,
                        });
                        if pd.live {
                            let mut s = live_state.lock().unwrap();
                            s.day_pnl = ((s.day_pnl + pnl) * 100.0).round() / 100.0;
                            s.total_pnl = ((s.total_pnl + pnl) * 100.0).round() / 100.0;
                            save_live_state(&s);
                            info!(
                                "🟢 LIVE RESOLVED {} {} result={} won={} pnl=${:+.2} (day_pnl=${:+.2})",
                                pd.ticker, pd.side, m.result, won, pnl, s.day_pnl
                            );
                        } else {
                            info!(
                                "RESOLVED {} {} result={} won={} pnl=${:+.2}",
                                pd.ticker, pd.side, m.result, won, pnl
                            );
                        }
                        dash.lock().unwrap().resolve(
                            &pd.ticker,
                            pd.side,
                            pd.entry,
                            &m.result,
                            won,
                            (pnl * 100.0).round() / 100.0,
                            pd.live,
                        );
                        pending.lock().unwrap().retain(|x| retain_after_resolve(x, &pd));
                    }
                }
                Err(e) => warn!("resolve get_market {}: {e}", pd.ticker),
            }
        }
    }
}

/// The 0.3s decision loop — the analogue of `state_updater` → `check_all_sessions`.
#[allow(clippy::too_many_arguments)]
async fn signal_loop(
    state: Arc<Mutex<AppState>>,
    ledger: Arc<Ledger>,
    pending: Arc<Mutex<Vec<Pending>>>,
    cfg: SessionConfig,
    dash: Arc<Mutex<Dash>>,
    order_client: Option<Arc<OrderClient>>,
    lcfg: LiveCfg,
    live_state: Arc<Mutex<LiveState>>,
    mcfg: MirrorCfg,
    http: reqwest::Client,
    rest: Arc<KalshiRest>,
    sel: SessionSel,
    anchor: ExecAnchor,
) {
    let session_tag = sel.session_tag();
    // The REFERENCE SHADOW: the f6 strategy is always evaluated alongside the selected
    // live strategy and emitted as a live=false would-be — the dashboard's green line
    // stays "shadow f6" regardless of which strategy trades live.
    let shadow_cfg = SessionSel::F6.config();
    let shadow_tag = SessionSel::F6.session_tag();
    let mut tick = tokio::time::interval(Duration::from_secs_f64(STATE_TICK_SECS));
    // Restart latch: seed from the persisted last CONFIRMED fill. If that window is
    // still the current one (redeploy mid-window), the per-tick `fired_window ==
    // win_key` check skips it — no double order. A past window simply never matches.
    let mut fired_window: Option<String> = live_state.lock().unwrap().last_filled_window.clone();
    let mut last_status_log = 0.0f64;
    // P0b per-window retry state (reset when the window rolls).
    let mut attempt_window: Option<String> = None;
    let mut attempt_count: i64 = 0;
    let mut last_attempt_ts: f64 = f64::NEG_INFINITY;
    // f6-shadow latch: the would-be is emitted once per window (in-memory only —
    // a mid-window restart may re-emit one shadow row; accepted, the dashboard
    // dedupes per window).
    let mut twin_window: Option<String> = None;

    loop {
        tick.tick().await;
        let now = now_secs();
        let now_utc = Utc::now();

        // ---- snapshot + maintain derived series under lock ----
        let (price_opt, history, buffer, kalshi, win, rolled);
        {
            let mut s = state.lock().unwrap();
            price_opt = s.binance_price;
            if let Some(px) = price_opt {
                s.price_history.push((now, px));
                let cutoff = now - config::PRICE_HISTORY_RETENTION_SECS;
                if s.price_history.first().map(|(t, _)| *t < cutoff).unwrap_or(false) {
                    s.price_history.retain(|(t, _)| *t >= cutoff);
                }
                let tcut = now - config::TRADE_BUFFER_RETENTION_SECS;
                if s.trade_buffer.first().map(|(t, _, _)| *t < tcut).unwrap_or(false) {
                    s.trade_buffer.retain(|(t, _, _)| *t >= tcut);
                }
            }
            rolled = s.window.roll_if_new(now_utc, price_opt);
            s.window.seed_lazy(price_opt);
            history = s.price_history.clone();
            buffer = s.trade_buffer.clone();
            kalshi = s.kalshi.clone();
            win = s.window.clone();
        }
        if rolled {
            info!("window roll → {:?}..{:?}", win.window_start, win.window_end);
        }

        // ---- signal inputs: our own feed by default, or the paper's exact state (MIRROR) ----
        let (ofi_1, _ofi_3, _ofi_5, buy_v, sell_v, _cnt) = compute_ofi(&buffer, now);
        let mut sigmas = compute_all_sigmas(&history, now);

        // defaults from our own Binance feed + local window/book
        let mut win = win;
        let mut book = kalshi.clone().unwrap_or_default();
        let mut price = price_opt.unwrap_or(0.0);
        let mut delta_open = price_opt.and_then(|p| win.delta_from_open(p));
        let mut delta_prev = price_opt.and_then(|p| win.delta_from_prev(p));
        let mut last_trade_exp: Option<f64> = None;

        if let Some(murl) = mcfg.url.as_deref() {
            // MIRROR: window_open / Δ / σ / book come straight from the paper engine,
            // so our (validated) f6 filter produces the SAME side + entry as the paper.
            match mirror::fetch(
                &http,
                murl,
                now_utc,
                &mcfg.session_key,
                Some(SessionSel::F6.key()), // f6 = reference shadow for the green line
            )
            .await
            {
                Some(m) if m.age_secs <= mcfg.max_age_secs => {
                    win = window::WindowState {
                        window_start: Some(m.window_start),
                        window_end: Some(m.window_end),
                        window_open_price: Some(m.window_open),
                        prev_close_price: Some(m.binance_price - m.delta_from_prev),
                    };
                    price = m.binance_price;
                    // H2 fix: key by cfg.sigma_type (f6→max30, f1→max10) so the gate's
                    // lookup can never miss and silently fall back to the local sigma.
                    insert_mirror_sigma(&mut sigmas, &cfg, m.sigma_max30);
                    // f6 reference-shadow σ — best-effort: absence only disables the
                    // shadow evaluation this tick, never the live path.
                    if let Some(ss) = m.sigma_shadow {
                        insert_mirror_sigma(&mut sigmas, &shadow_cfg, ss);
                    }
                    delta_open = Some(m.delta_from_open);
                    delta_prev = Some(m.delta_from_prev);
                    last_trade_exp = m.last_trade_expensive;
                    book = KalshiBook {
                        ticker: m.ticker,
                        yes_bid: m.yes_bid,
                        yes_bid_vol: m.yes_bid_vol,
                        yes_ask: m.yes_ask,
                        yes_ask_vol: m.yes_ask_vol,
                        no_bid: m.no_bid,
                        no_bid_vol: m.no_bid_vol,
                        no_ask: m.no_ask,
                        no_ask_vol: m.no_ask_vol,
                        last_price: None,
                        floor_strike: None,
                        ts: now,
                    };
                }
                other => {
                    // paper stale or unreachable → skip; never trade on a blind signal
                    if now - last_status_log > 10.0 {
                        last_status_log = now;
                        warn!(
                            "[{}] MIRROR {} — skipping tick (no blind trades)",
                            session_tag,
                            if other.is_some() {
                                "STALE"
                            } else {
                                "UNAVAILABLE (down, bad response, or rejected σ)"
                            }
                        );
                    }
                    dash.lock().unwrap().live.reason = "MIRROR WAIT".to_string();
                    continue;
                }
            }
        } else if price_opt.is_none() {
            continue; // own-feed mode needs a BTC price
        }

        let tau = win.tau(now_utc);
        let elapsed = win.elapsed_min(now_utc);
        let hour = win
            .window_start
            .map(|w| w.format("%H").to_string().parse::<i32>().unwrap_or(0))
            .unwrap_or(0);

        let shared = Shared {
            binance_price: Some(price),
            delta_from_open: delta_open,
            delta_from_prev: delta_prev,
            tau,
            elapsed_min: elapsed,
            sigma: sigmas.get("realized5_pmin").copied(),
            sigmas,
            ofi_1min: ofi_1,
            buy_vol_1min: buy_v,
            sell_vol_1min: sell_v,
            hour,
            yes_bid: book.yes_bid,
            yes_ask: book.yes_ask,
            yes_ask_vol: book.yes_ask_vol,
            yes_bid_vol: book.yes_bid_vol,
            no_bid: book.no_bid,
            no_ask: book.no_ask,
            no_ask_vol: book.no_ask_vol,
            no_bid_vol: book.no_bid_vol,
            last_trade_expensive: last_trade_exp,
        };

        let res = evaluate(&cfg, &shared);

        // 1-trade-per-window latch: `fired_window` holds the window we last fired in,
        // so when `win_key` changes the latch clears automatically.
        let win_key = win.window_start.map(|w| w.to_rfc3339());

        // periodic status
        if now - last_status_log > 10.0 {
            last_status_log = now;
            info!(
                "[{}] {} | btc={:.0} Δopen={:?} τ={:.1} e={:.1} σ({})={:.6} mkt={} yes_ask={:?} no_ask={:?}",
                session_tag,
                res.reason,
                price,
                shared.delta_from_open,
                tau,
                elapsed,
                cfg.sigma_type,
                display_sigma(&shared.sigmas, &cfg),
                book.ticker,
                book.yes_ask,
                book.no_ask,
            );
        }

        // ---- update dashboard live snapshot ----
        let (disp_p, disp_snr) = {
            let sig = shared
                .sigmas
                .get(&cfg.sigma_type)
                .copied()
                .filter(|v| *v > 0.0)
                .or(shared.sigma);
            match (shared.delta_from_open, sig) {
                (Some(d), Some(s)) => {
                    match p_model_classic(d, s, tau, price, cfg.kappa, cfg.tau_mode) {
                        Some((p, sn)) => (Some(p), sn),
                        None => (None, 0.0),
                    }
                }
                _ => (None, 0.0),
            }
        };
        {
            let (day_pnl, total_pnl) = {
                let s = live_state.lock().unwrap();
                (s.day_pnl, s.total_pnl)
            };
            let mut dl = dash.lock().unwrap();
            let l = &mut dl.live;
            l.day_pnl = day_pnl;
            l.total_pnl = total_pnl;
            l.market = book.ticker.clone();
            l.btc = price;
            l.delta_open = shared.delta_from_open;
            l.delta_prev = shared.delta_from_prev;
            l.tau = tau;
            l.elapsed = elapsed;
            // field NAME kept for dashboard wire-compat; value = the SELECTED session's σ
            l.sigma_max30 = display_sigma(&shared.sigmas, &cfg);
            l.p = disp_p;
            l.snr = disp_snr;
            l.reason = res.reason.clone();
            l.yes_ask = book.yes_ask;
            l.no_ask = book.no_ask;
            l.yes_bid = book.yes_bid;
            l.no_bid = book.no_bid;
            l.floor_strike = book.floor_strike;
            l.updated_iso = now_utc.to_rfc3339();
        }

        // ---- f6 REFERENCE SHADOW (the dashboard's green line): evaluate the f6 gate on
        //      the SAME mirrored state, independent of the selected live strategy, and
        //      emit its would-be (live=false, own Pending scored on real settlement)
        //      once per window. Gated on f6's OWN mirrored sigma being present — the
        //      engine's local-sigma fallback would draw a wrong-formula shadow. ----
        if lcfg.enabled
            && order_client.is_some()
            && !book.ticker.is_empty()
            && shared
                .sigmas
                .get(&shadow_cfg.sigma_type)
                .copied()
                .unwrap_or(0.0)
                > 0.0
        {
            if let Some(sfire) = evaluate(&shadow_cfg, &shared).fire {
                if twin_should_emit(twin_window.as_deref(), win_key.as_deref()) {
                    twin_window = win_key.clone();
                    emit_trigger(
                        &ledger, &pending, &shadow_cfg, &shared, &book, &win, &sfire, now,
                        now_utc, &dash, &shadow_tag,
                    );
                }
            }
        }

        // ---- trigger → place a live order. P0b: latch the window only on a non-retryable
        //      outcome (a fill, or a deliberate skip); a no-fill/error re-attempts within the
        //      window (bounded by retry_max_attempts + cooldown) instead of burning it. ----
        if let Some(fire) = res.fire {
            // reset per-window retry state when the window rolls
            if attempt_window.as_deref() != win_key.as_deref() {
                attempt_window = win_key.clone();
                attempt_count = 0;
                last_attempt_ts = f64::NEG_INFINITY;
            }
            let already = fired_window.as_deref() == win_key.as_deref();
            if !already && !book.ticker.is_empty() {
                if lcfg.enabled && order_client.is_some() {
                    if retry_gate(
                        attempt_count,
                        last_attempt_ts,
                        now,
                        lcfg.retry_max_attempts,
                        lcfg.retry_cooldown_secs,
                    ) {
                        last_attempt_ts = now;
                        let outcome = place_live(
                            order_client.as_ref().unwrap(),
                            &lcfg,
                            &live_state,
                            &ledger,
                            &pending,
                            &dash,
                            &cfg,
                            &shared,
                            &book,
                            &win,
                            &fire,
                            now,
                            now_utc,
                            &rest,
                            sel,
                            anchor,
                        )
                        .await;
                        if counts_as_attempt(outcome) {
                            attempt_count += 1;
                        }
                        if latch_decision(outcome) {
                            fired_window = win_key.clone();
                        }
                    }
                } else {
                    fired_window = win_key.clone();
                    emit_trigger(
                        &ledger, &pending, &cfg, &shared, &book, &win, &fire, now, now_utc, &dash,
                        &session_tag,
                    );
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn emit_trigger(
    ledger: &Ledger,
    pending: &Arc<Mutex<Vec<Pending>>>,
    cfg: &SessionConfig,
    shared: &Shared,
    book: &KalshiBook,
    win: &window::WindowState,
    fire: &engine::Fire,
    now: f64,
    now_utc: DateTime<Utc>,
    dash: &Arc<Mutex<Dash>>,
    session_tag: &str,
) {
    let side_str = match fire.side {
        Side::Yes => "yes",
        Side::No => "no",
    };
    let count = (cfg.stake / fire.entry).round();
    let limit_cents = (fire.entry * 100.0).round() as i64;
    let w_start = win.window_start.map(|w| w.to_rfc3339()).unwrap_or_default();
    let w_end = win.window_end.map(|w| w.to_rfc3339()).unwrap_or_default();

    ledger.append(&TriggerRecord {
        kind: "trigger",
        ts: now,
        ts_iso: now_utc.to_rfc3339(),
        session: session_tag,
        window_start: w_start,
        window_end: w_end,
        market_ticker: &book.ticker,
        action: "buy",
        order_type: "limit",
        side: side_str,
        count: count as i64,
        limit_price_cents: limit_cents,
        stake_usd: cfg.stake,
        entry: fire.entry,
        orderbook_entry: fire.orderbook_entry,
        last_trade_expensive: shared.last_trade_expensive,
        delta_from_open: shared.delta_from_open.unwrap_or(0.0),
        delta_from_prev: shared.delta_from_prev,
        binance_price: shared.binance_price.unwrap_or(0.0),
        sigma_type: &cfg.sigma_type,
        sigma_used: fire.sigma_used,
        snr: fire.snr,
        p: fire.p,
        tau: shared.tau,
        elapsed_min: shared.elapsed_min,
        yes_bid: book.yes_bid,
        yes_ask: book.yes_ask,
        no_bid: book.no_bid,
        no_ask: book.no_ask,
        floor_strike: book.floor_strike,
    });

    info!(
        "🔫 TRIGGER {} {} @ {:.2} ({} x {}c) Δ={:+.1} p={:?} snr={:.3} τ={:.1} mkt={}",
        session_tag,
        side_str.to_uppercase(),
        fire.entry,
        count,
        limit_cents,
        shared.delta_from_open.unwrap_or(0.0),
        fire.p,
        fire.snr,
        shared.tau,
        book.ticker
    );

    if let Some(w_end_dt) = win.window_end {
        pending.lock().unwrap().push(Pending {
            ticker: book.ticker.clone(),
            session: session_tag.to_string(),
            side: side_str,
            entry: fire.entry,
            count,
            stake: cfg.stake,
            window_end: w_end_dt,
            live: false,
        });
    }

    dash.lock().unwrap().triggers.push(TrigSummary {
        ts_iso: now_utc.to_rfc3339(),
        window_start: win.window_start.map(|w| w.to_rfc3339()).unwrap_or_default(),
        window_end: win.window_end.map(|w| w.to_rfc3339()).unwrap_or_default(),
        ticker: book.ticker.clone(),
        side: side_str.to_string(),
        entry: fire.entry,
        count: count as i64,
        delta: shared.delta_from_open.unwrap_or(0.0),
        p: fire.p,
        result: None,
        won: None,
        pnl: None,
        live: false, // strategy would-be (shadow twin)
    });
}

/// LIVE: place a real $STAKE IOC order for the fired trigger, record the real fill.
/// Safety: daily caps + loss-stop, price band (0.50, max_entry], count cap, never rests.
#[allow(clippy::too_many_arguments)]
async fn place_live(
    oc: &OrderClient,
    lcfg: &LiveCfg,
    live_state: &Arc<Mutex<LiveState>>,
    ledger: &Ledger,
    pending: &Arc<Mutex<Vec<Pending>>>,
    dash: &Arc<Mutex<Dash>>,
    cfg: &SessionConfig,
    shared: &Shared,
    book: &KalshiBook,
    win: &window::WindowState,
    fire: &engine::Fire,
    now: f64,
    now_utc: DateTime<Utc>,
    rest: &KalshiRest,
    sel: SessionSel,
    anchor: ExecAnchor,
) -> Outcome {
    let session_tag = sel.session_tag();
    let side_str: &'static str = match fire.side {
        Side::Yes => "yes",
        Side::No => "no",
    };
    let entry = fire.entry;
    // a context-only record for any skip/error/no-fill path (order fields set per-path below).
    // Self-contained (computes window strings on call) so the fill path stays unchanged.
    let ctx = |outcome: Outcome| {
        build_context_record(
            &session_tag,
            outcome,
            now,
            now_utc.to_rfc3339(),
            win.window_start.map(|w| w.to_rfc3339()).unwrap_or_default(),
            win.window_end.map(|w| w.to_rfc3339()).unwrap_or_default(),
            book.ticker.clone(),
            side_str,
            entry,
            fire.orderbook_entry,
            fire.p,
            shared.delta_from_open.unwrap_or(0.0),
        )
    };

    // daily reset + caps — compute the gate flags INSIDE the live_state lock, but append
    // the ledger row OUTSIDE it (never hold the state mutex across Ledger::append's mutex).
    let (cap_hit, loss_hit, day_pnl_now) = {
        let mut s = live_state.lock().unwrap();
        let today = now_utc.format("%Y-%m-%d").to_string();
        if s.day != today {
            *s = LiveState {
                day: today,
                trades_today: 0,
                day_pnl: 0.0,
                total_pnl: s.total_pnl,
                // keep the latch: a day roll doesn't un-fill the window it points at
                last_filled_window: s.last_filled_window.clone(),
            };
            save_live_state(&s);
        }
        (
            s.trades_today >= lcfg.max_trades_day,
            s.day_pnl <= -lcfg.daily_loss_stop,
            s.day_pnl,
        )
    };
    if cap_hit {
        warn!("LIVE HALT: max {} trades/day reached", lcfg.max_trades_day);
        ledger.append(&ctx(Outcome::SkipDailyCap));
        return Outcome::SkipDailyCap;
    }
    if loss_hit {
        warn!("LIVE HALT: daily loss stop (${:+.2} <= -${})", day_pnl_now, lcfg.daily_loss_stop);
        ledger.append(&ctx(Outcome::SkipLossStop));
        return Outcome::SkipLossStop;
    }
    if !(0.50 < entry && entry <= cfg.max_entry_price) {
        warn!("LIVE SKIP: entry {:.2} out of (0.50, {:.2}]", entry, cfg.max_entry_price);
        ledger.append(&ctx(Outcome::SkipBand));
        return Outcome::SkipBand;
    }
    // coid prefix attributes the fill to the SELECTED strategy on Kalshi's side too
    let coid = format!("{}{}-{}", sel.coid_prefix(), now_utc.timestamp_millis(), side_str);
    let v2side = match fire.side {
        Side::Yes => "bid",
        Side::No => "ask",
    };
    // ---- execution fork (EXEC_ANCHOR): how the IOC limit is priced ----
    // Each arm returns (exec_entry TELEMETRY Option, first_limit_price, requoted,
    // requote_limit_price, count, final price, status, resp, lat) for the shared tail.
    let (exec_entry, first_limit_price, requoted, requote_limit_price, count, price, status, resp, lat) = match anchor {
        ExecAnchor::Ask => {
            // LEGACY (verbatim): FRESH orderbook at order time — size + price off the CURRENT
            // real ask (not the ~0.5s-old mirrored paper ask). Falls back to the signal entry
            // on any error. Chases the ask; re-quote crosses deeper on a first no-fill.
            let ee = match rest.get_orderbook(&book.ticker, 10).await {
                Ok(raw) => {
                    let fb = KalshiBook::derive(&book.ticker, &raw, None, now);
                    let a = match fire.side {
                        Side::Yes => fb.yes_ask,
                        Side::No => fb.no_ask,
                    };
                    a.filter(|v| *v > 0.50 && *v <= 0.98).unwrap_or(entry)
                }
                Err(_) => entry,
            };
            let mut price = match fire.side {
                Side::Yes => (ee + lcfg.price_buf).min(0.99),
                Side::No => ((1.0 - ee) - lcfg.price_buf).max(0.01),
            };
            let first_limit_price = price;
            let mut requoted = false;
            let mut requote_limit_price: Option<f64> = None;
            let mut count = (lcfg.stake / ee).round();
            if count < 1.0 {
                count = 1.0;
            }
            if count > lcfg.max_count as f64 {
                count = lcfg.max_count as f64;
            }
            let (mut status, mut resp, mut lat) =
                match oc.create_ioc(&book.ticker, v2side, count, price, coid.clone()).await {
                    Ok(x) => x,
                    Err(e) => {
                        warn!("LIVE order error: {e}");
                        let mut r = ctx(Outcome::OrderError);
                        r.exec_entry = Some(ee);
                        r.first_limit_price = Some(first_limit_price);
                        ledger.append(&r);
                        return Outcome::OrderError;
                    }
                };
            // RE-QUOTE once on a no-fill: the book moved past our limit in the order RTT.
            // Cross deeper — bounded by requote_buf; applies only to the failed order.
            if status == 201 && resp.fill() <= 0.0 && lcfg.requote_buf > lcfg.price_buf {
                price = match fire.side {
                    Side::Yes => (ee + lcfg.requote_buf).min(0.97),
                    Side::No => ((1.0 - ee) - lcfg.requote_buf).max(0.03),
                };
                requoted = true;
                requote_limit_price = Some(price);
                info!(
                    "LIVE RE-QUOTE {} {} {}x @ {:.2} (1st no-fill → deeper cross)",
                    book.ticker,
                    side_str.to_uppercase(),
                    count,
                    price
                );
                if let Ok((s2, r2, l2)) = oc
                    .create_ioc(&book.ticker, v2side, count, price, format!("{coid}-rq"))
                    .await
                {
                    status = s2;
                    resp = r2;
                    lat = l2;
                }
            }
            (Some(ee), first_limit_price, requoted, requote_limit_price, count, price, status, resp, lat)
        }
        ExecAnchor::Signal => {
            // SIGNAL ANCHOR: limit = paper/signal price + crossing allowance; sizing off the
            // signal too. NO pre-order book GET in the critical path — the order fires
            // immediately (~150-250ms earlier than legacy); the book is fetched CONCURRENTLY,
            // bounded to TELEMETRY_TIMEOUT_MS, purely for exec_entry telemetry (fail-open —
            // its failure can never block, delay past the bound, or alter the order).
            // NO re-quote (anti-chase): a P0b retry re-anchors at this same fixed limit.
            let price = match fire.side {
                Side::Yes => signal_yes_limit(entry, lcfg.price_buf),
                Side::No => signal_no_limit(entry, lcfg.price_buf),
            };
            let count = signal_count(lcfg.stake, entry, lcfg.max_count);
            let (order_res, book_res) = tokio::join!(
                oc.create_ioc(&book.ticker, v2side, count, price, coid.clone()),
                tokio::time::timeout(
                    Duration::from_millis(TELEMETRY_TIMEOUT_MS),
                    rest.get_orderbook(&book.ticker, 10),
                ),
            );
            let exec_entry = book_res.ok().and_then(|r| r.ok()).and_then(|raw| {
                let fb = KalshiBook::derive(&book.ticker, &raw, None, now);
                filtered_ask(match fire.side {
                    Side::Yes => fb.yes_ask,
                    Side::No => fb.no_ask,
                })
            });
            let (status, resp, lat) = match order_res {
                Ok(x) => x,
                Err(e) => {
                    warn!("LIVE order error: {e}");
                    let mut r = ctx(Outcome::OrderError);
                    r.exec_entry = exec_entry;
                    r.first_limit_price = Some(price);
                    ledger.append(&r);
                    return Outcome::OrderError;
                }
            };
            (exec_entry, price, false, None, count, price, status, resp, lat)
        }
    };
    if status != 201 {
        warn!("LIVE order rejected HTTP {}", status);
        let mut r = ctx(Outcome::Rejected);
        r.exec_entry = exec_entry;
        r.first_limit_price = Some(first_limit_price);
        r.requote = Some(requoted);
        r.requote_limit_price = requote_limit_price;
        r.latency_ms = Some(lat as i64);
        ledger.append(&r);
        return Outcome::Rejected;
    }
    let fill = resp.fill();
    if fill <= 0.0 {
        info!(
            "LIVE NO-FILL {} {} {}x @ limit {:.2} (rem {})",
            book.ticker, side_str.to_uppercase(), count, price, resp.remaining_count
        );
        let rem = resp.remaining_count.parse::<f64>().unwrap_or(0.0);
        let mut r = ctx(Outcome::Nofill);
        r.exec_entry = exec_entry;
        r.first_limit_price = Some(first_limit_price);
        r.requote = Some(requoted);
        r.requote_limit_price = requote_limit_price;
        r.remaining_count = Some(rem as i64);
        r.latency_ms = Some(lat as i64);
        ledger.append(&r);
        return Outcome::Nofill; // nothing filled -> no position
    }
    // effective per-contract entry of the side actually bought
    let eff = match fire.side {
        Side::Yes => resp.avg_price().unwrap_or(entry),
        Side::No => 1.0 - resp.avg_price().unwrap_or(1.0 - entry),
    };
    let eff = (eff * 10000.0).round() / 10000.0;
    let fee = resp.fee();
    {
        let mut s = live_state.lock().unwrap();
        record_fill_state(&mut s, win.window_start.map(|w| w.to_rfc3339()));
        save_live_state(&s);
    }
    let w_start = win.window_start.map(|w| w.to_rfc3339()).unwrap_or_default();
    let w_end = win.window_end.map(|w| w.to_rfc3339()).unwrap_or_default();
    let remaining = resp.remaining_count.parse::<f64>().unwrap_or(0.0);
    let outcome = if remaining > 0.0 {
        Outcome::Partial
    } else {
        Outcome::Filled
    };
    // build_fill_record signature is FROZEN (its 4 tests untouched): pass a concrete f64
    // and overwrite with the true Option afterwards — ask mode is always Some (identical),
    // signal mode may be None when the concurrent telemetry fetch missed the bound.
    let mut fr = build_fill_record(
        &session_tag,
        outcome,
        now,
        now_utc.to_rfc3339(),
        w_start.clone(),
        w_end.clone(),
        book.ticker.clone(),
        side_str,
        entry, // fire.entry — the paper/signal entry
        fire.orderbook_entry,
        fire.p,
        shared.delta_from_open.unwrap_or(0.0),
        exec_entry.unwrap_or(entry),
        first_limit_price,
        requoted,
        requote_limit_price,
        remaining as i64,
        fill,
        eff,
        fee,
        lat as i64,
        &resp.order_id,
    );
    fr.exec_entry = exec_entry;
    ledger.append(&fr);
    dash.lock().unwrap().triggers.push(TrigSummary {
        ts_iso: now_utc.to_rfc3339(),
        window_start: w_start,
        window_end: w_end,
        ticker: book.ticker.clone(),
        side: side_str.to_string(),
        entry: eff,
        count: fill as i64,
        delta: shared.delta_from_open.unwrap_or(0.0),
        p: fire.p,
        result: None,
        won: None,
        pnl: None,
        live: true, // REAL fill — raw $5 money
    });
    if let Some(we) = win.window_end {
        pending.lock().unwrap().push(Pending {
            ticker: book.ticker.clone(),
            session: session_tag.clone(),
            side: side_str,
            entry: eff,
            count: fill,
            stake: lcfg.stake,
            window_end: we,
            live: true,
        });
    }
    info!(
        "🟢 LIVE FILLED {} {} {}x @ {:.3} (lat {}ms, fee ${:.3})",
        book.ticker, side_str.to_uppercase(), fill, eff, lat, fee
    );
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gate() -> GateSnapshot {
        // a "passing" snapshot: caps OK, in band, order 201, full fill
        GateSnapshot {
            trades_today: 0,
            max_trades_day: 96,
            day_pnl: 0.0,
            daily_loss_stop: 50.0,
            entry: 0.70,
            max_entry_price: 0.92,
            order_err: false,
            status: 201,
            fill: 7.0,
            remaining_count: 0,
        }
    }

    #[test]
    fn classify_fill_partial_nofill() {
        assert_eq!(classify_outcome(&gate()), Outcome::Filled);
        let mut g = gate();
        g.remaining_count = 3;
        assert_eq!(classify_outcome(&g), Outcome::Partial);
        let mut g = gate();
        g.fill = 0.0;
        assert_eq!(classify_outcome(&g), Outcome::Nofill);
    }

    #[test]
    fn classify_skips_and_errors() {
        let mut g = gate();
        g.trades_today = 96;
        assert_eq!(classify_outcome(&g), Outcome::SkipDailyCap);

        let mut g = gate();
        g.day_pnl = -50.0;
        assert_eq!(classify_outcome(&g), Outcome::SkipLossStop);

        let mut g = gate();
        g.entry = 0.40;
        assert_eq!(classify_outcome(&g), Outcome::SkipBand);

        let mut g = gate();
        g.order_err = true;
        assert_eq!(classify_outcome(&g), Outcome::OrderError);

        let mut g = gate();
        g.status = 400;
        assert_eq!(classify_outcome(&g), Outcome::Rejected);
    }

    #[test]
    fn classify_band_edges() {
        // entry == 0.50 → not (0.50 < entry) → SkipBand
        let mut g = gate();
        g.entry = 0.50;
        assert_eq!(classify_outcome(&g), Outcome::SkipBand);
        // entry == max_entry → allowed (entry <= max) → not band
        let mut g = gate();
        g.entry = 0.92;
        assert_eq!(classify_outcome(&g), Outcome::Filled);
        // just above max → SkipBand
        let mut g = gate();
        g.entry = 0.93;
        assert_eq!(classify_outcome(&g), Outcome::SkipBand);
    }

    #[test]
    fn classify_precedence_cap_wins() {
        // daily-cap AND loss-stop AND band all tripped → cap wins (checked first)
        let mut g = gate();
        g.trades_today = 96;
        g.day_pnl = -100.0;
        g.entry = 0.40;
        assert_eq!(classify_outcome(&g), Outcome::SkipDailyCap);
    }

    #[test]
    fn decompose_gap_basic_and_identity() {
        // paper said 0.51, fresh ask was 0.69, filled at 0.75
        let (gap, drift, walk) = decompose_gap(0.51, 0.69, 0.75);
        assert!((gap - 0.24).abs() < 1e-9);
        assert!((drift - 0.18).abs() < 1e-9);
        assert!((walk - 0.06).abs() < 1e-9);
        // identity always holds
        assert!((drift + walk - gap).abs() < 1e-9);
    }

    #[test]
    fn decompose_gap_degenerate_when_exec_equals_signal() {
        let (gap, drift, walk) = decompose_gap(0.70, 0.70, 0.72);
        assert!((drift).abs() < 1e-9);
        assert!((walk - gap).abs() < 1e-9);
    }

    #[test]
    fn is_dashboard_trigger_filter() {
        // legacy (no outcome) counts as a fill
        assert!(is_dashboard_trigger(None));
        assert!(is_dashboard_trigger(Some(Outcome::Filled)));
        assert!(is_dashboard_trigger(Some(Outcome::Partial)));
        for o in [
            Outcome::Nofill,
            Outcome::SkipDailyCap,
            Outcome::SkipLossStop,
            Outcome::SkipBand,
            Outcome::OrderError,
            Outcome::Rejected,
        ] {
            assert!(!is_dashboard_trigger(Some(o)), "{o:?} must not count");
        }
    }

    // a simple-fill record: paper entry 0.64, fresh ask 0.66, filled 7 @ 0.70, no re-quote
    fn fill_rec() -> LiveTriggerRecord {
        build_fill_record(
            "f6_wait270_shadow",
            Outcome::Filled,
            1.0,
            "2026-06-28T12:00:00+00:00".into(),
            "2026-06-28T11:45:00+00:00".into(),
            "2026-06-28T12:00:00+00:00".into(),
            "KXBTC15M-26JUN281145-45".into(),
            "yes",
            0.64, // signal_entry
            0.66, // orderbook_entry
            Some(0.73),
            113.34,
            0.66,  // exec_entry
            0.72,  // first_limit_price
            false, // requote
            None,  // requote_limit_price
            0,     // remaining_count
            7.0,   // fill
            0.70,  // eff
            0.0147,
            119,
            "ord-1",
        )
    }

    #[test]
    fn build_fill_record_full_decomposition() {
        let v: serde_json::Value = serde_json::to_value(fill_rec()).unwrap();
        // legacy `entry` key carries eff; signal_entry is the paper price
        assert_eq!(v["entry"], 0.70);
        assert_eq!(v["signal_entry"], 0.64);
        assert_eq!(v["exec_entry"], 0.66);
        assert_eq!(v["orderbook_entry"], 0.66);
        assert_eq!(v["first_limit_price"], 0.72);
        assert_eq!(v["requote"], false);
        assert_eq!(v["fill"], 7.0);
        assert_eq!(v["count"], 7);
        assert_eq!(v["latency_ms"], 119);
        assert_eq!(v["order_id"], "ord-1");
        // gap is computable offline: gap = eff - signal_entry, drift + walk == gap
        let (gap, drift, walk) = decompose_gap(0.64, 0.66, 0.70);
        assert!((gap - 0.06).abs() < 1e-9);
        assert!((drift + walk - gap).abs() < 1e-9);
    }

    #[test]
    fn build_fill_record_back_compat_keys() {
        let s = serde_json::to_string(&fill_rec()).unwrap();
        assert!(s.contains("\"kind\":\"trigger\""));
        assert!(s.contains("\"live\":true"));
        assert!(s.contains("\"outcome\":\"filled\""));
        // the resolver/dashboard read entry/side/count/delta_from_open — all present
        assert!(s.contains("\"entry\":0.7"));
        assert!(s.contains("\"side\":\"yes\""));
    }

    #[test]
    fn build_fill_record_partial_count_is_fill() {
        let mut r = fill_rec();
        r = build_fill_record(
            "f6_wait270_shadow",
            Outcome::Partial,
            r.ts,
            r.ts_iso.clone(),
            r.window_start.clone(),
            r.window_end.clone(),
            r.market_ticker.clone(),
            "yes",
            0.64,
            0.66,
            Some(0.73),
            113.34,
            0.66,
            0.72,
            false,
            None,
            3, // remaining_count > 0
            4.0, // fill
            0.70,
            0.01,
            119,
            "ord-2",
        );
        let v: serde_json::Value = serde_json::to_value(&r).unwrap();
        assert_eq!(v["outcome"], "partial");
        assert_eq!(v["remaining_count"], 3);
        assert_eq!(v["count"], 4); // count == fill as i64
    }

    #[test]
    fn build_fill_record_requote_has_both_prices() {
        let r = build_fill_record(
            "f6_wait270_shadow",
            Outcome::Filled,
            1.0,
            "t".into(),
            "ws".into(),
            "we".into(),
            "tkr".into(),
            "no",
            0.55,
            0.55,
            None,
            -90.0,
            0.58,
            0.36,       // first limit (NO side)
            true,       // requote
            Some(0.30), // requote limit
            0,
            8.0,
            0.62,
            0.015,
            300,
            "",
        );
        let v: serde_json::Value = serde_json::to_value(&r).unwrap();
        assert_eq!(v["requote"], true);
        assert_eq!(v["first_limit_price"], 0.36);
        assert_eq!(v["requote_limit_price"], 0.30);
        assert_ne!(v["first_limit_price"], v["requote_limit_price"]);
        // empty order_id is omitted, not written as ""
        assert!(v.get("order_id").is_none());
    }

    fn ctx_rec(outcome: Outcome) -> LiveTriggerRecord {
        build_context_record(
            "f6_wait270_shadow",
            outcome,
            1.0,
            "2026-06-28T12:00:00+00:00".into(),
            "ws".into(),
            "we".into(),
            "KXBTC15M-26JUN281145-45".into(),
            "no",
            0.58, // signal_entry
            0.58, // orderbook_entry
            Some(0.71),
            -77.0,
        )
    }

    #[test]
    fn build_context_record_skip_paths() {
        for o in [
            Outcome::SkipDailyCap,
            Outcome::SkipLossStop,
            Outcome::SkipBand,
        ] {
            let v: serde_json::Value = serde_json::to_value(ctx_rec(o)).unwrap();
            assert_eq!(v["kind"], "trigger");
            assert_eq!(v["outcome"], serde_json::to_value(o).unwrap());
            // no fill happened: entry carries the signal entry, eff/exec/fill/latency absent
            assert_eq!(v["entry"], 0.58);
            assert_eq!(v["signal_entry"], 0.58);
            assert!(v.get("eff").is_none());
            assert!(v.get("exec_entry").is_none());
            assert!(v.get("fill").is_none());
            assert!(v.get("latency_ms").is_none());
            // context kept for diffing
            assert_eq!(v["orderbook_entry"], 0.58);
            assert_eq!(v["delta_from_open"], -77.0);
        }
    }

    #[test]
    fn context_record_nofill_shape() {
        // mirror what place_live sets on the no-fill path
        let mut r = ctx_rec(Outcome::Nofill);
        r.exec_entry = Some(0.60);
        r.first_limit_price = Some(0.34);
        r.requote = Some(true);
        r.requote_limit_price = Some(0.30);
        r.remaining_count = Some(7);
        r.latency_ms = Some(426);
        let v: serde_json::Value = serde_json::to_value(&r).unwrap();
        assert_eq!(v["outcome"], "nofill");
        assert!(v.get("eff").is_none()); // never filled
        assert!(v.get("fee").is_none());
        assert_eq!(v["exec_entry"], 0.60);
        assert_eq!(v["remaining_count"], 7);
        assert_eq!(v["requote"], true);
        assert_eq!(v["latency_ms"], 426);
    }

    #[test]
    fn outcome_position_semantics() {
        // only fills/partials are positions (and reach the pending/dashboard push)
        assert!(Outcome::Filled.is_position());
        assert!(Outcome::Partial.is_position());
        for o in [
            Outcome::Nofill,
            Outcome::SkipDailyCap,
            Outcome::SkipLossStop,
            Outcome::SkipBand,
            Outcome::OrderError,
            Outcome::Rejected,
        ] {
            assert!(!o.is_position(), "{o:?} is not a position");
        }
    }

    #[test]
    fn loader_filters_outcome() {
        // legacy (no outcome) + filled + partial should count; nofill + skip should not.
        let lines = [
            r#"{"kind":"trigger","ts_iso":"t","market_ticker":"A","side":"yes","entry":0.7,"count":7,"delta_from_open":1.0}"#,
            r#"{"kind":"trigger","outcome":"filled","market_ticker":"B","side":"yes","entry":0.7,"count":7}"#,
            r#"{"kind":"trigger","outcome":"partial","market_ticker":"C","side":"no","entry":0.6,"count":4}"#,
            r#"{"kind":"trigger","outcome":"nofill","market_ticker":"D","side":"yes","entry":0.6}"#,
            r#"{"kind":"trigger","outcome":"skip_daily_cap","market_ticker":"E","side":"no","entry":0.6}"#,
            r#"{"kind":"trigger","outcome":"bogus_tag","market_ticker":"F","side":"no","entry":0.6}"#,
            r#"{"kind":"resolve","market_ticker":"B","side":"yes","entry":0.7,"result":"yes","won":true,"pnl_usd":2.5}"#,
        ];
        let mut d = Dash::default();
        for l in lines {
            replay_line_into(&mut d, l);
        }
        // A (legacy), B (filled), C (partial) → 3; D/E/F (nofill/skip/unknown) excluded
        assert_eq!(d.triggers.len(), 3);
    }

    #[test]
    fn loader_legacy_no_outcome_counts() {
        let mut d = Dash::default();
        replay_line_into(
            &mut d,
            r#"{"kind":"trigger","market_ticker":"A","side":"yes","entry":0.7,"count":7}"#,
        );
        assert_eq!(d.triggers.len(), 1);
    }

    #[test]
    fn loader_malformed_line_skipped() {
        let mut d = Dash::default();
        replay_line_into(&mut d, r#"{"kind":"trigger","outcome":"filled","market_ticker":"A","side":"yes","entry":0.7}"#);
        replay_line_into(&mut d, "this is not json {{{");
        replay_line_into(&mut d, r#"{"kind":"trigger","outcome":"filled","market_ticker":"B","side":"no","entry":0.6}"#);
        // both valid fills loaded; the garbage line did not abort or panic
        assert_eq!(d.triggers.len(), 2);
    }

    #[test]
    fn total_pnl_ignores_telemetry() {
        let content = [
            r#"{"kind":"trigger","outcome":"filled","entry":0.7}"#,
            r#"{"kind":"trigger","outcome":"nofill","entry":0.7}"#,
            r#"{"kind":"resolve","pnl_usd":2.5}"#,
            r#"{"kind":"resolve","pnl_usd":-1.0}"#,
        ]
        .join("\n");
        // only the two resolves count: 2.5 + (-1.0) = 1.5
        assert!((sum_resolve_pnl(&content) - 1.5).abs() < 1e-9);
    }

    #[test]
    fn trigsummary_shape_unchanged() {
        // the dashboard /shadow loader still deserializes Vec<TrigSummary> (serde(default) fields)
        let v: Vec<TrigSummary> =
            serde_json::from_str(r#"[{"ticker":"X","side":"yes","entry":0.7}]"#).unwrap();
        assert_eq!(v.len(), 1);
    }

    #[test]
    fn resolve_max_entry_override() {
        assert_eq!(resolve_max_entry(0.92, Some("0.85".into())), 0.85); // valid override
        assert_eq!(resolve_max_entry(0.92, None), 0.92); // unset -> default
        assert_eq!(resolve_max_entry(0.92, Some("junk".into())), 0.92); // unparsable -> default
        assert_eq!(resolve_max_entry(0.92, Some("1.5".into())), 0.92); // > 0.99 -> default
        assert_eq!(resolve_max_entry(0.92, Some("0.40".into())), 0.92); // <= 0.5 -> default
        assert_eq!(resolve_max_entry(0.92, Some("0.99".into())), 0.99); // upper edge ok
    }

    // ---- P0b: latch / retry ----
    #[test]
    fn counts_as_attempt_and_latch_tables() {
        for o in [Outcome::Nofill, Outcome::OrderError, Outcome::Rejected] {
            assert!(counts_as_attempt(o), "{o:?} is a retryable attempt");
            assert!(!latch_decision(o), "{o:?} must NOT latch (so it can retry)");
        }
        for o in [
            Outcome::Filled,
            Outcome::Partial,
            Outcome::SkipDailyCap,
            Outcome::SkipLossStop,
            Outcome::SkipBand,
        ] {
            assert!(!counts_as_attempt(o), "{o:?} is not a retry");
            assert!(latch_decision(o), "{o:?} latches the window");
        }
    }

    #[test]
    fn retry_gate_budget_and_cooldown() {
        // budget: blocked once attempt_count reaches n
        assert!(retry_gate(0, f64::NEG_INFINITY, 1.0, 2, 3.0));
        assert!(!retry_gate(2, 0.0, 100.0, 2, 3.0)); // exhausted
        // cooldown: must wait c seconds since last attempt
        assert!(!retry_gate(1, 10.0, 10.3, 2, 3.0)); // 0.3s < 3s
        assert!(retry_gate(1, 10.0, 13.0, 2, 3.0)); // 3.0s == c, inclusive
        // backwards clock -> false (safe)
        assert!(!retry_gate(0, 10.0, 9.0, 2, 3.0));
        // c==0 -> always passes on the time axis
        assert!(retry_gate(0, 5.0, 5.0, 1, 0.0));
    }

    /// Simulate one window's order loop using only the pure helpers (mirrors signal_loop).
    /// `outcomes[k]` is what the k-th placed order returns. Returns (orders_placed, latched).
    fn sim_window(outcomes: &[Outcome], n: i64, c: f64) -> (usize, bool) {
        let mut attempt_count = 0i64;
        let mut last_attempt_ts = f64::NEG_INFINITY;
        let mut latched = false;
        let mut placed = 0usize;
        let mut now = 0.0;
        for _ in 0..60 {
            // ~18s of 0.3s ticks
            now += 0.3;
            if latched {
                break;
            }
            if retry_gate(attempt_count, last_attempt_ts, now, n, c) {
                last_attempt_ts = now;
                let o = outcomes[placed.min(outcomes.len() - 1)];
                placed += 1;
                if counts_as_attempt(o) {
                    attempt_count += 1;
                }
                if latch_decision(o) {
                    latched = true;
                }
            }
        }
        (placed, latched)
    }

    #[test]
    fn retry_sequence_default_n2() {
        // immediate fill -> 1 order, latched
        assert_eq!(sim_window(&[Outcome::Filled], 2, 3.0), (1, true));
        // no-fill then fill -> 2 orders, latched (the bug fix: window NOT burned on no-fill)
        assert_eq!(sim_window(&[Outcome::Nofill, Outcome::Filled], 2, 3.0), (2, true));
        // two no-fills -> 2 attempts then exhausted, never latched, no 3rd order
        assert_eq!(sim_window(&[Outcome::Nofill, Outcome::Nofill], 2, 3.0), (2, false));
        // a skip latches immediately (no retry spam)
        assert_eq!(sim_window(&[Outcome::SkipDailyCap], 2, 3.0), (1, true));
        assert_eq!(sim_window(&[Outcome::SkipBand], 2, 3.0), (1, true));
    }

    #[test]
    fn nfr4_n1_c0_equals_legacy() {
        // N=1, C=0 == today's one-shot-per-window: a no-fill never retries.
        assert_eq!(sim_window(&[Outcome::Nofill, Outcome::Filled], 1, 0.0), (1, false));
        assert_eq!(sim_window(&[Outcome::Filled], 1, 0.0), (1, true));
    }

    #[test]
    fn at_most_one_fill_per_window() {
        // even if place_live could be called again, a fill latches and stops further orders
        assert_eq!(sim_window(&[Outcome::Filled, Outcome::Filled], 5, 0.0), (1, true));
    }
    // ---- slice 4: strategy selection + sigma-key wiring (live-f1-strategy) ----

    // TC-10.2/10.3: select_session matrix — unset/empty -> silent f6; known -> exact;
    // unknown -> f6 WITH a warning naming the bad value and the accepted set.
    #[test]
    fn select_session_matrix() {
        assert_eq!(select_session(None), (SessionSel::F6, None));
        assert_eq!(select_session(Some("")), (SessionSel::F6, None));
        assert_eq!(select_session(Some("   ")), (SessionSel::F6, None));
        assert_eq!(select_session(Some("f6_wait270")), (SessionSel::F6, None));
        assert_eq!(select_session(Some("f1_d50cap75")), (SessionSel::F1, None));
        assert_eq!(select_session(Some(" f1_d50cap75 ")), (SessionSel::F1, None));
        let (sel, warn) = select_session(Some("f7_foo"));
        assert_eq!(sel, SessionSel::F6);
        let w = warn.expect("unknown SESSION must warn");
        assert!(w.contains("f7_foo") && w.contains("f6_wait270") && w.contains("f1_d50cap75"));
        let (sel, warn) = select_session(Some("F1_D50CAP75")); // case-sensitive
        assert_eq!(sel, SessionSel::F6);
        assert!(warn.is_some());
    }

    // TC-4.1/4.3: the mirrored sigma lands under the SELECTED session's sigma_type key.
    #[test]
    fn insert_mirror_sigma_keys_by_sigma_type() {
        let mut m = std::collections::HashMap::new();
        insert_mirror_sigma(&mut m, &SessionConfig::f1_d50cap75(), 0.0002);
        assert_eq!(m.get("max10"), Some(&0.0002));
        assert_eq!(m.get("max30"), None);
        let mut m = std::collections::HashMap::new();
        insert_mirror_sigma(&mut m, &SessionConfig::f6_wait270(), 0.0005);
        assert_eq!(m.get("max30"), Some(&0.0005)); // legacy behavior identical
    }

    // Shared fixture: F1-passing signal (delta 60 >= 50, elapsed 3.5 >= 3.0, entry 0.85
    // <= 0.92; with sigma 0.0002 @ price 100k tau 2.0 -> snr 1.5, p=phi(0.6)~0.726 >= 0.65).
    fn f1_shared(local_sigma: Option<f64>) -> Shared {
        Shared {
            binance_price: Some(100_000.0),
            delta_from_open: Some(60.0),
            delta_from_prev: Some(30.0),
            tau: 2.0,
            elapsed_min: 3.5,
            sigma: local_sigma,
            yes_ask: Some(0.85),
            no_ask: Some(0.10),
            ..Default::default()
        }
    }

    // TC-4.2 (CRITICAL, merge blocker): with the mirrored sigma under the CORRECT key
    // ("max10"), the F1 gate decision is INVARIANT to the local fallback sigma — the
    // engine's .or(s.sigma) path is provably never exercised.
    #[test]
    fn f1_gate_invariant_to_local_sigma_fallback() {
        let cfg = SessionConfig::f1_d50cap75();
        let mut results = vec![];
        for local in [None, Some(1e-6), Some(0.005), Some(999.0)] {
            let mut s = f1_shared(local);
            insert_mirror_sigma(&mut s.sigmas, &cfg, 0.0002);
            let r = evaluate(&cfg, &s);
            results.push((r.reason.clone(), r.fire.map(|f| (f.side, f.entry))));
        }
        for w in results.windows(2) {
            assert_eq!(w[0], w[1], "gate decision must not depend on local sigma");
        }
        // and it actually fires on the mirrored sigma
        assert_eq!(results[0].0, "BUY YES");
        assert_eq!(results[0].1, Some((Side::Yes, 0.85)));
    }

    // TC-4.5: pre-fix defect pin — sigma left under "max30" while F1 wants "max10"
    // makes the decision DEPEND on the local sigma (fires on a tiny one, P-LOW on a
    // big one). This is the silent-wrong-formula failure the fix eliminates.
    #[test]
    fn f1_gate_with_wrong_key_depends_on_local_sigma() {
        let cfg = SessionConfig::f1_d50cap75();
        let mut s = f1_shared(Some(0.0002)); // favorable local sigma
        s.sigmas.insert("max30".to_string(), 0.0002); // OLD bug: wrong key
        assert_eq!(evaluate(&cfg, &s).reason, "BUY YES");
        let mut s = f1_shared(Some(0.005)); // unfavorable local sigma
        s.sigmas.insert("max30".to_string(), 0.0002);
        assert!(evaluate(&cfg, &s).reason.starts_with("P-LOW"));
    }

    // TC-4.6: operator displays keyed by the selected sigma_type — nonzero under F1.
    #[test]
    fn display_sigma_keyed_by_selected_type() {
        let cfg1 = SessionConfig::f1_d50cap75();
        let cfg6 = SessionConfig::f6_wait270();
        let mut m = std::collections::HashMap::new();
        m.insert("max10".to_string(), 0.0003);
        assert_eq!(display_sigma(&m, &cfg1), 0.0003);
        assert_eq!(display_sigma(&m, &cfg6), 0.0); // no max30 present
    }
    // ---- slice 5: session attribution threading (live-f1-strategy) ----

    // TC-1.3/TC-5.2/TC-7.7: ledger records carry the SELECTED session tag — F1 rows
    // must never be mislabeled f6 (replay, dashboards and analytics key on this field).
    #[test]
    fn records_tagged_by_selected_session() {
        let r = build_fill_record(
            "f1_d50cap75_shadow", Outcome::Filled, 1.0, "t".into(), "ws".into(),
            "we".into(), "tkr".into(), "yes", 0.8, 0.8, None, 60.0,
            0.8, 0.86, false, None, 0, 6.0, 0.8, 0.01, 100, "o",
        );
        assert_eq!(serde_json::to_value(&r).unwrap()["session"], "f1_d50cap75_shadow");
        let c = build_context_record(
            "f1_d50cap75_shadow", Outcome::Nofill, 1.0, "t".into(), "ws".into(),
            "we".into(), "tkr".into(), "no", 0.7, 0.7, None, -60.0,
        );
        assert_eq!(serde_json::to_value(&c).unwrap()["session"], "f1_d50cap75_shadow");
        // coid shape used by place_live: {prefix}{millis}-{side}
        let coid = format!("{}{}-{}", SessionSel::F1.coid_prefix(), 123i64, "yes");
        assert!(coid.starts_with("f1-"));
        let coid = format!("{}{}-{}", SessionSel::F6.coid_prefix(), 123i64, "yes");
        assert!(coid.starts_with("f6-"));
    }
    // ---- slice 6: restart double-order latch (live-f1-strategy) ----

    // TC-19.1: legacy live_state.json without the new field parses cleanly to None —
    // the prod state file must survive the upgrade.
    #[test]
    fn live_state_serde_back_compat() {
        let legacy = r#"{"day":"2026-06-30","trades_today":59,"day_pnl":-34.47,"total_pnl":-131.3}"#;
        let s: LiveState = serde_json::from_str(legacy).unwrap();
        assert_eq!(s.last_filled_window, None);
        assert_eq!(s.trades_today, 59);
        // and roundtrips with the field set
        let mut s2 = s.clone();
        s2.last_filled_window = Some("2026-07-05T12:00:00+00:00".to_string());
        let j = serde_json::to_string(&s2).unwrap();
        let back: LiveState = serde_json::from_str(&j).unwrap();
        assert_eq!(back.last_filled_window.as_deref(), Some("2026-07-05T12:00:00+00:00"));
    }

    // TC-19.2: a confirmed fill records the filled window (and bumps the day counter).
    #[test]
    fn record_fill_state_sets_latch() {
        let mut s = LiveState::default();
        record_fill_state(&mut s, Some("W1".to_string()));
        assert_eq!(s.trades_today, 1);
        assert_eq!(s.last_filled_window.as_deref(), Some("W1"));
    }

    // TC-19.3/19.4/19.6: the seeded latch skips ONLY the same still-open window; any
    // other (past/future) window never matches, so trading resumes normally.
    #[test]
    fn seeded_latch_blocks_only_same_window() {
        let fired_window: Option<String> = Some("2026-07-05T12:00:00+00:00".to_string());
        let same = Some("2026-07-05T12:00:00+00:00");
        let next = Some("2026-07-05T12:15:00+00:00");
        let much_later = Some("2026-07-05T13:00:00+00:00");
        assert!(fired_window.as_deref() == same); // already fired -> skip
        assert!(fired_window.as_deref() != next); // latch clear
        assert!(fired_window.as_deref() != much_later);
        // no persisted fill (legacy state) -> nothing blocked
        let none_latch: Option<String> = None;
        assert!(none_latch.as_deref() != same);
    }
    // ---- security-audit hardening (post-review) ----

    // SUBACCOUNT must parse strictly: garbage refuses (silent 0 would route real
    // orders to the PRIMARY account and defeat subaccount isolation).
    #[test]
    fn parse_subaccount_strict() {
        assert_eq!(parse_subaccount(None), Ok(0));
        assert_eq!(parse_subaccount(Some("")), Ok(0));
        assert_eq!(parse_subaccount(Some("  ")), Ok(0));
        assert_eq!(parse_subaccount(Some("0")), Ok(0));
        assert_eq!(parse_subaccount(Some("1")), Ok(1));
        assert_eq!(parse_subaccount(Some(" 1 ")), Ok(1));
        assert!(parse_subaccount(Some("1x")).is_err());
        assert!(parse_subaccount(Some("-1")).is_err());
        assert!(parse_subaccount(Some("one")).is_err());
    }
    // ---- slice 1 (dashboard-f1-live-line): replay live flags + retain hardening ----

    // TC-10.x: mixed pre/post-feature JSONL replays with correct flags and each
    // resolve attaching to its OWN row even at eff == signal_entry, both orders.
    #[test]
    fn replay_mixed_ledger_split_and_dual_resolve() {
        let twin = r#"{"kind":"trigger","ts_iso":"t1","window_start":"W1","window_end":"W2","market_ticker":"T","side":"yes","entry":0.85,"count":112,"delta_from_open":60.0,"p":0.7}"#;
        let livefill = r#"{"kind":"trigger","outcome":"filled","live":true,"ts_iso":"t2","window_start":"W1","window_end":"W2","market_ticker":"T","side":"yes","entry":0.85,"count":6,"delta_from_open":60.0,"p":0.7}"#;
        let res_live = r#"{"kind":"resolve","market_ticker":"T","side":"yes","entry":0.85,"result":"yes","won":true,"pnl_usd":0.83,"live":true}"#;
        let res_twin = r#"{"kind":"resolve","market_ticker":"T","side":"yes","entry":0.85,"result":"yes","won":true,"pnl_usd":13.0,"live":false}"#;
        for order in [[res_live, res_twin], [res_twin, res_live]] {
            let mut d = Dash::default();
            replay_line_into(&mut d, twin);
            replay_line_into(&mut d, livefill);
            for r in order {
                replay_line_into(&mut d, r);
            }
            assert_eq!(d.triggers.len(), 2);
            let tw = d.triggers.iter().find(|t| !t.live).unwrap();
            let lv = d.triggers.iter().find(|t| t.live).unwrap();
            assert_eq!(tw.pnl, Some(13.0));
            assert_eq!(lv.pnl, Some(0.83));
        }
    }

    // Legacy resolve rows (no live key) attach to live=false rows — pre-feature
    // ledgers are all-shadow, so absent -> false is the correct meaning.
    #[test]
    fn replay_legacy_resolve_goes_to_shadow_row() {
        let mut d = Dash::default();
        replay_line_into(
            &mut d,
            r#"{"kind":"trigger","ts_iso":"t","window_start":"W1","market_ticker":"T","side":"no","entry":0.7,"count":7,"delta_from_open":-60.0}"#,
        );
        replay_line_into(
            &mut d,
            r#"{"kind":"resolve","market_ticker":"T","side":"no","entry":0.7,"result":"no","won":true,"pnl_usd":2.1}"#,
        );
        assert!(!d.triggers[0].live);
        assert_eq!(d.triggers[0].pnl, Some(2.1));
    }

    // TC-13.3: flag-scoped retain — a twin resolve must never evict the live pending.
    #[test]
    fn retain_after_resolve_is_flag_scoped() {
        let mk = |live: bool| Pending {
            ticker: "T".to_string(),
            session: if live { "f1_d50cap75_shadow" } else { "f6_wait270_shadow" }.to_string(),
            side: "yes",
            entry: 0.85,
            count: if live { 6.0 } else { 112.0 },
            stake: if live { 5.0 } else { 100.0 },
            window_end: Utc::now(),
            live,
        };
        let twin = mk(false);
        let live = mk(true);
        let mut v = vec![twin.clone(), live.clone()];
        v.retain(|x| retain_after_resolve(x, &twin));
        assert_eq!(v.len(), 1);
        assert!(v[0].live, "twin resolve evicted the LIVE pending");
        let mut v = vec![mk(false), mk(true)];
        v.retain(|x| retain_after_resolve(x, &live));
        assert_eq!(v.len(), 1);
        assert!(!v[0].live, "live resolve evicted the twin pending");
    }
    // ---- slice 3 (dashboard-f1-live-line): twin latch ----

    // TC-6.5/8.x: once per window key; re-arms on window roll; never without a key.
    #[test]
    fn twin_latch_once_per_window() {
        assert!(twin_should_emit(None, Some("W1"))); // first fire tick
        assert!(!twin_should_emit(Some("W1"), Some("W1"))); // later ticks/retries: latched
        assert!(twin_should_emit(Some("W1"), Some("W2"))); // window rolled: re-armed
        assert!(!twin_should_emit(None, None)); // no window key -> never
        assert!(!twin_should_emit(Some("W1"), None));
    }
    // MAJOR-1 regression (code review): PRE-FEATURE live fills are LiveTriggerRecord
    // rows with live:true, but their resolves were written WITHOUT the flag. The
    // legacy-resolve fallback must attach them to the live row — otherwise every
    // historical live fill (incl. today's F1 fills) renders unresolved and the LIVE
    // chart line starts empty.
    #[test]
    fn replay_legacy_resolve_falls_back_to_live_row() {
        let mut d = Dash::default();
        // pre-feature live fill (LiveTriggerRecord always carried live:true)
        replay_line_into(
            &mut d,
            r#"{"kind":"trigger","outcome":"filled","live":true,"ts_iso":"t","window_start":"W1","market_ticker":"T","side":"yes","entry":0.87,"count":6,"delta_from_open":60.0}"#,
        );
        // its settlement, written by the OLD ResolveRecord (no live key)
        replay_line_into(
            &mut d,
            r#"{"kind":"resolve","market_ticker":"T","side":"yes","entry":0.87,"result":"yes","won":true,"pnl_usd":0.55}"#,
        );
        assert!(d.triggers[0].live);
        assert_eq!(d.triggers[0].pnl, Some(0.55), "legacy resolve must reach the live row");
        // and when BOTH a legacy shadow row and a legacy live row exist at the same
        // entry, the legacy resolve prefers the shadow row (pre-feature majority) —
        // the second legacy resolve then reaches the live row via the fallback.
        let mut d = Dash::default();
        replay_line_into(
            &mut d,
            r#"{"kind":"trigger","ts_iso":"t","window_start":"W1","market_ticker":"T","side":"yes","entry":0.87,"count":112,"delta_from_open":60.0}"#,
        );
        replay_line_into(
            &mut d,
            r#"{"kind":"trigger","outcome":"filled","live":true,"ts_iso":"t","window_start":"W1","market_ticker":"T","side":"yes","entry":0.87,"count":6,"delta_from_open":60.0}"#,
        );
        replay_line_into(
            &mut d,
            r#"{"kind":"resolve","market_ticker":"T","side":"yes","entry":0.87,"result":"yes","won":true,"pnl_usd":13.0}"#,
        );
        replay_line_into(
            &mut d,
            r#"{"kind":"resolve","market_ticker":"T","side":"yes","entry":0.87,"result":"yes","won":true,"pnl_usd":0.55}"#,
        );
        let tw = d.triggers.iter().find(|t| !t.live).unwrap();
        let lv = d.triggers.iter().find(|t| t.live).unwrap();
        assert_eq!(tw.pnl, Some(13.0));
        assert_eq!(lv.pnl, Some(0.55));
    }
    // ---- exec-signal-anchor slice 1: selector + pure pricing/sizing ----

    // TC-3.x: unknown value can NEVER select Signal on real money.
    #[test]
    fn select_exec_anchor_matrix() {
        assert_eq!(select_exec_anchor(None), (ExecAnchor::Ask, None));
        assert_eq!(select_exec_anchor(Some("")), (ExecAnchor::Ask, None));
        assert_eq!(select_exec_anchor(Some("  ")), (ExecAnchor::Ask, None));
        assert_eq!(select_exec_anchor(Some("ask")), (ExecAnchor::Ask, None));
        assert_eq!(select_exec_anchor(Some("signal")), (ExecAnchor::Signal, None));
        assert_eq!(select_exec_anchor(Some(" signal ")), (ExecAnchor::Signal, None));
        let (a, w) = select_exec_anchor(Some("SIGNAL"));
        assert_eq!(a, ExecAnchor::Ask);
        assert!(w.is_some());
        let (a, w) = select_exec_anchor(Some("sig"));
        assert_eq!(a, ExecAnchor::Ask);
        let w = w.unwrap();
        assert!(w.contains("sig") && w.contains("ask") && w.contains("signal"));
    }

    // TC-1.1/1.2/9.x/10.1/12.1/13.1: pricing/sizing math incl. boundaries.
    #[test]
    fn signal_pricing_and_sizing_math() {
        assert!((signal_yes_limit(0.92, 0.06) - 0.98).abs() < 1e-12); // band edge
        assert!((signal_yes_limit(0.85, 0.06) - 0.91).abs() < 1e-12);
        assert!((signal_yes_limit(0.55, 0.0) - 0.55).abs() < 1e-12); // buf 0 -> limit=signal
        assert!((signal_yes_limit(0.95, 0.07) - 0.99).abs() < 1e-12); // clamp binds
        assert!((signal_no_limit(0.92, 0.06) - 0.02).abs() < 1e-12);
        assert!((signal_no_limit(0.55, 0.60) - 0.01).abs() < 1e-12); // huge buf -> floor
        assert_eq!(signal_count(5.0, 0.90, 15), 6.0);
        assert_eq!(signal_count(5.0, 0.92, 15), 5.0);
        assert_eq!(signal_count(5.0, 0.51, 15), 10.0);
        assert_eq!(signal_count(100.0, 0.60, 15), 15.0); // max_count cap
        assert_eq!(signal_count(5.0, 4.9, 15), 1.0); // floor 1
        // byte-identity: exec_* twins are the exact legacy formulas
        for e in [0.55, 0.66, 0.85, 0.92, 0.98] {
            for b in [0.0, 0.02, 0.06] {
                assert_eq!(exec_yes_limit(e, b), (e + b).min(0.99));
                assert_eq!(exec_no_limit(e, b), ((1.0 - e) - b).max(0.01));
            }
            assert_eq!(exec_count(5.0, e, 15), (5.0f64 / e).round().clamp(1.0, 15.0));
        }
    }

    // TC-5.2: fail-open telemetry filter, same (0.50, 0.98] band as the legacy path.
    #[test]
    fn filtered_ask_band() {
        assert_eq!(filtered_ask(Some(0.85)), Some(0.85));
        assert_eq!(filtered_ask(Some(0.98)), Some(0.98)); // inclusive top
        assert_eq!(filtered_ask(Some(0.99)), None);
        assert_eq!(filtered_ask(Some(0.50)), None); // exclusive bottom
        assert_eq!(filtered_ask(Some(0.10)), None);
        assert_eq!(filtered_ask(None), None);
    }
    // ---- exec-signal-anchor slice 2: telemetry timeout seam + requote-never ----

    // TC-8.x: the 500ms bound cuts a hung book fetch — exec_entry degrades to None
    // instead of stalling place_live's return for the client's 8s timeout.
    // (Real-time test: tokio test-util pause/advance is not an enabled feature.)
    #[tokio::test]
    async fn telemetry_timeout_bounds_hung_fetch() {
        let t0 = std::time::Instant::now();
        let hung = std::future::pending::<Result<(), ()>>();
        let out = tokio::time::timeout(Duration::from_millis(TELEMETRY_TIMEOUT_MS), hung).await;
        assert!(out.is_err(), "timeout must fire, not hang");
        let waited = t0.elapsed().as_millis() as u64;
        assert!(waited >= TELEMETRY_TIMEOUT_MS && waited < 3000, "bounded near 500ms, got {waited}ms");
        // fast path passes through untouched
        let quick = tokio::time::timeout(
            Duration::from_millis(TELEMETRY_TIMEOUT_MS),
            std::future::ready(Ok::<f64, ()>(0.85)),
        )
        .await;
        assert_eq!(quick.unwrap().unwrap(), 0.85);
    }
}









