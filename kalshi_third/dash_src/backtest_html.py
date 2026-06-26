"""Backtest page HTML template."""

BACKTEST_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>P-Model Backtester — NEWUPDATEDDATA</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#e0e0e0;padding:12px;font-size:14px}
a{color:#58a6ff;text-decoration:none}
h1{text-align:center;color:#4fc3f7;margin-bottom:8px;font-size:1.3em}
.nav{text-align:center;margin-bottom:12px}
.nav a{padding:8px 24px;border-radius:6px;margin:0 4px;font-weight:700}
.nav .active{background:#1e2a3a;color:#4fc3f7}
.nav .inactive{background:#141b2d;color:#8b949e}
.layout{display:flex;gap:12px;max-width:1800px;margin:0 auto}
.panel{background:#141b2d;border:1px solid #1e2a3a;border-radius:8px;padding:14px}
.controls{width:320px;min-width:320px;max-height:calc(100vh - 80px);overflow-y:auto}
.results{flex:1;min-width:0}
.controls h2{color:#4fc3f7;font-size:0.95em;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px}
.ctrl{margin-bottom:12px}
.ctrl label{display:block;color:#8b949e;font-size:0.82em;margin-bottom:3px}
.ctrl .val{float:right;color:#4fc3f7;font-weight:700;font-family:'Cascadia Code',monospace;font-size:0.9em}
.ctrl input[type=range]{width:100%;accent-color:#4fc3f7;cursor:pointer}
.ctrl select{width:100%;background:#0a0e17;color:#e0e0e0;border:1px solid #1e2a3a;padding:6px 8px;border-radius:4px;font-size:0.9em;cursor:pointer}
.ctrl select:focus{border-color:#4fc3f7;outline:none}
.btn{background:#1b5e20;color:#fff;border:none;padding:10px 0;border-radius:6px;cursor:pointer;font-size:1em;font-weight:700;width:100%;margin-top:6px;letter-spacing:0.5px;transition:background 0.2s}
.btn:hover{background:#2e7d32}
.btn:active{background:#1b5e20}
.presets{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.preset{background:#1e2a3a;color:#8b949e;border:1px solid #30363d;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:0.78em;transition:all 0.15s}
.preset:hover{background:#30363d;color:#4fc3f7;border-color:#4fc3f7}
.divider{border:none;border-top:1px solid #1e2a3a;margin:14px 0}
.summary{display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:12px}
.stat{background:#0a0e17;border:1px solid #1e2a3a;border-radius:6px;padding:10px;text-align:center}
.stat .num{font-size:1.4em;font-weight:700;font-family:'Cascadia Code',monospace}
.stat .lbl{color:#78909c;font-size:0.7em;margin-top:2px;text-transform:uppercase}
.green{color:#4caf50}.red{color:#f44336}.yellow{color:#ffc107}.blue{color:#42a5f5}.white{color:#fff}.cyan{color:#4fc3f7}
.chart-box{background:#0a0e17;border:1px solid #1e2a3a;border-radius:8px;padding:12px;margin-bottom:12px;position:relative}
.chart-box h3{color:#78909c;font-size:0.8em;margin-bottom:8px;text-transform:uppercase}
#equity-canvas{width:100%;height:220px;display:block;cursor:crosshair}
.chart-info{width:260px;min-width:260px;background:#141b2d;border:1px solid #1e2a3a;border-radius:8px;padding:12px;font-size:0.78em;overflow-y:auto;max-height:calc(100vh - 80px);position:sticky;top:12px;align-self:flex-start}
.chart-info .ci-title{color:#4fc3f7;font-weight:700;margin-bottom:6px;font-size:1.05em}
.chart-info .ci-row{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid #141b2d}
.chart-info .ci-lbl{color:#78909c}
.chart-info .ci-val{color:#e0e0e0;font-family:'Cascadia Code',monospace;font-weight:600}
.chart-info .ci-section{color:#546e7a;font-size:0.9em;text-transform:uppercase;margin-top:8px;margin-bottom:3px;letter-spacing:0.5px}
.chart-info .ci-bar{height:14px;border-radius:2px;margin:2px 0}
.chart-info .ci-empty{color:#546e7a;text-align:center;padding:40px 0;font-style:italic}
.tabs{display:flex;gap:0;margin-bottom:0;border-bottom:2px solid #1e2a3a}
.tab{padding:8px 20px;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-2px;font-size:0.9em;transition:all 0.15s}
.tab:hover{color:#e0e0e0}
.tab.active{color:#4fc3f7;border-bottom-color:#4fc3f7;font-weight:600}
.tab-content{display:none;padding-top:10px}
.tab-content.active{display:block}
table{width:100%;border-collapse:collapse;font-size:0.82em}
th{background:#0d1117;color:#78909c;padding:6px 8px;text-align:left;font-weight:600;position:sticky;top:0;z-index:1}
td{padding:5px 8px;border-bottom:1px solid #141b2d}
tr:hover{background:#0d1117}
.trade-win{border-left:3px solid #4caf50}
.trade-loss{border-left:3px solid #f44336}
.formula-box{background:#0d1117;border:1px solid #1e2a3a;border-radius:6px;padding:10px;margin:10px 0;font-family:'Cascadia Code',monospace;font-size:0.85em;color:#4fc3f7;text-align:center}
#loading{display:none;text-align:center;padding:40px;color:#78909c;font-size:1.1em}
#loading .spinner{display:inline-block;width:24px;height:24px;border:3px solid #1e2a3a;border-top-color:#4fc3f7;border-radius:50%;animation:spin 0.8s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.empty-state{text-align:center;padding:60px 20px;color:#546e7a}
.empty-state .icon{font-size:3em;margin-bottom:12px}
.param-tag{display:inline-block;background:#1e2a3a;color:#8b949e;padding:2px 8px;border-radius:10px;font-size:0.75em;margin:1px}
.param-tag .pv{color:#4fc3f7}
.scroll-table{max-height:500px;overflow-y:auto;border:1px solid #1e2a3a;border-radius:6px}
.optgroup-label{color:#4fc3f7;font-weight:700}
</style>
</head>
<body>

<div class="nav">
<a href="/" class="inactive">LIVE</a>
<a href="/backtest" class="active">BACKTEST</a>
</div>
<h1>P-MODEL BACKTESTER</h1>

<div class="layout">

<!-- LEFT: Controls -->
<div class="panel controls">
<h2>Parameters</h2>

<div class="ctrl">
<label>Presets:</label>
<div class="presets">
<span class="preset" onclick="applyPreset('best5')">Best 5min</span>
<span class="preset" onclick="applyPreset('best15')">Best 15min</span>
<span class="preset" onclick="applyPreset('conservative')">Conservative</span>
<span class="preset" onclick="applyPreset('aggressive')">Aggressive</span>
<span class="preset" onclick="applyPreset('nofilter')">No Filter</span>
<span class="preset" onclick="applyPreset('ofi_combo')">Delta+OFI</span>
</div>
</div>

<div class="ctrl">
<label>Market: <span class="val" id="v-market">5min</span></label>
<select id="market" onchange="document.getElementById('v-market').textContent=this.value">
<option value="5min" selected>5-minute (465 windows)</option>
<option value="15min">15-minute (157 windows)</option>
</select>
</div>

<div class="ctrl">
<label>Delta source: <span class="val" id="v-delta-src">binance</span></label>
<select id="delta_source" onchange="document.getElementById('v-delta-src').textContent=this.value">
<option value="binance" selected>Binance (faster, more ticks)</option>
<option value="chainlink">Chainlink (oracle, on-chain)</option>
</select>
</div>

<hr class="divider">

<div class="ctrl">
<label>Sigma (volatility): <span class="val" id="v-sigma">static</span></label>
<select id="sigma" onchange="document.getElementById('v-sigma').textContent=this.value">
<optgroup label="Basic">
<option value="static" selected>static (0.0005)</option>
</optgroup>
<optgroup label="STD — trailing">
<option value="trail5">trail5 — STD 5min</option>
<option value="trail10">trail10 — STD 10min</option>
<option value="trail30">trail30 — STD 30min</option>
</optgroup>
<optgroup label="STD — per-minute">
<option value="trail5_pmin">trail5 per-min</option>
<option value="trail10_pmin">trail10 per-min ★</option>
<option value="trail30_pmin">trail30 per-min</option>
</optgroup>
<optgroup label="MAX |return|">
<option value="max5">max5 — MAX trail 5min</option>
<option value="max10">max10 — MAX trail 10min</option>
<option value="max30">max30 — MAX trail 30min</option>
<option value="max5_pmin">max5 per-min</option>
<option value="max10_pmin">max10 per-min ★</option>
<option value="max30_pmin">max30 per-min</option>
</optgroup>
<optgroup label="RANGE (Parkinson)">
<option value="range5">range5 — (max-min)/avg 5min</option>
<option value="range10">range10 — (max-min)/avg 10min ★★</option>
<option value="range30">range30 — (max-min)/avg 30min ★★★</option>
<option value="range5_pmin">range5 per-min</option>
<option value="range10_pmin">range10 per-min</option>
<option value="range30_pmin">range30 per-min</option>
</optgroup>
<optgroup label="Realized Vol">
<option value="realized5">realized5 — sqrt(sum(r²)) 5min</option>
<option value="realized10">realized10 — sqrt(sum(r²)) 10min</option>
<option value="realized5_pmin">realized5 per-min</option>
<option value="realized10_pmin">realized10 per-min</option>
</optgroup>
<optgroup label="Median">
<option value="median5">median5</option>
<option value="median10">median10</option>
<option value="median5_pmin">median5 per-min</option>
<option value="median10_pmin">median10 per-min</option>
</optgroup>
<optgroup label="MAD">
<option value="mad5">mad5 — mean abs return 5min</option>
<option value="mad10">mad10 — mean abs return 10min</option>
</optgroup>
<optgroup label="Intra-window (at entry)">
<option value="intra">intra — STD within window</option>
<option value="intra_pmin">intra per-min</option>
<option value="intra_max">intra MAX</option>
<option value="intra_range">intra range</option>
</optgroup>
</select>
</div>

<div class="ctrl">
<label>Tau mode (time decay): <span class="val" id="v-tau">sqrt</span></label>
<select id="tau_mode" onchange="document.getElementById('v-tau').textContent=this.value">
<option value="sqrt" selected>sqrt(τ)</option>
<option value="linear">linear τ ★</option>
<option value="none">none (f=1)</option>
</select>
</div>

<div class="ctrl">
<label>Kappa (κ sensitivity): <span class="val" id="v-kappa">1.5</span></label>
<input type="range" id="kappa" min="1" max="60" step="1" value="15" oninput="document.getElementById('v-kappa').textContent=(this.value/10).toFixed(1)">
</div>

<div class="ctrl">
<label>Entry time: <span class="val" id="v-entry">2 min</span></label>
<select id="entry_min" onchange="document.getElementById('v-entry').textContent=this.value+' min'">
<option value="1">1 min (τ=4 remaining)</option>
<option value="2" selected>2 min (τ=3 remaining)</option>
<option value="3">3 min (τ=2 remaining)</option>
</select>
</div>

<hr class="divider">

<div class="ctrl">
<label>Delta threshold (|Δ|≥): <span class="val" id="v-delta">$50</span></label>
<input type="range" id="delta_thresh" min="0" max="300" step="5" value="50" oninput="document.getElementById('v-delta').textContent='$'+this.value">
</div>

<div class="ctrl">
<label>P-model threshold (p≥): <span class="val" id="v-pthresh">off</span></label>
<input type="range" id="p_thresh" min="0" max="95" step="5" value="0" oninput="let v=this.value/100;document.getElementById('v-pthresh').textContent=v>0?v.toFixed(2):'off'">
</div>

<div class="ctrl">
<label>Edge threshold (p-entry≥): <span class="val" id="v-edge">off</span></label>
<input type="range" id="edge_thresh" min="0" max="50" step="1" value="0" oninput="let v=this.value/100;document.getElementById('v-edge').textContent=v>0?v.toFixed(2):'off'">
</div>

<div class="ctrl">
<label>OFI filter: <span class="val" id="v-ofi">off</span></label>
<select id="ofi_filter" onchange="document.getElementById('v-ofi').textContent=this.value">
<option value="off" selected>Off</option>
<option value="agree">OFI agrees with delta</option>
<option value="strong">Strong (|OFI| > $1M)</option>
<option value="vstrong">Very strong (|OFI| > $2M)</option>
</select>
</div>

<hr class="divider">

<div class="ctrl">
<label>Side: <span class="val" id="v-side">NO</span></label>
<select id="side" onchange="document.getElementById('v-side').textContent=this.value">
<option value="NO" selected>NO only (DOWN bets)</option>
<option value="YES">YES only (UP bets)</option>
<option value="BOTH">BOTH sides</option>
</select>
</div>

<div class="ctrl">
<label>Max entry price: <span class="val" id="v-maxentry">0.95</span></label>
<input type="range" id="max_entry" min="10" max="99" step="1" value="95" oninput="document.getElementById('v-maxentry').textContent=(this.value/100).toFixed(2)">
</div>

<div class="ctrl">
<label>Stake per trade: <span class="val" id="v-stake">$100</span></label>
<input type="range" id="stake" min="10" max="1000" step="10" value="100" oninput="document.getElementById('v-stake').textContent='$'+this.value">
</div>

<div class="ctrl">
<label>Min stake (liquidity): <span class="val" id="v-minstake">$5</span></label>
<input type="range" id="min_stake" min="0" max="100" step="5" value="5" oninput="document.getElementById('v-minstake').textContent='$'+this.value">
</div>

<button class="btn" onclick="runBacktest()">RUN BACKTEST</button>

<div id="formula-display" class="formula-box" style="margin-top:12px">
p = Φ(1.5 · |Δ| / (0.0005 · √τ))
</div>
</div>

<!-- RIGHT: Results -->
<div class="panel results">

<div id="loading"><span class="spinner"></span>Running backtest...</div>

<div id="empty-state" class="empty-state">
<div class="icon">📊</div>
<div>Set parameters and click <b>RUN BACKTEST</b></div>
<div style="color:#78909c;font-size:0.85em;margin-top:8px">Data: NEWUPDATEDDATA (228 × 5min, 78 × 15min windows)</div>
</div>

<div id="results-container" style="display:none">

<!-- Summary stats -->
<div class="summary" id="summary-grid">
<div class="stat"><div class="num white" id="s-trades">--</div><div class="lbl">Trades</div></div>
<div class="stat"><div class="num" id="s-wr">--</div><div class="lbl">Win Rate</div></div>
<div class="stat"><div class="num" id="s-pnl">--</div><div class="lbl">Total PnL</div></div>
<div class="stat"><div class="num" id="s-roi">--</div><div class="lbl">ROI</div></div>
<div class="stat"><div class="num" id="s-ev">--</div><div class="lbl">EV/$1</div></div>
<div class="stat"><div class="num" id="s-dd">--</div><div class="lbl">Max DD</div></div>
</div>

<div class="summary" style="grid-template-columns:repeat(4,1fr)">
<div class="stat"><div class="num green" id="s-wins">--</div><div class="lbl">Wins</div></div>
<div class="stat"><div class="num red" id="s-losses">--</div><div class="lbl">Losses</div></div>
<div class="stat"><div class="num cyan" id="s-avgentry">--</div><div class="lbl">Avg Entry</div></div>
<div class="stat"><div class="num yellow" id="s-avgedge">--</div><div class="lbl">Avg Edge</div></div>
</div>

<!-- Active params tags -->
<div id="params-tags" style="margin-bottom:10px"></div>

<!-- Equity curve -->
<div class="chart-box">
<h3>Equity Curve (balance over trades)</h3>
<canvas id="equity-canvas"></canvas>
</div>

<!-- Tabs -->
<div class="tabs">
<div class="tab active" data-tab="trades-tab">All Trades</div>
<div class="tab" data-tab="winners-tab">Winners</div>
<div class="tab" data-tab="losers-tab">Losers</div>
</div>

<div id="trades-tab" class="tab-content active">
<div class="scroll-table">
<table>
<thead><tr>
<th>#</th><th>Window</th><th>Side</th><th>Δ at entry</th><th>NO ask</th><th>NO bid</th><th>Entry $</th><th>Shares</th><th>Stake</th><th>L1</th><th>p-model</th><th>Edge</th><th>OFI</th><th>Result</th><th>Δ full</th><th>CL end</th><th>NO end</th><th>PnL</th><th>Balance</th>
</tr></thead>
<tbody id="trades-body"></tbody>
</table>
</div>
</div>

<div id="winners-tab" class="tab-content">
<div class="scroll-table">
<table>
<thead><tr><th>#</th><th>Window</th><th>Δ</th><th>Entry</th><th>p</th><th>Edge</th><th>PnL</th></tr></thead>
<tbody id="winners-body"></tbody>
</table>
</div>
</div>

<div id="losers-tab" class="tab-content">
<div class="scroll-table">
<table>
<thead><tr><th>#</th><th>Window</th><th>Δ</th><th>Entry</th><th>p</th><th>Edge</th><th>PnL</th></tr></thead>
<tbody id="losers-body"></tbody>
</table>
</div>
</div>

</div><!-- results-container -->
</div><!-- results panel -->

<!-- RIGHT: Trade Info -->
<div class="chart-info" id="chart-info">
<div class="ci-empty">Hover over chart<br>to see trade details</div>
</div>

</div><!-- layout -->

<script>
// Tabs
document.querySelectorAll('.tab').forEach(function(t){
  t.addEventListener('click',function(){
    var tabs=this.parentElement.querySelectorAll('.tab');
    tabs.forEach(function(x){x.classList.remove('active')});
    this.classList.add('active');
    var container=this.parentElement.parentElement;
    container.querySelectorAll('.tab-content').forEach(function(c){c.classList.remove('active')});
    var target=container.querySelector('#'+this.getAttribute('data-tab'));
    if(target)target.classList.add('active');
  });
});

function getParams(){
  return {
    market: document.getElementById('market').value,
    delta_source: document.getElementById('delta_source').value,
    sigma: document.getElementById('sigma').value,
    tau_mode: document.getElementById('tau_mode').value,
    kappa: (+document.getElementById('kappa').value/10),
    entry_min: +document.getElementById('entry_min').value,
    delta_thresh: +document.getElementById('delta_thresh').value,
    p_thresh: +document.getElementById('p_thresh').value/100,
    edge_thresh: +document.getElementById('edge_thresh').value/100,
    ofi_filter: document.getElementById('ofi_filter').value,
    side: document.getElementById('side').value,
    stake: +document.getElementById('stake').value,
    max_entry: +document.getElementById('max_entry').value/100,
    min_stake: +document.getElementById('min_stake').value
  };
}

function updateFormula(){
  var p=getParams();
  var sigmaStr=p.sigma==='static'?'0.0005':p.sigma;
  var tauStr=p.tau_mode==='sqrt'?'√τ':p.tau_mode==='linear'?'τ':'1';
  var f='p = Φ('+p.kappa.toFixed(1)+' · |Δ| / ('+sigmaStr+' · '+tauStr+'))';
  document.getElementById('formula-display').textContent=f;
}

// Update formula on any change
['market','delta_source','sigma','tau_mode','kappa','entry_min','delta_thresh','p_thresh','edge_thresh','ofi_filter','side','stake'].forEach(function(id){
  var el=document.getElementById(id);
  if(el)el.addEventListener('change',updateFormula);
  if(el)el.addEventListener('input',updateFormula);
});

function applyPreset(name){
  var presets={
    best5:{market:'5min',delta_source:'binance',sigma:'range10',tau_mode:'sqrt',kappa:20,entry_min:'3',delta_thresh:150,p_thresh:90,edge_thresh:0,ofi_filter:'off',side:'BOTH',max_entry:95,min_stake:5},
    best15:{market:'15min',delta_source:'binance',sigma:'trail5',tau_mode:'linear',kappa:5,entry_min:'2',delta_thresh:100,p_thresh:80,edge_thresh:0,ofi_filter:'off',side:'BOTH',max_entry:95,min_stake:5},
    conservative:{market:'5min',delta_source:'binance',sigma:'range10',tau_mode:'linear',kappa:5,entry_min:'3',delta_thresh:100,p_thresh:70,edge_thresh:10,ofi_filter:'agree',side:'BOTH',max_entry:95,min_stake:10},
    aggressive:{market:'5min',delta_source:'binance',sigma:'trail10_pmin',tau_mode:'linear',kappa:5,entry_min:'2',delta_thresh:25,p_thresh:0,edge_thresh:0,ofi_filter:'off',side:'BOTH',max_entry:95,min_stake:5},
    nofilter:{market:'5min',delta_source:'binance',sigma:'static',tau_mode:'sqrt',kappa:15,entry_min:'2',delta_thresh:0,p_thresh:0,edge_thresh:0,ofi_filter:'off',side:'BOTH',max_entry:95,min_stake:0},
    ofi_combo:{market:'5min',delta_source:'binance',sigma:'static',tau_mode:'linear',kappa:5,entry_min:'3',delta_thresh:50,p_thresh:0,edge_thresh:0,ofi_filter:'agree',side:'BOTH',max_entry:95,min_stake:10}
  };
  var s=presets[name];if(!s)return;
  document.getElementById('market').value=s.market;
  document.getElementById('delta_source').value=s.delta_source||'binance';
  document.getElementById('sigma').value=s.sigma;
  document.getElementById('tau_mode').value=s.tau_mode;
  document.getElementById('kappa').value=s.kappa;
  document.getElementById('entry_min').value=s.entry_min;
  document.getElementById('delta_thresh').value=s.delta_thresh;
  document.getElementById('p_thresh').value=s.p_thresh;
  document.getElementById('edge_thresh').value=s.edge_thresh;
  document.getElementById('ofi_filter').value=s.ofi_filter;
  document.getElementById('side').value=s.side;
  if(s.max_entry!=null)document.getElementById('max_entry').value=s.max_entry;
  if(s.min_stake!=null)document.getElementById('min_stake').value=s.min_stake;
  // Update display values
  document.getElementById('v-market').textContent=s.market;
  document.getElementById('v-delta-src').textContent=s.delta_source||'binance';
  document.getElementById('v-sigma').textContent=s.sigma;
  document.getElementById('v-tau').textContent=s.tau_mode;
  document.getElementById('v-kappa').textContent=(s.kappa/10).toFixed(1);
  document.getElementById('v-entry').textContent=s.entry_min+' min';
  document.getElementById('v-delta').textContent='$'+s.delta_thresh;
  var pt=s.p_thresh/100;document.getElementById('v-pthresh').textContent=pt>0?pt.toFixed(2):'off';
  var et=s.edge_thresh/100;document.getElementById('v-edge').textContent=et>0?et.toFixed(2):'off';
  document.getElementById('v-ofi').textContent=s.ofi_filter;
  document.getElementById('v-side').textContent=s.side;
  if(s.max_entry!=null)document.getElementById('v-maxentry').textContent=(s.max_entry/100).toFixed(2);
  if(s.min_stake!=null)document.getElementById('v-minstake').textContent='$'+s.min_stake;
  updateFormula();
  runBacktest();
}

function cls(v){return v>0?'green':v<0?'red':'white'}
function fmtPnl(v){return (v>=0?'+':'')+v.toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:0})}
function fmtOfi(v){if(!v)return'0';var a=Math.abs(v);if(a>=1e6)return(v/1e6).toFixed(1)+'M';if(a>=1e3)return(v/1e3).toFixed(0)+'K';return v.toFixed(0)}

function drawEquityCurve(data){
  var canvas=document.getElementById('equity-canvas');
  var dpr=window.devicePixelRatio||1;
  var rect=canvas.getBoundingClientRect();
  canvas.width=rect.width*dpr;
  canvas.height=rect.height*dpr;
  var ctx=canvas.getContext('2d');
  ctx.scale(dpr,dpr);
  var W=rect.width,H=rect.height;

  // Clear
  ctx.fillStyle='#0a0e17';
  ctx.fillRect(0,0,W,H);

  if(data.length<2){
    ctx.fillStyle='#546e7a';ctx.font='14px sans-serif';ctx.textAlign='center';
    ctx.fillText('No data',W/2,H/2);return;
  }

  var vals=data.map(function(d){return d.y});
  var minY=Math.min.apply(null,vals);
  var maxY=Math.max.apply(null,vals);
  var padY=(maxY-minY)*0.1||100;
  minY-=padY;maxY+=padY;
  var padL=60,padR=20,padT=10,padB=30;
  var cW=W-padL-padR,cH=H-padT-padB;

  function tx(i){return padL+i/(data.length-1)*cW}
  function ty(v){return padT+(1-(v-minY)/(maxY-minY))*cH}

  // Grid
  ctx.strokeStyle='#1e2a3a';ctx.lineWidth=1;
  var nGrid=5;
  for(var i=0;i<=nGrid;i++){
    var gv=minY+i*(maxY-minY)/nGrid;
    var gy=ty(gv);
    ctx.beginPath();ctx.moveTo(padL,gy);ctx.lineTo(W-padR,gy);ctx.stroke();
    ctx.fillStyle='#546e7a';ctx.font='11px monospace';ctx.textAlign='right';
    ctx.fillText('$'+Math.round(gv).toLocaleString(),padL-6,gy+4);
  }

  // Baseline at 10000
  var baseY=ty(10000);
  ctx.strokeStyle='#30363d';ctx.setLineDash([4,4]);
  ctx.beginPath();ctx.moveTo(padL,baseY);ctx.lineTo(W-padR,baseY);ctx.stroke();
  ctx.setLineDash([]);

  // Fill area
  ctx.beginPath();
  ctx.moveTo(tx(0),ty(data[0].y));
  for(var i=1;i<data.length;i++){ctx.lineTo(tx(i),ty(data[i].y))}
  ctx.lineTo(tx(data.length-1),ty(10000));
  ctx.lineTo(tx(0),ty(10000));
  ctx.closePath();
  var lastVal=data[data.length-1].y;
  var grad=ctx.createLinearGradient(0,padT,0,H-padB);
  if(lastVal>=10000){
    grad.addColorStop(0,'rgba(76,175,80,0.3)');grad.addColorStop(1,'rgba(76,175,80,0.02)');
  }else{
    grad.addColorStop(0,'rgba(244,67,54,0.02)');grad.addColorStop(1,'rgba(244,67,54,0.3)');
  }
  ctx.fillStyle=grad;ctx.fill();

  // Line
  ctx.beginPath();
  ctx.moveTo(tx(0),ty(data[0].y));
  for(var i=1;i<data.length;i++){ctx.lineTo(tx(i),ty(data[i].y))}
  ctx.strokeStyle=lastVal>=10000?'#4caf50':'#f44336';
  ctx.lineWidth=2;ctx.stroke();

  // Dots
  for(var i=0;i<data.length;i++){
    var x=tx(i),y=ty(data[i].y);
    ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);
    ctx.fillStyle=data[i].y>=10000?'#4caf50':'#f44336';
    ctx.fill();
  }

  // X axis labels (show every Nth)
  var step=Math.max(1,Math.floor(data.length/10));
  ctx.fillStyle='#546e7a';ctx.font='10px monospace';ctx.textAlign='center';
  for(var i=0;i<data.length;i+=step){
    var lbl=data[i].ts?data[i].ts.slice(5,16):'#'+i;
    ctx.fillText(lbl,tx(i),H-5);
  }

  // Store chart geometry for hover
  window._chartGeo={padL:padL,padR:padR,padT:padT,padB:padB,cW:cW,cH:cH,W:W,H:H,
    minY:minY,maxY:maxY,data:data,tx:tx,ty:ty};
}

// Draw crosshair overlay
function drawCrosshair(idx){
  var geo=window._chartGeo;if(!geo||idx<0)return;
  var canvas=document.getElementById('equity-canvas');
  var dpr=window.devicePixelRatio||1;
  var ctx=canvas.getContext('2d');

  // Redraw base chart first
  drawEquityCurve(geo.data);
  // Re-fetch geo after redraw (tx/ty are new closures)
  geo=window._chartGeo;

  // Now draw crosshair
  var rect=canvas.getBoundingClientRect();
  ctx.scale(dpr,dpr);
  var x=geo.tx(idx),y=geo.ty(geo.data[idx].y);

  // Vertical line
  ctx.strokeStyle='rgba(79,195,247,0.5)';ctx.lineWidth=1;ctx.setLineDash([3,3]);
  ctx.beginPath();ctx.moveTo(x,geo.padT);ctx.lineTo(x,geo.H-geo.padB);ctx.stroke();
  ctx.setLineDash([]);

  // Horizontal line
  ctx.strokeStyle='rgba(79,195,247,0.3)';ctx.setLineDash([3,3]);
  ctx.beginPath();ctx.moveTo(geo.padL,y);ctx.lineTo(geo.W-geo.padR,y);ctx.stroke();
  ctx.setLineDash([]);

  // Highlight dot
  ctx.beginPath();ctx.arc(x,y,6,0,Math.PI*2);
  ctx.fillStyle='rgba(79,195,247,0.3)';ctx.fill();
  ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);
  ctx.fillStyle=geo.data[idx].y>=10000?'#4caf50':'#f44336';ctx.fill();
  ctx.strokeStyle='#fff';ctx.lineWidth=1.5;ctx.stroke();

  // Value label
  ctx.fillStyle='#0d1117';ctx.fillRect(x-30,y-22,60,16);
  ctx.strokeStyle='#4fc3f7';ctx.lineWidth=1;ctx.strokeRect(x-30,y-22,60,16);
  ctx.fillStyle='#4fc3f7';ctx.font='bold 10px monospace';ctx.textAlign='center';
  ctx.fillText('$'+Math.round(geo.data[idx].y).toLocaleString(),x,y-11);
}

// Show trade info in panel
function showTradeInfo(tradeIdx){
  var panel=document.getElementById('chart-info');
  var trades=window._lastTrades;
  if(!trades||tradeIdx<0||tradeIdx>=trades.length){
    panel.innerHTML='<div class="ci-empty">Hover over chart<br>to see trade details</div>';
    return;
  }
  var t=trades[tradeIdx];
  var sideColor=t.side==='NO'?'#4caf50':'#42a5f5';
  var resColor=t.win?'#4caf50':'#f44336';

  var h='<div class="ci-title">Trade #'+t.n+'</div>';
  h+='<div class="ci-row"><span class="ci-lbl">Window</span><span class="ci-val" style="font-size:0.9em">'+t.window+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Side</span><span class="ci-val" style="color:'+sideColor+'">'+t.side+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Result</span><span class="ci-val" style="color:'+resColor+'">'+(t.win?'WIN':'LOSS')+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">PnL</span><span class="ci-val" style="color:'+(t.pnl>=0?'#4caf50':'#f44336')+'">$'+fmtPnl(t.pnl)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Balance</span><span class="ci-val">$'+t.balance.toLocaleString()+'</span></div>';

  h+='<div class="ci-section">Entry</div>';
  h+='<div class="ci-row"><span class="ci-lbl">Delta</span><span class="ci-val" style="color:'+(t.delta<0?'#f44336':'#4caf50')+'">$'+t.delta+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Entry price</span><span class="ci-val">'+t.entry.toFixed(3)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Stake</span><span class="ci-val">$'+Math.round(t.stake)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Shares</span><span class="ci-val">'+Math.round(t.shares)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">p-model</span><span class="ci-val">'+(t.p_model*100).toFixed(1)+'%</span></div>';

  h+='<div class="ci-section">Orderbook</div>';
  h+='<div class="ci-row"><span class="ci-lbl">Cheap ask</span><span class="ci-val">'+(t.no_ask!=null?t.no_ask.toFixed(3):'--')+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Cheap bid</span><span class="ci-val">'+(t.no_bid!=null?t.no_bid.toFixed(3):'--')+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">L1 shares</span><span class="ci-val">'+Math.round(t.l1_shares)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Bid vol</span><span class="ci-val">'+Math.round(t.total_bid_vol||0)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">Ask vol</span><span class="ci-val">'+Math.round(t.total_ask_vol||0)+'</span></div>';

  h+='<div class="ci-section">Resolve</div>';
  h+='<div class="ci-row"><span class="ci-lbl">CL delta</span><span class="ci-val" style="color:'+(t.cl_delta_full<0?'#f44336':'#4caf50')+'">$'+(t.cl_delta_full||0)+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">CL end</span><span class="ci-val">$'+(t.cl_end||0).toLocaleString()+'</span></div>';
  h+='<div class="ci-row"><span class="ci-lbl">End ask/bid</span><span class="ci-val">'+(t.no_ask_end!=null?t.no_ask_end.toFixed(2):'--')+'/'+(t.no_bid_end!=null?t.no_bid_end.toFixed(2):'--')+'</span></div>';

  panel.innerHTML=h;
}

// Canvas mouse events
(function(){
  var canvas=document.getElementById('equity-canvas');
  var hoverIdx=-1;
  canvas.addEventListener('mousemove',function(e){
    var geo=window._chartGeo;if(!geo||!geo.data||geo.data.length<2)return;
    var rect=canvas.getBoundingClientRect();
    var mx=e.clientX-rect.left;
    // Find nearest point
    var bestDist=Infinity,bestI=-1;
    for(var i=0;i<geo.data.length;i++){
      var px=geo.tx(i);
      var d=Math.abs(px-mx);
      if(d<bestDist){bestDist=d;bestI=i;}
    }
    if(bestI!==hoverIdx&&bestDist<30){
      hoverIdx=bestI;
      drawCrosshair(bestI);
      // tradeIdx = point index - 1 (point 0 = initial balance, point 1 = trade 1)
      showTradeInfo(bestI-1);
    }
  });
  canvas.addEventListener('mouseleave',function(){
    hoverIdx=-1;
    var geo=window._chartGeo;
    if(geo&&geo.data)drawEquityCurve(geo.data);
    document.getElementById('chart-info').innerHTML='<div class="ci-empty">Hover over chart<br>to see trade details</div>';
  });
})();

function renderResults(d){
  document.getElementById('empty-state').style.display='none';
  document.getElementById('results-container').style.display='block';

  var s=d.summary;
  document.getElementById('s-trades').textContent=s.trades;
  var wrEl=document.getElementById('s-wr');
  wrEl.textContent=(s.wr*100).toFixed(1)+'%';
  wrEl.className='num '+(s.wr>=0.8?'green':s.wr>=0.6?'yellow':'red');
  var pnlEl=document.getElementById('s-pnl');
  pnlEl.textContent='$'+fmtPnl(s.total_pnl);
  pnlEl.className='num '+cls(s.total_pnl);
  var roiEl=document.getElementById('s-roi');
  roiEl.textContent=s.roi.toFixed(1)+'%';
  roiEl.className='num '+cls(s.roi);
  var evEl=document.getElementById('s-ev');
  evEl.textContent=s.ev_per_dollar.toFixed(4);
  evEl.className='num '+(s.ev_per_dollar>0.3?'green':s.ev_per_dollar>0?'yellow':'red');
  document.getElementById('s-dd').textContent='$'+Math.round(s.max_drawdown);
  document.getElementById('s-dd').className='num '+(s.max_drawdown>500?'red':'yellow');
  document.getElementById('s-wins').textContent=s.wins;
  document.getElementById('s-losses').textContent=s.losses;
  document.getElementById('s-avgentry').textContent=s.avg_entry.toFixed(3);
  document.getElementById('s-avgedge').textContent=s.avg_edge.toFixed(3);

  // Params tags
  var p=d.params;
  var tagsHtml='<span class="param-tag">σ: <span class="pv">'+p.sigma+'</span></span>';
  tagsHtml+='<span class="param-tag">τ: <span class="pv">'+p.tau_mode+'</span></span>';
  tagsHtml+='<span class="param-tag">κ: <span class="pv">'+p.kappa+'</span></span>';
  tagsHtml+='<span class="param-tag">entry: <span class="pv">'+p.entry_min+'min</span></span>';
  tagsHtml+='<span class="param-tag">|Δ|≥: <span class="pv">$'+p.delta_thresh+'</span></span>';
  if(p.p_thresh>0)tagsHtml+='<span class="param-tag">p≥: <span class="pv">'+p.p_thresh+'</span></span>';
  if(p.ofi_filter!=='off')tagsHtml+='<span class="param-tag">OFI: <span class="pv">'+p.ofi_filter+'</span></span>';
  tagsHtml+='<span class="param-tag">side: <span class="pv">'+p.side+'</span></span>';
  document.getElementById('params-tags').innerHTML=tagsHtml;

  // Equity curve
  drawEquityCurve(d.equity_curve);

  // All trades table
  var tbody=document.getElementById('trades-body');
  var html='';
  d.trades.forEach(function(t){
    var rowCls=t.win?'trade-win':'trade-loss';
    var f='font-size:0.78em';
    html+='<tr class="'+rowCls+'">';
    html+='<td>'+t.n+'</td>';
    html+='<td style="'+f+'">'+t.window+'</td>';
    html+='<td style="color:'+(t.side==='NO'?'#4caf50':'#42a5f5')+';font-weight:700">'+t.side+'</td>';
    // Delta at entry
    html+='<td class="'+(t.delta<0?'red':'green')+'" style="font-weight:600">$'+t.delta+'</td>';
    // Poly orderbook at entry
    html+='<td style="'+f+'">'+(t.no_ask!=null?t.no_ask.toFixed(3):'--')+'</td>';
    html+='<td style="'+f+'">'+(t.no_bid!=null?t.no_bid.toFixed(3):'--')+'</td>';
    // Entry price used
    html+='<td style="font-weight:600">'+t.entry.toFixed(3)+'</td>';
    // Shares bought
    html+='<td style="'+f+';color:#78909c">'+Math.round(t.shares)+'</td>';
    // Stake (yellow if capped by liquidity)
    html+='<td style="'+f+';color:'+(t.stake<50?'#ffc107':'#e0e0e0')+'">$'+Math.round(t.stake)+'</td>';
    // L1 available
    html+='<td style="'+f+';color:#78909c">'+Math.round(t.l1_shares)+'</td>';
    // P-model
    html+='<td class="'+(t.p_model>=0.8?'green':t.p_model>=0.6?'yellow':'white')+'">'+(t.p_model*100).toFixed(1)+'%</td>';
    // Edge
    html+='<td class="'+(t.edge>0.2?'green':t.edge>0?'yellow':'red')+'">'+t.edge.toFixed(3)+'</td>';
    // OFI
    html+='<td style="'+f+'" class="'+(t.ofi<0?'red':'green')+'">'+fmtOfi(t.ofi)+'</td>';
    // Result
    html+='<td class="'+(t.win?'green':'red')+'" style="font-weight:700">'+(t.win?'WIN':'LOSS')+'</td>';
    // Full window delta (resolve)
    html+='<td style="'+f+'" class="'+(t.cl_delta_full<0?'red':'green')+'">$'+(t.cl_delta_full||0)+'</td>';
    // CL end price
    html+='<td style="'+f+'">$'+(t.cl_end||0).toLocaleString()+'</td>';
    // NO price at resolve
    html+='<td style="'+f+'">'+(t.no_ask_end!=null?t.no_ask_end.toFixed(2):'--')+'/'+(t.no_bid_end!=null?t.no_bid_end.toFixed(2):'--')+'</td>';
    // PnL
    html+='<td class="'+cls(t.pnl)+'" style="font-weight:700">$'+fmtPnl(t.pnl)+'</td>';
    // Balance
    html+='<td style="font-family:monospace">$'+t.balance.toLocaleString()+'</td>';
    html+='</tr>';
  });
  tbody.innerHTML=html||'<tr><td colspan="19" style="text-align:center;color:#546e7a">No trades matched filters</td></tr>';

  // Winners
  var wbody=document.getElementById('winners-body');
  html='';
  d.trades.filter(function(t){return t.win}).forEach(function(t){
    html+='<tr class="trade-win"><td>'+t.n+'</td><td style="font-size:0.78em">'+t.window+'</td><td>$'+t.delta+'</td><td>'+t.entry.toFixed(3)+'</td><td>'+(t.p_model*100).toFixed(1)+'%</td><td>'+t.edge.toFixed(3)+'</td><td class="green" style="font-weight:700">+$'+Math.round(t.pnl)+'</td></tr>';
  });
  wbody.innerHTML=html||'<tr><td colspan="7" style="text-align:center;color:#546e7a">No winners</td></tr>';

  // Losers
  var lbody=document.getElementById('losers-body');
  html='';
  d.trades.filter(function(t){return !t.win}).forEach(function(t){
    html+='<tr class="trade-loss"><td>'+t.n+'</td><td style="font-size:0.78em">'+t.window+'</td><td>$'+t.delta+'</td><td>'+t.entry.toFixed(3)+'</td><td>'+(t.p_model*100).toFixed(1)+'%</td><td>'+t.edge.toFixed(3)+'</td><td class="red" style="font-weight:700">-$'+Math.round(Math.abs(t.pnl))+'</td></tr>';
  });
  lbody.innerHTML=html||'<tr><td colspan="7" style="text-align:center;color:#4caf50">No losses!</td></tr>';
}

function runBacktest(){
  var params=getParams();
  document.getElementById('loading').style.display='block';
  document.getElementById('empty-state').style.display='none';
  document.getElementById('results-container').style.display='none';

  fetch('/api/backtest',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(params)
  })
  .then(function(r){return r.json()})
  .then(function(d){
    document.getElementById('loading').style.display='none';
    if(d.error){
      alert('Error: '+d.error);
      document.getElementById('empty-state').style.display='block';
      return;
    }
    renderResults(d);
  })
  .catch(function(e){
    document.getElementById('loading').style.display='none';
    document.getElementById('empty-state').style.display='block';
    alert('Error: '+e);
  });
}

// Handle window resize for chart
window.addEventListener('resize',function(){
  var container=document.getElementById('results-container');
  if(container.style.display!=='none' && window._lastEquityData){
    drawEquityCurve(window._lastEquityData);
  }
});

// Override renderResults to cache equity data
var _origRender=renderResults;
renderResults=function(d){
  window._lastEquityData=d.equity_curve;
  window._lastTrades=d.trades;
  _origRender(d);
};

updateFormula();
</script>
</body>
</html>"""
