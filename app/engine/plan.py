"""Trade plans under GTF rules. For every analysis mode whose execution timeframe was just scanned, the
HTF and ITF are read, the trade side is decided, and each execution zone with a trade score of 5 or more gets a plan.
The plan waits for the entry its score takes: type 1 for 7 or more, type 2 or 3 for 5 to 6.5. While it waits its
entry is the proximal line; once the entry happens the plan holds the real entry, stop loss and target.
Plans are versioned; nothing is overwritten."""
from bisect import bisect_right
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES, TIMEFRAMES
from ..data.resample import close_time
from ..data.sync import load_candles
from ..models import TradePlan, Zone, ZoneEvent
from . import gtf, window
from .contact import contacts
from .lifecycle import ACTIVE, counted_tests
from .score import entry_kind, htf_score, ltf_score


def current_plan(db: Session, zone_id: int, mode: str | None = None) -> TradePlan | None:
    """The current plan of a zone for one mode. Without a mode: a pending one if there is any, else any."""
    q = select(TradePlan).where(TradePlan.zone_id == zone_id, TradePlan.is_current.is_(True))
    if mode is not None:
        return db.scalars(q.where(TradePlan.mode == mode)).first()
    rows = db.scalars(q).all()
    return next((p for p in rows if p.status == "pending"), rows[0] if rows else None)


def mode_context(db: Session, key: str, mode: str, cmp: float, as_of=None) -> dict:
    """HTF zones, curve, both trends and the trade side for one instrument and mode, from stored data.
    as_of (the date of the last candle) sets scanner.py's history windows; without it every stored zone is used."""
    htf, itf, _ = MODES[mode]
    th = settings_store.thresholds(db, htf)
    zones = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == htf, Zone.status.in_(ACTIVE))).all()
    h_from, i_from = window.start(mode, htf, as_of, th), window.start(mode, itf, as_of, th)
    zones = window.keep(zones, h_from)
    htf_bars, itf_bars = load_candles(db, key, htf, last=20), load_candles(db, key, itf, last=60)
    if h_from is not None and not htf_bars.empty:
        htf_bars = htf_bars[htf_bars["ts"] >= h_from]
    if i_from is not None and not itf_bars.empty:
        itf_bars = itf_bars[itf_bars["ts"] >= i_from]
    ctx = gtf.context(zones, htf_bars["close"].to_numpy(dtype=float), itf_bars["close"].to_numpy(dtype=float), cmp, th)
    up = gtf.next_htf(mode)
    if th.get("check_next_htf") and up:        # GTF PDF p.30: the fresh zones one timeframe above the HTF
        above = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == up, Zone.status.in_(ACTIVE))).all()
        ctx.update(above=gtf.usable_htf(above, settings_store.thresholds(db, up)), above_tf=up)
    return ctx


def context_summary(ctx: dict, mode: str, th: dict) -> dict:
    htf, itf, ltf = MODES[mode]
    return {"mode": mode, "htf": htf, "itf": itf, "ltf": ltf, "market_state": ctx["market_state"], "curve": ctx["curve"],
            "htf_trend": ctx["htf_trend"], "itf_trend": ctx["itf_trend"], "itf_reason": ctx["itf_reason"], "side": ctx["side"],
            "demand": gtf.zone_brief(ctx["demand"], th), "supply": gtf.zone_brief(ctx["supply"], th)}


def record_entry(db: Session, zone_id: int, kind: str, price: float, ts: datetime, distal: float, th: dict, side: str) -> None:
    """An entry happened on a stored zone (seen by the scan as it went through the candles): the pending plans
    that were waiting for that entry type now hold the real entry, stop loss and target."""
    sign = 1 if side == "demand" else -1
    for p in db.scalars(select(TradePlan).where(TradePlan.zone_id == zone_id, TradePlan.is_current.is_(True), TradePlan.status == "pending")):
        if (p.entry_type or "TYPE_1") != kind:
            continue
        p.entry, p.stop = float(price), float(distal)
        p.target = float(price + sign * th["target_rr"] * abs(price - distal))
        p.status, p.triggered_ts = "triggered", ts


def refresh(db: Session, key: str, tf: str, snap: dict, th: dict, now: datetime) -> None:
    """Rescore every active zone of one instrument and timeframe, then rebuild the plans of each mode that
    executes on this timeframe. snap holds the last closed candle: cmp, low, high, ema (the 20 and 50 EMA) and the candle times."""
    active = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == tf, Zone.status.in_(ACTIVE))).all()
    for z in active:
        usable = gtf.ltf_ok(z) and counted_tests(z, th) <= th["max_wick_tests"]
        z.score, z.label = ltf_score(z, snap["ema"], th) if usable else htf_score(z, th)
    # A zone that is gone cancels its pending plans, in every mode. Only those few plans are read, not the dead zones.
    for p in db.scalars(select(TradePlan).join(Zone, Zone.id == TradePlan.zone_id).where(
            Zone.instrument_key == key, Zone.timeframe == tf, Zone.status.notin_(ACTIVE),
            TradePlan.is_current.is_(True), TradePlan.status == "pending")):
        p.status = "cancelled"
    # Every active zone on a higher timeframe, read once: the HTF contact of each setup is worked out from these.
    higher = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.status.in_(ACTIVE),
                                           Zone.timeframe.in_(TIMEFRAMES[:TIMEFRAMES.index(tf)]))).all() if TIMEFRAMES.index(tf) else []
    enabled = settings_store.get(db, "modes") or list(MODES)
    for mode in (m for m in MODES if m in enabled and MODES[m][2] == tf):
        as_of = snap["times"][-1] if len(snap["times"]) else None
        ctx = mode_context(db, key, mode, snap["cmp"], as_of)
        summary = context_summary(ctx, mode, th)
        keep: set[int] = set()
        found = gtf.candidates(window.keep(active, window.start(mode, tf, as_of, th)), ctx, snap["cmp"], snap["ema"], th)
        for rank, (z, total, label) in enumerate(found):
            kind = entry_kind(total, th)
            if kind is None:             # a trade score below 5 is not traded
                continue
            keep.add(z.id)
            since = len(snap["times"]) - bisect_right(snap["times"], z.created_ts)
            state = gtf.zone_state(z, since, snap["low"], snap["high"], snap["cmp"], th)
            plans = gtf.entry_plans(z, th)
            own = plans[gtf.PLAN_KEY[kind]]
            happened = bool(own["triggered"])
            sign = 1 if z.side == "demand" else -1
            entry = float(own["entry"]) if happened else float(z.proximal)      # until the entry happens, the proximal line stands in
            d = {"entry": entry, "stop": float(z.distal), "target_zone_id": None,
                 "target": float(entry + sign * th["target_rr"] * abs(entry - z.distal)),
                 "target_source": "fixed", "reward_risk": float(th["target_rr"])}
            hz = ctx["htf_zone"]       # this mode's HTF zone: checked against the timeframes above it
            touch = {"ltf": contacts(z, higher, snap["cmp"], th),
                     "htf": contacts(hz, higher, snap["cmp"], th, above_tf=MODES[mode][0]) if hz is not None else []}
            fields = dict(mode=mode, plans=plans, context={**summary, "score": total, "label": label, "rank": rank}, zone_state=state,
                          trend=ctx["itf_trend"], allowed=True, location_pos=None, grade=None, shared=touch, entry_type=kind)
            status = "triggered" if happened else "pending"
            when = None if not happened else z.t2_ts if kind == "TYPE_2" else close_time(z.t3_ts, tf) if kind == "TYPE_3" else z.updated_ts
            cur = current_plan(db, z.id, mode)
            if cur is None:
                db.add(TradePlan(zone_id=z.id, version=1, status=status, created_ts=now, triggered_ts=when, **d, **fields))
            elif cur.status == "triggered" and not happened:
                # The entry of an earlier test was taken; this is a new opportunity on the same zone, with its own plan.
                cur.is_current = False
                db.add(TradePlan(zone_id=z.id, version=cur.version + 1, status="pending", created_ts=now, **d, **fields))
            elif cur.status == "triggered":
                for k, v in fields.items():      # the entry stays as it was recorded
                    if k != "entry_type":
                        setattr(cur, k, v)
            else:                                # pending or cancelled: bring it up to date, in place
                if happened:
                    cur.triggered_ts = when
                cur.status = status
                for k, v in {**d, **fields}.items():
                    setattr(cur, k, v)
        # zones of this timeframe that no longer qualify in this mode: their pending plan is withdrawn
        ids = [z.id for z in active if z.id not in keep]
        if ids:
            for p in db.scalars(select(TradePlan).where(TradePlan.zone_id.in_(ids), TradePlan.mode == mode,
                                                        TradePlan.is_current.is_(True), TradePlan.status == "pending")):
                p.status = "cancelled"
