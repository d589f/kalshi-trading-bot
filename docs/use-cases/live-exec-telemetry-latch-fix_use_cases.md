# Use Cases: Live-Execution Telemetry + Window-Latch Fix

> Based on [PRD](../PRD.md) — section 1, internal id `live-exec-telemetry-latch-fix`

This feature has two parts under one id:

- **P0 — Live trade telemetry** (zero order-behavior change): every live fire
  (fill, partial, no-fill, and every early skip/error/reject) writes exactly one
  typed `LiveTriggerRecord` JSONL row with full entry-price decomposition fields.
- **P0b — Latch fix** (behavior change, shadow/log-only validation required first):
  the once-per-window latch `fired_window` is consumed only on a confirmed fill, so
  a no-fill no longer permanently burns the window; bounded retry (max attempts `N`
  + cooldown `C`) is allowed within the same window.

## Actors (non-UI Rust binary)

There is no UI and no HTTP API in this feature. The "actors" are internal code paths:

- **signal_loop** — the 0.3s decision loop (`kalshi_rs/src/main.rs` ≈ 538-779). Owns
  the `fired_window` latch (line 552) and the trigger block (lines 747-777). Under
  P0b it also owns the per-window attempt counter and last-attempt timestamp.
- **place_live** — the live order routine (`kalshi_rs/src/main.rs` ≈ 878-1044). Runs
  the safety gates, computes `exec_entry`/`price`/`count`, fires the IOC (with one
  optional re-quote), and (post-feature) appends the `LiveTriggerRecord` and returns
  an outcome to signal_loop.
- **OrderClient** — `create_ioc(...)` (`kalshi_rs/src/kalshi/orders.rs` ≈ 87-116).
  Returns `(http_status, OrderResp, latency_ms)`. `OrderResp` exposes `fill()`,
  `avg_price()`, `fee()`, `remaining_count`, `order_id`.
- **KalshiRest** — `get_orderbook(...)` (`main.rs` ≈ 927) for the fresh order-time
  `exec_entry`.
- **JSONL ledger** — `Ledger::append` (`kalshi_rs/src/ledger.rs` ≈ 24-41),
  `Mutex`-guarded, append-only.
- **Ledger loaders** — the "resolver"/dashboard replay paths
  `load_ledger_into_dash` (`main.rs` ≈ 64-102) and `total_pnl_from_ledger`
  (`main.rs` ≈ 153-167), which read each line as a `serde_json::Value` and switch
  on `kind`.

## Shared preconditions (apply unless a use case overrides them)

- `LIVE_TRADING=1` and an `OrderClient` is present (`lcfg.enabled && order_client.is_some()`,
  `main.rs` line 753); otherwise the loop takes the shadow `emit_trigger` path, which
  this feature does NOT change.
- A `res.fire` is present this tick (`main.rs` line 748) — i.e. the evaluator produced
  a trigger.
- The window is not yet latched for the current `win_key`
  (`fired_window.as_deref() != win_key.as_deref()`, line 749) — except where a use case
  is specifically about the latch.
- `book.ticker` is non-empty (line 751); an empty ticker is its own edge case (UC-12).
- The MIRROR 1:1 signal path (`main.rs` ≈ 599-646) has already produced the side/entry;
  this path is unchanged by the feature.
- `place_live` is `await`ed inline in signal_loop (lines 755-771), so two `place_live`
  invocations never overlap (no concurrency within the loop). This is a structural
  precondition for all P0b use cases.

## Invariants asserted across use cases (verify in every relevant postcondition)

- **INV-1 (one row per fire):** every code path in `place_live` that today returns
  (the 4 skips, order-error, rejected, no-fill, and the fill path) appends **exactly
  one** `LiveTriggerRecord`.
- **INV-2 (one filled position per window):** `fired_window` is consumed only on a
  confirmed fill (`filled` or `partial`); at most one filled position exists per
  `win_key`.
- **INV-3 (pricing/sizing unchanged):** `PRICE_BUF`/`REQUOTE_BUF`, and the formulas
  for `exec_entry` (≈ 927-937), first `price` and re-quote `price` (≈ 938-941,
  962-965), and `count` (≈ 942-948) are byte-for-byte identical to pre-feature.
- **INV-4 (MIRROR unchanged):** the MIRROR 1:1-with-paper signal path (≈ 599-646) is
  untouched; side and signal `entry` are identical to pre-feature.
- **INV-5 (trades_today only on fill):** `s.trades_today += 1` happens only after a
  confirmed fill (`main.rs` ≈ 1001-1005); retries and no-fills/skips never increment it.
- **INV-6 (additive JSON / backward compat):** old records (lacking the new fields)
  and new records both parse in `load_ledger_into_dash` and `total_pnl_from_ledger`;
  the `kind` discriminator stays `"trigger"` and the previously-consumed field names
  are unchanged.
- **INV-7 (append-only):** ledger remains append-only JSONL, one object per line,
  via `OpenOptions::new().create(true).append(true)`; no row is rewritten/deleted.
- **INV-8 (no added hot-path I/O):** telemetry reuses the existing `Ledger::append`;
  no new blocking network I/O is introduced on the decision path.

---

## UC-1: Fired order fills fully → one `filled` ledger row with full decomposition

**Actor**: place_live (called by signal_loop)
**Preconditions**: shared preconditions hold; safety gates pass (`trades_today <
max_trades_day`, `day_pnl > -daily_loss_stop`, `0.50 < entry <= max_entry_price`);
`create_ioc` returns HTTP 201 with `fill_count > 0` and `remaining_count == 0`.
**Trigger**: signal_loop reaches the trigger block (`main.rs` ≈ 748) with `res.fire`
present and the window not latched.

### Primary Flow (Happy Path)
1. signal_loop computes `win_key` and finds it not latched.
2. signal_loop calls `place_live(...).await` (latch is NOT set before the await —
   P0b removes the pre-await `fired_window = win_key` at line 754).
3. place_live passes the daily-cap, loss-stop, and band gates.
4. place_live fetches the fresh order-time book and computes `exec_entry`, first
   `price`, and `count` (unchanged formulas — INV-3).
5. place_live calls `create_ioc` once; response is HTTP 201, `fill() > 0`,
   `remaining_count == 0`.
6. place_live computes `eff` from `resp.avg_price()` (with the `entry`/`1-entry`
   fallback), `fee` from `resp.fee()`, `latency_ms` from `lat`.
7. place_live increments `trades_today` and saves live state (INV-5).
8. place_live appends **one** `LiveTriggerRecord` with `outcome = filled` and pushes
   to the dashboard + `pending` (settlement) exactly as today.
9. place_live returns a `filled` outcome to signal_loop.
10. signal_loop consumes the latch: `fired_window = win_key` (INV-2).

**Postconditions**: exactly one row appended (INV-1) with
`outcome == "filled"` and all decomposition fields present and non-null:
`entry` (= `fire.entry`), `exec_entry`, `orderbook_entry` (= `fire.orderbook_entry`),
`first_limit_price`, `requote_limit_price` (null here — no re-quote), `requote ==
false`, `remaining_count == 0`, `fill (> 0)`, `eff`, `fee`, `latency_ms`, plus context
`count`, `side`, `p`, `delta_from_open`, `window_start`, `window_end`, `market_ticker`,
`order_id`, `ts`, `ts_iso`. Offline, `gap = eff − entry`, `drift = exec_entry − entry`,
`walk = eff − exec_entry`, `gap = drift + walk` are all computable. Window is latched;
`trades_today` incremented by 1 (INV-2, INV-5). INV-3/INV-4/INV-7/INV-8 hold.

### Alternative Flows
- **UC-1-A1: YES side vs NO side** — `eff` and `price` are computed via the YES
  branch (`resp.avg_price()`, `exec_entry + price_buf`) or the NO branch
  (`1 - avg_price()`, `(1 - exec_entry) - price_buf`). Both produce a `filled` row;
  only the per-side formula differs (unchanged — INV-3).
- **UC-1-A2: `count` hits cap** — `count` computed as `round(stake/exec_entry)` is
  clamped to `[1, max_count]` (≈ 942-948). The record's `count`/`fill` reflect the
  clamped size; outcome still `filled`.

### Error Flows
- **UC-1-E1: serialization error in `Ledger::append`** — `serde_json::to_string`
  returns `Err`; `Ledger::append` logs `"ledger serialize: …"` and returns without
  writing (ledger.rs ≈ 27-30). The loop MUST NOT panic; place_live still returns its
  outcome and the latch decision is unaffected. (No row is written in this rare case —
  acceptable degradation, must not crash.)
- **UC-1-E2: ledger file open/write error** — `OpenOptions::open` or `writeln!`
  returns `Err`; `Ledger::append` logs and returns (ledger.rs ≈ 35-39). No panic; loop
  continues.

### Edge Cases
- **UC-1-EC1: `avg_price` missing on a fill** — `resp.average_fill_price` is `None`,
  so `resp.avg_price()` is `None` and `eff` falls back to `entry` (YES) /
  `1 - entry` (NO) (`main.rs` ≈ 996-997). The row still records `outcome = filled`
  with the fallback `eff`; `gap` is then `0` on the entry leg by construction —
  documented, not an error.
- **UC-1-EC2: `fee` absent** — `resp.average_fee_paid` is `None`; `resp.fee()` returns
  `0.0` (orders.rs ≈ 52-57). Row records `fee = 0.0`.
- **UC-1-EC3: `order_id` absent** — `resp.order_id` defaults to empty string
  (orders.rs ≈ 33-34); row records `order_id = ""` (or null per record shape). Not an
  error.

### Data Requirements
- **Input**: `fire` (entry, side, p, orderbook_entry), `shared.delta_from_open`,
  `win` (window bounds), `book.ticker`, fresh order-time book, `OrderResp`
  (`fill_count`, `remaining_count`, `average_fill_price`, `average_fee_paid`,
  `order_id`), `lat`, `lcfg`.
- **Output**: one `LiveTriggerRecord` (outcome `filled`); one `TrigSummary` pushed to
  dashboard; one `Pending` pushed for settlement.
- **Side Effects**: `trades_today += 1` and `save_live_state`; ledger append; outbound
  `create_ioc` (1 call) and `get_orderbook` (1 call).

---

## UC-2: Order accepted (HTTP 201) but ZERO fill → one `nofill` ledger row

**Actor**: place_live
**Preconditions**: shared preconditions hold; gates pass; `create_ioc` returns HTTP
201 with `fill() <= 0`. If re-quote is enabled it also returned no fill (see UC-4).
**Trigger**: same as UC-1.

### Primary Flow (Happy Path)
1-4. As UC-1 steps 1-4 (latch NOT pre-set).
5. `create_ioc` returns HTTP 201 with `fill() <= 0` and `remaining_count > 0`
   (or equal to the requested count).
6. Re-quote condition (`status == 201 && fill() <= 0 && requote_buf > price_buf`) is
   evaluated; if it does not apply, no re-quote is sent.
7. `status == 201` so it is not "rejected"; `fill <= 0` so place_live logs
   "LIVE NO-FILL" (≈ 987-992).
8. **Post-feature:** before returning, place_live appends **one** `LiveTriggerRecord`
   with `outcome = nofill` (today it returns silently at ≈ line 992 — INV-1 fixes this).
9. place_live returns a `nofill` outcome.
10. signal_loop does NOT latch the window (INV-2); per UC-7/UC-8 it may schedule a
    bounded retry.

**Postconditions**: exactly one `nofill` row (INV-1) with order-pricing fields
populated (`exec_entry`, `first_limit_price`, `requote`/`requote_limit_price`),
`remaining_count` recorded, `fill <= 0`, fill-economic fields that do not apply
(`eff`) recorded as null/absent. `trades_today` NOT incremented (INV-5). Window NOT
latched. No `Pending` pushed (no position). INV-3/INV-4/INV-6/INV-7 hold.

### Alternative Flows
- **UC-2-A1: re-quote disabled** (`requote_buf <= price_buf`) — only the first IOC is
  attempted; on no-fill the row records `requote == false`, `requote_limit_price` null.

### Error Flows
- **UC-2-E1: serialization/ledger write error** — as UC-1-E1/E2; no panic, loop
  continues, outcome still returned as `nofill`.

### Edge Cases
- **UC-2-EC1: `remaining_count` unparsable** — `resp.remaining_count` is a string;
  if absent/garbage it defaults to empty/`"0"`. Record stores the raw/parsed value as
  defined by the record shape; must not crash.

### Data Requirements
- **Input**: as UC-1 minus a successful fill; `resp.remaining_count`, `resp.fill()`.
- **Output**: one `LiveTriggerRecord` (outcome `nofill`). No dashboard/pending push.
- **Side Effects**: ledger append; `create_ioc` (1 call, or 2 with re-quote);
  `get_orderbook` (1 call). No state mutation.

---

## UC-3: PARTIAL fill (`fill > 0` AND `remaining_count > 0`) → one `partial` ledger row

**Actor**: place_live
**Preconditions**: shared preconditions hold; gates pass; `create_ioc` returns HTTP
201 with `fill() > 0` AND `remaining_count > 0`.
**Trigger**: same as UC-1.

### Primary Flow (Happy Path)
1-5. As UC-1 steps 1-5, but the response has `fill() > 0` and `remaining_count > 0`.
6. place_live treats this as a real (partial) position: computes `eff`, `fee`,
   `latency_ms`; increments `trades_today` (a position was opened — INV-5 still holds:
   increment happens because `fill > 0`).
7. place_live appends **one** `LiveTriggerRecord` with `outcome = partial`,
   `remaining_count` (> 0) recorded, `fill` = the partial count.
8. place_live pushes a `TrigSummary` and a `Pending` sized to the **filled** count
   (`count = fill`), exactly as the fill path does today.
9. place_live returns a `partial` outcome (treated as a confirmed fill for latching).
10. signal_loop consumes the latch (`partial` latches — FR-7, INV-2).

**Postconditions**: exactly one `partial` row (INV-1) with `remaining_count > 0`
recorded and all decomposition fields present (as UC-1). Window latched (no second
position for this window — INV-2). `trades_today` incremented by 1 (INV-5). `Pending`
count equals the filled count, not the requested count.

### Alternative Flows
- **UC-3-A1: partial after a re-quote** — first IOC no-fill, re-quote partially fills.
  Record carries `requote == true`, `requote_limit_price` set, `outcome = partial`.

### Error Flows
- **UC-3-E1: serialization/ledger write error** — as UC-1-E1/E2; latch decision still
  derived from the returned `partial` outcome.

### Edge Cases
- **UC-3-EC1: `fill` rounds to a smaller integer than requested** — `Pending`/record
  `count` use the actual filled count (`fill as i64`), guaranteeing settlement matches
  the real position size.

### Data Requirements
- **Input**: as UC-1 with `remaining_count > 0`.
- **Output**: one `LiveTriggerRecord` (outcome `partial`); one `TrigSummary`; one
  `Pending` sized to `fill`.
- **Side Effects**: `trades_today += 1`; ledger append; latch consumed; 1-2
  `create_ioc` calls.

---

## UC-4: Re-quote path runs (first IOC no-fill, then deeper IOC fills) → `requote == true`

**Actor**: place_live
**Preconditions**: shared preconditions hold; gates pass; `requote_buf > price_buf`
(re-quote enabled); first `create_ioc` returns HTTP 201 with `fill() <= 0`; the deeper
re-quote IOC returns a fill.
**Trigger**: same as UC-1.

### Primary Flow (Happy Path)
1-5. As UC-1 steps 1-5, but the first IOC returns HTTP 201 with `fill() <= 0`.
6. The re-quote guard passes (`status == 201 && resp.fill() <= 0 && requote_buf >
   price_buf`, ≈ 961). place_live computes the deeper `price` (YES:
   `(exec_entry + requote_buf).min(0.97)`; NO: `((1-exec_entry) - requote_buf).max(0.03)`
   — unchanged formula, INV-3) and records it as `requote_limit_price`.
7. place_live sends the second IOC with `client_order_id = "{coid}-rq"`; it returns
   HTTP 201 with `fill() > 0`.
8. place_live computes `eff`/`fee`/`latency_ms` from the re-quote response, increments
   `trades_today`, appends **one** `LiveTriggerRecord` with `requote == true`,
   `first_limit_price` = the first price, `requote_limit_price` = the deeper price,
   `outcome = filled` (or `partial` if `remaining_count > 0`).
9. Returns `filled`/`partial`; signal_loop latches the window.

**Postconditions**: exactly one row (INV-1) recording BOTH limit prices and
`requote == true`; `outcome` reflects the re-quote result. Window latched.
`latency_ms` reflects the re-quote RTT (`l2`). INV-3 holds (re-quote price formula
unchanged).

### Alternative Flows
- **UC-4-A1: re-quote also no-fills** — second IOC returns `fill() <= 0`; place_live
  falls through to the no-fill path → `outcome = nofill`, `requote == true`,
  `requote_limit_price` recorded. (Same as UC-2 but with `requote == true`.) Window NOT
  latched; eligible for bounded retry.
- **UC-4-A2: re-quote send fails** — the second `create_ioc` returns `Err`; the
  `if let Ok(...)` does not update `status/resp/lat` (≈ 973-980), so the first
  response stands. Outcome derives from the first response (`nofill`), `requote ==
  true` (a re-quote was attempted/issued). Must not crash.

### Error Flows
- **UC-4-E1: serialization/ledger write error** — as UC-1-E1/E2.

### Edge Cases
- **UC-4-EC1: `requote_buf == price_buf`** — guard is false; no re-quote; behaves as
  UC-2 with `requote == false`.

### Data Requirements
- **Input**: as UC-1 plus the second `OrderResp` (`s2, r2, l2`).
- **Output**: one `LiveTriggerRecord` with `requote == true` and both prices.
- **Side Effects**: 2 `create_ioc` calls; on fill, `trades_today += 1`, latch
  consumed, dashboard/pending push.

---

## UC-5: Early skip — daily cap reached → one `skip_daily_cap` ledger row

**Actor**: place_live
**Preconditions**: `LIVE_TRADING=1`; a `res.fire` is present; `s.trades_today >=
lcfg.max_trades_day` (`main.rs` ≈ 905).
**Trigger**: signal_loop calls `place_live` (latch not pre-set).

### Primary Flow (Happy Path)
1. place_live runs the daily-reset block; finds `trades_today >= max_trades_day`.
2. place_live logs "LIVE HALT: max … trades/day" and (post-feature) appends **one**
   `LiveTriggerRecord` with `outcome = skip_daily_cap` (today returns silently at
   ≈ 907 — INV-1 fixes this), populating known context (`entry`, `orderbook_entry`,
   `side`, `p`, `delta_from_open`, window bounds, `market_ticker`, `ts`, `ts_iso`) and
   recording order-pricing/fill fields as null/absent.
3. place_live returns a `skip_daily_cap` outcome.
4. signal_loop does NOT latch and does NOT consume a bounded-retry attempt (FR-9 — a
   pre-order gate skip is not an attempt).

**Postconditions**: exactly one `skip_daily_cap` row (INV-1); no order placed; no
state mutation; window NOT latched; attempt budget `N` NOT decremented. The skip is
terminal **for this tick**, but is re-evaluated on the next tick (it is not latched);
if the cap still holds, the next tick writes another `skip_daily_cap` row.

### Alternative Flows
- **UC-5-A1: cap cleared by daily reset** — if `now_utc` crossed into a new UTC day,
  the reset block zeroes `trades_today` first (≈ 901-904); the cap no longer applies
  and place_live proceeds (UC-1 path). No skip row.

### Error Flows
- **UC-5-E1: serialization/ledger write error** — as UC-1-E1/E2.

### Edge Cases
- **UC-5-EC1: repeated skips inflate row count** — because the skip is re-evaluated
  every 0.3s tick while a fire persists and the cap holds, many `skip_daily_cap` rows
  can be written for one window. Documented as expected; the latch is NOT used to
  suppress skip rows (skips never latch). (QA may assert at-least-one, not exactly-one,
  across a multi-tick window for skips — contrast INV-1 which is per-fire/per-call.)

### Data Requirements
- **Input**: `s.trades_today`, `lcfg.max_trades_day`, `fire`/`shared`/`win`/`book`
  context.
- **Output**: one `LiveTriggerRecord` (outcome `skip_daily_cap`), order/fill fields
  null.
- **Side Effects**: ledger append only; no `create_ioc`, no `get_orderbook`, no state
  change.

---

## UC-6: Early skip — loss-stop, band, order-error, rejected (four sub-cases)

**Actor**: place_live
**Preconditions**: `LIVE_TRADING=1`; `res.fire` present; one of the four conditions
below holds.
**Trigger**: signal_loop calls `place_live`.

### Primary Flow (Happy Path)
Each sub-case writes exactly one `LiveTriggerRecord` with the named outcome, returns
that outcome, and (per FR-9) does NOT latch the window. Pre-order gates do not consume
a retry attempt; post-order errors DO (see Postconditions).

- **UC-6-A1: `skip_loss_stop`** — `s.day_pnl <= -lcfg.daily_loss_stop` (≈ 909). Logs
  "LIVE HALT: daily loss stop"; appends one `skip_loss_stop` row (today returns at
  ≈ 911); no order placed; pre-order gate → no retry attempt consumed.
- **UC-6-A2: `skip_band`** — `!(0.50 < entry && entry <= cfg.max_entry_price)`
  (≈ 915). Logs "LIVE SKIP: entry … out of (0.50, …]"; appends one `skip_band` row
  (today returns at ≈ 917); no order placed; pre-order gate → no retry attempt
  consumed.
- **UC-6-A3: `order_error`** — `oc.create_ioc(...)` returns `Err` on the **first**
  attempt (≈ 953-956). Logs "LIVE order error"; appends one `order_error` row (today
  returns at ≈ 955); an order WAS attempted → DOES consume one bounded-retry attempt
  `N` (FR-9). Order-pricing fields (`exec_entry`, `first_limit_price`) are populated;
  fill fields null.
- **UC-6-A4: `rejected`** — after the (first and optional re-quote) IOC,
  `status != 201` (≈ 982-985). Logs "LIVE order rejected HTTP …"; appends one
  `rejected` row (today returns at ≈ 984); an order WAS attempted → DOES consume one
  bounded-retry attempt `N`. Records `latency_ms` and the HTTP path that failed where
  available.

**Postconditions**: for each sub-case, exactly one row of the named outcome (INV-1);
no latch (INV-2 preserved — only fills latch); `trades_today` unchanged (INV-5).
Pre-order gates (`skip_loss_stop`, `skip_band`) do NOT decrement `N`; post-order
errors (`order_error`, `rejected`) DO decrement `N` (count toward the per-window
attempt budget).

### Error Flows
- **UC-6-E1: serialization/ledger write error** in any sub-case — as UC-1-E1/E2; no
  panic; outcome still returned.

### Edge Cases
- **UC-6-EC1: band edge values** — `entry == 0.50` (excluded, `<` is strict) →
  `skip_band`; `entry == cfg.max_entry_price` (included, `<=`) → proceeds; `entry`
  just above `max_entry_price` → `skip_band`. Boundary behavior unchanged (INV-3).
- **UC-6-EC2: order-error on the re-quote only** — first IOC succeeded as no-fill,
  re-quote `create_ioc` returns `Err`; this is UC-4-A2 (not `order_error`), since the
  primary status came back 201; outcome derives from the first response.
- **UC-6-EC3: `rejected` after a re-quote** — both IOCs returned non-201; `status`
  holds the last non-201; outcome `rejected`. Two `create_ioc` calls were attempted —
  define whether that counts as one or two attempts toward `N`. Expected: it counts as
  **one** per-window attempt (one place_live invocation = at most one attempt unit),
  to keep `N` a per-tick/per-invocation budget. (Flagged for planner; default = one.)

### Data Requirements
- **Input**: `s.day_pnl`/`daily_loss_stop` (A1); `entry`/`max_entry_price` (A2);
  `create_ioc` `Err` (A3); `status` (A4).
- **Output**: one `LiveTriggerRecord` of the matching outcome.
- **Side Effects**: ledger append; A3/A4 also performed at least one outbound
  `create_ioc` (and A4 a `get_orderbook`). No `trades_today` change.

---

## UC-7: No-fill within a window → window NOT latched → bounded retry on later ticks → first fill latches

**Actor**: signal_loop (owns `fired_window`, attempt counter, last-attempt ts) +
place_live
**Preconditions**: `LIVE_TRADING=1`; the same `win_key` persists across ticks (window
not rolled); bounded-retry knobs `N` (max attempts/window) and `C` (cooldown) are set;
`N > 1` (retry enabled).
**Trigger**: a `nofill` outcome on attempt 1, then the fire persists on later ticks.

### Primary Flow (Happy Path)
1. Tick T0: window not latched; signal_loop calls `place_live`; outcome `nofill`
   (UC-2). signal_loop records attempt count = 1 and `last_attempt_ts = now`; does NOT
   set `fired_window` (INV-2).
2. Tick T0+0.3s: fire still present, window unchanged. signal_loop checks the cooldown:
   `now - last_attempt_ts < C` → it does NOT place an order this tick (UC-9 enforces
   this).
3. Tick at `now - last_attempt_ts >= C` and attempt count `< N`: signal_loop calls
   `place_live` again (attempt 2), increments attempt count, updates `last_attempt_ts`.
4. If attempt 2 returns `filled`/`partial`: place_live appends the row, increments
   `trades_today`, returns `filled`/`partial`; signal_loop sets `fired_window =
   win_key` (latched — INV-2). No further attempts this window.
5. If attempt 2 returns `nofill` again: repeat steps 2-4 until either a fill latches the
   window or attempt count reaches `N` (then UC-8).

**Postconditions**: at most ONE filled position per window (INV-2) — the first fill
latches and suppresses further orders. Each no-fill wrote its own `nofill` row (INV-1).
`trades_today` incremented exactly once (on the fill — INV-5), never inflated by the
no-fill attempts. Total order-placement attempts ≤ `N`. INV-3/INV-4 hold (each attempt
uses the unchanged pricing path on the then-current fresh book).

### Alternative Flows
- **UC-7-A1: first attempt fills** — no retry occurs; identical to UC-1; attempt count
  reaches 1 and the latch is consumed immediately.
- **UC-7-A2: `N == 1` (retry disabled)** — after a `nofill`, no retry is attempted;
  behavior is byte-for-byte the single-attempt-per-window behavior of today (NFR-4),
  except the extra `nofill` telemetry row. Window remains unlatched (a later window may
  fire), but no second attempt in the same window.

### Error Flows
- **UC-7-E1: an `order_error`/`rejected` mid-retry** — counts as one attempt toward `N`
  (FR-9); does not latch; retry continues if attempts remain and cooldown elapses.

### Edge Cases
- **UC-7-EC1: fire disappears between attempts** — if `res.fire` is absent on a later
  tick, no `place_live` call is made that tick; the attempt counter and cooldown are
  retained for the still-current window and resume if the fire returns within the same
  window.
- **UC-7-EC2: window rolls between attempts** — see UC-10 / UC-11 (counter resets on
  roll; in-flight old-window attempt must not latch the new window).

### Data Requirements
- **Input**: per-window `attempt_count`, `last_attempt_ts`, `N`, `C`, `place_live`
  outcome.
- **Output**: one `nofill` row per no-fill attempt; one `filled`/`partial` row on the
  fill.
- **Side Effects**: up to `N` `create_ioc`/`get_orderbook` invocations per window;
  latch consumed on first fill; `trades_today += 1` once.

---

## UC-8: Max attempts exhausted within a window → no further attempts until window roll resets state

**Actor**: signal_loop
**Preconditions**: `N` attempts have been placed in the current `win_key` without a
fill (all `nofill`/`order_error`/`rejected`); the window has not rolled.
**Trigger**: a later tick with the fire still present.

### Primary Flow (Happy Path)
1. signal_loop computes `attempt_count == N` for the current `win_key`.
2. signal_loop does NOT call `place_live` (budget exhausted), even though the window is
   unlatched and the fire persists. No order, no new row.
3. This persists for every subsequent tick until `win.window_start` (and thus
   `win_key`) changes.
4. On window roll (`roll_if_new` returns true / `win_key` changes, ≈ 576-585):
   signal_loop resets `attempt_count = 0` and clears `last_attempt_ts` (per FR-8) so
   the new window starts with a fresh budget and no cooldown carryover.

**Postconditions**: at most `N` order attempts occurred in the exhausted window; no
position opened there (INV-2 trivially satisfied — zero fills). On roll, attempt state
resets; the new window is eligible for up to `N` fresh attempts. INV-5 holds
(`trades_today` never incremented for a window with no fill).

### Alternative Flows
- **UC-8-A1: a fill occurs on attempt N** — the budget is consumed exactly at `N` and
  the fill latches; no "exhausted with no fill" state is reached. (This is UC-7 ending
  on the last allowed attempt.)

### Error Flows
- **UC-8-E1: window never rolls (clock/window bug)** — if `win_key` never changes, the
  budget stays exhausted and no orders are placed; this is the safe terminal state (no
  runaway ordering). Documented as acceptable fail-safe.

### Edge Cases
- **UC-8-EC1: roll detection** — the reset MUST key off the same `win_key`
  (`win.window_start.to_rfc3339()`) used for latching, so attempt-state and latch clear
  consistently on the same boundary. A mismatch would either leak attempts across
  windows or reset mid-window.

### Data Requirements
- **Input**: `attempt_count`, `N`, `win_key`, roll signal.
- **Output**: none (no order, no row) while exhausted.
- **Side Effects**: attempt-state reset on roll; no outbound calls while exhausted.

---

## UC-9: Bounded retry respects cooldown `C` (0.3s ticks must NOT order every tick)

**Actor**: signal_loop
**Preconditions**: `N > 1`; an attempt was just made at `last_attempt_ts`; the fire
persists and the window is unlatched; tick interval is 0.3s (`STATE_TICK_SECS`).
**Trigger**: successive 0.3s ticks within the cooldown interval.

### Primary Flow (Happy Path)
1. Attempt at tick T0 returns `nofill`; `last_attempt_ts = T0`, `attempt_count = 1`.
2. Ticks at T0+0.3, T0+0.6, … while `now - last_attempt_ts < C`: signal_loop evaluates
   the fire but does NOT call `place_live` — the cooldown gate blocks it.
3. First tick where `now - last_attempt_ts >= C` AND `attempt_count < N`: signal_loop
   calls `place_live` (next attempt).

**Postconditions**: between two attempts in the same window, at least `C` seconds
elapse; the trader does NOT place an order on every 0.3s tick. Number of orders in a
window ≤ `N`, and spacing ≥ `C` (NFR-1, FR-8).

### Alternative Flows
- **UC-9-A1: `C` large relative to window length** — if `C` exceeds the remaining
  window time, the second attempt may never fire before the window rolls; this is
  acceptable (bounded, no runaway). Effectively single-attempt for that window.

### Error Flows
- **UC-9-E1: clock non-monotonicity** — if `now` jumps backward, `now - last_attempt_ts`
  could be negative; the gate (`< C`) still blocks (treated as cooldown active), which
  is the safe direction. Documented.

### Edge Cases
- **UC-9-EC1: cooldown == 0** — if `C == 0` (a disabling value), the gate never blocks
  on time alone; attempts are then bounded purely by `N`. Must still cap at `N`
  (UC-8). With `C == 0` and `N == 1`, behavior equals today (NFR-4).

### Data Requirements
- **Input**: `last_attempt_ts`, `C`, `now`, `attempt_count`, `N`.
- **Output**: none on a blocked tick; an order on the first eligible tick.
- **Side Effects**: none on a blocked tick.

---

## UC-10: Trades_today increments only on fill (retries must not inflate the daily cap)

**Actor**: place_live + signal_loop
**Preconditions**: `LIVE_TRADING=1`; multiple attempts may occur per window (UC-7).
**Trigger**: a sequence of no-fill attempts followed (possibly) by a fill.

### Primary Flow (Happy Path)
1. Each `nofill`/`order_error`/`rejected`/skip attempt leaves `trades_today`
   unchanged (the increment at ≈ 1001-1005 is reached only after `fill > 0`).
2. Only when an attempt returns `filled`/`partial` does place_live execute
   `s.trades_today += 1; save_live_state(&s)` exactly once for that fill.

**Postconditions** (INV-5): after a window with `k` no-fill attempts then one fill,
`trades_today` increased by exactly 1, not `k+1`. The daily cap (`max_trades_day`,
UC-5) counts filled positions, never order attempts. A window that never fills does
not change `trades_today`.

### Alternative Flows
- **UC-10-A1: partial fill** — increments `trades_today` by 1 (a position opened),
  same as a full fill (UC-3).

### Error Flows
- **UC-10-E1: `save_live_state` write error** — if persisting fails, the in-memory
  `trades_today` is still incremented; the on-disk state may lag. Documented; the
  next successful save reconciles. Must not crash the loop.

### Edge Cases
- **UC-10-EC1: cap reached mid-retry-budget** — if a fill on attempt 2 pushes
  `trades_today` to `max_trades_day`, the NEXT window's first `place_live` call hits
  UC-5 (`skip_daily_cap`). The cap is enforced per fill, consistently.

### Data Requirements
- **Input**: `place_live` outcome, `s.trades_today`.
- **Output**: persisted `LiveState`.
- **Side Effects**: `trades_today += 1` and `save_live_state` only on fill.

---

## UC-11: Window rolls mid-retry → in-flight old-window attempt must not latch the new window

**Actor**: signal_loop + place_live
**Preconditions**: an attempt for window W is in flight (awaiting `create_ioc`); during
the await the wall clock crosses into window W+1.
**Trigger**: `place_live` for W returns after `win_key` would have advanced to W+1.

### Primary Flow (Happy Path)
1. signal_loop captures `win_key = W` at the top of the trigger block (computed from
   the snapshot taken this tick, ≈ 682) and calls `place_live` for W.
2. Because `place_live` is `await`ed inline (precondition; lines 755-771), no NEXT
   tick begins until this call returns — the loop body is single-threaded per
   iteration, so there is no truly concurrent W+1 evaluation overlapping the W await.
3. When `place_live` returns `filled` for W, signal_loop sets `fired_window =
   win_key`, where `win_key` is still the captured `W` value for this iteration (not a
   re-read of the window). The latch therefore latches W, not W+1.
4. The next loop iteration recomputes the snapshot; if the window has rolled to W+1,
   `roll_if_new` fires, `fired_window` (== W) no longer matches the new `win_key`
   (== W+1), so W+1 is unlatched and its attempt-state is freshly reset (UC-8 step 4).

**Postconditions**: a fill belonging to window W latches W only; W+1 begins unlatched
with a fresh attempt budget (INV-2 holds per-window). No fill is mis-attributed to the
wrong window.

### Alternative Flows
- **UC-11-A1: window rolls while budget exhausted** — old window W exhausted at `N`;
  on roll, attempt-state resets for W+1 (UC-8 step 4) regardless of W's outcome.

### Error Flows
- **UC-11-E1: stale `win_key` used for the ledger row** — the appended
  `window_start`/`window_end` come from `win` captured for the W iteration; they MUST
  describe W (the window the order targeted), not W+1, even if the clock has since
  advanced. Verify the record's window bounds match the order's window.

### Edge Cases
- **UC-11-EC1: latch keyed on captured value** — the latch MUST be set from the
  iteration-local `win_key` snapshot, never re-derived from `Utc::now()` after the
  await, to avoid latching the wrong window. This is the core correctness point of
  UC-11.

### Data Requirements
- **Input**: iteration-local `win_key`, `place_live` outcome, roll signal next tick.
- **Output**: latch set to the correct (captured) `win_key`; record window bounds
  describe the targeted window.
- **Side Effects**: latch consumed for W only; attempt-state reset for W+1.

---

## UC-12: Backward compatibility — loaders parse BOTH old and new records

**Actor**: ledger loaders — `load_ledger_into_dash` (`main.rs` ≈ 64-102) and
`total_pnl_from_ledger` (≈ 153-167), and the dashboard `TrigSummary`
deserialization.
**Preconditions**: a single JSONL ledger file contains a mix of pre-feature ad-hoc
live records (lacking the new fields) and post-feature `LiveTriggerRecord` rows.
**Trigger**: a process restart replays the ledger (`load_ledger_into_dash`), or the
all-time total is recomputed (`total_pnl_from_ledger`).

### Primary Flow (Happy Path)
1. The loader reads the file line by line, parsing each as `serde_json::Value`
   (`from_str`), skipping any line that fails to parse (`continue`, ≈ 71-74).
2. It switches on `v.get("kind")`. New `LiveTriggerRecord` rows MUST keep `kind ==
   "trigger"` so they continue to match the `Some("trigger")` arm (≈ 76).
3. The loader reads existing fields by name with `.as_str()/.as_f64()/.as_i64()` and
   defaults (`unwrap_or(...)`), so the new additive fields (`outcome`, `exec_entry`,
   `first_limit_price`, `requote*`, `eff`, `remaining_count`, etc.) are simply ignored
   by these loaders and cause no parse error.
4. `total_pnl_from_ledger` only sums `kind == "resolve"` rows; telemetry rows are
   ignored and do not affect the all-time total (≈ 158-161).

**Postconditions** (INV-6): a replay over a mixed file produces ZERO parse errors; old
records still populate `TrigSummary` exactly as before; new records also populate it;
the resolver PnL total is unchanged in meaning. No `TrigSummary` field is renamed or
removed; `kind` keeps its meaning.

### Alternative Flows
- **UC-12-A1: dashboard `TrigSummary` deserialization of pushed JSON** — the dashboard
  also `serde_json::from_str::<Vec<TrigSummary>>` for shadow pushes (`dashboard.rs`
  ≈ 357); `TrigSummary`'s shape is unchanged, so this keeps working. New per-fire
  outcome fields are not part of `TrigSummary` and are not pushed there.

### Error Flows
- **UC-12-E1: a malformed/truncated line** (e.g. a partial write) — `from_str` returns
  `Err`; the loader `continue`s (≈ 73). One bad line never aborts the replay; the count
  log reflects successfully parsed triggers.

### Edge Cases
- **UC-12-EC1: skip/no-fill rows loaded as dashboard triggers** — because skip,
  no-fill, partial, error, and rejected rows all carry `kind == "trigger"`, the current
  `load_ledger_into_dash` would push them all into `d.triggers` as if they were fills.
  Expected behavior MUST be defined: the loader SHOULD filter on the new `outcome`
  field (only `filled`/`partial` become dashboard triggers / pendings) OR continue to
  treat all `kind == "trigger"` rows as historical entries. The chosen behavior must be
  explicit so dashboard trade counts are not inflated by no-fills/skips. (Flagged for
  planner; the safe default for accurate stats is: only `filled`/`partial` rows — and
  legacy rows with no `outcome` field, treated as `filled` — count as triggers.)
- **UC-12-EC2: legacy record lacks `outcome`** — when `outcome` is absent (old record),
  the loader MUST treat it as a fill (legacy live records were only ever written on a
  fill), preserving historical counts.

### Data Requirements
- **Input**: mixed JSONL file (old ad-hoc + new typed records).
- **Output**: populated `Dash.triggers`; recomputed `total_pnl`.
- **Side Effects**: none (read-only replay); no write-back to the ledger (INV-7).

---

## UC-13: Serialization/append robustness across all outcomes (loop must never crash)

**Actor**: place_live + `Ledger::append`
**Preconditions**: any outcome path is taken; the serialize or file write can fail.
**Trigger**: `serde_json::to_string` returns `Err`, or `OpenOptions::open`/`writeln!`
returns `Err`.

### Primary Flow (Happy Path)
1. place_live builds a `LiveTriggerRecord` for the outcome and calls
   `Ledger::append(&rec)`.
2. If serialization succeeds and the write succeeds, the row is appended (normal case,
   covered by UC-1..UC-6).

### Error Flows
- **UC-13-E1: serialize error** — `Ledger::append` logs `"ledger serialize: {e}"` and
  returns without writing (ledger.rs ≈ 27-30). place_live continues, returns its
  outcome, signal_loop proceeds. NO panic, NO unwound state. (A `LiveTriggerRecord`
  with only serializable fields makes this near-impossible, but the path must be safe.)
- **UC-13-E2: file open error** — logged `"ledger open …: {e}"`, returns (≈ 39).
- **UC-13-E3: write error** — logged `"ledger write: {e}"`, returns (≈ 35-37).
- **UC-13-E4: poisoned mutex** — `lock().unwrap_or_else(|p| p.into_inner())` recovers
  the poisoned lock (≈ 32); the append proceeds. No panic.

**Postconditions**: under any append failure, the decision loop keeps running, the
latch decision is still driven by the returned outcome (telemetry failure does NOT
change order behavior — decoupling INV-8). At most one append is attempted per fire
(INV-1) regardless of success.

### Edge Cases
- **UC-13-EC1: append failure on a fill** — even if the `filled` row fails to write,
  `trades_today` was already incremented and the latch is still consumed; the position
  is real. The missing telemetry row is a logging gap, not a trading error.

### Data Requirements
- **Input**: a `LiveTriggerRecord`; the ledger file handle.
- **Output**: zero or one appended line.
- **Side Effects**: error log on failure; no panic; no effect on order/latch logic.

---

## Flow counts

- **Primary flows (one per use case, UC-1 … UC-13):** 13
- **Alternative flows (UC-N-Ax):** 19
  (UC-1: 2, UC-2: 1, UC-3: 1, UC-4: 2, UC-5: 1, UC-6: 4, UC-7: 2, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 0)
- **Error flows (UC-N-Ex):** 17
  (UC-1: 2, UC-2: 1, UC-3: 1, UC-4: 1, UC-5: 1, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 4)
- **Edge cases (UC-N-ECx):** 18
  (UC-1: 3, UC-2: 1, UC-3: 1, UC-4: 1, UC-5: 1, UC-6: 3, UC-7: 2, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 2, UC-13: 1)

**Total scenarios:** 67 (13 primary + 19 alternative + 17 error + 18 edge).
