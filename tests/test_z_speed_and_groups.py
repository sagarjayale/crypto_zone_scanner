"""The one-request chart, the forming candle, the coin list that follows the top N, the ranking behind the Top 20 and
Top 50 choices, and the scan workers. The file name sorts after test_sync_scan on purpose: it reuses those candles."""
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import settings_store
from app.db import SessionLocal
from app.jobs import scanpool
from app.jobs import scheduler as jobs
from app.main import app
from tests.test_sync_scan import KEY


def test_chart_data_in_one_request():
    with SessionLocal() as db:
        settings_store.put(db, "universe", [KEY])
    with TestClient(app) as c:
        got = c.get(f"/api/chart/{KEY}", params={"tf": "1h", "mode": "DAILY", "overlays": "4h,1d,nope", "old": "true"}).json()
        assert got["instrument_key"] == KEY and len(got["candles"]) == 1500 and set(got["candles"][0]) == {"ts", "open", "high", "low", "close"}
        assert got["zones"] and all(z["status"] in ("fresh", "tested") for z in got["zones"])
        assert set(got["overlays"]) == {"4h", "1d"} and got["result"]["mode"] == "DAILY"
        assert all(z["status"] in ("consumed", "violated") for z in got["old"]) and len(got["old"]) <= 300
        assert c.get(f"/api/chart/{KEY}", params={"tf": "1h", "result": "false"}).json()["result"] is None
        assert c.get(f"/api/chart/{KEY}", params={"tf": "7h"}).status_code == 400


def test_forming_candle_from_binance_is_cached(monkeypatch):
    calls = []

    class Stub:
        async def close(self):
            pass

        async def klines(self, symbol, interval, start=None, end=None, limit=1000):
            calls.append((symbol, interval, limit))
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            t = now.replace(second=0, microsecond=0, minute=now.minute - now.minute % 5)
            close_ms = int((t + timedelta(minutes=5)).replace(tzinfo=timezone.utc).timestamp() * 1000) - 1
            return [{"ts": t - timedelta(minutes=5), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "close_ms": close_ms - 300000},
                    {"ts": t, "open": 1.5, "high": 1.7, "low": 1.4, "close": 1.6, "close_ms": close_ms}]
    monkeypatch.setattr("app.main.client", Stub())
    with TestClient(app) as c:
        f = c.get(f"/api/forming/{KEY}", params={"tf": "5m"}).json()["forming"]
        assert f["close"] == 1.6 and f["open"] == 1.5 and f["close_ts"] > f["ts"]
        c.get(f"/api/forming/{KEY}", params={"tf": "5m"})
        assert calls == [(KEY, "5m", 2)]                                  # the second request within 3 seconds is served from memory
        assert c.get(f"/api/forming/{KEY}", params={"tf": "3mo"}).json() == {"forming": None}   # no Binance interval


def test_the_list_follows_the_top_n_until_chosen_by_hand():
    with SessionLocal() as db:
        settings_store.put(db, "universe_auto", True)
        settings_store.put(db, "top_n", 2)
        settings_store.put(db, "universe", [KEY])
        added = jobs.follow_top(db)
        assert settings_store.get(db, "universe") == ["BTCUSDT", "ETHUSDT"] and added == ["BTCUSDT", "ETHUSDT"]
    with TestClient(app) as c:
        s = c.get("/api/settings").json()
        assert s["ranked"] == ["BTCUSDT", "ETHUSDT"]                     # most traded first: the Top 20 and Top 50 choices
        out = c.put("/api/settings", json={"universe": [KEY, "BTCUSDT"]}).json()
        assert out["universe_auto"] is False and out["ranked"][0] == "BTCUSDT"     # chosen by hand: no longer automatic
        assert c.put("/api/settings", json={"top_n": 0}).status_code == 400
        assert c.put("/api/settings", json={"history_days": {"5m": 0}}).status_code == 400
        assert c.put("/api/settings", json={"history_days": {"5m": 120, "1h": 400}}).json()["history_days"] == {"5m": 120, "1h": 400}
        board = c.get("/api/results", params={"mode": "DAILY"}).json()
        assert board["ranked"][0] == "BTCUSDT" and set(board["ranked"]) == {KEY, "BTCUSDT"}
    with SessionLocal() as db:
        for k in ("universe_auto", "top_n", "history_days"):
            settings_store.put(db, k, {"universe_auto": True, "top_n": 50, "history_days": {"1m": 14, "5m": 90, "15m": 180, "30m": 180, "1h": 365}}[k])
        settings_store.put(db, "universe", [KEY])


def test_scan_workers_and_pages_answer_quickly():
    assert scanpool.workers() == 0                     # SQLite: scans run in a thread (one writer at a time)
    with TestClient(app) as c:
        for path in ("/api/results?mode=DAILY", "/api/data/status", f"/api/chart/{KEY}?tf=5m&mode=HOURLY", "/api/live/status?mode=DAILY"):
            t = time.perf_counter()
            assert c.get(path).status_code == 200
            assert time.perf_counter() - t < 2.0, path


def test_old_touched_again_rows_are_removed_once_and_no_longer_written():
    from sqlalchemy import func, inspect, select
    from app.db import engine
    from app.models import Setting, Zone, ZoneEvent
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ZoneEvent).where(ZoneEvent.event_type == "touched_again")) == 0   # scans write none
        zid = db.scalar(select(Zone.id).limit(1))
        db.add_all([ZoneEvent(zone_id=zid, ts=datetime(2026, 1, 1), event_type="touched_again", price=1.0) for _ in range(5)])
        db.commit()
        row = db.get(Setting, "events_tidied")
        if row is not None:
            db.delete(row); db.commit()
    assert jobs._tidy_events(lambda text: None) == 5
    assert jobs._tidy_events(lambda text: None) == 0                        # done once
    assert "ix_events_type_ts" in {ix["name"] for ix in inspect(engine).get_indexes("zone_events")}


def test_a_coin_is_never_ready_without_its_candles():
    import asyncio
    from app import status
    from app.models import InstrumentStatus
    from tests.fake_binance import FakeBinance
    with SessionLocal() as db:
        status.mark(db, "EMPTYUSDT", "ready")                      # as an earlier version could leave it
        status.adopt_existing(db, [KEY, "EMPTYUSDT"])
        row = db.get(InstrumentStatus, "EMPTYUSDT")
        assert (row.ready, row.state) == (False, "queued") and db.get(InstrumentStatus, KEY).ready is True
    asyncio.run(jobs.prepare(FakeBinance(), "EMPTYUSDT", download=False))      # a scan with nothing downloaded
    with SessionLocal() as db:
        db.expire_all()
        assert db.get(InstrumentStatus, "EMPTYUSDT").ready is False
        assert status.downloaded(db, [KEY, "EMPTYUSDT"]) == [KEY]


def test_a_row_held_by_another_writer_is_tried_again():
    from sqlalchemy.exc import OperationalError
    from app.db import retry_locked
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise OperationalError("UPDATE sync_state", {}, Exception("(1205, 'Lock wait timeout exceeded')"))
        return "stored"
    with SessionLocal() as db:
        assert retry_locked(db, flaky, wait=0) == "stored" and len(calls) == 3
        calls.clear()
        try:
            retry_locked(db, lambda: (_ for _ in ()).throw(OperationalError("x", {}, Exception("no such table"))), wait=0)
            assert False
        except OperationalError:
            pass                                    # anything other than a lock is not retried


def test_backtest_checks_run_in_worker_processes_with_the_same_result(monkeypatch):
    import asyncio
    from datetime import date
    from app.sim import scancheck
    calls = [(KEY, m, date(2026, 3, 2), {}) for m in ("DAILY", "WEEKLY")]
    in_thread = asyncio.run(scanpool.map_heavy(scancheck.check_key, calls))
    monkeypatch.setattr(scanpool, "workers", lambda: 2)
    try:
        in_processes = asyncio.run(scanpool.map_heavy(scancheck.check_key, calls))
    finally:
        scanpool.shutdown()
    assert [r["symbol"] for r in in_processes] == [KEY, KEY] and in_processes == in_thread


def test_the_list_of_saved_checks_reads_only_their_totals():
    from app.models import BacktestRun
    with TestClient(app) as c:
        out = c.post("/api/scancheck", json={"symbols": [KEY], "mode": "DAILY", "scan_date": "2026-03-02", "save": True}).json()
        listed = next(x for x in c.get("/api/scanchecks").json() if x["id"] == out["id"])
        assert listed["summary"] == out["summary"] and listed["coins"] == 1
    with SessionLocal() as db:                       # a check saved by an earlier version, without its totals
        r = db.get(BacktestRun, out["id"]); r.brief = None; db.commit()
    with TestClient(app) as c:
        assert next(x for x in c.get("/api/scanchecks").json() if x["id"] == out["id"])["summary"] == out["summary"]
    with SessionLocal() as db:
        assert db.get(BacktestRun, out["id"]).brief == out["summary"]
