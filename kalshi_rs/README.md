# kalshi_bot — KXBTC15M `third` / f6_wait270 live shadow runner (Rust)

A faithful Rust port of the `dash_src` paper engine (see `../kalshi_third/PAPER_ENGINE_SPEC.md`),
specialized to the one alive/profitable finding: the **`third` P-model continuation** strategy on
**Kalshi KXBTC15M** (BTC 15-minute up/down), live config **`f6_wait270`**.

This first stage is a **SHADOW runner**: it runs the exact signal against live feeds and, on each
trigger, writes the order it *would* have placed (an "empty"/dry-run order) to a JSONL ledger —
**nothing is sent to Kalshi**. A resolver then scores each would-be order on Kalshi's real
settlement. Purpose: measure how closely the live pipeline matches the paper engine before risking
money.

## Status: working
Verified live on the server — full f6 filter chain executes on real data:
`WAIT (e/4.5) → DELTA LOW → AMBIGUOUS → P-LOW → BUY YES/NO`, Kalshi book derived correctly,
settlement-based PnL recorded. No API credentials required (Kalshi market data is public).

## Strategy (f6_wait270)
Enter at 270 s (4.5 min) into the 15-min window; fire if `|Δ BTC from window-open| ≥ $20`,
`σ=max30`, `p = Φ(0.5·snr) ≥ 0.60`, entry `≤ 0.92`; bet the direction of the move (BOTH sides),
$100/trade, 1 trade/window. All formulas are verbatim from the spec (`src/signal.rs`,
`src/engine.rs`).

## Architecture
```
binance.rs   Binance.US @aggTrade  ──► AppState{price, trade_buffer}
kalshi/      REST poller (1s): GetMarkets(KXBTC15M) + orderbook ──► AppState{kalshi book}
signal.rs    sigma family (max30), p_model, OFI, phi  — spec §5 verbatim
window.rs    15-min window model, tau/elapsed, deltas — spec §4
engine.rs    classic pmodel filter chain               — spec §6.2
main.rs      0.3s signal loop → on trigger → ledger.jsonl ("empty order") + resolver(PnL)
kalshi/auth  RSA-PSS request signing (ready for WS feed / real orders — not used in shadow)
```

### Feed note (important)
Binance.com returns **HTTP 451** on US servers (geo-block) — and a US-compliant Kalshi host
(incl. AWS us-east-2) hits the same wall. So the live feed defaults to **Binance.US** (same
`btcusdt@aggTrade` schema). The signal is delta-based, so the .us↔.com price basis is irrelevant.
Override with `BINANCE_WS_URL`. The paper engine used Binance.com — so the .us↔.com feed
difference is itself one of the paper-vs-live discrepancies this runner measures.

### Settlement
KXBTC15M settles on **CF Benchmarks BRTI** (60-s average before close vs before open), NOT Binance —
hence a ~$66 Binance-spot vs BRTI basis. We compute the signal on Binance.US (fast/free) and let
Kalshi settle on BRTI; direction correlates, so the edge survives. The resolver reads Kalshi's real
`result` to score PnL with the **Kalshi fee** `ceil(0.07·C·P·(1−P))`.

## Build & run
```bash
cargo build --release
RUST_LOG=info ./target/release/kalshi_bot          # writes shadow_ledger.jsonl
# env: KALSHI_BASE (default prod), BINANCE_WS_URL (default Binance.US)
```

## Ledger
`shadow_ledger.jsonl` — one JSON per line. `kind:"trigger"` = a would-be order (ticker, side,
count, limit_price_cents, stake) + full signal context (Δ, σ, snr, p, τ, book snapshot) for diffing
vs paper. `kind:"resolve"` = settlement outcome + PnL on Kalshi's real result.

## Deploy (systemd)
`deploy/kalshi-shadow.service` → `/etc/systemd/system/`, then `systemctl enable --now kalshi-shadow`.
Logs: `journalctl -u kalshi-shadow -f`. Ledger: `/root/kalshi_rs/shadow_ledger.jsonl`.

## Not yet (next stages)
- Kalshi **WS** orderbook_delta feed (lower latency than 1s REST poll) — needs API key (RSA).
- Real order placement (FAK limit) — needs API key; start on DEMO env.
- Warmup: `max30` σ needs ~30 min of price history to be meaningful (matches paper warmup).
- True window-open backfill from a kline (currently lazy-seeded like the paper engine).
