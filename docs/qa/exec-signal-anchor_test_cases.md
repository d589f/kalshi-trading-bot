# Test Cases: Execution Signal-Anchor (Re-anchor Live IOC to the Signal Price)

> Based on [PRD](../PRD.md) (section 4, internal id `exec-signal-anchor`) and [Use Cases](../use-cases/exec-signal-anchor_use_cases.md)

This is a Rust binary (`kalshi_rs`). Each test below is realized either as a `cargo test`
unit/integration test over pure functions and small helpers, or as a documented
operational verification against the prod EU box. This feature is a **real-money
order-path change** (`f1_d50cap75`, `$5`, subaccount #1) gated behind `EXEC_ANCHOR`
whose default (`ask`) is **byte-identical** to today. It reuses, unchanged, the
Section 1 (`live-exec-telemetry-latch-fix`) P0b latch + bounded-retry + telemetry rails
and the Section 3 (`dashboard-f1-live-line`) live/shadow resolve split; where a
scenario is a pure reuse of an S1/S2/S3 rail this doc **references** the corresponding
UC (`S1-UC-N` / `S2-UC-N` / `S3-UC-N`) rather than re-deriving it.

Each test case carries a **Type**:

- **UNIT** — a `#[test]` (or `serde` round-trip) over a pure helper. No network, no
  real order. Deterministic and fast. The pricing/sizing/selector math and the
  fail-open telemetry decision are all extracted to pure helpers so they are UNIT
  (architect constraints 1, 2, 6). **Constraint-2 pricing/sizing UNIT tests are
  merge-blockers.**
- **INTEGRATION** — a `cargo test` that wires several components (env → selector →
  `place_live` branch, or a `tokio::join!` timing harness with mocked futures). No
  real money.
- **OPS** — a documented operational verification on the prod EU box
  (`34.32.177.126`, systemd `kalshi-shadow-com`) or the Buffalo dashboard
  (`23.95.217.78:8890`). Used only where real `create_ioc` RTT, real fills, or a
  deploy/rollback is inherent and cannot be a pure test. Every OPS TC placing real
  orders is gated behind the §2 F1 go-live audit (`S2-UC-6`).

---

## Seams under test (pure, extracted for testability)

Exact names are the planner's to fix; the **contracts** are fixed here by the
architect's binding constraints. These are the points the test-writer targets.

- **`resolve_exec_anchor(trimmed: &str) -> Option<ExecAnchor>`** (config.rs) — the
  PURE resolver (constraint 1). Input is already trimmed; matches **case-sensitive**;
  `"ask"` → `Some(Ask)`, `"signal"` → `Some(Signal)`, any other token → `None`. No
  logging, no env, no fallback inside the resolver.
- **`select_exec_anchor(env_val: Option<&str>) -> (ExecAnchor, Option<String>)`**
  (main.rs wrapper, constraint 1) — mirrors `select_session` (`main.rs` ≈ 64-78):
  trims, then `None`/empty → `(Ask, None)` **silent**; `Some("ask")` → `(Ask, None)`;
  `Some("signal")` → `(Signal, None)`; any other non-empty → `(Ask, Some(warning))`
  where the warning names the offending value **and** the accepted set (`ask`,
  `signal`). **An unrecognized value can NEVER select `Signal` on real money.**
- **`ExecAnchor`** enum = `Ask | Signal` (constraint 1).
- **Signal pricing/sizing pure helpers** (constraint 2, merge-blockers) — extracted if
  needed for testability:
  - `signal_yes_limit(signal_entry, price_buf) = (signal_entry + price_buf).min(0.99)`
  - `signal_no_limit(signal_entry, price_buf) = ((1.0 - signal_entry) - price_buf).max(0.01)`
  - `signal_count(stake, signal_entry, max_count) = round(stake / signal_entry)` clamped `[1, max_count]`
- **Legacy `ask` pricing/sizing helpers** (constraint 8) — `exec_yes_limit` /
  `exec_no_limit` = `exec_entry ± PRICE_BUF` clamped, `exec_count = round(stake /
  exec_entry)` clamped — proven **byte-identical** to pre-feature for the byte-identity
  regression (the same formulas with `exec_entry` as the anchor).
- **`TELEMETRY_TIMEOUT_MS`** const `= 500` (constraint 3) — the telemetry
  `get_orderbook` is wrapped in `tokio::time::timeout(Duration::from_millis(
  TELEMETRY_TIMEOUT_MS), get_orderbook(...))` and joined with `create_ioc` under
  `tokio::join!`. This is the **GAP-FIXED** resolution of the UC-8 design question
  (the raw `rest.rs` client timeout is 8s ≫ order RTT). `place_live`'s return time is
  bounded by `max(order_RTT, 500ms)`.
- **`build_fill_record(...)`** (ledger.rs / main.rs, constraint 4) — **signature
  UNCHANGED**; its **4 existing tests are untouched**. In `signal` mode the record's
  `exec_entry` is **overwritten post-construction** with the joined telemetry result
  (`Some` on fetch success, `None` on failure/timeout/filter-out). In `ask` mode
  `exec_entry` remains `Some(byte-identical)` as today.
- **S1 rails reused UNCHANGED** — `classify_outcome`, `latch_decision`,
  `counts_as_attempt`, `retry_gate`, `decompose_gap(entry, exec_entry, eff)`;
  the `signal_loop` retry state (`fired_window` / `attempt_window` / `attempt_count` /
  `last_attempt_ts`). Signal-anchor does **not** change these (INV-SA6).
- **S3 rail reused UNCHANGED** — `Dash::resolve(.., live: bool, ..)` equal-entry
  disambiguation (`S3-UC-5`). Signal-anchor makes `eff == signal_entry` more common,
  so this rail becomes **load-bearing** on signal-mode data (UC-14).

## Configuration anchors

- Signal-mode prod env pair: `LIVE_TRADING=1 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92
  SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 PRICE_BUF=0.06 EXEC_ANCHOR=signal`.
- Reused P0b rails (unchanged): `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`,
  `REQUOTE_BUF=0.12`, `MAX_COUNT=15`.
- **Regression anchor (constraint 8):** `EXEC_ANCHOR` unset/`ask` ⇒ byte-identical
  legacy `ask` path (synchronous book GET, `exec_entry ± PRICE_BUF`, `round(stake/
  exec_entry)`, deeper re-quote); all existing **75** `kalshi_rs` tests pass unchanged;
  no default drift.
- **HARD bound (INV-SA7):** signal-mode Yes limit ≤ `signal_entry + PRICE_BUF`, so
  `eff ≤ signal_entry + PRICE_BUF`. At `signal_entry = 0.92`, `PRICE_BUF = 0.06` this
  is `eff ≤ 0.98`. AC-7's `≤ 0.92` is a **population expectation**, not a per-fill law.

## Architect binding constraints → test-case index

| # | Binding constraint | Test cases |
|---|--------------------|-----------|
| 1 | `ExecAnchor` enum + pure `resolve_exec_anchor` (trim, case-sensitive, unknown→None) + `select_exec_anchor` wrapper (unset/empty→`(Ask,None)` silent; known→exact; unknown→`(Ask,Some(warn))`); matrix mirrors `select_session` | TC-2.1, TC-2.3, TC-2.4, TC-3.1, TC-3.2, TC-3.3, TC-3.4, TC-3.5 |
| 2 | Signal pricing/sizing **pure math (merge-blockers)**: Yes `=(signal+buf).min(0.99)`; No `=((1-signal)-buf).max(0.01)`; `count=round(stake/signal)` clamp `[1,max]`; boundaries `0.92+0.06→0.98`, `buf 0→limit=signal`, huge buf→`0.99`, near 0.5 | TC-1.1, TC-1.2, TC-9.1, TC-9.2, TC-9.3, TC-9.4, TC-10.1, TC-12.1, TC-13.1 |
| 3 | Telemetry concurrency: `TELEMETRY_TIMEOUT_MS=500` wrapping `get_orderbook` in `tokio::join!` with `create_ioc`; book timeout/error → `exec_entry=None`, order **UNAFFECTED** (fail-open, INV-SA4); return bounded by `max(order_RTT,500ms)` | TC-1.3, TC-1.8, TC-5.1, TC-5.3, TC-8.1, TC-8.2, TC-8.3, TC-8.4 |
| 4 | `exec_entry` Option threading: `build_fill_record` signature **UNCHANGED** (4 existing tests untouched); `exec_entry` overwritten post-construction; ask mode always `Some` (byte-identity), signal mode may be `None` | TC-1.11, TC-5.1, TC-5.4, TC-7.5 |
| 5 | Re-quote **NEVER** fires in signal mode: `requote=false`, `requote_limit_price=None` on **all** signal-mode records; ask-mode re-quote branch verbatim | TC-6.1, TC-7.1, TC-7.2, TC-15.1, TC-15.2, TC-15.3 |
| 6 | Retry-anchor fixedness: P0b retry with stable `fire.entry` re-derives the **SAME** limit (no carried state, no deepening); derivation pure per-invocation | TC-4.1, TC-4.2 |
| 7 | HARD per-fill bound `eff ≤ signal_entry + PRICE_BUF` (E2E/OPS assertion); `≤0.92` is a **population** expectation, band-edge `(0.86,0.92]` may fill up to `0.98` | TC-1.10o, TC-10.1, TC-10.2 |
| 8 | Byte-identity regression: `EXEC_ANCHOR` unset → ask path **verbatim** (75 tests green; limit/count/requote/exec_entry identical); f6 default, shadow twin, dashboard untouched | TC-2.1, TC-2.2, TC-2.6, TC-2.7o, TC-16.1 |
| 9 | OPS deploy: EU drop-in `+= EXEC_ANCHOR=signal`; restart first ~60s of window; runbook records exact deploy `ts`; rollback = remove env; post-deploy watch (`eff≤signal+0.06`, drift-tail gone, no-fill/coverage); S3 equal-entry recheck | TC-1.10o, TC-4.7o, TC-8.6o, TC-14.4o, TC-16.6o, TC-D.1–TC-D.7, go-live checklist |

---

## UC-1 — Signal-mode fill at/below the anchored limit → telemetry decomposition preserved

| # | UC Scenario | Type | Test Case (precondition → steps) | Expected Result |
|---|-------------|------|----------------------------------|-----------------|
| TC-1.1 | UC-1 primary (FR-4) | UNIT | **Pre:** `EXEC_ANCHOR=signal`, `PRICE_BUF=0.06`. **Steps:** call `signal_yes_limit(signal_entry, 0.06)` over a representative set (e.g. `0.55, 0.70, 0.855, 0.90, 0.92`) and `signal_no_limit(signal_entry, 0.06)`; the value is **independent of `exec_entry`** (constraint 2, merge-blocker). | Yes limit `== (signal_entry + 0.06).min(0.99)`; No limit `== ((1 - signal_entry) - 0.06).max(0.01)`; e.g. `0.855 → Yes 0.915`, `0.92 → Yes 0.98 / No 0.02`. `exec_entry` never appears in the formula. |
| TC-1.2 | UC-1 primary (FR-5) | UNIT | **Pre:** `STAKE=5`, `MAX_COUNT=15`. **Steps:** `signal_count(5, signal_entry, 15)` over `{0.55, 0.70, 0.90, 0.92}`. | `count == round(5 / signal_entry)` clamped `[1,15]`; e.g. `0.90 → 6`, `0.92 → 5`, `0.55 → 9`. Denominator is `signal_entry`, not `exec_entry`. |
| TC-1.3 | UC-1 primary (FR-7/8, constraint 3) | UNIT | **Pre:** signal-mode `filled` fire; the joined telemetry future resolved `Ok(ask)` within 500ms. **Steps:** build the `filled` `LiveTriggerRecord`; overwrite `exec_entry` from the join result. | Exactly one row, `outcome==filled`, `session==f1_d50cap75`, `live==true`; `entry==signal_entry`, `exec_entry==Some(ask-at-order-time)`, `first_limit_price==` signal-anchored limit (TC-1.1), `requote==false`, `requote_limit_price==None`; `remaining_count==0`, `fill>0`, `eff`, `fee`, `latency_ms`, `p`/`side`/`count`/`delta_from_open`/window bounds/`market_ticker`/`ts`/`ts_iso` all populated. `decompose_gap` yields `drift==exec_entry-entry`, `walk==eff-exec_entry`, `drift+walk==gap`. |
| TC-1.4 | UC-1 primary (FR-6, INV-SA2) | INTEGRATION | **Pre:** `EXEC_ANCHOR=signal`; band gate passes. **Steps:** drive `place_live` with a mocked `create_ioc`/`get_orderbook`; inspect that no `.await` on `get_orderbook` sits on the path from the band gate to `create_ioc`; on `filled` return, `signal_loop` sets `fired_window`. | The order POST fires with **no** preceding awaited book GET (INV-SA2, AC-4); the only `get_orderbook` is inside the `tokio::join!`. `place_live` returns `filled`; `latch_decision(Filled)==true` → window latched; `trades_today+=1`; a `TrigSummary{live:true}` and a `Pending` are pushed. |
| TC-1.5 | UC-1-A1 | UNIT | **Pre:** `EXEC_ANCHOR=signal`, `delta_from_open < 0` → `Side::No`. **Steps:** compute the No limit and size a `filled` No fire. | Side `No`; `price == signal_no_limit(signal_entry, 0.06)`; still exactly one `filled` signal-mode row tagged `f1_d50cap75`, `requote==false`. |
| TC-1.6 | UC-1-A2 | UNIT | **Pre:** the ask at order time is **below** the signal-anchored limit (even below `signal_entry`). **Steps:** an IOC that crosses at that lower ask; compute `eff`. | The fill lands at the market ask → `eff < signal_entry + PRICE_BUF` (possibly `eff < signal_entry`); the favorable fill is kept as-is (see UC-14). Row is `filled`, `requote==false`. |
| TC-1.7 | UC-1-E1 | UNIT | **Pre:** signal-mode `filled` outcome. **Steps:** force a serialize/append failure inside `place_live` (reuse `S1-UC-13` append-robustness). | The loop never crashes; the latch decision still follows the returned `filled` outcome; the missing row is a logging gap, not a trading error. |
| TC-1.8 | UC-1-EC1 (constraint 3) | INTEGRATION | **Pre:** signal-mode fire; the order POST future resolves **before** the telemetry future. **Steps:** run the `tokio::join!` harness with a fast POST and a slower (but < 500ms) book GET. | The join waits for **both**; the row is built only after both resolve, so `exec_entry` reflects the completed GET (`Some`). The **order execution** is not delayed by the GET (INV-SA4); only `place_live`'s **return** waits on the join — bounded by 500ms (UC-8). |
| TC-1.9 | UC-1-EC2 | UNIT | **Pre:** signal-mode fill lands exactly at the signal price. **Steps:** compute `decompose_gap` when `eff == signal_entry` and `exec_entry ≈ signal_entry`. | `drift ≈ 0`, `walk ≈ 0`, `gap ≈ 0`. This EQUAL-entry case is common in signal mode and loads the §3 live/shadow resolve disambiguation (`S3-UC-5`) — exercised in TC-14.4. |
| TC-1.10o | UC-1 primary (AC-7, constraints 7, 9) | OPS | **Pre:** EU rollout `EXEC_ANCHOR=signal` live. **Steps:** inspect the first `filled` signal-mode `LiveTriggerRecord` rows and order routing. | Every fill satisfies the **HARD bound** `eff ≤ signal_entry + PRICE_BUF (≤ 0.98)`; the `drift` right-tail (`exec_entry - signal_entry > 6c`) disappears; population of fills above `0.92` collapses from the pre-feature `4/20` toward ~0 (dominated by entries below `0.86`); `mirror gap ≈ 0`; orders route to **subaccount #1**. Go-live measurement (constraint 9). |
| TC-1.11 | UC-1 primary (constraint 4) | UNIT | **Pre:** the existing `build_fill_record` and its 4 tests. **Steps:** call `build_fill_record` with the unchanged signature for an ask-mode fill and a signal-mode fill; then overwrite `exec_entry` post-construction. | `build_fill_record`'s signature is **unchanged** and its 4 existing tests pass untouched; ask-mode `exec_entry` stays `Some(byte-identical)`; signal-mode `exec_entry` is overwritten to `Some(joined ask)` or `None` (fetch failed) with no other field disturbed. |

---

## UC-2 — Default / `ask` mode → byte-identical legacy order flow (regression guarantee)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-2.1 | UC-2 primary (FR-1/2/3, constraints 1, 8) | UNIT | **Pre:** none. **Steps:** `select_exec_anchor(None)`; then compute `exec_yes_limit(exec_entry,0.06)`, `exec_no_limit`, `exec_count(5,exec_entry,15)` over a representative `(signal_entry, exec_entry, side)` set and compare to the legacy formulas. | `select_exec_anchor(None) == (Ask, None)`. In `ask` mode `limit == exec_entry ± PRICE_BUF` (clamped) and `count == round(stake / exec_entry)` clamped `[1,15]` — equal to legacy values for every representative tuple over `Side::Yes` and `Side::No` (AC-1). |
| TC-2.2 | UC-2 primary (FR-3/9) | UNIT | **Pre:** `ask` mode, a first-IOC no-fill with `REQUOTE_BUF > PRICE_BUF`. **Steps:** run the deeper re-quote branch (reuse `S1-UC-4`). | The re-quote still fires at `exec_entry ± REQUOTE_BUF`; `requote==true`, `requote_limit_price` populated and distinct from `first_limit_price` — the ask-mode re-quote branch is **verbatim** (constraint 8). |
| TC-2.3 | UC-2-A1 (constraint 1) | UNIT | **Steps:** `select_exec_anchor(Some("ask"))`. | `(Ask, None)` — identical to unset; explicit `ask` selects `Ask` with **no** warning (AC-2). |
| TC-2.4 | UC-2-A2 (constraint 1) | UNIT | **Steps:** `select_exec_anchor(Some(" ask "))` (surrounding whitespace) and `select_exec_anchor(Some("ASK"))` (wrong case). | `" ask "` → trimmed → `(Ask, None)`; `"ASK"` → **unrecognized** (case-sensitive) → `(Ask, Some(warning))` → UC-3. Pins the trim + exact-case rule (mirrors `select_session`). |
| TC-2.5 | UC-2-E1 | UNIT | **Pre:** `ask` mode. **Steps:** drive an `ask`-mode order error / reject / no-fill (reuse `S1-UC-6`, `S1-UC-2`). | Outcomes handled byte-identically to §1; this feature does not touch the `ask` outcome paths. |
| TC-2.6 | UC-2-EC1 (constraint 8) | INTEGRATION | **Pre:** `SESSION` unset (→ f6) AND `EXEC_ANCHOR` unset (→ ask). **Steps:** boot; drive a firing tick. | The double default: f6 gate + legacy ask execution — the current prod-for-f6 baseline, wholly unchanged. Sigma key, telemetry tag, order flow all pre-feature (INV-SA1). |
| TC-2.7o | UC-2 primary (AC-8, constraint 8) | OPS/INTEGRATION | **Steps:** run the full `kalshi_rs` suite before/after the signal-anchor changes. | All existing **75** tests pass unchanged (`main.rs`, `ledger.rs`, `config.rs`, `mirror.rs`, `dashboard.rs`, `signal.rs`, `f1_regression.rs`); the new selector/limit/sizing/timeout tests are purely additive; no default drift. |

---

## UC-3 — Unrecognized `EXEC_ANCHOR` → fail loud + fall back to `ask` (never an unintended anchor on real money)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-3.1 | UC-3 primary (constraint 1) | UNIT | **Steps:** `resolve_exec_anchor` over `{"sig","signl","SIGNAL","ask!","Signal"}` (all pre-trimmed); then `select_exec_anchor(Some(raw))` for each. | `resolve_exec_anchor` → `None` for every one (case-sensitive, closed set). `select_exec_anchor` → `(Ask, Some(warning))` where the warning **names the offending value AND the accepted set** (`ask`, `signal`). The function is pure/total — never panics (NFR-2). |
| TC-3.2 | UC-3 primary (FR-1) | INTEGRATION | **Pre:** `EXEC_ANCHOR=sig` (unrecognized). **Steps:** boot; observe the log and the selected anchor. | `main()` logs an **`error!`** fail-loud (mirroring `session_warn` at `main.rs` ≈ 513-515) naming the bad value and the accepted set, stating it **fell back to `ask`**; the process runs the byte-identical `ask` path (UC-2). A typo degrades to today's known-safe behavior, **never** to `signal` (NFR-2). |
| TC-3.3 | UC-3-A1 (constraint 1) | UNIT | **Steps:** `select_exec_anchor(Some(""))` and `select_exec_anchor(Some("  "))`. | Both → `(Ask, None)` — treated as **unset**, a **silent** default (no warning). An empty/whitespace var is "not provided", distinct from the fail-loud unrecognized case (AC-2). |
| TC-3.4 | UC-3-E1 (constraint 1) | UNIT | **Steps:** `select_exec_anchor(Some("SIGNAL"))` and `select_exec_anchor(Some("Ask"))`. | Both → `(Ask, Some(warning))` — the match is **case-sensitive** after trim, so `SIGNAL` is unrecognized → fail-loud → `ask`, **NOT** `signal`. Runbook must document the exact lowercase spellings. |
| TC-3.5 | UC-3-EC1 (constraint 1) | UNIT | **Steps:** `select_exec_anchor(Some(" signal "))`. | Trimmed to `signal` → `(Signal, None)`; leading/trailing whitespace does **not** make a correct token unrecognized (AC-2). Only a value that fails to match **after** trim reaches UC-3. |

---

## UC-4 — Signal-mode no-fill → window NOT burned → P0b retry at the SAME fixed anchor → bounded stop

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-4.1 | UC-4 primary (FR-9/10, constraint 6) | UNIT | **Pre:** `EXEC_ANCHOR=signal`, same `win_key`, `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`, ask ran past `signal_entry+PRICE_BUF`. **Steps:** attempt1→`nofill`; intermediate 0.3s ticks; attempt2 after `now-last_attempt_ts>=3`. | Attempt1: one `nofill` row f1, `requote==false`, `requote_limit_price==None` (**no deeper re-quote**, FR-9); `attempt_count==1`, `last_attempt_ts=now`, **NOT** latched (latch-on-fill only). Intermediate ticks: `retry_gate` false (no order every tick, `S1-UC-9`). Attempt2 is re-anchored at the **SAME** `signal_entry ± PRICE_BUF` (constraint 6). |
| TC-4.2 | UC-4 primary (constraint 6) | UNIT | **Steps:** invoke the signal-mode limit derivation **twice** for the same `fire.entry`/`PRICE_BUF`/`side` (simulating attempt1 and attempt2), with no shared mutable state between calls. | Both invocations return the **identical** limit and count — the derivation is **pure per-invocation**: no carried state, no progressive deepening (contrast the `ask` re-quote which deepens by `REQUOTE_BUF`). Proves the fixed-anchor property. |
| TC-4.3 | UC-4-A1 | UNIT | **Steps:** attempt1 `nofill`; attempt2 (after cooldown, same anchor) **fills**. | `filled`/`partial` row, `trades_today+=1`, `fired_window` latched — at most one filled position per window (`S1-UC-7`); no further attempts this window. |
| TC-4.4 | UC-4-A2 | UNIT | **Pre:** `RETRY_MAX_ATTEMPTS=1`. **Steps:** attempt1 `nofill`. | Single attempt per window, byte-identical to pre-P0b single-shot (`S1-NFR-4`); still a signal-mode `nofill` row with `requote==false`; window stays unlatched. |
| TC-4.5 | UC-4-E1 | UNIT | **Steps:** attempt1 `order_error`/`rejected` mid-retry. | Counts as one attempt (`counts_as_attempt` true, an order was attempted); `latch_decision` false → not latched; retry continues if budget and cooldown allow (see UC-7). |
| TC-4.6 | UC-4-EC1 | UNIT | **Steps:** window rolls between attempts (reuse `S1-UC-11`). | The in-flight old-window attempt latches only its own captured `win_key`; the new window starts with a **fresh** budget and re-anchors at the **new** window's `signal_entry`. |
| TC-4.7o | UC-4-EC2 (§4.10, constraint 9) | OPS | **Pre:** signal-mode deployed. **Steps:** over a run, measure the no-fill rate and coverage vs paper F1; compare against the ~5-7/20 fast-book residual estimate. | The residual no-fills are the **accepted** trade-off of the fixed anchor; the retry (≤2 attempts) plus the ~150-250ms earlier arrival (no pre-order GET) partially recover them. No-fill rate and paper-F1 coverage are tracked; tighter caps are known-worse (§4.1 counterfactual) so the buffer stays `0.06`. |

---

## UC-5 — Concurrent telemetry fetch fails → order proceeds, `exec_entry = None`, decomposition partially unavailable

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-5.1 | UC-5 primary (FR-8, INV-SA4, constraints 3, 4) | INTEGRATION | **Pre:** `EXEC_ANCHOR=signal`; the order POST resolves `Ok`, the joined telemetry future resolves `Err`/timeout/filtered. **Steps:** run the join harness; build the outcome row. | The order outcome is determined **only** by the POST (`filled`) — the order is **not** blocked or delayed by the fetch failure (fail-open). `exec_entry` overwritten to `None`; `entry`, `first_limit_price`, `fill`, `eff`, `fee` still recorded. `gap = eff - signal_entry` stays computable; `drift`/`walk` are **unavailable (not wrong)** for this fill. |
| TC-5.2 | UC-5-A1 | UNIT | **Steps:** the telemetry GET succeeds but the derived side ask is `≤ 0.50` or `> 0.98` (thin/locked book) → filtered out (same filter as ask-mode derive, `main.rs` ≈ 1454). | `exec_entry == None`, identical to a transport failure. The telemetry filter is the **same** `(0.50, 0.98]` predicate as ask mode. |
| TC-5.3 | UC-5-E1 (constraint 3) | INTEGRATION | **Steps:** both the POST **and** the telemetry GET fail. | Outcome is `order_error` (from the POST `Err`, `S1-UC-6`) and `exec_entry == None`. Exactly one row written; the loop does not crash (`S1-UC-13`). No `eff` (no fill) — only context fields; decomposition unavailable. |
| TC-5.4 | UC-5-EC1 (constraint 4) | UNIT | **Steps:** telemetry fails on a `nofill`. | A signal-mode `nofill` row with `exec_entry==None`: neither `drift` nor `walk` nor `eff` exists (no fill, no ask) — the maximally-degraded but still-**valid** row. QA asserts it serializes/parses and the loop continues. |
| TC-5.5 | UC-5-EC2 | UNIT | **Steps:** over a synthetic run, some fills carry `exec_entry==Some`, some `None`; compute the population `drift` mean. | Offline drift analysis **skips** the `None` rows; the population `drift` mean is computed over the `Some` subset (as in §4.1's 20-fill sample). No fill is dropped or mis-scored. |

---

## UC-6 — Signal-mode partial fill → `partial` outcome, latches, no re-quote for the remainder

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-6.1 | UC-6 primary (FR-9, constraint 5) | UNIT | **Pre:** `EXEC_ANCHOR=signal`; IOC fills `fill>0` with `remaining_count>0`. **Steps:** classify and build the row; check the re-quote branch. | `outcome==partial`; the remainder is **not** chased — `requote==false`, `requote_limit_price==None` (FR-9). One `partial` row f1 with `exec_entry` `Some`/`None` (per the GET), `fill`, `remaining_count`, `eff` over filled contracts, `fee`, `latency_ms`; `trades_today+=1`; a `Pending` sized to the **filled** count. |
| TC-6.2 | UC-6-A1 | UNIT | **Steps:** NO-side partial (`Side::No` price). | Same as TC-6.1 with the No price; still `partial`, still latches, `requote==false`. |
| TC-6.3 | UC-6-E1 | UNIT | **Steps:** append failure on the `partial` row (`S1-UC-13`). | No crash; the latch still follows the returned `partial` outcome. |
| TC-6.4 | UC-6-EC1 | UNIT | **Steps:** `fill == count` exactly → `remaining_count == 0`. | This is `filled` (UC-1), **not** `partial`. The boundary is `remaining_count > 0`. |
| TC-6.5 | UC-6-EC2 | UNIT | **Steps:** a partial latched the window, then the window rolls (`S1-UC-11`). | The partial latched; a roll starts a fresh window; **no** second position for the partially-filled window. |
| TC-6.6 | UC-6 primary (S1-UC-7) | UNIT | **Steps:** `latch_decision(Partial)` then set `fired_window`. | `true` → window latched (a partial is a confirmed fill); no further attempts this window; the remainder is dropped (INV-SA3). |

---

## UC-7 — Order API error / reject in signal mode → same outcome rows as legacy, budget consumed, no double-latch

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-7.1 | UC-7-A1 (primary sub-case, FR-9/12, constraint 5) | UNIT | **Pre:** `EXEC_ANCHOR=signal`; `create_ioc` returns `Err`. **Steps:** classify and build the row. | `outcome==order_error`; one row f1; an order **was** attempted → `counts_as_attempt` true; `latch_decision` false → not latched; `requote==false`; **no** second `create_ioc` in signal mode. `trades_today` unchanged. |
| TC-7.2 | UC-7-A2 (primary sub-case, FR-9/12, constraint 5) | UNIT | **Pre:** the IOC returns HTTP `!= 201`. **Steps:** classify and build the row. | `outcome==rejected`; one row f1; consumes one attempt; not latched; `requote==false`, so there is **never** a second `create_ioc` (`-rq`) in signal mode. |
| TC-7.3 | UC-7-A3 | UNIT | **Steps:** sequence `[order_error/rejected, filled]` across the retry state machine (budget=1 used, cooldown elapsed). | Attempt2 fills → latch consumed, one `filled` row; at most one filled position per window. |
| TC-7.4 | UC-7-E1 | UNIT | **Steps:** both attempts error/reject. | Budget exhausted at `RETRY_MAX_ATTEMPTS`; no fill; no latch; window not traded until roll (`S1-UC-8`). Safe terminal state. |
| TC-7.5 | UC-7-EC1 (INV-SA4, constraint 4) | INTEGRATION | **Steps:** the order POST fails (`order_error`/`rejected`) while the concurrent telemetry GET **succeeds**. | The row may carry `exec_entry == Some(ask-at-order-time)` **even though the order failed** — the telemetry fetch is independent of the POST (INV-SA4). `drift = exec_entry - signal_entry` is computable but there is **no `eff`** (no fill), so `walk`/`gap` are absent. QA notes this valid asymmetry (a failed order can still have a populated `exec_entry`). |
| TC-7.6 | UC-7-EC2 | UNIT | **Steps:** compare a signal-mode error row to an `ask`-mode error row. | In `ask` mode the same paths record `requote` possibly `true`; signal-mode error rows are distinguishable by `requote==false` combined with the `first_limit_price`-vs-`entry` relation (see UC-16). |
| TC-7.7 | UC-7-A1/A2 (S1) | UNIT | **Steps:** `latch_decision(OrderError)` and `latch_decision(Rejected)`. | Both `false` → `fired_window` not set; no double-latch; the window remains eligible for the remaining retry budget (concurrency invariant). |

---

## UC-8 — Book fetch slower than the order POST → join bounded by `TELEMETRY_TIMEOUT_MS` (GAP-FIXED, 500ms)

The UC-8 design question ("`get_orderbook` client timeout 8s ≫ order RTT → `join!` can
stall `place_live` up to 8s") is resolved **GAP-FIXED** per architect constraint 3: the
telemetry GET is wrapped in `tokio::time::timeout(Duration::from_millis(500),
get_orderbook(...))` (`TELEMETRY_TIMEOUT_MS`), so `place_live`'s return is bounded by
`max(order_RTT, 500ms)` and a hung book yields `exec_entry = None` promptly (UC-5).

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-8.1 | UC-8 primary (constraint 3, GAP-FIXED) | INTEGRATION | **Pre:** `EXEC_ANCHOR=signal`. **Steps:** join harness — fast POST future, telemetry future that never resolves; measure the join completion time. | The join returns at ~`500ms` (the `TELEMETRY_TIMEOUT_MS` bound), NOT at the raw 8s `rest.rs` client timeout. The telemetry branch yields `None`; the order outcome (from the POST) is unaffected. |
| TC-8.2 | UC-8 primary (NFR-1, constraint 3) | INTEGRATION | **Steps:** vary the two future completion times and assert `place_live` return time. | `place_live` wall-clock return `== max(order_RTT, 500ms)` (upper-bounded); the **order execution latency** is strictly lower than `ask` mode (the POST was not preceded by any GET, NFR-1). |
| TC-8.3 | UC-8-A1 | INTEGRATION | **Steps:** book GET faster than / equal to the POST (the common case). | The join returns at the POST RTT; no extra delay; `exec_entry == Some`. |
| TC-8.4 | UC-8-E1 (constraint 3) | INTEGRATION | **Steps:** the telemetry future resolves `Err` (or hits the 500ms wrapper) mid-flight. | `exec_entry == None` (UC-5); `place_live` returns then; the order outcome is unaffected (already determined by the POST). |
| TC-8.5 | UC-8-EC1 | INTEGRATION | **Steps:** the POST is also slow (near its 10s `orders.rs` client timeout). | The join is bounded by the **larger** effective wait — here the POST's 10s client timeout dominates (the telemetry side is already bounded at 500ms). This is a network-pathology tail, not the target case; documented for completeness. |
| TC-8.6o | UC-8 design-flag resolution (constraint 9) | OPS | **Steps:** the FR-8/audit record for the join-wait design question. | Verdict **GAP-FIXED**: telemetry GET wrapped at `TELEMETRY_TIMEOUT_MS=500` (ref TC-8.1/TC-8.2). Monitored via `latency_ms` and ledger gaps; no double-order possible (`signal_loop` single-threaded, latch/attempt semantics intact, INV-SA6). |

---

## UC-9 — `PRICE_BUF` misconfigured (0 / negative / huge) → clamp behavior of the signal-anchored limit

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-9.1 | UC-9-A1 (primary sub-case, constraint 2) | UNIT | **Steps:** `signal_yes_limit(signal_entry, 0.0)` and `signal_no_limit(signal_entry, 0.0)`. | Yes limit `== signal_entry`; No limit `== 1 - signal_entry`. `buf 0 → limit == signal` (the tight-cap regime §4.1 showed is WORSE — cuts winners); well-defined, retry (UC-4) applies; no panic. |
| TC-9.2 | UC-9-A2 (primary sub-case, constraint 2) | UNIT | **Steps:** `signal_yes_limit(signal_entry, -0.03)` and `signal_no_limit(signal_entry, -0.03)`. | Yes limit `< signal_entry` (tighter → near-certain Yes no-fill); No limit `> 1 - signal_entry` (looser). Asymmetric but well-defined; **no panic** (NFR-2). |
| TC-9.3 | UC-9-A3 (primary sub-case, constraint 2) | UNIT | **Steps:** `signal_yes_limit(signal_entry, 0.50)` for `signal_entry ≥ 0.49`. | Yes limit **clamps at `0.99`** (`(signal_entry + 0.50).min(0.99)`); No limit `== ((1 - signal_entry) - 0.50).max(0.01)`. The `.min(0.99)`/`.max(0.01)` clamp is the **only** guard — it keeps values API-valid but does NOT enforce the `0.92` cap. |
| TC-9.4 | UC-9-A4 (constraint 2) | UNIT | **Steps:** `signal_entry = 0.92`, `PRICE_BUF = 0.07` → `signal_yes_limit`. | `(0.92 + 0.07).min(0.99) == 0.99` — the clamp binds **exactly** at the API ceiling. |
| TC-9.5 | UC-9-E1 | UNIT | **Steps:** `PRICE_BUF` set non-numeric in env; parse via the existing `LiveCfg` env parse. | The bad value falls back to the `LiveCfg` default (not a panic) — existing §1/§2 env-parse behavior, unchanged by this feature. |
| TC-9.6 | UC-9-EC1 | UNIT | **Steps:** prod values `PRICE_BUF=0.06`, `signal_entry ≤ 0.92` → `signal_yes_limit`. | Yes limit `≤ 0.98 < 0.99`, so `.min(0.99)` does **not** bind. The clamp is normally **inert**; it engages only under misconfiguration (TC-9.3/9.4). |
| TC-9.7 | UC-9-EC2 (FLAG) | OPS | **Steps:** decide whether to add a startup `PRICE_BUF` range check (e.g. warn/refuse if `∉ [0.01, 0.15]`). | **Planner/architect decision, flagged for QA**: §4.9 keeps `PRICE_BUF` an unvalidated env; today only the per-order clamp guards it. Not required by §4; recorded as an open item. |

---

## UC-10 — Signal entry at the band edge `0.92` → Yes limit `0.98` exactly (HARD vs SOFT cap tension)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-10.1 | UC-10 primary (FR-4/6/11, constraints 2, 7) | UNIT | **Pre:** `signal_entry = 0.92`, `PRICE_BUF = 0.06`, `MAX_ENTRY = 0.92`, `STAKE = 5`. **Steps:** compute Yes/No limit, count, and the cents format. | Yes limit `== (0.92 + 0.06).min(0.99) == 0.98` **exactly** (clamp does NOT bind); No limit `== (0.08 - 0.06).max(0.01) == 0.02`; `count == round(5/0.92) == 5` clamp `[1,15]`; `format!("{:.2}", 0.98) == "0.98"`. Maximum reachable Yes limit in signal mode is `0.98`. |
| TC-10.2 | UC-10 primary — HARD vs SOFT cap (constraint 7, INV-SA7) | UNIT/OPS | **Steps:** assert the **per-fill HARD bound** `eff ≤ signal_entry + PRICE_BUF` for band-edge entries; treat AC-7's `≤ 0.92` as a population claim. | **HARD (always true, per-fill):** `eff ≤ signal_entry + PRICE_BUF` → at `0.92` this is `eff ≤ 0.98`, and a fill CAN land in `(0.92, 0.98]` when `signal_entry ∈ (0.86, 0.92]` and the ask is above `0.92`. **SOFT (population, OPS):** `≤ 0.92` holds empirically because most F1 entries sit well below `0.86` and signal-anchor removes the +3.79c drift. E2E MUST assert the HARD bound per-fill and treat `≤ 0.92` as a breach-rate expectation (4/20 → ~0), **NOT** a per-fill guarantee. |
| TC-10.3 | UC-10-A1 | UNIT | **Steps:** `signal_entry = 0.9201`; run the band gate. | Band gate `> 0.92` → **`skip_band`**, no order (`S2-UC-7-A1`). `0.92` is the highest entry that can reach the `0.98` Yes limit; nothing above the cap ever prices an order. |
| TC-10.4 | UC-10-E1 | UNIT | **Steps:** `MAX_ENTRY = 0.97` (misconfigured); `signal_entry = 0.97`. | Band admits up to `0.97` → Yes limit `(0.97 + 0.06).min(0.99) == 0.99` (clamp binds) → `eff ≤ 0.99`. Documents that a mis-set `MAX_ENTRY` widens the HARD bound; prod keeps `MAX_ENTRY = 0.92`. |
| TC-10.5 | UC-10-EC1 | UNIT | **Steps:** `signal_entry = 0.50` exactly; run the band gate. | The band floor is **exclusive** (`0.50 < signal_entry`) → `0.50` is **rejected** (`skip_band`/no-price). The lowest admitted entry is just above `0.50` (Yes limit just above `0.56`). |

---

## UC-11 — `EXEC_ANCHOR=signal` with f6 (`SESSION` unset) → valid orthogonal combo

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-11.1 | UC-11 primary (FR-3/4) | INTEGRATION | **Pre:** `SESSION` unset (→ f6_wait270) AND `EXEC_ANCHOR=signal`. **Steps:** boot; drive an f6 fire. | `SessionConfig::f6_wait270()` gate AND `ExecAnchor::Signal` selected — the two selectors do **not** interact. `place_live` anchors the IOC at the **f6 signal entry** (`fire.entry` for f6): `price == (f6_signal + PRICE_BUF).min(0.99)`, `count == round(stake / f6_signal)`. Anchor logic is strategy-agnostic. No cross-contamination. |
| TC-11.2 | UC-11-A1 | INTEGRATION | **Pre:** `SESSION` unset + `EXEC_ANCHOR` unset. **Steps:** boot. | The current f6 prod baseline, wholly unchanged (== TC-2.6). |
| TC-11.3 | UC-11-E1 | UNIT | **Steps:** f6 `cfg.max_entry_price > 0.92`; compute the f6 signal Yes limit. | The f6 signal Yes limit `f6_signal + PRICE_BUF` can exceed `0.98`; the `.min(0.99)` clamp still bounds it (INV-SA5). QA verifies the f6 cap value if this combo is run. |
| TC-11.4 | UC-11-EC1 | INTEGRATION | **Steps:** unrecognized `SESSION` (→ f6 fallback, `S2-UC-10`) + `EXEC_ANCHOR=signal`. | The two selectors fail/select **independently** → f6 gate (with the `SESSION` warning logged) + signal execution. `EXEC_ANCHOR=signal` still selects `Signal`. |

**Scope note (flagged):** §4.9 keeps `signal` **opt-in** — it is not turned on
automatically for f6. It does not forbid an operator manually pairing f6 + `signal`;
this UC documents that pairing as **structurally sound but untested** (§4's 20 fills
are all F1). Recommended prod pairing remains `SESSION=f1_d50cap75` + `EXEC_ANCHOR=signal`.

---

## UC-12 — Count sizing shift `round(stake/signal_entry)` vs legacy `round(stake/exec_entry)`

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-12.1 | UC-12 primary (FR-5, constraint 2) | UNIT | **Pre:** `STAKE=5`, `signal_entry < exec_entry` (drift ≥ 0). **Steps:** `signal_count(5, 0.90, 15)` vs `exec_count(5, 0.93, 15)`. | Signal `round(5/0.90) == 6`; legacy `round(5/0.93) == 5` → signal mode buys **6 vs 5** (one extra contract near the boundary); generally `signal_count ≥ legacy_count` because the signal denominator is smaller. `[1,15]` clamp unchanged. |
| TC-12.2 | UC-12-A1 | UNIT | **Steps:** a drift too small to flip rounding, e.g. `signal_entry=0.895`, `exec_entry=0.905`, both `round(5/·)==6`. | `signal_count == legacy_count` — the shift is a **near-boundary** phenomenon, not universal. |
| TC-12.3 | UC-12-E1 | UNIT | **Steps:** `MAX_COUNT = 1`; `signal_count(5, signal_entry, 1)`. | Clamp forces `count == 1` for any entry; both modes size 1; the shift disappears. Prod keeps `MAX_COUNT = 15`. |
| TC-12.4 | UC-12-EC1 | UNIT | **Steps:** `signal_count(5, 0.5001, 15)`; and note the cap binds only below ~0.34. | `round(5/0.5001) == 10 < 15` — within the `(0.50, 0.92]` F1 band the cap **never binds** (would need `signal_entry < ~0.34`). The `MAX_COUNT` cap is belt-and-suspenders for F1. |
| TC-12.5 | UC-12-EC2 | UNIT | **Steps:** `signal_count(5, 0.92, 15)` — the floor `[1, .]`. | `round(5/0.92) == 5 ≥ 1` — the floor never binds in the F1 band either. |

---

## UC-13 — Signal-anchored limit rounding to cents (`format!("{:.2}")`) — unchanged formatter

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-13.1 | UC-13 primary (FR-6, INV-SA5, constraint 2) | UNIT | **Steps:** compute `signal_yes_limit(0.855, 0.06) = 0.915` as `f64`, then `format!("{:.2}", .)`; also assert `count` uses `format!("{:.2}", .)` (`orders.rs` ≈ 114-115). | The **existing** `{:.2}` formatter rounds the signal-anchored float; the only difference from ask mode is **which float** is rounded (signal-anchored vs exec-anchored). No new rounding path (INV-SA5). |
| TC-13.2 | UC-13-A1 | UNIT | **Steps:** No side, `signal_entry = 0.855` → `signal_no_limit = 0.085`; `format!("{:.2}", 0.085)`. | Rounds the same way (`"0.09"` per the f64 representation); No-side rounding identical to Yes-side. |
| TC-13.3 | UC-13-E1 | UNIT | **Steps:** a value already clamped to `[0.01, 0.99]` → `format!("{:.2}", .)`. | Cents rounding **cannot fail** — a clamped value always formats to a valid 2-decimal string (INV-SA5). |
| TC-13.4 | UC-13-EC1 | UNIT | **Steps:** a "half-cent" nominal `0.915` stored as the nearest `f64` (`0.9149999…`); `format!("{:.2}", .)`. | Yields `"0.91"`, **not** `"0.92"` — the **same** representation behavior as legacy `ask` mode (no regression). QA asserts the cents-rounded limit is what bounds `eff`, and does NOT assume exact half-cent-up rounding. |
| TC-13.5 | UC-13-EC2 | UNIT | **Steps:** `signal_entry = 0.86`, `PRICE_BUF = 0.06` → `0.92`; `format!("{:.2}", 0.92)`. | `"0.92"` — an already-exact cents value; no rounding artifact. |

---

## UC-14 — `eff` BETTER than the signal anchor (negative drift) → favorable fill kept; equal-entry loads S3 resolve

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-14.1 | UC-14 primary (FR-8, NFR-5) | UNIT | **Pre:** the ask at order time is at/below `signal_entry`. **Steps:** an IOC that crosses at that ask; compute `eff` and `decompose_gap`. | `eff ≤ signal_entry`; if the GET succeeded, `exec_entry = Some(ask) ≤ signal_entry` → `drift = exec_entry - signal_entry` is **negative** (favorable book) and `walk ≈ 0`. The favorable fill is **kept** — the telemetry never rewrites `eff` up to the anchor; real money recorded as-filled. |
| TC-14.2 | UC-14-A1 | UNIT | **Steps:** NO-side favorable fill (No ask undercuts the No limit). | Same favorable outcome on the No side; `eff` recorded as-filled, negative `drift`. |
| TC-14.3 | UC-14-E1 | UNIT | **Steps:** a favorable fill (`eff < signal_entry`) but the telemetry GET failed (`exec_entry == None`). | `eff < signal_entry` recorded; `drift` unavailable (no `exec_entry`); the favorable `gap = eff - signal_entry < 0` is still computable without `exec_entry`. |
| TC-14.4 | UC-14-EC1 (S3-UC-5, §3 FR-7) | UNIT | **Steps:** construct a live row (`entry = eff`, `live=true`) and a shadow-twin row (`entry = signal_entry`, `live=false`) with **EQUAL** `entry` (`eff == signal_entry`), both `result==None`; call `Dash::resolve(.., live=true, ..)` and `Dash::resolve(.., live=false, ..)`. | The `live=true` resolve updates **only** the live row; the `live=false` resolve updates **only** the twin row — disambiguated by the **`live` flag, not the entry value** (`$5` live vs `$100` twin PnL). Signal-anchor makes `eff == signal_entry` common, so this equal-entry test is now **load-bearing** (QA flag) and MUST run against signal-mode data. |
| TC-14.4o | UC-14-EC1 (constraint 9) | OPS | **Steps:** on the deployed signal-mode run, verify the live and shadow lines separate correctly on equal-entry windows. | The Buffalo LIVE line (raw `$5` `lv_pnl`) and green shadow line (`twin5`-normalized) attach the correct PnL to each row even when `eff == signal_entry`; no cross-attribution. Re-checks `S3-UC-5` on signal-mode data. |

---

## UC-15 — `requote` fields in signal-mode records → `requote == false` and `requote_limit_price == None` on EVERY outcome

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-15.1 | UC-15 primary (FR-9, AC-6, constraint 5) | UNIT | **Steps:** build the `LiveTriggerRecord` for **each** signal-mode outcome (`filled`, `partial`, `nofill`, `rejected`, `order_error`) and assert the re-quote fields. | Every signal-mode row has `requote == false` and `requote_limit_price == None`; the deeper re-quote branch (`main.rs` ≈ 1491-1513) is **never entered**; only one `create_ioc` (`first_limit_price`) per `place_live` invocation. |
| TC-15.2 | UC-15-A1 (constraint 5) | UNIT | **Steps:** the `ask`-mode contrast — a no-fill then deeper re-quote (`S1-UC-4`). | In `ask` mode `requote` can be `true` with a populated `requote_limit_price`; the field's legacy meaning is **unchanged** for `ask` (the ask-mode branch is verbatim). |
| TC-15.3 | UC-15-E1 (constraint 5) | UNIT | **Steps:** search the signal-mode code path for any assignment of `requote = true`. | There is **no** path in signal mode that sets `requote` true — `requote == false` is unconditional. Any row with `requote == true` is therefore definitely an `ask`-mode row (a useful offline discriminator, UC-16). |
| TC-15.4 | UC-15-EC1 | UNIT | **Steps:** a two-row window from a P0b **retry** (attempt1 `nofill`, attempt2 `nofill`); assert neither row is a "re-quote". | Both rows have `requote == false`; the retry is a **separate `place_live` invocation** (inter-call, cooldown-gated, same fixed anchor), NOT a re-quote (intra-call, deeper price). QA must not read a two-attempt window as a re-quote; the two mechanisms are distinct. |

---

## UC-16 — Mixed-mode ledger analysis → ask-mode vs signal-mode row distinguishability (offline segmentation)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-16.1 | UC-16 primary (FR-12, §4.7, constraint 8) | UNIT | **Steps:** a JSONL file with both `ask`-mode (pre-rollout) and `signal`-mode (post-rollout) `LiveTriggerRecord` rows; parse via the resolver/dashboard loaders (`S1-UC-12`). | Both modes write the **same** record shape (no schema change, §4.7); the loaders parse both **without error**. `first_limit_price` semantics differ: signal mode `first_limit_price - entry ≈ ±PRICE_BUF` (independent of `exec_entry`); ask mode `first_limit_price - exec_entry ≈ ±PRICE_BUF`. |
| TC-16.2 | UC-16-A1 | UNIT | **Steps:** a single-mode file (all-signal or all-ask). | Needs no discrimination; every row is the deployed mode. Mixed-mode only arises across a rollout/rollback boundary. |
| TC-16.3 | UC-16-E1 | UNIT | **Steps:** a `drift ≈ 0` ask-mode row where `exec_entry ≈ entry`; run a relation-based mode classifier. | When `exec_entry ≈ entry` the two relations coincide → the row is **anchor-ambiguous**; a naive classifier can mislabel. **Mitigation:** fall back to the deploy **timestamp** (authoritative). The mislabel is analytically harmless (the limit and `eff` are the same either way). |
| TC-16.4 | UC-16-EC1 | UNIT | **Steps:** run the resolver and the dashboard LIVE-line render over a mixed file. | The resolver and dashboard consume `eff`, the `live` flag (§3), and resolved PnL — all **anchor-agnostic**. Runtime scoring and the LIVE line render identically regardless of anchor mode (§4.8, UI unchanged). |
| TC-16.5 | UC-16-EC2 | UNIT | **Steps:** rows with `exec_entry == None` (UC-5) in the mixed file; attempt the `first_limit_price`-vs-`exec_entry` discriminator. | Those rows carry no `exec_entry` relation → rely on `requote` (`true` ⟹ ask) + the deploy timestamp for mode attribution. |
| TC-16.6o | UC-16 primary (constraint 9) | OPS | **Steps:** the runbook records the exact `EXEC_ANCHOR=signal` deploy `ts`. | The deploy timestamp is the **authoritative** mixed-mode segmentation boundary: rows before it are `ask`, after are `signal`. No explicit `anchor` field exists (§4.7 forbids one); analysts must not assume a self-describing row. |

---

## Rollout / rollback (EU box) — deploy OPS (FR-13, constraint 9)

| # | Scenario | Type | Test Case | Expected Result |
|---|----------|------|-----------|-----------------|
| TC-D.1 | FR-13 deploy | OPS | **Pre:** §2 F1 go-live audit passed (`S2-UC-6`). **Steps:** append `EXEC_ANCHOR=signal` to the EU `mirror.conf` drop-in (alongside `SESSION=f1_d50cap75 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92 PRICE_BUF=0.06`); restart `kalshi-shadow-com`. | The drop-in env change is the only edit; startup log names `ExecAnchor::Signal`; no code/binary fork. |
| TC-D.2 | FR-13 restart window | OPS | **Steps:** perform the restart in the **first ~60s** of a 15-min window. | No open window is mid-order at restart; the S1 latch + persisted `live_state` prevent double-ordering a filled window (`S1-UC-19` / §3 NFR-4). |
| TC-D.3 | FR-13 record deploy ts | OPS | **Steps:** the operator records the **exact** deploy timestamp in the runbook. | The `ts` is written down as the mixed-mode ledger segmentation boundary (TC-16.6o); unambiguous ask-vs-signal split. |
| TC-D.4 | FR-13 post-deploy watch | OPS | **Steps:** over the first live fills, monitor `eff`, `drift`, no-fill rate, and coverage vs paper F1. | Fills show `eff ≤ signal_entry + 0.06`; the `drift` right-tail (`> 6c`) is gone; no-fill rate and paper-F1 coverage are tracked (TC-4.7o); breach-rate above `0.92` collapses toward ~0 (TC-1.10o). |
| TC-D.5 | FR-13 S3 equal-entry recheck | OPS | **Steps:** re-check the §3 live/shadow resolve disambiguation on signal-mode data (`eff == signal_entry` now more likely). | The LIVE and shadow lines separate correctly at equal entries (TC-14.4o); `S3-UC-5` holds on signal-mode fills. |
| TC-D.6 | FR-13 rollback | OPS | **Steps:** remove the `EXEC_ANCHOR` line (or set `=ask`); restart `kalshi-shadow-com`. | Reverts to **byte-identical** legacy `ask` behavior with **no** data migration (INV-SA1); startup log confirms `ExecAnchor::Ask`; the ledger is append-only and intact. |
| TC-D.7 | FR-13 rollback mid-window | OPS | **Steps:** roll back with an open signal-mode position still `pending`/settling. | The resolver (unchanged) settles the pending into a `ResolveRecord` regardless of the current anchor mode; rollback does not strand open positions. |

---

## Go-live operational checklist (constraint 9 — must be green before `EXEC_ANCHOR=signal`)

| Gate | TC | Blocking? |
|------|----|-----------|
| §2 F1 go-live audit already passed (prerequisite) | `S2-UC-6` / TC-6.* (live-f1) | Yes |
| Selector matrix green (`select_exec_anchor` unset/ask/signal/whitespace/unknown) | TC-2.1, TC-2.3, TC-2.4, TC-3.1, TC-3.3, TC-3.4, TC-3.5 | Yes |
| Signal pricing/sizing pure-math (merge-blocker) unit tests green | TC-1.1, TC-1.2, TC-9.1–9.4, TC-10.1, TC-12.1, TC-13.1 | Yes |
| No pre-order book GET in signal mode (code-verified) | TC-1.4 | Yes |
| Telemetry fail-open + `TELEMETRY_TIMEOUT_MS=500` bound | TC-5.1, TC-8.1, TC-8.2 | Yes |
| Re-quote never fires in signal mode (all outcomes) | TC-15.1, TC-15.3 | Yes |
| Retry re-anchors at the SAME fixed price (pure per-invocation) | TC-4.1, TC-4.2 | Yes |
| `build_fill_record` signature unchanged; 4 existing tests untouched | TC-1.11 | Yes |
| Byte-identity regression: `EXEC_ANCHOR` unset ⇒ ask verbatim; all 75 tests green | TC-2.1, TC-2.2, TC-2.7o | Yes |
| UC-8 join-wait design question resolved GAP-FIXED (500ms) | TC-8.6o | Yes |
| HARD per-fill bound asserted (`eff ≤ signal_entry + PRICE_BUF`); `≤0.92` treated as population | TC-10.2, TC-1.10o | Yes |
| EU drop-in `EXEC_ANCHOR=signal`; restart first ~60s of window | TC-D.1, TC-D.2 | Yes |
| Runbook records exact deploy `ts` (mixed-mode boundary) | TC-D.3, TC-16.6o | Yes |
| Rollback = remove env → byte-identical ask, no migration | TC-D.6 | Yes |
| Post-deploy watch: `eff≤signal+0.06`, drift-tail gone, no-fill/coverage tracked | TC-1.10o, TC-4.7o, TC-D.4 | Yes (post-first-fill) |
| S3 equal-entry resolve re-checked on signal-mode data | TC-14.4o, TC-D.5 | Yes (post-first-fill) |

---

## Coverage matrix — every UC scenario → at least one test case

| Scenario | Mapped test case(s) |
|----------|---------------------|
| UC-1 (primary) | TC-1.1, TC-1.2, TC-1.3, TC-1.4, TC-1.10o, TC-1.11 |
| UC-1-A1 | TC-1.5 |
| UC-1-A2 | TC-1.6 |
| UC-1-E1 | TC-1.7 |
| UC-1-EC1 | TC-1.8 |
| UC-1-EC2 | TC-1.9 |
| UC-2 (primary) | TC-2.1, TC-2.2, TC-2.7o |
| UC-2-A1 | TC-2.3 |
| UC-2-A2 | TC-2.4 |
| UC-2-E1 | TC-2.5 |
| UC-2-EC1 | TC-2.6 |
| UC-3 (primary) | TC-3.1, TC-3.2 |
| UC-3-A1 | TC-3.3 |
| UC-3-E1 | TC-3.4 |
| UC-3-EC1 | TC-3.5 |
| UC-4 (primary) | TC-4.1, TC-4.2 |
| UC-4-A1 | TC-4.3 |
| UC-4-A2 | TC-4.4 |
| UC-4-E1 | TC-4.5 |
| UC-4-EC1 | TC-4.6 |
| UC-4-EC2 | TC-4.7o |
| UC-5 (primary) | TC-5.1 |
| UC-5-A1 | TC-5.2 |
| UC-5-E1 | TC-5.3 |
| UC-5-EC1 | TC-5.4 |
| UC-5-EC2 | TC-5.5 |
| UC-6 (primary) | TC-6.1, TC-6.6 |
| UC-6-A1 | TC-6.2 |
| UC-6-E1 | TC-6.3 |
| UC-6-EC1 | TC-6.4 |
| UC-6-EC2 | TC-6.5 |
| UC-7 (primary: A1/A2 sub-cases) | TC-7.1, TC-7.2, TC-7.7 |
| UC-7-A3 | TC-7.3 |
| UC-7-E1 | TC-7.4 |
| UC-7-EC1 | TC-7.5 |
| UC-7-EC2 | TC-7.6 |
| UC-8 (primary) | TC-8.1, TC-8.2 |
| UC-8-A1 | TC-8.3 |
| UC-8-E1 | TC-8.4 |
| UC-8-EC1 | TC-8.5 |
| UC-9 (primary: A1/A2/A3 sub-cases) | TC-9.1, TC-9.2, TC-9.3 |
| UC-9-A4 | TC-9.4 |
| UC-9-E1 | TC-9.5 |
| UC-9-EC1 | TC-9.6 |
| UC-9-EC2 | TC-9.7 |
| UC-10 (primary) | TC-10.1, TC-10.2 |
| UC-10-A1 | TC-10.3 |
| UC-10-E1 | TC-10.4 |
| UC-10-EC1 | TC-10.5 |
| UC-11 (primary) | TC-11.1 |
| UC-11-A1 | TC-11.2 |
| UC-11-E1 | TC-11.3 |
| UC-11-EC1 | TC-11.4 |
| UC-12 (primary) | TC-12.1 |
| UC-12-A1 | TC-12.2 |
| UC-12-E1 | TC-12.3 |
| UC-12-EC1 | TC-12.4 |
| UC-12-EC2 | TC-12.5 |
| UC-13 (primary) | TC-13.1 |
| UC-13-A1 | TC-13.2 |
| UC-13-E1 | TC-13.3 |
| UC-13-EC1 | TC-13.4 |
| UC-13-EC2 | TC-13.5 |
| UC-14 (primary) | TC-14.1 |
| UC-14-A1 | TC-14.2 |
| UC-14-E1 | TC-14.3 |
| UC-14-EC1 | TC-14.4, TC-14.4o |
| UC-15 (primary) | TC-15.1 |
| UC-15-A1 | TC-15.2 |
| UC-15-E1 | TC-15.3 |
| UC-15-EC1 | TC-15.4 |
| UC-16 (primary) | TC-16.1, TC-16.6o |
| UC-16-A1 | TC-16.2 |
| UC-16-E1 | TC-16.3 |
| UC-16-EC1 | TC-16.4 |
| UC-16-EC2 | TC-16.5 |

**All 76 UC scenarios (16 primary + 20 alternative + 16 error + 24 edge) map to at
least one test case. No gaps.** (UC-7-A1/A2 and UC-9-A1/A2/A3 are lettered sub-cases
inside their primary flows, mapped where they appear — see the coverage rows above.)

## Cross-cutting invariant coverage

| Invariant | Covered by |
|-----------|-----------|
| INV-SA1 (env is the only lever; unset/ask ⇒ byte-identical legacy) | TC-2.1, TC-2.2, TC-2.6, TC-2.7o, TC-D.6 |
| INV-SA2 (no pre-order book GET in signal mode) | TC-1.4, TC-8.2 |
| INV-SA3 (fixed signal anchor; never a chase; re-quote disabled) | TC-4.1, TC-4.2, TC-6.1, TC-15.1, TC-15.4 |
| INV-SA4 (fail-open telemetry; order depends only on POST) | TC-1.8, TC-5.1, TC-5.3, TC-7.5, TC-8.1, TC-8.4 |
| INV-SA5 (safety clamp + band + cents unchanged) | TC-9.3, TC-9.4, TC-9.6, TC-10.1, TC-13.1, TC-13.3 |
| INV-SA6 (§1/§2/§3 rails unchanged; no new field/endpoint/dep) | TC-1.11, TC-4.*, TC-6.6, TC-7.7, TC-16.1, TC-16.4 |
| INV-SA7 (HARD limit bound `eff ≤ signal_entry + PRICE_BUF`) | TC-1.10o, TC-10.1, TC-10.2, TC-10.4 |

## Concurrency / retry-latch interplay (P0b, signal-mode context)

| Property | Covered by |
|----------|-----------|
| Latch consumed on fill only (nofill/error/reject do not latch) | TC-1.4, TC-4.1, TC-4.5, TC-6.6, TC-7.7 |
| Retry re-anchors at the SAME fixed price (no deepening) | TC-4.1, TC-4.2 |
| Max 2 attempts per window; bounded stop | TC-4.1, TC-7.4 |
| Cooldown blocks per-tick ordering | TC-4.1 |
| Window roll resets budget/latch | TC-4.6, TC-6.5 |
| First fill stops retries | TC-4.3, TC-7.3 |
| `join!` return bounded by `max(order_RTT, 500ms)`; no double-order | TC-8.1, TC-8.2, TC-8.5, TC-8.6o |
| Equal-entry (`eff == signal_entry`) live/shadow resolve holds | TC-14.4, TC-14.4o, TC-D.5 |
