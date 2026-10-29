"""The full GTF result of one instrument in one analysis mode, shaped like the scan result of the original scanner:
price, nearest HTF demand and supply with their score breakdown, market state, curve, HTF trend, ITF trend, trade side,
the best LTF execution zone with its trade score and state, the signal with the entry its score takes, and the three entry types.
Built from stored zones and candles, and saved so the dashboard and chart open at once."""
from bisect import bisect_right
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES, TIMEFRAMES
from ..data.resample import close_time
from ..data.sync import load_candles, now_utc
from ..models import ModeResult, TradePlan, Zone
from . import confirm, gtf, plan, window
from .contact import contacts
from .core import ema_series
from .lifecycle import ACTIVE
from .score import ema_point, entry_kind, htf_score, parts


def zone_card(z, th: dict, total: float | None = None, label: str | None = None) -> dict | None:
    """A zone with its score and the freshness, strength and base breakdown."""
    if z is None:
        return None
    p = parts(z, th)
    if total is None:
        total, label = htf_score(z, th)
    return {"zone_id": getattr(z, "id", None), "timeframe": z.timeframe, "side": z.side, "proximal": z.proximal, "distal": z.distal,
            "pattern": z.pattern, "bases": z.base_count, "distal_from": getattr(z, "distal_from", None), "wick_tests": p["tests"], "testing": bool(z.in_visit), "has_gap": bool(z.has_gap),
            "created_ts": z.created_ts.isoformat() if z.created_ts else None, "score": total, "label": label,
            "freshness": p["freshness"], "strength": p["strength"], "strength_label": p["strength_label"], "base": p["base"]}


def no_side_reason(ctx: dict) -> str:
    itf, band, state = ctx["itf_trend"], ctx["curve"], ctx["market_state"]
    if state == "NO_ZONES":
        return "No active HTF zone on either side of the price"
    if state != "NORMAL":
        return "Only one HTF zone is left (price is expanding), and the ITF trend or HTF trend does not support a trade"
    if band == "UNKNOWN":
        return "The HTF demand and supply zones overlap, so there is no curve"
    if ctx.get("rule") != "trend":
        return "Price is in equilibrium and the ITF trend is sideways"
    if itf == "SIDEWAYS":
        return "The ITF trend is sideways"
    if itf == "UP" and band == "VERY HIGH":
        return "The ITF is up, but price is already at or above the HTF supply"
    if itf == "DOWN" and band == "VERY LOW":
        return "The ITF is down, but price is already at or below the HTF demand"
    return "The curve and the ITF trend do not give a side"


def build(db: Session, key: str, mode: str, cmp: float | None = None) -> dict:
    htf, itf, ltf = MODES[mode]
    th = settings_store.thresholds(db, ltf)
    out = {"instrument_key": key, "mode": mode, "htf": htf, "itf": itf, "ltf": ltf, "cmp": cmp, "market_state": None, "curve": None,
           "htf_trend": None, "itf_trend": None, "itf_reason": None, "trade_side": "NONE", "demand_zone": None, "supply_zone": None,
           "direction_rule": th.get("direction_rule") or "location", "ltf_zone": None, "ltf_zone_count": 0, "other_ltf_zones": [], "skipped_ltf_zones": [], "zone_state": None, "contact_ltf": [], "contact_htf": [], "can_paper": False,
           "confirmation": None,
           "signal": {"signal": "NO_TRADE", "primary_entry": None, "entry": None, "stoploss": None, "target": None, "rr": None,
                      "score": 0.0, "zone_type": None, "reason": None, "waiting": False, "watch": None, "type1": None, "type2": None, "type3": None}}
    bars = load_candles(db, key, ltf, last=300)
    if bars.empty:
        out["signal"]["reason"] = "No candles stored for the LTF yet"
        return out
    closes = bars["close"].to_numpy(dtype=float)
    last = bars.iloc[-1]
    price = float(cmp) if cmp else float(last["close"])
    out["cmp"], out["as_of"] = price, last["ts"].isoformat()
    ema = (float(ema_series(closes, int(th["ema_period"]))[-1]), float(ema_series(closes, int(th.get("ema_period_2") or 50))[-1]))
    as_of = last["ts"].to_pydatetime()
    ctx = plan.mode_context(db, key, mode, price, as_of)
    th_htf = settings_store.thresholds(db, htf)
    out.update(market_state=ctx["market_state"], curve=ctx["curve"], htf_trend=ctx["htf_trend"], itf_trend=ctx["itf_trend"],
               itf_reason=ctx["itf_reason"], trade_side=ctx["side"], demand_zone=zone_card(ctx["demand"], th_htf),
               supply_zone=zone_card(ctx["supply"], th_htf))
    if ctx["side"] == "NONE":
        out["signal"]["reason"] = no_side_reason(ctx)
        return out
    zones = window.keep(db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.timeframe == ltf, Zone.status.in_(ACTIVE))).all(),
                        window.start(mode, ltf, as_of, th))
    skipped: list = []
    found = gtf.candidates(zones, ctx, price, ema, th, skipped)
    out["ltf_zone_count"] = len(found)
    out["skipped_ltf_zones"] = [{**zone_card(sz, th), "why": why} for sz, why in skipped]
    if not found:
        out["signal"]["reason"] = (f"The LTF zone is not traded: {skipped[0][1]}" if skipped
                                   else "No valid LTF zone between the price and the HTF zone")
        return out
    z, total, label = found[0]
    times = [t.to_pydatetime() for t in bars["ts"]]
    # The other qualifying zones that can be traded (score 5 or more), best first: drawn on request on the chart and dashboard.
    out["other_ltf_zones"] = [other_zone(db, oz, ot, ol, mode, times, bars, cmp, price, ema, th) for oz, ot, ol in found[1:]
                              if entry_kind(ot, th) is not None]
    since = len(times) - bisect_right(times, z.created_ts)
    low, high, close = (price, price, price) if cmp else (float(last["low"]), float(last["high"]), float(last["close"]))
    state = gtf.zone_state(z, since, low, high, close, th)
    plans = gtf.entry_plans(z, th, touching=state == "TOUCHING_NOW")
    out["ltf_zone"], out["zone_state"] = zone_card(z, th, total, label), state
    out["ltf_zone"]["ema"] = ema_point(z, ema, th)
    out["signal"].update(score=round(total, 2), zone_type=label, type1=plans["type1"], type2=plans["type2"], type3=plans["type3"])
    higher = db.scalars(select(Zone).where(Zone.instrument_key == key, Zone.status.in_(ACTIVE),
                                           Zone.timeframe.in_(TIMEFRAMES[:TIMEFRAMES.index(ltf)]))).all()
    out["contact_ltf"] = contacts(z, higher, price, th)
    if ctx["htf_zone"] is not None:
        out["contact_htf"] = contacts(ctx["htf_zone"], higher, price, th, above_tf=htf)
    primary = gtf.primary_plan(plans, total, th)
    if primary is None:
        out["signal"]["reason"] = "The trade score is below 5"
        return out
    # The trade score picks the entry: 7 or more takes entry type 1, 5 to 6.5 takes entry type 2 or 3.
    # Until a type 2 or 3 entry happens it has no price yet; the proximal line is shown as the level to watch.
    out["signal"].update(signal=ctx["side"], primary_entry=primary["entry_type"], entry=primary["entry"], stoploss=primary["stoploss"],
                         target=primary["target"], rr=primary["rr"], waiting=not primary["triggered"], watch=gtf.px(z.proximal))
    priced = primary["entry"] if primary["entry"] is not None else z.proximal
    out["signal"]["fees_r"] = gtf.fees_r(priced, z.distal, th, maker_entry=primary["entry_type"] == "TYPE_1")
    out["signal"]["stop_pct"] = round(gtf.stop_pct(z), 4)
    pending = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id, TradePlan.mode == mode, TradePlan.is_current.is_(True),
                                                 TradePlan.status == "pending")).first()
    out["can_paper"] = pending is not None and z.status in ACTIVE
    out["confirmation"] = confirm.summary(confirm.scanner_checks(db, key, mode, ctx["side"], z, out["contact_ltf"], th))
    return out


def other_zone(db: Session, z, total: float, label: str, mode: str, times: list, bars, cmp, price: float, ema, th: dict) -> dict:
    """A qualifying LTF zone other than the best one: its card, state, the entry its score takes, and whether it can be
    paper traded. Shown under the coin on the dashboard when "All LTF zones" is ticked."""
    last = bars.iloc[-1]
    since = len(times) - bisect_right(times, z.created_ts)
    low, high, close = (price, price, price) if cmp else (float(last["low"]), float(last["high"]), float(last["close"]))
    state = gtf.zone_state(z, since, low, high, close, th)
    plans = gtf.entry_plans(z, th, touching=state == "TOUCHING_NOW")
    card = zone_card(z, th, total, label)
    card["ema"] = ema_point(z, ema, th)
    lead = gtf.primary_plan(plans, total, th)
    pending = db.scalars(select(TradePlan).where(TradePlan.zone_id == z.id, TradePlan.mode == mode, TradePlan.is_current.is_(True),
                                                 TradePlan.status == "pending")).first()
    return {"zone": card, "zone_state": state, "score": round(total, 2), "zone_type": label,
            "primary_entry": lead["entry_type"] if lead else None, "entry": lead["entry"] if lead else None,
            "stoploss": lead["stoploss"] if lead else gtf.px(z.distal), "target": lead["target"] if lead else None,
            "waiting": bool(lead) and not lead["triggered"], "watch": gtf.px(z.proximal),
            "stop_pct": round(gtf.stop_pct(z), 4),
            "fees_r": gtf.fees_r(lead["entry"] if lead and lead["entry"] is not None else z.proximal, z.distal, th,
                                 maker_entry=bool(lead) and lead["entry_type"] == "TYPE_1"),
            "can_paper": lead is not None and pending is not None and z.status in ACTIVE}


def candle_close(data: dict) -> datetime | None:
    """Close time of the last LTF candle a result is based on (as_of is that candle's open time)."""
    if not data.get("as_of"):
        return None
    return close_time(datetime.fromisoformat(data["as_of"]), data["ltf"])


def save(db: Session, key: str, mode: str, data: dict, now: datetime | None = None) -> None:
    """Save a result. signal_since moves only when the signal changes (no trade, buy, sell): it is the close of the
    candle on which the current signal first appeared, so it stays put while the signal holds."""
    row = db.get(ModeResult, (key, mode))
    if row is None:
        row = ModeResult(instrument_key=key, mode=mode)
        db.add(row)
    signal, closed = data["signal"]["signal"], candle_close(data)
    if row.signal != signal or row.signal_since is None:
        row.signal_since = closed or now or now_utc()
    row.signal, row.updated_at, row.data, row.candle_ts = signal, now or now_utc(), data, closed


def refresh(db: Session, key: str, changed: set[str] | None = None, now: datetime | None = None) -> int:
    """Rebuild and save the result of every enabled mode. With `changed` (timeframes that got new candles),
    only the modes that read one of those timeframes are rebuilt."""
    enabled = settings_store.get(db, "modes") or list(MODES)
    done = 0
    for mode in reversed([m for m in MODES if m in enabled]):     # highest mode first: mode agreement reads the modes above
        if changed is not None and not (set(MODES[mode]) & changed) and db.get(ModeResult, (key, mode)) is not None:
            continue
        save(db, key, mode, build(db, key, mode), now)
        done += 1
    db.commit()
    return done