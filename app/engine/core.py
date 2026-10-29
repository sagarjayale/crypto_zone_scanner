"""The engine itself: one instrument, one timeframe, fed one closed candle at a time.
It holds no database code, so live scanning, replay and backtest all drive this same class."""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from . import lifecycle
from .detect import LEG_DOWN, LEG_UP, classify, detect_at


def ema_series(close: np.ndarray, period: int) -> np.ndarray:
    """EMA seeded with the first close, as the GTF scanner computes it."""
    out = np.empty(len(close))
    if len(close) == 0:
        return out
    k, out[0] = 2.0 / (period + 1), close[0]
    for i in range(1, len(close)):
        out[i] = close[i] * k + out[i - 1] * (1 - k)
    return out


class TimeframeEngine:
    def __init__(self, df: pd.DataFrame, tf: str, th: dict, active: list | None = None, make_zone=None):
        self.tf, self.th = tf, th
        self.ts = [t.to_pydatetime() for t in df["ts"]]
        self.o, self.h, self.l, self.c = (df[x].to_numpy(dtype=float) for x in ("open", "high", "low", "close"))
        self.kinds = classify(self.o, self.h, self.l, self.c, th)
        self.ema = ema_series(self.c, int(th["ema_period"]))
        self.ema2 = ema_series(self.c, int(th.get("ema_period_2") or 50))
        self.active = list(active or [])
        self.closed: list = []          # violated and used-up zones, kept for replay and backtest
        self._make = make_zone or self._make_local
        self._next_id = 1

    def __len__(self) -> int:
        return len(self.ts)

    def _make_local(self, fields: dict):
        z = SimpleNamespace(id=self._next_id, timeframe=self.tf, superseded_by=None, parent_zone_id=None, score=0.0, label=None, **fields)
        self._next_id += 1
        return z

    def emas(self, i: int) -> tuple[float, float]:
        """The two EMA values at candle i: either one running through a zone earns the EMA point."""
        return float(self.ema[i]), float(self.ema2[i])

    def step(self, i: int) -> list[dict]:
        """Process closed candle i. Returns events: created, strength, touched, closed_inside, entry2, entry3,
        consumed, violated, and rejected (a zone candidate that failed a rule)."""
        o, h, l, c, ts = self.o[i], self.h[i], self.l[i], self.c[i], self.ts[i]
        events: list[dict] = []
        prev_ts, kind, max_tests = (self.ts[i - 1] if i else None), self.kinds[i], int(self.th["max_wick_tests"])
        for z in list(self.active):
            if z.created_ts >= ts:
                continue
            # A second exciting candle straight after the leg-out: price left the zone with two exciting candles.
            if z.created_ts == prev_ts and (z.legout_count or 1) < 2 and kind == (LEG_UP if z.side == "demand" else LEG_DOWN):
                z.legout_count = 2
                events.append({"type": "strength", "zone": z, "ts": ts, "price": float(c), "old": "1", "new": "2"})
            for etype, price, old, new in lifecycle.update(z, o, h, l, c, ts, max_tests, "pdf" if self.th.get("marking_rule") == "pdf" else "scanner"):
                z.updated_ts = ts
                events.append({"type": etype, "zone": z, "ts": ts, "price": float(price), "old": old, "new": new})
                if etype in ("violated", "consumed"):
                    self.active.remove(z)
                    self.closed.append(z)
        found, reason = detect_at(i, self.o, self.h, self.l, self.c, self.kinds, self.th)
        if reason:
            events.append({"type": "rejected", "zone": None, "ts": ts, "price": float(c), "old": None, "new": reason})
        if not found:
            return events
        z = self._make({
            "side": found["side"], "pattern": found["pattern"], "proximal": found["proximal"], "distal": found["distal"],
            "base_start_ts": self.ts[found["base_start"]], "base_count": found["base_count"], "created_ts": ts,
            "atr": 0.0, "peak_distance": 0.0, "touches": 0, "in_visit": False, "status": "fresh",
            "has_gap": found["has_gap"], "legout_body": found["legout_body"], "legout_close": found["legout_close"],
            "legin_explosive": found["legin_explosive"], "distal_from": found["distal_from"], "legout_count": 1, "inside_armed": False, "inside_ts": None,
            "inside_close": None, "t2_entry": None, "t2_ts": None, "t3_entry": None, "t3_ts": None, "updated_ts": ts})
        events.append({"type": "created", "zone": z, "ts": ts, "price": float(c), "old": None, "new": found["pattern"]})
        self.active.append(z)
        return events
