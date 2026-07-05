# Implementation Plan — exec-signal-anchor (PRD §4)

Re-anchor live IOC to the SIGNAL price: limit = signal_entry ± PRICE_BUF, count = round(stake/signal_entry),
NO synchronous pre-order book GET (order fires immediately), book fetched CONCURRENTLY (tokio::join! +
timeout 500ms const) as fail-open telemetry (exec_entry Option, None on failure), requote DISABLED in
signal mode. EXEC_ANCHOR=signal|ask env, default ask = byte-identical legacy.

MOTIVATION (2026-07-05, 20 real F1 fills): mirror +0.55c, drift +3.79c (ALL the degradation; the sync
book GET costs 100-250ms of it), walk -0.14c; 4/20 fills above 0.92 (to 0.983); day -$2.21 @ 85% WR.
Counterfactual signal+6c: -$0.59 (+$1.62); tight caps 0..2c WORSE (-$5.92, cut winners) → buffer exactly 6c.

## Deliverables (done)
- [x] PRD §4; use cases 16UC/76sc (UC-8 catch: book client timeout 8s vs sub-second order RTT → join
  would stall place_live 8s → GAP-FIXED timeout 500ms); QA all mapped; arch PASS w/ 5 binding resolutions.

## Architect binding
1. tokio::time::timeout(TELEMETRY_TIMEOUT_MS=500 const, not env) around telemetry GET inside join!.
2. ExecAnchor{Ask,Signal} + resolve_exec_anchor in config.rs; select_exec_anchor wrapper + error! in main.
3. place_live forks AFTER band gate; hoisted bindings (exec_entry: Option<f64>, first_limit_price,
   requoted=false, requote_limit_price=None, count, price, status/resp/lat); ask arm 1447-1513 VERBATIM;
   signal arm join!(create_ioc, timeout(500ms, get_orderbook)) + filtered_ask (0.50,0.98]; shared tail w/
   exec_entry threaded via POST-CONSTRUCTION overwrite (build_fill_record signature + 4 tests FROZEN;
   ask mode always Some = byte-identical). price var also read by no-fill log — signal sets price=limit.
4. Single bound eff<=signal+PRICE_BUF (<=0.98 at band edge) — NO second cap (cuts winners). "<=0.92" =
   population expectation, not per-fill law.
5. NO new ledger field; mixed-mode segmentation = runbook-recorded deploy ts (+requote==true ⟹ ask).

## Slices (all sequential waves)
### S1 (W1): config.rs ExecAnchor+resolver; main.rs select_exec_anchor + pure helpers
signal_yes_limit=(e+buf).min(0.99); signal_no_limit=((1-e)-buf).max(0.01); signal_count=round(stake/e)
clamp[1,max]; exec_* twins of legacy formulas (byte-identity assertions); filtered_ask(0.50,0.98];
TELEMETRY_TIMEOUT_MS=500. All #[cfg_attr(not(test),allow(dead_code))] until S2 wires. Tests: selector
matrix (unknown NEVER Signal), pricing boundaries (0.92+0.06→0.98; buf0; huge buf→0.99), counts.
[SECURITY pre-review]
### S2 (W2): place_live fork + threading main()->signal_loop->place_live + tail Option [SECURITY+ARCH]
Thread anchor param first (cargo check), then fork per sketch. Signal arm: no book GET before create_ioc;
join! concurrent; requote never (false/None on ALL outcomes); order Err → OrderError path w/ requoted=false.
Tail: rejected/no-fill r.exec_entry = exec_entry (Option); fill: build_fill_record(.., exec_entry
.unwrap_or(entry), ..) then fr.exec_entry = exec_entry overwrite. Tests: timeout seam (pause/advance),
filtered_ask edges, requote-never, full suite green.
### S3 (W3): f1_regression.rs guard
exec_* == legacy formulas grid both sides; signal_count >= exec_count when signal<=exec; hard bound
signal_yes_limit(e,b)<=e+b<=0.99; f6/shadow-twin untouched pins; record-shape roundtrip ask vs signal.
Full suite + clippy -D warnings (new code clean).
### S4 (W4): OPS runbook docs/audit/exec-signal-anchor_rollout.md [new] + deploy
EU drop-in += EXEC_ANCHOR=signal; restart first ~60s of 15-min window; RECORD exact deploy ts (mixed-mode
ledger boundary); rollback = remove env + restart; watch: eff<=signal+0.06 per fill, drift tail >6c gone,
no-fill rate + coverage vs paper F1, S3 equal-entry resolve recheck (eff==signal now common).

## AC (PRD 4.5): AC-1 default byte-identical; AC-2 selector matrix; AC-3 signal limit/count; AC-4 no
pre-order GET; AC-5 exec_entry Some on success/None on failure, order unaffected; AC-6 requote never +
retry re-anchors same fixed limit; AC-7 deployed fills eff<=signal+0.06; AC-8 75 legacy tests green.

## Risks
Real-money order path (ask arm verbatim + byte-identity tests + default ask + fail-loud selector);
more no-fills on fast runs (accepted; earlier arrival + P0b retry recover some); sizing shift (round
(5/signal) slightly more contracts, MAX_COUNT 15 cap); single-day counterfactual noise (justification is
structural: drift = the whole gap). No new deps; orders.rs/rest.rs/ledger.rs/dashboard.rs/mirror.rs UNTOUCHED.
