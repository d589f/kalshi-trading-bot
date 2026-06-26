# Kalshi `third` strategy — self-contained package

Everything for the **third P-model continuation strategy on Kalshi BTC 15-min (KXBTC15M)** —
the one finding from this research that is **alive and profitable** (unlike `third` on Polymarket 5m,
which decayed to negative). Plus the refit model, the venue comparison, and the full paper-engine
spec so the live paper/trading system can be re-implemented in another repo.

## Live identity: this IS `F6 wait270` (`f6_wait270`)
On the PROD Kalshi-15m paper dashboard this strategy runs under the session name **"F6 wait270"** —
the best performer there (live paper: **+$2.93/trade, WR 73.6%, +$1,556 over ~6 days**). It is the
`third` P-model with entry delayed to **270 s (4.5 min)** into the window. **Exact live config:**

| param | value | meaning |
|---|---|---|
| `entry_wait_min` | **4.5** | enter at 270 s into the 15-min window (the "wait270") |
| `delta_threshold` | 20 | fire when \|BTC move from window-open\| ≥ $20 |
| `max_entry_price` | 0.92 | skip entries above 0.92 |
| `sigma_type` | `max30` | σ = max rolling-60s std/mean over 30 min |
| `p_model_threshold` | 0.60 | require model p ≥ 0.60 |
| `kappa` | 0.5 | p = Φ(0.5·snr) |
| `tau_mode` | linear | f_tau = tau_minutes |
| `trade_side` | BOTH | bet the direction of the move |
| `stake` | 100 | $100/trade |

The backtest here (`kalshi_third_bt.py`) reconstructs this signal with entry ≈ minute 4 and
MAX_ENTRY 0.82/0.92 — a close approximation of the exact f6 config. See PAPER_ENGINE_SPEC.md §5–6
for the precise formulas the live `f6_wait270` runs.

## TL;DR finding
- **Same signal, WR ~74% on both venues** — but `third` on **Polymarket 5m is dead** (market priced
  the signal into the entry price; entry-AUC 0.64 > our 0.62), while **Kalshi 15m is alive**
  (cheaper entries, continuation edge not arbed out yet).
- **Kalshi 15m backtest (Apr–Jun 2026): +$7.6/trade idealized, +$3.6/trade realistic** (cross-checked
  vs live f6 paper), WR ~73%, **positive every month**.
- **Liquidity is good**: 1c spread (91% of time), ~$6.5k resting within 5c of the touch, BTC 15m
  volume ~$372k/market (≈12× Polymarket BTC 15m). Scales to low-thousands $/trade.
- **Latency**: Kalshi engine is AWS **us-east-2 (Ohio)**; our EU servers are ~99ms away. A us-east-2
  server = sub-ms. Kalshi has **no taker delay** today (but a CFTC delay proposal is pending).

## Files

### Backtest (Kalshi)
- **`kalshi_third_bt.py`** — backtest engine. Reconstructs the third signal from Binance BTC, enters
  at REAL Kalshi prices (`kalshi_entry.csv`), resolves at Kalshi settlement. Run: `python kalshi_third_bt.py`.
- **`kalshi_vs_third_chart.py`** — equity chart: Kalshi 15m third (alive) vs Polymarket third (dead).
- **`kalshi_vs_third.png`** — that chart.
- `kalshi_markets.csv` — 6,261 settled KXBTC15M markets (Apr 17–Jun 23): ticker, open/close, strike, result.
- `kalshi_entry.csv` — real Kalshi entry prices (yes_ask/yes_bid at minutes 3–5) per market.

### Refit model (deployed live as paper session `s_refit`)
- **`refit_third.py`** — logistic P(win) on raw features `[|delta_from_open|, delta_from_open, tau]`;
  walk-forward OOS; gate = "my P(win) > market entry". Beats the broken p_model (AUC 0.61 vs 0.55).
- **`refit_bake.json`** — baked coefficients (used by the live `s_refit` gate; see PAPER_ENGINE_SPEC §6).
- **`refit_pnl_chart.py`** / `refit_third.png` / `refit_vs_third_pnl.png` — walk-forward equity + AUC.
- `third_refit.csv` — s_third paper trades joined to raw features (sigma/ofi/snr/btc), the refit dataset.
- `third_full.csv` — full s_third paper-trade history (PM 5m).
- `third_equity.csv` — s_third/s_entry150/s_sigfilt resolved PnL (PM, for the comparison chart).

### Spec & engine source (for re-implementation)
- **`PAPER_ENGINE_SPEC.md`** — the DEFINITIVE 1117-line spec of the paper engine: every formula
  verbatim (p-model, sigma variants, OFI, fee, fill, resolution), what's read from the orderbook,
  config schema, strategy variants, constants, and re-implementation gotchas.
- **`dash_src/`** — the actual paper-dashboard source (15 .py files) the spec was extracted from.

### Notes
- `DAILY_2026-06-23.md` — daily summary (why Kalshi edge / Poly dead, the full session).
- `px.py` — Predexon + Binance data client (dependency of the backtest/refit scripts).

## How to reproduce the headline number
```bash
cd kalshi_third
export PREDEXON_API_KEY=...       # your Predexon key (px.py reads it from env; not committed)
python kalshi_third_bt.py        # prints EV/WR/monthly for Kalshi 15m third
python kalshi_vs_third_chart.py  # Kalshi-vs-Poly equity chart
python refit_third.py            # the refit model walk-forward
```
(`cache/binance/` holds the Binance 1m data so no re-download is needed.)

## Honest caveats
- Backtest EV (+$7.6) is idealized fills; **trust the live-validated +$3.6/trade** (the 2.5× gap is
  partly the live entry-price field `last_trade_expensive`/`max-ask`, not liquidity — see spec §6).
- Edge = venue inefficiency → will close as sharper players arrive (like Polymarket did).
- Both the Kalshi signal and the refit model **decay** → retrain weekly, forward-validate, live-FAK
  on small stake before scaling. Minus Kalshi per-contract fees (~$1–2/trade).
- `s_refit` is currently a **paper** A/B on PROD `/root/paper_compare` (vs `s_third`). Not live money.

## Deployment status
- Paper A/B `s_refit` (third+refit) running vs `s_third` on PROD (GCP trading-bot-5sec, `/root/paper_compare`,
  the dashboard viewed via NL tunnel `:8889`). Code patch + baked coeffs in PAPER_ENGINE_SPEC §6.
- To go live on Kalshi: us-east-2 server, US-compliant account, then SDLC `/bootstrap-feature` (see project CLAUDE.md).
