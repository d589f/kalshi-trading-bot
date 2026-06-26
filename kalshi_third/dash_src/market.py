"""Market discovery — find current BTC 5m market on Polymarket."""
import json
import sys
from datetime import datetime, timezone, timedelta

from .config import WINDOW_MINUTES, GAMMA_EVENTS_URL, BITCOIN_TAG_ID
from .state import state, state_lock

try:
    import requests as req_lib
except ImportError:
    req_lib = None


def _parse_market_slug(slug):
    slug_lower = (slug or "").lower()
    for prefix, mins in [("btc-updown-5m-", 5), ("btc-updown-15m-", 15)]:
        if slug_lower.startswith(prefix):
            try:
                ts = int(slug_lower.split("-")[-1])
                start = datetime.fromtimestamp(ts, tz=timezone.utc)
                end = start + timedelta(minutes=mins)
                return start, end, mins
            except (ValueError, OSError):
                pass
    return None


def _extract_market_info(m, slug):
    """Extract token IDs and assign YES/NO from a market dict."""
    token_ids = json.loads(m.get("clobTokenIds") or "[]")
    outcomes = m.get("outcomes") or "[]"
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if len(token_ids) >= 2:
        yes_tok, no_tok = token_ids[0], token_ids[1]
        if len(outcomes) >= 2:
            o0 = (outcomes[0] or "").lower()
            if o0 not in ("up", "yes"):
                yes_tok, no_tok = token_ids[1], token_ids[0]
        with state_lock:
            state["yes_token_id"] = yes_tok
            state["no_token_id"] = no_tok
        print(f"[market] Found {slug}, YES={yes_tok[:12]}..., NO={no_tok[:12]}...", flush=True)
        return slug, list(token_ids)
    return None, []


def find_current_market():
    if not req_lib:
        return None, []

    now = datetime.now(timezone.utc)

    if WINDOW_MINUTES == 15:
        try:
            from btc_15m_market_json import fetch_active_events, find_current_15m_market
            events = fetch_active_events(BITCOIN_TAG_ID)
            market = find_current_15m_market(events)
            if market and market.get("yes_up_id") and market.get("no_down_id"):
                return market.get("slug", ""), [market["yes_up_id"], market["no_down_id"]]
        except Exception as e:
            print(f"[dashboard] btc_15m_market_json error: {e}", file=sys.stderr)

    # ── PRIMARY: construct slug from current time window ──
    # Polymarket slugs encode the window start as a Unix timestamp.
    # This is the most reliable way to find the correct market for NOW.
    slot = (now.minute // WINDOW_MINUTES) * WINDOW_MINUTES
    w_start = now.replace(minute=slot, second=0, microsecond=0)
    target_ts = int(w_start.timestamp())
    target_slug = f"btc-updown-{WINDOW_MINUTES}m-{target_ts}"

    try:
        resp = req_lib.get(GAMMA_EVENTS_URL, params={"slug": target_slug}, timeout=10)
        resp.raise_for_status()
        events = resp.json() or []
        if events:
            ev = events[0]
            markets = ev.get("markets") or []
            if markets:
                m = markets[0]
                vol = m.get("volume") or 0
                prices = m.get("outcomePrices")
                print(f"[market] Direct slug hit: {target_slug} "
                      f"(vol={vol}, prices={prices})", flush=True)
                return _extract_market_info(m, target_slug)
    except Exception as e:
        print(f"[market] Direct slug lookup failed: {e}", file=sys.stderr, flush=True)

    # ── FALLBACK: try recent past windows (in case we're between windows) ──
    for offset_min in range(WINDOW_MINUTES, WINDOW_MINUTES * 4, WINDOW_MINUTES):
        past_start = w_start - timedelta(minutes=offset_min)
        past_ts = int(past_start.timestamp())
        past_slug = f"btc-updown-{WINDOW_MINUTES}m-{past_ts}"
        try:
            resp = req_lib.get(GAMMA_EVENTS_URL, params={"slug": past_slug}, timeout=10)
            resp.raise_for_status()
            events = resp.json() or []
            if events:
                ev = events[0]
                markets = ev.get("markets") or []
                if markets:
                    m = markets[0]
                    closed = m.get("closed", False)
                    if not closed:
                        print(f"[market] Fallback to recent: {past_slug}", flush=True)
                        return _extract_market_info(m, past_slug)
                    else:
                        print(f"[market] {past_slug} already closed, skipping", flush=True)
        except Exception as e:
            print(f"[market] Fallback lookup error: {e}", file=sys.stderr, flush=True)

    # ── LAST RESORT: old method — scan closed=false list ──
    target_tag = f"-{WINDOW_MINUTES}m-"
    try:
        url = f"{GAMMA_EVENTS_URL}?closed=false&limit=50&offset=0&order=id&ascending=false"
        r = req_lib.get(url, timeout=10)
        r.raise_for_status()
        events = r.json() or []

        candidates = []
        for ev in events:
            slug = (ev.get("slug") or "").lower()
            if target_tag not in slug or not slug.startswith("btc-"):
                continue
            markets = ev.get("markets") or []
            if not markets:
                continue
            parsed = _parse_market_slug(slug)
            if not parsed:
                continue
            ws, we, _ = parsed
            candidates.append({"slug": slug, "market": markets[0],
                              "w_start": ws, "w_end": we})

        # Find market whose window contains NOW
        current = [c for c in candidates if c["w_start"] <= now < c["w_end"]]
        if current:
            best = current[0]
            print(f"[market] Fallback scan found: {best['slug']}", flush=True)
            return _extract_market_info(best["market"], best["slug"])

        # Nearest past
        past = [c for c in candidates if c["w_start"] <= now]
        if past:
            past.sort(key=lambda c: c["w_start"], reverse=True)
            best = past[0]
            print(f"[market] Fallback scan, most recent: {best['slug']}", flush=True)
            return _extract_market_info(best["market"], best["slug"])

    except Exception as e:
        print(f"[dashboard] Gamma API error: {e}", file=sys.stderr, flush=True)

    print("[market] No BTC market found!", flush=True)
    return None, []
