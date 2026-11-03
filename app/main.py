"""FastAPI app: Binance data sync, zone engine and scan APIs, live prices, backtest, replay and paper trading.
Open /docs to call every endpoint by hand. Binance market data is public: no API key is used and no order is ever sent."""
import asyncio
from contextlib import asynccontextmanager
import csv
import io
from datetime import date, datetime, timedelta

from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, defer

import os

from . import config, live, settings_store, status
from .alerts import telegram
from .data import market
from .data import sync as data_sync
from .db import SessionLocal, engine, ensure_database, ensure_schema, get_db
from .engine import plan as plans
from .engine import eye
from .engine import result as results
from .jobs import scanpool
from .jobs import scheduler as jobs
from .jobs.runner import run_job, start_job
from .engine import scanner
from .engine.link import ACTIVE
from .models import (BacktestRun, BacktestTrade, Candle, Instrument, Job, ModeResult, PaperOrder, PaperTrade, Rejection, Setting,
                     SyncState, TradePlan, Zone, ZoneEvent)
from .sim import backtest as bt
from .sim import scancheck
from .sim import paper
from .sim.replay import SESSIONS, ReplaySession
from .binance.client import BinanceBanned, BinanceClient, BinanceError


client = BinanceClient()
# GTF-2: trade score and entry types 1, 2 and 3 from the course; a close inside a zone no longer ends it.
# GTF-3: zone marking as in the course: exciting leg-in, leg-out closing beyond the whole leg-in, exceptional distal line, gap candles.
# GTF-4: zones found, tested, used up and scored as scanner.py v6.4 does, with its history windows (the course marking stays a setting).
# GTF-5: the nearest HTF demand and supply must never have been tested (GTF PDF page 31); results and plans are rebuilt.
RULES_VERSION = "GTF-5"


RESET_TABLES = (ModeResult, ZoneEvent, TradePlan, Zone, Rejection)   # what a change of rules clears; candles are kept


def clear_zone_tables(report=lambda text: None, tries: int = 40, pause: float = 15.0) -> None:
    """Empty the tables a change of rules makes stale. On MySQL with TRUNCATE, which takes a moment whatever the size,
    waiting at most 30 seconds for a lock each time; if another connection holds the table, it waits and tries again
    (up to ten minutes) and says so on the job. On SQLite with DELETE."""
    import time as _time
    from sqlalchemy import text as sql
    from sqlalchemy.exc import OperationalError
    for model in RESET_TABLES:
        name = model.__tablename__
        for attempt in range(1, tries + 1):
            try:
                with engine.begin() as conn:
                    if engine.dialect.name == "mysql":
                        conn.execute(sql("SET SESSION lock_wait_timeout = 30, innodb_lock_wait_timeout = 30"))
                        try:
                            conn.execute(sql(f"TRUNCATE TABLE `{name}`"))
                        finally:
                            conn.execute(sql("SET SESSION lock_wait_timeout = DEFAULT, innodb_lock_wait_timeout = DEFAULT"))
                    else:
                        conn.execute(delete(model))
                break
            except OperationalError:
                if attempt == tries:
                    raise
                report(f"waiting for the {name} table: another database connection is holding it (try {attempt} of {tries}). "
                       "Stop any other copy of the app.")
                _time.sleep(pause)
        report(f"cleared {name}")


def reset_zones(db: Session, earlier: str | None = None) -> None:
    """Zones found by an earlier rule set do not carry what the current rules need, so they are cleared once.
    Candles are kept, and every zone is found again from them. Pending paper orders lose their zone and are cancelled.
    The tables are emptied by the background job (clear_zone_tables), never during start-up; here only the small things."""
    for order in db.scalars(select(PaperOrder).where(PaperOrder.status == "pending")).all():
        order.status, order.note = "cancelled", "zone rules changed"
    stale = ["strong_body_pct", "conservative_rr"]          # settings the current rules no longer have
    if earlier in (None, "GTF-1", "GTF-2", "GTF-3"):        # GTF-4 made scanner.py the default marking: earlier choices go back to it
        stale.append("marking_rule")
    if not (earlier or "").startswith("GTF"):              # thresholds saved before the GTF rules would be misread by them
        stale += ["leg_atr_mult", "max_width_atr", "stop_buffer_atr", "min_rr", "max_rr", "atr_period", "approach_atr",
                  "swing_bars", "move_open_targets", "min_score", "leg_body_pct", "base_body_pct"]
    for key in stale:
        row = db.get(Setting, key)
        if row is not None:
            db.delete(row)
    db.commit()


async def rescan_all() -> None:
    """After the rules changed: empty the zone tables, then find every zone again from the stored candles, in the
    background. No download is needed. The setting rules_reset_pending stays on until this has finished, so an app that
    was stopped half way does it again at the next start."""
    async def work(d, report):
        report("clearing the zones of the earlier rules")
        await asyncio.to_thread(clear_zone_tables, report)
        with SessionLocal() as db:
            for st in db.scalars(select(SyncState)).all():
                st.last_scanned_ts = None
            for model in (ZoneEvent, TradePlan, Zone):            # anything a scheduled scan wrote meanwhile
                db.execute(delete(model))
            db.commit()
            have = set(db.scalars(select(SyncState.instrument_key).where(SyncState.last_candle_ts.isnot(None))))
            keys = [k for k in universe(db) if k in have]      # a coin with no candles yet is left to its download
        if keys:
            await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, download=False), report)
        with SessionLocal() as db:
            row = db.get(Setting, "rules_reset_pending")
            if row is not None:
                db.delete(row)
                db.commit()
    await run_job("rebuild after a rule change", work)


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_database()
    ensure_schema()
    with SessionLocal() as db:
        earlier = settings_store.get(db, "engine_rules")
        if earlier is None and db.scalar(select(Zone.id).limit(1)) is None:     # a brand-new database: nothing to rebuild
            settings_store.put(db, "engine_rules", RULES_VERSION)
            earlier = RULES_VERSION
        if earlier != RULES_VERSION:            # only small changes here; the tables are emptied in the background
            reset_zones(db, earlier)
            settings_store.put(db, "rules_reset_pending", True)
            settings_store.put(db, "engine_rules", RULES_VERSION)
        rebuilt = bool(settings_store.get(db, "rules_reset_pending"))
        status.adopt_existing(db, list(settings_store.get(db, "universe") or []))
    sched = None
    if os.getenv("SCHEDULER_ENABLED", "1") == "1":   # set to 0 to run the pages and APIs without the schedule
        sched = jobs.build(client)
        sched.start()
        live.start_poller(client)
        jobs.start_up(client)       # the coin list on the first start, then a catch-up download and scan
    app.state.scheduler = sched
    if rebuilt:     # the zones were cleared for the new rules: find them again right away, nothing waits for it
        asyncio.get_running_loop().create_task(rescan_all())
    if sched and not rebuilt:   # zones used up long ago are cleared in the background (not while the rebuild empties the tables)
        asyncio.get_running_loop().create_task(asyncio.to_thread(jobs.prune_old_zones))
    if not rebuilt:             # once: remove the "touched again" rows earlier versions wrote, then index the alerts list
        asyncio.get_running_loop().create_task(jobs.tidy_events())
    yield
    if sched:
        sched.shutdown(wait=False)
    scanpool.shutdown()
    task = live.state.get("task")
    if task:
        task.cancel()
    await client.close()


app = FastAPI(title="Crypto zone scanner", version="1.0", lifespan=lifespan)
WEB = Path(__file__).parent / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/scanner")


@app.exception_handler(BinanceBanned)
async def _banned(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(BinanceError)
async def _binance_failed(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/paper", include_in_schema=False)
def paper_page():
    return FileResponse(WEB / "paper.html")


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(WEB / "settings.html")


@app.get("/scanner", include_in_schema=False)
def scanner_page():
    return FileResponse(WEB / "scanner.html")


@app.get("/chart", include_in_schema=False)
def chart_page():
    return FileResponse(WEB / "chart.html")


@app.get("/backtest", include_in_schema=False)
def backtest_page():
    return FileResponse(WEB / "backtest.html")


@app.get("/data", include_in_schema=False)
def data_page():
    return FileResponse(WEB / "data.html")


@app.get("/eye", include_in_schema=False)
def eye_page():
    return FileResponse(WEB / "eye.html")


@app.get("/premium", include_in_schema=False)
def premium_page():
    return FileResponse(WEB / "premium.html")


def row(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def resolve(db: Session, symbol: str) -> str:
    """Accepts a Binance symbol (BTCUSDT) or a base asset (BTC, which becomes BTCUSDT). A symbol already in the coin list
    or with stored candles is accepted even when the coin list has not been downloaded."""
    sym = symbol.strip().upper()
    for key in (sym, sym + "USDT"):
        if db.get(Instrument, key) is not None or key in universe(db) or db.scalar(select(Candle.ts).where(Candle.instrument_key == key).limit(1)) is not None:
            return key
    raise HTTPException(404, f"Unknown coin {symbol}. Use a Binance USDT perpetual symbol such as BTCUSDT, or refresh the coin list on the Data page.")


def ranked(db: Session, keys: list[str]) -> list[str]:
    """Coins of the list ordered by 24 hour volume, most traded first (coins without a volume last)."""
    vol = dict(db.execute(select(Instrument.instrument_key, Instrument.quote_volume).where(Instrument.instrument_key.in_(keys))).all()) if keys else {}
    return sorted(keys, key=lambda k: -(vol.get(k) or 0))


def names_for(db: Session, keys) -> dict:
    """Display name of each coin: its Binance symbol."""
    return {k: k for k in keys}


def universe(db: Session) -> list[str]:
    return list(settings_store.get(db, "universe") or [])


# ---------------------------------------------------------------- Binance (1)
@app.get("/api/binance/status", tags=["data"])
def binance_status():
    """Request weight used this minute (as Binance reports it), the limit and the share the app keeps to, and any ban."""
    return {**client.status(), "feed": live.feed_status(data_sync.now_utc())}


# ---------------------------------------------------------------- data (7)
def _keys(db: Session, symbols) -> list[str]:
    if symbols in (None, "all"):
        return universe(db)
    return [resolve(db, s) for s in symbols]


@app.post("/api/data/sync", tags=["data"])
async def data_sync_many(symbols: list[str] | str = Body("all"), timeframes: list[str] | None = Body(None),
                         repair: bool = Body(False), db: Session = Depends(get_db)):
    """Download what is missing and scan it, one instrument after another in the background. Each instrument
    becomes available on the pages as soon as it is done; the rest carry on. repair=true also re-fetches the
    gaps found in the last 30 days."""
    keys = _keys(db, symbols)

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, timeframes, repair), report)
    return {"job_id": start_job("sync and scan", work), "symbols": len(keys)}


@app.post("/api/data/backfill", tags=["data"])
async def data_backfill(symbols: list[str] | str = Body("all", embed=True), timeframes: list[str] | None = Body(None, embed=True),
                        db: Session = Depends(get_db)):
    """Full history download, ignoring the sync marker, followed by a scan. timeframes limits it to those timeframes, for
    example ["1m"] after raising the 1-minute history kept (the regular sync only adds candles after the last one)."""
    keys = _keys(db, symbols)
    if timeframes is not None and (not timeframes or any(tf not in config.FETCHED for tf in timeframes)):
        raise HTTPException(400, f"timeframes must be chosen from {list(config.FETCHED)}")

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, timeframes=timeframes, full=True,
                                                                   force=timeframes is None), report)
    label = "full download" if timeframes is None else f"full download of {', '.join(timeframes)}"
    return {"job_id": start_job(label, work), "symbols": len(keys)}


@app.post("/api/data/sync/{symbol:path}", tags=["data"])
async def data_sync_one(symbol: str, db: Session = Depends(get_db)):
    key = resolve(db, symbol)
    return {"instrument_key": key, "written": await data_sync.sync_symbol(db, client, key)}


@app.get("/api/data/status", tags=["data"])
def data_status(db: Session = Depends(get_db)):
    states = db.scalars(select(SyncState).order_by(SyncState.instrument_key)).all()
    return {"weight": client.status(), "status": status.overview(db, universe(db)), "candle_close": live.state.get("candle_close"),
            "scan_workers": scanpool.workers(),
            "rows": [{**row(s), "candles": s.candles or 0} for s in states]}


@app.get("/api/candles/{symbol:path}", tags=["data"])
def candles(symbol: str, tf: str = "1d", start: datetime | None = None, end: datetime | None = None,
            limit: int = Query(500, le=8000), first: bool = False, db: Session = Depends(get_db)):
    """The newest `limit` candles in the range, or with first=true the earliest ones (used to play forward from a date)."""
    if tf not in config.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {config.TIMEFRAMES}")
    if first:
        df = data_sync.load_candles(db, resolve(db, symbol), tf, since=start, until=end).head(limit)
    else:
        df = data_sync.load_candles(db, resolve(db, symbol), tf, since=start, until=end, last=limit)
    df["ts"] = df["ts"].astype(str) if not df.empty else df["ts"]
    return df.to_dict("records")


ZONE_VIEW = ("id", "side", "pattern", "proximal", "distal", "status", "score", "touches", "base_count", "label", "created_ts", "updated_ts")


def _zone_view(z: Zone) -> dict:
    return {k: getattr(z, k) for k in ZONE_VIEW}


@app.get("/api/chart/{symbol:path}", tags=["data"])
def chart_data(symbol: str, tf: str = "1d", mode: str = "MONTHLY", limit: int = Query(1500, le=8000), start: datetime | None = None,
               first: bool = False, old: bool = False, overlays: str = "", result: bool = True, db: Session = Depends(get_db)):
    """Everything the chart draws, in one request: the candles, the active zones of the timeframe (and the newest 300
    used-up ones with old=true), the active zones of the other timeframes named in overlays, and the mode's result."""
    if tf not in config.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {config.TIMEFRAMES}")
    key = resolve(db, symbol)
    df = (data_sync.load_candles(db, key, tf, since=start).head(limit) if first else data_sync.load_candles(db, key, tf, since=start, last=limit))
    if not df.empty:
        df["ts"] = df["ts"].astype(str)
    base = select(Zone).where(Zone.instrument_key == key)
    active = db.scalars(base.where(Zone.timeframe == tf, Zone.status.in_(ACTIVE))).all()
    used = db.scalars(base.where(Zone.timeframe == tf, Zone.status.notin_(ACTIVE)).order_by(Zone.created_ts.desc()).limit(300)).all() if old else []
    others = [t for t in overlays.split(",") if t in config.TIMEFRAMES and t != tf]
    extra = {t: [_zone_view(z) for z in db.scalars(base.where(Zone.timeframe == t, Zone.status.in_(ACTIVE))).all()] for t in others}
    mode = mode.upper() if mode.upper() in config.MODES else "MONTHLY"
    return {"instrument_key": key, "tf": tf, "candles": df[["ts", "open", "high", "low", "close"]].to_dict("records") if not df.empty else [],
            "zones": [_zone_view(z) for z in active], "old": [_zone_view(z) for z in used], "overlays": extra,
            "result": _result_row(db, key, mode, names_for(db, [key])) if result else None}


_forming_cache: dict[tuple[str, str], tuple[float, dict]] = {}


@app.get("/api/forming/{symbol:path}", tags=["data"])
async def forming(symbol: str, tf: str = "1d", db: Session = Depends(get_db)):
    """The candle that is still forming on a timeframe, straight from Binance (one request of weight 1, kept 3 seconds).
    Only for drawing: it is never stored and never used to find or test a zone. Quarter, half year and year have no
    Binance interval, so they have no forming candle here."""
    import time as _time
    if tf not in config.FETCHED:
        return {"forming": None}
    key = resolve(db, symbol)
    hit = _forming_cache.get((key, tf))
    if hit and _time.monotonic() - hit[0] < 3:
        return hit[1]
    rows = await client.klines(key, config.FETCHED[tf]["interval"], limit=2)
    now_ms = int(_time.time() * 1000)
    bar = next((r for r in reversed(rows) if r["close_ms"] >= now_ms), None)
    out = {"forming": None if bar is None else {"ts": bar["ts"].isoformat(), "open": bar["open"], "high": bar["high"], "low": bar["low"],
                                                "close": bar["close"], "close_ts": data_sync.candle_end(bar["ts"], tf).isoformat()}}
    _forming_cache[(key, tf)] = (_time.monotonic(), out)
    return out


@app.post("/api/instruments/refresh", tags=["data"])
async def instruments_refresh():
    """Download the list of USDT perpetuals again: filters, listing dates, funding intervals and 24 hour volumes."""
    async def work(d, report):
        report(f"{await jobs.store_instruments(d, client)} USDT perpetuals stored")
        await market.store_premium(d, client)
    return {"job_id": start_job("coin list", work)}


@app.get("/api/jobs/{job_id}", tags=["data"])
def job_status(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "No such job")
    return row(job)


# ---------------------------------------------------------------- scan (6)
@app.post("/api/scan/run", tags=["scan"])
async def scan_run(db: Session = Depends(get_db)):
    """Scan candles not yet scanned, for every coin in the list."""
    keys = universe(db)

    async def work(d, report):
        await jobs.each_instrument(keys, lambda key: jobs.prepare(client, key, download=False), report)
    return {"job_id": start_job("scan", work), "symbols": len(keys)}


@app.post("/api/scan/{symbol:path}", tags=["scan"])
async def scan_one(symbol: str, sync: bool = True, db: Session = Depends(get_db)):
    """Sync this instrument if a bar has closed since the last stored candle, then scan it."""
    key = resolve(db, symbol)
    synced = None
    if sync and data_sync.needs_sync(db, key):
        synced = await data_sync.sync_symbol(db, client, key)
    stats = await scanpool.scan(key, True)
    status.mark(db, key, "ready")
    return {"instrument_key": key, "synced": synced, "scan": stats}


def _setup_row(z: Zone, p: TradePlan, names: dict, triggered_ts=None) -> dict:
    ctx = p.context or {}
    return {"zone_id": z.id, "plan_id": p.id, "instrument_key": z.instrument_key,
            "symbol": names.get(z.instrument_key) or z.instrument_key,
            "mode": p.mode, "timeframe": z.timeframe, "side": z.side, "signal": "BUY" if z.side == "demand" else "SELL",
            "pattern": z.pattern, "proximal": z.proximal, "distal": z.distal, "score": z.score, "label": z.label,
            "status": z.status, "wick_tests": z.touches, "touches": z.touches, "base_count": z.base_count, "has_gap": bool(z.has_gap),
            "created_ts": z.created_ts, "plan_status": p.status, "plan_version": p.version, "zone_state": p.zone_state,
            "entry": p.entry, "stop": p.stop, "target": p.target, "reward_risk": p.reward_risk, "plans": p.plans,
            "curve": ctx.get("curve"), "htf_trend": ctx.get("htf_trend"), "itf_trend": ctx.get("itf_trend"),
            "market_state": ctx.get("market_state"), "htf": ctx.get("htf"), "itf": ctx.get("itf"),
            "htf_demand": ctx.get("demand"), "htf_supply": ctx.get("supply"), "allowed": p.allowed,
            "contact_ltf": (p.shared or {}).get("ltf", []) if isinstance(p.shared, dict) else [],
            "contact_htf": (p.shared or {}).get("htf", []) if isinstance(p.shared, dict) else [],
            "triggered_ts": triggered_ts, "price": live.state["prices"].get(z.instrument_key)}


@app.get("/api/setups", tags=["scan"])
def setups(mode: str | None = None, timeframe: str | None = None, side: str | None = None, min_score: float | None = None,
           symbol: str | None = None, include_triggered: bool = False, contact: str | None = None,
           db: Session = Depends(get_db)):
    """Untouched execution zones with a pending plan: the HTF curve and ITF trend of some mode allow the trade.
    A zone can appear once per mode. include_triggered adds setups whose entry was reached today.
    min_score 7 keeps Type 1 zones only; 5 keeps Type 2 and 3 as well. timeframe is the execution zone's timeframe.
    contact keeps setups whose LTF or HTF zone is in contact with a higher timeframe ("any", or one timeframe)."""
    floor = min_score if min_score is not None else settings_store.get(db, "min_score")

    def narrowed(q):
        if mode:
            q = q.where(TradePlan.mode == mode.upper())
        if timeframe:
            q = q.where(Zone.timeframe == timeframe)
        if side:
            q = q.where(Zone.side == side)
        if symbol:
            q = q.where(Zone.instrument_key == resolve(db, symbol))
        return q.where(TradePlan.is_current.is_(True), TradePlan.mode.isnot(None))

    base = select(Zone, TradePlan).join(TradePlan, TradePlan.zone_id == Zone.id)
    rows = db.execute(narrowed(base.where(Zone.status == "fresh", TradePlan.status == "pending", Zone.score >= floor))
                      .order_by(Zone.score.desc(), Zone.created_ts.desc())).all()
    names = names_for(db, {z.instrument_key for z, _ in rows})
    out = [_setup_row(z, p, names) for z, p in rows]
    if include_triggered:
        today = data_sync.now_utc() - timedelta(hours=24)        # crypto has no trading day: the last 24 hours
        hits = dict(db.execute(select(ZoneEvent.zone_id, func.min(ZoneEvent.ts)).where(
            ZoneEvent.event_type.in_(("alert_triggered", "touched")), ZoneEvent.ts >= today).group_by(ZoneEvent.zone_id)).all())
        if hits:
            q = narrowed(base.where(Zone.id.in_(list(hits)), TradePlan.status == "triggered"))
            out += [_setup_row(z, p, names, hits[z.id]) for z, p in db.execute(q).all()]
    if contact:      # "any": some higher timeframe is in contact; or a timeframe such as "1d": that one is
        if contact != "any" and contact not in config.TIMEFRAMES:
            raise HTTPException(400, "contact must be 'any' or a timeframe.")
        tfs = lambda r: {c["tf"] for c in r["contact_ltf"] + r["contact_htf"]}
        out = [r for r in out if (tfs(r) if contact == "any" else contact in tfs(r))]
    return out


@app.get("/api/zones/{symbol:path}", tags=["scan"])
def zones(symbol: str, timeframe: str | None = None, active_only: bool = False, inactive_only: bool = False,
          limit: int = Query(2000, le=5000), db: Session = Depends(get_db)):
    """Zones of one instrument, newest first. active_only: fresh and tested zones. inactive_only: violated and used-up zones."""
    q = select(Zone).where(Zone.instrument_key == resolve(db, symbol))
    if timeframe:
        q = q.where(Zone.timeframe == timeframe)
    if active_only:
        q = q.where(Zone.status.in_(ACTIVE))
    if inactive_only:
        q = q.where(Zone.status.notin_(ACTIVE))
    return [row(z) for z in db.scalars(q.order_by(Zone.created_ts.desc()).limit(limit)).all()]


@app.get("/api/zone/{zone_id}", tags=["scan"])
def zone_detail(zone_id: int, db: Session = Depends(get_db)):
    z = db.get(Zone, zone_id)
    if not z:
        raise HTTPException(404, "No such zone")
    events = db.scalars(select(ZoneEvent).where(ZoneEvent.zone_id == zone_id).order_by(ZoneEvent.ts)).all()
    plans = db.scalars(select(TradePlan).where(TradePlan.zone_id == zone_id).order_by(TradePlan.version)).all()
    return {"zone": row(z), "events": [row(e) for e in events], "plans": [row(p) for p in plans]}


@app.get("/api/rejections", tags=["scan"])
def rejections(symbol: str | None = None, timeframe: str | None = None, limit: int = Query(200, le=2000),
               db: Session = Depends(get_db)):
    q = select(Rejection)
    if symbol:
        q = q.where(Rejection.instrument_key == resolve(db, symbol))
    if timeframe:
        q = q.where(Rejection.timeframe == timeframe)
    return [row(r) for r in db.scalars(q.order_by(Rejection.ts.desc()).limit(limit)).all()]


# ---------------------------------------------------------------- settings (2)
HIDDEN_KEYS = {"telegram_token"}   # never returned by the API
READ_ONLY_KEYS = {"symbols", "telegram_configured", "sync_job_id", "ready", "status", "engine_rules", "rules_reset_pending",
                  "universe_set", "timeframes", "mode_frames", "ranked"}   # computed values, ignored if sent back


@app.get("/api/settings", tags=["system"])
def settings_get(db: Session = Depends(get_db)):
    out = dict(config.DEFAULTS)
    for s in db.scalars(select(Setting)).all():
        if s.key not in HIDDEN_KEYS:
            out[s.key] = s.value
    keys = out.get("universe") or []
    out["symbols"] = names_for(db, keys)
    out["ranked"] = ranked(db, keys)          # the list, most traded first: the Top 20 and Top 50 choices on the pages
    out["timeframes"], out["mode_frames"] = config.TIMEFRAMES, {m: list(v) for m, v in config.MODES.items()}
    out["ready"] = status.ready_keys(db, keys)            # the pages offer only these
    out["status"] = status.overview(db, keys)
    out["telegram_configured"] = telegram.configured()
    return out


@app.put("/api/settings", tags=["system"])
async def settings_put(values: dict = Body(...), db: Session = Depends(get_db)):
    """Save thresholds, the coin list (key 'universe'), per-timeframe overrides (key 'thresholds:<tf>'),
    fees, paper trading settings, the time zone, or the Telegram bot token and chat id. Coins new to the list are
    downloaded and scanned in the background."""
    if "direction_rule" in values and values["direction_rule"] not in ("location", "trend"):
        raise HTTPException(400, "direction_rule must be 'location' or 'trend'.")
    if "marking_rule" in values and values["marking_rule"] not in ("scanner", "pdf"):
        raise HTTPException(400, "marking_rule must be 'scanner' or 'pdf'.")
    if "htf_zone_pick" in values and values["htf_zone_pick"] not in ("fresh", "any"):
        raise HTTPException(400, "htf_zone_pick must be 'fresh' or 'any'.")
    if "zone_history" in values and values["zone_history"] not in ("scanner", "all"):
        raise HTTPException(400, "zone_history must be 'scanner' or 'all'.")
    if "entry_23" in values and values["entry_23"] not in ("TYPE_2", "TYPE_3"):
        raise HTTPException(400, "entry_23 must be 'TYPE_2' or 'TYPE_3'.")
    if "min_stop_pct" in values and not 0 <= float(values["min_stop_pct"]) <= 10:
        raise HTTPException(400, "Minimum stop distance must be from 0 (off) to 10 %.")
    if "min_bases" in values and not (float(values["min_bases"]) == int(float(values["min_bases"])) and 0 <= int(float(values["min_bases"])) <= 10):
        raise HTTPException(400, "Minimum base candles must be a whole number from 0 (off) to 10.")
    if "max_bases" in values and not (float(values["max_bases"]) == int(float(values["max_bases"])) and 0 <= int(float(values["max_bases"])) <= 10):
        raise HTTPException(400, "Maximum base candles must be a whole number from 0 (off) to 10.")
    for flag in ("skip_reaction_zones", "check_next_htf"):
        if flag in values and not isinstance(values[flag], bool):
            raise HTTPException(400, f"{flag} must be true or false.")
    _check_trailing(values)
    if "all_ltf_zones" in values and not isinstance(values["all_ltf_zones"], bool):
        raise HTTPException(400, "all_ltf_zones must be true or false.")
    if "itf_rule" in values and values["itf_rule"] not in ("atr", "pct"):
        raise HTTPException(400, "itf_rule must be 'atr' or 'pct'.")
    if "display_tz" in values:
        from zoneinfo import ZoneInfo
        try:
            ZoneInfo(str(values["display_tz"]))
        except Exception:
            raise HTTPException(400, f"Unknown time zone {values['display_tz']}. Use a name such as Asia/Riyadh or UTC.")
    if "paper_leverage" in values and not 1 <= float(values["paper_leverage"]) <= 125:
        raise HTTPException(400, "Leverage must be from 1 to 125.")
    for fee in ("fee_maker_pct", "fee_taker_pct"):
        if fee in values and not -0.1 <= float(values[fee]) <= 1:
            raise HTTPException(400, f"{fee} must be a percentage between -0.1 and 1.")
    if "universe" in values:
        values["universe"] = list(dict.fromkeys(str(k).strip().upper() for k in values["universe"] if str(k).strip()))
        if "universe_auto" not in values:
            values["universe_auto"] = False        # coins chosen by hand: the list no longer follows the top N
    if "top_n" in values and not 1 <= int(values["top_n"]) <= 300:
        raise HTTPException(400, "Top N must be from 1 to 300.")
    if "history_days" in values:
        hd = values["history_days"] or {}
        if not isinstance(hd, dict) or any(tf not in ("1m", "5m", "15m", "30m", "1h") or not 1 <= int(v) <= 3650 for tf, v in hd.items()):
            raise HTTPException(400, "history_days takes 1m, 5m, 15m, 30m and 1h, each from 1 to 3650 days.")
    before = set(universe(db))
    for k, v in values.items():
        if k in READ_ONLY_KEYS:
            continue
        settings_store.put(db, k, v)
    if "universe" in values:
        settings_store.put(db, "universe_set", True)
    if ("top_n" in values or values.get("universe_auto")) and settings_store.get(db, "universe_auto"):
        jobs.follow_top(db)                         # an automatic list takes the new top N at once
    added = [k for k in universe(db) if k not in before]
    job_id = None
    if added:
        for key in added:
            if not status.overview(db, [key])[key]["ready"]:
                status.mark(db, key, "queued")

        async def work(d, report):
            await jobs.each_instrument(added, lambda key: jobs.prepare(client, key), report, "new coins ready")
        job_id = start_job("new coins", work)
    return {**settings_get(db), "sync_job_id": job_id}


def _coin(i: Instrument, mine: set) -> dict:
    return {"instrument_key": i.instrument_key, "symbol": i.symbol, "name": i.name, "quote_volume": i.quote_volume,
            "funding_rate": i.funding_rate, "onboard_date": i.onboard_date, "in_list": i.instrument_key in mine}


@app.get("/api/instruments/search", tags=["system"])
def instruments_search(q: str = Query(..., min_length=1), db: Session = Depends(get_db)):
    """Find USDT perpetuals by symbol, for the coin list. Most traded first."""
    like = f"%{q.strip().upper()}%"
    rows = db.scalars(select(Instrument).where(Instrument.status == "TRADING", Instrument.symbol.ilike(like))
                      .order_by(func.coalesce(Instrument.quote_volume, 0).desc()).limit(30)).all()
    mine = set(universe(db))
    return [_coin(i, mine) for i in rows]


@app.get("/api/instruments/top", tags=["system"])
def instruments_top(n: int = Query(30, ge=1, le=300), db: Session = Depends(get_db)):
    """The n most traded USDT perpetuals over the last 24 hours, ready to add to the coin list."""
    if not db.scalar(select(func.count()).select_from(Instrument)):
        raise HTTPException(400, "The coin list has not been downloaded yet. Use Refresh coins on the Data page first.")
    keys = market.top_symbols(db, n)
    mine = set(universe(db))
    return {"count": len(keys), "rows": [_coin(db.get(Instrument, k), mine) for k in keys]}


@app.post("/api/instruments/match", tags=["system"])
def instruments_match(text: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Match pasted symbols (BTCUSDT, or just BTC) to USDT perpetuals, ready to add to the coin list."""
    wanted = list(dict.fromkeys(w.strip().upper() for w in text.replace(",", " ").replace(";", " ").split() if w.strip()))
    if not wanted:
        raise HTTPException(400, "Type or paste at least one symbol.")
    if len(wanted) > 300:
        raise HTTPException(400, "That is more than 300 symbols. Add them in smaller groups.")
    mine, rows = set(universe(db)), []
    for w in wanted:
        inst = db.get(Instrument, w) or db.get(Instrument, w + "USDT")
        rows.append({**_coin(inst, mine), "query": w, "found": inst.status == "TRADING"} if inst
                    else {"query": w, "found": False, "instrument_key": None, "symbol": w})
    return {"count": len(rows), "rows": rows}


@app.get("/api/jobs", tags=["system"])
def jobs_list(limit: int = Query(30, le=200), db: Session = Depends(get_db)):
    return [row(j) for j in db.scalars(select(Job).order_by(Job.id.desc()).limit(limit)).all()]


@app.post("/api/alerts/test", tags=["system"])
async def alerts_test():
    ok, detail = await telegram.send("Crypto zone scanner: test alert. Telegram is set up correctly.")
    if not ok:
        raise HTTPException(400, detail)
    return {"sent": True}


@app.post("/api/rebuild/zones", tags=["scan"])
async def scan_rebuild(db: Session = Depends(get_db)):
    """Clear all zones, plans and rejections for the coin list and scan the stored candles again.
    Use it after changing thresholds. Candles are kept; paper positions are kept."""
    keys = universe(db)

    def clear(key: str):
        with SessionLocal() as own:
            scanner.clear_key(own, key)

    async def work(d, report):
        done = 0

        async def one(key: str):
            nonlocal done
            await asyncio.to_thread(clear, key)
            await scanpool.scan(key, True)
            done += 1
            report(f"{done} of {len(keys)} coins rebuilt")
        await jobs.each_instrument(keys, one, lambda text: None)
        for key in keys:
            status.mark(d, key, "ready")
        report(f"{len(keys)} coins rebuilt")
    return {"job_id": start_job("rebuild zones", work), "symbols": len(keys)}


# ---------------------------------------------------------------- replay (2)
@app.post("/api/replay/start", tags=["replay"])
def replay_start(symbol: str = Body(...), timeframe: str = Body("1d"), start: datetime = Body(...),
                 db: Session = Depends(get_db)):
    """Start a replay session from a date. Runs on stored candles in a separate copy of the engine."""
    if timeframe not in config.TIMEFRAMES:
        raise HTTPException(400, f"timeframe must be one of {config.TIMEFRAMES}")
    try:
        session = ReplaySession(db, resolve(db, symbol), timeframe, start)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"session_id": session.id, "remaining": session.remaining}


@app.websocket("/ws/replay/{session_id}")
async def replay_ws(ws: WebSocket, session_id: str):
    """Commands: {"cmd": "play", "speed": bars per second}, "pause", "step", "next_event"."""
    await ws.accept()
    session = SESSIONS.get(session_id)
    if session is None:
        await ws.send_json({"type": "error", "message": "Replay session not found. Start a new one."})
        await ws.close()
        return
    await ws.send_json(session.init_payload())
    playing, delay = False, 0.5
    try:
        while True:
            send = 0
            try:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=delay if playing else None)
                cmd = msg.get("cmd")
                if cmd == "play":
                    playing, delay = True, 1 / max(0.5, min(60, float(msg.get("speed", 2))))
                elif cmd == "pause":
                    playing = False
                elif cmd == "step":
                    playing, send = False, 1
                elif cmd == "next_event":
                    playing, send = False, -1
            except asyncio.TimeoutError:
                send = 1
            while send:
                bar = session.next_bar()
                if bar is None:
                    playing = False
                    await ws.send_json({"type": "end"})
                    break
                await ws.send_json(bar)
                send = 0 if send == 1 or bar["events"] else send
    except WebSocketDisconnect:
        pass
    finally:
        SESSIONS.pop(session_id, None)


# ---------------------------------------------------------------- backtest (5)
@app.post("/api/backtest/run", tags=["backtest"])
async def backtest_run(symbols: list[str] | str = Body("all"), modes: list[str] = Body(["DAILY"]),
                       start: date = Body(...), end: date = Body(...), min_score: float | None = Body(None),
                       entry_plan: str = Body("rule"), fee_maker_pct: float | None = Body(None), fee_taker_pct: float | None = Body(None),
                       fee_bnb: bool | None = Body(None), include_funding: bool | None = Body(None),
                       trail_method: str | None = Body(None), trail_start_r: float | None = Body(None),
                       trail_keep_target: bool | None = Body(None), trail_atr_mult: float | None = Body(None),
                       trail_swing: int | None = Body(None), trail_tf: str | None = Body(None),
                       trail_lock_pct: float | None = Body(None), trail_lock_gap_pct: float | None = Body(None),
                       trail_lock_target: str | None = Body(None), db: Session = Depends(get_db)):
    """Start a backtest on stored candles. modes are GTF analysis modes (each brings its own HTF, ITF and LTF).
    entry_plan is rule (the entry the trade score takes), type1, type2 or type3. Fees, funding and the trailing stop
    (trail_method off, step, swing, atr or lock, and its options) come from the settings unless given here, so one setting can
    be compared with another on the same dates without changing the settings."""
    keys = _keys(db, symbols)
    modes = [m.upper() for m in modes]
    if not modes or any(m not in config.MODES for m in modes):
        raise HTTPException(400, f"Modes must be chosen from {list(config.MODES)}")
    if entry_plan not in bt.ENTRY_PLANS:
        raise HTTPException(400, f"entry_plan must be one of {list(bt.ENTRY_PLANS)}")
    if not keys:
        raise HTTPException(400, "No coins chosen.")
    if end < start:
        raise HTTPException(400, "The end date is before the start date.")
    have = {(k, tf) for k, tf in db.execute(select(Candle.instrument_key, Candle.timeframe).distinct())}
    missing = sorted({f"{k} {tf}" for k in keys for m in modes for tf in config.MODES[m] if (k, tf) not in have})
    if missing and len(keys) == 1:
        raise HTTPException(400, "No candles stored for: " + ", ".join(missing[:8]) + ". Sync them from the Data page first.")
    # With several coins, a coin that lacks one of the mode's timeframes (a new listing) is left out instead of the whole run.
    skipped = sorted({k for k in keys for m in modes for tf in config.MODES[m] if (k, tf) not in have})
    keys = [k for k in keys if k not in skipped]
    if not keys:
        raise HTTPException(400, "No coin has candles for every timeframe of this mode. Sync them from the Data page first.")
    run = BacktestRun(status="running", created_at=data_sync.now_utc(), progress="starting",
                      settings={"symbols": keys, "modes": modes, "start": str(start), "end": str(end),
                                "min_score": min_score if min_score is not None else settings_store.get(db, "min_score"),
                                "entry_plan": entry_plan, **_cost_settings(db, fee_maker_pct, fee_taker_pct, fee_bnb, include_funding),
                                **_trail_settings(db, {"trail_method": trail_method, "trail_start_r": trail_start_r,
                                                       "trail_keep_target": trail_keep_target, "trail_atr_mult": trail_atr_mult,
                                                       "trail_swing": trail_swing, "trail_tf": trail_tf,
                                                       "trail_lock_pct": trail_lock_pct, "trail_lock_gap_pct": trail_lock_gap_pct,
                                                       "trail_lock_target": trail_lock_target})})
    db.add(run)
    db.commit()
    asyncio.get_running_loop().create_task(asyncio.to_thread(bt.execute, run.id))
    note = _history_warning(db, keys, modes, start)
    if skipped:
        note = " ".join(filter(None, [f"Left out (missing candles for this mode): {', '.join(skipped[:10])}{' …' if len(skipped) > 10 else ''}.", note]))
    return {"run_id": run.id, "warning": note}


def _history_warning(db: Session, keys: list[str], modes: list[str], start: date) -> str | None:
    """A note when some coins' entry-timeframe candles begin less than a week before the start: their zones and EMAs
    have little history at first, so their trades only count once it has built up. None when every coin is fine."""
    from collections import Counter
    from .models import SyncState
    begin, notes = datetime.combine(start, datetime.min.time()), []
    for m in modes:
        ltf = config.MODES[m][2]
        firsts = dict(db.execute(select(SyncState.instrument_key, SyncState.first_candle_ts).where(
            SyncState.timeframe == ltf, SyncState.instrument_key.in_(keys))).all())
        late = [firsts[k] for k in keys if firsts.get(k) is not None and firsts[k] > begin - timedelta(days=7)]
        if late:
            common = Counter(d.date() for d in late).most_common(1)[0][0]
            notes.append(f"{len(late)} of {len(keys)} coins have {ltf} candles only from about {common} or later "
                         f"(mode {m}): pick {common + timedelta(days=7)} or later for a full first week.")
    return " ".join(notes) or None


def _check_trailing(values: dict) -> None:
    """The trailing stop settings, from the Settings page or a backtest run."""
    from .sim import trailing
    if values.get("trail_method") is not None and values["trail_method"] not in trailing.METHODS:
        raise HTTPException(400, f"trail_method must be one of {list(trailing.METHODS)}.")
    if values.get("trail_tf") is not None and values["trail_tf"] not in trailing.TIMEFRAMES:
        raise HTTPException(400, "trail_tf must be 'ltf', 'itf' or '1m'.")
    if values.get("trail_start_r") is not None and not 0.25 <= float(values["trail_start_r"]) <= 10:
        raise HTTPException(400, "Start trailing at must be from 0.25 to 10 R.")
    if values.get("trail_atr_mult") is not None and not 0.5 <= float(values["trail_atr_mult"]) <= 10:
        raise HTTPException(400, "ATR multiple must be from 0.5 to 10.")
    if values.get("trail_swing") is not None and int(values["trail_swing"]) not in range(1, 6):
        raise HTTPException(400, "Swing size must be 1 to 5 candles.")
    if values.get("trail_keep_target") is not None and not isinstance(values["trail_keep_target"], bool):
        raise HTTPException(400, "trail_keep_target must be true or false.")
    if values.get("trail_lock_pct") is not None and not 1 <= float(values["trail_lock_pct"]) <= 100:
        raise HTTPException(400, "Lock step must be from 1 to 100 % of the target distance.")
    if values.get("trail_lock_gap_pct") is not None and not 0 <= float(values["trail_lock_gap_pct"]) <= 100:
        raise HTTPException(400, "Gap before locking must be from 0 to 100 % of one step.")
    if values.get("trail_lock_target") is not None and values["trail_lock_target"] not in trailing.LOCK_TARGET:
        raise HTTPException(400, "trail_lock_target must be 'move' or 'close'.")


def _trail_settings(db: Session, given: dict) -> dict:
    """The trailing stop a backtest run uses: the values given, else the saved settings (off unless changed)."""
    _check_trailing(given)
    return {k: v if v is not None else settings_store.get(db, k) for k, v in given.items()}


def _cost_settings(db: Session, maker, taker, bnb, funding) -> dict:
    """The fee and funding settings a run uses: the ones given, or the saved settings. Kept with the run."""
    got = {"fee_maker_pct": maker, "fee_taker_pct": taker, "fee_bnb": bnb, "include_funding": funding}
    return {k: v if v is not None else settings_store.get(db, k) for k, v in got.items()}


def _run_head(r: BacktestRun) -> dict:
    s = r.summary or {}
    return {"id": r.id, "status": r.status, "progress": r.progress, "error": r.error, "created_at": r.created_at,
            "ended_at": r.ended_at, "settings": r.settings,
            "headline": {k: s.get(k) for k in ("trades", "win_rate", "expectancy", "profit_factor", "max_drawdown_r", "total_r",
                                               "stop_rate", "open_trades", "orders")}}


@app.get("/api/backtest/runs", tags=["backtest"])
def backtest_runs(db: Session = Depends(get_db)):
    runs = db.scalars(select(BacktestRun).options(defer(BacktestRun.summary)).order_by(BacktestRun.id.desc()).limit(100)).all()
    return [_run_head(r) for r in runs if not _is_scan(r)][:50]     # a backtest's summary is read only for backtests


@app.get("/api/backtest/{run_id}", tags=["backtest"])
def backtest_get(run_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "No such backtest run")
    trades = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts)).all()
    return {**_run_head(r), "summary": r.summary, "trades": [row(t) for t in trades]}


@app.delete("/api/backtest/{run_id}", tags=["backtest"])
def backtest_delete(run_id: int, db: Session = Depends(get_db)):
    """Delete a finished backtest run and its trades (a running one is cancelled first from the page)."""
    r = db.get(BacktestRun, run_id)
    if not r or _is_scan(r):
        raise HTTPException(404, "No such backtest run")
    if r.status == "running":
        raise HTTPException(400, "This run is still going. Cancel it first.")
    db.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run_id))
    db.delete(r)
    db.commit()
    return {"deleted": run_id}


@app.post("/api/backtest/{run_id}/cancel", tags=["backtest"])
def backtest_cancel(run_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, run_id)
    if not r:
        raise HTTPException(404, "No such backtest run")
    if r.status == "running":
        bt.CANCEL.add(run_id)
    return {"id": run_id, "status": r.status}


@app.get("/api/backtest/{run_id}/export", tags=["backtest"])
def backtest_export(run_id: int, db: Session = Depends(get_db)):
    trades = db.scalars(select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.entry_ts)).all()
    if not trades:
        raise HTTPException(404, "This run has no trades to export.")
    cols = [c.name for c in BacktestTrade.__table__.columns if c.name not in ("id", "run_id")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for t in trades:
        w.writerow([getattr(t, c) for c in cols])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="backtest_{run_id}.csv"'})


# ---------------------------------------------------------------- live (3)
ALERT_TYPES = ("alert_approaching", "alert_triggered")


BIAS_KEYS = (config.BTC, "ETHUSDT")      # shown above the dashboard: what the market leaders say in the chosen mode


@app.get("/api/live/status", tags=["live"])
def live_status(mode: str = "DAILY", db: Session = Depends(get_db)):
    """Everything the dashboard shows around the table: the price feed, what BTC and ETH say in the chosen mode
    (curve, trends and trade side), the last alerts, the last scan time and the next candle close."""
    now = data_sync.now_utc()
    mode = mode.upper() if mode.upper() in config.MODES else "DAILY"
    ltf = config.MODES[mode][2]
    bias = []
    for key in BIAS_KEYS:          # from the result saved at the last scan: no zones or candles are read here
        if key not in universe(db):
            continue
        saved = db.get(ModeResult, (key, mode))
        d = (saved.data or {}) if saved else {}
        bias.append({"instrument_key": key, "symbol": key, "price": live.state["prices"].get(key) or d.get("cmp"),
                     "curve": d.get("curve"), "itf_trend": d.get("itf_trend"), "htf_trend": d.get("htf_trend"),
                     "market_state": d.get("market_state"), "side": d.get("trade_side") or "NONE"})
    # The last 20 alerts, then what happened to those zones afterwards: two small queries on the event type and time index.
    hits = db.execute(select(ZoneEvent, Zone).join(Zone, Zone.id == ZoneEvent.zone_id).where(ZoneEvent.event_type.in_(ALERT_TYPES))
                      .order_by(ZoneEvent.ts.desc(), ZoneEvent.id.desc()).limit(20)).all()
    ids = list({z.id for _, z in hits})
    after = db.execute(select(ZoneEvent, Zone).join(Zone, Zone.id == ZoneEvent.zone_id).where(
        ZoneEvent.zone_id.in_(ids), ZoneEvent.event_type.in_(("violated", "consumed", "entry2", "entry3")))).all() if ids else []
    events = sorted(hits + after, key=lambda ez: (ez[0].ts, ez[0].id), reverse=True)[:20]
    alerts = [{"time": e.ts, "alert": e.event_type.replace("alert_", ""), "zone_id": z.id, "instrument_key": z.instrument_key,
               "symbol": z.instrument_key, "timeframe": z.timeframe,
               "side": z.side, "price": e.price, "proximal": z.proximal} for e, z in events]
    return {"now": now, "market_open": True, "binance": client.status(), "display_tz": settings_store.get(db, "display_tz"),
            "scheduler": app.state.scheduler is not None, "feed": live.feed_status(now), "last_scan": live.state["last_scan"],
            "candle_close": live.state.get("candle_close"),
            "next_bar_close": live.next_bar_close(now), "bias_mode": mode, "bias": bias, "alerts": alerts,
            "approach_pct": settings_store.get(db, "approach_pct"), "modes": {m: list(v) for m, v in config.MODES.items()},
            "prices": live.state["prices"]}


@app.post("/api/feed/reconnect", tags=["live"])
async def feed_reconnect():
    """Restart the live price feed (the Binance WebSocket)."""
    live.start_poller(client)
    return {"running": True}


@app.websocket("/ws/live")
async def live_ws(ws: WebSocket):
    """Pushes prices, alerts and scan notices to the pages."""
    await ws.accept()
    live.hub.clients.add(ws)
    try:
        if live.state["prices"]:
            await ws.send_json({"type": "prices", "data": live.state["prices"], "time": None})
        while True:
            await ws.receive_text()   # the page sends nothing; this just notices a disconnect
    except WebSocketDisconnect:
        pass
    finally:
        live.hub.clients.discard(ws)


# ---------------------------------------------------------------- paper trading (8)
def _paper_error(exc: ValueError):
    raise HTTPException(400, str(exc))


def _trade_view(t: PaperTrade, db: Session) -> dict:
    price = live.state["prices"].get(t.instrument_key)
    sign = 1 if t.side == "demand" else -1
    out = row(t)
    open_ = price is not None and t.exit_ts is None
    funding = paper.funding_so_far(db, t, data_sync.now_utc()) if t.exit_ts is None else t.funding
    end = t.exit_ts or data_sync.now_utc()
    out.update(symbol=t.instrument_key, price=price, funding=funding,
               duration_s=int((end - t.fill_ts).total_seconds()) if t.fill_ts else None,
               open_pnl=round((price - t.fill_price) * sign * t.quantity - (t.fees or 0) + (funding or 0), 4) if open_ else None)
    if t.exit_ts is None and t.fill_price and t.quantity:          # the "Trailing / locked" column of an open position
        out["lock"] = paper.lock_view(t, settings_store.thresholds(db, t.timeframe or "1d"))
    return out


@app.post("/api/paper/orders", tags=["paper"])
async def paper_order(zone_id: int = Body(...), mode: str | None = Body(None), leverage: float | None = Body(None),
                      db: Session = Depends(get_db)):
    """Create a pending paper order from a zone's current plan, at the leverage given or the leverage setting.
    Refused when the size is under the coin's minimum or the estimated liquidation price comes before the stop loss.
    Nothing is sent to Binance."""
    now = data_sync.now_utc()
    try:
        order = paper.place(db, zone_id, now, mode, leverage)
    except ValueError as exc:
        _paper_error(exc)
    await live.hub.broadcast({"type": "paper", "event": "order placed"})
    return row(order)


@app.delete("/api/paper/orders/{order_id}", tags=["paper"])
def paper_cancel(order_id: int, db: Session = Depends(get_db)):
    try:
        return row(paper.cancel(db, order_id))
    except ValueError as exc:
        _paper_error(exc)


@app.post("/api/paper/orders/{order_id}/enter", tags=["paper"])
async def paper_enter(order_id: int, allow_low_rr: bool = Body(False, embed=True), dry_run: bool = Body(False, embed=True),
                      db: Session = Depends(get_db)):
    """Enter a pending order now at the live mark price, as a market order (taker fee), keeping its stop and target.
    dry_run=true returns the numbers for the confirm box without entering. Under 1 to 1 the answer is 409 until it is
    sent again with allow_low_rr=true. The price is fetched from Binance if the live feed has none. Nothing is sent to Binance."""
    o = db.get(PaperOrder, order_id)
    if not o or o.status != "pending":
        raise HTTPException(400, "No pending order with that id.")
    price = live.state["prices"].get(o.instrument_key)
    if price is None:
        price = (await client.ltp([o.instrument_key])).get(o.instrument_key)
    if price is None:
        raise HTTPException(400, "No live price is available for this coin right now.")
    try:
        out = paper.enter_now(db, order_id, price, data_sync.now_utc(), allow_low_rr, dry_run)
    except paper.LowReward as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        _paper_error(exc)
    if not dry_run:
        await live.hub.broadcast({"type": "paper", "event": "entered"})
    return out


@app.get("/api/paper/positions", tags=["paper"])
def paper_positions(db: Session = Depends(get_db)):
    pending = [{**row(o), "symbol": o.instrument_key, "price": live.state["prices"].get(o.instrument_key), **paper.order_wait(db, o)}
               for o in paper.live_orders(db)]
    closed = db.scalars(select(PaperTrade).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))
                        .order_by(PaperTrade.exit_ts.desc()).limit(200)).all()
    return {"pending": pending, "open": [_trade_view(t, db) for t in paper.open_trades(db)],
            "closed": [_trade_view(t, db) for t in closed]}


@app.get("/api/paper/chart/{kind}/{item_id}", tags=["paper"])
def paper_chart(kind: str, item_id: int, db: Session = Depends(get_db)):
    """Everything the Paper page chart needs for one pending order (kind=order) or one open or closed trade (kind=trade),
    in one request: the candles of its timeframe from before the zone formed to after the exit (or to now), the zone
    with its score breakdown, and the order or trade details."""
    if kind not in ("order", "trade"):
        raise HTTPException(400, "kind must be order or trade")
    item = db.get(PaperOrder if kind == "order" else PaperTrade, item_id)
    if item is None:
        raise HTTPException(404, f"No such paper {kind}.")
    order = item if kind == "order" else (db.get(PaperOrder, item.order_id) if item.order_id else None)
    tf = item.timeframe or "1h"
    if tf not in config.TIMEFRAMES:
        raise HTTPException(400, f"Unknown timeframe {tf}")
    z = db.get(Zone, item.zone_id) if item.zone_id else None
    th = settings_store.thresholds(db, tf)
    now = data_sync.now_utc()
    bar = timedelta(minutes=config.TF_MINUTES.get(tf, 1440 * 30))
    pad = bar * (240 if bar < timedelta(days=1) else 45)
    fill_ts = getattr(item, "fill_ts", None)
    exit_ts = getattr(item, "exit_ts", None)
    start = min(t for t in (z.created_ts if z else None, fill_ts, order.created_at if order else None, now) if t is not None) - pad
    end = (exit_ts + pad) if exit_ts else now
    df = data_sync.load_candles(db, item.instrument_key, tf, since=start, until=end).tail(4000)
    candles = [{"ts": t.isoformat(), "open": o, "high": h, "low": l, "close": c}
               for t, o, h, l, c in zip(df["ts"], df["open"], df["high"], df["low"], df["close"])] if not df.empty else []
    plan = db.get(TradePlan, order.plan_id) if order is not None and order.plan_id else None
    entry = item.fill_price if kind == "trade" else item.entry
    status = "pending" if kind == "order" else ("open" if exit_ts is None else "closed")
    price = live.state["prices"].get(item.instrument_key)
    out = {"kind": kind, "id": item.id, "status": status, "instrument_key": item.instrument_key, "symbol": item.instrument_key,
           "timeframe": tf, "mode": plan.mode if plan else None, "side": item.side, "entry_type": item.entry_type, "entry": entry, "stop": item.stop, "target": item.target,
           "quantity": item.quantity, "leverage": item.leverage, "liq_price": item.liq_price,
           "rr": round(abs(item.target - entry) / abs(entry - item.stop), 2) if item.stop is not None and item.target is not None and entry != item.stop else None,
           "signal_ts": getattr(item, "signal_ts", None), "placed_at": order.created_at if order else None,
           "entry_ts": fill_ts, "exit_ts": exit_ts, "price": price, "candles": candles,
           "zone": results.zone_card(z, th) if z else None, "zone_status": z.status if z else None,
           "zone_created_ts": z.created_ts if z else None, "zone_proximal": z.proximal if z else None, "zone_distal": z.distal if z else None}
    if kind == "trade":
        view = _trade_view(item, db)
        out.update(result=item.result if exit_ts else "open", exit_price=item.exit_price, exit_reason=item.exit_reason,
                   fees=item.fees, funding=view["funding"], pnl=item.pnl, result_r=item.result_r, open_pnl=view["open_pnl"],
                   duration_s=view["duration_s"], history=item.history or [], label=item.label, trail=item.trail)
    return out


PAPER_CSV = ["id", "symbol", "timeframe", "side", "entry_type", "quantity", "leverage", "signal_ts", "fill_ts", "fill_price",
             "exit_ts", "exit_price", "duration", "stop", "target", "fees", "funding", "pnl", "result_r", "result"]


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    d, rest = divmod(int(seconds), 86400)
    h, m = rest // 3600, rest % 3600 // 60
    return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min"


@app.get("/api/paper/export", tags=["paper"])
def paper_export(db: Session = Depends(get_db)):
    """Closed paper trades as CSV, newest first. Times are in UTC, as stored; the duration is entry to exit."""
    trades = db.scalars(select(PaperTrade).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))
                        .order_by(PaperTrade.exit_ts.desc())).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c if not c.endswith("_ts") else c + " (UTC)" for c in PAPER_CSV])
    for t in trades:
        seconds = (t.exit_ts - t.fill_ts).total_seconds() if t.fill_ts else None
        values = {"symbol": t.instrument_key, "duration": _duration(seconds)}
        w.writerow([values[c] if c in values else getattr(t, c, None) for c in PAPER_CSV])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="paper_trades.csv"'})


@app.patch("/api/paper/positions/{trade_id}", tags=["paper"])
def paper_edit(trade_id: int, stop: float | None = Body(None), target: float | None = Body(None), db: Session = Depends(get_db)):
    try:
        return row(paper.edit(db, trade_id, stop, target, data_sync.now_utc()))
    except ValueError as exc:
        _paper_error(exc)


@app.post("/api/paper/positions/{trade_id}/close", tags=["paper"])
async def paper_close(trade_id: int, db: Session = Depends(get_db)):
    """Close at the live mark price, less the slippage setting, paying the taker fee. The price is fetched from Binance
    now if the live feed has none; without any live price the position cannot be closed."""
    t = db.get(PaperTrade, trade_id)
    if not t or t.exit_ts is not None:
        raise HTTPException(400, "No open position with that id.")
    price = live.state["prices"].get(t.instrument_key)
    if price is None:
        price = (await client.ltp([t.instrument_key])).get(t.instrument_key)
    if price is None:
        raise HTTPException(400, "No live price is available for this coin right now.")
    return row(paper.manual_close(db, trade_id, price, data_sync.now_utc()))


@app.get("/api/paper/summary", tags=["paper"])
def paper_summary(db: Session = Depends(get_db)):
    return paper.summary(db, live.state["prices"])


@app.post("/api/paper/reset", tags=["paper"])
def paper_reset(db: Session = Depends(get_db)):
    """Archive every paper order and trade and start again from the starting capital."""
    return {"archived": paper.reset(db)}


# ---------------------------------------------------------------- results (2)
def _result_row(db: Session, key: str, mode: str, names: dict, fresh: bool = False, saved: ModeResult | None = None) -> dict:
    saved = saved if saved is not None else db.get(ModeResult, (key, mode))
    data = dict(saved.data) if saved and saved.data and not fresh else results.build(db, key, mode)
    price = live.state["prices"].get(key)
    return {**data, "symbol": names.get(key) or key, "live_price": price,
            "updated_at": saved.updated_at if saved else None,
            "signal_since": saved.signal_since if saved else None,
            "candle_ts": saved.candle_ts if saved else results.candle_close(data)}


@app.get("/api/results", tags=["scan"])
def results_list(mode: str = "MONTHLY", db: Session = Depends(get_db)):
    """The dashboard: one full GTF result per ready coin for the chosen mode, plus the coins still on their way.
    Read from the results saved at each scan, all in one query; a coin with no saved result yet is worked out once."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    keys = universe(db)
    names = names_for(db, keys)
    state = status.overview(db, keys)
    ready = [k for k in keys if state[k]["ready"]]
    saved = {r.instrument_key: r for r in db.scalars(select(ModeResult).where(ModeResult.mode == mode, ModeResult.instrument_key.in_(ready)))} if ready else {}
    order = ranked(db, keys)
    return {"mode": mode, "frames": list(config.MODES[mode]), "total": len(keys), "ready": len(ready), "ranked": order,
            "direction_rule": settings_store.get(db, "direction_rule") or "location",
            "approach_pct": settings_store.get(db, "approach_pct"),
            "pending": [{"instrument_key": k, "symbol": k, **state[k]} for k in keys if not state[k]["ready"]],
            "busy": [k for k in ready if state[k]["state"] in ("downloading", "scanning")],
            "rows": [_result_row(db, k, mode, names, saved=saved.get(k)) for k in ready]}


@app.get("/api/eye", tags=["scan"])
def eye_list(mode: str = "DAILY", db: Session = Depends(get_db)):
    """GTF EYE and Premium pages: for every coin of the list, where the price stands against its nearest HTF demand and
    supply zones of the mode (in the zone, reacting from it, approaching it), its daily ATR % and the saved GTF signal.
    Filtering (criteria, side, standard or GTF, score, coins) is done on the page, so a click never waits for the server."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    keys = universe(db)
    approach = float(settings_store.get(db, "approach_pct") or 1.0)
    th = settings_store.thresholds(db, config.MODES[mode][0])
    return {"mode": mode, "frames": list(config.MODES[mode]), "approach_pct": approach, "react_candles": eye.REACT_CANDLES,
            "react_max_r": eye.REACT_MAX_R, "max_tests": int(th["max_wick_tests"]),
            "ranked": ranked(db, keys), "rows": eye.scan(db, keys, mode, live.state["prices"], approach, th)}


@app.get("/api/result/{symbol:path}", tags=["scan"])
def result_one(symbol: str, mode: str = "MONTHLY", db: Session = Depends(get_db)):
    """One coin in one mode, for the chart."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    key = resolve(db, symbol)
    return _result_row(db, key, mode, names_for(db, [key]))


# ---------------------------------------------------------------- scan-date check (1)
@app.post("/api/scancheck", tags=["backtest"])
async def scan_check(symbols: list[str] | str = Body("all"), mode: str = Body("MONTHLY"), scan_date: date = Body(...),
                     fee_maker_pct: float | None = Body(None), fee_taker_pct: float | None = Body(None), fee_bnb: bool | None = Body(None),
                     include_funding: bool | None = Body(None), save: bool = Body(False), db: Session = Depends(get_db)):
    """What the rules said about each coin on one date, from the candles up to that date only, and what then
    happened to the entry its trade score takes (type 1, or types 2 and 3) in the candles after it, in R after fees
    and funding. save=true keeps the report so it can be reopened later from the list of past checks."""
    mode = mode.upper()
    if mode not in config.MODES:
        raise HTTPException(400, f"mode must be one of {list(config.MODES)}")
    keys = status.ready_keys(db, universe(db)) if symbols in (None, "all") else [resolve(db, x) for x in symbols]
    if not keys:
        raise HTTPException(400, "No ready coin to check. Download and scan some first.")
    cost = _cost_settings(db, fee_maker_pct, fee_taker_pct, fee_bnb, include_funding)

    # Each coin is checked in a worker process, several at once on several CPU cores (a thread on SQLite).
    rows = await scanpool.map_heavy(scancheck.check_key, [(k, mode, scan_date, cost) for k in keys])
    out = {"id": None, "mode": mode, "scan_date": str(scan_date), "rows": rows, "summary": scancheck.summarize(rows), "costs": cost}
    if save:
        run = BacktestRun(status="done", created_at=data_sync.now_utc(), ended_at=data_sync.now_utc(), progress=f"{len(rows)} coins",
                          settings={"kind": "scan", "mode": mode, "scan_date": str(scan_date), "symbols": keys, **cost},
                          summary={"summary": out["summary"], "rows": rows}, brief=out["summary"])
        db.add(run)
        db.commit()
        out["id"] = run.id
    return out


def _is_scan(r: BacktestRun) -> bool:
    return isinstance(r.settings, dict) and r.settings.get("kind") == "scan"


@app.get("/api/scanchecks", tags=["backtest"])
def scan_checks(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    """Past scan-date checks, newest first, with their totals per plan."""
    # The coin rows of each check are not read here: only its saved totals (brief). A check saved by an earlier version
    # has none yet; its totals are taken from the full report once and kept.
    runs = [r for r in db.scalars(select(BacktestRun).options(defer(BacktestRun.summary)).order_by(BacktestRun.id.desc())
                                  .limit(limit * 3)).all() if _is_scan(r)][:limit]
    for r in runs:
        if r.brief is None:
            r.brief = (r.summary or {}).get("summary") or {}
    db.commit()
    return [{"id": r.id, "created_at": r.created_at, "mode": r.settings["mode"], "scan_date": r.settings["scan_date"],
             "coins": len(r.settings.get("symbols") or []), "summary": r.brief or None} for r in runs]


@app.get("/api/scancheck/{check_id}", tags=["backtest"])
def scan_check_get(check_id: int, db: Session = Depends(get_db)):
    """One saved scan-date check, exactly as it was reported."""
    r = db.get(BacktestRun, check_id)
    if not r or not _is_scan(r):
        raise HTTPException(404, "No such saved check")
    return {"id": r.id, "created_at": r.created_at, "mode": r.settings["mode"], "scan_date": r.settings["scan_date"],
            "costs": {k: r.settings.get(k) for k in ("fee_maker_pct", "fee_taker_pct", "fee_bnb", "include_funding")},
            "rows": (r.summary or {}).get("rows", []), "summary": (r.summary or {}).get("summary", {})}


@app.delete("/api/scancheck/{check_id}", tags=["backtest"])
def scan_check_delete(check_id: int, db: Session = Depends(get_db)):
    r = db.get(BacktestRun, check_id)
    if not r or not _is_scan(r):
        raise HTTPException(404, "No such saved check")
    db.delete(r)
    db.commit()
    return {"deleted": check_id}