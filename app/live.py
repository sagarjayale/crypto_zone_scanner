"""Live mode: mark prices of every coin from the Binance WebSocket (the all-market mark price stream, one connection for
every coin, pushed each second), alerts, paper fills, and the push of prices and alerts to the pages.

Binance routes USDT-M futures streams by kind since 2026: mark prices are a "market" stream, served from
wss://fstream.binance.com/market/ws/... The old wss://fstream.binance.com/ws/... address still accepts a connection but
has sent no mark prices since 23 April 2026, so a connection that stays silent is treated as broken.

The stream reconnects on its own with a growing pause, and Binance closes every connection after 24 hours, which is
handled the same way. While it is down or silent, mark prices are fetched over REST every few seconds instead (one call
for every coin), so alerts and paper fills carry on."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import datetime, timedelta

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config, settings_store
from .data.sync import now_utc
from .db import SessionLocal
from .alerts import telegram
from .models import Instrument, TradePlan, Zone, ZoneEvent
from .sim import paper
from .binance.client import BinanceError

STREAM = "!miniTicker@arr"      # last traded price of every coin, once a second (as TradingView and Binance order fills)
STREAM_PATH = "/market/ws/"     # Binance's routed address for market streams (mark price, klines, trades)
SILENT_AFTER = 15               # seconds without a message before the connection is dropped and opened again
log = logging.getLogger(__name__)
PROCESS_EVERY = 1.0     # seconds between two rounds of alert and paper checks
REST_EVERY = 5          # seconds between REST polls while the WebSocket is down
STALE_AFTER = 15        # seconds without a price before prices are shown as stale


class Hub:
    """Pages connected to /ws/live."""
    def __init__(self):
        self.clients: set[WebSocket] = set()

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.clients.discard(ws)


hub = Hub()
state = {"prices": {}, "last_poll": None, "error": None, "running": False, "last_scan": None, "task": None,
         "source": None, "connected": False, "reconnects": 0, "last_message": None, "url": None, "stopped": None}
_sent: set[tuple[int, str]] = set()   # (zone id, alert type) already raised


def market_open(now: datetime) -> bool:
    """Crypto never closes."""
    return True


def next_bar_close(now: datetime) -> datetime:
    """The next 5-minute candle close (UTC)."""
    t = now.replace(second=0, microsecond=0)
    return t + timedelta(minutes=5 - t.minute % 5)


def watch_rows(db: Session) -> list:
    """Fresh zones with a pending plan in some mode (the HTF curve and ITF trend allow the trade) at or above the minimum
    score: the best LTF zone of each coin and mode, or every qualifying one with the all_ltf_zones setting."""
    floor = settings_store.get(db, "min_score")
    rows = db.execute(select(Zone, TradePlan).join(TradePlan, TradePlan.zone_id == Zone.id).where(
        Zone.status == "fresh", TradePlan.is_current.is_(True), TradePlan.status == "pending",
        TradePlan.allowed.is_(True), Zone.score >= floor)).all()
    if settings_store.get(db, "all_ltf_zones"):
        return rows
    # Only the best LTF zone of each coin and mode (rank 0, the one on the dashboard row) alerts and gets automatic
    # paper orders. A plan saved before the rank was recorded counts as the best until its next scan.
    return [(z, p) for z, p in rows if int((p.context or {}).get("rank") or 0) == 0]


def check_prices(db: Session, prices: dict[str, float], now: datetime) -> list[dict]:
    """Compare live prices with every watched zone. Each alert fires once per zone:
    approaching (within approach_pct percent of the proximal line) and triggered (price reaches the proximal line).
    Reaching the line fills an entry type 1 plan. A plan waiting for entry type 2 or 3 stays pending until the scan sees the candles confirm."""
    rows = watch_rows(db)
    if not rows:
        return []
    ids = [z.id for z, _ in rows]
    for zid, etype in db.execute(select(ZoneEvent.zone_id, ZoneEvent.event_type).where(
            ZoneEvent.zone_id.in_(ids), ZoneEvent.event_type.in_(("alert_approaching", "alert_triggered")))):
        _sent.add((zid, etype))
    reach = float(settings_store.get(db, "approach_pct")) / 100.0
    names = dict(db.execute(select(Instrument.instrument_key, Instrument.symbol).where(
        Instrument.instrument_key.in_({z.instrument_key for z, _ in rows}))).all())
    alerts = []
    for z, p in rows:
        ltp = prices.get(z.instrument_key)
        if ltp is None:
            continue
        if z.side == "demand":
            reached, near = ltp <= z.proximal, z.proximal < ltp <= z.proximal * (1 + reach)
        else:
            reached, near = ltp >= z.proximal, z.proximal * (1 - reach) <= ltp < z.proximal
        kind = "alert_triggered" if reached else "alert_approaching" if near else None
        if kind is None or (z.id, kind) in _sent:
            continue
        _sent.add((z.id, kind))
        db.add(ZoneEvent(zone_id=z.id, ts=now, event_type=kind, price=ltp))
        if kind == "alert_triggered":
            if (p.entry_type or "TYPE_1") == "TYPE_1":      # a limit at the proximal line is filled; types 2 and 3 wait for the candles
                p.status, p.triggered_ts = "triggered", now
            _sent.add((z.id, "alert_approaching"))   # no approach alert once price is at the zone
        alerts.append({"type": "alert", "alert": kind[6:], "zone_id": z.id, "instrument_key": z.instrument_key,
                       "symbol": names.get(z.instrument_key) or z.instrument_key.split("|")[-1], "timeframe": z.timeframe,
                       "side": z.side, "price": ltp, "entry": p.entry, "stop": p.stop, "target": p.target,
                       "reward_risk": p.reward_risk, "score": z.score, "label": z.label, "mode": p.mode, "entry_type": p.entry_type or "TYPE_1",
                       "curve": (p.context or {}).get("curve"), "itf_trend": p.trend, "time": now.isoformat(timespec="seconds")})
    db.commit()
    return alerts


def _keys(db: Session, rows: list) -> list[str]:
    held = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db)}
    return sorted(set(settings_store.get(db, "universe") or []) | {z.instrument_key for z, _ in rows} | held)


def _wanted_keys() -> list[str]:
    """The coins whose prices are kept: the coin list, every watched coin, and anything held on paper (database work:
    runs in a worker thread)."""
    with SessionLocal() as db:
        return _keys(db, watch_rows(db))


def _process(prices: dict[str, float], now: datetime) -> tuple[list, list, int]:
    """Alerts, automatic orders, fills, stops and targets for one round of prices. Plain database work: it runs in a
    worker thread, so the web pages never wait for it."""
    with SessionLocal() as db:
        rows = watch_rows(db)
        held = bool(paper.live_orders(db) or paper.open_trades(db))
        if not rows and not held:          # nothing to check against: no queries beyond these three
            return [], [], 0
        placed = paper.auto_orders(db, rows, now)        # before the alert check, while the plans are still pending
        fills_ = paper.on_prices(db, prices, now) if held or placed else []
        alerts = check_prices(db, prices, now) if rows else []
    return alerts, fills_, placed


# The live checks get their own workers, so the minute job (downloads, scans, the candle safety net) cannot hold them up.
_LIVE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live")
_tasks: set = set()
_busy = {"processing": False}
_keys_cache = {"keys": None, "at": 0.0}


async def poll_once(client, now: datetime | None = None, prices: dict[str, float] | None = None) -> list[dict]:
    """One round: the coin list, every watched coin, and anything held on paper. Prices come from the WebSocket when
    given, otherwise from one REST call. Raises alerts, applies paper fills, and pushes all of it to the pages and
    Telegram. The database part runs in a worker thread; if the previous round is still at it, this round only pushes
    the prices."""
    now = now or now_utc()
    loop = asyncio.get_running_loop()
    if _keys_cache["keys"] is None or loop.time() - _keys_cache["at"] > 10 or prices is None:
        _keys_cache["keys"], _keys_cache["at"] = await loop.run_in_executor(_LIVE_POOL, _wanted_keys), loop.time()
    keys = _keys_cache["keys"]
    if not keys:
        return []
    if prices is None:
        prices = await client.ltp(keys)
        state["source"] = "REST"
    else:
        wanted = set(keys)
        prices = {k: v for k, v in prices.items() if k in wanted}
    state["prices"].update(prices)
    state["last_poll"], state["error"] = now, None
    await hub.broadcast({"type": "prices", "data": prices, "time": now.isoformat(timespec="seconds")})
    if _busy["processing"]:
        return []
    _busy["processing"] = True
    try:
        alerts, fills_, placed = await asyncio.get_running_loop().run_in_executor(_LIVE_POOL, _process, prices, now)
    finally:
        _busy["processing"] = False
    if alerts or fills_ or placed:
        _keys_cache["keys"] = None          # a fill or an order can change which coins are watched
    for a in alerts:
        await hub.broadcast(a)
        telegram.notify(telegram.alert_text(a))
    for e in fills_:
        await hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    if placed and not fills_:
        await hub.broadcast({"type": "paper", "event": "order placed"})
    return alerts


def parse_prices(message: str | bytes) -> dict[str, float]:
    """The all-market stream: a list of {"s": symbol, "c": last price, ...} (mini ticker), possibly wrapped by a combined
    stream. A mark price message ({"p": ...}) is read too."""
    data = json.loads(message)
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    out = {}
    for item in data if isinstance(data, list) else [data]:
        if not isinstance(item, dict) or not item.get("s"):
            continue
        value = item.get("c") if item.get("c") not in (None, "") else item.get("p")
        if value not in (None, ""):
            out[item["s"]] = float(value)
    return out


parse_mark_prices = parse_prices         # the old name, kept for anything that still uses it


def _spawn(coro) -> None:
    """Run the checks beside the feed: the feed keeps reading Binance's messages (and answering its pings) meanwhile."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _checks(client, prices: dict) -> None:
    try:
        await poll_once(client, prices=prices)
    except Exception as exc:     # a database hiccup must not drop the price feed
        state["error"] = f"{type(exc).__name__}: {exc}"
        log.warning("live checks: %s: %s", type(exc).__name__, exc)


def stream_url() -> str:
    return f"{config.BINANCE_WS}{STREAM_PATH}{STREAM}"


class Silent(Exception):
    """The WebSocket is open but no message came for SILENT_AFTER seconds."""


async def _stream(client, mine) -> None:
    """One WebSocket connection, until it drops or goes silent. Prices land in state at once; the checks run once a second."""
    import websockets
    url = stream_url()
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=2 ** 23) as ws:
        if mine():
            state["connected"], state["url"] = True, url
        last = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), SILENT_AFTER)
            except asyncio.TimeoutError:
                raise Silent(f"no message from {url} for {SILENT_AFTER} seconds")
            got = parse_prices(message)
            if not got or not mine():
                continue
            state["prices"].update(got)
            state["last_poll"] = state["last_message"] = now_utc()
            state["source"], state["error"] = "WebSocket", None
            if loop.time() - last >= PROCESS_EVERY:
                last = loop.time()
                _spawn(_checks(client, dict(state["prices"])))      # not awaited: the feed never waits for the checks


async def _rest_for(client, seconds: float, mine) -> None:
    """REST last prices every REST_EVERY seconds for a while, so alerts and paper fills carry on while the stream is down."""
    until = asyncio.get_running_loop().time() + seconds
    while mine():
        try:
            await poll_once(client)
        except BinanceError as exc:
            state["error"] = str(exc)
        except Exception as exc:
            state["error"] = f"{type(exc).__name__}: {exc}"
        if asyncio.get_running_loop().time() >= until:
            return
        await asyncio.sleep(REST_EVERY)


async def feeder(client) -> None:
    """Runs for the life of the app: the WebSocket, and REST polling while it is down. Only the newest feeder writes to
    the shared state, so a restart never leaves the page showing an old, stopped one."""
    me = asyncio.current_task()
    mine = lambda: state.get("task") is me
    state["running"], state["stopped"] = True, None
    pause = 1
    try:
        while mine():
            try:
                await _stream(client, mine)
                pause = 1                    # a clean close (Binance's 24 hour limit): reconnect at once
            except asyncio.CancelledError:
                if getattr(me, "cancelling", lambda: 1)() or not mine():
                    raise                    # a real stop: shutdown or a restart
                state["error"] = "WebSocket: the connection was cancelled inside the library; opening it again"
                log.warning("live feed: stray cancellation, reconnecting")
            except Exception as exc:
                state["error"] = f"WebSocket: {type(exc).__name__}: {exc}"
                log.warning("live feed: %s", state["error"])
            if not mine():
                break
            state["connected"] = False
            state["reconnects"] += 1
            await _rest_for(client, pause, mine)
            pause = min(pause * 2, 60)
    except BaseException as exc:
        if mine():
            state["stopped"] = f"{type(exc).__name__}: {exc}"[:200]
        raise
    finally:
        if mine():
            state["running"] = False
            state["connected"] = False


def start_poller(client) -> None:
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    state["task"] = asyncio.get_running_loop().create_task(feeder(client))


def ensure_running(client) -> bool:
    """Start the feed again if it has stopped for any reason. Returns True when it had to be restarted."""
    task = state.get("task")
    if task is None or task.done():
        start_poller(client)
        return True
    return False


def feed_status(now: datetime) -> dict:
    last = state["last_poll"]
    task = state.get("task")
    return {"running": bool(task and not task.done()), "connected": state["connected"], "source": state["source"], "last_poll": last,
            "last_message": state["last_message"], "url": state["url"] or stream_url(), "stopped": state["stopped"],
            "error": state["error"], "reconnects": state["reconnects"],
            "stale": bool(last is None or (now - last).total_seconds() > STALE_AFTER)}
