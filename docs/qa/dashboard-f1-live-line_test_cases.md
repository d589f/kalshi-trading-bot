# Test Cases: Dashboard — Separate LIVE F1 Line + LIVE Table/Cards

> Based on [PRD](../PRD.md) (section 3, internal id `dashboard-f1-live-line`) and [Use Cases](../use-cases/dashboard-f1-live-line_use_cases.md)

This is a **dashboard-only** feature on the Rust binary (`kalshi_rs`, `dashboard.rs`
+ producer hooks in `main.rs` + `ResolveRecord` in `ledger.rs`). It splits the
single mislabelled "shadow/LIVE" green series into **two** distinct series — a
**real-fill LIVE line** (raw `$5`) and a **strategy shadow twin line** (`$100`,
`twin5`-normalized) — via a single `live: bool` on `TrigSummary`. The tests below
are realized either as `cargo test` unit/integration tests over the pure split /
resolve / serde / replay seams, or as documented **OPS** verification against the
prod boxes (the chart JS is embedded HTML/JS and is verified visually plus by a
documented formula check). It **reuses, unchanged**, the Section 1 rails
(`LiveTriggerRecord`, `Pending.live`, latch-on-confirmed-fill, bounded retry,
`last_filled_window` boot seed) and the Section 2 F1 mirror/subaccount rails; where
a scenario is a pure reuse of a Section 1/2 rail this doc references the
corresponding `S1-UC` / `S2-UC` rather than re-deriving it.

Each test case carries a **Type**:

- **UNIT** — a `#[test]` (or `serde` round-trip) over a pure helper: the
  `TrigSummary.live` default, the `compare()` split, `Dash::resolve` predicate, the
  `body_json` `lv_*` aggregates, `replay_line_into` flag derivation, the resolver
  `retain` predicate, the anchor recurrence (documented formula). No network, no
  real order. Preferred; deterministic and fast.
- **INTEGRATION** — a `cargo test` wiring several seams (e.g. a constructed
  `Dash` with a mixed `shadow_com` vector → `compare()` → `body_json`, or a
  `POST /shadow_com` deserialize → store → `/stats`, or a mixed JSONL fixture →
  `replay_line_into` → dual `Dash::resolve`). No real money.
- **OPS** — documented operational verification on the prod EU box
  (`34.32.177.126`, systemd `kalshi-shadow-com`) and/or the Buffalo dashboard
  (`23.95.217.78:8890`). Used where the embedded chart JS, real live fills, a
  deploy, or an EU/Buffalo restart is inherent and cannot be a pure test.

---

## Seams under test (pure, extracted for testability)

Exact names are the planner's to fix; the **contracts** are fixed here. These are
the points the test-writer targets.

- **`TrigSummary.live: bool` with `#[serde(default)]`** (`dashboard.rs` ≈ 48-74) —
  the additive flag (constraint 2). Absent → `false` (shadow). Present-but-wrong-type
  → element parse **fails** (strict bool), so the whole `/shadow_com` array parse
  fails and the handler `400`s (keeping the last good `d.shadow_com`).
- **`compare()` split** (`dashboard.rs` ≈ 224-302) — per `wkey`, **two independent
  `.find()`s** over `self.shadow_com`: first `live=false` → `com_*`, first
  `live=true` → `lv_*` (constraint 6). Adds `match_lv` = `lv_side` vs `f1_side`
  (denominator: both present). Dedupes by window (first match per flag);
  `out.truncate(300)`.
- **`Dash::resolve(ticker, side, entry, result, won, pnl, live: bool)`**
  (`dashboard.rs` ≈ 120-131) — predicate `ticker` + `side` + `entry` within `1e-9` +
  `result.is_none()` + **`t.live == live`** (constraint 4). Two call sites pass the
  flag: replay (`main.rs` ≈ 362) and runtime resolver (`main.rs` ≈ 874).
- **Resolver `retain` predicate hardened with `x.live == pd.live`** (`main.rs`
  ≈ 882-884) — so a twin resolve (`pd.live=false`) cannot retain-out the live
  pending when `eff == signal_entry` (constraint 5).
- **`ResolveRecord.live: bool`** additive (`ledger.rs` ≈ 86-98) — written from
  `pd.live` (`main.rs` ≈ 846); replay reads `v["live"].as_bool()` → absent = `false`
  (constraint 3). Ship in the **same binary** as the producer `live=true`/`live=false`
  tagging (OPS gate).
- **`replay_line_into` trigger push** (`main.rs` ≈ 346-359) — sets `live` from the
  row: `LiveTriggerRecord` (`live:true`) → `true`; legacy `TriggerRecord` (no `live`)
  → `false` (FR-6). The `is_dashboard_trigger` outcome filter (≈ 340-344) still gates
  which rows become `d.triggers`.
- **Producer twin-emit placement + `twin_window` latch** — inside the live branch
  (`main.rs` ≈ 1133-1166), inside `if !already`, gated **only** by `twin_window`
  (a sibling of `attempt_window`/`fired_window`, reset on window roll ≈ 1126-1130),
  **decoupled from `retry_gate`**, **before** `place_live` (constraint 1). Tags:
  `place_live` `TrigSummary.live=true` (≈ 1502-1515); `emit_trigger`
  `TrigSummary.live=false` (≈ 1261-1274).
- **`body_json` LIVE aggregates** (`dashboard.rs` ≈ 304-340) — `summary.lv_match` /
  `lv_total` / `lv_pct` / `lv_positions` / `lv_pnl` (constraint 6).
- **Chart JS anchor + LIVE-line `build()`** (`dashboard.rs` ≈ 641-667) — anchor =
  running pink `f1_twin` cumulative **strictly before** the first visible live
  window, `0` if none; LIVE = anchor + Σ **raw** `lv_pnl` from `idx0` onward; no
  leading tail; no `NaN`; `null` `lv_pnl` → no step, real `0.0` → step (constraint 7).
- **Chart JS table + cards render** (`dashboard.rs` ≈ 545-549 / 683-702) — US column
  → LIVE column; US cards → LIVE cards; green label = shadow `twin5`; LIVE label =
  real-`$5` (constraint 8).

## Configuration anchors

- EU producer env (prod, reused Section 2): `LIVE_TRADING=1 STAKE=5 SUBACCOUNT=1
  MAX_ENTRY=0.92 SESSION=f1_d50cap75 MIRROR_SESSION=f1_d50cap75 DAILY_LOSS_STOP=30`.
- Reused P0b rails (unchanged): `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`.
- Shadow twin sizing: `emit_trigger` sizes at `cfg.stake=$100` (`main.rs` ≈ 1197) —
  a **known artifact**, `twin5`-normalized on the chart, NOT changed (FR-4).
- LIVE line sizing: raw `$5` real fills — **never** `twin5`-repriced (NFR-5).
- **Regression anchor (constraint 9):** an all-`live=false` (f6-era) `shadow_com`
  payload renders byte-identically to the pre-feature shadow view; `/paper`,
  `/paper_f1`, `/shadow_com` wire formats are additive-only; `place_live`'s order
  path (pricing/sizing/subaccount) is byte-unchanged; `day_pnl`/`total_pnl` advance
  **exactly once** per real live fill even with a duplicate twin pending.

## Architect binding constraints → test-case index

| # | Binding constraint | Test cases |
|---|--------------------|-----------|
| 1 | Twin emit inside `if !already` + live branch, gated ONLY by `twin_window` (sibling of `attempt_window`, reset on window roll), decoupled from `retry_gate`, BEFORE `place_live`; once/window across ticks+retries+no-fills+skips; NOT emitted when `fired_window` latched (incl. boot-seeded FILLED restart); re-emitted after NO-FILL restart (dup accepted); never mutates `fired_window`/`attempt_count`/`last_attempt_ts` | TC-1.2, TC-6.1, TC-6.5, TC-7.1, TC-7.6, TC-8.1, TC-8.2, TC-8.6, TC-8.7, TC-11.1, TC-11.7 |
| 2 | `TrigSummary.live` `#[serde(default)]`: absent→false (merge blocker); wrong-type→array parse FAILS, handler 400s, keeps last good `shadow_com` | TC-9.1, TC-9.3, TC-9.5, TC-9.6 |
| 3 | `ResolveRecord.live`: resolver writes `pd.live`; replay reads `v["live"]` absent→false; ship same binary as producer (OPS gate) | TC-5.2, TC-10.1, TC-10.2, TC-10.3, TC-10.5, TC-D.2 |
| 4 | `Dash::resolve(.., live)`: twin+live rows at EQUAL entry → `live=true` updates ONLY live, `live=false` ONLY twin (merge blocker, order-independent — both orders); both call sites pass the flag | TC-1.9, TC-5.1, TC-5.2, TC-5.6, TC-5.7, TC-10.2 |
| 5 | Resolver `retain` hardened with `x.live == pd.live`: twin resolve cannot remove the live pending (UNIT if extractable, else INTEGRATION via pending-vec sim) | TC-13.3 |
| 6 | `compare()` split: `com_*` from `live=false` only, `lv_*` from `live=true` only; both→both; live-only→`com_*` null; shadow-only→`lv_*` null; `lv_*` = side/entry/delta/pnl/result/won/count/p + `match_lv` (denominator = both `lv_side` AND `f1_side` present) | TC-1.1, TC-1.10, TC-3.3, TC-4.1, TC-4.2, TC-4.3, TC-4.4, TC-15.6 |
| 7 | Chart anchor = pink `f1_twin` cumulative STRICTLY BEFORE first visible live window, 0 if none; recompute per refresh; LIVE = anchor + Σ raw `lv_pnl`, only from first lv window, no NaN; `lv_pnl` null→no step vs real `0.0`→step | TC-14.1, TC-14.2, TC-14.4, TC-14.5, TC-14.6, TC-14.7, TC-14.8, TC-15.1, TC-15.4 |
| 8 | UI: table US col → LIVE (side/entry/Δ/=pa); US cards → LIVE cards w/ explicit denominator copy; green label = shadow `twin5`; LIVE label = real-`$5` (notes modeled-vs-real offset) | TC-3.1, TC-3.2, TC-3.4, TC-3.5 |
| 9 | Regression: f6-era all-shadow renders unchanged; `/paper`,`/paper_f1`,`/shadow_com` additive-only; `day_pnl`/`total_pnl` advance once/real-fill even with dup twin pending; `place_live` order path byte-unchanged | TC-2.3, TC-9.1, TC-9.2, TC-11.2, TC-11.7, TC-D.4 |

---

## UC-1 — E2E live F1 window → twin + live rows → compare split → chart → dual resolve

| # | UC Scenario | Type | Test Case (precondition → steps) | Expected Result |
|---|-------------|------|----------------------------------|-----------------|
| TC-1.1 | UC-1 primary (AC-3) | INTEGRATION | Construct a `Dash` whose `shadow_com` holds, for one `wkey`, a `live=true` fill row (`entry=eff`, resolved, `$5`-scale `pnl`) and a `live=false` twin row (`entry=signal_entry`, resolved, `$100`-scale `pnl`); `paper_f1` has a resolved trade in the same window; call `compare()`. | The window yields BOTH field sets: `com_*` sourced only from the twin (`com_entry==signal_entry`, `$100` `com_pnl`) and `lv_*` only from the fill (`lv_entry==eff`, raw `$5` `lv_pnl`); `match_lv` set from `lv_side` vs `f1_side`. `com_*` and `lv_*` never read the same row. |
| TC-1.2 | UC-1 primary (FR-2/3/4, constraint 1) | UNIT | Drive one window of the live branch (extracted latch/emit helper): `place_live` fills; assert the two `TrigSummary` pushes and the twin-latch behavior. | Exactly ONE `TrigSummary` with `live=true` (`entry=eff`, `count=fill`) from `place_live` and exactly ONE with `live=false` (`entry=signal_entry`, `count=round($100/entry)`) from `emit_trigger`; the twin is gated by `twin_window` (set once), emitted **before** `place_live`, and its emission does not touch `fired_window`/`attempt_count`/`last_attempt_ts`. |
| TC-1.3o | UC-1 primary (AC-5/8) | OPS | On the deployed F1 run, watch the Buffalo dashboard for one live-fill window. | Green `com_twin` (shadow) steps by the `twin5` twin PnL; the LIVE line (`#ff7b72`) plots from this window, anchored to the pink `f1_twin` cumulative just before it, stepping by raw `$5` `lv_pnl`; dual resolve attaches `$5` PnL to the LIVE row and `$100` PnL to the shadow row. |
| TC-1.4 | UC-1-A1 (NO-side) | UNIT | Same as TC-1.1 but `fire.side == No`. | `lv_side`/`com_side` render `NO`; per-side entry formulas unchanged (S1-UC-1-A1); the split logic is identical (partition by flag, not side). |
| TC-1.5 | UC-1-A2 (fill after re-quote) | UNIT | Live fill with `requote == true` (S1-UC-4); one live row `entry=eff`. | Still exactly one `live=true` row (`entry=eff`); the twin still fires exactly once (INV-D3). The wider `eff`-vs-`signal_entry` gap surfaces as a wider LIVE-vs-pink vertical gap (F1 180 s re-quote tail, PRD 2.10) — not asserted numerically here. |
| TC-1.6 | UC-1-A3 (partial fill) | UNIT | `fill>0`, `remaining_count>0` → `outcome=partial`, latches (S1-UC-3); live `TrigSummary` `count=fill`, `entry=eff`. | One `live=true` row sized to the partial `fill`; twin unaffected (still one `live=false` row at `$100`); split unchanged (see UC-15-A1). |
| TC-1.7 | UC-1-E1 (live ledger append fails) | UNIT | Force a serialize/append failure inside `place_live` (reuse S1-UC-13). | The loop never panics; the twin still emits; the latch decision still follows the returned outcome; the `TrigSummary`/`Pending` push is independent of the ledger append (missing ledger row is a logging gap, not a dashboard/trading error). |
| TC-1.8 | UC-1-E2 (`/shadow_com` POST fails) | INTEGRATION | EU push loop's `POST /shadow_com` returns an HTTP error / times out; Buffalo has a previously stored `d.shadow_com`. | The EU HTTP error is non-fatal (no crash on either box); Buffalo keeps rendering the **last** received `d.shadow_com` until the next successful push; no torn/empty overwrite. |
| TC-1.9 | UC-1-EC1 (equal entries) | UNIT | Twin and live rows share `(ticker, side, entry)` because `eff == signal_entry` (zero slippage); run `compare()` and both resolves. | `compare()` still splits correctly (partitions by the `live` flag, not by `entry`); `Dash::resolve` disambiguates by `t.live` (see TC-5.1); the LIVE line ($5 raw) and green line ($100 twin5) remain distinct data points even at equal entry. |
| TC-1.10 | UC-1-EC2 (live fill, no paper-F1) | UNIT | Window has a `live=true` row but no `paper_f1` trade; run `compare()`. | `lv_*` present, `f1_*` null → `match_lv == null` (excluded from the `lv_total` denominator, INV-D9); the LIVE line still steps by raw `lv_pnl`; the anchor uses the last pink value at/before this window (UC-14). |

---

## UC-2 — Shadow-only / pre-live → only twins, no LIVE line/column/cards (green as today)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-2.1 | UC-2 primary (NFR-2/AC-9) | INTEGRATION | `Dash` whose `shadow_com` holds ONLY `live=false` rows across several windows; call `compare()` + `body_json`. | Every window: `com_*` populated, `lv_*` **null**, `match_lv` null. `summary.lv_match`/`lv_total`/`lv_positions`/`lv_pct`/`lv_pnl` all `0`. No LIVE data to plot. |
| TC-2.2 | UC-2 primary (FR-4/5) | UNIT | The non-live `else` branch (`main.rs` ≈ 1167-1173) calls `emit_trigger`; inspect its `TrigSummary`. | The `TrigSummary` carries `live == false`; it keeps the `fired_window` latch; no `place_live`, no `live=true` row. |
| TC-2.3 | UC-2 primary (constraint 9, AC-9) | OPS | On a shadow-only box (Buffalo local, or EU before `LIVE_TRADING=1`), render the dashboard from an all-`live=false` feed. | Renders byte-equivalently to the pre-feature shadow view: green `com_twin` line + shadow match cards behave as before; LIVE line / LIVE column / LIVE cards are simply **absent**; **no parse errors**. |
| TC-2.4 | UC-2-A1 (US shadow present) | INTEGRATION | `self.triggers` (`us_*`) is non-empty in `/stats`; render table/cards. | `self.triggers` still appears in the `/stats` JSON but is **not** rendered in the table or cards (INV-D7); the blue `us` series is not fed (`build('us')` not called), consistent with pre-feature. |
| TC-2.5 | UC-2-A2 (mixed feed, late live rows) | INTEGRATION | Feed starts all `live=false`, then one `live=true` row appears in-period; re-run `compare()`. | Before the first `live=true` row: empty-LIVE rendering (UC-2). From the first `live=true` row: `lv_*`/LIVE line/column/cards begin (UC-1). Transition is the first live window (UC-14). |
| TC-2.6 | UC-2-E1 (empty feed) | INTEGRATION | `shadow_com` empty; call `compare()` and render. | `compare()` yields no `com_*`/`lv_*`; green and LIVE lines both empty; only pink/orange render if paper feeds exist; no crash, no undefined anchor (UC-14-EC1). |
| TC-2.7 | UC-2-EC1 (`LIVE_TRADING` toggled off mid-session) | INTEGRATION | Feed contains historical `live=true` rows, then only `live=false` rows for new windows. | New windows produce only twins; historical `live=true` rows keep their `lv_*`; the LIVE line stops extending (no new live windows) but retains its drawn history. Not a regression — the split is per-row, not per-session. |

---

## UC-3 — Table US column → LIVE column, US cards → LIVE cards (rendering)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-3.1 | UC-3 primary (FR-17, AC-6, constraint 8) | OPS | Load the Buffalo dashboard with a `compare()` output carrying `lv_*`; inspect the per-window table header + rows (`dashboard.rs` ≈ 545-549 / 698-702). | The middle **US (binance.us)** column group (`us_side`/`us_entry`/`us_delta`/`match_us`) is **removed**; a **LIVE** column (side / entry / Δ / `=pa` = `match_lv`) is rendered in its slot; the retained shadow (`com_*`) column is relabelled to name the **strategy shadow twin** (not "LIVE"); `us_*`/`match_us` are absent from the rendered rows. |
| TC-3.2 | UC-3 primary (FR-18, AC-7, constraint 8) | OPS | Inspect the summary cards (`dashboard.rs` ≈ 683-691). | The "US ↔ paper side-match" and "US: matched / windows" cards are **removed**; **LIVE↔paper-F1** cards appear: side-match % (`lv_pct`), fills count (`lv_positions`), and the realpnl "LIVE (period)" card sourced from **raw** `lv_pnl` (real `$5`, NOT `com_*` `twin5`); the shadow (`com_*`) side-match cards are retained, relabelled **SHADOW**. Card copy states the `match_lv` denominator explicitly ("of windows where both live and paper-F1 traded") and notes the LIVE line is real-`$5` with a modeled-vs-real rounding offset. |
| TC-3.3 | UC-3 primary (FR-12, constraint 6) | UNIT | Construct a `Dash` with N `live=true` rows in-period (some with a paper-F1 match, some without) and call `body_json`. | `summary.lv_positions` = count of `live=true` rows with `window_start >= started_iso`; `summary.lv_pnl` = Σ raw `lv_pnl` in-period; `lv_match`/`lv_total` count only windows where both `lv_side` and `f1_side` exist; `lv_pct = lv_match/lv_total` (guarded against div-by-zero). Existing `com_*` summary fields unchanged. |
| TC-3.4 | UC-3-A1 (com only, no lv) | OPS | Window has `com_*` but no `lv_*` (live no-fill/skip); render the row. | The LIVE column renders `—` for that row; the SHADOW column renders the twin. This is the key observability row: "paper/shadow traded, LIVE did not" (UC-6). |
| TC-3.5 | UC-3-A2 (lv only, no com) | OPS | Window has `lv_*` but no `com_*` (legacy/mixed feed; should not occur under steady-state per INV-D3); render the row. | The LIVE column renders; the SHADOW column renders `—`; the row does not break. |
| TC-3.6 | UC-3-E1 (`lv_*` absent from old `/stats`) | OPS | Buffalo build ahead of a stale EU feed: `/stats` payload lacks `lv_*`; the table JS reads `c.lv_side` etc. | Reads as `undefined` → renders `—` (same as null); no JS exception; the table still renders (UC-9). |
| TC-3.7 | UC-3-EC1 (table cap vs chart cap) | OPS | Feed has more than 60 LIVE windows; compare the table (sliced `d.compare.slice(0,60)`) vs the chart (300-cap). | LIVE rows older than the table slice appear on the chart but not the table; documented, not a bug. |
| TC-3.8 | UC-3-EC2 (`match_lv` null renders neutral) | OPS | A window with no paper-F1 or no live fill; inspect the `=pa` cell. | `mk(null)` renders the neutral dot `·` (≈ 557), distinct from `✓`/`✗`; a no-paper-F1 or no-live window shows `·`, not a false `✗`. |

---

## UC-4 — compare() split is exclusive (com_* from live=false, lv_* from live=true)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-4.1 | UC-4 primary (FR-10, AC-3, constraint 6) | UNIT | `shadow_com` with, across windows: one window with both flags, one with only `live=false`, one with only `live=true`; call `compare()`. | Both-window → `com_*` (from the `live=false` row) AND `lv_*` (from the `live=true` row); `live=false`-only window → `lv_*` **null**, `com_*` set; `live=true`-only window → `com_*` **null**, `lv_*` set. `lv_*` is sourced **exclusively** from `live=true`, `com_*` **exclusively** from `live=false` — never the same row. |
| TC-4.2 | UC-4 primary (FR-11, INV-D9) | UNIT | A window with `lv_*` and a `paper_f1` trade; another with `lv_*` but no `paper_f1`; call `compare()`. | `match_lv` computed (`lv_side` vs `f1_side`) only where both `lv_side` AND `f1_side` are present; the no-paper-F1 window has `match_lv == null`. Analogous to the existing `match_com`. |
| TC-4.3 | UC-4-A1 (multiple same-flag rows) | UNIT | A window with two `live=false` rows and two `live=true` rows; call `compare()`. | `compare()` takes the **first** `.find()` match per flag; later duplicates are ignored by the split (dedupe-by-window-takes-first). Exactly one `com_*` and one `lv_*` for the window. |
| TC-4.4 | UC-4-E1 (unresolved live row) | UNIT | A `live=true` row with `result==None`; call `compare()`. | `lv_pnl == null`; the split still populates `lv_side`/`lv_entry`/`lv_delta`; the chart step is deferred until resolved (UC-15); no error. |
| TC-4.5 | UC-4-EC1 (`wkey` collision) | UNIT | Two windows whose `window_start` share the first 16 chars (`wkey`) but differ in ticker; call `compare()`. | Both map to one `wkey`; the split inherits the pre-feature grouping; the first match per flag wins. Unchanged from pre-feature grouping. |

---

## UC-5 — Resolve disambiguation by the live flag (equal entries AND different entries)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-5.1 | UC-5 primary (FR-7, AC-2, constraint 4, merge blocker) | UNIT | A `Dash` with two `shadow_com` rows sharing `(ticker, side, entry)` (EQUAL `entry`, `eff == signal_entry`), one `live=true` and one `live=false`, both `result==None`; call `Dash::resolve(.., live=false)` then inspect, then `Dash::resolve(.., live=true)`. | `resolve(.., live=false)` updates **only** the `live=false` twin row (attaching `$100`-scale PnL), live row untouched; `resolve(.., live=true)` updates **only** the `live=true` row (attaching `$5`-scale PnL). Proven with equal `entry`. |
| TC-5.2 | UC-5 primary (FR-8/9, constraint 3/4) | UNIT | Verify the runtime resolver passes `pd.live` into `Dash::resolve` (`main.rs` ≈ 874) and writes `ResolveRecord.live` from `pd.live` (≈ 846); verify the replay branch (≈ 362) reads `v["live"].as_bool()` and passes it. | BOTH call sites pass the flag; the runtime resolve writes `ResolveRecord.live = pd.live`; the replay resolve reads the persisted `live` and passes it to `Dash::resolve`. Without this, restart-preserves-split (AC-8) fails at `eff == signal_entry`. |
| TC-5.3 | UC-5-A1 (different entries) | UNIT | Same as TC-5.1 but `eff != signal_entry` (non-zero slippage). | The pre-feature `entry`-within-`1e-9` predicate already disambiguates; the added `t.live == live` is redundant but harmless (still correct). The common case (slippage > 0). |
| TC-5.4 | UC-5-A2 (only live row) | UNIT | Only a `live=true` row exists for the window; call `resolve(.., live=true)` then `resolve(.., live=false)`. | `resolve(.., live=true)` matches the live row; the `live=false` resolve finds no matching row and is a silent no-op (UC-13). |
| TC-5.5 | UC-5-E1 (two live=true share tuple) | UNIT | Two `live=true` rows share `(ticker, side, entry)`, both unresolved (non-path under the S1 latch); call `resolve(.., live=true)`. | `resolve` updates the **first** unresolved match and `break`s (≈ 128); the second stays unresolved. Documented; the latch makes this a non-path under steady-state. |
| TC-5.6 | UC-5-EC1 (resolve order, both orders — constraint 4) | UNIT | Equal-entry twin+live rows; run the two resolves in **both** orders: (a) live-first then twin, (b) twin-first then live. | Order-**independent**: in both orders, each resolve targets its own flag; the live row ends with `$5` PnL and the twin row with `$100` PnL. The `t.live == live` term is the deciding factor. |
| TC-5.7 | UC-5-EC2 (`entry` differs < 1e-9 across flags) | UNIT | Twin and live entries numerically within `1e-9` (effectively equal); call both resolves. | The flag still disambiguates even when the `entry` term cannot; `t.live == live` decides (AC-2). |

---

## UC-6 — Live no-fill window → twin emitted, no live row → green steps, LIVE flat (KEY OBSERVABILITY)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-6.1 | UC-6 primary (FR-3, INV-D3, constraint 1) | INTEGRATION | Live branch where `place_live` returns `nofill` on all bounded attempts (S1-UC-2/8); the `twin_window` latch drives `emit_trigger`. | No `live=true` `TrigSummary` is pushed (S1-UC-2 pushes no dashboard row on no-fill); `fired_window` NOT latched; the twin **still fires exactly once** (independent of the no-fill); `compare()` → `com_*` set, `lv_*` null. |
| TC-6.2 | UC-6 primary (observability) | OPS | On the live dashboard, observe a real no-fill window. | The green `com_twin` line **steps** (twin exists + resolves) while the LIVE line stays **flat** across the window — a visible horizontal gap where real fills missed while the strategy would have traded. `trades_today`/`day_pnl` unchanged. |
| TC-6.3 | UC-6-A1 (paper-F1 also traded) | OPS | No-fill window where paper-F1 also traded. | Pink `f1_twin` steps, green `com_twin` steps, LIVE flat → the operator sees exactly how far real execution fell behind the signal; `match_lv` is null (no `lv_*`), so the no-fill does **not** count against the LIVE↔paper match % (INV-D9). |
| TC-6.4 | UC-6-A2 (paper-F1 did not trade) | UNIT | No-fill window where paper-F1 also did not trade; call `compare()`. | Only the green twin steps; pink and LIVE both flat; `com_*` set, `lv_*` and `f1_*` null. Consistent. |
| TC-6.5 | UC-6-E1 (twin mis-wired regression) | INTEGRATION | Regression guard: drive a no-fill window and assert the twin fires. | The twin MUST fire on a no-fill window (INV-D3). A regression where the twin depends on the live outcome (fires only on fill) would make the shadow line vanish on no-fill windows, re-introducing the defect — this test is the **merge-blocking** guard (constraint 1). |
| TC-6.6 | UC-6-EC1 (many no-fill windows) | OPS | Several consecutive no-fill windows. | The LIVE line is flat across all of them while green/pink climb; the accumulated horizontal gap equals total missed coverage. Expected, not a bug. |
| TC-6.7 | UC-6-EC2 (no-fill on FIRST intended window) | UNIT | The very first window the bot intends to trade no-fills; call `compare()` + anchor. | Still **no `lv_*`** (no fill), so the LIVE line has not started; the "first live-fill window" (UC-14) is the first window that actually **fills**, not the first that fires. |

---

## UC-7 — Live skip (daily-cap / loss-stop / band) → twin STILL emitted, no live row

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-7.1 | UC-7 primary (INV-D3, constraint 1) | INTEGRATION | Live branch where `place_live` short-circuits on a pre-order gate (`skip_daily_cap`/`skip_loss_stop`/`skip_band`, S1-UC-5/6); the `twin_window` latch drives `emit_trigger`. | `place_live` writes one skip `LiveTriggerRecord` (`live=true`) and returns without an order or a dashboard `TrigSummary` push; `fired_window` NOT latched; the twin **still fires once** (independent of the skip); `compare()` → `com_*` set, `lv_*` null. |
| TC-7.2 | UC-7-A1 (`skip_loss_stop`) | INTEGRATION | `day_pnl <= -30` trips the loss stop; subsequent windows skip the live order. | From the loss-stop trip, every subsequent window skips the live order but **still emits the twin** (INV-D3); the shadow line keeps tracking the strategy while the LIVE line goes flat for the rest of the day. The clearest demonstration of INV-D3's value. |
| TC-7.3 | UC-7-A2 (`skip_daily_cap`) | UNIT | `trades_today >= MAX_TRADES_DAY`; skip path with the twin latch. | Same shape: one `skip_daily_cap` row (`live=true`, no `TrigSummary`), twin emits once, LIVE flat, `com_*` set / `lv_*` null. |
| TC-7.4 | UC-7-A3 (`skip_band`) | UNIT | Entry outside `(0.50, 0.92]` → `skip_band`; the twin's `entry = signal_entry` may itself be outside the band. | The live order is skipped; the twin row is **still emitted** (the shadow records the strategy signal — it does not apply the live band gate). Documented divergence: the twin can carry an out-of-band entry while the live order did not fire. |
| TC-7.5 | UC-7-E1 (skip row append fails) | UNIT | Ledger append fails on a skip row (S1-UC-13). | No crash; the loop continues; the twin still emits. |
| TC-7.6 | UC-7-EC1 (repeated skip rows across ticks) | UNIT | A persistent skip gate + persisting fire across a multi-tick window (skips don't latch, S1-UC-5-EC1). | Multiple skip `LiveTriggerRecord` rows (one per tick) but **only one twin** (the `twin_window` latch fires once per window, INV-D3); the chart/table are unaffected by the extra skip rows (skips push no `TrigSummary`). |

---

## UC-8 — Bounded retry within a window (max 2) → twin emitted EXACTLY ONCE

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-8.1 | UC-8 primary (INV-D3, NFR-4, constraint 1) | UNIT | `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`, same `win_key`: attempt1 `nofill`, cooldown ticks, attempt2 `filled`; drive the retry state machine + twin latch. | The twin fires **exactly once** (on the first tick the window is not twin-latched and a fire is present); cooldown ticks blocked by `retry_gate` do NOT re-fire the twin; attempt2 fills → one `live=true` row, `fired_window` latched, twin NOT re-emitted. `compare()` → one `com_*` + one `lv_*`; exactly one green step and one LIVE step. |
| TC-8.2 | UC-8 primary (constraint 1, twin never mutates retry state) | UNIT | Emit the twin, then inspect `fired_window`/`attempt_count`/`last_attempt_ts`. | Emitting the twin leaves `fired_window`, `attempt_count`, and `last_attempt_ts` **unchanged** — the twin latch is fully decoupled from the retry/latch state. |
| TC-8.3 | UC-8-A1 (both attempts no-fill) | UNIT | Attempt1 and attempt2 both `nofill` (budget exhausted). | One twin, zero live rows → UC-6 shape (green steps, LIVE flat); the twin is still exactly once. |
| TC-8.4 | UC-8-A2 (`RETRY_MAX_ATTEMPTS=1`) | UNIT | Retry disabled; single attempt, then `nofill` or `filled`. | Single attempt per window (byte-identical to pre-P0b, NFR-4); twin exactly once; identical split behavior. |
| TC-8.5 | UC-8-E1 (`order_error`/`rejected` mid-retry) | UNIT | Attempt1 → `order_error` or `rejected` (S1-UC-6). | Counts as one attempt; does NOT latch; the twin is unaffected (already fired once). |
| TC-8.6 | UC-8-EC1 (window rolls mid-retry, constraint 1 sibling reset) | UNIT | The window rolls between attempts (S1-UC-11); inspect the roll block (`main.rs` ≈ 1126-1130). | On roll, `attempt_window` AND `twin_window` reset (both siblings of the roll block); the new window gets a fresh twin (one) and a fresh retry budget; the old-window twin/live rows are unaffected. |
| TC-8.7 | UC-8-EC2 (twin fires on cooldown-blocked tick, constraint 1 decoupled + BEFORE place_live) | UNIT | A tick where the live attempt is cooldown-blocked by `retry_gate` but the window is not yet twin-latched and a fire is present. | The twin latch is **independent of `retry_gate`**; the twin fires on this tick (before/without a `place_live` attempt), pinning the rule "first tick not twin-latched + fire present, twin emitted **before** `place_live`". Either twin-before-attempt1 ordering is acceptable as long as the twin is exactly once per window (INV-D3). |

---

## UC-9 — Old /shadow_com payload without the live field → all rows shadow (no break)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-9.1 | UC-9 primary (FR-1, AC-1, NFR-1, constraint 2, merge blocker) | UNIT | `serde_json::from_str::<Vec<TrigSummary>>` on a `/shadow_com` array whose elements **lack** the `live` field. | Every element deserializes with `live == false` (`#[serde(default)]`); no parse error. |
| TC-9.2 | UC-9 primary (constraint 9) | UNIT | Feed the `live`-less (defaulted-false) vector to `compare()`. | All rows treated as `live=false` → `com_*` set, `lv_*` null for every window (shadow-only rendering, UC-2); the chart renders the green line, the LIVE line is absent. A **mixed** feed (some with `live`, some without) parses per-element — `live`-less rows are shadow, tagged rows split normally. |
| TC-9.3 | UC-9-A1 (single-object default, merge blocker) | UNIT | Deserialize a single `TrigSummary` JSON **object** with no `live` key. | `live == false` (the merge-blocking back-compat assertion, AC-1). |
| TC-9.4 | UC-9-A2 (old mislabelled live fills) | INTEGRATION | Replay/receive pre-feature live fills that were pushed before the producer set `live=true`. | They read as `live=false` → render on the **green shadow** line, not the LIVE line. Documented consequence: historical untagged live fills cannot be retro-split; only `live=true` rows populate the LIVE line (forward-looking from the first tagged fill). |
| TC-9.5 | UC-9-E1 (malformed element) | INTEGRATION | `POST /shadow_com` with a malformed element in the array; drive the handler (`dashboard.rs` ≈ 414-422). | `from_str::<Vec<TrigSummary>>` returns `Err`; the handler returns `400 Bad Request` with `parse: {e}` and does **not** overwrite `d.shadow_com` — Buffalo keeps the last good feed; no crash. |
| TC-9.6 | UC-9-EC1 (`live` present but wrong type, constraint 2 merge blocker) | UNIT | `POST /shadow_com` array where one element has `live` as a non-bool (`"true"` string or `1`). | Strict `bool` deserialization rejects the non-bool at the element level → the whole array parse fails → `400` (UC-9-E1) → the last good `d.shadow_com` is retained. A wrong-typed producer is caught, not silently mis-split. |

---

## UC-10 — Ledger replay across restart → reconstruct split + dual resolve (ResolveRecord.live back-compat)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-10.1 | UC-10 primary (FR-6, constraint 3) | UNIT | `replay_line_into` over a `LiveTriggerRecord` (`live:true`) and a legacy `TriggerRecord` (no `live` key), both passing the `is_dashboard_trigger` filter. | The `LiveTriggerRecord` → `TrigSummary.live == true`; the legacy `TriggerRecord` → `TrigSummary.live == false`; two distinct `dash.triggers` rows reconstructed for the window with correct flags. |
| TC-10.2 | UC-10 primary (FR-9, AC-8, constraint 3/4) | UNIT | The resolve branch reads `v["live"].as_bool()` from a `ResolveRecord` and passes it to `Dash::resolve`; run a `live:true` and a `live:false` resolve against equal-entry rows. | The `live:true` resolve attaches to the live row (`$5`-scale PnL); the `live:false` resolve attaches to the twin row (`$100`-scale PnL) — even when `eff == signal_entry`. |
| TC-10.3 | UC-10 primary (AC-8, OPS gate) | INTEGRATION | Full replay over a mixed JSONL fixture: for one window a `LiveTriggerRecord` (`live:true`) + a `TriggerRecord` (twin) + two `ResolveRecord`s (`live:true`/`live:false`), interleaved with legacy rows lacking `live`. | Reconstructs two distinct `dash.triggers` rows with correct flags; each resolve attaches to its own row, preserving the `$5`/`$100` split across restart; **zero parse errors** on the mixed pre-/post-feature file. `ResolveRecord.live` MUST ship in the **same binary** as the producer tagging (else this replay path is broken). |
| TC-10.4 | UC-10-A1 (only legacy rows) | UNIT | Replay a pre-feature ledger (no `live` anywhere). | All triggers replay as `live=false`; all resolves as `live=false`; the feed renders shadow-only (UC-2); no LIVE line — correct, there were no tagged live fills historically. |
| TC-10.5 | UC-10-A2 (LiveTriggerRecord `live:true` but resolve lacks `live`) | UNIT | Partial-upgrade ledger: new trigger telemetry, old resolve writer (no `live` key); `eff == signal_entry`. | `v["live"].as_bool()` → `None` → `false` → the resolve matches the **twin** row by entry; the live row stays unresolved and the twin gets both resolves (one overwriting). The exact degraded hazard FR-9 exists to prevent (the resolver MUST write `ResolveRecord.live` going forward). With **distinct** entries it still resolves correctly by entry. |
| TC-10.6 | UC-10-E1 (malformed ledger line) | INTEGRATION | A malformed/truncated line inside the replay stream. | `serde_json::from_str::<Value>` fails; `replay_line_into`'s caller `continue`s (S1-UC-12-E1); one bad line never aborts the replay. |
| TC-10.7 | UC-10-EC1 (`is_dashboard_trigger` outcome filter) | UNIT | Replay a `LiveTriggerRecord` with a non-fill `outcome` (`nofill`/`skip_*`/`order_error`/`rejected`). | Filtered out of `d.triggers` by the outcome gate (≈ 340-344); no-fill/skip rows do NOT become live `TrigSummary` rows on replay; only `filled`/`partial` (and legacy no-outcome) rows do. Keeps replayed `lv_*` counts equal to real fills. |
| TC-10.8 | UC-10-EC2 (twin `TriggerRecord` has no `outcome`) | UNIT | Replay a shadow `TriggerRecord` (no `outcome` field). | The missing `outcome` is treated as a dashboard trigger (legacy = filled, S1-UC-12-EC2); the twin replays as a `live=false` dashboard row. Correct. |

---

## UC-11 — EU restart mid-window → twin_window in-memory → twin double-emit (OPEN QUESTION)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-11.1 | UC-11 primary (constraint 1, resolution 1) | INTEGRATION | EU restarts **mid-window** on a window that has NOT filled (`fired_window` not boot-seeded); the twin already fired once pre-restart; the in-memory `twin_window` resets; the fire persists after restart. | `if !already` is true (window not latched) → `emit_trigger` fires a **second** twin for the same window → a duplicate `live=false` `TriggerRecord` + `Pending{live:false}` + `TrigSummary`. Accepted duplicate (PRD 3.10 resolution 1). |
| TC-11.2 | UC-11 primary (blast radius, constraint 9) | UNIT | Given two `live=false` twin rows for one window (the duplicate) plus one `live=true` row, run `compare()` + `body_json`. | Chart/table are **safe**: `compare()` dedupes by window (first `.find()` per flag) → only ONE `com_*` and one `lv_*` per window plotted/rendered. But `summary.com_positions` counts ALL `live=false` rows in-period → **inflates by one** for the duplicated window. The live `day_pnl`/`total_pnl` is untouched by the duplicate (twins are `live=false`). |
| TC-11.3 | UC-11-A1 (restart after window rolled) | UNIT | Restart lands after the window rolled to a fresh window. | The new window is legitimately fresh; its twin fires once (INV-D3); no duplicate — the duplicate only arises when the SAME window is still current after restart. |
| TC-11.4 | UC-11-A2 (restart before twin ever fired) | UNIT | Restart before the twin's first emission for the window. | The twin fires once after restart (its first emission); no duplicate. |
| TC-11.5 | UC-11-E1 (replay double-counts both twins) | INTEGRATION | A later full replay over a ledger holding both the pre- and post-restart `TriggerRecord`s for one window. | Reconstructs two `live=false` rows for the window; the same dedupe-by-window protection applies to the chart/table; `com_positions` inflation persists in the replayed view. Consistent with TC-11.1/11.2 runtime behavior. |
| TC-11.6 | UC-11-EC1 (live order re-placed after restart) | OPS | The live latch fails to protect an already-filled window after restart (out of scope for THIS feature). | Out of scope: it is Section 1's persisted-latch responsibility (NFR-4). This UC assumes NFR-4 holds and only the twin can double-emit; a live double-order would be an S1 defect, not a dashboard-line defect. |
| TC-11.7 | UC-11 (constraint 1 boot-seeded FILLED restart; constraint 9 once-per-fill) | UNIT | EU restarts on a window that **DID fill** pre-restart; boot seeds `fired_window` from `last_filled_window == current win_key` (S1) → `already == true`; the fire persists. | The live branch's `if !already` block is **skipped** → NO second live order AND **NO second twin** for the already-filled window (the twin is gated inside `if !already`). Consequently `day_pnl`/`total_pnl` advance **exactly once** per real fill across the restart; no duplicate twin for a filled window. |

---

## UC-12 — Buffalo restart mid-stream → recompute from the last pushed vectors

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-12.1 | UC-12 primary | INTEGRATION | Buffalo restarts (in-memory `shadow_com`/`paper_f1`/`paper` empty); serve `/stats` before any post-restart push, then apply one full EU push. | `/stats` before the first push → empty `compare[]` render (no crash, UC-2-E1); the EU push loop POSTs the **full** current `dash.triggers` vector, so one push fully repopulates `d.shadow_com` (`d.shadow_com = trigs`, ≈ 418); `compare()` re-splits by `live` and the LIVE line re-anchors from the repopulated pink series. Stateless render. |
| TC-12.2 | UC-12-A1 (paper feeds not yet re-pushed) | UNIT | After a Buffalo restart, `paper_f1` is empty but `shadow_com` has live rows; recompute the anchor. | The LIVE anchor defaults to `0` (no pink before the first live window, UC-14-EC1) until the `/paper_f1` cron re-pushes; the absolute baseline shifts, but the relative LIVE steps are unchanged; self-heals on the next `/paper_f1` push. |
| TC-12.3 | UC-12-E1 (EU push down during restart) | INTEGRATION | The EU push loop is down while Buffalo restarts. | Buffalo renders empty until the EU push resumes; no crash; empty is honest (no stale-but-wrong data); recovery is automatic when pushes resume. |
| TC-12.4 | UC-12-EC1 (request mid-push, concurrency) | INTEGRATION | A concurrent `/stats` GET arrives while `POST /shadow_com` is replacing the vector (holds `d.lock()`, ≈ 417-419). | The `/stats` request sees either the old or the new **full** vector (mutex-serialized), never a torn half-vector; consistent snapshot per request. |

---

## UC-13 — Resolve arrives for a window with no matching dash.triggers row → silent no-op

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-13.1 | UC-13 primary | UNIT | `Dash::resolve(ticker, side, entry, .., live)` where no `self.triggers` row matches the predicate. | The loop completes without hitting `break` (≈ 123-130); nothing is updated; **no panic, no error** — an unmatched resolve is a silent no-op. The `ResolveRecord` is still appended and, if `live`, `day_pnl`/`total_pnl` are still advanced (the missing dashboard row does not lose the PnL accounting). |
| TC-13.2 | UC-13-A1 (wrong live flag causes miss) | UNIT | Call `resolve` with the **wrong** `live` value against rows whose entries are (a) distinct and (b) equal. | (a) distinct entries → matches no row (the entry term still disambiguates); (b) equal entries → could match the wrong row. Demonstrates why FR-9 (always write `ResolveRecord.live`) is required to make the flag authoritative. |
| TC-13.3 | UC-13-A2 (resolver `retain` hardened, constraint 5) | UNIT | A pending vector holding a `live=true` pending and a `live=false` pending at **EQUAL** `(ticker, side, entry)`; settle the twin (`pd.live=false`) → apply the `retain` predicate `x.live == pd.live` (+ ticker/side/entry). | The `retain` removes **only** the twin pending; the live pending **survives** to settle on its own resolve. A twin resolve cannot retain-out the live pending. (UNIT if the retain predicate is extractable; else INTEGRATION over a simulated pending vec.) |
| TC-13.4 | UC-13-E1 (`get_market` error) | INTEGRATION | The settlement fetch (`get_market`) errors this cycle. | The resolver logs `warn!("resolve get_market ...")` (≈ 887) and leaves the pending for the next cycle; no `resolve` call this cycle; retries next tick. |
| TC-13.5 | UC-13-EC1 (two windows share tuple) | UNIT | Two different windows share `(ticker, side, entry, live)` (extremely unlikely); call `resolve`. | `resolve` updates the first unresolved match; window bounds are not part of the predicate; documented limitation inherited from pre-feature `resolve`. |

---

## UC-14 — LIVE line anchor — normal, sparse pink, no pink, 300-row scroll-out

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-14.1 | UC-14 primary (FR-14/15, AC-5, constraint 7, documented formula check) | UNIT | Pin the anchor recurrence against a constructed `compare[]`: rows in chronological order (`cmp.slice().reverse()`), first row with non-null `lv_pnl` = `idx0`. | `anchor = Σ (twin5 f1 step) over pink windows STRICTLY BEFORE idx0` (running `f1_twin` cumulative just prior); `LIVE[k] = anchor + Σ raw lv_pnl` over live windows `>= idx0`; no LIVE point exists before `idx0` (no leading zero tail); recomputed on every `/stats` refresh; the anchor is never `null`/`NaN`. |
| TC-14.2 | UC-14 primary (visual) | OPS | On the live dashboard, read the first plotted LIVE point vs the pink line. | The LIVE line's **first** plotted value equals the pink `f1_twin` cumulative at the window immediately **before** the first live-fill window; each later point adds raw `lv_pnl`; the vertical gap to pink is the accumulated execution divergence in raw `$5`. |
| TC-14.3 | UC-14-A1 (pink dense before first live) | UNIT | Pink present and dense in the windows before `idx0`. | The anchor is well-defined (the running pink cumulative just prior); the LIVE line starts on the pink line. The normal case. |
| TC-14.4 | UC-14-E1 (`lv_pnl` present, `lv_entry` missing) | UNIT | A live window with a non-null `lv_pnl` but a missing `lv_entry`; build the LIVE line. | The LIVE step uses raw `lv_pnl` (money) directly — the missing `lv_entry` does NOT break the step (unlike `twin5`, which needs entry). The LIVE line is entry-independent by design (INV-D4). |
| TC-14.5 | UC-14-EC1 (no pink at/before first live, constraint 7) | UNIT | No pink `f1_twin` value at or before the first live window. | The anchor "pink cumulative just prior" is undefined → it MUST default to **`0`** (the LIVE line starts at `0 + first raw lv_pnl`); the chart JS MUST NOT emit an undefined/`null`/`NaN` anchor (the precise resolution of the PRD 3.10 open question). |
| TC-14.6 | UC-14-EC2 (sparse pink) | UNIT | Pink series has gaps / some pink windows unresolved (`f1_pnl == null`) before `idx0`. | The anchor uses the **last available** running pink cumulative at or before the first live window (null pink windows add `0`, so the anchor is the last resolved pink cumulative); a sparse pink series does not produce a jumpy anchor. |
| TC-14.7 | UC-14-EC3 (first live window scrolls out of 300-cap, constraint 7 no NaN) | OPS/UNIT | The original first-live window ages beyond the 300 most-recent `compare` windows (`truncate(300)`, ≈ 300). | `idx0` shifts to the **earliest live window still visible**; the anchor recomputes to the pink cumulative just before THAT window; because both the pink and LIVE cumulatives are computed only over the visible 300 rows, they re-baseline **together** (relative LIVE-vs-pink gap preserved; absolute baseline drifts — acceptable/documented). The card raw `lv_pnl` sum remains the authoritative absolute figure. The chart MUST NOT error or produce a `NaN` anchor when the first live window is beyond row 300. |
| TC-14.8 | UC-14-EC4 (pink & live same first window, constraint 7 STRICTLY BEFORE) | UNIT | The first live window is also the earliest window overall (no window strictly before it), and a pink trade exists in that same window. | The anchor is `0` (no strictly-before window); the same-window pink step belongs to the concurrent step, NOT the anchor (anchor sums pink **strictly before** `idx0`, default `0`). Matches FR-15's "immediately before". |

---

## UC-15 — lv_pnl null (unresolved) → LIVE steps only on resolved; one live row/window; partial fill

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-15.1 | UC-15 primary (INV-D4, constraint 7) | UNIT | A `live=true` row with `result==None` (`lv_pnl == null`) among resolved live windows; build the LIVE line. | The LIVE-line `build()` accumulates raw `lv_pnl` **only where non-null** (analogous to the `com_twin` build gating on `com_pnl != null`, ≈ 651/656); an unresolved live window adds `0` → the LIVE line **holds flat** across it until it resolves, then **steps** on the next refresh. No premature/guessed PnL is plotted. |
| TC-15.2 | UC-15-A1 (partial fill) | UNIT | A partial-fill live row (`count = fill`, `entry = eff`); on resolve its raw `lv_pnl` scales to the partial count. | One live row, one LIVE step scaled to the partial count; the twin (`$100`, full-size) is unaffected (S1-UC-3). |
| TC-15.3 | UC-15-A2 (multiple live fills impossible) | UNIT | Attempt to create two `live=true` rows for one window (non-path under the S1 latch); call `compare()` + build. | At most one `live=true` row per window (`fired_window` latches on the first fill, S1-INV-2); even if a duplicate appeared, the first-`.find()` split (UC-4-A1) plots only one `lv_*`; the LIVE line steps at most once per window. |
| TC-15.4 | UC-15-E1 (real `0` vs null, constraint 7) | UNIT | Two live windows: one with `lv_pnl == 0.0` (real breakeven resolve) and one with `lv_pnl == null` (unresolved); build the LIVE line and compute `lv_positions`. | The build distinguishes `null` (**no step**) from a real `0.0` (**a step of `0`**); both look flat visually, but the real `0` counts toward `lv_positions`/win-rate and the `lv_pnl` sum, while `null` does NOT. Merge-relevant: `lv_pnl==0` counts as a resolved live position, `null` does not. |
| TC-15.5 | UC-15-EC1 (all live windows unresolved) | UNIT/OPS | A fresh live start: every `lv_pnl` is null. | The LIVE line is flat at the anchor value (no steps yet) but still **exists** (anchored at `idx0`) until the first live resolve. Correct depiction of "positions open, none settled". |
| TC-15.6 | UC-15-EC2 (`lv_positions` counts fills, not resolves, constraint 6/8) | UNIT | A mix of resolved and unresolved `live=true` rows in-period; call `body_json`. | `summary.lv_positions` = count of `live=true` rows in-period (`window_start >= started_iso`), resolved or not (fills); `summary.lv_pnl` = Σ only **resolved** raw `lv_pnl`. The cards MUST label these distinctly (fills count vs realized PnL) so an operator does not read an unresolved fill as a `$0` result. |

---

## UC-D — Deploy / cross-cutting regression (constraint 3 same-binary ship, constraint 9)

| # | UC Scenario | Type | Test Case | Expected Result |
|---|-------------|------|-----------|-----------------|
| TC-D.1 | Both boxes, single codebase (INV-D1, NFR-3) | OPS | Rebuild BOTH boxes from the **same** `dashboard.rs`: deploy the EU producer (`kalshi-shadow-com`) first, then the Buffalo consumer (`kalshi-shadow`). | The `TrigSummary` struct and the `compare()` split are shared, not forked per box; the EU box produces the `live` flag, Buffalo consumes the split. Version/build hashes match across boxes. |
| TC-D.2 | ResolveRecord.live same-binary gate (constraint 3) | OPS | Verify the EU binary that writes `ResolveRecord.live` (`main.rs` ≈ 846) is the **same** binary that tags `place_live`/`emit_trigger` `TrigSummary.live`. | Producer tagging and `ResolveRecord.live` ship together; a producer that tags `live=true` without a resolver that writes `ResolveRecord.live` would break restart-preserves-split at `eff == signal_entry` (TC-10.5). Go-live gate. |
| TC-D.3 | EU restart during a no-window gap (UC-11 mitigation) | OPS | Schedule the EU deploy/restart during a no-window gap (between 15-min windows), not mid-window. | Avoids the mid-window twin double-emit (UC-11); the restart lands before a fresh window opens, so the twin fires exactly once for the next window. |
| TC-D.4 | Order path byte-unchanged + additive wire (constraint 9) | OPS | Diff `place_live`'s order path (pricing/sizing/subaccount) before/after; verify `/paper`, `/paper_f1`, `/shadow_com` wire formats. | `place_live`'s order/pricing/sizing/subaccount is byte-unchanged (Section 1 & 2 rails intact); the only wire change is the additive `TrigSummary.live` field; no HTTP route added/removed; `day_pnl`/`total_pnl` advance exactly once per real fill even with a duplicate twin pending (TC-11.7). |
| TC-D.5 | Live dashboard behavioral verification | OPS | After both boxes are deployed, watch the live dashboard across a mix of fill and no-fill windows. | Green (`com_twin` shadow) keeps moving on no-fill windows while the LIVE line gaps flat (UC-6); the first plotted LIVE point equals the pink `f1_twin` previous cumulative (UC-14); no LIVE line before the first live fill; no `NaN`/undefined anchor. |

---

## Go-live / deploy checklist (must be green before enabling the split in prod)

| Gate | TC | Blocking? |
|------|----|-----------|
| `TrigSummary.live` absent-key → `false` (serde default) | TC-9.1, TC-9.3 | Yes (merge blocker) |
| `TrigSummary.live` wrong-type → array parse fails → `400` → keep last good feed | TC-9.6, TC-9.5 | Yes (merge blocker) |
| `Dash::resolve(.., live)` disambiguates equal entries, order-independent | TC-5.1, TC-5.6 | Yes (merge blocker) |
| Both `Dash::resolve` call sites (replay ~362 + resolver ~874) pass the flag | TC-5.2 | Yes |
| Resolver `retain` hardened with `x.live == pd.live` (twin cannot remove live pending) | TC-13.3 | Yes |
| `compare()` split exclusive (`com_*`←false, `lv_*`←true) + `match_lv` denominator | TC-4.1, TC-4.2 | Yes (merge blocker) |
| Twin emitted exactly once/window across ticks, retries, no-fills, skips | TC-6.5, TC-7.6, TC-8.1 | Yes (merge blocker) |
| Twin NOT emitted on boot-seeded FILLED-window restart; re-emitted (dup) on NO-FILL restart | TC-11.7, TC-11.1 | Yes |
| Mixed JSONL replay reconstructs the split + dual resolve (zero parse errors) | TC-10.3 | Yes |
| `ResolveRecord.live` ships in the SAME binary as the producer tagging | TC-D.2 | Yes |
| LIVE anchor = pink cumulative strictly before first live window, `0` if none, no `NaN` | TC-14.1, TC-14.5, TC-14.7 | Yes |
| LIVE line plots raw `$5` (no `twin5`); `lv_pnl` null → no step, real `0.0` → step | TC-15.1, TC-15.4 | Yes |
| Table: US column → LIVE column; cards: US → LIVE (explicit denominator copy) | TC-3.1, TC-3.2 | Yes |
| f6-era all-shadow payload renders unchanged (no regression) | TC-2.3 | Yes |
| `place_live` order path byte-unchanged; wire additive-only; PnL once/fill | TC-D.4 | Yes |
| Both boxes rebuilt from the same `dashboard.rs`; EU restart in a no-window gap | TC-D.1, TC-D.3 | Yes |
| Live dashboard: green moves on no-fill; first LIVE point == pink prev cumulative | TC-6.2, TC-14.2, TC-D.5 | Yes (post-first-fill) |

---

## Coverage matrix — every UC scenario → at least one test case

| Scenario | Mapped test case(s) |
|----------|---------------------|
| UC-1 (primary) | TC-1.1, TC-1.2, TC-1.3o |
| UC-1-A1 | TC-1.4 |
| UC-1-A2 | TC-1.5 |
| UC-1-A3 | TC-1.6 |
| UC-1-E1 | TC-1.7 |
| UC-1-E2 | TC-1.8 |
| UC-1-EC1 | TC-1.9 |
| UC-1-EC2 | TC-1.10 |
| UC-2 (primary) | TC-2.1, TC-2.2, TC-2.3 |
| UC-2-A1 | TC-2.4 |
| UC-2-A2 | TC-2.5 |
| UC-2-E1 | TC-2.6 |
| UC-2-EC1 | TC-2.7 |
| UC-3 (primary) | TC-3.1, TC-3.2, TC-3.3 |
| UC-3-A1 | TC-3.4 |
| UC-3-A2 | TC-3.5 |
| UC-3-E1 | TC-3.6 |
| UC-3-EC1 | TC-3.7 |
| UC-3-EC2 | TC-3.8 |
| UC-4 (primary) | TC-4.1, TC-4.2 |
| UC-4-A1 | TC-4.3 |
| UC-4-E1 | TC-4.4 |
| UC-4-EC1 | TC-4.5 |
| UC-5 (primary) | TC-5.1, TC-5.2 |
| UC-5-A1 | TC-5.3 |
| UC-5-A2 | TC-5.4 |
| UC-5-E1 | TC-5.5 |
| UC-5-EC1 | TC-5.6 |
| UC-5-EC2 | TC-5.7 |
| UC-6 (primary) | TC-6.1, TC-6.2 |
| UC-6-A1 | TC-6.3 |
| UC-6-A2 | TC-6.4 |
| UC-6-E1 | TC-6.5 |
| UC-6-EC1 | TC-6.6 |
| UC-6-EC2 | TC-6.7 |
| UC-7 (primary) | TC-7.1 |
| UC-7-A1 | TC-7.2 |
| UC-7-A2 | TC-7.3 |
| UC-7-A3 | TC-7.4 |
| UC-7-E1 | TC-7.5 |
| UC-7-EC1 | TC-7.6 |
| UC-8 (primary) | TC-8.1, TC-8.2 |
| UC-8-A1 | TC-8.3 |
| UC-8-A2 | TC-8.4 |
| UC-8-E1 | TC-8.5 |
| UC-8-EC1 | TC-8.6 |
| UC-8-EC2 | TC-8.7 |
| UC-9 (primary) | TC-9.1, TC-9.2 |
| UC-9-A1 | TC-9.3 |
| UC-9-A2 | TC-9.4 |
| UC-9-E1 | TC-9.5 |
| UC-9-EC1 | TC-9.6 |
| UC-10 (primary) | TC-10.1, TC-10.2, TC-10.3 |
| UC-10-A1 | TC-10.4 |
| UC-10-A2 | TC-10.5 |
| UC-10-E1 | TC-10.6 |
| UC-10-EC1 | TC-10.7 |
| UC-10-EC2 | TC-10.8 |
| UC-11 (primary) | TC-11.1, TC-11.2, TC-11.7 |
| UC-11-A1 | TC-11.3 |
| UC-11-A2 | TC-11.4 |
| UC-11-E1 | TC-11.5 |
| UC-11-EC1 | TC-11.6 |
| UC-12 (primary) | TC-12.1 |
| UC-12-A1 | TC-12.2 |
| UC-12-E1 | TC-12.3 |
| UC-12-EC1 | TC-12.4 |
| UC-13 (primary) | TC-13.1 |
| UC-13-A1 | TC-13.2 |
| UC-13-A2 | TC-13.3 |
| UC-13-E1 | TC-13.4 |
| UC-13-EC1 | TC-13.5 |
| UC-14 (primary) | TC-14.1, TC-14.2 |
| UC-14-A1 | TC-14.3 |
| UC-14-E1 | TC-14.4 |
| UC-14-EC1 | TC-14.5 |
| UC-14-EC2 | TC-14.6 |
| UC-14-EC3 | TC-14.7 |
| UC-14-EC4 | TC-14.8 |
| UC-15 (primary) | TC-15.1 |
| UC-15-A1 | TC-15.2 |
| UC-15-A2 | TC-15.3 |
| UC-15-E1 | TC-15.4 |
| UC-15-EC1 | TC-15.5 |
| UC-15-EC2 | TC-15.6 |

**All 85 UC scenarios (15 primary + 29 alternative + 16 error + 25 edge) map to at least one test case. No gaps.** (The use-case document's summary line rounds to "82"; every scenario body present in the text is mapped above.)

---

## Cross-cutting invariant coverage (INV-D1 … INV-D9)

| Invariant | Covered by |
|-----------|-----------|
| INV-D1 (one flag, one codebase) | TC-1.2, TC-9.1, TC-D.1 |
| INV-D2 (additive / back-compat, no new routes) | TC-9.1, TC-9.3, TC-9.6, TC-10.1, TC-10.4, TC-D.4 |
| INV-D3 (twin fires once/window, independent of live outcome) | TC-6.1, TC-6.5, TC-7.1, TC-7.6, TC-8.1, TC-8.2, TC-8.7, TC-11.7 |
| INV-D4 (LIVE = raw `$5`, anchored, no leading tail) | TC-14.1, TC-14.4, TC-14.5, TC-15.1, TC-15.4 |
| INV-D5 (green stays `twin5` shadow, no live fills) | TC-2.1, TC-4.1, TC-9.2, TC-11.2 |
| INV-D6 (resolve attaches to correct row by `live`) | TC-1.9, TC-5.1, TC-5.6, TC-5.7, TC-10.2, TC-13.3 |
| INV-D7 (US removed from UI, may remain in JSON) | TC-2.4, TC-3.1, TC-3.2 |
| INV-D8 (no new hot-path cost, no strategy change) | TC-8.2, TC-D.4 |
| INV-D9 (LIVE↔paper-F1 match denominator explicit) | TC-1.10, TC-3.2, TC-3.3, TC-4.2, TC-6.3 |

## Concurrency / restart-latch interplay

| Property | Covered by |
|----------|-----------|
| Twin emitted exactly once across retry attempts / cooldown ticks | TC-8.1, TC-8.7 |
| Twin never mutates `fired_window`/`attempt_count`/`last_attempt_ts` | TC-8.2 |
| Window roll resets `twin_window` (sibling of `attempt_window`) | TC-8.6 |
| Boot-seeded FILLED restart → no second live order AND no second twin | TC-11.7 |
| NO-FILL mid-window restart → twin re-emitted (dup accepted), chart/table safe | TC-11.1, TC-11.2, TC-11.5 |
| `day_pnl`/`total_pnl` advance once/real-fill even with duplicate twin pending | TC-11.2, TC-11.7, TC-D.4 |
| Resolver `retain` cannot remove the live pending on a twin resolve | TC-13.3 |
| Resolve order-independence (twin-first vs live-first) | TC-5.6 |
| Buffalo POST/GET serialization (no torn vector) | TC-12.4 |
| Full-vector push repopulates after Buffalo restart (stateless render) | TC-12.1 |
