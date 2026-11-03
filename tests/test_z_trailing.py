"""The trailing stop loss: off changes nothing; each method on hand-made candles; the backtest and paper trading use it
the same way; and the zone facts every backtest trade records. The file name sorts after test_z_paper and
test_z_backtest on purpose: it reuses their data and helpers."""
from datetime import datetime, timedelta

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
FEES = {**DEFAULTS, "fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False}


def trail(method, side="demand", entry=100.0, stop=99.0, th=NO_FEES, prior=(), **opts):
    return trailing.Trailer(side, entry, stop, th, trailing.options({"trail_method": method, **opts}), prior)


def test_off_by_default_and_off_never_moves():
    assert DEFAULTS["trail_method"] == "off" and DEFAULTS["trail_keep_target"] is True and DEFAULTS["trail_tf"] == "ltf"
    t = trail("off")
    for bar in ((101.5, 100.2, 101.2), (103.0, 101.0, 102.8)):
        assert t.close_bar(*bar) is None
    assert t.stop == 99.0 and t.moves == 0 and not t.active


def test_step_moves_half_an_r_at_a_time_and_only_tightens():
    t = trail("step")
    assert t.close_bar(100.8, 100.1, 100.6) is None                     # +0.8 R: not started
    assert t.close_bar(101.1, 100.4, 100.9) == 100.0                    # +1 R: break-even
    assert t.close_bar(101.6, 100.8, 101.4) == 100.5                    # +1.5 R: +0.5 R
    assert t.close_bar(102.1, 101.2, 101.9) == 101.0                    # +2 R: +1 R
    assert t.close_bar(101.5, 100.3, 100.5) is None and t.stop == 101.0  # price falls back: the stop stays
    assert t.moves == 3 and t.best_r == pytest.approx(2.1)


def test_a_short_is_the_mirror_image():
    t = trail("step", side="supply", entry=100.0, stop=101.0)
    assert t.close_bar(99.8, 98.9, 99.1) == 100.0                       # +1.1 R for a short: break-even
    assert t.close_bar(99.2, 97.9, 98.2) == 99.0                        # +2.1 R: stop at +1 R, below the entry


def test_break_even_includes_the_fees():
    t = trail("step", th=FEES)
    assert t.close_bar(101.0, 100.2, 100.9) == pytest.approx(100.07)    # entry x (1 + 0.02% + 0.05%)
    assert "break-even" in t.why


def test_swing_goes_under_each_new_higher_low():
    t = trail("swing", trail_swing=2)
    bars = [(101.2, 100.9, 101.0), (101.4, 100.8, 101.2), (101.3, 100.6, 100.9),   # a low of 100.6 ...
            (101.8, 100.9, 101.6), (102.2, 101.3, 102.0)]                          # ... confirmed two candles later
    stops = [t.close_bar(*b) for b in bars]
    assert stops[0] == 100.0                                            # +1.2 R: break-even first
    pad = 0.1 * sum(h - l for h, l, _ in t.bars[-14:]) / len(t.bars[-14:])
    assert stops[4] == pytest.approx(100.6 - pad) and t.stop > 100.0


def test_atr_keeps_a_distance_behind_the_best_price():
    prior = [(100.5, 99.5, 100.0)] * 15                                 # ATR of 1
    t = trail("atr", prior=prior, trail_atr_mult=1.5)
    assert t.close_bar(101.2, 100.2, 101.0) == 100.0                    # best 101.2 - 1.5 x ATR is below break-even
    t.close_bar(103.0, 102.0, 102.8)
    assert t.stop == pytest.approx(103.0 - 1.5 * t._atr())


def test_start_later_and_remove_the_target():
    t = trail("step", trail_start_r=1.5, trail_keep_target=False)
    assert t.close_bar(101.2, 100.4, 101.0) is None and not t.target_removed
    assert t.close_bar(101.6, 100.9, 101.5) == 100.5 and t.target_removed


def test_the_backtest_is_unchanged_with_trailing_off_and_records_the_zone_facts():
    with SessionLocal() as db:
        plain = run_one(db, KEY, MODE, START, END, OPTS)
        off = run_one(db, KEY, MODE, START, END, {**OPTS, "trail_method": "off"})
    assert plain == off and plain
    assert {t["exit_reason"] for t in plain} <= {"target", "stop", "open"} and all(t["stop_moves"] == 0 for t in plain)
    for t in plain:
        assert t["final_stop"] == t["stop"] and t["base_count"] >= 1 and t["zone_age"] >= 1 and t["departure"] >= 0
        assert t["room_ltf_r"] is None or t["room_ltf_r"] > 0
        assert t["approach"] is None or t["approach"] >= 0


def test_the_backtest_with_a_step_trailing_stop():
    with SessionLocal() as db:
        plain = {(t["instrument_key"], t["entry_ts"], t["zone_created_ts"]): t for t in run_one(db, KEY, MODE, START, END, OPTS)}
        stepped = run_one(db, KEY, MODE, START, END, {**OPTS, "trail_method": "step"})
    assert {(t["instrument_key"], t["entry_ts"], t["zone_created_ts"]) for t in stepped} == set(plain)   # same entries
    trailed = [t for t in stepped if t["exit_reason"] == "trail"]
    assert trailed, "the test data has no trade that went +1 R and came back"
    for t in trailed:
        # never worse than the same trade without trailing, unless that one went on to its target
        p = plain[(t["instrument_key"], t["entry_ts"], t["zone_created_ts"])]
        assert t["stop_moves"] >= 1 and (t["result_r"] >= p["result_r"] - 1e-9 or p["result"] == "win")
        assert t["mfe_r"] >= 1.0 and (t["final_stop"] - t["entry"]) * (1 if t["side"] == "demand" else -1) > 0
    for t in stepped:                                                   # a trade that never reached +1 R ends as before
        if t["mfe_r"] < 0.99 and t["result"] != "open":
            p = plain[(t["instrument_key"], t["entry_ts"], t["zone_created_ts"])]
            assert (t["result"], t["result_r"]) == (p["result"], p["result_r"])


def test_paper_positions_trail_on_closed_candles_and_exit_as_trailing_stop():
    from tests.test_z_live import LIVE_KEY
    from tests.test_z_paper import NOW, fresh_setup
    zid = fresh_setup()                                                 # long: entry 521, stop 512 (R = 9), target 557
    with SessionLocal() as db:
        settings_store.put(db, "trail_method", "step")
        try:
            paper.place(db, zid, NOW)
            paper.on_prices(db, {LIVE_KEY: 521.0}, NOW)
            t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
            assert t.trail["trail_method"] == "step" and t.trail["first_stop"] == 512.0 and t.trail["tf"] == "1h"
            hour = NOW.replace(minute=0) + timedelta(hours=1)
            data_sync.upsert_candles(db, LIVE_KEY, "1h", [
                {"ts": hour, "open": 521, "high": 525, "low": 520, "close": 524, "volume": 1, "oi": None},       # +0.4 R
                {"ts": hour + timedelta(hours=1), "open": 524, "high": 535, "low": 523, "close": 534, "volume": 1, "oi": None}])  # +1.6 R
            moved = paper.update_trailing(db, t, hour + timedelta(hours=2))
            db.commit()
            assert [e["event"] for e in moved] == ["stop moved"] and t.stop == pytest.approx(521 + 0.5 * 9)
            assert t.history[-1]["field"] == "stop" and "trailing stop (step)" in t.history[-1]["note"]
            assert paper.update_trailing(db, t, hour + timedelta(hours=2)) == []      # nothing new: the stop stays
            out = paper.on_prices(db, {LIVE_KEY: 525.0}, hour + timedelta(hours=2, minutes=5))
            db.refresh(t)
            assert [e["event"] for e in out] == ["stopped"] and t.exit_reason == "trail" and t.result == "win"
        finally:
            settings_store.put(db, "trail_method", "off")


def test_the_settings_and_a_backtest_run_take_the_trailing_options():
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"trail_method": "fast"}).status_code == 400
        assert c.put("/api/settings", json={"trail_tf": "htf"}).status_code == 400
        assert c.put("/api/settings", json={"trail_swing": 9}).status_code == 400
        out = c.put("/api/settings", json={"trail_method": "atr", "trail_atr_mult": 3, "trail_tf": "itf"}).json()
        assert (out["trail_method"], out["trail_atr_mult"], out["trail_tf"]) == ("atr", 3, "itf")
        c.put("/api/settings", json={"trail_method": "off", "trail_atr_mult": 2, "trail_tf": "ltf"})
        run = c.post("/api/backtest/run", json={"symbols": [KEY], "modes": [MODE], "start": "2026-10-01", "end": "2026-10-02",
                                                "trail_method": "swing", "trail_swing": 3}).json()["run_id"]
        settings = c.get(f"/api/backtest/{run}").json()["settings"]
        assert (settings["trail_method"], settings["trail_swing"], settings["trail_tf"]) == ("swing", 3, "ltf")
        assert c.post("/api/backtest/run", json={"symbols": [KEY], "modes": [MODE], "start": "2026-10-01", "end": "2026-10-02",
                                                 "trail_method": "nope"}).status_code == 400


def test_one_minute_trailing_in_the_backtest():
    """Trailing on 1-minute candles: inside each LTF candle the exit is checked minute by minute and each minute's close
    moves the stop. Where no 1-minute candles are stored the LTF candle stands in, and the trade says so."""
    from app.data.sync import load_candles
    with SessionLocal() as db:
        minutes = load_candles(db, KEY, "1m", last=0)
        if minutes.empty or minutes["ts"].max() - minutes["ts"].min() < timedelta(days=2):          # the test candles end on 18 Oct; syncs dated today keep only 14 days of 1-minute candles
            import pytest
            pytest.skip("the 1-minute test candles have aged out against today's date")
        first_minute = minutes["ts"].min().to_pydatetime()
        since = first_minute.replace(hour=0, minute=0) + timedelta(days=1)
        plain = run_one(db, KEY, MODE, since, END, OPTS)
        on_ltf = run_one(db, KEY, MODE, since, END, {**OPTS, "trail_method": "step"})
        on_1m = run_one(db, KEY, MODE, since, END, {**OPTS, "trail_method": "step", "trail_tf": "1m"})
        wide = run_one(db, KEY, MODE, START, END, {**OPTS, "trail_method": "step", "trail_tf": "1m"})
    assert plain and len(on_1m) == len(on_ltf) == len(plain)                       # the same entries
    used = {t.get("trail_tf_used") for t in on_1m if t["result"] != "open"}
    assert "1m" in used
    assert {t.get("trail_tf_used") for t in wide if t["entry_ts"] < first_minute - timedelta(days=1)} <= {"ltf", None}
    minute_exits = [t for t in on_1m if t["exit_ts"] and t["exit_ts"].minute % 5]
    assert minute_exits, "1-minute trailing should exit inside a 5-minute candle at least once"
    for t in on_1m:
        if t["exit_reason"] == "trail":
            assert t["stop_moves"] >= 1 and t["mfe_r"] >= 1.0


def test_one_minute_trailing_for_paper_positions():
    from tests.test_z_live import LIVE_KEY
    from tests.test_z_paper import NOW, fresh_setup
    zid = fresh_setup()
    with SessionLocal() as db:
        settings_store.put(db, "trail_method", "step")
        settings_store.put(db, "trail_tf", "1m")
        try:
            paper.place(db, zid, NOW)
            paper.on_prices(db, {LIVE_KEY: 521.0}, NOW)
            t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
            assert (t.trail["trail_tf"], t.trail["tf"]) == ("1m", "1m")
        finally:
            settings_store.put(db, "trail_method", "off")
            settings_store.put(db, "trail_tf", "ltf")
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"trail_tf": "1m"}).json()["trail_tf"] == "1m"
        c.put("/api/settings", json={"trail_tf": "ltf"})


def test_a_full_download_can_be_limited_to_one_timeframe(monkeypatch):
    calls = []

    async def fake_prepare(client, key, timeframes=None, full=False, force=True, **kw):
        calls.append((key, timeframes, full, force))
    monkeypatch.setattr("app.jobs.scheduler.prepare", fake_prepare)
    with TestClient(app) as c:
        assert c.post("/api/data/backfill", json={"symbols": [KEY], "timeframes": ["2m"]}).status_code == 400
        job = c.post("/api/data/backfill", json={"symbols": [KEY], "timeframes": ["1m"]}).json()["job_id"]
        for _ in range(50):
            if c.get(f"/api/jobs/{job}").json()["status"] != "running":
                break
            import time
            time.sleep(0.1)
    assert calls == [(KEY, ["1m"], True, False)]
