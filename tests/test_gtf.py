"""GTF top-down logic: nearest HTF zones, the curve, both trends, the direction filter, market states and execution zones."""
from types import SimpleNamespace

import numpy as np

from app.config import DEFAULTS
from app.engine import gtf

TH = dict(DEFAULTS, marking_rule="pdf", zone_history="all")


def zone(side, prox, dist, **over):
    z = SimpleNamespace(id=over.pop("zid", 1), side=side, proximal=float(prox), distal=float(dist), status="fresh", touches=0,
                        base_count=2, has_gap=False, legout_body=0.8, legout_close=float(prox) + (10 if side == "demand" else -10),
                        legin_explosive=True, pattern="DBR" if side == "demand" else "RBD", in_visit=False, legout_count=2,
                        inside_armed=False, inside_ts=None, inside_close=None, t2_entry=None, t2_ts=None, t3_entry=None, t3_ts=None)
    z.__dict__.update(over)
    return z


HTF = [zone("demand", 500, 480, zid=1), zone("demand", 420, 400, zid=2), zone("supply", 620, 640, zid=3), zone("supply", 700, 720, zid=4)]
UP = np.linspace(400, 560, 80)          # a rising ITF: the 50 SMA slopes up
DOWN = UP[::-1].copy()
FLAT = np.full(80, 500.0)
QUIET = np.full(20, 550.0)              # HTF closes that breach neither zone


def test_nearest_zones_and_curve_bands():
    nd, ns = gtf.nearest_zones(HTF, 530.0, TH)
    assert (nd.id, ns.id) == (1, 3)
    assert gtf.nearest_zones(HTF, 490.0, TH)[0].id == 1          # inside the demand zone still counts (above its distal line)
    assert gtf.nearest_zones(HTF, 470.0, TH)[0].id == 2          # below the distal line: the next demand down
    assert gtf.nearest_zones([zone("demand", 500, 480, status="consumed")], 530.0, TH) == (None, None)
    band = lambda p: gtf.curve(p, nd, ns)
    assert [band(p) for p in (495, 500, 530, 540, 570, 580, 600, 620, 650)] == \
        ["VERY LOW", "VERY LOW", "LOW", "LOW", "EQUILIBRIUM", "EQUILIBRIUM", "HIGH", "VERY HIGH", "VERY HIGH"]
    assert gtf.curve(530.0, nd, None) == "UNKNOWN"


def test_trends():
    nd, ns = gtf.nearest_zones(HTF, 530.0, TH)
    assert gtf.htf_trend(QUIET, nd, ns) == "SIDEWAYS"
    assert gtf.htf_trend(np.array([550.0] * 18 + [625.0, 630.0]), nd, ns) == "UPTREND"       # two closes above the supply
    assert gtf.htf_trend(np.array([550.0] * 18 + [495.0, 490.0]), nd, ns) == "DOWNTREND"
    assert gtf.itf_trend(UP)["trend"] == "UP" and gtf.itf_trend(DOWN)["trend"] == "DOWN" and gtf.itf_trend(FLAT)["trend"] == "SIDEWAYS"
    assert gtf.itf_trend(np.linspace(400, 560, 30))["trend"] == "UP" and "23 SMA" in gtf.itf_trend(np.linspace(400, 560, 30))["reason"]
    assert gtf.itf_trend(np.linspace(400, 560, 10))["trend"] == "UP" and gtf.itf_trend(np.array([1.0, 2.0]))["trend"] == "SIDEWAYS"


BANDS = ("VERY LOW", "LOW", "EQUILIBRIUM", "HIGH", "VERY HIGH")


def test_location_beats_trend():
    """The decision table: demand side of the curve always buys, supply side always sells, the ITF decides only in equilibrium."""
    table = {itf: [gtf.direction_filter(b, itf) for b in BANDS] for itf in ("UP", "DOWN", "SIDEWAYS")}
    assert table == {"UP": ["BUY", "BUY", "BUY", "SELL", "SELL"], "DOWN": ["BUY", "BUY", "SELL", "SELL", "SELL"],
                     "SIDEWAYS": ["BUY", "BUY", "NONE", "SELL", "SELL"]}
    assert gtf.direction_filter("UNKNOWN", "UP") == "NONE"


def test_the_six_example_rows():
    """Demand 350 to 400 and supply 600 to 650, as in the decision table's worked example."""
    zones = [zone("demand", 400, 350, zid=1), zone("supply", 600, 650, zid=2)]
    flat = np.full(20, 500.0)
    rows = [(375, DOWN, "VERY LOW", "BUY"), (420, FLAT, "LOW", "BUY"), (500, UP, "EQUILIBRIUM", "BUY"),
            (500, FLAT, "EQUILIBRIUM", "NONE"), (580, UP, "HIGH", "SELL"), (620, UP, "VERY HIGH", "SELL")]
    for price, itf, band, side in rows:
        ctx = gtf.context(zones, flat, itf, float(price), TH)
        assert (ctx["curve"], ctx["side"]) == (band, side), price
        assert ctx["htf_zone"] is (None if side == "NONE" else zones[0] if side == "BUY" else zones[1])
    assert gtf.context(zones, flat, UP, 400.0, TH)["curve"] == "VERY LOW"          # exactly on the demand proximal
    assert gtf.context(zones, flat, UP, 466.0, TH)["curve"] == "LOW" and gtf.context(zones, flat, UP, 467.0, TH)["curve"] == "EQUILIBRIUM"


def test_the_earlier_rule_is_still_available():
    old = {itf: [gtf.direction_filter(b, itf, "trend") for b in BANDS] for itf in ("UP", "DOWN", "SIDEWAYS")}
    assert old == {"UP": ["BUY", "BUY", "BUY", "BUY", "NONE"], "DOWN": ["NONE", "SELL", "SELL", "SELL", "SELL"], "SIDEWAYS": ["NONE"] * 5}
    th = {**TH, "direction_rule": "trend"}
    assert gtf.context(HTF, QUIET, FLAT, 530.0, th)["side"] == "NONE" and gtf.context(HTF, QUIET, FLAT, 530.0, TH)["side"] == "BUY"


def test_context_normal_and_expansion():
    ctx = gtf.context(HTF, QUIET, UP, 530.0, TH)
    assert (ctx["market_state"], ctx["curve"], ctx["itf_trend"], ctx["side"], ctx["htf_zone"].id) == ("NORMAL", "LOW", "UP", "BUY", 1)
    ctx = gtf.context(HTF, QUIET, DOWN, 600.0, TH)
    assert (ctx["curve"], ctx["side"], ctx["htf_zone"].id) == ("HIGH", "SELL", 3)
    assert gtf.context(HTF, QUIET, FLAT, 530.0, TH)["side"] == "BUY"             # low on the curve: the ITF is not asked
    assert gtf.context(HTF, QUIET, FLAT, 560.0, TH)["side"] == "NONE"            # equilibrium with a sideways ITF
    assert gtf.context(HTF, QUIET, UP, 600.0, TH)["side"] == "SELL"              # high on the curve sells even with the ITF up
    only_demand = [z for z in HTF if z.side == "demand"]
    ctx = gtf.context(only_demand, QUIET, UP, 530.0, TH)        # no supply left above: bullish expansion
    assert (ctx["market_state"], ctx["curve"], ctx["side"]) == ("BULLISH_EXPANSION", "EQUILIBRIUM", "BUY")
    assert gtf.context(only_demand, QUIET, DOWN, 530.0, TH)["side"] == "NONE"
    only_supply = [z for z in HTF if z.side == "supply"]
    assert gtf.context(only_supply, QUIET, DOWN, 530.0, TH)["market_state"] == "BEARISH_EXPANSION"
    assert gtf.context([], QUIET, UP, 530.0, TH)["market_state"] == "NO_ZONES"


def test_execution_zones_sit_between_price_and_the_htf_zone():
    ctx = gtf.context(HTF, QUIET, UP, 530.0, TH)                 # BUY against the HTF demand 500 to 480
    ltf = [zone("demand", 521, 514, zid=10), zone("demand", 478.5, 470, zid=11),      # inside the 10% buffer below 480
           zone("demand", 470, 465, zid=12),                                          # too far below the HTF zone
           zone("demand", 535, 531, zid=13),                                          # above price
           zone("supply", 525, 528, zid=14),                                          # wrong side
           zone("demand", 510, 505, zid=15, pattern="RBR"),                           # continuation: not an execution zone
           zone("demand", 512, 508, zid=16, touches=2),                               # tested twice
           zone("demand", 518, 513, zid=17, touches=1, legout_count=1, base_count=5),
           zone("demand", 516, 511, zid=18, touches=1, in_visit=True, legout_count=1)]  # its first test is going on: still fresh
    got = gtf.candidates(ltf, ctx, 530.0, (519.0, 600.0), TH)
    assert [(z.id, s, lab) for z, s, lab in got] == [(10, 8.0, "TYPE_1"), (11, 7.0, "TYPE_1"), (18, 6.0, "TYPE_2_3"), (17, 3.5, "REJECT")]
    assert [z.id for z, _, _ in gtf.candidates(ltf, ctx, 530.0, (600.0, 519.0), TH)][0] == 10           # the 50 EMA earns the point too
    assert gtf.primary_plan(gtf.entry_plans(got[0][0], TH), 8.0, TH)["entry_type"] == "TYPE_1"
    assert gtf.primary_plan(gtf.entry_plans(got[2][0], TH), 6.0, TH)["entry_type"] == "TYPE_3"
    assert gtf.primary_plan(gtf.entry_plans(got[3][0], TH), 3.5, TH) is None
    assert gtf.candidates(ltf, gtf.context(HTF, QUIET, FLAT, 560.0, TH), 560.0, 519.0, TH) == []      # equilibrium, ITF sideways: no side
    far = gtf.candidates([zone("demand", 250, 245, zid=20)], {"side": "BUY", "htf_zone": zone("demand", 240, 200)}, 530.0, None, TH)
    assert far == []                                             # more than 50% away from price


def test_zone_states():
    z = zone("demand", 521, 514)
    assert gtf.zone_state(z, 0, 530, 546, 544, TH) == "JUST_FORMED"
    assert gtf.zone_state(z, 9, 519, 534, 520, TH) == "TOUCHING_NOW"
    assert gtf.zone_state(z, 9, 524, 529, 525, TH) == "APPROACHING"        # within 1% above the proximal line
    assert gtf.zone_state(z, 9, 540, 552, 550, TH) == "ACTIVE"
    s = zone("supply", 520, 526)
    assert gtf.zone_state(s, 9, 512, 517, 516, TH) == "APPROACHING" and gtf.zone_state(s, 9, 512, 522, 519, TH) == "TOUCHING_NOW"