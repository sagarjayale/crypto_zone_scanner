"""Sync and scan against a fake Binance client (tests/fake_binance.py). The candles are generated in the test only;
the application itself never contains made-up data."""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select

from app.data import market
from app.data import sync as data_sync
from app.db import Base, SessionLocal, engine
from app.engine import scanner
from app.models import Candle, FundingRate, Instrument, OpenInterest, SyncState, TradePlan, Zone
from tests.fake_binance import FakeBinance

KEY = "TESTUSDT"
NOW = datetime(2026, 10, 18, 11, 37, 20)            # a Sunday: crypto trades on
LISTED = NOW - timedelta(days=120)


@pytest.fixture(scope="module")
def db():
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        asyncio.run(market.store_instruments(s, FakeBinance(now=NOW, listed=LISTED)))
        yield s


def count(db, tf):
    return db.scalar(select(func.count()).where(Candle.instrument_key == KEY, Candle.timeframe == tf))


def test_coin_list_keeps_trading_usdt_perpetuals_with_their_filters(db):
    keys = set(db.scalars(select(Instrument.instrument_key)))
    assert {"BTCUSDT", "ETHUSDT", "TESTUSDT"} <= keys and "OLDUSDT" not in keys and "BTCUSDT_261225" not in keys
    t = db.get(Instrument, KEY)
    assert (t.tick_size, t.step_size, t.min_qty, t.min_notional, t.funding_hours) == (0.01, 0.001, 0.001, 5.0, 4)
    assert db.get(Instrument, "BTCUSDT").funding_hours == 8 and t.onboard_date == LISTED.replace(microsecond=0)
    assert market.top_symbols(db, 2) == ["BTCUSDT", "ETHUSDT"]


def test_first_sync_fills_every_timeframe_from_the_listing_date(db):
    client = FakeBinance(now=NOW, listed=LISTED)
    asyncio.run(data_sync.sync_symbol(db, client, KEY, now=NOW))
    for tf in ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w", "1mo", "3mo"):
        assert count(db, tf) > 0, tf
    # every candle stored has closed; the one forming at 11:37 is not there
    assert db.get(SyncState, (KEY, "1m")).last_candle_ts == datetime(2026, 10, 18, 11, 36)
    assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 18, 11, 30)
    assert db.get(SyncState, (KEY, "4h")).last_candle_ts == datetime(2026, 10, 18, 4, 0)
    assert db.get(SyncState, (KEY, "1d")).last_candle_ts == datetime(2026, 10, 17)            # Saturday's candle
    assert db.get(SyncState, (KEY, "1w")).last_candle_ts == datetime(2026, 10, 5)             # the week of 12 October closes on Monday 19th
    assert db.get(SyncState, (KEY, "1mo")).last_candle_ts == datetime(2026, 9, 1)
    assert db.get(SyncState, (KEY, "3mo")).last_candle_ts == datetime(2026, 7, 1)
    assert abs(count(db, "1m") - 14 * 1440) <= 1                                               # only 14 days of 1 minute candles
    assert count(db, "5m") == 90 * 288 - 1                                                    # 90 days kept (history_days setting)
    assert count(db, "30m") == 120 * 48 - 1                                                   # 180 days kept, but the coin is only 120 days old
    assert all(db.get(SyncState, (KEY, tf)).gaps == 0 for tf in ("1m", "5m", "1h"))
    big = [c for c in client.calls if c[0] == "klines"]
    assert max(c[5] for c in big) == 1000 and min(c[5] for c in big) >= 1


def test_second_sync_asks_only_for_what_closed_since(db):
    before = {tf: count(db, tf) for tf in ("1m", "5m", "1d", "1w")}
    client = FakeBinance(now=NOW, listed=LISTED)
    asyncio.run(data_sync.sync_symbol(db, client, KEY, now=NOW))
    assert before == {tf: count(db, tf) for tf in before}
    k = [c for c in client.calls if c[0] == "klines"]
    assert all(c[5] <= 3 and c[6] == 1 for c in k)          # tiny requests at weight 1 each
    later = NOW + timedelta(minutes=10)
    client = FakeBinance(now=later, listed=LISTED)
    asyncio.run(data_sync.sync_symbol(db, client, KEY, ["1m", "5m"], now=later))
    assert db.get(SyncState, (KEY, "1m")).last_candle_ts == datetime(2026, 10, 18, 11, 46)
    assert abs(count(db, "1m") - before["1m"]) <= 1          # ten new ones in, the ten that fell out of the 14 days removed
    assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 18, 11, 40)
    for tf in ("1m", "5m", "1h", "1d", "3mo"):           # the count kept on the sync row, updated by arithmetic, stays exact
        assert db.get(SyncState, (KEY, tf)).candles == count(db, tf), tf
    assert data_sync.needs_sync(db, KEY, now=later + timedelta(minutes=5)) and not data_sync.needs_sync(db, KEY, now=later)


def test_gaps_are_found_and_repaired(db):
    hole = (datetime(2026, 10, 10, 3, 0), datetime(2026, 10, 10, 4, 55))
    db.execute(delete(Candle).where(Candle.instrument_key == KEY, Candle.timeframe == "5m", Candle.ts >= hole[0], Candle.ts <= hole[1]))
    db.commit()
    assert data_sync.gap_list(db, KEY, "5m", NOW) == [hole] and data_sync.gap_count(db, KEY, "5m", NOW) == 24
    asyncio.run(data_sync.sync_symbol(db, FakeBinance(now=NOW, listed=LISTED), KEY, ["5m"], repair_days=30, now=NOW))
    assert data_sync.gap_list(db, KEY, "5m", NOW) == [] and db.get(SyncState, (KEY, "5m")).gaps == 0


def test_funding_and_open_interest_are_stored(db):
    client = FakeBinance(now=NOW, listed=LISTED)
    client.funding[KEY] = [{"ts": NOW - timedelta(hours=4 * k), "rate": 0.0001, "mark": 500.0} for k in range(30, 0, -1)]
    assert asyncio.run(market.sync_funding(db, client, KEY)) == 30
    assert asyncio.run(market.sync_funding(db, client, KEY)) == 0           # only what is new
    asyncio.run(market.sync_open_interest(db, client, KEY))
    asyncio.run(market.sync_open_interest(db, client, KEY))                 # the same rows again: replaced, not doubled
    assert db.scalar(select(func.count()).select_from(OpenInterest).where(OpenInterest.period == "5m")) == 500
    asyncio.run(market.store_premium(db, client))
    assert db.get(Instrument, KEY).funding_rate == 0.0001
    db.execute(delete(FundingRate))
    db.commit()


def test_scan_creates_zones_and_is_incremental(db):
    first = scanner.scan_symbol(db, KEY, now=NOW)
    assert sum(s["zones"] for s in first) > 0
    zones = db.scalars(select(Zone).where(Zone.instrument_key == KEY)).all()
    assert {z.timeframe for z in zones} >= {"1m", "5m", "15m", "1h", "4h", "1d"}
    assert {z.status for z in zones} <= {"fresh", "tested", "violated", "consumed"}
    for z in zones:
        assert (z.proximal > z.distal) == (z.side == "demand")
        assert z.distal_from in ("base", "leg-in", "leg-out") and z.legout_count in (1, 2)
    active = [z for z in zones if z.status in ("fresh", "tested")]
    assert all(z.label in ("TYPE_1", "TYPE_2_3", "WEAK", "REJECT") and 0 <= z.score <= 8 for z in active)
    plans = db.scalars(select(TradePlan).where(TradePlan.is_current.is_(True))).all()
    for p in plans:        # every plan belongs to a mode and waits for the entry its trade score takes
        z = db.get(Zone, p.zone_id)
        score = p.context["score"]
        assert p.mode and p.stop == z.distal and p.reward_risk == 2.0 and score >= 5 and p.context["side"] in ("BUY", "SELL")
        assert p.entry_type == ("TYPE_1" if score >= 7 else "TYPE_3")
        assert abs(abs(p.target - p.entry) - 2 * abs(p.entry - p.stop)) < 1e-6
    n_zones = len(zones)
    second = scanner.scan_symbol(db, KEY, now=NOW)
    assert sum(s["candles"] for s in second) == 0
    assert db.scalar(select(func.count()).select_from(Zone)) == n_zones
