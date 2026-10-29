"""The Binance client against a mock transport: the weight limit read from exchangeInfo, the weight header, 429 back-off,
418 ban, and kline parsing. No network is used."""
import asyncio
import json
from datetime import datetime

import httpx
import pytest

from app.binance import client as bc

INFO = {"rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400},
                       {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 1200}], "symbols": []}
KLINE = [1760745600000, "67000.1", "67100.0", "66900.5", "67050.0", "123.456", 1760745899999, "8273645.12", 1500, "60", "4000000", "0"]


def make(handler):
    c = bc.BinanceClient(base="https://fapi.test")
    c.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def test_limit_from_exchange_info_weight_header_and_klines():
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        if req.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=INFO, headers={"X-MBX-USED-WEIGHT-1M": "1"})
        return httpx.Response(200, json=[KLINE], headers={"X-MBX-USED-WEIGHT-1M": "6"})

    async def go():
        c = make(handler)
        rows = await c.klines("BTCUSDT", "5m", start=datetime(2025, 10, 18), limit=1000)
        return c, rows
    c, rows = asyncio.run(go())
    assert seen[0][0] == "/fapi/v1/exchangeInfo"                        # the limit is read before the first real call
    assert seen[1] == ("/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "5m", "limit": "1000", "startTime": "1760745600000"})
    assert rows == [{"ts": datetime(2025, 10, 18), "open": 67000.1, "high": 67100.0, "low": 66900.5, "close": 67050.0,
                     "volume": 8273645, "oi": 0, "close_ms": 1760745899999}]          # volume in USDT
    st = c.status()
    assert (st["limit_1m"], st["budget_1m"], st["used_1m"], st["banned"]) == (2400, 1200, 6, False)
    assert [bc.kline_weight(n) for n in (5, 99, 100, 499, 500, 1000, 1500)] == [1, 1, 2, 2, 5, 5, 10]


def test_429_waits_and_retries_then_418_stops_everything(monkeypatch):
    slept = []

    async def no_sleep(s):
        slept.append(s)
    monkeypatch.setattr(bc.asyncio, "sleep", no_sleep)
    answers = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json=[])])

    def handler(req):
        if req.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=INFO)
        return next(answers)

    async def go():
        c = make(handler)
        assert await c.klines("BTCUSDT", "1m", limit=10) == []
        return c
    c = asyncio.run(go())
    assert 7.0 in slept and c.status()["last_429"] is not None

    def banned(req):
        return httpx.Response(418, headers={"Retry-After": "120"}) if req.url.path != "/fapi/v1/exchangeInfo" else httpx.Response(200, json=INFO)

    async def go2():
        c = make(banned)
        with pytest.raises(bc.BinanceBanned, match="banned this IP for 120 seconds"):
            await c.klines("BTCUSDT", "1m", limit=10)
        with pytest.raises(bc.BinanceBanned, match="banned this IP until"):        # nothing more is sent until the ban ends
            await c.premium_index()
        return c
    c = asyncio.run(go2())
    assert c.status()["banned"] is True and c.status()["banned_until"]


def test_the_limiter_waits_for_the_next_minute_when_the_budget_is_spent(monkeypatch):
    w = bc.Weight()
    w.limit = 100                     # budget 50
    w.minute, w.used = w._now_minute(), 48
    waited = []

    async def fake_sleep(s):
        waited.append(s)
        w.minute = None               # the next minute has started
    monkeypatch.setattr(bc.asyncio, "sleep", fake_sleep)
    asyncio.run(w.take(5))
    assert waited and w.planned == 5 and w.used == 0
