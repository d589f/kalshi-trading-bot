# Kalshi Trading Bot — Product Requirements

This document captures feature-level requirements for the Rust Kalshi trading bot
(`kalshi_rs`) before any implementation begins. Each feature is a numbered section
with a fixed structure: description, user story, functional requirements,
non-functional requirements, acceptance criteria, affected endpoints, schema
changes, and UI changes. Sections cross-reference each other by number.

- **Version:** 0.2
- **Last updated:** 2026-07-05
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

---

## 2. Live F1 Strategy Switch + Entry-Fidelity Audit

Internal id: `live-f1-strategy`. Switch the Rust live trader (`kalshi_rs`) from
strategy **`f6_wait270`** to **`f1_d50cap75`** and re-enter **LIVE** trading at a
**$5 stake on Kalshi subaccount #1**, gated behind an **entry-fidelity audit** that
proves the Rust gate reproduces the paper F1 signal 1:1 before real money is at
risk. Cross-references Section 1 (`live-exec-telemetry-latch-fix`): this feature
reuses, unchanged, the P0 telemetry (`LiveTriggerRecord`) and P0b latch-on-fill +
bounded-retry rails that Section 1 delivered.

### 2.1 Feature description

The bot is a Kalshi **KXBTC15M** (BTC 15-minute binary) trader running in **MIRROR
mode**: it reads the Python paper engine's live state
(`http://127.0.0.1:8893` on the EU box, `kalshi_rs/src/mirror.rs`) and re-evaluates
the strategy gate (`kalshi_rs/src/engine.rs::evaluate`) so the live signal equals
the paper signal 1:1 — only the real execution (fill price, fees, no-fills)
differs. Live trading was **stopped 2026-06-30 at total real PnL −$131.30** because
`f6_wait270`'s edge sits at post-fee breakeven (WR ≈ 73.5% vs the ≈ 73.5% needed).

`f1_d50cap75` is a **sibling paper strategy** whose config lives **only in the paper
engine DB** (not in this repo). Its parameters:

| param | value |
|-------|-------|
| `kappa` | 0.4 |
| `delta_threshold` | $50 |
| `p_model_threshold` | 0.65 |
| `sigma_type` | `max10` |
| `entry_wait` | 3.0 min (180 s into the 15-min window) |
| `max_entry_price` | 0.92 (the `cap75` name is legacy — the real cap is 0.92) |
| `trade_side` | BOTH |
| execution | taker |
| `liq_filter` | off |
| `threshold_gap` | 0.1 |

Honest **$5 + real-Kalshi-fee** recompute over full history (16.06–05.07, 1498
trades): **F1 +$154.60** (EV +$0.103/trade, WR 77.8% vs breakeven ≈ 77.0% → ≈ +0.8pt
cushion) vs **f6 +$56.36** (≈ 0pt cushion). During the old live window F1 would still
have lost (−$30.14, less than f6). Post-stop (30.06–05.07) F1 = **+$85.60 @ 80.2%
WR**. The edge is **thin and regime-dependent, and F1 has NEVER traded live**: entry
at 180 s rides stronger momentum than f6's 270 s, so F1's live no-fill / slippage
profile is unknown. This risk framing is binding — see Section 2.10.

Two correctness hazards make this more than a config swap:

1. **MIRROR reads a hardcoded session key.** `kalshi_rs/src/mirror.rs` (≈ line 74)
   reads `sessions_state["f6_wait270"]["live_sigma"]`. Under F1 it must read
   `sessions_state["f1_d50cap75"]["live_sigma"]` (verified present on the endpoint).
2. **The mirrored sigma is inserted under a hardcoded map key.**
   `kalshi_rs/src/main.rs` (≈ line 868) does `sigmas.insert("max30", m.sigma_max30)`
   while the gate looks up `s.sigmas.get(&cfg.sigma_type)` (`engine.rs` ≈ lines
   123 & 135). With F1's `sigma_type = "max10"` the lookup **misses** and silently
   falls back to `s.sigma` (the `realized5_pmin` sigma, `main.rs` ≈ line 918) — the
   bot would then trade a formula that is **NOT F1**. This is a silent-wrong-trade
   defect and MUST be fixed and unit-tested.

### 2.2 User story

As the **operator of the live Kalshi trader**, I want to switch the live strategy
from `f6_wait270` to `f1_d50cap75` via an environment variable (no code fork) and
re-enter live trading at a $5 stake on the isolated, $147.73-funded subaccount #1,
**after** an entry-fidelity audit proves the Rust gate reproduces the paper F1
signal 1:1, so that I trade the strategy with the measured post-fee cushion instead
of the breakeven one — while keeping the default (env-unset) behavior byte-identical
to today's f6 mirror and preserving every existing safety rail.

### 2.3 Functional requirements

**Strategy selection (env-driven, no fork)**

1. Strategy selection MUST be driven by a `SESSION` environment variable accepting
   exactly the values `f6_wait270` and `f1_d50cap75`. Default (unset or
   unrecognized) MUST resolve to `f6_wait270` — today's behavior. An unrecognized
   non-empty value MUST fail loudly at startup (log an error and either abort or
   fall back to `f6_wait270`; the fallback path MUST log which session it selected)
   rather than silently mis-trade.
2. A new factory `SessionConfig::f1_d50cap75()` MUST be added to
   `kalshi_rs/src/config.rs` (alongside the existing `f6_wait270()` at ≈ lines
   40-58) returning: `kappa = 0.4`, `delta_threshold = 50.0`,
   `p_model_threshold = 0.65`, `sigma_type = "max10"`, `tau_mode = Linear`,
   `trade_side = Both`, `max_entry_price = 0.92`, `entry_wait_min = 3.0` (180 s),
   `liq_filter = false`, `sigma_max = None`, and the remaining fields matching the
   f6 factory's defaults (no regime blocks / kill hours). The `SESSION` value MUST
   select which factory `main.rs` (≈ line 417, currently hardcoded
   `SessionConfig::f6_wait270()`) calls — no `#[cfg]` fork, no duplicate binary.
3. The `SESSION_NAME` used for telemetry `session` tags and log lines
   (`kalshi_rs/src/main.rs` const at ≈ line 57, currently
   `"f6_wait270_shadow"`) MUST be derived from the selected session so live F1
   fills are attributable to `f1_d50cap75` in the ledger, not mislabeled as f6.

**MIRROR session parameterization**

4. `kalshi_rs/src/mirror.rs::fetch` MUST read `live_sigma` from the paper session
   named by the selected strategy, not the hardcoded `"f6_wait270"` key at ≈ line
   74. The session key MUST come from a `MIRROR_SESSION` env variable, defaulting to
   the value derived from `SESSION` (so `SESSION=f1_d50cap75` reads
   `sessions_state["f1_d50cap75"]["live_sigma"]`). If the named session key is
   absent from `/api/sessions_state`, `fetch` MUST return `None` (skip the tick, per
   the existing "never trade blind" contract at `mirror.rs` ≈ lines 48, 87-99) — it
   MUST NOT fall back to a different session's sigma.
5. The `MirrorSnap` sigma field (`mirror.rs` ≈ line 25, currently named
   `sigma_max30`) carries the **selected session's** `live_sigma` regardless of the
   field's legacy name; any rename is cosmetic and MUST NOT change the value's
   meaning or the freshness (`age_secs`) guard.

**CRITICAL — mirrored sigma keyed by `sigma_type`**

6. The mirrored sigma insertion at `kalshi_rs/src/main.rs` ≈ line 868 MUST insert
   under `cfg.sigma_type` (`sigmas.insert(cfg.sigma_type.clone(), <mirrored
   sigma>)`), NOT the hardcoded `"max30"`. This guarantees the gate lookup
   `s.sigmas.get(&cfg.sigma_type)` (`engine.rs` ≈ lines 123, 135) hits the mirrored
   value for BOTH f6 (`max30`) and F1 (`max10`), so neither session silently falls
   back to `realized5_pmin`.
7. A **unit test** MUST assert that, given a `SessionConfig` with `sigma_type =
   "max10"` and a mirrored sigma value, the `Shared.sigmas` map the gate consumes
   contains that value under key `"max10"` (and that the gate resolves sigma from
   that key, not the `realized5_pmin` fallback). An equivalent assertion MUST hold
   for `sigma_type = "max30"` (f6) so the fix is proven non-regressive for both.

**Entry-fidelity audit (gate before go-live)**

8. Before `LIVE_TRADING=1` is enabled for F1, an **entry-fidelity audit document**
   MUST be produced comparing the Rust gate against the paper F1 engine, with a
   per-item **parity verdict** (MATCH / GAP-FIXED / GAP-ACCEPTED) for each of:
   - **p-model formula** — `p = phi(kappa * snr)`, `snr = |delta| / (sigma_usd *
     f_tau)`, tau linear (`kalshi_rs/src/signal.rs` ≈ lines 175-199) vs the paper
     `compute_p_model`.
   - **entry_wait timing** — paper enters at exactly 180 s; verify the Rust elapsed
     gate `s.elapsed_min < cfg.entry_wait_min` (`engine.rs` ≈ lines 97-98) fires at
     the same instant with `entry_wait_min = 3.0`.
   - **threshold_gap semantics** — the paper F1 cfg has `threshold_gap = 0.1`; the
     Rust gate has **no** `threshold_gap` logic today (confirmed absent). The audit
     MUST determine what `threshold_gap` does in the paper engine and whether it
     affects entry/side decisions; if it does, the Rust gate MUST replicate it or
     the divergence MUST be explicitly quantified and accepted.
   - **max_entry band** — the reject-below-0.5 / cap-at-`max_entry_price` band
     (`engine.rs` ≈ lines 187-199) matches the paper.
   - **liq_filter off** — confirm `liq_filter = false` yields no `session_liq` skip
     path divergence (`engine.rs` ≈ lines 203-206).
   - **sigma source** — confirm F1's `max10` mirrored sigma is used (depends on
     FR-6) and that the local `compute_all_sigmas` map is not relied upon in MIRROR
     mode.
   - **execution buffers** — how `PRICE_BUF` (prod `0.06`) and `REQUOTE_BUF`
     (`0.12`) move the effective entry vs the paper entry (mirror gap vs drift vs
     walk, per Section 1 FR-2/AC-2), with a recommendation whether to keep the prod
     values for F1's 180 s (higher-momentum) entry.
   Any parity gap found MUST be either fixed (with a `GAP-FIXED` verdict and a
   reference to the code change) or explicitly accepted in writing (`GAP-ACCEPTED`
   with rationale). The audit doc's existence and completeness is a go-live gate.

**Live rollout config (EU box) + reused safety rails**

9. Live rollout on the EU box (`34.32.177.126`, systemd service
   `kalshi-shadow-com`, via a drop-in env file) MUST set: `LIVE_TRADING=1`,
   `STAKE=5`, `SUBACCOUNT=1` (subaccount #1 holds $147.73), `MAX_ENTRY=0.92`,
   `SESSION=f1_d50cap75`, `MIRROR_SESSION=f1_d50cap75`. `DAILY_LOSS_STOP=30` is the
   recommended value (the old $100 stop is 68% of the $147.73 subaccount balance —
   too large); `MAX_TRADES_DAY` is unchanged.
10. All existing safety rails MUST be reused **unchanged** (no new safety logic in
    this feature): P0b latch-on-confirmed-fill + bounded retry (max 2 attempts /
    window, `RETRY_MAX_ATTEMPTS` / `RETRY_COOLDOWN_SECS`, Section 1 FR-6..9), daily
    trade cap and loss stop, the `(0.50, max_entry]` price band, subaccount
    isolation (`SUBACCOUNT` → `OrderClient`, `main.rs` ≈ lines 466-472), and the
    `LiveTriggerRecord` telemetry with entry decomposition (Section 1 FR-2/FR-3).

### 2.4 Non-functional requirements

1. **Byte-identical default.** With `SESSION` and `MIRROR_SESSION` unset, live and
   shadow behavior — signal, side, entry, sigma key, telemetry `session` tag, and
   order flow — MUST be byte-for-byte identical to the current f6 mirror. The env
   switch is the only behavior lever.
2. **No new hot-path latency.** The strategy switch adds no blocking I/O to the
   0.3 s decision loop; the mirrored-sigma keying (FR-6) is a `String` key change,
   not new work. MIRROR still reads `/api/state` + `/api/sessions_state` once per
   tick with the existing 3 s timeouts (`mirror.rs` ≈ lines 54-71).
3. **All existing tests keep passing.** The FR-7 unit test is additive; no existing
   test in `kalshi_rs` may regress.
4. **Rollback procedure (documented).** Rollback MUST be a config/binary swap with
   no data migration: restore the previous `*.bak` binary and the previous drop-in
   env file (which lacks `SESSION`/`MIRROR_SESSION` and thus reverts to f6), then
   restart `kalshi-shadow-com`. The procedure MUST be written down (in the audit /
   runbook doc) with the exact file paths and restart command.
5. **Fail-closed on ambiguity.** Any condition that would cause the bot to trade a
   non-F1 formula (missing session key, sigma-key miss, unrecognized `SESSION`)
   MUST skip the tick or fall back to the logged f6 default — never place a live
   order on an unverified signal.

### 2.5 Acceptance criteria

1. **F1 gate parameters.** With `SESSION=f1_d50cap75`, startup logs (`main.rs` ≈
   lines 419-428) show `entry_wait=3min delta>=50 p>=0.65 sigma=max10
   max_entry=0.92`, and the gate fires only when `kappa 0.4 / |Δ| ≥ 50 / p ≥ 0.65 /
   max10 sigma / elapsed ≥ 180 s / entry ≤ 0.92` all hold.
2. **Mirrored sigma lands under `sigma_type` (unit-tested).** The FR-7 unit test
   passes: a mirrored sigma with `sigma_type = "max10"` is retrievable from the
   gate's `sigmas` map under `"max10"` and is the value the gate uses (not the
   `realized5_pmin` fallback); the `"max30"` (f6) assertion also passes.
3. **MIRROR reads the F1 session.** With `SESSION=f1_d50cap75` (or
   `MIRROR_SESSION=f1_d50cap75`), `mirror::fetch` reads
   `sessions_state["f1_d50cap75"]["live_sigma"]`; if that key is absent the tick is
   skipped (no order), verified by log line.
4. **Default unchanged.** With `SESSION` unset, an integration/behavioral check
   confirms the bot selects `f6_wait270`, reads
   `sessions_state["f6_wait270"]["live_sigma"]`, inserts the sigma under `"max30"`,
   and tags telemetry as f6 — identical to pre-feature behavior.
5. **Audit doc exists with per-item verdicts.** The Section 2.3 FR-8 audit document
   exists and carries a MATCH / GAP-FIXED / GAP-ACCEPTED verdict for every listed
   item (p-model, entry_wait, threshold_gap, max_entry band, liq_filter, sigma
   source, execution buffers), with any GAP-FIXED verdict pointing at the code
   change and any GAP-ACCEPTED carrying written rationale.
6. **Deployed and measured.** On the EU box with `LIVE_TRADING=1 STAKE=5
   SUBACCOUNT=1 SESSION=f1_d50cap75`, the first live fills produce
   `LiveTriggerRecord` rows whose **mirror gap** (`signal_entry` − paper F1 entry)
   is ≈ 0, and whose `drift` (`exec_entry − signal_entry`) and `walk`
   (`eff − exec_entry`) decompose the remaining entry deviation (per Section 1
   AC-2). Orders route to subaccount #1.
7. **Observability.** From the first live fills, the Buffalo dashboard
   (`23.95.217.78:8890`) shows the live-F1 series (green, pushed via
   `/shadow_com`) against the paper-F1 series (pink, `/paper_f1`) — both series
   already exist (`kalshi_rs/src/dashboard.rs` ≈ lines 103-108); no dashboard change
   is required.

### 2.6 Affected endpoints

Internal Rust binary — no public HTTP routes are created or modified. Relevant
**outbound** calls (contracts unchanged; only the session key read changes):

- `GET {MIRROR_STATE_URL}/api/sessions_state` — `mirror.rs` ≈ lines 63-71. The read
  key changes from the hardcoded `"f6_wait270"` to the selected session
  (`"f1_d50cap75"` for F1). Verified that the endpoint exposes
  `f1_d50cap75.live_sigma`.
- `GET {MIRROR_STATE_URL}/api/state` — `mirror.rs` ≈ lines 54-62. Unchanged.
- `POST {dashboard}/shadow_com` and `POST {dashboard}/paper_f1` — the existing
  dashboard push endpoints feeding the green (live-F1) and pink (paper-F1) series.
  Unchanged; already wired.
- `OrderClient::create_ioc(...)` — live IOC placement, now routed to
  `SUBACCOUNT=1`. Contract and pricing unchanged from Section 1.

### 2.7 Schema changes

No SQL database. No ledger record-shape change: F1 reuses the `LiveTriggerRecord`
from Section 1 (`kalshi_rs/src/ledger.rs`) as-is, including the
`signal_entry` / `exec_entry` / `eff` decomposition fields. Config-level "schema"
changes only:

- **New factory** `SessionConfig::f1_d50cap75()` in `kalshi_rs/src/config.rs`
  (additive; the `f6_wait270()` factory and the `SessionConfig` struct shape are
  unchanged).
- **New env inputs** `SESSION` and `MIRROR_SESSION` (both optional; defaults
  preserve f6). No change to the existing env knobs (`STAKE`, `SUBACCOUNT`,
  `MAX_ENTRY`, `DAILY_LOSS_STOP`, `MAX_TRADES_DAY`, `PRICE_BUF`, `REQUOTE_BUF`,
  `RETRY_*`).
- **Behavioral key change** (not a shape change): the mirrored-sigma map key becomes
  `cfg.sigma_type` instead of the literal `"max30"` (FR-6). The `MirrorSnap` sigma
  field may be renamed cosmetically (FR-5) with no meaning change.

### 2.8 UI changes

None. The Buffalo dashboard already renders the live-F1 (green, `/shadow_com`) and
paper-F1 (pink, `/paper_f1`) series (`kalshi_rs/src/dashboard.rs` ≈ lines 103-108,
196-255). This feature adds no page, component, or field.

### 2.9 Out of scope

- Lowering `PRICE_BUF` / `REQUOTE_BUF` as part of this feature (the FR-8 audit may
  *recommend* a change; actually changing the prod buffers is a separate config
  change, consistent with Section 1.9).
- Adding any strategy beyond `f6_wait270` and `f1_d50cap75` (the `SESSION` enum is
  intentionally closed to these two).
- A Kalshi WebSocket orderbook feed, us-east co-location, or a modeled paper twin
  (all remain out of scope per Section 1.9).
- Any schema, HTTP-endpoint, or dashboard-UI change.
- Re-tuning the Kalshi liquidity gate (`liq_filter` stays off, matching paper F1).

### 2.10 Risks and open questions

Documented honestly, per the operator's explicit instruction:

- **Thin, regime-dependent edge.** F1's measured cushion is only ≈ +0.8pt WR over
  breakeven ($5 + real Kalshi fees). This is small enough that a regime shift can
  erase it; the +$154.60 all-time figure is not a forward guarantee.
- **Never traded live.** F1's live no-fill and slippage profile is **unknown**.
  Entering at 180 s (vs f6's 270 s) rides stronger momentum, which historically
  drives the entry-price right-tail (Section 1: 13% of f6 trades paid ≥6c). F1's
  drift/walk could be worse; the Section 1 telemetry is the instrument that will
  reveal it from the first fills.
- **Would still have lost in the old live window** (−$30.14), just less than f6.
  Live viability rests on the post-stop regime (30.06–05.07, +$85.60 @ 80.2% WR)
  persisting — an assumption, not a fact.
- **Small subaccount.** Subaccount #1 holds only $147.73; hence the
  `DAILY_LOSS_STOP=30` recommendation. A run of bad windows can still draw the
  balance down materially at $5/trade.
- **Open question — `threshold_gap` semantics.** The paper F1 cfg sets
  `threshold_gap = 0.1` but the Rust gate has no such logic. FR-8 must resolve
  whether this is a real entry-affecting parameter (parity gap) or inert for the
  pmodel path before go-live.
