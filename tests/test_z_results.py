"""The saved per-mode result and instrument readiness."""
import asyncio
import time
from datetime import timedelta

import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store, status
from app.db import SessionLocal
from app.engine import plan, result
from app.jobs import scheduler as jobs
from app.main import app
from app.models import InstrumentStatus, ModeResult, Zone
from tests.fake_binance import FakeBinance
from tests.test_sync_scan import LISTED, NOW
from tests.test_z_gtf_plans import GKEY, T0, snap


def test_result_has_every_part_of_the_pipeline():
    """GKEY was seeded by test_z_gtf_plans: day zones 500/480 and 620/640, 1 hr falling, 30 min zones 521/514 and 560/565."""
    with SessionLocal() as db:
        r = result.build(db, GKEY, "DAILY", cmp=555.0)
        assert (r["htf"], r["itf"], r["ltf"], r["cmp"]) == ("1d", "1h", "30m", 555.0)
        assert r["demand_zone"]["proximal"] == 500.0 and r["supply_zone"]["proximal"] == 620.0
        d = r["demand_zone"]
        assert d["score"] == d["freshness"] + d["strength"] + d["base"] and d["label"] in ("TYPE_1", "TYPE_2_3", "WEAK")
        assert (r["curve"], r["itf_trend"], r["trade_side"], r["market_state"]) == ("EQUILIBRIUM", "DOWN", "SELL", "NORMAL")
        z, sig = r["ltf_zone"], r["signal"]
        assert (z["proximal"], z["distal"], z["timeframe"]) == (560.0, 565.0, "30m") and r["zone_state"] == "APPROACHING"
        assert sig["signal"] == "SELL" and sig["zone_type"] in ("TYPE_1", "TYPE_2_3") and sig["score"] == z["score"]
        assert sig["type1"]["triggered"] is False and sig["type2"]["entry"] is None and sig["type3"]["entry"] is None
        assert (sig["primary_entry"], sig["entry"], sig["stoploss"], sig["target"], sig["waiting"]) == ("TYPE_1", 560.0, 565.0, 550.0, True)   # score 7: a limit at the proximal line, waiting
        touching = result.build(db, GKEY, "DAILY", cmp=561.0)
        assert touching["zone_state"] == "TOUCHING_NOW" and touching["signal"]["primary_entry"] == "TYPE_1" and touching["signal"]["waiting"] is False
        assert (touching["signal"]["entry"], touching["signal"]["stoploss"], touching["signal"]["target"]) == (560.0, 565.0, 550.0)
        far = result.build(db, GKEY, "DAILY", cmp=450.0)        # below the day demand: only supply is left, a bearish expansion
        assert (far["market_state"], far["demand_zone"], far["curve"], far["signal"]["signal"]) == ("BEARISH_EXPANSION", None, "EQUILIBRIUM", "SELL")
        assert r["direction_rule"] == "location"
        low = result.build(db, GKEY, "DAILY", cmp=526.0)        # low on the curve buys although the 1 hr ITF is down
        assert (low["curve"], low["itf_trend"], low["trade_side"], low["ltf_zone"]["proximal"]) == ("LOW", "DOWN", "BUY", 521.0)
        conf = low["confirmation"]
        assert [c["key"] for c in conf["checks"]] == ["modes", "contact", "btc", "volume", "strength", "funding", "oi"]
        assert conf["max"] == sum(c["max"] for c in conf["checks"]) and conf["points"] <= conf["max"] and conf["label"] in ("strong", "moderate", "weak", None)
        byk = {c["key"]: c for c in conf["checks"]}
        assert byk["contact"]["points"] == 1 and "1w" in byk["contact"]["note"]                  # the week zone touches the 30 minute zone
        assert byk["btc"]["applicable"] is False and byk["strength"]["applicable"] is False      # BTC is not stored in this test
        assert byk["funding"]["applicable"] is False and byk["oi"]["applicable"] is False        # nor funding or open interest for this coin
        assert result.build(db, GKEY, "WEEKLY", cmp=555.0)["confirmation"] is None               # no trade, no confirmation
        up = result.build(db, GKEY, "WEEKLY", cmp=555.0)        # no week supply stored and the day ITF is flat: no side
        assert up["signal"]["signal"] == "NO_TRADE" and up["signal"]["reason"]
        none = result.build(db, "NOTHINGUSDT", "MONTHLY")
        assert none["signal"] == {**none["signal"], "signal": "NO_TRADE", "reason": "No candles stored for the LTF yet"}


def test_refresh_saves_one_row_per_enabled_mode_and_only_rebuilds_what_changed():
    with SessionLocal() as db:
        assert result.refresh(db, GKEY) == 11
        rows = db.scalars(select(ModeResult).where(ModeResult.instrument_key == GKEY)).all()
        assert {r.mode for r in rows} == set(__import__("app.config", fromlist=["MODES"]).MODES)
        assert db.get(ModeResult, (GKEY, "DAILY")).signal in ("BUY", "SELL", "NO_TRADE")
        assert result.refresh(db, GKEY, changed={"5m"}) == 4          # 15M, 30M, HOURLY and 2HR read the 5 minute timeframe
        assert result.refresh(db, GKEY, changed={"1y"}) == 1 and result.refresh(db, GKEY, changed=set()) == 0


def test_instruments_show_up_one_by_one_as_they_become_ready(monkeypatch):
    slow, fast = "SLOWCOUSDT", "FASTCOUSDT"

    class Gate(FakeBinance):     # the slow coin's download waits until it is released
        release = None

        async def klines(self, symbol, interval, start=None, end=None, limit=1000):
            if symbol == slow:
                await Gate.release.wait()
            return await FakeBinance.klines(self, symbol, interval, start, end, limit)

    fake = Gate(now=NOW, listed=NOW - timedelta(days=20))
    monkeypatch.setattr("app.main.client", fake)
    with SessionLocal() as db:
        settings_store.put(db, "universe", [GKEY])
        status.mark(db, GKEY, "ready")
    with TestClient(app) as c:
        Gate.release = c.portal.call(asyncio.Event)
        out = c.put("/api/settings", json={"universe": [GKEY, fast, slow]}).json()
        assert out["ready"] == [GKEY] and out["status"][fast]["state"] in ("queued", "downloading", "scanning", "ready")
        for _ in range(240):                                   # the fast one finishes while the slow one is still downloading
            s = c.get("/api/settings").json()
            if fast in s["ready"]:
                break
            time.sleep(0.25)
        assert s["ready"] == [GKEY, fast] and s["status"][slow]["state"] == "downloading"
        board = c.get("/api/results?mode=MONTHLY").json()
        assert (board["total"], board["ready"]) == (3, 2) and [p["instrument_key"] for p in board["pending"]] == [slow]
        assert [r["instrument_key"] for r in board["rows"]] == [GKEY, fast] and board["rows"][1]["signal"]["signal"] in ("BUY", "SELL", "NO_TRADE")
        assert c.get("/api/results?mode=NOPE").status_code == 400
        one = c.get(f"/api/result/{fast}?mode=HOURLY").json()
        assert one["mode"] == "HOURLY" and one["ltf"] == "5m" and "demand_zone" in one
        c.portal.call(Gate.release.set)                        # now let the slow one through
        for _ in range(240):
            s = c.get("/api/settings").json()
            if slow in s["ready"]:
                break
            time.sleep(0.25)
        assert s["ready"] == [GKEY, fast, slow]
        assert c.get("/api/data/status").json()["status"][slow]["state"] == "ready"


def test_failed_download_is_recorded_and_a_ready_instrument_stays_ready():
    class Broken(FakeBinance):
        async def klines(self, *a, **k):
            from app.binance.client import BinanceError
            raise BinanceError("Binance said no")
    key = "FASTCOUSDT"
    try:
        asyncio.run(jobs.prepare(Broken(), key))
    except Exception:
        pass
    with SessionLocal() as db:
        row = db.get(InstrumentStatus, key)
        assert row.state == "failed" and "Binance said no" in row.detail and row.ready is True     # still shown, with its last data
        status.mark(db, key, "ready")