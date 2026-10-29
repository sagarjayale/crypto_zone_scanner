"""Scan service: drives the engine over candles not yet scanned and writes zones, events,
plans and rejections to the database. One instrument and one timeframe at a time.

Built to be cheap when nothing changed and to never hold up the web pages:
- a timeframe with no new candle since its last scan is skipped without loading anything;
- zones found during a scan live in memory and are written in one go at the end;
- on a long catch-up (the first scan of a coin) zones that were used up long ago are not stored at all;
- scan_key() opens its own database session, so it can run in a worker thread."""
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import TIMEFRAMES
from ..data.resample import close_time
from ..data.sync import load_candles, now_utc
from ..db import SessionLocal, retry_locked
from ..models import Rejection, SyncState, TradePlan, Zone, ZoneEvent
from . import plan
from .core import TimeframeEngine
from .lifecycle import ACTIVE

WARMUP = 300            # candles loaded before the first unscanned one, so the EMA is settled
REJECTION_DAYS = 90     # rejections are logged only for recent candles
CATCH_UP = 400          # more new candles than this is a catch-up: long-dead zones are not stored
KEEP_CLOSED_DAYS = 120  # violated and used-up zones are kept this long, for the chart (daily and above)
KEEP_INTRADAY = {"1m": 3, "5m": 10, "15m": 15, "30m": 30, "1h": 45, "2h": 60, "4h": 90}   # shorter on the busy intraday timeframes


def keep_days(tf: str) -> int:
    return KEEP_INTRADAY.get(tf, KEEP_CLOSED_DAYS)
# Events the engine reports but the database does not keep. Under the scanner.py marking a zone stays while price sits
# beyond it, and every candle there reaches the proximal line again: one "touched_again" a candle, millions of rows on
# 1 minute candles, and nothing reads them (the zone's own test count holds the number).
QUIET_EVENTS = {"touched_again"}
SCAN_LOCK = threading.Lock()   # one scan at a time: scans are CPU work and gain nothing from overlapping
ZONE_FIELDS = [c.name for c in Zone.__table__.columns if c.name not in ("id", "instrument_key", "timeframe")]


def scan_timeframe(db: Session, key: str, tf: str, now: datetime | None = None, force: bool = True) -> dict:
    now = now or now_utc()
    stats = {"timeframe": tf, "candles": 0, "zones": 0, "violated": 0, "touched": 0, "skipped": False}
    st = db.get(SyncState, (key, tf))
    if st is None:                  # candles stored without a sync row: make it and commit at once, holding nothing
        st = SyncState(instrument_key=key, timeframe=tf)
        db.add(st)
        db.commit()
    if not force and (st.last_candle_ts is None or (st.last_scanned_ts is not None and st.last_candle_ts <= st.last_scanned_ts)):
        stats["skipped"] = True          # nothing stored, or no candle has closed on this timeframe since the last scan
        return stats
    th = settings_store.thresholds(db, tf)
    first_scan = st.last_scanned_ts is None
    if first_scan:
        df = load_candles(db, key, tf)
    else:
        parts = [load_candles(db, key, tf, until=st.last_scanned_ts, last=WARMUP),
                 load_candles(db, key, tf, since=st.last_scanned_ts + timedelta(seconds=1))]
        parts = [p for p in parts if not p.empty]
        df = pd.concat(parts, ignore_index=True) if parts else load_candles(db, key, tf, last=0)
    if df.empty:
        return stats

    made: list[SimpleNamespace] = []      # zones found in this scan; written to the database after the loop

    def make_zone(fields: dict) -> SimpleNamespace:
        z = SimpleNamespace(id=None, timeframe=tf, score=0.0, label=None, swing_level=None, broke_swing=False,
                            parent_zone_id=None, superseded_by=None, **fields)
        made.append(z)
        return z

    active = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == tf, Zone.status.in_(ACTIVE))).all()
    eng = TimeframeEngine(df, tf, th, active=active, make_zone=make_zone)
    if st.last_scanned_ts is None:
        start = 0
    else:
        newer = (df["ts"] > st.last_scanned_ts).to_numpy()
        start = int(newer.argmax()) if newer.any() else len(df)
    reject_from = now - timedelta(days=REJECTION_DAYS)
    events: list[dict] = []
    entries: list[tuple] = []        # (zone id, entry type, price, candle time) for zones already stored

    for i in range(start, len(eng)):
        stats["candles"] += 1
        for ev in eng.step(i):
            z = ev["zone"]
            if ev["type"] == "rejected":
                if ev["ts"] >= reject_from:
                    db.add(Rejection(instrument_key=key, timeframe=tf, ts=ev["ts"], reason=ev["new"]))
                continue
            if ev["type"] not in QUIET_EVENTS:
                events.append(ev)
            if ev["type"] == "created":
                stats["zones"] += 1
            elif ev["type"] == "touched":
                stats["touched"] += 1
                if z.id is not None:      # a stored zone: a limit at its proximal line (entry type 1) is filled
                    entries.append((z.id, "TYPE_1", float(z.proximal), ev["ts"], z))
            elif ev["type"] in ("entry2", "entry3") and z.id is not None:
                when = ev["ts"] if ev["type"] == "entry2" else close_time(ev["ts"], tf)     # type 2 enters at the open, type 3 at the close
                entries.append((z.id, "TYPE_2" if ev["type"] == "entry2" else "TYPE_3", ev["price"], when, z))
            elif ev["type"] in ("violated", "consumed"):
                stats["violated"] += 1

    # Write the new zones in one go. On a catch-up, a zone that was used up long ago is of no use to anyone
    # (the backtest and replay rebuild zones themselves), so it is left out along with its events.
    catch_up = first_scan or stats["candles"] > CATCH_UP
    keep_from = eng.ts[-1] - timedelta(days=keep_days(tf))
    stored: dict[int, Zone] = {}
    for z in made:
        if catch_up and z.status not in ACTIVE and (z.updated_ts or z.created_ts) < keep_from:
            continue
        row = Zone(instrument_key=key, timeframe=tf, **{f: getattr(z, f) for f in ZONE_FIELDS if hasattr(z, f)})
        stored[id(z)] = row
    db.add_all(stored.values())
    db.flush()                             # assigns the ids, all at once
    for ev in events:
        z = ev["zone"]
        zone_id = z.id if z.id is not None else (stored[id(z)].id if id(z) in stored else None)
        if zone_id is not None:
            db.add(ZoneEvent(zone_id=zone_id, ts=ev["ts"], event_type=ev["type"], price=ev["price"], old_value=ev["old"], new_value=ev["new"]))
    for zone_id, kind, price, when, z in entries:
        plan.record_entry(db, zone_id, kind, price, when, z.distal, th, z.side)
    stats["stored"] = len(stored)
    db.flush()
    snap = {"cmp": float(eng.c[-1]), "low": float(eng.l[-1]), "high": float(eng.h[-1]), "ema": eng.emas(len(eng) - 1), "times": eng.ts}
    plan.refresh(db, key, tf, snap, th, now)
    # The sync row is shared with the every-minute download. It is written last, just before the commit, so the
    # download never waits behind a scan (MySQL holds a written row until the commit).
    st.last_scanned_ts = eng.ts[-1]
    db.commit()
    return stats


def scan_symbol(db: Session, key: str, timeframes: list[str] | None = None, now: datetime | None = None, force: bool = True) -> list[dict]:
    """Highest timeframe first, so the HTF zones and ITF candles are in place before an execution timeframe is planned.
    force=False skips every timeframe that has no new candle; force=True also replans those (after a settings change)."""
    return [scan_timeframe(db, key, tf, now, force) for tf in TIMEFRAMES if not timeframes or tf in timeframes]


def scan_key(key: str, force: bool = True, now: datetime | None = None, timeframes: list[str] | None = None) -> list[dict]:
    """Scan one instrument with its own database session, then rebuild its saved results.
    Meant for asyncio.to_thread, so the pages stay responsive."""
    from . import result            # here, not at the top: result imports plan, which this module also uses
    with SCAN_LOCK, SessionLocal() as db:
        stats = retry_locked(db, lambda: scan_symbol(db, key, timeframes, now, force))
        changed = None if force else {s["timeframe"] for s in stats if not s["skipped"]}
        if changed is None or changed:
            result.refresh(db, key, changed, now)
        return stats


def clear_key(db: Session, key: str) -> None:
    """Remove the zones, plans, events and rejections of one instrument and mark its timeframes unscanned. Candles are kept."""
    from ..models import SyncState
    zone_ids = select(Zone.id).where(Zone.instrument_key == key)
    for model in (ZoneEvent, TradePlan):
        db.execute(delete(model).where(model.zone_id.in_(zone_ids)))
    db.execute(delete(Zone).where(Zone.instrument_key == key))
    db.execute(delete(Rejection).where(Rejection.instrument_key == key))
    from ..models import ModeResult
    db.execute(delete(ModeResult).where(ModeResult.instrument_key == key))
    for st in db.scalars(select(SyncState).where(SyncState.instrument_key == key)).all():
        st.last_scanned_ts = None
    db.commit()


def prune_closed(db: Session, now: datetime | None = None, batch: int = 2000) -> int:
    """Delete zones that were violated or used up longer ago than their timeframe keeps them, with their events
    and plans. Works in batches so it never holds the database for long. Returns how many zones went."""
    now, gone = now or now_utc(), 0
    for tf in TIMEFRAMES:
        cutoff = now - timedelta(days=keep_days(tf))
        while True:
            ids = list(db.scalars(select(Zone.id).where(Zone.timeframe == tf, Zone.status.notin_(ACTIVE), Zone.updated_ts < cutoff).limit(batch)))
            if not ids:
                break
            for model in (ZoneEvent, TradePlan):
                db.execute(delete(model).where(model.zone_id.in_(ids)))
            db.execute(delete(Zone).where(Zone.id.in_(ids)))
            db.commit()
            gone += len(ids)
    return gone
