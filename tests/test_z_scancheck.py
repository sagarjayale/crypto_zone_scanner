"""Scan-date check on the candles created by test_sync_scan (the file name sorts after it on purpose)."""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.sim import scancheck
from tests.test_sync_scan import KEY


def find_dates(db, mode, want):
    """Scan dates on which the mode gave a trade, searching a few months of the stored data."""
    out, day = [], date(2026, 7, 1)
    while day < date(2026, 10, 17):
        r = scancheck.check(db, KEY, mode, day)
        if r["signal"]["signal"] != "NO_TRADE":
            out.append(r)
            if len(out) >= want:
                return out
        day += timedelta(days=2)
    return out


def test_uses_only_candles_up_to_the_scan_date_and_follows_each_plan():
    with SessionLocal() as db:
        rows = find_dates(db, "HOURLY", 14)
        assert rows, "no trade found on any sampled scan date"
        for r in rows:
            scan = datetime.fromisoformat(r["scan_ts"])
            assert scan.date() <= date.fromisoformat(r["scan_date"]) and r["ltf"] == "5m"
            z = r["ltf_zone"]
            assert datetime.fromisoformat(z["created_ts"]) <= scan                     # the zone existed on the scan date
            for name in ("demand_zone", "supply_zone"):
                if r[name]:
                    assert datetime.fromisoformat(r[name]["created_ts"]) <= scan
            assert set(r["outcomes"]) == {"type1", "type2", "type3"}
            score, sig = z["score"], r["signal"]
            assert score >= 5 and sig["primary_entry"] == ("TYPE_1" if score >= 7 else "TYPE_3")
            for plan, o in r["outcomes"].items():
                assert o["status"] in ("target hit", "stopped", "open", "never entered", "not taken")
                if o["status"] in ("target hit", "stopped", "open"):
                    assert datetime.fromisoformat(o["entry_ts"]) > scan and datetime.fromisoformat(o["exit_ts"]) >= datetime.fromisoformat(o["entry_ts"])
                    assert (o["stop"] < o["entry"] < o["target"]) if z["side"] == "demand" else (o["stop"] > o["entry"] > o["target"])
                    assert o["stop"] == z["distal"] and abs(abs(o["target"] - o["entry"]) - 2 * abs(o["entry"] - o["stop"])) < 1e-6
                if o["status"] in ("target hit", "stopped", "open"):
                    assert o["fees_r"] < 0
                if o["status"] == "target hit":     # 2 R less the fees: on a thin zone the fees can take most of it
                    assert abs(o["result_r"] - (2.0 + o["fees_r"] + o["funding_r"])) < 0.02 and o["exit_price"] == o["target"]
                if o["status"] == "stopped":
                    assert o["result_r"] <= -1.0
            one, two, three = (r["outcomes"][k] for k in ("type1", "type2", "type3"))
            if score >= 7:                 # the score takes entry type 1: types 2 and 3 are not followed
                assert two["status"] == three["status"] == "not taken" and "takes entry type 1" in two["note"]
                if one["status"] in ("target hit", "stopped", "open"):
                    assert one["entry"] == z["proximal"]
            else:                          # 5 to 6.5: entry types 2 and 3
                assert one["status"] == "not taken" and "takes entry type 2 or 3" in one["note"]
                lo, hi = sorted((z["proximal"], z["distal"]))
                if two["status"] in ("target hit", "stopped", "open"):
                    assert lo <= two["entry"] <= hi                                    # it opened inside the zone
                if three["status"] in ("target hit", "stopped", "open"):
                    assert (three["entry"] > z["proximal"]) == (z["side"] == "demand")   # it closed back out of the zone
        s = scancheck.summarize(rows)
        for k in ("type1", "type2", "type3"):
            assert s[k]["entered"] == s[k]["target_hit"] + s[k]["stopped"] + s[k]["open"]
            assert s[k]["entered"] + s[k]["never_entered"] + s[k]["not_taken"] == len(rows)
            assert s[k]["fees_r"] <= 0
        assert sum(s[k]["entered"] for k in s) > 0, "no entry was taken on any sampled scan date"
        print("scan-date outcomes:", {k: (v["entered"], v["target_hit"], v["stopped"]) for k, v in s.items()})


def test_dates_outside_the_data_and_the_api():
    with SessionLocal() as db:
        early = scancheck.check(db, KEY, "HOURLY", date(2020, 1, 1))
        assert early["signal"]["signal"] == "NO_TRADE" and "Stored candles start" in early["signal"]["reason"]
        missing = scancheck.check(db, "NOTHINGUSDT", "MONTHLY", date(2026, 8, 1))
        assert "missing" in missing["signal"]["reason"]
    with TestClient(app) as c:
        out = c.post("/api/scancheck", json={"symbols": [KEY], "mode": "HOURLY", "scan_date": "2026-08-10"}).json()
        assert out["mode"] == "HOURLY" and len(out["rows"]) == 1 and out["rows"][0]["symbol"] and set(out["summary"]) == {"type1", "type2", "type3"}
        assert c.post("/api/scancheck", json={"symbols": [KEY], "mode": "NOPE", "scan_date": "2026-08-10"}).status_code == 400


def test_checks_are_saved_listed_and_reopened():
    with TestClient(app) as c:
        body = {"symbols": [KEY], "mode": "HOURLY", "scan_date": "2026-08-10"}
        assert c.post("/api/scancheck", json=body).json()["id"] is None                  # the chart's own look-ups are not saved
        before = len(c.get("/api/scanchecks").json())
        saved = c.post("/api/scancheck", json={**body, "save": True}).json()
        assert saved["id"] and len(c.get("/api/scanchecks").json()) == before + 1
        head = c.get("/api/scanchecks").json()[0]
        assert (head["id"], head["mode"], head["scan_date"], head["coins"]) == (saved["id"], "HOURLY", "2026-08-10", 1)
        assert set(head["summary"]) == {"type1", "type2", "type3"}
        again = c.get(f"/api/scancheck/{saved['id']}").json()
        assert again["rows"] == saved["rows"] and again["summary"] == saved["summary"]    # reopened exactly as it was reported
        assert again["costs"] == {"fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False, "include_funding": True}
        assert saved["id"] not in [r["id"] for r in c.get("/api/backtest/runs").json()]   # kept apart from date-range runs
        assert c.delete(f"/api/scancheck/{saved['id']}").json() == {"deleted": saved["id"]}
        assert c.get(f"/api/scancheck/{saved['id']}").status_code == 404


def test_candles_can_be_read_forward_from_a_date():
    with TestClient(app) as c:
        fwd = c.get(f"/api/candles/{KEY}", params={"tf": "5m", "start": "2026-08-10T00:00:00", "limit": 80, "first": True}).json()
        assert len(fwd) == 80 and fwd[0]["ts"].startswith("2026-08-10 00:00") and fwd[-1]["ts"].startswith("2026-08-10 06:35")
        last = c.get(f"/api/candles/{KEY}", params={"tf": "5m", "start": "2026-08-10T00:00:00", "limit": 80}).json()
        assert last[-1]["ts"] > fwd[-1]["ts"]                                               # without first, the newest are returned