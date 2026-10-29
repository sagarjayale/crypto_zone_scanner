"""GTF PDF filters for a strong execution zone: strong base (p.22), credibility (p.35-36) and the higher timeframe
caution (p.30). All three are off by default and only decide which zones are traded."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import DEFAULTS
from app.engine import gtf
from app.main import app

T0 = datetime(2026, 10, 1)
TH = {**DEFAULTS, "marking_rule": "scanner"}


def zone(side="demand", proximal=100.0, distal=98.0, bases=2, gap=False, body=0.5, count=1, hours=10, status="fresh"):
    return SimpleNamespace(side=side, proximal=proximal, distal=distal, base_count=bases, has_gap=gap, legout_body=body,
                           legout_count=count, created_ts=T0 + timedelta(hours=hours), status=status, touches=0, in_visit=False,
                           pattern="DBR" if side == "demand" else "RBD", legin_explosive=True, id=hours)


def test_all_three_are_off_by_default():
    assert DEFAULTS["max_bases"] == 0 and DEFAULTS["skip_reaction_zones"] is False and DEFAULTS["check_next_htf"] is False
    assert gtf.too_small(zone(bases=5), TH) is None


def test_next_htf_is_one_timeframe_above_the_mode_htf():
    assert gtf.next_htf("4HR") == "1d" and gtf.next_htf("DAILY") == "1w" and gtf.next_htf("15M") == "30m"
    assert gtf.next_htf("YEARLY") is None


def test_strong_base_allows_more_bases_only_with_a_strong_leg_out():
    th = {**TH, "max_bases": 2}
    assert gtf.too_small(zone(bases=2), th) is None
    assert "3 base candles" in gtf.too_small(zone(bases=3), th)
    assert gtf.too_small(zone(bases=3, gap=True), th) is None                 # a gap: strong leg-out
    assert gtf.too_small(zone(bases=4, body=0.8), th) is None                 # scanner.py marking: body of 70% or more
    pdf = {**TH, "marking_rule": "pdf", "max_bases": 2}
    assert gtf.too_small(zone(bases=4, count=2), pdf) is None                 # PDF marking: two exciting candles
    assert gtf.too_small(zone(bases=4, count=1, body=0.9), pdf) is not None


def test_a_zone_formed_in_the_reaction_of_an_earlier_zone_is_not_authentic():
    dz1 = zone(proximal=100, distal=98, hours=1)
    inside = zone(proximal=101, distal=99.5, hours=20)          # its base sits on DZ1: a reaction of DZ1
    above = zone(proximal=104, distal=102, hours=20)            # a higher zone that never came back to DZ1
    assert gtf.reaction_of(inside, [dz1, inside, above], TH) is dz1
    assert gtf.reaction_of(above, [dz1, inside, above], TH) is None
    assert gtf.reaction_of(dz1, [dz1, inside], TH) is None                    # the earlier zone itself is authentic
    good = zone(proximal=101, distal=99.5, hours=20, gap=True)                 # good closing: tradeable all the same
    assert gtf.reaction_of(good, [dz1, good], TH) is None
    supply1, supply2 = zone("supply", 100, 102, hours=1), zone("supply", 99, 100.5, hours=5)
    assert gtf.reaction_of(supply2, [supply1, supply2], TH) is supply1


def test_the_opposite_zone_one_timeframe_up_between_entry_and_target_blocks_the_trade():
    z = zone(proximal=100, distal=98)                         # long: entry 100, stop 98, target 104
    assert gtf.blocked_above(z, [zone("supply", 102, 103)], TH) is not None
    assert gtf.blocked_above(z, [zone("supply", 105, 106)], TH) is None       # beyond the target
    assert gtf.blocked_above(z, [zone("demand", 102, 101)], TH) is None       # same side: no block
    s = zone("supply", 100, 102)                              # short: entry 100, stop 102, target 96
    assert gtf.blocked_above(s, [zone("demand", 97, 95)], TH) is not None
    assert gtf.blocked_above(s, [zone("demand", 95, 94)], TH) is None


def test_candidates_skip_and_say_why():
    hz = zone(proximal=95, distal=93, hours=0)
    ctx = {"htf_zone": hz, "side": "BUY"}
    dz1, dz2 = zone(proximal=100, distal=98, hours=1), zone(proximal=101, distal=99, hours=20)
    got = lambda th, c=ctx: (gtf.candidates([dz1, dz2], c, 110.0, None, th, s := []), s)
    found, skipped = got(TH)
    assert {z.id for z, _t, _l in found} == {1, 20}
    found, skipped = got({**TH, "skip_reaction_zones": True})
    assert [z.id for z, _t, _l in found] == [1] and "non-authentic" in skipped[0][1]
    blocked = {**ctx, "above": [zone("supply", 102.5, 104)], "above_tf": "1d"}
    found, skipped = got({**TH, "check_next_htf": True}, blocked)
    assert found == [] and len(skipped) == 2 and "1d supply zone 102.5-104" in skipped[0][1]   # both targets reach it
    far = {**ctx, "above": [zone("supply", 104.5, 106)], "above_tf": "1d"}
    found, _ = got({**TH, "check_next_htf": True}, far)
    assert [z.id for z, _t, _l in found] == [1]                 # dz2 (target 105) reaches the zone at 104.5, dz1 (target 104) does not
    found, _ = got({**TH, "check_next_htf": False}, blocked)
    assert len(found) == 2                                     # off: the higher zone is ignored


def test_the_settings_take_the_three_filters():
    with TestClient(app) as c:
        assert c.put("/api/settings", json={"max_bases": 2.5}).status_code == 400
        assert c.put("/api/settings", json={"check_next_htf": "yes"}).status_code == 400
        out = c.put("/api/settings", json={"max_bases": 2, "skip_reaction_zones": True, "check_next_htf": True}).json()
        assert (out["max_bases"], out["skip_reaction_zones"], out["check_next_htf"]) == (2, True, True)
        c.put("/api/settings", json={"max_bases": 0, "skip_reaction_zones": False, "check_next_htf": False})
