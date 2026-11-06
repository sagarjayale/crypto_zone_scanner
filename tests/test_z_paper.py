"""Paper trading with USDT, step sizes, leverage, fees and funding; Telegram; and the settings and coin list APIs.
The file name sorts after test_sync_scan and test_z_live on purpose: it reuses their helpers and data."""
import asyncio
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import live, settings_store
from app.alerts import telegram
from app.data import sync as data_sync
from app.db import SessionLocal
from app.main import app
from app.models import FundingRate, Instrument, PaperOrder, PaperTrade, TradePlan, Zone
from app.sim import paper
from tests.test_sync_scan import KEY
from tests.test_z_live import LIVE_KEY, LiveFake, make_setup

NOW = datetime(2026, 10, 18, 11, 0)
QTY = 11.111                       # 1% of 10,000 USDT = 100 USDT at risk, 9 USDT a coin to the stop, step 0.001


def setup_module():
    with SessionLocal() as db:
        db.merge(Instrument(instrument_key=LIVE_KEY, symbol=LIVE_KEY, name="LIVE", status="TRADING", tick_size=0.01, step_size=0.001,
                            min_qty=0.001, min_notional=5.0, funding_hours=8, quote_volume=1e6))
        db.commit()


def fresh_setup() -> int:
    """A new setup, with earlier paper trades archived so the balance (and so the size) starts at 10,000 USDT again."""
    with SessionLocal() as db:
        paper.reset(db)
        return make_setup(db)   # demand 521 / 514, stop 512, target 557, score 8, allowed


def test_size_leverage_and_liquidation():
    with SessionLocal() as db:
        assert paper.quantity(db, LIVE_KEY, 521.0, 512.0) == QTY
        assert paper.liquidation("demand", 100.0, 10, 0.5) == pytest.approx(90.5)
        assert paper.liquidation("supply", 100.0, 10, 0.5) == pytest.approx(109.5)
        assert paper.check_leverage(db, "demand", 521.0, 512.0, 5)[1] == pytest.approx(521 * (1 - 0.2 + 0.005))
        with pytest.raises(ValueError, match="comes before the stop loss"):
            paper.check_leverage(db, "demand", 521.0, 512.0, 100)          # liquidated near 518, before the 512 stop
        db.get(Instrument, LIVE_KEY).min_notional = 10_000.0
        db.commit()
        assert paper.quantity(db, LIVE_KEY, 521.0, 512.0) == 0.0          # under the minimum order value
        zid = make_setup(db)
        with pytest.raises(ValueError, match="minimum order size"):
            paper.place(db, zid, NOW)
        db.get(Instrument, LIVE_KEY).min_notional = 5.0
        db.commit()


def test_order_fill_stop_with_fees():
    zid = fresh_setup()
    with SessionLocal() as db:
        order = paper.place(db, zid, NOW)
        assert (order.quantity, order.entry, order.stop, order.target, order.leverage) == (QTY, 521.0, 512.0, 557.0, 5.0)
        with pytest.raises(ValueError, match="already exists"):
            paper.place(db, zid, NOW)
        assert paper.on_prices(db, {LIVE_KEY: 530.0}, NOW) == []
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 520.5}, NOW)] == ["filled"]
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        assert (trade.fill_price, trade.risk, trade.margin) == (521.0, 9.0, pytest.approx(QTY * 521 / 5))
        assert trade.fees == pytest.approx(QTY * 521 * 0.0002)                       # a limit entry pays the maker fee
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 511.0}, NOW)] == ["stopped"]
        db.refresh(trade)
        fees = QTY * 521 * 0.0002 + QTY * 511 * 0.0005                               # the stop pays the taker fee
        assert (trade.exit_price, trade.exit_reason, trade.result) == (511.0, "stop", "loss")
        assert trade.pnl == pytest.approx(-10 * QTY - fees, abs=1e-3) and trade.result_r == pytest.approx((-10 * QTY - fees) / (9 * QTY), abs=1e-3)


def test_target_and_funding_paid_while_open():
    zid = fresh_setup()
    with SessionLocal() as db:
        paper.place(db, zid, NOW)
        paper.on_prices(db, {LIVE_KEY: 521.0}, NOW)
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        # two funding settlements while the long is open: +0.01% (longs pay) and -0.02% (longs receive), one before the fill
        for ts, rate in ((NOW - timedelta(hours=1), 0.05), (NOW + timedelta(hours=1), 0.0001), (NOW + timedelta(hours=9), -0.0002)):
            db.merge(FundingRate(instrument_key=LIVE_KEY, ts=ts, rate=rate, mark=530.0))
        db.commit()
        later = NOW + timedelta(hours=10)
        assert [e["event"] for e in paper.on_prices(db, {LIVE_KEY: 558.0}, later)] == ["target hit"]
        db.refresh(trade)
        funding = QTY * 530.0 * (0.0002 - 0.0001)
        fees = QTY * 521 * 0.0002 + QTY * 557 * 0.0002                                # limit in, limit out: maker both ways
        assert trade.funding == pytest.approx(funding) and trade.fees == pytest.approx(fees)
        assert trade.pnl == pytest.approx(36 * QTY - fees + funding, abs=1e-3) and trade.result == "win"
        db.execute(delete(FundingRate).where(FundingRate.instrument_key == LIVE_KEY))
        db.commit()


def test_pending_order_is_cancelled_when_its_zone_is_violated():
    zid = fresh_setup()
    with SessionLocal() as db:
        order = paper.place(db, zid, NOW)
        db.get(Zone, zid).status = "violated"
        db.commit()
        assert paper.on_prices(db, {LIVE_KEY: 520.0}, NOW) == []
        db.refresh(order)
        assert (order.status, order.note) == ("cancelled", "zone violated")


def test_candles_catch_a_fill_and_stop_that_the_feed_missed():
    key = "PAPERUSDT"
    day = datetime(2026, 10, 5)
    bars = [(10, 0, 530, 532, 528, 529), (10, 5, 529, 530, 520, 524), (10, 10, 524, 526, 511, 513), (10, 15, 513, 560, 512, 558)]
    with SessionLocal() as db:
        data_sync.upsert_candles(db, key, "5m", [{"ts": day.replace(hour=h, minute=m), "open": o, "high": hi, "low": lo, "close": c,
                                                  "volume": 1, "oi": 0} for h, m, o, hi, lo, c in bars])
        z = Zone(instrument_key=key, timeframe="5m", side="demand", pattern="DBR", proximal=521.0, distal=514.0,
                 base_start_ts=day, base_count=2, created_ts=day, atr=10.0, peak_distance=30.0, touches=0, in_visit=False, status="fresh", score=8)
        db.add(z); db.flush()
        db.add(TradePlan(zone_id=z.id, version=1, entry=521.0, stop=512.0, target=557.0, reward_risk=4.0, status="pending", is_current=True, allowed=True))
        db.commit()
        paper.place(db, z.id, day.replace(hour=10, minute=0))
        events = paper.on_candles(db, key)
        assert [(e["event"], e["price"]) for e in events] == [("filled", 521.0), ("stopped", 512.0)]     # fill 10:05, stop 10:10
        t = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == z.id)).one()
        assert (t.fill_ts, t.exit_ts, t.exit_price) == (day.replace(hour=10, minute=5), day.replace(hour=10, minute=10), 512.0)
        assert t.result_r == pytest.approx(-1.0 - (521 * 0.0002 + 512 * 0.0005) / 9, abs=1e-3)       # one R lost, plus fees
        assert paper.on_candles(db, key) == []      # nothing is processed twice


def test_telegram_text_and_send(monkeypatch):
    a = {"alert": "approaching", "symbol": "XYZUSDT", "timeframe": "1d", "side": "demand", "price": 530.0, "entry": 521.0, "stop": 512.0,
         "target": 557.0, "reward_risk": 4.0, "score": 8}
    assert telegram.alert_text(a) == "XYZUSDT Day demand zone (long): approaching at 530\nEntry type 1 | Entry 521 | Stop 512 | Target 557 | R:R 4.0 | Score 8"
    cheap = telegram.alert_text({**a, "price": 0.00001234, "entry": 0.0000121, "stop": 0.00001195, "target": 0.0000124})
    assert "at 1.234e-05" in cheap or "at 0.00001234" in cheap
    hit = telegram.alert_text({**a, "alert": "triggered"})
    assert hit.startswith("XYZUSDT Day demand zone (long): entry reached at 530")
    three = telegram.alert_text({**a, "alert": "triggered", "entry_type": "TYPE_3", "score": 6})       # reaching the zone is not yet the entry
    assert three.splitlines()[0].endswith("price is at the zone at 530")
    assert three.splitlines()[1] == "Entry type 3: wait for a candle to close inside the zone, then the next candle closes back out of it | Stop 512 | Score 6"
    assert telegram.paper_text({"event": "filled", "instrument_key": "XYZUSDT", "side": "supply", "quantity": 1.5, "price": 2.25}) == "Paper trade filled: XYZUSDT short 1.5 at 2.25"
    assert asyncio.run(telegram.send("x")) == (False, "Telegram is not set up: add the bot token and chat id in settings.")
    sent = {}

    class Reply:
        status_code = 200
        text = ""
        def json(self):
            return {"ok": True}

    async def fake_post(self, url, json=None, **kw):
        sent.update(url=url, json=json)
        return Reply()
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    with SessionLocal() as db:
        settings_store.put(db, "telegram_token", "123:abc"); settings_store.put(db, "telegram_chat_id", "42")
    assert asyncio.run(telegram.send("hello")) == (True, "sent")
    assert sent == {"url": "https://api.telegram.org/bot123:abc/sendMessage", "json": {"chat_id": "42", "text": "hello"}}


def wait_job(c, job_id):
    for _ in range(240):
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.25)
    raise AssertionError("job did not finish")


def test_paper_and_settings_through_the_api(monkeypatch):
    zid = fresh_setup()
    fake = LiveFake(prices={LIVE_KEY: 540.0}, now=datetime(2026, 10, 18, 11, 37, 20))
    monkeypatch.setattr("app.main.client", fake)
    with SessionLocal() as db:
        settings_store.put(db, "universe", [LIVE_KEY])
    with TestClient(app) as c:
        for path in ("/paper", "/settings"):
            assert c.get(path).status_code == 200
        assert c.get("/options").status_code == 404
        # paper: place, fill through a live price round, edit, close, summary, reset
        assert c.post("/api/paper/orders", json={"zone_id": zid, "leverage": 100}).status_code == 400     # liquidated before the stop
        order = c.post("/api/paper/orders", json={"zone_id": zid, "leverage": 3}).json()
        assert order["status"] == "pending" and order["leverage"] == 3 and c.post("/api/paper/orders", json={"zone_id": zid}).status_code == 400
        fake.prices[LIVE_KEY] = 520.0
        c.portal.call(live.poll_once, fake)
        pos = c.get("/api/paper/positions").json()
        mine = [t for t in pos["open"] if t["zone_id"] == zid]
        assert len(mine) == 1 and mine[0]["symbol"] == LIVE_KEY and mine[0]["leverage"] == 3
        assert mine[0]["open_pnl"] == pytest.approx((520.0 - 521.0) * QTY - QTY * 521 * 0.0002, abs=1e-3)
        tid = mine[0]["id"]
        assert c.patch(f"/api/paper/positions/{tid}", json={"stop": 600}).status_code == 400            # stop above target
        edited = c.patch(f"/api/paper/positions/{tid}", json={"stop": 515}).json()
        assert edited["stop"] == 515 and edited["history"][0]["field"] == "stop" and edited["history"][0]["old"] == 512
        fake.prices[LIVE_KEY] = 540.0
        c.portal.call(live.poll_once, fake)
        closed = c.post(f"/api/paper/positions/{tid}/close").json()
        assert closed["exit_reason"] == "manual" and abs(closed["exit_price"] - 540.0 * (1 - 0.0005)) < 1e-6 and closed["result"] == "win"
        assert c.post(f"/api/paper/positions/{tid}/close").status_code == 400
        s = c.get("/api/paper/summary").json()
        assert s["closed_trades"] >= 1 and s["balance"] == round(s["capital"] + s["realised"], 2) and s["capital"] == 10000.0
        assert s["fees_paid"] > 0 and "margin_used" in s
        assert c.delete("/api/paper/orders/999999").status_code == 400
        assert c.post("/api/paper/reset").json()["archived"] > 0
        s = c.get("/api/paper/summary").json()
        assert (s["balance"], s["closed_trades"], s["open_positions"], s["pending_orders"]) == (s["capital"], 0, 0, 0)
        # auto mode places an order for a watched setup on the next price round
        zid2 = fresh_setup()
        c.put("/api/settings", json={"paper_auto": True, "paper_auto_min_score": 8})
        fake.prices[LIVE_KEY] = 560.0
        c.portal.call(live.poll_once, fake)
        assert zid2 in [o["zone_id"] for o in c.get("/api/paper/positions").json()["pending"]]
        c.put("/api/settings", json={"paper_auto": False})
        # settings: validation, secrets stay hidden, coin search, top N, pasted symbols, new coins are downloaded, rebuild
        got = c.get("/api/settings").json()
        assert "telegram_token" not in got and got["telegram_configured"] is True and got["display_tz"] == "Asia/Riyadh"
        assert got["fee_maker_pct"] == 0.02 and got["fee_taker_pct"] == 0.05 and got["itf_rule"] == "atr"
        assert c.put("/api/settings", json={"display_tz": "Mars/Base"}).status_code == 400
        assert c.put("/api/settings", json={"paper_leverage": 200}).status_code == 400
        assert c.put("/api/settings", json={"itf_rule": "magic"}).status_code == 400
        assert c.post("/api/alerts/test").status_code in (200, 400)        # no network in tests: either answer is handled
        assert c.get("/api/instruments/search", params={"q": "live"}).json()[0]["instrument_key"] == LIVE_KEY
        top = c.get("/api/instruments/top", params={"n": 2}).json()
        assert top["count"] == 2 and top["rows"][0]["instrument_key"] == "BTCUSDT"
        m = c.post("/api/instruments/match", json={"text": "btc, ethusdt NOPE"}).json()
        assert [(r["query"], r["instrument_key"], r["found"]) for r in m["rows"]] == [("BTC", "BTCUSDT", True), ("ETHUSDT", "ETHUSDT", True), ("NOPE", None, False)]
        out = c.put("/api/settings", json={"universe": [LIVE_KEY, KEY.lower()]}).json()
        assert out["universe"] == [LIVE_KEY, KEY]                                                       # symbols are upper-cased
        assert out["sync_job_id"] and wait_job(c, out["sync_job_id"])["status"] == "done"
        assert c.get("/api/jobs").json()[0]["type"] == "new coins"
        # 1 and 5 minute zones are left out: the download in between keeps only the last 14 days of 1 minute and 90 days
        # of 5 minute candles counted from today's real date, so once the calendar moves past the test data (18 Oct 2026)
        # the rebuild sees fewer of them.
        active = lambda d: sorted((z.timeframe, z.side, z.proximal, z.distal, z.touches, z.status) for z in d.scalars(
            select(Zone).where(Zone.instrument_key == KEY, Zone.status.in_(("fresh", "tested")), Zone.timeframe.notin_(("1m", "5m")))))
        with SessionLocal() as db:
            before = active(db)
        c.put("/api/settings", json={"universe": [KEY]})
        assert wait_job(c, c.post("/api/rebuild/zones").json()["job_id"])["status"] == "done"
        with SessionLocal() as db:
            after = active(db)
        assert before and after == before        # same candles and rules give the same active zones
