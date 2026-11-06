"""Paper page chart popup: candles of another timeframe, and changing the stop and target of a pending order or an
open position from the chart (with the size worked out again, and a stop already past the price refused)."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import live
from app.db import SessionLocal
from app.main import app
from app.models import PaperTrade
from app.sim import paper
from tests.test_z_live import LIVE_KEY
from tests.test_z_paper import NOW, QTY, fresh_setup


def test_pending_order_stop_and_target_from_the_chart():
    zid = fresh_setup()
    with SessionLocal() as db:
        order = paper.place(db, zid, NOW)                     # long: entry 521, stop 512, target 557, size QTY
        oid = order.id
        edited = paper.edit_order(db, oid, 503.0, 570.0, NOW)
        assert (edited.stop, edited.target) == (503.0, 570.0)
        assert edited.quantity == pytest.approx(QTY / 2, abs=0.002)        # twice the distance to the stop: half the size
        assert edited.note.startswith(paper.EDITED)
        for stop, target in ((525.0, 570.0), (503.0, 515.0)):              # stop above the entry, target below it
            with pytest.raises(ValueError):
                paper.edit_order(db, oid, stop, target, NOW)
        assert paper._refresh_order(db, edited) and (edited.stop, edited.target) == (503.0, 570.0)   # kept, not reset to the plan
        paper.cancel(db, oid)


def test_position_stop_past_the_price_is_refused():
    zid = fresh_setup()
    with SessionLocal() as db:
        paper.place(db, zid, NOW)
        paper.on_prices(db, {LIVE_KEY: 520.5}, NOW)
        trade = db.scalars(select(PaperTrade).where(PaperTrade.zone_id == zid)).one()
        with pytest.raises(ValueError, match="past the price"):
            paper.edit(db, trade.id, 523.0, None, NOW, price=522.0)         # a long's stop above the price
        with pytest.raises(ValueError, match="past the price"):
            paper.edit(db, trade.id, None, 521.5, NOW, price=522.0)         # a long's target below the price
        assert paper.edit(db, trade.id, 516.0, 560.0, NOW, price=522.0).stop == 516.0
        paper.manual_close(db, trade.id, 522.0, NOW)


def test_chart_in_another_timeframe_and_order_edit_api():
    zid = fresh_setup()
    with SessionLocal() as db:
        oid = paper.place(db, zid, NOW).id
    with TestClient(app) as c:
        own = c.get(f"/api/paper/chart/order/{oid}").json()
        other = c.get(f"/api/paper/chart/order/{oid}", params={"tf": "1h"}).json()
        assert other["timeframe"] == "1h" and other["own_timeframe"] == own["timeframe"]
        assert c.get(f"/api/paper/chart/order/{oid}", params={"tf": "7m"}).status_code == 400
        got = c.patch(f"/api/paper/orders/{oid}", json={"stop": 506.0, "target": 566.0}).json()
        assert (got["stop"], got["target"]) == (506.0, 566.0)
        assert c.patch(f"/api/paper/orders/{oid}", json={"stop": 530.0}).status_code == 400
        assert c.patch("/api/paper/orders/999999", json={"stop": 1.0}).status_code == 400
        page = c.get("/paper").text
        assert 'id="tc-tfs"' in page and 'id="tc-save"' in page and 'class="tc-modal"' in page
        c.delete(f"/api/paper/orders/{oid}")
    live.state["prices"].pop(LIVE_KEY, None)
