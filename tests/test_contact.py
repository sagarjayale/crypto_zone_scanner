"""HTF contact: inside, overlap, touch and near against higher-timeframe zones."""
from types import SimpleNamespace

from app.config import DEFAULTS
from app.engine.contact import contacts, relate

TH = dict(DEFAULTS, marking_rule="pdf", zone_history="all")
PRICE = 812.0


def zone(tf, prox, dist, side="demand", status="fresh", zid=1):
    return SimpleNamespace(id=zid, timeframe=tf, side=side, proximal=float(prox), distal=float(dist), status=status)


def test_the_four_relations():
    setup = zone("1h", 790, 786)
    r = relate(setup, zone("2h", 791, 784), PRICE, TH)
    assert (r["relation"], r["low"], r["high"]) == ("inside", 784.0, 791.0)
    r = relate(setup, zone("4h", 788, 780), PRICE, TH)
    assert (r["relation"], r["low"], r["high"]) == ("overlap", 786.0, 788.0)
    r = relate(setup, zone("1d", 786, 772), PRICE, TH)
    assert (r["relation"], r["at"], r["where"]) == ("touch", 786.0, "below")
    r = relate(setup, zone("1w", 782, 762), PRICE, TH)
    assert (r["relation"], r["gap"], r["pct"], r["where"]) == ("near", 4.0, 0.49, "below")
    assert relate(setup, zone("1mo", 775, 750), PRICE, TH) is None                 # 11 points away: more than 1%
    assert relate(setup, zone("1d", 785.4, 772), PRICE, TH)["relation"] == "touch"   # 0.6 points: within 0.1% of price
    assert relate(zone("30m", 819, 822, "supply"), zone("4h", 824, 829, "supply"), PRICE, TH)["where"] == "above"


def test_one_entry_per_higher_timeframe_nearest_first():
    setup = zone("1h", 790, 786)
    pool = [zone("1y", 740, 690, zid=1), zone("1mo", 775, 750, zid=2), zone("1w", 782, 762, zid=3), zone("1d", 786, 772, zid=4),
            zone("1d", 789, 780, zid=5),                       # the same timeframe twice: the stronger contact wins
            zone("4h", 788, 780, zid=6), zone("2h", 791, 784, zid=7),
            zone("2h", 790, 786, "supply", zid=8),             # other side
            zone("4h", 789, 785, status="consumed", zid=9),    # used up
            zone("30m", 790, 787, zid=10), zone("1h", 791, 785, zid=11)]      # not higher than the setup's own timeframe
    got = contacts(setup, pool, PRICE, TH)
    assert [(c["tf"], c["relation"], c["zone_id"]) for c in got] == [("2h", "inside", 7), ("4h", "overlap", 6), ("1d", "overlap", 5), ("1w", "near", 3)]


def test_htf_zone_is_checked_only_above_its_own_timeframe():
    month = zone("1mo", 775, 750)
    pool = [zone("1y", 744, 690, zid=1), zone("6mo", 750, 715, zid=2), zone("3mo", 778, 745, zid=3), zone("1w", 772, 758, zid=4), month]
    got = contacts(month, pool, PRICE, TH, above_tf="1mo")
    assert [(c["tf"], c["relation"]) for c in got] == [("3mo", "inside"), ("6mo", "touch"), ("1y", "near")]
    day = zone("1d", 786, 772)
    got = contacts(day, pool, PRICE, TH)
    assert [(c["tf"], c["relation"]) for c in got] == [("1w", "touch"), ("1mo", "overlap"), ("3mo", "overlap")]
