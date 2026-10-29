"""Entry types 2 and 3 from the first candle to the paper fill, through the real scanner.
One stock in DAILY mode (HTF day, ITF 1 hr, LTF 30 min). The 30 minute candles are fed in three steps."""
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, select

from app import settings_store
from app.data.sync import upsert_candles
from app.db import SessionLocal
from app.engine import result, scanner
from app.models import PaperOrder, PaperTrade, Setting, TradePlan, Zone
from app.sim import paper, scancheck

T0 = datetime(2026, 6, 1, 9, 15)
NOW = datetime(2026, 6, 1, 15, 40)
ROWS = [  # open, high, low, close: 30 minute candles from 09:15
    (600, 602, 598, 601), (601, 603, 597, 599), (599, 600, 560, 562), (562, 563, 539, 540),      # the way down
    (540, 542, 518, 520), (520, 524, 514, 518), (518, 523, 515, 521), (521, 546, 520, 544),      # drop, two base candles, rally: demand 521 to 514
    (544, 547, 541, 545),                                                                         # a small candle: only one exciting candle left the zone
    (545, 546, 530, 532),                                                                         # 13:45, price heads back
    (532, 533, 516, 518),                                                                         # 14:15, closes inside the zone
    (519, 527, 518, 526),                                                                         # 14:45, opens inside (type 2 at 519) and closes back above (type 3 at 526)
    (526, 541, 525, 540),                                                                         # 15:15, leaves: the test is over
]


def feed(db, key, upto, rows=None):
    upsert_candles(db, key, "30m", [{"ts": T0 + timedelta(minutes=30 * n), "open": o, "high": h, "low": l, "close": c, "volume": 100, "oi": 0}
                                    for n, (o, h, l, c) in enumerate((rows or ROWS)[:upto])])
    db.commit()
    scanner.scan_timeframe(db, key, "30m", now=NOW)


def setup(db, key):
    flat = lambda tf, closes, step: [{"ts": T0 + step * n, "open": c - 1, "high": c + 2, "low": c - 2, "close": float(c), "volume": 1, "oi": 0} for n, c in enumerate(closes)]
    upsert_candles(db, key, "1d", flat("1d", [550.0] * 25, timedelta(days=1)))
    upsert_candles(db, key, "1h", flat("1h", np.linspace(400, 560, 80), timedelta(hours=1)))
    mk = lambda side, prox, dist, pat: Zone(instrument_key=key, timeframe="1d", side=side, pattern=pat, proximal=prox, distal=dist, base_start_ts=T0,
                                            base_count=2, created_ts=T0 - timedelta(days=30), atr=0.0, touches=0, in_visit=False, status="fresh", score=0,
                                            has_gap=False, legout_body=0.8, legout_count=2, legout_close=prox, legin_explosive=True)
    db.add_all([mk("demand", 500.0, 480.0, "RBR"), mk("supply", 620.0, 640.0, "DBD")])
    db.commit()


def tidy(db, key):
    """Paper orders and positions are kept in step by the five minute job; none may be left for the tests that follow."""
    db.execute(delete(PaperTrade).where(PaperTrade.instrument_key == key))
    db.execute(delete(PaperOrder).where(PaperOrder.instrument_key == key))
    db.commit()


def the_zone(db, key):
    return db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == "30m", Zone.side == "demand")).one()


def test_entry_type_3_from_waiting_to_the_paper_fill():
    key = "ENTRY3USDT"
    with SessionLocal() as db:
        setup(db, key)
        feed(db, key, 10)                                    # up to 13:45: the zone exists and price is on its way back
        z = the_zone(db, key)
        assert (z.proximal, z.distal, z.pattern, z.legout_count, z.touches, z.score, z.label) == (521.0, 514.0, "DBR", 1, 0, 6.0, "TYPE_2_3")
        p = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id)).one()
        assert (p.mode, p.entry_type, p.status, p.entry, p.stop, p.target) == ("DAILY", "TYPE_3", "pending", 521.0, 514.0, 535.0)
        r = result.build(db, key, "DAILY")
        sig = r["signal"]
        assert (sig["signal"], sig["score"], sig["primary_entry"], sig["waiting"], sig["entry"], sig["watch"], sig["stoploss"]) == ("BUY", 6.0, "TYPE_3", True, None, 521.0, 514.0)
        assert r["ltf_zone"]["strength_label"] == "one exciting candle" and r["can_paper"] is True
        order = paper.place(db, z.id, NOW, "DAILY")
        assert (order.entry_type, order.entry, order.status) == ("TYPE_3", 521.0, "pending")
        assert paper.on_prices(db, {key: 520.0}, NOW) == []          # price at the proximal line does not fill a type 3 order
        assert db.get(PaperOrder, order.id).status == "pending"

        feed(db, key, 11)                                    # 14:15 closes inside the zone: still waiting
        db.refresh(z); db.refresh(p)
        assert (z.touches, z.in_visit, z.inside_armed, z.status, z.score) == (1, True, True, "tested", 6.0)     # the test going on does not cost freshness
        assert p.status == "pending" and "has closed inside" in p.plans["type3"]["note"] and paper.on_candles(db, key) == []

        feed(db, key, 12)                                    # 14:45 opens inside and closes back above the zone
        db.refresh(z); db.refresh(p)
        assert (z.t2_entry, z.t3_entry, z.in_visit) == (519.0, 526.0, True)
        assert (p.status, p.entry, p.stop, p.target, p.version) == ("triggered", 526.0, 514.0, 550.0, 1)
        assert p.triggered_ts == datetime(2026, 6, 1, 15, 15)        # the close of the 14:45 candle
        sig = result.build(db, key, "DAILY")["signal"]
        assert (sig["primary_entry"], sig["waiting"], sig["entry"], sig["target"]) == ("TYPE_3", False, 526.0, 550.0)
        assert (sig["type2"]["entry"], sig["type2"]["target"], sig["type1"]["triggered"]) == (519.0, 529.0, True)
        events = paper.on_candles(db, key)
        assert [e["event"] for e in events] == ["filled"] and events[0]["price"] == 526.0
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == z.id)).one()
        assert (trade.fill_price, trade.stop, trade.target, trade.fill_ts) == (526.0, 514.0, 550.0, datetime(2026, 6, 1, 15, 15))
        assert abs(trade.quantity - 100 / 12) < 1e-9                         # 1% of 10,000 USDT over 12 USDT a coin to the stop
        assert db.get(PaperOrder, order.id).status == "filled"

        feed(db, key, 13)                                    # 15:15 leaves the zone: tested once, the score falls below 5
        db.refresh(z); db.refresh(p)
        assert (z.in_visit, z.touches, z.status, z.score) == (False, 1, "tested", 4.5)
        assert p.status == "triggered" and p.is_current                # the plan that was entered stays as the record
        out = result.build(db, key, "DAILY")["signal"]
        assert (out["signal"], out["reason"]) == ("NO_TRADE", "The trade score is below 5")
        # the scan-date check, asked on the same day, sees the same entries with only the candles up to its date
        chk = scancheck.check(db, key, "DAILY", T0.date() - timedelta(days=1))
        assert chk["signal"]["reason"].startswith("No 30m candle had closed")
        tidy(db, key)


def test_entry_type_2_fills_at_the_open_and_checks_that_candle():
    key = "ENTRY2USDT"
    with SessionLocal() as db:
        settings_store.put(db, "entry_23", "TYPE_2")
        try:
            setup(db, key)
            feed(db, key, 11)
            z = the_zone(db, key)
            p = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id)).one()
            assert (p.entry_type, p.status, p.entry) == ("TYPE_2", "pending", 521.0)
            order = paper.place(db, z.id, NOW, "DAILY")
            # 5 minute candles inside the 14:45 candle: the open at 519, then a run to the target of 529
            five = [(519, 521, 518, 520), (520, 530, 519, 529.5), (529.5, 531, 528, 530)]
            upsert_candles(db, key, "5m", [{"ts": datetime(2026, 6, 1, 14, 45) + timedelta(minutes=5 * n), "open": o, "high": h, "low": l, "close": c, "volume": 1, "oi": 0}
                                           for n, (o, h, l, c) in enumerate(five)])
            feed(db, key, 12)
            db.refresh(p)
            assert (p.status, p.entry, p.stop, p.target, p.triggered_ts) == ("triggered", 519.0, 514.0, 529.0, datetime(2026, 6, 1, 14, 45))
            events = paper.on_candles(db, key)               # booked once the candle has closed, then the candles since the open are checked
            assert [(e["event"], e["price"]) for e in events] == [("filled", 519.0), ("target hit", 529.0)]
            trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == z.id)).one()
            fees = 519 * 0.0005 + 529 * 0.0002                     # entered with a market order (taker), out at the target (maker)
            assert (trade.fill_price, trade.exit_price, trade.result, trade.quantity) == (519.0, 529.0, "win", 20.0)
            assert abs(trade.result_r - (2.0 - fees / 5)) < 1e-3
            assert db.get(PaperOrder, order.id).entry == 519.0
        finally:
            db.delete(db.get(Setting, "entry_23"))
            db.commit()
            tidy(db, key)


def test_a_score_of_7_takes_entry_type_1_and_a_second_test_gets_its_own_plan():
    key = "ENTRY1USDT"
    rows = list(ROWS)
    rows[8] = (544, 560, 543, 558)                           # a second exciting candle leaves the zone: strength 2, score 7
    with SessionLocal() as db:
        setup(db, key)
        feed(db, key, 9, rows)
        z = the_zone(db, key)
        p = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id)).one()
        assert (z.legout_count, z.score, z.label, p.entry_type, p.status, p.entry) == (2, 7.0, "TYPE_1", "TYPE_1", "pending", 521.0)
        rows = rows[:9] + [(558, 559, 530, 532), (532, 533, 520, 528), (528, 540, 527, 539)]       # back to the zone at 14:15, away at 14:45
        feed(db, key, 11, rows)
        db.refresh(z); db.refresh(p)
        assert (z.touches, z.in_visit, z.score) == (1, True, 7.0) and (p.status, p.entry, p.triggered_ts) == ("triggered", 521.0, datetime(2026, 6, 1, 14, 15))
        feed(db, key, 12, rows)                          # the test is over: tested once, 1.5 + 2 + 2 = 5.5, entry type 3 next time
        db.refresh(z); db.refresh(p)
        plans = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id).order_by(TradePlan.version)).all()
        assert (z.in_visit, z.score, z.label) == (False, 5.5, "TYPE_2_3")
        assert [(x.version, x.entry_type, x.status, x.is_current) for x in plans] == [(1, "TYPE_1", "triggered", False), (2, "TYPE_3", "pending", True)]
        order = paper.place(db, z.id, NOW, "DAILY")      # a tested zone can be paper traded on its second test
        assert order.entry_type == "TYPE_3"
        tidy(db, key)
