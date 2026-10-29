"""Binance USDT-M futures REST client for public market data. No API key is used and no order is ever sent.

Rate limits. Binance counts request weight per IP per minute. The limit itself is read from exchangeInfo at the first
call (rateLimits, REQUEST_WEIGHT), not hard-coded. Every response carries the weight used so far this minute in the
X-MBX-USED-WEIGHT-1M header; the client keeps that number and waits for the next minute before a request would take
usage past the share of the limit set by WEIGHT_SHARE. HTTP 429 (too many requests) waits for Retry-After and tries
again. HTTP 418 (the IP is banned) stops every call until the ban ends and says so on the Data page.

The /futures/data endpoints (open interest history) are not counted in that weight; Binance limits them separately,
so they get their own small sliding-window limit."""
import asyncio
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from .. import config

WEIGHT_SHARE = 0.5          # stay at or under half of Binance's per-minute weight
DEFAULT_WEIGHT_LIMIT = 2400  # used only until exchangeInfo has told the real limit
KLINE_LIMIT = 1000          # candles per kline call: weight 5, the cheapest per candle
DATA_LIMIT = (300, 300)     # /futures/data calls: at most 300 per 300 seconds, well under Binance's limit


class BinanceError(Exception):
    pass


class BinanceBanned(BinanceError):
    pass


def kline_weight(limit: int) -> int:
    """Weight of one kline call, by its limit (Binance USDT-M futures)."""
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


def ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def from_ms(value) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).replace(tzinfo=None)


def parse_kline(row: list) -> dict:
    """[open time, open, high, low, close, volume, close time, quote volume, ...]. Volume is stored in USDT (quote volume)."""
    return {"ts": from_ms(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
            "volume": int(float(row[7] or 0)), "oi": 0, "close_ms": int(row[6])}


class Weight:
    """Request weight used this minute, as Binance reports it, and the wait before the next call."""

    def __init__(self):
        self.limit = DEFAULT_WEIGHT_LIMIT
        self.used = 0                 # last value of X-MBX-USED-WEIGHT-1M
        self.minute = None            # the minute that value belongs to
        self.planned = 0              # weight of calls sent this minute and not answered yet
        self.banned_until: float | None = None
        self.last_429: float | None = None
        self.lock = asyncio.Lock()
        self.data_calls: deque[float] = deque()

    def budget(self) -> int:
        return int(self.limit * WEIGHT_SHARE)

    def _now_minute(self) -> int:
        return int(time.time() // 60)

    async def take(self, weight: int) -> None:
        async with self.lock:
            while True:
                if self.banned_until and time.time() < self.banned_until:
                    raise BinanceBanned(f"Binance has banned this IP until {datetime.fromtimestamp(self.banned_until, timezone.utc):%Y-%m-%d %H:%M} UTC. Downloads are stopped until then.")
                m = self._now_minute()
                if self.minute != m:
                    self.minute, self.used, self.planned = m, 0, 0
                if self.used + self.planned + weight <= self.budget():
                    self.planned += weight
                    return
                await asyncio.sleep(max(0.2, 60 - time.time() % 60 + 0.5))      # wait for the next minute

    def settle(self, weight: int, header: str | None) -> None:
        if self._now_minute() == self.minute:
            self.planned = max(0, self.planned - weight)
            if header is not None:
                try:
                    self.used = max(self.used, int(header))
                except ValueError:
                    pass

    async def take_data(self) -> None:
        window, most = DATA_LIMIT
        while True:
            now = time.monotonic()
            while self.data_calls and now - self.data_calls[0] > window:
                self.data_calls.popleft()
            if len(self.data_calls) < most:
                self.data_calls.append(now)
                return
            await asyncio.sleep(window - (now - self.data_calls[0]) + 0.1)

    def status(self) -> dict:
        banned = bool(self.banned_until and time.time() < self.banned_until)
        return {"used_1m": self.used if self.minute == self._now_minute() else 0, "limit_1m": self.limit, "budget_1m": self.budget(),
                "share": WEIGHT_SHARE, "banned": banned,
                "banned_until": datetime.fromtimestamp(self.banned_until, timezone.utc).replace(tzinfo=None).isoformat() if banned else None,
                "last_429": datetime.fromtimestamp(self.last_429, timezone.utc).replace(tzinfo=None).isoformat() if self.last_429 else None}


class BinanceClient:
    def __init__(self, base: str | None = None):
        self.base = base or config.BINANCE_REST
        self.weight = Weight()
        self.http = httpx.AsyncClient(timeout=30)
        self._limits_read = False

    # ---- low level ----
    async def _get(self, path: str, params: dict | None = None, weight: int = 1, data: bool = False):
        if not self._limits_read and path != "/fapi/v1/exchangeInfo":
            await self.exchange_info()
        for attempt in range(4):
            if data:
                await self.weight.take_data()
                await self.weight.take(0)        # still refuses while banned
            else:
                await self.weight.take(weight)
            try:
                r = await self.http.get(f"{self.base}{path}", params=params)
            except httpx.HTTPError as exc:
                self.weight.settle(weight, None)
                if attempt == 3:
                    raise BinanceError(f"{path}: could not reach Binance ({type(exc).__name__})") from exc
                await asyncio.sleep(2 * (attempt + 1))
                continue
            self.weight.settle(weight, r.headers.get("X-MBX-USED-WEIGHT-1M") or r.headers.get("x-mbx-used-weight-1m"))
            if r.status_code == 200:
                return r.json()
            wait = float(r.headers.get("Retry-After") or 60)
            if r.status_code == 418:
                self.weight.banned_until = time.time() + wait
                raise BinanceBanned(f"Binance banned this IP for {int(wait)} seconds (HTTP 418). Downloads are stopped until then.")
            if r.status_code == 429:
                self.weight.last_429 = time.time()
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 500 and attempt < 3:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise BinanceError(f"{path}: {r.status_code} {r.text[:200]}")
        raise BinanceError(f"{path}: Binance kept answering 429 (too many requests)")

    # ---- symbols ----
    async def exchange_info(self) -> dict:
        data = await self._get("/fapi/v1/exchangeInfo", weight=1)
        for lim in data.get("rateLimits", []):
            if lim.get("rateLimitType") == "REQUEST_WEIGHT" and lim.get("interval") == "MINUTE" and int(lim.get("intervalNum", 1)) == 1:
                self.weight.limit = int(lim["limit"])
        self._limits_read = True
        return data

    async def funding_info(self) -> list[dict]:
        """Coins whose funding interval or caps were changed from the default. Others settle every 8 hours."""
        return await self._get("/fapi/v1/fundingInfo", weight=1)

    async def tickers_24h(self) -> list[dict]:
        return await self._get("/fapi/v1/ticker/24hr", weight=40)

    # ---- candles ----
    async def klines(self, symbol: str, interval: str, start: datetime | None = None, end: datetime | None = None,
                     limit: int = KLINE_LIMIT) -> list[dict]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            params["startTime"] = ms(start)
        if end is not None:
            params["endTime"] = ms(end)
        rows = await self._get("/fapi/v1/klines", params, weight=kline_weight(limit))
        return [parse_kline(r) for r in rows]

    # ---- prices, funding, open interest ----
    async def premium_index(self) -> list[dict]:
        """Mark price, index price and the current funding rate of every coin."""
        return await self._get("/fapi/v1/premiumIndex", weight=10)

    async def ltp(self, keys: list[str]) -> dict[str, float]:
        """Mark prices for the given coins, from one call for all coins. Used when the WebSocket is down."""
        wanted = set(keys)
        return {r["symbol"]: float(r["markPrice"]) for r in await self.premium_index() if r["symbol"] in wanted}

    async def funding_history(self, symbol: str, start: datetime | None = None, limit: int = 1000) -> list[dict]:
        params = {"symbol": symbol, "limit": limit}
        if start is not None:
            params["startTime"] = ms(start)
        rows = await self._get("/fapi/v1/fundingRate", params, weight=1)
        return [{"ts": from_ms(r["fundingTime"]), "rate": float(r["fundingRate"]),
                 "mark": float(r["markPrice"]) if r.get("markPrice") not in (None, "") else None} for r in rows]

    async def open_interest_hist(self, symbol: str, period: str, limit: int = 500) -> list[dict]:
        """Open interest history (Binance keeps only about the last 30 days)."""
        rows = await self._get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit}, data=True)
        return [{"ts": from_ms(r["timestamp"]), "oi": float(r["sumOpenInterest"]), "value": float(r["sumOpenInterestValue"])} for r in rows]

    def status(self) -> dict:
        return self.weight.status()

    async def close(self):
        await self.http.aclose()
