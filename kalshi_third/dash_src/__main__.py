"""Entry point: python -m dashboard"""
import asyncio
import sys
import threading

from .config import HTTP_PORT
from .state import state, state_lock
from .helpers import _current_window, warmup_price_history, load_weighted_model
from .database import init_db
from .market import find_current_market
try:
    from .backtest import load_backtest_data
except Exception:
    def load_backtest_data(): pass
from .http_server import run_http
from .ws_consumers import (consume_binance_trades, consume_binance_depth,
                           consume_poly_rtds, consume_poly_clob)
from .tasks import (window_tracker, state_updater, data_recorder, orderbook_reader, paper_gamma_resolver_loop)
from .paper_trading import restore_from_db, create_session, get_all_sessions


def _ensure_weighted_session():
    """Create weighted session if it doesn't exist yet."""
    sessions = get_all_sessions()
    for sid, info in sessions.items():
        if info and info.get("config", {}).get("sigma_type") == "weighted":
            print(f"[dashboard] Weighted session already exists: {sid}", flush=True)
            return
    # Create new weighted session
    sid = f"s{int(__import__('time').time())}"
    create_session(sid, {
        "name": "weighted",
        "kappa": 0,              # not used for weighted
        "delta_threshold": 0,    # not used — model handles it
        "p_model_threshold": 0,  # not used
        "sigma_type": "weighted",  # triggers weighted path
        "tau_mode": "none",
        "trade_side": "BOTH",
        "max_entry_price": 0.95,  # overridden by model
        "stake": 100,
        "min_stake": 5,
    }, balance=10000.0)
    print(f"[dashboard] Created weighted session: {sid}", flush=True)


async def main():
    print("[dashboard] Starting BTC Trading Dashboard...", flush=True)

    init_db()
    restore_from_db()
    warmup_price_history()
    load_backtest_data()
    load_weighted_model()
    _ensure_weighted_session()

    http_thread = threading.Thread(target=run_http, daemon=True)
    http_thread.start()

    slug, token_ids = find_current_market()
    with state_lock:
        if slug:
            state["market_slug"] = slug
        if token_ids:
            state["market_token_ids"] = token_ids
    print(f"[dashboard] Market: {slug}, tokens: {token_ids}", flush=True)

    w_start, w_end = _current_window()
    with state_lock:
        state["window_start_utc"] = w_start.isoformat()
        state["window_end_utc"] = w_end.isoformat()

    tasks = [
        asyncio.create_task(consume_binance_trades(), name="binance_trades"),
        asyncio.create_task(consume_binance_depth(), name="binance_depth"),
        asyncio.create_task(consume_poly_rtds(), name="poly_rtds"),
        asyncio.create_task(consume_poly_clob(), name="poly_clob"),
        asyncio.create_task(window_tracker(), name="window_tracker"),
        asyncio.create_task(state_updater(), name="state_updater"),
        asyncio.create_task(data_recorder(), name="data_recorder"),
        asyncio.create_task(orderbook_reader(), name="orderbook_reader"),
        asyncio.create_task(paper_gamma_resolver_loop(), name="paper_gamma_resolver"),
    ]

    print(f"[dashboard] All streams started. Open http://localhost:{HTTP_PORT}", flush=True)

    coro_map = {
        "binance_trades": consume_binance_trades,
        "binance_depth": consume_binance_depth,
        "poly_rtds": consume_poly_rtds,
        "poly_clob": consume_poly_clob,
        "window_tracker": window_tracker,
        "state_updater": state_updater,
        "data_recorder": data_recorder,
        "orderbook_reader": orderbook_reader,
        "paper_gamma_resolver": paper_gamma_resolver_loop,
    }

    while True:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = task.get_name()
            try:
                exc = task.exception()
                if exc:
                    print(f"[dashboard] Task {name} CRASHED: {type(exc).__name__}: {exc}",
                          file=sys.stderr, flush=True)
                    import traceback
                    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
                else:
                    print(f"[dashboard] Task {name} finished unexpectedly",
                          file=sys.stderr, flush=True)
            except asyncio.CancelledError:
                print(f"[dashboard] Task {name} was cancelled", file=sys.stderr, flush=True)

            if name in coro_map:
                print(f"[dashboard] Restarting {name}...", flush=True)
                new_task = asyncio.create_task(coro_map[name](), name=name)
                pending.add(new_task)

        tasks = list(pending)
        if not tasks:
            print("[dashboard] All tasks dead, exiting", file=sys.stderr, flush=True)
            break
        await asyncio.sleep(1)


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[dashboard] Stopped.", flush=True)
    except Exception as e:
        print(f"\n[dashboard] FATAL: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    run()
