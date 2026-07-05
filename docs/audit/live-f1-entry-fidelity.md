# Entry-Fidelity Audit — Live F1 Strategy Switch (`live-f1-strategy`)

- **Date:** 2026-07-05
- **Feature:** PRD Section 2 (`docs/PRD.md`) — switch the Rust live trader (`kalshi_rs`)
  from `f6_wait270` to `f1_d50cap75`, re-enter LIVE at $5 on subaccount #1.
- **Scope:** signal parity between the Rust live gate (`kalshi_rs/src/engine.rs` +
  `signal.rs` + `mirror.rs` + `main.rs`) and the Python paper engine's F1 session
  (`/root/paper_compare_kalshi_15m/dashboard/` on the EU box `34.32.177.126`,
  read via SSH on 2026-07-05; live DB config read from
  `/root/paper_compare_kalshi_15m/live_data.db` table `paper_config`).
- **Requirement:** PRD 2.3 FR-8 / AC-5. This document is a go-live gate for
  `LIVE_TRADING=1`.

F1 session config as deployed in the paper DB (`paper_config`, `session_id='f1_d50cap75'`):

```
cfg_kappa=0.4  cfg_delta_threshold=50.0  cfg_p_model_threshold=0.65
cfg_sigma_type=max10  cfg_tau_mode=linear  cfg_trade_side=BOTH
cfg_max_entry_price=0.92  cfg_entry_wait_min=3.0  cfg_liq_filter=off
cfg_vol_imb_kill=0.0  cfg_kill_hours=''  cfg_ofi_align=off
cfg_strategy_type=pmodel  cfg_exec_mode=taker  cfg_threshold_gap=0.1
```

`cfg_strategy_type='pmodel'` + `cfg_exec_mode='taker'` routes F1 to the classic
`_evaluate_signal` path (`paper_trading.py:865-894`, `check_all_sessions` dispatch) —
that path, and only that path, is what the Rust gate must reproduce.

## Summary verdict table

| # | Item | Verdict | Evidence (one line) |
|---|------|---------|---------------------|
| 1 | p-model formula parity | MATCH | `paper_trading.py:402-415` == `signal.rs:180-200` + `engine.rs:121-157`: `p = Phi(kappa*|Δ|/(σ·price_ref·max(τ,0.1)))`, τ normalized 0..1 both sides, None-p passes both sides |
| 2 | entry_wait timing (180 s) | MATCH | `paper_trading.py:345-346` `elapsed_min < _ew` == `engine.rs:97-98` `s.elapsed_min < cfg.entry_wait_min`; both strict `<`, fire allowed at exactly 180.0 s, same window_start, same host clock |
| 3 | threshold_gap = 0.1 semantics | MATCH | Inert for F1: `threshold_gap` is read only by the `noob_fader` strategy (`paper_trading.py:554`) and a display string (`:1263`); the classic pmodel path (`:337-465`) never references it — Slice 7 contingency not triggered |
| 4 | max_entry band (0.92) | MATCH | `paper_trading.py:429` `entry_price > max_entry_price` == `engine.rs:197-198` `entry > cfg.max_entry_price`; both reject strictly-greater → 0.92 inclusive; `main.rs:1222` re-checks `(0.50, 0.92]` before the order |
| 5 | liq_filter off parity | MATCH | Paper skips spread/depth unless `liq_filter == "on"` (`paper_trading.py:595`, F1 = `off`; also `config.py:26 VENUE="limitless"` force-skips) and `vol_imb_kill=0.0` skips; Rust `engine.rs:247-268` skips both under `liq_filter=false`, `vol_imb_kill=0.0` |
| 6 | sigma source (mirrored max10) | GAP-FIXED | Today `mirror.rs:74` hardcodes `"f6_wait270"` and `main.rs:868` inserts under `"max30"` → F1 would silently trade `realized5_pmin`; fixed by plan Slices 1 (f1 factory `sigma_type="max10"`), 2 (session-keyed fail-closed fetch), 4 (insert under `cfg.sigma_type`, TC-4.2); paper `max10` definition == Rust `max{win}` (`helpers.py:124-138` vs `signal.rs:121-142`) |
| 7 | execution buffers (PRICE_BUF=0.06 / REQUOTE_BUF=0.12) | GAP-ACCEPTED | Buffers are execution-side, out of scope per PRD 2.9; ledger (184 live f6 fills): entry drag lives in drift (mean +0.97c, p90 +4c, max +30c), not in the buffer (walk mean −0.40c); recommendation: keep 0.06/0.12 for F1 go-live, re-evaluate after ~50 F1 fills |

## 1. p-model formula parity — MATCH

**Paper** (`/root/paper_compare_kalshi_15m/dashboard/paper_trading.py:401-420`, classic
path of `_evaluate_signal`):

```python
sigma_type = session_cfg.get("sigma_type", "realized5_pmin")
sigma = shared.get("sigmas", {}).get(sigma_type) or shared.get("sigma")
tau = shared.get("tau", 2)
price_ref = shared.get("binance_price", 70000)

if sigma and sigma > 0 and price_ref > 0:
    sigma_usd = sigma * price_ref
    if session_cfg["tau_mode"] == "linear":
        f_tau = max(tau, 0.1)
    elif session_cfg["tau_mode"] == "sqrt":
        f_tau = max(math.sqrt(tau), 0.1)
    else:
        f_tau = 1.0
    snr = abs(delta) / (sigma_usd * f_tau)
    p = _phi(session_cfg["kappa"] * snr)
else:
    p = None

pt = session_cfg["p_model_threshold"]
if p is not None and p < pt:
    return f"P-LOW ({p:.1%}<{pt:.0%})", None, None, None
```

with `_phi` (`paper_trading.py:80-82`):

```python
def _phi(x):
    import math
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))
```

and τ built normalized 0..1 (`tasks.py:199`):

```python
tau = max((w_e - datetime.now(timezone.utc)).total_seconds() / 60 / WINDOW_MINUTES, 0.1)
```

**Rust** (`kalshi_rs/src/signal.rs:180-200`):

```rust
let sigma_usd = sigma * price_ref;
let f_tau = match tau_mode {
    TauMode::Linear => tau.max(0.1),
    TauMode::Sqrt => tau.sqrt().max(0.1),
    TauMode::None => 1.0,
};
let snr = delta_from_open.abs() / (sigma_usd * f_tau);
let p = phi(kappa * snr);
```

with `phi` (`signal.rs:16-18`) `0.5 * (1.0 + libm::erf(x / SQRT_2))` — `libm::erf` is
the same libm routine CPython's `math.erf` calls — and the same normalized τ
(`kalshi_rs/src/window.rs:70-78`, `(w_end - now)/60/WINDOW_MINUTES`, floored 0.1;
the 0.1 floor is applied a second time inside `f_tau` on both sides). The threshold
comparison is identical: strict `p < threshold` rejects, and a **None p passes** on
both sides (`paper_trading.py:420` `if p is not None and p < pt`; `engine.rs:152-157`
`if let Some(pv) = p { if pv < cfg.p_model_threshold { skip } }`). Delta source is
`delta_from_open` on both sides; in MIRROR mode the Rust gate consumes the paper's
own `delta_from_open`, `binance_price`, `tau`-inputs (window bounds) and book
verbatim (`main.rs:856-886`), so there is no rounding divergence to audit — the
inputs are the same bytes. Sigma lookup semantics: paper's `or`-fallback treats
`0.0`/missing as falsy; Rust `.filter(|v| *v > 0.0).or(s.sigma)` (`engine.rs:121-126`)
— same behavior for the only reachable cases (missing/zero/positive), and the
`<= 0.0` case is unreachable under the fail-closed mirror fetch (see item 6).

## 2. entry_wait timing (180 s) — MATCH

**Paper** (`paper_trading.py:344-347`):

```python
elapsed_min = shared.get("elapsed_min", 0)
_ew = float(session_cfg.get("entry_wait_min", 3) or 3)
if elapsed_min < _ew:
    return f"WAIT ({elapsed_min:.1f}/{_ew:.1f})", None, None, None
```

with `elapsed_min` in real minutes since window start (`tasks.py:192`):

```python
elapsed_min = (datetime.now(timezone.utc) - w_s).total_seconds() / 60
```

**Rust** (`kalshi_rs/src/engine.rs:97-99`):

```rust
if s.elapsed_min < cfg.entry_wait_min {
    skip!("WAIT ({:.1}/{:.1})", s.elapsed_min, cfg.entry_wait_min);
}
```

with `elapsed_min` = `(now - window_start)/60` (`window.rs:81-86`) and
`entry_wait_min = 3.0` in the F1 factory (plan Slice 1, mirroring
`cfg_entry_wait_min=3.0` in the paper DB).

Both gates use strict `<`: the boundary instant `elapsed_min == 3.0` (exactly 180 s)
is **allowed to fire on both sides** — inclusive lower bound, identical semantics.
In MIRROR mode the Rust window_start is the paper's `window_start_utc`
(`main.rs:861-866`), and both processes run on the same EU host (MIRROR_STATE_URL =
`127.0.0.1:8893`), so elapsed is measured from the same origin on the same clock.
Residual: tick-cadence jitter (paper state_updater loop vs Rust 0.3 s loop) can
shift the actual first-fire tick by under a second on either side; this is inherent
to the mirror architecture (freshness-guarded by `age_secs <= 5`) and does not
change which windows fire.

## 3. threshold_gap = 0.1 semantics — MATCH

This was the go-live blocker (PRD 2.10 open question; architect constraint 6).
Resolved: **`threshold_gap` is inert for F1's config** — it cannot change which side
fires or whether a fire happens on the classic pmodel/taker path.

Every reference in the paper engine
(`grep -rn "threshold_gap" /root/paper_compare_kalshi_15m --include=*.py` — 5 hits,
all in `dashboard/paper_trading.py`):

| Line | Context | Effect on F1 |
|---|---|---|
| 103 | `DEFAULT_CONFIG = { ... "threshold_gap": 0.10, ... }` | config default only |
| 190 | `update_session_config` float-cast whitelist | config plumbing only |
| 237 | `restore_from_db` float-cast whitelist | config plumbing only |
| 554 | `TH = float(session_cfg.get("threshold_gap", 0.10))` | **inside `_evaluate_signal_noob_fader`** (function spans 466-588) |
| 1263 | `th = cfg.get("threshold_gap", 0.05)` | inside `get_session_info`, and only in the `strategy_type == "noob_fader"` display branch — builds a formula string for the dashboard |

The only behavioral use is line 554, in the ML `noob_fader` strategy, where the fire
condition is `gap = p_cal - market` vs `±TH` (`paper_trading.py:565-583`) — a dead
zone around the *market price* for a calibrated ML model output. F1 is dispatched by
`check_all_sessions` (`paper_trading.py:876-894`) on `strategy_type`:

```python
strat = cfg.get("strategy_type", "pmodel")
if strat == "noob_fader": ...          # NOT F1 (F1: cfg_strategy_type='pmodel')
elif strat == "ml_evgate": ...
elif strat == "logit_ev": ...
...
elif cfg.get("exec_mode", "taker") == "maker": ...  # NOT F1 (F1: cfg_exec_mode='taker')
else:
    sig_text, side, entry, conf = _evaluate_signal(cfg, shared)   # <-- F1's path
```

The classic `_evaluate_signal` (`paper_trading.py:337-465`, fully read for this
audit) contains **no** reference to `threshold_gap` — no dead-zone, no hysteresis,
no side-gap requirement. The `cfg_threshold_gap=0.1` row in F1's DB config is a
copied-in default (`DEFAULT_CONFIG` line 103) that the F1 code path never reads.

Consequently the Rust gate, which has no threshold_gap logic, is already at parity
for F1. **Slice 7 (replicate threshold_gap in engine.rs) is NOT triggered and must
be skipped**; per the plan's conditional-renumber rule, Slice 8 → Wave 5 and
Slice 9 → Wave 6.

## 4. max_entry band (0.92) — MATCH

**Paper** (`paper_trading.py:423-430`):

```python
entry_price = shared.get("last_trade_expensive")
if not entry_price or entry_price <= 0.5:
    entry_price = shared.get("orderbook_entry")
if not entry_price or entry_price <= 0.5:
    return "NO PRICE", None, None, None
if entry_price > session_cfg["max_entry_price"]:
    return f"HIGH ({entry_price:.2f})", None, None, None
```

with `orderbook_entry = max(yes_ask or 0, no_ask or 0)` (`tasks.py:179`).

**Rust gate** (`kalshi_rs/src/engine.rs:187-199`):

```rust
let orderbook_entry = s.yes_ask.unwrap_or(0.0).max(s.no_ask.unwrap_or(0.0));
let mut entry = s.last_trade_expensive.unwrap_or(0.0);
if entry <= 0.5 { entry = orderbook_entry; }
if entry <= 0.5 { skip!("NO PRICE"); }
// 11. max_entry
if entry > cfg.max_entry_price {
    skip!("HIGH ({:.2}>{:.2})", entry, cfg.max_entry_price);
}
```

Identical entry sourcing (last-trade-expensive if > 0.5, else expensive-side ask),
identical lower bound (`<= 0.5` rejects), and identical upper bound: both reject
strictly-greater, so **entry == 0.92 is allowed on both sides** (inclusive cap).
`main.rs` re-checks the same band immediately before placing the live order
(`main.rs:1222-1226`):

```rust
if !(0.50 < entry && entry <= cfg.max_entry_price) {
    warn!("LIVE SKIP: entry {:.2} out of (0.50, {:.2}]", entry, cfg.max_entry_price);
```

— same `(0.50, 0.92]` interval, evaluated on the signal entry (`fire.entry`), so the
re-check can never disagree with the gate. The deploy env `MAX_ENTRY=0.92`
(`main.rs:418` `resolve_max_entry`) equals the F1 factory value, a no-op override.
(The *execution* re-read `exec_entry` is clamped to `(0.50, 0.98]` at
`main.rs:1238`, not to 0.92 — that is an execution-price concern, covered under
item 7.)

## 5. liq_filter off parity — MATCH

F1 paper config: `cfg_liq_filter='off'`, `cfg_vol_imb_kill='0.0'`.

**Paper** (`paper_trading.py:589-612`, `_session_liq`, called from the classic path
at each BUY branch):

```python
_is_ll = getattr(_cfg, "VENUE", "polymarket") == "limitless"
if (not _is_ll) and str(cfg.get("liq_filter", "on")) == "on":
    skip, reason = _check_liquidity_paper(side, shared)
    if skip:
        return skip, reason
vk = float(cfg.get("vol_imb_kill", 0) or 0)
if vk > 0:
    ...
return False, ""
```

For F1 the spread/depth check is skipped twice over: `liq_filter` is the string
`"off"` (≠ `"on"`), **and** this instance sets `VENUE = "limitless"`
(`config.py:26`) which force-disables the check venue-wide. `vol_imb_kill = 0.0`
disables the volume-imbalance kill. `_session_liq` therefore always returns
`(False, "")` for F1.

**Rust** (`kalshi_rs/src/engine.rs:247-268`, `session_liq`):

```rust
if cfg.liq_filter {
    if let Some(r) = check_liquidity(side, s) { return Some(r); }
}
let vk = cfg.vol_imb_kill;
if vk > 0.0 { ... }
None
```

With the F1 factory's `liq_filter: false` and `vol_imb_kill: 0.0` (plan Slice 1,
mirroring the DB rows) both branches are skipped and `session_liq` returns `None` —
no divergence, no skip path either side. The remaining regime gates are equally
inert on both sides: `cfg_kill_hours=''` / `kill_hours: vec![]` and
`cfg_ofi_align='off'` / `ofi_align: false` (`engine.rs:223-244` vs paper
`_regime_block`, `paper_trading.py:613-640`).

## 6. sigma source (paper max10 → Rust gate) — GAP-FIXED

**The gap (present in the repo today).** Two hardcoded keys break F1:

- `kalshi_rs/src/mirror.rs:74` reads the wrong session under F1:

  ```rust
  let sigma = ss.get("f6_wait270")?.get("live_sigma")?.as_f64()?;
  ```

- `kalshi_rs/src/main.rs:868` inserts under the wrong map key for F1:

  ```rust
  sigmas.insert("max30".to_string(), m.sigma_max30);
  ```

  while the gate looks up `s.sigmas.get(&cfg.sigma_type)` (`engine.rs:123,135`).
  With F1's `sigma_type = "max10"` the lookup misses and silently falls back to
  `.or(s.sigma)` = the **locally computed** `realized5_pmin`
  (`main.rs:918`) — the bot would trade a formula that is not F1
  (PRD 2.1 hazard 2, silent-wrong-trade).

**The fix (this feature's plan):**

- **Slice 1** — `SessionConfig::f1_d50cap75()` factory with `sigma_type: "max10"`
  (`kalshi_rs/src/config.rs`).
- **Slice 2** — `mirror::fetch` takes the session key as a parameter (F1 →
  `sessions_state["f1_d50cap75"]["live_sigma"]`) and `extract_live_sigma` is
  **fail-closed**: `None` for a missing key/field, non-numeric, or `<= 0.0` value →
  the tick is skipped (mirror.rs "never trade blind" contract, `mirror.rs:48`).
- **Slice 4** — `insert_mirror_sigma` inserts under `cfg.sigma_type.clone()`
  replacing the `"max30"` literal at `main.rs:868`; TC-4.2 (merge blocker) proves
  the gate never reaches the `.or(s.sigma)` fallback under F1.

**Paper-side verification of what `live_sigma` is.** The mirrored value is stored
per-session at `paper_trading.py:897` (`check_all_sessions`):

```python
st["live_sigma"] = shared.get("sigmas", {}).get(sigma_type) or shared.get("sigma") or 0
```

— the **same expression** the paper gate itself evaluates at
`paper_trading.py:402`, so the mirrored `live_sigma` equals the sigma the paper F1
gate uses at that instant, fallback chain included. Verified live on the endpoint
(2026-07-05): `sessions_state["f1_d50cap75"]["live_sigma"] = 0.000157`
(present, numeric, > 0), served by `http_server.py:1443-1452` from
`get_all_sessions()`.

**Paper max10 definition == Rust max{win}.** Paper
(`/root/paper_compare_kalshi_15m/dashboard/helpers.py:121-138`, inside
`_compute_all_sigmas`, win ∈ {5, 10, 30}):

```python
# max: max of rolling 1-min std within window
max_std = 0
for i in range(len(pts)):
    chunk_prices = []
    t0 = pts[i][0]
    for t, p in pts:
        if t0 - 60 <= t <= t0:
            chunk_prices.append(p)
    if len(chunk_prices) >= 3:
        cavg = sum(chunk_prices) / len(chunk_prices)
        if cavg > 0:
            s = _std(chunk_prices) / cavg
            if s > max_std:
                max_std = s
result[f"max{win}"] = max_std
```

with `_std` = population std, ÷N (`helpers.py:54-59`), over the trailing
`win`-minute price slice (`_get_prices_window`, `helpers.py:47-51`, cutoff
`now - win*60`). Rust (`kalshi_rs/src/signal.rs:121-142`) computes the identical
quantity (two-pointer over the same `t0-60 <= t <= t0` chunks, `std_pop / mean`,
chunk length >= 3, max over the window; `std_pop` at `signal.rs:21-33` is the same
÷N two-pass std). So for `max10`: **max over the trailing 10-minute window of the
trailing-60 s std/mean** — same definition both sides. (In MIRROR mode the Rust
local computation is not what the gate consumes for `sigma_type` — the mirrored
paper value overwrites that key — which is exactly why parity of the *source*
matters and is what Slices 2+4 guarantee.)

**Residual divergence, accepted as conservative:** when the paper's whole sigma
chain yields 0/None, the paper gate sets `p = None` and **None-p passes** the
p-threshold (it can still trade); the Rust mirror instead fails closed (Slice 2:
`live_sigma <= 0.0` → skip tick, no order). The live bot can only *miss* such
degenerate windows, never mis-trade them — consistent with PRD 2.4 NFR-5
(fail-closed on ambiguity).

## 7. Execution buffers (PRICE_BUF=0.06 / REQUOTE_BUF=0.12) — GAP-ACCEPTED

Buffers shape the *execution* price, not the signal: the gate/coverage decisions
stay on the mirrored signal entry (items 1-6), so buffer settings cannot create a
signal-parity gap. Changing prod buffer values is explicitly out of scope for this
feature (PRD 2.9); this section is analysis + recommendation only.

**Current prod values** (EU box drop-in
`/etc/systemd/system/kalshi-shadow-com.service.d/mirror.conf`, read 2026-07-05):
`PRICE_BUF=0.06`; `REQUOTE_BUF` unset → binary default `0.12` (`main.rs:443`).

**Mechanics** (`main.rs:1231-1296`): before ordering, the bot re-reads the *current*
real orderbook and prices the IOC off that fresh ask (`exec_entry`, clamped to
`(0.50, 0.98]`, `main.rs:1238`), with limit = `exec_entry ± PRICE_BUF`
(`main.rs:1243-1244`). Because the order is an IOC *crossing* the book, the buffer
is a fill-probability allowance, not a paid premium — the fill happens at the real
ask (≤ limit). On a 201 no-fill, one re-quote at `exec_entry ± REQUOTE_BUF`
(`main.rs:1274-1296`). Count = `stake / exec_entry`, capped at `MAX_COUNT` default
15 (`main.rs:441,1251-1257`).

**Live f6 telemetry** (`/home/dmitrii/kalshi_rs/shadow_ledger.jsonl`, 184 filled
live rows with `signal_entry`/`exec_entry`/`eff`, 2026-06-28 → 06-30, f6 @ 270 s):

| Component | mean | median | p90 | max |
|---|---|---|---|---|
| drift = `exec_entry − signal_entry` | +0.97c | 0 | +4c | +30c |
| walk = `eff − exec_entry` | −0.40c | 0 | +0.1c | +2c |
| total = `eff − signal_entry` | +0.57c | 0 | +4c | +30c |

29/184 fills (15.8%) paid ≥ 3c over the signal entry — and that drag sits almost
entirely in **drift** (the book moving between the mirrored signal and the fresh-ask
re-read), not in the buffer (walk is ≈ 0 or negative: IOC fills at the ask, under
the limit). Zero of the 184 fills used the re-quote path, so `REQUOTE_BUF=0.12` is
an untested-in-anger safety net, not an active cost.

**F1-specific analysis.** F1 enters at 180 s vs f6's 270 s — measurably stronger
momentum (PRD 2.10: the 180 s entry "rides stronger momentum", and the entry-price
right-tail is momentum-driven). Expected effect: a fatter **drift** tail (larger
signal→exec gaps), which the buffers do not cause and cannot remove — they only
decide whether the moved ask still fills (small PRICE_BUF → more no-fills; the
no-fill EV cost for F1 is unknown since F1 has never traded live). Lowering
PRICE_BUF for F1 would therefore trade a known, small crossing allowance for an
unknown no-fill tail on the strategy's first live run — the wrong side of the
uncertainty.

**Recommendation (keep, with watch items):**

1. **Keep `PRICE_BUF=0.06` and `REQUOTE_BUF=0.12` for F1 go-live.** They are
   fill-probability rails, not paid spread; the observed cost driver is drift, which
   is buffer-independent.
2. Re-evaluate after ~50 F1 live fills using the Section 1 telemetry
   (`signal_entry`/`exec_entry`/`eff` decomposition): if F1's drift p90 stays ≤ ~4c
   and walk stays ≈ 0, PRICE_BUF could be tightened as a separate config change;
   if the no-fill/requote rate is materially higher than f6's, do not tighten.
3. **Watch item:** the `exec_entry` clamp accepts up to **0.98** (`main.rs:1238`)
   while the signal band caps at 0.92 — under 180 s momentum the fresh-ask re-read
   can execute up to 6c above the signal cap (observed worst case at 270 s: +30c
   total on one fill). Pre-existing execution behavior, unchanged by this feature;
   monitor it in F1's first fills rather than change it here.

## Go-live gate

**No item blocks `LIVE_TRADING=1`.** Specifically:

- Item 3 (`threshold_gap`, the designated go-live blocker per architect constraint
  6 and PRD 2.10) resolves to **inert for F1's pmodel/taker path** — no Rust
  replication needed; Slice 7 must be skipped.
- Item 6 is a real defect in today's code but is fixed inside this feature's own
  plan: **Slices 1, 2 and 4 (with TC-4.2 as a merge blocker) must be merged and
  deployed before the flip** — that ordering is already enforced by the plan
  (Slice 9, Step 2 checklist).
- Items 1, 2, 4, 5 show formula-, boundary- and skip-path-identical behavior.
- Item 7 requires no change (execution-side; keep prod values).

Remaining pre-flip conditions from the plan (restated for the checklist, all
outside this audit's 7 items): restart latch (Slice 6) shipped;
`DAILY_LOSS_STOP=30` set (current drop-in still has `100`); shadow pre-flight with
`LIVE_TRADING=0` shows F1 params + `subaccount=1` at startup and mirror gap ≈ 0 vs
paper F1 on real triggers.

## Rollback

Rollback is a config/binary swap on the EU box (`34.32.177.126`, service
`kalshi-shadow-com`) — no data migration (PRD 2.4 NFR-4).

**Config rollback (strategy → f6):**

1. Edit the systemd drop-in:
   `/etc/systemd/system/kalshi-shadow-com.service.d/mirror.conf`
2. Remove (or comment out) the `Environment=SESSION=f1_d50cap75` and
   `Environment=MIRROR_SESSION=f1_d50cap75` lines. With both unset the resolver
   defaults to `f6_wait270` — today's behavior, byte-identical (PRD 2.4 NFR-1).
   To stop live orders entirely instead, set `Environment=LIVE_TRADING=0`.
3. Apply:

   ```
   sudo systemctl daemon-reload && sudo systemctl restart kalshi-shadow-com
   ```

**Binary rollback (if the new binary itself must be reverted):**

1. Timestamped backups live next to the release binary:
   `/home/dmitrii/kalshi_rs/target/release/kalshi_bot.bak.TIMESTAMP`
   (e.g. `kalshi_bot.bak.20260629-145700`; verified present 2026-07-05).
2. Restore the last-known-good backup over the active binary:

   ```
   sudo cp /home/dmitrii/kalshi_rs/target/release/kalshi_bot.bak.<TIMESTAMP> \
           /home/dmitrii/kalshi_rs/target/release/kalshi_bot
   sudo systemctl daemon-reload && sudo systemctl restart kalshi-shadow-com
   ```

3. If the drop-in was also changed for F1, perform the config rollback above in the
   same edit (drop-in backups exist alongside:
   `mirror.conf.bak.20260628-205410`, `mirror.conf.bak.20260628-211515`).

**Verification after rollback:** `journalctl -u kalshi-shadow-com -n 50` shows the
startup line `session 'F6 wait270': entry_wait=4.5min delta>=20 p>=0.6 sigma=max30
max_entry=0.92` and, if live was disabled, `LIVE_TRADING=false` in the order-client
line; the ledger tags subsequent rows `f6_wait270_shadow`.
