"""Equity: Kalshi 15m third (backtest, profitable) vs Polymarket third (s_third, decayed).
Plus an honest 'realistic Kalshi' line scaled to the live-f6 cross-check (+$3.6/tr vs idealized +$7.6)."""
import csv, datetime as dt, os
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import kalshi_third_bt as K  # loads markets + Binance, exposes run()

D = os.path.dirname(os.path.abspath(__file__))

# --- Kalshi 15m third backtest ---
kt = K.run(entry_ci=3, max_entry=0.82, pgate=False)
kt.sort(key=lambda x: x["t"])
k_ev = sum(x["pnl"] for x in kt) / len(kt)
REAL = 3.64 / k_ev          # haircut so the 'realistic' line ends at the live-validated EV (+$3.64/tr)
kt_t = [x["t"] for x in kt]
k_cum, k_real, c, cr = [], [], 0.0, 0.0
for x in kt:
    c += x["pnl"]; cr += x["pnl"] * REAL; k_cum.append(c); k_real.append(cr)

# --- Polymarket third (s_third paper) ---
pm = []
_pmcsv = f"{D}/third_equity.csv" if os.path.exists(f"{D}/third_equity.csv") else "C:/tmp/third_equity.csv"
for r in csv.DictReader(open(_pmcsv, encoding="utf-8")):
    if r["session_id"] == "s_third":
        pm.append((dt.datetime.fromisoformat(r["resolved_ts"]), float(r["pnl"])))
pm.sort(key=lambda x: x[0])
pm_t = [t for t, _ in pm]; pm_cum, p = [], 0.0
for _, v in pm: p += v; pm_cum.append(p)

fig, ax = plt.subplots(figsize=(13.5, 6.6))
ax.plot(kt_t, k_cum, color="#2ca02c", lw=2.4,
        label=f"Kalshi 15m third — БЭКТЕСТ (идеализ. филлы): {k_cum[-1]:+,.0f}$  ({len(kt)} tr, EV +{k_ev:.2f}/tr, WR {100*sum(x['won'] for x in kt)/len(kt):.0f}%)")
ax.plot(kt_t, k_real, color="#2ca02c", lw=2.0, ls="--",
        label=f"Kalshi 15m third — РЕАЛИСТ. (по live f6, ~+$3.6/tr): {k_real[-1]:+,.0f}$")
ax.plot(pm_t, pm_cum, color="#999999", lw=2.2,
        label=f"Polymarket third (s_third пейпер): {pm_cum[-1]:+,.0f}$  ({len(pm)} tr) — декей, плоский/в минус с июня")
ax.axhline(0, color="#333", lw=0.8)
ax.set_ylabel("кумулятивный PnL, $  (каждая с нуля)")
ax.set_title("Kalshi 15m third (ЖИВ) vs Polymarket third (МЁРТВ) — equity\n"
             "тот же сигнал, WR ~74% на обоих; разница = ЦЕНА входа (Kalshi дешевле / ещё не запрайсен)",
             fontweight="bold")
ax.legend(fontsize=9, loc="upper left"); ax.grid(alpha=0.25)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
for lab in ax.get_xticklabels(): lab.set_rotation(30)
fig.tight_layout(); p_out = f"{D}/kalshi_vs_third.png"; fig.savefig(p_out, dpi=140, bbox_inches="tight"); plt.close(fig)
print("saved", p_out)
print(f"Kalshi backtest {k_cum[-1]:+.0f}$ | realistic {k_real[-1]:+.0f}$ | PM third {pm_cum[-1]:+.0f}$")
