# Paper-Trading Engine — Definitive Re-Implementation Specification

**Source:** `perp_sim/dash_src/` (Polymarket BTC up/down 5-minute binary-options paper engine).
**Scope:** Everything needed to re-implement the engine EXACTLY in another repo: data flow, feeds, orderbook derivation, window model, signal math, entry filters, paper-fill mechanics, resolution, config schema, variants, constants, and gotchas.
**Every formula in this document is verbatim from the source.** Where two code paths differ (e.g. two SNR formulas), both are reproduced — do **not** unify them.

---

## 1. Overview & Architecture

### 1.1 Process model

A single collector/dashboard process runs **5 independent asyncio loops** plus one **daemon thread** (CLOB WS), all sharing two module-global dicts in `state.py`:

- `state` — flat shared-state dict, guarded by `state_lock` (an `RLock`).
- `orderbook` — `{"yes": {...}, "no": {...}}`, guarded by `ob_lock`.
- `trade_buffer` — list of `(ts, notional, is_sell)`, guarded by `trade_buffer_lock`.

### 1.2 End-to-end data flow

```
FEEDS                          STATE                         SIGNAL                  PAPER FILL            RESOLVE
─────                          ─────                         ──────                  ──────────            ───────
Binance @aggTrade  ─┐
                    ├─► state[binance_price]   ─► update_sigma  ─► sigmas{...}  ─┐
                    └─► trade_buffer           ─► compute_ofi   ─► ofi/vol      ─┤
Binance @depth5    ───► bn_bid1/ask1/bn_spread                                  ├─► shared dict ─► check_all_sessions
Polymarket RTDS    ───► state[chainlink_price]                                  │      (per session)        │
Polymarket CLOB WS ─┐                          ─► update_delta  ─► delta_from_* │   dispatch by             ▼
                    ├─► orderbook[yes/no]                          ─► p_model   │   strategy_type    _try_open
                    ├─► poly_*_bid/ask/_vol                                     │   then sigma_type   (1/window,
                    └─► poly_last_trade_*       ─► update_signal ─► signal      ─┘   → evaluator →    balance≥stake,
CLOB REST (1.5s)   ───► orderbook[yes/no] (authoritative) + market_resolved          (sig,side,        shares=stake/
Gamma API          ───► market_slug, token_ids, resolution outcome                    entry,conf)       entry − fee)
                                                                                                            │
                                                                              window_tracker (new window)   ▼
                                                                              ─► resolve_all_positions(outcome_up)
                                                                              paper_gamma_resolver_loop (20s)
                                                                              ─► resolve_pending_via_gamma()
```

### 1.3 The 6 loops / threads and cadences

| Loop / thread | Cadence | Responsibility |
|---|---|---|
| `state_updater` | **0.3 s** | `update_sigma → compute_ofi → update_delta → update_signal`, build `shared`, gate on warmup, call `check_all_sessions(shared)` |
| `orderbook_reader` | **1.5 s** | `fetch_deep_orderbook()` (REST, authoritative book + resolved detection) |
| `window_tracker` | **2 s** | detect new 5-min window; resolve old positions; snapshot window-open; clear book; discover new market |
| `data_recorder` | **1.0 s** (orderbook snapshot every 5th ≈ 5 s) | persist ticks/signals/orderbook_snapshots to SQLite; pull Deribit fields from collector :9999 |
| `paper_gamma_resolver_loop` | **20 s** | `resolve_pending_via_gamma()` per-position fallback resolution |
| CLOB WS daemon thread | recv timeout 1.0 s, flush every 0.2 s | `book`/`price_change`/`last_trade_price` events → `orderbook` + `poly_*` state keys |

### 1.4 Three signal paths (key asymmetries — read this first)

| Path | Selected by | Delta used | SNR tau term |
|---|---|---|---|
| **Classic P-model** | `strategy_type='pmodel'` (default) AND `sigma_type≠'weighted'` | `delta_from_open` | `f_tau` per `tau_mode` (linear/sqrt) |
| **Weighted** | `strategy_type='pmodel'` AND `sigma_type=='weighted'` | `delta_from_prev` | **always `sqrt(max(tau,0.1))`** |
| **caley / ml_evgate** | `strategy_type='caley'` / `'ml_evgate'` | `delta_from_open` | **always linear `max(tau,0.1)`** |
| **noob_fader** | `strategy_type='noob_fader'` | `delta_from_prev` | n/a (LGBM) |

There is also a standalone `helpers.update_signal()` that drives the dashboard's single-signal display; it uses `delta_from_prev`. The per-session paper engine does **not** use `update_signal`'s output — sessions are evaluated by `check_all_sessions`.

---

## 2. External Data Inputs

Four WebSockets + two HTTP endpoints. All consumers are independent reconnecting loops with exponential backoff `1.0 → min(backoff*2, 30)` s. All per-message exceptions are silently swallowed (`except Exception: pass`); only connection-level errors are logged.

### 2.1 Binance aggregated trades — `wss://stream.binance.com:9443/ws/btcusdt@aggTrade`

Per message:
```python
price = float(d.get("p", 0))      # USD
qty   = float(d.get("q", 0))      # BTC
notional = price * qty            # USD
is_sell = d.get("m", False)       # buyer-is-maker flag: m=True ⇒ aggressor SOLD (taker sell)
ts = time.time()                  # wall clock seconds
trade_buffer.append((ts, notional, is_sell))
state["binance_price"] = price
state["binance_ts"]    = d.get("T", "")   # Binance event time T (epoch ms) stored verbatim as string
```
- `binance_price` is the **primary** BTC price everywhere (delta, window-open, sigma_usd).
- `is_sell` semantics: encodes "aggressor sold." OFI subtracts notional when `is_sell`.

### 2.2 Binance depth5 — `wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms`

Level-1 only:
```python
bids = d.get("b") or d.get("bids") or []
asks = d.get("a") or d.get("asks") or []
if bids and asks:
    b1p, b1v = float(bids[0][0]), float(bids[0][1])
    a1p, a1v = float(asks[0][0]), float(asks[0][1])
    state["bn_bid1_p"]=b1p; state["bn_bid1_v"]=b1v
    state["bn_ask1_p"]=a1p; state["bn_ask1_v"]=a1v
    state["bn_spread"]=round(a1p - b1p, 2)   # Binance SPOT spread (USD, 2dp) — a FEATURE, not the LIQ gate
```
ping_interval = 20 s. Frame skipped if either side empty.

### 2.3 Polymarket RTDS (Chainlink oracle) — `wss://ws-live-data.polymarket.com`

Subscribe topic `crypto_prices_chainlink`, type `*`, filter `{"symbol":"btc/usd"}`. Two parse paths:
```python
# (a) payload.data is a list of {timestamp, value}
for item in payload["data"]:
    val = item.get("value")
    if val is not None:
        state["chainlink_price"] = float(val)
        state["chainlink_ts"]    = item.get("timestamp") or ""
# (b) single update message
if topic == "crypto_prices_chainlink" and msg_type == "update":
    sym = (payload.get("symbol") or "").lower()
    val = payload.get("value")
    if "btc" in sym and val is not None:
        state["chainlink_price"] = float(val)
        state["chainlink_ts"]    = payload.get("timestamp", "")
```
- App-level `PING` sent every 5 s; `PONG`/`pong`/`""` frames skipped. ping_interval=10 s.
- **`chainlink_price` is only a FALLBACK price** — Binance is primary. RTDS is the oracle Polymarket *resolves* against, so the engine's Binance-derived delta can disagree with on-chain truth (~4.7% mismatch).

### 2.4 Polymarket CLOB market WS — `wss://ws-subscriptions-clob.polymarket.com/ws/market`

Runs in a **daemon thread** (`websockets.sync.client`), `ping_interval=None`, recv timeout 1.0 s. Subscribe `{"assets_ids": token_ids, "type": "market"}` once `state['market_token_ids']` is populated (else sleep 5 s). Polls `market_token_ids` every 2 s and on each recv timeout; on change → clear book + reconnect. Three event types (§3).

### 2.5 Gamma events HTTP — `https://gamma-api.polymarket.com/events?slug=<slug>`

Market discovery (build slug from window-start unix seconds) and resolution (fetch closed market, read `outcomePrices`). Timeout 5 s for resolution fetch.

### 2.6 CLOB REST book — `https://clob.polymarket.com/book?token_id=<tid>`

Authoritative deep-book poll, timeout 5 s. Used by `fetch_deep_orderbook` (§3).

---

## 3. Orderbook Values (EXACT derivation)

The same `orderbook` and `poly_*` fields are written by **two racing writers**: REST (`src='rest'`, ~1.5 s) and WS (`src='ws_book'`/`'ws_delta'`). **Last writer wins — there is NO precedence logic.** Re-implementations must replicate last-writer-wins.

### 3.1 Level parsing (identical in REST and WS `book` branch)

```python
bids = sorted(
    [[float(b["price"]), float(b["size"])] for b in bids_raw
     if b.get("price") and float(b.get("size", 0)) > 0],
    key=lambda x: -x[0]
)[:OB_MAX_LEVELS]          # bids DESC by price (best bid first)
asks = sorted(
    [[float(a["price"]), float(a["size"])] for a in asks_raw
     if a.get("price") and float(a.get("size", 0)) > 0],
    key=lambda x: x[0]
)[:OB_MAX_LEVELS]          # asks ASC by price (best ask first)
```
- Drop a level if `price` falsy OR `size <= 0`.
- `OB_MAX_LEVELS = 20`.

### 3.2 Per-side aggregates (all 3 write paths)

```python
orderbook[side]["levels"]        = max(len(bids or []), len(asks or []))
orderbook[side]["total_bid_vol"] = sum(b[1] for b in (bids or []))
orderbook[side]["total_ask_vol"] = sum(a[1] for a in (asks or []))
orderbook[side]["src"]           = "rest" | "ws_book" | "ws_delta"
orderbook[side]["ts"]            = _now_utc().isoformat()
```

### 3.3 Top-of-book → state (`_update_ob_tob`, WS path)

```python
bids = orderbook[side].get("bids", []); asks = orderbook[side].get("asks", [])
bid_p = bids[0][0] if bids else None;  bid_v = bids[0][1] if bids else None
ask_p = asks[0][0] if asks else None;  ask_v = asks[0][1] if asks else None
pfx = "poly_" + side + "_"
state[pfx+"bid"]=bid_p; state[pfx+"bid_vol"]=bid_v
state[pfx+"ask"]=ask_p; state[pfx+"ask_vol"]=ask_v
```
The REST path (`fetch_deep_orderbook`) does **not** call `_update_ob_tob`; it inlines the same 8 assignments. REST iteration order is **`('no', no_tid)` THEN `('yes', yes_tid)`** — NO side written first.

### 3.4 The 8 top-of-book state keys

| Key | Derivation | Meaning |
|---|---|---|
| `poly_yes_bid` | `orderbook['yes']['bids'][0][0]` | best YES bid, [0,1] |
| `poly_yes_bid_vol` | `orderbook['yes']['bids'][0][1]` | shares at best YES bid |
| `poly_yes_ask` | `orderbook['yes']['asks'][0][0]` | best YES ask (price to BUY YES) |
| `poly_yes_ask_vol` | `orderbook['yes']['asks'][0][1]` | shares at best YES ask |
| `poly_no_bid` | `orderbook['no']['bids'][0][0]` | best NO bid |
| `poly_no_bid_vol` | `orderbook['no']['bids'][0][1]` | shares at best NO bid |
| `poly_no_ask` | `orderbook['no']['asks'][0][0]` | best NO ask (price to BUY NO) — **THE entry price for NO-side trades** |
| `poly_no_ask_vol` | `orderbook['no']['asks'][0][1]` | shares at best NO ask (depth/liquidity at entry) |

`None` if that side's book is empty.

### 3.5 `last_trade_expensive` / `last_trade_cheap` (from `last_trade_price` WS event ONLY)

Triggered **only** by `event_type == 'last_trade_price'` (a real executed trade — NOT the book). NEVER set by REST.

```python
if ob_side and price > 0:
    state[f"poly_{ob_side}_last_trade"]      = price
    state[f"poly_{ob_side}_last_trade_size"] = size
    state[f"poly_{ob_side}_last_trade_ts"]   = _now_utc().isoformat()
    other = "yes" if ob_side == "no" else "no"
    other_last = state.get(f"poly_{other}_last_trade")
    if other_last:
        expensive_price = max(price, other_last)
        cheap_price     = min(price, other_last)
    else:
        expensive_price = price if price > 0.5 else 1 - price   # single-side fallback
        cheap_price     = 1 - expensive_price
    state["poly_last_trade_expensive"] = round(expensive_price, 4)
    state["poly_last_trade_cheap"]     = round(cheap_price, 4)
```
- Two-sided branch (both sides traded): `expensive = max(...)`, `cheap = min(...)` of the two **possibly-stale** last trades — so `expensive + cheap` need **not** equal 1.0.
- Single-side fallback: folds price around 0.5, IGNORING which side traded. A NO-side trade at 0.30 → `expensive=0.70, cheap=0.30`, but `poly_no_last_trade` still records 0.30.
- Until a trade prints, both stay `None` even with a populated book.

### 3.6 `price_change` deltas (batched, 200 ms)

`is_bid` classification:
```python
chg_side = (chg.get("side") or "").upper()
if   chg_side in ("BUY","BID"):   is_bid = True
elif chg_side in ("SELL","ASK"):  is_bid = False
else:                             is_bid = price <= 0.5   # heuristic fallback
```
Queued to `_pending_changes`; applied at most every `_APPLY_INTERVAL = 0.2` s (force-flushed on recv timeout). Apply = dedup (keep last per `(side, round(price,4), is_bid)`) then upsert:
```python
for i, lv in enumerate(levels):
    if abs(lv[0] - price) < 0.0001:
        if size <= 0: levels.pop(i)      # remove level
        else:         lv[1] = size       # overwrite size
        found = True; break
if not found and size > 0:
    levels.append([price, size])
```
Then re-sort (bids desc, asks asc), trim to 20, recompute aggregates (`src='ws_delta'`), `_update_ob_tob`. **Eventually-consistent, not tick-exact** — sub-200ms updates to the same level collapse to the last value.

### 3.7 WS book side resolution + fallback

```python
side = _resolve_ob_side(asset_id)   # match state['yes_token_id'] / state['no_token_id']
if not side:                        # token ids not loaded → can MISLABEL the side
    bids_raw = data.get("bids") or []
    bid1 = float(bids_raw[0]["price"]) if bids_raw else 0.5
    side = "no" if bid1 < 0.5 else "yes"
```

### 3.8 Resolved-market detection (`_is_resolved_book`)

```python
def _is_resolved_book(bids, asks):
    if not bids and not asks: return False
    best_bid = bids[0][0] if bids else 0
    best_ask = asks[0][0] if asks else 1
    if best_bid <= 0.05 or best_ask >= 0.95: return True
    if best_ask - best_bid > 0.85:           return True
    return False
```
- Empty bid side ⇒ `best_bid=0` (≤0.05 ⇒ resolved); empty ask side ⇒ `best_ask=1` (≥0.95 ⇒ resolved). **Either side resolved ⇒ whole market resolved** → false-positives on thin/half-empty new markets.
- Edge-triggered transition sets `state['market_resolved']`, `market_resolved_ts`, and raises `_market_rediscovery_requested[0]=True` (a 1-element mutable list used as a cross-module flag).

### 3.9 REST error handling

`fetch_orderbook_rest` returns `(None, None)` on HTTP≠200 or exception; `fetch_deep_orderbook` then `continue`s (skips that side, does **not** clear it). A transient REST error leaves the previous book intact; only a successful fetch overwrites. `_rest_hashes` is for change-logging only — does NOT gate processing.

---

## 4. Window Model

### 4.1 Window slot detection (`_current_window`)

```python
def _current_window():
    now = _now_utc()                                       # datetime.now(timezone.utc)
    slot  = (now.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    start = now.replace(minute=slot, second=0, microsecond=0)
    end   = start + timedelta(minutes=WINDOW_MINUTES)
    return start, end
```
`WINDOW_MINUTES = 5` (this LOCAL config). New window detected in `window_tracker` when `w_start != last_window`.

### 4.2 New-window sequence (`window_tracker`) — ORDER MATTERS

On every new window:
1. **Resolve OLD positions first** using `prev_slug` + OLD state (§8).
2. **Snapshot window-open price** (carry old open into prev_close, stamp window times, set new open):
```python
price = bn or cl                                  # binance primary, chainlink fallback
if state["window_open_price"] is not None:
    state["prev_close_price"] = price             # carry current price into prev_close
state["window_start_utc"] = w_start.isoformat()
state["window_end_utc"]   = w_end.isoformat()
state["window_open_price"] = price
```
3. **Clear** `orderbook['yes'/'no']` to empty and NULL all 6 `*_last_trade*` keys + `expensive`/`cheap` (new tokens). **Does NOT null `poly_*_bid/ask/_vol`** — those persist stale up to ~1.5 s until next REST/WS write.
4. `find_current_market()` → new slug + token ids.
5. `db_insert_window`.

### 4.3 Open price seeding (lazy, in `update_delta`)

```python
price  = bn or cl
w_open = state["window_open_price"]
prev   = state["prev_close_price"]
if price and w_open is None:
    state["window_open_price"] = price; w_open = price
if price and prev is None and w_open is not None:
    state["prev_close_price"] = w_open; prev = w_open
```
- `window_open_price` is **lazily** seeded with the first price seen after the field is `None`; the **reset to `None` at each boundary happens in `window_tracker` (tasks.py), NOT here**.
- `prev_close_price` carry-forward is **split across two functions**: `window_tracker` sets `prev_close=price` when open was not None; `update_delta` seeds `prev_close=w_open` if None.

### 4.4 tau (minutes to expiry) — NOT normalized by window length

In `state_updater` (the tau placed into `shared['tau']`):
```python
tau = 2
if w_end_str:
    try:
        w_e = datetime.fromisoformat(w_end_str)
        tau = max((w_e - datetime.now(timezone.utc)).total_seconds() / 60, 0.1)
    except Exception:
        pass
```
- **tau = raw minutes-to-expiry, floored at 0.1, default 2.** Divisor is **60** (seconds→minutes), NOT `WINDOW_MINUTES`. There is NO `/WINDOW_MINUTES` normalization.
- In `helpers.update_delta` the same formula is used but the missing-window fallback is **7.5**, not 2:
```python
tau = max((w_end - now).total_seconds() / 60, 0.1)   # else fallback 7.5
```

### 4.5 elapsed_min

```python
elapsed_min = 0
if w_start_str:
    try:
        w_s = datetime.fromisoformat(w_start_str)
        elapsed_min = (datetime.now(timezone.utc) - w_s).total_seconds() / 60
    except Exception:
        pass
```
Not floored/capped; default 0. Placed into `shared['elapsed_min']`. (`noob_fader` ignores this and uses seconds-in-window directly — §6.)

---

## 5. Signal Computation (ALL formulas verbatim)

### 5.1 Standard normal CDF (`_phi`)

```python
def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))     # math.erf = exact libm, NOT an approximation
```
Defined identically in `helpers.py:209-210` and `paper_trading.py:79-81`. If porting to a language without `erf`, match it with a high-precision erf.

### 5.2 Sigma family (`helpers._compute_all_sigmas`, driven by Binance price history)

Driven each 0.3 s by `update_sigma`: append `(now, binance_price)` to `_price_history`, trim to last **2100 s (35 min)**, recompute all variants over `win ∈ {5, 10, 30}` minutes. Sigma computation **no-ops entirely if `len(_price_history) < 20`**. Returns dict variants require ≥10 price points per window and ≥3 returns. `state['sigma'] = _sigma_cache[cfg.SIGMA_TYPE]` (default `realized5_pmin`, fallback `realized5_pmin`).

**Helpers:**
```python
def _std(values):                              # POPULATION std (÷ N, not N-1)
    if len(values) < 2: return 0
    mean = sum(values) / len(values)
    var  = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var) if var > 0 else 0

def _returns(prices):                          # consecutive fractional returns
    ret = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret.append((prices[i] - prices[i-1]) / prices[i-1])
    return ret

def _duration_min(ts_list):                    # ACTUAL span in minutes (not nominal window)
    if len(ts_list) < 2: return 1
    d = (ts_list[-1] - ts_list[0]) / 60
    return max(d, 0.01)
```

**Variants** (`avg = mean(prices)`, `dur = _duration_min(ts)`):

```python
result = {"static": SIGMA_STATIC}              # SIGMA_STATIC = 0.0005, always present

# trail{win}: coefficient of variation
trail = _std(prices) / avg
result[f"trail{win}"]      = trail
result[f"trail{win}_pmin"] = trail / dur

# realized{win}: std of returns  (DEFAULT sigma family)
rets = _returns(prices)
if len(rets) >= 3:
    realized = _std(rets)
    result[f"realized{win}"]      = realized
    result[f"realized{win}_pmin"] = realized / dur

# median{win}/mad{win}:  median stores the median RETURN; mad stores median abs deviation
sorted_rets = sorted(rets)
med = sorted_rets[len(sorted_rets)//2]                 # simple middle element (biased on even N)
mad_vals = sorted(abs(r - med) for r in rets)
mad = mad_vals[len(mad_vals)//2]
result[f"median{win}"]      = med if med > 0 else realized
result[f"median{win}_pmin"] = (med if med > 0 else realized) / dur
result[f"mad{win}"]         = mad if mad > 0 else realized

# range{win}: (high-low)/mean
hi, lo = max(prices), min(prices)
rng = (hi - lo) / avg if avg > 0 else 0
result[f"range{win}"]      = rng
result[f"range{win}_pmin"] = rng / dur

# max{win}: MAX over the window of trailing-60s std/mean   ("max30" peak measure)
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
            if s > max_std: max_std = s
result[f"max{win}"]      = max_std
result[f"max{win}_pmin"] = max_std                     # NO /dur — already a peak per-minute measure
```
- **`max{win}_pmin == max{win}`** (no `/dur`), unlike every other `_pmin` variant. `max30` is the peak-volatility "third strategy" sigma. O(N²) inner loop runs every 0.3 s over up to 35 min of points.
- `SIGMA_STATIC = 0.0005` train_std ≈ 2.168e-19 (near zero) — see weighted normalization guard.

**Sigma lookup:**
```python
def get_sigma_for_type(sigma_type):
    if sigma_type == "static": return SIGMA_STATIC
    val = _sigma_cache.get(sigma_type)
    if val and val > 0: return val
    return _sigma_cache.get("realized5_pmin") or _sigma_cache.get("realized5")
```

### 5.3 P-model (`helpers.compute_p_model`) — VERBATIM

```python
def compute_p_model(delta, sigma, tau_minutes):
    if delta is None or sigma is None or tau_minutes is None or tau_minutes <= 0:
        return None
    sigma_usd = sigma * (state.get("binance_price") or state.get("chainlink_price") or 70000)
    if sigma_usd <= 0:
        return None
    if   cfg.TAU_MODE == "linear": f_tau = tau_minutes
    elif cfg.TAU_MODE == "sqrt":   f_tau = math.sqrt(tau_minutes)
    else:                          f_tau = 1.0
    if f_tau <= 0: f_tau = 0.1
    snr = abs(delta) / (sigma_usd * f_tau)
    p   = _phi(cfg.P_MODEL_KAPPA * snr)
    with state_lock:
        state["snr"] = round(snr, 3)
    return round(p, 4)
```
- `sigma_usd = sigma × BTC_price`. **BTC price fallback chain: `binance_price → chainlink_price → literal 70000`.**
- `f_tau = tau_minutes` (linear, default) — **tau in MINUTES** ⇒ `sigma_usd` is implicitly per-minute.
- `snr = |delta_USD| / (sigma_usd · f_tau)`; `p = Φ(κ·snr)`, `κ=0.5`. Rounded 4dp; snr rounded 3dp. Returns `None` on any missing/zero input.
- In `helpers.update_delta`, `p_model` is computed off **`delta_from_prev`** with tau = minutes-to-window-end (floor 0.1, fallback 7.5).

### 5.4 Classic P-model in the paper engine (`_evaluate_signal`) — VERBATIM

```python
sigma_type = session_cfg.get("sigma_type", "realized5_pmin")
sigma      = shared.get("sigmas", {}).get(sigma_type) or shared.get("sigma")
tau        = shared.get("tau", 2)
price_ref  = shared.get("binance_price", 70000)
delta      = shared.get("delta_from_open")        # NB: delta_from_OPEN here

if sigma and sigma > 0 and price_ref > 0:
    sigma_usd = sigma * price_ref
    if   session_cfg["tau_mode"] == "linear": f_tau = max(tau, 0.1)
    elif session_cfg["tau_mode"] == "sqrt":   f_tau = max(math.sqrt(tau), 0.1)
    else:                                     f_tau = 1.0
    snr = abs(delta) / (sigma_usd * f_tau)
    p   = _phi(session_cfg["kappa"] * snr)
else:
    p = None
```
- **Two differences vs `compute_p_model`:** (a) delta source is `delta_from_open`, not `delta_from_prev`; (b) `f_tau` is floored at 0.1 via `max(tau,0.1)`.
- If `sigma`/`price` invalid, `p = None` — and the p-threshold check is `if p is not None and p < pt`, so **a `None` p PASSES** (signal proceeds). This is a real, intentional fall-through.

### 5.5 OFI (`helpers.compute_ofi`) — VERBATIM

```python
def compute_ofi():
    now = time.time()
    with trade_buffer_lock:
        cutoff = now - 300
        while trade_buffer and trade_buffer[0][0] < cutoff:
            trade_buffer.pop(0)
        ofi_1 = ofi_3 = ofi_5 = 0
        buy_1 = sell_1 = 0
        count_1 = 0
        for ts, notional, is_sell in trade_buffer:
            age = now - ts
            val = -notional if is_sell else notional
            if age <= 60:
                ofi_1 += val; count_1 += 1
                if is_sell: sell_1 += notional
                else:       buy_1  += notional
            if age <= 180:
                ofi_3 += val
            ofi_5 += val
    state["ofi_1min"]=round(ofi_1,0); state["ofi_3min"]=round(ofi_3,0); state["ofi_5min"]=round(ofi_5,0)
    state["buy_vol_1min"]=round(buy_1,0); state["sell_vol_1min"]=round(sell_1,0)
    state["trade_count_1min"]=count_1
```
- OFI = signed sum of **notional** (buy `+`, sell `−`). Buffer pruned to 300 s. `ofi_1` ≤60 s, `ofi_3` ≤180 s, `ofi_5` all. `buy_vol`/`sell_vol` are **unsigned** 1-min sums. All ofi/vol rounded 0dp.

### 5.6 Weighted 22-feature model (`helpers.compute_weighted_score`) — VERBATIM

```python
def compute_weighted_score(shared):
    if _weighted_model is None:
        return None, None, None
    delta = shared.get("delta_from_prev")           # NB: delta_from_PREV here
    btc   = shared.get("binance_price", 0)
    tau   = shared.get("tau", 2)
    sigmas= shared.get("sigmas", {})
    if delta is None or btc <= 0:
        return None, None, None

    ofi_1=shared.get("ofi_1min",0) or 0; ofi_5=shared.get("ofi_5min",0) or 0
    buy_vol=shared.get("buy_vol_1min",0) or 0; sell_vol=shared.get("sell_vol_1min",0) or 0
    trade_count=shared.get("trade_count_1min",0) or 0; hour=shared.get("hour",0)

    snr_vals = {}
    for st in ["static","realized5_pmin","trail5_pmin","range5_pmin"]:
        sv = sigmas.get(st, 0)
        if sv > 0 and btc > 0:
            snr_vals[st] = abs(delta) / (sv * btc * math.sqrt(max(tau, 0.1)))   # sqrt(tau) ALWAYS
        else:
            snr_vals[st] = 0

    fdict = {
        "abs_delta": abs(delta),
        "delta_pct": delta / btc if btc > 0 else 0,
        "snr_static": snr_vals.get("static",0),
        "snr_realized5_pmin": snr_vals.get("realized5_pmin",0),
        "snr_trail5_pmin": snr_vals.get("trail5_pmin",0),
        "snr_range5_pmin": snr_vals.get("range5_pmin",0),
        "p_k0.7_static": _phi(0.7 * snr_vals.get("static",0)),
        "p_k1.0_static": _phi(1.0 * snr_vals.get("static",0)),
        "p_k0.7_realized5_pmin": _phi(0.7 * snr_vals.get("realized5_pmin",0)),
        "ofi_1min": ofi_1, "ofi_5min": ofi_5,
        "vol_imbalance": (buy_vol - sell_vol) / max(buy_vol + sell_vol, 1),
        "trade_count_1min": trade_count,
        "is_night": 1 if 0 <= hour < 6 else 0,
        "is_weekend": 1 if shared.get("dow",0) >= 5 else 0,
        "hour_sin": math.sin(2*math.pi*hour/24),
        "hour_cos": math.cos(2*math.pi*hour/24),
        "sigma_static": sigmas.get("static", SIGMA_STATIC),
        "sigma_realized5_pmin": sigmas.get("realized5_pmin",0),
        "sigma_trail5_pmin": sigmas.get("trail5_pmin",0),
        "entry_expensive": shared.get("orderbook_entry",0) or 0,
        "tau": tau,
    }

    fvec = np.array([float(fdict.get(fn,0)) for fn in _weighted_model["feature_names"]])
    fvec = np.nan_to_num(fvec)
    stds_safe = np.where(_weighted_model["stds"] > 1e-10, _weighted_model["stds"], 1.0)
    fvec_norm = (fvec - _weighted_model["means"]) / stds_safe
    fvec_norm = np.clip(fvec_norm, -5.0, 5.0)
    score = float(np.dot(_weighted_model["weights"], fvec_norm))
    return score, _weighted_model["threshold"], _weighted_model["max_entry"]
```
**Pipeline:** build raw vector in `feature_names` order → `nan_to_num` (NaN/inf→0) → z-score `(x − means) / stds_safe` (stds≤1e-10 replaced by 1.0) → clip `[-5,5]` → `dot(weights, normalized)`.
**Weighted SNR uses `sqrt(max(tau,0.1))` ALWAYS** and multiplies sigma by `btc` (USD-izing). This is NOT the same as `compute_p_model`'s linear-tau SNR. Replicate BOTH guards (`stds_safe` and clip) exactly — `sigma_static`'s near-zero std would otherwise produce inf.

**The 22 features in EXACT order** (`optimized_weights.json` → `_weighted_model["feature_names"]`):
```
abs_delta, delta_pct, snr_static, snr_realized5_pmin, snr_trail5_pmin, snr_range5_pmin,
p_k0.7_static, p_k1.0_static, p_k0.7_realized5_pmin, ofi_1min, ofi_5min, vol_imbalance,
trade_count_1min, is_night, is_weekend, hour_sin, hour_cos, sigma_static,
sigma_realized5_pmin, sigma_trail5_pmin, entry_expensive, tau
```

**Model params (`optimized_weights.json`, repo root):**
- `threshold = 0.10707533811365622`
- `max_entry = 0.8823996226008942`
- `weights` (22, in feature order):
```
[-0.9702078398271025, 4.405698506842733, 2.6080111964202155, -3.118184301316748,
 4.734004395112659, 1.3762518642429666, 0.006539209625475717, 4.321297506595564,
 2.7289845412203686, -3.384026387297148, -1.450583600269217, -0.9909011602079415,
 -0.40638158627223864, -2.6791899013557643, 2.484086494043569, -1.0718752767713846,
 1.688589097381843, 4.5962462711851995, 4.758071080668296, 4.939530476883772,
 -3.344700087305629, 4.928216902420454]
```
- `means` (22) and `stds` (22) are the per-feature z-score subtrahend/divisor (e.g. `abs_delta` mean 55.311 / std 59.774; `sigma_static` mean 0.0005 / std 2.168e-19; `ofi_5min` mean −105605.34).
- `train_pnl/test_pnl = 3334.25 / 506.84`; `train_wr/test_wr = 0.9286 / 0.8571` (recorded in JSON; informational).

### 5.7 `update_delta` (computes the deltas; VERBATIM core)

```python
price = bn or cl
if price and w_open: state["delta_from_open"] = round(price - w_open, 2)
if price and prev:   state["delta_from_prev"] = round(price - prev, 2)
```
2dp USD, signed.

---

## 6. Entry Decision & Filters (in order)

### 6.1 Dispatch (`check_all_sessions`, every 0.3 s after warmup)

```python
strat = cfg.get("strategy_type", "pmodel")
if   strat == "noob_fader": sig,side,entry,conf = _evaluate_signal_noob_fader(cfg, shared)
elif strat == "ml_evgate":  sig,side,entry,conf = _evaluate_signal_ml_evgate(cfg, shared)
elif strat == "caley":      sig,side,entry,conf = _evaluate_signal_caley(cfg, shared)
else:                       sig,side,entry,conf = _evaluate_signal(cfg, shared)
```
Paused sessions: `last_signal='PAUSED'`, skipped. If `(side, entry)` non-None → `_try_open`.

### 6.2 Classic P-model filter order (`_evaluate_signal`, default `pmodel`) — EXACT sequence

1. `delta = shared['delta_from_open']`; if `None` → **`'WAITING'`**.
2. `elapsed_min = shared.get('elapsed_min', 0)`; `_ew = float(cfg.get('entry_wait_min',3) or 3)`; if `elapsed_min < _ew` → **`'WAIT (e/_ew)'`**.
3. **Weighted branch**: if `sigma_type == 'weighted'` → weighted path (§6.3), returns here.
4. `if abs(delta) < delta_threshold` → **`'DELTA LOW'`**.
5. **Dual-confirmation (AMBIGUOUS)**: if `sign(delta_from_open) != sign(delta_from_prev)` → **`'AMBIGUOUS'`** (`>=0` is the positive class). Classic path ONLY.
6. **`_regime_block`** (§6.6): kill_hours → `'HOUR-BLOCK'`; ofi_align → `'OFI-MISALIGN'`.
7. `sigma = sigmas[sigma_type] or sigma`. **sigma_max cap**: `_sigmax = cfg.get("sigma_max")`; if `_sigmax and sigma and sigma > float(_sigmax)` → **`'SIGMA-HIGH'`**.
8. Compute `sigma_usd, f_tau, snr, p` (§5.4).
9. **p-threshold**: `if p is not None and p < p_model_threshold` → **`'P-LOW'`**. (`None` p bypasses.)
10. **Entry price sourcing**: `last_trade_expensive` if truthy and `>0.5`, else `orderbook_entry`; each must be `>0.5` else **`'NO PRICE'`**:
```python
entry_price = shared.get("last_trade_expensive")
if not entry_price or entry_price <= 0.5:
    entry_price = shared.get("orderbook_entry")
if not entry_price or entry_price <= 0.5:
    return "NO PRICE", None, None, None
```
11. **max_entry**: if `entry_price > max_entry_price` → **`'HIGH'`**.
12. **REFIT gate** (only if `refit_gate` truthy):
```python
_pf = _refit_pfit(delta, tau)
if _pf is None or _pf <= entry_price + float(cfg.get("refit_margin",0.0) or 0.0):
    return ("REFIT-SKIP (...)" if _pf is not None else "REFIT-ERR"), None, None, None
```
13. **Side filter**: `trade_side='NO'` needs `delta <= -dt`; `'YES'` needs `delta >= dt`; `'BOTH'` by sign. On the chosen side run **`_session_liq`** (§6.5); if skip → reason. Else return `'BUY NO'`/`'BUY YES'`, side, entry, p.

### 6.3 Weighted path (`sigma_type == 'weighted'`)

```python
score, threshold, max_entry = compute_weighted_score(shared)
if score is None:        return "NO SCORE", None, None, None
if score <= threshold:   return f"SCORE LOW ({score:.2f})", None, None, None
entry_price = shared.get("last_trade_expensive")
if not entry_price or entry_price <= 0.5:
    entry_price = shared.get("orderbook_entry")
# (>0.5 check) → "NO PRICE"
if entry_price > max_entry: return f"HIGH ({entry_price:.2f}>{max_entry:.2f})", None, None, None
# side by delta sign (delta = delta_from_open); _check_liquidity_paper directly (no vol_imb_kill)
```

### 6.4 Global liquidity (`_check_liquidity_paper`, FAIL-CLOSED)

```python
LIQ_SPREAD_KILL = 0.0142
LIQ_DEPTH_KILL  = 424.0
LIQ_FILTER_ENABLED = True
...
if ask is None or bid is None:
    return True, "LIQ:no_quotes"                # FAIL-CLOSED
spread = ask - bid
if spread > LIQ_SPREAD_KILL:
    return True, f"LIQ:wide_spread {spread:.4f}>{LIQ_SPREAD_KILL}"
if ask_vol is not None and bid_vol is not None:  # depth check OPTIONAL (fail-open if vols missing)
    depth = (ask_vol or 0) + (bid_vol or 0)
    if depth <= LIQ_DEPTH_KILL:
        return True, f"LIQ:thin_book {depth:.0f}<={LIQ_DEPTH_KILL:.0f}"
return False, ""
```
Picks `poly_yes_*` or `poly_no_*` by side. Disabled entirely if `LIQ_FILTER_ENABLED` False.

### 6.5 Per-session liquidity (`_session_liq`)

```python
# 1) if cfg.liq_filter == 'on': run _check_liquidity_paper (spread/depth)
# 2) vol-imbalance kill:
vk = float(cfg.get("vol_imb_kill", 0) or 0)
if vk > 0:
    bv = shared.get("buy_vol_1min") or 0
    sv = shared.get("sell_vol_1min") or 0
    tot = bv + sv
    if tot > 0:
        imb = (bv - sv) / tot
        if side == "YES" and imb < -vk: return True, f"VOLIMB {imb:+.2f}<-{vk} vs YES"
        if side == "NO"  and imb >  vk: return True, f"VOLIMB {imb:+.2f}>{vk} vs NO"
```
Classic/caley/ml_evgate call `_session_liq`; weighted calls `_check_liquidity_paper` directly.

### 6.6 Regime block (`_regime_block`) — VERBATIM

```python
def _regime_block(cfg, delta, shared):
    kh = str(cfg.get("kill_hours","") or "")
    if kh:
        ws = shared.get("window_start") or shared.get("window_start_utc") or ""
        hr = -1
        try:    hr = int(ws[11:13])
        except Exception: hr = shared.get("hour", -1)
        blocked = set()
        for x in kh.split(","):
            x = x.strip()
            if x:
                try: blocked.add(int(x))
                except Exception: pass
        if hr in blocked:
            return f"HOUR-BLOCK ({hr})"
    if str(cfg.get("ofi_align","off")) == "on":
        ofi1 = shared.get("ofi_1min") or 0
        sgn  = 1 if delta > 0 else (-1 if delta < 0 else 0)
        if ofi1 * sgn < 0:
            return f"OFI-MISALIGN ({ofi1:+.0f} vs {'YES' if delta>0 else 'NO'})"
    return None
```

### 6.7 caley (F2) — VERBATIM

```python
snr  = abs(delta) / (sigma * btc * max(tau, 0.1))    # LINEAR tau
p_re = _phi(0.5 * snr)
p_cal = float(_np.interp(p_re, xs, ys))              # isotonic, f2_iso.json X_thresholds/y_thresholds
side  = "YES" if delta > 0 else "NO"
entry = _merged_entry(side, shared)
ev      = p_cal * (1 - entry) / entry - (1 - p_cal)  # binary EV per $1
ev_thr  = float(session_cfg.get("ev_thr", 0.15))
if ev < ev_thr:
    return f"EV LOW ({ev:+.3f}<{ev_thr})", None, None, None
```
- `delta = delta_from_open`; `sigma = sigmas[sigma_type] or sigma` (sigma_type default `max30`); `btc` default 70000; `tau` default 2.
- caley defaults via `.get`: `ev_thr 0.15`, `max_entry_price 0.80`, `delta_threshold 20`.
- Also applies `entry_wait_min`, `_regime_block`, and rejects `entry <= 0.50` (`'NO PRICE'`).

### 6.8 ml_evgate (F4) core8 LGBM — VERBATIM

```python
snr  = abs(delta) / (sigma * btc * max(tau, 0.1))    # LINEAR tau
p_re = _phi(0.5 * snr)
vol_imb_s = ((bv - sv) / tot if tot > 0 else 0.0) * sgn     # signed by delta direction
feats = _np.array([[snr, p_re, entry, abs(delta), sigma, max(tau,0.1), spread, vol_imb_s]],
                  dtype=_np.float64)
_np.nan_to_num(feats, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
raw   = float(model.predict(feats)[0])                # f4_model.lgb
p_cal = float(_np.interp(raw, ix, iy)) if ix else raw # meta iso_x/iso_y
ev      = p_cal * (1 - entry) / entry - (1 - p_cal)
ev_thr  = float(session_cfg.get("ev_thr", 0.18))
if ev < ev_thr:
    return f"EV LOW ({ev:+.3f}<{ev_thr})", None, None, None
```
- 8 features (core8): `[snr, p_re, entry, |delta|, sigma, max(tau,0.1), spread, vol_imb_signed]`.
- `spread = (poly_<side>_ask or 0) - (poly_<side>_bid or 0)`. `entry = _merged_entry(side, shared)`.
- ml_evgate defaults via `.get`: `ev_thr 0.18`, `max_entry_price 0.82`, `delta_threshold 15`.

### 6.9 noob_fader 25-feature LGBM — VERBATIM core

```python
# fires ONLY in decision zone [60, 180) SECONDS into window (sec_in_window from window_start_utc vs now)
market = (yb + ya) / 2.0                  # yes mid; requires yb>0, ya<1, ya>yb else "NO BOOK"
p_raw  = float(model.predict(feats)[0])   # new_noob_fresh.lgb, 25 features
p_cal  = float(_np.interp(p_raw, xs, ys)) # new_noob_fresh_cal.json
gap = p_cal - market
TH  = float(session_cfg.get("threshold_gap", 0.10))
# optional: skip if |p_cal - 0.5| < uncertainty_eps  (env UNCERTAINTY_EPS default 0.0)
if gap > TH:        # BUY YES
    entry = float(ya)
elif gap < -TH:     # BUY NO
    if no_a is None or no_a <= 0 or no_a >= 1:
        no_a = 1.0 - float(yb)            # fallback 1 - yes_bid
    entry = float(no_a)
# max_entry filter applies (default 0.95). confidence = p_cal (YES) / 1 - p_cal (NO)
```
- `delta` source = `delta_from_prev`. Uses seconds-in-window, **NOT** `entry_wait_min`/`elapsed_min`.
- **`_NOOB_FEATURES` (25, verbatim) — note `delta_from_prev` is DUPLICATED at positions 3-4** (intentional, to match the trained model's 25-column layout — replicate exactly):
```
binance_price, chainlink_price, delta_from_prev, delta_from_prev, abs_delta, sign_delta,
ofi_1min, ofi_3min, ofi_5min, buy_vol_1min, sell_vol_1min, trade_count_1min, vol_imbalance,
sigma, p_model, snr, bn_spread, hour_utc, hour_sin, hour_cos, day_of_week, is_weekend,
sec_in_window, frac_in_window, tau_norm
```
`frac = sec_in_window / 300.0`.

### 6.10 `_merged_entry` (caley/ml_evgate only) — VERBATIM

```python
def _merged_entry(side, shared):
    ya, yb = shared.get("poly_yes_ask"), shared.get("poly_yes_bid")
    na, nb = shared.get("poly_no_ask"),  shared.get("poly_no_bid")
    def v(x): return x if (x is not None and 0.01 < x < 0.99) else None
    if side == "YES":
        cands = [c for c in (v(ya), (round(1 - nb, 2) if v(nb) else None)) if c is not None]
    else:
        cands = [c for c in (v(na), (round(1 - yb, 2) if v(yb) else None)) if c is not None]
    return min(cands) if cands else None
```
Best executable BUY price = `min(side ask, 1 − opposite bid)`. Quotes valid only in `(0.01, 0.99)`. NOT used by classic/weighted.

---

## 7. Paper Fill Mechanics

### 7.1 Fee (`calc_fee`) — VERBATIM

```python
FEE_RATE = 0.25
FEE_EXPONENT = 2

def calc_fee(shares, price):
    if price <= 0 or price >= 1:
        return 0.0
    fee = shares * price * FEE_RATE * (price * (1 - price)) ** FEE_EXPONENT
    return max(fee, 0.0001)
```
- Fee charged on BUY, expressed in **SHARES** (not USD). `0.0` only when price degenerate (`<=0` or `>=1`); otherwise floored at `0.0001`. `(price·(1-price))²` peaks fee at price 0.5.

### 7.2 Open (`_try_open`) — VERBATIM accounting

Guards BEFORE accounting (inside `paper_lock`): reset `buys_this_window` when `window_start` changes; reject if `buys_this_window >= 1` (**1 trade/window, hardcoded**); reject if `balance < stake`.

```python
raw_shares     = stake / entry_price
buy_fee_shares = calc_fee(raw_shares, entry_price)
shares         = raw_shares - buy_fee_shares
trade_dict     = {... "stake": stake, "shares": round(shares, 2), ...}
st["buys_this_window"] += 1
st["last_buy_ts"]       = time.time()
st["balance"]          -= stake          # debited by FULL stake, NOT stake+fee
st["total_staked"]     += stake
```
- `raw_shares = stake / entry_price`. Net `shares = raw_shares − fee_in_shares`, rounded to 2dp before storage and used at resolution.
- **Balance debited by full `stake`** — the fee comes out of shares, not USD. On a win, fee reduces payout, not the stake debit.
- `trade_dict['delta']` stores `delta_from_prev` (passed from `check_all_sessions` as `shared.get('delta_from_prev',0)`). `ob_shares = shared.get('ob_shares',0)` is carried through (rounded 0dp), informational only.
- `stake = cfg['stake']` (USD). `db_insert_paper_trade` persists; position appended to `positions`; `_save_session`.

---

## 8. Resolution & PnL

### 8.1 Window resolve (`resolve_all_positions(outcome_up)`) — VERBATIM

```python
won = (pos["side"] == "YES" and outcome_up) or \
      (pos["side"] == "NO"  and not outcome_up)
if won:
    payout = pos["shares"] * 1.0          # each winning share redeems at $1
    pnl    = payout - pos["stake"]
else:
    pnl    = -pos["stake"]
    payout = 0
s["state"]["total_pnl"]    += pnl
s["state"]["balance"]      += payout      # stake already debited at open ⇒ net win = +pnl
s["state"]["total_trades"] += 1
if won: s["state"]["wins"]   += 1
else:   s["state"]["losses"] += 1
```
Resolved positions prepended to history (cap **200**); positions cleared; `db_resolve_paper_trade` if `db_id`. `resolve_pending_via_gamma()` uses identical math per-position.

### 8.2 Resolution source priority (`window_tracker`, on new window) — STRICT

**(1) Gamma `outcomePrices` — ONLY if `closed==True` AND clean binary:**
```python
op = m.get("outcomePrices")
if isinstance(op, str): op = json.loads(op)
if gamma_closed and op and len(op) == 2:
    if   op[0] == "1": outcome_up = True;  resolve_src = "gamma"
    elif op[1] == "1": outcome_up = False; resolve_src = "gamma"
```
`['1','0']` = UP won; `['0','1']` = DOWN won. `['0.5','0.5']` on a pending market must **NOT** resolve. `gamma_closed = bool(m.get('closed', False))`.

**(2) DEFER — Gamma reachable but not closed:** do NOTHING (leave positions active), retry next window + via 20s loop.
```python
if outcome_up is None and gamma_net_ok and not gamma_closed:
    print(f"[paper] DEFER {prev_slug} — Gamma says not closed yet")
```

**(3) FALLBACK — Gamma UNREACHABLE only:**
```python
elif outcome_up is None:
    if was_resolved and yes_bid is not None and no_bid is not None:
        outcome_up = (yes_bid > no_bid); resolve_src = "orderbook"
    else:
        outcome_up = (delta_close > 0);  resolve_src = "binance_delta"
    resolve_all_positions(outcome_up)
```
`delta_close = state['delta_from_open'] or state['delta_from_prev'] or 0` (prefers `delta_from_open`). `was_resolved = state['market_resolved']`.

`resolve_all_positions` is called in the fallback branch and in the final gamma-success else; **never** in DEFER.

### 8.3 Gamma per-position fallback (`paper_gamma_resolver_loop`, 20 s)

`resolve_pending_via_gamma()` re-checks pending positions via Gamma (`closed=True` + `outcomePrices`), identical won/payout/pnl math per-position.

---

## 9. Session Config Schema & Variants

### 9.1 `DEFAULT_CONFIG` (20 keys) — VERBATIM

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | str | `"Default"` | display label |
| `kappa` | float | `0.5` | `p=Φ(kappa·snr)` |
| `delta_threshold` | float | `20` | min `|delta_from_open|` USD (caley .get 20, ml_evgate .get 15) |
| `p_model_threshold` | float | `0.60` | classic only; `None` p bypasses |
| `sigma_type` | str | `"realized5_pmin"` | key into `shared['sigmas']`; `'weighted'` routes weighted path |
| `tau_mode` | str | `"linear"` | linear/sqrt/else→1.0 |
| `trade_side` | str | `"NO"` | NO/YES/BOTH |
| `max_entry_price` | float | `0.95` | reject if entry > this (caley .get 0.80, ml_evgate .get 0.82, noob .get 0.95) |
| `stake` | float | `10` | USD per trade |
| `min_stake` | float | `5` | **carried, unused** in core path |
| `paused` | bool | `False` | restore: `v.lower()=='true'`; update: `bool(v)` |
| `strategy_type` | str | `"pmodel"` | pmodel/noob_fader/ml_evgate/caley |
| `threshold_gap` | float | `0.10` | noob_fader fade threshold |
| `entry_wait_min` | float | `3.0` | minutes into window before entry |
| `liq_filter` | str | `"on"` | `'on'`→spread/depth gate |
| `vol_imb_kill` | float | `0.0` | >0 enables vol-imbalance kill |
| `kill_hours` | str | `""` | CSV UTC hours to block (F5) |
| `ofi_align` | str | `"off"` | `'on'`→require OFI sign = delta sign |
| `ev_thr` | float | `0.0` | caley/ml_evgate EV gate (.get 0.15/0.18) |
| `refit_gate` | str | `""` | `""`=off, `"1"`=on (logistic P>entry) |
| `refit_margin` | (str→float at use) | `0.0` | margin in REFIT gate; **NOT in float-parse sets** |

**NOT in DEFAULT_CONFIG (create-only):**
| Key | Default | Notes |
|---|---|---|
| `sigma_max` | absent (no cap) | `cfg.get`; if set and `sigma > sigma_max` → `'SIGMA-HIGH'`. Distinguishes `s_sigfilt`. |
| `uncertainty_eps` | env `UNCERTAINTY_EPS` (0.0) | noob_fader skip if `|p_cal-0.5| < eps` |

### 9.2 Persistence mapping

- `_save_session`: every config key → `paper_config` row `(session_id, 'cfg_'+key, str(value))`; state → `'st_'+key`. ALL `str()`-ified. Table PK `(session_id, key)`, `INSERT OR REPLACE`.
- **Float-parse set** (identical in `restore_from_db` and `update_session_config`): `kappa, delta_threshold, p_model_threshold, max_entry_price, stake, min_stake, threshold_gap, entry_wait_min, vol_imb_kill, ev_thr`. `paused`→bool. All others raw string.
- `restore_from_db`: applies `cfg_` key **only if `real_key in cfg`** (DEFAULT_CONFIG). Skips sessions with `'__deleted__'=='true'`. State ints: `total_trades/wins/losses/buys_this_window → int(float(v))`; floats: `balance/starting_balance/total_pnl/total_staked/last_buy_ts`.
- `update_session_config`: mutates **only keys already in config** (`if k in s['config']`). Cannot add new keys.
- **`sigma_max`/`uncertainty_eps` can ONLY enter via `create_session`'s `cfg.update(config)`** (bypasses the key filter). They are silently dropped by both `update_session_config` and `restore_from_db` ⇒ NOT durable across restart unless added to DEFAULT_CONFIG. **Latent persistence bug** — re-implementers should add them to DEFAULT_CONFIG to make them survive restart.

### 9.3 Default session state

| Key | Default |
|---|---|
| `balance` / `starting_balance` | `10000.0` (create_session arg) |
| `total_pnl`, `total_staked` | `0.0` |
| `total_trades`, `wins`, `losses`, `buys_this_window` | `0` |
| `last_buy_ts` | `0` |

### 9.4 Strategy variants table

Variant identity = `(strategy_type, sigma_type, per-session knob values)`. **The 12 named variants below have NO seeding code in the repo** — they exist only as `paper_config` rows created at runtime via `/api/session/create` + `/api/session/config`. The only code-seeded sessions are `s1` (fallback, `name='NO realized5'`) and the auto-created weighted session (`_ensure_weighted_session`). To re-implement 1:1 you must dump each variant's `cfg_*` from the live DB.

| Variant | Distinguishing config |
|---|---|
| `s_third` | classic base; `sigma_type` = `realized5_pmin` (or third's `max30`), `entry_wait_min=3` |
| `s_entry150` | base + `entry_wait_min = 2.5` (150 s) |
| `s_sigfilt` | base + `sigma_max` cap (create-only key) |
| `f1_d50cap75` | `delta_threshold = 50` + `max_entry_price = 0.75` |
| `f2_caley` | `strategy_type = 'caley'` (`ev_thr ≈ 0.15`, max_entry 0.80) |
| `f4_ml_ev` | `strategy_type = 'ml_evgate'` (`ev_thr ≈ 0.18`, max_entry 0.82, delta 15) |
| `f5_hourofi` | base + `kill_hours` set + `ofi_align = 'on'` |
| `f6_wait270` | base + `entry_wait_min = 4.5` (270 s) |
| `live_today` / `live_yest` | PROD-aligned classic configs scoped to a date |
| `weighted` | `sigma_type = 'weighted'` (strategy_type stays `pmodel`); auto-seeded with kappa/delta_threshold/p_model_threshold all 0, tau_mode `'none'`, trade_side `'BOTH'`, stake 100 |
| `s_refit` | base + `refit_gate = '1'` (+ `refit_margin`) |

**Critical:** the `third` family in PROD typically uses `sigma_type='max30'`. `DEFAULT_CONFIG`'s `realized5_pmin` is NOT the third config — a third-equivalent variant must explicitly set `sigma_type='max30'`.

### 9.5 REFIT logistic (`s_refit`) — VERBATIM

```python
_REFIT = {"mu": [34.7207, -0.1901, 97.704], "sd": [30.5271, 46.2319, 27.7529],
          "coef": [1.12247, -0.08119, -0.20489], "intercept": 1.88440}
def _refit_pfit(dfo, tau_min):
    try:
        tau_s = float(tau_min or 0) * 60.0          # MINUTES→SECONDS (model trained on seconds)
        feats = [abs(float(dfo)), float(dfo), tau_s]
        z = _REFIT["intercept"] + sum(c*(f-m)/s for c,f,m,s
                                      in zip(_REFIT["coef"], feats, _REFIT["mu"], _REFIT["sd"]))
        return 1.0 / (1.0 + math.exp(-z))
    except Exception:
        return None
```
Standardized logistic on `[|delta_from_open|, delta_from_open, tau_seconds]`. **Unit gotcha:** `tau_min*60` is mandatory.

---

## 10. Constants Reference

### 10.1 `config.py`

| Constant | Value | Meaning |
|---|---|---|
| `BINANCE_TRADES_WS` | `wss://stream.binance.com:9443/ws/btcusdt@aggTrade` | price + trade_buffer |
| `BINANCE_DEPTH_WS` | `wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms` | bn_bid1/ask1 + bn_spread |
| `POLY_RTDS_WS` | `wss://ws-live-data.polymarket.com` | chainlink_price (fallback/oracle) |
| `POLY_CLOB_WS` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | orderbook + last trades |
| `GAMMA_EVENTS_URL` | `https://gamma-api.polymarket.com/events` | discovery + resolution |
| `CLOB_BOOK_URL` | `https://clob.polymarket.com/book` | REST deep book |
| `BITCOIN_TAG_ID` | `"235"` | Gamma BTC tag (15m branch only) |
| `HTTP_PORT` | `8888` | dashboard port |
| `P_MODEL_KAPPA` | `0.5` | κ |
| `SIGMA_DEFAULT` | `0.0005` | cold-start/static sigma; seeds `state['sigma']` |
| `P_MODEL_THRESHOLD` | `0.60` | min p (helpers.update_signal) |
| `DELTA_THRESHOLD` | `20` | min |delta| USD (helpers.update_signal) |
| `TAU_MODE` | `"linear"` | f_tau mode |
| `SIGMA_TYPE` | `"realized5_pmin"` | default sigma key |
| `TRADE_SIDE` | `"NO"` | side filter |
| `MAX_ENTRY_PRICE` | `0.95` | hard entry cap (helpers.update_signal) |
| `PAPER_STAKE` | `10` | per-trade stake |
| `PAPER_MIN_STAKE` | `5` | min stake floor |
| `WINDOW_MINUTES` | `5` | window length |
| `OB_MAX_LEVELS` | `20` | levels per side |
| `BASE` | `dirname(dirname(abspath(__file__)))` | perp_sim dir |
| `DB_PATH` | `<BASE>/live_data.db` | SQLite ledger |

### 10.2 `paper_trading.py` (NOT in config.py)

| Constant | Value | Meaning |
|---|---|---|
| `FEE_RATE` | `0.25` | Polymarket crypto fee (eff. 2026-03-06) |
| `FEE_EXPONENT` | `2` | exponent on `price·(1-price)` |
| `calc_fee` floor | `0.0001` | min fee in shares |
| `LIQ_SPREAD_KILL` | `0.0142` | max PM spread |
| `LIQ_DEPTH_KILL` | `424.0` | min depth (ask_vol+bid_vol) |
| `LIQ_FILTER_ENABLED` | `True` | global LIQ master switch |
| `_UNCERTAINTY_EPS` | `float(env UNCERTAINTY_EPS, 0.0)` | noob gate |
| `_REFIT` | see §9.5 | s_refit logistic |
| default balance | `10000.0` | starting balance |
| max-per-window | `1` | hardcoded 1 trade/window |
| history cap | `200` | resolved trades kept (info returns first 30) |
| `_NOOB_MODEL_PATH` | `/root/doubletrade2notactual/models/new_noob_fresh.lgb` | noob LGBM |
| `_NOOB_CAL_PATH` | `…/new_noob_fresh_cal.json` | noob calibrator |
| `_F2_ISO_PATH` | `/root/paper_compare/dashboard/artifacts/f2_iso.json` | caley isotonic |
| `_F4_MODEL_PATH`/`_F4_META_PATH` | `…/f4_model.lgb`, `…/f4_meta.json` | ml_evgate |

### 10.3 `orderbook.py` / `helpers.py` / loop cadences

| Constant | Value | Meaning |
|---|---|---|
| `_APPLY_INTERVAL` | `0.2` | price_change batch flush interval |
| price match tol | `0.0001` | delta upsert level-match |
| dedup price round | `4` decimals | dedup key |
| resolved thresholds | `bid≤0.05 \| ask≥0.95 \| spread>0.85` | `_is_resolved_book` |
| price_history retention | `2100` s (35 min) | `_price_history` |
| trade_buffer retention | `300` s (5 min) | OFI |
| `_warmup_ready` count | `600` | DB prices to set warm |
| `warmup_time` | `60` if warm else `1800` s | session eval gate |
| `entry_min` (helpers) | `3` min | update_signal wait |
| BTC fallback | `70000` | compute_p_model |
| tau fallback | floor 0.1; 7.5 (update_delta) / 2 (shared) | |
| z-score clamp | `[-5.0, 5.0]` | weighted |
| `stds_safe` threshold | `1e-10` → 1.0 | weighted |
| state_updater | `0.3` s | sigma/ofi/delta/signal + sessions |
| orderbook_reader | `1.5` s | REST deep fetch |
| window_tracker | `2` s | window detection |
| data_recorder | `1.0` s (ob snap ÷5 ≈ 5 s) | persistence |
| paper_gamma_resolver_loop | `20` s | per-position Gamma fallback |
| Gamma resolve timeout | `5` s | |
| collector snapshot | `http://127.0.0.1:9999/snapshot`, timeout 2 | Deribit fields |
| WS backoff | `1.0 → min(×2, 30)` s | all consumers |
| ping_interval | Binance 20 / RTDS 10 (+app PING 5) / CLOB None | |

---

## 11. Re-Implementation Checklist / Gotchas

1. **`poly_` prefix reused across venues.** All Polymarket-CLOB orderbook keys are `poly_*` (`poly_yes_ask`, `poly_no_ask`, `poly_last_trade_expensive`, etc.). The same field naming is reused for Kalshi/other venues in sibling deployments — when porting, do not assume `poly_` means Polymarket-specific data shape; treat it as "the venue book." Side mapping (`yes`/`no`) and token-id resolution are venue-agnostic in the engine.

2. **tau is NOT normalized by `WINDOW_MINUTES`.** `tau = max((w_end − now).total_seconds()/60, 0.1)` — raw minutes-to-expiry, floor 0.1, default **2** in `state_updater`, **7.5** in `helpers.update_delta`. Do NOT divide by window length. For a 15-min config the slug prefix becomes `btc-updown-15m-` and a separate `btc_15m_market_json` discovery branch activates, but tau math is unchanged (still `/60`).

3. **`last_trade_expensive` is a proxy from REAL trades only.** It comes ONLY from `last_trade_price` WS events, NEVER from the book and NEVER from REST. Two-sided branch = `max/min` of two possibly-stale last trades (so `expensive+cheap ≠ 1` in general); single-side fallback folds price around 0.5 ignoring which side traded. Until a trade prints it stays `None`. Entry sourcing prefers it (`>0.5`) before `orderbook_entry`.

4. **No slippage, no delay, no partial fills.** `raw_shares = stake/entry_price` at the exact quoted price; `shares = raw_shares − fee_in_shares` rounded 2dp; win pays `shares × $1`. There is no orderbook walk, no `ob_shares` cap enforcement (it's carried but not applied to size), no execution latency, no maker/taker distinction. This is an **idealized fill** — entry executes at the quote with zero market impact.

5. **Two different SNR formulas — do NOT unify.** `compute_p_model`/classic: `snr = |delta| / (sigma·BTC · f_tau)` with `f_tau = tau_minutes` (LINEAR, per `tau_mode`). Weighted: `snr = |delta| / (sigma·BTC · sqrt(max(tau,0.1)))` (sqrt ALWAYS, regardless of `tau_mode`). caley/ml_evgate: `snr = |delta| / (sigma·BTC · max(tau,0.1))` (LINEAR always).

6. **Delta source differs by path.** classic/caley/ml_evgate gate on `delta_from_open`; weighted + noob_fader use `delta_from_prev`. The dual-confirmation (AMBIGUOUS) gate compares BOTH and runs in classic ONLY. The persisted `trade_dict['delta']` is `delta_from_prev`. The `binance_delta` resolution fallback uses `delta_from_open or delta_from_prev or 0`.

7. **sigma is FRACTIONAL until ×BTC price.** `state['sigma']` and all `sigmas{}` are dimensionless (std/mean or std-of-returns). USD-ization happens at signal time (`sigma·price`). Don't double-multiply. `SIGMA_DEFAULT=0.0005` (config) and `SIGMA_STATIC=0.0005` (helpers) are two separate literals with the same value — keep in sync.

8. **`p == None` PASSES the p-threshold.** `if p is not None and p < pt`. When sigma/price invalid, the threshold check is skipped, not failed. Intentional.

9. **`>0.5` entry floor is a hard, separate filter** from `max_entry_price`. Classic/weighted reject any entry `<= 0.5` (`'NO PRICE'`); caley/ml_evgate reject `<= 0.50`. The strategy buys the WINNING (expensive) side, so entry must be `>0.5`. Cheap/out-of-money prices are never traded in these paths.

10. **Fee is in SHARES, balance debited by full stake.** `shares = raw_shares − calc_fee(...)`, `balance -= stake` (not stake+fee). Fee reduces payout (`shares×$1`), not the stake debit.

11. **1 trade/window keyed on `window_start` change, not a cooldown.** If `window_start` is stale/None the window never resets and trading stalls; if it flips spuriously it could allow extra trades.

12. **Resolution strictness.** Gamma `outcomePrices` only when `closed==True` AND binary `['1','0']`/`['0','1']`. `['0.5','0.5']` must NOT resolve. Gamma-reachable-but-open → DEFER (no resolve). Only Gamma-unreachable → orderbook (`yes_bid>no_bid`, needs `market_resolved` + both bids) else binance_delta. The binance_delta path can disagree with on-chain Chainlink truth (~4.7%) — degraded path only.

13. **Last-writer-wins orderbook race.** REST (`src='rest'`, 1.5 s) and WS (`ws_book`/`ws_delta`) both write the same `orderbook`/`poly_*` keys with no precedence. price_change deltas are batched/deduped (200 ms, eventually-consistent). Replicate this or diverge.

14. **Module-global mutable singletons.** `_pending_changes`, `_rest_hashes`, `_rest_fetch_count`, `_last_apply_time`, `_market_rediscovery_requested[0]`, `_clob_msg_count[0]`, `_price_history`, `_sigma_cache`, `_weighted_model`, `_warmup_ready`, `_sessions`. The engine assumes a **single collector process** — multiple instances in one process would clobber these.

15. **Weighted model file-path mismatch.** `load_weighted_model()` resolves `optimized_weights.json` to `perp_sim/` (`dirname(dirname(__file__))`), but the actual file lives at repo root. In this exact tree the model **fails to load** (`_weighted_model` stays None → `compute_weighted_score` returns `(None,None,None)` → `'NO SCORE'`, no trade). When re-implementing, place the JSON next to the package parent OR fix the path.

16. **Absolute PROD model paths.** noob/caley/ml_evgate artifacts are `/root/...` absolute. On a different host they fail to load and those strategies return `*_LOAD_ERR` (no trade) — graceful but silently inert.

17. **`noob_fader` time gate is `[60,180)` SECONDS** (from `window_start_utc` vs now), NOT `entry_wait_min`/`elapsed_min`. `_NOOB_FEATURES` has `delta_from_prev` duplicated at positions 3-4 — intentional, replicate exactly.

18. **`paused` parsing diverges:** `restore_from_db` uses `v.lower()=='true'` (correct); `update_session_config` uses `bool(v)` (`bool('false')` is True). Masked in practice because the dashboard uses dedicated `/api/session/pause|resume` endpoints, but a raw config POST with `paused='false'` would PAUSE.

19. **`max_entry`/`ev_thr`/`delta_threshold` have per-path `.get` defaults** differing from DEFAULT_CONFIG: caley (ev 0.15, max 0.80, delta 20); ml_evgate (ev 0.18, max 0.82, delta 15); noob (max 0.95). DEFAULT_CONFIG: ev_thr 0.0, delta 20, max 0.95.

20. **`_phi` uses exact `math.erf` (libm)**, not an approximation. p is rounded only 4dp, so a porting target must match erf to high precision.

21. **Window reset leaves `poly_*_bid/ask/_vol` stale** for up to ~1.5 s after a flip (only `*_last_trade*` + book are cleared). Sessions can momentarily see the previous market's quotes.

22. **`min_stake` is carried but unused** in the core path; `sigma_max`/`uncertainty_eps`/`refit_margin` are read with `.get`+`float()` at use site and are NOT in the float-parse/persistence sets (string-stored, create-only for the first two).