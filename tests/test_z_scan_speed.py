"""What keeps scans cheap: unchanged timeframes are skipped, long-dead zones are not stored on a catch-up,
old used-up zones are pruned, and a scan can run in a worker thread while the app keeps answering."""
import asyncio
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.data import sync as data_sync
from app.db import SessionLocal
from app.engine import scanner
from app.engine.core import TimeframeEngine
from app.main import app
from app.models import SyncState, Zone, ZoneEvent
from app import settings_store
from tests.fake_binance import FakeBinance
from tests.test_sync_scan import LISTED, NOW

SKEY = "SPEEDUSDT"


def test_catch_up_stores_live_zones_and_recent_dead_ones_only():
    with SessionLocal() as db:
        asyncio.run(data_sync.sync_symbol(db, FakeBinance(now=NOW, listed=LISTED), SKEY, now=NOW))
        stats = {s["timeframe"]: s for s in scanner.scan_symbol(db, SKEY, now=NOW)}
        found, stored = sum(s["zones"] for s in stats.values()), sum(s.get("stored", 0) for s in stats.values())
        assert stored < found / 4                                   # most zones died long ago and were never written (120 days of test data)
        zones = db.scalars(select(Zone).where(Zone.instrument_key == SKEY)).all()
        assert len(zones) == stored
        last = {tf: db.get(SyncState, (SKEY, tf)).last_scanned_ts for tf in stats}
        for z in zones:
            if z.status not in ("fresh", "tested"):
                assert z.updated_ts >= last[z.timeframe] - timedelta(days=scanner.keep_days(z.timeframe))
        # the live zones are exactly what the engine itself ends with: nothing useful was dropped
        df = data_sync.load_candles(db, SKEY, "30m")
        eng = TimeframeEngine(df, "30m", settings_store.thresholds(db, "30m"))
        for i in range(len(eng)):
            eng.step(i)
        live_db = sorted((z.side, z.proximal, z.distal, z.touches) for z in zones if z.timeframe == "30m" and z.status in ("fresh", "tested"))
        assert live_db == sorted((z.side, z.proximal, z.distal, z.touches) for z in eng.active) and live_db
        orphans = db.scalar(select(func.count()).select_from(ZoneEvent).where(ZoneEvent.zone_id.notin_(select(Zone.id))))
        assert orphans == 0                                         # every stored event belongs to a stored zone
        print("zones found", found, "stored", stored)


def test_unchanged_timeframes_are_skipped_unless_forced():
    with SessionLocal() as db:
        t = time.perf_counter()
        quick = scanner.scan_symbol(db, SKEY, now=NOW, force=False)
        took = time.perf_counter() - t
        assert all(s["skipped"] for s in quick) and took < 1.0
        forced = scanner.scan_symbol(db, SKEY, now=NOW, force=True)
        assert not any(s["skipped"] for s in forced) and sum(s["candles"] for s in forced) == 0


def test_old_used_up_zones_are_pruned():
    with SessionLocal() as db:
        old = Zone(instrument_key=SKEY, timeframe="1d", side="demand", pattern="DBR", proximal=10.0, distal=9.0, base_start_ts=NOW,
                   base_count=1, created_ts=NOW - timedelta(days=400), updated_ts=NOW - timedelta(days=300), atr=0.0, status="consumed")
        keep = Zone(instrument_key=SKEY, timeframe="1d", side="demand", pattern="DBR", proximal=10.0, distal=9.0, base_start_ts=NOW,
                    base_count=1, created_ts=NOW - timedelta(days=400), updated_ts=NOW - timedelta(days=300), atr=0.0, status="fresh")
        db.add_all([old, keep]); db.flush()
        db.add(ZoneEvent(zone_id=old.id, ts=NOW, event_type="consumed", price=9.5)); db.commit()
        old_id, keep_id = old.id, keep.id
        assert scanner.prune_closed(db, now=NOW) >= 1
        assert db.get(Zone, old_id) is None and db.get(Zone, keep_id) is not None      # an active zone is never pruned, however old
        assert db.scalar(select(func.count()).select_from(ZoneEvent).where(ZoneEvent.zone_id == old_id)) == 0
        db.delete(db.get(Zone, keep_id)); db.commit()


def test_pages_answer_while_a_scan_runs():
    """A forced scan of every timeframe runs in a worker thread; the API must keep answering meanwhile."""
    with SessionLocal() as db:
        settings_store.put(db, "universe", [SKEY])
        scanner.clear_key(db, SKEY)                 # make the scan a full one
    with TestClient(app) as c:
        job = c.post("/api/scan/run").json()["job_id"]
        slowest, calls = 0.0, 0
        while c.get(f"/api/jobs/{job}").json()["status"] == "running":
            t = time.perf_counter()
            assert c.get("/api/binance/status").status_code == 200
            slowest, calls = max(slowest, time.perf_counter() - t), calls + 1
            time.sleep(0.05)
        assert c.get(f"/api/jobs/{job}").json()["status"] == "done"
        assert calls > 5 and slowest < 2.0, (calls, slowest)       # it was answering all the way through
        print("requests answered during the scan:", calls, "slowest", round(slowest, 2), "s")
