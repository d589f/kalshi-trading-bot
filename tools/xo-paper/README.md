# xo-paper — 3-venue 5-min paper simulator

Runs an **identical rule set** against XO (5-min Pulse), Polymarket (5-min up/down) and
Kalshi (15-min KXBTC15M) simultaneously, so the *venues* are what gets compared, not the
strategies. Read-only against the live trading stack — it never places orders and never
touches the production bot.

## Rules

| rule | decision point | signal | entry |
|---|---|---|---|
| `rev1` / `rev5` | window open | trailing 1-min / 5-min BTC move before the window, ≥0.1% → bet **against** it. Two lookbacks because the 90d backtest favoured 1-min while the first live cut used 5-min | open ask (~0.50) |
| `fade` | 60 s in | book mid deviates ≥0.06 from `Φ(displacement / σ√time_left)` → buy the under-priced side | decision ask |
| `mom` | 60 s in (Kalshi 180 s) | \|displacement\| ≥ $50 → bet **with** the move (the live F1 analog) | decision ask |
| `flatUP` | window open | none — always buy UP (drift control) | open ask |

Sizing `n = round(5 / price)` contracts (~$5). Fees: XO 0%, Polymarket 0%, Kalshi `0.07·p·(1−p)`.

## Data path

- **Books:** live WebSocket — XO `wss://orderbooks.xo.market/ws/market` (full `book` snapshots),
  Polymarket CLOB WS (`book` + `price_change` deltas, reconstructed client-side).
  REST fallback only when the cache goes >5 s stale. Kalshi uses signed REST @2 s.
- **Settlement:** each venue's own resolution API — XO `/api/markets/<id>` → `winningOutcomeId`,
  Polymarket Gamma `outcomePrices`, Kalshi `result`. Binance spot is used for the *signal*
  (displacement / fair value) and only as a fallback after 45 min unresolved.
- **Output:** `$OUTDIR/trades.csv` (~140 KB/day) + `heartbeat`.

## Venue gotchas encoded here

- **XO `outcomes[].name` is `null`** — UP/DOWN must be resolved positionally (`outcomes[0]` = UP).
- **XO resolved markets are not listable** (`statuses=RESOLVED` returns 0); poll `/api/markets/<id>`.
- **Polymarket 5-min windows are deployed ~24 h early.** `startDate` is the deploy time; the real
  window is `eventStartTime` → `endDate`. The live window is
  `slug = btc-updown-5m-<floor(now/300)*300>` via Gamma. `clobTokenIds[0]` = UP.
- **Kalshi has no book at window open**, so open-entry rules (`rev`, `flatUP`) cannot fill there.

## Env

```
PREDEXON_API_KEY=...        # Polymarket market discovery (optional; Gamma is used for 5m)
KALSHI_ACCESS_KEY_ID=...    # Kalshi REST signing
KALSHI_PEM=/path/key.pem    # Kalshi private key
```

## Deployment (systemd, reboot-persistent)

```
/etc/systemd/system/xo-paper.service          # the simulator      (Restart=always)
/etc/systemd/system/xo-dash.service           # dashboard on 127.0.0.1:8896
/etc/systemd/system/xo-dash-tunnel.service    # ssh -R 8896 -> public box (User=dmitrii)
```

Watchdog (`xo_paper_watchdog.sh`, root cron every minute) restarts the unit when it is
inactive **or** when the heartbeat is >120 s stale — `Restart=always` alone only catches
crashes, not hangs.

## Files

- `xo_paper.py` — the simulator (WS books, venue settlement, rule engine, CSV ledger)
- `xo_dash.py` — self-contained live dashboard (P&L matrix, per-venue curves, trade log)
- `xo_paper_watchdog.sh` — heartbeat watchdog
- `simple_poll.py` — dead-simple REST 3-venue book poller (used to record raw books)
- `plot_books5m.py` — renders a recorded book session to PNG
