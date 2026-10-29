"""Export stored candles (and funding) to compressed CSV files, so a backtest can be run elsewhere on the same data.
Run from the crypto_zone_scanner folder with the app's virtual environment:
    python scripts/export_candles.py                 # top 20 coins, the timeframes of the 2HR, 4HR and DAILY modes
    python scripts/export_candles.py --top 50 --with-1m
It reads the database named in .env and writes candles_export/*.csv.gz plus candles_export.zip. Nothing is changed."""
import argparse
import os
import sys
import zipfile
from datetime import timedelta

sys.path.insert(0, os.getcwd())
import pandas as pd                                     # noqa: E402
from sqlalchemy import select                           # noqa: E402

from app.data.market import top_symbols                 # noqa: E402
from app.data.sync import now_utc                       # noqa: E402
from app.db import SessionLocal                         # noqa: E402
from app.models import Candle, FundingRate, Instrument  # noqa: E402

DAYS = {"5m": 95, "15m": 185, "30m": 185, "1h": 400, "2h": 400, "4h": 400, "1d": 800, "1w": 2000, "1mo": 3000}

ap = argparse.ArgumentParser()
ap.add_argument("--top", type=int, default=20)
ap.add_argument("--with-1m", action="store_true", help="also the 1-minute candles (large)")
args = ap.parse_args()
if args.with_1m:
    DAYS["1m"] = 65
os.makedirs("candles_export", exist_ok=True)
now = now_utc()
with SessionLocal() as db:
    keys = top_symbols(db, args.top)
    print("coins:", ", ".join(keys))
    for tf, days in DAYS.items():
        rows = db.execute(select(Candle.instrument_key, Candle.ts, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
                          .where(Candle.instrument_key.in_(keys), Candle.timeframe == tf, Candle.ts >= now - timedelta(days=days))).all()
        df = pd.DataFrame(rows, columns=["key", "ts", "open", "high", "low", "close", "volume"])
        df.to_csv(f"candles_export/{tf}.csv.gz", index=False, compression="gzip")
        print(f"{tf:4s} {len(df):9d} candles")
    fr = pd.DataFrame(db.execute(select(FundingRate.instrument_key, FundingRate.ts, FundingRate.rate, FundingRate.mark)
                                 .where(FundingRate.instrument_key.in_(keys))).all(), columns=["key", "ts", "rate", "mark"])
    fr.to_csv("candles_export/funding.csv.gz", index=False, compression="gzip")
    ins = pd.DataFrame(db.execute(select(Instrument.instrument_key, Instrument.onboard_date, Instrument.tick_size, Instrument.step_size,
                                         Instrument.min_qty, Instrument.min_notional, Instrument.quote_volume)
                                  .where(Instrument.instrument_key.in_(keys))).all(),
                       columns=["key", "onboard_date", "tick_size", "step_size", "min_qty", "min_notional", "quote_volume"])
    ins.to_csv("candles_export/instruments.csv.gz", index=False, compression="gzip")
with zipfile.ZipFile("candles_export.zip", "w", zipfile.ZIP_STORED) as z:
    for name in sorted(os.listdir("candles_export")):
        z.write(os.path.join("candles_export", name))
print("send me: candles_export.zip (%.1f MB)" % (os.path.getsize("candles_export.zip") / 1e6))
