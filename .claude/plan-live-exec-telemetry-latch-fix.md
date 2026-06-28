# Implementation Plan: `live-exec-telemetry-latch-fix`

Two parts under one feature id: **P0 — live trade telemetry** (zero order-behavior change, safe) and **P0b — fix latch-before-await** (behavior change, real money; shadow validation required). Ship P0 (slices 1-5) fully before P0b (slice 6).

## Docs
- PRD: `docs/PRD.md` §1
- Use cases: `docs/use-cases/live-exec-telemetry-latch-fix_use_cases.md` (67 scenarios, UC-1..UC-13)
- QA: `docs/qa/live-exec-telemetry-latch-fix_test_cases.md` (81 test cases, full coverage)
- Architecture review: PASS

## Authoritative decisions (architect + QA)
- Schema: one `kind:"trigger"`, additive `outcome` enum (snake_case: filled|partial|nofill|skip_daily_cap|skip_loss_stop|skip_band|order_error|rejected). Non-applicable order fields `Option` + `skip_serializing_if none`. New `LiveTriggerRecord` + `Outcome` in `ledger.rs` (additive; do NOT modify `TriggerRecord`/`ResolveRecord`).
- Backward-compat (load-bearing): record with no `outcome` = legacy = FILLED. `load_ledger_into_dash` pushes to `d.triggers` only when outcome ∈ {filled,partial} or absent.
- `pending` is in-memory only (never rebuilt from ledger); nofill/skip rows must never reach the pending push (stays after the fill<=0 return).
- `place_live` returns `Outcome`; remove pre-await `fired_window = win_key` (main.rs:754); latch only on filled/partial, keyed on iteration-local `win_key`.
- Retry: env `RETRY_MAX_ATTEMPTS`=2 (N), `RETRY_COOLDOWN_SECS`=3.0 (C). signal_loop locals attempt_count/last_attempt_ts/attempt_window reset on win_key change. Increment AFTER place_live returns ONLY for {nofill,order_error,rejected}; re-quote inside one place_live ≠ 2nd attempt. N=1,C=0 reproduces legacy exactly.
- Pure helpers to extract (testability contract): classify_outcome, decompose_gap, is_dashboard_trigger, latch_decision, counts_as_attempt, retry_gate, pricing/sizing helpers, row builders.
- Invariants: pricing/sizing math + MIRROR path byte-for-byte unchanged; at most one filled position/window; loop never crashes on serialize error; pending push fill-only.

## Slices

### Slice 1 — `Outcome` enum + `LiveTriggerRecord` + serde tests  [Wave 1]
- Files: `kalshi_rs/src/ledger.rs` (additive types + `#[cfg(test)] mod tests`).
- UC: UC-1..UC-6, UC-12 (record shape/tags).
- Outcome enum `#[serde(rename_all="snake_case")]`: Filled,Partial,Nofill,SkipDailyCap,SkipLossStop,SkipBand,OrderError,Rejected (derive Serialize+Deserialize for roundtrip).
- `LiveTriggerRecord` owned-String fields. Always: kind="trigger", live=true, outcome, ts, ts_iso, session, window_start/end, market_ticker, side, entry(=eff legacy meaning), signal_entry:Option<f64>(=fire.entry), p:Option, delta_from_open. Option+skip_if_none order fields: exec_entry, orderbook_entry, first_limit_price, requote_limit_price, requote:Option<bool>, remaining_count:Option<i64>, fill:Option<f64>, eff:Option<f64>, fee:Option<f64>, latency_ms:Option<i64>, order_id:Option<String>, count:Option<i64>.
- Done when: serde tests pass; `to_string(&Outcome::SkipDailyCap)=="\"skip_daily_cap\""`; skip record JSON omits `exec_entry`; `cargo build --release` ok.

### Slice 2 — pure helpers classify_outcome/decompose_gap/is_dashboard_trigger + tests  [Wave 1]
- Files: `kalshi_rs/src/main.rs` (free fns + new test mod; not yet wired — zero behavior change).
- UC: UC-1,2,3,5,6,12 classification/filter.
- classify_outcome(GateSnapshot)->Outcome with precedence EXACTLY in place_live source order: daily-cap→loss-stop→band→order_err→(status!=201⇒Rejected)→(fill<=0⇒Nofill)→(remaining>0⇒Partial)→Filled. Band predicate byte-identical to main.rs:915.
- decompose_gap(entry,exec_entry,eff)->(gap,drift,walk): gap=eff-entry, drift=exec_entry-entry, walk=eff-exec_entry.
- is_dashboard_trigger(Option<Outcome>)->bool: true iff None|Filled|Partial.
- Done when: 8 test fns pass; clippy clean; cap+loss+band all true → SkipDailyCap; is_dashboard_trigger(None)==true, (Some(Nofill))==false.

### Slice 3 — replace ad-hoc json! fill row with typed record (FILL path only, no behavior change)  [Wave 2]
- Files: `kalshi_rs/src/main.rs` place_live fill path (replace json! at 1008-1014).
- UC: UC-1,3,4,12 fill-path shape; INV-3.
- build_fill_record(...); capture first_limit_price BEFORE re-quote overwrites price (after 941); in re-quote block set requote=true, requote_limit_price=Some(price). Bookkeeping-only locals MUST NOT change values fed to create_ioc.
- entry JSON key carries eff (back-compat with resolver/dashboard main.rs:82); signal_entry carries fire.entry; count=fill as i64.
- Done when: 4 tests pass; build ok; git diff main.rs 927-948 & 962-965 shows pricing/sizing FORMULAS unchanged; serialized fill record entry==eff.
- Pre-review: architect.

### Slice 4 — place_live -> Outcome + append one row on EVERY skip/error/no-fill path  [Wave 3]
- Files: `kalshi_rs/src/main.rs` place_live signature + early-return sites (907/911/917/955/984/992).
- UC: UC-2,4,5,6,13; INV-1,5,8.
- build_skip_record(...) context fields, order Options None. Each early return builds+appends then returns Outcome::X.
- LOCK-SCOPE FIX: daily-cap/loss-stop returns sit inside live_state mutex (898-913) — restructure so append happens OUTSIDE the lock (Ledger has its own mutex). Preserve gate precedence.
- Bind `let _outcome = place_live(...).await;` (latch unchanged this slice). order_error only the FIRST-IOC Err; final outcome uses LAST (status,resp) after optional re-quote.
- Mid-slice: cargo build after wiring skip/error returns before no-fill/fill.
- Done when: 5 test groups pass; build ok; place_live returns ledger::Outcome; every former silent path appends+returns; should_push_position(Nofill)==false, (Partial)==true.
- Pre-review: security + architect (real-money routine).

### Slice 5 — load_ledger_into_dash filters on outcome (legacy=filled)  [Wave 4]
- Files: `kalshi_rs/src/main.rs` load_ledger_into_dash (64-102).
- UC: UC-12; INV-6,7.
- Gate d.triggers.push on is_dashboard_trigger via tolerant outcome_from_str (absent→None→treat as fill; unknown tag→skip push). Refactor sum into sum_resolve_pnl; per-line parse into replay_line_into for testability. No write-back.
- Done when: 5 tests pass; mixed fixture → d.triggers.len()==3 (legacy+filled+partial); sum_resolve_pnl only resolves; malformed line doesn't abort replay.
- Pre-review: architect.

### Slice 6 — P0b latch fix + bounded retry — REAL-MONEY BEHAVIOR  [Wave 5]
- Files: `kalshi_rs/src/main.rs` signal_loop trigger block (747-777), env knobs near 217, locals near 552.
- UC: UC-7,8,9,10,11; INV-2; NFR-4.
- latch_decision(o)=matches!(o,Filled|Partial); counts_as_attempt(o)=matches!(o,Nofill|OrderError|Rejected); retry_gate(cnt,last_ts,now,n,c)=cnt<n && (now-last_ts)>=c (negative delta→false=safe).
- Env: env_i64("RETRY_MAX_ATTEMPTS",2), env_f64("RETRY_COOLDOWN_SECS",3.0).
- signal_loop locals attempt_count/last_attempt_ts(NEG_INFINITY)/attempt_window; reset on win_key!=attempt_window.
- Trigger block: capture iteration-local win_key (682) as SOLE latch+attempt key; if fired_window==win_key skip; else if live && retry_gate && !ticker.empty: set last_attempt_ts=now; let outcome=place_live(...).await (NO pre-await latch — delete 754 on live path); after: if counts_as_attempt(outcome) attempt_count+=1; if latch_decision(outcome) fired_window=win_key. Shadow emit_trigger path keeps its own latch UNCHANGED.
- NFR-4 anchor: N=1,C=0 == today's single-attempt behavior (aside from telemetry row). trades_today increment stays fill-only.
- Done when: all P0b tests pass; build+clippy clean; grep finds no `fired_window =` before live place_live().await; nfr4_n1_c0_equals_legacy proves equivalence; latch_decision(Nofill)==false, (Filled)==true.
- Pre-review: security + architect — REAL-MONEY, extra rigor. PRD AC-6: shadow/log-only validation REQUIRED before prod.

### Slice 7 — regression guard: pricing/sizing/MIRROR diff-clean + full gate  [Wave 6]
- Files: `kalshi_rs/src/main.rs` extract value-preserving pricing/sizing helpers; guard tests.
- UC: AC-5; INV-3,4.
- exec_entry_from_ask, first_limit_price, requote_limit_price, order_count, eff_price, should_requote — extraction only, no math change. MIRROR (599-646) NOT touched.
- Done when: 6 guard tests pass; WHOLE suite green; build ok; `cargo clippy --all-targets -- -D warnings` clean; git diff main.rs 599-646 ZERO changes across branch.
- Pre-review: architect (final INV-3/4 sign-off).

## Acceptance criteria
- AC-1 one row per fire with correct outcome.
- AC-2 decompose_gap: drift+walk==gap within 1e-9.
- AC-3 mixed pre/post JSONL replays zero parse errors; legacy+filled+partial count, nofill/skip don't.
- AC-4 no-fill no longer burns window; ≤N bounded attempts w/ cooldown C; first fill latches; ≤1 filled/window.
- AC-5 pricing/sizing/MIRROR unchanged.
- AC-6 P0b shadow-validated before prod.
- Loop never crashes on append failure.

## Files to modify
- `kalshi_rs/src/ledger.rs` (additive: Outcome + LiveTriggerRecord + tests).
- `kalshi_rs/src/main.rs` (helpers, place_live->Outcome + appends, loader filter, signal_loop retry+latch, env knobs, tests).
- No new files. orders.rs/dashboard.rs/engine.rs/config.rs/window.rs untouched.

## Risks
- Real money (HIGH): slice 6 changes when live IOCs fire. Mitigations: pure tested retry_gate; N=1,C=0 anchor; mandatory shadow validation; security+architect pre-review.
- Latch correctness (HIGH): latch keyed on iteration-local captured win_key, never post-await clock read.
- Back-compat (MED): loader filters on outcome; legacy lacks it → is_dashboard_trigger(None)==true; entry-key-means-eff preserved.
- trades_today/daily cap (MED): increment stays fill-only.
- No new crates; append-only JSONL; no auth/external-contract change (only IOC invocation count under P0b, bounded by N).

## Dependencies
- No new crates (serde+derive, serde_json, chrono, tracing all present).
- New env: RETRY_MAX_ATTEMPTS (i64, def 2), RETRY_COOLDOWN_SECS (f64, def 3.0). Exact-legacy: RETRY_MAX_ATTEMPTS=1 RETRY_COOLDOWN_SECS=0.
- Tooling cargo test/build/clippy from `kalshi_rs/`.

## Waves
| Wave | Slices | Rationale |
|------|--------|-----------|
| 1 | 1, 2 | disjoint files (ledger.rs; main.rs new helpers+test mod, no overlap with later-edited regions) |
| 2 | 3 | place_live fill path; needs slice 1 types |
| 3 | 4 | place_live skip/error paths + signature; needs slice 3 |
| 4 | 5 | loader filter; needs is_dashboard_trigger + outcome field |
| 5 | 6 | P0b signal_loop; needs place_live->Outcome. REAL-MONEY |
| 6 | 7 | regression guard + full gate |
Only Wave 1 parallelizes; slices 3-7 share main.rs → sequential.

## Review Notes
### Critic Findings
- Deferred: run a Plan Critic pass before implementing slice 6 (real-money) if desired.
### Changes Made (pre-emptive)
- entry-key ambiguity resolved: record carries BOTH entry(=eff, legacy) and additive signal_entry(=fire.entry) so AC-2 computable without breaking dashboard PnL.
- live_state lock-scope: append must happen AFTER the state mutex drops (slice 4).
- Every Done-when is a concrete cargo test name + boolean/grep/diff check.
### Acknowledged Minor
- Exact helper signatures may be adjusted by test-writer for ergonomics as long as documented contracts + test assertions hold.
