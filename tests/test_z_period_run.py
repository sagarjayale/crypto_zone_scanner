"""The Backtest page's "From this date to today" run: order counters, the summary with halves and sides, the history
note, coins left out for missing candles, and deleting a run."""
import time
from datetime import datetime

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.sim.backtest import run_one, summarize
from tests.test_sync_scan import KEY
from tests.test_z_backtest import END, MODE, OPTS, START


def test_run_one_counts_setups_and_fills():
    stats: dict = {}
    with SessionLocal() as db:
        trades = run_one(db, KEY, MODE, START, END, OPTS, stats=stats)
        again = run_one(db, KEY, MODE, START, END, OPTS)          # without stats: the same trades
    assert [t["entry_ts"] for t in trades] == [t["entry_ts"] for t in again]
    assert stats["filled"] == len(trades) and stats["setups"] >= stats["filled"] > 0


def t(day, result, r, side="demand"):
    ts = datetime(2026, 9, day, 12)
    return {"result": result, "result_r": r, "exit_ts": ts, "entry_ts": ts, "mfe_r": 2 if result == "win" else 0.5, "side": side}


def test_summary_halves_sides_stop_rate_and_orders():
    trades = [t(2, "win", 1.95), t(5, "loss", -1.1), t(8, "loss", -1.08, "supply"),          # first half (to 16 Sep)
              t(20, "win", 1.94, "supply"), t(25, "win", 1.96), t(27, "open", 0.3)]           # second half
    s = summarize(trades, datetime(2026, 9, 1), datetime(2026, 10, 1), {"setups": 9, "filled": 6})
    assert s["trades"] == 5 and s["stop_rate"] == 40.0 and s["win_rate"] == 60.0 and s["open_trades"] == 1
    assert s["halves"]["mid"].startswith("2026-09-16")
    assert (s["halves"]["first"]["trades"], s["halves"]["first"]["stop_rate"]) == (3, 66.7)
    assert (s["halves"]["second"]["trades"], s["halves"]["second"]["win_rate"]) == (2, 100.0)
    assert s["by_side"]["demand"]["trades"] == 3 and s["by_side"]["supply"]["total_r"] == round(-1.08 + 1.94, 3)
    assert s["orders"] == {"setups": 9, "filled": 6, "never_filled": 3}
    assert "halves" not in summarize(trades) and "orders" not in summarize(trades)


def test_the_page_runs_from_a_date_to_today():
    with TestClient(app) as c:
        page = c.get("/backtest").text
        assert "From this date to today" in page and 'id="p-past"' in page
        body = {"symbols": [KEY, "ETHUSDT"], "modes": [MODE], "start": "2026-07-01", "end": "2026-10-18"}
        out = c.post("/api/backtest/run", json=body).json()                    # ETHUSDT has no candles: left out, not refused
        assert "ETHUSDT" in (out["warning"] or "")
        run_id = out["run_id"]
        for _ in range(160):
            r = c.get(f"/api/backtest/{run_id}").json()
            if r["status"] != "running":
                break
            time.sleep(0.5)
        assert r["status"] == "done", r
        assert r["settings"]["symbols"] == [KEY]
        s = r["summary"]
        assert s["orders"]["filled"] == len(r["trades"]) and s["orders"]["setups"] >= s["orders"]["filled"]
        assert s["halves"]["first"]["trades"] + s["halves"]["second"]["trades"] == s["trades"]
        head = next(x for x in c.get("/api/backtest/runs").json() if x["id"] == run_id)["headline"]
        assert head["stop_rate"] == s["stop_rate"] and head["orders"] == s["orders"]
        assert c.delete(f"/api/backtest/{run_id}").json() == {"deleted": run_id}
        assert c.get(f"/api/backtest/{run_id}").status_code == 404
        assert c.delete(f"/api/backtest/{run_id}").status_code == 404
