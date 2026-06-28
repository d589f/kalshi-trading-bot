# Kalshi Trading Bot — Product Requirements

This document captures feature-level requirements for the Rust Kalshi trading bot
(`kalshi_rs`) before any implementation begins. Each feature is a numbered section
with a fixed structure: description, user story, functional requirements,
non-functional requirements, acceptance criteria, affected endpoints, schema
changes, and UI changes. Sections cross-reference each other by number.

- **Version:** 0.1
- **Last updated:** 2026-06-28
- **Owner:** trading-bot maintainers

---

## 1. Live-Execution Telemetry + Window-Latch Fix

Internal id: `live-exec-telemetry-latch-fix`. Delivered as two parts under one
feature: **P0 — live trade telemetry** (zero order-behavior change) and **P0b —
fix latch-before-await** (behavior change; shadow/log-only validation required
before prod).

### 1.1 Feature description

The live trader `place_live` in `kalshi_rs/src/main.rs` (≈ lines 878-1044)
systematically overpays versus the paper engine on entry price, and the loss is
currently **unmeasurable** because the live ledger persists too few fields to
decompose the gap and writes **nothing** on no-fills or early skips.

Measured on prod **2026-06-28** with real money:

- Real all-time live PnL = **−$11.42**.
- Entry-price drag versus paper = **+$12.23 over 163 trades** — the slippage is
  approximately the entire loss.
- Mean entry gap **+1.12c**, median **0c**, but a momentum right-tail dominates:
  **13% of trades pay ≥6c**, max **+24c**.
- No-fill rate **~9%**, and **each no-fill permanently burns the window** because
  the once-per-window latch (`fired_window` in `signal_loop`, `kalshi_rs/src/main.rs`
  ≈ line 552) is set **before** the order await (≈ line 754, immediately before the
  `place_live(...).await` at ≈ lines 755-771).

This feature makes the gap measurable and stops the window-burn, **without
changing pricing or sizing strategy**.

**P0 — Live trade telemetry (zero order-behavior change).** Replace the ad-hoc
`serde_json::json!` live record (currently `kalshi_rs/src/main.rs` ≈ lines
1008-1014) with a typed `LiveTriggerRecord` analogous to the existing typed
`TriggerRecord` in `kalshi_rs/src/ledger.rs` (lines 47-82). Persist enough per-fire
context to decompose the entry-price gap offline, and write exactly one ledger row
on **every** outcome — including no-fills (today `place_live` returns silently at
`kalshi_rs/src/main.rs` ≈ line 992) and every early skip (daily-cap ≈ line 907,
loss-stop ≈ line 911, band ≈ line 917, order-error ≈ line 955, rejected ≈ line 984).

**P0b — Fix latch-before-await (behavior change).** The window latch
`fired_window` must be consumed only on a **CONFIRMED FILL**, preserving the
1-filled-position-per-window economics, while a no-fill is allowed to re-attempt
within the same window under a **bounded** policy (max attempts per window +
cooldown between attempts) so the trader does not place an order on every 0.3s
tick. `place_live` returns an outcome that `signal_loop` uses to decide whether to
latch.

### 1.2 User story

As the **operator of the live Kalshi trader**, I want every live fire — fills,
no-fills, and skips — recorded with full entry-price decomposition fields, and I
want a no-fill to no longer permanently waste its trading window, so that I can
measure exactly where the +$12.23 entry drag comes from and recover the ~9% of
windows currently lost to no-fills, **without** altering the pricing or sizing
strategy that keeps live 1:1 with paper.

### 1.3 Functional requirements

**P0 — Telemetry**

1. Define a typed `LiveTriggerRecord` (serializable, in `kalshi_rs/src/ledger.rs`)
   analogous to the existing `TriggerRecord` (ledger.rs lines 47-82). `place_live`
   constructs and appends it via the existing `Ledger::append` (ledger.rs lines
   24-41); the ad-hoc `serde_json::json!` block at `main.rs` ≈ lines 1008-1014 is
   removed.
2. `LiveTriggerRecord` MUST carry these fields per fire:
   - Entry decomposition: `entry` (signal/paper entry, from `fire.entry`),
     `exec_entry` (fresh order-time ask computed at `main.rs` ≈ lines 927-937),
     `orderbook_entry` (from `fire.orderbook_entry`).
   - Order pricing: `first_limit_price` (the first limit price sent, `price` at
     `main.rs` ≈ lines 938-941), `requote_limit_price` (`Option`, the deeper
     re-quote price at ≈ lines 962-965 when one is sent), and `requote` (bool —
     true iff a re-quote IOC was issued).
   - Fill result: `remaining_count` (from `resp.remaining_count`,
     `kalshi_rs/src/kalshi/orders.rs` line 38), `fill` (from `resp.fill()`,
     orders.rs line 46), `eff` (effective per-contract entry computed at `main.rs`
     ≈ lines 995-999), `fee` (from `resp.fee()`, orders.rs line 52),
     `latency_ms` (the order RTT `lat`).
   - Signal/window context: `count`, `side`, `p` (`fire.p`), `delta_from_open`
     (from `shared.delta_from_open`), `window_start`, `window_end`,
     `market_ticker` (`book.ticker`), `order_id` (from `resp.order_id`,
     orders.rs line 34), `ts` (`now`), `ts_iso` (`now_utc.to_rfc3339()`).
3. Add an `outcome` enum field on `LiveTriggerRecord` with exactly these variants,
   serialized as stable lowercase string tags: `filled`, `partial`, `nofill`,
   `skip_daily_cap`, `skip_loss_stop`, `skip_band`, `order_error`, `rejected`.
   - `filled`: `fill > 0` and `remaining_count == 0`.
   - `partial`: `fill > 0` and `remaining_count > 0`.
   - `nofill`: order accepted (HTTP 201) but `fill <= 0` after any re-quote (`main.rs`
     ≈ lines 987-992).
   - `skip_daily_cap`: `trades_today >= max_trades_day` (≈ line 905).
   - `skip_loss_stop`: `day_pnl <= -daily_loss_stop` (≈ line 909).
   - `skip_band`: entry outside `(0.50, max_entry_price]` (≈ line 915).
   - `order_error`: `create_ioc` returned `Err` (≈ lines 953-956).
   - `rejected`: HTTP status `!= 201` (≈ lines 982-985).
4. `place_live` MUST append **exactly one** `LiveTriggerRecord` on **every** code
   path that today returns — including the four early skips, the order-error path,
   the rejected path, and the no-fill path that currently return silently. For skip
   and error outcomes, fill-result and order-pricing fields that do not apply are
   recorded as `null`/absent (additive JSON, see FR-5); known context fields
   (`entry`, `orderbook_entry`, `side`, `p`, `delta_from_open`, window bounds,
   `market_ticker`, `ts`, `ts_iso`) are still populated when available at that path.
5. **Backward compatibility — additive JSON.** New fields are additive: existing
   records (which lack the new fields) and new records MUST both parse in the
   downstream resolver and dashboard loaders. New optional fields deserialize as
   `None`/default when absent; the existing `kind` discriminator and the fields
   already consumed by the resolver/dashboard are unchanged in name and meaning.

**P0b — Latch fix**

6. `place_live` MUST return a typed outcome value (an enum matching the FR-3
   `outcome` set, or a compatible "latch / do-not-latch" signal derived from it)
   that `signal_loop` uses to decide latching. The current call site at `main.rs`
   ≈ lines 753-771 sets `fired_window = win_key.clone()` **before** the await; this
   pre-await assignment MUST be removed.
7. The latch `fired_window` is consumed (set to the current `win_key`) **only on a
   confirmed fill** (`outcome == filled` or `outcome == partial`). At most **one
   filled position per window** is guaranteed — once latched, no further live order
   is placed for that window key (the latch clears automatically when `win_key`
   changes, per the existing comment at `main.rs` ≈ lines 680-682).
8. On a `nofill` outcome, the window is **not** latched; the trader may retry within
   the same window under a bounded policy:
   - **Max attempts per window**: at most `N` order-placement attempts per window
     key (`N` configurable via env; default to be set by architect/planner).
   - **Cooldown between attempts**: a minimum interval `C` (configurable via env;
     default to be set by architect/planner) between successive attempts in the
     same window, so the trader does not place an order on every 0.3s tick.
   - Retrying stops at the **first** fill; thereafter the window is latched per FR-7.
   - Per-window attempt count and last-attempt timestamp are tracked in
     `signal_loop` local state (sibling to `fired_window`) and reset on window roll.
9. Skip and error outcomes (`skip_daily_cap`, `skip_loss_stop`, `skip_band`,
   `order_error`, `rejected`) do **not** latch the window and do **not** consume a
   bounded retry attempt where the skip is a pre-order gate (daily-cap, loss-stop,
   band); an `order_error` or `rejected` after an order was attempted DOES count
   toward the per-window attempt budget `N`.
10. **No pricing/sizing/strategy change.** `PRICE_BUF` / `REQUOTE_BUF`
    (`main.rs` ≈ lines 124-127, 215-216) are unchanged; the computation of
    `exec_entry` (≈ lines 927-937), the first `price` and re-quote `price`
    (≈ lines 938-941, 962-965), and `count` (≈ lines 942-948) are unchanged; the
    MIRROR 1:1-with-paper signal path (`main.rs` ≈ lines 599-646) is unchanged.

### 1.4 Non-functional requirements

1. **Hot-path latency.** No added latency on the 0.3s decision loop hot path beyond
   what is inherent to the existing order flow. Telemetry serialization reuses the
   existing `Ledger::append` path (a `Mutex`-guarded append, ledger.rs lines 24-41);
   no new blocking network I/O is introduced on the decision path.
2. **Append-only ledger.** Ledger writes remain append-only JSONL — one JSON object
   per line, created/appended via `OpenOptions::new().create(true).append(true)`
   (ledger.rs lines 33-40). No record is rewritten or deleted.
3. **Configuration via env.** New bounded-retry knobs (`N` max attempts/window, `C`
   cooldown) are read from environment variables consistent with the existing
   `LiveCfg` env pattern (`main.rs` ≈ lines 210-216, e.g. `env_i64` / `env_f64` with
   defaults), so they default safely and can be tuned without recompiling.
4. **No strategy regression.** With the bounded-retry knobs set to their disabling
   values (e.g. `N = 1`), live order behavior is byte-for-byte equivalent to today's
   single-attempt-per-window behavior, aside from the additional telemetry rows.

### 1.5 Acceptance criteria

1. Every live fire appends **exactly one** ledger row whose `outcome` matches what
   happened; no-fills and all four skip paths plus order-error and rejected are no
   longer silent (each writes a row).
2. A `filled` (or `partial`) record contains all decomposition fields — `entry`
   (= `fire.entry`), `exec_entry`, `orderbook_entry`, `first_limit_price`,
   `requote_limit_price`, `requote`, `remaining_count`, `fill`, `eff`, `fee`,
   `latency_ms` — such that, offline:
   - `gap = eff − entry` is computable, and
   - its split is computable: `drift = exec_entry − entry` and
     `walk = eff − exec_entry` (so `gap = drift + walk`).
3. Old ledger records (which lack the new fields) still parse in the resolver and
   dashboard loaders; new records also parse. A loader replay over a mixed file of
   pre- and post-feature records produces no parse errors.
4. A no-fill no longer permanently burns the window: within one window key, after a
   `nofill` outcome the trader may retry up to `N` attempts (`N` configurable, env,
   default set by architect/planner) with cooldown `C` between attempts, and stops
   at the first fill. At most **one** filled position per window is guaranteed (a
   second fill for the same window key is impossible because the latch is consumed
   on first fill).
5. Order pricing/sizing logic and the MIRROR 1:1-with-paper signal path are
   unchanged by this feature: `PRICE_BUF` / `REQUOTE_BUF` and the formulas for
   `exec_entry`, `first_limit_price`, `requote_limit_price`, and `count` are
   identical to pre-feature behavior (verified by diffing the relevant `main.rs`
   ranges 927-948 and 962-965).
6. **P0b validated shadow/log-only before prod.** The latch-fix behavior change is
   exercised in a shadow/log-only mode (no real orders, or against a sandbox)
   demonstrating: (a) a simulated no-fill triggers a bounded retry within the same
   window, (b) the retry respects `N` and `C`, and (c) the window latches on the
   first simulated fill — before the change is enabled in prod.

### 1.6 Affected endpoints

This is an internal Rust binary, not an HTTP service. No public API routes are
created or modified. Relevant **outbound** call sites (unchanged in contract, only
in invocation count/latching):

- `OrderClient::create_ioc(...)` — `kalshi_rs/src/main.rs` ≈ lines 950-951, 973-974
  (the live IOC order placement). Under P0b it may be invoked more than once per
  window across bounded retries, but its arguments and pricing are unchanged.
- `KalshiRest::get_orderbook(...)` — `main.rs` ≈ line 927 (fresh order-time book for
  `exec_entry`); unchanged.

### 1.7 Schema changes

No SQL database. The "schema" is the append-only JSONL ledger record shape
(`kalshi_rs/src/ledger.rs`) plus persisted state files.

- **New type `LiveTriggerRecord`** in `kalshi_rs/src/ledger.rs`, with the fields in
  FR-2 and the `outcome` enum in FR-3. Fields are additive relative to the prior
  ad-hoc live record (`main.rs` ≈ lines 1008-1014); existing field names
  (`kind`, `live`, `ts`, `ts_iso`, `session`, `window_start`, `window_end`,
  `market_ticker`, `side`, `entry`, `count`, `delta_from_open`, `p`, `order_id`,
  `fee`, `latency_ms`) are preserved so the resolver/dashboard keep parsing.
- **No change** to `TriggerRecord` (ledger.rs lines 47-82) or `ResolveRecord`
  (lines 85-98).
- New env-config knobs for bounded retry (`N`, `C`) read in `main.rs` alongside the
  existing `LiveCfg` construction (≈ lines 210-216); whether they live on `LiveCfg`
  or as `signal_loop` locals is a design decision for the architect/planner.

### 1.8 UI changes

The dashboard is the read-only operator view fed by `kalshi_rs/src/dashboard.rs`
(`TrigSummary`, lines 59-67). No new dashboard pages or components are required by
this feature.

- The dashboard loaders MUST continue to parse the ledger after the record-shape
  change (additive JSON; see FR-5, AC-3). No `TrigSummary` field is renamed or
  removed.
- Surfacing the new `outcome`/decomposition fields in the dashboard UI is **not**
  required by this feature and is deferred.

### 1.9 Out of scope

The following are explicitly **not** part of this feature:

- Lowering `PRICE_BUF` / `REQUOTE_BUF` (a separate prod-config change).
- A Kalshi WebSocket orderbook feed.
- us-east co-location.
- A modeled paper twin.
