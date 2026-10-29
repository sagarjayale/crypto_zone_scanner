"""Fees, funding, the ITF trend rule for crypto, and prices kept to 8 significant digits."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.config import DEFAULTS
from app.engine import gtf
from app.sim import costs

T0 = datetime(2026, 10, 18, 8, 0)


def test_fee_rates_and_which_side_of_the_book():
    assert costs.fee_rates(DEFAULTS) == (pytest.approx(0.0002), pytest.approx(0.0005))
    assert costs.fee_rates({**DEFAULTS, "fee_bnb": True}) == (pytest.approx(0.00018), pytest.approx(0.00045))
    assert costs.entry_is_maker("TYPE_1") and not costs.entry_is_maker("TYPE_3") and not costs.entry_is_maker("TYPE_2")
    assert costs.exit_is_maker("target") and not costs.exit_is_maker("stop") and not costs.exit_is_maker("manual") and not costs.exit_is_maker("open")


def test_result_in_r_after_fees():
    win = costs.result_r("demand", 100.0, 102.0, 1.0, "TYPE_1", "target", DEFAULTS)
    assert win["gross_r"] == 2.0 and win["fees_r"] == pytest.approx(-(100 * 0.0002 + 102 * 0.0002))
    assert win["r"] == pytest.approx(2.0 - 0.0404)
    loss = costs.result_r("supply", 100.0, 101.0, 1.0, "TYPE_3", "stop", DEFAULTS)
    assert loss["r"] == pytest.approx(-1.0 - (100 * 0.0005 + 101 * 0.0005))
    thin = costs.result_r("demand", 100.0, 100.2, 0.1, "TYPE_1", "target", DEFAULTS)       # a zone 0.1% wide
    assert thin["r"] == pytest.approx(2.0 - 0.4004)                                         # fees take a fifth of the reward


def test_funding_between_entry_and_exit():
    f = costs.Funding([(T0, 0.0001, 100.0), (T0 + timedelta(hours=8), -0.0003, 110.0), (T0 + timedelta(hours=16), 0.0002, None)])
    assert f.per_unit("demand", T0, T0 + timedelta(hours=8), 100.0) == pytest.approx(0.0003 * 110)        # only settlements after the entry
    assert f.per_unit("supply", T0 - timedelta(minutes=1), T0 + timedelta(hours=8), 100.0) == pytest.approx(0.0001 * 100 - 0.0003 * 110)
    assert f.per_unit("demand", T0 + timedelta(hours=9), T0 + timedelta(hours=20), 105.0) == pytest.approx(-0.0002 * 105)  # no mark: the entry price
    assert f.per_unit("demand", T0, T0, 100.0) == 0.0 and costs.Funding([]).per_unit("demand", T0, T0 + timedelta(days=1), 1.0) == 0.0


def test_itf_trend_atr_rule_scales_with_how_much_a_coin_moves():
    rng = np.random.default_rng(3)
    calm = 100 + np.cumsum(rng.normal(0.02, 0.05, 80))        # drifts up slowly, small candles
    wild = 100 + np.cumsum(rng.normal(0.02, 2.0, 80))         # the same drift lost in large candles
    th = dict(DEFAULTS)
    assert gtf.itf_trend(calm, th)["trend"] == "UP"
    assert gtf.itf_trend(wild, th)["trend"] in ("SIDEWAYS", "UP", "DOWN") and "ATR rule" in gtf.itf_trend(wild, th)["reason"]
    pct = dict(th, itf_rule="pct", itf_slope_pct=0.05)
    assert gtf.itf_trend(calm, pct)["trend"] == gtf.itf_trend(calm)["trend"] == "UP"           # without th: scanner.py's 0.05%
    flat = np.full(80, 50.0)
    assert gtf.itf_trend(flat, th)["trend"] == "SIDEWAYS"


def test_prices_keep_eight_significant_digits():
    assert gtf.px(67012.345678) == 67012.346 and gtf.px(0.0000123456789) == 0.000012345679 and gtf.px(1.5) == 1.5
    z = type("Z", (), {"side": "demand", "proximal": 0.00001234, "distal": 0.00001198, "in_visit": False, "inside_armed": False,
                       "t2_entry": None, "t3_entry": None, "t2_ts": None, "t3_ts": None})()
    one = gtf.entry_plans(z, {"target_rr": 2.0})["type1"]
    assert (one["entry"], one["stoploss"], one["target"]) == (0.00001234, 0.00001198, 0.00001306)    # 2 decimals would make all three 0.0
