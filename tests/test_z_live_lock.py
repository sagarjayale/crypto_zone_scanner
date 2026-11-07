"""Lock trailing on the live price, the trailing stop reading only new candles, the 5 minute fallback, the lighter
per-second order check, and the price feed that never waits for the checks."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import live
from app.sim import paper, trailing

TH = {"maker_fee_pct": 0.0, "taker_fee_pct": 0.0, "bnb_discount": False}


def trade(**kw):
    st = {**trailing.options({"trail_method": "lock", "trail_lock_pct": 10, "trail_lock_gap_pct": 10, "trail_lock_target": "move"}),
          "tf": "1m", "first_stop": 0.02363, "first_target": 0.02477, "moves": 0, "no_target": True, "target_moves": 0}
    t = SimpleNamespace(id=1, instrument_key="XUSDT", side="demand", fill_price=0.02401, stop=0.02363, target=0.02477,
                        quantity=1000.0, fees=0.0, exit_ts=None, checked_ts=None, timeframe="5m", history=[], trail=st,
                        fill_ts=datetime(2026, 11, 5, 15, 10), leverage=2, margin=10, liq_price=0.012, entry_type="TYPE_1", zone_id=None)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


class DB:
    def get(self, model, key):          # no 1 minute candles stored: the safety net runs on 5 minute ones
        return None


def test_the_live_price_locks_at_once_and_only_tightens():
    t = trade()
    now = datetime(2026, 11, 5, 15, 13, 40)
    # your example: 0.02401, step 0.000076, gap 0.0000076. 0.02425 is past step 3's trigger 0.0242456
    out = paper.live_lock(DB(), t, 0.02425, now, TH)
    assert [e["event"] for e in out] == ["stop moved"]
    assert t.stop == pytest.approx(0.02401 + 3 * 0.000076) and t.trail["best"] == 0.02425
    assert "live price" in t.history[-1]["note"]
    assert t.checked_ts == datetime(2026, 11, 5, 15, 10)        # 5 minute safety net: from the candle after this one
    assert paper.live_lock(DB(), t, 0.02415, now, TH) == []      # price falls back: nothing moves back
    assert t.stop == pytest.approx(0.02401 + 3 * 0.000076)
    assert paper.live_lock(DB(), t, 0.02425, now, TH) == []      # the same best again: no work


def test_live_lock_ignores_other_methods_and_losing_prices():
    t = trade()
    assert paper.live_lock(DB(), t, 0.02390, datetime(2026, 11, 5, 15, 11), TH) == []    # below the entry
    t2 = trade(trail={**trade().trail, "trail_method": "atr"})
    assert paper.live_lock(DB(), t2, 0.03, datetime(2026, 11, 5, 15, 11), TH) == []


def test_the_feed_does_not_wait_for_the_checks(monkeypatch):
    seen = []

    async def slow(client, prices=None, now=None):
        await asyncio.sleep(0.2)
        seen.append(prices)
    monkeypatch.setattr(live, "poll_once", slow)

    async def run():
        t0 = asyncio.get_running_loop().time()
        live._spawn(live._checks(None, {"A": 1.0}))
        took = asyncio.get_running_loop().time() - t0
        await asyncio.sleep(0.3)
        return took
    assert asyncio.run(run()) < 0.05 and seen == [{"A": 1.0}]
