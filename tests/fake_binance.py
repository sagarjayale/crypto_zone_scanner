"""A fake Binance client for the tests. Candles are generated here only, deterministically from their open time, so a
candle downloaded twice is the same candle. The application itself never contains made-up data."""
import calendar
import math
from datetime import datetime, timedelta

import numpy as np

from app.binance.client import kline_weight

MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080}
LISTED = datetime(2024, 1, 3)        # the fake coin's listing date


def _ms(t: datetime) -> int:
    """A naive UTC time as Binance's milliseconds (the machine's own time zone plays no part)."""
    return calendar.timegm(t.timetuple()) * 1000 + t.microsecond // 1000


def _months(start: datetime, n: int) -> datetime:
    m = start.month - 1 + n
    return datetime(start.year + m // 12, m % 12 + 1, 1)


WAVES = [(40 * 1440, 0.0), (9 * 1440, 1.3), (2.3 * 1440, 2.1), (13 * 60, 0.7), (3.1 * 60, 2.9), (47, 1.9), (13, 0.4)]


def level(t: datetime, scale: float) -> float:
    """Price at a moment: waves from 13 minutes to 40 days long, each larger the longer it is (as a random walk scales),
    so price trends and turns on every timeframe, plus noise fixed by the moment itself."""
    x = (t - datetime(2020, 1, 1)).total_seconds() / 60
    rng = np.random.default_rng(int(x) % (2 ** 32))
    shock = rng.normal(0, 0.5 * scale) * (3 if rng.random() < 0.12 else 1)
    waves = sum(0.45 * math.sqrt(p / 5) * math.sin(x / p * 2 * math.pi / 6 + phase) for p, phase in WAVES)
    return 500 + waves + shock


def candle(t: datetime, interval: str) -> dict:
    nxt = _months(t, 1) if interval == "1M" else t + timedelta(minutes=MINUTES[interval])
    span = (nxt - t).total_seconds() / 60
    scale = 1.5 * math.sqrt(span / 5)
    o, c = level(t, scale), level(nxt, scale)
    rng = np.random.default_rng((_ms(t) // 1000 + int(span)) % (2 ** 32))
    hi, lo = max(o, c) + abs(rng.normal(0, 0.25 * scale)), min(o, c) - abs(rng.normal(0, 0.25 * scale))
    vol = int(1_000_000 * (1 + abs(c - o) / scale))
    return {"ts": t, "open": o, "high": hi, "low": lo, "close": c, "volume": vol, "oi": 0,
            "close_ms": _ms(nxt) - 1}


def align(t: datetime, interval: str) -> datetime:
    if interval == "1M":
        return datetime(t.year, t.month, 1) if t == datetime(t.year, t.month, 1) else _months(datetime(t.year, t.month, 1), 1)
    if interval == "1w":
        monday = datetime(t.year, t.month, t.day) - timedelta(days=t.weekday())
        return monday if monday >= t else monday + timedelta(days=7)
    step = MINUTES[interval]
    base = datetime(t.year, t.month, t.day)
    m = (t - base).total_seconds() / 60
    k = math.ceil(m / step)
    return base + timedelta(minutes=k * step)


class FakeBinance:
    def __init__(self, symbols=("BTCUSDT", "ETHUSDT", "TESTUSDT"), now: datetime | None = None, listed: datetime = LISTED):
        self.symbols, self.now, self.listed = list(symbols), now, listed
        self.calls: list[tuple] = []
        self.funding: dict[str, list[dict]] = {}
        self.prices: dict[str, float] = {}

    # ---- symbols ----
    async def exchange_info(self) -> dict:
        self.calls.append(("exchangeInfo",))
        sym = lambda s, vol: {"symbol": s, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING", "baseAsset": s[:-4],
                              "onboardDate": _ms(self.listed),
                              "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                                          {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                                          {"filterType": "MIN_NOTIONAL", "notional": "5"}]}
        out = [sym(s, 0) for s in self.symbols]
        out.append({**sym("OLDUSDT", 0), "status": "SETTLING"})                      # not trading: left out
        out.append({**sym("BTCUSDT_261225", 0), "contractType": "CURRENT_QUARTER"})   # not a perpetual: left out
        return {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400}], "symbols": out}

    async def funding_info(self) -> list[dict]:
        return [{"symbol": "TESTUSDT", "fundingIntervalHours": 4}]

    async def tickers_24h(self) -> list[dict]:
        return [{"symbol": s, "quoteVolume": str(1e9 / (n + 1))} for n, s in enumerate(self.symbols)]

    # ---- candles ----
    async def klines(self, symbol, interval, start=None, end=None, limit=1000):
        self.calls.append(("klines", symbol, interval, start, end, limit, kline_weight(limit)))
        t = align(max(start or self.listed, self.listed), interval)
        out = []
        stop = end or self.now or datetime(2100, 1, 1)
        while len(out) < limit and t <= stop and (self.now is None or t <= self.now):
            out.append(candle(t, interval))
            t = _months(t, 1) if interval == "1M" else t + timedelta(minutes=MINUTES[interval])
        return out

    # ---- prices, funding, open interest ----
    async def premium_index(self) -> list[dict]:
        return [{"symbol": s, "markPrice": str(self.prices.get(s, 500.0)), "lastFundingRate": "0.0001",
                 "nextFundingTime": _ms((self.now or datetime(2026, 1, 1)) + timedelta(hours=1))} for s in self.symbols]

    async def ltp(self, keys):
        return {k: self.prices[k] for k in keys if k in self.prices}

    async def funding_history(self, symbol, start=None, limit=1000):
        rows = [r for r in self.funding.get(symbol, []) if start is None or r["ts"] >= start]
        return rows[:limit]

    async def open_interest_hist(self, symbol, period, limit=500):
        end = self.now or datetime(2026, 1, 1)
        step = timedelta(minutes=5 if period == "5m" else 60)
        t0 = end.replace(minute=end.minute - end.minute % 5, second=0, microsecond=0) - step * (limit - 1)
        return [{"ts": t0 + step * k, "oi": 1000.0 + k, "value": (1000.0 + k) * 500} for k in range(limit)]

    def status(self) -> dict:
        return {"used_1m": 0, "limit_1m": 2400, "budget_1m": 1200, "share": 0.5, "banned": False, "banned_until": None, "last_429": None}

    async def close(self):
        pass
