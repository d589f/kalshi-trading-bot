"""Parallel BTC 5m orderbook archiver — pmxt-grade event stream.

Runs alongside the dashboard's main collector. Does NOT modify `state` dict,
does NOT interfere with paper trading. Pure output.

Design:
  • Async task #1: discover_upcoming() — every 60s refresh list of BTC 5m
    asset_ids for current + next 5 windows (25 min ahead).
  • Async task #2: ws_loop() — maintain single CLOB WS connection, SUBSCRIBE
    new asset_ids as they appear, push events to queue.
  • Async task #3: writer_loop() — consume queue, batch-flush to parquet
    every 2s. New file per hour (UTC boundary).
  • Async task #4: cleanup_loop() — delete archive files older than N days.

Schema is identical to pmxt archive so files can be cross-inspected with
same tools (scripts/inspect_pmxt_archive2.py works on our files too).
"""
from __future__ import annotations
import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
import websockets
import pyarrow as pa
import pyarrow.parquet as pq

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

ARCHIVE_DIR = Path("/root/doubletrade2notactual/archive")
LOOKAHEAD_MIN = 25          # subscribe N minutes ahead
GAMMA_POLL_SEC = 60         # rediscover upcoming markets
FLUSH_SEC = 2.0             # flush batch to parquet
CLEANUP_DAYS = 14           # delete files older than N days
WS_PING_SEC = 20            # websocket ping

# Schema matches pmxt archive exactly for cross-compat.
# Using float64 instead of decimal128 for simpler Python handling — still
# accurate at Polymarket's 0.01 tick granularity, and fits fine in parquet.
SCHEMA = pa.schema([
    ("timestamp_received", pa.timestamp("ms", tz="UTC")),
    ("timestamp",          pa.timestamp("ms", tz="UTC")),
    ("market",             pa.binary()),      # 66-byte condition_id as ASCII
    ("event_type",         pa.string()),
    ("asset_id",           pa.string()),
    ("bid_prices",         pa.list_(pa.float64())),
    ("bid_sizes",          pa.list_(pa.float64())),
    ("ask_prices",         pa.list_(pa.float64())),
    ("ask_sizes",          pa.list_(pa.float64())),
    ("price",              pa.float64()),
    ("size",               pa.float64()),
    ("side",               pa.string()),
    ("best_bid",           pa.float64()),
    ("best_ask",           pa.float64()),
])


def _log(msg: str) -> None:
    print(f"[archiver] {msg}", flush=True)


# ── Market discovery (upcoming BTC 5m windows) ──

def _round_down_5min(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)


def discover_btc5m_assets() -> dict[str, dict]:
    """Return {asset_id: {slug, condition_id, window_start, role}}.

    role = 'yes' or 'no'.
    Covers current window + next LOOKAHEAD_MIN/5 windows.
    """
    now = datetime.now(timezone.utc)
    cur = _round_down_5min(now)
    # current, +5, +10, +15, +20 min
    slots = [cur + timedelta(minutes=5 * i)
             for i in range(LOOKAHEAD_MIN // 5 + 1)]
    out: dict[str, dict] = {}
    for slot in slots:
        ts = int(slot.timestamp())
        slug = f"btc-updown-5m-{ts}"
        try:
            r = requests.get(GAMMA_URL, params={"slug": slug}, timeout=5)
            events = r.json() or []
        except Exception as e:
            _log(f"gamma {slug} error: {e}")
            continue
        if not events:
            continue
        markets = events[0].get("markets") or []
        if not markets:
            continue
        m = markets[0]
        try:
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            cid = m.get("conditionId") or ""
        except Exception:
            continue
        if len(token_ids) != 2:
            continue
        yes_tok, no_tok = token_ids[0], token_ids[1]
        if outcomes and len(outcomes) >= 2 and outcomes[0].lower() not in ("up", "yes"):
            yes_tok, no_tok = no_tok, yes_tok
        out[yes_tok] = {"slug": slug, "condition_id": cid,
                        "window_start": slot.isoformat(), "role": "yes"}
        out[no_tok] = {"slug": slug, "condition_id": cid,
                       "window_start": slot.isoformat(), "role": "no"}
    return out


# ── Rotation + batch writer ──

class HourlyWriter:
    """Writes parquet files rotated at UTC hour boundaries."""

    def __init__(self, archive_dir: Path):
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._hour: Optional[datetime] = None
        self._writer: Optional[pq.ParquetWriter] = None
        self._rows_written = 0
        self._file_rows = 0
        self._cur_path: Optional[Path] = None

    def _path_for(self, hour: datetime) -> Path:
        return self.archive_dir / f"btc5m_orderbook_{hour:%Y-%m-%dT%H}.parquet"

    def write(self, batch: list[dict]) -> None:
        if not batch:
            return
        now = datetime.now(timezone.utc).replace(minute=0, second=0,
                                                  microsecond=0)
        if self._hour != now:
            if self._writer:
                self._writer.close()
                _log(f"rotated: closed {self._cur_path.name} "
                     f"({self._file_rows} rows)")
            self._hour = now
            self._cur_path = self._path_for(now)
            self._writer = pq.ParquetWriter(self._cur_path, SCHEMA,
                                             compression="snappy")
            self._file_rows = 0
            _log(f"opened {self._cur_path.name}")
        # build arrow table
        tbl = pa.Table.from_pylist(batch, schema=SCHEMA)
        self._writer.write_table(tbl)
        self._rows_written += len(batch)
        self._file_rows += len(batch)

    def close(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None


# ── Main archiver ──

class PolyArchiver:
    def __init__(self):
        self.assets: dict[str, dict] = {}
        self.subscribed: set[str] = set()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=100_000)
        self.writer = HourlyWriter(ARCHIVE_DIR)
        self.running = True
        self._last_stats = time.monotonic()
        self._last_stats_rows = 0

    async def discover_loop(self):
        while self.running:
            try:
                new = await asyncio.to_thread(discover_btc5m_assets)
                if new:
                    added = set(new.keys()) - set(self.assets.keys())
                    self.assets.update(new)
                    if added:
                        _log(f"discovered {len(added)} new asset_ids "
                             f"(total {len(self.assets)})")
            except Exception as e:
                _log(f"discover error: {e}")
            await asyncio.sleep(GAMMA_POLL_SEC)

    async def ws_loop(self):
        backoff = 1.0
        while self.running:
            try:
                async with websockets.connect(
                    CLOB_WS, ping_interval=WS_PING_SEC, close_timeout=5,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    _log("ws connected")
                    backoff = 1.0
                    # initial subscribe
                    self.subscribed.clear()
                    if self.assets:
                        await self._send_subscribe(ws, list(self.assets.keys()))
                    while self.running:
                        # Check for new asset_ids to subscribe
                        pending = set(self.assets.keys()) - self.subscribed
                        if pending:
                            await self._send_subscribe(ws, list(pending))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        now_ms = int(time.time() * 1000)
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(data, list):
                            for ev in data:
                                self._handle_event(ev, now_ms)
                        elif isinstance(data, dict):
                            self._handle_event(data, now_ms)
            except Exception as e:
                _log(f"ws error: {type(e).__name__}: {e}; retry in {backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _send_subscribe(self, ws, asset_ids: list[str]) -> None:
        msg = {"assets_ids": asset_ids, "type": "market"}
        await ws.send(json.dumps(msg))
        self.subscribed.update(asset_ids)
        _log(f"subscribed +{len(asset_ids)} asset_ids (total {len(self.subscribed)})")

    def _handle_event(self, data: dict, now_ms: int) -> None:
        ev_type = data.get("event_type") or ""
        asset_id = data.get("asset_id") or ""
        # price_change has nested "price_changes" list with asset_id per change
        if ev_type == "price_change":
            changes = data.get("price_changes") or data.get("changes") or []
            for chg in changes:
                aid = chg.get("asset_id") or asset_id
                if aid not in self.assets:
                    continue
                row = self._build_row_price_change(chg, aid, now_ms, data)
                self._enqueue(row)
            return
        if asset_id and asset_id not in self.assets:
            return  # skip — not our market
        if ev_type == "book":
            row = self._build_row_book(data, asset_id, now_ms)
            self._enqueue(row)
        elif ev_type == "last_trade_price":
            row = self._build_row_trade(data, asset_id, now_ms)
            self._enqueue(row)
        elif ev_type == "tick_size_change":
            row = self._build_row_tick_size(data, asset_id, now_ms)
            self._enqueue(row)

    def _enqueue(self, row: dict) -> None:
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            _log("queue full! dropping event")

    def _parse_market_bytes(self, asset_id: str) -> bytes:
        """Return the condition_id as ASCII bytes (pmxt format), or zeros."""
        meta = self.assets.get(asset_id) or {}
        cid = meta.get("condition_id") or ""
        # pmxt stores "0x..." 66-byte ASCII
        if cid and not cid.startswith("0x"):
            cid = "0x" + cid
        return cid.encode("ascii", errors="replace") if cid else b""

    @staticmethod
    def _parse_ts(data: dict, now_ms: int) -> int:
        ts = data.get("timestamp")
        if ts is None:
            return now_ms
        try:
            ts = int(ts)
            if ts < 10**12:  # seconds → ms
                ts *= 1000
            return ts
        except (ValueError, TypeError):
            return now_ms

    def _build_row_book(self, data: dict, asset_id: str, now_ms: int) -> dict:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bp = [float(b.get("price", 0)) for b in bids]
        bs = [float(b.get("size", 0)) for b in bids]
        ap = [float(a.get("price", 0)) for a in asks]
        assz = [float(a.get("size", 0)) for a in asks]
        best_bid = max(bp) if bp else None
        best_ask = min(ap) if ap else None
        return {
            "timestamp_received": pa.scalar(now_ms, type=pa.timestamp("ms", tz="UTC")).as_py(),
            "timestamp":          pa.scalar(self._parse_ts(data, now_ms), type=pa.timestamp("ms", tz="UTC")).as_py(),
            "market":             self._parse_market_bytes(asset_id),
            "event_type":         "book",
            "asset_id":           asset_id,
            "bid_prices":         bp, "bid_sizes":  bs,
            "ask_prices":         ap, "ask_sizes":  assz,
            "price":              None, "size": None, "side": None,
            "best_bid":           best_bid,
            "best_ask":           best_ask,
        }

    def _build_row_price_change(self, chg: dict, aid: str, now_ms: int,
                                 parent: dict) -> dict:
        try:
            price = float(chg.get("price", 0))
            size = float(chg.get("size", 0))
        except Exception:
            price = 0; size = 0
        return {
            "timestamp_received": pa.scalar(now_ms, type=pa.timestamp("ms", tz="UTC")).as_py(),
            "timestamp":          pa.scalar(self._parse_ts(parent, now_ms), type=pa.timestamp("ms", tz="UTC")).as_py(),
            "market":             self._parse_market_bytes(aid),
            "event_type":         "price_change",
            "asset_id":           aid,
            "bid_prices":         [], "bid_sizes":  [],
            "ask_prices":         [], "ask_sizes":  [],
            "price":              price, "size": size,
            "side":               str(chg.get("side", "")).upper(),
            "best_bid":           float(chg.get("best_bid")) if chg.get("best_bid") else None,
            "best_ask":           float(chg.get("best_ask")) if chg.get("best_ask") else None,
        }

    def _build_row_trade(self, data: dict, aid: str, now_ms: int) -> dict:
        try:
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
        except Exception:
            price = 0; size = 0
        return {
            "timestamp_received": pa.scalar(now_ms, type=pa.timestamp("ms", tz="UTC")).as_py(),
            "timestamp":          pa.scalar(self._parse_ts(data, now_ms), type=pa.timestamp("ms", tz="UTC")).as_py(),
            "market":             self._parse_market_bytes(aid),
            "event_type":         "last_trade_price",
            "asset_id":           aid,
            "bid_prices":         [], "bid_sizes":  [],
            "ask_prices":         [], "ask_sizes":  [],
            "price":              price, "size": size,
            "side":               str(data.get("side", "")).upper(),
            "best_bid":           None, "best_ask": None,
        }

    def _build_row_tick_size(self, data: dict, aid: str, now_ms: int) -> dict:
        return {
            "timestamp_received": pa.scalar(now_ms, type=pa.timestamp("ms", tz="UTC")).as_py(),
            "timestamp":          pa.scalar(self._parse_ts(data, now_ms), type=pa.timestamp("ms", tz="UTC")).as_py(),
            "market":             self._parse_market_bytes(aid),
            "event_type":         "tick_size_change",
            "asset_id":           aid,
            "bid_prices":         [], "bid_sizes":  [],
            "ask_prices":         [], "ask_sizes":  [],
            "price":              None, "size": None, "side": None,
            "best_bid":           None, "best_ask": None,
        }

    async def writer_loop(self):
        batch: list[dict] = []
        last_flush = time.monotonic()
        while self.running:
            try:
                row = await asyncio.wait_for(self.queue.get(), timeout=FLUSH_SEC)
                batch.append(row)
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()
            if batch and (now - last_flush) >= FLUSH_SEC:
                try:
                    self.writer.write(batch)
                except Exception as e:
                    _log(f"write error: {e}")
                # stats
                if (now - self._last_stats) >= 60:
                    elapsed = now - self._last_stats
                    delta = self.writer._rows_written - self._last_stats_rows
                    _log(f"rate: {delta/elapsed:.0f} rows/s, "
                         f"total={self.writer._rows_written}, "
                         f"queue={self.queue.qsize()}")
                    self._last_stats = now
                    self._last_stats_rows = self.writer._rows_written
                batch = []
                last_flush = now

    async def cleanup_loop(self):
        while self.running:
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)
                for p in ARCHIVE_DIR.glob("btc5m_orderbook_*.parquet"):
                    try:
                        stem = p.stem.replace("btc5m_orderbook_", "")
                        t = datetime.strptime(stem, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
                        if t < cutoff:
                            p.unlink()
                            _log(f"deleted old file {p.name}")
                    except Exception:
                        pass
            except Exception as e:
                _log(f"cleanup error: {e}")
            await asyncio.sleep(3600)

    async def run(self):
        _log(f"starting; archive_dir={ARCHIVE_DIR}")
        try:
            await asyncio.gather(
                self.discover_loop(),
                self.ws_loop(),
                self.writer_loop(),
                self.cleanup_loop(),
            )
        finally:
            self.running = False
            self.writer.close()


# Entry point for live_dashboard.py integration.
async def main():
    await PolyArchiver().run()


if __name__ == "__main__":
    asyncio.run(main())
