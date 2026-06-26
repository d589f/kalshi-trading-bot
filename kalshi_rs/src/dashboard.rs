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
#[derive(Default, Clone, Serialize)]
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
    /// triggers from the SECOND shadow (EU box, Binance.com feed), pushed via POST /shadow_com
    pub shadow_com: Vec<TrigSummary>,
    pub shadow_com_updated: String,
    pub started_iso: String,
}

fn wkey(s: &str) -> String {
    s.chars().take(16).collect() // "YYYY-MM-DDTHH:MM"
}

impl Dash {
    /// Update the matching live trigger with its settlement.
    pub fn resolve(&mut self, ticker: &str, side: &str, entry: f64, result: &str, won: bool, pnl: f64) {
        for t in self.triggers.iter_mut() {
            if t.ticker == ticker && t.side == side && (t.entry - entry).abs() < 1e-9 && t.result.is_none() {
                t.result = Some(result.to_string());
                t.won = Some(won);
                t.pnl = Some(pnl);
                break;
            }
        }
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
                let c = self.shadow_com.iter().find(|t| wkey(&t.window_start) == *w);
                let match_us = match (s, p) {
                    (Some(s), Some(p)) => Some(s.side.eq_ignore_ascii_case(&p.side)),
                    _ => None,
                };
                let match_com = match (c, p) {
                    (Some(c), Some(p)) => Some(c.side.eq_ignore_ascii_case(&p.side)),
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
                })
            })
            .collect();
        out.truncate(60);
        out
    }

    fn body_json(&self) -> String {
        let cmp = self.compare();
        // side-agreement summary among windows where BOTH paper and that shadow fired
        let (mut us_n, mut us_ok, mut com_n, mut com_ok) = (0i64, 0i64, 0i64, 0i64);
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
        }
        let pct = |ok: i64, n: i64| if n > 0 { (1000.0 * ok as f64 / n as f64).round() / 10.0 } else { 0.0 };
        json!({
            "live": self.live,
            "agg": self.agg(),
            "paper_agg": self.paper_agg(),
            "compare": cmp,
            "summary": {
                "us_match": us_ok, "us_total": us_n, "us_pct": pct(us_ok, us_n),
                "com_match": com_ok, "com_total": com_n, "com_pct": pct(com_ok, com_n),
                "com_positions": self.shadow_com.iter().filter(|t| t.window_start.as_str() >= self.started_iso.as_str()).count(),
                "com_updated": self.shadow_com_updated,
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
            let (status, ctype, out) = if method == "POST" && path.starts_with("/paper") {
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
<title>Kalshi f6 — feeds vs paper</title>
<style>
 body{background:#0d1117;color:#c9d1d9;font:13px/1.5 ui-monospace,Menlo,monospace;margin:0;padding:18px}
 h1{font-size:16px;margin:14px 0 6px} .sub{color:#8b949e;margin-bottom:12px}
 .grid{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
 .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:10px 13px;min-width:115px}
 .big{min-width:175px}
 .k{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:.5px} .v{font-size:19px;font-weight:600;margin-top:2px} .vb{font-size:26px}
 .green{color:#3fb950}.red{color:#f85149}.blue{color:#58a6ff}.yellow{color:#d29922}.dim{color:#8b949e}
 table{border-collapse:collapse;width:100%;font-size:12px;margin-bottom:18px} th,td{text-align:right;padding:4px 7px;border-bottom:1px solid #21262d}
 th{color:#8b949e;font-weight:500} td.l,th.l{text-align:left} .sep{border-left:2px solid #30363d}
 .reason{font-size:15px;font-weight:600} button{background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:6px 12px;cursor:pointer}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;margin-right:6px;animation:p 1.5s infinite}@keyframes p{50%{opacity:.3}}
</style></head><body>
<h1><span class=dot></span>Kalshi f6_wait270 — LIVE $5 (real orders) vs paper</h1>
<div class=sub id=sub>connecting…</div>
<div class=grid id=match></div>
<div class=grid id=live></div>
<h1>Cumulative PnL: <span class=yellow>paper</span> · <span class=green>LIVE ($5→$100-eq, ×20)</span> · <span class=blue>US</span> <span id=chartlbl class=dim></span></h1>
<div id=chart style="width:100%;height:340px;border:1px solid #30363d;border-radius:8px;overflow:hidden"></div>
<div style="margin:8px 0"><button onclick=load()>↻ refresh</button> <label><input type=checkbox id=auto checked> auto 2s</label></div>
<h1>Per-window: <span class=yellow>PAPER</span> vs <span class=blue>US (binance.us)</span> vs <span class=green>LIVE $5 (binance.com, real)</span></h1>
<table><thead><tr><th class=l>window</th>
 <th class=sep>paper</th><th>entry</th><th>Δ</th><th>pnl</th>
 <th class=sep>US</th><th>entry</th><th>Δ</th><th>=pa</th>
 <th class=sep>LIVE</th><th>entry</th><th>Δ</th><th>=pa</th></tr></thead><tbody id=cmp></tbody></table>
<script src="/lwc.js"></script>
<script>
const f=(x,d=2)=>x==null?'—':(+x).toFixed(d);
const sc=s=>s=='YES'?'green':s=='NO'?'red':'dim';
function card(k,v,cls,big){return `<div class="card ${big?'big':''}"><div class=k>${k}</div><div class="v ${big?'vb':''} ${cls||''}">${v}</div></div>`}
const rc=v=>v==null?'':(v>0?'green':v<0?'red':'');
const mk=(b)=>b===true?'<span class=green>✓</span>':b===false?'<span class=red>✗</span>':'<span class=dim>·</span>';
let _chart=null, _S={};
function initChart(){
 const el=document.getElementById('chart');
 _chart=LightweightCharts.createChart(el,{
   layout:{background:{type:'solid',color:'#0d1117'},textColor:'#c9d1d9',fontFamily:'ui-monospace,Menlo,monospace',fontSize:12},
   grid:{vertLines:{color:'#1c2128'},horzLines:{color:'#1c2128'}},
   rightPriceScale:{borderColor:'#30363d'},
   timeScale:{borderColor:'#30363d',timeVisible:true,secondsVisible:false},
   crosshair:{mode:LightweightCharts.CrosshairMode.Normal,vertLine:{color:'#8b949e',labelBackgroundColor:'#30363d'},horzLine:{color:'#8b949e',labelBackgroundColor:'#30363d'}},
 });
 const mk=(c,t)=>_chart.addLineSeries({color:c,lineWidth:2,title:t,priceLineVisible:false,lastValueVisible:true,
   priceFormat:{type:'custom',formatter:v=>(v>=0?'+$':'-$')+Math.abs(v).toFixed(0)}});
 _S.paper=mk('#e3a008','paper'); _S.com=mk('#3fb950','COM'); _S.us=mk('#58a6ff','US');
 // zero baseline
 _S.paper.createPriceLine({price:0,color:'#555',lineStyle:LightweightCharts.LineStyle.Dashed,lineWidth:1});
 new ResizeObserver(()=>_chart.applyOptions({width:el.clientWidth,height:340})).observe(el);
 _chart.applyOptions({width:el.clientWidth,height:340});
}
function updateChart(cmp){
 if(typeof LightweightCharts==='undefined'){return}
 if(!_chart) initChart();
 const rows=cmp.slice().reverse();
 const LSC=20; // LIVE $5 -> $100-equivalent so the curve scales alongside paper ($100)
 // gate=true → accumulate ONLY on windows where BOTH paper and LIVE resolved, so the
 // two curves are apples-to-apples (LIVE caps/halts no longer make paper look better).
 const build=(k,sc,gate)=>{sc=sc||1;let t=0;const out=[];let last=null;for(const r of rows){const ts=Math.floor(Date.parse(r.window+':00Z')/1000); if(!ts)continue; const both=(r.pa_pnl!=null&&r.com_pnl!=null); if(r[k]!=null&&(!gate||both))t+=r[k]*sc; if(ts!==last){out.push({time:ts,value:+t.toFixed(2)});last=ts;}else if(out.length){out[out.length-1].value=+t.toFixed(2);}} return out;};
 const tot=(k,sc,gate)=>{const a=build(k,sc,gate);return a.length?a[a.length-1].value:0};
 _S.paper.setData(build('pa_pnl',1,true)); _S.com.setData(build('com_pnl',LSC,true)); _S.us.setData(build('us_pnl'));
 const mw=rows.filter(r=>r.pa_pnl!=null&&r.com_pnl!=null).length;
 document.getElementById('chartlbl').textContent=`(matched windows only: ${mw} · paper $${Math.round(tot('pa_pnl',1,true))} vs LIVE $${Math.round(tot('com_pnl',LSC,true))} [×20→$100-eq · real $${tot('com_pnl',1,true).toFixed(2)}] · US $${Math.round(tot('us_pnl'))})`;
}
async function load(){
 try{const r=await fetch('/stats');const d=await r.json();const L=d.live,S=d.summary;
 document.getElementById('sub').textContent=`updated ${L.updated_iso} · since ${d.started} · US-feed ${L.binance_feed} · COM upd ${S.com_updated||'—'} · paper upd ${d.paper_agg.updated||'—'}`;
 document.getElementById('match').innerHTML=
   card('US ↔ paper side-match',`${f(S.us_pct,1)}%`,S.us_pct>=90?'green':S.us_pct>=70?'yellow':'red',true)
  +card('US: matched / windows',`${S.us_match} / ${S.us_total}`,'blue')
  +card('LIVE ↔ paper side-match',`${f(S.com_pct,1)}%`,S.com_pct>=90?'green':S.com_pct>=70?'yellow':'red',true)
  +card('LIVE: matched / windows',`${S.com_match} / ${S.com_total}`,'green')
  +card('positions P/US/COM',`${d.paper_agg.positions}/${d.agg.positions}/${S.com_positions}`);
 document.getElementById('live').innerHTML=
  card('US reason',`<span class="reason ${L.reason&&L.reason.startsWith('BUY')?'green':'yellow'}">${L.reason||'—'}</span>`)
  +card('market',L.market||'—')+card('BTC us',f(L.btc,0))
  +card('Δ open',f(L.delta_open),rc(L.delta_open))+card('τ',f(L.tau,2))+card('elapsed',f(L.elapsed,1))
  +card('σ max30',f(L.sigma_max30,6))+card('p',f(L.p,3))+card('yes/no ask',f(L.yes_ask)+'/'+f(L.no_ask));
 updateChart(d.compare);
 document.getElementById('cmp').innerHTML=d.compare.map(c=>
   `<tr><td class=l>${c.window}</td>
    <td class="sep ${sc(c.pa_side)}">${c.pa_side||'—'}</td><td>${f(c.pa_entry)}</td><td>${f(c.pa_delta,1)}</td><td class="${rc(c.pa_pnl)}">${c.pa_pnl==null?'…':(c.pa_pnl>=0?'+':'')+f(c.pa_pnl)}</td>
    <td class="sep ${sc(c.us_side)}">${c.us_side||'—'}</td><td>${f(c.us_entry)}</td><td>${f(c.us_delta,1)}</td><td>${mk(c.match_us)}</td>
    <td class="sep ${sc(c.com_side)}">${c.com_side||'—'}</td><td>${f(c.com_entry)}</td><td>${f(c.com_delta,1)}</td><td>${mk(c.match_com)}</td></tr>`).join('');
 }catch(e){document.getElementById('sub').textContent='fetch error: '+e}
}
load();setInterval(()=>{if(document.getElementById('auto').checked)load()},2000);
</script></body></html>"#;
