"""The minute job catches up timeframes a skipped or failed run left behind, runs the paper safety net for held coins
even when their download fails, and the Paper page is told how far behind a coin's candles are."""
import asyncio
from datetime import datetime, timedelta

from app.jobs import scheduler as jobs
from app.models import SyncState
from app.sim import paper
from app.db import SessionLocal

KEY = "CATCHUPUSDT"


def state(db, tf, last, synced):
    st = db.get(SyncState, (KEY, tf)) or SyncState(instrument_key=KEY, timeframe=tf)
    st.last_candle_ts, st.last_sync_at = last, synced
    db.merge(st)
    db.commit()


def test_overdue_timeframes():
    now = datetime(2026, 10, 18, 12, 7, 8)
    with SessionLocal() as db:
        state(db, "5m", datetime(2026, 10, 18, 4, 45), datetime(2026, 10, 18, 4, 50, 13))      # 7 hours behind
        state(db, "15m", datetime(2026, 10, 18, 11, 45), datetime(2026, 10, 18, 12, 0, 8))     # up to date (next closes 12:15)
        state(db, "1h", datetime(2026, 10, 18, 9, 0), datetime(2026, 10, 18, 11, 59, 0))       # behind, but asked 8 min ago
        late = jobs.overdue_timeframes(db, [KEY], now)
    assert late == {KEY: ["5m"]}
    with SessionLocal() as db:
        assert sorted(jobs.overdue_timeframes(db, [KEY], now + timedelta(hours=1))[KEY]) == ["15m", "1h", "5m"]
        assert jobs.overdue_timeframes(db, [], now) == {}


def test_safety_net_runs_even_when_the_download_fails(monkeypatch):
    now = datetime(2026, 10, 18, 12, 0, 8)
    ran = []

    async def broken(*a, **k):
        raise RuntimeError("binance timeout")
    monkeypatch.setattr(jobs.data_sync, "sync_symbol", broken)
    monkeypatch.setattr(paper, "on_candles", lambda db, key: ran.append(key) or [])
    monkeypatch.setattr(paper, "live_orders", lambda db, key=None: [])
    monkeypatch.setattr(paper, "open_trades", lambda db, key=None: [type("T", (), {"instrument_key": KEY})()])
    monkeypatch.setattr(jobs.status, "ready_keys", lambda db, keys: [k for k in keys if k == KEY])
    monkeypatch.setattr(jobs.status, "downloaded", lambda db, keys: list(keys))

    async def no_job(*a, **k):
        return None
    monkeypatch.setattr(jobs, "run_job", no_job)
    out = asyncio.run(jobs.bar_close(object(), now))
    assert ran == [KEY] and out["errors"] == 1 and "binance timeout" in out["first_error"]


def test_safety_net_for_a_held_coin_that_is_not_downloaded(monkeypatch):
    ran = []
    monkeypatch.setattr(paper, "on_candles", lambda db, key: ran.append(key) or [])
    monkeypatch.setattr(paper, "live_orders", lambda db, key=None: [])
    monkeypatch.setattr(paper, "open_trades", lambda db, key=None: [type("T", (), {"instrument_key": "GONEUSDT"})()])
    monkeypatch.setattr(jobs.status, "ready_keys", lambda db, keys: [])
    monkeypatch.setattr(jobs.status, "downloaded", lambda db, keys: [])
    asyncio.run(jobs.bar_close(object(), datetime(2026, 10, 18, 12, 0, 8)))
    assert ran == ["GONEUSDT"]


def test_candle_lag_for_the_paper_page():
    with SessionLocal() as db:
        state(db, "1m", datetime(2026, 10, 18, 4, 45), datetime(2026, 10, 18, 4, 46, 8))
        lag = paper.candle_lag(db, KEY, datetime(2026, 10, 18, 12, 46, 30))
        assert lag == {"tf": "1m", "minutes": 8 * 60}
        assert paper.candle_lag(db, "NOSUCHUSDT")["minutes"] is None
