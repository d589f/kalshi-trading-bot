# Implementation Plan — dashboard-f1-live-line (PRD §3)

Split the mislabelled green "shadow/LIVE" series: green = strategy shadow twin (twin5), NEW line = real $5 LIVE fills anchored at pink paper-F1 cumulative before first live window. Table US column -> LIVE. Single `live: bool` flag threads it all.

## Deliverables (done 2026-07-05)
- [x] PRD §3 (FR-1..18, AC-1..9) — incl. FR-9 ResolveRecord.live (PM catch: replay disambiguation needs it)
- [x] Use cases: docs/use-cases/dashboard-f1-live-line_use_cases.md — 15 UC / 85 scenarios, INV-D1..D9
- [x] Architecture review PASS — 5 binding resolutions + 5 action items (below)
- [x] QA: docs/qa/dashboard-f1-live-line_test_cases.md — all scenarios mapped, go-live gate table

## Architect binding resolutions
1. Twin double-emit on no-fill mid-window restart: ACCEPT duplicate (compare() first-find dedupes; only com_positions +1). Filled-window restart already protected: twin sits inside `if !already` (fired_window seeded from persisted last_filled_window).
2. Anchor: pink f1_twin cumulative STRICTLY BEFORE first visible lv window, else 0; client-side, recompute per refresh; on 300-row scroll both re-baseline together (relative gap preserved); never NaN.
3. match_lv denominator = windows with BOTH lv_side and f1_side; card copy states it explicitly.
4. Twin emit: inside live branch, inside `if !already && !book.ticker.is_empty()`, gated ONLY by twin_window latch (sibling of attempt_window, reset on window roll), decoupled from retry_gate, BEFORE place_live. Never touches fired_window/attempt_*/order path.
5. TrigSummary.live #[serde(default)]: lenient on ABSENT (→false), STRICT on wrong type (element parse fails → 400 keeps last good feed).
Action items: (2) FR-9 + resolve(live) + both call sites + producer in SAME binary/deploy; (3) resolver retain gains x.live == pd.live (flag-blind retain = latent stranding bug at eff==signal_entry); (4) dashboard.rs gets its FIRST #[cfg(test)] module w/ 2 merge blockers; (5) label copy: real-$5 vs modeled twin5 rounding offset expected even at zero slippage.

## Slices

### S1 (Wave 1): schema flag + resolve disambiguation + producer/replay plumbing [SECURITY pre-review]
Files: dashboard.rs, ledger.rs, main.rs (compile-coupled: exhaustive struct literals).
- TrigSummary += `#[serde(default)] pub live: bool`.
- Dash::resolve(.., live: bool) — predicate += `t.live == live`. Both call sites: replay (~362), resolver (~874).
- ledger.rs ResolveRecord += `pub live: bool`; resolver writes pd.live.
- Resolver retain predicate += `x.live == pd.live`.
- main.rs literals: emit_trigger TrigSummary live:false; place_live TrigSummary live:true; replay TrigSummary live = v["live"].as_bool().unwrap_or(false); replay resolve passes same.
- NEW dashboard.rs #[cfg(test)]: (a) no live key → false; wrong-type live → Vec parse err; (b) equal-entry twin+live resolve disambiguation BOTH orders; only-live no-op; two-same-flag first-match break. main.rs tests: replay flag derivation, mixed-JSONL reconstruct + dual resolve @ eff==signal_entry, retain hardening.
Done when: all above unit tests pass; cargo build ok.

### S2 (Wave 2): Buffalo consumer split — compare() lv_* + body_json aggregates
Files: dashboard.rs.
- compare(): two finds — c = first !t.live match, lv = first t.live match. com_* ONLY from c. NEW lv_side/lv_entry/lv_delta/lv_pnl/lv_result/lv_won/lv_count/lv_p from lv. match_lv (lv vs f1_side, both-present denominator). us_*/com_* JSON retained (US leaves UI only). truncate(300) unchanged.
- body_json summary += lv_match/lv_total/lv_pct/lv_positions (live rows in period, resolved or not)/lv_pnl (raw sum of resolved).
- Tests: exclusive split (both/live-only/shadow-only windows), match_lv denominator, first-match, unresolved lv row (lv_pnl null but side/entry set), all-shadow feed → lv_* all null/0, aggregates.

### S3 (Wave 2, parallel w/ S2): EU producer twin emit + twin_window latch [SECURITY pre-review]
Files: main.rs.
- `let mut twin_window: Option<String> = None;` sibling of attempt_window; reset on window roll.
- In live branch, before retry_gate/place_live, decoupled from retry_gate: if twin_window != win_key { twin_window = win_key; emit_trigger(...); }. Inside `if !already` (inherits filled-restart dedupe). Non-live else unchanged.
- Extract testable helper twin_should_emit(twin_window, win_key) -> bool.
- Tests: once per window across ticks/retries/no-fills/skips; doesn't mutate fired_window/attempt_*; window roll re-arms; already==true → no twin.

### S4 (Wave 3): chart JS + table + cards
Files: dashboard.rs (HTML const).
- _S.live=mk('#ff7b72','LIVE'). Green stays com_twin.
- LIVE build: idx0 = first chronological row w/ lv_pnl!=null; anchor = f1_twin cumulative strictly before idx0 else 0; LIVE[k] = anchor + Σ raw lv_pnl (null → no step; 0.0 → step); plot from idx0 only; no NaN (scroll-out guard).
- Tooltip: LIVE segment (real fill raw pnl) + shadow twin segment.
- Header/chartlbl: LIVE real $5 vs shadow twin; rounding-offset note.
- Table: US group (us_*/match_us cells) → LIVE group (lv_side/lv_entry/lv_delta/match_lv); com group relabelled shadow twin.
- Cards: LIVE (period) from raw lv_pnl/lv_positions/WR; US cards → LIVE↔paper-F1 (lv_pct, lv_positions, denominator copy); com cards relabelled SHADOW.
Done when: build ok; greps (+#ff7b72,_S.live,lv_side,match_lv / -us_side,match_us in row template); anchor formula check documented.

### S5 (Wave 4): OPS deploy both boxes [SECURITY+ARCHITECT pre-review]
EU producer FIRST (34.32.177.126 kalshi-shadow-com; restart in no-window gap; ResolveRecord.live + tagging in same binary), then Buffalo consumer (23.95.217.78 kalshi-shadow, root pw). Backups *.bak.TS both. Verify: green steps on no-fill while LIVE flat; LIVE first point == pink prev cumulative; LIVE column/cards; day_pnl advances once per fill. Rollback: restore .bak + restart.

## Wave/file disjointness
W1={S1}, W2={S2:dashboard.rs, S3:main.rs} disjoint OK, W3={S4}, W4={S5 ops}.

## Acceptance criteria: PRD 3.5 AC-1..AC-9 (serde default; equal-entry disambiguation order-independent; exclusive compare split; green excludes live fills; LIVE first point = pink prev cum; table US→LIVE; cards; replay reconstruct; all-shadow regression).

## Risks
Real-money accounting path (retain hardening) — TC-D.4 day_pnl once per fill; EU live restart (no-window gap; persisted fill latch protects orders; twin dup accepted); wire compat additive (mixed JSONL replay 0 errors); anchor drift on 300-cap (accepted, card = authoritative absolute); twin mis-gating would kill shadow line on no-fills (merge blocker TC-6.5).
