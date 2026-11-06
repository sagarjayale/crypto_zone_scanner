"""Scheduled jobs, in UTC, every day of the week: crypto never closes. Each job does what the matching API does, so
anything the schedule does can also be run by hand. A job that fails is recorded and never stops the app.

  every minute, 8 s past   candle close: download the candles that just closed (1m always when a mode uses it; 5m, 15m,
                           30m, 1h, 2h, 4h, day, week and month when theirs closed) for 8 coins at a time, rescan only
                           the coins and timeframes that got one, check paper orders of coins held on paper
  every 5 minutes          funding rates and mark prices of all coins; open interest of the coin list (every 15 minutes)
  every hour, +2 min       funding settlements of the coin list (some coins settle every 1 or 4 hours)
  00:10 UTC                coin list refresh (filters, listing dates, funding intervals, 24 hour volumes); an
                           automatic coin list follows the top N by volume and new coins are downloaded
  00:20 UTC                gap repair and clean-up
  at start-up              coin list on the first start (top N by volume), then a catch-up download and scan
  every minute, 30 s past  the live price feed is started again if it has stopped
  every 5 minutes          the first start again, while it has not succeeded (Binance was unreachable)"""
import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .. import live, settings_store, status
from ..alerts import telegram
from ..config import MODES
from ..data import market
from ..data import sync as data_sync
from ..data.resample import close_time, open_after
from ..db import SessionLocal
from ..engine import scanner
from ..models import Instrument, Job, Rejection, SyncState
from ..sim import paper
from . import scanpool
from .runner import run_job

log = logging.getLogger(__name__)


def _universe(db) -> list[str]:
    return list(settings_store.get(db, "universe") or [])


async def store_instruments(db, client) -> int:
    return await market.store_instruments(db, client)


SYNC_AT_ONCE = 6     # coins downloaded side by side; the weight limiter still caps the request rate


async def each_instrument(keys: list[str], one, report, label: str = "coins") -> None:
    """Run `one(key)` for every coin, a few at a time. One failing coin does not stop the others;
    the first error is raised at the end so the job is recorded as failed."""
    gate, done, errors = asyncio.Semaphore(SYNC_AT_ONCE), 0, []

    async def guarded(key):
        nonlocal done
        async with gate:
            try:
                await one(key)
            except Exception as exc:
                errors.append(f"{key}: {type(exc).__name__}: {exc}")
            done += 1
            report(f"{done} of {len(keys)} {label}" + (f", {len(errors)} failed" if errors else ""))
    await asyncio.gather(*(guarded(k) for k in keys))
    if errors:
        raise RuntimeError(f"{len(errors)} of {len(keys)} failed. First: {errors[0]}")


async def prepare(client, key: str, timeframes: list[str] | None = None, repair: bool = False, retry_if_late: bool = False,
                  now: datetime | None = None, force: bool = True, download: bool = True, full: bool = False) -> None:
    """Take one coin through download, scan and results, and mark it ready. A coin that was ready before stays visible
    on the pages while it is being updated; a new one appears when this finishes."""
    def note(state: str, detail: str | None = None):
        with SessionLocal() as d:
            status.mark(d, key, state, detail)
    try:
        if download:
            note("downloading")
            with SessionLocal() as d:      # each coin has its own session, so several can run side by side
                await data_sync.sync_symbol(d, client, key, timeframes, full=full, repair_days=30 if repair else 0, now=now)
                if timeframes is None:     # a full pass also brings funding and open interest up to date
                    try:
                        await market.sync_funding(d, client, key)
                        await market.sync_open_interest(d, client, key)
                    except Exception as exc:     # extras for the costs and the confirmation score; candles matter more
                        log.warning("funding or open interest for %s failed: %s", key, exc)
        note("scanning")
        await scanpool.scan(key, force, now)      # CPU work: in a worker process, never in the web server's
        with SessionLocal() as d:
            complete = bool(status.downloaded(d, [key]))
        if complete:
            note("ready")
        else:                      # never shown as ready without its candles
            note("queued", "no candles yet: waiting for its download")
            return
    except Exception as exc:
        note("failed", f"{type(exc).__name__}: {exc}")
        raise
    await live.hub.broadcast({"type": "ready", "instrument_key": key})   # the pages add this coin without a reload


async def sync_and_scan(client, job_type: str, timeframes: list[str] | None = None, repair: bool = False,
                        retry_if_late: bool = False, now: datetime | None = None, force: bool = True):
    """Sync every coin in the list, scan it, then tell the pages.
    force=False scans only the timeframes that received a new candle (the candle-close job)."""
    paper_events: list[dict] = []

    async def work(db, report):
        held = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db)}
        keys = _universe(db) + sorted(held - set(_universe(db)))   # anything held on paper is kept in step too
        if timeframes is not None:      # the candle-close job: coins still on their first full download are left to it
            keys = status.ready_keys(db, keys)

        async def one(key: str):
            await prepare(client, key, timeframes, repair, retry_if_late, now, force)
            with SessionLocal() as d:
                paper_events.extend(paper.on_candles(d, key))
        await each_instrument(keys, one, report)
        live.state["last_scan"] = now or data_sync.now_utc()
    job_id = await run_job(job_type, work)
    await live.hub.broadcast({"type": "scan", "time": (now or data_sync.now_utc()).isoformat(timespec="seconds")})
    for e in paper_events:
        await live.hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    return job_id


CATCH_UP = ("5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo")


def overdue_timeframes(db, keys: list[str], now: datetime) -> dict[str, list[str]]:
    """Per coin, the downloaded timeframes whose next candle has already closed but is not stored: left behind when a
    minute run was skipped (the previous one still running) or failed. Only timeframes the coin already has; a first
    download stays with its own job. One query for all coins."""
    if not keys:
        return {}
    out: dict[str, list[str]] = {}
    rows = db.execute(select(SyncState.instrument_key, SyncState.timeframe, SyncState.last_candle_ts, SyncState.last_sync_at).where(
        SyncState.instrument_key.in_(keys), SyncState.timeframe.in_(CATCH_UP), SyncState.last_candle_ts.isnot(None))).all()
    for key, tf, last, synced in rows:
        if close_time(open_after(last, tf), tf) > now:
            continue
        # asked for already within one candle's length (a coin Binance stopped trading has nothing new): wait
        if synced is not None and now - synced < min(close_time(last, tf) - last, timedelta(days=1)):
            continue
        out.setdefault(key, []).append(tf)
    return out


def closed_timeframes(now: datetime, enabled: list[str] | None = None) -> list[str]:
    """Downloaded timeframes with a candle that closed at the last minute boundary (UTC). 1-minute candles only when an
    enabled mode reads them (the 15 min mode)."""
    t = now.replace(second=0, microsecond=0)
    minute = t.hour * 60 + t.minute
    used = {tf for m in (enabled or list(MODES)) if m in MODES for tf in MODES[m]}
    tfs = ["1m"] if "1m" in used else []
    for tf, size in (("5m", 5), ("15m", 15), ("30m", 30), ("1h", 60), ("2h", 120), ("4h", 240)):
        if minute % size == 0:
            tfs.append(tf)
    if minute == 0:
        tfs.append("1d")
        if t.weekday() == 0:
            tfs.append("1w")
        if t.day == 1:
            tfs.append("1mo")
    return tfs


async def bar_close(client, now: datetime | None = None) -> dict | None:
    """The every-minute job, kept light: download the candles that just closed for every ready coin (several at once),
    rescan only the coins that got one and only those timeframes, and check paper orders of coins held on paper.
    No status rows and no job row are written for a normal run (a failure is recorded as a job); the summary is kept in
    memory and shown on the Data page."""
    started = asyncio.get_running_loop().time()
    now = now or data_sync.now_utc()

    def plan():
        with SessionLocal() as db:
            tfs_ = closed_timeframes(now, settings_store.get(db, "modes"))
            held_ = {o.instrument_key for o in paper.live_orders(db)} | {t.instrument_key for t in paper.open_trades(db)}
            keys_ = status.downloaded(db, status.ready_keys(db, _universe(db) + sorted(held_ - set(_universe(db)))))
            # coins still on their first full download are left to it: the minute job never starts a partial one
            return tfs_, held_, keys_, overdue_timeframes(db, keys_, now)
    tfs, held, keys, late = await asyncio.to_thread(plan)
    if not (keys or held) or not (tfs or late or held):
        return None
    gate, errors, scanned, events = asyncio.Semaphore(8), [], [], []

    async def one(key: str):
        # this minute's timeframes, plus any whose candles fell behind (a run that was skipped or failed is caught up here)
        wanted = sorted(set(tfs) | set(late.get(key, ())))
        got: dict = {}
        if wanted:
            async with gate:
                try:
                    with SessionLocal() as d:
                        got = await data_sync.sync_symbol(d, client, key, wanted, now=now)
                except Exception as exc:
                    errors.append(f"{key}: {type(exc).__name__}: {exc}")
                    # no return: the paper safety net and trailing below still run on the candles already stored
        try:
            if any(got.values()):
                await scanpool.scan(key, False, now)
                scanned.append(key)
        except Exception as exc:
            errors.append(f"{key}: {type(exc).__name__}: {exc}")
        if key in held:
            await safety_net(key)

    async def safety_net(key: str):
        """Paper orders and positions of this coin against the stored candles, and its trailing stop: every minute,
        whatever happened to the download or the scan."""
        def net():
            with SessionLocal() as d:
                return paper.on_candles(d, key)
        try:
            events.extend(await asyncio.to_thread(net))
        except Exception as exc:
            errors.append(f"{key} (paper): {type(exc).__name__}: {exc}")
    await asyncio.gather(*(one(k) for k in keys))
    await asyncio.gather(*(safety_net(k) for k in sorted(held - set(keys))))     # held coins not downloaded here
    summary = {"at": now.isoformat(timespec="seconds"), "timeframes": tfs, "coins": len(keys), "scanned": len(scanned),
               "caught_up": sum(len(v) for v in late.values()),
               "seconds": round(asyncio.get_running_loop().time() - started, 1), "errors": len(errors), "first_error": errors[0] if errors else None}
    live.state["last_scan"], live.state["candle_close"] = now, summary
    if errors:
        async def record(db, report):
            raise RuntimeError(f"{len(errors)} of {len(keys)} coins failed. First: {errors[0]}")
        await run_job("candle close", record)
    await live.hub.broadcast({"type": "scan", "time": now.isoformat(timespec="seconds"), "coins": scanned})
    for e in events:
        await live.hub.broadcast(e)
        telegram.notify(telegram.paper_text(e))
    return summary


async def market_data(client, open_interest: bool = False):
    """Funding rate and mark price of every coin; open interest history of the coin list when asked."""
    async def work(db, report):
        n = await market.store_premium(db, client)
        done = 0
        if open_interest:
            for key in _universe(db):
                try:
                    await market.sync_open_interest(db, client, key)
                    done += 1
                except Exception as exc:
                    log.warning("open interest for %s failed: %s", key, exc)
        report(f"{n} funding rates" + (f", open interest for {done} coins" if open_interest else ""))
    return await run_job("funding and open interest", work)


async def funding_history(client):
    async def work(db, report):
        total = 0
        for key in _universe(db):
            total += await market.sync_funding(db, client, key)
        report(f"{total} funding settlements stored")
    return await run_job("funding history", work)


def follow_top(db) -> list[str]:
    """While the coin list is automatic (the user has not chosen coins by hand), it is the top N by 24 hour volume.
    Returns the coins that came into the list. Coins that left it keep their candles and zones."""
    if not settings_store.get(db, "universe_auto"):
        return []
    top = market.top_symbols(db, int(settings_store.get(db, "top_n") or 50))
    if not top:
        return []
    before = _universe(db)
    if top != before:
        settings_store.put(db, "universe", top)
    added = [k for k in top if k not in before]
    for key in added:
        if not status.overview(db, [key])[key]["ready"]:
            status.mark(db, key, "queued")
    return added


async def refresh_coins(client, download_new: bool = True):
    """The coin list from Binance; with an automatic list, the top N by volume again, and the new coins downloaded."""
    added: list[str] = []

    async def work(db, report):
        n = await market.store_instruments(db, client)
        added.extend(await asyncio.to_thread(follow_top, db))
        report(f"{n} USDT perpetuals stored" + (f"; {len(added)} new in the top {settings_store.get(db, 'top_n')}" if added else ""))
    job_id = await run_job("coin list", work)
    if added and download_new:
        async def fetch(db, report):
            await each_instrument(added, lambda key: prepare(client, key), report, "new coins ready")
        await run_job("new coins", fetch)
    return job_id


async def first_start(client):
    """On the very first start the coin list is empty: fetch the coins and take the top N by 24 hour volume."""
    async def work(db, report):
        n = await market.store_instruments(db, client)
        if not settings_store.get(db, "universe_set"):
            settings_store.put(db, "universe_auto", True)
            settings_store.put(db, "universe", [])
            top = follow_top(db)
            settings_store.put(db, "universe_set", True)
            report(f"{n} USDT perpetuals stored; the top {len(top)} by volume are your coin list")
        await market.store_premium(db, client)
    await run_job("first start", work)
    await sync_and_scan(client, "catch-up")


async def retry_first_start(client):
    """If the first start could not reach Binance, try again every 5 minutes until the coin list exists."""
    with SessionLocal() as db:
        if settings_store.get(db, "universe_set") or _universe(db):
            return None
    return await first_start(client)


async def feed_watchdog(client):
    """The live price feed must always run: start it again if it stopped for any reason."""
    if live.ensure_running(client):
        log.warning("live feed was not running; started it again")


async def health(client):
    """Repair recent gaps, and clear out old log rows."""
    async def work(db, report):
        await asyncio.to_thread(data_sync.recount_all)          # candle and gap counts checked again from scratch
        gappy = sorted({s.instrument_key for s in db.query(SyncState).filter(SyncState.gaps > 0)})
        for key in gappy:
            await data_sync.sync_symbol(db, client, key, repair_days=30)
        now = data_sync.now_utc()
        db.execute(delete(Rejection).where(Rejection.ts < now - timedelta(days=90)))
        db.execute(delete(Job).where(Job.started_at < now - timedelta(days=7)))    # the candle-close job alone adds 1,440 rows a day
        db.commit()
        gone = await asyncio.to_thread(prune_old_zones)
        report(f"{len(gappy)} coins repaired, {gone} old used-up zones removed, old log rows cleared")
    return await run_job("health", work)


def _tidy_events(report) -> int:
    """Delete the "touched_again" rows earlier versions wrote, in batches by id so the table is never locked for long,
    then add the index the alerts list reads. Runs once; afterwards there is nothing left to delete."""
    from sqlalchemy import func as f, inspect, text
    from ..db import engine
    from ..models import ZoneEvent
    gone = 0
    with SessionLocal() as db:
        if settings_store.get(db, "events_tidied"):
            return 0
        top = db.scalar(select(f.max(ZoneEvent.id))) or 0
        for lo in range(0, top + 1, 50000):
            n = db.execute(delete(ZoneEvent).where(ZoneEvent.id >= lo, ZoneEvent.id < lo + 50000,
                                                   ZoneEvent.event_type == "touched_again")).rowcount or 0
            db.commit()
            gone += n
            if n:
                report(f"{gone:,} unneeded zone events removed ({min(100, round(100 * (lo + 50000) / max(top, 1)))}%)")
        if "ix_events_type_ts" not in {ix["name"] for ix in inspect(engine).get_indexes("zone_events")}:
            report("adding an index for the alerts list")
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX ix_events_type_ts ON zone_events (event_type, ts)"))
        settings_store.put(db, "events_tidied", True)
    return gone


async def tidy_events():
    with SessionLocal() as db:
        if settings_store.get(db, "events_tidied"):
            return None

    async def work(db, report):
        report(f"{await asyncio.to_thread(_tidy_events, report):,} unneeded zone events removed")
    return await run_job("tidy zone events", work)


def prune_old_zones() -> int:
    """Remove zones that were used up long ago. Own session: runs in a worker thread. A clean-up only: if the database
    is busy (a table being emptied or locked), it is skipped and simply runs again next time."""
    from sqlalchemy.exc import OperationalError
    with SessionLocal() as db:
        try:
            return scanner.prune_closed(db)
        except OperationalError as exc:
            db.rollback()
            log.warning("old zone clean-up skipped, the database was busy: %s", exc.orig)
            return 0


def build(client) -> AsyncIOScheduler:
    sch = AsyncIOScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 50})
    sch.add_job(bar_close, CronTrigger(second=8), args=[client], id="candle close")
    sch.add_job(market_data, CronTrigger(minute="1-59/5", second=30), args=[client], id="funding")
    sch.add_job(market_data, CronTrigger(minute="3-59/15", second=40), args=[client, True], id="open interest")
    sch.add_job(funding_history, CronTrigger(minute=2, second=20), args=[client], id="funding history")
    sch.add_job(refresh_coins, CronTrigger(hour=0, minute=10), args=[client], id="coin list")
    sch.add_job(health, CronTrigger(hour=0, minute=20), args=[client], id="health")
    sch.add_job(feed_watchdog, CronTrigger(second=30), args=[client], id="live feed watchdog")
    sch.add_job(retry_first_start, CronTrigger(minute="*/5", second=45), args=[client], id="first start retry")
    return sch


def start_up(client) -> None:
    """At start-up: the first-start job when there is no coin list yet. Otherwise the coin list is refreshed (an
    automatic list follows the top N by volume), then a catch-up downloads what closed while the app was stopped and
    rescans only what changed."""
    with SessionLocal() as db:
        first = not settings_store.get(db, "universe_set") and not _universe(db)
    loop = asyncio.get_running_loop()
    if first:
        loop.create_task(first_start(client))
        return

    async def boot():
        await refresh_coins(client, download_new=False)       # new coins are downloaded by the catch-up just below
        await sync_and_scan(client, "catch-up", force=False)
    loop.create_task(boot())
