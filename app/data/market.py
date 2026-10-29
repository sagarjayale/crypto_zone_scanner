"""Coins, funding and open interest from Binance, stored for the scanner, the confirmation score and the cost model.

Coins: GET /fapi/v1/exchangeInfo, keeping contractType PERPETUAL, quoteAsset USDT and status TRADING, with the price
and quantity filters each coin has. The funding interval comes from /fapi/v1/fundingInfo (coins missing there settle
every 8 hours). The 24 hour USDT volume from /fapi/v1/ticker/24hr ranks them for "top N by volume"."""
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..binance.client import from_ms
from ..models import FundingRate, Instrument, OpenInterest
from .sync import now_utc


def _filters(sym: dict) -> dict:
    out = {}
    for f in sym.get("filters", []):
        t = f.get("filterType")
        if t == "PRICE_FILTER":
            out["tick_size"] = float(f["tickSize"])
        elif t == "LOT_SIZE":
            out["step_size"], out["min_qty"] = float(f["stepSize"]), float(f["minQty"])
        elif t == "MIN_NOTIONAL":
            out["min_notional"] = float(f.get("notional") or f.get("minNotional") or 0)
    return out


def perpetuals(info: dict) -> list[dict]:
    return [s for s in info.get("symbols", []) if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"]


async def store_instruments(db: Session, client) -> int:
    """Refresh every USDT perpetual that is trading. Coins that stopped trading are kept but marked, so their history and
    zones stay readable. The database part runs in a worker thread."""
    info = await client.exchange_info()
    try:
        hours = {r["symbol"]: int(r.get("fundingIntervalHours") or 8) for r in await client.funding_info()}
    except Exception:          # the funding interval is a nicety; the coin list must not fail because of it
        hours = {}
    volume = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in await client.tickers_24h()}
    return await asyncio.to_thread(_store_instruments, db, info, hours, volume)


def _store_instruments(db: Session, info: dict, hours: dict, volume: dict) -> int:
    now, live = now_utc(), perpetuals(info)
    keep = {s["symbol"] for s in live}
    have = {i.instrument_key: i for i in db.scalars(select(Instrument)).all()}      # one query, not one per coin
    for inst in have.values():
        if inst.instrument_key not in keep:
            inst.status = "NOT TRADING"
    for s in live:
        key = s["symbol"]
        inst = have.get(key) or Instrument(instrument_key=key)
        inst.symbol, inst.name, inst.status = key, s.get("baseAsset"), "TRADING"
        for k, v in _filters(s).items():
            setattr(inst, k, v)
        inst.onboard_date = from_ms(s["onboardDate"]) if s.get("onboardDate") else None
        inst.funding_hours = hours.get(key, 8)
        inst.quote_volume = volume.get(key)
        inst.updated_at = now
        if key not in have:
            db.add(inst)
    db.commit()
    return len(live)


def top_symbols(db: Session, n: int) -> list[str]:
    rows = db.scalars(select(Instrument.instrument_key).where(Instrument.status == "TRADING", Instrument.quote_volume.isnot(None))
                      .order_by(Instrument.quote_volume.desc()).limit(n)).all()
    return list(rows)


async def store_premium(db: Session, client) -> int:
    """Mark price and the current funding rate of every stored coin (one call for all of them, stored in a worker thread)."""
    rows = await client.premium_index()
    return await asyncio.to_thread(_store_premium, db, rows)


def _store_premium(db: Session, rows: list[dict]) -> int:
    n = 0
    have = {i.instrument_key: i for i in db.scalars(select(Instrument)).all()}
    for r in rows:
        inst = have.get(r.get("symbol"))
        if inst is None:
            continue
        inst.mark_price = float(r["markPrice"]) if r.get("markPrice") else None
        inst.funding_rate = float(r["lastFundingRate"]) if r.get("lastFundingRate") not in (None, "") else None
        inst.next_funding = from_ms(r["nextFundingTime"]) if r.get("nextFundingTime") else None
        n += 1
    db.commit()
    return n


def _upsert(db: Session, model, rows: list[dict], keys: tuple[str, ...]) -> int:
    """Insert rows, replacing any with the same key, in batches (MySQL, PostgreSQL or SQLite)."""
    if not rows:
        return 0
    from .sync import upsert_statement
    stmt = upsert_statement(db.bind.dialect.name, model, keys, [c for c in rows[0] if c not in keys])
    for i in range(0, len(rows), 2000):
        db.execute(stmt, rows[i:i + 2000])
        db.commit()
    return len(rows)


async def sync_funding(db: Session, client, key: str) -> int:
    """Funding settlements since the last stored one (from the listing date the first time, 1,000 per call)."""
    last = db.scalar(select(func.max(FundingRate.ts)).where(FundingRate.instrument_key == key))
    inst = db.get(Instrument, key)
    start = last + timedelta(seconds=1) if last else (inst.onboard_date if inst and inst.onboard_date else datetime(2019, 9, 1))
    total = 0
    for _ in range(50):
        db.commit()
        rows = await client.funding_history(key, start=start)
        if not rows:
            break
        total += await asyncio.to_thread(_upsert, db, FundingRate, [{"instrument_key": key, **r} for r in rows], ("instrument_key", "ts"))
        if len(rows) < 1000:
            break
        start = rows[-1]["ts"] + timedelta(seconds=1)
    return total


async def sync_open_interest(db: Session, client, key: str) -> int:
    """Open interest every 5 minutes (about the last 41 hours) and every hour (about the last 20 days). Binance keeps
    only about 30 days, so this is saved as it comes and the history here grows over time. Only the readings since the
    last stored one are asked for."""
    total = 0
    now = now_utc()
    for period, step in (("5m", timedelta(minutes=5)), ("1h", timedelta(hours=1))):
        last = db.scalar(select(func.max(OpenInterest.ts)).where(OpenInterest.instrument_key == key, OpenInterest.period == period))
        db.commit()
        limit = 500 if last is None else max(2, min(500, int((now - last) / step) + 2))
        rows = await client.open_interest_hist(key, period, limit)
        rows = [r for r in rows if last is None or r["ts"] > last]
        total += await asyncio.to_thread(_upsert, db, OpenInterest, [{"instrument_key": key, "period": period, **r} for r in rows], ("instrument_key", "period", "ts"))
    return total
