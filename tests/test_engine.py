"""GTF zone rules on hand-made candles: classification, detection, the trade score, freshness and the three entry types."""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.config import DEFAULTS
from app.engine import gtf, lifecycle
from app.engine.detect import BASE, LEG_DOWN, LEG_UP, NONE, classify, detect_at
from app.engine.core import TimeframeEngine
from app.engine.score import entry_kind, htf_score, ltf_score

TH = dict(DEFAULTS, marking_rule="pdf", zone_history="all")      # these tests cover the course (PDF) marking
CANDLES = [  # open, high, low, close
    (540, 542, 518, 520), (520, 524, 514, 518), (518, 523, 515, 521), (521, 546, 520, 544), (544, 560, 542, 558),
    (558, 568, 555, 565), (565, 566, 548, 550), (550, 552, 528, 530), (530, 534, 519, 532),
]


def arrays(rows):
    a = np.array(rows, dtype=float)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3]


def found_at(rows, i, **over):
    th = dict(TH, **over)
    o, h, l, c = arrays(rows)
    return detect_at(i, o, h, l, c, classify(o, h, l, c, th), th)


def marked(rows, **over):
    """Pattern, proximal line, distal line and the candle the distal line came from, for the zone the last candle completes."""
    found, reason = found_at(rows, len(rows) - 1, **over)
    return (found["pattern"], found["proximal"], found["distal"], found["distal_from"]) if found else reason


def as_zone(found, **over):
    z = SimpleNamespace(id=1, side=found["side"], pattern=found["pattern"], proximal=found["proximal"], distal=found["distal"],
                        base_count=found["base_count"], has_gap=found["has_gap"], legout_body=found["legout_body"],
                        legout_close=found["legout_close"], legin_explosive=found["legin_explosive"], touches=0, status="fresh",
                        in_visit=False, legout_count=1, inside_armed=False, inside_ts=None, inside_close=None,
                        t2_entry=None, t2_ts=None, t3_entry=None, t3_ts=None)
    z.__dict__.update(over)
    return z


def test_candle_types():
    o, h, l, c = arrays(CANDLES[:5] + [(558, 568, 558, 563), (563, 573, 563, 563)])
    assert list(classify(o, h, l, c, TH)) == [LEG_DOWN, BASE, BASE, LEG_UP, LEG_UP, NONE, BASE]   # exactly 50% is neither; a doji is a base


def test_demand_zone_from_the_base_candles():
    found, reason = found_at(CANDLES, 3)
    assert reason is None
    assert (found["side"], found["pattern"], found["proximal"], found["distal"], found["base_count"]) == ("demand", "DBR", 521.0, 514.0, 2)
    assert found["has_gap"] is False and found["legin_explosive"] is True and round(found["legout_body"], 3) == 0.885
    assert found_at(CANDLES, 4) == (None, None)              # a second explosive candle has no base behind it
    assert gtf.ltf_ok(as_zone(found))                        # drop-base-rally with an explosive leg-in: usable for execution


def test_trade_score_as_the_course_gives_it():
    z = as_zone(found_at(CANDLES, 3)[0], legout_count=2)      # CANDLES[4] is a second exciting candle
    assert htf_score(z, TH) == (7.0, "TYPE_1")               # fresh 3 + two exciting candles 2 + two base candles 2
    assert ltf_score(z, (518.0, 600.0), TH) == (8.0, "TYPE_1")        # the 20 EMA runs through the zone
    assert ltf_score(z, (600.0, 515.0), TH) == (8.0, "TYPE_1")        # so does the 50 EMA: either one earns the point
    assert ltf_score(z, (540.0, 600.0), TH) == (7.0, "TYPE_1") and ltf_score(z, None, TH)[0] == 7.0 and ltf_score(z, 518.0, TH)[0] == 8.0
    one = as_zone(found_at(CANDLES, 3)[0])                   # only one exciting candle and no gap: strength 1
    assert htf_score(one, TH) == (6.0, "TYPE_2_3")
    assert htf_score(as_zone(found_at(CANDLES, 3)[0], has_gap=True), TH)[0] == 7.0      # a gap scores full strength
    assert htf_score(as_zone(found_at(CANDLES, 3)[0], legout_body=0.99), TH)[0] == 6.0  # a big body alone no longer does
    z.touches = 1
    assert ltf_score(z, None, TH) == (5.5, "TYPE_2_3")       # tested once: freshness 1.5
    z.in_visit = True                                        # ... but while that first test is still going on, it is fresh
    assert ltf_score(z, None, TH) == (7.0, "TYPE_1")
    z.touches, z.in_visit = 2, False
    assert ltf_score(z, 518.0, TH) == (0.0, "REJECT") and htf_score(z, TH) == (4.0, "WEAK")      # tested twice: freshness 0
    weak = as_zone(found_at(CANDLES, 3)[0], base_count=5)
    assert htf_score(weak, TH) == (5.0, "TYPE_2_3")          # one exciting candle 1, five base candles 1
    assert htf_score(as_zone(found_at(CANDLES, 3)[0], base_count=6, legout_count=2), TH)[0] == 5.0   # more than five: 0


def test_the_score_picks_the_entry_type():
    assert [entry_kind(x, TH) for x in (8, 7, 6.5, 6, 5, 4.5, 0)] == ["TYPE_1", "TYPE_1", "TYPE_3", "TYPE_3", "TYPE_3", None, None]
    assert entry_kind(6, {**TH, "entry_23": "TYPE_2"}) == "TYPE_2" and entry_kind(7, {**TH, "entry_23": "TYPE_2"}) == "TYPE_1"


def run_candles(z, rows, start=1):
    events = []
    for n, (o, h, l, c) in enumerate(rows, start):
        events += lifecycle.update(z, o, h, l, c, n, rule="pdf")
    return [e[0] for e in events]


def fresh():
    return as_zone(found_at(CANDLES, 3)[0])                  # demand 521 to 514


def test_entry_type_1_a_limit_at_the_proximal_line():
    z = fresh()
    plans = gtf.entry_plans(z, TH)
    assert (plans["type1"]["entry"], plans["type1"]["stoploss"], plans["type1"]["target"], plans["type1"]["triggered"]) == (521.0, 514.0, 535.0, False)
    assert plans["type2"]["entry"] is None and plans["type3"]["entry"] is None and not plans["type2"]["triggered"]
    assert run_candles(z, CANDLES[4:]) == ["touched"]                       # 530/534/519/532 wicks into the zone and closes above it
    assert (z.status, z.touches, z.in_visit) == ("tested", 1, True)
    assert gtf.entry_plans(z, TH)["type1"]["triggered"] is True
    assert run_candles(z, [(533, 540, 531, 538)], start=10) == [] and z.in_visit is False      # the test is over
    assert gtf.entry_plans(z, TH)["type1"]["triggered"] is False             # the next opportunity is the next test


def test_entry_type_2_closes_inside_then_opens_inside():
    z = fresh()
    assert run_candles(z, [(530, 531, 516, 517)]) == ["touched", "closed_inside"]       # the first candle closes inside the zone
    assert z.status == "tested" and z.inside_armed and gtf.entry_plans(z, TH)["type2"]["triggered"] is False
    assert "has closed inside" in gtf.entry_plans(z, TH)["type2"]["note"]
    assert run_candles(z, [(518.5, 520, 516, 519)], start=2) == ["entry2"]              # the next opens inside: entry at that open
    two = gtf.entry_plans(z, TH)["type2"]
    assert (two["entry"], two["stoploss"], two["target"], two["triggered"]) == (518.5, 514.0, 527.5, True)
    assert gtf.entry_plans(z, TH)["type3"]["triggered"] is False             # it has not left the zone yet
    assert run_candles(z, [(519, 524, 518, 523)], start=3) == ["entry3"]                 # and now it closes back above: type 3
    three = gtf.entry_plans(z, TH)["type3"]
    assert (three["entry"], three["stoploss"], three["target"]) == (523.0, 514.0, 541.0)
    assert z.touches == 1                                                    # the whole stay in the zone is one test


def test_entry_type_3_closes_inside_then_leaves_the_zone():
    z = fresh()
    run_candles(z, [(530, 531, 516, 518)])
    assert run_candles(z, [(522, 526, 521.5, 525)], start=2) == ["entry3"]              # gaps up and closes above: type 3, never type 2
    assert z.t2_entry is None and z.t3_entry == 525.0 and z.in_visit is True
    assert run_candles(z, [(526, 530, 524, 529)], start=3) == [] and z.in_visit is False
    # the next candle must be the very next one: a close inside, a candle that neither opens inside nor leaves, then nothing
    z = fresh()
    run_candles(z, [(530, 531, 516, 518)])
    assert run_candles(z, [(522, 523, 519, 520.5)], start=2) == []          # opens above the zone and closes inside again
    assert z.inside_armed and run_candles(z, [(520, 526, 519, 525)], start=3) == ["entry2", "entry3"]


def test_tested_twice_uses_the_zone_up():
    z = fresh()
    assert run_candles(z, [(530, 531, 520, 529)]) == ["touched"]            # first test
    assert run_candles(z, [(529, 535, 527, 534)], start=2) == [] and z.status == "tested" and not z.in_visit
    assert ltf_score(z, None, TH) == (4.5, "REJECT") and htf_score(z, TH)[0] == 4.5     # 1.5 + 1 + 2: no longer tradeable
    assert run_candles(z, [(534, 535, 519, 528)], start=3) == ["touched"] and z.touches == 2   # second test
    assert run_candles(z, [(528, 533, 526, 532)], start=4) == ["consumed"] and z.status == "consumed"
    assert run_candles(z, [(532, 533, 519, 530)], start=5) == []            # nothing more happens to it
    loose = fresh()
    for n, row in enumerate([(530, 531, 520, 529), (529, 535, 527, 534), (534, 535, 519, 528), (528, 533, 526, 532)]):
        lifecycle.update(loose, *row, n, max_tests=2)
    assert loose.status == "tested"                                          # with two earlier tests allowed it lives on


def test_violated_and_a_new_test_starts_clean():
    z = fresh()
    assert run_candles(z, [(530, 531, 510, 512)]) == ["violated"] and z.status == "violated"       # closes beyond the distal line
    assert run_candles(z, [(530, 531, 519, 530)]) == []
    z = fresh()
    run_candles(z, [(530, 531, 516, 517)])
    assert run_candles(z, [(517, 518, 509, 511)], start=2) == ["entry2", "violated"]    # opened inside, then closed through the zone
    assert z.t2_entry == 517.0 and z.status == "violated"
    z = fresh()                                                              # the entries of one test do not carry over to the next
    run_candles(z, [(530, 531, 516, 518), (518, 524, 517, 523), (523, 530, 522, 529)])
    assert z.t2_entry == 518.0 and z.t3_entry == 523.0 and not z.in_visit
    assert gtf.entry_plans(z, TH)["type2"]["triggered"] is False
    assert run_candles(z, [(529, 530, 520, 526)], start=4) == ["touched"] and z.t2_entry is None and z.t3_entry is None


def test_two_exciting_candles_are_found_by_the_engine():
    df = pd.DataFrame(CANDLES, columns=["open", "high", "low", "close"])
    df["ts"] = pd.date_range("2026-01-01", periods=len(df), freq="D")
    eng = TimeframeEngine(df, "1d", TH)
    seen = [(i, e["type"]) for i in range(len(eng)) for e in eng.step(i) if e["type"] != "rejected"]
    assert (3, "created") in seen and (4, "strength") in seen and (8, "touched") in seen
    z = eng.active[0]
    assert (z.proximal, z.distal, z.legout_count, z.touches, z.in_visit) == (521.0, 514.0, 2, 1, True)
    assert ltf_score(z, eng.emas(8), TH)[0] >= 7 and len(eng.emas(8)) == 2
    single = pd.DataFrame(CANDLES[:4] + [(544, 547, 541, 545)], columns=["open", "high", "low", "close"])    # a small candle follows
    single["ts"] = pd.date_range("2026-01-01", periods=len(single), freq="D")
    eng = TimeframeEngine(single, "1d", TH)
    for i in range(len(eng)):
        eng.step(i)
    assert eng.active[0].legout_count == 1 and htf_score(eng.active[0], TH)[0] == 6.0


def test_leg_out_must_close_beyond_the_whole_leg_in():
    # Course page 24: the example marked wrong. The leg-out closes at 115, above the leg-in close but below its high of 121.
    wrong = [(120, 121, 99, 100), (101, 102.5, 99.5, 100.5), (101, 116, 100, 115)]
    assert marked(wrong) == "leg-out did not close above the leg-in high"
    assert marked(wrong, marking_rule="scanner") == ("DBR", 101.0, 99.5, "base")           # scanner.py took it
    assert marked(wrong[:2] + [(101, 126, 100, 125)]) == ("DBR", 101.0, 99.0, "leg-in")
    assert marked(wrong[:2] + [(101, 121.5, 100, 121)]) == "leg-out did not close above the leg-in high"   # at the high is not above it
    supply = [(100, 121, 99, 120), (120, 122, 118, 119.5), (119, 120, 104, 105)]
    assert marked(supply) == "leg-out did not close below the leg-in low"
    assert marked(supply[:2] + [(119, 120, 94, 95)]) == ("RBD", 119.5, 122.0, "base")


def test_distal_line_takes_the_leg_in_or_leg_out_wick_when_it_is_beyond_the_base():
    # Course pages 18 to 20, the exceptional marking. The proximal line never moves.
    assert marked([(120, 121, 94, 100), (99, 101, 97, 100), (100, 126, 99.5, 125)]) == ("DBR", 100.0, 94.0, "leg-in")
    assert marked([(120, 121, 98, 100), (99, 101, 97, 100), (100, 126, 95, 125)]) == ("DBR", 100.0, 95.0, "leg-out")
    assert marked([(120, 121, 98, 100), (99, 101, 97, 100), (100, 126, 99.5, 125)]) == ("DBR", 100.0, 97.0, "base")
    # Continuation patterns: price came from beyond the zone, so the leg-in wick is not used; the leg-out wick is.
    assert marked([(80, 100.5, 79, 100), (100, 102, 98, 100.4), (100.4, 112, 99, 111)]) == ("RBR", 100.4, 98.0, "base")
    assert marked([(80, 100.5, 79, 100), (100, 102, 98, 100.4), (100.4, 112, 96, 111)]) == ("RBR", 100.4, 96.0, "leg-out")
    assert marked([(100, 131, 99, 120), (120, 123, 119, 121), (120, 120.5, 90, 91)]) == ("RBD", 120.0, 131.0, "leg-in")
    assert marked([(140, 141, 119, 120), (120, 123, 119, 121), (120, 124.5, 100, 101)]) == ("DBD", 120.0, 124.5, "leg-out")
    for rows in ([(120, 121, 94, 100), (99, 101, 97, 100), (100, 126, 99.5, 125)], [(100, 131, 99, 120), (120, 123, 119, 121), (120, 120.5, 90, 91)]):
        assert marked(rows, marking_rule="scanner")[3] == "base"                           # scanner.py: base candles only


def test_leg_in_must_be_an_exciting_candle():
    long_base = [(540, 542, 518, 520)] + [(520, 523, 516, 519)] * 8 + [(521, 546, 520, 544)]
    assert marked(long_base) == "more base candles than allowed"
    found, _ = found_at(long_base, 9, marking_rule="scanner")                             # scanner.py: 8 base candles leave a base of 1,
    assert found["base_count"] == 1 and found["legin_explosive"] is False and not gtf.ltf_ok(as_zone(found))   # with a base candle as leg-in
    six = [(540, 542, 518, 520)] + [(520, 523, 516, 519)] * 6 + [(521, 546, 520, 544)]
    assert marked(six) == ("DBR", 520.0, 516.0, "base") and found_at(six, 7)[0]["base_count"] == 6
    half = [(540, 550, 530, 530), (530, 533, 527, 531), (531, 556, 530, 555)]             # the candle before the base has a body of exactly 50%
    assert marked(half) == "the candle before the base is not an exciting candle"


def test_a_gap_with_a_small_candle_works_like_an_exciting_candle():
    # Course page 25. The candle opens above the base high and holds the gap at its close.
    rows = [(106, 107, 99, 100), (100, 102, 98, 100.5), (109, 111, 107.5, 109.5)]
    assert marked(rows) == ("DBR", 100.5, 98.0, "base") and found_at(rows, 2)[0]["has_gap"] is True
    assert marked(rows, marking_rule="scanner") is None and marked(rows, gap_exciting_pct=0) is None
    assert marked(rows[:2] + [(102.1, 111, 102.05, 102.6)]) is None                        # a gap of 0.1% is not significant
    assert marked(rows[:2] + [(109, 125, 95, 101.9)]) is None                             # the gap was filled by the close
    assert marked([(94, 101, 93, 100), (100, 102, 98, 99.5), (91, 92.5, 89, 90.5)]) == ("RBD", 99.5, 102.0, "base")
    # A gap candle can also be the leg-in: it gapped down into the base, so the pattern is a drop-base-rally.
    assert marked([(100, 100.5, 99, 99.8), (95, 96, 94, 95.3), (95.3, 96.2, 94.5, 95.6), (95.6, 102, 95.4, 101.5)]) == ("DBR", 95.6, 94.0, "leg-in")


def test_continuation_pattern_marks_location_but_not_execution():
    rows = [(480, 506, 478, 505), (505, 509, 501, 506), (506, 532, 505, 530)]
    found, _ = found_at(rows, 2)
    assert (found["pattern"], found["proximal"], found["distal"]) == ("RBR", 506.0, 501.0)
    assert not gtf.ltf_ok(as_zone(found))


def test_supply_mirror_and_gap():
    rows = [(500, 522, 498, 520), (520, 526, 517, 522), (521, 522, 495, 497)]
    found, _ = found_at(rows, 2)
    assert (found["side"], found["pattern"], found["proximal"], found["distal"]) == ("supply", "RBD", 520.0, 526.0)
    assert gtf.ltf_ok(as_zone(found)) and found["has_gap"] is False
    rows[2] = (515, 516, 495, 497)                    # opens below the previous low
    assert found_at(rows, 2)[0]["has_gap"] is True
    z = as_zone(found)
    plans = gtf.entry_plans(z, TH)
    assert (plans["type1"]["entry"], plans["type1"]["stoploss"], plans["type1"]["target"]) == (520.0, 526.0, 508.0)
    assert run_candles(z, [(510, 524, 509, 523)]) == ["touched", "closed_inside"]           # closes inside the supply zone
    assert run_candles(z, [(522, 523, 512, 514)], start=2) == ["entry2", "entry3"]          # opens inside, closes back below it
    plans = gtf.entry_plans(z, TH)
    assert (plans["type2"]["entry"], plans["type2"]["target"]) == (522.0, 514.0) and (plans["type3"]["entry"], plans["type3"]["target"]) == (514.0, 490.0)
