# Test Cases: Live-Execution Telemetry + Window-Latch Fix

> Based on [PRD](../PRD.md) (section 1, internal id `live-exec-telemetry-latch-fix`) and [Use Cases](../use-cases/live-exec-telemetry-latch-fix_use_cases.md)

This is a Rust binary (`kalshi_rs`). All test cases below are realized as `cargo test`
**unit tests over pure functions / small helpers** — there is no HTTP API and no
browser E2E. The implementation is expected to extract the order-flow decision logic
into pure, testable helpers so that `signal_loop`/`place_live` reduce to thin glue
around them. Each test case names how it is exercised:

- **unit** — a `#[test]` over a pure helper (no network, no real order). Preferred.
- **serde** — a `serde_json` round-trip / deserialize-from-fixture test.
- **manual-shadow** — a documented manual or shadow/log-only (sandbox, no real money)
  check, used ONLY where live order RTT (`latency_ms`, real `create_ioc`) is inherent
  and cannot be made a pure unit test. Per PRD AC-6, P0b MUST be shadow-validated
  before prod regardless.

## Helpers under test (pure, extracted for testability)

These are the seams the test-writer targets. Exact names are the planner's to fix;
the contracts are fixed here:

- `LiveTriggerRecord` — typed serializable ledger record (ledger.rs). Carries the
  FR-2 fields plus an additive `outcome` enum. Non-applicable order fields are
  `Option<_>` with `#[serde(skip_serializing_if = "Option::is_none")]`.
- `Outcome` enum — `{ filled, partial, nofill, skip_daily_cap, skip_loss_stop,
  skip_band, order_error, rejected }`, serialized as snake_case string tags. Single
  `kind: "trigger"` discriminator; `outcome` is the per-fire discriminant.
- `classify_outcome(...)` — pure mapping from a `place_live` result snapshot
  (gate flags, `create_ioc` Ok/Err, HTTP status, `fill`, `remaining_count`) to an
  `Outcome`. Encodes FR-3 rules.
- `latch_decision(outcome) -> bool` — true iff `outcome ∈ {filled, partial}`
  (FR-7). The ONLY thing that consumes `fired_window`.
- `counts_as_attempt(outcome) -> bool` — true iff `outcome ∈ {nofill, order_error,
  rejected}`; false for `{skip_daily_cap, skip_loss_stop, skip_band, filled,
  partial}` (FR-9 + architect decision: fills latch, they don't "use" the budget).
- `retry_gate(attempt_count, last_attempt_ts, now, n, c) -> bool` — pure; true iff a
  new `place_live` attempt is allowed this tick: `attempt_count < n && now -
  last_attempt_ts >= c`.
- `is_dashboard_trigger(outcome: Option<Outcome>) -> bool` — loader filter
  (architect decision): true iff `outcome ∈ {filled, partial}` OR `outcome` is
  absent (legacy = filled). False for nofill / skip_* / order_error / rejected.
- `decompose_gap(entry, exec_entry, eff) -> (gap, drift, walk)` — pure; AC-2.

## Configuration knobs (architect decision)

- `RETRY_MAX_ATTEMPTS` (env, default **2** = `N`) — max order-placement attempts per
  window key.
- `RETRY_COOLDOWN_SECS` (env, default **3.0** = `C`) — minimum interval between
  attempts in the same window.
- **NFR-4 regression anchor:** `N = 1, C = 0` MUST reproduce today's exact
  single-attempt, latch-before behavior (aside from the additive telemetry rows).

---

## 1. Outcome serialization & record shape (P0 telemetry)

### 1.1 Outcome enum tags
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 1.1.1 | UC-1, UC-3 | serde: serialize `Outcome::Filled` and `Outcome::Partial` | JSON tags are exactly `"filled"` and `"partial"` (snake_case). **serde** |
| 1.1.2 | UC-2 | serde: serialize `Outcome::Nofill` | JSON tag is exactly `"nofill"`. **serde** |
| 1.1.3 | UC-5, UC-6 | serde: serialize each skip/error variant | Tags are exactly `"skip_daily_cap"`, `"skip_loss_stop"`, `"skip_band"`, `"order_error"`, `"rejected"` — snake_case, one-to-one with the FR-3 set, no extra/renamed variants. **serde** |
| 1.1.4 | UC-12 | serde: deserialize each snake_case tag back to its `Outcome` variant | Round-trips losslessly; an unknown tag is a deserialize error (no silent default to a real outcome). **serde** |

### 1.2 Discriminator & additive fields
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 1.2.1 | UC-12 | serde: serialize any `LiveTriggerRecord` | `kind == "trigger"` and `live == true` are present and unchanged from the legacy ad-hoc record. **serde** |
| 1.2.2 | UC-1 | serde: a `filled` record with all decomposition fields set | Serialized JSON contains `entry, exec_entry, orderbook_entry, first_limit_price, requote, remaining_count, fill, eff, fee, latency_ms` plus context `count, side, p, delta_from_open, window_start, window_end, market_ticker, order_id, ts, ts_iso`. **serde** |
| 1.2.3 | UC-5, UC-6 | serde: a `skip_daily_cap` record with order/fill fields `None` | Non-applicable Option fields (`exec_entry, first_limit_price, requote_limit_price, requote, remaining_count, fill, eff, fee, latency_ms, order_id`) are **omitted** from the JSON (`skip_serializing_if = Option::is_none`); context fields (`entry, orderbook_entry, side, p, delta_from_open, window_start, window_end, market_ticker, ts, ts_iso`) are still present where available. **serde** |
| 1.2.4 | UC-1 | serde: a `filled` record with no re-quote | `requote == false` is serialized; `requote_limit_price` is `None` and therefore omitted from JSON. **serde** |
| 1.2.5 | UC-4 | serde: a `filled` record that went through a re-quote | `requote == true` serialized; BOTH `first_limit_price` and `requote_limit_price` present and distinct. **serde** |

### 1.3 Gap-decomposition correctness (AC-2)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 1.3.1 | UC-1 | unit: `decompose_gap(entry, exec_entry, eff)` for `entry=0.60, exec_entry=0.63, eff=0.65` | `drift = exec_entry − entry = 0.03`, `walk = eff − exec_entry = 0.02`, `gap = eff − entry = 0.05`, and `drift + walk == gap` (within f64 epsilon). **unit** |
| 1.3.2 | UC-1-EC1 | unit: `decompose_gap` when `eff` fell back to `entry` (avg_price missing) | `gap == 0`, `walk == −drift`, sum still equals `gap`; documented degenerate case, not an error. **unit** |
| 1.3.3 | UC-1 | unit: `decompose_gap` with a negative gap (`eff < entry`, favorable fill) | `gap`, `drift`, `walk` may be negative; identity `drift + walk == gap` still holds. **unit** |

---

## 2. Outcome classification (place_live result → Outcome)

### 2.1 Fill / partial / no-fill
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 2.1.1 | UC-1 | unit: `classify_outcome` with status 201, `fill > 0`, `remaining_count == 0` | `Outcome::Filled`. **unit** |
| 2.1.2 | UC-3 | unit: `classify_outcome` with status 201, `fill > 0`, `remaining_count > 0` | `Outcome::Partial`. **unit** |
| 2.1.3 | UC-2 | unit: `classify_outcome` with status 201, `fill <= 0` | `Outcome::Nofill`. **unit** |
| 2.1.4 | UC-2-EC1 | unit: `classify_outcome` with `remaining_count` unparsable (defaults to 0 via `OrderResp`) and `fill <= 0` | `Outcome::Nofill`; no panic on parse fallback. **unit** |

### 2.2 Skips & errors (pre-order gates and post-order failures)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 2.2.1 | UC-5 | unit: `classify_outcome` when `trades_today >= max_trades_day` | `Outcome::SkipDailyCap`; classification short-circuits before any order/orderbook input is consulted. **unit** |
| 2.2.2 | UC-6-A1 | unit: `classify_outcome` when `day_pnl <= -daily_loss_stop` | `Outcome::SkipLossStop`. **unit** |
| 2.2.3 | UC-6-A2 | unit: `classify_outcome` when `!(0.50 < entry && entry <= max_entry_price)` | `Outcome::SkipBand`. **unit** |
| 2.2.4 | UC-6-A3 | unit: `classify_outcome` when first `create_ioc` returned `Err` | `Outcome::OrderError`. **unit** |
| 2.2.5 | UC-6-A4 | unit: `classify_outcome` when final HTTP `status != 201` | `Outcome::Rejected`. **unit** |
| 2.2.6 | UC-6-EC1 | unit: band edges — `entry == 0.50` → SkipBand (strict `<`); `entry == max_entry_price` → NOT skip (inclusive `<=`); `entry` just above `max_entry_price` → SkipBand | Boundary classification matches the unchanged guard (INV-3). **unit** |
| 2.2.7 | UC-5-A1 | unit: gate ordering — fresh UTC day so `trades_today` was reset to 0, cap no longer applies | `classify_outcome` does NOT return SkipDailyCap; proceeds to the next gate. **unit** |

### 2.3 Classification precedence (gate ordering)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 2.3.1 | UC-5, UC-6 | unit: when multiple skip conditions hold (cap AND loss-stop AND out-of-band) | Precedence matches `place_live` source order: daily-cap first, then loss-stop, then band — exactly one outcome returned (cap wins). **unit** |

---

## 3. One row per fire (INV-1) — append on every outcome path

### 3.1 Record-built-per-path (verified via the row builder, no network)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 3.1.1 | UC-1 | unit: row builder for a `filled` snapshot produces exactly one `LiveTriggerRecord` with `outcome == filled` and all decomposition fields populated | One record; `entry == fire.entry`, `eff` computed, `requote == false`. **unit** |
| 3.1.2 | UC-2 | unit: row builder for a `nofill` snapshot | One record, `outcome == nofill`, `eff` is `None`, order-pricing fields populated, `remaining_count` recorded. **unit** |
| 3.1.3 | UC-3 | unit: row builder for a `partial` snapshot | One record, `outcome == partial`, `remaining_count > 0`, `fill` = partial count, full decomposition present. **unit** |
| 3.1.4 | UC-5 | unit: row builder for `skip_daily_cap` | One record, `outcome == skip_daily_cap`, context fields present, order/fill fields `None`. **unit** |
| 3.1.5 | UC-6-A1 | unit: row builder for `skip_loss_stop` | One record, `outcome == skip_loss_stop`, order/fill fields `None`. **unit** |
| 3.1.6 | UC-6-A2 | unit: row builder for `skip_band` | One record, `outcome == skip_band`, order/fill fields `None`. **unit** |
| 3.1.7 | UC-6-A3 | unit: row builder for `order_error` | One record, `outcome == order_error`; `exec_entry` + `first_limit_price` populated (an order was priced), fill fields `None`. **unit** |
| 3.1.8 | UC-6-A4 | unit: row builder for `rejected` | One record, `outcome == rejected`; `latency_ms` populated where available, fill fields `None`. **unit** |
| 3.1.9 | UC-1 | unit/manual-shadow: across one `place_live` invocation per outcome, exactly one `Ledger::append` call is made | Append call-count == 1 per fire (the four skips, order-error, rejected, no-fill, and fill paths) — none returns silently. Exercised by a fake ledger sink counting appends (**unit**) and confirmed in shadow (**manual-shadow**). |

### 3.2 Append happens before the early return (the bug being fixed)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 3.2.1 | UC-2 | unit: the `nofill` path appends its row BEFORE returning (today returns silently at main.rs ≈992) | Append-sink records one `nofill` row; no early silent return. **unit** |
| 3.2.2 | UC-5, UC-6 | unit: each skip/error/rejected path appends BEFORE returning (today silent at ≈907/911/917/955/984) | Append-sink records one row per path before control returns. **unit** |

---

## 4. Pending push must never see nofill/skip rows (in-memory invariant)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 4.1.1 | UC-2 | unit: a `nofill` outcome produces NO `Pending` push and NO dashboard `TrigSummary` push | `pending` and `dash.triggers` are unchanged (the push sits after the `fill <= 0` return). **unit** |
| 4.1.2 | UC-5, UC-6 | unit: skip / order_error / rejected outcomes produce NO `Pending` and NO dashboard push | `pending` empty; dashboard untouched. **unit** |
| 4.1.3 | UC-1, UC-3 | unit: `filled`/`partial` outcomes DO push exactly one `Pending` (sized to `fill`) and one `TrigSummary` | One pending with `count == fill`, one dashboard trigger. **unit** |
| 4.1.4 | UC-3-EC1 | unit: partial fill where `fill` rounds smaller than requested | `Pending.count` and record `count` equal the actual filled count (`fill as i64`), not the requested `count`. **unit** |

---

## 5. Latch decision (P0b — consume only on confirmed fill)

### 5.1 latch_decision helper (INV-2 / FR-7)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 5.1.1 | UC-1 | unit: `latch_decision(Filled)` | `true` — window latches. **unit** |
| 5.1.2 | UC-3 | unit: `latch_decision(Partial)` | `true` — partial latches (treated as a confirmed fill). **unit** |
| 5.1.3 | UC-2 | unit: `latch_decision(Nofill)` | `false` — window NOT latched. **unit** |
| 5.1.4 | UC-5, UC-6 | unit: `latch_decision` for each skip/error/rejected | `false` for all of `skip_daily_cap, skip_loss_stop, skip_band, order_error, rejected`. **unit** |

### 5.2 Pre-await latch removed (the latch-before bug)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 5.2.1 | UC-1 | unit: simulate the trigger-block latch flow — `fired_window` is set ONLY from the returned outcome, never before the `place_live` call | After a `nofill`, `fired_window` is still unset; after a `filled`, it equals `win_key`. No pre-await assignment exists. **unit** |
| 5.2.2 | UC-1 | manual-shadow: confirm in shadow/log-only mode that the pre-await `fired_window = win_key` (old main.rs ≈754) is gone | Logs show latch set strictly after a confirmed-fill outcome (PRD AC-6). **manual-shadow** |

### 5.3 At-most-one filled position per window (INV-2 across a retry sequence)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 5.3.1 | UC-7 | unit: drive a simulated window through outcomes `[nofill, nofill, filled]` via the retry state machine | After the `filled`, `fired_window == win_key`; any subsequent tick in the same window places NO further order (latched). Exactly one latch event. **unit** |
| 5.3.2 | UC-7 | unit: drive outcomes `[nofill, filled, (would-be) filled]` — the second fill must be impossible | Once latched on the first fill, the retry gate refuses further attempts; total fills for the window == 1. **unit** |

---

## 6. Retry accounting (attempt counter, cooldown, cap)

### 6.1 counts_as_attempt — which outcomes consume the budget (FR-9 + architect)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.1.1 | UC-2, UC-7 | unit: `counts_as_attempt(Nofill)` | `true` — a no-fill consumes one attempt. **unit** |
| 6.1.2 | UC-6-A3, UC-7-E1 | unit: `counts_as_attempt(OrderError)` | `true` — an order WAS attempted. **unit** |
| 6.1.3 | UC-6-A4, UC-7-E1 | unit: `counts_as_attempt(Rejected)` | `true` — an order WAS attempted. **unit** |
| 6.1.4 | UC-5 | unit: `counts_as_attempt(SkipDailyCap)` | `false` — pre-order gate, no attempt consumed. **unit** |
| 6.1.5 | UC-6-A1, UC-6-A2 | unit: `counts_as_attempt(SkipLossStop)` and `counts_as_attempt(SkipBand)` | `false` for both — pre-order gates. **unit** |
| 6.1.6 | UC-1, UC-3 | unit: `counts_as_attempt(Filled)` / `counts_as_attempt(Partial)` | `false` — a fill latches the window; it does not "spend" a retry attempt (the latch supersedes the budget). **unit** |

### 6.2 Counter incremented AFTER place_live returns
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.2.1 | UC-7 | unit: feed a sequence of outcomes through the retry state machine; assert the counter only ticks up on `{nofill, order_error, rejected}` | After `[nofill, skip_band, order_error, rejected]`, `attempt_count == 3` (skip not counted); increment happens post-return. **unit** |
| 6.2.2 | UC-7-E1 | unit: an `order_error`/`rejected` mid-retry increments the counter and does NOT latch | `attempt_count` += 1; `fired_window` unset; retry continues if budget/cooldown allow. **unit** |

### 6.3 Re-quote is NOT a second attempt
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.3.1 | UC-4 | unit: a single `place_live` invocation that fired the first IOC AND the deeper re-quote IOC counts as exactly ONE attempt | `attempt_count` increments by at most 1 for the whole invocation, regardless of re-quote. **unit** |
| 6.3.2 | UC-6-EC3 | unit: both IOCs (first + re-quote) returned non-201 → `rejected` | Counts as exactly ONE per-window attempt (architect: one place_live = at most one attempt unit). **unit** |

### 6.4 Cooldown C blocks retry within the interval (retry_gate)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.4.1 | UC-9 | unit: `retry_gate(attempt_count=1, last_attempt_ts=T0, now=T0+0.3, n=2, c=3.0)` | `false` — cooldown active; no attempt this tick. **unit** |
| 6.4.2 | UC-9 | unit: `retry_gate(... now=T0+3.0 ..., c=3.0)` (first tick where `now − last >= C`) | `true` — attempt allowed (boundary `>=` inclusive). **unit** |
| 6.4.3 | UC-9 | unit: repeated 0.3s ticks within `C` never return `true` until `C` elapses | Across ticks T0+0.3 … T0+2.9, gate is `false`; trader does not order every tick (NFR-1). **unit** |
| 6.4.4 | UC-9-EC1 | unit: `C == 0` — gate never blocks on time | `retry_gate` time-condition is always satisfied; attempts then bounded purely by `N`. **unit** |
| 6.4.5 | UC-9-E1 | unit: clock non-monotonicity — `now < last_attempt_ts` (negative delta) | Gate returns `false` (treated as cooldown active — the safe direction); no attempt. **unit** |
| 6.4.6 | UC-9-A1 | unit: `C` larger than remaining window — second attempt never becomes eligible before roll | Gate stays `false` until window rolls; effectively single-attempt for that window; bounded, no runaway. **unit** |

### 6.5 N caps attempts per window
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.5.1 | UC-8 | unit: with `attempt_count == N`, `retry_gate` returns `false` regardless of cooldown | No further attempt while exhausted; window unlatched and fire present, still no order. **unit** |
| 6.5.2 | UC-8 | unit: drive `N` consecutive `nofill`s, then more ticks | Exactly `N` attempts placed; subsequent ticks place none until roll. **unit** |
| 6.5.3 | UC-8-A1 | unit: a `filled` arrives on attempt `N` | Budget consumed exactly at `N`; the fill latches; no "exhausted-with-no-fill" state reached. **unit** |
| 6.5.4 | UC-8-E1 | unit: `win_key` never changes (clock/window bug) — budget stays at `N` | No further orders ever placed; safe terminal state (no runaway ordering). **unit** |
| 6.5.5 | UC-9-EC1 | unit: `N == 1, C == 0` together | At most one attempt per window, no time gate — equals today's single-attempt behavior (NFR-4). **unit** |

### 6.6 Window roll resets attempt state (FR-8)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 6.6.1 | UC-8 | unit: on `win_key` change, `attempt_count` resets to 0 and `last_attempt_ts` clears | New window starts with a fresh `N` budget and no cooldown carryover. **unit** |
| 6.6.2 | UC-7-EC2, UC-11-A1 | unit: roll while budget exhausted | Attempt state for the new window resets regardless of the old window's outcome. **unit** |
| 6.6.3 | UC-8-EC1 | unit: reset keys off the SAME `win_key` (`window_start.to_rfc3339()`) used for latching | Attempt-state reset and latch clear on the same boundary; no leak across windows, no mid-window reset. **unit** |
| 6.6.4 | UC-7-EC1 | unit: fire disappears between attempts (no `res.fire` a tick) within the same window | No `place_live` call that tick; `attempt_count` and `last_attempt_ts` retained; resume if the fire returns within the same window. **unit** |

---

## 7. Retry sequencing & first-fill latch (signal_loop state machine)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 7.1.1 | UC-7 | unit: full happy retry — T0 `nofill` (count=1, no latch), cooldown blocks intermediate ticks, attempt 2 after `C` returns `filled` | Latch set on the fill; no further attempts; `attempt_count` reflects 2 attempts; exactly one fill. **unit** |
| 7.1.2 | UC-7-A1 | unit: first attempt fills | No retry; `attempt_count == 1`; latch consumed immediately (identical to UC-1). **unit** |
| 7.1.3 | UC-7-A2 | unit: `N == 1` — after a `nofill`, no retry attempted | No second attempt in the same window; one extra `nofill` telemetry row written; window stays unlatched. **unit** |
| 7.1.4 | UC-7 | unit: retries stop at the FIRST fill even with budget remaining | Sequence `[nofill, filled, …]` with `N=3`: after the fill, no 3rd attempt. **unit** |

---

## 8. trades_today increments only on fill (INV-5)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 8.1.1 | UC-10 | unit: a window of `k` `nofill` attempts then one `filled` | `trades_today` increases by exactly 1, not `k+1`. **unit** |
| 8.1.2 | UC-10 | unit: `nofill` / `order_error` / `rejected` / skip outcomes | `trades_today` unchanged for each. **unit** |
| 8.1.3 | UC-10-A1 | unit: a `partial` fill | `trades_today` += 1 (a position opened), same as a full fill. **unit** |
| 8.1.4 | UC-10-EC1 | unit: a fill on attempt 2 pushes `trades_today` to `max_trades_day` | The NEXT window's first classify returns `skip_daily_cap`; cap enforced per fill, consistently. **unit** |
| 8.1.5 | UC-10-E1 | unit: `save_live_state` write error after increment | In-memory `trades_today` still incremented; loop does not crash; on-disk lag documented, reconciled on next save. **unit** |

---

## 9. Window roll mid-retry — latch the correct window (UC-11)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 9.1.1 | UC-11 | unit: latch is set from the iteration-local captured `win_key` (W), not a post-await `Utc::now()` re-read | A fill for W latches `fired_window = W`, even if the wall clock has since advanced into W+1. **unit** |
| 9.1.2 | UC-11-EC1 | unit: assert the latch source is the captured snapshot value, never re-derived after the await | No code path latches from a freshly-read window; core correctness of UC-11. **unit** |
| 9.1.3 | UC-11 | unit: next iteration after roll — `fired_window (==W)` != new `win_key (==W+1)` | W+1 is unlatched and its attempt-state freshly reset (UC-8 step 4 / TC-6.6.1). **unit** |
| 9.1.4 | UC-11-E1 | unit: the appended row's `window_start`/`window_end` come from `win` captured for the W iteration | Record window bounds describe W (the targeted window), not W+1, even after the clock advanced. **unit** |

---

## 10. Backward compatibility — loaders parse old + new (INV-6)

### 10.1 load_ledger_into_dash filter on outcome (architect decision)
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 10.1.1 | UC-12-EC2 | serde/unit: `is_dashboard_trigger(None)` — a legacy record with NO `outcome` field | `true` — legacy live records were only written on a fill; treated as `filled`, preserving historical counts. **unit** |
| 10.1.2 | UC-12-EC1 | unit: `is_dashboard_trigger` for `filled` and `partial` | `true` — pushed into `d.triggers`. **unit** |
| 10.1.3 | UC-12-EC1 | unit: `is_dashboard_trigger` for `nofill`, `skip_daily_cap`, `skip_loss_stop`, `skip_band`, `order_error`, `rejected` | `false` — NOT pushed into `d.triggers`; dashboard trade counts not inflated by no-fills/skips. **unit** |
| 10.1.4 | UC-12-EC1 | unit: replay a mixed file (legacy fill rows + new `filled`, `partial`, `nofill`, `skip_*` rows) through `load_ledger_into_dash` | `d.triggers.len()` counts only legacy + `filled` + `partial` rows; nofill/skip rows are not counted. **unit** |

### 10.2 Mixed-file parse robustness
| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 10.2.1 | UC-12 | serde: deserialize a legacy ad-hoc live JSON line (no `outcome`, no `exec_entry`, etc.) as a `serde_json::Value` and through any typed reader | Parses with new Option fields = `None`/default; `kind == "trigger"` matches the existing arm; no error. **serde** |
| 10.2.2 | UC-12 | serde: deserialize a new `LiveTriggerRecord` JSON line | Parses; previously-consumed field names (`ts_iso, window_start, window_end, market_ticker, side, entry, count, delta_from_open, p`) are present and unchanged in meaning. **serde** |
| 10.2.3 | UC-12 | unit: replay a file mixing pre- and post-feature records through `load_ledger_into_dash` | Zero parse errors; old rows populate `TrigSummary` as before; new fill/partial rows also populate it (AC-3). **unit** |
| 10.2.4 | UC-12 | unit: `total_pnl_from_ledger` over the mixed file | Sums only `kind == "resolve"` rows; all telemetry rows (any outcome) ignored; all-time total unchanged in meaning. **unit** |
| 10.2.5 | UC-12-E1 | unit: a malformed/truncated line in the middle of the file | `from_str` errs on that line, loader `continue`s; one bad line never aborts the replay; parsed-count log correct. **unit** |
| 10.2.6 | UC-12-A1 | serde: `serde_json::from_str::<Vec<TrigSummary>>` of a dashboard shadow-push payload | `TrigSummary` shape unchanged; deserializes fine; new per-fire outcome fields are not part of `TrigSummary` and are not pushed there. **serde** |

---

## 11. Serialize/append robustness — loop never crashes (UC-13, INV-8)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 11.1.1 | UC-13, UC-1-E1, UC-2-E1, UC-3-E1, UC-4-E1, UC-5-E1, UC-6-E1, UC-7 | unit: `Ledger::append` with a record whose `Serialize` returns `Err` (forced via a test wrapper) | Logs `"ledger serialize: …"`, returns without writing; no panic; caller (place_live) still returns its outcome. **unit** |
| 11.1.2 | UC-13-E2, UC-1-E2 | unit: `Ledger::append` to an un-openable path (e.g. a directory) | Logs `"ledger open …: …"`, returns; no panic. **unit** |
| 11.1.3 | UC-13-E3 | unit: `Ledger::append` where the write fails | Logs `"ledger write: …"`, returns; no panic. **unit** |
| 11.1.4 | UC-13-E4 | unit: append through a poisoned `Mutex` | `lock().unwrap_or_else(|p| p.into_inner())` recovers; the append proceeds; no panic. **unit** |
| 11.1.5 | UC-13 | unit: latch decision is driven by the returned outcome regardless of append success/failure | A `filled` outcome whose append failed still yields `latch_decision == true`; telemetry failure does not change order behavior. **unit** |
| 11.1.6 | UC-13-EC1 | unit: append failure on a `filled` fire | `trades_today` already incremented and latch still consumed; missing row is a logging gap, not a trading error; loop continues. **unit** |
| 11.1.7 | UC-13 | unit: at most one append attempted per fire even on failure | Append-sink shows exactly one append attempt regardless of outcome (INV-1 preserved). **unit** |

---

## 12. Pricing / sizing / MIRROR unchanged (INV-3 / INV-4, AC-5)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 12.1.1 | UC-1, INV-3 | unit/guard: `exec_entry` formula — fresh ask filtered to `(0.50, 0.98]`, fallback to `entry` | A pure helper reproducing main.rs ≈927-937 returns the filtered ask, else `entry`; values match pre-feature for a fixed book fixture. **unit** |
| 12.1.2 | UC-1-A1 | unit: first-`price` formula per side — YES `(exec_entry + price_buf).min(0.99)`, NO `((1 − exec_entry) − price_buf).max(0.01)` | Helper matches main.rs ≈938-941 exactly for both sides. **unit** |
| 12.1.3 | UC-1-A2 | unit: `count` formula — `round(stake / exec_entry)` clamped to `[1, max_count]` | Helper matches main.rs ≈942-948; clamps at both ends; record `count`/`fill` reflect the clamped size. **unit** |
| 12.1.4 | UC-4, INV-3 | unit: re-quote `price` formula — YES `(exec_entry + requote_buf).min(0.97)`, NO `((1 − exec_entry) − requote_buf).max(0.03)` | Helper matches main.rs ≈962-965 exactly. **unit** |
| 12.1.5 | UC-4-EC1, UC-2-A1 | unit: re-quote guard — fires iff `status == 201 && fill <= 0 && requote_buf > price_buf` | `requote_buf == price_buf` → no re-quote (`requote == false`, behaves as UC-2); `requote_buf > price_buf` with a no-fill → re-quote issued. **unit** |
| 12.1.6 | AC-5, INV-3 | manual-shadow / documented-diff: `git diff` of main.rs ranges 927-948 and 962-965 shows the pricing/sizing formulas are byte-for-byte unchanged | Diff over those ranges shows no formula change (only extraction into helpers / added telemetry); recorded as a review artifact. **manual-shadow** |
| 12.1.7 | INV-4 | manual-shadow / documented-diff: MIRROR 1:1 signal path (main.rs ≈599-646) unchanged | Diff shows the side/entry signal path untouched. **manual-shadow** |

---

## 13. Edge cases on fill economics (UC-1 edges)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 13.1.1 | UC-1-A1 | unit: `eff` per side — YES `avg_price().unwrap_or(entry)`, NO `1 − avg_price().unwrap_or(1 − entry)`, rounded to 4 dp | Helper matches main.rs ≈995-999 for both sides. **unit** |
| 13.1.2 | UC-1-EC1 | unit: `avg_price` missing on a fill | `eff` falls back to `entry` (YES) / `1 − entry` (NO); `outcome == filled`; documented degenerate gap (TC-1.3.2). **unit** |
| 13.1.3 | UC-1-EC2 | unit: `fee` absent — `OrderResp::fee()` with `average_fee_paid == None` | Returns `0.0`; record `fee == 0.0`. **unit** |
| 13.1.4 | UC-1-EC3 | unit: `order_id` absent — `OrderResp::order_id` defaults to `""` | Record `order_id == ""` (or `None`/omitted per record shape); not an error. **unit** |

---

## 14. Re-quote outcome variants (UC-3 / UC-4 alternatives)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 14.1.1 | UC-3-A1 | unit: first IOC no-fill, re-quote partially fills | Record `requote == true`, `requote_limit_price` set, `outcome == partial`. **unit** |
| 14.1.2 | UC-4-A1 | unit: re-quote also no-fills | `outcome == nofill`, `requote == true`, `requote_limit_price` recorded; window NOT latched; eligible for bounded retry. **unit** |
| 14.1.3 | UC-4-A2 | unit: re-quote `create_ioc` returns `Err` — `if let Ok(...)` does not update status/resp/lat | Outcome derives from the FIRST response (`nofill`); `requote == true` (a re-quote was attempted); no crash. **unit** |
| 14.1.4 | UC-6-EC2 | unit: order-error on the re-quote only, first IOC was a 201 no-fill | Classified as UC-4-A2 (`nofill`), NOT `order_error` — primary status came back 201; outcome from the first response. **unit** |

---

## 15. Skip-row multiplicity (UC-5 edge — per-tick, not per-window)

| # | Use Case | Test Case | Expected Result |
|---|----------|-----------|-----------------|
| 15.1.1 | UC-5-EC1 | unit: across a multi-tick window where the cap holds and the fire persists, each tick re-evaluates `skip_daily_cap` and appends a row | Multiple `skip_daily_cap` rows may be written for one window (skips never latch); QA asserts at-least-one per tick, NOT exactly-one per window (INV-1 is per-fire/per-call, not per-window). **unit** |

---

## Coverage matrix (every UC scenario → at least one TC)

| Scenario | Mapped test case(s) |
|----------|---------------------|
| UC-1 (primary) | 1.2.2, 1.3.1, 2.1.1, 3.1.1, 4.1.3, 5.1.1, 5.2.1, 12.1.1 |
| UC-1-A1 | 12.1.2, 13.1.1 |
| UC-1-A2 | 12.1.3 |
| UC-1-E1 | 11.1.1 |
| UC-1-E2 | 11.1.2 |
| UC-1-EC1 | 1.3.2, 13.1.2 |
| UC-1-EC2 | 13.1.3 |
| UC-1-EC3 | 13.1.4 |
| UC-2 (primary) | 2.1.3, 3.1.2, 3.2.1, 4.1.1, 5.1.3, 6.1.1 |
| UC-2-A1 | 12.1.5 |
| UC-2-E1 | 11.1.1 |
| UC-2-EC1 | 2.1.4 |
| UC-3 (primary) | 1.1.1, 2.1.2, 3.1.3, 4.1.3, 5.1.2, 6.1.6 |
| UC-3-A1 | 14.1.1 |
| UC-3-E1 | 11.1.1 |
| UC-3-EC1 | 4.1.4 |
| UC-4 (primary) | 1.2.5, 6.3.1, 12.1.4 |
| UC-4-A1 | 14.1.2 |
| UC-4-A2 | 14.1.3 |
| UC-4-E1 | 11.1.1 |
| UC-4-EC1 | 12.1.5 |
| UC-5 (primary) | 1.1.3, 1.2.3, 2.2.1, 3.1.4, 4.1.2, 5.1.4, 6.1.4 |
| UC-5-A1 | 2.2.7 |
| UC-5-E1 | 11.1.1 |
| UC-5-EC1 | 15.1.1 |
| UC-6 (primary) | 1.1.3, 2.2.2–2.2.5, 5.1.4 |
| UC-6-A1 | 2.2.2, 3.1.5, 6.1.5 |
| UC-6-A2 | 2.2.3, 3.1.6, 6.1.5 |
| UC-6-A3 | 2.2.4, 3.1.7, 6.1.2 |
| UC-6-A4 | 2.2.5, 3.1.8, 6.1.3 |
| UC-6-E1 | 11.1.1 |
| UC-6-EC1 | 2.2.6 |
| UC-6-EC2 | 14.1.4 |
| UC-6-EC3 | 6.3.2 |
| UC-7 (primary) | 5.3.1, 5.3.2, 6.2.1, 7.1.1, 7.1.4, 6.1.1 |
| UC-7-A1 | 7.1.2 |
| UC-7-A2 | 7.1.3 |
| UC-7-E1 | 6.1.2, 6.1.3, 6.2.2 |
| UC-7-EC1 | 6.6.4 |
| UC-7-EC2 | 6.6.2 |
| UC-8 (primary) | 6.5.1, 6.5.2, 6.6.1 |
| UC-8-A1 | 6.5.3 |
| UC-8-E1 | 6.5.4 |
| UC-8-EC1 | 6.6.3 |
| UC-9 (primary) | 6.4.1, 6.4.2, 6.4.3 |
| UC-9-A1 | 6.4.6 |
| UC-9-E1 | 6.4.5 |
| UC-9-EC1 | 6.4.4, 6.5.5 |
| UC-10 (primary) | 8.1.1, 8.1.2 |
| UC-10-A1 | 8.1.3 |
| UC-10-E1 | 8.1.5 |
| UC-10-EC1 | 8.1.4 |
| UC-11 (primary) | 9.1.1, 9.1.3 |
| UC-11-A1 | 6.6.2 |
| UC-11-E1 | 9.1.4 |
| UC-11-EC1 | 9.1.2 |
| UC-12 (primary) | 1.1.4, 1.2.1, 10.1.4, 10.2.1–10.2.5 |
| UC-12-A1 | 10.2.6 |
| UC-12-E1 | 10.2.5 |
| UC-12-EC1 | 10.1.2, 10.1.3, 10.1.4 |
| UC-12-EC2 | 10.1.1 |
| UC-13 (primary) | 11.1.1, 11.1.5, 11.1.7 |
| UC-13-E1 | 11.1.1 |
| UC-13-E2 | 11.1.2 |
| UC-13-E3 | 11.1.3 |
| UC-13-E4 | 11.1.4 |
| UC-13-EC1 | 11.1.6 |

**Every UC scenario (67 total: 13 primary + 19 alternative + 17 error + 18 edge) maps to at least one test case. No gaps.**

## Cross-cutting invariant coverage

| Invariant | Covered by |
|-----------|-----------|
| INV-1 (one row per fire) | 3.1.1–3.1.9, 3.2.1, 3.2.2, 11.1.7 |
| INV-2 (one filled position/window) | 5.1.*, 5.3.1, 5.3.2, 9.1.1 |
| INV-3 (pricing/sizing unchanged) | 12.1.1–12.1.6, 13.1.1 |
| INV-4 (MIRROR unchanged) | 12.1.7 |
| INV-5 (trades_today only on fill) | 8.1.1–8.1.5 |
| INV-6 (additive JSON / back-compat) | 1.2.1, 10.1.*, 10.2.* |
| INV-7 (append-only) | implicit in 10.2.* (read-only replay), 11.1.* (no rewrite) |
| INV-8 (no added hot-path I/O) | 11.1.5 (telemetry decoupled from order behavior) |
