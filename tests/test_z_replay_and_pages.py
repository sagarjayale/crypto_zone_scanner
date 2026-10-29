"""Replay and pages. The file name sorts after test_sync_scan on purpose: these tests reuse the candles and zones it creates.
(Gap repair is tested in test_sync_scan; crypto has no splits, so there is no adjusted-history check.)"""
import calendar
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db import SessionLocal
from app.main import app
from app.models import Zone
from tests.test_sync_scan import KEY, NOW


def test_pages_and_static_files_load():
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).status_code == 307
        for path in ("/chart", "/data", "/backtest", "/paper", "/settings", "/scanner", "/static/js/chart.js", "/static/css/app.css",
                     "/static/vendor/lightweight-charts.standalone.production.js"):
            assert c.get(path).status_code == 200, path
        got = c.get("/api/settings").json()
        assert set(got["symbols"]) == set(got["universe"])      # a display name for every instrument in the list


def test_replay_streams_bars_without_touching_live_tables():
    with SessionLocal() as db:
        zones_before = db.scalar(select(func.count()).select_from(Zone))
    with TestClient(app) as c:
        bad = c.post("/api/replay/start", json={"symbol": KEY, "timeframe": "30m", "start": "2030-01-01T00:00:00"})
        assert bad.status_code == 400 and "last stored candle" in bad.json()["detail"]
        r = c.post("/api/replay/start", json={"symbol": KEY, "timeframe": "30m", "start": "2026-10-01T00:00:00"}).json()
        with c.websocket_connect(f"/ws/replay/{r['session_id']}") as ws:
            init = ws.receive_json()
            assert init["type"] == "init" and len(init["candles"]) == 300 and init["remaining"] == r["remaining"]
            first_replay_bar = calendar.timegm(datetime(2026, 10, 1, 0, 0).timetuple())
            assert init["candles"][-1]["time"] < first_replay_bar
            ws.send_json({"cmd": "step"})
            bar = ws.receive_json()
            assert bar["type"] == "bar" and bar["remaining"] == r["remaining"] - 1
            assert bar["candle"]["time"] == first_replay_bar
            ws.send_json({"cmd": "next_event"})
            seen = 0
            while True:
                m = ws.receive_json()
                seen += 1
                if m["type"] == "end" or m["events"]:
                    break
            assert m["type"] == "bar" and m["events"][0]["type"] in ("created", "touched", "entry2", "entry3", "consumed", "violated")
            for z in m["zones"]:
                assert {"entry", "stop", "target", "score", "created"} <= set(z)
            ws.send_json({"cmd": "play", "speed": 60})
            assert ws.receive_json()["type"] == "bar"      # bars arrive on their own while playing
            ws.send_json({"cmd": "pause"})
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Zone)) == zones_before


def test_replay_matches_the_live_scan():
    """Same engine, same candles: replay to the end must leave the same active zones as the scan did."""
    from app.sim.replay import ReplaySession
    with SessionLocal() as db:
        s = ReplaySession(db, KEY, "1h", datetime(2026, 10, 1))
        while s.next_bar():
            pass
        live = db.scalars(select(Zone).where(Zone.instrument_key == KEY, Zone.timeframe == "1h",
                                             Zone.status.in_(("fresh", "tested")))).all()
        assert sorted((z.side, z.proximal, z.distal, z.touches) for z in s.eng.active) == \
               sorted((z.side, z.proximal, z.distal, z.touches) for z in live)
