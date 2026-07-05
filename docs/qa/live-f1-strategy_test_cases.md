# Test Cases: Live F1 Strategy Switch + Entry-Fidelity Audit

> Based on [PRD](../PRD.md) (section 2, internal id `live-f1-strategy`) and [Use Cases](../use-cases/live-f1-strategy_use_cases.md)

This is a Rust binary (`kalshi_rs`). The tests below are realized either as `cargo test`
unit/integration tests over pure functions and small helpers, or as documented
operational verification steps executed against the prod EU box. F1 reuses the
Section 1 (`live-exec-telemetry-latch-fix`) rails unchanged (`LiveTriggerRecord`,
`classify_outcome`, `latch_decision`, `counts_as_attempt`, `retry_gate`,
`decompose_gap`); where a scenario is a pure reuse of a Section 1 rail this doc
references it rather than re-deriving it.

Each test case carries a **Type**:

- **UNIT** — a `#[test]` (or `serde` round-trip) over a pure helper. No network, no
  real order. Preferred; deterministic and fast.
- **INTEGRATION** — a `cargo test` that wires several components (e.g. env → factory
  selection → gate, or a mocked mirror HTTP server → `fetch` → skip). No real money.
- **OPS** — a documented operational verification on the prod EU box
  (`34.32.177.126`, systemd `kalshi-shadow-com`) or the Buffalo dashboard
  (`23.95.217.78:8890`). Used only where live order RTT, real `create_ioc`, or a
  deploy/rollback is inherent and cannot be a pure test. Every OPS TC that places
  real orders is gated behind the FR-8 audit (UC-6).

---

## Seams under test (pure, extracted for testability)

Exact names are the planner's to fix; the **contracts** are fixed here. These are
the points the test-writer targets.

- **`resolve_session(name: &str) -> Option<SessionConfig>`** (config.rs) — the PURE
  resolver (architect constraint 1). Trims the input, matches **case-sensitive**,
  returns `None` for empty/unrecognized. Fail-loud logging and the f6 fallback live
  in `main()`, NOT in the resolver.
- **`SessionConfig::f1_d50cap75()`** (config.rs) — the F1 factory (constraint 5).
- **`SessionConfig::f6_wait270()`** (config.rs, existing) — the f6 factory.
- **session-name / coid-prefix derivation** (constraint 7) — the telemetry
  `SESSION_NAME` (main.rs ~57) and the client-order-id prefix (main.rs ~1258,
  currently hardcoded `"f6-"`) both derived from the selected session.
- **mirrored-sigma insert** (constraint 2) — `sigmas.insert(cfg.sigma_type.clone(),
  mirrored)` at main.rs ~868 (was hardcoded `"max30"`), consumed by
  `evaluate` via `s.sigmas.get(&cfg.sigma_type)` (engine.rs ~123/135) with the
  `.filter(|v| *v > 0.0).or(s.sigma)` fallback (engine.rs 125-126).
- **`extract_live_sigma(sessions_state: &Value, session_key: &str) -> Option<f64>`**
  — the pure core of `mirror::fetch` (constraint 3). Fail-closed: `Some` only for a
  present, numeric, **strictly-positive** value; `None` for missing key, missing
  field, non-numeric/null, and `<= 0.0`.
- **`resolve_mirror_session_key(session: &SessionSel, mirror_env: Option<&str>) ->
  String`** — derives the mirror key (default = selected session; `MIRROR_SESSION`
  overrides).
- **`LiveState.last_filled_window: Option<String>`** + **`initial_fired_window(state,
  current_win_key) -> Option<String>`** — the restart double-order guard
  (constraint 4). `place_live` fill path writes `last_filled_window`; boot seeds
  `fired_window` from it when it equals the current window key.
- **`evaluate(&cfg, &shared) -> EvalResult`** (engine.rs) — the gate. **Engine code
  is UNCHANGED** by this feature; the F1 tests drive it with the F1 `cfg`
  (constraint 6).
- **display value keyed by `cfg.sigma_type`** (constraint 8) — the status-log σ and
  the dashboard `LiveSnap` σ card (main.rs ~952, ~992) read the sigma under
  `cfg.sigma_type`, not the literal `"max30"`.

## Configuration anchors

- F1 env pair (prod): `LIVE_TRADING=1 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92
  SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 DAILY_LOSS_STOP=30`.
- Reused P0b rails (unchanged): `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`.
- **Regression anchor (constraint 9):** `SESSION`/`MIRROR_SESSION` unset ⇒ f6,
  byte-identical; all existing 33+ `kalshi_rs` tests pass unchanged; no default drift.

## Architect binding constraints → test-case index

| # | Binding constraint | Test cases |
|---|--------------------|-----------|
| 1 | Pure `resolve_session` matrix (trim, case-sensitive, empty/garbage→None) | TC-1.1, TC-1.11, TC-2.1, TC-2.2, TC-10.1, TC-10.4, TC-10.5 |
| 2 | Mirror sigma inserted under `cfg.sigma_type` (both max10 & max30); fix prevents `.or(s.sigma)` fallback | TC-4.1, TC-4.2, TC-4.3, TC-4.4, TC-4.5, TC-2.6 |
| 3 | `mirror::fetch` fail-closed table (positive→Some; 0/neg/NaN/string/null/absent→None) | TC-3.1, TC-3.5, TC-3.6, TC-11.1, TC-14.1, TC-14.2, TC-14.5 |
| 4 | `LiveState.last_filled_window` restart latch (serde compat, fill writes, boot seeds/clears) | TC-19.1, TC-19.2, TC-19.3, TC-19.4, TC-19.6, TC-19.7 |
| 5 | F1 factory exact params (+ `MAX_ENTRY` via `resolve_max_entry`) | TC-1.2, TC-1.5, TC-1.7, TC-1.8 |
| 6 | Gate behavior with F1 params (engine.rs UNCHANGED); boundaries; F1-vs-f6 divergence | TC-5.1, TC-9.2, TC-9.3, TC-17.1–TC-17.5, TC-7.4 |
| 7 | Session-name attribution (telemetry tag + coid prefix derived from session) | TC-1.3, TC-2.3, TC-5.2, TC-7.7, TC-8.6 |
| 8 | Display consumers keyed by `cfg.sigma_type` (σ card nonzero under F1) | TC-4.6, TC-4.7 |
| 9 | Regression: SESSION unset ⇒ f6 byte-identical; existing tests pass | TC-2.3, TC-2.7 |
| 10 | OPS/go-live gates (startup log, subaccount=1, shadow pre-flight, audit-before-live, loss-stop=30, rollback) | TC-1.4o, TC-5.9, TC-5.10, TC-6.1–TC-6.5, TC-18.4, TC-21.*, TC-22.* |

---

## UC-1 — Operator selects F1 at boot → config, log, sigma key, mirror key, attribution

| # | UC Scenario | Type | Test Case (precondition → steps) | Expected Result |
|---|-------------|------|----------------------------------|-----------------|
| TC-1.1 | UC-1 primary | UNIT | Call `resolve_session("f1_d50cap75")`. | Returns `Some(cfg)` whose identity is the F1 factory (asserted field-by-field in TC-1.2); never `None`, never the f6 config. |
| TC-1.2 | UC-1 primary | UNIT | Build `SessionConfig::f1_d50cap75()`; assert every field (constraint 5). | `kappa==0.4`, `delta_threshold==50.0`, `p_model_threshold==0.65`, `sigma_type=="max10"`, `tau_mode==Linear`, `trade_side==Both`, `max_entry_price==0.92`, `entry_wait_min==3.0`, `liq_filter==false`, `sigma_max==None`; all remaining fields equal the f6 factory defaults (no `kill_hours`, no regime blocks). |
| TC-1.3 | UC-1 primary | UNIT | Derive the telemetry `SESSION_NAME` and the coid prefix from the selected F1 session (constraint 7). | `SESSION_NAME` names `f1_d50cap75` (not `"f6_wait270_shadow"`); coid prefix is the F1-derived prefix (not the hardcoded `"f6-"`). |
| TC-1.4o | UC-1 primary (AC-1) | OPS | On the EU box with `SESSION=f1_d50cap75`, restart `kalshi-shadow-com`; read the startup config log (main.rs ~419-428). | Log shows `entry_wait=3min delta>=50 p>=0.65 sigma=max10 max_entry=0.92 stake=$5`. Go-live gate item (constraint 10). |
| TC-1.5 | UC-1 primary / step 3 | UNIT | `resolve_max_entry(Some(0.92), factory=0.92)` — env value in range `(0.5, 0.99]`. | Effective cap `== 0.92` (env stands). |
| TC-1.6 | UC-1-A1 | INTEGRATION | Set `SESSION=f1_d50cap75` and `MIRROR_SESSION=f6_wait270`; boot. | Gate uses **F1 params**; mirror key resolves to `f6_wait270`. Documented valid explicit divergence (not a bug); startup log makes the pair visible. |
| TC-1.7 | UC-1-A2 | UNIT | `MAX_ENTRY` unset → `resolve_max_entry(None, factory=0.92)`. | Falls back to factory `0.92`; log still shows `max_entry=0.92`. |
| TC-1.8 | UC-1-A3 | UNIT | `resolve_max_entry(Some(1.5), 0.92)` and `resolve_max_entry(Some(0.4), 0.92)` — out of `(0.5, 0.99]`. | Both filtered → fall back to factory `0.92`; no abort. |
| TC-1.9 | UC-1-E1 | OPS | Build guard: `cargo test` references `SessionConfig::f1_d50cap75()` (via TC-1.2). | If the factory is missing the crate does not compile → merge blocked. NFR-3 (all tests pass) is the guard; not a runtime path. |
| TC-1.10 | UC-1-EC1 | INTEGRATION | `SESSION=f1_d50cap75`, `LIVE_TRADING` unset; drive a firing tick. | F1 config is selected and gates in shadow: the loop takes the `emit_trigger` path (main.rs ~1049-1052); `place_live` is **NOT** called; no real order. Enables pre-go-live shadow validation. |
| TC-1.11 | UC-1-EC2 | UNIT | `resolve_session(" f1_d50cap75 ")` (leading/trailing whitespace) and `resolve_session("F1_D50CAP75")` (uppercase). | `" f1_d50cap75 "` → trimmed → `Some(F1)`; `"F1_D50CAP75"` → `None` (case-sensitive, no case-fold). Pins the trim + exact-case rule. |

---

## UC-2 — Default / f6 selection unchanged → byte-identical legacy behavior (regression)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-2.1 | UC-2 primary | UNIT | `resolve_session("f6_wait270")`; assert f6 fields. | `Some(f6)` with `sigma_type=="max30"`, `entry_wait_min==4.5` (270 s), `delta_threshold==20.0`, `p_model_threshold==0.60`, `kappa==0.5`. |
| TC-2.2 | UC-2 primary (unset) | UNIT | `resolve_session("")` (empty) → `None`; `main()` maps unset/`None` to `f6_wait270()`. | Empty/unset → `None` from the resolver → f6 chosen at `main()` level. Byte-identical baseline. |
| TC-2.3 | UC-2 primary | INTEGRATION | `SESSION`/`MIRROR_SESSION` unset; boot; observe factory, sigma key, mirror key, telemetry tag, coid prefix. | Selects `f6_wait270()`; sigma inserted under `"max30"`; mirror reads `sessions_state["f6_wait270"]["live_sigma"]`; telemetry tag `"f6_wait270_shadow"`; coid prefix `"f6-"`. Byte-for-byte pre-feature (INV-F1, AC-4, constraint 9). |
| TC-2.4 | UC-2-A1 | INTEGRATION | `SESSION=f6_wait270` explicitly; boot. | Identical to unset; the explicit value selects the same factory; no warning, no divergence. |
| TC-2.5 | UC-2-E1 | INTEGRATION | `MIRROR_SESSION=f6_wait270` set while `SESSION` unset; boot. | Behavior unchanged; explicit mirror key equals the default; benign restatement. |
| TC-2.6 | UC-2-EC1 | UNIT | Insert mirrored sigma `M` under `cfg.sigma_type` with `sigma_type=="max30"`; run the gate with local `s.sigma=F != M`. | `Shared.sigmas["max30"]==M`; the gate resolves sigma from `"max30"` (== `M`), NOT `realized5_pmin`. Proves the key-by-`sigma_type` refactor is value-identical for f6 (AC-2, constraint 2 legacy case). |
| TC-2.7 | UC-2 primary (NFR-3) | OPS/INTEGRATION | Run the full `kalshi_rs` suite before/after the F1 changes. | All existing 33+ tests pass unchanged; the FR-7 test is purely additive; no default drift (constraint 9). |

---

## UC-3 — MIRROR reads the selected session's `live_sigma` (H1 fix)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-3.1 | UC-3 primary | UNIT | Canned `sessions_state` JSON containing `f1_d50cap75.live_sigma = 12.3`; `extract_live_sigma(json, "f1_d50cap75")`. | Returns `Some(12.3)` — reads the **selected** session key, replacing the hardcoded `"f6_wait270"` at mirror.rs ~74 (H1). |
| TC-3.2 | UC-3 primary | INTEGRATION | Mock HTTP server serving `/api/state` + `/api/sessions_state` (F1 present, fresh `last_update`); call `mirror::fetch` with the resolved key `f1_d50cap75`. | `Some(MirrorSnap)` whose sigma field carries the F1 `live_sigma`; `age_secs` computed from `last_update`; freshness guard meaning unchanged (FR-5). No fallback to another session. |
| TC-3.3 | UC-3-A1 | UNIT | `resolve_mirror_session_key(SESSION=f1_d50cap75, MIRROR_SESSION=Some("f6_wait270"))`, then `extract_live_sigma(json, key)`. | Key resolves to `"f6_wait270"` verbatim; the extractor is key-agnostic and reads whatever session that names (operator's choice, per UC-1-A1). |
| TC-3.4 | UC-3-E1 | INTEGRATION | Mock server returns HTTP 500 (or times out, or non-JSON body) on `/api/state` or `/api/sessions_state`; call `fetch`. | `fetch` returns `None` (via `.ok()?` / `.json().await.ok()?`); caller skips the tick (UC-13). Never trades blind (INV-F4). |
| TC-3.5 | UC-3-EC1 | UNIT | `sessions_state` has `f1_d50cap75` present but WITHOUT a `live_sigma` field; `extract_live_sigma`. | Returns `None` (missing field → `None`, constraint 3) → skip tick. |
| TC-3.6 | UC-3-EC2 | UNIT | `live_sigma` present as JSON `null`; and separately as the string `"abc"`; `extract_live_sigma`. | Both → `None` (`.as_f64()` fails) → skip. A "NaN-ish" value cannot pass this point. |

---

## UC-4 — CRITICAL: mirrored sigma keyed by `sigma_type`; gate hits `max10` for F1 (unit-tested)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-4.1 | UC-4 primary (FR-7) | UNIT | `cfg.sigma_type=="max10"`, mirrored value `M`; build the `sigmas` map via the insert-under-`cfg.sigma_type` helper. | `Shared.sigmas["max10"] == M` (the mirrored value is retrievable under `"max10"`). |
| TC-4.2 | UC-4 primary (CRITICAL) | UNIT | Insert `M` under `cfg.sigma_type="max10"`; run `evaluate` **twice**, once with local fallback `s.sigma = F1val`, once with `s.sigma = F2val` (F1val ≠ F2val, both wildly different from `M`), all other inputs fixed. | The gate's resolved sigma, computed `p`, and fire/skip decision are **INVARIANT** to `s.sigma` — the `.filter(|v|*v>0.0)` passes on `M` so `.or(s.sigma)` (engine.rs 126) is never reached. Proves the fix prevents the fallback from being exercised. |
| TC-4.3 | UC-4-A1 | UNIT | Same as TC-4.1/TC-4.2 but `cfg.sigma_type=="max30"` (f6), mirrored value `M`. | `Shared.sigmas["max30"]==M`; gate uses it, invariant to `s.sigma`. Legacy path identical; the FR-7 test asserts BOTH max10 and max30 (AC-2). |
| TC-4.4 | UC-4-E1 | UNIT | Regression detector: the TC-4.1/TC-4.2 assertions are the merge-blocking guard — re-run them against an implementation that reverts the insert to a hardcoded `"max30"` while `cfg.sigma_type=="max10"`. | The assertion FAILS (`sigmas["max10"]` absent; gate output varies with `s.sigma`). Confirms the test catches any reintroduction of H2. |
| TC-4.5 | UC-4-EC1 | UNIT | Pre-fix pin: with hardcoded `insert("max30", M)` and `cfg.sigma_type="max10"`, run `evaluate` twice varying `s.sigma` (F1val vs F2val). | `get("max10")` MISSES → `.or(s.sigma)` returns the local `realized5_pmin` → the gate's `p`/decision **CHANGES** with `s.sigma`; the bot could fire a non-F1 trade. Documents the exact defect the fix removes; TC-4.2 proves it no longer occurs post-fix. |
| TC-4.6 | UC-4-EC2 | UNIT | Display helper (constraint 8): the σ value shown in the status log and dashboard `LiveSnap` reads the sigma under `cfg.sigma_type`. | Under F1 the helper returns the mirrored `max10` value (nonzero), not `0.0`; under f6 it returns the `max30` value. |
| TC-4.7 | UC-4-EC2 (AC-7) | OPS | On the deployed F1 run, view the Buffalo dashboard σ card. | The σ card shows a **nonzero** value under F1 (not `0.0`) — cosmetic display keyed off `cfg.sigma_type`. (If planner instead accepts the `0.0` display as harmless, this TC records the GAP-ACCEPTED note; trading correctness is unaffected either way.) |

---

## UC-5 — End-to-end live F1 fire → fill → telemetry → resolve → dashboard

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-5.1 | UC-5 primary (AC-1) | INTEGRATION | Drive `evaluate(&f1_cfg, &shared)` with a Shared fixture where all F1 gates pass: `elapsed_min>=3.0`, `|delta_from_open|>=50`, dual-confirm `sign(delta_open)==sign(delta_prev)`, `p=phi(0.4*snr)>=0.65` on the mirrored `max10` sigma, entry in `(0.50, 0.92]`. | The F1 gate FIRES; side = YES if `delta>=0` else NO (`trade_side=Both`); `liq_filter=false` → no `session_liq` skip. Engine.rs unchanged (constraint 6). |
| TC-5.2 | UC-5 primary (AC-6) | UNIT | Row builder for a `filled` F1 fire (reuse Section 1 `LiveTriggerRecord`). | Exactly one record, `outcome==filled`, `session` tagged `f1_d50cap75` (INV-F5, constraint 7); full decomposition present: `entry(==fire.entry)`, `exec_entry`, `orderbook_entry`, `first_limit_price`, `requote*`, `remaining_count`, `fill`, `eff`, `fee`, `latency_ms`, `p`, `side`, `count`, `delta_from_open`, window bounds, `market_ticker`, `ts`/`ts_iso`. |
| TC-5.3 | UC-5 primary | UNIT | `latch_decision(Filled)` then set `fired_window=win_key` (reuse Section 1). | `true` → window latched; a later same-window tick places no further order. |
| TC-5.4 | UC-5-A1 | INTEGRATION | Shared with `delta_from_open < 0`, all other F1 gates pass. | Side = NO; per-side entry/price formulas apply (Section 1 unchanged); still a `filled` F1 row tagged `f1_d50cap75`. |
| TC-5.5 | UC-5-A2 | UNIT | First IOC no-fill then deeper re-quote fills (reuse Section 1). | `requote==true`; both `first_limit_price` and `requote_limit_price` recorded and distinct; `outcome==filled`. (Re-quote frequency expected higher at 180 s — measured via telemetry, PRD 2.10; not asserted numerically here.) |
| TC-5.6 | UC-5-A3 | UNIT | `fill>0` and `remaining_count>0` (reuse Section 1). | `outcome==partial`; latches; `Pending` sized to the filled count; full decomposition present. |
| TC-5.7 | UC-5-E1 | UNIT | Force a serialize/append failure inside `place_live` (reuse Section 1 append-robustness). | Loop never crashes; the latch decision still follows the returned outcome; missing row is a logging gap, not a trading error. |
| TC-5.8 | UC-5-EC1 | UNIT | `decompose_gap(entry, exec_entry, eff)` on an F1 fill (reuse Section 1 AC-2). | `gap = eff − entry`, `drift = exec_entry − entry`, `walk = eff − exec_entry`, and `drift + walk == gap`. Makes F1's (unknown, PRD 2.10) 180 s slippage profile measurable per fill. |
| TC-5.9 | UC-5 primary (AC-6) | OPS | On the deployed F1 run, inspect the first `filled` `LiveTriggerRecord` rows and the order routing. | **Mirror gap** (`entry` − paper F1 entry) ≈ 0; `drift`/`walk` decompose the remaining deviation; orders route to **subaccount #1**. Go-live measurement (constraint 10). |
| TC-5.10 | UC-5 primary (AC-7) | OPS | From the first live fills, view the Buffalo dashboard. | Green live-F1 series (`shadow_com`) rendered against pink paper-F1 series (`paper_f1`); both already wired — no dashboard code change. |

---

## UC-6 — Entry-fidelity audit is the go-live gate (per-item MATCH / GAP-FIXED / GAP-ACCEPTED)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-6.1 | UC-6 primary (AC-5) | OPS | Verify the FR-8 audit document exists and carries a verdict for **all seven** items: p-model formula, entry_wait timing, threshold_gap, max_entry band, liq_filter, sigma source, execution buffers. | Each item has exactly one of MATCH / GAP-FIXED / GAP-ACCEPTED; no item left blank. Hard go-live gate (constraint 10). |
| TC-6.2 | UC-6 primary | OPS | For each verdict, verify supporting evidence. | Every GAP-FIXED points at a specific code change (+ a unit test, e.g. TC-4.*, TC-14.*, TC-19.*, TC-21.*); every GAP-ACCEPTED carries a written rationale. |
| TC-6.3 | UC-6-A1 | OPS | Confirm any buffer recommendation is recorded but not applied. | `PRICE_BUF` (prod `0.06`) / `REQUOTE_BUF` (`0.12`) are unchanged in the prod config; the recommendation is captured for a separate future change (out of scope, PRD 2.9). |
| TC-6.4 | UC-6-E1 | OPS | Attempt-to-enable check: if any item is a real GAP with no fix and no accepted rationale, `LIVE_TRADING=1` for F1 MUST NOT be enabled. | Go-live is blocked until the gap is fixed or explicitly accepted in writing (INV-F4, NFR-5). |
| TC-6.5 | UC-6-EC1 | OPS | Cross-dependency check on the sigma-source verdict. | The sigma-source item is MATCH/GAP-FIXED only if (a) FR-6 keys sigma by `sigma_type` (UC-4 fixed) AND (b) the non-positive-sigma fallback (UC-14) is fixed or GAP-ACCEPTED. A MATCH ignoring UC-14 is an incomplete verdict. |

---

## UC-7 — Paper F1 trades but the bot skips (band / daily-cap / loss-stop) → context row, no order

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-7.1 | UC-7-A1 | UNIT | `classify_outcome` when the F1 entry is outside `(0.50, 0.92]` (reuse Section 1). | `Outcome::SkipBand`; one `skip_band` row tagged `f1_d50cap75`; no order; pre-order gate → no retry attempt consumed; window not latched. |
| TC-7.2 | UC-7-A2 | UNIT | `classify_outcome` when `trades_today >= MAX_TRADES_DAY`. | `Outcome::SkipDailyCap`; one row f1; no order; no attempt consumed. |
| TC-7.3 | UC-7-A3 | UNIT | `classify_outcome` when `day_pnl <= -30`. | `Outcome::SkipLossStop`; one row f1; no order; no attempt consumed (see UC-18). |
| TC-7.4 | UC-7-A4 | UNIT | Band edge: paper fires at `entry == 0.92`; run the band gate (constraint 6, `>` vs `>=`). | `entry == 0.92` PROCEEDS (band inclusive at the cap: `entry > 0.92` is false); only `entry > 0.92` yields `skip_band`. |
| TC-7.5 | UC-7-E1 | UNIT | Ledger append fails on a skip row (reuse Section 1). | No crash; loop continues; the skip outcome still returns. |
| TC-7.6 | UC-7-EC1 | UNIT | Persistent gate + persisting F1 fire across a multi-tick window (skips don't latch). | A skip row per tick; QA asserts **at-least-one**, not exactly-one, per window. |
| TC-7.7 | UC-7 primary | UNIT | Attribution across all three skip variants (constraint 7). | Every skip row's `session` tag is `f1_d50cap75`, never `f6` (INV-F5). |

---

## UC-8 — F1 no-fill → window NOT latched → bounded retry (max 2, 3 s cooldown) → first fill latches

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-8.1 | UC-8 primary | UNIT | Retry state machine, same `win_key`, `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`: attempt1→`nofill`; intermediate 0.3 s ticks; attempt2 after `now−last_attempt_ts>=3`→`filled`. | After attempt1: one `nofill` row f1, `attempt_count==1`, `last_attempt_ts=now`, NOT latched. Intermediate ticks: `retry_gate` false (no order every tick). Attempt2 fills: `filled` row, `trades_today+=1`, `fired_window` latched; no further attempts (concurrency: latch on fill only). |
| TC-8.2 | UC-8 primary | UNIT | Attempt2 also `nofill`. | `attempt_count==2==RETRY_MAX_ATTEMPTS` → exhausted; no further attempts until window roll resets state. |
| TC-8.3 | UC-8-A1 | UNIT | `RETRY_MAX_ATTEMPTS=1` after a `nofill`. | Single attempt per window, byte-identical to pre-P0b (NFR-4); still one F1 `nofill` telemetry row; window stays unlatched. |
| TC-8.4 | UC-8-E1 | UNIT | `order_error`/`rejected` on attempt1 mid-retry. | Counts as one attempt (`counts_as_attempt`); does NOT latch (`latch_decision` false); retry continues if budget and cooldown allow. No double-latch on order error. |
| TC-8.5 | UC-8-EC1 | UNIT | Window rolls between attempts (reuse Section 1 UC-11). | The in-flight old-window attempt latches only its own captured `win_key`; the new window starts with a fresh budget and cleared cooldown. |
| TC-8.6 | UC-8 primary | UNIT | Attribution of retry rows (constraint 7). | Every `nofill` row tagged `f1_d50cap75`. |

---

## UC-9 — F1 and f6 disagree on a window → only the selected session's gate matters

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-9.1 | UC-9 primary | UNIT | Exactly one `SessionConfig` (F1) active; `evaluate(&f1_cfg, &shared)` where Δ=60 at 190 s, p=0.70. | F1 gate fires; only F1 params affect the decision; f6 thresholds are not present in the process (INV-F2). |
| TC-9.2 | UC-9 primary (constraint 6) | UNIT | Same `shared` with Δ=30 (above f6's 20, below F1's 50); run `evaluate` with the F1 cfg AND (separately) with the f6 cfg. | F1 gate does **NOT** fire (`delta.abs() < 50`); the f6 gate **WOULD** fire (`delta.abs() >= 20`). Divergence proven; the bot under F1 does not trade this window. |
| TC-9.3 | UC-9-A1 | UNIT | `shared` at `elapsed_min==3.5` (210 s), other gates pass; run F1 cfg vs f6 cfg. | F1's entry_wait gate passes (`elapsed >= 3.0`); f6's does not (`elapsed < 4.5`). In the 180–270 s band F1 may fire where f6 has not reached — intended earlier entry (rides stronger momentum, PRD 2.10). |
| TC-9.4 | UC-9-E1 | OPS | Operator intent vs actual: read the startup config log before trusting a run. | The startup log (TC-1.4o) is the authoritative statement of which gate is live; QA/ops verify its params match the intended session. No code error. |
| TC-9.5 | UC-9-EC1 | UNIT | Single `cfg` produces at most one `fire` per tick. | One `EvalResult` per tick with one side; there is no path where the bot holds both F1-YES and f6-NO for a window (only one strategy is live). |

---

## UC-10 — Unrecognized `SESSION` value → fail loud at startup (no silent f6 mis-trade)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-10.1 | UC-10 primary | UNIT | `resolve_session` over `"f1"`, `"f7_foo"`, `"f1_d50cap75x"`, and an arbitrary typo. | Each → `None` (unrecognized; the enum is closed to the two accepted values). |
| TC-10.2 | UC-10 primary | INTEGRATION | Boot with an unrecognized non-empty `SESSION` (e.g. `f7_foo`). | `main()` logs an ERROR naming the bad value AND the accepted set (`f6_wait270`, `f1_d50cap75`), then either (a) aborts, or (b) falls back to `f6_wait270` **and logs which session it selected**. A silent, unlogged fallback is forbidden (FR-1, INV-F4). |
| TC-10.3 | UC-10-A1 | INTEGRATION | If planner picks abort-on-unknown: boot with unrecognized `SESSION`. | Process exits non-zero; systemd restart-loops until the env is fixed; no orders placed. (Default recommendation is logged-fallback to f6 so the shadow stays up.) |
| TC-10.4 | UC-10-E1 | UNIT | `resolve_session("")` (empty string). | `None`, treated as **unset** → default f6 (UC-2), NOT as an unrecognized-value error. Distinguishes "not provided" from a garbage value. |
| TC-10.5 | UC-10-EC1 | UNIT | Near-miss: `resolve_session("f1_d50cap75 ")` (trailing space) and `resolve_session("F1_D50CAP75")` (casing). | Trailing space → trimmed → `Some(F1)` (matches after trim); casing → `None` → unrecognized → this UC. Runbook must call out the exact accepted spellings. |

---

## UC-11 — `MIRROR_SESSION` key absent from `/api/sessions_state` → fail closed (skip tick)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-11.1 | UC-11 primary | UNIT | Valid `sessions_state` JSON that does NOT contain the resolved key `f1_d50cap75`; `extract_live_sigma(json, "f1_d50cap75")`. | `None` (key absent, constraint 3). MUST NOT fall back to another session's sigma (FR-4). |
| TC-11.2 | UC-11 primary | INTEGRATION | `fetch` returns `None` (missing key); drive the signal_loop MIRROR match (main.rs ~887-898). | Takes the `other` arm: rate-limited (~10 s) warn log `MIRROR ... skipping tick`, dashboard reason `MIRROR WAIT`, `continue` — no `evaluate`, no order (AC-3). |
| TC-11.3 | UC-11-A1 | INTEGRATION | Key absent for several ticks, then the mock server begins exposing `f1_d50cap75.live_sigma`. | The next `fetch` succeeds; normal F1 evaluation resumes with no restart. |
| TC-11.4 | UC-11-E1 | INTEGRATION | `/api/sessions_state` itself missing/malformed (not just the key). | `fetch` → `None`, tick skipped; same fail-closed outcome as UC-3-E1 / UC-13, different cause. |
| TC-11.5 | UC-11-EC1 | OPS | `MIRROR_SESSION` names a session that does not exist (typo). | This UC fires every tick → permanent skip, bot never trades; the rate-limited log is the only symptom; ops verify the `MIRROR_SESSION` spelling against the endpoint. (A present-but-mismatched valid key is UC-1-A1 — it trades — not this UC.) |

---

## UC-12 — Mirror state stale (`age_secs > MIRROR_MAX_AGE_SECS`) → skip tick

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-12.1 | UC-12 primary | INTEGRATION | `fetch` returns `Some(m)` with `m.age_secs > mcfg.max_age_secs` (default 5 s); drive the guard at main.rs ~860. | Guard false → `other` arm: `MIRROR STALE ... skipping tick` (rate-limited), dashboard `MIRROR WAIT`, `continue` — no evaluate, no order (INV-F4). |
| TC-12.2 | UC-12-A1 | INTEGRATION | Stale snapshots, then `last_update` advances so `age_secs` drops under the threshold. | F1 evaluation resumes on the next fresh snapshot. |
| TC-12.3 | UC-12-E1 | OPS | Clock skew: the bot's clock is ahead of the paper engine's `last_update`. | `age_secs` inflates → over-skipping (safe direction — skips rather than trades stale); never a wrong trade. Ops keep NTP synced. |
| TC-12.4 | UC-12-EC1 | UNIT | `age_secs == mcfg.max_age_secs` exactly. | Guard is `<=` (inclusive) → exactly-at-threshold PASSES (not stale); evaluation proceeds. Boundary unchanged from pre-feature. |

---

## UC-13 — Paper engine down / `:8893` unreachable → no trades, no crash

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-13.1 | UC-13 primary | INTEGRATION | No server listening (connection refused) or a 3 s timeout; call `fetch`, then run several loop ticks. | `fetch` → `None`; `other` arm logs `MIRROR UNREACHABLE ... skipping tick` (rate-limited), `continue`; the loop keeps ticking at 0.3 s; the process does NOT panic/exit; retries every tick. |
| TC-13.2 | UC-13-A1 | INTEGRATION | Server drops and returns repeatedly across ticks. | Each tick independently skips or evaluates; no latch/attempt-state corruption (window roll is time-based, not fetch-based). |
| TC-13.3 | UC-13-E1 | INTEGRATION | Partial response: `/api/state` OK but `/api/sessions_state` fails (and vice versa). | Either failing `.ok()?` → `None`; whole tick skipped; no half-built `MirrorSnap` reaches the gate. |
| TC-13.4 | UC-13-EC1 | INTEGRATION | Response arrives just under the 3 s timeout but with an old `last_update`. | `fetch` succeeds; staleness is then handled by UC-12 (freshness guard), not this UC. |

---

## UC-14 — Non-positive / NaN-ish mirrored sigma → gate must not fire on the fallback sigma (fail-closed)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-14.1 | UC-14-A1 | UNIT | `live_sigma` is JSON `null` / a non-numeric string; `extract_live_sigma`. | `None` → `fetch` returns `None` → tick skipped. Correct fail-closed (INV-F4). |
| TC-14.2 | UC-14-A2 (GAP-FIXED) | UNIT | `live_sigma` is a real `0.0`; and separately a real negative (e.g. `-3.1`); `extract_live_sigma`. | Both → `None` — `mirror::fetch` rejects `live_sigma <= 0.0` (constraint 3, the FR-8 sigma-source GAP-FIXED). The non-positive value never reaches the gate/fallback. |
| TC-14.3 | UC-14-A2 (INV-F4) | UNIT | With the constraint-3 fix, feed a `0.0`/negative mirrored sigma through the full skip path. | The tick is skipped; the gate NEVER computes `p` on the local `realized5_pmin` fallback. The bot does not place a non-F1 order. |
| TC-14.4 | UC-14-EC1 | UNIT | A tiny-but-positive sigma (e.g. `0.0001`); `extract_live_sigma` then `evaluate`. | Passes `fetch` (`>0`); inflates `snr` → higher `p` → gate MORE likely to fire. Legitimate (fires), distinct from the non-positive failure mode. |
| TC-14.5 | UC-14-EC2 | UNIT | Exactly `0.0` at the fetch boundary; also assert the engine.rs `.filter(|v|*v>0.0)` semantics. | `0.0` is rejected at `fetch` (`<= 0.0` → `None`) per the fix; documents that even if it reached the gate, `*v > 0.0` is false for `0.0` so it would be dropped there too. Boundary is strict-greater. |
| TC-14.6 | UC-14-E1 (audit) | OPS | The FR-8 sigma-source audit item records the resolution. | Verdict is GAP-FIXED (`fetch` rejects `<= 0.0`, ref TC-14.2/TC-14.3) OR GAP-ACCEPTED (written rationale that `live_sigma` is empirically always `>0` + monitoring). Go-live blocker until resolved (UC-6-EC1). |

---

## UC-15 — Order API error / reject under F1 → outcome row, retry budget consumed, no double-latch

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-15.1 | UC-15-A1 | UNIT | `classify_outcome` with first `create_ioc` → `Err` (reuse Section 1). | `Outcome::OrderError`; one `order_error` row tagged `f1_d50cap75`; `counts_as_attempt` true (an order was attempted); `latch_decision` false. |
| TC-15.2 | UC-15-A2 | UNIT | `classify_outcome` with final HTTP status `!= 201`. | `Outcome::Rejected`; one `rejected` row f1; `counts_as_attempt` true; no latch. |
| TC-15.3 | UC-15-A1/A2 | UNIT | `latch_decision(OrderError)` and `latch_decision(Rejected)`. | Both `false` → `fired_window` not set; no double-latch; the window remains eligible for the remaining retry budget (concurrency invariant). |
| TC-15.4 | UC-15-A3 | UNIT | Sequence `[order_error, filled]` across the retry state machine (budget=1 used, cooldown elapsed). | Attempt2 fills → latch consumed, one `filled` row; at most one filled position per window. |
| TC-15.5 | UC-15-E1 | UNIT | Sequence `[order_error, rejected]` (both attempts fail). | Budget exhausted at `RETRY_MAX_ATTEMPTS`; no fill; no latch; window not traded until roll. Safe terminal state. |
| TC-15.6 | UC-15-EC1 | UNIT | Both IOCs (first + re-quote) in one `place_live` return non-201 → `rejected`. | Counts as exactly ONE per-window attempt (one `place_live` invocation = one attempt unit), per Section 1. |

---

## UC-16 — Boot / first-eval mid-window at `elapsed > 180 s` → may fire immediately

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-16.1 | UC-16 primary | INTEGRATION | Fresh process (`fired_window=None`); first fresh `MirrorSnap` at `elapsed_min>3.0` (e.g. 200 s), all other F1 gates pass. | The F1 gate fires immediately on this first eligible tick; `place_live` runs and may fill (a late, worse entry). Allowed — identical to f6's behavior at its 270 s wait; no "too late in window" suppression (INV-F6). |
| TC-16.2 | UC-16-A1 | OPS | Paper entered at 180 s; bot boots at 200 s. | The bot's late entry books a different (later) price; the dashboard shows the live-F1 point offset from the paper-F1 point for that window; the offset is measured via the entry decomposition (elevated `drift`). |
| TC-16.3 | UC-16-A2 | UNIT | Bot boots after the paper entry but the fire has passed (Δ fell below 50, or p dropped). | The F1 gate no longer fires; no order. Only a currently-passing signal fires. |
| TC-16.4 | UC-16-E1 | UNIT | Window rolls immediately after boot (first evaluable window is W+1, fresh). | Normal UC-5 flow for W+1; no late-entry concern. |
| TC-16.5 | UC-16-EC1 | UNIT | Extreme late entry: fire at 880 s of a 900 s window with gates passing. | Still allowed (same as f6 today); no hard cutoff added by this feature. The audit's execution-buffer item may note late-window fills as a slippage contributor. |

---

## UC-17 — Gate boundary values (elapsed 180.0 s, Δ = 50, p = 0.65, entry = 0.92)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-17.1 | UC-17-A1 | UNIT | `evaluate` with `elapsed_min == 3.0` exactly (180.0 s), F1 cfg (constraint 6). | Entry_wait gate `elapsed < entry_wait` → `3.0 < 3.0` is FALSE → PASSES at exactly 180 s (inclusive). Matches paper's "enter at exactly 180 s". |
| TC-17.2 | UC-17-A2 | UNIT | `|delta_from_open| == 50.0` exactly. | `delta.abs() < 50.0` → `50 < 50` FALSE → not "DELTA LOW" → PASSES (inclusive at 50). |
| TC-17.3 | UC-17-A3 | UNIT | `p == 0.65` exactly. | `pv < 0.65` → `0.65 < 0.65` FALSE → not "P-LOW" → PASSES (inclusive at 0.65). |
| TC-17.4 | UC-17-A4 | UNIT | `entry == 0.92` exactly; and separately `entry == 0.50`. | `entry == 0.92`: `entry > 0.92` FALSE → PASSES (inclusive at the cap). `entry == 0.50`: the `entry <= 0.5` floor path rejects (exclusive at the floor). |
| TC-17.5 | UC-17-A5 | UNIT | Just beyond each boundary: `elapsed=179.9 s`; `Δ=49.99`; `p=0.6499`; `entry=0.9201`. | `elapsed` → WAIT (no fire); `Δ` → DELTA LOW; `p` → P-LOW; `entry` → HIGH (`skip_band`). Each is the "does not fire" side (constraint 6). |
| TC-17.6 | UC-17-E1 | OPS | Float representation at the boundary: paper "exactly 50" may be `49.9999997` vs Rust `50.0000002`. | The FR-8 audit decides whether sub-epsilon differences can flip a fire/skip at the boundary and, if so, quantifies/accepts it (a GAP-ACCEPTED boundary-jitter note is acceptable; the exactly-on-boundary volume is tiny). |
| TC-17.7 | UC-17-EC1 | UNIT | Sigma/price invalid so `p_model_classic` returns `None`; run the p-gate. | `None`-p PASSES (engine.rs 152-157). Combined with UC-14: a fallback-to-local sigma could still yield `Some(p)` on the wrong formula — reinforcing why UC-14 MUST fail closed (TC-14.2/14.3) rather than relying on a `None`-p skip. |

---

## UC-18 — Daily loss stop ($30) crossed mid-day → subsequent triggers skip with `skip_loss_stop`

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-18.1 | UC-18 primary | UNIT | `classify_outcome` with `day_pnl <= -30` (`DAILY_LOSS_STOP=30`) on the next F1 fire. | `Outcome::SkipLossStop`; one row f1; no order; pre-order gate → no retry attempt consumed. |
| TC-18.2 | UC-18 primary | UNIT | Multiple ticks after the stop is crossed, fire persisting. | Every subsequent fire writes another `skip_loss_stop` row and places no order (skips don't latch; re-evaluated per tick). |
| TC-18.3 | UC-18-A1 | UNIT | UTC-day reset zeroes `day_pnl`/`trades_today` (reuse Section 1). | The stop clears; F1 trading resumes on the next fire. |
| TC-18.4 | UC-18-A2 | OPS | Deployed env check for the loss-stop value. | If `DAILY_LOSS_STOP=100` is deployed it is 68% of the $147.73 balance (too large, PRD 2.10). The runbook mandates `30` for F1; QA verifies the deployed drop-in env value (constraint 10). |
| TC-18.5 | UC-18-E1 | UNIT | `day_pnl` accounting lag: a fill's settlement not yet resolved when the next fire evaluates. | The stop trips one window late; acceptable (bounded by $5/window); documented. |
| TC-18.6 | UC-18-EC1 | UNIT | Exactly `day_pnl == -30`. | Gate `day_pnl <= -30` → `-30 <= -30` TRUE → stop trips at exactly `-$30` (inclusive). |

---

## UC-19 — Deploy / restart mid-window (latch state lost) → must not double-order a filled window

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-19.1 | UC-19-E1 (GAP-FIXED) | UNIT | Serde backward-compat: deserialize an OLD `LiveState` JSON that lacks `last_filled_window` (constraint 4). | Parses successfully; `last_filled_window == None`; no error (additive field). |
| TC-19.2 | UC-19-E1 (GAP-FIXED) | UNIT | `place_live` fill path (`filled`/`partial`) writes state. | `LiveState.last_filled_window == Some(current win_key)` after a confirmed fill. |
| TC-19.3 | UC-19 primary | UNIT | Boot seeding: `initial_fired_window(state{last_filled_window==W}, current_win_key==W)`. | Returns `Some(W)` → `fired_window` is pre-latched → the current window is treated as already filled → the trigger is SKIPPED → **no double order** for a window that already filled (INV-2 survives restart). |
| TC-19.4 | UC-19 primary | UNIT | Boot with a PAST window: `initial_fired_window(state{last_filled_window==W}, current_win_key==W+3)`. | Returns `None` (keys differ) → latch clear → the current window is eligible → the bot trades W+3 normally. |
| TC-19.5 | UC-19-E1 (audit) | OPS | The FR-8 audit records the restart double-order resolution. | Verdict GAP-FIXED (boot seeds `fired_window` from persisted `last_filled_window`, ref TC-19.1–19.4). (If instead GAP-ACCEPTED: written rationale bounding the single-$5 exposure + ledger duplicate-window detection.) |
| TC-19.6 | UC-19-EC1 | UNIT | Restart lands AFTER W rolled to W+1 with no fill yet in W+1 (`last_filled_window==W`, `current==W+1`). | `initial_fired_window` → `None`; the fresh empty latch is correct; W+1 SHOULD be eligible. Distinguishes the benign case from the hazard. |
| TC-19.7 | UC-19-EC2 | UNIT | Restart mid-retry, no fill yet in W (`last_filled_window` still a prior window, `attempt_*` state lost). | The new process may re-attempt within W under a fresh budget; does NOT violate INV-2 (no fill happened); may exceed the intended `N` across the restart boundary; bounded by band/cap/stop. Documented. |

---

## UC-20 — Subaccount #1 balance insufficient for the order → order-error path

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-20.1 | UC-20 primary | UNIT | Kalshi rejects the IOC for insufficient funds → transport `Err` (→ `order_error`) or non-201 (→ `rejected`) (reuse Section 1 FR-3). | One outcome row tagged `f1_d50cap75`; consumes one retry attempt (post-order); does NOT latch. |
| TC-20.2 | UC-20 primary | UNIT | Bounded retry with the balance still insufficient on attempt2. | Attempt2 also errors/rejects → budget exhausted; window not traded (UC-15-E1). |
| TC-20.3 | UC-20-A1 | UNIT | `count` clamped to `[1, MAX_COUNT]` but even a `count==1` $5 IOC is rejected near-zero balance. | Same error/reject handling; one outcome row; no latch. |
| TC-20.4 | UC-20-E1 | UNIT | Partial affordability: Kalshi partially fills rather than rejects. | `outcome==partial` (UC-5-A3); `trades_today+=1`; latch consumed. Only a TRUE rejection takes the error path. |
| TC-20.5 | UC-20-EC1 | OPS | Subaccount #1 drained to ~zero over a run. | Every subsequent F1 fire errors/rejects and never fills; combined with the daily loss stop (UC-18) the bot effectively halts — intended small-subaccount fail-safe (PRD 2.10); ops top up or stop. |
| TC-20.6 | UC-20 primary | INTEGRATION | Order-client routing (main.rs ~466-484) with `SUBACCOUNT=1`. | The `OrderClient` is bound to subaccount #1; only subaccount #1 is affected, never the main balance (FR-9, INV-F6). Verified via `order client ready | ... subaccount=1` log. |

---

## UC-21 — `threshold_gap` parity (open question) → determine paper semantics, verdict before go-live

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-21.1 | UC-21 primary | OPS | The auditor inspects the paper engine to determine what `threshold_gap = 0.1` does for the pmodel/F1 path (affects entry/side, or inert). | The semantics are documented: either (a) it affects the entry/side decision, or (b) it is inert for the pmodel path. No guessing. |
| TC-21.2 | UC-21 primary (AC-5) | OPS | A verdict is recorded for the `threshold_gap` item. | MATCH (inert, rationale) / GAP-FIXED (replicated in engine.rs + a unit test) / GAP-ACCEPTED (divergence quantified — how many historical F1 fires flip — and accepted in writing). Closed before `LIVE_TRADING=1`. |
| TC-21.3 | UC-21-A1 | OPS/UNIT | If `threshold_gap` acts as `delta_threshold ± gap` hysteresis: quantify the near-boundary window fraction affected (ties to UC-17). | The affected fraction is quantified; if GAP-FIXED, a unit test asserts the Rust gate reproduces the hysteresis near Δ ≈ 50. |
| TC-21.4 | UC-21-E1 | OPS | Paper semantics undeterminable from available source (config lives only in the paper DB). | The auditor MUST obtain the behavior from the paper maintainer or a live probe; an undetermined verdict is NOT a valid go-live state. |
| TC-21.5 | UC-21-EC1 | OPS/UNIT | If `threshold_gap` is a side-selection dead-zone (suppresses fires within `gap` of the threshold). | Highest-impact divergence (omitting it makes Rust fire on marginally-weaker signals → worse entries) → MUST be GAP-FIXED (with a unit test on the replicated dead-zone), not merely accepted. |

---

## UC-22 — Rollback to f6 (config/binary swap, no data migration)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-22.1 | UC-22 primary (NFR-4) | OPS | Restore the previous `*.bak` binary AND the previous drop-in env file (no `SESSION`/`MIRROR_SESSION`); restart `kalshi-shadow-com`. | The bot boots f6: `f6_wait270()`, `max30` sigma key, `f6_wait270` mirror key, `f6_wait270_shadow` telemetry tag, `f6-` coid prefix — byte-identical to pre-F1 (INV-F1); startup log confirms the f6 params. Exact paths + restart command are in the runbook (constraint 10). |
| TC-22.2 | UC-22-A1 | OPS | Env-only rollback (same binary): unset `SESSION`/`MIRROR_SESSION` (or set `SESSION=f6_wait270`) and restart. | Reverts to f6 without a binary swap (one binary, env-selected, FR-2); startup log shows f6. |
| TC-22.3 | UC-22-A2 | OPS | Disable live entirely: set `LIVE_TRADING=0` (or unset) while keeping F1 selected. | Drops to shadow/log-only; F1 gate still evaluated for observation; no real orders. |
| TC-22.4 | UC-22-E1 | OPS | Partial rollback: old binary restored but `SESSION=f1_d50cap75` left in the env; the old binary predates FR-2. | The old binary ignores `SESSION` and runs f6 (effectively still a rollback), but the env is misleading. The runbook mandates reverting BOTH artifacts; QA verifies the startup log shows f6. |
| TC-22.5 | UC-22-EC1 | OPS | Rollback mid-window with an open F1 position still `pending`/settling. | The resolver (unchanged) settles the pending into a `ResolveRecord` regardless of the current session; rollback does not strand open positions. |
| TC-22.6 | UC-22 primary | OPS | Inspect the ledger after rollback. | Append-only and intact; prior F1 rows remain, correctly tagged `f1_d50cap75` (no rewrite); no data migration performed. |

---

## Go-live operational checklist (constraint 10 — must be green before `LIVE_TRADING=1`)

| Gate | TC | Blocking? |
|------|----|-----------|
| Startup log shows F1 params (`entry_wait=3min delta>=50 p>=0.65 sigma=max10 max_entry=0.92`) | TC-1.4o | Yes |
| Order-client log shows `subaccount=1` | TC-20.6 | Yes |
| Shadow pre-flight FIRST (`LIVE_TRADING=0`): F1 gate validated log-only | TC-1.10 | Yes |
| Shadow mirror gap ≈ 0 vs paper F1 on live triggers (measured before real orders) | TC-5.9 (shadow-first, then live) | Yes |
| Entry-fidelity audit doc complete (7 verdicts) | TC-6.1, TC-6.2 | Yes |
| `threshold_gap` verdict recorded BEFORE `LIVE_TRADING=1` | TC-21.2 | Yes |
| Non-positive sigma fail-closed resolved (fetch rejects `<=0.0`) | TC-14.2, TC-14.6 | Yes |
| Restart double-order guard decided (fixed via `last_filled_window`) | TC-19.3, TC-19.5 | Yes |
| `DAILY_LOSS_STOP=30` set on the deployed env | TC-18.4 | Yes |
| Dashboard live-F1 (green) vs paper-F1 (pink) visible | TC-5.10 | Yes (post-first-fill) |
| Rollback runbook documented (paths + restart command) | TC-22.1 | Yes |

---

## Coverage matrix — every UC scenario → at least one test case

| Scenario | Mapped test case(s) |
|----------|---------------------|
| UC-1 (primary) | TC-1.1, TC-1.2, TC-1.3, TC-1.4o, TC-1.5 |
| UC-1-A1 | TC-1.6 |
| UC-1-A2 | TC-1.7 |
| UC-1-A3 | TC-1.8 |
| UC-1-E1 | TC-1.9 |
| UC-1-EC1 | TC-1.10 |
| UC-1-EC2 | TC-1.11 |
| UC-2 (primary) | TC-2.1, TC-2.2, TC-2.3 |
| UC-2-A1 | TC-2.4 |
| UC-2-E1 | TC-2.5 |
| UC-2-EC1 | TC-2.6 |
| UC-3 (primary) | TC-3.1, TC-3.2 |
| UC-3-A1 | TC-3.3 |
| UC-3-E1 | TC-3.4 |
| UC-3-EC1 | TC-3.5 |
| UC-3-EC2 | TC-3.6 |
| UC-4 (primary) | TC-4.1, TC-4.2 |
| UC-4-A1 | TC-4.3 |
| UC-4-E1 | TC-4.4 |
| UC-4-EC1 | TC-4.5 |
| UC-4-EC2 | TC-4.6, TC-4.7 |
| UC-5 (primary) | TC-5.1, TC-5.2, TC-5.3, TC-5.9, TC-5.10 |
| UC-5-A1 | TC-5.4 |
| UC-5-A2 | TC-5.5 |
| UC-5-A3 | TC-5.6 |
| UC-5-E1 | TC-5.7 |
| UC-5-EC1 | TC-5.8 |
| UC-6 (primary) | TC-6.1, TC-6.2 |
| UC-6-A1 | TC-6.3 |
| UC-6-E1 | TC-6.4 |
| UC-6-EC1 | TC-6.5 |
| UC-7 (primary: A1/A2/A3 sub-cases) | TC-7.1, TC-7.2, TC-7.3, TC-7.7 |
| UC-7-A4 | TC-7.4 |
| UC-7-E1 | TC-7.5 |
| UC-7-EC1 | TC-7.6 |
| UC-8 (primary) | TC-8.1, TC-8.2, TC-8.6 |
| UC-8-A1 | TC-8.3 |
| UC-8-E1 | TC-8.4 |
| UC-8-EC1 | TC-8.5 |
| UC-9 (primary) | TC-9.1, TC-9.2 |
| UC-9-A1 | TC-9.3 |
| UC-9-E1 | TC-9.4 |
| UC-9-EC1 | TC-9.5 |
| UC-10 (primary) | TC-10.1, TC-10.2 |
| UC-10-A1 | TC-10.3 |
| UC-10-E1 | TC-10.4 |
| UC-10-EC1 | TC-10.5 |
| UC-11 (primary) | TC-11.1, TC-11.2 |
| UC-11-A1 | TC-11.3 |
| UC-11-E1 | TC-11.4 |
| UC-11-EC1 | TC-11.5 |
| UC-12 (primary) | TC-12.1 |
| UC-12-A1 | TC-12.2 |
| UC-12-E1 | TC-12.3 |
| UC-12-EC1 | TC-12.4 |
| UC-13 (primary) | TC-13.1 |
| UC-13-A1 | TC-13.2 |
| UC-13-E1 | TC-13.3 |
| UC-13-EC1 | TC-13.4 |
| UC-14-A1 | TC-14.1 |
| UC-14-A2 | TC-14.2, TC-14.3 |
| UC-14-E1 | TC-14.2, TC-14.6 |
| UC-14-EC1 | TC-14.4 |
| UC-14-EC2 | TC-14.5 |
| UC-15-A1 | TC-15.1, TC-15.3 |
| UC-15-A2 | TC-15.2, TC-15.3 |
| UC-15-A3 | TC-15.4 |
| UC-15-E1 | TC-15.5 |
| UC-15-EC1 | TC-15.6 |
| UC-16 (primary) | TC-16.1 |
| UC-16-A1 | TC-16.2 |
| UC-16-A2 | TC-16.3 |
| UC-16-E1 | TC-16.4 |
| UC-16-EC1 | TC-16.5 |
| UC-17-A1 | TC-17.1 |
| UC-17-A2 | TC-17.2 |
| UC-17-A3 | TC-17.3 |
| UC-17-A4 | TC-17.4 |
| UC-17-A5 | TC-17.5 |
| UC-17-E1 | TC-17.6 |
| UC-17-EC1 | TC-17.7 |
| UC-18 (primary) | TC-18.1, TC-18.2 |
| UC-18-A1 | TC-18.3 |
| UC-18-A2 | TC-18.4 |
| UC-18-E1 | TC-18.5 |
| UC-18-EC1 | TC-18.6 |
| UC-19 (primary) | TC-19.3, TC-19.4 |
| UC-19-E1 | TC-19.1, TC-19.2, TC-19.5 |
| UC-19-EC1 | TC-19.6 |
| UC-19-EC2 | TC-19.7 |
| UC-20 (primary) | TC-20.1, TC-20.2, TC-20.6 |
| UC-20-A1 | TC-20.3 |
| UC-20-E1 | TC-20.4 |
| UC-20-EC1 | TC-20.5 |
| UC-21 (primary) | TC-21.1, TC-21.2 |
| UC-21-A1 | TC-21.3 |
| UC-21-E1 | TC-21.4 |
| UC-21-EC1 | TC-21.5 |
| UC-22 (primary) | TC-22.1, TC-22.6 |
| UC-22-A1 | TC-22.2 |
| UC-22-A2 | TC-22.3 |
| UC-22-E1 | TC-22.4 |
| UC-22-EC1 | TC-22.5 |

**All 96 UC scenarios (22 primary + 27 alternative + 22 error + 25 edge) map to at least one test case. No gaps.**

## Cross-cutting invariant coverage

| Invariant | Covered by |
|-----------|-----------|
| INV-F1 (env is the only lever; unset ⇒ byte-identical f6) | TC-2.3, TC-2.4, TC-2.5, TC-22.1 |
| INV-F2 (selected session's gate is the only gate) | TC-9.1, TC-9.5 |
| INV-F3 (sigma keyed by `sigma_type`, no silent fallback) | TC-4.1, TC-4.2, TC-4.3, TC-2.6 |
| INV-F4 (fail-closed on ambiguity) | TC-3.4, TC-3.5, TC-3.6, TC-10.2, TC-11.1, TC-12.1, TC-13.1, TC-14.1, TC-14.2, TC-14.3 |
| INV-F5 (attribution to `f1_d50cap75`) | TC-1.3, TC-5.2, TC-7.7, TC-8.6, TC-15.1 |
| INV-F6 (Section 1 rails unchanged) | TC-5.3, TC-7.*, TC-8.*, TC-15.*, TC-18.*, TC-20.6 |
| INV-F7 (no new hot-path work) | TC-3.2 (unchanged 2-GET/tick), TC-4.2 (String-key change only) |
| INV-F8 (additive config; existing tests pass) | TC-1.2, TC-2.7, TC-19.1 |

## Concurrency / retry-latch interplay (P0b, F1 context)

| Property | Covered by |
|----------|-----------|
| Latch consumed on fill only | TC-5.3, TC-8.1, TC-15.3 |
| Max 2 attempts per window | TC-8.2, TC-15.5 |
| No double-latch on order error/reject | TC-8.4, TC-15.3, TC-15.5 |
| Cooldown blocks per-tick ordering | TC-8.1 |
| Window roll resets budget/latch | TC-8.5, TC-19.6 |
| First fill stops retries | TC-8.1, TC-15.4 |
| Restart does not double-order a filled window | TC-19.3, TC-19.5 |
