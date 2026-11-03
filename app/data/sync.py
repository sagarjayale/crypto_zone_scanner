"""The one place that downloads candles from Binance. Three callers use it: the scheduled jobs, the sync API and the
scan of a single coin. It downloads only what is missing, never stores a candle that is still forming, and finds and
fills gaps. All times are UTC."""
import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import DERIVED, FETCHED, TF_MINUTES
from ..db import retry_locked
from ..models import Candle, Instrument, SyncState
from .resample import close_time, open_after, resample_monthly

_locks: dict[str, asyncio.Lock] = {}
COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]
GAP_DAYS = 30           # how far back gaps are looked for and repaired
EARLIEST = datetime(2019, 9, 1)   # Binance USDT-M futures started in September 2019


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def candle_end(ts: datetime, tf: str) -> datetime:
    return close_time(ts, tf)


_UPSERT_CACHE: dict = {}


def upsert_statement(dialect: str, model, keys: tuple[str, ...], values: list[str]):
    """One INSERT ... ON DUPLICATE KEY UPDATE (MySQL) or ON CONFLICT DO UPDATE (SQLite, PostgreSQL) per table, built once
    and run with many rows at a time: the database driver sends the rows in batches and the statement is never compiled
    again (compiling a statement with thousands of values in it was the costliest step of a download)."""
    # one statement per table AND set of columns: a batch without a column (old funding records have no mark price)
    # must not reuse a statement that updates that column, or MySQL refuses it ("Unknown column 'new.mark'")
    key = (dialect, model.__tablename__, tuple(keys), tuple(values))
    if key not in _UPSERT_CACHE:
        if dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert
            stmt = insert(model)
            stmt = stmt.on_duplicate_key_update(**{c: stmt.inserted[c] for c in values})
        else:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            stmt = insert(model)
            stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_={c: getattr(stmt.excluded, c) for c in values})
        _UPSERT_CACHE[key] = stmt
    return _UPSERT_CACHE[key]


def upsert_candles(db: Session, key: str, tf: str, rows: list[dict]) -> int:
    """Insert candles, replacing any that already exist. Works on MySQL, PostgreSQL and SQLite."""
    if not rows:
        return 0
    payload = [{"instrument_key": key, "timeframe": tf, **{c: r[c] for c in COLS}} for r in rows]
    stmt = upsert_statement(db.bind.dialect.name, Candle, ("instrument_key", "timeframe", "ts"), [c for c in COLS if c != "ts"])
    for i in range(0, len(payload), 2000):
        db.execute(stmt, payload[i:i + 2000])
        db.commit()      # one short write per batch, so nothing else waits long for the database
    return len(payload)


def load_candles(db: Session, key: str, tf: str, since: datetime | None = None,
                 until: datetime | None = None, last: int | None = None) -> pd.DataFrame:
    q = select(*[getattr(Candle, c) for c in COLS]).where(Candle.instrument_key == key, Candle.timeframe == tf)
    if since is not None:
        q = q.where(Candle.ts >= since)
    if until is not None:
        q = q.where(Candle.ts <= until)
    if last:
        rows = db.execute(q.order_by(Candle.ts.desc()).limit(last)).all()[::-1]
    else:
        rows = db.execute(q.order_by(Candle.ts)).all()
    df = pd.DataFrame(rows, columns=COLS)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"])
    return df


def get_state(db: Session, key: str, tf: str) -> SyncState:
    st = db.get(SyncState, (key, tf))
    if st is None:
        st = SyncState(instrument_key=key, timeframe=tf)
        db.add(st)
        db.flush()
    return st


def _last_ts(db: Session, key: str, tf: str):
    return db.scalar(select(func.max(Candle.ts)).where(Candle.instrument_key == key, Candle.timeframe == tf))


def _count(db: Session, st: SyncState) -> None:
    """Candles stored and the first one, kept on the sync row so the Data page never counts the whole candle table."""
    n, first = db.execute(select(func.count(), func.min(Candle.ts)).where(Candle.instrument_key == st.instrument_key,
                                                                          Candle.timeframe == st.timeframe)).one()
    st.candles, st.first_candle_ts = int(n or 0), first


def _rebuild_derived(db: Session, key: str, now: datetime) -> None:
    """Quarter, half year and year from the stored monthly candles (a few hundred rows at most, so all of them)."""
    months = load_candles(db, key, "1mo")
    for dtf in DERIVED:
        out = resample_monthly(months, dtf, now)
        upsert_candles(db, key, dtf, out.to_dict("records"))
        st = get_state(db, key, dtf)
        st.last_candle_ts, st.last_sync_at = _last_ts(db, key, dtf), now
        _count(db, st)


def keep_days(db: Session, tf: str) -> int | None:
    """Days of candles kept for a timeframe: the history_days setting, else config.FETCHED; None keeps everything."""
    got = (settings_store.get(db, "history_days") or {}).get(tf)
    return int(got) if got else FETCHED[tf]["keep_days"]


def first_wanted(db: Session, key: str, tf: str, now: datetime) -> datetime:
    """Where the history of a timeframe starts: the coin's listing date, or keep_days back for the short timeframes."""
    inst = db.get(Instrument, key)
    listed = inst.onboard_date if inst is not None and inst.onboard_date else EARLIEST
    keep = keep_days(db, tf)
    return max(listed, now - timedelta(days=keep)) if keep else listed


def gap_list(db: Session, key: str, tf: str, now: datetime, days: int = GAP_DAYS) -> list[tuple[datetime, datetime]]:
    """Missing stretches in the last `days` of an intraday timeframe, as (first missing open, last missing open)."""
    if tf not in TF_MINUTES or tf in ("1w",):
        return []
    step = timedelta(minutes=TF_MINUTES[tf])
    since = max(now - timedelta(days=days), first_wanted(db, key, tf, now))
    times = list(db.scalars(select(Candle.ts).where(Candle.instrument_key == key, Candle.timeframe == tf, Candle.ts >= since).order_by(Candle.ts)))
    gaps = []
    for a, b in zip(times, times[1:]):
        if b - a > step:
            gaps.append((a + step, b - step))
    return gaps


def gap_count(db: Session, key: str, tf: str, now: datetime, days: int = GAP_DAYS) -> int:
    """Number of candles missing in the last `days` (between stored candles)."""
    if tf not in TF_MINUTES:
        return 0
    step = TF_MINUTES[tf]
    return sum(int((b - a).total_seconds() // 60 // step) + 1 for a, b in gap_list(db, key, tf, now, days))


def _store(db: Session, key: str, tf: str, rows: list[dict], now: datetime, exact: bool = False) -> int:
    """Write one downloaded timeframe: candles, marker, gap count, and the timeframes built from it.

    Kept cheap for the every-minute job: when the new candles follow straight on from the last stored one, the candle
    count is updated by arithmetic and the gap count is left as it was. Everything is counted again on a first or large
    download, when the new candles leave a hole, on a repair (exact=True), and by the nightly repair job."""
    st = get_state(db, key, tf)
    prev = st.last_candle_ts
    written = upsert_candles(db, key, tf, rows)
    keep = keep_days(db, tf)
    pruned = 0
    if keep:
        pruned = db.execute(delete(Candle).where(Candle.instrument_key == key, Candle.timeframe == tf,
                                                 Candle.ts < now - timedelta(days=keep))).rowcount or 0
    newest = max((r["ts"] for r in rows), default=None)
    st.last_candle_ts = max(prev, newest) if prev and newest else (newest or prev or _last_ts(db, key, tf))
    st.last_sync_at = now
    joined = bool(rows) and prev is not None and min(r["ts"] for r in rows) <= open_after(prev, tf)
    if exact or st.candles is None or prev is None or len(rows) > 2000 or (rows and not joined):
        _count(db, st)
        st.gaps = gap_count(db, key, tf, now)
    else:
        st.candles = max(0, (st.candles or 0) + sum(1 for r in rows if r["ts"] > prev) - pruned)
    if tf == "1mo" and rows:
        _rebuild_derived(db, key, now)
    db.commit()
    return written


async def _fetch(client, key: str, tf: str, start: datetime, now: datetime, end: datetime | None = None) -> list[dict]:
    """Every closed candle from `start` (an open time) on, page by page. Stops at the first page that reaches now."""
    interval, rows, cur = FETCHED[tf]["interval"], [], start
    stop = end or now
    while cur <= stop:
        if tf in TF_MINUTES:      # ask only for as many candles as can exist: a small request costs less weight
            need = int((stop - cur).total_seconds() // 60 // TF_MINUTES[tf]) + 2
        else:                     # monthly candles
            need = (stop.year - cur.year) * 12 + stop.month - cur.month + 2
        page = await client.klines(key, interval, start=cur, end=end, limit=max(1, min(1000, need)))
        if not page:
            break
        rows += page
        nxt = open_after(page[-1]["ts"], tf)
        if nxt <= cur:
            break
        cur = nxt
        if len(page) < min(1000, need) - 1:
            break
    out, seen = [], set()
    for r in rows:     # never store a candle that is still forming
        if candle_end(r["ts"], tf) <= now and r["ts"] not in seen and (end is None or r["ts"] <= end):
            seen.add(r["ts"])
            out.append({c: r[c] for c in COLS})
    return out


async def _sync_once(db: Session, client, key: str, wanted: set, full: bool, repair: bool, now: datetime) -> dict:
    result: dict[str, int] = {}
    for tf in FETCHED:
        if tf not in wanted:
            continue
        st = get_state(db, key, tf)
        first = first_wanted(db, key, tf, now)
        last = None if full else st.last_candle_ts
        db.commit()      # never hold a write open across a network wait: other coins are syncing alongside
        start = first if last is None or last < first else open_after(last, tf)
        rows = await _fetch(client, key, tf, start, now)
        if repair:
            for a, b in await asyncio.to_thread(gap_list, db, key, tf, now):
                rows += await _fetch(client, key, tf, a, now, end=b)
        # Storing and resampling is plain database and pandas work. It runs in a worker thread, so the web pages
        # keep answering while a long download is written.
        result[tf] = await asyncio.to_thread(retry_locked, db, lambda: _store(db, key, tf, rows, now, repair or full))
    return result


async def sync_symbol(db: Session, client, key: str, timeframes: list[str] | None = None, full: bool = False,
                      repair_days: int = 0, now: datetime | None = None) -> dict:
    """Incremental sync for one coin. Returns candles written per downloaded timeframe. repair_days > 0 also fills
    gaps found in the last 30 days."""
    now = now or now_utc()
    wanted = {DERIVED.get(tf, tf) for tf in timeframes} if timeframes else set(FETCHED)
    async with _locks.setdefault(key, asyncio.Lock()):
        return await _sync_once(db, client, key, wanted, full, bool(repair_days), now)


def needs_sync(db: Session, key: str, now: datetime | None = None, tf: str = "5m") -> bool:
    """True if a candle of this timeframe has closed since the last stored one (or nothing is stored)."""
    now = now or now_utc()
    st = db.get(SyncState, (key, tf))
    if st is None or st.last_candle_ts is None:
        return True
    return close_time(open_after(st.last_candle_ts, tf), tf) <= now


def recount_all(now: datetime | None = None) -> int:
    """Count candles and gaps again for every coin and timeframe (the nightly job). Own session: runs in a worker thread."""
    from ..db import SessionLocal
    now = now or now_utc()
    with SessionLocal() as db:
        states = db.scalars(select(SyncState)).all()
        for st in states:
            _count(db, st)
            st.gaps = gap_count(db, st.instrument_key, st.timeframe, now)
            db.commit()
        return len(states)
