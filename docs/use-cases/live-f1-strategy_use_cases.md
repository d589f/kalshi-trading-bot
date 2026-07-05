# Use Cases: Live F1 Strategy Switch + Entry-Fidelity Audit

> Based on [PRD](../PRD.md) — section 2, internal id `live-f1-strategy`

This feature switches the Rust live trader (`kalshi_rs`) from strategy
`f6_wait270` to `f1_d50cap75` via an environment variable (no code fork) and
re-enters LIVE trading at a **$5 stake on Kalshi subaccount #1**, gated behind an
**entry-fidelity audit** proving the Rust gate reproduces the paper F1 signal 1:1.
It reuses, unchanged, the Section 1 (`live-exec-telemetry-latch-fix`) rails:
`LiveTriggerRecord` telemetry (P0) and latch-on-confirmed-fill + bounded retry
(P0b). This document is the single source of truth for the F1 E2E tests; where a
scenario is a pure reuse of a Section 1 rail, it references the corresponding
`live-exec-telemetry-latch-fix` UC rather than re-deriving it.

Two correctness hazards make this more than a config swap and are the spine of
this document:

- **H1 — hardcoded MIRROR session key.** `mirror.rs::fetch` reads
  `sessions_state["f6_wait270"]["live_sigma"]` (mirror.rs line 74). Under F1 it
  must read `sessions_state["f1_d50cap75"]["live_sigma"]`.
- **H2 — mirrored sigma under a hardcoded map key (silent-wrong-trade).**
  `main.rs` line 868 does `sigmas.insert("max30", m.sigma_max30)` while the gate
  looks up `s.sigmas.get(&cfg.sigma_type)` (engine.rs lines 123/135). With F1's
  `sigma_type = "max10"` the lookup **misses** and the gate silently falls back
  to `s.sigma` (= the **local** `realized5_pmin` from `compute_all_sigmas`,
  main.rs lines 918/846) — trading a formula that is **NOT F1**. This is the
  critical defect being fixed and unit-tested (FR-6/FR-7).

## Actors

This is an internal Rust binary — there is no HTTP API or interactive UI. The
"actors" are the operator, the code paths, and the external processes the bot
talks to:

- **Operator** — sets env vars on the EU box (`34.32.177.126`, systemd
  `kalshi-shadow-com`, via a drop-in env file), deploys the binary, watches the
  dashboard, and owns the go-live gate (the FR-8 audit). Sets `SESSION`,
  `MIRROR_SESSION`, `LIVE_TRADING`, `STAKE`, `SUBACCOUNT`, `MAX_ENTRY`,
  `DAILY_LOSS_STOP`.
- **main() / boot** — `kalshi_rs/src/main.rs::main` (≈ 408-627). Selects the
  `SessionConfig` factory (line 417, today hardcoded `f6_wait270()`), derives the
  telemetry `SESSION_NAME` (const at line 57, today `"f6_wait270_shadow"`),
  builds `LiveCfg`/`MirrorCfg`, resolves `SUBACCOUNT` → `OrderClient`
  (lines 466-484), and logs the selected session config (lines 419-428).
- **signal_loop** — the 0.3s decision loop (`main.rs` ≈ 790-1056). Owns the
  MIRROR fetch/skip block (856-903), builds `Shared` (912-933), calls
  `evaluate(&cfg, &shared)` (935), owns the `fired_window` latch (805) and the
  per-window retry state `attempt_window`/`attempt_count`/`last_attempt_ts`
  (808-810), and drives `place_live` via `retry_gate`/`latch_decision`
  (1004-1054). **Owns H2's fix point:** `sigmas.insert(...)` at line 868.
- **evaluate (the gate)** — `kalshi_rs/src/engine.rs::evaluate` (83-220). Runs the
  F1 filter chain: entry_wait (97-98), delta threshold (104), dual-confirmation
  (109-113), sigma resolution with the `.filter(|v| *v > 0.0).or(s.sigma)`
  fallback (121-139), p-model (143-157), side (159-182), entry sourcing/band
  (184-199), liquidity (203-206). **Consumes H2's map key** at 123/135.
- **mirror::fetch** — `kalshi_rs/src/mirror.rs::fetch` (49-100). Reads
  `/api/state` + `/api/sessions_state` (3s timeouts), extracts the session
  `live_sigma` (**H1's fix point**, line 74), guards freshness via `age_secs`
  (line 78/98). Returns `None` on any error → caller skips the tick.
- **SessionConfig factory** — `kalshi_rs/src/config.rs`. Today only
  `f6_wait270()` (40-58); this feature adds `f1_d50cap75()` (FR-2).
- **place_live** — the live IOC routine (Section 1). Reused unchanged; appends
  one `LiveTriggerRecord` per fire and returns an `outcome`.
- **Paper F1 engine** — the Python paper process at `http://127.0.0.1:8893` on the
  EU box, exposing `/api/state` and `/api/sessions_state`. Owns the
  `f1_d50cap75` session's `live_sigma` and the authoritative F1 signal the mirror
  reproduces.
- **Kalshi REST/OrderClient** — `create_ioc(...)` routed to `SUBACCOUNT=1`.
  Contract/pricing unchanged from Section 1.
- **Resolver** — the settlement task (`main.rs` ≈ 614-621) that scores filled
  positions into `ResolveRecord` PnL, unchanged.
- **Buffalo dashboard** — `23.95.217.78:8890` (`kalshi_rs/src/dashboard.rs`).
  Renders the green live-F1 series (`shadow_com`, dashboard.rs 106-108) against
  the pink paper-F1 series (`paper_f1`, 103-105). No change required.

## Shared preconditions (apply unless a use case overrides them)

- The bot runs in **MIRROR mode**: `MIRROR_STATE_URL` is set (e.g.
  `http://127.0.0.1:8893`), so signal inputs come from the paper engine, not the
  bot's own Binance feed (`main.rs` 856-903).
- The FR-6 fix is in place for all F1 use cases: `main.rs` line 868 inserts the
  mirrored sigma under `cfg.sigma_type` (not the literal `"max30"`), so the gate
  lookup hits. (UC-4 is specifically about this fix; UC-2 asserts f6 is
  non-regressive.)
- For live use cases: `LIVE_TRADING=1`, an `OrderClient` is present
  (`lcfg.enabled && order_client.is_some()`, `main.rs` 1016), and `SUBACCOUNT=1`
  routed the client to the isolated $147.73 subaccount.
- The Section 1 P0b rails are enabled and unchanged: `RETRY_MAX_ATTEMPTS=2`,
  `RETRY_COOLDOWN_SECS=3` (defaults, `main.rs` 444-445); latch on confirmed fill
  only.
- The FR-8 audit is a **go-live gate**: no F1 use case that places real orders is
  valid before the audit doc exists with a verdict for every listed item (UC-6).

## Invariants asserted across use cases (verify in every relevant postcondition)

- **INV-F1 (env is the only lever).** With `SESSION`/`MIRROR_SESSION` unset,
  every observable — selected factory, sigma map key, mirror session key,
  telemetry `session` tag, order flow — is **byte-for-byte** the current f6
  mirror (NFR-1). The switch changes behavior *only* when the env vars are set.
- **INV-F2 (selected session's gate is the only gate).** Exactly one
  `SessionConfig` is active per process; the gate evaluates against that config's
  params only. f6 and F1 never both gate in one process (UC-9).
- **INV-F3 (sigma keyed by sigma_type).** The mirrored `live_sigma` is retrievable
  from `Shared.sigmas` under `cfg.sigma_type` (`"max10"` for F1, `"max30"` for
  f6), and the gate resolves sigma from that key — never silently from the local
  `realized5_pmin` fallback when the mirrored value is present and positive
  (FR-6/FR-7, UC-4).
- **INV-F4 (fail-closed on ambiguity).** Any condition that would make the bot
  trade a non-F1 formula — unrecognized `SESSION`, absent `MIRROR_SESSION` key,
  stale/unreachable mirror, non-positive mirrored sigma — MUST skip the tick or
  fall back to the **logged** f6 default; it MUST NOT place a live order on an
  unverified signal (NFR-5).
- **INV-F5 (attribution).** A live F1 fill's `LiveTriggerRecord` `session` tag
  identifies `f1_d50cap75`, never `f6` (FR-3), so ledger/dashboard stats are not
  mislabeled.
- **INV-F6 (Section 1 rails unchanged).** No new safety logic is added: latch,
  bounded retry, daily cap, loss stop, `(0.50, max_entry]` band, subaccount
  isolation, and `LiveTriggerRecord` decomposition behave exactly as their
  Section 1 UCs specify (FR-10). This feature only selects *which* strategy those
  rails run.
- **INV-F7 (no new hot-path work).** The switch adds no blocking I/O to the 0.3s
  loop; the sigma keying is a `String`-key change, and MIRROR still reads
  `/api/state` + `/api/sessions_state` once per tick with the existing 3s
  timeouts (NFR-2).
- **INV-F8 (additive config).** `SessionConfig::f1_d50cap75()` is additive; the
  `f6_wait270()` factory and the `SessionConfig` struct shape are unchanged; all
  existing `kalshi_rs` tests keep passing (NFR-3).

---

## UC-1: Operator selects F1 at boot → correct config, log, sigma key, mirror key, attribution

**Actor**: Operator, main()/boot
**Preconditions**: EU box; drop-in env file sets `SESSION=f1_d50cap75` (and, by
default derivation, `MIRROR_SESSION=f1_d50cap75`); `SessionConfig::f1_d50cap75()`
exists (FR-2).
**Trigger**: operator restarts `kalshi-shadow-com`; the binary boots.

### Primary Flow (Happy Path)
1. `main()` reads `SESSION`; the value `f1_d50cap75` selects the
   `SessionConfig::f1_d50cap75()` factory at the selection point (`main.rs` line
   417, today hardcoded `f6_wait270()`), replacing the hardcoded call — no
   `#[cfg]` fork, no duplicate binary (FR-2).
2. The selected `cfg` carries `kappa = 0.4`, `delta_threshold = 50.0`,
   `p_model_threshold = 0.65`, `sigma_type = "max10"`, `tau_mode = Linear`,
   `trade_side = Both`, `max_entry_price = 0.92`, `entry_wait_min = 3.0` (180 s),
   `liq_filter = false`, `sigma_max = None`, remaining fields matching the f6
   factory's defaults (no `kill_hours`, no regime blocks).
3. `MAX_ENTRY` env (if set to `0.92`) is applied via `resolve_max_entry`
   (`main.rs` 418, 50-55); it is within `(0.5, 0.99]` so it stands at `0.92`.
4. The telemetry `SESSION_NAME` (const at `main.rs` line 57, today
   `"f6_wait270_shadow"`) is **derived from the selected session** so it names
   `f1_d50cap75` (FR-3, INV-F5) — not the f6 literal.
5. The startup config log (`main.rs` 419-428) prints:
   `session '...': entry_wait=3min delta>=50 p>=0.65 sigma=max10 max_entry=0.92
   stake=$5` (AC-1). `stake=$5` reflects the env `STAKE=5` used for live sizing
   (the factory's `stake` field is display-only; live sizing uses `LiveCfg.stake`).
6. `MirrorCfg` / the mirror session key resolves to `f1_d50cap75` (default derived
   from `SESSION`, overridable by `MIRROR_SESSION`), so `mirror::fetch` will read
   the F1 session (UC-3).
7. `SUBACCOUNT=1` routes the `OrderClient` to subaccount #1 (`main.rs` 466-484),
   logged `order client ready | ... subaccount=1` (FR-9, INV-F6).

**Postconditions**: exactly one `SessionConfig` is active (INV-F2) and it is the
F1 config; startup log shows the F1 params (AC-1); telemetry will tag `f1_d50cap75`
(INV-F5); the mirror will read the F1 session key (UC-3); orders will route to
subaccount #1. No order is placed at boot.

### Alternative Flows
- **UC-1-A1: `MIRROR_SESSION` explicitly overrides the derived default** — the
  operator sets `SESSION=f1_d50cap75` but `MIRROR_SESSION=f6_wait270` (e.g. to
  gate F1 params on the f6 sigma for a controlled experiment). The gate uses F1
  params but reads the f6 session's `live_sigma`. This is a valid explicit
  divergence; documented so a mismatched pair is not mistaken for a bug. (Not a
  recommended prod config; the recommended pair is both `f1_d50cap75`.)
- **UC-1-A2: `MAX_ENTRY` unset** — the band cap defaults to the factory's
  `max_entry_price = 0.92`; the log still shows `max_entry=0.92`.
- **UC-1-A3: `MAX_ENTRY` out of range** (e.g. `1.5` or `0.4`) — `resolve_max_entry`
  filters it (`> 0.5 && <= 0.99`) and falls back to the factory `0.92`. No abort;
  the effective cap is `0.92`.

### Error Flows
- **UC-1-E1: `SESSION=f1_d50cap75` but `SessionConfig::f1_d50cap75()` missing**
  (build-time only) — a compile error; cannot ship. Not a runtime path, but the
  FR-7 unit test and NFR-3 (all tests pass) guard against merging without it.

### Edge Cases
- **UC-1-EC1: `SESSION` set but `LIVE_TRADING` unset** — the F1 config is selected
  and gates in shadow (log-only) mode; the loop takes the `emit_trigger` path
  (`main.rs` 1049-1052), NOT `place_live`. Useful for pre-go-live shadow
  validation of the F1 gate without real orders.
- **UC-1-EC2: case / whitespace in `SESSION`** — the accepted values are exactly
  `f6_wait270` and `f1_d50cap75`; define whether matching is exact
  (case-sensitive, trimmed) — a value like `F1_D50CAP75` or ` f1_d50cap75 ` MUST
  either match after a documented normalization or be treated as unrecognized
  (UC-10). Expected: exact match after trim; anything else → UC-10. (Flagged for
  planner; default = trim then exact, case-sensitive.)

### Data Requirements
- **Input (env)**: `SESSION=f1_d50cap75`, `MIRROR_SESSION` (optional, default
  derived), `MAX_ENTRY=0.92`, `STAKE=5`, `SUBACCOUNT=1`, `LIVE_TRADING=1`,
  `DAILY_LOSS_STOP=30`, `MIRROR_STATE_URL`.
- **Output**: selected `SessionConfig` (F1); derived `SESSION_NAME`; resolved
  mirror session key; `OrderClient` bound to subaccount 1; startup log lines.
- **Side Effects**: none on-chain/on-ledger at boot; process state configured.

---

## UC-2: Default / f6 selection unchanged → byte-identical legacy behavior (regression guarantee)

**Actor**: main()/boot, signal_loop, mirror::fetch
**Preconditions**: `SESSION` unset OR `SESSION=f6_wait270`; `MIRROR_SESSION` unset.
**Trigger**: the binary boots without the F1 env vars.

### Primary Flow (Happy Path)
1. `main()` finds `SESSION` unset (or `f6_wait270`) and selects
   `SessionConfig::f6_wait270()` — the current default (FR-1).
2. `cfg.sigma_type == "max30"`; `entry_wait_min == 4.5` (270 s);
   `delta_threshold == 20.0`; `p_model_threshold == 0.60`; `kappa == 0.5`.
3. `SESSION_NAME` resolves to `"f6_wait270_shadow"` (unchanged, FR-3).
4. `mirror::fetch` reads `sessions_state["f6_wait270"]["live_sigma"]` (the
   default-derived key equals the current hardcoded key).
5. The mirrored sigma is inserted under `cfg.sigma_type == "max30"` (FR-6 keys it
   by `sigma_type`; for f6 that equals the old literal `"max30"`, so the value and
   key are identical to pre-feature).
6. The gate resolves sigma from `sigmas.get("max30")` — the mirrored value — and
   evaluates the f6 chain exactly as today.

**Postconditions** (INV-F1, AC-4): selected factory, sigma key (`max30`), mirror
session key (`f6_wait270`), telemetry `session` tag (`f6_wait270_shadow`), and
order flow are byte-for-byte identical to pre-feature f6 behavior. An
integration/behavioral check confirms this. INV-F8 holds (existing tests pass).

### Alternative Flows
- **UC-2-A1: `SESSION=f6_wait270` explicitly** — identical to unset; the explicit
  value is accepted and selects the same factory. No warning, no divergence.

### Error Flows
- **UC-2-E1: `MIRROR_SESSION=f6_wait270` set while `SESSION` unset** — explicit
  mirror key equals the default; behavior unchanged. (A benign explicit
  restatement of the default.)

### Edge Cases
- **UC-2-EC1: FR-6 keying must be non-regressive for f6** — the FR-7 unit test
  MUST assert that with `sigma_type = "max30"` the mirrored value is retrievable
  under `"max30"` and the gate uses it (not `realized5_pmin`), proving the
  key-by-`sigma_type` change did not break f6 (AC-2). The bug the change fixes is
  invisible for f6 (its literal already equalled `max30`), so the test is the only
  guard that the refactor stayed value-identical.

### Data Requirements
- **Input (env)**: `SESSION` unset (or `f6_wait270`); `MIRROR_SESSION` unset.
- **Output**: f6 config selected; `max30` sigma key; `f6_wait270` mirror key;
  `f6_wait270_shadow` tag.
- **Side Effects**: identical to pre-feature; no new behavior.

---

## UC-3: MIRROR reads the selected session's `live_sigma` (H1 fix)

**Actor**: mirror::fetch
**Preconditions**: MIRROR mode; the resolved mirror session key is `f1_d50cap75`
(from `MIRROR_SESSION`, default derived from `SESSION=f1_d50cap75`); the paper
engine is reachable and fresh.
**Trigger**: signal_loop calls `mirror::fetch(...)` on a tick (`main.rs` 859).

### Primary Flow (Happy Path)
1. `fetch` GETs `/api/state` and `/api/sessions_state` (mirror.rs 54-71, 3s
   timeouts each).
2. It extracts `sessions_state["f1_d50cap75"]["live_sigma"]` — the **selected
   session's** key, replacing the hardcoded `"f6_wait270"` at mirror.rs line 74
   (FR-4, H1). The endpoint is verified to expose `f1_d50cap75.live_sigma`
   (AC-3).
3. `.as_f64()` yields a positive sigma; `fetch` builds a `MirrorSnap` carrying
   that value in its sigma field (FR-5 — the field's legacy name `sigma_max30` is
   cosmetic; the meaning is "the selected session's `live_sigma`").
4. Freshness is computed from `st["last_update"]` → `age_secs` (mirror.rs 77-78);
   the snapshot is returned to signal_loop.

**Postconditions**: the F1 session's `live_sigma` (not f6's) is what the bot
gates on (AC-3). The `age_secs` freshness guard is unchanged (FR-5). No fallback
to a different session's sigma occurred.

### Alternative Flows
- **UC-3-A1: `MIRROR_SESSION` override** — as UC-1-A1: the key comes from
  `MIRROR_SESSION` verbatim; `fetch` reads whatever session that names, defaulting
  to the `SESSION`-derived value. The fetch logic is key-agnostic; correctness is
  the operator's choice of key.

### Error Flows
- **UC-3-E1: `/api/state` or `/api/sessions_state` HTTP error / timeout / non-JSON**
  — any `.ok()?` / `.json().await.ok()?` fails → `fetch` returns `None` → caller
  skips the tick (UC-13). Never trades blind (INV-F4).

### Edge Cases
- **UC-3-EC1: session present but `live_sigma` absent** — `ss.get("f1_d50cap75")`
  is `Some` but `.get("live_sigma")` is `None` → `?` → `fetch` returns `None` →
  skip tick. Covered by UC-11 (fail-closed on missing key).
- **UC-3-EC2: `live_sigma` present as JSON null / non-numeric string** —
  `.as_f64()` returns `None` → `?` → `fetch` returns `None` → skip. A "NaN-ish"
  value cannot pass this point (see UC-14).

### Data Requirements
- **Input**: resolved mirror session key; `/api/sessions_state` JSON.
- **Output**: `MirrorSnap` with the F1 `live_sigma`, or `None`.
- **Side Effects**: two outbound GETs per tick (unchanged count, INV-F7).

---

## UC-4: CRITICAL — mirrored sigma keyed by `sigma_type`; gate hits `max10` for F1 (unit-tested)

**Actor**: signal_loop (the `sigmas.insert` at `main.rs` 868) + evaluate (the
`s.sigmas.get(&cfg.sigma_type)` at engine.rs 123/135)
**Preconditions**: MIRROR mode; `cfg.sigma_type == "max10"` (F1); a fresh
`MirrorSnap` with a positive `live_sigma`.
**Trigger**: a tick where a valid `MirrorSnap` is obtained.

### Primary Flow (Happy Path)
1. signal_loop obtains the `MirrorSnap` (UC-3) and inserts its sigma into the
   `sigmas` map under `cfg.sigma_type` — `sigmas.insert(cfg.sigma_type.clone(),
   <mirrored sigma>)` — replacing the hardcoded `sigmas.insert("max30", ...)` at
   `main.rs` line 868 (FR-6, H2 fix).
2. signal_loop builds `Shared` and calls `evaluate(&cfg, &shared)`.
3. The gate resolves sigma via `s.sigmas.get(&cfg.sigma_type)` (engine.rs 123):
   for F1 this is `get("max10")`, which now **hits** the mirrored value; the
   `.filter(|v| *v > 0.0)` passes (positive) so the mirrored value is used, NOT
   the `.or(s.sigma)` local `realized5_pmin` fallback (engine.rs 126).
4. The redundant reassignment at engine.rs 135-139 re-confirms the same
   `max10` value; the p-model (signal.rs 180) runs on the mirrored max10 sigma.

**Postconditions** (INV-F3, AC-2): the gate consumed the mirrored max10 sigma;
the local `compute_all_sigmas` map (main.rs 846) was **not** relied upon for the
selected sigma_type. The bot is trading the F1 formula, not the fallback.

### Alternative Flows
- **UC-4-A1: f6 non-regression** — with `cfg.sigma_type == "max30"`, the insert
  keys `max30` and the gate's `get("max30")` hits — identical to pre-feature. The
  FR-7 unit test asserts BOTH the `max10` and `max30` cases (AC-2).

### Error Flows
- **UC-4-E1: FR-7 unit test fails (regression detector)** — if a future change
  reverts the insert to a hardcoded key, the FR-7 test MUST fail: given
  `sigma_type = "max10"` and a mirrored value, `Shared.sigmas["max10"]` MUST equal
  that value and the gate MUST resolve sigma from it (not `realized5_pmin`). This
  is the merge-blocking guard against re-introducing H2.

### Edge Cases
- **UC-4-EC1: pre-fix behavior (documents the defect being fixed)** — with the
  hardcoded `insert("max30", ...)` and `cfg.sigma_type = "max10"`, `get("max10")`
  MISSES → `.or(s.sigma)` returns the **local** `realized5_pmin` (main.rs 918) →
  the gate computes `p` on a non-F1 sigma and can fire a silently-wrong trade.
  This edge case exists only to pin the exact failure the fix removes; the E2E
  suite MUST show it does NOT occur post-fix.
- **UC-4-EC2: cosmetic display still hardcodes `max30`** — the periodic status log
  (`main.rs` 945/952) and the dashboard `live.sigma_max30` field (`main.rs` 992)
  read `sigmas.get("max30")`, which under F1 is absent → they display `0.0`. This
  is a **cosmetic** observability gap (the *displayed* σ label), NOT a trading
  defect — the gate's `disp_p` at main.rs 960-966 correctly uses `cfg.sigma_type`.
  Expected behavior MUST be decided: either leave the σ label reading `0.0` for F1
  (documented harmless) or key those displays off `cfg.sigma_type` too. (Flagged
  for planner; trading correctness is unaffected either way.)

### Data Requirements
- **Input**: `cfg.sigma_type`, the mirrored `live_sigma`.
- **Output**: `Shared.sigmas[cfg.sigma_type] = mirrored sigma`; a gate decision
  that uses it.
- **Side Effects**: none beyond the in-memory map insert.

---

## UC-5: End-to-end live F1 fire → fill → telemetry → resolve → dashboard

**Actor**: signal_loop → place_live → OrderClient → resolver → dashboard
**Preconditions**: UC-1 config active; UC-6 audit passed (go-live gate);
`LIVE_TRADING=1 STAKE=5 SUBACCOUNT=1 SESSION=f1_d50cap75
MIRROR_SESSION=f1_d50cap75 MAX_ENTRY=0.92 DAILY_LOSS_STOP=30`; a fresh F1
`MirrorSnap`; all safety gates pass; `create_ioc` fills.
**Trigger**: a tick where the F1 gate produces `res.fire`.

### Primary Flow (Happy Path)
1. signal_loop obtains a fresh F1 `MirrorSnap` (UC-3), inserts the sigma under
   `max10` (UC-4), builds `Shared`, and calls `evaluate`.
2. The F1 gate fires only when **all** hold: `elapsed_min >= 3.0` (≥180 s,
   engine.rs 97-98), `|delta_from_open| >= 50` (engine.rs 104), dual-confirmation
   `sign(delta_open) == sign(delta_prev)` (109-113), p-model
   `p = phi(0.4 * snr) >= 0.65` (143-157), `sigma` from the mirrored `max10`
   (UC-4), and entry in `(0.50, 0.92]` (187-199). Side = YES if `delta >= 0`
   else NO (`trade_side = Both`, 160-167). `liq_filter = false` → no
   `session_liq` skip (203-206). (AC-1)
3. signal_loop resets per-window retry state on window roll (`main.rs` 1008-1013),
   finds the window not latched (`already == false`, 1014), and passes the
   `retry_gate` (1017-1023).
4. signal_loop calls `place_live(...).await` (1025-1041) — the Section 1 routine,
   unchanged (INV-F6).
5. `place_live` passes the daily-cap, loss-stop, and band gates; fetches the fresh
   order-time book; computes `exec_entry`/`first_limit_price`/`count` (Section 1
   INV-3, unchanged); sends a $5 IOC on subaccount #1.
6. `create_ioc` returns HTTP 201 with `fill > 0`, `remaining_count == 0`.
7. `place_live` appends **one** `LiveTriggerRecord` with `outcome = filled`,
   `session` tagged `f1_d50cap75` (INV-F5), and the full decomposition:
   `entry` (= paper F1 `fire.entry`, the "signal_entry"), `exec_entry`,
   `orderbook_entry`, `first_limit_price`, `requote*`, `remaining_count`, `fill`,
   `eff`, `fee`, `latency_ms`, plus `p`, `side`, `count`, `delta_from_open`,
   window bounds, `market_ticker`, `ts`/`ts_iso` (Section 1 FR-2). It increments
   `trades_today`, pushes a `TrigSummary` + a `Pending`.
8. `place_live` returns `filled`; signal_loop latches (`fired_window = win_key`,
   1045-1046).
9. The resolver later settles the `Pending` into a `ResolveRecord` PnL (Section 1
   unchanged).
10. The dashboard's green live-F1 series (`shadow_com`, dashboard.rs 106-108) is
    pushed and rendered against the pink paper-F1 series (`paper_f1`, 103-105)
    (AC-7). No dashboard code change is required.

**Postconditions** (AC-6/AC-7): exactly one `filled` F1 row per window (INV-F6);
its **mirror gap** (`entry` − paper F1 entry) ≈ 0 by construction (the mirror
reproduces the paper signal 1:1), and `drift = exec_entry − entry` /
`walk = eff − exec_entry` decompose the remaining deviation (Section 1 AC-2); the
order routed to subaccount #1; the window is latched; live-F1 vs paper-F1 is
visible on the dashboard.

### Alternative Flows
- **UC-5-A1: NO-side F1 fire** — `delta_from_open < 0` → side NO; the per-side
  entry/price formulas apply (Section 1 UC-1-A1). Still a `filled` F1 row.
- **UC-5-A2: fill after a re-quote** — first IOC no-fill, deeper re-quote fills;
  `requote == true`, both prices recorded (Section 1 UC-4). The 180 s F1 entry
  rides stronger momentum than f6's 270 s, so re-quote frequency is expected to be
  **higher** for F1; the telemetry is the instrument that measures it (PRD 2.10).
- **UC-5-A3: partial fill** — `fill > 0` and `remaining_count > 0` → `outcome =
  partial`, latches (Section 1 UC-3). `Pending` sized to the filled count.

### Error Flows
- **UC-5-E1: any `place_live` append/serialize failure** — Section 1 UC-13; the
  loop never crashes; the latch decision still follows the returned outcome.

### Edge Cases
- **UC-5-EC1: F1's higher-momentum entry widens `walk`** — the entry-price
  right-tail (Section 1: 13% of f6 trades paid ≥6c) may be worse at 180 s. The
  `eff`/`exec_entry`/`entry` fields MUST make `gap = drift + walk` computable per
  fill so the F1 slippage profile (unknown before go-live, PRD 2.10) is measurable
  from the first fills. No behavior change — an observability requirement.

### Data Requirements
- **Input**: F1 `MirrorSnap`, F1 `cfg`, fresh order-time book, `OrderResp`,
  `LiveCfg` (stake $5, subaccount 1, loss-stop $30).
- **Output**: one `filled`/`partial` `LiveTriggerRecord` (session `f1_d50cap75`);
  a `Pending`; a `TrigSummary`; a later `ResolveRecord`; the dashboard series.
- **Side Effects**: one real $5 IOC on subaccount #1; `trades_today += 1`; ledger
  append; latch consumed.

---

## UC-6: Entry-fidelity audit is the go-live gate (per-item MATCH / GAP-FIXED / GAP-ACCEPTED)

**Actor**: Operator (author) + architect
**Preconditions**: the FR-6 sigma-key fix and the FR-2/FR-4 selection/mirror
changes are implemented; `LIVE_TRADING=1` for F1 is **NOT yet** enabled.
**Trigger**: readiness review before enabling real F1 orders.

### Primary Flow (Happy Path)
1. An **entry-fidelity audit document** is produced comparing the Rust gate
   against the paper F1 engine (FR-8).
2. It carries a per-item **verdict** (MATCH / GAP-FIXED / GAP-ACCEPTED) for each
   of the seven items:
   - **p-model formula** — `p = phi(kappa*snr)`, `snr = |delta|/(sigma_usd*f_tau)`,
     tau linear (signal.rs 180-199) vs the paper `compute_p_model`.
   - **entry_wait timing** — paper enters at exactly 180 s; the Rust
     `s.elapsed_min < cfg.entry_wait_min` gate (engine.rs 97-98) fires at the same
     instant with `entry_wait_min = 3.0`.
   - **threshold_gap semantics** — paper F1 sets `threshold_gap = 0.1`; the Rust
     gate has no such logic (confirmed absent). See UC-21 (the dedicated sub-audit).
   - **max_entry band** — reject-below-0.5 / cap-at-`max_entry_price` (engine.rs
     187-199) matches the paper.
   - **liq_filter off** — `liq_filter = false` yields no `session_liq` divergence
     (engine.rs 203-206).
   - **sigma source** — F1's `max10` mirrored sigma is used (depends on FR-6/UC-4)
     and the local `compute_all_sigmas` map is not relied on in MIRROR mode.
   - **execution buffers** — how `PRICE_BUF` (prod `0.06`) / `REQUOTE_BUF`
     (`0.12`) move `eff` vs the paper entry (gap = drift + walk), with a
     recommendation whether to keep the prod buffers for F1's 180 s entry.
3. Every gap is either **GAP-FIXED** (verdict points at the code change) or
   **GAP-ACCEPTED** (written rationale). No item is left without a verdict.

**Postconditions** (AC-5): the audit doc exists and is complete (a verdict for
every item). Its existence + completeness is a hard go-live gate — real F1 orders
MUST NOT be enabled until it is done.

### Alternative Flows
- **UC-6-A1: a buffer recommendation is made but not applied** — the audit MAY
  recommend lowering `PRICE_BUF`/`REQUOTE_BUF` for F1's momentum; **applying** it
  is out of scope for this feature (PRD 2.9) and is a separate config change. The
  recommendation is recorded; the prod buffers stay put unless a separate change
  lands.

### Error Flows
- **UC-6-E1: an item's verdict is a real GAP with no fix and no accepted
  rationale** — go-live is **blocked**. The bot MUST NOT be enabled for real F1
  orders until the gap is fixed or explicitly accepted in writing (INV-F4, NFR-5).

### Edge Cases
- **UC-6-EC1: sigma-source verdict depends on UC-4/UC-14** — the sigma-source item
  cannot be MATCH unless (a) FR-6 keys the sigma by `sigma_type` (UC-4) AND (b) the
  non-positive-sigma fallback behavior (UC-14) is either fixed or GAP-ACCEPTED. A
  MATCH here without addressing UC-14 is an incomplete verdict.

### Data Requirements
- **Input**: the paper F1 config + `compute_p_model`; the Rust gate source
  (engine.rs, signal.rs, mirror.rs, main.rs 868); Section 1 telemetry fields.
- **Output**: the audit/runbook document with seven verdicts + a rollback
  procedure (NFR-4, UC-22).
- **Side Effects**: none (documentation); gates the deploy.

---

## UC-7: Paper F1 trades but the bot skips (band / daily-cap / loss-stop) → context row, no order

**Actor**: place_live (Section 1 skip paths) in F1 context
**Preconditions**: UC-1 F1 config active, `LIVE_TRADING=1`; the paper F1 engine
fired for this window, but a bot-side pre-order gate rejects it.
**Trigger**: signal_loop reaches the trigger block with an F1 `res.fire`, but a
gate in `place_live` short-circuits.

### Primary Flow (Happy Path)
1. The F1 gate fires (same signal as paper); signal_loop calls `place_live`.
2. `place_live` hits a pre-order gate and writes exactly **one**
   `LiveTriggerRecord` with the matching skip outcome, tagged `f1_d50cap75`
   (INV-F5), then returns without placing an order (Section 1 UC-5/UC-6):
   - **UC-7-A1: `skip_band`** — the F1 entry is outside `(0.50, 0.92]` (paper F1
     can fire at entries the bot's tighter/`MAX_ENTRY` band rejects, or below 0.5
     on a thin book). One `skip_band` row; no order; pre-order gate → no retry
     attempt consumed.
   - **UC-7-A2: `skip_daily_cap`** — `trades_today >= MAX_TRADES_DAY`. One
     `skip_daily_cap` row; no order; no attempt consumed.
   - **UC-7-A3: `skip_loss_stop`** — `day_pnl <= -30` (`DAILY_LOSS_STOP=30`). One
     `skip_loss_stop` row; no order; no attempt consumed (see UC-18).

**Postconditions**: a context row is written for each skipped-but-paper-fired
window so the live-vs-paper divergence is auditable; no order; `trades_today`
unchanged; window not latched (skips never latch — Section 1 INV-2). The
dashboard live-F1 series therefore legitimately has **fewer** points than the
paper-F1 series for those windows.

### Alternative Flows
- **UC-7-A4: band edge — paper fires at entry == 0.92** — the bot's band is
  inclusive at the cap (`entry <= max_entry_price`), so entry `== 0.92` PROCEEDS
  (not a skip); see UC-17-EC. Only `entry > 0.92` yields `skip_band`.

### Error Flows
- **UC-7-E1: ledger append fails on a skip row** — Section 1 UC-13; no crash.

### Edge Cases
- **UC-7-EC1: repeated skip rows per window** — a persistent gate + persisting fire
  writes a skip row every 0.3s tick (skips don't latch); QA asserts
  at-least-one, not exactly-one, across a multi-tick window (Section 1 UC-5-EC1).

### Data Requirements
- **Input**: F1 `fire` context; the failing gate's inputs (`entry`,
  `trades_today`, `day_pnl`).
- **Output**: one `LiveTriggerRecord` of the skip outcome (session `f1_d50cap75`).
- **Side Effects**: ledger append only; no order, no state change.

---

## UC-8: F1 no-fill → window NOT latched → bounded retry (max 2, 3 s cooldown) → first fill latches

**Actor**: signal_loop (retry state) + place_live
**Preconditions**: UC-1 F1 config, `LIVE_TRADING=1`; the same `win_key` persists;
`RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3` (Section 1 P0b, reused unchanged).
**Trigger**: a `nofill` outcome on attempt 1, with the F1 fire persisting.

### Primary Flow (Happy Path)
1. Attempt 1 returns `nofill` (Section 1 UC-2): one `nofill` row (session
   `f1_d50cap75`); `attempt_count == 1`; `last_attempt_ts = now`; NOT latched.
2. Ticks within the 3 s cooldown: `retry_gate` (main.rs 1017-1023) blocks — no
   order placed on every 0.3s tick (Section 1 UC-9).
3. First tick at `now - last_attempt_ts >= 3` and `attempt_count < 2`: attempt 2
   is placed.
4. On a fill: `filled`/`partial` row, `trades_today += 1`, `fired_window` latched
   — at most one filled F1 position per window (Section 1 INV-2). No further
   attempts this window.
5. If attempt 2 also no-fills: `attempt_count == 2 == RETRY_MAX_ATTEMPTS` →
   exhausted; no further attempts until window roll resets state (Section 1 UC-8).

**Postconditions** (INV-F6): the Section 1 P0b behavior is byte-identical; the
only F1-specific note is the **rows are tagged `f1_d50cap75`** and the no-fill
frequency is expected higher at 180 s (PRD 2.10) — the retry recovers windows the
first attempt would have burned.

### Alternative Flows
- **UC-8-A1: `RETRY_MAX_ATTEMPTS=1` (retry disabled)** — single attempt per window,
  byte-identical to pre-P0b (Section 1 NFR-4). Still an F1 `nofill` telemetry row.

### Error Flows
- **UC-8-E1: `order_error`/`rejected` mid-retry** — counts as one attempt toward
  the budget (Section 1 FR-9, UC-15); does not latch; retry continues if budget
  and cooldown allow.

### Edge Cases
- **UC-8-EC1: window rolls mid-retry** — Section 1 UC-11; the in-flight old-window
  attempt latches only its own window; the new window starts with a fresh budget.

### Data Requirements
- **Input**: per-window `attempt_count`/`last_attempt_ts`, `RETRY_*`,
  `place_live` outcome.
- **Output**: one row per attempt; a `filled`/`partial` row on the fill.
- **Side Effects**: up to 2 `create_ioc` calls/window; latch on first fill;
  `trades_today += 1` once.

---

## UC-9: F1 and f6 disagree on a window → only the selected session's gate matters

**Actor**: evaluate (the single active `cfg`)
**Preconditions**: MIRROR mode with `SESSION=f1_d50cap75`; a window where the F1
gate would fire but the f6 gate would not (or vice versa), because their
thresholds differ (F1: Δ≥50, p≥0.65, 180 s, max10; f6: Δ≥20, p≥0.60, 270 s,
max30).
**Trigger**: a tick where the two strategies' gate decisions diverge.

### Primary Flow (Happy Path)
1. Exactly one `SessionConfig` is active (INV-F2) — the F1 config (UC-1).
2. `evaluate(&cfg, &shared)` uses ONLY F1 params; the f6 thresholds are not
   present in the process and cannot affect the decision.
3. If F1's gate fires (e.g. Δ = 60 at 190 s, p = 0.70), the bot trades — regardless
   of whether f6 (270 s wait, or 20 Δ / 0.60 p) would have. Conversely, if F1 does
   not fire (e.g. Δ = 30, below F1's 50 but above f6's 20), the bot does NOT trade,
   even though f6 would have.

**Postconditions**: the live signal equals the **paper F1** signal 1:1 (the
mirror + F1 gate), never a blend of F1 and f6. The dashboard's paper-F1 (pink)
line is the correct comparison; the paper-f6 line is not the bot's reference under
F1.

### Alternative Flows
- **UC-9-A1: earlier-window entry (180 s vs 270 s)** — F1 evaluates the entry gate
  at `elapsed >= 3.0` while f6 waits to `4.5`. In the 180–270 s band F1 may fire on
  a window f6 has not yet reached; this is the intended earlier-entry behavior, not
  a bug. The entry rides stronger momentum (PRD 2.10).

### Error Flows
- **UC-9-E1: operator expects f6 behavior but set F1** — no code error; the startup
  log (UC-1 step 5) is the authoritative statement of which gate is live. QA/ops
  verify the log's params match intent before trusting the run.

### Edge Cases
- **UC-9-EC1: same window, opposite side is impossible from one gate** — a single
  `cfg` produces at most one `fire` per tick with one side; there is no scenario
  where the bot holds both F1-YES and f6-NO for the same window (only one strategy
  is live). Cross-strategy comparison is a dashboard concern, not a runtime one.

### Data Requirements
- **Input**: the single active `cfg`; `shared`.
- **Output**: one `EvalResult` from the F1 gate only.
- **Side Effects**: none beyond the normal fire/skip path.

---

## UC-10: Unrecognized `SESSION` value → fail loud at startup (no silent f6 mis-trade)

**Actor**: main()/boot
**Preconditions**: `SESSION` set to a non-empty value that is neither
`f6_wait270` nor `f1_d50cap75` (e.g. `f1`, `f7_foo`, `f1_d50cap75x`, a typo).
**Trigger**: the binary boots and reads `SESSION`.

### Primary Flow (Happy Path)
1. `main()` reads `SESSION`, finds it unrecognized and non-empty.
2. It **fails loud**: logs an error naming the bad value and the accepted set
   (`f6_wait270`, `f1_d50cap75`), and either (a) aborts startup, or (b) falls back
   to `f6_wait270` while **logging which session it selected** (FR-1). The chosen
   behavior MUST be explicit — a silent fallback that does not log is forbidden
   (INV-F4, NFR-5).
3. If the fallback path is chosen, the process runs f6 and the startup config log
   (UC-1 step 5) shows f6 params — so the operator can see the mismatch and
   correct it.

**Postconditions** (INV-F4): the bot never runs an *unverified* strategy silently.
Either it aborts (operator fixes the env and restarts) or it runs the **logged**
f6 default. It never trades a non-F1 formula while claiming to be F1.

### Alternative Flows
- **UC-10-A1: abort-on-unknown chosen** — the process exits non-zero; systemd
  restart-loops until the env is fixed. Safest option; no orders placed. (Planner
  decides abort vs logged-fallback; default recommendation = logged-fallback to
  f6 so the shadow stays up, matching the current always-f6 baseline.)

### Error Flows
- **UC-10-E1: `SESSION=""` (empty string)** — treated as unset → default f6 (UC-2),
  NOT as an unrecognized value. An empty var is "not provided". Documented so the
  empty case is not conflated with the fail-loud case.

### Edge Cases
- **UC-10-EC1: near-miss typo** (`f1_d50cap75 ` trailing space, `F1_D50CAP75`
  casing) — resolved per UC-1-EC2's normalization rule (trim + case-sensitive
  exact). A value that does not match after normalization is unrecognized → this
  UC. The audit/runbook MUST call out the exact accepted spellings.

### Data Requirements
- **Input**: `SESSION` (unrecognized non-empty).
- **Output**: an error log; either process abort or a logged f6 fallback + its
  startup config log.
- **Side Effects**: no orders; no ledger writes at boot.

---

## UC-11: `MIRROR_SESSION` key absent from `/api/sessions_state` → fail closed (skip tick)

**Actor**: mirror::fetch + signal_loop
**Preconditions**: MIRROR mode; the resolved mirror session key (e.g.
`f1_d50cap75`) is NOT present in the `/api/sessions_state` response (paper engine
running an older config, a renamed session, or the key not yet materialized).
**Trigger**: `mirror::fetch` tries to read the session's `live_sigma`.

### Primary Flow (Happy Path)
1. `fetch` GETs `/api/sessions_state` successfully (valid JSON).
2. `ss.get("f1_d50cap75")` returns `None` (key absent) → the `?` operator
   short-circuits → `fetch` returns `None` (mirror.rs 74). It MUST NOT fall back to
   a different session's sigma (FR-4).
3. signal_loop's MIRROR match takes the `other` arm (`main.rs` 887-898): it logs
   (rate-limited to every 10 s) `MIRROR UNREACHABLE ... skipping tick` (or a
   dedicated "session key missing" message), sets the dashboard reason to
   `MIRROR WAIT`, and `continue`s — no evaluate, no order (AC-3).

**Postconditions** (INV-F4, AC-3): no order is placed on a missing-key tick,
verified by log line; the bot never substitutes another session's sigma. This
persists every tick until the key appears; then normal F1 evaluation resumes.

### Alternative Flows
- **UC-11-A1: key appears mid-run** — once the paper engine exposes
  `f1_d50cap75.live_sigma`, the next `fetch` succeeds and normal F1 evaluation
  resumes with no restart needed.

### Error Flows
- **UC-11-E1: `sessions_state` itself missing / malformed** — Section behaves as
  UC-3-E1 / UC-13: `fetch` returns `None`, tick skipped. Same fail-closed outcome,
  different cause (transport vs missing key).

### Edge Cases
- **UC-11-EC1: wrong-but-present key via `MIRROR_SESSION` typo** — if
  `MIRROR_SESSION` names a session that does not exist, this UC fires every tick
  (permanent skip) and the bot never trades. The rate-limited log is the only
  symptom; ops MUST verify the `MIRROR_SESSION` spelling against the endpoint. A
  present-but-wrong session (e.g. `MIRROR_SESSION=f6_wait270` under F1 params) is
  UC-1-A1, not this UC — it trades on a valid-but-mismatched sigma.

### Data Requirements
- **Input**: `/api/sessions_state` lacking the key; resolved `MIRROR_SESSION`.
- **Output**: `fetch → None`; a skipped tick; a rate-limited warn log.
- **Side Effects**: none; no order, no ledger write.

---

## UC-12: Mirror state stale (`age_secs > MIRROR_MAX_AGE_SECS`) → skip tick

**Actor**: signal_loop (freshness guard) + mirror::fetch
**Preconditions**: MIRROR mode; `fetch` returns `Some(MirrorSnap)` but
`m.age_secs > mcfg.max_age_secs` (`MIRROR_MAX_AGE_SECS`, default 5 s, `main.rs`
450); the paper engine is up but hasn't refreshed `last_update` recently.
**Trigger**: a tick where the snapshot is fresh-fetched but old.

### Primary Flow (Happy Path)
1. `fetch` returns `Some(m)`; signal_loop's guard `m.age_secs <= mcfg.max_age_secs`
   (`main.rs` 860) is FALSE.
2. The match takes the `other` arm (887-898): logs (rate-limited) `MIRROR STALE
   ... skipping tick`, sets dashboard reason `MIRROR WAIT`, `continue`s — no
   evaluate, no order (INV-F4).

**Postconditions**: a stale paper signal never drives a live F1 order; the bot
waits for a fresh snapshot. `age_secs` uses the same freshness meaning as
pre-feature (FR-5) — the F1 switch does not weaken the guard.

### Alternative Flows
- **UC-12-A1: freshness recovers** — once the paper engine advances `last_update`,
  the next snapshot's `age_secs` drops under the threshold and F1 evaluation
  resumes.

### Error Flows
- **UC-12-E1: clock skew inflates `age_secs`** — if the bot's clock is ahead of the
  paper engine's `last_update`, `age_secs` can be large even for a fresh signal →
  over-skipping. Safe direction (skips rather than trades stale). Documented; ops
  should keep NTP synced. Never causes a wrong trade.

### Edge Cases
- **UC-12-EC1: `age_secs` exactly `== MIRROR_MAX_AGE_SECS`** — the guard is
  `<=` (inclusive), so exactly-at-threshold PASSES (not stale) and evaluation
  proceeds. Boundary unchanged from pre-feature.

### Data Requirements
- **Input**: `MirrorSnap.age_secs`, `MIRROR_MAX_AGE_SECS`.
- **Output**: skipped tick on stale; a rate-limited warn log.
- **Side Effects**: none; no order.

---

## UC-13: Paper engine down / `:8893` unreachable → no trades, no crash

**Actor**: mirror::fetch + signal_loop
**Preconditions**: MIRROR mode; the paper engine process is down, or
`127.0.0.1:8893` refuses/times out.
**Trigger**: `mirror::fetch` attempts its GETs.

### Primary Flow (Happy Path)
1. `client.get(...).send().await` fails (connection refused / timeout after 3 s)
   or `.json()` fails → `.ok()?` returns `None` → `fetch` returns `None`
   (mirror.rs 54-71).
2. signal_loop's `other` arm (`main.rs` 887-898, `other.is_none()`) logs
   (rate-limited) `MIRROR UNREACHABLE ... skipping tick`, sets `MIRROR WAIT`, and
   `continue`s.
3. The loop keeps ticking at 0.3 s; the process does NOT panic or exit; it retries
   the fetch every tick.

**Postconditions** (INV-F4): the bot places no orders while the paper engine is
down; it self-heals when the engine returns (UC-11-A1 / UC-12-A1). No crash, no
zombie state.

### Alternative Flows
- **UC-13-A1: intermittent flapping** — the engine drops and returns repeatedly;
  each tick independently skips or evaluates. No latch/attempt-state corruption
  because window roll is time-based, not fetch-based.

### Error Flows
- **UC-13-E1: partial response** (`/api/state` ok, `/api/sessions_state` fails, or
  vice versa) — either failing `.ok()?` returns `None`; whole tick skipped. No
  half-built `MirrorSnap` reaches the gate.

### Edge Cases
- **UC-13-EC1: slow-but-not-dead engine** — a response arriving just under the 3 s
  timeout succeeds but may be stale (old `last_update`) → then handled by UC-12
  (freshness), not this UC.

### Data Requirements
- **Input**: unreachable `MIRROR_STATE_URL`.
- **Output**: `fetch → None`; skipped ticks; rate-limited warn logs.
- **Side Effects**: none; no order, no crash.

---

## UC-14: Non-positive / NaN-ish mirrored sigma → gate must not fire on the wrong (fallback) sigma

**Actor**: mirror::fetch + evaluate (the `.filter(|v| *v > 0.0).or(s.sigma)` path)
**Preconditions**: MIRROR mode, F1 (`sigma_type = "max10"`); the paper session's
`live_sigma` is 0, negative, null, or a non-numeric string.
**Trigger**: a tick where the mirrored sigma is not a positive number.

### Primary Flow (Happy Path — the two sub-cases)
1. **UC-14-A1: `live_sigma` is null / non-numeric ("NaN"-ish)** — `.as_f64()`
   returns `None` → `?` → `fetch` returns `None` → tick skipped (UC-11/UC-13
   path). JSON cannot carry a real NaN, so a "NaN-ish" value fails here and never
   reaches the gate. **Correct fail-closed** (INV-F4).
2. **UC-14-A2: `live_sigma` is a real 0 or negative number** — it parses via
   `.as_f64()`, so `fetch` returns `Some(MirrorSnap)` with a non-positive sigma;
   signal_loop inserts it under `max10` (UC-4). The gate's
   `s.sigmas.get("max10").copied().filter(|v| *v > 0.0)` (engine.rs 125) DROPS the
   non-positive value → `.or(s.sigma)` falls back to the **local** `realized5_pmin`
   (engine.rs 126, main.rs 918). The redundant reassign (135-139) also skips it
   (`if v > 0.0`).

**Postconditions (REQUIRED, INV-F4)**: on a non-positive mirrored sigma the bot
MUST NOT place a live F1 order computed on the **local fallback** sigma — that is a
non-F1 formula. The expected behavior is **fail-closed / skip**, exactly as the
null case (UC-14-A1).

### Error Flows
- **UC-14-E1 (FLAGGED GAP — the current code does NOT fail closed for A2)** — with
  today's `.or(s.sigma)` fallback, a real 0/negative mirrored sigma silently
  substitutes the local `realized5_pmin` and the p-model can fire a **non-F1**
  trade. This violates INV-F4/NFR-5. The FR-8 audit's **sigma-source** item MUST
  resolve this before go-live via one of:
  1. **GAP-FIXED** — when the mirrored value for the selected `sigma_type` is
     present-but-non-positive (or, more strictly, whenever in MIRROR mode), the
     gate/loop skips the tick instead of falling back to the local sigma (e.g.
     `fetch` rejects `live_sigma <= 0.0`, or the insert/gate treats a non-positive
     mirrored value as "skip, don't fall back").
  2. **GAP-ACCEPTED** — written rationale that the paper `live_sigma` is
     empirically always > 0 and a monitoring/alert catches any zero, so the
     residual risk is accepted. (Weaker; must be explicit.)
  Until fixed or accepted, this is a go-live blocker (UC-6-E1, UC-6-EC1).

### Edge Cases
- **UC-14-EC1: sigma so small `p` never reaches 0.65** — a tiny-but-positive sigma
  inflates `snr` and thus `p`; a tiny sigma makes the gate MORE likely to fire, not
  less. This is not the failure mode of this UC (that is the non-positive fallback);
  documented to distinguish "small sigma" (legitimate, fires) from "non-positive
  sigma" (must not fall back).
- **UC-14-EC2: exactly `0.0`** — `*v > 0.0` is FALSE for `0.0`, so it is dropped
  and falls back (the A2 hazard). The boundary is strict-greater, so `0.0` behaves
  as negative here.

### Data Requirements
- **Input**: `live_sigma` ∈ {null, "NaN", 0, negative}; `s.sigma` (local
  `realized5_pmin`).
- **Output**: A1 → skipped tick; A2 → per the audit verdict, either a skipped tick
  (fixed) or a documented-accepted fallback.
- **Side Effects**: none if fail-closed; a wrong order if the GAP is neither fixed
  nor accepted (the outcome this UC exists to prevent).

---

## UC-15: Order API error / reject under F1 → outcome row, retry budget consumed, no double-latch

**Actor**: place_live + signal_loop (Section 1 rails, F1 context)
**Preconditions**: UC-1 F1 config, `LIVE_TRADING=1`; an order is attempted but the
first `create_ioc` returns `Err`, or the response is non-201.
**Trigger**: signal_loop calls `place_live` and the order fails at the API.

### Primary Flow (Happy Path)
1. **UC-15-A1: `order_error`** — `create_ioc` returns `Err` on the first attempt →
   one `order_error` row (session `f1_d50cap75`); an order WAS attempted → it
   consumes one bounded-retry attempt (`counts_as_attempt`, main.rs 1042-1044);
   does NOT latch (Section 1 UC-6-A3).
2. **UC-15-A2: `rejected`** — the IOC returns HTTP `!= 201` → one `rejected` row;
   consumes one attempt; does NOT latch (Section 1 UC-6-A4).
3. In both cases, `latch_decision(outcome)` is FALSE (only fills latch), so
   `fired_window` is NOT set — no double-latch, and the window remains eligible for
   the remaining retry budget (UC-8).

**Postconditions** (INV-F6): exactly one row per failed attempt; `trades_today`
unchanged (no fill); the window is not latched by an error; the per-window attempt
budget is decremented so errors cannot spin forever within a window.

### Alternative Flows
- **UC-15-A3: error then fill on retry** — attempt 1 `order_error`/`rejected`
  (budget = 1 used), attempt 2 (after cooldown) fills → latch consumed, one
  `filled` row. At most one filled position per window (Section 1 INV-2).

### Error Flows
- **UC-15-E1: both attempts error/reject** — budget exhausted at
  `RETRY_MAX_ATTEMPTS`; no fill; no latch; window not traded until roll (Section 1
  UC-8). Safe terminal state.

### Edge Cases
- **UC-15-EC1: reject after a re-quote** — both IOCs non-201; counts as **one**
  per-window attempt (one `place_live` invocation = one attempt unit), per Section
  1 UC-6-EC3.

### Data Requirements
- **Input**: `create_ioc` `Err` / non-201 status; retry state.
- **Output**: one `order_error`/`rejected` `LiveTriggerRecord` (session
  `f1_d50cap75`).
- **Side Effects**: attempt budget decremented; no latch; no `trades_today`
  change.

---

## UC-16: Boot / first-eval mid-window at `elapsed > 180 s` but before window end → may fire immediately

**Actor**: main()/boot, signal_loop, evaluate
**Preconditions**: UC-1 F1 config; the bot starts (or the first fresh
`MirrorSnap` arrives) when the current 15-min window is already past 180 s but not
yet ended; all other F1 gates would pass.
**Trigger**: the first tick where a fresh F1 snapshot yields `elapsed_min > 3.0`.

### Primary Flow (Happy Path)
1. The window is unlatched (fresh process, `fired_window = None`, main.rs 805).
2. `evaluate` finds `elapsed_min >= cfg.entry_wait_min (3.0)` → the entry_wait gate
   passes (engine.rs 97-98); if Δ/p/sigma/band all pass, the F1 gate fires
   **immediately** on this first eligible tick.
3. `place_live` runs and may fill — entering the window **late** (e.g. at 200 s or
   later), which is a worse entry than the paper's 180 s entry (more momentum
   already realized).

**Postconditions (EXPECTED BEHAVIOR — documented, not a defect)**: firing while
mid-window past the entry_wait is **allowed** as long as the other gates pass —
this is identical to today's f6 behavior at its 270 s wait. The paper would have
entered at exactly 180 s; the bot entering late books a worse `entry`/`exec_entry`,
which the Section 1 telemetry captures as an elevated mirror gap / drift for that
fill. No special "too late in window" suppression exists or is added (INV-F6).

### Alternative Flows
- **UC-16-A1: paper already entered at 180 s; bot boots at 200 s** — the mirror's
  `live_sigma` and Δ reflect the paper's ongoing window; the bot's late entry is at
  a different (later) price. The dashboard will show the live-F1 point offset from
  the paper-F1 point for that window — expected, and measured via the entry
  decomposition.
- **UC-16-A2: bot boots after the window's paper entry but the fire has passed**
  (Δ fell back below 50, or p dropped) — the F1 gate no longer fires; no order.
  Only a *currently-passing* signal fires.

### Error Flows
- **UC-16-E1: window rolls immediately after boot** — the first evaluable window is
  W+1 (fresh); normal UC-5 flow. No late-entry concern.

### Edge Cases
- **UC-16-EC1: extreme late entry (near window end)** — firing at, say, 880 s of a
  900 s window is still allowed if gates pass; the settlement horizon is very short.
  Documented as accepted (same as f6 today); the audit's execution-buffer item
  (UC-6) may note late-window fills as a slippage contributor. No hard cutoff is
  added by this feature.

### Data Requirements
- **Input**: `elapsed_min` at first fresh snapshot; F1 gate inputs.
- **Output**: an immediate fire (and possible fill) if all gates pass; else a skip.
- **Side Effects**: as UC-5 on fire; late entry recorded with its worse `entry`.

---

## UC-17: Gate boundary values (elapsed 180.0 s, Δ = 50, p = 0.65, entry = 0.92)

**Actor**: evaluate
**Preconditions**: UC-1 F1 config; a `MirrorSnap` sitting exactly on a gate
boundary.
**Trigger**: a tick at an exact threshold.

### Primary Flow (Happy Path — each boundary)
- **UC-17-A1: `elapsed_min == 3.0` (exactly 180.0 s)** — the entry_wait gate is
  `s.elapsed_min < cfg.entry_wait_min` (engine.rs 97). At exactly `3.0`, `3.0 <
  3.0` is FALSE → the gate does NOT skip → entry_wait PASSES at exactly 180 s. This
  matches the paper's "enter at exactly 180 s" (AC-1, FR-8 entry_wait item).
- **UC-17-A2: `|delta_from_open| == 50.0`** — the threshold gate is `delta.abs() <
  cfg.delta_threshold` (engine.rs 104). At exactly `50.0`, `50 < 50` is FALSE → not
  "DELTA LOW" → PASSES. The threshold is inclusive at 50.
- **UC-17-A3: `p == 0.65` exactly** — the p gate is `pv < cfg.p_model_threshold`
  (engine.rs 154). At exactly `0.65`, `0.65 < 0.65` is FALSE → not "P-LOW" →
  PASSES. Inclusive at 0.65.
- **UC-17-A4: `entry == 0.92` exactly** — the band is `0.50 < entry` and `entry >
  cfg.max_entry_price` → skip (engine.rs 189-198). At exactly `0.92`, `0.92 > 0.92`
  is FALSE → not "HIGH" → PASSES (inclusive at the cap). `entry == 0.50` → the
  `entry <= 0.5` reset/`NO PRICE` path (189-193) rejects it (exclusive at the
  floor).

**Postconditions**: all four F1 thresholds are inclusive at their nominal value
(≥180 s, ≥50 Δ, ≥0.65 p, ≤0.92 entry) and exclusive just beyond; this matches the
paper F1 semantics (the FR-8 audit MUST confirm the paper uses the same
inclusive/exclusive sense — a mismatch is a parity gap).

### Alternative Flows
- **UC-17-A5: just beyond each boundary** — `elapsed = 179.9 s` → WAIT;
  `Δ = 49.9` → DELTA LOW; `p = 0.6499` → P-LOW; `entry = 0.9201` → HIGH
  (`skip_band`). Each is the "does not fire" side of its boundary.

### Error Flows
- **UC-17-E1: floating-point representation at the boundary** — `elapsed_min`,
  `delta`, `p`, and `entry` are `f64`; a value the paper computes as "exactly 50"
  may be `49.99999997` in one engine and `50.00000002` in the other. The FR-8 audit
  MUST decide whether such sub-epsilon differences can flip a fire/skip at the
  boundary and, if so, quantify/accept it (a `GAP-ACCEPTED` boundary-jitter note is
  acceptable; the volume of exactly-on-boundary windows is tiny).

### Edge Cases
- **UC-17-EC1: p is `None`** — if the sigma/price is invalid, `p_model_classic`
  returns `None`; the p-gate treats `None`-p as **PASS** (engine.rs 152-157,
  comment "None-p PASSES"). Combined with UC-14, a fallback-to-local sigma could
  still produce a `Some(p)` on the wrong formula — reinforcing why UC-14 must fail
  closed rather than relying on a `None`-p skip.

### Data Requirements
- **Input**: boundary-exact `elapsed_min`, `delta`, `p`, `entry`.
- **Output**: fire (inclusive side) or skip (exclusive side) per boundary.
- **Side Effects**: as UC-5 on fire.

---

## UC-18: Daily loss stop ($30) crossed mid-day → all subsequent triggers skip with `skip_loss_stop`

**Actor**: place_live (loss-stop gate) in F1 context
**Preconditions**: UC-1 F1 config, `LIVE_TRADING=1`, `DAILY_LOSS_STOP=30` (the
recommended value for the $147.73 subaccount, FR-9); cumulative `day_pnl` for the
UTC day has reached `<= -30` after a run of losing F1 windows.
**Trigger**: an F1 fire on any window after the stop is crossed.

### Primary Flow (Happy Path)
1. A losing sequence drives `s.day_pnl <= -30` (each loss resolved by the settler
   into `day_pnl`).
2. The next F1 fire calls `place_live`; the loss-stop gate `day_pnl <=
   -daily_loss_stop` is TRUE → one `skip_loss_stop` row (session `f1_d50cap75`);
   no order; pre-order gate → no retry attempt consumed (Section 1 UC-6-A1).
3. Every subsequent F1 fire that day writes another `skip_loss_stop` row and places
   no order (skips don't latch; re-evaluated per tick — Section 1 UC-5-EC1).

**Postconditions**: once the day's loss stop is hit, no further real F1 orders are
placed until the UTC-day reset zeroes `day_pnl`; the small subaccount is protected
from a runaway day (PRD 2.10 "small subaccount" risk). The dashboard live-F1 series
flat-lines for the rest of the day while paper-F1 keeps trading — an expected,
auditable divergence.

### Alternative Flows
- **UC-18-A1: UTC-day reset clears the stop** — at the next UTC day the reset block
  zeroes `day_pnl`/`trades_today` and F1 trading resumes (Section 1 UC-5-A1).
- **UC-18-A2: `DAILY_LOSS_STOP` misconfigured to the old $100** — if the drop-in
  env still carries `DAILY_LOSS_STOP=100`, the stop is 68% of the $147.73 balance
  (too large, PRD 2.10). Not a code error, but the runbook (UC-6/UC-22) MUST
  specify `30` for F1; QA verifies the deployed env value.

### Error Flows
- **UC-18-E1: `day_pnl` accounting lag** — if a fill's settlement hasn't resolved
  yet, `day_pnl` may not reflect the latest loss when the next fire evaluates; the
  stop trips one window late. Acceptable (bounded by $5/window); documented.

### Edge Cases
- **UC-18-EC1: exactly `day_pnl == -30`** — the gate is `day_pnl <=
  -daily_loss_stop`; `-30 <= -30` is TRUE → the stop trips at exactly `-$30`
  (inclusive). One more $5 window cannot push it further because it is already
  gated.

### Data Requirements
- **Input**: `s.day_pnl`, `DAILY_LOSS_STOP=30`.
- **Output**: `skip_loss_stop` rows (session `f1_d50cap75`) for all post-stop
  fires.
- **Side Effects**: no orders after the stop; ledger skip rows only.

---

## UC-19: Deploy / restart mid-window (latch state lost) → must not double-order a window that already filled

**Actor**: main()/boot, signal_loop
**Preconditions**: UC-1 F1 config, `LIVE_TRADING=1`; a live F1 position already
FILLED for window W (latched in the prior process); the operator redeploys the
binary or systemd restarts the service **before** window W rolls.
**Trigger**: the new process boots into window W with a fresh, empty latch.

### Primary Flow (Happy Path — the hazard)
1. Prior process: filled window W, `fired_window = W` (in-memory latch, main.rs
   805/1046), `trades_today` persisted via `save_live_state`.
2. Restart: the new process initializes `fired_window = None` and `attempt_window
   = None` (main.rs 805/808) — the latch is **process-local and NOT persisted**;
   there is **no ledger replay guard** that reconstructs `fired_window` from the
   ledger/pending on boot (verified: `load_live_state`, main.rs 395/456, restores
   `day_pnl`/`trades_today`/`total_pnl` but not the per-window latch).
3. If window W is still current and the F1 gate still fires, the new process sees
   `already == false` (main.rs 1014) and MAY place a **second** order for the
   already-filled window W.

**Postconditions (REQUIRED)**: at most **one filled position per window** must
hold across restarts (Section 1 INV-2 must survive a redeploy). The E2E suite MUST
exercise a mid-window restart and assert no second fill for a window that already
filled.

### Error Flows
- **UC-19-E1 (FLAGGED GAP — no replay guard exists today)** — because the latch is
  in-memory only, a restart mid-window CAN re-order window W. FR-10 says this
  feature adds **no new safety logic**, so the go-live audit (UC-6) MUST address
  this residual risk via one of:
  1. **GAP-FIXED** — a boot-time ledger/pending replay that reconstructs
     `fired_window` for the current window (if a `filled`/`partial`
     `LiveTriggerRecord` exists for `win_key`, pre-latch it). (This would be new
     safety logic — a scope decision for the planner/architect, possibly deferred
     to a follow-up, but it MUST be an explicit decision.)
  2. **GAP-ACCEPTED** — written rationale bounding the exposure: a redeploy is a
     rare, operator-initiated event; the second order is still a single $5 IOC
     capped by the band/daily-stop, subaccount #1 isolates the blast radius, and
     the operator times deploys to window rolls. The residual is accepted and
     monitored via the ledger (a duplicate window in `LiveTriggerRecord` fills is
     detectable offline).
  Until fixed or explicitly accepted, this is a go-live consideration, not a silent
  assumption.

### Edge Cases
- **UC-19-EC1: restart between windows** — if the restart lands after W rolls to
  W+1 (no fill yet in W+1), the fresh empty latch is correct — W+1 SHOULD be
  eligible. The hazard is strictly "restart while the just-filled window is still
  current"; QA must distinguish the two.
- **UC-19-EC2: restart mid-retry (no fill yet)** — attempt state is lost on
  restart; the new process may re-attempt within W under a fresh budget. This does
  NOT violate INV-2 (no fill happened), only potentially exceeds the intended `N`
  attempts across the restart boundary. Documented; bounded by band/cap/stop.

### Data Requirements
- **Input**: persisted `LiveState` (no latch); the ledger's prior `filled` row for
  W; the current `win_key`.
- **Output**: no second order for an already-filled W (required); the audit verdict
  on the replay-guard gap.
- **Side Effects**: at risk — a duplicate $5 IOC on subaccount #1 if the gap is
  neither fixed nor accepted (the outcome this UC exists to surface).

---

## UC-20: Subaccount #1 balance insufficient for the order → order-error path

**Actor**: place_live + OrderClient
**Preconditions**: UC-1 F1 config, `LIVE_TRADING=1`, `SUBACCOUNT=1`; subaccount
#1's balance (started at $147.73, drawn down by prior losses/positions) is below
what a $5 × `count` IOC requires; the F1 gate fires.
**Trigger**: `place_live` sends the IOC and Kalshi rejects it for insufficient
funds.

### Primary Flow (Happy Path)
1. `place_live` computes `count`/`price` and sends the IOC via `create_ioc`
   (routed to subaccount #1, main.rs 466-484).
2. Kalshi returns an error or a non-201 status for insufficient balance → this maps
   to `order_error` (transport/`Err`) or `rejected` (non-201) per Section 1 FR-3.
   One outcome row is written (session `f1_d50cap75`); it consumes one retry
   attempt (post-order); does NOT latch (UC-15).
3. On the bounded retry, if the balance is still insufficient, the second attempt
   also errors/rejects → budget exhausted, window not traded (UC-15-E1).

**Postconditions**: a balance shortfall degrades to the standard error/reject path
— no crash, no double-latch, a telemetry row per attempt; subaccount isolation
means only subaccount #1 is affected, never the main balance (FR-9, INV-F6).

### Alternative Flows
- **UC-20-A1: `count` clamps but still unaffordable** — `count` is clamped to
  `[1, MAX_COUNT]`; even a `count == 1` $5 IOC can be rejected if the balance is
  near zero. Same error/reject handling.

### Error Flows
- **UC-20-E1: partial affordability** — if a smaller-than-requested fill is
  possible, Kalshi may partially fill (`partial`, UC-5-A3) rather than reject;
  handled as a normal partial, `trades_today += 1`, latch consumed. Only a true
  rejection takes the error path.

### Edge Cases
- **UC-20-EC1: balance drained to effectively zero** — every subsequent F1 fire
  error/rejects and never fills; combined with the daily loss stop (UC-18), the bot
  effectively halts on the drawn-down subaccount. This is the intended fail-safe for
  the small-subaccount risk (PRD 2.10); ops top up or stop the run.

### Data Requirements
- **Input**: subaccount #1 balance; `count`/`price`; Kalshi order response.
- **Output**: `order_error`/`rejected` row (or `partial` if partially affordable),
  session `f1_d50cap75`.
- **Side Effects**: retry budget consumed; no latch on full rejection; only
  subaccount #1 touched.

---

## UC-21: `threshold_gap` parity (open question) → determine paper semantics, verdict before go-live

**Actor**: Operator/architect (audit) + evaluate
**Preconditions**: paper F1 config sets `threshold_gap = 0.1`; the Rust gate has
**no** `threshold_gap` logic (confirmed absent — engine.rs has no such branch).
**Trigger**: the FR-8 audit reaches the `threshold_gap` item (a listed go-live
gate item, UC-6).

### Primary Flow (Happy Path)
1. The auditor inspects the **paper engine** to determine what `threshold_gap`
   actually does for the `pmodel`/F1 path: whether it (a) affects the entry/side
   decision (e.g. a hysteresis band around `delta_threshold` or `p_model_threshold`,
   or a dead-zone that suppresses fires near the threshold), or (b) is inert for the
   pmodel path (used only by a different strategy type, or only for display).
2. A **verdict** is recorded (UC-6):
   - **MATCH** — if `threshold_gap` is inert for the F1 pmodel path, the Rust gate's
     absence of the logic is correct; no divergence. (Rationale documented.)
   - **GAP-FIXED** — if it affects entry/side, the Rust gate replicates it (a code
     change in engine.rs), with the verdict pointing at the change and a unit test.
   - **GAP-ACCEPTED** — if it affects entry only marginally and replication is
     deferred, the divergence is quantified (how many historical F1 fires would
     flip) and accepted in writing.

**Postconditions** (AC-5): the `threshold_gap` open question (PRD 2.10) is closed
with an explicit verdict before `LIVE_TRADING=1` for F1. No silent assumption that
"the Rust gate is close enough" is permitted.

### Alternative Flows
- **UC-21-A1: `threshold_gap` interacts with `delta_threshold`** — if the paper uses
  `delta_threshold ± threshold_gap` as a hysteresis, the Rust gate's plain
  `delta.abs() < cfg.delta_threshold` (engine.rs 104) diverges near Δ ≈ 50 ±
  (0.1-scaled). The audit MUST quantify the fraction of near-boundary windows
  affected (ties to UC-17's boundary analysis).

### Error Flows
- **UC-21-E1: paper semantics undeterminable from available source** — if the paper
  F1 config lives only in the paper DB (not this repo) and its `threshold_gap`
  handling cannot be read, the auditor MUST obtain it (from the paper engine
  maintainer or a live probe) rather than guess. An undetermined verdict is NOT a
  valid go-live state.

### Edge Cases
- **UC-21-EC1: `threshold_gap` affects side-selection dead-zone** — if it suppresses
  fires when `|delta|` is within `threshold_gap` of the threshold (an ambiguity
  guard), omitting it makes the Rust gate fire on marginally-weaker signals than
  paper — worse entries. This would be the highest-impact divergence and MUST be
  GAP-FIXED, not merely accepted, if confirmed.

### Data Requirements
- **Input**: paper F1 config (`threshold_gap = 0.1`) and its `compute_p_model` /
  evaluate path; the Rust engine.rs gate.
- **Output**: a MATCH / GAP-FIXED / GAP-ACCEPTED verdict in the audit doc (UC-6),
  plus a code change + test if GAP-FIXED.
- **Side Effects**: possibly a gate change in engine.rs (if GAP-FIXED); otherwise
  documentation only.

---

## UC-22: Rollback to f6 (config/binary swap, no data migration)

**Actor**: Operator
**Preconditions**: F1 is live and the operator decides to revert (edge erased,
slippage worse than modeled, or any go-live regret — PRD 2.10).
**Trigger**: a rollback decision.

### Primary Flow (Happy Path)
1. The operator restores the previous `*.bak` binary and the previous drop-in env
   file — the one that **lacks** `SESSION`/`MIRROR_SESSION` — so the bot reverts to
   the f6 default (UC-2). No data migration is required (NFR-4).
2. The operator restarts `kalshi-shadow-com`.
3. The bot boots f6: `SessionConfig::f6_wait270()`, `max30` sigma key,
   `f6_wait270` mirror key, `f6_wait270_shadow` telemetry tag — byte-identical to
   pre-F1 (INV-F1). The startup config log confirms the f6 params (UC-1 step 5,
   with f6 values).

**Postconditions** (NFR-4): rollback is a pure config/binary swap; the ledger is
unchanged (append-only; F1 rows remain, correctly tagged `f1_d50cap75` — no
rewrite); the bot resumes f6. The exact file paths + restart command are written
in the audit/runbook doc (a go-live deliverable).

### Alternative Flows
- **UC-22-A1: rollback by env only (same binary)** — if the current binary already
  supports both sessions (it does — one binary, env-selected, FR-2), the operator
  can revert by simply unsetting `SESSION`/`MIRROR_SESSION` (or setting
  `SESSION=f6_wait270`) and restarting — no binary swap needed. The `*.bak` binary
  swap is the belt-and-suspenders path.
- **UC-22-A2: disable live entirely** — set `LIVE_TRADING=0` (or unset) to drop to
  shadow/log-only while keeping F1 selected, if the operator wants to keep observing
  F1 without real orders.

### Error Flows
- **UC-22-E1: partial rollback (binary reverted, env not)** — if the operator
  restores the old binary but leaves `SESSION=f1_d50cap75` in the env, and the old
  binary predates FR-2 (no F1 factory / no `SESSION` handling), it ignores the var
  and runs f6 — effectively still a rollback, but the env is misleading. The runbook
  MUST specify reverting BOTH artifacts to avoid confusion; QA verifies the startup
  log shows f6 after rollback.

### Edge Cases
- **UC-22-EC1: rollback mid-window with an open F1 position** — an F1 position filled
  just before rollback is still `pending`/settling; the resolver (unchanged) settles
  it into a `ResolveRecord` regardless of the current session. Rollback does not
  strand open positions; they resolve on their own schedule.

### Data Requirements
- **Input**: the previous `*.bak` binary + previous drop-in env file (no F1 vars);
  the restart command.
- **Output**: a running f6 bot (byte-identical to pre-F1); the ledger intact.
- **Side Effects**: process restart; no data migration; no ledger rewrite.

---

## Flow counts

- **Primary flows (one per use case, UC-1 … UC-22):** 22
- **Alternative flows (UC-N-Ax):** 27
  (UC-1: 3, UC-2: 1, UC-3: 1, UC-4: 1, UC-5: 3, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 0, UC-15: 1, UC-16: 2, UC-17: 1,
  UC-18: 2, UC-19: 0, UC-20: 1, UC-21: 1, UC-22: 2)
  (Note: UC-7 and UC-14 and UC-17 also carry lettered sub-*cases* inside their
  primary flow — `UC-7-A1..A3`, `UC-14-A1/A2`, `UC-17-A1..A5` — counted where they
  appear; the tally above counts the distinct alternative-flow bullets.)
- **Error flows (UC-N-Ex):** 22
  (UC-1: 1, UC-2: 1, UC-3: 1, UC-4: 1, UC-5: 1, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 1, UC-15: 1, UC-16: 1, UC-17: 1,
  UC-18: 1, UC-19: 1, UC-20: 1, UC-21: 1, UC-22: 1)
- **Edge cases (UC-N-ECx):** 25
  (UC-1: 2, UC-2: 1, UC-3: 2, UC-4: 2, UC-5: 1, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 2, UC-15: 1, UC-16: 1, UC-17: 1,
  UC-18: 1, UC-19: 2, UC-20: 1, UC-21: 1, UC-22: 1)

**Total scenarios:** 96 (22 primary + 27 alternative + 22 error + 25 edge).

## Go-live gate summary (traceability for QA / E2E)

The following must all be green before real F1 orders (`LIVE_TRADING=1`) are
enabled — each maps to a UC and a PRD acceptance criterion:

| Gate | UC | PRD AC |
|------|----|--------|
| F1 config selected + logged | UC-1 | AC-1 |
| Default f6 byte-identical | UC-2 | AC-4, NFR-1 |
| MIRROR reads F1 session key | UC-3, UC-11 | AC-3 |
| Sigma keyed by `sigma_type` (unit test, both `max10`+`max30`) | UC-4 | AC-2 |
| Entry-fidelity audit doc complete (7 verdicts) | UC-6, UC-21 | AC-5 |
| Fail-closed on non-positive sigma resolved | UC-14 | AC-5 (sigma source) |
| Restart double-order guard decided (fix or accept) | UC-19 | FR-10 / NFR-5 |
| Deployed telemetry shows mirror-gap ≈ 0 + decomposition | UC-5 | AC-6 |
| Dashboard live-F1 vs paper-F1 visible | UC-5 | AC-7 |
| Rollback procedure documented | UC-22 | NFR-4 |
