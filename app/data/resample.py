"""Candle boundaries in UTC, as Binance builds them, and the three timeframes Binance does not have.

Crypto trades around the clock: no sessions, no holidays. A day runs 00:00 to 24:00 UTC, a week starts on Monday
00:00 UTC, a month on the 1st at 00:00 UTC. Quarter, half year and year are built from closed monthly candles and only
returned once their last month has closed, so the engine never sees a candle that is still forming."""
from datetime import datetime, timedelta

import pandas as pd

from ..config import TF_MINUTES

AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "oi": "last"}
MONTHS = {"1mo": 1, "3mo": 3, "6mo": 6, "1y": 12}


def _add_months(ts: datetime, n: int) -> datetime:
    m = ts.month - 1 + n
    return ts.replace(year=ts.year + m // 12, month=m % 12 + 1, day=1)


def period_start(ts: pd.Series, tf: str) -> pd.Series:
    """Start of the quarter, half year or year that each month belongs to (calendar periods, UTC)."""
    size = MONTHS[tf]
    month = ((ts.dt.month - 1) // size) * size + 1
    return pd.to_datetime({"year": ts.dt.year, "month": month, "day": 1})


def resample_monthly(df: pd.DataFrame, tf: str, now: datetime) -> pd.DataFrame:
    """Monthly candles -> quarter, half year or year. A period counts only once its last month has closed, and only if
    all its months are stored (the first period of a coin listed mid-quarter is built from the months that exist)."""
    if df.empty:
        return df
    d = df.copy()
    d["ts"] = period_start(d["ts"], tf).values
    out = d.groupby("ts", as_index=False).agg(AGG)
    done = [close_time(t.to_pydatetime(), tf) <= now for t in out["ts"]]
    return out[done].reset_index(drop=True)


def close_time(ts: datetime, tf: str) -> datetime:
    """When the candle that opens at ts closes. Used to make sure a lower timeframe never reads a higher-timeframe
    candle before it has closed, and to drop candles that are still forming."""
    if tf in TF_MINUTES:
        return ts + timedelta(minutes=TF_MINUTES[tf])
    return _add_months(ts, MONTHS[tf])


def open_after(ts: datetime, tf: str) -> datetime:
    """Open time of the candle after the one that opens at ts."""
    return close_time(ts, tf)
