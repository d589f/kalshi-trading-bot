## Feature: live-exec-telemetry-latch-fix (P0 telemetry + P0b latch fix)

## Branch: feat/live-exec-telemetry-latch-fix

## Status: bootstrapping complete — ready to implement Wave 1 slice 1/7

## Context
Live Kalshi f6 trader overpays vs paper. MEASURED on prod (EU box, 2026-06-28): real all-time PnL −$11.42; entry drag vs paper +$12.23 over 163 trades (≈ the whole loss). Mean gap +1.12c, median 0, momentum right-tail (13% pay ≥6c, max +24c). No-fill ~9%, each burns the window (latch-before-await bug). Live ledger persists too little to decompose. This feature: P0 makes the gap measurable, P0b stops the window-burn. Pricing strategy UNCHANGED. PRICE_BUF/REQUOTE_BUF tuning is a SEPARATE later step (user approves separately, after P0b).

Prod access: EU box trading-bot-5sec @ 34.32.177.126 (ssh dmitrii + key, sudo), service kalshi-shadow-com, ledger /home/dmitrii/kalshi_rs/shadow_ledger.jsonl, paper DB /root/paper_compare_kalshi_15m/live_data.db (session f6_wait270). Prod runs PRICE_BUF=0.06, REQUOTE_BUF=0.12 (default). DO NOT change prod without explicit user OK.

## Plan
Full plan: `.claude/plan-live-exec-telemetry-latch-fix.md`

### Wave 1
- [ ] Slice 1: ledger.rs Outcome enum + LiveTriggerRecord + serde tests
- [ ] Slice 2: main.rs pure helpers classify_outcome/decompose_gap/is_dashboard_trigger + tests

### Wave 2
- [ ] Slice 3: main.rs replace json! fill row with typed record (FILL path, no behavior change) — architect pre-review

### Wave 3
- [ ] Slice 4: main.rs place_live -> Outcome + append row on every skip/error/no-fill path — security+architect pre-review

### Wave 4
- [ ] Slice 5: main.rs load_ledger_into_dash outcome filter (legacy=filled) — architect pre-review

### Wave 5
- [ ] Slice 6: P0b latch fix + bounded retry (REAL-MONEY) — security+architect pre-review + shadow validation before prod

### Wave 6
- [ ] Slice 7: regression guard (pricing/sizing/MIRROR diff-clean) + full cargo gate

## Completed
- Bootstrap docs: PRD §1, use cases (67 scenarios), architecture review (PASS), QA (81 test cases), plan (7 slices).

## Blockers
- none
