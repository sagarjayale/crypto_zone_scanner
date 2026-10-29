"""The scanner.py v6.4 marking (the default): the app's engine is run next to the zone functions of scanner.py itself
(tests/reference_scanner_v64.py, copied unchanged) on the same candles, and both must find the same zones with the same
lines, tests and scores."""
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.config import DEFAULTS
from app.engine import gtf, lifecycle, window
from app.engine.core import TimeframeEngine
from app.engine.score import ema_inside, htf_score, ltf_score
from tests import reference_scanner_v64 as ref

TH = dict(DEFAULTS, marking_rule="scanner", zone_history="all")


def random_candles(seed: int, n: int = 1500) -> pd.DataFrame:
    """Candles around 100 with many base candles, long base runs, gaps and doji-like candles."""
    rng = np.random.default_rng(seed)
    rows, px, run = [], 100.0, 0
    for _ in range(n):
        if run == 0 and rng.random() < 0.03:
            run = int(rng.integers(6, 16))                      # a long run of base candles
        size = abs(rng.normal(1.2, 0.5)) + 0.2
        share = rng.uniform(0.05, 0.45) if (run > 0 or rng.random() < 0.45) else rng.uniform(0.55, 0.95)
        run = max(run - 1, 0)
        sign = 1 if rng.random() < 0.5 + (100 - px) / 60 else -1   # drifts back towards 100
        o = px + (rng.normal(0, 0.8) if rng.random() < 0.08 else 0.0)
        body = sign * share * size
        c = o + body
        up = (size - abs(body)) * rng.random()
        rows.append((o, max(o, c) + up, min(o, c) - (size - abs(body) - up), c))
        px = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"]).round(2)
    df["ts"] = pd.date_range("2020-01-01", periods=len(df), freq="D")
    return df


def with_live(df: pd.DataFrame) -> pd.DataFrame:
    """scanner.py always treats the last row as the running candle and leaves it out, so one is added for it."""
    live = df.iloc[[-1]].copy()
    live["ts"] = live["ts"] + pd.Timedelta(days=1)
    return pd.concat([df, live], ignore_index=True)


def app_zones(df: pd.DataFrame):
    eng = TimeframeEngine(df, "1d", TH)
    for i in range(len(eng)):
        eng.step(i)
    index = {t: k for k, t in enumerate(eng.ts)}
    # A zone whose last candle closed inside it is kept for one more candle (entry types 2 and 3); scanner.py drops it at once.
    return eng, [(z, index[z.created_ts]) for z in eng.active if not z.inside_armed]


@pytest.mark.parametrize("seed", range(1, 25))
def test_htf_zones_are_the_same_as_scanner_py(seed):
    df = random_candles(seed)
    demand, supply = ref.detect_htf_zones(with_live(df))
    theirs = {(side, z["formed_at"], z["proximal"], z["distal"], z["pattern"], z["wick_tests"], z["score"])
              for side, zones in (("demand", demand), ("supply", supply)) for z in zones}
    _, mine = app_zones(df)
    ours = {(z.side, k, round(z.proximal, 4), round(z.distal, 4), z.pattern, z.touches, htf_score(z, TH)[0]) for z, k in mine}
    assert len(theirs) >= 3
    assert ours == theirs


@pytest.mark.parametrize("seed", range(1, 25))
def test_ltf_execution_zones_are_the_same_as_scanner_py(seed):
    df = random_candles(seed)
    live = with_live(df)
    eng, mine = app_zones(df)
    emas = eng.emas(len(eng) - 1)
    for side, want, htf_zone, cmp in (("BUY", "demand", {"proximal": 0.0, "distal": 0.0}, 150.0),
                                      ("SELL", "supply", {"proximal": 1000.0, "distal": 1000.0}, 80.0)):
        found = ref.ltf_execution_zones(live, side, htf_zone, cmp)
        theirs = {(z["legout_idx"], z["proximal"], z["distal"], z["wick_tests"], z["score"] - z["ema_bonus"]) for z in found}
        ours = set()
        for z, k in mine:
            if z.side != want or not gtf.ltf_ok(z) or lifecycle.counted_tests(z, TH) > TH["max_wick_tests"]:
                continue
            if not (cmp >= z.proximal >= 0) if side == "BUY" else not (cmp <= z.proximal <= 1000):
                continue
            score = ltf_score(z, emas, TH)[0] - (1.0 if ema_inside(z, emas[0]) else 0.0)
            ours.add((k, round(z.proximal, 2), round(z.distal, 2), z.touches, score))
        assert ours == theirs, side


def test_a_long_base_run_is_cut_like_scanner_py():
    lead = [(110, 111, 99, 100)]                                          # a red candle before the run
    base = [(100, 101, 99, 100.3), (100.3, 101.2, 99.4, 100.1)]
    out = [(100, 106, 99.8, 105.8)]                                       # an exciting green leg-out
    for k, expect in ((5, 5), (6, 6), (7, None), (8, 1), (13, 6), (14, None), (15, 1)):
        rows = lead + [base[n % 2] for n in range(k)] + out
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
        df["ts"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
        demand, _ = ref.detect_htf_zones(with_live(df))
        _, mine = app_zones(df)
        assert [z.base_count for z, _ in mine] == ([expect] if expect else []), k
        assert [z["bases"] for z in demand] == ([expect] if expect else []), k


def test_a_close_inside_uses_the_zone_up_one_candle_later_and_tests_count_per_candle():
    rows = [(110, 111, 99, 100), (100, 101, 99, 100.3), (100, 106, 99.8, 105.8),   # demand 100.3 to 99
            (105.8, 107, 104, 106), (106, 106.5, 100.2, 104), (104, 105, 100.1, 103),   # two candles in a row reach 100.3
            (103, 104, 99.6, 99.9),                                                  # closes inside the zone
            (99.9, 102, 99.5, 101.5)]                                                # opens inside, closes back out
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
    eng = TimeframeEngine(df, "1d", TH)
    first = None
    seen = []
    for i in range(len(eng)):
        events = eng.step(i)
        first = first or next((e["zone"] for e in events if e["type"] == "created"), None)
        seen.append([e["type"] for e in events if e["zone"] is first and e["type"] != "created"])
    z = first
    assert seen[4] == ["touched"] and seen[5] == ["touched_again"] and seen[6] == ["touched_again", "closed_inside"]
    assert seen[7] == ["entry2", "entry3", "consumed"]                      # types 2 and 3 still come, then the zone is gone
    assert (z.touches, z.status, z.t2_entry, z.t3_entry) == (3, "consumed", 99.9, 101.5)
    # A close beyond the distal line does not end a zone under scanner.py: it is ignored while price is beyond it.
    rows = rows[:3] + [(105.8, 107, 104, 106), (106, 106.2, 97, 98), (98, 98.5, 97.5, 98.2)]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["ts"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
    eng = TimeframeEngine(df, "1d", TH)
    for i in range(len(eng)):
        eng.step(i)
    assert [(z.status, z.touches) for z in eng.active if z.side == "demand"] == [("tested", 2)]
    nd, _ = gtf.nearest_zones(eng.active, 98.2, TH)
    assert nd is None                                                        # price is below its distal line


def test_history_windows_for_crypto():
    th, end = dict(TH, zone_history="scanner"), date(2026, 10, 12)
    s = lambda mode, tf: window.start(mode, tf, end, th)
    stop = datetime(2026, 10, 12, 23, 59, 59)
    assert s("15M", "1m") == stop - timedelta(days=14) and s("HOURLY", "5m") == stop - timedelta(days=30) and s("4HR", "15m") == stop - timedelta(days=30)
    assert s("DAILY", "30m") == stop - timedelta(days=90) and s("4HR", "4h") == stop - timedelta(days=90) and s("HOURLY", "1h") == stop - timedelta(days=365)
    assert s("DAILY", "1d") is None and s("MONTHLY", "1mo") is None and s("YEARLY", "1y") is None      # day and above: everything stored
    assert window.start("DAILY", "30m", end, TH) is None                      # zone_history "all"
    zones = [type("Z", (), {"base_start_ts": datetime(2026, 7, 14)})(), type("Z", (), {"base_start_ts": datetime(2026, 7, 15)})()]
    assert len(window.keep(zones, window.start("DAILY", "30m", end, th))) == 1


def test_the_whole_app_runs_on_the_scanner_py_marking_and_windows():
    """Sync, scan, results for every mode and a scan-date check, with the scanner.py marking and history windows on."""
    import asyncio
    from sqlalchemy import select
    from app import settings_store
    from app.config import MODES
    from app.data import sync as data_sync
    from app.db import SessionLocal
    from app.engine import result, scanner
    from app.models import Setting, Zone
    from app.sim import scancheck
    from tests.fake_binance import FakeBinance
    from tests.test_sync_scan import LISTED, NOW

    key = "SCANNERPYUSDT"
    with SessionLocal() as db:
        settings_store.put(db, "marking_rule", "scanner")
        settings_store.put(db, "zone_history", "scanner")
        try:
            asyncio.run(data_sync.sync_symbol(db, FakeBinance(now=NOW, listed=LISTED), key, now=NOW))
            scanner.scan_symbol(db, key, now=NOW)
            zones = db.scalars(select(Zone).where(Zone.instrument_key == key)).all()
            assert zones and {z.status for z in zones} <= {"fresh", "tested", "consumed"}       # nothing is violated under scanner.py
            assert all(z.distal_from == "base" for z in zones)
            for mode in MODES:
                r = result.build(db, key, mode)
                assert r["signal"]["signal"] in ("BUY", "SELL", "NO_TRADE")
                for z in (r["demand_zone"], r["supply_zone"], r["ltf_zone"]):
                    assert z is None or z["strength_label"].startswith(("gap", "strong leg-out", "normal leg-out"))
            chk = scancheck.check(db, key, "DAILY", date(2026, 9, 1), {"include_funding": False})
            assert chk["scan_ts"] is not None and set(chk["outcomes"]) == {"type1", "type2", "type3"}
        finally:
            scanner.clear_key(db, key)                 # later tests count every zone and plan in the database
            for k in ("marking_rule", "zone_history"):
                settings_store.put(db, k, {"marking_rule": "pdf", "zone_history": "all"}[k])


def test_the_nearest_htf_zones_are_the_nearest_never_tested_ones():
    """GTF PDF page 31: from the price, the nearest fresh demand below and the nearest fresh supply above. The example
    of the logic: demand A 990 tested 3 times, demand B 900 fresh, supply C 1,020 tested once, supply D 1,060 fresh."""
    from types import SimpleNamespace as Z
    mk = lambda side, prox, dist, touches: Z(side=side, proximal=prox, distal=dist, touches=touches, status="tested" if touches else "fresh",
                                           in_visit=False, base_count=2, has_gap=False, legout_body=0.8, legout_count=1)
    zones = [mk("demand", 990, 980, 3), mk("demand", 900, 880, 0), mk("supply", 1020, 1030, 1), mk("supply", 1060, 1075, 0)]
    nd, ns = gtf.nearest_zones(zones, 1000, TH)
    assert (nd.proximal, ns.proximal, gtf.curve(1000, nd, ns)) == (900, 1060, "EQUILIBRIUM")
    nd, ns = gtf.nearest_zones(zones, 1000, dict(TH, htf_zone_pick="any"))                 # scanner.py: tested zones count too
    assert (nd.proximal, ns.proximal, gtf.curve(1000, nd, ns)) == (990, 1020, "LOW")
    nd, _ = gtf.nearest_zones(zones, 895, TH)                                              # price inside the fresh demand B
    assert nd.proximal == 900 and gtf.curve(895, nd, ns) == "VERY LOW"
    zones[1].touches = 1                                                                   # once an HTF candle has closed touching it
    nd, _ = gtf.nearest_zones(zones, 895, TH)
    assert nd is None
