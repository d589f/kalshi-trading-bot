## Feature: live-exec-telemetry-latch-fix (P0 telemetry + P0b latch fix)

## Branch: feat/live-exec-telemetry-latch-fix

## Status: P0 telemetry DEPLOYED TO PROD (slices 1-5) — HARD STOP before slice 6 (P0b, real-money) per user

## Deploy note (2026-06-28 ~15:20 UTC)
P0 telemetry live on EU box (kalshi-shadow-com, PID 465625, new binary built on-box 8m18s).
Behavior byte-identical; only the live ledger format changed. Verified on a real fill:
new records carry outcome + signal_entry/exec_entry/first_limit_price/requote/remaining/fill/eff/latency.
Backups for rollback: /home/dmitrii/kalshi_rs/{target/release/kalshi_bot,src/main.rs,src/ledger.rs}.bak.20260628-180739
TODO when data accrues: validate gap decomposition semantics on NO-side trades (saw one eff=0.39 vs exec_entry=0.65 fill worth a sanity check); then tune PRICE_BUF/REQUOTE_BUF on data. Branch not pushed/merged.

## Context
Live Kalshi f6 trader overpays vs paper. MEASURED on prod (EU box, 2026-06-28): real all-time PnL −$11.42; entry drag vs paper +$12.23 over 163 trades (≈ the whole loss). Mean gap +1.12c, median 0, momentum right-tail (13% pay ≥6c, max +24c). No-fill ~9%, each burns the window (latch-before-await bug). Live ledger persists too little to decompose. This feature: P0 makes the gap measurable, P0b stops the window-burn. Pricing strategy UNCHANGED. PRICE_BUF/REQUOTE_BUF tuning is a SEPARATE later step (user approves separately, after P0b).

Prod access: EU box trading-bot-5sec @ 34.32.177.126 (ssh dmitrii + key, sudo), service kalshi-shadow-com, ledger /home/dmitrii/kalshi_rs/shadow_ledger.jsonl, paper DB /root/paper_compare_kalshi_15m/live_data.db (session f6_wait270). Prod runs PRICE_BUF=0.06, REQUOTE_BUF=0.12 (default). DO NOT change prod without explicit user OK.

## Plan
Full plan: `.claude/plan-live-exec-telemetry-latch-fix.md`

### Wave 1
- [x] Slice 1: ledger.rs Outcome enum + LiveTriggerRecord + serde tests — c00eb53
- [x] Slice 2: main.rs pure helpers classify_outcome/decompose_gap/is_dashboard_trigger + tests — 86279b5

### Wave 2
- [x] Slice 3: typed fill record (FILL path, no behavior change) — bcb55f6

### Wave 3
- [x] Slice 4: place_live -> Outcome + row on every skip/error/no-fill path — 4a7fdc1

### Wave 4
- [x] Slice 5: load_ledger_into_dash outcome filter (legacy=filled) — 0f7d08b

### Wave 5  [STOP — needs user go-ahead + shadow validation]
- [ ] Slice 6: P0b latch fix + bounded retry (REAL-MONEY). N=2/C=3 defaults; N=1/C=0 == legacy.

### Wave 6
- [ ] Slice 7: regression guard (pricing/sizing/MIRROR diff-clean) + full cargo gate

## Order-connection keep-warm (added 2026-06-28 ~22:05 UTC) — commit 955a9f6
Root cause of the ~250-400ms order latency = COLD TCP+TLS per order (measured on EU box:
cold ~270ms TLS handshake vs ~10ms warm; ping to CloudFront PoP = 4.8ms, so NOT distance).
OrderClient had its own pool, orders fire ~once/15min > reqwest ~90s idle → cold every time.
Fix: pool_idle_timeout(None) + tcp_keepalive(30s) on OrderClient; main.rs spawns a warm-ping
(GET /portfolio/balance every ORDER_WARM_SECS=30) so the order POST is ~1 RTT. Behavior-neutral.
Validation = OPERATIONAL: watch latency_ms in new ledger fills drop from ~250 toward ~100-150.
DEPLOY in progress: backups ts 20260628-220447. (rebuild ~8min on-box + restart.)

## Notes
- 27 tests pass; clippy clean on new code; pricing/sizing/order-send byte-identical (P0 = telemetry only).
- Deploy ordering: slices 4+5 ship together (4 writes nofill/skip rows; 5 makes loader ignore them). Both committed.
- Once deployed, the new live ledger fields let us decompose the gap: drift = exec_entry-signal_entry, walk = eff-exec_entry. THEN tune PRICE_BUF/REQUOTE_BUF on data.
- Branch only; nothing pushed; prod untouched.

## Completed
- Bootstrap docs: PRD §1, use cases (67 scenarios), architecture review (PASS), QA (81 test cases), plan (7 slices).

## Blockers
- none
