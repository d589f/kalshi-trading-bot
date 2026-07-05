# Implementation Plan — live-f1-strategy (Live F1 Strategy Switch + Entry-Fidelity Audit)

Switch the Rust live trader (kalshi_rs) from f6_wait270 to f1_d50cap75 via env (SESSION / MIRROR_SESSION),
re-enter LIVE at $5 on subaccount #1, gated behind an entry-fidelity audit.
Default (env-unset) behavior MUST stay byte-identical to today's f6 mirror.

## Deliverables checklist (Phase 1 — all DONE 2026-07-05)
- [x] PRD: docs/PRD.md Section 2 (live-f1-strategy), AC 2.5, risks 2.10
- [x] Use cases: docs/use-cases/live-f1-strategy_use_cases.md — 22 UCs, 96 scenarios, INV-F1..F8
- [x] Architecture review: PASS-conditional, 8 binding constraints (below)
- [x] QA: docs/qa/live-f1-strategy_test_cases.md — all 96 scenarios mapped, go-live checklist

## Architect's binding constraints (violating = rejected)
1. Pure resolver in config.rs (`resolve_session(name)->Option<SessionConfig>`, trim, case-sensitive); fail-loud log + f6 fallback in main(); NO panic/exit in config.rs.
2. Sigma-key fix in main.rs mirror branch (~868): insert under cfg.sigma_type. Re-key BOTH display consumers (status log ~952, dashboard LiveSnap ~992). Keep MirrorSnap/LiveSnap field NAMES.
3. Fail-closed sigma: mirror::fetch returns None on live_sigma<=0.0. engine.rs MUST NOT be modified for this.
4. Restart latch: LiveState.last_filled_window:Option<String> (serde default), written on fill in place_live, fired_window seeded on boot.
5. Session attribution runtime-derived, threaded (no mutable global); coid "f6-" prefix at main.rs:1258 derives from session.
6. threshold_gap audit (paper source on EU box) verdict recorded BEFORE LIVE_TRADING=1; contingency slice 7 if gate-affecting.
7. Deploy: both SESSION and MIRROR_SESSION explicit; STAKE=5, SUBACCOUNT=1, MAX_ENTRY=0.92, DAILY_LOSS_STOP=30; shadow pre-flight (LIVE_TRADING=0) with mirror-gap verification BEFORE flip to 1. Rollback runbook.
8. Security pre-review: slices 2 (fail-closed sigma) + 9 (live rollout). Architect pre-review: 1, 4, 5, 6, 7.

## Slices

### Slice 1 — config.rs: pure session resolver, F1 factory, SessionSel attribution helpers, mirror-key resolver
Wave: 1. Files: kalshi_rs/src/config.rs.
- SessionConfig::f1_d50cap75(): kappa 0.4, delta 50.0, p 0.65, sigma_type "max10", tau linear, Both, max_entry 0.92, entry_wait 3.0, liq_filter false, sigma_max None.
- enum SessionSel {F6,F1}: key(), config(), session_tag() ("{key}_shadow"; f6 == "f6_wait270_shadow" exactly), coid_prefix() ("f6-"/"f1-").
- resolve_session_sel(name)->Option<SessionSel> (trim, case-sensitive, empty->None); resolve_session(name)->Option<SessionConfig>.
- resolve_mirror_session_key(sel, mirror_env)->String (trimmed non-empty env wins, else sel.key()).
TCs: TC-1.1/1.2/1.3/1.11, TC-2.1/2.2, TC-3.3, TC-10.1/10.4/10.5.
Done when: resolver matrix passes ("f1_d50cap75"->F1 all fields, " f1_d50cap75 "->F1, "F1_D50CAP75"->None, ""->None, "f6_wait270"->f6); tag/coid/mirror-key derivations exact; existing tests green.
Pre-review: architect.

### Slice 2 — mirror.rs: session-parameterized fetch + fail-closed extract_live_sigma
Wave: 1. Files: kalshi_rs/src/mirror.rs.
- Pure extract_live_sigma(&Value, session_key)->Option<f64>: Some only if present, numeric, strictly >0.0; None for missing key/field, null/non-numeric, <=0.0.
- fetch(...) gains session_key param; mirror.rs:74 hardcoded "f6_wait270" removed. MirrorSnap.sigma_max30 NAME unchanged.
TCs: TC-3.1/3.5/3.6, TC-11.1, TC-14.1/14.2/14.5. Integration via canned JSON (NO new dev-dep — hand-rolled or downgraded to OPS per planner decision).
Done when: table-driven matrix passes; grep confirms no "f6_wait270" literal in mirror.rs.
Pre-review: architect + SECURITY.

### Slice 3 — docs/audit/live-f1-entry-fidelity.md: 7-verdict entry-fidelity audit + rollback runbook
Wave: 1. Files: docs/audit/live-f1-entry-fidelity.md [new].
Verdicts (MATCH/GAP-FIXED/GAP-ACCEPTED) for: p-model formula parity; entry_wait 180s; threshold_gap=0.1 semantics (read paper source on EU box /root/paper_compare_kalshi_15m — go-live blocker); max_entry band; liq_filter off; sigma source (GAP-FIXED -> slices 2+4); execution buffers PRICE_BUF=0.06/REQUOTE_BUF=0.12 at 180s entry (recommendation only). Plus rollback runbook.
TCs: TC-6.1/6.2/6.3/6.5, TC-14.6, TC-17.6, TC-19.5, TC-21.1/21.2/21.4.
Done when: all 7 items have exactly one verdict; threshold_gap section concludes gate-affecting->Slice 7 OR inert->MATCH/GAP-ACCEPTED with rationale; Rollback section present.
Pre-review: architect.

### Slice 4 — main.rs: strategy wiring (env->resolver+fail-loud, sigma-key insert fix, display re-key, mirror-key plumbing)
Wave: 2. Files: kalshi_rs/src/main.rs.
- select_session(env)->(SessionSel, Option<String>) pure: unset/empty->(F6,None); known->(sel,None); unknown->(F6,Some(warn naming value+accepted set)). main() logs warn via error!, no exit.
- insert_mirror_sigma(&mut sigmas, cfg, val): insert under cfg.sigma_type.clone() (replaces hardcoded "max30" @868).
- display_sigma(sigmas,cfg) used at status log (~952) and LiveSnap (~992).
- mirror_key threaded into signal_loop -> mirror::fetch.
TCs: TC-4.1/4.2(CRITICAL: gate invariant to s.sigma fallback)/4.3/4.4/4.5/4.6, TC-2.6, TC-1.5-1.8/1.10, TC-2.4/2.5, TC-10.2/10.3, TC-11.2/11.3, TC-12.x, TC-13.1/13.2.
Done when: TC-4.2 proves .or(s.sigma) never reached under F1; no "f6_wait270"/"max30" literals at 868/952/992; full suite green. Mid-slice build every 3 edits.
Pre-review: architect.

### Slice 5 — main.rs: session attribution threading + coid prefix
Wave: 3. Files: kalshi_rs/src/main.rs.
- session_tag/coid_prefix from SessionSel threaded through signal_loop -> place_live -> build_fill_record/build_context_record, emit_trigger, resolver (ResolveRecord.session), coid @1258 -> "{prefix}...". No mutable global. f6 path byte-identical.
TCs: TC-1.3, TC-2.3, TC-5.2, TC-7.7, TC-8.6, TC-15.1.
Done when: records tagged by selected session; coid "f1-" under F1, "f6-" under F6; SESSION unset -> all tags "f6_wait270_shadow". Full suite green.
Pre-review: architect.

### Slice 6 — main.rs: restart double-order latch
Wave: 4. Files: kalshi_rs/src/main.rs.
- LiveState.last_filled_window: Option<String> #[serde(default)]; fill path in place_live writes Some(win_key)+save; pure initial_fired_window(state,current)->Option<String> (equal->Some else None); signal_loop seeds fired_window on boot.
TCs: TC-19.1 (serde back-compat), 19.2, 19.3 (same window->skip), 19.4/19.6 (past->clear), 19.7.
Done when: legacy live_state.json parses (None); boot-with-current-window skips double order; full suite green.
Pre-review: architect.

### Slice 7 (CONDITIONAL) — engine.rs/config.rs: replicate threshold_gap IF audit says gate-affecting
Wave: 5. Files: engine.rs, config.rs, audit doc.
Executed ONLY on GAP-FIXED verdict from Slice 3. SessionConfig.threshold_gap (f6 0.0, F1 0.1); replicate paper dead-zone/hysteresis in engine.rs (ONLY sanctioned engine.rs change). TCs: TC-21.3/21.5.
Done when: skipped with clean engine.rs diff (MATCH/GAP-ACCEPTED), OR deadzone test passes + f6 byte-identical at gap=0.0.
Pre-review: architect.

### Slice 8 — regression guard: tests/f1_regression.rs + full cargo test + clippy
Wave: 6. Files: kalshi_rs/tests/f1_regression.rs [new].
F1 gate fires/boundaries (180.0s/Δ50/p0.65/entry0.92, 0.50 floor), F1-vs-f6 divergence (Δ=30; elapsed 210s), f6 byte-identity guard (config/insert-key/mirror-key/tag/coid with SESSION unset).
TCs: TC-5.1, TC-9.1-9.5, TC-17.1-17.5/17.7, TC-16.3-16.5, TC-1.9, TC-2.3/2.7.
Done when: cargo test 0 failures (existing 33+ plus new); cargo clippy --all-targets -- -D warnings exits 0.
Pre-review: none.

### Slice 9 — deploy (OPS): shadow pre-flight then LIVE_TRADING=1 on EU box
Wave: 7. Files: EU box systemd drop-in + on-box build; audit doc finalize.
Env: SESSION=f1_d50cap75 AND MIRROR_SESSION=f1_d50cap75 (both explicit), STAKE=5, SUBACCOUNT=1, MAX_ENTRY=0.92, DAILY_LOSS_STOP=30, RETRY_MAX_ATTEMPTS=2, RETRY_COOLDOWN_SECS=3.
Step 1 shadow pre-flight (LIVE_TRADING=0): startup log shows F1 params + subaccount=1; mirror-gap ≈0 vs paper F1 on real triggers.
Step 2 flip LIVE_TRADING=1 ONLY after go-live checklist green (audit 7 verdicts incl threshold_gap, fail-closed sigma shipped, restart latch shipped, DAILY_LOSS_STOP=30, rollback runbook).
Done when: first live filled row tagged f1, subaccount #1, mirror gap ≈0; dashboard green live-F1 vs pink paper-F1.
Pre-review: SECURITY + architect.

## Wave summary
| Wave | Slices | Files |
|---|---|---|
| 1 | 1 (config.rs), 2 (mirror.rs), 3 (docs/audit) | disjoint — parallel OK |
| 2 | 4 (main.rs) | |
| 3 | 5 (main.rs) | |
| 4 | 6 (main.rs) | |
| 5 | 7 (engine.rs+config.rs) CONDITIONAL | |
| 6 | 8 (tests/f1_regression.rs) | |
| 7 | 9 (OPS deploy) | |
Conditional-renumber: if Slice 7 skipped, Slice 8 -> Wave 5, Slice 9 -> Wave 6.

## Acceptance criteria (PRD 2.5)
AC-1 F1 gate params logged+enforced; AC-2 sigma under sigma_type key (unit-tested, both max10/max30); AC-3 mirror reads f1_d50cap75.live_sigma, absent->skip; AC-4 default f6 byte-identical; AC-5 audit doc 7 verdicts; AC-6 deployed, mirror gap ≈0, subaccount #1; AC-7 dashboard live-F1 vs paper-F1.

## Risks
Real money $147.73 subaccount (loss stop 30); silent-wrong-trade H2 sigma key (CRITICAL — TC-4.2 merge blocker); fail-open bad sigma (slice 2); thin regime-dependent edge (+0.8pt cushion); never traded live (180s momentum, unknown no-fill tail); threshold_gap unknown (go-live blocker); mirror single source of truth (fail-closed skip); restart double-order (slice 6); serde back-compat (TC-19.1).

## Dependencies
No new runtime deps. NO new dev-deps (HTTP mock escalation resolved: pure-fn unit tests + hand-rolled TcpListener or OPS downgrade). Paper engine 8893 exposes f1_d50cap75.live_sigma (verified). EU box ssh (key in session scratchpad), Buffalo dashboard.
