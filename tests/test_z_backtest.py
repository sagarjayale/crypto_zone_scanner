"""Backtest under GTF rules, on the candles created by test_sync_scan (the file name sorts after it on purpose)."""
import time
from datetime import datetime

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.sim.backtest import run_one, summarize
from tests.test_sync_scan import KEY

START, END = datetime(2026, 7, 1), datetime(2026, 10, 18, 11, 0)
OPTS = {"min_score": 0}         # fees from the settings: maker 0.02%, taker 0.05%
MODE = "HOURLY"      # HTF 1 hr, ITF 30 min, LTF 5 min


def check_common(t):
    assert (t["mode"], t["timeframe"]) == (MODE, "5m") and t["pattern"] in ("DBR", "RBD") and t["reward_risk"] == 2.0
    assert START <= t["entry_ts"] <= END and t["entry_ts"] > t["zone_created_ts"] and t["exit_ts"] >= t["entry_ts"]
    assert t["stop"] == t["zone_distal"]
    if t["side"] == "demand":      # location beats trend: a buy is low on the curve, or in equilibrium with the ITF up
        assert t["stop"] < t["entry"] < t["target"]
        assert t["curve"] in ("VERY LOW", "LOW") or (t["curve"] == "EQUILIBRIUM" and t["trend"] == "UP")
    else:
        assert t["stop"] > t["entry"] > t["target"]
        assert t["curve"] in ("HIGH", "VERY HIGH") or (t["curve"] == "EQUILIBRIUM" and t["trend"] == "DOWN")
    # fees: maker for a type 1 entry and for the target, taker for a type 2 or 3 entry and for the stop
    f_in = 0.0002 if t["entry_type"] == "TYPE_1" else 0.0005
    if t["result"] == "win":
        fees = (t["entry"] * f_in + t["target"] * 0.0002) / t["risk"]
        assert abs(t["fees_r"] + fees) < 1e-3 and abs(t["result_r"] - (2.0 - fees + t["funding_r"])) < 2e-3 and t["mfe_r"] >= 1.99
    if t["result"] == "loss":
        assert t["result_r"] < -1 and t["fees_r"] < 0
    assert t["result"] in ("win", "loss", "open")


def test_the_score_decides_the_entry_type():
    with SessionLocal() as db:
        trades = run_one(db, KEY, MODE, START, END, OPTS)          # "rule": type 1 for 7 or more, type 3 for 5 to 6.5
    assert len(trades) > 5 and {t["entry_type"] for t in trades} <= {"TYPE_1", "TYPE_3"}
    for t in trades:
        check_common(t)
        if t["entry_type"] == "TYPE_1":
            assert t["score"] >= 7 and t["entry"] == t["zone_proximal"]              # a limit at the proximal line
        else:
            assert 5 <= t["score"] < 7 and (t["entry"] > t["zone_proximal"]) == (t["side"] == "demand")   # it closed back out of the zone
    print("trades by entry type:", {k: sum(1 for t in trades if t["entry_type"] == k) for k in ("TYPE_1", "TYPE_3")})


def test_no_lookahead_future_candles_do_not_change_past_trades():
    """Cutting the data off earlier must leave every earlier, already closed trade exactly as it was."""
    cut = datetime(2026, 9, 1, 23, 59)
    with SessionLocal() as db:
        full = run_one(db, KEY, MODE, START, END, OPTS)
        short = run_one(db, KEY, MODE, START, cut, OPTS)
    pick = lambda rows: sorted((t["entry_ts"], t["side"], t["entry"], t["stop"], t["target"], t["exit_ts"], t["result"])
                               for t in rows if t["result"] != "open" and t["exit_ts"] <= cut)
    assert pick(short) and pick(short) == pick(full)


def test_minimum_score_only_removes_trades():
    with SessionLocal() as db:
        every = run_one(db, KEY, MODE, START, END, OPTS)
        type1 = run_one(db, KEY, MODE, START, END, {**OPTS, "min_score": 7})
    assert len(type1) <= len(every) and all(t["score"] >= 7 and t["label"] == "TYPE_1" for t in type1)


def test_each_entry_type_on_its_own():
    with SessionLocal() as db:
        got = {k: run_one(db, KEY, MODE, START, END, {**OPTS, "entry_plan": k}) for k in ("type1", "type2", "type3")}
        rule = run_one(db, KEY, MODE, START, END, OPTS)
    for k, kind in (("type1", "TYPE_1"), ("type2", "TYPE_2"), ("type3", "TYPE_3")):
        assert got[k] and all(t["entry_type"] == kind for t in got[k]), k
        for t in got[k]:
            check_common(t)
    for t in got["type2"]:         # it opened inside the zone, at a score of 5 to 6.5
        lo, hi = sorted((t["zone_proximal"], t["zone_distal"]))
        assert lo <= t["entry"] <= hi and 5 <= t["score"] < 7
    key = lambda t: (t["entry_ts"], t["entry_type"], t["entry"])
    assert {key(t) for t in rule if t["entry_type"] == "TYPE_1"} == set(map(key, got["type1"]))
    # A zone is traded once. Under the rule a zone entered by type 1 on its first test is not entered again on its second,
    # so the rule's type 3 trades are some of the trades that type 3 on its own takes.
    assert {key(t) for t in rule if t["entry_type"] == "TYPE_3"} <= set(map(key, got["type3"]))
    print("trades:", {k: len(v) for k, v in got.items()})


def test_summary_breakdowns():
    with SessionLocal() as db:
        trades = run_one(db, KEY, MODE, START, END, OPTS)
    s = summarize(trades)
    closed = [t for t in trades if t["result"] != "open"]
    for name in ("by_label", "by_mode", "by_curve", "by_score", "by_timeframe"):
        assert sum(v["trades"] for v in s[name].values()) == len(closed), name
    assert set(s["by_mode"]) == {MODE} and set(s["by_label"]) <= {"TYPE_1", "TYPE_2_3", "REJECT"}
    assert s["reached"]["1"] >= s["reached"]["2"] >= sum(1 for t in closed if t["result"] == "win")


def test_backtest_api_run_results_export_and_missing_data():
    with TestClient(app) as c:
        assert c.get("/backtest").status_code == 200
        body = {"symbols": [KEY], "modes": ["HOURLY", "DAILY"], "start": "2026-07-01", "end": "2026-10-18", "min_score": 0}
        assert c.post("/api/backtest/run", json={**body, "symbols": ["NOTHINGUSDT"]}).status_code == 404     # not a coin at all
        bad = c.post("/api/backtest/run", json={**body, "symbols": ["ETHUSDT"]})                             # a coin with no candles stored
        assert bad.status_code == 400 and "Sync them" in bad.json()["detail"]
        assert c.post("/api/backtest/run", json={**body, "modes": ["FORTNIGHT"]}).status_code == 400
        assert c.post("/api/backtest/run", json={**body, "entry_plan": "guess"}).status_code == 400
        run_id = c.post("/api/backtest/run", json=body).json()["run_id"]
        for _ in range(160):
            r = c.get(f"/api/backtest/{run_id}").json()
            if r["status"] != "running":
                break
            time.sleep(0.5)
        assert r["status"] == "done", r
        s = r["summary"]
        assert s["trades"] > 0 and len(r["trades"]) == s["trades"] + s["open_trades"]
        assert set(s["by_mode"]) <= {"HOURLY", "DAILY"} and len(s["equity"]) == s["trades"]
        assert r["settings"]["entry_plan"] == "rule" and r["trades"][0]["mode"] in ("HOURLY", "DAILY")
        assert (r["settings"]["fee_maker_pct"], r["settings"]["fee_taker_pct"], r["settings"]["include_funding"]) == (0.02, 0.05, True)
        assert all(t["fees_r"] < 0 for t in r["trades"])
        csv_text = c.get(f"/api/backtest/{run_id}/export").text
        assert csv_text.splitlines()[0].startswith("instrument_key,timeframe,side,score,entry,stop,target")
        assert len(csv_text.splitlines()) == len(r["trades"]) + 1
        assert c.get("/api/backtest/runs").json()[0]["id"] == run_id
        assert c.post(f"/api/backtest/{run_id}/cancel").json()["status"] == "done"
