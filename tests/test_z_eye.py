"""GTF EYE and Premium pages: in zone, reacting, approaching per side, ATR %, and the /api/eye endpoint."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
from fastapi.testclient import TestClient

from app import settings_store
from app.db import SessionLocal
from app.engine import eye
from app.main import app
from tests.test_sync_scan import KEY


def zone(side, proximal, distal, score=7.0, zid=1, touches=0):
    return SimpleNamespace(id=zid, side=side, proximal=proximal, distal=distal, pattern="DBR" if side == "demand" else "RBD",
                           score=score, label="TYPE_1", status="fresh", touches=touches, created_ts=datetime(2026, 9, 1))


DEMAND = zone("demand", 100.0, 95.0, zid=1)
FAR = zone("demand", 80.0, 75.0, zid=2)
SUPPLY = zone("supply", 120.0, 125.0, zid=3)
CALM = [(110.0, 105.0, 108.0)] * 3                 # (high, low, close): none of them touched a zone


def test_in_zone_wins_over_everything():
    v = eye.side_view([DEMAND, FAR], "demand", 97.0, CALM, 1.0)
    assert v["state"] == "in_zone" and v["zone_id"] == 1 and v["distance_pct"] == 0.0


def test_approaching_is_within_the_approach_percent():
    assert eye.side_view([DEMAND], "demand", 100.8, CALM, 1.0)["state"] == "approaching"
    far = eye.side_view([DEMAND], "demand", 103.0, CALM, 1.0)
    assert far["state"] is None and round(far["distance_pct"], 2) == round(3 / 103 * 100, 2)
    sup = eye.side_view([SUPPLY], "supply", 119.2, CALM, 1.0)
    assert sup["state"] == "approaching" and sup["side"] == "supply"


def test_reacting_needs_a_recent_touch_and_price_back_outside():
    touched = [(108.0, 104.0, 106.0), (104.0, 99.5, 101.0), (106.0, 101.0, 105.0)]    # the middle candle dipped into the zone
    assert eye.side_view([DEMAND], "demand", 105.0, touched, 1.0)["state"] == "reacting"
    old = [(104.0, 99.5, 101.0)] + CALM                                                 # the touch is 4 candles back
    assert eye.side_view([DEMAND], "demand", 105.0, old[-eye.REACT_CANDLES:], 1.0)["state"] is None
    hit = [(121.0, 115.0, 117.0)] + CALM[:2]
    assert eye.side_view([SUPPLY], "supply", 112.0, hit, 1.0)["state"] == "reacting"


def test_no_zone_on_the_trading_side_gives_none():
    assert eye.side_view([DEMAND], "demand", 90.0, CALM, 1.0) is None          # price below the demand zone: not a demand view
    assert eye.side_view([], "supply", 90.0, CALM, 1.0) is None


def test_atr_percent_of_the_last_close():
    df = pd.DataFrame({"high": [11.0] * 16, "low": [9.0] * 16, "close": [10.0] * 16})
    assert eye.atr_pct(df) == 20.0
    assert eye.atr_pct(df.iloc[:1]) is None


def test_pages_and_endpoint():
    with SessionLocal() as db:
        before = settings_store.get(db, "universe")
        settings_store.put(db, "universe", [KEY])
    try:
        with TestClient(app) as c:
            for path, needle in (("/eye", "GTF EYE Coin Scanner"), ("/premium", "Premium Filters")):
                page = c.get(path)
                assert page.status_code == 200 and needle in page.text
            for js in ("eye.js", "premium.js", "eyecommon.js"):
                assert c.get(f"/static/js/{js}").status_code == 200
            eye._cache.clear()
            out = c.get("/api/eye", params={"mode": "daily"}).json()
            assert out["mode"] == "DAILY" and out["frames"][0] == "1d" and out["ranked"] == [KEY]
            for r in out["rows"]:
                assert r["instrument_key"] == KEY and r["price"] > 0
                for side in ("demand", "supply"):
                    z = r[side]
                    assert z is None or (z["side"] == side and z["state"] in (None, "in_zone", "reacting", "approaching"))
            assert c.get("/api/eye", params={"mode": "nope"}).status_code == 400
    finally:
        with SessionLocal() as db:
            settings_store.put(db, "universe", before)


def test_reacting_stops_once_price_is_far_and_worn_zones_are_left_out():
    touched = [(104.0, 99.5, 101.0)] + CALM[:2]
    assert eye.side_view([DEMAND], "demand", 109.0, touched, 1.0)["reaction_r"] == 1.8          # 9 above, zone 5 high
    assert eye.side_view([DEMAND], "demand", 112.0, touched, 1.0)["state"] is None              # more than 2 zone heights away
    worn = zone("demand", 100.0, 95.0, touches=3)
    assert eye.side_view([worn], "demand", 100.5, CALM, 1.0, max_tests=1) is None
    once = zone("demand", 100.0, 95.0, touches=1)
    assert eye.side_view([once], "demand", 100.5, CALM, 1.0, max_tests=1)["state"] == "approaching"
    just = zone("demand", 100.0, 95.0, touches=2)                                                 # 1 test before, then the reaction
    assert eye.side_view([just], "demand", 105.0, touched, 1.0, max_tests=1)["state"] == "reacting"


def test_the_zones_own_candles_are_not_a_reaction():
    t = datetime(2026, 9, 1)
    from datetime import timedelta
    own = [(104.0, 99.0, 103.0, t - timedelta(days=1)), (103.0, 99.5, 102.0, t), (110.0, 102.0, 109.0, t + timedelta(days=1))]
    assert eye.side_view([DEMAND], "demand", 105.0, own, 1.0)["state"] is None       # the dips were the base and the leg-out
    later = own[1:] + [(106.0, 99.8, 105.0, t + timedelta(days=2))]
    assert eye.side_view([DEMAND], "demand", 105.0, later, 1.0)["state"] == "reacting"


def test_intraday_modes_use_the_live_zone_window(monkeypatch):
    from app.engine import window
    seen = {}
    real = window.keep

    def spy(zones, since):
        seen["since"] = since
        return real(zones, since)
    monkeypatch.setattr(window, "keep", spy)
    with SessionLocal() as db:
        eye._cache.clear()
        eye.scan(db, [KEY], "15M", {}, 1.0, {"max_wick_tests": 1, "zone_history": "scanner"})
        assert seen["since"] is not None and (eye.now_utc() - seen["since"]).days == 30       # 15 min zones: the last 30 days
        eye._cache.clear()
        eye.scan(db, [KEY], "DAILY", {}, 1.0, {"max_wick_tests": 1, "zone_history": "scanner"})
        assert seen["since"] is None                                                            # daily and up: all zones
    assert eye.EYE_MODES[:5] == ("15M", "30M", "HOURLY", "2HR", "4HR")
