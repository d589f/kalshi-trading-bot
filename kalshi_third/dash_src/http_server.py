"""HTTP server and HTML templates."""
import json
import time
import socket
import sqlite3
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

from .config import HTTP_PORT, WINDOW_MINUTES, DB_PATH
from .state import (state, state_lock, orderbook, ob_lock,
                    conn_status, conn_lock, start_time, _clob_msg_count)
from .backtest import run_backtest, load_backtest_data
from .backtest_html import BACKTEST_HTML as BACKTEST_HTML_NEW
from .paper_trading import get_paper_state, configure_paper

try:
    import websockets
except ImportError:
    websockets = None


# ============================================================
# HTML TEMPLATES
# ============================================================

METRICS_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Market Metrics</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;margin:0;padding:0}
nav{text-align:center;padding:12px;background:#161b22;border-bottom:1px solid #30363d}
nav a{color:#58a6ff;text-decoration:none;padding:8px 20px;background:#0d1117;border-radius:6px;margin:0 4px;font-size:13px}
nav a:hover{background:#1c2333}
.controls{text-align:center;padding:12px;background:#161b22;border-bottom:1px solid #30363d}
.controls button{padding:8px 20px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-family:monospace;font-size:13px;margin:0 4px}
.controls button:hover{background:#2ea043}
.controls span{color:#8b949e;font-size:12px;margin-left:12px}
#status{color:#8b949e;font-size:11px}
.img-wrap{overflow-x:auto;overflow-y:hidden;width:100%;padding:8px;background:#0d1117}
.img-wrap img{height:calc(100vh - 110px);width:auto;max-width:none;display:block}
</style>
</head><body>
<nav>
<a href="/">LIVE</a>
<a href="/backtest">BACKTEST</a>
<a href="/history">HISTORY</a>
<a href="/metrics" style="color:#e040fb">METRICS</a>
<a href="/chart" style="color:#00d4aa">CHART</a>
</nav>
<div class="controls">
<button onclick="refresh()">Refresh Now</button>
<button onclick="toggleAuto()">Auto-refresh: <span id="autoLabel">OFF</span></button>
<span id="status">Last update: never</span>
</div>
<div class="img-wrap">
<img id="chart" src="/api/metrics/chart" alt="Loading...">
</div>
<script>
var autoInterval=null;
function refresh(){
document.getElementById('status').textContent='Generating...';
fetch('/api/metrics/generate',{method:'POST'}).then(r=>r.json()).then(d=>{
document.getElementById('chart').src='/api/metrics/chart?t='+Date.now();
document.getElementById('status').textContent='Last update: '+new Date().toLocaleTimeString();
});
}
function toggleAuto(){
if(autoInterval){clearInterval(autoInterval);autoInterval=null;document.getElementById('autoLabel').textContent='OFF'}
else{autoInterval=setInterval(refresh,60000);document.getElementById('autoLabel').textContent='60s';refresh()}
}
refresh();
</script></body></html>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>BTC Trading Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#e0e0e0;padding:8px}
h1{text-align:center;color:#4fc3f7;margin-bottom:6px;font-size:1.2em}
.top-grid{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;max-width:100%;margin:0 auto 6px auto;flex-shrink:0}
.card{background:#141b2d;border:1px solid #1e2a3a;border-radius:8px;padding:10px}
.card h2{color:#90caf9;font-size:0.85em;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
.metric{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid #1a2235}
.metric:last-child{border:none}
.label{color:#78909c;font-size:0.8em}
.value{font-weight:700;font-size:0.85em;font-family:'Cascadia Code',monospace}
.big{font-size:1.5em;text-align:center;padding:4px 0}
.green{color:#4caf50} .red{color:#f44336} .yellow{color:#ffc107} .blue{color:#42a5f5} .white{color:#fff}
.signal-box{text-align:center;padding:10px;border-radius:8px;margin-top:4px}
.signal-buy-no{background:#1b5e20;border:2px solid #4caf50}
.signal-buy-yes{background:#0d47a1;border:2px solid #42a5f5}
.signal-none{background:#1a1a2e;border:2px solid #333}
.signal-wait{background:#4a3000;border:2px solid #ff9800}
.signal-text{font-size:1.1em;font-weight:700}
.ofi-bar{height:16px;border-radius:4px;margin:3px 0;position:relative;overflow:hidden}
.ofi-bar-inner{height:100%;transition:width 0.3s}
.ts{color:#546e7a;font-size:0.7em;text-align:center;margin-top:4px}
.paper-section{max-width:100%;margin:8px auto;background:#141b2d;border:1px solid #1e2a3a;border-radius:8px;padding:12px}
.paper-section h2{color:#ffc107;font-size:1em;margin-bottom:8px}
.paper-stats{display:grid;grid-template-columns:repeat(8,1fr);gap:6px;margin-bottom:10px}
.paper-stat{background:#0a0e17;border:1px solid #1a2235;border-radius:6px;padding:8px;text-align:center}
.paper-stat .num{font-size:1.3em;font-weight:700;font-family:'Cascadia Code',monospace}
.paper-stat .lbl{color:#78909c;font-size:0.7em;margin-top:2px}
.paper-positions{width:100%;border-collapse:collapse;font-size:0.82em;margin-top:6px}
.paper-positions th{background:#1a2235;color:#78909c;padding:5px 8px;text-align:left;font-size:0.8em}
.paper-positions td{padding:4px 8px;border-bottom:1px solid #1a2235}
.paper-positions tr:hover{background:#1a2235}
.active-badge{display:inline-block;background:#1b5e20;color:#4caf50;padding:2px 8px;border-radius:10px;font-size:0.75em;font-weight:700}
.hist-win{color:#4caf50}.hist-loss{color:#f44336}
</style>
</head>
<body>
<div style="text-align:center;margin-bottom:12px"><a href="/" style="color:#4fc3f7;text-decoration:none;font-weight:700;padding:8px 24px;background:#1e2a3a;border-radius:6px;margin:0 4px">LIVE</a><a href="/backtest" style="color:#8b949e;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">BACKTEST</a><a href="/history" style="color:#ffc107;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">HISTORY</a><a href="/metrics" style="color:#e040fb;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">METRICS</a><a href="/chart" style="color:#00d4aa;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">CHART</a></div>
<h1>BTC """ + str(WINDOW_MINUTES) + """-MIN BINARY OPTIONS DASHBOARD</h1>
<div class="top-grid">

<div class="card">
<h2>Oracle Price (Chainlink)</h2>
<div class="big white" id="cl-price">--</div>
<div class="metric"><span class="label">Binance</span><span class="value" id="bn-price">--</span></div>
<div class="metric"><span class="label">Spread</span><span class="value" id="bn-spread">--</span></div>
</div>

<div class="card">
<h2>Delta (Price Move)</h2>
<div class="big" id="delta-prev">--</div>
<div class="metric"><span class="label">From window open</span><span class="value" id="delta-open">--</span></div>
<div class="metric"><span class="label">Prev close</span><span class="value" id="prev-close">--</span></div>
<div class="metric"><span class="label">Window open</span><span class="value" id="win-open">--</span></div>
</div>

<div class="card">
<h2>P-Model</h2>
<div class="big" id="p-model">--</div>
<div class="metric"><span class="label">SNR</span><span class="value" id="snr">--</span></div>
<div class="metric"><span class="label">Sigma</span><span class="value" id="sigma">--</span></div>
</div>

<div class="card">
<h2>OFI</h2>
<div class="metric"><span class="label">1m</span><span class="value" id="ofi-1">--</span></div>
<div class="ofi-bar"><div class="ofi-bar-inner" id="ofi-1-bar" style="width:50%;background:#666"></div></div>
<div class="metric"><span class="label">3m</span><span class="value" id="ofi-3">--</span></div>
<div class="metric"><span class="label">5m</span><span class="value" id="ofi-5">--</span></div>
<div class="metric"><span class="label">Buy</span><span class="value green" id="buy-vol">--</span></div>
<div class="metric"><span class="label">Sell</span><span class="value red" id="sell-vol">--</span></div>
<div class="metric"><span class="label">Trades</span><span class="value" id="trade-cnt">--</span></div>
</div>

<div class="card">
<h2>Signal</h2>
<div class="signal-box signal-none" id="signal-box">
<div class="signal-text" id="signal-text">WAITING</div>
</div>
<div class="metric" style="margin-top:4px"><span class="label">Conf</span><span class="value" id="confidence">--</span></div>
<div class="metric"><span class="label">Entry</span><span class="value" id="entry">--</span></div>
<div class="metric"><span class="label">Side</span><span class="value" id="side">--</span></div>
<div id="strategy-formula" style="margin-top:6px;font-size:0.65em;color:#8b949e;text-align:center;font-family:monospace;word-break:break-all"></div>
</div>

<div class="card">
<h2>Window</h2>
<div class="metric"><span class="label">Time</span><span class="value" id="window">--</span></div>
<div class="metric"><span class="label">Left</span><span class="value yellow" id="time-left">--</span></div>
<div class="metric"><span class="label">Market</span><span class="value" id="market" style="font-size:0.65em">--</span></div>
<div class="metric"><span class="label">Uptime</span><span class="value" id="uptime">--</span></div>
<div class="metric"><span class="label">DB</span><span class="value" id="db-records" style="font-size:0.7em">--</span></div>
</div>

</div>

<div class="paper-section">
<h2>PAPER TRADING SESSIONS
<button onclick="addSession()" style="margin-left:8px;padding:2px 10px;background:#2e7d32;border:none;color:#fff;cursor:pointer;font-size:0.75em">+ Add Session</button>
<span id="mkt-tabs" style="margin-left:16px">
<button id="mtab5" onclick="setMarket('5')" style="padding:3px 12px;background:#1f6feb;border:none;color:#fff;cursor:pointer;font-size:0.78em;border-radius:4px;margin-right:3px">PM 5M</button>
<button id="mtab15" onclick="setMarket('15')" style="padding:3px 12px;background:#21262d;border:none;color:#8b949e;cursor:pointer;font-size:0.78em;border-radius:4px;margin-right:3px">PM 15M</button>
<button id="mtabll5" onclick="setMarket('ll5')" style="padding:3px 12px;background:#21262d;border:none;color:#8b949e;cursor:pointer;font-size:0.78em;border-radius:4px;margin-right:3px">LL 5M</button>
<button id="mtabll15" onclick="setMarket('ll15')" style="padding:3px 12px;background:#21262d;border:none;color:#8b949e;cursor:pointer;font-size:0.78em;border-radius:4px;margin-right:3px">LL 15M</button>
<button id="mtabllh" onclick="setMarket('llh')" style="padding:3px 12px;background:#21262d;border:none;color:#8b949e;cursor:pointer;font-size:0.78em;border-radius:4px;margin-right:3px">LL 1H</button>
<button id="mtabk15" onclick="setMarket('k15')" style="padding:3px 12px;background:#21262d;border:none;color:#8b949e;cursor:pointer;font-size:0.78em;border-radius:4px">K 15M</button>
</span>
<span id="mkt-indicator" style="margin-left:10px;color:#f9a825;font-size:0.72em"></span>
</h2>
<div id="sessions-container"></div>
</div>
</div>

<div class="ts" id="last-update" style="margin-top:8px">--</div>

<script>
function fmt(n,d){if(n==null)return'--';return Number(n).toFixed(d)}
function fmtK(n){if(n==null)return'--';let a=Math.abs(n);if(a>=1e6)return(n/1e6).toFixed(1)+'M';if(a>=1e3)return(n/1e3).toFixed(0)+'K';return n.toFixed(0)}
function cls(n){return n>0?'green':n<0?'red':'white'}
var _activeMkt='5';
var _MKT={'5':{ss:'/api/sessions_state',st:'/api/state'},'15':{ss:'/api/sessions_state_15m',st:'/api/state_15m'},'ll5':{ss:'/api/sessions_state_ll5m',st:'/api/state_ll5m'},'ll15':{ss:'/api/sessions_state_ll15m',st:'/api/state_ll15m'},'llh':{ss:'/api/sessions_state_llhourly',st:'/api/state_llhourly'},'k15':{ss:'/api/sessions_state_k15m',st:'/api/state_k15m'}};
function _ep(kind){return (_MKT[_activeMkt]||_MKT['5'])[kind];}

function update(){
fetch(_ep('st')).then(r=>r.json()).then(s=>{
document.getElementById('cl-price').textContent='$'+fmt(s.chainlink_price,2);
document.getElementById('bn-price').textContent='$'+fmt(s.binance_price,2);
document.getElementById('bn-spread').textContent='$'+fmt(s.bn_spread,2);

let dp=s.delta_from_prev;
let el=document.getElementById('delta-prev');
el.textContent=(dp!=null?(dp>=0?'+':'')+fmt(dp,0):'--');
el.className='big '+(dp>50?'green':dp<-50?'red':'white');

document.getElementById('delta-open').textContent=s.delta_from_open!=null?(s.delta_from_open>=0?'+':'')+fmt(s.delta_from_open,0):'--';
document.getElementById('prev-close').textContent='$'+fmt(s.prev_close_price,2);
document.getElementById('win-open').textContent='$'+fmt(s.window_open_price,2);

let pm=document.getElementById('p-model');
pm.textContent=s.p_model!=null?(s.p_model*100).toFixed(1)+'%':'--';
pm.className='big '+(s.p_model>=0.8?'green':s.p_model>=0.6?'yellow':'white');
document.getElementById('snr').textContent=fmt(s.snr,3);
document.getElementById('sigma').textContent=s.sigma?s.sigma.toFixed(6):'--';

document.getElementById('ofi-1').textContent=fmtK(s.ofi_1min);
document.getElementById('ofi-1').className='value '+cls(s.ofi_1min);
document.getElementById('ofi-3').textContent=fmtK(s.ofi_3min);
document.getElementById('ofi-3').className='value '+cls(s.ofi_3min);
document.getElementById('ofi-5').textContent=fmtK(s.ofi_5min);
document.getElementById('ofi-5').className='value '+cls(s.ofi_5min);
document.getElementById('buy-vol').textContent='$'+fmtK(s.buy_vol_1min);
document.getElementById('sell-vol').textContent='$'+fmtK(s.sell_vol_1min);
document.getElementById('trade-cnt').textContent=s.trade_count_1min||'--';

// OFI bar
let bar=document.getElementById('ofi-1-bar');
let total=Math.abs(s.buy_vol_1min||0)+Math.abs(s.sell_vol_1min||0);
if(total>0){let pct=(s.buy_vol_1min||0)/total*100;bar.style.width=pct+'%';bar.style.background=pct>55?'#4caf50':pct<45?'#f44336':'#666';}

let sb=document.getElementById('signal-box');
let st=document.getElementById('signal-text');
st.textContent=s.signal||'--';
sb.className='signal-box '+(s.signal?.includes('BUY NO')?'signal-buy-no':s.signal?.includes('BUY YES')?'signal-buy-yes':s.signal?.includes('WAIT')?'signal-wait':'signal-none');
document.getElementById('confidence').textContent=s.signal_confidence!=null?(s.signal_confidence*100).toFixed(1)+'%':'--';
document.getElementById('entry').textContent=s.signal_entry!=null?fmt(s.signal_entry,3):'--';
document.getElementById('side').textContent=s.signal_side||'--';
if(s.strategy_formula)document.getElementById('strategy-formula').textContent=s.strategy_formula;

// Window
if(s.window_start_utc&&s.window_end_utc){
let ws=s.window_start_utc.slice(11,16);
let we=s.window_end_utc.slice(11,16);
document.getElementById('window').textContent=ws+' - '+we+' UTC';
let end=new Date(s.window_end_utc);
let left=Math.max(0,Math.floor((end-new Date())/1000));
let mm=Math.floor(left/60);let ss=left%60;
document.getElementById('time-left').textContent=mm+':'+(ss<10?'0':'')+ss;
}
document.getElementById('market').textContent=s.market_slug||'--';
let up=s.uptime_s||0;
document.getElementById('uptime').textContent=Math.floor(up/60)+'m '+up%60+'s';
document.getElementById('last-update').textContent='Last update: '+(s.last_update||'--');
}).catch(()=>{});
}

function updateDB(){
fetch('/api/db/stats').then(r=>r.json()).then(d=>{
document.getElementById('db-records').textContent=
(d.ticks||0).toLocaleString()+' ticks / '+(d.signals||0)+' signals';
}).catch(()=>{});
}

var _openCfg={};
function _sb(action,sid,bg,fg,label){
return '<button data-action="'+action+'" data-sid="'+sid+'" style="padding:2px 10px;background:'+bg+';border:none;color:'+fg+';cursor:pointer;font-size:0.75em;border-radius:3px">'+label+'</button>';
}
function _inp(sid,key,val,w){
return '<input data-cfg="'+key+'" data-sid="'+sid+'" value="'+val+'" style="width:'+(w||'60px')+';background:#0d1117;color:#e0e0e0;border:1px solid #30363d;padding:2px 4px;font-size:0.8em;border-radius:3px;font-family:monospace">';
}
function _sel(sid,key,val,opts){
var h='<select data-cfg="'+key+'" data-sid="'+sid+'" style="background:#0d1117;color:#e0e0e0;border:1px solid #30363d;padding:2px 4px;font-size:0.8em;border-radius:3px">';
opts.forEach(function(o){h+='<option value="'+o+'"'+(o===val?' selected':'')+'>'+o+'</option>';});
return h+'</select>';
}
function setMarket(m){
_activeMkt=m;
var TABS={'5':'mtab5','15':'mtab15','ll5':'mtabll5','ll15':'mtabll15','llh':'mtabllh','k15':'mtabk15'};
for(var k in TABS){var b=document.getElementById(TABS[k]);if(b){b.style.background=(k===m)?'#1f6feb':'#21262d';b.style.color=(k===m)?'#fff':'#8b949e';}}
var LBL={'15':'Polymarket 15-min — view only (edit on :8889)','ll5':'Limitless 5-min — view only','ll15':'Limitless 15-min — view only','llh':'Limitless hourly — view only','k15':'Kalshi 15-min — view only'};
var ind=document.getElementById('mkt-indicator');if(ind)ind.textContent=LBL[m]||'';
var c=document.getElementById('sessions-container');if(c)c.innerHTML='';
update();updateSessions();
}
var _extCache={}, _extFetch={};
function loadExtStats(sid){
var now=Date.now();
if(_extFetch[sid] && (now-_extFetch[sid])<15000) return;  // throttle: at most once / 15s
_extFetch[sid]=now;
fetch('/api/session_extended_stats?sid='+sid).then(function(r){return r.json()}).then(function(d){
var el=document.getElementById('ext-'+sid); if(!el||d.error||!d.n)return;
function card(lbl,val,col){return '<div style="text-align:center"><div style="font-size:1.05em;font-weight:700;font-family:monospace;color:'+(col||'#e0e0e0')+'">'+val+'</div><div style="color:#78909c;font-size:0.62em">'+lbl+'</div></div>';}
var evc=d.avg>=0?'#3fb950':'#f85149';
var ev7c=d.ev7>=0?'#3fb950':'#f85149';
var html=
'<div style="grid-column:1/-1;color:#1f6feb;font-size:0.66em;font-weight:700;margin-bottom:2px">EXTENDED METRICS</div>'+
card('Avg PnL','$'+d.avg.toFixed(2),evc)+
card('EV 7d','$'+d.ev7.toFixed(2)+' ('+d.n7+')',ev7c)+
card('Sharpe',d.sharpe.toFixed(3))+
card('PF',d.pf.toFixed(2))+
card('MaxDD','$'+d.maxdd.toFixed(0),'#f85149')+
card('Avg Entry',d.avg_entry.toFixed(3))+
card('WR',d.wr.toFixed(1)+'%');
_extCache[sid]=html;
el.innerHTML=html;
}).catch(function(){});
}
function updateSessions(){
fetch(_ep('ss')).then(function(r){return r.json()}).then(function(sessions){
try{
var c=document.getElementById('sessions-container');
if(!c)return;
var ids=Object.keys(sessions);
if(ids.length===0){c.innerHTML='<div style="color:#546e7a;padding:8px">No sessions. Click + Add Session.</div>';return;}
ids.forEach(function(sid){
var s=sessions[sid];
var el=document.getElementById('sess-'+sid);
if(!el){el=document.createElement('div');el.id='sess-'+sid;el.style.cssText='margin-bottom:10px;padding:10px;background:#161b22;border:1px solid #30363d;border-radius:6px';c.appendChild(el);}
if(_openCfg[sid]&&el.querySelector('[data-cfg]')){
var sigEl=el.querySelector('[data-livesig]');
if(sigEl)sigEl.textContent=s.last_signal||'';
var sigmaEl=el.querySelector('[data-livesigma]');
if(sigmaEl)sigmaEl.textContent='σ='+(s.live_sigma||0).toExponential(2)+' ('+s.config.sigma_type+')';
return;
}
var cfg=s.config;
var pnlColor=s.total_pnl>=0?'#3fb950':'#f85149';
var wr=s.total_trades>0?(s.wins/s.total_trades*100).toFixed(0)+'%':'--';
var sig=s.last_signal||'';
var sigColor=sig.indexOf('BUY')>=0?'#3fb950':sig==='PAUSED'?'#f9a825':'#8b949e';
var isOpen=_openCfg[sid];
var h='<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">';
h+='<span style="color:#4fc3f7;font-weight:bold;font-size:0.95em">'+cfg.name+' <span style="color:#546e7a;font-size:0.75em">['+sid+']</span></span>';
h+='<span style="display:flex;gap:4px;align-items:center">';
h+=cfg.paused?_sb('resume',sid,'#2e7d32','#fff','Resume'):_sb('pause',sid,'#f9a825','#000','Pause');
h+=' <button data-toggle="'+sid+'" style="padding:2px 10px;background:#1f6feb;border:none;color:#fff;cursor:pointer;font-size:0.75em;border-radius:3px">'+(isOpen?'Close':'Config')+'</button>';
h+=' '+_sb('reset',sid,'#c62828','#fff','Reset');
h+=' '+_sb('delete',sid,'#546e7a','#fff','X');
h+='</span></div>';
h+='<div style="font-size:0.7em;color:#8b949e;margin-bottom:4px;font-family:monospace">'+s.formula+'</div>';
h+='<div style="display:flex;gap:12px;font-size:0.85em;margin-bottom:6px;flex-wrap:wrap">';
h+='<span style="color:#fff">$'+s.balance.toLocaleString()+'</span>';
h+='<span style="color:'+pnlColor+'">PnL: $'+(s.total_pnl>=0?'+':'')+s.total_pnl.toFixed(2)+'</span>';
h+='<span>'+s.total_trades+' trades</span>';
h+='<span style="color:#3fb950">'+s.wins+'W</span> / <span style="color:#f85149">'+s.losses+'L</span>';
h+='<span>WR: '+wr+'</span>';
h+='<span data-livesig="1" style="color:'+sigColor+'">Signal: '+sig+'</span>';
var lsig=s.live_sigma||0;
h+='<span data-livesigma="1" style="color:#546e7a">σ='+lsig.toExponential(2)+' ('+cfg.sigma_type+')</span>';
h+='</div>';
if(sid==='s_third'||sid==='s_entry150'){h+='<div id="ext-'+sid+'" style="display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin:4px 0 8px 0;padding:6px;background:#0a0e17;border:1px solid #1f6feb;border-radius:6px">'+(_extCache[sid]||'<div style="grid-column:1/-1;color:#1f6feb;font-size:0.7em;font-weight:700">EXTENDED METRICS</div>')+'</div>';}
if(isOpen){
var _c='background:#0d1117;color:#e0e0e0;border:1px solid #30363d;padding:4px 8px;border-radius:4px;width:100%';
var _s='width:100%;accent-color:#58a6ff';
var _l='color:#8b949e;font-size:0.85em;display:block;margin-bottom:2px';
var _v='float:right;color:#58a6ff;font-weight:700;font-family:monospace';
function _r(label,key,min,max,step,val,fmt){
return '<div><label style="'+_l+'">'+label+' <span style="'+_v+'" id="vd_'+key+'_'+sid+'">'+fmt(val)+'</span></label>'+
'<input type="range" data-cfg="'+key+'" data-sid="'+sid+'" data-fmt="'+key+'" min="'+min+'" max="'+max+'" step="'+step+'" value="'+val+'" style="'+_s+'"></div>';
}
function _d(label,key,opts,cur){
var o='';opts.forEach(function(x){o+='<option value="'+x[0]+'"'+(x[0]===cur?' selected':'')+'>'+x[1]+'</option>';});
return '<div><label style="'+_l+'">'+label+'</label><select data-cfg="'+key+'" data-sid="'+sid+'" style="'+_c+'">'+o+'</select></div>';
}
h+='<div style="background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;margin-bottom:6px">';
h+='<div style="font-size:0.9em;color:#79c0ff;margin-bottom:10px;font-weight:bold">STRATEGY CONFIG</div>';
h+='<div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px 16px">';
h+='<div><label style="'+_l+'">Session name</label><input data-cfg="name" data-sid="'+sid+'" value="'+cfg.name+'" style="'+_c+'"></div>';
h+=_r('Delta threshold','delta_threshold',0,400,5,cfg.delta_threshold,function(v){return '$'+v;});
h+=_r('Kappa','kappa',1,50,1,Math.round(cfg.kappa*10),function(v){return (v/10).toFixed(1);});
h+=_r('P-model min','p_model_threshold',0,95,5,Math.round(cfg.p_model_threshold*100),function(v){return v==0?'off':v+'%';});
h+=_r('Max entry price','max_entry_price',50,99,1,Math.round(cfg.max_entry_price*100),function(v){return (v/100).toFixed(2);});
h+=_r('Stake per trade','stake',1,500,1,cfg.stake,function(v){return '$'+v;});
h+=_d('Side','trade_side',[['NO','NO (DOWN)'],['YES','YES (UP)'],['BOTH','BOTH (NO+YES)']],cfg.trade_side);
h+=_d('Tau mode','tau_mode',[['linear','linear'],['sqrt','sqrt']],cfg.tau_mode);
var _sigmas=[
['static','static (0.0005)'],
['trail5','trail5 — STD 5min'],['trail10','trail10 — STD 10min'],['trail30','trail30 — STD 30min'],
['trail5_pmin','trail5 per-min'],['trail10_pmin','trail10 per-min'],['trail30_pmin','trail30 per-min'],
['max5','max5 — MAX 5min'],['max10','max10 — MAX 10min'],['max30','max30 — MAX 30min'],
['max5_pmin','max5 per-min'],['max10_pmin','max10 per-min'],
['range5','range5 — Parkinson 5min'],['range10','range10 — Parkinson 10min'],['range30','range30 — Parkinson 30min'],
['range5_pmin','range5 per-min'],['range10_pmin','range10 per-min'],['range30_pmin','range30 per-min'],
['realized5','realized5 — sqrt(r2) 5min'],['realized10','realized10 — sqrt(r2) 10min'],
['realized5_pmin','realized5 per-min'],['realized10_pmin','realized10 per-min'],
['median5','median5'],['median10','median10'],['median5_pmin','median5 per-min'],['median10_pmin','median10 per-min'],
['mad5','mad5 — mean abs 5min'],['mad10','mad10 — mean abs 10min'],
['intra','intra STD'],['intra_pmin','intra per-min'],['intra_max','intra MAX'],['intra_range','intra range']
];
h+=_d('Sigma type','sigma_type',_sigmas,cfg.sigma_type);
h+='</div>';
h+='<div style="margin-top:12px;display:flex;gap:10px;align-items:center">';
h+='<button data-save="'+sid+'" style="padding:6px 24px;background:#238636;border:none;color:#fff;cursor:pointer;font-size:0.9em;border-radius:6px;font-weight:bold">SAVE</button>';
h+='<span data-savemsg="'+sid+'" style="font-size:0.85em"></span>';
h+='</div></div>';
}
if(s.active_positions&&s.active_positions.length>0){
h+='<div style="margin-bottom:4px;font-size:0.8em;color:#f9a825">Active: '+s.active_positions.length+' position(s)</div>';
h+='<table style="width:100%;font-size:0.75em;border-collapse:collapse;margin-bottom:6px;background:rgba(249,168,37,0.05);border:1px solid rgba(249,168,37,0.3)"><tr style="color:#f9a825"><th style="padding:2px 4px;text-align:left">LIVE</th><th>Entry</th><th>Stake</th><th>Shares</th><th style="text-align:left">Signal</th><th>Δ</th><th>Opened</th></tr>';
s.active_positions.forEach(function(a){
var sc=a.side==='YES'?'#42a5f5':'#4caf50';
var ws=a.window?a.window.slice(11,19):'';
var opened=a.ts?a.ts.slice(11,19):'';
h+='<tr><td style="padding:2px 4px;color:'+sc+';font-weight:bold">'+a.side+'</td>';
h+='<td>'+(a.entry||0).toFixed(3)+'</td>';
h+='<td>$'+(a.stake||0)+'</td>';
h+='<td>'+((a.shares||0).toFixed?(a.shares||0).toFixed(1):(a.shares||0))+'</td>';
h+='<td style="font-size:0.7em">'+(a.signal||'')+'</td>';
h+='<td>'+(a.delta!=null?((a.delta>=0?'+':'')+a.delta):'')+'</td>';
h+='<td style="font-size:0.7em">'+opened+'</td></tr>';
});
h+='</table>';
}
if(s.history&&s.history.length>0){
h+='<table style="width:100%;font-size:0.75em;border-collapse:collapse"><tr style="color:#546e7a"><th style="padding:2px 4px">Side</th><th>Entry</th><th>Stake</th><th>PnL</th><th>Result</th><th>Time</th></tr>';
s.history.slice(0,5).forEach(function(t){
var rc=t.result==='WIN'?'#3fb950':'#f85149';
h+='<tr><td style="padding:2px 4px">'+t.side+'</td><td>'+(t.entry||0).toFixed(3)+'</td><td>$'+t.stake+'</td>';
h+='<td style="color:'+rc+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td>';
h+='<td style="color:'+rc+'">'+t.result+'</td><td>'+(t.resolved_ts||'').slice(11,19)+'</td></tr>';
});
h+='</table>';
}
el.innerHTML=h;
if(sid==='s_third'||sid==='s_entry150'){loadExtStats(sid);}
});
}catch(e){
console.error('updateSessions:',e);
var c=document.getElementById('sessions-container');
if(c)c.innerHTML='<div style="color:#f44336;padding:8px">JS Error: '+e.message+'</div>';
}
}).catch(function(e){console.error('sessions fetch:',e);});
}
document.addEventListener('input',function(e){
var el=e.target;if(!el.dataset.fmt||!el.dataset.sid)return;
var k=el.dataset.fmt,sid=el.dataset.sid,v=parseFloat(el.value);
var span=document.getElementById('vd_'+k+'_'+sid);if(!span)return;
if(k==='delta_threshold')span.textContent='$'+v;
else if(k==='kappa')span.textContent=(v/10).toFixed(1);
else if(k==='p_model_threshold')span.textContent=v==0?'off':v+'%';
else if(k==='max_entry_price')span.textContent=(v/100).toFixed(2);
else if(k==='stake')span.textContent='$'+v;
});
document.addEventListener('click',function(e){
var btn=e.target;
if(_activeMkt!=='5'&&(btn.dataset.toggle||btn.dataset.save||btn.dataset.action)){return;}
if(btn.dataset.toggle){_openCfg[btn.dataset.toggle]=!_openCfg[btn.dataset.toggle];updateSessions();return;}
if(btn.dataset.save){
var sid=btn.dataset.save;
var inputs=document.querySelectorAll('[data-cfg][data-sid="'+sid+'"]');
var upd={id:sid};
inputs.forEach(function(el){
var k=el.dataset.cfg,v=el.value;
if(k==='kappa')v=(parseFloat(v)/10).toFixed(1);
else if(k==='p_model_threshold')v=(parseFloat(v)/100).toFixed(2);
else if(k==='max_entry_price')v=(parseFloat(v)/100).toFixed(2);
upd[k]=v;
});
fetch('/api/session/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(upd)})
.then(function(r){return r.json()}).then(function(d){
var msg=document.querySelector('[data-savemsg="'+sid+'"]');
if(d.error){if(msg){msg.textContent='Error: '+d.error;msg.style.color='#f85149';}}
else{if(msg){msg.textContent='Saved!';msg.style.color='#3fb950';}setTimeout(function(){if(msg)msg.textContent='';},2000);updateSessions();}
}).catch(function(e){alert('Save error: '+e)});
return;
}
if(!btn.dataset.action||!btn.dataset.sid)return;
var action=btn.dataset.action,sid=btn.dataset.sid;
if(action==='delete'&&!confirm('Delete session '+sid+'?'))return;
fetch('/api/session/'+action,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:sid})})
.then(function(){updateSessions()}).catch(function(e){alert('Error: '+e)});
});
function addSession(){
if(_activeMkt!=='5'){alert('This grid is view-only. Manage sessions on the venue instance.');return;}
var name=prompt('Session name:');
if(!name)return;
var sid='s'+Date.now();
fetch('/api/session/create',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({id:sid,config:{name:name},balance:10000})})
.then(function(r){return r.json()}).then(function(d){
if(d.error){alert(d.error);}else{_openCfg[sid]=true;updateSessions();}
}).catch(function(e){alert('Error: '+e)});
}
function updatePaper(){updateSessions()}
function updatePaperLegacy(){
fetch('/api/paper_state').then(function(r){return r.json()}).then(function(p){
var balEl=document.getElementById('pt-balance');
balEl.textContent='$'+p.balance.toLocaleString();
var pnlEl=document.getElementById('pt-pnl');
pnlEl.textContent=(p.total_pnl>=0?'+':'')+('$'+p.total_pnl.toFixed(2));
pnlEl.className='num '+(p.total_pnl>0?'green':p.total_pnl<0?'red':'white');
document.getElementById('pt-trades').textContent=p.total_trades;
var wrEl=document.getElementById('pt-wr');
if(p.total_trades>0){wrEl.textContent=(p.win_rate*100).toFixed(1)+'%';wrEl.className='num '+(p.win_rate>=0.7?'green':p.win_rate>=0.5?'yellow':'red')}
else{wrEl.textContent='--';wrEl.className='num white'}
document.getElementById('pt-wins').textContent=p.wins;
document.getElementById('pt-losses').textContent=p.losses;
document.getElementById('pt-staked').textContent='$'+p.total_staked.toFixed(0);
document.getElementById('pt-window').textContent=p.buys_this_window+'/'+p.max_per_window;

var badge=document.getElementById('paper-active-badge');
if(p.active_count>0){badge.style.display='inline-block';badge.textContent=p.active_count+' ACTIVE ($'+p.active_stake.toFixed(0)+')'}
else{badge.style.display='none'}

var ab=document.getElementById('pt-active-body');
if(p.active_positions.length===0){ab.innerHTML='<tr><td colspan="8" style="color:#546e7a;text-align:center">No active positions</td></tr>'}
else{var h='';for(var i=0;i<p.active_positions.length;i++){var a=p.active_positions[i];h+='<tr><td style="color:'+(a.side==='YES'?'#42a5f5':'#4caf50')+'">'+a.side+'</td><td>'+a.entry.toFixed(3)+'</td><td>$'+a.stake+'</td><td>'+(a.shares||0)+'</td><td>'+(a.ob_shares||0)+'</td><td style="font-size:0.75em">'+a.signal+'</td><td>'+(a.delta>=0?'+':'')+a.delta+'</td><td style="font-size:0.7em">'+a.ts.slice(11,19)+'</td></tr>'}ab.innerHTML=h}

var hb=document.getElementById('pt-history-body');
if(p.history.length===0){hb.innerHTML='<tr><td colspan="7" style="color:#546e7a;text-align:center">No trades yet</td></tr>'}
else{var h='';for(var i=0;i<Math.min(p.history.length,20);i++){var t=p.history[i];var isWin=t.result==='WIN';h+='<tr><td style="color:'+(t.side==='YES'?'#42a5f5':'#4caf50')+'">'+t.side+'</td><td>'+t.entry.toFixed(3)+'</td><td>$'+t.stake+'</td><td class="'+(t.pnl>=0?'hist-win':'hist-loss')+'">'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td><td class="'+(isWin?'hist-win':'hist-loss')+'">'+t.result+'</td><td>'+t.outcome+'</td><td style="font-size:0.7em">'+t.ts.slice(11,19)+'</td></tr>'}hb.innerHTML=h}
}).catch(function(){});
}

function configPaper(reset){
var bal=document.getElementById('cfg-balance').value;
var stk=document.getElementById('cfg-stake').value;
var body=JSON.stringify({starting_balance:parseFloat(bal),stake_per_trade:parseFloat(stk),reset:!!reset});
fetch('/api/paper/config',{method:'POST',headers:{'Content-Type':'application/json'},body:body})
.then(function(r){return r.json()}).then(function(d){
var msg=document.getElementById('cfg-msg');
if(d.error){msg.textContent=d.error;msg.style.color='#f44336'}
else{msg.textContent=reset?'Reset!':'Applied!';msg.style.color='#4caf50';updatePaper()}
setTimeout(function(){msg.textContent=''},3000);
}).catch(function(){});
}

function configPaper(reset){
var bal=document.getElementById('cfg-balance');
var stk=document.getElementById('cfg-stake');
if(!bal||!stk)return;
var body=JSON.stringify({starting_balance:parseFloat(bal.value),stake_per_trade:parseFloat(stk.value),reset:!!reset});
fetch('/api/paper/config',{method:'POST',headers:{'Content-Type':'application/json'},body:body})
.then(function(){updateSessions()});
}

setInterval(update,1000);
setInterval(updateSessions,2000);
setInterval(updateDB,10000);
update();updateSessions();updateDB();
</script>
</body>
</html>"""


BACKTEST_HTML_LEGACY = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Strategy Backtester</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#e0e0e0;padding:16px}
h1{text-align:center;color:#58a6ff;margin-bottom:16px}
.layout{display:flex;gap:16px;max-width:1600px;margin:0 auto}
.panel{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;flex:1}
.panel h2{color:#79c0ff;font-size:1em;margin-bottom:12px}
.controls{min-width:320px;max-width:380px}
.ctrl{margin-bottom:14px}
.ctrl label{display:block;color:#8b949e;font-size:0.85em;margin-bottom:4px}
.ctrl input[type=range]{width:100%;accent-color:#58a6ff}
.ctrl .val{float:right;color:#58a6ff;font-weight:700;font-family:monospace}
.ctrl select{width:100%;background:#0d1117;color:#e0e0e0;border:1px solid #30363d;padding:6px;border-radius:4px}
.check-group{display:flex;gap:12px;flex-wrap:wrap}
.check-group label{color:#c9d1d9;font-size:0.85em;cursor:pointer}
.check-group input{accent-color:#58a6ff}
button{background:#238636;color:#fff;border:none;padding:10px 24px;border-radius:6px;cursor:pointer;font-size:1em;font-weight:600;width:100%;margin-top:8px}
button:hover{background:#2ea043}
.results{flex:2}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
.stat{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;text-align:center}
.stat .num{font-size:1.6em;font-weight:700;font-family:monospace}
.stat .lbl{color:#8b949e;font-size:0.75em;margin-top:2px}
.green{color:#3fb950}.red{color:#f85149}.yellow{color:#d29922}.blue{color:#58a6ff}
table{width:100%;border-collapse:collapse;font-size:0.85em;margin-top:12px}
th{background:#21262d;color:#8b949e;padding:6px 8px;text-align:left;font-weight:600}
td{padding:5px 8px;border-bottom:1px solid #21262d}
tr:hover{background:#161b22}
.tabs{display:flex;gap:0;margin-bottom:12px}
.tab{padding:8px 20px;background:#21262d;color:#8b949e;cursor:pointer;border:1px solid #30363d}
.tab:first-child{border-radius:6px 0 0 6px}
.tab:last-child{border-radius:0 6px 6px 0}
.tab.active{background:#30363d;color:#58a6ff;font-weight:600}
.preset-btn{display:inline-block;background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:0.8em;margin:2px}
.preset-btn:hover{background:#30363d;color:#58a6ff}
#loading{display:none;text-align:center;padding:20px;color:#8b949e}
</style>
</head>
<body>
<div style="text-align:center;margin-bottom:12px"><a href="/" style="color:#8b949e;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">LIVE</a><a href="/backtest" style="color:#58a6ff;text-decoration:none;font-weight:700;padding:8px 24px;background:#21262d;border-radius:6px;margin:0 4px">BACKTEST</a><a href="/chart" style="color:#00d4aa;text-decoration:none;padding:8px 24px;background:#141b2d;border-radius:6px;margin:0 4px">CHART</a></div>
<h1>STRATEGY BACKTESTER</h1>

<div class="layout">
<div class="panel controls">
<h2>PARAMETERS</h2>

<div class="ctrl">
<label>Presets:</label>
<span class="preset-btn" onclick="preset('conservative')">Conservative</span>
<span class="preset-btn" onclick="preset('balanced')">Balanced</span>
<span class="preset-btn" onclick="preset('aggressive')">Aggressive</span>
<span class="preset-btn" onclick="preset('pmodel')">P-Model</span>
<span class="preset-btn" onclick="preset('combined')">Delta+OFI</span>
</div>

<div class="ctrl">
<label>Delta threshold: <span class="val" id="v-delta">$150</span></label>
<input type="range" id="delta" min="0" max="400" step="10" value="150" oninput="updVal('delta','$')">
</div>

<div class="ctrl">
<label>OFI threshold: <span class="val" id="v-ofi">$0 (off)</span></label>
<input type="range" id="ofi" min="0" max="10000000" step="500000" value="0" oninput="updOfi()">
</div>

<div class="ctrl">
<label>P-model min: <span class="val" id="v-pmodel">0 (off)</span></label>
<input type="range" id="pmodel" min="0" max="95" step="5" value="0" oninput="updPct('pmodel')">
</div>

<div class="ctrl">
<label>Unified score min: <span class="val" id="v-unified">0 (off)</span></label>
<input type="range" id="unified" min="0" max="90" step="5" value="0" oninput="updPct('unified')">
</div>

<div class="ctrl">
<label>Kappa (P-model): <span class="val" id="v-kappa">1.5</span></label>
<input type="range" id="kappa" min="5" max="50" step="1" value="15" oninput="document.getElementById('v-kappa').textContent=(this.value/10).toFixed(1)">
</div>

<div class="ctrl">
<label>Stake per trade ($): <span class="val" id="v-stake">$100</span></label>
<input type="range" id="stake" min="1" max="1000" step="1" value="100" oninput="document.getElementById('v-stake').textContent='$'+this.value">
</div>

<div class="ctrl">
<label>Side:</label>
<select id="side">
<option value="NO">NO only (DOWN bets)</option>
<option value="BOTH">BOTH (NO + YES)</option>
</select>
</div>

<div class="ctrl">
<label>Chunks:</label>
<div class="check-group">
<label><input type="checkbox" id="c05" checked> 0-5min</label>
<label><input type="checkbox" id="c510" checked> 5-10min</label>
<label><input type="checkbox" id="c1015" checked> 10-15min</label>
</div>
</div>

<div class="ctrl">
<label>Data source:</label>
<div class="check-group">
<label><input type="checkbox" id="sALL" checked> ALLDATA</label>
<label><input type="checkbox" id="sALL4" checked> ALLDATA4</label>
</div>
</div>

<button onclick="runBacktest()">RUN BACKTEST</button>
<button onclick="runPaper()" style="background:#1f6feb;margin-top:6px">PAPER TRADES</button>

<div id="data-info" style="margin-top:12px;color:#8b949e;font-size:0.8em"></div>
</div>

<div class="panel results">
<div class="tabs">
<div class="tab active" onclick="showTab(this,'results-tab')">Results</div>
<div class="tab" onclick="showTab(this,'chunk-tab')">By Chunk</div>
<div class="tab" onclick="showTab(this,'trades-tab')">Trades</div>
<div class="tab" onclick="showTab(this,'paper-tab')">Paper Trades</div>
</div>

<div id="loading">Running backtest...</div>

<div id="results-tab">
<div class="summary" id="summary">
<div class="stat"><div class="num" id="s-signals">--</div><div class="lbl">SIGNALS</div></div>
<div class="stat"><div class="num" id="s-wr">--</div><div class="lbl">WIN RATE</div></div>
<div class="stat"><div class="num" id="s-pnl">--</div><div class="lbl">TOTAL PnL</div></div>
<div class="stat"><div class="num" id="s-roi">--</div><div class="lbl">ROI</div></div>
</div>
<div class="summary">
<div class="stat"><div class="num" id="s-wins">--</div><div class="lbl">WINS</div></div>
<div class="stat"><div class="num" id="s-losses">--</div><div class="lbl">LOSSES</div></div>
<div class="stat"><div class="num" id="s-staked">--</div><div class="lbl">TOTAL STAKED</div></div>
<div class="stat"><div class="num" id="s-avg">--</div><div class="lbl">AVG PnL/TRADE</div></div>
</div>
</div>

<div id="chunk-tab" style="display:none">
<table id="chunk-table">
<thead><tr><th>Chunk</th><th>Signals</th><th>Wins</th><th>Losses</th><th>WR</th><th>PnL</th><th>ROI</th></tr></thead>
<tbody></tbody>
</table>
</div>

<div id="trades-tab" style="display:none">
<table id="trades-table">
<thead><tr><th>Window</th><th>Chunk</th><th>Side</th><th>Delta</th><th>Entry</th><th>Resolve</th><th>PnL</th><th>Source</th></tr></thead>
<tbody></tbody>
</table>
</div>

<div id="paper-tab" style="display:none">
<div class="summary" id="paper-summary">
<div class="stat"><div class="num" id="p-total">--</div><div class="lbl">TRADES</div></div>
<div class="stat"><div class="num" id="p-wr">--</div><div class="lbl">WIN RATE</div></div>
<div class="stat"><div class="num" id="p-pnl">--</div><div class="lbl">PnL</div></div>
<div class="stat"><div class="num" id="p-wins">--</div><div class="lbl">W / L</div></div>
</div>
<table id="paper-table">
<thead><tr><th>Date</th><th>Rule</th><th>Side</th><th>Delta</th><th>Entry</th><th>PnL</th><th>Win</th></tr></thead>
<tbody></tbody>
</table>
</div>

</div>
</div>

<script>
function updVal(id,pre){document.getElementById('v-'+id).textContent=pre+document.getElementById(id).value}
function updOfi(){let v=+document.getElementById('ofi').value;document.getElementById('v-ofi').textContent=v==0?'$0 (off)':'-$'+(v/1e6).toFixed(1)+'M'}
function updPct(id){let v=+document.getElementById(id).value;document.getElementById('v-'+id).textContent=v==0?'0 (off)':(v/100).toFixed(2)}

function getParams(){
let chunks=[];
if(document.getElementById('c05').checked)chunks.push('0-5min');
if(document.getElementById('c510').checked)chunks.push('5-10min');
if(document.getElementById('c1015').checked)chunks.push('10-15min');
let sources=[];
if(document.getElementById('sALL').checked)sources.push('ALLDATA');
if(document.getElementById('sALL4').checked)sources.push('ALLDATA4');
return{
delta_thresh:+document.getElementById('delta').value,
ofi_thresh:-(+document.getElementById('ofi').value),
p_thresh:+document.getElementById('pmodel').value/100,
unified_thresh:+document.getElementById('unified').value/100,
kappa:+document.getElementById('kappa').value/10,
stake:+document.getElementById('stake').value,
side:document.getElementById('side').value,
chunks:chunks,
sources:sources
}}

function preset(name){
let s={
conservative:{delta:200,ofi:0,pmodel:70,unified:0,kappa:15},
balanced:{delta:150,ofi:0,pmodel:0,unified:0,kappa:15},
aggressive:{delta:50,ofi:0,pmodel:0,unified:0,kappa:15},
pmodel:{delta:0,ofi:0,pmodel:70,unified:0,kappa:15},
combined:{delta:50,ofi:1000000,pmodel:0,unified:0,kappa:15},
}[name]||{};
if(s.delta!=null)document.getElementById('delta').value=s.delta;
if(s.ofi!=null)document.getElementById('ofi').value=s.ofi;
if(s.pmodel!=null)document.getElementById('pmodel').value=s.pmodel;
if(s.unified!=null)document.getElementById('unified').value=s.unified;
if(s.kappa!=null)document.getElementById('kappa').value=s.kappa;
updVal('delta','$');updOfi();updPct('pmodel');updPct('unified');
document.getElementById('v-kappa').textContent=(document.getElementById('kappa').value/10).toFixed(1);
runBacktest();
}

function showTab(el,id){
document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
el.classList.add('active');
['results-tab','chunk-tab','trades-tab','paper-tab'].forEach(t=>{
document.getElementById(t).style.display=t==id?'block':'none'})
}

function cls(v){return v>0?'green':v<0?'red':''}
function fmt(v){return v>=1000?'$'+(v/1000).toFixed(1)+'K':'$'+v.toFixed(0)}

function runBacktest(){
document.getElementById('loading').style.display='block';
fetch('/api/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(getParams())})
.then(r=>r.json()).then(d=>{
document.getElementById('loading').style.display='none';
if(d.error){alert(d.error);return}
let t=d.total;
document.getElementById('s-signals').textContent=t.signals;
let wrEl=document.getElementById('s-wr');wrEl.textContent=(t.wr*100).toFixed(1)+'%';wrEl.className='num '+(t.wr>=0.9?'green':t.wr>=0.7?'yellow':'red');
let pnlEl=document.getElementById('s-pnl');pnlEl.textContent='$'+t.total_pnl.toLocaleString();pnlEl.className='num '+cls(t.total_pnl);
let roiEl=document.getElementById('s-roi');roiEl.textContent=t.roi.toFixed(1)+'%';roiEl.className='num '+cls(t.roi);
document.getElementById('s-wins').textContent=t.wins;document.getElementById('s-wins').className='num green';
document.getElementById('s-losses').textContent=t.losses;document.getElementById('s-losses').className='num red';
document.getElementById('s-staked').textContent=fmt(t.total_staked);
document.getElementById('s-avg').textContent='$'+(t.signals>0?(t.total_pnl/t.signals).toFixed(0):'0');document.getElementById('s-avg').className='num '+cls(t.total_pnl);

let ct=document.querySelector('#chunk-table tbody');ct.innerHTML='';
for(let[k,v]of Object.entries(d.by_chunk)){
ct.innerHTML+='<tr><td>'+k+'</td><td>'+v.signals+'</td><td class="green">'+v.wins+'</td><td class="red">'+v.losses+'</td><td class="'+(v.wr>=0.9?'green':v.wr>=0.7?'yellow':'red')+'">'+(v.wr*100).toFixed(1)+'%</td><td class="'+cls(v.total_pnl)+'">$'+v.total_pnl.toLocaleString()+'</td><td class="'+cls(v.roi)+'">'+v.roi.toFixed(1)+'%</td></tr>'}

let tt=document.querySelector('#trades-table tbody');tt.innerHTML='';
(d.trades||[]).forEach(function(t){
tt.innerHTML+='<tr><td style="font-size:0.75em">'+(t.window?t.window.slice(0,16):'')+'</td><td>'+t.chunk+'</td><td>'+t.side+'</td><td>$'+(t.delta?t.delta.toFixed(0):'')+'</td><td>'+(t.entry?t.entry.toFixed(3):'')+'</td><td>'+t.resolve+'</td><td class="'+cls(t.pnl)+'">$'+(t.pnl?t.pnl.toFixed(1):'')+'</td><td style="font-size:0.75em">'+t.source+'</td></tr>'});

document.getElementById('data-info').textContent='Data: '+d.data_info.total_windows+' windows ('+d.data_info.alldata+' ALLDATA + '+d.data_info.alldata4+' ALLDATA4)';
}).catch(function(e){document.getElementById('loading').style.display='none';alert('Error: '+e)})}

function runPaper(){
fetch('/api/paper',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(getParams())})
.then(r=>r.json()).then(function(d){
if(d.error){alert(d.error);return}
showTab(document.querySelectorAll('.tab')[3],'paper-tab');
document.getElementById('p-total').textContent=d.total;
let wrEl=document.getElementById('p-wr');wrEl.textContent=(d.wr*100).toFixed(1)+'%';wrEl.className='num '+(d.wr>=0.8?'green':d.wr>=0.6?'yellow':'red');
let pnlEl=document.getElementById('p-pnl');pnlEl.textContent='$'+d.pnl.toFixed(2);pnlEl.className='num '+cls(d.pnl);
document.getElementById('p-wins').textContent=d.wins+' / '+d.losses;
let tt=document.querySelector('#paper-table tbody');tt.innerHTML='';
(d.trades||[]).forEach(function(t){
tt.innerHTML+='<tr><td>'+t.date+'</td><td>'+t.rule+'</td><td>'+t.side+'</td><td>$'+t.delta+'</td><td>'+t.entry.toFixed(3)+'</td><td class="'+cls(t.pnl)+'">$'+t.pnl.toFixed(4)+'</td><td>'+(t.win?'<span class=green>WIN</span>':'<span class=red>LOSS</span>')+'</td></tr>'})
}).catch(function(e){alert('Error: '+e)})}

runBacktest();
</script>
</body>
</html>"""


def _build_history_html():
    """Build full trade history HTML page with filters and metrics."""
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row

    cfg_rows = conn.execute("SELECT session_id, key, value FROM paper_config").fetchall()
    sessions = {}
    for r in cfg_rows:
        sid = r["session_id"]
        if sid not in sessions:
            sessions[sid] = {}
        sessions[sid][r["key"]] = r["value"]

    active = {s: d for s, d in sessions.items() if d.get("__deleted__") != "true" and d.get("cfg_name")}

    trades = conn.execute(
        "SELECT * FROM paper_trades WHERE result IS NOT NULL ORDER BY resolved_ts DESC"
    ).fetchall()
    trades = [dict(t) for t in trades]
    conn.close()

    # Build trades JSON for client-side filtering
    import json as _json
    trades_json = []
    for t in trades:
        sid = t.get("session_id", "?")
        name = active.get(sid, {}).get("cfg_name", sid[:8])
        trades_json.append({
            "ts": (t.get("resolved_ts") or t.get("ts") or "")[:19],
            "name": name,
            "side": t.get("side", "?"),
            "entry": t.get("entry_price", 0) or 0,
            "delta": t.get("delta", 0) or 0,
            "signal": t.get("signal", ""),
            "result": t.get("result", "?"),
            "outcome": t.get("outcome", "?"),
            "pnl": t.get("pnl", 0) or 0,
            "window": (t.get("window_start") or "")[:19],
        })

    # Strategy names for filter buttons
    strat_names = sorted(set(active[s].get("cfg_name", s) for s in active))

    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Trade History</title>
<style>
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,monospace;margin:20px}
h1{color:#f1c40f}h2{color:#58a6ff;margin-top:20px}
a{color:#58a6ff;text-decoration:none;padding:8px 20px;background:#161b22;border-radius:6px;margin:0 4px}
a:hover{background:#1c2333}
table{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0}
th{background:#161b22;color:#58a6ff;padding:6px 10px;border:1px solid #30363d;position:sticky;top:0;cursor:pointer}
th:hover{background:#1c2333}
td{padding:4px 10px;border:1px solid #30363d;text-align:center}
tr:hover{background:#1c2333}
.pos{color:#2ecc71;font-weight:bold}.neg{color:#e74c3c;font-weight:bold}
nav{text-align:center;margin-bottom:20px}
.filters{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin:10px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.fbtn{padding:6px 14px;border-radius:6px;border:1px solid #30363d;background:#0d1117;color:#c9d1d9;cursor:pointer;font-size:12px;font-family:monospace}
.fbtn:hover{background:#1c2333}
.fbtn.active{background:#1f6feb;color:#fff;border-color:#1f6feb}
.flabel{color:#8b949e;font-size:11px;margin-right:4px}
select{background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:4px 8px;font-family:monospace;font-size:12px}
.summary-cards{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
.scard{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 18px;text-align:center;min-width:90px}
.scard .sv{font-size:20px;font-weight:bold}.scard .sl{font-size:10px;color:#8b949e}
</style>
</head><body>
<nav><a href="/">LIVE</a> <a href="/backtest">BACKTEST</a> <a href="/history" style="color:#f1c40f">HISTORY</a> <a href="/chart" style="color:#00d4aa">CHART</a></nav>
<h1>Trade History</h1>

<div class="filters">
<span class="flabel">Strategy:</span>
<button class="fbtn active" onclick="setStrat('ALL')">ALL</button>
""" + "".join(f'<button class="fbtn" onclick="setStrat(\'{n}\')">{n}</button>' for n in strat_names) + """
<span class="flabel" style="margin-left:16px">Result:</span>
<button class="fbtn active" onclick="setResult('ALL')">ALL</button>
<button class="fbtn" onclick="setResult('WIN')">WIN</button>
<button class="fbtn" onclick="setResult('LOSS')">LOSS</button>
<span class="flabel" style="margin-left:16px">Side:</span>
<button class="fbtn active" onclick="setSide('ALL')">ALL</button>
<button class="fbtn" onclick="setSide('YES')">YES</button>
<button class="fbtn" onclick="setSide('NO')">NO</button>
<span class="flabel" style="margin-left:16px">Period:</span>
<input type="date" id="dateFrom" class="fbtn" onchange="render()" style="cursor:pointer">
<span class="flabel">to</span>
<input type="date" id="dateTo" class="fbtn" onchange="render()" style="cursor:pointer">
<button class="fbtn" onclick="setDatePreset('today')">Today</button>
<button class="fbtn" onclick="setDatePreset('3d')">3D</button>
<button class="fbtn" onclick="setDatePreset('7d')">7D</button>
<button class="fbtn" onclick="setDatePreset('all')">All</button>
</div>

<div class="summary-cards" id="summary"></div>

<div id="chart-wrap" style="background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px;margin:10px 0">
<canvas id="pnlChart" height="180"></canvas>
</div>

<h2 id="table-title">All Trades</h2>
<table>
<thead>
<tr><th onclick="sortBy('ts')">Time</th><th onclick="sortBy('name')">Strategy</th>
<th onclick="sortBy('side')">Side</th><th onclick="sortBy('entry')">Entry</th>
<th onclick="sortBy('delta')">Delta</th><th>Signal</th>
<th onclick="sortBy('result')">Result</th><th>Outcome</th>
<th onclick="sortBy('pnl')">PnL</th><th>Window</th></tr>
</thead>
<tbody id="tbody"></tbody>
</table>

<script>
var ALL_TRADES=""" + _json.dumps(trades_json) + """;
var filterStrat='ALL', filterResult='ALL', filterSide='ALL';
var sortCol='ts', sortAsc=false;

function setDatePreset(p){
  var df=document.getElementById('dateFrom');
  var dt=document.getElementById('dateTo');
  if(p==='all'){df.value='';dt.value='';render();return}
  var now=new Date();
  dt.value=now.toISOString().slice(0,10);
  if(p==='today') df.value=dt.value;
  else if(p==='3d'){var d=new Date(now);d.setDate(d.getDate()-3);df.value=d.toISOString().slice(0,10)}
  else if(p==='7d'){var d=new Date(now);d.setDate(d.getDate()-7);df.value=d.toISOString().slice(0,10)}
  render();
}

function setStrat(v){filterStrat=v;updButtons();render()}
function setResult(v){filterResult=v;updButtons();render()}
function setSide(v){filterSide=v;updButtons();render()}

function updButtons(){
  document.querySelectorAll('.fbtn').forEach(function(b){
    var t=b.textContent;
    b.classList.remove('active');
    if((t===filterStrat)||(t===filterResult)||(t===filterSide)||(t==='ALL'&&filterStrat==='ALL'&&b.onclick.toString().indexOf('Strat')>0)||(t==='ALL'&&filterResult==='ALL'&&b.onclick.toString().indexOf('Result')>0)||(t==='ALL'&&filterSide==='ALL'&&b.onclick.toString().indexOf('Side')>0)){}
  });
  document.querySelectorAll('.fbtn').forEach(function(b){
    var txt=b.textContent;
    var fn=b.getAttribute('onclick')||'';
    if(fn.indexOf('setStrat')>=0&&((txt===filterStrat)||(txt==='ALL'&&filterStrat==='ALL'))) b.classList.add('active');
    if(fn.indexOf('setResult')>=0&&((txt===filterResult)||(txt==='ALL'&&filterResult==='ALL'))) b.classList.add('active');
    if(fn.indexOf('setSide')>=0&&((txt===filterSide)||(txt==='ALL'&&filterSide==='ALL'))) b.classList.add('active');
  });
}

function sortBy(col){
  if(sortCol===col) sortAsc=!sortAsc;
  else{sortCol=col;sortAsc=col==='ts'?false:true}
  render();
}

function filtered(){
  return ALL_TRADES.filter(function(t){
    if(filterStrat!=='ALL'&&t.name!==filterStrat) return false;
    if(filterResult!=='ALL'&&t.result!==filterResult) return false;
    if(filterSide!=='ALL'&&t.side!==filterSide) return false;
    var df=document.getElementById('dateFrom').value;
    var dt=document.getElementById('dateTo').value;
    if(df&&t.ts.slice(0,10)<df) return false;
    if(dt&&t.ts.slice(0,10)>dt) return false;
    return true;
  });
}

function render(){
  var data=filtered();
  // Sort
  data.sort(function(a,b){
    var va=a[sortCol],vb=b[sortCol];
    if(typeof va==='number'){return sortAsc?va-vb:vb-va}
    va=String(va);vb=String(vb);
    return sortAsc?va.localeCompare(vb):vb.localeCompare(va);
  });
  // Summary
  var n=data.length, wins=0, totalPnl=0, pnls=[];
  var peak=0,run=0,maxDD=0,grossW=0,grossL=0;
  for(var i=data.length-1;i>=0;i--){
    var p=data[i].pnl;pnls.push(p);totalPnl+=p;
    if(data[i].result==='WIN')wins++;
    run+=p;if(run>peak)peak=run;var dd=peak-run;if(dd>maxDD)maxDD=dd;
    if(p>0)grossW+=p;else grossL+=Math.abs(p);
  }
  var wr=n>0?(wins/n*100).toFixed(1)+'%':'--';
  var pf=grossL>0?(grossW/grossL).toFixed(2):'--';
  var avg=n>0?(totalPnl/n).toFixed(2):'--';
  var std=0;if(n>1){var m=totalPnl/n;for(var i=0;i<pnls.length;i++)std+=(pnls[i]-m)*(pnls[i]-m);std=Math.sqrt(std/(n-1))}
  var sharpe=std>0?(totalPnl/n/std).toFixed(3):'--';
  var pc=totalPnl>=0?'pos':'neg';
  document.getElementById('summary').innerHTML=
    '<div class="scard"><div class="sv">'+n+'</div><div class="sl">Trades</div></div>'+
    '<div class="scard"><div class="sv">'+wins+'</div><div class="sl">Wins</div></div>'+
    '<div class="scard"><div class="sv">'+(n-wins)+'</div><div class="sl">Losses</div></div>'+
    '<div class="scard"><div class="sv">'+wr+'</div><div class="sl">Win Rate</div></div>'+
    '<div class="scard"><div class="sv '+pc+'">$'+(totalPnl>=0?'+':'')+totalPnl.toFixed(0)+'</div><div class="sl">PnL</div></div>'+
    '<div class="scard"><div class="sv neg">$'+maxDD.toFixed(0)+'</div><div class="sl">MaxDD</div></div>'+
    '<div class="scard"><div class="sv">'+sharpe+'</div><div class="sl">Sharpe</div></div>'+
    '<div class="scard"><div class="sv">'+pf+'</div><div class="sl">PF</div></div>'+
    '<div class="scard"><div class="sv">$'+avg+'</div><div class="sl">Avg PnL</div></div>';
  // Title
  var title=filterStrat==='ALL'?'All Trades':filterStrat+' Trades';
  if(filterResult!=='ALL') title+=' ('+filterResult+' only)';
  if(filterSide!=='ALL') title+=' ('+filterSide+' only)';
  document.getElementById('table-title').textContent=title+' ('+n+')';
  // Table
  var html='';
  var show=Math.min(data.length,1000);
  for(var i=0;i<show;i++){
    var t=data[i];
    var pc=t.pnl>0?'pos':t.pnl<0?'neg':'';
    var rc=t.result==='WIN'?'pos':'neg';
    html+='<tr><td>'+t.ts+'</td><td>'+t.name+'</td><td>'+t.side+'</td>'+
      '<td>'+t.entry.toFixed(3)+'</td><td>'+(t.delta>=0?'+':'')+t.delta.toFixed(0)+'</td>'+
      '<td>'+t.signal+'</td><td class="'+rc+'">'+t.result+'</td><td>'+t.outcome+'</td>'+
      '<td class="'+pc+'">$'+(t.pnl>=0?'+':'')+t.pnl.toFixed(2)+'</td><td>'+t.window+'</td></tr>';
  }
  document.getElementById('tbody').innerHTML=html;
  drawChart(data);
}

function drawChart(data){
  var canvas=document.getElementById('pnlChart');
  var ctx=canvas.getContext('2d');
  var W=canvas.parentElement.clientWidth-24;
  canvas.width=W;var H=canvas.height;
  ctx.clearRect(0,0,W,H);
  if(data.length<2){ctx.fillStyle='#8b949e';ctx.font='14px monospace';ctx.fillText('Not enough trades for chart',W/2-100,H/2);return}
  // Reverse to chronological
  var chron=data.slice().reverse();
  var cum=[0];for(var i=0;i<chron.length;i++)cum.push(cum[i]+chron[i].pnl);
  var mn=Math.min.apply(null,cum),mx=Math.max.apply(null,cum);
  if(mx===mn){mx=mn+100}
  var pad={l:60,r:20,t:25,b:30};
  var cw=W-pad.l-pad.r,ch=H-pad.t-pad.b;
  function x(i){return pad.l+i/cum.length*cw}
  function y(v){return pad.t+ch-(v-mn)/(mx-mn)*ch}
  // Grid
  ctx.strokeStyle='#30363d';ctx.lineWidth=0.5;
  var steps=5;for(var i=0;i<=steps;i++){
    var val=mn+(mx-mn)*i/steps;var yy=y(val);
    ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();
    ctx.fillStyle='#8b949e';ctx.font='10px monospace';ctx.textAlign='right';
    ctx.fillText('$'+val.toFixed(0),pad.l-5,yy+3);
  }
  // Zero line
  if(mn<0&&mx>0){ctx.strokeStyle='#8b949e';ctx.lineWidth=1;ctx.setLineDash([4,4]);
    ctx.beginPath();ctx.moveTo(pad.l,y(0));ctx.lineTo(W-pad.r,y(0));ctx.stroke();ctx.setLineDash([])}
  // Fill area
  ctx.beginPath();ctx.moveTo(x(0),y(0));
  for(var i=0;i<cum.length;i++)ctx.lineTo(x(i),y(cum[i]));
  ctx.lineTo(x(cum.length-1),y(0));ctx.closePath();
  var endPnl=cum[cum.length-1];
  ctx.fillStyle=endPnl>=0?'rgba(46,204,113,0.12)':'rgba(231,76,60,0.12)';ctx.fill();
  // Line
  ctx.beginPath();ctx.moveTo(x(0),y(cum[0]));
  for(var i=1;i<cum.length;i++)ctx.lineTo(x(i),y(cum[i]));
  ctx.strokeStyle=endPnl>=0?'#2ecc71':'#e74c3c';ctx.lineWidth=2;ctx.stroke();
  // Dots for losses
  for(var i=0;i<chron.length;i++){
    if(chron[i].result==='LOSS'){
      ctx.beginPath();ctx.arc(x(i+1),y(cum[i+1]),3,0,Math.PI*2);
      ctx.fillStyle='#e74c3c';ctx.fill();
    }
  }
  // Title
  ctx.fillStyle='#c9d1d9';ctx.font='bold 13px monospace';ctx.textAlign='left';
  var label='Cumulative PnL';
  if(filterStrat!=='ALL')label+=': '+filterStrat;
  label+=' ($'+(endPnl>=0?'+':'')+endPnl.toFixed(0)+')';
  ctx.fillText(label,pad.l,16);
  // X-axis labels
  ctx.fillStyle='#8b949e';ctx.font='9px monospace';ctx.textAlign='center';
  var xlabels=6;
  for(var i=0;i<xlabels;i++){
    var idx=Math.floor(i/(xlabels-1)*(chron.length-1));
    var ts=chron[idx]?chron[idx].ts.substring(5,16):'';
    ctx.fillText(ts,x(idx+1),H-5);
  }
  // Max drawdown shading
  var peakVal=cum[0],ddStart=0,ddEnd=0,maxDDv=0;
  for(var i=1;i<cum.length;i++){
    if(cum[i]>peakVal)peakVal=cum[i];
    var dd=peakVal-cum[i];
    if(dd>maxDDv){maxDDv=dd;ddEnd=i;
      for(var j=i;j>=0;j--){if(cum[j]>=peakVal){ddStart=j;break}}}
  }
  if(maxDDv>0){
    ctx.fillStyle='rgba(231,76,60,0.08)';
    ctx.fillRect(x(ddStart),pad.t,x(ddEnd)-x(ddStart),ch);
    ctx.fillStyle='#e74c3c';ctx.font='9px monospace';ctx.textAlign='center';
    ctx.fillText('MaxDD $'+maxDDv.toFixed(0),(x(ddStart)+x(ddEnd))/2,pad.t+12);
  }
}
window.addEventListener('resize',function(){render()});
render();
</script>
</body></html>"""
    return html.encode()




# __PATCHED_TV_CHART__
CHART_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Live Chart</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0b1220;color:#c9d1d9;font-family:-apple-system,monospace;height:100vh;overflow:hidden;display:flex;flex-direction:column}
nav{text-align:center;padding:10px;background:#161b22;border-bottom:1px solid #30363d;flex:0 0 auto}
nav a{color:#58a6ff;text-decoration:none;padding:6px 16px;background:#0d1117;border-radius:6px;margin:0 3px;font-size:13px}
nav a:hover{background:#1c2333}
nav a.active{color:#e040fb}
#chart-frame{flex:1 1 auto;border:0;width:100%}
</style>
</head><body>
<nav>
<a href="/">LIVE</a>
<a href="/backtest">BACKTEST</a>
<a href="/history">HISTORY</a>
<a href="/metrics">METRICS</a>
<a href="/chart" class="active">CHART</a>
</nav>
<iframe id="chart-frame" src="/tv/index.html?theme=dark" title="TradingView"></iframe>
</body></html>"""

# === PNL_CHART_PATCH_v1 START ===

PNL_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Paper PnL — strategies</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
nav{padding:8px 14px;background:#161b22;border-bottom:1px solid #30363d;flex-shrink:0}
nav a{color:#58a6ff;text-decoration:none;padding:4px 10px;font-size:13px;border-radius:4px}
nav a.active{color:#fff;background:#1f6feb}
nav a:hover{background:#1c2333}
#top{display:flex;gap:10px;align-items:center;padding:8px 14px;border-bottom:1px solid #21262d;flex-wrap:wrap;flex-shrink:0}
.range{display:flex;gap:4px;align-items:center}
.range button{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px}
.range button:hover{background:#30363d}
.range button.active{background:#1f6feb;border-color:#1f6feb}
#info{color:#8b949e;font-size:12px;margin-left:auto}
#legend{display:flex;flex-wrap:wrap;gap:6px;padding:8px 14px;border-bottom:1px solid #21262d;flex-shrink:0;max-height:80px;overflow-y:auto}
.lg-item{display:flex;gap:6px;align-items:center;padding:4px 10px;background:#161b22;border-radius:6px;cursor:pointer;font-size:11px;user-select:none;border:1px solid #21262d}
.lg-item:hover{border-color:#30363d}
.lg-item.off{opacity:0.35}
.lg-color{width:11px;height:11px;border-radius:2px;flex-shrink:0}
.lg-pnl{font-family:'Cascadia Code',monospace;font-weight:700}
.lg-pnl.pos{color:#5fcf91}
.lg-pnl.neg{color:#ff6b7d}
.lg-trades{color:#8b949e}
#wrap{flex:1 1 auto;width:100%}
</style>
</head>
<body>
<nav>
<a href="/">LIVE</a>
<a href="/backtest">BACKTEST</a>
<a href="/metrics">METRICS</a>
<a href="/chart">CHART</a>
<a href="/pnl" class="active">PNL</a>
</nav>
<div id="top">
  <div class="range" id="range">
    <button data-d="0.5">12h</button>
    <button data-d="1">1d</button>
    <button data-d="3" class="active">3d</button>
    <button data-d="7">7d</button>
    <button data-d="14">14d</button>
    <button data-d="30">30d</button>
    <button id="refresh">refresh</button>
  </div>
  <span id="info">loading...</span>
</div>
<div id="legend"></div>
<div id="wrap"></div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const wrap=document.getElementById('wrap');
const legend=document.getElementById('legend');
const info=document.getElementById('info');
const chart=LightweightCharts.createChart(wrap,{
  layout:{background:{color:'#0d1117'},textColor:'#e6edf3'},
  grid:{vertLines:{color:'#21262d'},horzLines:{color:'#21262d'}},
  rightPriceScale:{scaleMargins:{top:0.1,bottom:0.1}},
  timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#30363d'},
  crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
});
const zeroLine=chart.addLineSeries({color:'#30363d',lineWidth:1,priceLineVisible:false,lineStyle:LightweightCharts.LineStyle.Dashed});
function resize(){chart.applyOptions({width:wrap.clientWidth,height:wrap.clientHeight})}
window.addEventListener('resize',resize);resize();

let allSessions=[];
let seriesBySid={};
let currentDays=3;

function makeLegend(){
  legend.innerHTML='';
  for(const s of allSessions){
    const lastPnl=s.points.length?s.points[s.points.length-1].cum_pnl:0;
    const cls=lastPnl>=0?'pos':'neg';
    const sign=lastPnl>=0?'+':'';
    const item=document.createElement('div');
    item.className='lg-item';
    item.dataset.sid=s.session_id;
    item.innerHTML=
      `<span class="lg-color" style="background:${s.color}"></span>`+
      `<span>${s.session_id.substring(0,18)}</span>`+
      `<span class="lg-pnl ${cls}">${sign}$${lastPnl.toFixed(0)}</span>`+
      `<span class="lg-trades">${s.trades}t</span>`;
    item.addEventListener('click',()=>{
      item.classList.toggle('off');
      const series=seriesBySid[s.session_id];
      if(series)series.applyOptions({visible:!item.classList.contains('off')});
    });
    legend.appendChild(item);
  }
}

function loadData(days){
  currentDays=days;
  info.textContent='loading...';
  fetch(`/api/pnl_history?days=${days}`,{cache:'no-store'})
    .then(r=>r.json())
    .then(d=>{
      if(d.error){info.textContent='error: '+d.error;return}
      for(const sid of Object.keys(seriesBySid)){chart.removeSeries(seriesBySid[sid])}
      seriesBySid={};
      allSessions=d.sessions||[];
      allSessions.sort((a,b)=>{
        const ap=a.points.length?a.points[a.points.length-1].cum_pnl:0;
        const bp=b.points.length?b.points[b.points.length-1].cum_pnl:0;
        return bp-ap;
      });
      let minT=null,maxT=null;
      for(const s of allSessions){
        const series=chart.addLineSeries({color:s.color,lineWidth:2,priceLineVisible:false,title:s.session_id.substring(0,14)});
        const points=s.points.map(p=>({time:p.ts,value:p.cum_pnl}));
        if(points.length){
          series.setData(points);
          if(minT===null||points[0].time<minT)minT=points[0].time;
          if(maxT===null||points[points.length-1].time>maxT)maxT=points[points.length-1].time;
        }
        seriesBySid[s.session_id]=series;
      }
      if(minT!==null&&maxT!==null){
        zeroLine.setData([{time:minT,value:0},{time:maxT,value:0}]);
      }
      try{chart.timeScale().fitContent()}catch(e){}
      makeLegend();
      const total=allSessions.reduce((acc,s)=>acc+(s.trades||0),0);
      info.textContent=`${allSessions.length} sessions · ${total} trades · last ${days}d`;
    })
    .catch(e=>{info.textContent='fetch error: '+e});
}

document.querySelectorAll('#range button[data-d]').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('#range button[data-d]').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    loadData(parseFloat(btn.dataset.d));
  });
});
document.getElementById('refresh').addEventListener('click',()=>loadData(currentDays));
setInterval(()=>loadData(currentDays),60000);
loadData(3);
</script>
</body>
</html>"""
# === PNL_CHART_PATCH_v1 END ===




class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == "/api/state":
            from .helpers import get_strategy_formula
            with state_lock:
                data = dict(state)
                data.pop("errors", None)
            data["strategy_formula"] = get_strategy_formula()
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/" or path == "/dashboard":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/metrics":
            body = METRICS_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/backtest":
            body = BACKTEST_HTML_NEW.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/metrics/chart":
            import os
            chart_path = "/tmp/metrics_timeline.png"
            if os.path.exists(chart_path):
                with open(chart_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()
            return
        elif path == "/api/metrics":
            try:
                conn = sqlite3.connect(DB_PATH, timeout=2)
                rows = conn.execute("""
                    SELECT strftime('%H:%M', ts) as minute,
                        AVG(ABS(delta_from_open)) as delta,
                        AVG(sigma) as sigma,
                        AVG(buy_vol_1min + sell_vol_1min) as volume,
                        AVG(trade_count_1min) as trade_count,
                        AVG(binance_price) as btc_price
                    FROM ticks
                    WHERE ts >= datetime('now', '-60 minutes')
                    AND binance_price IS NOT NULL AND sigma > 0
                    GROUP BY strftime('%H:%M', ts)
                    ORDER BY ts
                """).fetchall()
                today_trades = conn.execute("""
                    SELECT COUNT(*),
                        SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END),
                        SUM(pnl)
                    FROM paper_trades
                    WHERE DATE(ts) = DATE('now') AND result IS NOT NULL
                    AND session_id = 's1773258829954'
                """).fetchone()
                daily_avg = conn.execute("""
                    SELECT ROUND(AVG(ABS(delta_from_open)),2),
                        ROUND(AVG(sigma)*1000000,2),
                        ROUND(AVG(buy_vol_1min + sell_vol_1min),0)
                    FROM ticks
                    WHERE DATE(ts) BETWEEN '2026-03-17' AND '2026-03-19'
                    AND binance_price IS NOT NULL AND sigma > 0
                """).fetchone()
                conn.close()
                data = {
                    "minutes": [{"t": r[0], "delta": round(r[1] or 0, 2), "sigma": round((r[2] or 0)*1e6, 3),
                                 "volume": round(r[3] or 0, 0), "trades": round(r[4] or 0, 0),
                                 "btc": round(r[5] or 0, 2)} for r in rows],
                    "today": {"n": today_trades[0] or 0, "wins": today_trades[1] or 0,
                              "pnl": round(today_trades[2] or 0, 2)},
                    "weekday_avg": {"delta": daily_avg[0] or 0, "sigma": daily_avg[1] or 0, "volume": daily_avg[2] or 0}
                }
                body = json.dumps(data).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/debug":
            now = time.time()
            with conn_lock:
                debug = {}
                for name, info in conn_status.items():
                    debug[name] = {
                        "connected": info["connected"],
                        "last_error": info["last_error"],
                        "msg_count": info["msg_count"],
                        "last_msg_ago": round(now - info["last_msg_time"], 1) if info["last_msg_time"] else None,
                    }
            with state_lock:
                debug["market_slug"] = state.get("market_slug")
                debug["market_token_ids"] = state.get("market_token_ids", [])
                debug["chainlink_price"] = state.get("chainlink_price")
                debug["binance_price"] = state.get("binance_price")
            debug["uptime_s"] = int(now - start_time)
            debug["websockets_version"] = getattr(websockets, "__version__", "unknown") if websockets else "missing"
            from .helpers import _sigma_cache as _sc
            debug["sigma_cache"] = {k: round(v, 10) for k, v in _sc.items()} if _sc else "EMPTY"
            debug["sigma_cache_len"] = len(_sc)
            body = json.dumps(debug, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/orderbook":
            with ob_lock:
                ob_data = {
                    "yes": {"bids": orderbook["yes"]["bids"], "asks": orderbook["yes"]["asks"], "ts": orderbook["yes"].get("ts"),
                            "levels": orderbook["yes"].get("levels", 0), "total_bid_vol": orderbook["yes"].get("total_bid_vol", 0),
                            "total_ask_vol": orderbook["yes"].get("total_ask_vol", 0), "src": orderbook["yes"].get("src", "?")},
                    "no": {"bids": orderbook["no"]["bids"], "asks": orderbook["no"]["asks"], "ts": orderbook["no"].get("ts"),
                           "levels": orderbook["no"].get("levels", 0), "total_bid_vol": orderbook["no"].get("total_bid_vol", 0),
                           "total_ask_vol": orderbook["no"].get("total_ask_vol", 0), "src": orderbook["no"].get("src", "?")},
                    "ws_msgs": _clob_msg_count[0],
                }
            with state_lock:
                ob_data["market_slug"] = state.get("market_slug")
                ob_data["yes_token_id"] = state.get("yes_token_id")
                ob_data["no_token_id"] = state.get("no_token_id")
                ob_data["market_resolved"] = state.get("market_resolved", False)
                ob_data["p_model"] = state.get("p_model")
                ob_data["delta"] = state.get("delta_from_prev")
                ob_data["delta_open"] = state.get("delta_from_open")
            body = json.dumps(ob_data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/sessions_state":
            from .paper_trading import get_all_sessions
            body = json.dumps(get_all_sessions()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        elif path.startswith("/api/sessions_state_") or path.startswith("/api/state_"):
            # Proxy other venue/window instances (same-origin). {} on failure so
            # the 5m dashboard never breaks if an instance is down.
            import urllib.request
            _PROXY_PORTS = {"15m": 8889, "ll5m": 8890, "ll15m": 8891, "llhourly": 8892, "k15m": 8893}
            if path.startswith("/api/sessions_state_"):
                _tag = path[len("/api/sessions_state_"):]; up = "/api/sessions_state"
            else:
                _tag = path[len("/api/state_"):]; up = "/api/state"
            _pport = _PROXY_PORTS.get(_tag)
            body = b"{}"
            if _pport:
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (_pport, up), timeout=2) as r:
                        body = r.read()
                except Exception:
                    body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        elif path.startswith("/api/session_extended_stats"):
            import sqlite3 as _sq, urllib.parse as _up, math as _m
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            qs = _up.parse_qs(_up.urlparse(self.path).query)
            sid = qs.get("sid", [""])[0]
            out = {}
            try:
                cn = _sq.connect("file:%s?mode=ro" % DB_PATH, uri=True, timeout=3)
                rows = cn.execute(
                    "SELECT pnl, result, entry_price, window_start FROM paper_trades "
                    "WHERE session_id=? AND result IN ('WIN','LOSS') ORDER BY window_start", (sid,)).fetchall()
                cn.close()
                n = len(rows)
                if n:
                    pnls = [(r[0] or 0.0) for r in rows]
                    wins = sum(1 for r in rows if r[1] == "WIN")
                    tot = sum(pnls)
                    gw = sum(p for p in pnls if p > 0); gl = sum(-p for p in pnls if p < 0)
                    peak = run = maxdd = 0.0
                    for p in pnls:
                        run += p
                        if run > peak: peak = run
                        if peak - run > maxdd: maxdd = peak - run
                    mean = tot / n
                    std = (sum((p - mean) ** 2 for p in pnls) / (n - 1)) ** 0.5 if n > 1 else 0.0
                    sharpe = (mean / std) if std > 0 else 0.0
                    avg_entry = sum((r[2] or 0) for r in rows) / n
                    # last 7 days EV
                    cut = (_dt.now(_tz.utc) - _td(days=7)).isoformat()
                    rec = [(r[0] or 0) for r in rows if (r[3] or "") >= cut]
                    ev7 = (sum(rec) / len(rec)) if rec else 0.0
                    out = {"n": n, "wins": wins, "losses": n - wins, "wr": wins / n * 100,
                           "pnl": tot, "maxdd": maxdd, "sharpe": sharpe,
                           "pf": (gw / gl) if gl > 0 else 0.0, "avg": mean,
                           "avg_entry": avg_entry, "ev7": ev7, "n7": len(rec)}
            except Exception as _e:
                out = {"error": str(_e)[:80]}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return
        elif path == "/api/paper_state":
            ps = get_paper_state()
            body = json.dumps(ps).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/db/stats":
            try:
                conn = sqlite3.connect(DB_PATH, timeout=2)
                ticks = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
                signals = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
                windows = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
                ob_snaps = conn.execute("SELECT COUNT(*) FROM orderbook_snapshots").fetchone()[0]
                first = conn.execute("SELECT ts FROM ticks ORDER BY rowid ASC LIMIT 1").fetchone()
                last = conn.execute("SELECT ts FROM ticks ORDER BY rowid DESC LIMIT 1").fetchone()
                conn.close()
                body = json.dumps({
                    "ticks": ticks, "signals": signals, "windows": windows,
                    "orderbook_snapshots": ob_snaps,
                    "first_tick": first[0] if first else None,
                    "last_tick": last[0] if last else None,
                    "db_path": DB_PATH,
                }).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/history":
            body = _build_history_html()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/trades_for_chart":
            # Stream trades from live_trading/trades.db as TV-compatible markers.
            import sqlite3 as _sq3, urllib.parse as _up
            try:
                q = _up.urlparse(self.path).query
                qs = _up.parse_qs(q)
                limit = int(qs.get("limit", ["2000"])[0])
                limit = max(10, min(5000, limit))
            except Exception:
                limit = 2000
            rows = []
            db_path = "/root/live_trading/trades.db"
            try:
                conn = _sq3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
                conn.row_factory = _sq3.Row
                cur = conn.execute(
                    "SELECT id,ts,side,entry_price,fill_price,stake,pnl,result,delta,"
                    "market_slug,window_start,fill_status,strategy "
                    "FROM trades WHERE fill_status IN ('FILLED','matched') "
                    "AND strategy='third' ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                for r in cur.fetchall():
                    # Prefer window_start (exact 5min boundary); fallback to ts.
                    ts_ms = None
                    try:
                        ws = r["window_start"]
                        if ws:
                            from datetime import datetime as _dt
                            ts_ms = int(_dt.fromisoformat(ws.replace("Z","+00:00")).timestamp() * 1000)
                    except Exception:
                        pass
                    if ts_ms is None:
                        try:
                            from datetime import datetime as _dt
                            ts_ms = int(_dt.fromisoformat(r["ts"].replace("Z","+00:00")).timestamp() * 1000)
                        except Exception:
                            continue
                    rows.append({
                        "trade_id": r["id"],
                        "ts_ms": ts_ms,
                        "side": r["side"],
                        "entry_price": r["entry_price"],
                        "fill_price": r["fill_price"],
                        "stake": r["stake"],
                        "pnl": r["pnl"],
                        "result": r["result"],
                        "delta": r["delta"],
                        "market_slug": r["market_slug"],
                    })
                conn.close()
            except Exception as _e:
                rows = []
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(rows).encode())
            return


        

        # === PNL_CHART_PATCH_v1 START ===
        elif path == "/pnl":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache")
            self.end_headers()
            self.wfile.write(PNL_HTML.encode())
            return

        elif path == "/api/pnl_history":
            import sqlite3 as _sq3, urllib.parse as _up, hashlib as _hl, os as _os
            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
            try:
                qs = _up.parse_qs(_up.urlparse(self.path).query)
                days = float(qs.get("days", ["7"])[0])
                days = max(0.1, min(days, 90.0))
            except Exception:
                days = 7.0
            cutoff = (_dt.now(_tz.utc) - _td(days=days)).isoformat()
            db_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "live_data.db")
            db_path = _os.path.normpath(db_path)
            sessions_out = {}
            try:
                conn = _sq3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
                conn.row_factory = _sq3.Row
                cur = conn.execute(
                    "SELECT session_id, ts, COALESCE(resolved_ts, ts) AS sort_ts, "
                    "pnl, stake "
                    "FROM paper_trades "
                    "WHERE pnl IS NOT NULL "
                    "AND (COALESCE(resolved_ts, ts)) >= ? "
                    "ORDER BY sort_ts ASC",
                    (cutoff,),
                )
                for r in cur.fetchall():
                    sid = r["session_id"] or "?"
                    try:
                        ts = _dt.fromisoformat(r["sort_ts"].replace("Z", "+00:00"))
                        ts_unix = int(ts.timestamp())
                    except Exception:
                        continue
                    if sid not in sessions_out:
                        colhex = _hl.md5(sid.encode()).hexdigest()[:6]
                        sessions_out[sid] = {
                            "session_id": sid,
                            "stake": float(r["stake"]) if r["stake"] is not None else None,
                            "color": "#" + colhex,
                            "_cum": 0.0,
                            "trades": 0,
                            "points": [],
                        }
                    sessions_out[sid]["_cum"] += float(r["pnl"])
                    sessions_out[sid]["trades"] += 1
                    # Dedupe by ts — lightweight-charts requires strictly increasing time.
                    # If multiple trades resolved in the same unix second, merge cum_pnl
                    # into the last point instead of appending a duplicate-time entry.
                    pts = sessions_out[sid]["points"]
                    if pts and pts[-1]["ts"] == ts_unix:
                        pts[-1]["cum_pnl"] = round(sessions_out[sid]["_cum"], 2)
                    else:
                        pts.append({
                            "ts": ts_unix,
                            "cum_pnl": round(sessions_out[sid]["_cum"], 2),
                        })
                conn.close()
                for s in sessions_out.values():
                    s.pop("_cum", None)
                body = json.dumps({
                    "sessions": list(sessions_out.values()),
                    "days_requested": days,
                    "from_ts": cutoff,
                    "db_path": db_path,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except Exception as _e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(_e), "db_path": db_path}).encode())
            return
        # === PNL_CHART_PATCH_v1 END ===

        elif path == "/chart":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(CHART_HTML.encode())
            return

        elif path.startswith("/tv/"):
            # Static file server for TradingView assets under /root/doubletrade2notactual/tv/
            import os as _os, mimetypes as _mt
            rel = path[len("/tv/"):]
            # Reject path traversal attempts
            if ".." in rel or rel.startswith("/"):
                self.send_response(403); self.end_headers(); return
            full = _os.path.join("/root/doubletrade2notactual/tv", rel)
            if not _os.path.isfile(full):
                self.send_response(404); self.end_headers(); return
            ctype, _ = _mt.guess_type(full)
            if not ctype:
                if full.endswith(".js"): ctype = "application/javascript"
                elif full.endswith(".css"): ctype = "text/css"
                elif full.endswith(".html"): ctype = "text/html"
                else: ctype = "application/octet-stream"
            try:
                with open(full, "rb") as _f:
                    data = _f.read()
            except Exception:
                self.send_response(500); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif path == "/api/fader_trades":
            # Fader-strategy trades for the last N hours, precomputed and
            # cached in /tmp/fader_trades.json. Regen via cron or manually
            # with `python3 /root/doubletrade2notactual/compute_fader.py`.
            try:
                with open("/tmp/fader_trades.json", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/metrics/generate":
            import subprocess
            try:
                subprocess.Popen(["python3", "/root/doubletrade2notactual/generate_metrics.py"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                import time as _t; _t.sleep(8)
                body = json.dumps({"ok": True}).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b'{}'
        try:
            params = json.loads(body)
        except Exception:
            params = {}

        ppath = self.path.split('?')[0]
        if ppath == "/api/backtest":
            result = run_backtest(params)
        elif ppath == "/api/paper/config":
            result = configure_paper(
                starting_balance=params.get("starting_balance"),
                stake_per_trade=params.get("stake_per_trade"),
                reset=params.get("reset", False),
            )
        elif ppath == "/api/sessions":
            from .paper_trading import get_all_sessions
            result = get_all_sessions()
        elif ppath == "/api/session/create":
            from .paper_trading import create_session
            sid = params.get("id", f"s{int(time.time())}")
            cfg = params.get("config", {})
            bal = float(params.get("balance", 10000))
            result = create_session(sid, cfg, bal) or {"error": "failed"}
        elif ppath == "/api/session/delete":
            from .paper_trading import delete_session
            sid = params.get("id")
            ok = delete_session(sid)
            result = {"status": "ok"} if ok else {"error": "not found"}
        elif ppath == "/api/session/config":
            from .paper_trading import update_session_config
            sid = params.get("id")
            updates = {k: v for k, v in params.items() if k != "id"}
            result = update_session_config(sid, updates)
        elif ppath == "/api/session/reset":
            from .paper_trading import reset_session
            sid = params.get("id")
            bal = float(params["balance"]) if "balance" in params else None
            result = reset_session(sid, bal)
        elif ppath == "/api/session/pause":
            from .paper_trading import update_session_config
            sid = params.get("id")
            result = update_session_config(sid, {"paused": True})
        elif ppath == "/api/session/resume":
            from .paper_trading import update_session_config
            sid = params.get("id")
            result = update_session_config(sid, {"paused": False})
        elif ppath == "/api/backtest/reload":
            load_backtest_data()
            from .backtest import _data_info
            result = {"status": "ok", "data": _data_info}
        else:
            result = {'error': 'unknown endpoint'}

        resp = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    address_family = socket.AF_INET
    daemon_threads = True


def run_http():
    server = ThreadedHTTPServer(("0.0.0.0", HTTP_PORT), DashboardHandler)
    print(f"[dashboard] HTTP server on http://0.0.0.0:{HTTP_PORT}", flush=True)
    server.serve_forever()
