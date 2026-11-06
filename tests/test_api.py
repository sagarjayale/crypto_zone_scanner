from fastapi.testclient import TestClient

from app.main import app


def test_api_boots_and_reports_empty_state():
    with TestClient(app) as c:
        assert c.get("/api/binance/status").json()["banned"] is False
        assert {"rows", "weight", "status"} <= set(c.get("/api/data/status").json())
        s = c.get("/api/settings").json()
        assert s["min_score"] == 5.0 and s["top_n"] == 50 and s["universe_auto"] is True and s["universe"] == [] and "15M" in s["modes"]
        assert c.put("/api/settings", json={"min_score": 7}).json()["min_score"] == 7
        c.put("/api/settings", json={"min_score": 5.0})
        assert s["direction_rule"] == "location" and c.put("/api/settings", json={"direction_rule": "sometimes"}).status_code == 400
        assert c.put("/api/settings", json={"direction_rule": "trend"}).json()["direction_rule"] == "trend"
        c.put("/api/settings", json={"direction_rule": "location"})
        assert c.get("/api/setups").status_code == 200
        assert c.get("/api/zone/999999").status_code == 404
        assert c.post("/api/data/sync/NOPEUSDT").status_code == 404           # not a coin the app knows
        assert len([r for r in app.routes if getattr(r, "tags", None)]) == 51   # data 10, scan 10, system 8, scan-date checks 4, replay 1, backtest 6, live 2, paper 10