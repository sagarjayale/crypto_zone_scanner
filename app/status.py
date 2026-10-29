"""Readiness of each coin in the list. A coin appears on the dashboard, chart, paper and
backtest pages only after it has been downloaded and scanned in full once. Later updates never hide it again."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .data.sync import now_utc
from .models import Candle, InstrumentStatus, SyncState


def mark(db: Session, key: str, state: str, detail: str | None = None) -> None:
    row = db.get(InstrumentStatus, key)
    if row is None:
        row = InstrumentStatus(instrument_key=key, ready=False)
        db.add(row)
    row.state, row.detail, row.updated_at = state, (detail or "")[:255] or None, now_utc()
    if state == "ready":
        row.ready = True
    db.commit()


def ready_keys(db: Session, universe: list[str]) -> list[str]:
    ok = set(db.scalars(select(InstrumentStatus.instrument_key).where(InstrumentStatus.ready.is_(True))))
    return [k for k in universe if k in ok]


def overview(db: Session, universe: list[str]) -> dict[str, dict]:
    rows = {r.instrument_key: r for r in db.scalars(select(InstrumentStatus).where(InstrumentStatus.instrument_key.in_(universe)))}
    return {k: {"state": rows[k].state if k in rows else "queued", "ready": bool(rows[k].ready) if k in rows else False,
                "detail": rows[k].detail if k in rows else None, "updated_at": rows[k].updated_at if k in rows else None} for k in universe}


def downloaded(db: Session, keys: list[str]) -> list[str]:
    """The coins whose first full download has finished: they have hourly candles (a partial download, such as only the
    1 minute candles, does not count)."""
    if not keys:
        return []
    have = set(db.scalars(select(SyncState.instrument_key).where(SyncState.instrument_key.in_(keys), SyncState.timeframe == "1h",
                                                                  SyncState.last_candle_ts.isnot(None))))
    for k in keys:              # candles stored without a sync row (imported, or from a test): one indexed look-up each
        if k not in have and db.scalar(select(Candle.ts).where(Candle.instrument_key == k, Candle.timeframe == "1h").limit(1)) is not None:
            have.add(k)
    return [k for k in keys if k in have]


def adopt_existing(db: Session, universe: list[str]) -> None:
    """Instruments scanned by an earlier version have no status row yet: those with scanned daily candles are ready."""
    have = set(db.scalars(select(InstrumentStatus.instrument_key)))
    for key in universe:
        if key in have:
            continue
        st = db.get(SyncState, (key, "1d"))
        done = st is not None and st.last_scanned_ts is not None
        db.add(InstrumentStatus(instrument_key=key, state="ready" if done else "queued", ready=done, updated_at=now_utc()))
    for row in db.scalars(select(InstrumentStatus).where(InstrumentStatus.state.in_(("downloading", "scanning")))):
        row.state, row.detail = ("ready", None) if row.ready else ("queued", "interrupted by a restart")   # nothing is running at start-up
    # A coin marked ready before its candles were downloaded (an earlier version did this) waits for its download again.
    full = set(downloaded(db, list(universe)))
    for row in db.scalars(select(InstrumentStatus).where(InstrumentStatus.ready.is_(True), InstrumentStatus.instrument_key.in_(universe))):
        if row.instrument_key not in full:
            row.ready, row.state, row.detail = False, "queued", "no candles yet: downloaded by the catch-up"
    db.commit()
