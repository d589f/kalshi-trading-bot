# Use Cases: Execution Signal-Anchor (Re-anchor Live IOC to the Signal Price)

> Based on [PRD](../PRD.md) — section 4, internal id `exec-signal-anchor`

This feature is a **real-money order-path change** on the EU live trader
(`kalshi_rs`, strategy `f1_d50cap75`, `$5`, subaccount #1). It re-anchors the live
IOC **limit price and sizing** to the **signal entry** (`fire.entry`, the paper-1:1
gate price) instead of the **fresh order-time ask**, and removes the synchronous
pre-order orderbook GET from the critical path. It is gated behind a new
`EXEC_ANCHOR` env whose default (`ask`) is **byte-identical** to today — the same
regression-guarantee pattern as the `SESSION` env in section 2 (`select_session`,
`main.rs` ≈ lines 64-78). This document is the single source of truth for the
`exec-signal-anchor` E2E tests; where a scenario is a pure reuse of a section 1/2/3
rail it **references** the corresponding UC rather than re-deriving it.

### Cross-reference legend

| Tag | File | PRD |
|-----|------|-----|
| **S1-UC-N** | `live-exec-telemetry-latch-fix_use_cases.md` | §1 — `LiveTriggerRecord`, `drift`/`walk`, P0b latch + bounded retry |
| **S2-UC-N** | `live-f1-strategy_use_cases.md` | §2 — `SESSION` env-selection pattern, F1 gate, EU rollout |
| **S3-UC-N** | `dashboard-f1-live-line_use_cases.md` | §3 — live/shadow `TrigSummary.live` split, resolve disambiguation |

Reused rails referenced (not re-derived here):

- **Env-selection pattern** — `S2-UC-1` (operator selects at boot → config/log/
  attribution), `S2-UC-2` (default byte-identical regression guarantee), `S2-UC-10`
  (unrecognized value → fail-loud + fallback). `EXEC_ANCHOR` mirrors `SESSION`.
- **P0b retry / telemetry** — `S1-UC-1` (filled row + full decomposition), `S1-UC-2`
  (201-but-zero-fill `nofill`), `S1-UC-3` (partial), `S1-UC-6` (skip / order_error /
  rejected), `S1-UC-7` (no-fill → not latched → bounded retry → first fill latches),
  `S1-UC-8` (attempts exhausted), `S1-UC-9` (cooldown; 0.3s ticks must not order
  every tick), `S1-UC-10` (`trades_today` increments only on fill), `S1-UC-11`
  (window rolls mid-retry), `S1-UC-12` (loaders parse old+new), `S1-UC-13`
  (serialize/append robustness — loop never crashes).
- **Live/shadow resolve** — `S3-UC-5` (resolve disambiguation by the `live` flag with
  **equal** entries) — signal-anchor makes `eff == signal_entry` more common, loading
  this harder.

## Actors

Internal Rust binary — no HTTP API or interactive UI. The actors are the operator,
the code paths, and the external processes the bot talks to:

- **Operator** — appends `EXEC_ANCHOR=signal` to the EU box drop-in `mirror.conf`
  (`34.32.177.126`, systemd `kalshi-shadow-com`) alongside `SESSION=f1_d50cap75`,
  `STAKE=5`, `SUBACCOUNT=1`, `MAX_ENTRY=0.92`, `PRICE_BUF=0.06` (§2 FR-9), and
  restarts in the first ~60s of a 15-min window. Owns rollback (remove the line /
  set `=ask`).
- **main() / boot** — `main.rs::main`. Reads `EXEC_ANCHOR`, resolves it through the
  pure selector `select_exec_anchor` (new, mirroring `select_session` at `main.rs`
  ≈ 64-78), logs a fail-loud warning on an unrecognized value, and threads the
  resolved `ExecAnchor` into `place_live`.
- **select_exec_anchor** — the new **pure** function `select_exec_anchor(env_val:
  Option<&str>) -> (ExecAnchor, Option<String>)`, `ExecAnchor = Ask | Signal`
  (FR-2). Unit-testable without env or a live order, exactly as `select_session`'s
  test (`main.rs` ≈ 2104-2115).
- **place_live** — the live IOC routine (`main.rs` ≈ 1364-1618). In `ask` mode:
  synchronous `rest.get_orderbook(&book.ticker, 10).await` (≈ 1447-1457) →
  `exec_entry ± PRICE_BUF` limit (≈ 1458-1461) → `count = round(stake / exec_entry)`
  (≈ 1467-1473) → `create_ioc` (≈ 1476-1487) → deeper re-quote on no-fill (≈
  1491-1513). In `signal` mode: **no** pre-order GET, limit/count off `signal_entry`,
  order POST + telemetry GET run under `tokio::join!`, re-quote disabled.
- **signal_loop** — the 0.3s decision loop. Owns the `fired_window` latch and the
  per-window retry state (`attempt_window` / `attempt_count` / `last_attempt_ts`),
  drives `place_live` via `retry_gate` / `counts_as_attempt` / `latch_decision`
  (`main.rs` ≈ 1206-1257). **Unchanged** by this feature (S1 rails).
- **KalshiRest::get_orderbook** — `rest.rs` ≈ 151; the orderbook GET. Its HTTP client
  has an **8s** timeout (`rest.rs` ≈ 104-106). In `ask` mode awaited before the
  order; in `signal` mode awaited concurrently, **telemetry only**.
- **OrderClient::create_ioc** — `orders.rs` ≈ 103-133; the live IOC POST. Its HTTP
  client has a **10s** timeout with HTTP/2 keep-alive (`orders.rs` ≈ 78-85); the
  order RTT is sub-second in steady state. Formats `count`/`price` to cents via
  `format!("{:.2}", .)` (`orders.rs` ≈ 114-115). Routed to `SUBACCOUNT=1`.
- **Ledger / LiveTriggerRecord** — `ledger.rs`; the append-only JSONL row. Fields
  `exec_entry` (`Option<f64>`, ≈ 167), `first_limit_price` / `requote` /
  `requote_limit_price` (≈ 171-175) are **reused**, not added (§4.7).
- **Resolver / dashboard** — settlement task + Buffalo dashboard (§1/§3). Unchanged;
  they consume `eff`, the `live` flag, and PnL — all anchor-agnostic.

## Shared preconditions (apply unless a use case overrides them)

- The bot runs in **MIRROR mode** on the EU box; `fire.entry` is the **signal
  entry** (paper-1:1, §2), also called `signal_entry` throughout this doc.
- For live use cases: `LIVE_TRADING=1`, an `OrderClient` present, `SUBACCOUNT=1`,
  `STAKE=5`, `MAX_ENTRY=0.92`, `PRICE_BUF=0.06` (prod), `SESSION=f1_d50cap75`, and
  the §2 F1 go-live audit passed (`S2-UC-6`).
- Section 1 P0b rails enabled and **unchanged**: `RETRY_MAX_ATTEMPTS=2`,
  `RETRY_COOLDOWN_SECS=3`; latch on confirmed fill only.
- The `(0.50 < signal_entry ≤ cfg.max_entry_price]` band gate (`main.rs` ≈
  1438-1442) runs **before any order** in both modes (§4 FR-11).

## Invariants asserted across use cases (verify in every relevant postcondition)

- **INV-SA1 (env is the only lever).** With `EXEC_ANCHOR` unset/`ask`, `place_live`
  is **byte-for-byte** the pre-feature `ask` path — synchronous book GET,
  `exec_entry ± PRICE_BUF`, `round(stake/exec_entry)`, and the deeper re-quote all
  run as today (FR-3, AC-1, AC-8). `signal` mode is reachable only via the explicit
  env value.
- **INV-SA2 (no pre-order book GET in signal mode).** In `signal` mode there is
  **no** awaited `get_orderbook` on the path from the band gate to `create_ioc`; the
  book GET, if it runs, is inside the `tokio::join!` alongside the POST (FR-6, AC-4).
- **INV-SA3 (fixed signal anchor; never a chase).** In `signal` mode the limit =
  `signal_entry ± PRICE_BUF` and `count = round(stake / signal_entry)`; the deeper
  re-quote is disabled (`requote == false`, `requote_limit_price == None`); a P0b
  retry re-anchors at the **same** fixed price (FR-4/5/9/10, AC-3/AC-6).
- **INV-SA4 (fail-open telemetry).** The concurrent orderbook GET **never** blocks or
  fails the order. `exec_entry = Some(ask-at-order-time)` on fetch success, `None` on
  fetch failure; the order outcome depends **only** on the POST result (FR-7/FR-8,
  AC-5, NFR-5).
- **INV-SA5 (safety clamp + band + cents unchanged).** The band gate runs before any
  order; the `.min(0.99)` / `.max(0.01)` clamp is retained; cents rounding via
  `format!("{:.2}", .)` is unchanged (FR-6/FR-11).
- **INV-SA6 (§1/§2/§3 rails unchanged).** P0b latch/retry, the ledger record shapes,
  subaccount isolation, the honest shadow-twin, the dashboard live/shadow split, and
  the MIRROR 1:1 signal path are **unchanged**. This feature changes only the anchor
  used to price/size the live IOC (FR-10/FR-12, AC-8). No new field, endpoint, or
  dependency (§4.7, NFR-3).
- **INV-SA7 (hard limit bound).** In `signal` mode the Yes IOC limit ≤
  `signal_entry + PRICE_BUF`, so `eff ≤ signal_entry + PRICE_BUF`. With
  `signal_entry ≤ 0.92` and `PRICE_BUF = 0.06`, `eff ≤ 0.98`. **This is the HARD
  bound.** AC-7's "no fill above the `0.92` cap" is a **SOFT / empirical** claim that
  holds because most F1 entries sit well below `0.86` — see **UC-10** for the
  boundary tension.

---

## UC-1: Signal-mode fill at/below the signal-anchored limit → telemetry decomposition preserved

**Actor**: signal_loop → place_live → OrderClient (order POST) ‖ KalshiRest
(concurrent telemetry GET) → resolver → dashboard
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions hold; a fresh F1
`MirrorSnap` yields `res.fire` with `signal_entry = fire.entry ∈ (0.50, 0.92]`; all
safety gates pass; the IOC crosses (the ask ≤ the signal-anchored limit).
**Trigger**: a tick where the F1 gate fires and the window is unlatched.

### Primary Flow (Happy Path)
1. signal_loop resets per-window retry state on window roll, finds the window
   unlatched, passes `retry_gate`, and calls `place_live(..., ExecAnchor::Signal)`.
2. `place_live` passes the daily-cap, loss-stop, and band gates (`main.rs` ≈
   1428-1442). **No** synchronous `get_orderbook` runs (INV-SA2).
3. The IOC is priced off the **signal entry** (FR-4): `Side::Yes` →
   `price = (signal_entry + PRICE_BUF).min(0.99)`; `Side::No` →
   `price = ((1.0 - signal_entry) - PRICE_BUF).max(0.01)`. `first_limit_price`
   records this signal-anchored limit.
4. Sizing uses the signal entry as denominator (FR-5):
   `count = round(stake / signal_entry)` clamped to `[1, max_count]` (`MAX_COUNT`
   default 15).
5. The order POST fires **immediately** after the gates. Concurrently (same instant,
   via `tokio::join!`, FR-7) the orderbook GET runs **for telemetry only** — its
   derived ask (`KalshiBook::derive` + `yes_ask`/`no_ask`, `main.rs` ≈ 1448-1454) is
   captured as the "ask at order time".
6. The join completes; `create_ioc` returned HTTP 201 with `fill > 0`,
   `remaining_count == 0` → **filled** (`S1-UC-1`).
7. `place_live` appends **one** `LiveTriggerRecord` (`outcome = filled`, session
   `f1_d50cap75`) with: `entry = signal_entry`, `exec_entry = Some(ask-at-order-time)`
   (from the concurrent GET), `first_limit_price` (signal-anchored), `requote = false`,
   `requote_limit_price = None` (FR-9), `remaining_count`, `fill`, `eff`, `fee`,
   `latency_ms`, plus `p`/`side`/`count`/`delta_from_open`/window bounds/
   `market_ticker`/`ts`/`ts_iso`. It increments `trades_today`, pushes a `TrigSummary`
   (`live = true`, §3) and a `Pending`.
8. `place_live` returns `filled`; signal_loop latches `fired_window` (`S1-UC-7`).
9. The resolver later settles the `Pending` into a `ResolveRecord`; the dashboard
   LIVE line renders the fill against paper-F1 (§3, unchanged).

**Postconditions** (AC-3/AC-5/AC-7): exactly one `filled` signal-mode row per window;
`eff ≤ signal_entry + PRICE_BUF ≤ 0.98` (INV-SA7); `drift = exec_entry − signal_entry`
and `walk = eff − exec_entry` are **computable** (fetch succeeded); no pre-order book
GET occurred (INV-SA2); the order routed to subaccount #1; the window is latched.

### Alternative Flows
- **UC-1-A1: NO-side fire** — `delta_from_open < 0` → `Side::No`; the No formulas of
  steps 3-4 apply (`price = ((1 - signal_entry) - PRICE_BUF).max(0.01)`). Still one
  `filled` signal-mode row.
- **UC-1-A2: eff strictly better than the anchor** — the ask at order time is below
  the signal-anchored limit (even below `signal_entry`); the IOC fills at that ask,
  so `eff < signal_entry + PRICE_BUF` (possibly `eff < signal_entry`). Kept as-is —
  a favorable fill; see **UC-14** (negative drift).

### Error Flows
- **UC-1-E1: append/serialize failure on the `filled` row** — `S1-UC-13`; the loop
  never crashes; the latch decision still follows the returned outcome.

### Edge Cases
- **UC-1-EC1: order returns before the telemetry GET** — the join waits for both;
  the row is built only after both resolve, so `exec_entry` reflects the completed
  GET (Some/None). The order execution itself is **not** delayed by the GET
  (INV-SA4); but the *return* of `place_live` waits on the join — see **UC-8** for
  the bounded-wait design flag.
- **UC-1-EC2: `eff == signal_entry` (zero drift)** — the signal-anchored fill lands
  exactly at the signal price; `drift = exec_entry − signal_entry` may be `~0`,
  `walk ≈ 0`. This EQUAL-entry case is common in signal mode and loads the §3
  live/shadow resolve disambiguation (`S3-UC-5`) — see **UC-14**.

### Data Requirements
- **Input**: `ExecAnchor::Signal`, `fire` (`signal_entry`, `side`, `p`, `delta`),
  `PRICE_BUF`, `stake`, `max_count`; the concurrent book GET result; the `OrderResp`.
- **Output**: one `filled` `LiveTriggerRecord` (session `f1_d50cap75`, `live=true`)
  with `exec_entry = Some(...)`; a `Pending`; a `TrigSummary`; a later `ResolveRecord`.
- **Side Effects**: one real `$5` IOC on subaccount #1; `trades_today += 1`; ledger
  append; latch consumed; **no** pre-order book GET.
- **Traceability**: FR-4, FR-5, FR-6, FR-7, FR-8; AC-3, AC-5, AC-7; NFR-1, NFR-5.

---

## UC-2: Default / `ask` mode → byte-identical legacy order flow (regression guarantee)

**Actor**: main()/boot + place_live (the `ask` path)
**Preconditions**: `EXEC_ANCHOR` unset OR `ask`; live preconditions hold.
**Trigger**: an F1 fire with `EXEC_ANCHOR` resolving to `ask`.

### Primary Flow (Happy Path)
1. `main()` resolves `select_exec_anchor(None) == (Ask, None)` (unset) or
   `("ask") == (Ask, None)` and threads `ExecAnchor::Ask` into `place_live` (FR-1/2).
2. `place_live` runs the **synchronous** `rest.get_orderbook(&book.ticker, 10).await`
   (`main.rs` ≈ 1447-1457): derives the current ask, filters to `(0.50, 0.98]`, else
   falls back to `entry` — exactly as today.
3. Limit = `exec_entry ± PRICE_BUF` (≈ 1458-1461); `count = round(stake / exec_entry)`
   clamped `[1, max_count]` (≈ 1467-1473); `first_limit_price` = the exec-anchored
   limit.
4. On a no-fill the deeper single re-quote at `exec_entry ± REQUOTE_BUF` runs (≈
   1491-1513, gated by `REQUOTE_BUF > PRICE_BUF`) — **unchanged** (`S1-UC-4`).
5. The rest of the outcome handling (filled/partial/nofill/rejected/order_error) is
   byte-identical to §1 behavior.

**Postconditions** (INV-SA1, AC-1, AC-8): the computed limit and `count` equal the
legacy values for every representative `(signal_entry, exec_entry, PRICE_BUF, stake,
side)`; the re-quote still fires; all 75 pre-existing tests stay green (FR-12).
`EXEC_ANCHOR` added at most one enum comparison to the `ask` path (NFR-4).

### Alternative Flows
- **UC-2-A1: `EXEC_ANCHOR=ask` explicit** — identical to unset; the explicit value
  selects `Ask` with no warning (AC-2). No divergence from the default.
- **UC-2-A2: whitespace / case `" ask "`, mixed case** — trimmed then matched exactly
  (mirrors `select_session`); `" ask "` → `(Ask, None)`. A case variant that is not
  the exact token (e.g. `ASK`) is **unrecognized** → **UC-3**.

### Error Flows
- **UC-2-E1: legacy `ask`-mode order error / reject / no-fill** — handled exactly as
  §1 (`S1-UC-6`, `S1-UC-2`); this feature does not touch the `ask` outcome paths.

### Edge Cases
- **UC-2-EC1: `SESSION` unset (f6) + `EXEC_ANCHOR` unset (ask)** — the double default:
  f6 gate + legacy ask execution — the current prod-for-f6 baseline, wholly
  unchanged. (f6 + `signal` is the unusual combo of **UC-11**.)

### Data Requirements
- **Input**: `EXEC_ANCHOR` unset/`ask`; the synchronous book GET; `fire`; buffers.
- **Output**: legacy-shaped `LiveTriggerRecord` rows (`exec_entry` from the
  synchronous GET; `requote` possibly `true`).
- **Side Effects**: identical to pre-feature; synchronous pre-order GET runs.
- **Traceability**: FR-1, FR-2, FR-3, FR-12; AC-1, AC-8; NFR-4. Mirrors `S2-UC-2`.

---

## UC-3: Unrecognized `EXEC_ANCHOR` value → fail loud + fall back to `ask` (never an unintended anchor on real money)

**Actor**: main()/boot + select_exec_anchor
**Preconditions**: `EXEC_ANCHOR` set to a non-empty value that is neither `ask` nor
`signal` after trim (e.g. `sig`, `signl`, `SIGNAL`, `signal ` with an internal typo,
`ask!`).
**Trigger**: the binary boots and reads `EXEC_ANCHOR`.

### Primary Flow (Happy Path)
1. `select_exec_anchor(Some(raw))` trims `raw`; it is non-empty and matches neither
   token → returns `(Ask, Some(warning))` — a **pure, total** function that never
   panics (FR-2, NFR-2).
2. `main()` logs the warning **fail-loud** via `error!` (mirroring the `session_warn`
   log at `main.rs` ≈ 513-515), naming the bad value and the accepted set
   (`ask`, `signal`), and stating that it **fell back to `ask`** (FR-1).
3. The process runs in **`ask` mode** — the byte-identical legacy path (UC-2) — so a
   typo degrades to today's known-safe behavior, never to `signal` by accident.

**Postconditions** (NFR-2): the bot never silently adopts an unintended anchor; an
unrecognized value is loudly logged and resolves to `ask`. No order is placed at
boot; the selector's result is unit-testable without env (AC-2).

### Alternative Flows
- **UC-3-A1: empty / whitespace-only value** — `select_exec_anchor(Some(""))` and
  `(Some("  "))` return `(Ask, None)` — treated as **unset**, a *silent* default (no
  warning), distinct from the fail-loud unrecognized case (AC-2). An empty var is
  "not provided".

### Error Flows
- **UC-3-E1: value differs only by case (`SIGNAL`, `Ask`)** — the match is
  case-sensitive after trim (consistent with `select_session`), so `SIGNAL` is
  **unrecognized** → fail-loud → `ask` (this UC), NOT `signal`. The runbook MUST
  document the exact lowercase spellings.

### Edge Cases
- **UC-3-EC1: `" signal "` (surrounding whitespace, correct token)** — trimmed to
  `signal` → `(Signal, None)`; a leading/trailing space does NOT make it
  unrecognized (AC-2). Only a value that fails to match **after** trim reaches this
  UC.

### Data Requirements
- **Input**: `EXEC_ANCHOR` (unrecognized non-empty, or empty/whitespace for A1).
- **Output**: `(Ask, Some(warning))` + an `error!` log line (unrecognized); or
  `(Ask, None)` silently (empty). Selected `ExecAnchor::Ask`.
- **Side Effects**: no orders, no ledger writes at boot.
- **Traceability**: FR-1, FR-2; AC-2; NFR-2. Mirrors `S2-UC-10`.

---

## UC-4: Signal-mode no-fill → window NOT burned → P0b retry at the SAME fixed anchor → second no-fill → bounded stop

**Actor**: signal_loop (retry state) + place_live
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; the same `win_key`
persists; `RETRY_MAX_ATTEMPTS=2`, `RETRY_COOLDOWN_SECS=3` (S1 P0b, reused unchanged);
the ask at the signal instant ran **beyond** the signal-anchored limit
(`signal_entry + PRICE_BUF`).
**Trigger**: attempt 1 returns `nofill` with the F1 fire persisting.

### Primary Flow (Happy Path)
1. Attempt 1: the IOC at `signal_entry + PRICE_BUF` does not cross (the book already
   ran past the fixed anchor) → HTTP 201 with `fill <= 0` → **nofill** (`S1-UC-2`).
   **No deeper re-quote fires** (FR-9): `requote == false`, `requote_limit_price
   == None`. One `nofill` row (session `f1_d50cap75`); `attempt_count == 1`;
   `last_attempt_ts = now`; window **NOT latched** (S1 latch-on-fill only) — the
   window is not burned.
2. Ticks within the 3s cooldown: `retry_gate` blocks — no order on every 0.3s tick
   (`S1-UC-9`).
3. First tick at `now − last_attempt_ts ≥ 3` and `attempt_count < 2`: attempt 2 is
   placed, **re-anchored at the SAME** `signal_entry ± PRICE_BUF` (FR-10) — a fixed
   anchor across attempts, never a progressively deeper chase (contrast the `ask`
   re-quote).
4. If attempt 2 also no-fills: `attempt_count == 2 == RETRY_MAX_ATTEMPTS` → exhausted;
   no further attempts until window roll resets state (`S1-UC-8`). Two `nofill` rows;
   the window is not traded and not latched.

**Postconditions** (AC-6, INV-SA3, INV-SA6): the S1 P0b behavior is byte-identical
except that **each attempt re-uses the same fixed signal anchor** and **no re-quote
ever fires**; the no-fill did not burn the window (a retry got a second chance); a
bounded stop after 2 attempts prevents per-tick spamming.

### Alternative Flows
- **UC-4-A1: retry SUCCEEDS at the same anchor** — attempt 1 `nofill`; attempt 2
  (after cooldown, same `signal_entry ± PRICE_BUF`) **fills** → `filled`/`partial`
  row, `trades_today += 1`, `fired_window` latched — at most one filled position per
  window (`S1-UC-7`). No further attempts this window.
- **UC-4-A2: `RETRY_MAX_ATTEMPTS=1` (retry disabled)** — single attempt per window,
  byte-identical to pre-P0b single-shot (S1 NFR-4); still a signal-mode `nofill` row
  with `requote == false`.

### Error Flows
- **UC-4-E1: `order_error` / `rejected` mid-retry** — counts as one attempt toward
  the budget (`S1-UC-6`, and **UC-7** here); does not latch; retry continues if
  budget and cooldown allow.

### Edge Cases
- **UC-4-EC1: window rolls mid-retry** — `S1-UC-11`; the in-flight old-window attempt
  latches only its own window; the new window starts with a fresh budget and re-
  anchors at the **new** window's `signal_entry`.
- **UC-4-EC2: the fast-book residual (accepted trade-off)** — per §4.10, 5-7 of 20
  sampled fills would not fit the 6c buffer at their signal instant; the retry (up to
  2 attempts) plus the ~150-250ms earlier arrival (no pre-order GET) partially
  recover these. The residual no-fills are the **accepted** cost of the fixed anchor
  (tighter caps are strictly worse — they cut winners, §4.1 counterfactual).

### Data Requirements
- **Input**: per-window `attempt_count`/`last_attempt_ts`, `RETRY_*`, the
  `place_live` outcome; the fixed `signal_entry ± PRICE_BUF` limit.
- **Output**: one row per attempt (`nofill`, or a `filled`/`partial` on success), all
  with `requote == false`.
- **Side Effects**: up to 2 `create_ioc` calls / window at the **same** price; latch
  on first fill; `trades_today += 1` once (on fill).
- **Traceability**: FR-9, FR-10; AC-6; §4.10. Reuses `S1-UC-2/7/8/9/11`.

---

## UC-5: Concurrent telemetry fetch fails → order proceeds, `exec_entry = None`, decomposition partially unavailable

**Actor**: place_live (the `tokio::join!` telemetry branch, fail-open)
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; the order POST succeeds,
but the concurrent `get_orderbook` errors, times out, or returns a book whose ask is
filtered out.
**Trigger**: a signal-mode fire where the POST resolves but the telemetry GET does
not yield a usable ask.

### Primary Flow (Happy Path)
1. The order POST (priced/sized off `signal_entry`) fires; the telemetry GET runs
   concurrently (FR-7).
2. The GET fails (transport error / `error_for_status` / non-JSON) OR the derived ask
   is filtered out (not in `(0.50, 0.98]`) → the telemetry branch yields **no** ask
   (FR-8, INV-SA4).
3. The order outcome is determined **only** by the POST result (e.g. `filled`) — the
   order was **not** blocked or delayed by the fetch failure.
4. `place_live` appends the outcome row with `exec_entry = None`. `entry`,
   `first_limit_price` (signal-anchored), `fill`, `eff`, `fee`, etc. are still
   recorded; only `exec_entry` is absent.

**Postconditions** (AC-5, NFR-5): the fill is booked normally; `drift = exec_entry −
signal_entry` and `walk = eff − exec_entry` are **unavailable (not wrong)** for this
one fill because `exec_entry` is `None`. `gap = eff − signal_entry` remains computable
(does not need `exec_entry`). No fabricated or order-blocking value is written.

### Alternative Flows
- **UC-5-A1: GET succeeds but ask is filtered out** — the book returned but the
  side's ask is `≤ 0.50` or `> 0.98` (thin/locked book) → `exec_entry = None`, same
  as a transport failure. The telemetry filter is identical to the `ask`-mode derive
  filter (`main.rs` ≈ 1454).

### Error Flows
- **UC-5-E1: both POST and telemetry GET fail** — the outcome is `order_error`
  (from the POST `Err`, `S1-UC-6`) and `exec_entry = None`. The order row is written
  once; the loop does not crash (`S1-UC-13`). Decomposition is unavailable and there
  is no `eff` either (no fill) — only the context fields.

### Edge Cases
- **UC-5-EC1: fetch fails on a no-fill** — a signal-mode `nofill` row with
  `exec_entry = None`: neither `drift` nor `walk` nor `eff` exists (no fill, no ask)
  — the row is pure context. This is the maximally-degraded but still-valid row; QA
  asserts it parses and the loop continues.
- **UC-5-EC2: intermittent telemetry** — across a run, some fills have
  `exec_entry = Some` and some `None`; offline drift analysis simply skips the `None`
  rows. The population-level `drift` mean is computed over the `Some` subset (as in
  §4.1's 20-fill sample).

### Data Requirements
- **Input**: the POST result; the (failed/filtered) telemetry GET.
- **Output**: an outcome row with `exec_entry = None`; the order still completes.
- **Side Effects**: the real IOC executes on the POST alone; no order delay from the
  fetch (INV-SA4).
- **Traceability**: FR-7, FR-8; AC-5; NFR-5.

---

## UC-6: Signal-mode partial fill → `partial` outcome, latches, no re-quote for the remainder

**Actor**: place_live
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; the IOC crosses for
**part** of `count` at the signal-anchored limit (`fill > 0` and `remaining_count > 0`).
**Trigger**: a signal-mode fire where the ask has depth for only part of the order.

### Primary Flow (Happy Path)
1. The order POST at `signal_entry ± PRICE_BUF` fills `fill > 0` with
   `remaining_count > 0` → **partial** (`S1-UC-3`).
2. Because the re-quote is **disabled** in signal mode (FR-9), the remainder is
   **not** chased with a deeper IOC — `requote == false`, `requote_limit_price
   == None`. (In `ask` mode the re-quote only fires on a *zero* fill anyway, so a
   partial never re-quoted there either; signal mode makes the no-re-quote explicit
   for all outcomes.)
3. `place_live` appends one `partial` row (session `f1_d50cap75`) with
   `exec_entry = Some(...)` (if the concurrent GET succeeded, else `None` — UC-5),
   `fill`, `remaining_count`, `eff` (over the filled contracts), `fee`, `latency_ms`.
   `trades_today += 1`; pushes a `Pending` sized to the **filled** count.
4. `place_live` returns `partial`; signal_loop **latches** `fired_window` (partial is
   a confirmed fill — `S1-UC-3`, `S1-UC-7`). No further attempts this window.

**Postconditions** (AC-6): a partial is a confirmed position and latches; the
remainder is dropped (not chased), consistent with the anti-chase design (INV-SA3);
the `Pending` reflects only the filled contracts so the resolver scores the right
size.

### Alternative Flows
- **UC-6-A1: NO-side partial** — same, with the No price/side; still latches.

### Error Flows
- **UC-6-E1: append failure on the `partial` row** — `S1-UC-13`; no crash; latch
  still follows the returned `partial` outcome.

### Edge Cases
- **UC-6-EC1: `fill == count` exactly (full fill)** — `remaining_count == 0` → this
  is `filled` (UC-1), not `partial`. The boundary is `remaining_count > 0`.
- **UC-6-EC2: partial then window roll** — the partial latched the window; a roll
  starts a fresh window (`S1-UC-11`); no second position for the partially-filled
  window.

### Data Requirements
- **Input**: the POST `OrderResp` (`fill`, `remaining_count`); the concurrent GET.
- **Output**: one `partial` row (`live=true`); a `Pending` sized to `fill`.
- **Side Effects**: a real partial IOC on subaccount #1; `trades_today += 1`; latch
  consumed; remainder dropped.
- **Traceability**: FR-9; AC-6. Reuses `S1-UC-3`.

---

## UC-7: Order API error / reject in signal mode → same outcome rows as legacy, budget consumed, no double-latch

**Actor**: place_live + signal_loop
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; an order is attempted
but `create_ioc` returns `Err` (transport) or the response is non-201.
**Trigger**: a signal-mode fire where the POST fails at the API.

### Primary Flow (Happy Path)
1. **UC-7-A1: `order_error`** — `create_ioc` returns `Err` on the signal-anchored POST
   → one `order_error` row (session `f1_d50cap75`); an order **was** attempted → it
   consumes one bounded-retry attempt (`counts_as_attempt`); does **not** latch
   (`S1-UC-6`, `S2-UC-15`).
2. **UC-7-A2: `rejected`** — the IOC returns HTTP `!= 201` → one `rejected` row;
   consumes one attempt; does not latch. **No `-rq` re-quote** is issued (FR-9), so
   there is never a second `create_ioc` in signal mode.
3. In both cases `latch_decision(outcome)` is `false` (only fills latch), so the
   window remains eligible for the remaining retry budget (UC-4).

**Postconditions** (INV-SA3, INV-SA6): exactly one row per failed attempt; the
outcome rows are the **same shape** as legacy `ask` mode (FR-12) except `requote`
is always `false`; `trades_today` unchanged; the window is not latched by an error.

### Alternative Flows
- **UC-7-A3: error then fill on retry** — attempt 1 `order_error`/`rejected` (budget
  = 1 used), attempt 2 (after cooldown, same anchor) fills → latch consumed, one
  `filled` row. At most one filled position per window.

### Error Flows
- **UC-7-E1: both attempts error / reject** — budget exhausted at
  `RETRY_MAX_ATTEMPTS`; no fill; no latch; window not traded until roll (`S1-UC-8`).

### Edge Cases
- **UC-7-EC1: concurrent telemetry GET SUCCEEDS while the order fails** — because the
  telemetry fetch is independent of the POST (INV-SA4), an `order_error` / `rejected`
  row in signal mode may carry `exec_entry = Some(ask-at-order-time)` even though the
  order failed. This is **valid and useful**: it records the book at the failed-order
  instant. `drift` is computable (`exec_entry − signal_entry`) but there is **no
  `eff`** (no fill), so `walk`/`gap` are absent. QA notes this asymmetry (a failed
  order can still have a populated `exec_entry`).
- **UC-7-EC2: legacy `ask` mode comparison** — in `ask` mode the same paths record
  `requote` possibly `true`; signal-mode error rows are distinguishable by `requote
  == false` combined with the `first_limit_price`-vs-`entry` relation (see **UC-16**).

### Data Requirements
- **Input**: `create_ioc` `Err` / non-201; the concurrent GET; retry state.
- **Output**: one `order_error` / `rejected` `LiveTriggerRecord` (session
  `f1_d50cap75`), `requote == false`, `exec_entry` `Some`/`None` per the GET.
- **Side Effects**: attempt budget decremented; no latch; no `trades_today` change.
- **Traceability**: FR-9, FR-12. Reuses `S1-UC-6`, `S2-UC-15`.

---

## UC-8: Book fetch slower than the order POST → `join!` waits (bounded by the fetch timeout) — DESIGN QUESTION

**Actor**: place_live (`tokio::join!(order_post, book_get)`)
**Preconditions**: `EXEC_ANCHOR=signal`; the order POST resolves quickly (sub-second
RTT) but the concurrent `get_orderbook` is slow (network stall / slow endpoint).
**Trigger**: a signal-mode fire where the telemetry GET lags the POST.

### Primary Flow (Happy Path)
1. Both futures start together under `tokio::join!` (FR-7).
2. The order POST returns in an order RTT (`create_ioc` client timeout **10s**,
   `orders.rs` ≈ 79; steady-state RTT sub-second). The market order is **already
   placed and executed** at the exchange at this instant.
3. The book GET is still in flight. `tokio::join!` does **not** return until **both**
   futures complete, so `place_live` blocks until the GET finishes or **its client
   times out at 8s** (`rest.rs` ≈ 106).
4. When the GET resolves (success → `exec_entry = Some`; timeout/error → `exec_entry
   = None`, UC-5), the row is written and `place_live` returns.

**Postconditions**: the **order execution latency** is genuinely lower than `ask`
mode (the POST was not preceded by any GET — NFR-1 satisfied). BUT `place_live`'s
**wall-clock return** can extend up to the **8s `get_orderbook` timeout** past the
order RTT, delaying the ledger append, the latch decision, and the next 0.3s tick.

### FLAGGED DESIGN QUESTION (join wait bound vs order RTT)
- **Verified**: `get_orderbook`'s client timeout is **8s** (`rest.rs` ≈ 104-106) — an
  order of magnitude **greater** than the sub-second order RTT (`create_ioc` client
  10s). Per the task's check ("flag as a design question if timeout > order RTT"):
  **timeout (8s) ≫ order RTT → FLAG.** The `join!` **can** extend `place_live` well
  beyond the order RTT — bounded at 8s in the pathological case.
- **Blast radius (bounded, not a double-order)**: signal_loop is single-threaded and
  `await`s `place_live`, so a slow join **blocks** the loop for that fire; it cannot
  cause a second order (the latch/attempt semantics are intact, INV-SA6). The harm is
  (a) an up-to-8s stall in loop responsiveness for that fire and (b) possibly missing
  the window's *remaining* retry attempts if the stall eats the window.
- **Note the comparison to `ask` mode**: in `ask` mode the same 8s worst case exists
  but **before** the order (a hung GET delays the order itself). Signal mode is
  **strictly better** for order timing (the POST already fired); the residual is only
  the post-order return delay.
- **Options for the planner / architect** (decide before rollout):
  1. **GAP-FIXED (recommended)** — give the telemetry GET a **short bound** below the
     order RTT budget (e.g. wrap it in `tokio::time::timeout(Duration::from_millis(
     ~500), get_orderbook(...))`, or use a dedicated short-timeout client), so the
     join returns at ~`max(order_RTT, 500ms)` and a hung book yields `exec_entry =
     None` promptly (UC-5) instead of stalling 8s.
  2. **GAP-ACCEPTED** — written rationale that the orderbook endpoint is normally fast
     (~100-250ms, §4.1), an 8s stall is rare, and a single-fire stall neither double-
     orders nor corrupts state; monitored via `latency_ms` / ledger gaps.

### Alternative Flows
- **UC-8-A1: book GET faster than / equal to the POST** — the common case; the join
  returns at the POST RTT; no extra delay. `exec_entry = Some`.

### Error Flows
- **UC-8-E1: book GET hits its 8s timeout** — the GET future resolves `Err` at 8s →
  `exec_entry = None` (UC-5) and `place_live` returns then. The order outcome is
  unaffected (already determined by the POST).

### Edge Cases
- **UC-8-EC1: both slow** — if the POST is also slow (near its 10s timeout) the join
  is bounded by the **larger** of the two client timeouts (10s POST). This is a
  network-pathology tail, not the target case; documented for completeness.

### Data Requirements
- **Input**: the two futures' completion times; the `get_orderbook` (8s) and
  `create_ioc` (10s) client timeouts.
- **Output**: one outcome row after the join; `exec_entry` per the GET; a possibly-
  delayed `place_live` return.
- **Side Effects**: the order executes at POST time; the loop blocks until the join
  completes (≤ the larger client timeout).
- **Traceability**: FR-6, FR-7; NFR-1. **Design flag → §4 audit / planner.**

---

## UC-9: `PRICE_BUF` misconfigured (0 / negative / huge) → clamp behavior of the signal-anchored limit

**Actor**: place_live (limit computation) + the `.min(0.99)` / `.max(0.01)` clamp
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; `PRICE_BUF` set to an
extreme value via env (`lcfg.price_buf`, `main.rs` ≈ 541). `PRICE_BUF` is **not
validated** by this feature (§4.9 keeps it an unchanged env).
**Trigger**: a signal-mode fire priced with the extreme buffer.

### Primary Flow (Happy Path — the sub-cases)
1. **UC-9-A1: `PRICE_BUF = 0`** — Yes limit = `(signal_entry + 0).min(0.99) =
   signal_entry`; No limit = `((1 − signal_entry) − 0).max(0.01) = 1 − signal_entry`.
   The IOC crosses only if the ask is **at or below** the signal price → **many more
   no-fills** (this is exactly the tight-cap regime §4.1 showed is WORSE — it cuts
   winners). Behavior is well-defined; the retry (UC-4) applies.
2. **UC-9-A2: `PRICE_BUF < 0` (negative)** — Yes limit = `(signal_entry + neg)` <
   `signal_entry` → an even tighter (below-signal) limit → near-certain no-fill for
   Yes. No limit = `((1 − signal_entry) − neg)` = *higher* → looser for No. Asymmetric
   but well-defined; no panic (NFR-2). Almost certainly no-fills the intended side.
3. **UC-9-A3: `PRICE_BUF` huge (e.g. 0.50)** — Yes limit = `(signal_entry +
   0.50).min(0.99)` → **clamps at 0.99** for any `signal_entry ≥ 0.49`; the fill can
   land at up to 0.99 (far above the `0.92` cap intent). No limit = `((1 −
   signal_entry) − 0.50).max(0.01)`. The `.min(0.99)` / `.max(0.01)` clamp is the
   **only** guard — it prevents out-of-range API values but does NOT enforce the
   `0.92` cap.

**Postconditions**: the limit math never panics and is always in `[0.01, 0.99]`
(INV-SA5); but an extreme `PRICE_BUF` degrades execution (0/negative → no-fills; huge
→ pays up to 0.99). The prod value is `0.06` and is the measured sweet spot (§4.1);
the clamp is a safety net, not a policy.

### Alternative Flows
- **UC-9-A4: `PRICE_BUF` such that the Yes limit exactly hits 0.99** — e.g.
  `signal_entry = 0.92`, `PRICE_BUF = 0.07` → `0.99.min(0.99) = 0.99`; the clamp binds
  exactly at the API ceiling.

### Error Flows
- **UC-9-E1: `PRICE_BUF` non-numeric / unparseable in env** — handled by the existing
  `LiveCfg` env parse (unchanged by this feature); a bad value falls back to the
  `LiveCfg` default, not to a panic. (This is §1/§2 env-parse behavior, not new here.)

### Edge Cases
- **UC-9-EC1: the clamp is normally inert (prod values)** — with `PRICE_BUF = 0.06`
  and `signal_entry ≤ 0.92`, the Yes limit ≤ `0.98 < 0.99`, so `.min(0.99)` does
  **not** bind (FR-4/FR-9). The clamp only engages under misconfiguration (A3/A4).
- **UC-9-EC2: FLAG — should `PRICE_BUF` be range-validated?** This feature keeps
  `PRICE_BUF` an unvalidated env (§4.9). Whether to add a startup range check (e.g.
  warn/refuse if `PRICE_BUF ∉ [0.01, 0.15]`) is a **planner decision**; today only the
  per-order clamp guards it. Flagged for QA/architect; not required by §4.

### Data Requirements
- **Input**: `lcfg.price_buf` (extreme); `signal_entry`; `side`.
- **Output**: a clamped, in-range limit; well-defined fill/no-fill behavior.
- **Side Effects**: degraded execution under misconfiguration; no crash.
- **Traceability**: FR-4, FR-5, FR-11; §4.9; §4.10 (buffer risk).

---

## UC-10: Signal entry at the band edge `0.92` → Yes limit `0.98` exactly (hard vs soft cap tension)

**Actor**: place_live (band gate + limit computation)
**Preconditions**: `EXEC_ANCHOR=signal`; `MAX_ENTRY=0.92`; `PRICE_BUF=0.06`;
`signal_entry == 0.92` exactly (the band is inclusive at the cap — `S2-UC-17-A4`).
**Trigger**: a signal-mode fire at the exact band edge.

### Primary Flow (Happy Path)
1. The band gate `0.50 < signal_entry ≤ 0.92` **passes** at exactly `0.92` (inclusive
   at the cap; `S2-UC-17-A4`, `main.rs` ≈ 1438-1442). The order proceeds.
2. Yes limit = `(0.92 + 0.06).min(0.99) = 0.98` **exactly** — within the Kalshi range,
   the `.min(0.99)` clamp does **not** bind (FR-9/FR-11). No side: `((1 − 0.92) −
   0.06).max(0.01) = (0.08 − 0.06).max(0.01) = 0.02`.
3. `count = round(5 / 0.92) = round(5.43) = 5`, clamped `[1, 15]` → 5.
4. `create_ioc` formats the limit to cents: `format!("{:.2}", 0.98) = "0.98"` (FR-6).

**Postconditions** (INV-SA7): the maximum Yes limit reachable in signal mode is
`0.98` (at the `0.92` band edge). The IOC fills at the ask **≤ 0.98**, so `eff ≤ 0.98`
(the HARD bound).

### FLAGGED TENSION — hard bound (`≤ 0.98`) vs AC-7's soft claim (`≤ 0.92`)
- **Hard invariant (INV-SA7)**: `eff ≤ signal_entry + PRICE_BUF`. At `signal_entry =
  0.92` this is `eff ≤ 0.98`, NOT `≤ 0.92`. A fill CAN land in `(0.92, 0.98]` when
  `signal_entry` is near the cap **and** the ask is above `0.92`.
- **Soft claim (AC-7, §4.5)**: "no fill lands above the `0.92` signal cap." This holds
  **empirically** because (a) most F1 signal entries sit well below `0.86` (so
  `signal_entry + 0.06 < 0.92`), and (b) signal-anchor removes the +3.79c drift that
  produced the pre-feature `4/20` breaches (0.927-0.983). It is **not** a limit-math
  invariant for entries in `(0.86, 0.92]`.
- **QA guidance**: E2E MUST assert the HARD bound `eff ≤ signal_entry + PRICE_BUF`
  (always true) and treat AC-7's `≤ 0.92` as a **population expectation** measured
  over real fills (the breach rate should collapse from 4/20 toward ~0, dominated by
  entries below 0.86), **not** as a per-fill guarantee. Flag for `qa-planner`.

### Alternative Flows
- **UC-10-A1: `signal_entry` just above `0.92`** — `0.9201` → band gate `> 0.92` →
  **`skip_band`**, no order (`S2-UC-7-A1`). So `0.92` is the highest entry that can
  reach the `0.98` Yes limit; nothing above the cap ever prices an order.

### Error Flows
- **UC-10-E1: `MAX_ENTRY` misconfigured higher (e.g. 0.97)** — the band would admit
  `signal_entry` up to 0.97 → Yes limit `(0.97 + 0.06).min(0.99) = 0.99` (clamp
  binds) → `eff ≤ 0.99`. Documented so a mis-set `MAX_ENTRY` is understood to widen
  the hard bound; prod keeps `MAX_ENTRY = 0.92` (§2 FR-9).

### Edge Cases
- **UC-10-EC1: `signal_entry == 0.50` exactly** — the band floor is exclusive
  (`0.50 < signal_entry`), so `0.50` is **rejected** (`skip_band` / no-price path,
  `S2-UC-17-A4`). The lowest admitted entry is just above `0.50`, giving a Yes limit
  just above `0.56`.

### Data Requirements
- **Input**: `signal_entry = 0.92`, `PRICE_BUF = 0.06`, `MAX_ENTRY = 0.92`, `stake = 5`.
- **Output**: Yes limit `0.98` (`"0.98"`), No limit `0.02`, `count = 5`; a fill with
  `eff ≤ 0.98`.
- **Side Effects**: one IOC at the band edge; no clamp binding at prod values.
- **Traceability**: FR-4, FR-6, FR-9, FR-11; AC-7 (soft); INV-SA7 (hard). **QA flag.**

---

## UC-11: `EXEC_ANCHOR=signal` with f6 selected (`SESSION` unset) → valid orthogonal combo, anchor still correct

**Actor**: main()/boot + place_live
**Preconditions**: `SESSION` unset (→ f6_wait270, `S2-UC-2`) AND `EXEC_ANCHOR=signal`.
`EXEC_ANCHOR` (order anchor) and `SESSION` (strategy gate) are **independent** env
vars.
**Trigger**: a f6 fire while `EXEC_ANCHOR=signal`.

### Primary Flow (Happy Path)
1. `main()` selects `SessionConfig::f6_wait270()` (f6 gate: 270s wait, Δ≥20, p≥0.60,
   max30) AND `ExecAnchor::Signal`. The two selectors do not interact.
2. `place_live` anchors the IOC at the **f6 signal entry** (`fire.entry` for f6) —
   `price = (f6_signal_entry + PRICE_BUF).min(0.99)`, `count = round(stake /
   f6_signal_entry)`. The anchor logic is strategy-agnostic; `fire.entry` is
   paper-1:1 for f6 too.
3. Since f6 enters at **270s** (LESS momentum than F1's 180s), the pre-order drift is
   structurally **smaller**, so the signal anchor is still correct and arguably even
   safer for f6 (less ask run-away to begin with).

**Postconditions**: the combo is **valid** — f6 gate + signal-anchored execution. No
code error, no cross-contamination. The band gate uses `cfg.max_entry_price` (f6's
cap, which may differ from `0.92`); the `.min(0.99)` clamp still guards (INV-SA5).

### FLAGGED SCOPE NOTE
- §4.9 marks "applying `EXEC_ANCHOR=signal` to the **f6 default** ... as a default" as
  **out of scope** — meaning `signal` is not turned ON automatically for f6; the env
  stays **opt-in**. It does NOT forbid an operator from manually pairing f6 +
  `signal`. This UC documents that manual pairing as **structurally sound but
  untested** (§4's measured motivation and buffer counterfactual are the 20 **F1**
  fills; f6 + signal has no equivalent measurement). Recommended prod pairing remains
  `SESSION=f1_d50cap75` + `EXEC_ANCHOR=signal` (§4.3 FR-13).

### Alternative Flows
- **UC-11-A1: f6 + `EXEC_ANCHOR=ask` (unset)** — the current f6 prod baseline, wholly
  unchanged (UC-2-EC1).

### Error Flows
- **UC-11-E1: f6 band cap differs from 0.92** — if f6's `cfg.max_entry_price` >
  `0.92`, the f6 signal Yes limit `f6_signal + PRICE_BUF` can exceed `0.98`; the
  `.min(0.99)` clamp still bounds it. QA verifies the f6 cap value if this combo is
  ever run.

### Edge Cases
- **UC-11-EC1: unrecognized `SESSION` + `EXEC_ANCHOR=signal`** — `SESSION` fails loud
  and falls back to f6 (`S2-UC-10`) while `EXEC_ANCHOR=signal` still selects `Signal`;
  the two fail/select independently → f6 gate + signal execution, with the `SESSION`
  warning logged.

### Data Requirements
- **Input**: `SESSION` unset, `EXEC_ANCHOR=signal`; f6 `fire.entry`.
- **Output**: signal-anchored IOC off the f6 signal entry; f6-tagged telemetry.
- **Side Effects**: valid live orders under f6 gate + signal anchor (if
  `LIVE_TRADING=1`).
- **Traceability**: FR-3, FR-4; §4.9 (opt-in). Orthogonality of `SESSION` / `EXEC_ANCHOR`.

---

## UC-12: Count sizing shift — `round(stake/signal_entry)` vs legacy `round(stake/exec_entry)` → one extra contract near boundaries, `MAX_COUNT` cap

**Actor**: place_live (sizing)
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; `signal_entry <
exec_entry` (the usual case — mean drift +3.79c, §4.1).
**Trigger**: a signal-mode fire where the round() of the two denominators differ.

### Primary Flow (Happy Path)
1. Signal mode sizes `count = round(stake / signal_entry)`; legacy sizes
   `round(stake / exec_entry)` (FR-5). Because `signal_entry ≤ exec_entry`
   (drift ≥ 0), the signal denominator is **smaller**, so signal-mode `count ≥ legacy
   count`.
2. Near a rounding boundary this is **one extra contract**. Worked example
   (`stake = 5`): `signal_entry = 0.90` → `5/0.90 = 5.56 → round 6`; `exec_entry =
   0.93` → `5/0.93 = 5.38 → round 5`. Signal mode buys **6** vs legacy **5** — one
   more contract, slightly more `$5`-relative exposure.
3. The `[1, max_count]` clamp (`MAX_COUNT` default 15) is **unchanged** and bounds the
   exposure (FR-5).

**Postconditions** (§4.10 sizing risk): signal mode buys slightly **more** contracts
per `$5` than legacy; the delta is small at F1 entries (`0.5-0.92`) and stays well
under the `MAX_COUNT = 15` cap.

### Alternative Flows
- **UC-12-A1: no boundary crossed** — when `round(5/signal) == round(5/exec)` (drift
  too small to flip the rounding), the count is identical to legacy; the shift is a
  near-boundary phenomenon, not universal.

### Error Flows
- **UC-12-E1: `MAX_COUNT` misconfigured to 1** — the clamp forces `count = 1` for any
  entry; both modes size 1; the shift disappears (clamped). Documented; prod keeps the
  default 15.

### Edge Cases
- **UC-12-EC1: `MAX_COUNT` cap binds at very low `signal_entry`** — `signal_entry`
  just above `0.50` → `5/0.5001 = 9.998 → round 10`; still `< 15`. To hit the cap you
  need `signal_entry < ~0.34` (`5/0.34 = 14.7 → 15`), which the `(0.50, 0.92]` band
  **excludes**. So within the F1 band the cap **never binds** — count ranges ~5
  (`5/0.92`) to ~10 (`5/0.50+`). The cap is a belt-and-suspenders bound, not an active
  constraint for F1.
- **UC-12-EC2: floor `[1, .]`** — `round(5/0.92) = 5 ≥ 1`; the floor never binds in
  the F1 band either.

### Data Requirements
- **Input**: `stake`, `signal_entry`, `exec_entry`, `max_count`.
- **Output**: `count = round(stake/signal_entry)` clamped `[1, 15]`; recorded on the row.
- **Side Effects**: slightly larger contract count than legacy near boundaries;
  bounded by `MAX_COUNT`.
- **Traceability**: FR-5; §4.10 (sizing shift).

---

## UC-13: Signal-anchored limit rounding to cents (`format!("{:.2}")`) → unchanged formatter, same rounding as legacy

**Actor**: place_live (limit float) → create_ioc (`orders.rs` ≈ 115)
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; `signal_entry +
PRICE_BUF` is not an exact cent (e.g. `0.855 + 0.06 = 0.915`).
**Trigger**: a signal-mode fire whose anchored float needs cents rounding.

### Primary Flow (Happy Path)
1. `place_live` computes the Yes limit `signal_entry + PRICE_BUF` (or the No formula)
   as an `f64`; this float is passed to `create_ioc`, which rounds it to cents with
   the **existing** `format!("{:.2}", price)` (FR-6, unchanged).
2. The rounding is identical to `ask` mode — the only difference is **which float**
   is rounded (a signal-anchored value vs an exec-anchored value); the formatter and
   its behavior are the same.
3. Kalshi receives the cents-rounded price; the IOC fills at the ask ≤ the cents-
   rounded limit, so `eff ≤ rounded_limit`.

**Postconditions** (AC-3): the limit Kalshi sees is `format!("{:.2}", signal_entry ±
PRICE_BUF)`; no new rounding path is introduced (INV-SA5). `count` is likewise
formatted `{:.2}` (`orders.rs` ≈ 114), unchanged.

### Alternative Flows
- **UC-13-A1: No-side rounding** — `((1 − signal_entry) − PRICE_BUF).max(0.01)` is
  rounded the same way; e.g. `signal_entry = 0.855` → No limit `0.145 − 0.06 = 0.085
  → "0.09"` (per the formatter's rounding of the f64 representation).

### Error Flows
- **UC-13-E1: none specific** — cents rounding cannot fail; a value already clamped to
  `[0.01, 0.99]` (INV-SA5) always formats to a valid 2-decimal string.

### Edge Cases
- **UC-13-EC1: tie / float-representation rounding** — a "half-cent" nominal like
  `0.915` is stored as the nearest `f64` (`0.9149999...`), so `format!("{:.2}", .)`
  yields `"0.91"`, not `"0.92"`. This is the **same** representation behavior as
  legacy `ask` mode (no regression); QA asserts the cents-rounded limit is what bounds
  `eff`, and does NOT assume exact half-cent-up rounding.
- **UC-13-EC2: already-exact cents** — `signal_entry = 0.86`, `PRICE_BUF = 0.06` →
  `0.92 → "0.92"`; no rounding artifact.

### Data Requirements
- **Input**: the signal-anchored `f64` limit; the `{:.2}` formatter.
- **Output**: a 2-decimal price string sent to Kalshi; `eff ≤` that value.
- **Side Effects**: none beyond formatting; identical to legacy.
- **Traceability**: FR-6; AC-3.

---

## UC-14: `eff` BETTER than the signal anchor (negative drift) → favorable fill kept; equal-entry case loads S3 resolve

**Actor**: place_live (fill) + telemetry + resolver
**Preconditions**: `EXEC_ANCHOR=signal`; live preconditions; the ask at order time is
**at or below** `signal_entry` (book moved favorably, or the signal was conservative).
**Trigger**: a signal-mode fire that fills below the signal price.

### Primary Flow (Happy Path)
1. The IOC (limit `signal_entry + PRICE_BUF`) crosses immediately; the fill executes
   at the **market ask**, which is ≤ `signal_entry` → `eff ≤ signal_entry`.
2. If the concurrent GET succeeded, `exec_entry = Some(ask-at-order-time) ≤
   signal_entry` → `drift = exec_entry − signal_entry` is **negative** (a favorable
   book) and `walk = eff − exec_entry ≈ 0` (execution vs the ask is ~free, as the
   §4.1 mean `walk = −0.14c` shows).
3. The favorable fill is **kept** — no special handling; the row records the better
   `eff`. This is the good tail the signal anchor preserves (the IOC still executes at
   the best available price ≤ limit).

**Postconditions** (AC-5, NFR-5): a fill better than the signal price is booked and
correctly decomposed (negative `drift`); the telemetry never rewrites `eff` up to the
anchor — real money is recorded as-filled.

### Alternative Flows
- **UC-14-A1: NO-side favorable fill** — the No ask undercuts the No limit; same
  favorable outcome on the No side.

### Error Flows
- **UC-14-E1: favorable fill but telemetry GET failed** — `eff < signal_entry` is
  recorded, but `exec_entry = None` (UC-5) → `drift` unavailable; the favorable `gap =
  eff − signal_entry < 0` is still computable without `exec_entry`.

### Edge Cases
- **UC-14-EC1: `eff == signal_entry` (zero drift) — loads S3 resolve disambiguation**
  — signal-anchor makes `eff == signal_entry` **much more common** than in `ask` mode
  (that convergence is the whole point). When the live fill's `entry` (= `eff`) equals
  the shadow twin's `entry` (= `signal_entry`), the §3 resolver relies on the `live`
  flag — **not** the entry value — to attach the right (`$5` live vs `$100` twin) PnL
  (`S3-UC-5`, §3 FR-7). **QA flag**: this feature *increases* the rate of equal-entry
  windows, so `S3-UC-5`'s equal-entry resolve test is now **load-bearing** for
  signal-mode fills and MUST be exercised against signal-anchored data.

### Data Requirements
- **Input**: a market ask ≤ `signal_entry`; the concurrent GET; the `OrderResp`.
- **Output**: a `filled`/`partial` row with `eff ≤ signal_entry`, negative/zero
  `drift`; a `Pending`/`TrigSummary` (`live=true`) whose `entry` may equal the twin's.
- **Side Effects**: a favorable real fill; the S3 live/shadow resolve split exercised
  at equal entries.
- **Traceability**: FR-8; AC-5; NFR-5. Cross-refs `S3-UC-5`, §3 FR-7. **QA flag.**

---

## UC-15: `requote` fields in signal-mode records → `requote == false` and `requote_limit_price == None` on EVERY outcome

**Actor**: place_live (record construction)
**Preconditions**: `EXEC_ANCHOR=signal`; any outcome (filled / partial / nofill /
rejected / order_error).
**Trigger**: `place_live` builds the `LiveTriggerRecord` for a signal-mode fire.

### Primary Flow (Happy Path)
1. For **every** signal-mode outcome, the recorded `requote == false` and
   `requote_limit_price` is **absent** (`None`) (FR-9, AC-6). The deeper re-quote
   branch (`main.rs` ≈ 1491-1513) is **not entered** in signal mode.
2. No second `create_ioc` with a `-rq` client-order-id is ever issued in signal mode
   (§4.6). The `first_limit_price` (the signal-anchored limit) is the only limit sent.

**Postconditions** (AC-6): signal-mode rows are uniformly `requote == false`; any row
with `requote == true` is therefore **definitely** an `ask`-mode row (a useful
mode-discriminator for offline analysis — see UC-16).

### Alternative Flows
- **UC-15-A1: `ask` mode contrast** — in `ask` mode `requote` can be `true` with a
  populated `requote_limit_price` (`S1-UC-4`); this is the field's legacy meaning and
  is unchanged for `ask`.

### Error Flows
- **UC-15-E1: none** — `requote == false` is unconditional in signal mode; there is no
  path that sets it true.

### Edge Cases
- **UC-15-EC1: retry vs re-quote (do NOT conflate)** — a signal-mode **P0b retry**
  (UC-4) is a **separate `place_live` invocation** gated by cooldown, producing a
  **distinct row** that ALSO has `requote == false`. It is NOT a "re-quote" (which was
  a second IOC **within one** `place_live` call, at a *deeper* price). Both re-quote
  (intra-call, deeper) and the retry (inter-call, same anchor) are distinct
  mechanisms; signal mode disables the former and keeps the latter at a fixed anchor.
  QA must not read a two-row window (two retry attempts) as a re-quote.

### Data Requirements
- **Input**: the signal-mode outcome; the (disabled) re-quote branch.
- **Output**: `requote == false`, `requote_limit_price == None` on the row.
- **Side Effects**: exactly one `create_ioc` per `place_live` invocation in signal mode.
- **Traceability**: FR-9, FR-10; AC-6. Contrast `S1-UC-4`.

---

## UC-16: Mixed-mode ledger analysis → are ask-mode and signal-mode rows distinguishable? (offline segmentation — QA flag)

**Actor**: offline analyst / resolver / dashboard loader
**Preconditions**: the append-only ledger contains rows from BOTH an `ask`-mode period
(pre-rollout) and a `signal`-mode period (post-`EXEC_ANCHOR=signal`). No new field
records the anchor mode (§4.7 — no schema change).
**Trigger**: an offline slippage analysis (or the §1 drift/walk decomposition) over a
mixed file.

### Primary Flow (Happy Path)
1. Both modes write the **same** `LiveTriggerRecord` shape (FR-12, §4.7); the loaders
   (resolver, dashboard) parse both without error (`S1-UC-12`, additive-JSON).
2. `first_limit_price` **semantics differ** by mode:
   - `signal` mode: `first_limit_price = signal_entry ± PRICE_BUF` — i.e.
     `first_limit_price − entry ≈ ±PRICE_BUF`, **independent of `exec_entry`**.
   - `ask` mode: `first_limit_price = exec_entry ± PRICE_BUF` — i.e.
     `first_limit_price − exec_entry ≈ ±PRICE_BUF`, related to the drifted `exec_entry`,
     **not** `entry`.
3. **Discriminators an analyst can use** (in decreasing strength):
   - `requote == true` ⟹ **definitely `ask` mode** (signal mode is always `false`,
     UC-15). (`requote == false` is inconclusive — both modes produce it.)
   - When `exec_entry` is present and `drift = exec_entry − entry ≠ 0`: check which
     relation holds — `first_limit_price − entry == ±PRICE_BUF` (signal) vs
     `first_limit_price − exec_entry == ±PRICE_BUF` (ask). When `drift ≈ 0` the two
     relations coincide and the row is **anchor-ambiguous** (but analytically
     equivalent — same limit either way).
   - The **rollout timestamp** (`ts`): the operator knows the exact instant
     `EXEC_ANCHOR=signal` was deployed (§4.3 FR-13); rows before it are `ask`, after
     are `signal`. This is the **most reliable** segmentation.

**Postconditions**: mixed-mode rows are parseable and, in the common drift≠0 case,
**derivably** attributable to a mode via `first_limit_price`-vs-`entry`-vs-`exec_entry`
plus the `requote` flag; the deploy timestamp is the authoritative boundary.

### FLAGGED FOR QA
- There is **no explicit `anchor` field** (§4.7 forbids a new field). If downstream
  analysis needs hard per-row mode attribution, it MUST rely on the **rollout
  timestamp** (primary) and the `first_limit_price` relation + `requote` flag
  (secondary derivation). QA/analysts should NOT assume a self-describing row.
  Recommend the runbook record the exact `EXEC_ANCHOR=signal` deploy `ts` so the
  boundary is unambiguous. (A future additive `anchor` tag is a possible follow-up but
  is **out of scope** here — §4.7.)

### Alternative Flows
- **UC-16-A1: all-signal or all-ask file** — a single-mode file needs no
  discrimination; every row is the deployed mode. The mixed case only arises across a
  rollout/rollback boundary.

### Error Flows
- **UC-16-E1: a `drift ≈ 0` ask-mode row misread as signal (or vice versa)** — when
  `exec_entry ≈ entry`, the two relations are indistinguishable; a naive relation-based
  classifier can mislabel. Mitigation: fall back to the deploy timestamp; the
  mislabel is analytically harmless (the limit and `eff` are the same either way).

### Edge Cases
- **UC-16-EC1: resolver / dashboard don't care about anchor mode** — they consume
  `eff`, the `live` flag (§3), and resolved PnL — all **anchor-agnostic**. Only offline
  drift/slippage segmentation needs the ask-vs-signal split; runtime scoring and the
  LIVE line render identically regardless of anchor (§4.8, UI unchanged).
- **UC-16-EC2: rows with `exec_entry == None` (UC-5)** — carry no `exec_entry`
  relation, so the `first_limit_price`-vs-`exec_entry` discriminator is unavailable;
  rely on `requote` + timestamp for those rows.

### Data Requirements
- **Input**: a mixed `LiveTriggerRecord` JSONL file; the `EXEC_ANCHOR=signal` deploy
  timestamp.
- **Output**: per-row mode attribution (by timestamp, or derived from
  `first_limit_price`/`requote`); an unchanged parse (no errors).
- **Side Effects**: none (offline analysis); no ledger rewrite (append-only).
- **Traceability**: FR-12; §4.7 (no new field); §4.8 (UI unchanged). Reuses
  `S1-UC-12`. **QA flag.**

---

## Flow counts

- **Primary flows (one per use case, UC-1 … UC-16):** 16
- **Alternative flows (UC-N-Ax):** 20
  (UC-1: 2, UC-2: 2, UC-3: 1, UC-4: 2, UC-5: 1, UC-6: 1, UC-7: 1, UC-8: 1, UC-9: 1,
  UC-10: 1, UC-11: 1, UC-12: 1, UC-13: 1, UC-14: 1, UC-15: 1, UC-16: 1)
- **Error flows (UC-N-Ex):** 16
  (one per use case, UC-1-E1 … UC-16-E1)
- **Edge cases (UC-N-ECx):** 24
  (UC-1: 2, UC-2: 1, UC-3: 1, UC-4: 2, UC-5: 2, UC-6: 2, UC-7: 2, UC-8: 1, UC-9: 2,
  UC-10: 1, UC-11: 1, UC-12: 2, UC-13: 2, UC-14: 1, UC-15: 1, UC-16: 2)

Note: UC-7-A1/A2, UC-9-A1..A3 are lettered **sub-cases inside a primary flow** (an
enumerated set of variants), counted where they appear.

**Total scenarios:** 76 (16 primary + 20 alternative + 16 error + 24 edge).

## Traceability (for QA / E2E)

Each row must be green before / after the EU rollout (`EXEC_ANCHOR=signal`):

| Requirement | UC | PRD 4.x |
|-------------|----|---------|
| `EXEC_ANCHOR` selector pure + fail-loud + `ask` fallback | UC-3 | FR-1, FR-2; AC-2; NFR-2 |
| Default (`ask`/unset) byte-identical legacy flow | UC-2 | FR-3; AC-1, AC-8; NFR-4 |
| Signal-mode limit `signal_entry ± PRICE_BUF` (unit) | UC-1, UC-10, UC-13 | FR-4; AC-3 |
| Signal-mode sizing `round(stake/signal_entry)` (unit) | UC-1, UC-12 | FR-5; AC-3 |
| No pre-order book GET in signal mode (code-verified) | UC-1, UC-8 | FR-6; AC-4; NFR-1 |
| Concurrent telemetry GET → `exec_entry` Some/None | UC-1, UC-5 | FR-7, FR-8; AC-5; NFR-5 |
| Re-quote disabled; `requote == false` always | UC-6, UC-7, UC-15 | FR-9; AC-6 |
| P0b retry re-anchors at the SAME fixed price | UC-4 | FR-10; AC-6 |
| Band gate + `.min(0.99)`/`.max(0.01)` + cents unchanged | UC-9, UC-10, UC-13 | FR-11; AC-3 |
| §1/§2/§3 rails + record shapes unchanged; 75 tests green | UC-2, UC-16 | FR-12; AC-8 |
| Deployed fills: `eff ≤ signal_entry + 0.06`, drift-tail gone | UC-1, UC-10 | AC-7 |
| Lower order-POST latency in signal mode | UC-1, UC-8 | NFR-1 |

### Open design questions flagged for planner / architect / QA

1. **`join!` wait bound (UC-8)** — `get_orderbook` client timeout is **8s** ≫
   sub-second order RTT; `tokio::join!` can stall `place_live`'s return up to 8s
   post-order. Decide GAP-FIXED (bound the telemetry GET with a short
   `tokio::time::timeout`, ~500ms) vs GAP-ACCEPTED (rare, single-fire stall, no
   double-order).
2. **Hard vs soft cap (UC-10)** — INV-SA7 guarantees `eff ≤ signal_entry + PRICE_BUF`
   (`≤ 0.98` at the band edge), NOT AC-7's `≤ 0.92`. E2E must assert the hard bound
   per-fill and treat `≤ 0.92` as a population expectation.
3. **`PRICE_BUF` validation (UC-9-EC2)** — no startup range check exists; decide
   whether to add one (out of scope per §4.9, flagged only).
4. **Mode segmentation (UC-16)** — no explicit `anchor` field; rely on the deploy
   timestamp + `first_limit_price`/`requote` derivation. Record the deploy `ts` in the
   runbook.
5. **Equal-entry resolve load (UC-14-EC1)** — signal-anchor raises the rate of
   `eff == signal_entry`, making `S3-UC-5`'s equal-entry live/shadow resolve
   disambiguation load-bearing; exercise it against signal-mode data.
