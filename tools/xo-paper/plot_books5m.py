"""Plot the 3-venue 5-min orderbook recording -> PNG.
Panel 1: BTC price (context) with per-venue window rollovers marked.
Panel 2: P(UP) mid per venue + shaded [bid,ask] spread band (the 'stakan' — level & width).
Panel 3: top-of-book depth $ per venue (log) — XO thin vs Poly vs Kalshi deep.
  python plot_books5m.py books5m.csv out.png"""
import sys, csv, math
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SRC = sys.argv[1] if len(sys.argv) > 1 else "books5m.csv"
OUT = sys.argv[2] if len(sys.argv) > 2 else "books5m.png"

def f(x):
    try:
        return float(x)
    except Exception:
        return math.nan

rows = list(csv.DictReader(open(SRC)))
if not rows:
    print("empty csv"); sys.exit(1)
t0 = f(rows[0]["t"])
tm = [(f(r["t"]) - t0) / 60.0 for r in rows]   # elapsed minutes

def mid(r, b, a):
    bv, av = f(r[b]), f(r[a])
    return (bv + av) / 2 if bv == bv and av == av else math.nan

VEN = [
    ("XO 5m",      "xo_bid", "xo_ask", "xo_dep", "xo_slug", "#d6336c"),
    ("Poly 5m",    "po_bid", "po_ask", "po_dep", "po_slug", "#1c7ed6"),
    ("Kalshi 15m", "k_bid",  "k_ask",  "k_dep",  "k_tk",    "#2f9e44"),
]

fig, ax = plt.subplots(3, 1, figsize=(15, 11), sharex=True,
                       gridspec_kw={"height_ratios": [1.1, 2.2, 1.3]})

# ---- panel 1: BTC ----
btc = [f(r["btc"]) for r in rows]
ax[0].plot(tm, btc, color="#f08c00", lw=1.3)
ax[0].set_ylabel("BTC price $")
ax[0].set_title("5-min BTC up/down orderbook — XO (5m pulse) vs Polymarket (5m) vs Kalshi (15m, no 5m exists)  ·  live WS recording, 1s",
                fontsize=12, weight="bold")
ax[0].grid(alpha=0.25)

# window rollover markers (per venue slug change)
for name, _b, _a, _d, slugcol, col in VEN:
    prev = None
    for i, r in enumerate(rows):
        s = r.get(slugcol, "")
        if s and s != prev:
            for a in ax:
                a.axvline(tm[i], color=col, alpha=0.12, lw=1)
            prev = s

# ---- panel 2: P(UP) mid + spread band ----
for name, b, a, d, slug, col in VEN:
    m = [mid(r, b, a) for r in rows]
    bid = [f(r[b]) for r in rows]
    ask = [f(r[a]) for r in rows]
    ax[1].plot(tm, m, color=col, lw=1.6, label=name)
    ax[1].fill_between(tm, bid, ask, color=col, alpha=0.18, linewidth=0)
ax[1].axhline(0.5, color="k", alpha=0.3, ls="--", lw=0.8)
ax[1].set_ylim(0, 1)
ax[1].set_ylabel("P(UP)  =  mid, band = bid–ask spread")
ax[1].legend(loc="upper right", ncol=3)
ax[1].grid(alpha=0.25)

# ---- panel 3: depth ----
for name, b, a, d, slug, col in VEN:
    dep = [f(r[d]) if f(r[d]) > 0 else math.nan for r in rows]
    ax[2].plot(tm, dep, color=col, lw=1.4, label=name)
ax[2].set_yscale("log")
ax[2].set_ylabel("top-book depth $ (log)")
ax[2].set_xlabel("elapsed minutes")
ax[2].legend(loc="upper right", ncol=3)
ax[2].grid(alpha=0.25, which="both")

# coverage annotation
def cov(col):
    return sum(1 for r in rows if r.get(col, "") not in ("", "nan"))
note = "rows=%d  |  non-empty: XO %d, Poly %d, Kalshi %d" % (
    len(rows), cov("xo_bid"), cov("po_bid"), cov("k_bid"))
fig.text(0.01, 0.005, note, fontsize=9, color="#555")

plt.tight_layout(rect=[0, 0.02, 1, 1])
plt.savefig(OUT, dpi=110)
print("wrote", OUT, "|", note)
