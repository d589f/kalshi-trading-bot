//! Tiny built-in HTTP dashboard — live shadow state, position stats, and a side-by-side
//! comparison vs the prod paper engine (`f6_wait270`). Hand-rolled server (no extra deps).
//!
//! Routes:
//!   GET  /        → HTML page (auto-refreshing)
//!   GET  /stats   → JSON {live, agg, compare[], paper_agg, triggers[]}
//!   POST /paper   → ingest the prod paper f6 trades (a JSON array); pushed by a prod cron.
//!
//! View: http://<server>:8890  (or SSH-tunnel: ssh -L 8890:localhost:8890 root@<server>)

use std::sync::{Arc, Mutex};

use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tracing::{info, warn};

/// Latest live signal snapshot (updated every 0.3s tick).
#[derive(Default, Clone, Serialize, Deserialize)]
pub struct LiveSnap {
    pub market: String,
    pub btc: f64,
    pub delta_open: Option<f64>,
    pub delta_prev: Option<f64>,
    pub tau: f64,
    pub elapsed: f64,
    pub sigma_max30: f64,
    pub p: Option<f64>,
    pub snr: f64,
    pub reason: String,
    pub yes_ask: Option<f64>,
    pub no_ask: Option<f64>,
    pub yes_bid: Option<f64>,
    pub no_bid: Option<f64>,
    pub floor_strike: Option<f64>,
    pub updated_iso: String,
    pub binance_feed: String,
    pub order_mode: String,
    /// authoritative REAL live PnL pushed from the bot (fees + slippage included)
    #[serde(default)]
    pub day_pnl: f64,
    #[serde(default)]
    pub total_pnl: f64,
}

/// One live would-be order + its settlement (filled in by the resolver).
#[derive(Clone, Serialize, Deserialize, Default)]
pub struct TrigSummary {
    #[serde(default)]
    pub ts_iso: String,
    #[serde(default)]
    pub window_start: String,
    #[serde(default)]
    pub window_end: String,
    #[serde(default)]
    pub ticker: String,
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub entry: f64,
    #[serde(default)]
    pub count: i64,
    #[serde(default)]
    pub delta: f64,
    #[serde(default)]
    pub p: Option<f64>,
    #[serde(default)]
    pub result: Option<String>,
    #[serde(default)]
    pub won: Option<bool>,
    #[serde(default)]
    pub pnl: Option<f64>,
    /// true = a REAL live fill (raw $5 money); false = the strategy's shadow would-be
    /// (the "honest twin"). Old producers omit the key → false (all-shadow), which is
    /// exactly the pre-feature meaning of the feed.
    #[serde(default)]
    pub live: bool,
}

/// A paper f6 trade pushed from prod (`paper_compare_kalshi_15m`, session f6_wait270).
#[derive(Clone, Serialize, Deserialize, Default)]
pub struct PaperTrade {
    #[serde(default)]
    pub window_start: String,
    #[serde(default)]
    pub side: String, // YES | NO
    #[serde(default)]
    pub entry_price: f64,
    #[serde(default)]
    pub delta: f64,
    #[serde(default)]
    pub p_model: Option<f64>,
    #[serde(default)]
    pub pnl: Option<f64>,
    #[serde(default)]
    pub result: Option<String>,
    #[serde(default)]
    pub market_slug: String,
}

#[derive(Default)]
pub struct Dash {
    pub live: LiveSnap,
    pub triggers: Vec<TrigSummary>,
    pub paper: Vec<PaperTrade>,
    pub paper_updated: String,
    /// second paper series (f1_d50cap75) pushed via POST /paper_f1 — comparison line
    pub paper_f1: Vec<PaperTrade>,
    pub paper_f1_updated: String,
    /// triggers from the SECOND shadow (EU box, Binance.com feed), pushed via POST /shadow_com
    pub shadow_com: Vec<TrigSummary>,
    pub shadow_com_updated: String,
    /// live signal snapshot pushed from the EU mirror bot — the REAL binance.com signal we trade
    /// (the local `live` field is Buffalo's low-volume binance.US shadow, kept as a fallback)
    pub live_com: LiveSnap,
    pub live_com_updated: String,
    pub started_iso: String,
}

fn wkey(s: &str) -> String {
    s.chars().take(16).collect() // "YYYY-MM-DDTHH:MM"
}

impl Dash {
    /// Update the matching trigger with its settlement. `live` is part of the match:
    /// a live fill and its shadow twin can share (ticker, side, entry) when
    /// eff == signal_entry — without the flag the wrong row could take the result.
    /// Returns whether a row matched (replay uses it for the legacy-resolve fallback).
    #[allow(clippy::too_many_arguments)]
    pub fn resolve(
        &mut self,
        ticker: &str,
        side: &str,
        entry: f64,
        result: &str,
        won: bool,
        pnl: f64,
        live: bool,
    ) -> bool {
        for t in self.triggers.iter_mut() {
            if t.ticker == ticker
                && t.side == side
                && (t.entry - entry).abs() < 1e-9
                && t.live == live
                && t.result.is_none()
            {
                t.result = Some(result.to_string());
                t.won = Some(won);
                t.pnl = Some(pnl);
                return true;
            }
        }
        false
    }

    fn agg(&self) -> serde_json::Value {
        let cut = self.started_iso.as_str();
        let trig: Vec<&TrigSummary> = self
            .triggers
            .iter()
            .filter(|t| t.window_start.as_str() >= cut)
            .collect();
        let n = trig.len();
        let yes = trig.iter().filter(|t| t.side == "yes").count();
        let avg_entry = if n > 0 {
            trig.iter().map(|t| t.entry).sum::<f64>() / n as f64
        } else {
            0.0
        };
        let res: Vec<&TrigSummary> = trig.iter().copied().filter(|t| t.result.is_some()).collect();
        let rn = res.len();
        let wins = res.iter().filter(|t| t.won == Some(true)).count();
        let total_pnl: f64 = res.iter().filter_map(|t| t.pnl).sum();
        json!({
            "positions": n, "yes": yes, "no": n - yes,
            "avg_entry": r4(avg_entry),
            "resolved": rn, "wins": wins,
            "win_rate": if rn>0 {r1(100.0*wins as f64/rn as f64)} else {0.0},
            "total_pnl": r2(total_pnl),
            "avg_pnl": if rn>0 {r2(total_pnl/rn as f64)} else {0.0},
        })
    }

    fn paper_agg(&self) -> serde_json::Value {
        let cut = self.started_iso.as_str();
        let pp: Vec<&PaperTrade> = self
            .paper
            .iter()
            .filter(|t| t.window_start.as_str() >= cut)
            .collect();
        let n = pp.len();
        let yes = pp.iter().filter(|t| t.side.eq_ignore_ascii_case("yes")).count();
        let avg_entry = if n > 0 {
            pp.iter().map(|t| t.entry_price).sum::<f64>() / n as f64
        } else {
            0.0
        };
        let res: Vec<&PaperTrade> = pp
            .iter()
            .copied()
            .filter(|t| t.result.as_deref().map(|r| !r.is_empty()).unwrap_or(false))
            .collect();
        let rn = res.len();
        let wins = res
            .iter()
            .filter(|t| t.result.as_deref() == Some("WIN") || t.pnl.map(|p| p > 0.0).unwrap_or(false))
            .count();
        let total_pnl: f64 = res.iter().filter_map(|t| t.pnl).sum();
        json!({
            "positions": n, "yes": yes, "no": n - yes,
            "avg_entry": r4(avg_entry),
            "resolved": rn, "wins": wins,
            "win_rate": if rn>0 {r1(100.0*wins as f64/rn as f64)} else {0.0},
            "total_pnl": r2(total_pnl),
            "updated": self.paper_updated,
        })
    }

    fn paper_f1_agg(&self) -> serde_json::Value {
        let cut = self.started_iso.as_str();
        let pp: Vec<&PaperTrade> = self
            .paper_f1
            .iter()
            .filter(|t| t.window_start.as_str() >= cut)
            .collect();
        let n = pp.len();
        let res: Vec<&PaperTrade> = pp
            .iter()
            .copied()
            .filter(|t| t.result.as_deref().map(|r| !r.is_empty()).unwrap_or(false))
            .collect();
        let rn = res.len();
        let wins = res
            .iter()
            .filter(|t| t.result.as_deref() == Some("WIN") || t.pnl.map(|p| p > 0.0).unwrap_or(false))
            .count();
        let total_pnl: f64 = res.iter().filter_map(|t| t.pnl).sum();
        json!({
            "positions": n, "resolved": rn, "wins": wins,
            "win_rate": if rn>0 {r1(100.0*wins as f64/rn as f64)} else {0.0},
            "total_pnl": r2(total_pnl),
            "updated": self.paper_f1_updated,
        })
    }

    /// Per-window side-by-side of shadow trigger vs paper trade.
    fn compare(&self) -> Vec<serde_json::Value> {
        use std::collections::BTreeMap;
        let cut = wkey(&self.started_iso);
        let mut keys: BTreeMap<String, ()> = BTreeMap::new();
        for t in &self.triggers {
            if wkey(&t.window_start) >= cut {
                keys.insert(wkey(&t.window_start), ());
            }
        }
        for p in &self.paper {
            if wkey(&p.window_start) >= cut {
                keys.insert(wkey(&p.window_start), ());
            }
        }
        for p in &self.paper_f1 {
            if wkey(&p.window_start) >= cut {
                keys.insert(wkey(&p.window_start), ());
            }
        }
        for t in &self.shadow_com {
            if wkey(&t.window_start) >= cut {
                keys.insert(wkey(&t.window_start), ());
            }
        }
        let mut out: Vec<serde_json::Value> = keys
            .keys()
            .rev()
            .map(|w| {
                let s = self.triggers.iter().find(|t| wkey(&t.window_start) == *w);
                let p = self.paper.iter().find(|t| wkey(&t.window_start) == *w);
                // shadow_com carries BOTH the strategy's shadow twin (live=false) and
                // real live fills (live=true) — split them so the green line stays a
                // strategy line and the LIVE line is real money.
                let c = self
                    .shadow_com
                    .iter()
                    .find(|t| !t.live && wkey(&t.window_start) == *w);
                let lv = self
                    .shadow_com
                    .iter()
                    .find(|t| t.live && wkey(&t.window_start) == *w);
                let f1 = self.paper_f1.iter().find(|t| wkey(&t.window_start) == *w);
                let match_us = match (s, p) {
                    (Some(s), Some(p)) => Some(s.side.eq_ignore_ascii_case(&p.side)),
                    _ => None,
                };
                let match_com = match (c, p) {
                    (Some(c), Some(p)) => Some(c.side.eq_ignore_ascii_case(&p.side)),
                    _ => None,
                };
                // LIVE ↔ paper-F1 side agreement: only windows where BOTH exist count
                let match_lv = match (lv, f1) {
                    (Some(l), Some(f)) => Some(l.side.eq_ignore_ascii_case(&f.side)),
                    _ => None,
                };
                json!({
                    "window": w,
                    "pa_side": p.map(|x| x.side.to_uppercase()),
                    "pa_entry": p.map(|x| x.entry_price),
                    "pa_delta": p.map(|x| x.delta),
                    "pa_pnl": p.and_then(|x| x.pnl),
                    "us_side": s.map(|x| x.side.to_uppercase()),
                    "us_entry": s.map(|x| x.entry),
                    "us_delta": s.map(|x| x.delta),
                    "us_pnl": s.and_then(|x| x.pnl),
                    "com_side": c.map(|x| x.side.to_uppercase()),
                    "com_entry": c.map(|x| x.entry),
                    "com_delta": c.map(|x| x.delta),
                    "com_pnl": c.and_then(|x| x.pnl),
                    "match_us": match_us,
                    "match_com": match_com,
                    // --- enriched fields for the hover tooltip ---
                    "pa_p": p.and_then(|x| x.p_model),
                    "pa_result": p.and_then(|x| x.result.clone()),
                    "pa_slug": p.map(|x| x.market_slug.clone()),
                    "us_p": s.and_then(|x| x.p),
                    "us_count": s.map(|x| x.count),
                    "com_p": c.and_then(|x| x.p),
                    "com_count": c.map(|x| x.count),
                    "com_ticker": c.map(|x| x.ticker.clone()),
                    "com_result": c.and_then(|x| x.result.clone()),
                    "com_won": c.and_then(|x| x.won),
                    "com_ts": c.map(|x| x.ts_iso.clone()),
                    "f1_side": f1.map(|x| x.side.to_uppercase()),
                    "f1_entry": f1.map(|x| x.entry_price),
                    "f1_delta": f1.map(|x| x.delta),
                    "f1_pnl": f1.and_then(|x| x.pnl),
                    "f1_p": f1.and_then(|x| x.p_model),
                    "f1_result": f1.and_then(|x| x.result.clone()),
                    // --- real live fills (raw $5 money) ---
                    "lv_side": lv.map(|x| x.side.to_uppercase()),
                    "lv_entry": lv.map(|x| x.entry),
                    "lv_delta": lv.map(|x| x.delta),
                    "lv_pnl": lv.and_then(|x| x.pnl),
                    "lv_result": lv.and_then(|x| x.result.clone()),
                    "lv_won": lv.and_then(|x| x.won),
                    "lv_count": lv.map(|x| x.count),
                    "lv_p": lv.and_then(|x| x.p),
                    "match_lv": match_lv,
                })
            })
            .collect();
        // Rows are newest-first, so this cap decides how far BACK the chart can reach.
        // It must stay comfortably behind the newest deploy marker (the zero-view CUT),
        // otherwise early windows silently fall off the tail and the "since update"
        // totals SHRINK day by day even while the curve only rises. 300 rows was ~3
        // days and had already slid past the CUT. 3000 ≈ 31 days of 15-min windows;
        // override with DASH_ROWS when a marker gets older than that.
        let cap: usize = std::env::var("DASH_ROWS")
            .ok()
            .and_then(|v| v.parse().ok())
            .filter(|n| *n > 0)
            .unwrap_or(3000);
        out.truncate(cap);
        // Each row carries ~40 keys but only the sources that fired in that window
        // are ever set, so at this row count the null padding dominates the wire
        // size. The client reads every one of these with `!= null`, which treats a
        // missing key exactly like an explicit null, so dropping them is invisible
        // to the page and roughly halves the payload.
        for v in out.iter_mut() {
            if let Some(o) = v.as_object_mut() {
                o.retain(|_, val| !val.is_null());
            }
        }
        // The table only renders the newest ~60 rows; every older row exists purely to
        // feed the chart's cumulative sums. Shipping the full ~40-key object for all of
        // them made the response big enough to outrun the page's 2s refresh, which
        // stacked requests until the browser's per-host connection limit stalled the
        // page. Older rows keep only the fields the chart actually reads.
        const TABLE_ROWS: usize = 120;
        const CHART_KEYS: [&str; 10] = [
            "window", "pa_pnl", "pa_entry", "com_pnl", "com_entry", "com_won", "f1_pnl",
            "f1_entry", "lv_pnl", "lv_entry",
        ];
        for v in out.iter_mut().skip(TABLE_ROWS) {
            if let Some(o) = v.as_object_mut() {
                o.retain(|k, _| CHART_KEYS.contains(&k.as_str()));
            }
        }
        out
    }

    fn body_json(&self) -> String {
        let cmp = self.compare();
        // side-agreement summary among windows where BOTH paper and that shadow fired
        let (mut us_n, mut us_ok, mut com_n, mut com_ok, mut lv_n, mut lv_ok) =
            (0i64, 0i64, 0i64, 0i64, 0i64, 0i64);
        for c in &cmp {
            if let Some(b) = c.get("match_us").and_then(|v| v.as_bool()) {
                us_n += 1;
                if b {
                    us_ok += 1;
                }
            }
            if let Some(b) = c.get("match_com").and_then(|v| v.as_bool()) {
                com_n += 1;
                if b {
                    com_ok += 1;
                }
            }
            if let Some(b) = c.get("match_lv").and_then(|v| v.as_bool()) {
                lv_n += 1;
                if b {
                    lv_ok += 1;
                }
            }
        }
        let pct = |ok: i64, n: i64| if n > 0 { (1000.0 * ok as f64 / n as f64).round() / 10.0 } else { 0.0 };
        let cut = self.started_iso.as_str();
        // real live fills in-period: positions = fills pushed (resolved or not);
        // pnl = raw $5 sum over the resolved ones (authoritative absolute figure)
        let lv_rows: Vec<&TrigSummary> = self
            .shadow_com
            .iter()
            .filter(|t| t.live && t.window_start.as_str() >= cut)
            .collect();
        let lv_positions = lv_rows.len();
        let lv_pnl: f64 = lv_rows.iter().filter_map(|t| t.pnl).sum();
        json!({
            "live": self.live,
            "live_com": self.live_com,
            "live_com_updated": self.live_com_updated,
            "agg": self.agg(),
            "paper_agg": self.paper_agg(),
            "paper_f1_agg": self.paper_f1_agg(),
            "compare": cmp,
            "summary": {
                "us_match": us_ok, "us_total": us_n, "us_pct": pct(us_ok, us_n),
                "com_match": com_ok, "com_total": com_n, "com_pct": pct(com_ok, com_n),
                "com_positions": self.shadow_com.iter().filter(|t| !t.live && t.window_start.as_str() >= cut).count(),
                "com_updated": self.shadow_com_updated,
                "lv_match": lv_ok, "lv_total": lv_n, "lv_pct": pct(lv_ok, lv_n),
                "lv_positions": lv_positions,
                "lv_pnl": r2(lv_pnl),
            },
            "started": self.started_iso,
        })
        .to_string()
    }
}

fn r1(x: f64) -> f64 {
    (x * 10.0).round() / 10.0
}
fn r2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}
fn r4(x: f64) -> f64 {
    (x * 10000.0).round() / 10000.0
}

pub async fn serve(dash: Arc<Mutex<Dash>>, port: u16) {
    let token = std::env::var("DASH_TOKEN").unwrap_or_default();
    let addr = format!("0.0.0.0:{port}");
    let listener = match TcpListener::bind(&addr).await {
        Ok(l) => l,
        Err(e) => {
            warn!("dashboard bind {addr} failed: {e}");
            return;
        }
    };
    info!("dashboard on http://{addr}  (POST /paper for prod sync)");
    loop {
        let (mut sock, _peer) = match listener.accept().await {
            Ok(x) => x,
            Err(e) => {
                warn!("dashboard accept: {e}");
                continue;
            }
        };
        let dash = dash.clone();
        let token = token.clone();
        tokio::spawn(async move {
            let (method, path, hdr_token, body) = match read_request(&mut sock).await {
                Some(x) => x,
                None => return,
            };
            let bad_token = !token.is_empty() && hdr_token != token;
            let (status, ctype, out) = if method == "POST" && path == "/paper_f1" {
                if bad_token {
                    ("403 Forbidden", "text/plain", "bad token".to_string())
                } else {
                    match serde_json::from_str::<Vec<PaperTrade>>(&body) {
                        Ok(trades) => {
                            let n = trades.len();
                            let mut d = dash.lock().unwrap();
                            d.paper_f1 = trades;
                            d.paper_f1_updated = d.live.updated_iso.clone();
                            ("200 OK", "application/json", json!({"ok": true, "n": n}).to_string())
                        }
                        Err(e) => ("400 Bad Request", "text/plain", format!("parse: {e}")),
                    }
                }
            } else if method == "POST" && path.starts_with("/paper") {
                if bad_token {
                    ("403 Forbidden", "text/plain", "bad token".to_string())
                } else {
                    match serde_json::from_str::<Vec<PaperTrade>>(&body) {
                        Ok(trades) => {
                            let n = trades.len();
                            let mut d = dash.lock().unwrap();
                            d.paper = trades;
                            d.paper_updated = d.live.updated_iso.clone();
                            ("200 OK", "application/json", json!({"ok": true, "n": n}).to_string())
                        }
                        Err(e) => ("400 Bad Request", "text/plain", format!("parse: {e}")),
                    }
                }
            } else if method == "POST" && path.starts_with("/shadow_com") {
                if bad_token {
                    ("403 Forbidden", "text/plain", "bad token".to_string())
                } else {
                    match serde_json::from_str::<Vec<TrigSummary>>(&body) {
                        Ok(trigs) => {
                            let n = trigs.len();
                            let mut d = dash.lock().unwrap();
                            d.shadow_com = trigs;
                            d.shadow_com_updated = d.live.updated_iso.clone();
                            ("200 OK", "application/json", json!({"ok": true, "n": n}).to_string())
                        }
                        Err(e) => ("400 Bad Request", "text/plain", format!("parse: {e}")),
                    }
                }
            } else if method == "POST" && path.starts_with("/live_com") {
                if bad_token {
                    ("403 Forbidden", "text/plain", "bad token".to_string())
                } else {
                    match serde_json::from_str::<LiveSnap>(&body) {
                        Ok(snap) => {
                            let mut d = dash.lock().unwrap();
                            d.live_com_updated = snap.updated_iso.clone();
                            d.live_com = snap;
                            ("200 OK", "application/json", json!({"ok": true}).to_string())
                        }
                        Err(e) => ("400 Bad Request", "text/plain", format!("parse: {e}")),
                    }
                }
            } else if path.starts_with("/lwc.js") {
                ("200 OK", "application/javascript", LWC_JS.to_string())
            } else if path.starts_with("/stats") {
                ("200 OK", "application/json", dash.lock().unwrap().body_json())
            } else {
                ("200 OK", "text/html; charset=utf-8", HTML.to_string())
            };
            let resp = format!(
                "HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {}\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n\r\n{out}",
                out.len()
            );
            let _ = sock.write_all(resp.as_bytes()).await;
            let _ = sock.shutdown().await;
        });
    }
}

/// Read a full HTTP request: returns (method, path, x-token header, body).
async fn read_request(sock: &mut tokio::net::TcpStream) -> Option<(String, String, String, String)> {
    let mut buf: Vec<u8> = Vec::with_capacity(4096);
    let mut tmp = [0u8; 4096];
    // read until headers complete
    let header_end = loop {
        if let Some(pos) = find_subslice(&buf, b"\r\n\r\n") {
            break pos + 4;
        }
        let n = sock.read(&mut tmp).await.ok()?;
        if n == 0 {
            return None;
        }
        buf.extend_from_slice(&tmp[..n]);
        if buf.len() > 1_048_576 {
            return None; // 1MB cap
        }
    };
    let head = String::from_utf8_lossy(&buf[..header_end]).to_string();
    let mut lines = head.lines();
    let req_line = lines.next().unwrap_or("");
    let mut parts = req_line.split_whitespace();
    let method = parts.next().unwrap_or("GET").to_string();
    let path = parts.next().unwrap_or("/").to_string();
    let mut content_length = 0usize;
    let mut token = String::new();
    for l in lines {
        let ll = l.to_ascii_lowercase();
        if let Some(v) = ll.strip_prefix("content-length:") {
            content_length = v.trim().parse().unwrap_or(0);
        } else if ll.starts_with("x-token:") {
            token = l[8..].trim().to_string();
        }
    }
    let mut body = buf[header_end..].to_vec();
    while body.len() < content_length {
        let n = sock.read(&mut tmp).await.ok()?;
        if n == 0 {
            break;
        }
        body.extend_from_slice(&tmp[..n]);
        if body.len() > 4_194_304 {
            break;
        }
    }
    Some((method, path, token, String::from_utf8_lossy(&body).to_string()))
}

fn find_subslice(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// TradingView Lightweight Charts (vendored), served at /lwc.js
const LWC_JS: &str = include_str!("../static/lwc.js");

const HTML: &str = r#"<!doctype html><html><head><meta charset=utf-8>
<title>kalshi f1 term</title>
<style>
 body{background:#ffffff;color:#111111;font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:6px 10px}
 ::selection{background:#111;color:#fff}
 .bar{background:#f0f0f0;color:#111;padding:2px 10px;margin:-6px -10px 12px;border-bottom:1px solid #ccc;font-weight:600;white-space:pre-wrap}
 .bar .host{background:#111;color:#fff;padding:0 7px}
 .bar .dim{color:#888}
 .cur{display:inline-block;width:8px;background:#111;animation:bl 1.1s steps(1) infinite}
 @keyframes bl{50%{opacity:0}}
 .panes{display:flex;gap:10px;flex-wrap:wrap;align-items:stretch;margin-bottom:12px}
 .pane{border:1px solid #bbb;padding:8px 10px;min-width:230px;flex:1;background:#fff}
 .ptitle{color:#444;margin:-16px 0 6px 2px;background:#fff;display:inline-block;padding:0 6px;font-weight:700}
 .kv{display:flex;justify-content:space-between;gap:14px}
 .kv .k{color:#888}
 .kv .v{font-weight:700;text-align:right;white-space:nowrap;color:#111}
 .kv.big .v{font-size:16px}
 .green{color:#1a7f37}.red{color:#cf222e}.blue{color:#0969da}.yellow{color:#9a6700}.dim{color:#999}
 table{border-collapse:collapse;width:100%;font-size:12px;margin:2px 0 4px}
 th,td{text-align:right;padding:1px 6px}
 th{color:#555;font-weight:600;border-bottom:1px solid #999}
 td{border-bottom:1px solid #eee}
 td.l,th.l{text-align:left} .sep{border-left:1px solid #ccc}
 .reason{font-weight:700}
 button{background:#e6e6e6;color:#111;border:1px solid #bbb;padding:1px 10px;cursor:pointer;font:inherit;font-weight:600}
 .hint{color:#999;margin:3px 0 2px}
 .updrow td{background:#cf222e;color:#fff;text-align:center;font-weight:700;padding:2px 6px;border-bottom:none;letter-spacing:.4px}
 .fbar{display:flex;gap:14px;margin:6px 0 0;color:#777}
 .fbar b{color:#111;font-weight:700;margin-right:1px}
 .fbar .fl{background:#e6e6e6;color:#111;padding:0 8px;cursor:pointer;border:1px solid #ccc}
 .tip{position:absolute;z-index:60;display:none;background:#ffffff;color:#111;border:1px solid #888;box-shadow:3px 3px 0 rgba(0,0,0,.15);padding:8px 10px;font-size:12px;line-height:1.4;pointer-events:none;max-width:340px;min-width:230px}
 .tip h4{margin:0 0 5px;font-size:12px;color:#111;border-bottom:1px solid #ddd;padding-bottom:3px;font-weight:700}
 .tip h4 .mk{font-weight:400;color:#888;font-size:11px}
 .tip .seg{margin:5px 0 1px;font-weight:700}
 .tip .row{display:flex;justify-content:space-between;gap:16px}
 .tip .lbl{color:#888}
 .tip .none{color:#aaa;font-style:italic}
 .tip .green{color:#1a7f37}.tip .red{color:#cf222e}.tip .dim{color:#999}
</style></head><body>
<div class=bar><span class=host> kalshi-f1@prod </span> LIVE $5 real · subacct#1 · mirror f1_d50cap75 · loss-stop $30<span class=cur>&nbsp;</span>
<span id=sub class=dim>connecting…</span></div>
<div class=panes>
 <div class=pane><div class=ptitle>┌─[ money ]────────</div><div id=realpnl></div></div>
 <div class=pane><div class=ptitle>┌─[ sync ]────</div><div id=match></div></div>
 <div class=pane><div class=ptitle>┌─[ signal ]──────────</div><div id=live></div></div>
</div>
<div class=pane style="margin-bottom:9px">
<div class=ptitle>┌─[ equity: <span class=yellow>paper f6</span> · <span style="color:#d6409f">paper F1 (сплошная=книга движка, пунктир=real)</span> · <span class=green>shadow f6</span> · <span style="color:#cf222e">LIVE F1 real (старт от точки F1)</span> ]───</div>
<div id=chartwrap style="position:relative">
<div id=chart style="width:100%;height:340px"></div>
<div id=tip class=tip></div>
</div>
<div class=hint><span id=chartlbl></span></div>
<div class=fbar><span><b>1</b><span class=fl>Hover=Info</span></span><span><b>5</b><span class=fl onclick=load()>Refresh</span></span><span><b>7</b><label class=fl>FULL история <input type=checkbox id=fullhist onchange="if(_cmp){updateChart(_cmp);if(_chart)_chart.timeScale().fitContent();}"></label></span><span><b>9</b><label class=fl>Auto2s <input type=checkbox id=auto checked></label></span><span><b>10</b><span class=fl>Kalshi-F1</span></span></div>
</div>
<div class=pane>
<div class=ptitle>┌─[ windows: <span style="color:#d6409f">paper F1</span> | <span style="color:#cf222e">LIVE F1</span> | <span class=green>shadow f6</span> ]───</div>
<table><thead><tr><th class=l>window</th>
 <th class=sep>paper</th><th>entry</th><th>Δ</th><th>pnl</th>
 <th class=sep>LIVE</th><th>entry</th><th>Δ</th><th>pnl real$</th><th>=f1</th>
 <th class=sep>SHADOW</th><th>entry</th><th>Δ</th><th>=pa</th></tr></thead><tbody id=cmp></tbody></table>
</div>
<script src="/lwc.js"></script>
<script>
// Деплой-маркеры бота: визуально отделяют позиции ДО и ПОСЛЕ обновлений.
// Дописываются при каждом деплое (время UTC, минуты).
const UPDATES=[
 {t:'2026-07-05T21:00',label:'SIGNAL-ANCHOR (лимит от цены paper)'},
 {t:'2026-07-06T10:30',label:'ANCHOR-FREEZE (ретрай в ту же цену)'},
 {t:'2026-07-06T13:15',label:'WS BOOK FEED (книга пейпера real-time)'},
 {t:'2026-07-06T21:00',label:'KILL-HOURS: live скипает 04/08/14/22 UTC'},
 {t:'2026-07-08T16:30',label:'LIVE ON без стоп-лосса (по решению)'},
 {t:'2026-07-09T10:00',label:'KILL-HOURS → только 14 UTC (82д: 4/8/22 зелёные)'},
 {t:'2026-07-14T07:45',label:'LIVE ON: сабаккаунт пополнен +$100'},
 {t:'2026-07-16T10:30',label:'MIN_ENTRY: полоса входа 0.65-0.75 (фильтр эджа)'},
];
// нулевая точка zero-view БЕЗ визуального маркера (включение live 14.07 после пополнения):
// участвует только в выборе среза CUT, в UPDATES не входит => ни стрелки, ни строки в таблице.
const CHART_ZERO='2026-07-14T07:45';
// Live stake changes. Only the CHART divides by these, to keep one continuous $5
// scale across a stake step; the table/tooltip/cards always show the real money.
// Add a row here (window-aligned, UTC minutes) whenever STAKE changes on the box.
const STAKE_STEPS=[
 {t:'2026-07-27T12:45', f:5},   // STAKE $5 -> $25 (MAX_COUNT 15 -> 40, loss stop $130)
];
const curStake=()=>{let f=1;for(const s of STAKE_STEPS)f=s.f;return 5*f;};
const f=(x,d=2)=>x==null?'—':(+x).toFixed(d);
const money=v=>(v>=0?'+$':'-$')+Math.abs(v||0).toFixed(2);
const sc=s=>s=='YES'?'green':s=='NO'?'red':'dim';
function card(k,v,cls,big){return `<div class="kv ${big?'big':''}"><span class=k>${k}</span><span class="v ${cls||''}">${v}</span></div>`}
const rc=v=>v==null?'':(v>0?'green':v<0?'red':'');
const mk=(b)=>b===true?'<span class=green>✓</span>':b===false?'<span class=red>✗</span>':'<span class=dim>·</span>';
// $5 Kalshi-economics TWIN of a paper trade: run the paper signal through the SAME economics
// as the live bot (integer count=round($5/entry) cap 15, Kalshi fee on win AND loss, loss=-cost),
// then ×20 BOTH curves. This strips the accounting-model artifacts (fractional shares, Polymarket
// fee, -$100 flat loss) so paper-vs-LIVE shows only the REAL gap: slippage + no-fills.
function kfee(c,p){return (p<=0||p>=1||c<=0)?0:Math.ceil(0.07*c*p*(1-p)*10000)/10000;}
function twin5(entry,won){if(entry==null||entry<=0)return null;let c=Math.round(5/entry);if(c<1)c=1;if(c>15)c=15;const cost=c*entry+kfee(c,entry);return won?(c-cost):(-cost);}
let _chart=null, _S={}, _cmp=[], _tipTimer=null;
function initChart(){
 const el=document.getElementById('chart');
 _chart=LightweightCharts.createChart(el,{
   layout:{background:{type:'solid',color:'#ffffff'},textColor:'#777777',fontFamily:'ui-monospace,Menlo,monospace',fontSize:12},
   grid:{vertLines:{color:'#eeeeee'},horzLines:{color:'#eeeeee'}},
   rightPriceScale:{borderColor:'#cccccc'},
   timeScale:{borderColor:'#cccccc',timeVisible:true,secondsVisible:false},
   crosshair:{mode:LightweightCharts.CrosshairMode.Normal,vertLine:{color:'#999999',labelBackgroundColor:'#dddddd'},horzLine:{color:'#999999',labelBackgroundColor:'#dddddd'}},
 });
 const mk=(c,t,ls)=>_chart.addLineSeries({color:c,lineWidth:2,lineStyle:ls||0,title:t,priceLineVisible:false,lastValueVisible:true,
   priceFormat:{type:'custom',formatter:v=>(v>=0?'+$':'-$')+Math.abs(v).toFixed(0)}});
 _S.paper=mk('#e3a008','paper f6'); _S.com=mk('#2da44e','SHADOW f6'); _S.f1=mk('#d6409f','F1'); _S.f1r=mk('#d6409f','F1 real',2); _S.lv=mk('#cf222e','LIVE');
 // zero baseline
 _S.paper.createPriceLine({price:0,color:'#cccccc',lineStyle:LightweightCharts.LineStyle.Dashed,lineWidth:1});
 new ResizeObserver(()=>_chart.applyOptions({width:el.clientWidth,height:340})).observe(el);
 _chart.applyOptions({width:el.clientWidth,height:340});
 setupTip();
}
// ---- hover tooltip: hold the cursor ~1.1s over a chart point to reveal that window's trade ----
function setupTip(){
 const tip=document.getElementById('tip');
 const wrap=document.getElementById('chartwrap');
 let curT=null,curX=0,curY=0;
 // crosshair only RECORDS the time/point under the cursor (does NOT hide the tip),
 // so the 2s auto-refresh redraw can't clobber a held tooltip.
 _chart.subscribeCrosshairMove(p=>{ if(p.time&&p.point){curT=p.time;curX=p.point.x;curY=p.point.y;} });
 // hide only on a REAL pointer move; reveal after ~1s of holding still.
 wrap.addEventListener('mousemove',()=>{
  tip.style.display='none';
  if(_tipTimer)clearTimeout(_tipTimer);
  _tipTimer=setTimeout(()=>{ if(curT!=null) showTip(curT,curX,curY); },1000);
 });
 wrap.addEventListener('mouseleave',()=>{
  if(_tipTimer){clearTimeout(_tipTimer);_tipTimer=null;}
  tip.style.display='none'; curT=null;
 });
}
function nearestRow(t){let best=null,bd=1e18;for(const r of (_cmp||[])){const ts=Math.floor(Date.parse(r.window+':00Z')/1000);if(!ts)continue;const d=Math.abs(ts-t);if(d<bd){bd=d;best=r;}}return best;}
function showTip(t,x,y){
 const tip=document.getElementById('tip'),r=nearestRow(t);
 if(!r){tip.style.display='none';return;}
 tip.innerHTML=renderTip(r);tip.style.display='block';
 const wrap=document.getElementById('chartwrap'),W=wrap.clientWidth,tw=tip.offsetWidth,th=tip.offsetHeight;
 let lx=x-tw/2;lx=Math.max(4,Math.min(lx,W-tw-4));
 let ty=y-th-14;if(ty<4)ty=y+18;
 tip.style.left=lx+'px';tip.style.top=ty+'px';
}
function tseg(name,side,entry,delta,p,pnl,res,note){
 if(!side)return `<div class=seg style="color:#6e7681">${name}: <span class=none>не торговал это окно</span></div>`;
 const col=side=='YES'?'#1a7f37':'#cf222e';
 const rcol=res=='WIN'?'green':res=='LOSS'?'red':'dim';
 let h=`<div class=seg style="color:${col}">${name}: ${side}${note||''}</div>`;
 h+=`<div class=row><span class=lbl>entry</span><span>${f(entry)}</span></div>`;
 h+=`<div class=row><span class=lbl>Δ от open</span><span>${f(delta,1)}</span></div>`;
 h+=`<div class=row><span class=lbl>p-model</span><span>${f(p,3)}</span></div>`;
 if(res)h+=`<div class=row><span class=lbl>исход</span><span class=${rcol}>${res}</span></div>`;
 if(pnl!=null)h+=`<div class=row><span class=lbl>pnl</span><span class="${pnl>=0?'green':'red'}">${pnl>=0?'+':''}${f(pnl)}</span></div>`;
 return h;
}
function renderTip(r){
 const wt=r.window.slice(5).replace('T',' ');
 const tkr=r.com_ticker||r.pa_slug||'';
 let h=`<h4>окно ${wt} <span class=mk>${tkr}</span></h4>`;
 // LIVE = real fill (raw $5 money, actual count + actual fee)
 const lvres=r.lv_result||(r.lv_won===true?'WIN':r.lv_won===false?'LOSS':null);
 h+=tseg('🔴 LIVE real'+(r.lv_count?` ${r.lv_count}×`:''),r.lv_side,r.lv_entry,r.lv_delta,r.lv_p,r.lv_pnl,lvres,'');
 const cnt=r.com_count?` ${r.com_count}×`:'';
 const lres=r.com_result||(r.com_won===true?'WIN':r.com_won===false?'LOSS':null);
 // show the $5-normalized pnl (twin5 on the fill entry), not raw com_pnl which is a $100 shadow row
 const ltw=(r.com_pnl!=null&&r.com_entry)?twin5(r.com_entry,(r.com_won!=null?r.com_won:r.com_pnl>0)):r.com_pnl;
 const lnote=(r.com_pnl!=null&&Math.abs(r.com_pnl-ltw)>0.5)?` <span class=lbl>(raw $100-shadow ${r.com_pnl>=0?'+':''}${f(r.com_pnl,0)})</span>`:'';
 h+=tseg('🟢 SHADOW f6 $5'+cnt,r.com_side,r.com_entry,r.com_delta,r.com_p,ltw,lres,lnote);
 const ptw=(r.pa_pnl!=null&&r.pa_entry)?twin5(r.pa_entry,r.pa_pnl>0):null;
 const pnote=ptw!=null?` <span class=lbl>(orig $100-model ${r.pa_pnl>=0?'+':''}${f(r.pa_pnl,0)})</span>`:'';
 h+=tseg('📄 PAPER f6 $5-twin',r.pa_side,r.pa_entry,r.pa_delta,r.pa_p,ptw,r.pa_result,pnote);
 const mm=b=>b===true?'<span class=green>✓</span>':b===false?'<span class=red>✗</span>':'<span class=dim>—</span>';
 h+=`<div class=row style="margin-top:6px;border-top:1px solid #30363d;padding-top:5px"><span class=lbl>LIVE↔paper-F1</span><span>${mm(r.match_lv)}</span></div>`;
 h+=`<div class=row><span class=lbl>SHADOW↔paper f6</span><span>${mm(r.match_com)}</span></div>`;
 return h;
}
function updateChart(cmp){
 if(typeof LightweightCharts==='undefined'){return}
 if(!_chart) initChart();
 _cmp=cmp;
 // BOTH lines normalized to $5: paper via twin5, and shadow/LIVE ALSO via twin5 on its own
 // fill entry (com_entry) — NOT raw com_pnl. The bot's shadow path (emit_trigger) sizes at
 // cfg.stake=$100 while live fills size at $5, so raw com_pnl silently mixes $5 and $100 rows;
 // twin5(com_entry,won) re-prices every row at a uniform $5 so the curve is apples-to-apples.
 for(const r of cmp){
  r.pa_twin=(r.pa_pnl!=null&&r.pa_entry)?twin5(r.pa_entry,r.pa_pnl>0):null;
  r.com_twin=(r.com_pnl!=null&&r.com_entry)?twin5(r.com_entry,(r.com_won!=null?r.com_won:r.com_pnl>0)):null;
  r.f1_twin=(r.f1_pnl!=null&&r.f1_entry)?twin5(r.f1_entry,r.f1_pnl>0):null;
  // F1 real: тот же исход paper, но по РЕАЛЬНОЙ цене нашего филла (lv_entry).
  // Окна без реального филла (фантомы устаревшей книги / пропуски) = нет сделки.
  r.f1_real=(r.f1_pnl!=null&&r.lv_entry!=null)?twin5(r.lv_entry,r.f1_pnl>0):null;
 }
 // ---- режим отображения ----
 // По умолчанию: "с апдейта" — только окна с момента ПОСЛЕДНЕГО деплой-маркера (UPDATES),
 // и каждая из 5 линий стартует с $0 в этой точке. Общий ноль = честное сравнение роста
 // после обновы, без исторического груза. Чекбокс "FULL история" (нижняя панель) — прежний
 // вид: вся история, LIVE заякорен к розовой точке F1.
 const full=!!(document.getElementById('fullhist')&&document.getElementById('fullhist').checked);
 const CUT=[...UPDATES.map(u=>u.t),CHART_ZERO].sort().pop()||'';
 const rowsAll=cmp.slice().reverse();
 // r.window и CUT — один формат 'YYYY-MM-DDTHH:MM', лексикографика = хронология.
 // Когда 300-строчный кап уедет за CUT, фильтр станет no-op — но к тому моменту
 // появится и более свежий маркер очередного деплоя.
 const rows=full?rowsAll:rowsAll.filter(r=>r.window>=CUT);
 // gate=true would accumulate only on both-resolved windows; we plot ungated.
 const build=(k,sc,gate)=>{sc=sc||1;let t=0;const out=[];let last=null;for(const r of rows){const ts=Math.floor(Date.parse(r.window+':00Z')/1000); if(!ts)continue; const both=(r.pa_pnl!=null&&r.com_pnl!=null); if(r[k]!=null&&(!gate||both))t+=r[k]*sc; if(ts!==last){out.push({time:ts,value:+t.toFixed(2)});last=ts;}else if(out.length){out[out.length-1].value=+t.toFixed(2);}} return out;};
 const tot=(k,sc,gate)=>{const a=build(k,sc,gate);return a.length?a[a.length-1].value:0};
 // Every trade is its own step on each line, so all paper losses show and the curve never
 // goes flat just because the other side no-filled. Both lines run identical $5 Kalshi
 // economics (integer contracts, Kalshi fee on win+loss, loss=-cost) via twin5 on each
 // side's own fill entry, so the remaining paper-vs-LIVE gap is ONLY real slippage +
 // no-fills, never a stake-scale artifact.
 _S.paper.setData(build('pa_twin')); _S.com.setData(build('com_twin')); _S.f1.setData(build('f1_twin')); _S.f1r.setData(build('f1_real'));
 try{_S.paper.setMarkers((full?UPDATES:UPDATES.filter(u=>u.t>=CUT)).map(u=>({time:Math.floor(Date.parse(u.t+':00Z')/1000),position:'belowBar',color:'#cf222e',shape:'arrowUp',text:'upd'})));}catch(e){}
 // LIVE line: REAL $5 fills (raw lv_pnl, no twin5 re-pricing).
 //  - "с апдейта": кумулятив от $0 с точки среза — общий ноль со всеми линиями; плоские
 //    участки = окна без входа (kill-hours / no-fill / p-фильтр).
 //  - FULL: заякорен к розовой кумулятиве F1 строго ПЕРЕД первым видимым live-окном, так
 //    разрыв LIVE-vs-pink = чистая экзекуция (slippage + no-fills + реальные fees).
 // NOTE: even at zero slippage LIVE needn't sit exactly on pink — twin5 models
 // round($5/e) cap 15 + modeled fee vs the ACTUAL filled count and actual fee.
 // The live stake is not constant over history, so the CHART divides each fill by
 // the stake in force at that window. Without this a stake step would put a jump
 // in the curve that looks like performance but is only position size, and it
 // would break comparability with the $5 twins. The TABLE, the tooltip and the
 // cards keep the RAW real money — only this line is rescaled.
 const lvScale=w=>{let f=1;for(const s of STAKE_STEPS){if(w>=s.t)f=s.f;}return f;};
 const lvSeries=[];{
  let f1c=0, anchor=full?null:0, lt=0, last=null;
  for(const r of rows){
   const ts=Math.floor(Date.parse(r.window+':00Z')/1000); if(!ts)continue;
   if(full){
    if(anchor===null && r.lv_pnl!=null) anchor=+f1c.toFixed(2); // pink cum STRICTLY BEFORE this window (0 if none)
    if(r.f1_twin!=null) f1c+=r.f1_twin;
   }
   if(anchor!==null){
    if(r.lv_pnl!=null) lt+=r.lv_pnl/lvScale(r.window);
    const v=+(anchor+lt).toFixed(2);
    if(ts!==last){lvSeries.push({time:ts,value:v});last=ts;}
    else if(lvSeries.length){lvSeries[lvSeries.length-1].value=v;}
   }
  }
 }
 _S.lv.setData(lvSeries);
 const paw=rows.filter(r=>r.pa_pnl!=null).length, cow=rows.filter(r=>r.com_pnl!=null).length, lvw=rows.filter(r=>r.lv_pnl!=null).length;
 const lvSum=rows.reduce((a,r)=>a+(r.lv_pnl||0),0);
 const f1rw=rows.filter(r=>r.f1_real!=null).length;
 document.getElementById('chartlbl').textContent=full
  ?`(FULL история · paper/F1/shadow = uniform $5 twin · paper f6 ${money(tot('pa_twin'))}/${paw}w · shadow f6 ${money(tot('com_twin'))}/${cow}w · paper F1 ${money(tot('f1_twin'))} (книга движка) vs F1 real ${money(tot('f1_real'))}/${f1rw}w (пунктир — по нашим реальным ценам) · LIVE F1 РЕАЛЬНЫЕ ${money(lvSum)}/${lvw}w — линия в $5-масштабе (стейк сейчас $${curStake()}), таблица в реальных $)`
  :`(с ${CUT.replace('T',' ')}Z — все линии от $0 · paper f6 ${money(tot('pa_twin'))}/${paw}w · shadow f6 ${money(tot('com_twin'))}/${cow}w · paper F1 ${money(tot('f1_twin'))} vs F1 real ${money(tot('f1_real'))}/${f1rw}w (пунктир — наши реальные цены) · LIVE РЕАЛЬНЫЕ ${money(lvSum)}/${lvw}w — линия в $5-масштабе (стейк $${curStake()}), таблица в реальных $ · вся история — чекбокс FULL внизу)`;
}
let _loading=false;
async function load(){
 // The refresh timer fires every 2s but a full /stats round-trip can exceed that.
 // Without this guard the requests stack until the browser's per-host connection
 // limit is saturated and the page stops updating entirely.
 if(_loading)return; _loading=true;
 try{const r=await fetch('/stats');const d=await r.json();const S=d.summary;
 // prefer the EU mirror's REAL binance.com signal; fall back to Buffalo's binance.US shadow
 const L=(d.live_com&&d.live_com.market)?d.live_com:d.live;
 const feedlbl=(d.live_com&&d.live_com.market)?'binance.com · EU mirror (REAL signal we trade)':(L.binance_feed+' · US shadow');
 document.getElementById('sub').textContent=`signal ${L.updated_iso} · ${feedlbl} · since ${d.started} · LIVE push ${d.live_com_updated||'—'} · paper ${d.paper_agg.updated||'—'}`;
 // ===== authoritative REAL money (pushed from the bot, fees+slippage in) + period paper-vs-live =====
 const lc=d.live_com||{};
 let pT=0,shT=0,nSh=0,f1T=0,f1Same=0,lvP=0,nLv=0,wLv=0;
 for(const c of d.compare){
  if(c.pa_pnl!=null&&c.pa_entry){pT+=twin5(c.pa_entry,c.pa_pnl>0);}
  if(c.com_pnl!=null&&c.com_entry){shT+=twin5(c.com_entry,(c.com_won!=null?c.com_won:c.com_pnl>0));nSh++;}
  if(c.f1_pnl!=null&&c.f1_entry){f1T+=twin5(c.f1_entry,c.f1_pnl>0);}
  if(c.lv_pnl!=null){lvP+=c.lv_pnl;nLv++;if(c.lv_pnl>0)wLv++;
   // F1-twin over the SAME windows live filled — the honest execution-efficiency base
   if(c.f1_pnl!=null&&c.f1_entry)f1Same+=twin5(c.f1_entry,c.f1_pnl>0);}
 }
 const edge=f1Same!=0?Math.round(100*lvP/f1Same):null;
 document.getElementById('realpnl').innerHTML=
   card('💵 LIVE real · ALL-TIME',money(lc.total_pnl),rc(lc.total_pnl),true)
  +card('LIVE real · today',money(lc.day_pnl),rc(lc.day_pnl),true)
  +card('🔴 LIVE F1 (period, real $)',`${money(lvP)} · ${nLv}w · WR ${nLv?Math.round(100*wLv/nLv):0}%`,rc(lvP))
  +card('F1 $5-twin (period)',money(f1T),rc(f1T))
  +card('shadow f6 twin (period)',`${money(shT)} · ${nSh}w`,rc(shT))
  +card('paper f6 $5-twin (period)',money(pT),rc(pT))
  +card('live = % of F1-twin (те же окна)',edge!=null?`${edge}%`:'—',edge==null?'dim':edge>=90?'green':edge>=70?'yellow':'red');
 document.getElementById('match').innerHTML=
   card('LIVE ↔ paper-F1 side-match',`${f(S.lv_pct,1)}%`,S.lv_pct>=90?'green':S.lv_pct>=70?'yellow':'red',true)
  +card('LIVE: matched / оба торговали',`${S.lv_match||0} / ${S.lv_total||0}`,'')
  +card('LIVE fills (period)',`${S.lv_positions||0} · ${money(S.lv_pnl||0)}`,rc(S.lv_pnl))
  +card('shadow f6 ↔ paper f6 side-match',`${f(S.com_pct,1)}%`,S.com_pct>=90?'green':S.com_pct>=70?'yellow':'red',true)
  +card('shadow f6: matched / windows',`${S.com_match} / ${S.com_total}`,'green')
  +card('positions P/F1/SH',`${d.paper_agg.positions}/${d.paper_f1_agg.positions}/${S.com_positions}`);
 document.getElementById('live').innerHTML=
  card('reason',`<span class="reason ${L.reason&&L.reason.startsWith('BUY')?'green':'yellow'}">${L.reason||'—'}</span>`)
  +card('market',L.market||'—')+card('BTC',f(L.btc,0))
  +card('Δ open',f(L.delta_open),rc(L.delta_open))+card('τ',f(L.tau,2))+card('elapsed',f(L.elapsed,1))
  +card('σ',f(L.sigma_max30,6))+card('p',f(L.p,3))+card('yes/no ask',f(L.yes_ask)+'/'+f(L.no_ask));
 updateChart(d.compare);
 {
  const rows=d.compare.slice(0,60);
  let html='';let prevW=null;
  for(const c of rows){
   for(const u of UPDATES){
    // маркер стоит между окном новее (prevW) и текущим окном (или над самым свежим)
    if((prevW===null&&u.t>=c.window)||(prevW!==null&&u.t<prevW&&u.t>=c.window)){
     html+=`<tr class=updrow><td colspan=14>─── ОБНОВЛЕНИЕ ${u.t.slice(5).replace('T',' ')}Z · ${u.label} ───</td></tr>`;
    }
   }
   html+=`<tr><td class=l>${c.window}</td>
    <td class="sep ${sc(c.f1_side)}">${c.f1_side||'—'}</td><td>${f(c.f1_entry)}</td><td>${f(c.f1_delta,1)}</td><td class="${rc(c.f1_pnl)}">${c.f1_pnl==null?'…':(c.f1_pnl>=0?'+':'')+f(c.f1_pnl)}</td>
    <td class="sep ${sc(c.lv_side)}">${c.lv_side||'—'}</td><td>${f(c.lv_entry)}</td><td>${f(c.lv_delta,1)}</td><td class="${rc(c.lv_pnl)}">${c.lv_pnl==null?(c.lv_side?'…':'—'):(c.lv_pnl>=0?'+':'')+f(c.lv_pnl)}</td><td>${mk(c.match_lv)}</td>
    <td class="sep ${sc(c.com_side)}">${c.com_side||'—'}</td><td>${f(c.com_entry)}</td><td>${f(c.com_delta,1)}</td><td>${mk(c.match_com)}</td></tr>`;
   prevW=c.window;
  }
  document.getElementById('cmp').innerHTML=html;
 }
 }catch(e){document.getElementById('sub').textContent='fetch error: '+e}
 finally{_loading=false}
}
// 5s, not 2s: /stats now carries a month of chart history, so polling faster than
// the response takes just re-downloads the same payload continuously. The windows
// this dashboard tracks are 15 minutes long — 5s is well inside "live".
load();setInterval(()=>{if(document.getElementById('auto').checked)load()},5000);
</script></body></html>"#;

#[cfg(test)]
mod tests {
    use super::*;

    // AC-1 (merge blocker): old producers omit `live` — absent key MUST read false
    // (all-shadow = exact pre-feature meaning). Wrong-TYPE live must fail the parse
    // (strict): silently mis-splitting real-money rows from shadow rows is worse
    // than rejecting the payload (handler keeps the last good feed on 400).
    #[test]
    fn trig_summary_live_serde() {
        let t: TrigSummary = serde_json::from_str("{}").unwrap();
        assert!(!t.live);
        let v: Vec<TrigSummary> = serde_json::from_str(
            r#"[{"ticker":"T","side":"yes","entry":0.85,"count":6,"live":true}]"#,
        )
        .unwrap();
        assert!(v[0].live);
        assert!(serde_json::from_str::<Vec<TrigSummary>>(r#"[{"live":"true"}]"#).is_err());
        assert!(serde_json::from_str::<Vec<TrigSummary>>(r#"[{"live":1}]"#).is_err());
    }

    fn row(live: bool) -> TrigSummary {
        TrigSummary {
            ticker: "T".into(),
            side: "yes".into(),
            entry: 0.85, // twin signal entry == live eff (the ambiguous case)
            live,
            ..Default::default()
        }
    }

    // AC-2 (merge blocker): equal-entry twin+live rows — each resolve updates ONLY
    // the row with its own flag, in EITHER settle order.
    #[test]
    fn resolve_disambiguates_by_live_flag_order_independent() {
        for twin_first in [true, false] {
            let mut d = Dash {
                triggers: vec![row(false), row(true)],
                ..Default::default()
            };
            let calls: Vec<(bool, f64)> = if twin_first {
                vec![(false, 13.0), (true, 0.83)]
            } else {
                vec![(true, 0.83), (false, 13.0)]
            };
            for (live, pnl) in calls {
                d.resolve("T", "yes", 0.85, "yes", true, pnl, live);
            }
            let twin = d.triggers.iter().find(|t| !t.live).unwrap();
            let lv = d.triggers.iter().find(|t| t.live).unwrap();
            assert_eq!(twin.pnl, Some(13.0), "twin_first={twin_first}");
            assert_eq!(lv.pnl, Some(0.83), "twin_first={twin_first}");
        }
    }

    // A twin resolve with no matching twin row is a NO-OP (must not touch the live row).
    #[test]
    fn resolve_no_matching_flag_is_noop() {
        let mut d = Dash { triggers: vec![row(true)], ..Default::default() };
        d.resolve("T", "yes", 0.85, "yes", true, 13.0, false);
        assert_eq!(d.triggers[0].pnl, None);
    }

    // Two rows with the SAME flag: first unresolved match takes the result (break).
    #[test]
    fn resolve_same_flag_first_match_breaks() {
        let mut d = Dash { triggers: vec![row(false), row(false)], ..Default::default() };
        d.resolve("T", "yes", 0.85, "yes", true, 13.0, false);
        assert_eq!(d.triggers[0].pnl, Some(13.0));
        assert_eq!(d.triggers[1].pnl, None);
    }
    // ---- slice 2: compare() split + body_json lv aggregates ----

    fn trig(w: &str, side: &str, entry: f64, pnl: Option<f64>, live: bool) -> TrigSummary {
        TrigSummary {
            window_start: format!("{w}:00+00:00"),
            ticker: "T".into(),
            side: side.into(),
            entry,
            pnl,
            result: pnl.map(|_| "yes".to_string()),
            live,
            ..Default::default()
        }
    }
    fn pf1(w: &str, side: &str, pnl: Option<f64>) -> PaperTrade {
        PaperTrade {
            window_start: format!("{w}:00+00:00"),
            side: side.into(),
            entry_price: 0.8,
            pnl,
            result: pnl.map(|_| "WIN".to_string()),
            ..Default::default()
        }
    }

    // AC-3/AC-4: com_* exclusively from live=false, lv_* exclusively from live=true;
    // one-sided windows leave the other side null.
    #[test]
    fn compare_splits_shadow_and_live() {
        let mut d = Dash {
            started_iso: "2026-07-05T00:00".into(),
            ..Default::default()
        };
        // W1: both twin (signal 0.85) and live fill (eff 0.87)
        d.shadow_com.push(trig("2026-07-05T12:00", "yes", 0.85, Some(13.0), false));
        d.shadow_com.push(trig("2026-07-05T12:00", "yes", 0.87, Some(0.55), true));
        // W2: twin only (live no-fill — the flat-gap window)
        d.shadow_com.push(trig("2026-07-05T12:15", "no", 0.70, Some(-70.0), false));
        // W3: live only (degenerate)
        d.shadow_com.push(trig("2026-07-05T12:30", "no", 0.75, None, true));
        d.paper_f1.push(pf1("2026-07-05T12:00", "YES", Some(24.0)));
        d.paper_f1.push(pf1("2026-07-05T12:15", "NO", Some(-100.0)));
        let cmp = d.compare();
        let row = |w: &str| cmp.iter().find(|c| c["window"] == w).unwrap().clone();
        let w1 = row("2026-07-05T12:00");
        assert_eq!(w1["com_entry"], 0.85);
        assert_eq!(w1["lv_entry"], 0.87);
        assert_eq!(w1["com_pnl"], 13.0);
        assert_eq!(w1["lv_pnl"], 0.55);
        assert_eq!(w1["match_lv"], true);
        let w2 = row("2026-07-05T12:15");
        assert_eq!(w2["com_entry"], 0.70);
        assert!(w2["lv_side"].is_null(), "no-fill window must have null lv_*");
        assert!(w2["match_lv"].is_null(), "lv absent -> excluded from denominator");
        let w3 = row("2026-07-05T12:30");
        assert!(w3["com_side"].is_null());
        assert_eq!(w3["lv_entry"], 0.75);
        assert!(w3["lv_pnl"].is_null(), "unresolved live row keeps lv_pnl null");
        assert!(w3["match_lv"].is_null(), "no paper-F1 -> match_lv excluded");
    }

    // Summary aggregates: lv_positions counts fills (resolved or not); lv_pnl sums
    // only resolved raw pnl; all-shadow feed -> every lv aggregate zero.
    #[test]
    fn body_json_lv_aggregates() {
        let mut d = Dash {
            started_iso: "2026-07-05T00:00".into(),
            ..Default::default()
        };
        d.shadow_com.push(trig("2026-07-05T12:00", "yes", 0.87, Some(0.55), true));
        d.shadow_com.push(trig("2026-07-05T12:15", "no", 0.75, None, true));
        d.shadow_com.push(trig("2026-07-05T12:00", "yes", 0.85, Some(13.0), false));
        let v: serde_json::Value = serde_json::from_str(&d.body_json()).unwrap();
        assert_eq!(v["summary"]["lv_positions"], 2);
        assert_eq!(v["summary"]["lv_pnl"], 0.55);
        assert_eq!(v["summary"]["com_positions"], 1); // shadow rows only now
        let empty: serde_json::Value =
            serde_json::from_str(&Dash { started_iso: "2026-07-05T00:00".into(), ..Default::default() }.body_json())
                .unwrap();
        assert_eq!(empty["summary"]["lv_positions"], 0);
        assert_eq!(empty["summary"]["lv_pnl"], 0.0);
    }
}

