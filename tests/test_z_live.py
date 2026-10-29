"""Live mode: schema upgrade, price checks and alerts, the candle-close job, the WebSocket message format, and the live
APIs. The file name sorts after test_sync_scan on purpose: it reuses those candles."""
import asyncio
import json
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app import live, settings_store, status
from app.binance.client import BinanceError
from app.data import sync as data_sync
from app.db import SessionLocal, engine, ensure_schema
from app.jobs import scheduler as jobs
from app.main import app
from app.models import Job, SyncState, TradePlan, Zone, ZoneEvent
from tests.fake_binance import FakeBinance
from tests.test_sync_scan import KEY, LISTED

LIVE_KEY = "LIVEUSDT"


class LiveFake(FakeBinance):
    """The fake client, with mark prices that the test sets."""
    def __init__(self, prices=None, now=None):
        super().__init__(now=now, listed=LISTED)
        self.prices, self.ltp_calls = prices or {}, 0

    async def ltp(self, keys):
        self.ltp_calls += 1
        return {k: self.prices[k] for k in keys if k in self.prices}


def test_schema_upgrade_adds_a_missing_column():
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN progress"))
    assert "progress" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
    ensure_schema()
    assert "progress" in {c["name"] for c in inspect(engine).get_columns("jobs")}
    ensure_schema()   # running it again changes nothing


def make_setup(db) -> int:
    """A fresh 1-hour demand zone with a pending plan in WEEKLY mode (week, day, 1 hr)."""
    z = Zone(instrument_key=LIVE_KEY, timeframe="1h", side="demand", pattern="DBR", proximal=521.0, distal=514.0,
             base_start_ts=datetime(2026, 10, 1, 10, 15), base_count=2, created_ts=datetime(2026, 10, 1, 12, 15), atr=0.0,
             peak_distance=0.0, touches=0, in_visit=False, status="fresh", score=8.0, label="TYPE_1", has_gap=False,
             legout_body=0.88, legout_close=544.0, legin_explosive=True)
    db.add(z)
    db.flush()
    db.add(TradePlan(zone_id=z.id, version=1, entry=521.0, stop=512.0, target=557.0, reward_risk=4.0, status="pending",
                     is_current=True, allowed=True, trend="UP", mode="WEEKLY",
                     context={"mode": "WEEKLY", "curve": "LOW", "htf_trend": "SIDEWAYS", "itf_trend": "UP", "market_state": "NORMAL", "side": "BUY"}))
    db.commit()
    return z.id


class Page:
    def __init__(self):
        self.got = []

    async def send_json(self, message):
        self.got.append(message)


def test_alerts_fire_once_each_and_reach_the_page():
    page = Page()
    live.hub.clients.add(page)
    with SessionLocal() as db:
        zid = make_setup(db)
        settings_store.put(db, "universe", [LIVE_KEY])
    now = data_sync.now_utc()
    client = LiveFake(prices={LIVE_KEY: 550.0})
    assert asyncio.run(live.poll_once(client, now)) == []                          # more than 1% away: nothing
    client.prices[LIVE_KEY] = 525.0
    first = asyncio.run(live.poll_once(client, now))
    assert [(a["alert"], a["zone_id"], a["entry"], a["stop"], a["target"]) for a in first] == [("approaching", zid, 521.0, 512.0, 557.0)]
    assert asyncio.run(live.poll_once(client, now)) == []                          # same state: no repeat
    # prices pushed from the WebSocket instead of fetched: the same checks, no REST call
    calls = client.ltp_calls
    assert [a["alert"] for a in asyncio.run(live.poll_once(client, now, prices={LIVE_KEY: 520.5, "OTHERUSDT": 1.0}))] == ["triggered"]
    assert client.ltp_calls == calls and "OTHERUSDT" not in live.state["prices"]
    client.prices[LIVE_KEY] = 520.5
    assert asyncio.run(live.poll_once(client, now)) == []
    with SessionLocal() as db:
        assert db.scalars(select(TradePlan).where(TradePlan.zone_id == zid)).one().status == "triggered"
        kinds = [e.event_type for e in db.scalars(select(ZoneEvent).where(ZoneEvent.zone_id == zid).order_by(ZoneEvent.id))]
        assert kinds == ["alert_approaching", "alert_triggered"]
    assert [m["type"] for m in page.got].count("alert") == 2 and page.got[0]["type"] == "prices"
    assert live.state["prices"][LIVE_KEY] == 520.5 and live.state["last_poll"] == now
    live.hub.clients.discard(page)


def test_alert_goes_straight_to_triggered_when_price_gaps_into_the_zone():
    with SessionLocal() as db:
        zid = make_setup(db)
    alerts = asyncio.run(live.poll_once(LiveFake(prices={LIVE_KEY: 519.0}), data_sync.now_utc()))
    assert [(a["alert"], a["zone_id"]) for a in alerts] == [("triggered", zid)]


def test_mark_price_stream_messages_and_market_hours():
    raw = json.dumps([{"e": "markPriceUpdate", "s": "BTCUSDT", "p": "67012.30000000", "r": "0.00010000"},
                      {"e": "markPriceUpdate", "s": "PEPEUSDT", "p": "0.0000123400"}, {"e": "markPriceUpdate", "s": "BAD"}])
    assert live.parse_mark_prices(raw) == {"BTCUSDT": 67012.3, "PEPEUSDT": 0.00001234}
    assert live.parse_mark_prices(json.dumps({"stream": "!markPrice@arr@1s", "data": [{"s": "ETHUSDT", "p": "2500"}]})) == {"ETHUSDT": 2500.0}
    assert live.stream_url() == "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"      # Binance's routed address since 2026
    assert live.market_open(datetime(2026, 10, 18, 3, 0)) and live.market_open(datetime(2026, 12, 25, 23, 59))   # a Sunday, Christmas
    assert live.next_bar_close(datetime(2026, 10, 18, 23, 57, 30)) == datetime(2026, 10, 19, 0, 0)


def test_candle_close_job_downloads_what_closed_and_scans():
    now = datetime(2026, 10, 18, 12, 0, 8)      # 12:00: 1m, 5m, 15m, 30m, 1h, 2h and 4h candles have just closed
    with SessionLocal() as db:
        settings_store.put(db, "universe", [KEY])
        status.mark(db, KEY, "ready")
    client, page = LiveFake(now=now), Page()
    live.hub.clients.add(page)
    out = asyncio.run(jobs.bar_close(client, now))
    live.hub.clients.discard(page)
    asked = {c[2] for c in client.calls if c[0] == "klines"}
    assert asked == {"1m", "5m", "15m", "30m", "1h", "2h", "4h"}                 # not the day, week or month
    assert (out["coins"], out["scanned"], out["errors"]) == (1, 1, 0) and out["timeframes"][0] == "1m"
    with SessionLocal() as db:
        assert db.scalars(select(Job).where(Job.type == "candle close")).first() is None    # a normal run writes no job row
        assert db.get(SyncState, (KEY, "1m")).last_candle_ts == datetime(2026, 10, 18, 11, 59)
        assert db.get(SyncState, (KEY, "5m")).last_candle_ts == datetime(2026, 10, 18, 11, 55)
        assert db.get(SyncState, (KEY, "1h")).last_candle_ts == datetime(2026, 10, 18, 11, 0)
        assert db.get(SyncState, (KEY, "4h")).last_candle_ts == datetime(2026, 10, 18, 8, 0)
        assert db.get(SyncState, (KEY, "5m")).last_scanned_ts == datetime(2026, 10, 18, 11, 55)
    again = asyncio.run(jobs.bar_close(LiveFake(now=now), now))
    assert again["scanned"] == 0                                                   # nothing new: nothing rescanned
    assert live.state["last_scan"] == now and page.got[-1]["type"] == "scan" and page.got[-1]["coins"] == [KEY]
    with SessionLocal() as db:
        settings_store.put(db, "modes", ["DAILY", "MONTHLY"])
    assert asyncio.run(jobs.bar_close(client, datetime(2026, 10, 18, 12, 7, 8))) is None      # no mode reads 1 minute candles
    with SessionLocal() as db:
        db.delete(db.get(__import__("app.models", fromlist=["Setting"]).Setting, "modes"))
        db.commit()


def test_failed_job_is_recorded_not_raised():
    class Down(LiveFake):
        async def exchange_info(self):
            raise BinanceError("/fapi/v1/exchangeInfo: could not reach Binance (ConnectError)")
    job_id = asyncio.run(jobs.refresh_coins(Down()))
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed" and "could not reach Binance" in job.error


def test_live_apis_and_page():
    with SessionLocal() as db:
        settings_store.put(db, "universe", ["BTCUSDT", LIVE_KEY])
    with TestClient(app) as c:
        assert c.get("/scanner").status_code == 200 and c.get("/", follow_redirects=False).headers["location"] == "/scanner"
        s = c.get("/api/live/status").json()
        assert {"market_open", "feed", "bias", "alerts", "next_bar_close", "last_scan", "prices", "scheduler", "binance", "display_tz"} <= set(s)
        assert s["scheduler"] is False and s["market_open"] is True and s["display_tz"] == "Asia/Riyadh"
        assert s["bias"][0]["symbol"] == "BTCUSDT" and s["bias"][0]["side"] == "NONE"          # no BTC candles here
        assert s["bias_mode"] == "DAILY" and s["modes"]["MONTHLY"] == ["1mo", "1w", "1d"] and s["modes"]["15M"] == ["15m", "5m", "1m"]
        assert [a["alert"] for a in s["alerts"]][:3] == ["triggered", "triggered", "approaching"]
        rows = c.get("/api/setups?include_triggered=true").json()
        hit = [r for r in rows if r["instrument_key"] == LIVE_KEY]
        assert len(hit) == 2 and all(r["plan_status"] == "triggered" and r["triggered_ts"] and r["price"] == 519.0 for r in hit)
        assert [r for r in c.get("/api/setups").json() if r["instrument_key"] == LIVE_KEY] == []
        b = c.get("/api/binance/status").json()
        assert {"used_1m", "limit_1m", "budget_1m", "banned", "feed"} <= set(b) and b["banned"] is False
        with c.websocket_connect("/ws/live") as ws:
            assert ws.receive_json()["type"] == "prices"            # last known prices on connect
            c.portal.call(live.hub.broadcast, {"type": "scan", "time": "x"})
            assert ws.receive_json() == {"type": "scan", "time": "x"}
