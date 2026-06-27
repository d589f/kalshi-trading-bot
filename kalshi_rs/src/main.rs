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

use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use chrono::{DateTime, SecondsFormat, Utc};
use tracing::{info, warn};
use tracing_subscriber::EnvFilter;

use config::{SessionConfig, STATE_TICK_SECS};
use dashboard::{Dash, TrigSummary};
use engine::{evaluate, Shared, Side};
use kalshi::auth::Signer;
use kalshi::orders::OrderClient;
use kalshi::rest::{KalshiRest, PROD_BASE, SERIES_KXBTC15M};
use kalshi::{kalshi_fee_usd, pick_market_for_close, KalshiBook};
use ledger::{Ledger, ResolveRecord, TriggerRecord};
use signal::{compute_all_sigmas, compute_ofi, p_model_classic};
use state::AppState;

fn env_f64(k: &str, d: f64) -> f64 {
    std::env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}
fn env_i64(k: &str, d: i64) -> i64 {
    std::env::var(k).ok().and_then(|v| v.parse().ok()).unwrap_or(d)
}

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

/// A fired order awaiting Kalshi settlement (shadow would-be, or a real live fill).
#[derive(Clone)]
struct Pending {
    ticker: String,
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
}

/// MIRROR config (env). When `url` is set, the f6 signal is taken straight from the
/// prod paper engine (window_open / Δ / σ / book) instead of our own Binance feed,
/// making the side + entry 1:1 with the paper. `max_age_secs` guards a stale/dead paper:
/// if the paper's last_update is older than this, we SKIP the tick (never trade blind).
#[derive(Clone)]
struct MirrorCfg {
    url: Option<String>,
    max_age_secs: f64,
}

const LIVE_STATE_PATH: &str = "live_state.json";

#[derive(Default, Clone, serde::Serialize, serde::Deserialize)]
struct LiveState {
    day: String,
    trades_today: i64,
    day_pnl: f64,
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
    };
    // MIRROR: take the signal from the paper engine (1:1). Off unless MIRROR_STATE_URL set.
    let mcfg = MirrorCfg {
        url: std::env::var("MIRROR_STATE_URL").ok().filter(|s| !s.is_empty()),
        max_age_secs: env_f64("MIRROR_MAX_AGE_SECS", 5.0),
    };
    if let Some(u) = &mcfg.url {
        info!("MIRROR mode ON — signal sourced from paper engine {u} (max_age={}s)", mcfg.max_age_secs);
    }
    let http = reqwest::Client::new();
    let live_state = Arc::new(Mutex::new(load_live_state()));
    let order_client: Option<Arc<OrderClient>> =
        match (std::env::var("KALSHI_KEY_ID"), std::env::var("KALSHI_KEY_PATH")) {
            (Ok(kid), Ok(kpath)) => match std::fs::read_to_string(&kpath) {
                Ok(pem) => match Signer::from_pem(kid, &pem).and_then(|s| OrderClient::new(base.clone(), s)) {
                    Ok(oc) => {
                        info!(
                            "order client ready | LIVE_TRADING={} stake=${} max/day={} loss_stop=${}",
                            lcfg.enabled, lcfg.stake, lcfg.max_trades_day, lcfg.daily_loss_stop
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
    signal_loop(state, ledger, pending, cfg, dash, order_client, lcfg, live_state, mcfg, http).await;
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
                            session: SESSION_NAME,
                            market_ticker: &pd.ticker,
                            side: pd.side,
                            entry: pd.entry,
                            stake_usd: pd.stake,
                            result: &m.result,
                            won,
                            pnl_usd: (pnl * 100.0).round() / 100.0,
                        });
                        if pd.live {
                            let mut s = live_state.lock().unwrap();
                            s.day_pnl = ((s.day_pnl + pnl) * 100.0).round() / 100.0;
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
            match mirror::fetch(&http, murl, now_utc).await {
                Some(m) if m.age_secs <= mcfg.max_age_secs => {
                    win = window::WindowState {
                        window_start: Some(m.window_start),
                        window_end: Some(m.window_end),
                        window_open_price: Some(m.window_open),
                        prev_close_price: Some(m.binance_price - m.delta_from_prev),
                    };
                    price = m.binance_price;
                    sigmas.insert("max30".to_string(), m.sigma_max30);
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
                            SESSION_NAME,
                            if other.is_some() { "STALE" } else { "UNREACHABLE" }
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
                } else if lcfg.enabled && order_client.is_some() {
                    fired_window = win_key.clone();
                    place_live(
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
                    )
                    .await;
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
) {
    // daily reset + caps
    {
        let mut s = live_state.lock().unwrap();
        let today = now_utc.format("%Y-%m-%d").to_string();
        if s.day != today {
            *s = LiveState { day: today, trades_today: 0, day_pnl: 0.0 };
            save_live_state(&s);
        }
        if s.trades_today >= lcfg.max_trades_day {
            warn!("LIVE HALT: max {} trades/day reached", lcfg.max_trades_day);
            return;
        }
        if s.day_pnl <= -lcfg.daily_loss_stop {
            warn!("LIVE HALT: daily loss stop (${:+.2} <= -${})", s.day_pnl, lcfg.daily_loss_stop);
            return;
        }
    }
    let entry = fire.entry;
    if !(0.50 < entry && entry <= cfg.max_entry_price) {
        warn!("LIVE SKIP: entry {:.2} out of (0.50, {:.2}]", entry, cfg.max_entry_price);
        return;
    }
    let side_str: &'static str = match fire.side {
        Side::Yes => "yes",
        Side::No => "no",
    };
    let (v2side, mut price) = match fire.side {
        Side::Yes => ("bid", (entry + lcfg.price_buf).min(0.99)),
        Side::No => ("ask", ((1.0 - entry) - lcfg.price_buf).max(0.01)),
    };
    let mut count = (lcfg.stake / entry).round();
    if count < 1.0 {
        count = 1.0;
    }
    if count > lcfg.max_count as f64 {
        count = lcfg.max_count as f64;
    }
    let coid = format!("f6-{}-{}", now_utc.timestamp_millis(), side_str);
    let (mut status, mut resp, mut lat) =
        match oc.create_ioc(&book.ticker, v2side, count, price, coid.clone()).await {
            Ok(x) => x,
            Err(e) => {
                warn!("LIVE order error: {e}");
                return;
            }
        };
    // RE-QUOTE once on a no-fill: the book moved past our limit in the order RTT (fast/choppy
    // window). Cross deeper — the aggressive price applies ONLY to the failed order, so normal
    // fills keep the tight buffer. IOC still fills at the real ask (≤ limit), bounded by requote_buf.
    if status == 201 && resp.fill() <= 0.0 && lcfg.requote_buf > lcfg.price_buf {
        price = match fire.side {
            Side::Yes => (entry + lcfg.requote_buf).min(0.97),
            Side::No => ((1.0 - entry) - lcfg.requote_buf).max(0.03),
        };
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
    if status != 201 {
        warn!("LIVE order rejected HTTP {}", status);
        return;
    }
    let fill = resp.fill();
    if fill <= 0.0 {
        info!(
            "LIVE NO-FILL {} {} {}x @ limit {:.2} (rem {})",
            book.ticker, side_str.to_uppercase(), count, price, resp.remaining_count
        );
        return; // nothing filled -> no position
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
        s.trades_today += 1;
        save_live_state(&s);
    }
    let w_start = win.window_start.map(|w| w.to_rfc3339()).unwrap_or_default();
    let w_end = win.window_end.map(|w| w.to_rfc3339()).unwrap_or_default();
    ledger.append(&serde_json::json!({
        "kind": "trigger", "live": true, "ts": now, "ts_iso": now_utc.to_rfc3339(),
        "session": SESSION_NAME, "window_start": w_start.clone(), "window_end": w_end.clone(),
        "market_ticker": book.ticker.clone(), "side": side_str, "entry": eff, "count": fill as i64,
        "delta_from_open": shared.delta_from_open.unwrap_or(0.0), "p": fire.p,
        "order_id": resp.order_id, "fee": fee, "latency_ms": lat as i64,
    }));
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
    });
    if let Some(we) = win.window_end {
        pending.lock().unwrap().push(Pending {
            ticker: book.ticker.clone(),
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
}
