"""The lock trailing stop: steps of a % of the distance from the entry to the target, and the target moving forward."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import settings_store
from app.config import DEFAULTS
from app.data import sync as data_sync
from app.db import SessionLocal
from app.main import app
from app.models import PaperTrade
from app.sim import paper, trailing
from app.sim.backtest import run_one
from tests.test_sync_scan import KEY
from tests.test_z_backtest import END, MODE, OPTS, START

NO_FEES = {**DEFAULTS, "fee_maker_pct": 0.0, "fee_taker_pct": 0.0}


def lock(side="demand", entry=100.0, stop=90.0, target=200.0, th=NO_FEES, **opts):
    return trailing.Trailer(side, entry, stop, th, trailing.options({"trail_method": "lock", **opts}), target=target)


def bar(t, high, low):
    return t.close_bar(high, low, (high + low) / 2)


def test_defaults_are_off_with_ten_percent_steps_and_a_moving_target():
    assert DEFAULTS["trail_method"] == "off"
    assert (DEFAULTS["trail_lock_pct"], DEFAULTS["trail_lock_gap_pct"], DEFAULTS["trail_lock_target"]) == (10.0, 20.0, "move")


def test_the_example_entry_100_stop_90_target_200():
    t = lock()
    assert t.target_removed                   # "move": the target never closes the trade
    assert bar(t, 105, 101) is None and t.stop == 90
    assert bar(t, 112, 104) == pytest.approx(110)          # 10 % of the way and 2 more: 10 locked
    assert bar(t, 121, 111) is None and t.stop == pytest.approx(110)   # not yet 2 past 120
    assert bar(t, 125, 115) == pytest.approx(120)          # 125: 20 locked, a fall back to 120 exits at +20
    assert bar(t, 152, 130) == pytest.approx(150)
    assert bar(t, 200, 150) == pytest.approx(190) and t.target == pytest.approx(210)   # target reached: it moves forward
    assert bar(t, 202, 195) == pytest.approx(200) and t.target == pytest.approx(210)
    assert bar(t, 210, 200) is None and t.target == pytest.approx(220) and t.target_moves == 2
    assert bar(t, 205, 201) is None and t.stop == pytest.approx(200)  # a pullback never lowers the stop


def test_a_short_is_the_mirror_image():
    t = lock("supply", 100.0, 110.0, 0.0)
    assert bar(t, 99, 88) == pytest.approx(90)
    assert bar(t, 90, 75) == pytest.approx(80)
    assert t.stop == pytest.approx(80) and t.target == pytest.approx(0)


def test_close_at_the_target_and_other_step_sizes():
    t = lock(trail_lock_target="close")
    assert not t.target_removed
    bar(t, 200, 150)
    assert t.target == 200 and t.stop == pytest.approx(190)
    half = lock(trail_lock_pct=50, trail_lock_gap_pct=0)       # steps of 50, no gap
    assert bar(half, 149, 100) is None and bar(half, 150, 120) == pytest.approx(150)


def test_the_stop_never_goes_under_break_even_and_lock_needs_a_target():
    fees = {**DEFAULTS, "fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False}
    t = lock(entry=100.0, stop=99.0, target=100.05, th=fees, trail_lock_pct=10, trail_lock_gap_pct=0)   # steps smaller than the fees
    assert bar(t, 100.03, 100.0) == pytest.approx(100 * 1.0007)
    none = lock(target=None)
    assert bar(none, 150, 120) is None and none.stop == 90 and not none.target_removed


def test_the_backtest_with_the_lock_trailing_stop():
    with SessionLocal() as db:
        plain = {(t["entry_ts"], t["zone_created_ts"]): t for t in run_one(db, KEY, MODE, START, END, OPTS)}
        moving = run_one(db, KEY, MODE, START, END, {**OPTS, "trail_method": "lock"})
        closing = run_one(db, KEY, MODE, START, END, {**OPTS, "trail_method": "lock", "trail_lock_target": "close"})
    assert {(t["entry_ts"], t["zone_created_ts"]) for t in moving} == set(plain)      # the same entries
    assert moving and not [t for t in moving if t["exit_reason"] == "target"]        # "move": never closed at the target
    for t in moving:
        sign = 1 if t["side"] == "demand" else -1
        assert (t["final_target"] - t["target"]) * sign >= -1e-9                     # the target only moves forward
        if t["exit_reason"] == "trail":
            assert (t["final_stop"] - t["entry"]) * sign > 0 and t["stop_moves"] >= 1
    assert {t["exit_reason"] for t in closing} <= {"target", "stop", "trail", "open"}
    for t in closing:
        if t["exit_reason"] == "target":
            assert t["final_target"] == t["target"]


def test_paper_positions_lock_and_move_the_target():
    from tests.test_z_live import LIVE_KEY
    from tests.test_z_paper import NOW, fresh_setup
    zid = fresh_setup()                                     # long: entry 521, stop 512, target 557 (36 away)
    with SessionLocal() as db:
        for k, v in (("trail_method", "lock"), ("trail_lock_pct", 50.0), ("trail_lock_gap_pct", 20.0)):
            settings_store.put(db, k, v)                    # steps of 18, gap 3.6
        try:
            paper.place(db, zid, NOW)
            paper.on_prices(db, {LIVE_KEY: 521.0}, NOW)
            t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
            assert t.trail["no_target"] and t.trail["first_target"] == 557.0
            assert paper.on_prices(db, {LIVE_KEY: 558.0}, NOW + timedelta(minutes=1)) == []   # past the target: no close
            hour = NOW.replace(minute=0) + timedelta(hours=1)
            data_sync.upsert_candles(db, LIVE_KEY, "1h", [
                {"ts": hour, "open": 521, "high": 543, "low": 520, "close": 540, "volume": 1, "oi": None},            # 22 up: 18 locked
                {"ts": hour + timedelta(hours=1), "open": 540, "high": 562, "low": 539.5, "close": 560, "volume": 1, "oi": None}])  # target reached
            out = paper.update_trailing(db, t, hour + timedelta(hours=2))
            db.commit()
            assert [e["event"] for e in out] == ["stop moved", "target moved"]
            assert t.stop == pytest.approx(521 + 36) and t.target == pytest.approx(557 + 18)
            assert t.history[-1]["field"] == "target" and "moved forward" in t.history[-1]["note"]
            assert paper.update_trailing(db, t, hour + timedelta(hours=2)) == []
            out = paper.on_prices(db, {LIVE_KEY: 556.0}, hour + timedelta(hours=2, minutes=5))
            db.refresh(t)
            assert [e["event"] for e in out] == ["stopped"] and t.exit_reason == "trail" and t.result == "win"
        finally:
            for k in ("trail_method", "trail_lock_pct", "trail_lock_gap_pct"):
                settings_store.put(db, k, DEFAULTS[k])


def test_the_settings_and_a_backtest_run_take_the_lock_options():
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"trail_lock_pct": 0}).status_code == 400
        assert c.put("/api/settings", json={"trail_lock_gap_pct": 150}).status_code == 400
        assert c.put("/api/settings", json={"trail_lock_target": "stay"}).status_code == 400
        out = c.put("/api/settings", json={"trail_method": "lock", "trail_lock_pct": 25, "trail_lock_target": "close"}).json()
        assert (out["trail_method"], out["trail_lock_pct"], out["trail_lock_target"]) == ("lock", 25, "close")
        c.put("/api/settings", json={"trail_method": "off", "trail_lock_pct": 10, "trail_lock_target": "move"})
        run = c.post("/api/backtest/run", json={"symbols": [KEY], "modes": [MODE], "start": "2026-10-01", "end": "2026-10-02",
                                                "trail_method": "lock", "trail_lock_pct": 30}).json()["run_id"]
        settings = c.get(f"/api/backtest/{run}").json()["settings"]
        assert (settings["trail_method"], settings["trail_lock_pct"], settings["trail_lock_target"]) == ("lock", 30, "move")


def test_the_locked_column_of_an_open_position():
    from types import SimpleNamespace
    st = {**trailing.options({"trail_method": "lock"}), "first_stop": 90.0, "first_target": 200.0, "target_moves": 0}
    t = SimpleNamespace(side="demand", fill_price=100.0, stop=90.0, target=200.0, quantity=1.0, fees=0.0, trail=st)
    k = paper.lock_view(t, NO_FEES)
    assert not k["is_locked"] and k["locked_usdt"] == -10 and k["steps"] == 0
    assert (k["next_trigger"], k["next_stop"]) == (pytest.approx(112), pytest.approx(110))
    t.stop = 120.0                                            # price went to 125: 20 locked
    k = paper.lock_view(t, NO_FEES)
    assert k["is_locked"] and k["locked_usdt"] == 20 and k["locked_r"] == 2 and k["steps"] == 2 and k["pct_locked"] == 20
    assert (k["next_trigger"], k["next_stop"]) == (pytest.approx(132), pytest.approx(130))
    t.stop, t.target, st["target_moves"] = 210.0, 220.0, 2     # past the first target
    k = paper.lock_view(t, {**DEFAULTS, "fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False})
    assert k["steps"] == 11 and k["target"] == 220 and k["target_moves"] == 2 and k["first_target"] == 200
    assert k["locked_after_fees"] == pytest.approx(110 - 210 * 0.0005)
    short = SimpleNamespace(side="supply", fill_price=100.0, stop=80.0, target=0.0, quantity=2.0, fees=0.0,
                            trail={**st, "first_stop": 110.0, "first_target": 0.0, "target_moves": 0})
    k = paper.lock_view(short, NO_FEES)
    assert k["locked_usdt"] == 40 and k["steps"] == 2 and k["next_trigger"] == pytest.approx(68)
    off = SimpleNamespace(side="demand", fill_price=100.0, stop=90.0, target=120.0, quantity=1.0, fees=0.0, trail=None)
    assert paper.lock_view(off, NO_FEES)["method"] == "off" and "next_trigger" not in paper.lock_view(off, NO_FEES)
