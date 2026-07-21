"""Live XO/Poly/Kalshi paper P&L dashboard. Reads ~/xo_paper/trades.csv fresh on
every request, renders a self-contained page (summary matrix + per-venue cum-P&L
canvas charts + trade log), auto-refreshing. Serves 127.0.0.1:8896 (reverse-tunneled
to Buffalo:8896). Read-only."""
import csv, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CSVP = "/home/dmitrii/xo_paper/trades.csv"
VENUES = [("XO", "XO 5m", "#ec407a"), ("POLY", "Poly 5m", "#42a5f5"), ("KALSHI", "Kalshi 15m", "#66bb6a")]
RULES = [("rev", "reversion", "#66bb6a"), ("mom", "momentum (F1)", "#ffa726"),
         ("fade", "fair-fade", "#29b6f6"), ("flatUP", "flat UP", "#90a4ae")]

def load():
    if not os.path.exists(CSVP):
        return []
    try:
        return list(csv.DictReader(open(CSVP)))
    except Exception:
        return []

def num(x):
    try: return float(x)
    except Exception: return None

def compute():
    rows = load()
    summary, series = {}, {}
    for v, _, _ in VENUES:
        for r, _, _ in RULES:
            summary[(v, r)] = {"cum": 0.0, "trades": 0, "wins": 0}
            series.setdefault(v, {})[r] = []
    for row in rows:
        v, r = row.get("venue"), row.get("rule")
        if (v, r) not in summary:
            continue
        cum = num(row.get("cum"))
        if cum is not None:
            summary[(v, r)]["cum"] = cum
            series[v][r].append(round(cum, 3))
        if row.get("pnl") not in ("", None):     # an actual trade
            summary[(v, r)]["trades"] += 1
            if row.get("won") == "1":
                summary[(v, r)]["wins"] += 1
    recent = rows[-26:][::-1]
    return summary, series, recent, len(rows)

def render():
    summary, series, recent, nrows = compute()
    data = {"series": series,
            "rules": [{"k": k, "label": l, "c": c} for k, l, c in RULES],
            "venues": [{"k": k, "label": l, "c": c} for k, l, c in VENUES]}
    # summary matrix html
    mrows = []
    for r, rlabel, rc in RULES:
        cells = ""
        for v, _, _ in VENUES:
            s = summary[(v, r)]
            cum = s["cum"]; n = s["trades"]; wr = (100 * s["wins"] / n) if n else None
            cls = "pos" if cum > 0.001 else ("neg" if cum < -0.001 else "zero")
            wrs = ("%.0f%%" % wr) if wr is not None else "—"
            cells += ("<td class='%s'><span class='big'>%+.2f</span>"
                      "<span class='sub'>%d tr · WR %s</span></td>" % (cls, cum, n, wrs))
        mrows.append("<tr><td class='rl' style='color:%s'>%s<span class='rsub'>%s</span></td>%s</tr>"
                     % (rc, r, rlabel, cells))
    matrix = "".join(mrows)
    vhead = "".join("<th style='color:%s'>%s</th>" % (c, l) for _, l, c in VENUES)
    # trade log
    logrows = ""
    for row in recent:
        pnl = num(row.get("pnl"))
        pcls = "pos" if (pnl or 0) > 0 else ("neg" if (pnl or 0) < 0 else "")
        t = row.get("t", "")
        try: hhmm = time.strftime("%H:%M", time.gmtime(float(t)))
        except Exception: hhmm = ""
        logrows += ("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "<td>%s</td><td class='%s'>%s</td><td class='dim'>%s</td></tr>" % (
            hhmm, row.get("venue", ""), row.get("rule", ""), row.get("side", "") or "—",
            row.get("entry", "") or "", row.get("outcome", ""),
            pcls, ("%+.2f" % pnl) if pnl is not None else "—", row.get("note", "")))
    upd = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    return (PAGE.replace("{{DATA}}", json.dumps(data)).replace("{{VHEAD}}", vhead)
            .replace("{{MATRIX}}", matrix).replace("{{LOG}}", logrows)
            .replace("{{NROWS}}", str(nrows)).replace("{{UPD}}", upd))

PAGE = """<!DOCTYPE html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<meta http-equiv=refresh content=15>
<title>XO paper — 5-min BTC</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0a0e17;color:#dfe6ee;padding:14px;font-size:14px}
h1{color:#4fc3f7;font-size:1.15em;margin-bottom:2px}
.sub0{color:#7d8aa0;font-size:.82em;margin-bottom:14px}
.sub0 b{color:#aeb9cc}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:#111827;border:1px solid #1e2a3a;border-radius:10px;padding:12px}
.card h2{font-size:.9em;margin-bottom:8px;font-weight:600}
canvas{width:100%;height:150px;display:block}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.matrix td,.matrix th{padding:9px 10px;text-align:right;border-bottom:1px solid #1a2437}
.matrix th{color:#7d8aa0;font-weight:600;font-size:.85em;text-align:right}
.matrix th:first-child,.matrix td.rl{text-align:left}
td.rl{font-weight:700;font-size:.95em}
td.rl .rsub{display:block;color:#6b7688;font-weight:400;font-size:.75em}
.matrix .big{display:block;font-size:1.15em;font-weight:700;font-variant-numeric:tabular-nums}
.matrix .sub{display:block;color:#6b7688;font-size:.72em}
.pos{color:#3fdc7f}.pos .big{color:#3fdc7f}.neg{color:#ff6b6b}.neg .big{color:#ff6b6b}.zero .big{color:#8b98ac}
.log{background:#111827;border:1px solid #1e2a3a;border-radius:10px;padding:12px;overflow-x:auto}
.log h2{font-size:.9em;margin-bottom:8px}
.log table{font-size:.85em}
.log td,.log th{padding:4px 8px;text-align:left;white-space:nowrap;border-bottom:1px solid #161f30}
.log th{color:#7d8aa0}
.dim{color:#6b7688}
.legend{font-size:.78em;color:#7d8aa0;margin-top:6px}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 10px;vertical-align:middle}
</style></head><body>
<h1>XO vs Polymarket — 5-min BTC paper simulator</h1>
<div class=sub0>same rule menu on each venue · settle = Binance spot · fees: XO 0%, Poly 0%, Kalshi 0.07·p(1−p) · size ≈ $5/trade · {{NROWS}} rows · updated {{UPD}} · auto-refresh 15s</div>

<table class=matrix style="background:#111827;border:1px solid #1e2a3a;border-radius:10px;margin-bottom:14px">
<tr><th>strategy</th>{{VHEAD}}</tr>{{MATRIX}}</table>

<div class=grid id=charts></div>

<div class=log><h2>recent windows</h2><table>
<tr><th>UTC</th><th>venue</th><th>rule</th><th>side</th><th>entry</th><th>outcome</th><th>P&L</th><th>note</th></tr>
{{LOG}}</table></div>

<script>
const D={{DATA}};
function draw(cv,rules,ser){
 const dpr=window.devicePixelRatio||1,w=cv.clientWidth,h=cv.clientHeight;
 cv.width=w*dpr;cv.height=h*dpr;const x=cv.getContext('2d');x.scale(dpr,dpr);
 let all=[];rules.forEach(r=>{(ser[r.k]||[]).forEach(v=>all.push(v))});
 if(!all.length){x.fillStyle='#4a5568';x.font='12px sans-serif';x.fillText('no trades yet',10,h/2);return;}
 let mn=Math.min(0,...all),mx=Math.max(0,...all);if(mx-mn<1){mx+=1;mn-=1;}
 const pad=6,gw=w-pad*2,gh=h-pad*2;
 const X=i=>pad+(maxlen<2?gw/2:gw*i/(maxlen-1)),Y=v=>pad+gh*(1-(v-mn)/(mx-mn));
 let maxlen=1;rules.forEach(r=>maxlen=Math.max(maxlen,(ser[r.k]||[]).length));
 // grid + zero line
 x.strokeStyle='#1a2437';x.lineWidth=1;x.beginPath();for(let g=0;g<=4;g++){const yy=pad+gh*g/4;x.moveTo(pad,yy);x.lineTo(w-pad,yy);}x.stroke();
 x.strokeStyle='#33415a';x.setLineDash([3,3]);x.beginPath();x.moveTo(pad,Y(0));x.lineTo(w-pad,Y(0));x.stroke();x.setLineDash([]);
 rules.forEach(r=>{const s=ser[r.k]||[];if(s.length<1)return;
  x.strokeStyle=r.c;x.lineWidth=1.8;x.beginPath();
  s.forEach((v,i)=>{const px=X(i),py=Y(v);i?x.lineTo(px,py):x.moveTo(px,py);});x.stroke();
  const li=s.length-1;x.fillStyle=r.c;x.beginPath();x.arc(X(li),Y(s[li]),2.6,0,7);x.fill();});
}
const cont=document.getElementById('charts');
D.venues.forEach(v=>{
 const card=document.createElement('div');card.className='card';
 const leg=D.rules.map(r=>`<span class=dot style="background:${r.c}"></span>${r.k}`).join('');
 card.innerHTML=`<h2 style="color:${v.c}">${v.label}</h2><canvas></canvas><div class=legend>${leg}</div>`;
 cont.appendChild(card);
 draw(card.querySelector('canvas'),D.rules,(D.series[v.k]||{}));
});
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        try:
            body = render().encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as e:
            b = ("error: %s" % e).encode(); self.send_response(500); self.end_headers(); self.wfile.write(b)

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8896), H).serve_forever()
