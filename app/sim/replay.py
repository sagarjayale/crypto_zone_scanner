"""Replay: feeds stored candles one at a time into a separate, in-memory copy of the engine.
Nothing is written to the live tables, and no Binance call is made."""
import calendar
import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from .. import settings_store
from ..data.sync import load_candles
from ..engine import gtf
from ..engine.core import TimeframeEngine
from ..engine.lifecycle import counted_tests
from ..engine.score import htf_score, ltf_score

SESSIONS: dict[str, "ReplaySession"] = {}
HISTORY_BARS = 300


def epoch(ts: datetime) -> int:
    """A UTC candle time as an epoch. The chart shifts it to the display time zone."""
    return calendar.timegm(ts.timetuple())


class ReplaySession:
    def __init__(self, db: Session, key: str, tf: str, start: datetime):
        self.key, self.tf = key, tf
        self.th = settings_store.thresholds(db, tf)
        df = load_candles(db, key, tf)
        if df.empty:
            raise ValueError("No candles stored for this instrument and timeframe. Sync it first.")
        self.eng = TimeframeEngine(df, tf, self.th)
        later = [n for n, t in enumerate(self.eng.ts) if t >= start]
        if not later:
            raise ValueError(f"No candles on or after {start}. The last stored candle is {self.eng.ts[-1]}.")
        self.i = later[0]
        for k in range(self.i):   # build the zones that existed at the start date, silently
            self.eng.step(k)
        self.id = uuid.uuid4().hex[:12]
        SESSIONS[self.id] = self

    @property
    def remaining(self) -> int:
        return len(self.eng) - self.i

    def candle(self, i: int) -> dict:
        e = self.eng
        return {"time": epoch(e.ts[i]), "open": e.o[i], "high": e.h[i], "low": e.l[i], "close": e.c[i]}

    def zone_view(self, z) -> dict:
        emas = self.eng.emas(min(self.i, len(self.eng) - 1))
        usable = gtf.ltf_ok(z) and counted_tests(z, self.th) <= self.th["max_wick_tests"]
        total, label = ltf_score(z, emas, self.th) if usable else htf_score(z, self.th)
        conf = gtf.entry_plans(z, self.th)["type1"]        # the zone's own lines: entry at the proximal, stop at the distal
        return {"id": z.id, "side": z.side, "pattern": z.pattern, "proximal": z.proximal, "distal": z.distal,
                "status": z.status, "touches": z.touches, "base_count": z.base_count, "has_gap": bool(z.has_gap),
                "created": epoch(z.created_ts), "ended": epoch(z.updated_ts) if z.status in ("violated", "consumed") else None,
                "score": total, "label": label, "execution": gtf.ltf_ok(z),
                "entry": conf["entry"], "stop": conf["stoploss"], "target": conf["target"], "reward_risk": conf["rr"],
                "target_source": "fixed"}

    def init_payload(self) -> dict:
        first = max(0, self.i - HISTORY_BARS)
        return {"type": "init", "timeframe": self.tf, "remaining": self.remaining,
                "candles": [self.candle(k) for k in range(first, self.i)],
                "zones": [self.zone_view(z) for z in self.eng.active]}

    def next_bar(self) -> dict | None:
        if self.i >= len(self.eng):
            return None
        events = [e for e in self.eng.step(self.i) if e["type"] not in ("rejected", "strength", "closed_inside")]
        out = {"type": "bar", "candle": self.candle(self.i), "remaining": self.remaining - 1,
               "events": [{"type": e["type"], "zone_id": e["zone"].id, "side": e["zone"].side,
                           "price": e["price"], "time": epoch(e["ts"]), "detail": e["new"]} for e in events],
               "zones": [self.zone_view(z) for z in self.eng.active],
               "closed": [self.zone_view(e["zone"]) for e in events if e["type"] in ("violated", "consumed")]}
        self.i += 1
        return out
