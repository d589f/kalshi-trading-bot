# exec-signal-anchor — EU rollout runbook

Feature: PRD §4. Signal-anchored IOC execution (limit = signal_entry ± PRICE_BUF, no pre-order
book GET, concurrent 500ms-bounded telemetry, requote disabled). Branch feat/exec-signal-anchor.

## Preconditions
- [x] Slice-3 suite green (83 tests) + clippy new-code clean.
- [x] §2 F1 go-live audit passed (docs/audit/live-f1-entry-fidelity.md).
- [x] Security/code review of the diff: PASS, 0 critical/major; 2 MINORs fixed pre-deploy (clamp panic guard on MAX_COUNT<1, stale dead_code markers) — e1cc5b9.

## Deploy (EU box 34.32.177.126, systemd kalshi-shadow-com)
1. Backup: `cp target/release/kalshi_bot target/release/kalshi_bot.bak.<TS>` + `src.bak.<TS>`.
2. Upload src (LF-normalized), verify sha256 vs local HEAD, `cargo build --release` on box (~10 min).
3. Drop-in `/etc/systemd/system/kalshi-shadow-com.service.d/mirror.conf` — ADD one line:
   `Environment=EXEC_ANCHOR=signal`
   (alongside SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 STAKE=5 SUBACCOUNT=1 MAX_ENTRY=0.92
   PRICE_BUF=0.06 DAILY_LOSS_STOP=30 MAX_TRADES_DAY=96 LIVE_TRADING=1).
4. `daemon-reload` then restart **in the first ~60s of a 15-min window** (F1 cannot fire before 180s;
   the persisted fill latch protects a filled window either way).
5. Verify startup log: `order client ready | ... exec_anchor=Signal` and the F1 session line.

## Deploy timestamp (RECORD — the authoritative ask-vs-signal ledger segmentation boundary)
- **EXEC_ANCHOR=signal live since (UTC): 2026-07-05T21:00:03Z** (restart at window start; PID 1187023; startup log shows exec_anchor=Signal)
- Secondary discriminators for mixed-mode analysis: `requote==true` ⟹ ask-mode row;
  in signal rows `first_limit_price == round(signal_entry ± 0.06, 2)` exactly.

## Rollback
- Remove the `Environment=EXEC_ANCHOR=signal` line (or set `=ask`) → `sudo systemctl daemon-reload
  && sudo systemctl restart kalshi-shadow-com` → byte-identical legacy ask path. No data migration
  (append-only ledger; no schema change). Binary rollback (if code suspect): restore
  `kalshi_bot.bak.<TS>` + restart.

## Post-deploy watch (first ~20 fills)
- [ ] HARD per-fill bound: every fill `eff <= signal_entry + 0.06` (`<= 0.98` worst case at band
      edge). `<= 0.92` is a POPULATION expectation, not a per-fill law (entries in (0.86, 0.92]
      may legitimately fill up to 0.98).
- [ ] drift right-tail: no more `eff - signal > 6c` rows (pre-fix: 4/20 fills, worst +13.3c).
- [ ] no-fill rate + coverage vs paper F1 windows (pre-fix coverage 20/21; counterfactual predicted
      5-7/20 no-fills WITHOUT the latency win — actual should be better; P0b retries recover some).
- [ ] latency_ms of fills should drop (no book RTT in path).
- [ ] S3 equal-entry live/shadow resolve disambiguation re-check (eff == signal_entry is now common).
- [ ] Compare day PnL vs paper-F1 twin over the same windows after ~1 day.

## Open items / risks (from PRD 4.10)
- PRICE_BUF has no startup range validation (per-order clamp only) — acceptable, documented.
- Single-day counterfactual noise: justification is structural (drift +3.79c = the whole gap).
- If no-fill rate spikes on fast runs, consider PRICE_BUF 0.06 -> 0.08 for F1 (separate decision).
