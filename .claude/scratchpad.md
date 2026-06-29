## Feature: live-exec-telemetry-latch-fix (P0 telemetry + P0b latch fix)

## Branch: feat/live-exec-telemetry-latch-fix

## Status: 2026-06-29. P0b (latch fix) DONE on branch (283ef53), tested, NOT yet deployed. Live max_entry 0.92, loss-stop $100.

## P0b done (slice 6, commit 283ef53): latch on fill, retry no-fills (RETRY_MAX_ATTEMPTS=2, RETRY_COOLDOWN_SECS=3.0; N=1/C=0=legacy). Fixes the 17-no-fill-burns-window leak. 33 tests pass. Real-money behavior change — deploy = rebuild+restart, then watch for runaway ordering (bounded: max 2 place_live/window).
## Root-cause reconciliation (2026-06-29): live ≈ paper on matched trades (100% same outcomes). The -$65 = near-breakeven WR window (71% vs ~72-74% breakeven) + Kalshi fee -$24 (per-contract 0.07*C*P*(1-P), confirmed official) + live missed ~28% of paper trades, but most misses were TRANSIENT (my ~10 deploy restarts + the now-reverted max_entry cap + daily caps), only 17 were no-fills (the latch, now fixed). Green dashboard line = real paper engine (not backtest), $100 NO-fee; +$4191 was earned mostly Jun22-25 BEFORE live. Over live's window paper was ~flat (+$3.90 net@$5 w/fee). EDGE not dead — thin: net +2.6%/trade only if WR>=74%. TRIPWIRE: 1 week data, WR>=74% keep / <73% stop.

## MORNING FINDINGS (2026-06-29) — overnight was bad
- total_pnl -$65.66 (lost ~$26 overnight). Live overnight: 34 trades, WR 65%, -$25.83.
- http2 FAILED to fix latency: 46% of fills still cold ~280-477ms (warm ones hit the ~105ms floor). mean 215 vs ~250 before = marginal. Only us-east COLOCATION fixes latency.
- WR DECAY trend: backtest 78% -> live recent 71% -> overnight 65%. PAPER also fell to 69% overnight (paper LOST too) -> it's the EDGE decaying, not just execution. (DAILY warned edge decays.)
- max_entry 0.85 BACKFIRED: paper's expensive trades (>0.85) that live skipped won 91% & were profitable; cheap trades (<=0.85) live kept won only 61%. So the cap removed the winners. REVERTED to 0.92.
- USER DECISION: keep live running (bet WR recovers), revert max_entry to 0.92. Done.
- TRIPWIRE to watch: if total approaches -$100+ or WR stays <70% another day, recommend pause. Real options: retrain/find new edge, colocation for latency, or accept decay & stop.

## (prior) OVERNIGHT HANDOFF (deployed to prod, EU box kalshi-shadow-com)

## OVERNIGHT HANDOFF (deployed to prod, EU box kalshi-shadow-com)
Live config NOW: LIVE_TRADING=1, STAKE=5, MAX_ENTRY=0.85 (paper stays 0.92 for A/B),
PRICE_BUF=0.06, DAILY_LOSS_STOP=100 (raised from 50 per user — wants room for reversal),
MAX_TRADES_DAY=96. Binary = P0 telemetry + keep-warm + HTTP/2 (h2 keepalive) + MAX_ENTRY override.
PnL at handoff: total -$39.83, today -$18.97 / 81 trades. Pushed to main (Peanut-PM + d589f) up to 68c71a7 (+ later commits 955a9f6/d78c528/68c71a7 on branch; main was merged at 4236cda, later commits may be branch-only — CHECK before next push).

OPEN ITEMS for morning:
1. http2 latency: only 1 post-restart fill (150ms) — need 3-4 to confirm the ~300ms cold tail is gone (warm floor ~105ms). Query ledger fills after restart. If still ~300 → only colocation (us-east) fixes it.
2. max_entry 0.85 effect: compare live(0.85) vs paper(0.92) head-to-head; skip_band rows in ledger = the expensive trades live now skips — did paper win/lose on them?
3. WR question: backtest 6-wk WR 78.5% (strong edge, robust train/test); live recent 71% (within noise of 77% but low). Is it noise or decay? Overnight data helps.
4. Backtest sweep (kalshi_third/sweep.py): later entry (em=5-6 ~300-360s) > current 270s; delta threshold barely matters; lower max_entry better EV/trade; p-gate marginal. BUT idealized fills — backtest +5-11% net while live negative; gap is mostly WR(period) + slippage. Don't act on absolute numbers.

NOT done: slice 6 (P0b latch fix) — still pending, latch-burns-window bug remains on prod (minor: no-fill burns the window). Plan in .claude/plan-live-exec-telemetry-latch-fix.md.

Prod access: ssh dmitrii@34.32.177.126 (key in session scratchpad eu_key), sudo ok. Backups of every deploy: *.bak.TIMESTAMP on box. Backtest harness: kalshi_third/kalshi_third_bt.py (run()), sweep.py.

## (prior) Status: P0 telemetry DEPLOYED TO PROD (slices 1-5)

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
DEPLOYED 2026-06-28 ~19:15 UTC (box time): rebuilt on-box (9m46s), restarted, PID 664400.
Startup log confirms "order connection keep-warm every 30s (GET /portfolio/balance)", no ping failures.
Backups ts 20260628-220447 (binary + main.rs + orders.rs) for rollback. Watching next fills' latency_ms
to confirm drop from ~250 → ~100-150. Pushed to main on both Peanut-PM + d589f (4236cda).

## Notes
- 27 tests pass; clippy clean on new code; pricing/sizing/order-send byte-identical (P0 = telemetry only).
- Deploy ordering: slices 4+5 ship together (4 writes nofill/skip rows; 5 makes loader ignore them). Both committed.
- Once deployed, the new live ledger fields let us decompose the gap: drift = exec_entry-signal_entry, walk = eff-exec_entry. THEN tune PRICE_BUF/REQUOTE_BUF on data.
- Branch only; nothing pushed; prod untouched.

## Completed
- Bootstrap docs: PRD §1, use cases (67 scenarios), architecture review (PASS), QA (81 test cases), plan (7 slices).

## Blockers
- none
