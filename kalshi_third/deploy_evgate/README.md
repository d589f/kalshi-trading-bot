# EV-gate paper A/B — what's deployed on the prod Kalshi paper engine

Deployed 2026-07-02 as a **forward test** of the EV-gate strategy found by `../sweep.py` /
`../reverify.py`. **Paper only — no real money.** The live-money trader (Rust `kalshi-shadow-com`)
is untouched.

## The strategy
Recalibrate the `third` signal `p` with a small Kalshi-trained logistic, then **buy iff
P(win) > entry_price** (`ev = P/entry - 1 > 0`) — a dynamic, price-aware gate replacing f6's fixed
thresholds (Δ≥20, p≥0.60, price≤0.92).

- `evgate_bake.json` — p-only logistic (2 weights). Session `s_evgate` (`strategy_type=logit_ev`).
- `evgate_full_bake.json` — 5-feature logistic (snr, p, entry, |Δ|, σ). Session `s_evgate_full`
  (`strategy_type=logit_ev_full`).

Backtest (pure holdout, dataset A Apr17–Jun23): EV-gate **+$12.85/tr vs f6 +$5.57** (~2.3×), well
calibrated OOS, more slippage-robust than f6. **Open risk:** edge leans on cheap contracts whose live
fillability is unproven — that's exactly what this paper A/B measures.

## Where it runs
- Prod paper engine `paper_compare_kalshi_15m` (`run_kalshi_15m.py`, screen `paper_compare_kalshi15m`,
  log `/tmp/paper_compare_kalshi15m.log`), DB `paper_compare_kalshi_15m/live_data.db`.
- `evaluator.py` here = the `_evaluate_signal_logit_ev` function added to
  `dashboard/paper_trading.py` (additive), plus two dispatch branches (`logit_ev`, `logit_ev_full`).
- Bakes copied to `dashboard/evgate_bake.json` and `dashboard/evgate_full_bake.json`.
- Backup of the pre-patch file kept on the box: `paper_trading.py.bak_evgate_<ts>`.

## Session config (both)
`ev_thr=0` (EV>0 ⇔ P>entry), `entry_wait_min=4.5`, `delta_threshold=0`, `max_entry_price=0.99`,
`sigma_type=max30`, `stake=100`, `trade_side=BOTH` (evaluator picks continuation side internally).

## Manage
- Pause: set session `paused=true`. Full stop: `screen -S paper_compare_kalshi15m -X quit`.
- Rollback code: restore `paper_trading.py.bak_evgate_<ts>` and restart the screen.
- Watch: `paper_trades WHERE session_id IN ('s_evgate','s_evgate_full')` vs `f6_wait270`.

## Redeploy from scratch
1. Retrain bakes: `python3 ../sweep.py` deps, or the fit block in `../reverify.py` (seed=42).
2. Insert `evaluator.py` before `check_all_sessions` in `dashboard/paper_trading.py`; add the two
   `elif strat == "logit_ev"/"logit_ev_full"` dispatch branches; drop the two bakes in `dashboard/`.
3. `py_compile` the file, `create_session("s_evgate"/"s_evgate_full", {...})`, restart the screen.

Credentials (prod SSH / Predexon key) are NOT stored here — supply them via your own environment.
