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
mod signal;
mod state;
mod window;

use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use chrono::{DateTime, SecondsFormat, Utc};
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

use config::{SessionConfig, STATE_TICK_SECS};
use dashboard::{Dash, TrigSummary};
use engine::{evaluate, Shared, Side};
use kalshi::rest::{KalshiRest, PROD_BASE, SERIES_KXBTC15M};
use kalshi::{kalshi_fee_usd, pick_market_for_close, KalshiBook};
use ledger::{Ledger, ResolveRecord, TriggerRecord};
use signal::{compute_all_sigmas, compute_ofi, p_model_classic};
use state::AppState;

const SESSION_NAME: &str = "f6_wait270_shadow";
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

/// Replay the JSONL ledger into the dashboard so shadow history survives restarts.
fn load_ledger_into_dash(path: &str, dash: &Arc<Mutex<Dash>>) {
    let content = match std::fs::read_to_string(path) {
        Ok(c) => c,
        Err(_) => return,
    };
    let mut d = dash.lock().unwrap();
    for line in content.lines() {
        let v: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        match v.get("kind").and_then(|k| k.as_str()) {
            Some("trigger") => d.triggers.push(TrigSummary {
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
            }),
            Some("resolve") => d.resolve(
                v["market_ticker"].as_str().unwrap_or(""),
                v["side"].as_str().unwrap_or(""),
                v["entry"].as_f64().unwrap_or(0.0),
                v["result"].as_str().unwrap_or(""),
                v["won"].as_bool().unwrap_or(false),
                v["pnl_usd"].as_f64().unwrap_or(0.0),
            ),
            _ => {}
        }
    }
    info!("loaded {} shadow triggers from ledger", d.triggers.len());
}

/// A fired would-be order awaiting Kalshi settlement.
#[derive(Clone)]
struct Pending {
    ticker: String,
    side: &'static str, // "yes" | "no"
    entry: f64,
    count: f64,
    stake: f64,
    window_end: DateTime<Utc>,
}

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
        .init();

    let base = std::env::var("KALSHI_BASE").unwrap_or_else(|_| PROD_BASE.to_string());
    info!("kalshi base = {base}");

    let cfg = SessionConfig::f6_wait270();
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
    let rest = Arc::new(KalshiRest::new(base)?);

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
        d.live.order_mode = std::env::var("ORDER_MODE").unwrap_or_else(|_| "log-only (shadow)".to_string());
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
        info!("push mode: posting triggers to {push_url}");
        tokio::spawn(async move {
            let client = reqwest::Client::new();
            loop {
                tokio::time::sleep(Duration::from_secs(10)).await;
                let body = {
                    let dd = d.lock().unwrap();
                    serde_json::to_string(&dd.triggers).unwrap_or_default()
                };
                let _ = client
                    .post(&push_url)
                    .header("X-Token", &token)
                    .header("Content-Type", "application/json")
                    .timeout(Duration::from_secs(8))
                    .body(body)
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
        tokio::spawn(async move { resolver(rc, pend, led, dsh).await });
    }

    // 4) Signal loop @ 0.3s
    signal_loop(state, ledger, pending, cfg, dash).await;
    Ok(())
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

/// Score fired would-be orders on Kalshi's real settlement.
async fn resolver(
    rest: Arc<KalshiRest>,
    pending: Arc<Mutex<Vec<Pending>>>,
    ledger: Arc<Ledger>,
    dash: Arc<Mutex<Dash>>,
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
                            session: SESSION_NAME,
                            market_ticker: &pd.ticker,
                            side: pd.side,
                            entry: pd.entry,
                            stake_usd: pd.stake,
                            result: &m.result,
                            won,
                            pnl_usd: (pnl * 100.0).round() / 100.0,
                        });
                        info!(
                            "RESOLVED {} {} result={} won={} pnl=${:+.2}",
                            pd.ticker, pd.side, m.result, won, pnl
                        );
                        dash.lock().unwrap().resolve(
                            &pd.ticker,
                            pd.side,
                            pd.entry,
                            &m.result,
                            won,
                            (pnl * 100.0).round() / 100.0,
                        );
                        pending.lock().unwrap().retain(|x| {
                            !(x.ticker == pd.ticker && x.side == pd.side && x.entry == pd.entry)
                        });
                    }
                }
                Err(e) => warn!("resolve get_market {}: {e}", pd.ticker),
            }
        }
    }
}

/// The 0.3s decision loop — the analogue of `state_updater` → `check_all_sessions`.
async fn signal_loop(
    state: Arc<Mutex<AppState>>,
    ledger: Arc<Ledger>,
    pending: Arc<Mutex<Vec<Pending>>>,
    cfg: SessionConfig,
    dash: Arc<Mutex<Dash>>,
) {
    let mut tick = tokio::time::interval(Duration::from_secs_f64(STATE_TICK_SECS));
    let mut fired_window: Option<String> = None;
    let mut last_status_log = 0.0f64;

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

        let price = match price_opt {
            Some(p) => p,
            None => continue, // no BTC yet
        };

        // ---- compute signal inputs ----
        let sigmas = compute_all_sigmas(&history, now);
        let (ofi_1, _ofi_3, _ofi_5, buy_v, sell_v, _cnt) = compute_ofi(&buffer, now);
        let tau = win.tau(now_utc);
        let elapsed = win.elapsed_min(now_utc);
        let hour = win
            .window_start
            .map(|w| w.format("%H").to_string().parse::<i32>().unwrap_or(0))
            .unwrap_or(0);

        let book = kalshi.clone().unwrap_or_default();
        let shared = Shared {
            binance_price: Some(price),
            delta_from_open: win.delta_from_open(price),
            delta_from_prev: win.delta_from_prev(price),
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
            last_trade_expensive: None, // Kalshi: no last-trade-expensive proxy yet (REST book only)
        };

        let res = evaluate(&cfg, &shared);

        // 1-trade-per-window latch: `fired_window` holds the window we last fired in,
        // so when `win_key` changes the latch clears automatically.
        let win_key = win.window_start.map(|w| w.to_rfc3339());

        // periodic status
        if now - last_status_log > 10.0 {
            last_status_log = now;
            info!(
                "[{}] {} | btc={:.0} Δopen={:?} τ={:.1} e={:.1} σmax30={:.6} mkt={} yes_ask={:?} no_ask={:?}",
                SESSION_NAME,
                res.reason,
                price,
                shared.delta_from_open,
                tau,
                elapsed,
                shared.sigmas.get("max30").copied().unwrap_or(0.0),
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
            let mut dl = dash.lock().unwrap();
            let l = &mut dl.live;
            l.market = book.ticker.clone();
            l.btc = price;
            l.delta_open = shared.delta_from_open;
            l.delta_prev = shared.delta_from_prev;
            l.tau = tau;
            l.elapsed = elapsed;
            l.sigma_max30 = shared.sigmas.get("max30").copied().unwrap_or(0.0);
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

        // ---- trigger → emit would-be order (once per window) ----
        if let Some(fire) = res.fire {
            let already = fired_window.as_deref() == win_key.as_deref();
            if !already {
                if book.ticker.is_empty() {
                    // no market yet — don't latch; wait for the poller
                } else {
                    fired_window = win_key.clone();
                    emit_trigger(&ledger, &pending, &cfg, &shared, &book, &win, &fire, now, now_utc, &dash);
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
        session: SESSION_NAME,
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
        SESSION_NAME,
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
            side: side_str,
            entry: fire.entry,
            count,
            stake: cfg.stake,
            window_end: w_end_dt,
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
    });
}
