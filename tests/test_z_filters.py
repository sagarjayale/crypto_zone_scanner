"""The minimum stop distance and minimum base candles settings: off (0) changes nothing; on, they only decide which LTF
zones are traded. How zones are found and scored is not touched. Also the fees-as-R estimate."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULTS
from app.engine import gtf
from app.main import app
from app.models import Zone


def zone(proximal, distal, bases=2):
    return Zone(instrument_key="FILTERUSDT", timeframe="5m", side="demand", pattern="DBR", proximal=proximal, distal=distal,
                base_start_ts=datetime(2026, 10, 1), base_count=bases, created_ts=datetime(2026, 10, 1, 1), atr=0.0, peak_distance=0.0,
                touches=0, in_visit=False, status="fresh", score=7.0, label="TYPE_1", has_gap=False, legout_body=0.9,
                legout_close=proximal + 5, legin_explosive=True)


THIN, WIDE = zone(521.0, 520.9), zone(515.0, 512.0, bases=1)        # stop 0.019% and 0.58% away
CTX = {"side": "BUY", "htf_zone": SimpleNamespace(side="demand", proximal=500.0, distal=490.0)}


def pick(**settings):
    th, skipped = {**DEFAULTS, **settings}, []
    return [z for z, _, _ in gtf.candidates([THIN, WIDE], CTX, 530.0, (None, None), th, skipped)], [why for _, why in skipped]


def test_off_by_default_changes_nothing():
    assert DEFAULTS["min_stop_pct"] == 0 and DEFAULTS["min_bases"] == 0
    found, skipped = pick()
    assert set(map(id, found)) == {id(THIN), id(WIDE)} and skipped == []


def test_a_thin_zone_is_skipped_and_the_next_one_is_traded():
    found, skipped = pick(min_stop_pct=0.2)
    assert found == [WIDE] and skipped == ["zone too thin: stop 0.0192% away, minimum 0.2%"]
    assert round(gtf.stop_pct(WIDE), 3) == 0.583
    found, skipped = pick(min_stop_pct=1.0)
    assert found == [] and len(skipped) == 2                        # nothing left to trade: the reason says why


def test_minimum_base_candles():
    found, skipped = pick(min_bases=2)
    assert found == [THIN] and skipped == ["1 base candle, minimum 2"]
    assert pick(min_bases=2, min_stop_pct=0.2) == ([], ["zone too thin: stop 0.0192% away, minimum 0.2%", "1 base candle, minimum 2"])


def test_fees_in_r():
    th = {**DEFAULTS, "fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False, "target_rr": 2}
    f = gtf.fees_r(100.0, 99.0, th)                                  # 1% stop, limit entry
    assert f == {"loss": pytest.approx(0.07, abs=0.005), "win": pytest.approx(0.04, abs=0.005)}
    thin = gtf.fees_r(100.0, 99.95, th)                              # 0.05% stop: fees are most of the risk
    assert thin["loss"] == pytest.approx(1.4, abs=0.01)
    assert gtf.fees_r(100.0, 99.0, th, maker_entry=False)["loss"] == pytest.approx(0.1, abs=0.005)
    assert gtf.fees_r(100.0, 100.0, th) == {"loss": None, "win": None}


def test_the_settings_are_checked_and_saved():
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"min_stop_pct": -1}).status_code == 400
        assert c.put("/api/settings", json={"min_bases": 1.5}).status_code == 400
        out = c.put("/api/settings", json={"min_stop_pct": 0.2, "min_bases": 2}).json()
        assert (out["min_stop_pct"], out["min_bases"]) == (0.2, 2)
        out = c.put("/api/settings", json={"min_stop_pct": 0, "min_bases": 0}).json()
        assert (out["min_stop_pct"], out["min_bases"]) == (0, 0)
