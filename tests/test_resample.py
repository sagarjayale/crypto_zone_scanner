"""Candle boundaries in UTC, and quarter, half year and year built from closed monthly candles."""
from datetime import datetime

import pandas as pd

from app.data.resample import close_time, resample_monthly
from app.jobs.scheduler import closed_timeframes


def months(start: str, end: str) -> pd.DataFrame:
    ts = pd.date_range(start, end, freq="MS")
    return pd.DataFrame({"ts": ts, "open": range(100, 100 + len(ts)), "high": [200.0] * len(ts), "low": [50.0] * len(ts),
                         "close": range(101, 101 + len(ts)), "volume": 10, "oi": 0})


def test_close_times_are_utc_and_round_the_clock():
    assert close_time(datetime(2026, 10, 18, 23, 55), "5m") == datetime(2026, 10, 19, 0, 0)       # no session end
    assert close_time(datetime(2026, 10, 17), "1d") == datetime(2026, 10, 18)                     # a Saturday is a day like any other
    assert close_time(datetime(2026, 10, 12), "1w") == datetime(2026, 10, 19)                     # Monday to Monday
    assert close_time(datetime(2026, 2, 1), "1mo") == datetime(2026, 3, 1)
    assert close_time(datetime(2026, 10, 1), "3mo") == datetime(2027, 1, 1)
    assert close_time(datetime(2026, 7, 1), "6mo") == datetime(2027, 1, 1)
    assert close_time(datetime(2026, 1, 1), "1y") == datetime(2027, 1, 1)


def test_quarter_half_year_and_year_only_when_their_last_month_has_closed():
    df = months("2024-02-01", "2026-09-01")            # listed in February 2024; September 2026 is the last closed month
    now = datetime(2026, 10, 18, 12, 0)
    q = resample_monthly(df, "3mo", now)
    assert q["ts"].iloc[0] == pd.Timestamp("2024-01-01") and q["ts"].iloc[-1] == pd.Timestamp("2026-07-01")
    first = q.iloc[0]
    assert (first["open"], first["close"], first["volume"]) == (100, 102, 20)     # built from February and March only
    h = resample_monthly(df, "6mo", now)
    assert list(h["ts"]) == [pd.Timestamp(x) for x in ("2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01", "2026-01-01")]
    y = resample_monthly(df, "1y", now)
    assert list(y["ts"]) == [pd.Timestamp("2024-01-01"), pd.Timestamp("2025-01-01")]     # 2026 is still forming
    assert len(resample_monthly(df, "3mo", datetime(2026, 9, 30, 23, 59))) == len(q) - 1   # Q3 closes at 1 October 00:00 UTC


def test_closed_timeframes_by_the_minute():
    assert closed_timeframes(datetime(2026, 10, 18, 14, 7, 8)) == ["1m"]
    assert closed_timeframes(datetime(2026, 10, 18, 14, 15, 8)) == ["1m", "5m", "15m"]
    assert closed_timeframes(datetime(2026, 10, 18, 16, 0, 8)) == ["1m", "5m", "15m", "30m", "1h", "2h", "4h"]
    assert closed_timeframes(datetime(2026, 10, 19, 0, 0, 8)) == ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1d", "1w"]   # a Monday
    assert closed_timeframes(datetime(2026, 11, 1, 0, 0, 8))[-2:] == ["1d", "1mo"]
    assert closed_timeframes(datetime(2026, 10, 18, 14, 7, 8), ["DAILY", "MONTHLY"]) == []       # nothing reads 1 minute candles
