## 2026-07-27 DASHBOARD "since update" FIX (Buffalo) + live-bot diagnosis.
(A) WHY LIVE STOPPED TRADING: bot never crashed — active since 07-16, 0 restarts, LIVE_TRADING=1, no loss-stop/daily-cap hit. Last real fill 07-24 21:03. Three compounding causes: (1) LOW-VOL REGIME — 5762 `DELTA LOW` vs 17 signals in 24h, BTC moving $26-67 per 15m window against the $50 threshold, so signals fell from ~19/day to ~1.5/day; (2) the [0.65,0.75] band now rejects what's left — on 07-23/24 signals priced 0.68-0.75 (in band, all filled), since then they price 0.63-0.65 (below band); (3) BOUNDARY BUG in main.rs:1584 `!(lcfg.min_entry < entry && ...)` — lower bound is STRICT, so entry == 0.65 is rejected (log literally prints `out of (0.65, 0.75]`). TWO of the last three signals were exactly 0.65 and died on this. Quick fix = MIN_ENTRY=0.64 (env only, no rebuild); proper fix = `<=`. Also found: 28.4% of evaluations see an EMPTY orderbook (yes_ask=1.0 AND no_ask=1.0) -> 458 of 903 "HIGH" skips are the 1.00 artifact, not a real price; and ~25s market-discovery lag at each window roll (`no open KXBTC15M market`, ~100 warn/hour).
(B) REAL P&L (ground truth = Kalshi /portfolio/settlements; NOTE: portfolio endpoints must be signed on the path WITHOUT the query string, else 401; fields are `revenue` in CENTS and `yes_total_cost_dollars`/`no_total_cost_dollars`/`fee_cost` in DOLLARS): since 07-16 10:30 marker LIVE **+$69.51** (119 settlements, WR 84%, fees $10.65); since 07-14 +$63.12 (200, WR 82%); ALL-TIME KXBTC15M **-$220.13** (881 settlements, fees $74.37). Validated against subaccount#1 balance $166.17. Paper F1 same period: 315 trades, +$3232.15 at its native $100 stake (= +$161.61 per $5), WR 87%.
(C) DASHBOARD BUG (the "+30 yesterday, +16 today" the user reported): dashboard.rs compare() rows are newest-first and were `out.truncate(300)` = ~3 days of 15-min windows, while the zero-view CUT is the newest deploy marker (07-16). Once the 300-row tail slid past that marker, early windows silently fell off and the since-update total SHRANK daily even though the curve only rose. Confirmed arithmetic: losing 07-22 (+$14.03) took ~+$30 -> +$15.87. FIXED on Buffalo: cap 300 -> 3000 (~31 days, env-overridable `DASH_ROWS`) + null-field stripping (payload 2.02MB->1.41MB, 11.2s->6.9s). SECOND cap found and fixed: /root/push_paper_f1.py and push_paper_f6.py on EU had `limit 300` in their SQL, so the pink F1 line only reached back to 07-20 — raised to 3000 (now pushes 1799/2277 rows). Buffalo now: 2688 compare rows spanning 06-27..07-27; since-update LIVE reads +$70.77 over 95 windows (matches the +$69.51 settlement ground truth; delta is window-start vs settled-time bucketing). Buffalo build ~48s, service `kalshi-shadow`, src /root/kalshi_rs, backups *.bak.20260727-080603. EU binary NOT rebuilt (render-only change; EU rebuild is 14-18min thin-LTO + ~30-40min paper warmup on the REAL-MONEY box — not worth it).

## 2026-07-20 XO exploration + PAPER SIM DEPLOYED. (1) XO "BTC 5min Pulse" = live 5-min up/down vs open, tradable WHOLE window (my earlier "pre-start only" was WRONG — user's screenshot proved live mid-window book). XO fee = 0% maker+taker CONFIRMED 3 ways; settles Chainlink TWAP (openingTwap vs closingTwap ~60 samples); thin ($360-1370/side, intermittent). See [[xo-market-viability]]. (2) 5-min BTC signal (90d, both feeds, lookahead-clean, reproduced from scratch): MOMENTUM loses 46-49%; ANTI-momentum (fade completed move at fresh open) 53-54% robust, NOT bounce artifact. BUT intra-window fade-to-open LOSES 17-29% (displacement sticky, already priced) — so naive XO fade unprofitable; edge only if XO mis-prices (untested). (3) Live 3-venue orderbook recording (books5m.png): XO median depth $682 (dips $4) vs Poly $154k vs Kalshi $250k; XO |mid−fair| ≈0.031 (only slightly worse than tight venue, NOT wild mispricing — the chart swings are mostly real fair-value moves). Harness: kalshi_third/xo_momentum.py, xo_fade_open.py; scratchpad/simple_poll.py (3-venue REST poller), plot_books5m.py. XO outcome-name gotcha: outcomes[].name is null now → UP = outcomes[0] by INDEX. (4) DEPLOYED xo_paper.py on EU under systemd unit `xo-paper` (Restart=always, isolated, live bot untouched): papers 3 rules {mom(F1 |Δ|≥$50 @60s), fade(|mid−fair|≥0.06), flatUP} on BOTH XO(5m) + Kalshi(15m), settle on binance (chainlink_price field is NULL in engine → basis flag), fee 0 XO / kfee Kalshi, n=round(5/px). Writes ~/xo_paper/trades.csv (~140KB/day, tiny). cum P&L recomputed from CSV on restart. joined-late guard: skip windows where op sec>20. First valid windows ~13:55Z 20.07. (5) POLY 5m DOES exist — I was wrong twice: Polymarket "btc-updown-5m-<epoch>" IS a real 5-min market; `startDate`=deploy time (24h early), real window = `eventStartTime`→`endDate` (5min), slug epoch=unix(eventStartTime) on 300-grid; Predexon lists them ~24h ahead so use Gamma `?slug=btc-updown-5m-<floor(now/300)*300>` for the LIVE window; clobTokenIds[0]=UP. Integrated as POLY leg (fee=0, same as XO). Also XO outcome names are null → UP=outcomes[0] by index. (6) LIVE DASHBOARD deployed: EU runs xo_dash.py (systemd unit `xo-dash`, http 127.0.0.1:8896, reads trades.csv, matrix venue×rule + per-venue cum-P&L canvas charts + trade log, auto-refresh 15s) + reverse-tunnel `xo-dash-tunnel` (systemd, ssh -R 8896→Buffalo via id_dash_tunnel, mirrors existing dash-tunnel.service; Buffalo firewall inactive). **Live URL: http://23.95.217.78:8896/** (Buffalo, same box as user's 8890-8894 dashboards; did NOT touch dash-tunnel.service). Buffalo access = root@23.95.217.78 (password, given by user). All 3 units Restart=always. Data tiny/noisy first day — judge over 1-2 days. (7) SWITCHED xo_paper.py to WS: XO (wss://orderbooks.xo.market/ws/market, subscribe up+dn tokens, event_type=book) + Poly (wss://ws-subscriptions-clob.polymarket.com/ws/market) live sub-second books cached in BOOKS{token:(bb,ba,nb,na,dep,ts)}; REST fallback when cache >5s stale; Kalshi stays signed-REST @2s (its WS = auth+delta reconstruction = crash surface, 15m reference sampled rarely). Arch: asyncio(xo_ws+po_ws) main + discover() thread + paper_loop() thread (blocking REST fallback OK there); outer while-loop restarts ws_main on death. Confirmed live: ws_xo=0s ws_po=0s books=12. (8) WATCHDOG + reboot-persistence: converted xo-paper/xo-dash/xo-dash-tunnel from transient systemd-run to /etc/systemd/system/*.service unit files (enabled, Restart=always, survive REBOOT). xo_paper writes /home/dmitrii/xo_paper/heartbeat each loop; /home/dmitrii/xo_paper_watchdog.sh (root cron every 1min, existing cron entries preserved) restarts xo-paper if inactive OR heartbeat >120s stale (catches hangs, not just crashes). tunnel unit runs User=dmitrii (its key/known_hosts).

## 2026-07-16 MIN_ENTRY edge-filter (commit 08388b9, pushed both remotes): conditional-edge search on F1/BTC found the only coherent robust bucket = entry price 0.65-0.75 (+0.097/tr both halves, survives haircut; mechanism = moderate favorites underpriced). Added LiveCfg.min_entry (env MIN_ENTRY default 0.50) → live-only band [MIN_ENTRY, MAX_ENTRY]; place_live + classify_outcome + test. EU deploy IN PROGRESS: main.rs uploaded (bak main.rs.bak.20260716), env set MIN_ENTRY=0.65 MAX_ENTRY=0.75 (bak mirror.conf.bak.20260716-band), build running as systemd unit `eub` (waiter b0k5fxkgi) — THIS build also finally carries fee-fix 41ad0db that never reached EU. PENDING: window-aligned restart after build. F1 base -$2.7/day → filtered ~breakeven (loss-control, not alpha; edge=sub-fee). Also this turn: ETH15M edge = not deployable (regime luck, sub-fee); SOL15M 19/20 negative (thinner≠less efficient); Poly NOT zero-fee (same as Kalshi); MM idea = untested but momentum edges are taker-edges (can't capture as maker), pure-MM needs live book+trade data. See [[kalshi-edge-search-honest-result]].

## 2026-07-10 INCIDENT + fix (commit 2c97fb0): paper stopped 12:00-14:15Z (my BOOK_STALE gate too strict: ticker is CHANGE-based, silence≠stale → 29,661 skips; fixed with alive-stamp in kalshi_ws.py on every frame/recv-timeout while connected + gate default 4.0s), then EU DISK 100% FULL at 14:07Z → engine died, keepalive relaunched corpse every min. Freed ~2GB (live_trading+/tmp logs → .tail, journal vacuum 100M; NO DBs touched). Engine up 18:5xZ, WARMUP till ~19:55Z. LIVE off since 00:00:30Z = by design (auto-off timer). STILL FLAGGED to owner: /root/paper_compare live_data.db 2.2GB growing + 1.2GB May/June baks + cargo target 771M — disk WILL refill in ~1-2 weeks.
## 2026-07-09 LIVE-ON no-stop + divergence investigation (commit 7820824): user ordered LIVE back ON without loss stop (16:30Z 08.07, DAILY_LOSS_STOP=1000, auto-off timer live-off-jul10 @ 2026-07-10 00:00:30Z restores 0/30). Red-vs-pink divergence explained: exec parity fine (-3.06/43tr, gap +0.81c); 5 "missed winner" windows = ENGINE PHANTOM FILLS AT WS SPEED (paper 0.54-0.81, real ask +8..26c 100-300ms later, ledger proof; bot fired 0.1-0.3s after engine — mirror-the-entry idea moot, already built). Bound +6c correctly refused chase. KILL_HOURS narrowed to 14 (10:00Z restart, bak mirror.conf.bak.20260709-kh14). Engine binance-WS backoff 30s->5s patched on disk (bak ws_consumers.py.bak_backoff_20260709), engine restart scheduled 00:06Z (engine-restart-backoff2, screen quit + pkill run_kalshi_15[m], cron keepalive revives). Dash markers added for both deploys. + STALE-BOOK fill gate patched into engine _try_open (commit c10384b, repo copies in tools/paper-engine/): paper fill REFUSED when kalshi_ws_ts age > BOOK_STALE_SECS (2.0s default) — closes the 4-7s WS-death blind window (kalshi-ws keepalive drops 9x/day); logs "[paper] STALE-BOOK skip". Activates with the same 00:06Z engine restart. Does NOT fix sub-300ms quote death (physics).
## 2026-07-08 ROUND 2 POLYMARKET (commit 3ed613a, both remotes): LIVE_TRADING=0 deployed (EU, bak mirror.conf.bak.20260708-liveoff). Predexon key from user -> kalshi_third/.env (untracked). Poly 15m tape 1560 windows Jun22-Jul8 (~1500 prints/win, zero fee), frozen params = pure OOS: F1 ev -0.017/tr (null replicated 2nd venue); d75/ci5/f6 "plus" configs killed by skeptics (approx-DOWN synthetic fills carried PnL / one trending week / best-of-10 @1.6sigma); MAKER DEAD (filled WR 69-75 vs BE 74-78, unfilled win 82-100 = adverse selection; pure MM -26/day); calibration +1pp only; cross-venue NULL. FINAL VERDICT: 15m BTC updown efficiently priced both venues, no edge at our latency. Watch-only (frozen, future tape): F1_d75/ci5 real-tape, Poly UP 0.8-0.9 bucket. Poly tape cache 4GB local (kalshi_third/cache), fetcher stopped at ~1560.
## 2026-07-08 EDGE-SEARCH ITERATION (commit 488f81c, both remotes): found+fixed 60s LOOKAHEAD in px.btc_path (klines keyed by open, stored close) — ALL June third-sweep results were inflated. Refetched 82d Kalshi history (7695 markets, all 15 minutes for 6230 windows, fetch_kalshi_hist.py works from US box only). Workflow 17 agents: momentum honest = F1 analog train -2.53/d test +0.67/d, f6 train -5.64/d; 14/472 configs positive both periods (chance); fade 0/648; regime-gate anti-predictive; hours: only h14 UTC honestly toxic (live KILL_HOURS {4,8,22} not supported by 82d!). All 6 finalists killed by skeptics: stale-quote capture, zero settlement edge vs market mid. LEAD (unverified, pre-fix data): market MIDs underprice continuation ~10c (bucket 0.7-0.8 realized 0.91) => maker-side re-check on fixed core = round 2. VERDICT: no taker edge at our latency; recommend live OFF.
## 2026-07-07 DASH SINCE-UPDATE VIEW (commit 91d0deb, deployed Buffalo ~09:4xZ, pushed both remotes + main FF): chart now DEFAULTS to "с апдейта" — only windows >= last UPDATES marker (2026-07-06T21:00 KILL-HOURS), ALL 5 lines re-zeroed to $0 at that cut (LIVE = raw lv_pnl cum from 0, pink-anchor logic is FULL-mode only). New "FULL история" checkbox in fbar restores old full-history view. Render-only; 88 tests; JS node-checked; verified live on :8890. Rollback: dashboard.rs.bak.20260707-sinceupd on Buffalo.
## OPEN DECISION (user hasn't answered): stop live now vs 3rd tripwire day — night Jul 6→7 LIVE -$27.00 WR67% vs paper same-windows -$25.67 (parity, loss is regime); total -$155.88, subacct ≈$123.

## Feature: live-f1-strategy — switch live trader from f6_wait270 to f1_d50cap75, STRAIGHT TO LIVE (user decision 2026-07-05), $5 stake, subaccount #1 ($147.73 there). Plus entry-fidelity audit (maximize signal parity with paper F1).

## Branch: feat/live-f1-strategy (off feat/dashboard-f1-compare @ 38bee0c)

## Feature: exec-signal-anchor — IOC limit = signal±PRICE_BUF, no pre-order book GET (concurrent telemetry, timeout 500ms), requote off in signal mode. EXEC_ANCHOR env default ask=legacy. Branch feat/exec-signal-anchor (off dashboard-f1-live-line 706ad1d), bootstrap c4c5dde, plan .claude/plan-exec-signal-anchor.md.
## Status: exec-signal-anchor COMPLETE + follow-ups deployed 06.07: anchor-freeze f16fc84 (retry-chase killed, 10:30Z), fast tick TICK_SECS=0.1 f3807b9 (12:00Z), PAPER ENGINE WS BOOK FEED (13:15Z — tools/paper-engine/, engine's REST book lagged 8-16c on dumps => phantom paper fills; pink overstated ~$11/day; now sub-second). Dashboard: F1-real dashed line 893aa4d; light theme. ENGINE RESTART GOTCHA: ~30-40min WARMUP, sessions frozen, live_sigma=0, bot fails closed — restart only when acceptable. Missed windows 13:15-13:55 due to this warmup (safe).
## Slices: S1 848d6d2 (selector+helpers), S2 3375cca (place_live fork: ask arm verbatim / signal arm join!(create_ioc, timeout500ms book GET), requote off), S3 3c76186 (regression guard), review PASS 0crit/0major -> e1cc5b9 (MAX_COUNT<1 clamp panic guard + markers) + runbook w/ deploy ts. 84 tests.
## WATCH (first ~20 fills): eff <= signal_entry+0.06 per fill; drift tail >6c gone; no-fill rate vs paper F1 coverage (pre-fix 20/21); latency_ms drop (no book RTT); deploy-ts boundary for ask/signal ledger segmentation = 2026-07-05T21:00:03Z.
### W1 S1: config.rs ExecAnchor+resolver; main.rs select_exec_anchor + pure pricing helpers + TELEMETRY_TIMEOUT_MS [SECURITY] — pending
### W2 S2: place_live fork (ask verbatim / signal join!) + anchor threading + tail exec_entry Option overwrite [SECURITY+ARCH] — pending
### W3 S3: f1_regression guard (byte-identity grid, hard bound, clippy) — pending
### W4 S4: runbook docs/audit/exec-signal-anchor_rollout.md + EU deploy (drop-in EXEC_ANCHOR=signal, restart first 60s of window, RECORD deploy ts, watch eff<=signal+0.06) — pending

## (prev) Feature: dashboard-f1-live-line — separate LIVE F1 chart line (real $5, anchored at pink paper-F1) + honest shadow twin (green stays shadow) + table US column -> LIVE. Branch feat/dashboard-f1-live-line (off feat/live-f1-strategy), bootstrap 26326e6, plan .claude/plan-dashboard-f1-live-line.md.
## Status: COMPLETE — deployed both boxes 2026-07-05 ~14:05 UTC
### Wave 1
- [x] S1: live flag + flag-scoped resolve/retain + replay — a55db5b (71 tests)
### Wave 2
- [x] S2: compare() com_*/lv_* split + lv aggregates — 1d7d6a3 (74)
- [x] S3: twin_window latch + twin emit in live branch — 76b0b57 (72)
### Wave 3
- [x] S4: chart LIVE line #ff7b72 anchored at pink F1 + table US->LIVE + cards + tooltip — 1070d8c
### Wave 4 + review
- [x] Review found MAJOR-1: pre-feature live fills (LiveTriggerRecord live:true) had flag-less resolves -> replay couldn't match -> LIVE line would render EMPTY. Fixed: resolve() returns bool, legacy resolve tries shadow then falls back to live row — 8dd193a (75 tests). MINOR-1 acknowledged: dash.triggers uncapped, twin ~doubles growth, /shadow_com 4MB body cap hits in ~100d — FOLLOW-UP needed (retention cap).
- [x] S5 deployed: same source both boxes (EU rebuild 9m33s after fix re-upload; Buffalo needed current Cargo.toml too — its old one lacked reqwest http2 feature, E0599). EU restarted 14:02:07 UTC inside window <180s (no F1 fire possible), Buffalo 14:05. VERIFIED live: /stats has 7 lv rows (today's F1 fills, 100% match_lv, all positive), 287 shadow rows; HTML has _S.lv/#ff7b72/LIVE column. Backups: EU src.bak+kalshi_bot.bak.20260705-161144, Buffalo .bak.20260705-161700.
- WATCH: next F1 trigger must produce BOTH twin (green steps) + live fill (LIVE line steps); no-fill window -> green steps, LIVE flat.

## (prev feature) Status: COMPLETE — F1 LIVE ON PROD since 2026-07-05 11:45 UTC (PID 709583). Feature done, all 9 slices (7 skipped by audit verdict).
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
