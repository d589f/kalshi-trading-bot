# Use Cases: Dashboard — Separate LIVE F1 Line + LIVE Table/Cards

> Based on [PRD](../PRD.md) — section 3, internal id `dashboard-f1-live-line`

This is a **dashboard-only** feature (chart + per-window table + summary cards). It
splits the single mislabelled "shadow/LIVE" green series into **two** distinct
series — a **real-fill LIVE line** and a **strategy shadow twin line** — so
execution divergence (slippage, no-fills, fees) is visible while `f1_d50cap75`
trades LIVE. It cross-references Section 1 (`live-exec-telemetry-latch-fix`) for
`LiveTriggerRecord`, `Pending.live`, and the window latch, and Section 2
(`live-f1-strategy`) for the `/shadow_com` (green) and `/paper_f1` (pink) series.
This section **supersedes** the Section 2.5 AC-7 / 2.8 "no dashboard change
required" assumption — that held only while the green line was an honest shadow;
under `LIVE_TRADING=1` the green line silently became the live line and the
strategy shadow line vanished (PRD 3.1). Where a scenario is a pure reuse of a
Section 1 / Section 2 rail (latch, bounded retry, MIRROR gate, telemetry), this
document references the corresponding UC in
`live-exec-telemetry-latch-fix_use_cases.md` (prefix **S1-UC**) or
`live-f1-strategy_use_cases.md` (prefix **S2-UC**) rather than re-deriving it.

## The defect this feature fixes (the spine of this document)

In `signal_loop` (`main.rs` ≈ 1124-1175) the shadow path (`emit_trigger`, the
`else` branch ≈ 1167-1173) and the real-order path (`place_live`, ≈ 1133-1166) are
**mutually exclusive**. With `LIVE_TRADING=1`, only `place_live` runs, so the EU
bot's `dash.triggers` — and therefore `/shadow_com`, the green line, and the
`com_*` table columns — contains **only live fills**. The line labelled
"shadow/COM" silently became the **live** line, and there is **no strategy-level
shadow line at all**. Live fills also size at `STAKE=$5` while the historical
shadow (`emit_trigger`) sizes at `cfg.stake=$100` (`main.rs` ≈ 1197), so the two
were on different money scales; the earlier `twin5` normalization (commit
`c5a0a98`) papered over the scale but could not restore the missing shadow line.

## Topology (who produces, who consumes)

The `live` flag has exactly one **producer** (the EU box) and one **consumer** of
the split (the Buffalo box); both run the **same** `dashboard.rs` (single
codebase, INV-D1).

- **EU box** — GCP `34.32.177.126`, systemd `kalshi-shadow-com`, the real `$5`
  live F1 trader + the paper engine. Its **local** `dash.triggers` vector holds
  **both** the live fills (`place_live`, `live=true`) and the strategy shadow
  twins (`emit_trigger`, `live=false`) for the SAME windows. The EU resolver
  (`main.rs` ≈ 838-889) settles those rows in place, then the push loop POSTs the
  whole `dash.triggers` vector to Buffalo's `/shadow_com` (`main.rs` ≈ 657-693).
  The rows arrive at Buffalo **already resolved**.
- **Buffalo box** — `23.95.217.78:8890`, the read-only operator dashboard. It
  receives the EU feed into `d.shadow_com` (`POST /shadow_com`, `dashboard.rs`
  ≈ 410-423), splits it by the `live` flag in `compare()` (`dashboard.rs`
  ≈ 224-302) into `com_*` (twin) + `lv_*` (live), and renders the chart / table /
  cards. Buffalo's own low-volume binance.US shadow is `self.triggers` (`us_*`),
  which this feature **removes from the UI** (but may leave in `/stats`).

## Actors (internal Rust binary — no interactive UI, no new HTTP routes)

- **Operator** — watches the Buffalo dashboard; wants the real LIVE F1 fills on
  their own anchored line and the green line back to an honest shadow.
- **signal_loop (EU)** — the 0.3s decision loop (`main.rs` ≈ 1124-1175). Owns the
  `fired_window` latch, the `attempt_window`/`attempt_count`/`last_attempt_ts`
  retry state (reset on window roll ≈ 1126-1130), and — **new** — the
  `twin_window` latch (FR-3) that fires the honest shadow twin once per window
  **regardless** of the live outcome.
- **place_live (EU)** — the live IOC routine (Section 1). Pushes a `TrigSummary`
  with `entry = eff` (effective fill), `count = fill`, and — **new** —
  `live = true` (`main.rs` ≈ 1502-1515); pushes `Pending{ live: true }`
  (≈ 1516-1526). Order/pricing/sizing unchanged (Section 1 & 2 rails intact).
- **emit_trigger (EU + Buffalo)** — the shadow would-be order (`main.rs`
  ≈ 1180-1275). Writes a `TriggerRecord` (no `live` field → shadow on replay),
  pushes `Pending{ live: false }` (≈ 1250-1258) and a `TrigSummary` with
  `entry = fire.entry` (signal entry), `count = round($100/entry)` (≈ 1197), and
  — **new** — `live = false` (≈ 1261-1274). Its `$100` sizing is a **known
  artifact**, NOT changed (FR-4), and is `twin5`-normalized on the chart.
- **EU resolver** — the settlement task (`main.rs` ≈ 838-889). Appends a
  `ResolveRecord` (now with an additive `live` field from `pd.live`, FR-9,
  ledger.rs ≈ 86-98), updates live `day_pnl`/`total_pnl` only when `pd.live`
  (≈ 859-867), and calls `Dash::resolve(.., live)` (≈ 874-881) — **new**: passes
  `pd.live` so the correct row is updated.
- **replay_line_into (EU boot)** — ledger replay (`main.rs` ≈ 335-372). Its
  trigger push (≈ 345-360) sets `live` from the row (`LiveTriggerRecord` → `true`,
  legacy `TriggerRecord` → `false`, FR-6); its resolve branch (≈ 362-369) reads
  `v["live"].as_bool()` from `ResolveRecord` and passes it to `Dash::resolve`.
- **Dash::resolve** — the settlement matcher (`dashboard.rs` ≈ 120-131). **New**:
  gains a `live: bool` parameter and adds `t.live == live` to its predicate
  (currently `ticker` + `side` + `entry` within `1e-9` + `result.is_none()`).
- **compare() (Buffalo consumer)** — `dashboard.rs` ≈ 224-302. **New**: sources
  `com_*` only from `live=false` `shadow_com` rows and adds `lv_*` sourced only
  from `live=true` rows; adds `match_lv` (live side vs `f1_side`). Dedupes by
  window (first `.find()` match per source). `out.truncate(300)` (≈ 300).
- **body_json (Buffalo)** — `dashboard.rs` ≈ 304-340. **New**: adds
  `summary.lv_match`/`lv_total`/`lv_pct`/`lv_positions`/`lv_pnl`.
- **Chart JS (Buffalo HTML/JS)** — `dashboard.rs` ≈ 511-706. Owns `twin5`
  (≈ 563), the per-line `build()` cumulative (≈ 656), the series (≈ 576),
  `renderTip` (≈ 624-640), the chart header (≈ 538), `chartlbl` (≈ 666), the
  table (≈ 545-549 header, 698-702 rows), and the cards (≈ 679-691).

## Shared preconditions (apply unless a use case overrides them)

- Both boxes run the **same** rebuilt `dashboard.rs` (INV-D1); the `TrigSummary`
  struct and the `compare()` split are shared, never forked per box (NFR-3).
- The EU box is `LIVE_TRADING=1`, `SESSION=f1_d50cap75`, `STAKE=5`,
  `SUBACCOUNT=1` — i.e. Section 2 F1 live mode (S2-UC-1). Buffalo is shadow-only.
- `/shadow_com`, `/paper_f1`, `/paper` transports are unchanged; the only wire
  change is the additive `TrigSummary.live` field (NFR-1, FR-1).
- Section 1 & 2 rails are intact and unchanged: latch-on-confirmed-fill + bounded
  retry (S1-UC-7/8/9), telemetry (S1-UC-1..6), MIRROR gate + subaccount isolation
  (S2-UC-3/4/5). This feature adds **no** strategy, pricing, sizing, or order-flow
  change (PRD 3.9).
- `wkey(s)` truncates a window-start ISO string to its first 16 chars
  (`"YYYY-MM-DDTHH:MM"`, `dashboard.rs` ≈ 116-118); "per window" always means
  "per `wkey`".

## Invariants asserted across use cases (verify in every relevant postcondition)

- **INV-D1 (one flag, one codebase).** A single `live: bool` on `TrigSummary`
  splits the feed; the EU box tags real fills `true` and shadow twins `false`; the
  same `dash.triggers` vector carries both; the Buffalo `compare()` splits by the
  flag. Both boxes share the struct + split verbatim (FR-1, NFR-3).
- **INV-D2 (additive / back-compat, no new routes).** `TrigSummary.live` is
  `#[serde(default)]` → absent reads as `false` (shadow). `ResolveRecord.live` is
  additive; replay reads `v["live"].as_bool()` → absent reads as `false`. No
  `TrigSummary` field is renamed/removed; no HTTP route is added/removed
  (FR-1/FR-9, NFR-1, PRD 3.6).
- **INV-D3 (twin fires once per window, independent of the live outcome).** The
  `twin_window` latch is a sibling of `fired_window`/`attempt_window`, reset on
  window roll, and does **not** depend on `place_live`'s fill / no-fill / skip /
  retry outcome. The shadow line therefore exists even when the live order
  no-filled or was capped (FR-3, NFR-4).
- **INV-D4 (LIVE line = raw `$5`, anchored, no leading tail).** The LIVE line plots
  `anchor + cumulative RAW lv_pnl` (actual `$5` money), **never** `twin5`-repriced
  (NFR-5). Its anchor equals the pink `f1_twin` **cumulative** value at the window
  immediately before the first live-fill window (default `0` if no pink value
  before it). It is plotted **only** from the first live-fill window onward — no
  leading zero tail (FR-14/15, AC-5, PRD 3.10).
- **INV-D5 (green stays the `twin5` shadow, contains no live fills).** The green
  `com_twin` line sums `live=false` `shadow_com` rows only, `twin5`-normalized; its
  window count equals the `live=false` row count and excludes every `live=true`
  fill (FR-13, AC-4).
- **INV-D6 (resolve attaches to the correct row by the `live` flag).**
  `Dash::resolve(.., live)` updates only the row whose `t.live == live` (plus
  ticker/side/entry/`result.is_none()`), so a twin resolve updates the twin row
  (`$100`-scale PnL) and a live resolve updates the live row (`$5`-scale PnL) —
  **even when** `eff == signal_entry` (FR-7/8/9, AC-2/AC-8).
- **INV-D7 (US removed from UI, may remain in JSON).** The per-window table's
  middle "US (binance.us)" column and the two "US ↔ paper" cards are replaced by
  LIVE column / LIVE cards; `self.triggers` (Buffalo binance.US) is not rendered
  but MAY remain in the `/stats` payload (FR-17/18, AC-6/7, PRD 3.9).
- **INV-D8 (no new hot-path cost, no strategy change).** The producer change is one
  extra `emit_trigger` call per window (already the shadow-mode cost) plus a
  boolean; the consumer change is pure in-memory partitioning of an
  already-received vector. No new network I/O on the 0.3s loop (NFR-6).
- **INV-D9 (LIVE↔paper-F1 match denominator is explicit).** `match_lv` /
  `lv_match` / `lv_total` are defined **only** for windows where **both** a live
  fill and a paper-F1 trade exist; no-fill windows and paper-F1-absent windows are
  excluded from the denominator (FR-11, PRD 3.10).

---

## UC-1: End-to-end live F1 window → twin + live rows → compare split → chart (green shadow + anchored LIVE) → dual resolve

**Actor**: signal_loop (EU) → place_live + emit_trigger → EU resolver → `/shadow_com`
push → Buffalo compare() → chart JS
**Preconditions**: EU F1 live mode (shared preconditions); a fresh F1 `MirrorSnap`
(S2-UC-3); the F1 gate fires this window; `create_ioc` fills; paper-F1 (`/paper_f1`,
pink) has resolved trades in windows before this one.
**Trigger**: a tick where the F1 gate produces `res.fire` and the window is not yet
latched.

### Primary Flow (Happy Path)
1. signal_loop resets per-window retry state on window roll (`main.rs` ≈ 1126-1130)
   and takes the **live branch** (`lcfg.enabled && order_client.is_some()`,
   ≈ 1133).
2. **Live order** — the `retry_gate` passes; signal_loop calls `place_live(...)`
   (≈ 1142-1159). `create_ioc` returns HTTP 201, `fill > 0`, `remaining_count == 0`
   → `outcome = filled` (S1-UC-1). `place_live` appends one `LiveTriggerRecord`
   (`live` = `true`, session `f1_d50cap75`), pushes a `TrigSummary` with
   `entry = eff`, `count = fill`, and **`live = true`** (≈ 1502-1515), and pushes
   `Pending{ live: true }` (≈ 1516-1526). signal_loop consumes `fired_window`
   (≈ 1163-1164).
3. **Shadow twin** — in the SAME live branch, signal_loop **additionally** calls
   `emit_trigger` exactly once for this window, gated by the dedicated
   `twin_window` latch (FR-3, INV-D3). `emit_trigger` writes a `TriggerRecord` (no
   `live` field → shadow), pushes `Pending{ live: false }`, and pushes a
   `TrigSummary` with `entry = fire.entry` (the signal entry), `count =
   round($100/entry)`, and **`live = false`** (≈ 1261-1274). The twin latch is
   independent of the live fill/no-fill/skip/retry outcome.
4. Now `dash.triggers` holds **two** rows for this window: a `live=true` fill row
   (`entry = eff`) and a `live=false` twin row (`entry = signal_entry`).
5. **Dual resolve (EU)** — when the window settles, the resolver settles **both**
   pendings. For each: it appends a `ResolveRecord` carrying `live = pd.live`
   (FR-9) and calls `Dash::resolve(ticker, side, entry, result, won, pnl, pd.live)`
   (FR-8). The `pd.live=true` resolve updates the live row (`$5`-scale PnL, and
   `day_pnl`/`total_pnl` are advanced, ≈ 859-867); the `pd.live=false` resolve
   updates the twin row (`$100`-scale PnL) (INV-D6).
6. **Push** — the EU push loop POSTs the resolved `dash.triggers` to Buffalo's
   `/shadow_com`; Buffalo stores it in `d.shadow_com` (each element now carries
   `live`).
7. **compare() split (Buffalo)** — for this window, `compare()` finds the
   `live=false` twin (→ `com_*`: `com_side`/`com_entry`/`com_delta`/`com_pnl`/
   `com_result`/`com_won`/`com_count`/`com_p`/`com_ticker`/`com_ts`) and the
   `live=true` fill (→ new `lv_*`: `lv_side`/`lv_entry`/`lv_delta`/`lv_pnl`/
   `lv_result`/`lv_won`/`lv_count`/`lv_p`) (FR-10). It also sets `match_lv` = live
   side vs `f1_side` (FR-11, INV-D9).
8. **Chart (Buffalo)** — the **green** `com_twin` line steps by the `twin5`-normalized
   twin PnL (FR-13, INV-D5). The **new LIVE line** (`#ff7b72`) is plotted from this
   window onward: its first point = the pink `f1_twin` cumulative at the window
   immediately before this one (the anchor), and it steps by **raw** `lv_pnl` (no
   `twin5`) thereafter (FR-14/15, INV-D4). Orange `pa_twin` and pink `f1_twin`
   are unchanged.
9. **Table + cards** — the per-window row shows the LIVE column (`lv_*`) and the
   retained SHADOW column (`com_*`); the LIVE cards show `lv_pct`/`lv_positions`/
   raw `lv_pnl` (UC-3).

**Postconditions** (AC-3/AC-5/AC-8): exactly one `live=true` row and one
`live=false` twin row exist for the window; `com_*` is populated only from the
twin, `lv_*` only from the fill (INV-D5); the green shadow line and the anchored
raw-`$5` LIVE line are both drawn; each resolve attached to its own row (INV-D6);
Section 1 & 2 rails are intact (INV-D8). The operator can read execution divergence
(slippage/fees) as the vertical gap between the anchored LIVE line and the pink
paper-F1 line.

### Alternative Flows
- **UC-1-A1: NO-side fill** — `fire.side = No`; `lv_side`/`com_side` render `NO`;
  the per-side entry formulas are unchanged (S1-UC-1-A1). Split logic identical.
- **UC-1-A2: fill after a re-quote** — `requote == true`; still one live row with
  `entry = eff` (S1-UC-4). The twin still fires once (INV-D3). F1's 180s entry
  makes re-quotes more likely (PRD 2.10) — visible as a wider LIVE-vs-pink gap.
- **UC-1-A3: partial fill** — `fill > 0` and `remaining_count > 0` → `outcome =
  partial`, latches; the live `TrigSummary` uses `count = fill` (the partial count)
  and `entry = eff`; its `Pending` settles to the partial size (S1-UC-3). One live
  row; twin unaffected (see UC-15).

### Error Flows
- **UC-1-E1: live ledger/serialize append fails** — S1-UC-13; the loop never
  panics; the twin still emits and the latch decision still follows the returned
  outcome. The live `TrigSummary`/`Pending` push is independent of the ledger
  append.
- **UC-1-E2: `/shadow_com` POST fails / times out** — the EU push loop's HTTP error
  is non-fatal; Buffalo keeps rendering the **last** received `d.shadow_com` until
  the next successful push (UC-12). No crash on either box.

### Edge Cases
- **UC-1-EC1: twin and live entries EQUAL (`eff == signal_entry`, zero slippage)** —
  both rows share `(ticker, side, entry)`; the split still works because `compare()`
  partitions by the `live` flag (not by entry) and resolve disambiguates by
  `t.live` (UC-5, AC-2). The chart shows the LIVE line landing exactly on the
  shadow's twin5 value only if scales coincide — but they generally differ
  (`$5` raw vs `$100` twin5), so the two lines are still distinct.
- **UC-1-EC2: live fill but paper-F1 did not trade this window** — `lv_*` present,
  `f1_*` null → `match_lv` is null (excluded from the `lv_total` denominator,
  INV-D9). The LIVE line still steps by raw `lv_pnl`; the anchor uses the last
  pink value at/before this window (UC-14).

### Data Requirements
- **Input**: F1 `fire`, `OrderResp`, window bounds, `book.ticker`, the pink
  `f1_twin` cumulative series, `pd.live` on each `Pending`.
- **Output**: one `LiveTriggerRecord` (`live:true`) + one `TriggerRecord` (shadow);
  two `TrigSummary` rows (`live` true/false) in `dash.triggers`; two
  `ResolveRecord` rows (each with `live`); `compare[].com_*` + `compare[].lv_*` +
  `match_lv`; the green + LIVE chart series.
- **Side Effects**: one real `$5` IOC on subaccount #1; `trades_today += 1` once;
  live `day_pnl`/`total_pnl` advanced by the live resolve only.

---

## UC-2: Shadow-only / pre-live mode → only twins, no LIVE line/column/cards (green works as today)

**Actor**: signal_loop (EU non-live else branch OR Buffalo) → emit_trigger →
compare() → chart
**Preconditions**: `LIVE_TRADING=0` (or no `OrderClient`), so the loop takes the
non-live `else` branch (`main.rs` ≈ 1167-1173) — this is the Buffalo box, and any
EU box before live is enabled. The `shadow_com` feed contains **only** `live=false`
rows.
**Trigger**: a tick where the gate fires in shadow (log-only) mode.

### Primary Flow (Happy Path)
1. The non-live `else` branch keeps the `fired_window` latch and calls
   `emit_trigger` (≈ 1168-1172), whose `TrigSummary` now carries `live = false`
   (FR-4/FR-5). No `place_live`, no live row.
2. `compare()` finds only `live=false` rows per window → `com_*` populated, `lv_*`
   **null** (FR-10). `match_lv` null for every window.
3. `body_json` summary: `lv_match`/`lv_total`/`lv_positions` = `0`, `lv_pct` = `0`,
   `lv_pnl` = `0` (no `live=true` rows in-period).
4. Chart: the green `com_twin` line renders exactly as in the pre-feature shadow
   view (twin5-normalized); orange `pa_twin` and pink `f1_twin` unchanged. The
   **LIVE line is absent** (no first live window → no anchor, no points, no leading
   tail) (NFR-2, AC-9).
5. Table: the LIVE column renders `—` for every row (`lv_*` null); the SHADOW
   (`com_*`) column renders as before. Cards: the LIVE cards show `0% · 0w · $0`.

**Postconditions** (NFR-2, AC-9): the dashboard renders equivalently to the
pre-feature shadow view — green shadow line + shadow cards behave as before, and
the LIVE line / LIVE column values / LIVE cards are simply empty. **No parse
errors** on an all-shadow feed.

### Alternative Flows
- **UC-2-A1: Buffalo's own binance.US shadow present** — `self.triggers` (`us_*`)
  still exists in `/stats`, but is **not** rendered in the table/cards (INV-D7,
  FR-17). The chart's blue `us` series is not fed (`build('us')` is not called;
  ≈ 664 sets only paper/com/f1), consistent with the pre-feature state.
- **UC-2-A2: mixed feed with a few late live rows** — as soon as one `live=true`
  row appears in-period, UC-1's LIVE line/column/cards begin; before that, this
  UC's empty-LIVE rendering holds. The transition is the first live window (UC-14).

### Error Flows
- **UC-2-E1: empty `shadow_com` feed** — `compare()` yields no `com_*`/`lv_*`;
  the green and LIVE lines are both empty; only pink/orange (if paper feeds exist)
  render. No crash, no undefined anchor (UC-14-EC1).

### Edge Cases
- **UC-2-EC1: `LIVE_TRADING` toggled off mid-session** — new windows produce only
  twins; historical `live=true` rows still in the feed keep their `lv_*`. The LIVE
  line stops extending (no new live windows) but retains its drawn history. Not a
  regression — the split is per-row, not per-session.

### Data Requirements
- **Input**: an all-`live=false` `shadow_com` feed (or empty).
- **Output**: `com_*` populated, `lv_*` null, `lv_*` summary = 0.
- **Side Effects**: none; read-only rendering.

---

## UC-3: Table US column → LIVE column, US cards → LIVE cards (rendering)

**Actor**: Chart JS table/cards render (Buffalo)
**Preconditions**: a `compare()` output with at least one window carrying `lv_*`
(and possibly `com_*` and `f1_*`).
**Trigger**: `load()` fetches `/stats` and re-renders the table + cards (2s auto).

### Primary Flow (Happy Path)
1. **Table header** (`dashboard.rs` ≈ 545-549): the middle **US (binance.us)**
   column group (`US`/`entry`/`Δ`/`=pa`, backed by `us_side`/`us_entry`/`us_delta`/
   `match_us`) is **removed**; a **LIVE** column group is rendered in its slot,
   backed by `lv_*` (side / entry / Δ / `=pa` where `=pa` renders `match_lv` vs
   paper-F1). The retained shadow column (`com_*`) is relabelled to name the
   **strategy shadow twin** (not "LIVE") (FR-17, AC-6).
2. **Table rows** (≈ 698-702): each row renders `pa_*` (paper), then the LIVE
   column from `lv_*` (`lv_side` colored, `lv_entry`, `lv_delta`, `mk(match_lv)`),
   then the SHADOW column from `com_*`. `us_*` / `match_us` are **absent** from the
   rendered rows.
3. **Cards** (≈ 686-691): the "US ↔ paper side-match" and "US: matched / windows"
   cards are **removed** and replaced by **LIVE↔paper-F1** cards: side-match %
   (`lv_pct`), fills count (`lv_positions`), and (in the realpnl grid ≈ 683-685)
   the "LIVE (period)" card sourced from **raw** `lv_pnl` (real `$5`), not `com_*`
   `twin5` (FR-18, AC-7). The shadow (`com_*`) side-match cards are **retained**,
   relabelled **SHADOW**.
4. The card copy makes the `match_lv` denominator explicit ("of windows where both
   live and paper-F1 traded") so the percentage is not misread as coverage (INV-D9,
   PRD 3.10).

**Postconditions** (AC-6/AC-7): the rendered table shows LIVE where US used to be;
`us_*`/`match_us` do not appear in the rows; the LIVE cards report real-`$5`
metrics; the SHADOW cards remain. `self.triggers` may still appear in `/stats`
(INV-D7).

### Alternative Flows
- **UC-3-A1: window has `com_*` but no `lv_*`** (twin only, live no-fill/skip) —
  the LIVE column renders `—` for that row; the SHADOW column renders the twin.
  This is the key observability row (UC-6): visible "paper/shadow traded, LIVE did
  not".
- **UC-3-A2: window has `lv_*` but no `com_*`** — should not occur under
  steady-state (the twin always fires per INV-D3), but if it does (e.g. a
  legacy/mixed feed), the LIVE column renders and the SHADOW column renders `—`.
  Must not break the row.

### Error Flows
- **UC-3-E1: `lv_*` fields absent from an old `/stats` payload** — a Buffalo build
  ahead of a stale EU feed: the JS reads `c.lv_side` etc. as `undefined` and
  renders `—` (same as null). No JS exception; the table still renders (UC-9).

### Edge Cases
- **UC-3-EC1: table is capped to recent rows** — the table slices `compare` to the
  most recent rows (`d.compare.slice(0,60)`, ≈ 698) while the chart uses the full
  (300-cap) history; LIVE rows older than the table slice are on the chart but not
  the table. Documented; not a bug.
- **UC-3-EC2: `match_lv` null renders neutral** — `mk(null)` renders the neutral
  dot `·` (≈ 557), distinct from `✓`/`✗`. A no-paper-F1 or no-live window shows `·`,
  not a false `✗`.

### Data Requirements
- **Input**: `compare[].lv_*`, `compare[].com_*`, `compare[].match_lv`,
  `summary.lv_*`.
- **Output**: rendered table LIVE column + LIVE/SHADOW cards.
- **Side Effects**: none; DOM render only.

---

## UC-4: compare() split is exclusive (com_* from live=false, lv_* from live=true)

**Actor**: compare() (Buffalo)
**Preconditions**: `d.shadow_com` contains, across windows, a mix of `live=true`
and `live=false` rows.
**Trigger**: `/stats` → `body_json` → `compare()`.

### Primary Flow (Happy Path)
1. For each window key, `compare()` performs **two** independent `.find()`s over
   `self.shadow_com`: the first `live=false` match → `com_*`, the first `live=true`
   match → `lv_*` (FR-10). The existing single `c = shadow_com.find(...)` (≈ 254)
   is replaced by this two-way split.
2. A window with **both** yields both `com_*` (twin) and `lv_*` (live). A window
   with only a `live=false` row yields `lv_*` **null**; a window with only a
   `live=true` row yields `com_*` **null** (AC-3).
3. `match_lv` (FR-11) is computed only when both `lv_*` and `f1_side` exist,
   analogous to the existing `match_com` (≈ 260-263).

**Postconditions** (AC-3): `lv_*` is populated **exclusively** from `live=true`
rows and `com_*` **exclusively** from `live=false` rows; the two are never sourced
from the same row. The green line (INV-D5) and the LIVE line (INV-D4) therefore
never share a data point.

### Alternative Flows
- **UC-4-A1: multiple rows of the same flag in one window** — `compare()` takes the
  **first** `.find()` match per flag. Under steady-state there is at most one live
  row (latch, S1-INV-2) and one twin (INV-D3) per window, so this is a no-op; on a
  duplicate (UC-10/UC-11) the first is used and later duplicates are ignored by the
  split (dedupe-by-window-takes-first).

### Error Flows
- **UC-4-E1: a `live=true` row with `result==None` (unresolved)** — `lv_pnl` is
  null; the split still populates `lv_side`/`lv_entry`/`lv_delta`, and the chart
  step is deferred until resolved (UC-15). No error.

### Edge Cases
- **UC-4-EC1: `wkey` collision across different tickers in one minute** — two
  windows sharing the first 16 chars of `window_start` map to one `wkey`;
  `compare()` already keys by `wkey` (pre-feature behavior). The split inherits
  this; the first match per flag wins. Unchanged from pre-feature grouping.

### Data Requirements
- **Input**: `self.shadow_com` (mixed `live` rows), `self.paper_f1` (for
  `f1_side`).
- **Output**: per-window `com_*` + `lv_*` + `match_lv`.
- **Side Effects**: none; in-memory partition (INV-D8).

---

## UC-5: Resolve disambiguation by the live flag (equal entries AND different entries)

**Actor**: EU resolver → Dash::resolve
**Preconditions**: `dash.triggers` holds, for one window, a `live=true` fill row
(`entry = eff`) and a `live=false` twin row (`entry = signal_entry`); both have
`result == None`; both pendings are due for settlement.
**Trigger**: the resolver settles the two pendings (S1 resolver task, ≈ 838-889).

### Primary Flow (Happy Path)
1. `Dash::resolve` gains a `live: bool` parameter and adds `t.live == live` to its
   match predicate (`ticker` + `side` + `entry` within `1e-9` + `result.is_none()`
   + `t.live == live`) (FR-7).
2. The twin `Pending{ live:false }` settles → the resolver calls
   `Dash::resolve(.., live=false)` → it updates **only** the `live=false` twin row
   (attaching the `$100`-scale PnL), leaving the live row untouched.
3. The live `Pending{ live:true }` settles → `Dash::resolve(.., live=true)` updates
   **only** the `live=true` row (attaching the `$5`-scale PnL).
4. The runtime resolver passes `pd.live` (already on `Pending`, ≈ 383) into
   `Dash::resolve` (FR-8, ≈ 874-881) and writes it into `ResolveRecord.live`
   (FR-9, ≈ 846-858).

**Postconditions** (AC-2, INV-D6): with **equal** entries (`eff == signal_entry`,
zero slippage), the twin resolve updates only the twin row and the live resolve
updates only the live row — proven by a unit test using equal `entry` values. Each
row carries the correct scale of PnL.

### Alternative Flows
- **UC-5-A1: different entries (`eff != signal_entry`, non-zero slippage)** — the
  pre-feature `entry`-within-`1e-9` predicate already disambiguates; the added
  `t.live == live` is redundant but harmless (still correct). This is the common
  case (slippage > 0).
- **UC-5-A2: only the live row exists (twin no-fill impossible, but live-only feed)**
  — `Dash::resolve(.., live=true)` matches the live row; a `live=false` resolve for
  the same window finds no row and is a no-op (UC-13).

### Error Flows
- **UC-5-E1: two `live=true` rows share `(ticker, side, entry)`** — should not
  occur (one fill per window, S1-INV-2), but if it does, `resolve` updates the
  **first** unresolved match and `break`s (≈ 128); the second stays unresolved.
  Documented; the latch makes this a non-path under steady-state.

### Edge Cases
- **UC-5-EC1: resolve order (twin-first vs live-first)** — because the predicate
  includes `t.live == live`, the settlement order of the two pendings does not
  matter; each resolve targets its own flag. Order-independence is the correctness
  point.
- **UC-5-EC2: `entry` differs by less than `1e-9` across flags** — the flag still
  disambiguates even if the entries are numerically within `1e-9` (equal case);
  the `t.live == live` term is the deciding factor there (AC-2).

### Data Requirements
- **Input**: two `Pending`s (`live` true/false), the settled market result.
- **Output**: two updated `TrigSummary` rows (correct PnL scale each); two
  `ResolveRecord` rows (each with `live`).
- **Side Effects**: `day_pnl`/`total_pnl` advanced only by the `live=true` resolve
  (≈ 859-867).

---

## UC-6: Live no-fill window → twin emitted, no live row → green steps, LIVE line flat gap (KEY OBSERVABILITY)

**Actor**: signal_loop (live branch) → place_live (no-fill) + emit_trigger (twin)
**Preconditions**: EU F1 live mode; the F1 gate fires; `place_live` returns
`nofill` (S1-UC-2) on all bounded attempts (S1-UC-8); the window does not fill.
**Trigger**: a live fire whose order does not fill within the window.

### Primary Flow (Happy Path)
1. signal_loop takes the live branch; `place_live` no-fills (no `TrigSummary` with
   `live=true` is pushed, S1-UC-2 pushes no dashboard row on a no-fill) and does
   **not** latch `fired_window` (S1-INV-2). Bounded retry may run (UC-8), still no
   fill.
2. **The twin still fires** — the `twin_window` latch fires `emit_trigger` exactly
   once for this window **regardless** of the no-fill (FR-3, INV-D3). A `live=false`
   twin `TrigSummary` (`entry = signal_entry`) is pushed and later resolved.
3. `compare()` for this window: `com_*` populated (twin), `lv_*` **null** (no live
   fill).
4. **Chart**: the green `com_twin` line **steps** on this window (the twin exists
   and resolves); the **LIVE line stays flat** across this window (no `lv_pnl` to
   add) — a visible horizontal gap where the real fills missed while the strategy
   would have traded (FR-14, the observability the operator explicitly wants).
5. **Table**: the row shows the SHADOW column populated and the LIVE column `—`
   (UC-3-A1).

**Postconditions**: the shadow line and paper-F1 line advance while the LIVE line
holds flat, making the **no-fill coverage gap visible** as horizontal divergence.
No live row exists for the window; `trades_today` unchanged; `day_pnl` unchanged.
This is the core value of the feature (PRD 3.2).

### Alternative Flows
- **UC-6-A1: paper-F1 also traded this window** — pink `f1_twin` steps, green
  `com_twin` steps, LIVE flat → the operator sees exactly how much real execution
  fell behind the signal on this window. `match_lv` is null (no `lv_*`), so the
  no-fill does **not** count against the LIVE↔paper match % (INV-D9).
- **UC-6-A2: paper-F1 did not trade** — only the green twin steps; pink and LIVE
  both flat. Still consistent.

### Error Flows
- **UC-6-E1: twin latch mis-wired to depend on the live outcome** — a regression
  where the twin only fires on a live fill would make the shadow line vanish on
  no-fill windows, re-introducing the defect. The E2E test MUST assert the twin
  fires on a no-fill window (INV-D3). This is the merge-blocking guard.

### Edge Cases
- **UC-6-EC1: many no-fill windows in a row** — the LIVE line is flat across all of
  them while green/pink climb; the accumulated horizontal gap equals the total
  missed coverage. Expected, not a bug.
- **UC-6-EC2: no-fill on the FIRST live-intended window** — if the very first
  window the bot intends to trade no-fills, there is still **no `lv_*`** (no fill),
  so the LIVE line has not started yet; the "first live-fill window" (UC-14) is the
  first window that actually **fills**, not the first that fires.

### Data Requirements
- **Input**: an F1 fire, a `nofill` outcome, the twin `emit_trigger`.
- **Output**: one `live=false` twin row + resolve; **no** `live=true` row; `lv_*`
  null for the window.
- **Side Effects**: up to `N` `create_ioc` calls (no fill); one twin Pending; no
  `trades_today`/`day_pnl` change.

---

## UC-7: Live skip (daily-cap / loss-stop / band) → twin STILL emitted, no live row

**Actor**: signal_loop (live branch) → place_live (skip) + emit_trigger (twin)
**Preconditions**: EU F1 live mode; the F1 gate fires; `place_live` short-circuits
on a pre-order gate — `skip_daily_cap`, `skip_loss_stop`, or `skip_band` (S1-UC-5,
S1-UC-6, S2-UC-7).
**Trigger**: a live fire that a safety gate rejects before an order is placed.

### Primary Flow (Happy Path)
1. `place_live` writes one skip `LiveTriggerRecord` (`live=true`, the skip outcome)
   and returns without an order or a dashboard `TrigSummary` push (S1-UC-5/6). No
   `lv_*` for the window; `fired_window` not latched (skips never latch).
2. **The twin still fires** once via the `twin_window` latch (FR-3, INV-D3),
   independent of the skip. `com_*` populated, `lv_*` null.
3. Chart/table: identical shape to UC-6 — green steps, LIVE flat, LIVE column `—`.
   The difference from UC-6 is the **cause** (a deliberate safety skip, not a
   market no-fill); both leave the LIVE line flat, which is the correct depiction
   (the bot did not take a real position).

**Postconditions**: the shadow line records what the strategy would have done even
on skipped windows; the LIVE line correctly shows no step (no real position). The
divergence between green (would-be) and LIVE (real) now includes deliberate skips,
not just no-fills — both are legitimate "no real fill" causes.

### Alternative Flows
- **UC-7-A1: `skip_loss_stop` (daily loss stop hit, `DAILY_LOSS_STOP=30`)** — from
  the moment the loss stop trips, every subsequent window skips the live order but
  **still emits the twin** (INV-D3), so the shadow line keeps tracking the strategy
  while the LIVE line goes flat for the rest of the day. This is the clearest
  demonstration of INV-D3's value.
- **UC-7-A2: `skip_daily_cap` (max trades/day reached)** — same shape; twin emits,
  LIVE flat.
- **UC-7-A3: `skip_band` (entry outside `(0.50, 0.92]`)** — the twin's
  `entry = signal_entry` may itself be outside the band; the twin row is still
  emitted (the shadow does not apply the live band gate — it records the strategy
  signal), while the live order is skipped. Documented divergence.

### Error Flows
- **UC-7-E1: skip row ledger append fails** — S1-UC-13; no crash; the twin still
  emits.

### Edge Cases
- **UC-7-EC1: repeated skip rows across ticks** — `place_live` skips are
  re-evaluated every tick (skips don't latch, S1-UC-5-EC1) → multiple skip
  `LiveTriggerRecord` rows, but **only one twin** (the `twin_window` latch fires
  once per window, INV-D3). The chart/table are unaffected by the extra skip rows
  (skips push no `TrigSummary`).

### Data Requirements
- **Input**: an F1 fire, a skip outcome, the twin `emit_trigger`.
- **Output**: skip `LiveTriggerRecord` row(s); one `live=false` twin row; `lv_*`
  null.
- **Side Effects**: no order; no `trades_today` change; twin Pending pushed once.

---

## UC-8: Bounded retry within a window (max 2 attempts) → twin emitted EXACTLY ONCE

**Actor**: signal_loop (live branch, retry state + twin latch)
**Preconditions**: EU F1 live mode; `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3`
(S1-UC-7/8/9, reused unchanged); attempt 1 no-fills, attempt 2 fills (or both
no-fill).
**Trigger**: a `nofill` on attempt 1 with the fire persisting.

### Primary Flow (Happy Path)
1. Attempt 1: `place_live` no-fills; `attempt_count = 1`; not latched (S1-UC-7). The
   **twin fires once here** (or on whichever tick the twin latch first opens),
   gated by `twin_window` (FR-3).
2. Later ticks within cooldown are blocked by `retry_gate` (S1-UC-9); the twin does
   **not** re-fire (its latch is already set for the window, INV-D3).
3. Attempt 2 (cooldown elapsed): `place_live` fills → one `live=true` row, latch
   consumed. The twin is **not** emitted a second time.
4. `compare()`: one `com_*` (the single twin) + one `lv_*` (the fill). Exactly one
   green step and one LIVE step for the window.

**Postconditions** (INV-D3, NFR-4): across up to `N` live attempts in a window, the
shadow twin is emitted **exactly once**; the live line steps at most once (one fill
per window, S1-INV-2). Retry attempts never multiply the twin or the live row.

### Alternative Flows
- **UC-8-A1: both attempts no-fill (budget exhausted)** — one twin, zero live rows
  → UC-6 shape (green steps, LIVE flat). The twin is still exactly once.
- **UC-8-A2: `RETRY_MAX_ATTEMPTS=1` (retry disabled)** — single attempt; twin once;
  identical split behavior.

### Error Flows
- **UC-8-E1: `order_error`/`rejected` mid-retry** — counts as one attempt
  (S1-UC-6/FR-9); does not latch; the twin is unaffected (already fired once).

### Edge Cases
- **UC-8-EC1: window rolls mid-retry** — on roll, `attempt_window` AND
  `twin_window` reset (both siblings of the roll block, ≈ 1126-1130); the new
  window gets a fresh twin (one) and a fresh retry budget (S1-UC-11). The
  old-window twin/live rows are unaffected.
- **UC-8-EC2: twin fires on a tick where the live attempt is cooldown-blocked** —
  the twin latch is independent of `retry_gate`; if the design fires the twin on
  the FIRST tick the fire is seen (regardless of whether a live attempt is placed
  that tick), the twin can precede attempt 1. Either ordering is acceptable as long
  as the twin is exactly once per window (INV-D3). The planner MUST pin the exact
  tick the twin fires; default: on the first tick the window is not
  twin-latched and a fire is present.

### Data Requirements
- **Input**: per-window `attempt_count`/`last_attempt_ts`/`twin_window`,
  `place_live` outcomes.
- **Output**: exactly one `live=false` twin row; at most one `live=true` fill row.
- **Side Effects**: up to `N` `create_ioc` calls; one twin Pending; latch on first
  fill.

---

## UC-9: Old /shadow_com payload without the live field → all rows treated as shadow (chart does not break)

**Actor**: `POST /shadow_com` deserialize → compare() → chart JS
**Preconditions**: an **older EU producer** (or a replayed old push) sends a
`/shadow_com` array whose `TrigSummary` elements **lack** the `live` field; the
Buffalo box runs the new `dashboard.rs`.
**Trigger**: `serde_json::from_str::<Vec<TrigSummary>>` on the old payload
(`dashboard.rs` ≈ 414).

### Primary Flow (Happy Path)
1. `TrigSummary.live` has `#[serde(default)]` (FR-1), so every element missing the
   key deserializes with `live == false` (AC-1). No parse error (NFR-1, INV-D2).
2. `compare()` treats all such rows as `live=false` → `com_*` populated, `lv_*`
   **null** for every window. This is exactly the shadow-only rendering (UC-2).
3. The chart renders the green `com_twin` line (these old rows were the "shadow/LIVE"
   line before the feature); the **LIVE line is absent** (no `live=true` rows). No
   JS exception; the anchor code sees no first-live window and skips the LIVE line
   (UC-14-EC1).

**Postconditions** (AC-1, NFR-1): a `live`-less payload parses and renders as an
all-shadow feed; the chart, table, and cards degrade to their shadow-only form
without error. A **mixed** feed (some elements with `live`, some without) parses
per-element — the `live`-less ones are shadow, the tagged ones split normally.

### Alternative Flows
- **UC-9-A1: unit test — `live`-less JSON deserializes to `false`** — a
  `TrigSummary` JSON object with no `live` key MUST deserialize with
  `live == false` (AC-1). This is the merge-blocking back-compat assertion.
- **UC-9-A2: old rows that used to be the mislabelled live line** — pre-feature
  live fills (pushed before the producer set `live=true`) are `live=false` on
  replay/receipt, so they render on the **green shadow** line, not the LIVE line.
  This is the documented consequence: historical live fills that were never tagged
  cannot be retro-split; only rows tagged `live=true` populate the LIVE line
  (documented, acceptable — the feature is forward-looking from the first tagged
  fill).

### Error Flows
- **UC-9-E1: malformed element in the array** — `from_str::<Vec<TrigSummary>>`
  returns `Err`; the handler returns `400 Bad Request` with `parse: {e}` (≈ 422)
  and does **not** overwrite `d.shadow_com` — Buffalo keeps the last good feed. No
  crash.

### Edge Cases
- **UC-9-EC1: `live` present but not a bool (e.g. `"true"` string / `1`)** — serde
  with `#[serde(default)]` on a `bool` field rejects a non-bool at the element
  level; the whole array parse fails → `400` (UC-9-E1). Documented so a
  wrong-typed producer is caught, not silently mis-split. (Planner may relax to a
  lenient deserializer; default: strict bool.)

### Data Requirements
- **Input**: a `/shadow_com` array without (or with mixed) `live` fields.
- **Output**: `d.shadow_com` with `live` defaulted to `false` where absent; an
  all-shadow (or partially-split) render.
- **Side Effects**: none on parse success beyond storing the feed; `400` + no store
  on parse failure.

---

## UC-10: Ledger replay across restart → reconstruct the live/twin split + dual resolve (ResolveRecord.live back-compat)

**Actor**: replay_line_into (EU boot) → Dash::resolve
**Preconditions**: an EU restart replays a JSONL ledger containing, for one window,
a live fill (`LiveTriggerRecord`, `live:true`), its shadow twin (`TriggerRecord`,
no `live` key), and their **two** resolve rows (`ResolveRecord`, one with `live:true`
and one with `live:false`), possibly interleaved with legacy rows lacking `live`.
**Trigger**: `load_ledger` replays each line via `replay_line_into` (≈ 335-372).

### Primary Flow (Happy Path)
1. **Trigger rows** — `replay_line_into` sets `live` from the row (FR-6): a
   `LiveTriggerRecord` (`live: true`, ledger.rs ≈ 144/208) → `TrigSummary.live =
   true`; a legacy `TriggerRecord` (no `live` key) → `false`. Two distinct
   `dash.triggers` rows are reconstructed for the window with correct flags (≈
   345-360).
2. **Resolve rows** — the resolve branch (≈ 362-369) reads `v["live"].as_bool()`
   from `ResolveRecord` and passes it to `Dash::resolve(.., live)` (FR-9). The
   `live:true` resolve attaches to the live row (`$5`-scale PnL); the `live:false`
   resolve attaches to the twin row (`$100`-scale PnL) — **even when**
   `eff == signal_entry` (AC-8, INV-D6).
3. **Legacy resolve rows** (no `live` key) — `v["live"].as_bool()` returns `None` →
   `false` (INV-D2), so a pre-feature resolve row disambiguates to `live=false`
   (matches the shadow/twin row by entry as before). Degraded-but-safe: pre-feature
   ledgers had no live/twin split, so `false` is the correct legacy interpretation.

**Postconditions** (AC-8): a replay over a mixed JSONL fixture reconstructs the
two rows and attaches each resolve to the correct row, preserving the `$5`/`$100`
split across restarts. **Zero parse errors** on a mixed pre-/post-feature file
(INV-D2, S1-UC-12).

### Alternative Flows
- **UC-10-A1: only legacy rows (pre-feature ledger)** — all triggers replay as
  `live=false`; all resolves as `live=false`; the feed renders shadow-only (UC-2).
  No LIVE line — correct, there were no tagged live fills historically.
- **UC-10-A2: `LiveTriggerRecord` with `live:true` but its resolve lacks `live`** —
  a partial-upgrade ledger (new trigger telemetry, old resolve writer): the live
  row is `live=true`, but its resolve reads `false` → it would match the **twin**
  row by entry, not the live row. If `eff == signal_entry`, the live row stays
  unresolved and the twin row gets both resolves (one overwriting). This is the
  degraded case FR-9 exists to prevent (the resolver MUST write `ResolveRecord.live`
  going forward). Documented as the exact hazard; the fix is FR-9 (always write
  `live` on resolve). With distinct entries it still resolves correctly by entry.

### Error Flows
- **UC-10-E1: malformed/truncated ledger line** — `serde_json::from_str::<Value>`
  fails; `replay_line_into`'s caller `continue`s (S1-UC-12-E1); one bad line never
  aborts the replay.

### Edge Cases
- **UC-10-EC1: `is_dashboard_trigger` filter on outcome** — a `LiveTriggerRecord`
  with a non-fill `outcome` (`nofill`/skip/etc.) is filtered out of `d.triggers` by
  the outcome gate (≈ 340-344, S1-UC-12-EC1) — so no-fill/skip rows do NOT become
  live `TrigSummary` rows on replay; only `filled`/`partial` (and legacy no-outcome)
  rows do. This keeps replayed `lv_*` counts equal to real fills.
- **UC-10-EC2: twin `TriggerRecord` has no `outcome` field** — a `TriggerRecord`
  (shadow) has no `outcome`; the filter treats a missing `outcome` as a dashboard
  trigger (legacy = filled, S1-UC-12-EC2), so the twin replays as a `live=false`
  dashboard row. Correct.

### Data Requirements
- **Input**: a mixed JSONL ledger (LiveTriggerRecord `live:true`, TriggerRecord,
  ResolveRecord with/without `live`).
- **Output**: two reconstructed `dash.triggers` rows with correct flags; each
  resolve attached to its own row.
- **Side Effects**: none (read-only replay, append-only ledger untouched).

---

## UC-11: EU restart mid-window → twin_window latch in-memory → possible twin double-emit (OPEN QUESTION)

**Actor**: signal_loop (EU) after a mid-window restart
**Preconditions**: the EU box restarts **mid-window** (the window has not rolled);
the in-memory `twin_window` latch resets on restart (it is not persisted); the live
order is protected by the persisted window latch / `live_state` (NFR-4).
**Trigger**: after restart, the same window is still current and the F1 gate fires
again.

### Primary Flow (Happy Path — documenting the open question)
1. Before restart, the twin already fired once for the window (INV-D3) and its
   `live=false` `TriggerRecord` is in the ledger.
2. On restart, replay reconstructs the live-order protection (S1 latch-on-fill +
   persisted `live_state`), so `place_live` does **not** re-place a live order for
   an already-filled window (NFR-4, S1-UC-11). But the in-memory `twin_window`
   latch is fresh (reset), so the twin latch is **not** set for the current window.
3. If the fire persists, `emit_trigger` fires a **second** twin for the same window
   → a duplicate `live=false` `TriggerRecord` + `Pending{live:false}` +
   `TrigSummary`.

**Postconditions (behavior to define — the PRD 3.10 open question)**: the duplicate
twin is a shadow row (`$100`-scale, `twin5`-normalized). Its blast radius is bounded
by the consumer:
- **Chart + table are safe** — `compare()` dedupes by window (first `.find()` match
  per flag, UC-4-A1), so only ONE `com_*` per window is plotted/rendered regardless
  of duplicate twin rows.
- **The `com_positions` COUNT can inflate** — `summary.com_positions` counts **all**
  `live=false` `shadow_com` rows in-period (≈ 334), so a duplicate twin
  double-counts by one in that card.
- **A duplicate twin `Pending` can double-resolve** — two `live=false` pendings for
  the same `(ticker, side, entry)` would each try to update the twin row; `resolve`
  updates the first unresolved match and `break`s (≈ 128), so the second resolve is
  a no-op on the row but still writes a second `ResolveRecord` and (harmlessly, as
  a shadow) does not touch `day_pnl` (twin is `live=false`).

The planner MUST choose the accepted behavior (PRD 3.10):
1. **Accept the duplicate** — document that a mid-window restart may inflate
   `com_positions` by one and write a duplicate shadow row; the chart/table are
   unaffected (dedupe-by-window). Lowest complexity.
2. **Persist the twin latch** — give `twin_window` the same persistence as
   `fired_window`/`live_state`, so the twin is once-per-window even across a
   restart. Higher fidelity, more state.

### Alternative Flows
- **UC-11-A1: restart after the window rolled** — the new window is legitimately
  fresh; its twin fires once (INV-D3). No duplicate — the duplicate only arises
  when the SAME window is still current after restart.
- **UC-11-A2: restart before the twin ever fired** — the twin fires once after
  restart (its first emission for the window). No duplicate.

### Error Flows
- **UC-11-E1: replay double-counts the pre-restart twin AND the post-restart twin** —
  the ledger holds both `TriggerRecord`s; a later full replay (UC-10) reconstructs
  two `live=false` rows for the window. Same dedupe-by-window protection applies to
  the chart/table; `com_positions` inflation persists in the replayed view.
  Consistent with UC-11's runtime behavior.

### Edge Cases
- **UC-11-EC1: live order re-placed after restart (latch NOT protective)** — out of
  scope for THIS feature (it is Section 1's persisted-latch responsibility, NFR-4);
  if the live latch fails to protect, that is an S1 defect, not a dashboard-line
  defect. This UC assumes NFR-4 holds and only the twin can double-emit.

### Data Requirements
- **Input**: a mid-window EU restart; in-memory `twin_window` reset.
- **Output**: possibly two `live=false` twin rows for one window.
- **Side Effects**: possible `com_positions` +1; chart/table unaffected
  (dedupe-by-window).

---

## UC-12: Buffalo restart mid-stream → recompute from the last pushed vectors

**Actor**: Buffalo box (consumer) after a restart
**Preconditions**: Buffalo restarts while the EU box keeps pushing; Buffalo's
`d.shadow_com`/`d.paper_f1`/`d.paper` are in-memory (populated by POST, not
persisted across a Buffalo restart).
**Trigger**: Buffalo comes back up; the next EU push lands.

### Primary Flow (Happy Path)
1. On restart, Buffalo's in-memory vectors are empty until the next POST arrives.
   `/stats` before the first post-restart push returns empty `compare[]` → the
   chart/table/cards render empty (no crash, UC-2-E1).
2. The EU push loop POSTs the **full** current `dash.triggers` vector to
   `/shadow_com` (the producer always sends the whole in-period vector, not a
   delta), so one push fully repopulates `d.shadow_com`; `compare()` re-splits by
   `live` and the LIVE line re-anchors from the repopulated pink series (UC-14).
3. Steady state resumes with no operator action.

**Postconditions**: a Buffalo restart loses no data permanently — the EU push is
the source of truth for `shadow_com`, and each push is a full replacement
(`d.shadow_com = trigs`, ≈ 418). The LIVE line/anchor are recomputed each `/stats`
from the current vectors (stateless render), so a restart is transparent once the
next push lands.

### Alternative Flows
- **UC-12-A1: paper feeds (`/paper`, `/paper_f1`) not yet re-pushed** — if the pink
  `f1_twin` series is empty after a Buffalo restart but live rows exist, the LIVE
  anchor defaults to `0` (no pink before the first live window, UC-14-EC1) until the
  paper-F1 cron re-pushes. The absolute baseline shifts; the relative LIVE steps are
  unchanged. Self-heals on the next `/paper_f1` push.

### Error Flows
- **UC-12-E1: EU push loop is down during the Buffalo restart window** — Buffalo
  renders empty until EITHER the EU push resumes; no crash, no stale-but-wrong data
  (empty is honest). Recovery is automatic when pushes resume.

### Edge Cases
- **UC-12-EC1: Buffalo serves a request mid-push** — the `POST /shadow_com` handler
  holds `d.lock()` while replacing the vector (≈ 417-419); a concurrent `/stats`
  either sees the old or the new full vector (mutex-serialized), never a torn
  half-vector. Consistent snapshot per request.

### Data Requirements
- **Input**: the EU full-vector pushes; Buffalo in-memory vectors.
- **Output**: recomputed `compare[]` + chart after the first post-restart push.
- **Side Effects**: none persistent on Buffalo.

---

## UC-13: Resolve arrives for a window with no matching dash.triggers row → silent no-op

**Actor**: EU resolver → Dash::resolve
**Preconditions**: the resolver settles a `Pending` whose corresponding
`dash.triggers` row is absent — e.g. the trigger row aged out of the vector, was
never pushed, or the `(ticker, side, entry, live)` tuple does not match any
unresolved row.
**Trigger**: `Dash::resolve(ticker, side, entry, result, won, pnl, live)` is called.

### Primary Flow (Happy Path)
1. `Dash::resolve` iterates `self.triggers`, testing `ticker` + `side` + `entry`
   within `1e-9` + `result.is_none()` + `t.live == live` (FR-7). No row matches.
2. The loop completes without hitting the `break` (≈ 123-130); the function returns
   having updated nothing. **No panic, no error** — a resolve with no matching row
   is a silent no-op.
3. The `ResolveRecord` is still appended to the ledger (the settlement is real,
   ≈ 846-858) and, if `live`, `day_pnl`/`total_pnl` are still advanced (≈ 859-867)
   — the missing dashboard row does not lose the PnL accounting.

**Postconditions**: an unmatched resolve leaves the dashboard trigger vector
unchanged and never crashes; the authoritative PnL is still recorded in the ledger
and (for live) in `live_state`. The dashboard may show a row as unresolved
(dangling `result==None`) if its resolve was mis-matched — a display gap, not a data
loss.

### Alternative Flows
- **UC-13-A1: wrong `live` flag causes the miss** — if a resolve is called with the
  wrong `live` value (e.g. UC-10-A2's degraded legacy resolve), it may match no row
  (when entries differ) or the wrong row (when entries are equal). This is why FR-9
  (always write `ResolveRecord.live`) is required — it makes the flag authoritative.
- **UC-13-A2: pending retained after unmatched resolve** — the resolver `retain`s
  out the settled pending regardless of the dashboard match (≈ 882-884), so an
  unmatched resolve does not leave a stuck pending re-settling every 20s.

### Error Flows
- **UC-13-E1: `get_market` error (settlement fetch fails)** — the resolver logs
  `warn!("resolve get_market ...")` (≈ 887) and leaves the pending for the next
  cycle; no `resolve` call is made this cycle. Retries next tick.

### Edge Cases
- **UC-13-EC1: two windows share `(ticker, side, entry, live)`** — extremely
  unlikely (same ticker + side + exact entry + flag across two windows), but
  `resolve` would update the first unresolved match. The window bounds are not part
  of the predicate; documented limitation inherited from pre-feature `resolve`.

### Data Requirements
- **Input**: a `Pending` with no matching `dash.triggers` row.
- **Output**: no dashboard change; a `ResolveRecord` still written.
- **Side Effects**: `day_pnl`/`total_pnl` advanced if `live`; pending retained-out.

---

## UC-14: LIVE line anchor — normal, sparse pink, no pink, and 300-row scroll-out

**Actor**: Chart JS (Buffalo) anchor computation
**Preconditions**: `compare[]` (300-cap) is available; at least one window carries
`lv_pnl` (a first live-fill window exists); the pink `f1_twin` series is computed
per-row via `twin5` (≈ 652).
**Trigger**: `updateChart(cmp)` recomputes the LIVE line each `/stats` refresh.

### Primary Flow (Happy Path)
1. The chart computes the running pink `f1_twin` cumulative across rows in
   chronological order (`rows = cmp.slice().reverse()`, ≈ 654).
2. It finds the **first** row with a non-null `lv_pnl` — the first live-fill window
   (`idx0`).
3. The **anchor** = the pink `f1_twin` cumulative at the window **immediately
   before** `idx0` (the running pink cumulative just prior). From the anchor, the
   LIVE line steps by **raw** `lv_pnl` (NO `twin5`, INV-D4) for each subsequent live
   window.
4. The LIVE line is plotted **only** from `idx0` onward — no point exists before the
   first live window (no leading zero tail, FR-14, AC-5).

**Postconditions** (AC-5, INV-D4): the LIVE line's first plotted value equals the
pink cumulative just before the first live fill, so the vertical gap between the
LIVE line and the pink line at any later window is the accumulated execution
divergence (slippage + fees + no-fill coverage) in raw `$5` money.

### Alternative Flows
- **UC-14-A1: pink present and dense before the first live window** — the normal
  case; the anchor is well-defined and the LIVE line starts on the pink line.

### Error Flows
- **UC-14-E1: `lv_pnl` present but `lv_entry` missing** — the LIVE line needs the
  raw `lv_pnl` (money), not the entry, to step; a missing `lv_entry` does not break
  the step (unlike `twin5` which needs entry). The step uses raw `lv_pnl` directly
  (INV-D4). Documented: the LIVE line is entry-independent by design.

### Edge Cases
- **UC-14-EC1: NO pink `f1_twin` value at or before the first live window** — the
  anchor "pink cumulative just prior" is undefined; per PRD 3.10 it MUST default to
  **`0`** (the LIVE line starts at `0 + first raw lv_pnl`). The chart JS MUST NOT
  emit an undefined/`null` anchor. This is the precise resolution of the PRD 3.10
  open question ("the running `f1_twin` cumulative at the last window with a value
  at or before the first live window, defaulting to `0` if none").
- **UC-14-EC2: sparse pink (gaps, some pink windows unresolved)** — the anchor uses
  the **last available** running pink cumulative at or before the first live window
  (skipping null pink windows, which contribute no step). A pink window with
  `f1_pnl==null` (unresolved) adds `0` to the cumulative, so the anchor is the last
  resolved pink cumulative. Documented so a sparse pink series does not produce a
  jumpy anchor.
- **UC-14-EC3: first live window scrolls out of the 300-row `compare` cap** —
  `compare()` keeps the 300 most-recent windows (`keys.rev()` then `truncate(300)`,
  ≈ 300). Once the original first-live window ages beyond the 300 most-recent
  windows, `idx0` shifts to the **earliest live window still visible**, and the
  anchor recomputes to the pink cumulative just before THAT window. Because BOTH the
  pink cumulative and the LIVE cumulative are computed only over the visible 300
  rows, they re-baseline **together** — the absolute baseline drifts as history
  scrolls, but the RELATIVE LIVE-vs-pink gap within the visible window is preserved.
  This re-anchoring drift is **acceptable and documented** (not a correctness bug);
  the card real-money totals (raw `lv_pnl` sum, ≈ 683-685) remain the authoritative
  absolute figure. The E2E test MUST assert the chart does not error or produce a
  `NaN` anchor when the first live window is beyond row 300.
- **UC-14-EC4: pink and live trade the SAME first window (no window strictly
  before)** — if the first live window is also the earliest window overall (no
  window before it), the anchor is `0` (UC-14-EC1). If a pink trade exists in that
  same first live window, whether that same-window pink step is included in the
  anchor MUST be pinned: the recommended rule is the anchor sums pink **strictly
  before** the first live window (the same-window pink belongs to the concurrent
  step, not the anchor), defaulting to `0`. (Flagged for planner; default =
  strictly-before with `0` fallback, matching FR-15's "immediately before".)

### Data Requirements
- **Input**: `compare[].lv_pnl`, `compare[].f1_pnl`/`f1_entry` (for pink `twin5`),
  window ordering.
- **Output**: the LIVE line points `{time, anchor + Σ raw lv_pnl}` from `idx0`
  onward.
- **Side Effects**: none; pure chart computation.

---

## UC-15: lv_pnl null (unresolved) → LIVE line steps only on resolved; one live row per window; partial fill

**Actor**: Chart JS (Buffalo) LIVE-line `build()` + compare()
**Preconditions**: a live fill exists for a window but has not yet settled
(`result == None` → `lv_pnl == null`); OR a partial fill; OR the (impossible under
latch) multiple-fill case.
**Trigger**: `updateChart` builds the LIVE line while some live windows are
unresolved.

### Primary Flow (Happy Path)
1. A `live=true` row with `result==None` has `lv_pnl == null`. The LIVE-line
   `build()` accumulates raw `lv_pnl` **only where it is non-null** (analogous to
   the existing `com_twin` build gating on `com_pnl != null`, ≈ 651/656). An
   unresolved live window therefore adds **`0`** to the cumulative → the LIVE line
   **holds flat** across it until it resolves.
2. Once the resolver settles the window (UC-1 step 5) and the next push lands,
   `lv_pnl` becomes non-null and the LIVE line **steps** on the next refresh.

**Postconditions** (INV-D4): the LIVE line steps **only on resolved** live windows;
an unresolved live fill is drawn flat (the position exists but its PnL is not yet
known), matching the pink/green lines' resolve-gated stepping. No premature or
guessed PnL is plotted.

### Alternative Flows
- **UC-15-A1: partial fill** — the live row has `count = fill` (partial size),
  `entry = eff`; on resolve its raw `lv_pnl` scales to the partial count. One live
  row, one LIVE step. The twin (`$100`, full-size) is unaffected (S1-UC-3).
- **UC-15-A2: multiple live fills in one window are IMPOSSIBLE** — `fired_window`
  latches on the first confirmed fill (S1-INV-2), so at most one `live=true` row
  per window. Even if a duplicate somehow appeared, `compare()`'s first-`.find()`
  split (UC-4-A1) plots only one `lv_*` per window. The LIVE line steps at most once
  per window.

### Error Flows
- **UC-15-E1: `lv_pnl` is `0` (a real breakeven resolve) vs `null` (unresolved)** —
  the build MUST distinguish `null` (no step) from a real `0` PnL (a step of `0`,
  which visually looks flat but IS a resolved window). Both look flat on the chart,
  but a real `0` counts toward `lv_positions`/win-rate while a `null` does not.
  The E2E test MUST assert `lv_pnl==0` counts as a resolved live position and
  `lv_pnl==null` does not.

### Edge Cases
- **UC-15-EC1: all live windows unresolved (fresh live start)** — every `lv_pnl` is
  null; the LIVE line is flat at the anchor value (no steps yet). The line still
  exists (anchored at `idx0`), just flat, until the first live resolve. Correct
  depiction of "positions open, none settled".
- **UC-15-EC2: `lv_positions` counts fills, not resolves** — `summary.lv_positions`
  counts `live=true` `shadow_com` rows in-period (FR-12), i.e. fills, resolved or
  not; `summary.lv_pnl` sums only resolved raw `lv_pnl`. The cards MUST label these
  distinctly (fills count vs realized PnL) so an operator does not read an unresolved
  fill as a `$0` result.

### Data Requirements
- **Input**: `compare[].lv_pnl` (null / real), `lv_result`/`lv_won`,
  `summary.lv_positions`/`lv_pnl`.
- **Output**: a resolve-gated LIVE line; `lv_positions` (fills) vs `lv_pnl`
  (realized) cards.
- **Side Effects**: none; render-time gating.

---

## Flow counts

- **Primary flows (one per use case, UC-1 … UC-15):** 15
- **Alternative flows (UC-N-Ax):** 30
  (UC-1: 3, UC-2: 2, UC-3: 2, UC-4: 1, UC-5: 2, UC-6: 2, UC-7: 3, UC-8: 2, UC-9: 2,
  UC-10: 2, UC-11: 2, UC-12: 1, UC-13: 2, UC-14: 1, UC-15: 2)
- **Error flows (UC-N-Ex):** 15
  (UC-1: 2, UC-2: 1, UC-3: 1, UC-4: 1, UC-5: 1, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 1, UC-15: 1)
- **Edge cases (UC-N-ECx):** 22
  (UC-1: 2, UC-2: 1, UC-3: 2, UC-4: 1, UC-5: 2, UC-6: 2, UC-7: 1, UC-8: 2, UC-9: 1,
  UC-10: 2, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 4, UC-15: 2)

**Total scenarios:** 82 (15 primary + 30 alternative + 15 error + 22 edge).

## Traceability to PRD 3.5 acceptance criteria

- **AC-1** (old JSON → `live=false`) → UC-9, UC-9-A1.
- **AC-2** (resolve disambiguation, equal entries) → UC-5, UC-5-EC1/EC2.
- **AC-3** (compare() split exclusive) → UC-4, UC-1 step 7.
- **AC-4** (green line contains no live fills) → INV-D5, UC-2, UC-6.
- **AC-5** (LIVE line anchor) → UC-14, UC-1 step 8.
- **AC-6** (table shows LIVE instead of US) → UC-3.
- **AC-7** (cards show LIVE metrics) → UC-3.
- **AC-8** (restart preserves the split) → UC-10.
- **AC-9** (shadow-only view unchanged) → UC-2.

## Open questions surfaced for the planner (from PRD 3.10)

- **Twin double-emit on mid-window restart** (UC-11): accept the duplicate
  (chart/table safe via dedupe-by-window; `com_positions` may inflate by one) **or**
  persist the `twin_window` latch. Default recommendation: accept + document, since
  the consumer already dedupes by window.
- **Anchor when pink is sparse / absent** (UC-14-EC1/EC2/EC4): anchor = running
  `f1_twin` cumulative at the last pink value **strictly before** the first live
  window, default `0` if none. Chart JS MUST never emit a `null`/`NaN` anchor.
- **`match_lv` denominator** (INV-D9, UC-3 step 4): only windows where BOTH a live
  fill and a paper-F1 trade exist; card copy MUST state the denominator explicitly.
- **Twin firing tick** (UC-8-EC2): pin the exact tick the twin fires within a
  window; default: first tick where the window is not twin-latched and a fire is
  present.
- **Strict vs lenient `live` deserialization** (UC-9-EC1): default strict bool
  (a non-bool `live` fails the array parse → `400`); planner may relax.
</content>
</invoke>
