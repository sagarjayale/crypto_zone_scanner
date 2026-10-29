"""Times on the dashboard (signal since, the candle a result rests on) and on the paper page (placed, signal, entry,
exit, duration, CSV). The file name sorts after test_z_paper on purpose: it reuses its helpers and data."""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.engine import result as results
from app.main import app
from app.models import ModeResult, PaperTrade
from app.sim import paper
from tests.test_sync_scan import KEY
from tests.test_z_live import LIVE_KEY
from tests.test_z_paper import NOW, fresh_setup


def fake_result(signal: str, as_of: str) -> dict:
    return {"instrument_key": "TIMEUSDT", "mode": "DAILY", "ltf": "1h", "as_of": as_of, "signal": {"signal": signal}}


def test_signal_since_holds_while_the_signal_holds_and_moves_when_it_changes():
    with SessionLocal() as db:
        results.save(db, "TIMEUSDT", "DAILY", fake_result("BUY", "2026-10-19T08:00:00"), datetime(2026, 10, 19, 9, 0, 8))
        db.commit()
        row = db.get(ModeResult, ("TIMEUSDT", "DAILY"))
        assert row.signal_since == datetime(2026, 10, 19, 9, 0) and row.candle_ts == datetime(2026, 10, 19, 9, 0)   # 08:00 candle closes 09:00
        results.save(db, "TIMEUSDT", "DAILY", fake_result("BUY", "2026-10-19T10:00:00"), datetime(2026, 10, 19, 11, 0, 8))
        db.commit()
        assert row.signal_since == datetime(2026, 10, 19, 9, 0) and row.candle_ts == datetime(2026, 10, 19, 11, 0)   # still a buy: unchanged
        results.save(db, "TIMEUSDT", "DAILY", fake_result("NO_TRADE", "2026-10-19T11:00:00"), datetime(2026, 10, 19, 12, 0, 8))
        db.commit()
        assert row.signal_since == datetime(2026, 10, 19, 12, 0)                                                        # changed: moves
        db.delete(row)
        db.commit()


def test_the_dashboard_rows_carry_their_times():
    with TestClient(app) as c:
        rows = c.get("/api/results", params={"mode": "DAILY"}).json()["rows"]
        mine = next(r for r in rows if r["instrument_key"] == KEY)
        assert mine["signal_since"] and mine["candle_ts"] and mine["updated_at"]
        assert datetime.fromisoformat(mine["candle_ts"]) == results.candle_close(mine)


def test_paper_times_duration_and_csv():
    zid = fresh_setup()
    with SessionLocal() as db:
        db.merge(ModeResult(instrument_key=LIVE_KEY, mode="WEEKLY", signal="BUY", signal_since=NOW - timedelta(hours=2)))
        db.commit()
        order = paper.place(db, zid, NOW)
        assert order.created_at == NOW and order.signal_ts == NOW - timedelta(hours=2)      # the signal behind the order
    with TestClient(app) as c:
        pending = next(o for o in c.get("/api/paper/positions").json()["pending"] if o["zone_id"] == zid)
        assert pending["created_at"].startswith("2026-10-18T11:00") and pending["signal_ts"].startswith("2026-10-18T09:00")
    with SessionLocal() as db:
        paper.on_prices(db, {LIVE_KEY: 521.0}, NOW + timedelta(minutes=15))                  # filled
        paper.on_prices(db, {LIVE_KEY: 558.0}, NOW + timedelta(hours=27, minutes=45))         # target: 1 d 3 h 30 min later
        t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        assert t.signal_ts == NOW - timedelta(hours=2) and t.fill_ts == NOW + timedelta(minutes=15)
    with TestClient(app) as c:
        closed = next(x for x in c.get("/api/paper/positions").json()["closed"] if x["zone_id"] == zid)
        assert closed["duration_s"] == (27 * 60 + 30) * 60
        csv = c.get("/api/paper/export")
        assert csv.status_code == 200 and "paper_trades.csv" in csv.headers["content-disposition"]
        head, *lines = csv.text.strip().splitlines()
        assert head.startswith("id,symbol,timeframe,side") and "fill_ts (UTC)" in head and "duration" in head
        line = next(x for x in lines if x.split(",")[0] == str(t.id))
        assert "1 d 3 h" in line and "2026-10-18 11:15:00" in line and "2026-10-19 14:45:00" in line
    with SessionLocal() as db:
        db.delete(db.get(ModeResult, (LIVE_KEY, "WEEKLY")))
        db.commit()


def test_enter_a_pending_order_now_at_market():
    import pytest
    from app.models import PaperOrder
    from app import settings_store
    zid = fresh_setup()                                              # long: entry 521, stop 512, target 557
    with SessionLocal() as db:
        settings_store.put(db, "paper_slippage_pct", 0)
        order = paper.place(db, zid, NOW)
        oid = order.id
        with pytest.raises(ValueError, match="past the stop"):
            paper.enter_now(db, oid, 511.0, NOW)
        with pytest.raises(ValueError, match="past the target"):
            paper.enter_now(db, oid, 560.0, NOW)
        with pytest.raises(paper.LowReward, match="only 0.55 to 1"):
            paper.enter_now(db, oid, 541.0, NOW)                     # 16 to the target, 29 to the stop
        look = paper.enter_now(db, oid, 530.0, NOW, dry_run=True)
        assert look["rr"] == 1.5 and look["quantity"] == 5.555 and db.get(PaperOrder, oid).status == "pending"   # nothing entered
        got = paper.enter_now(db, oid, 530.0, NOW + timedelta(minutes=3))
        t = db.get(PaperTrade, got["trade_id"])
        assert (t.fill_price, t.stop, t.target, t.entry_type, t.fill_ts) == (530.0, 512.0, 557.0, "MANUAL", NOW + timedelta(minutes=3))
        assert t.quantity == 5.555 and t.risk == 18.0                # 1% of 10,000 = 100 USDT over 18 a coin, step 0.001
        assert t.fees == pytest.approx(5.555 * 530 * 0.0005)         # a market entry pays the taker fee
        assert db.get(PaperOrder, oid).status == "filled"
        with pytest.raises(ValueError, match="No pending order"):
            paper.enter_now(db, oid, 530.0, NOW)


def test_enter_now_through_the_api_asks_before_a_poor_reward():
    from app import live, settings_store
    zid = fresh_setup()
    with SessionLocal() as db:
        settings_store.put(db, "paper_slippage_pct", 0)
        oid = paper.place(db, zid, NOW).id
    live.state["prices"][LIVE_KEY] = 541.0
    try:
        with TestClient(app) as c:
            assert c.post(f"/api/paper/orders/{oid}/enter", json={"dry_run": True}).json()["low_rr"] is True
            refused = c.post(f"/api/paper/orders/{oid}/enter", json={})
            assert refused.status_code == 409 and "0.55 to 1" in refused.json()["detail"]
            ok = c.post(f"/api/paper/orders/{oid}/enter", json={"allow_low_rr": True})
            assert ok.status_code == 200 and ok.json()["price"] == 541.0
            assert c.post(f"/api/paper/orders/{oid}/enter", json={}).status_code == 400          # already entered
            opened = next(t for t in c.get("/api/paper/positions").json()["open"] if t["id"] == ok.json()["trade_id"])
            assert opened["entry_type"] == "MANUAL" and opened["fill_price"] == 541.0
    finally:
        live.state["prices"].pop(LIVE_KEY, None)


def test_the_paper_chart_of_an_order_an_open_trade_and_a_closed_trade():
    from app import settings_store
    zid = fresh_setup()
    with SessionLocal() as db:
        settings_store.put(db, "paper_slippage_pct", 0)
        oid = paper.place(db, zid, NOW).id
    with TestClient(app) as c:
        d = c.get(f"/api/paper/chart/order/{oid}").json()
        assert d["status"] == "pending" and d["entry_ts"] is None and d["placed_at"].startswith("2026-10-18T11:00")
        assert (d["entry"], d["stop"], d["target"], d["rr"]) == (521.0, 512.0, 557.0, 4.0)
        assert d["zone"]["zone_id"] == zid and d["zone"]["score"] is not None and isinstance(d["candles"], list)
    with SessionLocal() as db:
        paper.on_prices(db, {LIVE_KEY: 521.0}, NOW + timedelta(minutes=15))
        tid = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one().id
    with TestClient(app) as c:
        d = c.get(f"/api/paper/chart/trade/{tid}").json()
        assert d["status"] == "open" and d["result"] == "open" and d["entry_ts"].startswith("2026-10-18T11:15") and d["exit_ts"] is None
        assert d["placed_at"].startswith("2026-10-18T11:00") and d["liq_price"] and "fees" in d
    with SessionLocal() as db:
        paper.on_prices(db, {LIVE_KEY: 511.0}, NOW + timedelta(hours=2))
    with TestClient(app) as c:
        d = c.get(f"/api/paper/chart/trade/{tid}").json()
        assert d["status"] == "closed" and d["result"] == "loss" and d["exit_reason"] == "stop" and d["duration_s"] == 105 * 60
        assert c.get("/api/paper/chart/trade/999999").status_code == 404
        assert c.get(f"/api/paper/chart/nope/{tid}").status_code == 400


def test_only_the_best_ltf_zone_alerts_unless_every_zone_is_asked_for():
    from app import live, settings_store
    from app.models import TradePlan, Zone
    from tests.test_z_live import make_setup
    with SessionLocal() as db:
        best, other = make_setup(db), make_setup(db)
        p = db.scalars(select(TradePlan).where(TradePlan.zone_id == other)).one()
        p.context = {**p.context, "rank": 1}                       # the second-best zone of the same coin and mode
        db.commit()
        ids = lambda: {z.id for z, _ in live.watch_rows(db)}
        assert best in ids() and other not in ids()               # default: only the best zone
        settings_store.put(db, "all_ltf_zones", True)
        assert {best, other} <= ids()
        settings_store.put(db, "all_ltf_zones", False)
        for zid in (best, other):
            db.get(Zone, zid).status = "violated"
        db.commit()
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"all_ltf_zones": "yes"}).status_code == 400
        assert c.put("/api/settings", json={"all_ltf_zones": False}).json()["all_ltf_zones"] is False


def test_the_result_lists_the_other_ltf_zones_and_plans_keep_their_rank(monkeypatch):
    from types import SimpleNamespace
    from app.engine import gtf
    from app.models import TradePlan
    with SessionLocal() as db:
        assert any("rank" in (p.context or {}) for p in db.scalars(select(TradePlan).where(TradePlan.zone_id.isnot(None))).all())
        real = gtf.candidates
        from app.models import ModeResult
        hit = next(((r.instrument_key, r.mode) for r in db.scalars(select(ModeResult)).all()
                    if (r.data or {}).get("ltf_zone_count") and results.build(db, r.instrument_key, r.mode)["ltf_zone_count"]), None)
        assert hit, "the test data has no LTF setup in any mode"
        key, mode = hit

        def two(zones, ctx, cmp, ema, th, skipped=None):      # the best zone, plus a weaker copy of it
            found = real(zones, ctx, cmp, ema, th, skipped)
            if not found:
                return found
            z, total, label = found[0]
            weaker = SimpleNamespace(**{c.name: getattr(z, c.name) for c in z.__table__.columns})
            weaker.base_count = 9
            return found[:1] + [(weaker, 5.0, "TYPE_2_3"), (weaker, 4.0, "REJECT")]     # a score of 4 is not traded: left out
        monkeypatch.setattr(gtf, "candidates", two)
        out = results.build(db, key, mode)
        assert out["ltf_zone_count"] == 3 and len(out["other_ltf_zones"]) == 1
        o = out["other_ltf_zones"][0]
        assert o["score"] == 5.0 and o["primary_entry"] in ("TYPE_2", "TYPE_3") and o["stoploss"] is not None and o["zone"]["zone_id"]
