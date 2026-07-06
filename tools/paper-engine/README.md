# paper-engine WS book feed (deployed 2026-07-06 13:15Z)

kalshi_ws.py — WebSocket top-of-book consumer for the PYTHON paper engine on the EU box
(/root/paper_compare_kalshi_15m/dashboard/kalshi_ws.py). NOT part of the Rust bot.
Kept here for persistence — the engine dir is not a git repo.

WHY: the engine polled the Kalshi book via REST every ~3.3-4s; measured lag vs the real
book: median 1c / p90 4c quiet, +8..16c on dumps -> paper "filled" at prices that no
longer existed (pink line overstated by ~$11/day; live no-fills on phantom prices).

WHAT: subscribes the Kalshi WS `ticker` channel (yes_bid_dollars/yes_ask_dollars per
change; auth = same signed headers as REST, key /home/dmitrii/.kalshi_live.pem).
Updates ONLY price fields via the same complement mapping as ll_ws._apply; volumes kept
from REST (ticker has no depth; zeroing would trip liq-filter sessions). Stamps
kalshi_ws_ts + ll_ob_ts.

INTEGRATION (backups *.bak_ws_20260706 on box):
- dashboard/ll_ws.py poll_ll_orderbook_rest: skips its _apply while kalshi_ws_ts < 4s
  old (REST = discovery + automatic fallback; WS dead -> old behavior within 4s).
- dashboard/__main__.py: import + task "kalshi_ws" + coro_map (supervisor auto-restart).

VERIFIED: ask updates sub-second (age 0.1-1.7s vs 3-4s before); engine load ok.
GOTCHA: engine restart => ~30-40 min WARMUP (state.signal = "WARMUP (...s remaining)"),
sessions don't evaluate, live_sigma=0, the Rust bot fails closed (no trades) until it
ends. Restart the engine only when this cost is acceptable.
ROLLBACK: restore dashboard/{ll_ws.py,__main__.py}.bak_ws_20260706, rm kalshi_ws.py,
kill run_kalshi_15m.py (keepalive relaunches).
