"""GTF plans from stored data: one instrument in DAILY mode (HTF day, ITF 1 hr, LTF 30 min)."""
from datetime import datetime, timedelta

import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.engine import plan
from app.main import app
from app.models import TradePlan, Zone

GKEY = "GTFPLANUSDT"
T0 = datetime(2026, 6, 1, 9, 15)


def candles(tf, closes, step):
    return [{"ts": T0 + step * n, "open": c - 1, "high": c + 2, "low": c - 2, "close": float(c), "volume": 1, "oi": 0} for n, c in enumerate(closes)]


def seed(db, itf_closes):
    upsert_candles(db, GKEY, "1d", candles("1d", [550.0] * 25, timedelta(days=1)))          # no HTF close breaches a zone
    upsert_candles(db, GKEY, "1h", candles("1h", itf_closes, timedelta(hours=1)))
    upsert_candles(db, GKEY, "30m", candles("30m", [540.0, 535.0, 530.0], timedelta(minutes=30)))
    mk = lambda tf, side, prox, dist, pat: Zone(
        instrument_key=GKEY, timeframe=tf, side=side, pattern=pat, proximal=prox, distal=dist, base_start_ts=T0, base_count=2,
        created_ts=T0, atr=0.0, peak_distance=0.0, touches=0, in_visit=False, status="fresh", score=0, has_gap=False,
        legout_body=0.8, legout_count=2, legout_close=prox + (15 if side == "demand" else -15), legin_explosive=True)
    zones = [mk("1d", "demand", 500.0, 480.0, "RBR"), mk("1d", "supply", 620.0, 640.0, "DBD"),
             mk("30m", "demand", 521.0, 514.0, "DBR"), mk("30m", "supply", 560.0, 565.0, "RBD")]
    db.add_all(zones)
    db.commit()
    return zones[2].id, zones[3].id


def snap(cmp):
    return {"cmp": cmp, "low": cmp - 1, "high": cmp + 1, "ema": 518.0, "times": [T0, T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)]}


def test_buy_plan_when_itf_is_up_then_withdrawn_when_it_turns():
    with SessionLocal() as db:
        demand_id, supply_id = seed(db, np.linspace(400, 560, 80))
        th = settings_store.thresholds(db, "30m")
        plan.refresh(db, GKEY, "30m", snap(530.0), th, T0)
        db.commit()
        p = db.scalars(select(TradePlan).where(TradePlan.zone_id == demand_id)).one()
        assert (p.mode, p.status, p.entry, p.stop, p.target, p.reward_risk) == ("DAILY", "pending", 521.0, 514.0, 535.0, 2.0)
        c = p.context
        assert (c["curve"], c["itf_trend"], c["htf_trend"], c["market_state"], c["side"]) == ("LOW", "UP", "SIDEWAYS", "NORMAL", "BUY")
        assert (c["htf"], c["itf"], c["ltf"], c["demand"]["proximal"], c["supply"]["proximal"]) == ("1d", "1h", "30m", 500.0, 620.0)
        assert p.entry_type == "TYPE_1" and p.plans["type1"]["entry"] == 521.0 and p.plans["type1"]["triggered"] is False
        assert p.plans["type2"]["entry"] is None and p.plans["type3"]["entry"] is None and p.triggered_ts is None
        z = db.get(Zone, demand_id)
        assert (z.score, z.label) == (8.0, "TYPE_1")                       # fresh 3 + two exciting candles 2 + two bases 2, plus the EMA inside the zone
        assert db.scalars(select(TradePlan).where(TradePlan.zone_id == supply_id)).first() is None     # ITF up: no sell plan
        assert p.shared == {"ltf": [], "htf": []}                          # nothing higher is close to either zone yet
        # a week demand zone that touches the 30 minute zone, and a month one that covers the day (HTF) zone
        mk = lambda tf, prox, dist: Zone(instrument_key=GKEY, timeframe=tf, side="demand", pattern="RBR", proximal=prox, distal=dist,
                                         base_start_ts=T0, base_count=2, created_ts=T0, atr=0.0, touches=0, status="fresh", score=0,
                                         has_gap=False, legout_body=0.8, legout_count=2, legout_close=prox + 10, legin_explosive=True)
        db.add_all([mk("1w", 514.0, 490.0), mk("1mo", 505.0, 470.0)])
        db.commit()
        plan.refresh(db, GKEY, "30m", snap(530.0), th, T0)
        db.commit(); db.refresh(p)
        assert [(c["tf"], c["relation"]) for c in p.shared["ltf"]] == [("1d", "near"), ("1w", "touch")] or \
               [(c["tf"], c["relation"]) for c in p.shared["ltf"]] == [("1w", "touch")]
        assert [(c["tf"], c["relation"]) for c in p.shared["htf"]] == [("1w", "overlap"), ("1mo", "inside")]
        # the same zone, price now within 1% of the proximal line
        plan.refresh(db, GKEY, "30m", snap(525.0), th, T0)
        db.commit(); db.refresh(p)
        assert p.zone_state == "APPROACHING" and p.version == 1
    with TestClient(app) as c:
        rows = [r for r in c.get("/api/setups?mode=DAILY").json() if r["zone_id"] == demand_id]
        assert len(rows) == 1 and (rows[0]["signal"], rows[0]["curve"], rows[0]["itf_trend"], rows[0]["label"]) == ("BUY", "LOW", "UP", "TYPE_1")
        assert rows[0]["plans"]["type1"]["target"] == 535.0 and rows[0]["htf_demand"]["proximal"] == 500.0
        assert [r for r in c.get("/api/setups?mode=WEEKLY").json() if r["zone_id"] == demand_id] == []
        assert rows[0]["contact_htf"][-1]["tf"] == "1mo" and any(x["tf"] == "1w" for x in rows[0]["contact_ltf"])
        mine = lambda q: [r for r in c.get(f"/api/setups?mode=DAILY&{q}").json() if r["zone_id"] == demand_id]
        assert len(mine("contact=any")) == 1 and len(mine("contact=1mo")) == 1 and mine("contact=1y") == []
        assert len(mine("timeframe=30m")) == 1 and mine("timeframe=1h") == []
        assert c.get("/api/setups?contact=sometimes").status_code == 400
        assert [r for r in c.get("/api/setups?min_score=8.5").json() if r["zone_id"] == demand_id] == []
        st = c.get("/api/live/status?mode=DAILY").json()
        assert st["bias_mode"] == "DAILY"
    with SessionLocal() as db:        # the ITF turns down: the buy plan is withdrawn and the supply zone gets a sell plan
        upsert_candles(db, GKEY, "1h", candles("1h", np.linspace(560, 400, 80), timedelta(hours=1)))
        db.commit()
        plan.refresh(db, GKEY, "30m", snap(555.0), settings_store.thresholds(db, "30m"), T0)
        db.commit()
        assert db.scalars(select(TradePlan).where(TradePlan.zone_id == demand_id)).one().status == "cancelled"
        sell = db.scalars(select(TradePlan).where(TradePlan.zone_id == supply_id)).one()
        assert (sell.status, sell.entry, sell.stop, sell.target, sell.context["side"]) == ("pending", 560.0, 565.0, 550.0, "SELL")


def test_only_enabled_modes_are_planned():
    with SessionLocal() as db:
        settings_store.put(db, "modes", ["WEEKLY"])           # DAILY switched off
        zid = db.scalars(select(Zone.id).where(Zone.instrument_key == GKEY, Zone.timeframe == "30m", Zone.side == "supply")).one()
        db.query(TradePlan).filter(TradePlan.zone_id == zid).delete()
        db.commit()
        plan.refresh(db, GKEY, "30m", snap(555.0), settings_store.thresholds(db, "30m"), T0)
        db.commit()
        assert db.scalars(select(TradePlan).where(TradePlan.zone_id == zid)).first() is None
        db.delete(db.get(__import__("app.models", fromlist=["Setting"]).Setting, "modes"))
        db.commit()
