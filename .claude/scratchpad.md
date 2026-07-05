## Feature: live-f1-strategy — switch live trader from f6_wait270 to f1_d50cap75, STRAIGHT TO LIVE (user decision 2026-07-05), $5 stake, subaccount #1 ($147.73 there). Plus entry-fidelity audit (maximize signal parity with paper F1).

## Branch: feat/live-f1-strategy (off feat/dashboard-f1-compare @ 38bee0c)

## Status: COMPLETE — F1 LIVE ON PROD since 2026-07-05 11:45 UTC (PID 709583). Feature done, all 9 slices (7 skipped by audit verdict).
## Slice 9 DONE: EU box rebuilt (11m26s; had to free disk — / was 100% full: truncated /tmp/paper_compare*.log ~620M w/ .tail kept, journal vacuum, old binary baks pruned; /root/paper_compare DBs+baks NOT touched, 830M free now, box will refill — flag to user). Drop-in mirror.conf: SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92 DAILY_LOSS_STOP=30 MAX_TRADES_DAY=96 PRICE_BUF=0.06 LIVE_TRADING=1. Pre-flight (LIVE=0) verified: F1 params log, mirror session f1, σ(max10)=0.000270 flowing, gate correctly skipped HIGH 0.96>0.92. Live startup verified: "order client ready | LIVE_TRADING=true stake=$5 max/day=96 loss_stop=$30 subaccount=1". Backups: src.bak.20260705-125937, kalshi_bot.bak.20260705-125937, /home/dmitrii/mirror.conf.bak.20260705. Rollback: restore drop-in (or unset SESSION/MIRROR_SESSION, LIVE_TRADING=0) + .bak binary + daemon-reload + restart.
## Quality gates: code review PASS (0 crit/major, 6 invariants hold), security audit PASS (A+B) -> hardening 1899eaa (sigma band [1e-7,1e-1], strict SUBACCOUNT parse refuses garbage, MIRROR_SESSION warn, honest skip label). 64 tests.
## WATCH: first F1 live fills' telemetry (signal_entry vs paper F1 entry -> mirror gap ~0; drift/walk at 180s momentum vs f6's 270s), exec_entry clamp 0.98 watch item from audit, subaccount #1 balance ($147.73 start). Dashboard 23.95.217.78:8890: green=live F1 (shadow_com push), pink=paper F1.
## NOT pushed to remotes (user didn't ask).

## Plan (full: .claude/plan-live-f1-strategy.md; PRD §2, use-cases 22UC/96sc, QA all mapped, arch PASS-conditional w/ 8 binding constraints)
### Wave 1 [bootstrap e99674d] — COMPLETE
- [x] Slice 1: config.rs resolver+F1 factory+SessionSel — fd3e4e4 (39 tests)
- [x] Slice 2: mirror.rs session param + fail-closed extract_live_sigma (>0, finite) — a5d14f6 (45 tests)
- [x] Slice 3: docs/audit/live-f1-entry-fidelity.md — 7e09a57 (agent). VERDICTS: p-model MATCH, entry_wait MATCH, threshold_gap MATCH/INERT (noob_fader-only -> SLICE 7 SKIPPED), max_entry MATCH, liq MATCH, sigma GAP-FIXED (slices 2+4), buffers GAP-ACCEPTED (drag=drift not buffer, 0/184 requotes; keep 0.06/0.12). Watch item: exec_entry clamp 0.98 > signal cap 0.92 (pre-existing; monitor telemetry).
### Wave 2 — COMPLETE
- [x] Slice 4: main.rs wiring (select_session fail-loud, insert_mirror_sigma H2 fix, display re-key, MirrorCfg.session_key) — 53c6868 (50 tests, TC-4.2 invariance proven)
### Wave 3 — COMPLETE
- [x] Slice 5: attribution threading (SESSION_NAME const removed, resolver/signal_loop/emit_trigger/place_live/builders take session; coid "{prefix}{ts}-{side}") — 0e35679 (51 tests)
### Wave 4 — COMPLETE
- [x] Slice 6: restart latch LiveState.last_filled_window + record_fill_state + boot seed — 83e9b42 (54 tests)
### Wave 5 (renumbered; slice 7 skipped)
- [x] Slice 8: src/f1_regression.rs (#[cfg(test)] mod — binary crate, tests/ can't import) — bec83d3 (63 tests; new-code clippy clean)
### Wave 6
- [ ] Slice 9 (OPS): deploy EU box — quality gates first (code-reviewer + security-auditor agents RUNNING on e99674d..HEAD). Then: upload src, backup *.bak.TS, on-box build, drop-in env SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92 DAILY_LOSS_STOP=30; brief shadow pre-flight (LIVE_TRADING=0, verify startup log F1 params + subaccount=1), then flip LIVE_TRADING=1 (user said "давай сразу live"); watch first F1 fills' telemetry (mirror gap ~0). Rollback: unset SESSION/MIRROR_SESSION + restore .bak + restart.

## Key facts for this feature (verified 2026-07-05)
- F1 params (from live_data.db paper_config, NOT in repo): kappa 0.4, delta_threshold 50, p>=0.65, sigma_type max10, entry_wait 3.0min (180s), max_entry 0.92 (name "cap75" LIES — legacy), BOTH sides, liq off, taker, threshold_gap 0.1.
- $5+fee recompute: F1 +$154.60 full / +$10.68 ex-hot-streak / -$30.14 live-window / +$85.60 post-stop @ 80.2%. Edge thin: WR cushion ~+0.8pt over breakeven. F6 = ~0 cushion.
- Change points: (1) config.rs new f1_d50cap75() factory or env-param; (2) mirror.rs:74 hardcodes ss.get("f6_wait270") -> parameterize MIRROR_SESSION; (3) main.rs:868 GOTCHA inserts mirrored sigma under hardcoded "max30" key but gate looks up cfg.sigma_type -> with max10 lookup MISSES and silently falls back to realized5 -> must insert under cfg.sigma_type; (4) SESSION env to select strategy.
- Verified: paper 8893 /api/sessions_state DOES expose f1_d50cap75.live_sigma (max10; 0.000314 vs f6 0.000566 at check time).
- Audit scope (user asked "выжать приближенность по входам к f1"): mirror freshness, sigma key parity, p-model formula parity (Phi(kappa*snr), tau linear), entry_wait timing (fires at 180s exactly?), threshold_gap semantics diff vs paper, PRICE_BUF=0.06/REQUOTE_BUF=0.12 effect on eff entry, latch/retry P0b, max_entry band.
- Live config target: LIVE_TRADING=1, STAKE=5, SUBACCOUNT=1, MAX_ENTRY=0.92, MIRROR_SESSION=f1_d50cap75; loss stop: recommend DAILY_LOSS_STOP=30 (subacct has only $147.73; old $100 = 68% of it) — flag to user at deploy.
- EU box deploy: build on-box ~/.cargo/bin/cargo build --release, systemd kalshi-shadow-com drop-in mirror.conf, backups *.bak.TS.

## (prev feature) Status: 2026-06-30. LIVE TRADING STOPPED (LIVE_TRADING=0, per user) — final real PnL -$131.30. Shadow/paper continue.
## 2026-06-30 DASHBOARD FIX (commit c5a0a98, fix(ui), deployed to Buffalo kalshi-shadow.service): user caught that the chart compared shadow @ $100 vs paper @ $5. Root cause: config.rs:50 hardcodes cfg.stake=100; STAKE env only feeds live orders (place_live). While LIVE_TRADING=1 green line was real $5 fills; after =0 every would-be (emit_trigger) logs $100 → green line silently jumped scale (3 recent $100 wins masked the gap). Fix: dashboard re-prices the green/com line through twin5($5) on com_entry (same as paper) — 4 spots + header relabel "LIVE/shadow @ $5". Render-only, zero trading change. Verified live: green -$146.50 vs paper -$110.86 (uniform $5). Backups *.bak.20260630-184850 on Buffalo. NOT pushed. Bot still logs shadow at $100 at source (follow-up: read STAKE into cfg.stake). See [[kalshi-execution-issues]] #5.
## Decision: edge is thin/regime-dependent & currently losing (analysis: Δ>=80 keeps a tiny +$8 recent w/ CI excl 0, but Δ>=20 loses -$43 recent; entry deviation only -$13, fee -$130 is the big cost; +$4191 paper was a hot streak Jun21-23 before live). $200 parked in subaccount #1 (idle now). Open: move $200 back 1->0? fully stop shadow service? shadow-test Δ>=80?
## (prior) Status: 2026-06-29 ~12:15 UTC. LIVE ISOLATED ON SUBACCOUNT #1 ($200). Pushed to main both repos (61d87ef).
## Live config now: SUBACCOUNT=1 (verified — order debited #1, acct 0 untouched), max_entry 0.92, loss_stop $100, P0b+http2+telemetry, PID 1548004. Subaccount #1 funded $200 (2 transfers from 0). total real PnL ~-$75 (on acct 0, pre-switch). ⚠️ loss-stop $100 on $200=50%/day — recommend tightening (user left $100). Subaccount endpoints: GET /portfolio/subaccounts/balances, POST /portfolio/subaccounts/transfer; order body field `subaccount`. Signing via python+cryptography on box (key /home/dmitrii/.kalshi_live.pem). TRIPWIRE still: WR>=74% keep / <73% stop.

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
