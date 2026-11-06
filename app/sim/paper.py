"""Paper trading. No order is ever sent to Binance: fills are simulated against live mark prices from the WebSocket,
with stored 1-minute candles (5-minute where 1-minute ones are missing) as a safety net for anything the live feed
missed. The fill rules are the ones the backtest uses (app/sim/fills.py); fees and funding are those of sim/costs.py.

Size: the quantity that loses paper_risk_pct of the current balance at the stop loss, rounded down to the coin's step
size, and refused when it is under the coin's minimum quantity or minimum order value.
Leverage: the margin set aside is the position value divided by the leverage. The liquidation price is an estimate for
isolated margin with the maintenance margin rate setting (Binance's real rate depends on the position size), and an
order whose liquidation price comes before its stop loss is refused."""
import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings_store
from ..data.sync import load_candles, now_utc
from ..engine.link import ACTIVE
from ..engine.plan import current_plan
from ..models import Instrument, PaperOrder, PaperTrade, TradePlan, Zone
from . import costs, fills, trailing


def live_orders(db: Session, key: str | None = None) -> list[PaperOrder]:
    q = select(PaperOrder).where(PaperOrder.status == "pending", PaperOrder.archived.isnot(True))
    return list(db.scalars(q.where(PaperOrder.instrument_key == key) if key else q).all())


def open_trades(db: Session, key: str | None = None) -> list[PaperTrade]:
    q = select(PaperTrade).where(PaperTrade.exit_ts.is_(None), PaperTrade.archived.isnot(True))
    return list(db.scalars(q.where(PaperTrade.instrument_key == key) if key else q).all())


def balance(db: Session) -> float:
    capital = float(settings_store.get(db, "paper_capital"))
    closed = db.scalars(select(PaperTrade.pnl).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))).all()
    return capital + sum(p or 0 for p in closed)


def _floor_step(qty: float, step: float | None) -> float:
    if not step or step <= 0:
        return qty
    n = math.floor(qty / step + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals)


def quantity(db: Session, key: str, entry: float, stop: float) -> float:
    """The quantity that loses the chosen share of the balance at the stop loss, rounded down to the step size.
    0 when it would be under the coin's minimum quantity or minimum order value."""
    risk_amount = balance(db) * float(settings_store.get(db, "paper_risk_pct")) / 100.0
    per_unit = abs(entry - stop)
    if per_unit <= 0 or entry <= 0:
        return 0.0
    inst = db.get(Instrument, key)
    qty = _floor_step(risk_amount / per_unit, inst.step_size if inst else None)
    if inst is not None and ((inst.min_qty and qty < inst.min_qty) or (inst.min_notional and qty * entry < inst.min_notional)):
        return 0.0
    return qty


def liquidation(side: str, entry: float, leverage: float, mmr_pct: float) -> float:
    """Estimated liquidation price for isolated margin: the price at which the margin left equals the maintenance margin."""
    mmr = mmr_pct / 100.0
    return entry * (1 - 1 / leverage + mmr) if side == "demand" else entry * (1 + 1 / leverage - mmr)


def check_leverage(db: Session, side: str, entry: float, stop: float, leverage: float | None = None) -> tuple[float, float]:
    """(leverage, liquidation price). Raises ValueError when the position would be liquidated before its stop loss."""
    lev = float(leverage or settings_store.get(db, "paper_leverage") or 1)
    if lev < 1 or lev > 125:
        raise ValueError("Leverage must be from 1 to 125.")
    liq = liquidation(side, entry, lev, float(settings_store.get(db, "paper_mmr_pct")))
    if (liq >= stop) if side == "demand" else (liq <= stop):
        raise ValueError(f"At {lev:g}x the estimated liquidation price {liq:.8g} comes before the stop loss {stop:.8g}. Use less leverage.")
    return lev, liq


def _size_error(db: Session, key: str) -> str:
    inst = db.get(Instrument, key)
    need = f" (Binance minimum: {inst.min_qty:g} coins and {inst.min_notional:g} USDT)" if inst and inst.min_qty and inst.min_notional else ""
    return "The position for your risk setting is below this coin's minimum order size" + need + "."


def place(db: Session, zone_id: int, now: datetime, mode: str | None = None, leverage: float | None = None) -> PaperOrder:
    """Create a pending paper order from a zone's plan. Entry type 1 is a limit at the proximal line. Entry types 2
    and 3 wait for the candles to confirm, and fill at the entry the scan records. One live order or position per zone."""
    z = db.get(Zone, zone_id)
    p = current_plan(db, zone_id, mode) if z else None
    if not z or not p:
        raise ValueError("This zone has no trade plan.")
    if z.status not in ACTIVE or p.status != "pending":
        raise ValueError(f"This setup can no longer be traded: the zone is {z.status} and the plan is {p.status}.")
    taken = db.scalars(select(PaperOrder).where(PaperOrder.zone_id == zone_id, PaperOrder.archived.isnot(True),
                                                PaperOrder.status.in_(("pending", "filled")))).first()
    if taken:
        raise ValueError("A paper order already exists for this zone.")
    qty = quantity(db, z.instrument_key, p.entry, p.stop)
    if qty <= 0:
        raise ValueError(_size_error(db, z.instrument_key))
    lev, liq = check_leverage(db, z.side, p.entry, p.stop, leverage)
    order = PaperOrder(signal_ts=signal_time(db, z.instrument_key, mode or p.mode, now), plan_id=p.id, zone_id=z.id, instrument_key=z.instrument_key, timeframe=z.timeframe, side=z.side,
                       quantity=qty, entry=p.entry, stop=p.stop, target=p.target, leverage=lev, liq_price=liq,
                       status="pending", created_at=now, archived=False, entry_type=p.entry_type or "TYPE_1")
    db.add(order)
    db.commit()
    return order


def signal_time(db: Session, key: str, mode: str | None, now: datetime) -> datetime:
    """When the dashboard signal of this coin and mode appeared (saved with each scan), else now."""
    from ..models import ModeResult
    row = db.get(ModeResult, (key, mode)) if mode else None
    return row.signal_since if row is not None and row.signal_since is not None else now


def cancel(db: Session, order_id: int, note: str = "cancelled by you") -> PaperOrder:
    order = db.get(PaperOrder, order_id)
    if not order or order.status != "pending":
        raise ValueError("No pending order with that id.")
    order.status, order.note = "cancelled", note
    db.commit()
    return order


def _fill(db: Session, order: PaperOrder, ts: datetime) -> PaperTrade:
    order.status = "filled"
    lev = order.leverage or float(settings_store.get(db, "paper_leverage") or 1)
    maker, taker = costs.fee_rates(settings_store.thresholds(db, order.timeframe or "1d"))
    rate = maker if costs.entry_is_maker(order.entry_type) else taker
    trade = PaperTrade(order_id=order.id, zone_id=order.zone_id, instrument_key=order.instrument_key, timeframe=order.timeframe,
                       side=order.side, quantity=order.quantity, fill_price=order.entry, fill_ts=ts, stop=order.stop,
                       target=order.target, risk=abs(order.entry - order.stop), kind="zone", archived=False, history=[],
                       entry_type=order.entry_type or "TYPE_1", leverage=lev, margin=order.quantity * order.entry / lev,
                       liq_price=liquidation(order.side, order.entry, lev, float(settings_store.get(db, "paper_mmr_pct"))),
                       fees=order.quantity * order.entry * rate, funding=0.0, signal_ts=order.signal_ts)
    trade.trail = _trail_state(db, order)
    db.add(trade)
    db.flush()
    return trade


class LowReward(ValueError):
    """Entering now leaves less than 1 to 1 between the stop and the target: allowed only once you confirm."""


def enter_now(db: Session, order_id: int, price: float, now: datetime, allow_low_rr: bool = False, dry_run: bool = False) -> dict:
    """Turn a pending order into an open position at the live price now (a market order: taker fee and the slippage
    setting), keeping its stop and target. The size is worked out again from the real entry, so the risk stays at
    paper_risk_pct of the balance. Refused when price is already past the stop or the target, when the size is under
    the coin's minimum, or when the liquidation would come before the stop. Under 1 to 1 it needs allow_low_rr.
    dry_run=True only works the numbers out, for the confirm box."""
    order = db.get(PaperOrder, order_id)
    if not order or order.status != "pending" or order.archived:
        raise ValueError("No pending order with that id.")
    long = order.side == "demand"
    slip = float(settings_store.get(db, "paper_slippage_pct") or 0) / 100.0
    entry = float(price) * (1 + slip if long else 1 - slip)
    stop, target = float(order.stop), float(order.target)
    if (entry <= stop) if long else (entry >= stop):
        raise ValueError(f"The price {entry:.8g} is already past the stop loss {stop:.8g}: this setup is over.")
    if (entry >= target) if long else (entry <= target):
        raise ValueError(f"The price {entry:.8g} is already past the target {target:.8g}: nothing is left to gain.")
    rr = abs(target - entry) / abs(entry - stop)
    qty = quantity(db, order.instrument_key, entry, stop)
    if qty <= 0:
        raise ValueError(_size_error(db, order.instrument_key))
    lev, liq = check_leverage(db, order.side, entry, stop, order.leverage)
    from ..engine.gtf import fees_r
    preview = {"fees_r": fees_r(entry, stop, settings_store.thresholds(db, order.timeframe or "1d"), maker_entry=False),
               "order_id": order.id, "symbol": order.instrument_key, "side": order.side, "price": round(entry, 10),
               "stop": stop, "target": target, "rr": round(rr, 2), "quantity": qty, "risk_usdt": round(qty * abs(entry - stop), 2),
               "leverage": lev, "liq_price": liq, "low_rr": rr < 1}
    if dry_run:
        return preview
    if rr < 1 and not allow_low_rr:
        raise LowReward(f"Entering now leaves only {rr:.2f} to 1 between the stop and the target.")
    order.entry, order.quantity, order.liq_price, order.leverage = entry, qty, liq, lev
    order.entry_type, order.note = "MANUAL", "entered by you at market"
    trade = _fill(db, order, now)
    trade.label = "entered by you"
    db.commit()
    return {**preview, "trade_id": trade.id}


def _trail_state(db: Session, order: PaperOrder) -> dict | None:
    """The trailing stop settings a new position keeps for its whole life (None when trailing is off at the fill)."""
    opts = trailing.options({k: settings_store.get(db, k) for k in trailing.OPTION_KEYS})
    if not trailing.is_on(opts):
        return None
    tf = order.timeframe or "1h"
    if opts["trail_tf"] == "1m":
        tf = "1m"                      # the stop moves at every 1-minute close (the safety net checks each minute too)
    elif opts["trail_tf"] == "itf":
        from ..config import MODES
        p = db.get(TradePlan, order.plan_id) if order.plan_id else None
        if p is not None and p.mode in MODES:
            tf = MODES[p.mode][1]
    # lock with "move": the trade never closes at its target; the target only moves forward, from the fill on
    no_target = opts["trail_method"] == "lock" and opts["trail_lock_target"] == "move"
    return {**opts, "tf": tf, "first_stop": float(order.stop), "first_target": float(order.target), "moves": 0,
            "no_target": no_target, "target_moves": 0}


def target_of(trade: PaperTrade) -> float:
    """The live target: none (out of reach) once a trailing stop that does not keep the target has started."""
    if trade.trail and trade.trail.get("no_target"):
        return math.inf if trade.side == "demand" else -math.inf
    return trade.target


def _stop_reason(trade: PaperTrade) -> str:
    return "trail" if trade.trail and trade.trail.get("moves") else "stop"


def update_trailing(db: Session, trade: PaperTrade, now: datetime) -> list[dict]:
    """Move the stop of an open position with a trailing stop, from the closed candles of its trailing timeframe since
    the fill (worked out again from the start each time, so it is the same as the backtest). The stop only tightens:
    a stop you set by hand that is already tighter stays."""
    st = trade.trail
    if not st or trade.exit_ts is not None:
        return []
    tf = st.get("tf") or trade.timeframe or "1h"
    th = settings_store.thresholds(db, trade.timeframe or "1d")
    prior = load_candles(db, trade.instrument_key, tf, until=trade.fill_ts - timedelta(seconds=1), last=20)
    bars = load_candles(db, trade.instrument_key, tf, since=trade.fill_ts + timedelta(seconds=1))
    t = trailing.Trailer(trade.side, trade.fill_price, st["first_stop"], th, st,
                         list(zip(prior["high"], prior["low"], prior["close"])) if not prior.empty else [],
                         target=st.get("first_target", trade.target))
    for b in bars.itertuples():
        t.close_bar(b.high, b.low, b.close)
    events, log = [], list(trade.history or [])
    tighter = (t.stop > trade.stop) if trade.side == "demand" else (t.stop < trade.stop)
    state = dict(st)
    if t.moves and tighter:
        log.append({"time": now.isoformat(timespec="seconds"), "field": "stop", "old": trade.stop, "new": round(t.stop, 10),
                    "note": f"trailing stop ({st['trail_method']}): {t.why}, best +{t.best_r:.2f} R"})
        trade.stop = float(t.stop)
        state["moves"] = int(state.get("moves") or 0) + 1
        events.append(_event("stop moved", trade, trade.stop, now))
    if t.target_moves > int(state.get("target_moves") or 0):      # lock with "move": the target moved forward
        log.append({"time": now.isoformat(timespec="seconds"), "field": "target", "old": trade.target, "new": round(t.target, 10),
                    "note": f"target moved forward: price reached it, the trade runs on (stop locked at {t.stop:.8g})"})
        trade.target = float(t.target)
        state["target_moves"] = t.target_moves
        events.append(_event("target moved", trade, trade.target, now))
    elif t.target_removed and not state.get("no_target"):
        state["no_target"] = True
        log.append({"time": now.isoformat(timespec="seconds"), "field": "target", "old": trade.target, "new": None,
                    "note": "target removed: the trailing stop lets the trade run"})
    if state != st:
        trade.trail, trade.history = state, log
    return events


def lock_view(trade: PaperTrade, th: dict) -> dict:
    """What an open position has locked, for the Open positions table: the profit if price came back to the stop now
    (before and after fees, in USDT and R) and, for the lock method, how many steps are locked, the price that locks
    the next one and the target now. Worked out from the position as stored, so it is the same on every page load."""
    sign = 1 if trade.side == "demand" else -1
    st = trade.trail or {}
    entry, stop, qty = float(trade.fill_price), float(trade.stop), float(trade.quantity)
    first_stop = float(st.get("first_stop", stop))
    risk_usdt = abs(entry - first_stop) * qty
    _maker, taker = costs.fee_rates(th)
    locked = (stop - entry) * sign * qty
    out = {"method": st.get("trail_method") or "off", "locked_usdt": round(locked, 4),
           "locked_after_fees": round(locked - (trade.fees or 0) - stop * qty * taker, 4),
           "locked_r": round(locked / risk_usdt, 2) if risk_usdt else None, "is_locked": locked > 0,
           "stop": stop, "target": float(trade.target) if trade.target is not None else None}
    if out["method"] != "lock" or st.get("first_target") is None:
        return out
    first_target = float(st["first_target"])
    step = abs(first_target - entry) * float(st.get("trail_lock_pct") or 10) / 100
    gap = step * float(st.get("trail_lock_gap_pct") if st.get("trail_lock_gap_pct") is not None else 20) / 100
    if step <= 0:
        return out
    steps = max(0, int(math.floor((stop - entry) * sign / step + 1e-6)))
    to_target = max(1, int(round(abs(first_target - entry) / step)))       # steps from the entry to the first target
    target_moves = int(st.get("target_moves") or 0)
    shown = max(to_target, steps + 1)                                       # the ladder: every step to the target, and the next one
    out.update(steps=steps, step=step, pct_locked=round(steps * float(st.get("trail_lock_pct") or 10), 2),
               next_trigger=entry + sign * ((steps + 1) * step + gap), next_stop=entry + sign * (steps + 1) * step,
               first_target=first_target, target_moves=target_moves,
               target_moves_on=st.get("trail_lock_target", "move") == "move",
               steps_to_target=to_target, gap=gap, entry=entry, first_stop=first_stop,
               levels=[entry + sign * k * step for k in range(1, shown + 1)],
               target_reached=target_moves > 0, beyond_target=steps > to_target,
               locked_r_per_step=round(step / abs(entry - first_stop), 3) if entry != first_stop else None)
    return out


def order_wait(db: Session, order: PaperOrder) -> dict:
    """Where an entry type 2 or 3 order stands with its zone: waiting for a candle to close inside the zone, or armed
    (a candle has closed inside, so the next candle can enter). Entry type 1 is a limit: it fills on a touch."""
    if order.entry_type not in ("TYPE_2", "TYPE_3") or not order.zone_id:
        return {"wait": "limit"}
    z = db.get(Zone, order.zone_id)
    if z is None:
        return {"wait": "unknown"}
    if z.inside_armed:
        return {"wait": "armed", "since": z.inside_ts.isoformat() if z.inside_ts else None}
    return {"wait": "testing" if z.in_visit else "waiting"}


def funding_so_far(db: Session, trade: PaperTrade, until: datetime) -> float:
    """USDT received (+) or paid (-) at the funding times since the fill, from the stored funding history."""
    if not settings_store.get(db, "include_funding"):
        return 0.0
    return costs.Funding.load(db, trade.instrument_key).per_unit(trade.side, trade.fill_ts, until, trade.fill_price) * trade.quantity


def close(trade: PaperTrade, price: float, reason: str, ts: datetime, db: Session | None = None) -> None:
    """Close at `price`. Profit and loss is after both fees and the funding paid or received while open."""
    sign = 1 if trade.side == "demand" else -1
    trade.exit_price, trade.exit_ts, trade.exit_reason = float(price), ts, reason
    maker, taker = costs.fee_rates(settings_store.thresholds(db, trade.timeframe or "1d")) if db is not None else (0.0002, 0.0005)
    trade.fees = (trade.fees or 0.0) + trade.quantity * price * (maker if costs.exit_is_maker(reason) else taker)
    trade.funding = funding_so_far(db, trade, ts) if db is not None else (trade.funding or 0.0)
    gross = (price - trade.fill_price) * sign * trade.quantity
    trade.pnl = round(gross - trade.fees + trade.funding, 4)
    trade.result = "win" if trade.pnl > 0 else "loss"
    trade.result_r = round(trade.pnl / (trade.risk * trade.quantity), 3) if trade.risk and trade.quantity else None


def _event(kind: str, t, price: float, ts: datetime) -> dict:
    return {"type": "paper", "event": kind, "instrument_key": t.instrument_key, "timeframe": t.timeframe, "side": t.side,
            "price": price, "quantity": t.quantity, "pnl": getattr(t, "pnl", None), "time": ts.isoformat(timespec="seconds")}


def _refresh_order(db: Session, order: PaperOrder) -> bool:
    """Keep a pending order in step with its zone and plan. Returns False if it was cancelled."""
    first = db.get(TradePlan, order.plan_id)
    z, p = db.get(Zone, order.zone_id), current_plan(db, order.zone_id, first.mode if first else None)
    if first is not None and first.status == "triggered":
        return True                    # its own plan was entered: the order is about to be filled, whatever came after
    if not z or z.status not in ACTIVE or not p or p.status == "cancelled":
        order.status, order.note = "cancelled", f"zone {z.status if z else 'removed'}"
        return False
    edited = bool(order.note and order.note.startswith(EDITED))
    if edited and p.status == "pending" and (p.entry, p.entry_type or "TYPE_1") == (order.entry, order.entry_type or "TYPE_1"):
        return True                    # your stop and target stay while the scanner's entry is unchanged
    if p.status == "pending" and (p.entry, p.stop, p.target, p.entry_type or "TYPE_1") != (order.entry, order.stop, order.target, order.entry_type or "TYPE_1"):
        order.note = None              # the plan has a new entry: the order follows it again
        order.plan_id, order.entry, order.stop, order.target = p.id, p.entry, p.stop, p.target   # the plan got a new version
        order.entry_type = p.entry_type or "TYPE_1"
        order.quantity = quantity(db, order.instrument_key, p.entry, p.stop)
        try:
            order.leverage, order.liq_price = check_leverage(db, order.side, p.entry, p.stop, order.leverage)
        except ValueError as exc:
            order.status, order.note = "cancelled", str(exc)[:255]
            return False
        if order.quantity <= 0:
            order.status, order.note = "cancelled", "below the coin's minimum order size"
            return False
    return True


def _confirmed_fill(db: Session, order: PaperOrder) -> list[dict]:
    """Entry types 2 and 3 are not limit orders: they fill when the scan has recorded the entry on the order's plan,
    at the price and candle time of that entry. Type 2 enters at a candle's open, which is only known once that candle
    has closed, so its fill is booked one candle late and the candles since then are checked for the stop and target."""
    p = db.get(TradePlan, order.plan_id)
    if p is None or p.status != "triggered":
        return []
    ts = p.triggered_ts or now_utc()
    order.entry, order.stop, order.target = p.entry, p.stop, p.target
    order.quantity = quantity(db, order.instrument_key, p.entry, p.stop) if p.entry != p.stop else 0.0
    if order.quantity <= 0:
        order.status, order.note = "cancelled", "the entry was on the stop loss line" if p.entry == p.stop else "below the coin's minimum order size"
        return []
    try:
        order.leverage, order.liq_price = check_leverage(db, order.side, p.entry, p.stop, order.leverage)
    except ValueError as exc:
        order.status, order.note = "cancelled", str(exc)[:255]
        return []
    trade = _fill(db, order, ts)
    trade.checked_ts = ts - timedelta(seconds=1)      # the candles from the entry onwards are still to be checked
    return [_event("filled", trade, order.entry, ts)]


def is_limit(order: PaperOrder) -> bool:
    return (order.entry_type or "TYPE_1") == "TYPE_1"


def on_prices(db: Session, prices: dict[str, float], now: datetime) -> list[dict]:
    """Apply one round of live prices: fill pending orders, then check stops and targets."""
    events = []
    for order in live_orders(db):
        if not _refresh_order(db, order):
            continue
        if not is_limit(order):
            events.extend(_confirmed_fill(db, order))
            continue
        ltp = prices.get(order.instrument_key)
        if ltp is not None and fills.entry_touched(order.side, order.entry, ltp, ltp):
            trade = _fill(db, order, now)
            events.append(_event("filled", trade, order.entry, now))
    for trade in open_trades(db):
        ltp = prices.get(trade.instrument_key)
        if ltp is None:
            continue
        if fills.stop_hit(trade.side, trade.stop, ltp, ltp):
            close(trade, ltp, _stop_reason(trade), now, db)   # a stop is a market exit: you get the price that is there
            events.append(_event("stopped", trade, ltp, now))
        elif (ltp >= target_of(trade)) if trade.side == "demand" else (ltp <= target_of(trade)):
            close(trade, trade.target, "target", now, db)  # a target is a limit exit: you get the target price
            events.append(_event("target hit", trade, trade.target, now))
    db.commit()
    return events


def _net_tf(db: Session, key: str) -> tuple[str, timedelta]:
    """The finest stored timeframe for the safety net: 1 minute, or 5 minutes for a coin without 1-minute candles."""
    from ..models import SyncState
    st = db.get(SyncState, (key, "1m"))
    return ("1m", timedelta(minutes=1)) if st is not None and st.last_candle_ts is not None else ("5m", timedelta(minutes=5))


def candle_lag(db: Session, key: str, now: datetime | None = None) -> dict:
    """How far behind the safety net's candles (1 minute, or 5 minutes without them) are for a coin: minutes from the
    close of the newest stored candle to now. A few minutes behind is normal; much more means stops, fills and the
    trailing stop on stored candles wait until the candles catch up."""
    from ..data.resample import close_time
    from ..models import SyncState
    tf, step = _net_tf(db, key)
    st = db.get(SyncState, (key, tf))
    if st is None or st.last_candle_ts is None:
        return {"tf": tf, "minutes": None}
    now = now or now_utc()
    return {"tf": tf, "minutes": max(0, int((now - close_time(st.last_candle_ts, tf)).total_seconds() // 60))}


def on_candles(db: Session, key: str) -> list[dict]:
    """Safety net, run after each candle close: replay the stored candles that the order or position has not seen yet,
    with the same rules as the backtest. Catches anything the live price feed missed."""
    events = []
    tf, step = _net_tf(db, key)
    for order in live_orders(db, key):
        if not _refresh_order(db, order):
            continue
        if not is_limit(order):
            events.extend(_confirmed_fill(db, order))
            continue
        since = max(order.created_at, order.checked_ts + step) if order.checked_ts else order.created_at
        df = load_candles(db, key, tf, since=since)
        for c in df.itertuples():
            ts = c.ts.to_pydatetime()
            order.checked_ts = ts
            if fills.entry_touched(order.side, order.entry, c.high, c.low):
                trade = _fill(db, order, ts)
                trade.checked_ts = ts
                events.append(_event("filled", trade, order.entry, ts))
                if fills.stop_hit(order.side, order.stop, c.high, c.low):   # entry and stop in one candle: a loss
                    close(trade, order.stop, "stop", ts, db)
                    events.append(_event("stopped", trade, order.stop, ts))
                break
    for trade in open_trades(db, key):
        after = trade.checked_ts or trade.fill_ts
        df = load_candles(db, key, tf, since=after + timedelta(seconds=1))
        for c in df.itertuples():
            ts = c.ts.to_pydatetime()
            trade.checked_ts = ts
            hit = fills.exit_after_entry(trade.side, trade.stop, target_of(trade), c.open, c.high, c.low)
            if hit:
                close(trade, hit[0], _stop_reason(trade) if hit[1] == "loss" else "target", ts, db)
                events.append(_event("stopped" if hit[1] == "loss" else "target hit", trade, hit[0], ts))
                break
    for trade in open_trades(db, key):     # after the candles are checked: a trailing stop moves on what has closed
        events.extend(update_trailing(db, trade, now_utc()))
    db.commit()
    return events


def manual_close(db: Session, trade_id: int, price: float, now: datetime) -> PaperTrade:
    trade = db.get(PaperTrade, trade_id)
    if not trade or trade.exit_ts is not None:
        raise ValueError("No open position with that id.")
    slip = float(settings_store.get(db, "paper_slippage_pct")) / 100.0
    close(trade, price * (1 - slip) if trade.side == "demand" else price * (1 + slip), "manual", now, db)
    db.commit()
    return trade


def edit(db: Session, trade_id: int, stop: float | None, target: float | None, now: datetime, price: float | None = None) -> PaperTrade:
    """Change the stop and target of an open position. With the live price given, a stop or target the price is already
    past is refused (it would close the position at once). A trailing stop keeps tightening from what it has locked:
    a stop set looser than that is moved back at the next candle close."""
    trade = db.get(PaperTrade, trade_id)
    if not trade or trade.exit_ts is not None:
        raise ValueError("No open position with that id.")
    new_stop = trade.stop if stop is None else stop
    new_target = trade.target if target is None else target
    if (new_stop >= new_target) if trade.side == "demand" else (new_stop <= new_target):
        raise ValueError("The stop and target are the wrong way round for this position.")
    if price is not None:
        long = trade.side == "demand"
        if stop is not None and ((new_stop >= price) if long else (new_stop <= price)):
            raise ValueError(f"The stop {new_stop:g} is already past the price {price:g}: it would close the position at once.")
        if target is not None and target != trade.target and ((new_target <= price) if long else (new_target >= price)):
            raise ValueError(f"The target {new_target:g} is already past the price {price:g}: it would close the position at once.")
    log = list(trade.history or [])
    for field, value in (("stop", new_stop), ("target", new_target)):
        if value != getattr(trade, field):
            log.append({"time": now.isoformat(timespec="seconds"), "field": field, "old": getattr(trade, field), "new": value})
            setattr(trade, field, value)
    trade.history = log
    db.commit()
    return trade


EDITED = "edited by you"


def edit_order(db: Session, order_id: int, stop: float | None, target: float | None, now: datetime) -> PaperOrder:
    """Change the stop and target of a pending order. The size is worked out again so the order still risks the risk
    setting at its stop, and the leverage check is made again. The entry stays the scanner's. The order then keeps your
    stop and target while its plan's entry stays the same; if the scanner moves the entry, the order follows the plan again."""
    order = db.get(PaperOrder, order_id)
    if not order or order.status != "pending":
        raise ValueError("No pending order with that id.")
    new_stop = order.stop if stop is None else float(stop)
    new_target = order.target if target is None else float(target)
    long = order.side == "demand"
    if not ((new_stop < order.entry < new_target) if long else (new_target < order.entry < new_stop)):
        raise ValueError("For a long the stop must be below the entry and the target above it; for a short the other way round.")
    qty = quantity(db, order.instrument_key, order.entry, new_stop)
    if qty <= 0:
        raise ValueError("With this stop the size is below the coin's minimum order size.")
    order.leverage, order.liq_price = check_leverage(db, order.side, order.entry, new_stop, order.leverage)
    order.stop, order.target, order.quantity = new_stop, new_target, qty
    order.note = f"{EDITED} at {now.isoformat(timespec='seconds')}"
    db.commit()
    return order


def auto_orders(db: Session, rows: list, now: datetime) -> int:
    """Auto mode: place a paper order for every watched setup at or above the chosen score."""
    if not settings_store.get(db, "paper_auto"):
        return 0
    floor, placed = int(settings_store.get(db, "paper_auto_min_score")), 0
    for z, _ in rows:
        if z.score >= floor:
            try:
                place(db, z.id, now)
                placed += 1
            except ValueError:
                pass   # already has an order
    return placed


def summary(db: Session, prices: dict[str, float]) -> dict:
    capital = float(settings_store.get(db, "paper_capital"))
    closed = db.scalars(select(PaperTrade).where(PaperTrade.exit_ts.isnot(None), PaperTrade.archived.isnot(True))).all()
    opened = open_trades(db)
    realised = sum(t.pnl or 0 for t in closed)
    open_pnl, margin = 0.0, 0.0
    for t in opened:
        margin += t.margin or 0.0
        ltp = prices.get(t.instrument_key)
        if ltp is not None:
            open_pnl += (ltp - t.fill_price) * (1 if t.side == "demand" else -1) * t.quantity - (t.fees or 0.0)
    rs = [t.result_r for t in closed if t.result_r is not None]
    wins = sum(1 for t in closed if t.result == "win")
    return {"capital": capital, "balance": round(capital + realised, 2), "realised": round(realised, 2), "open_pnl": round(open_pnl, 2),
            "margin_used": round(margin, 2), "fees_paid": round(sum(t.fees or 0 for t in closed), 2),
            "funding": round(sum(t.funding or 0 for t in closed), 2),
            "closed_trades": len(closed), "open_positions": len(opened), "pending_orders": len(live_orders(db)),
            "win_rate": round(100 * wins / len(closed), 1) if closed else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
            "auto": bool(settings_store.get(db, "paper_auto")), "auto_min_score": settings_store.get(db, "paper_auto_min_score"),
            "risk_pct": settings_store.get(db, "paper_risk_pct"), "leverage": settings_store.get(db, "paper_leverage")}


def reset(db: Session) -> int:
    n = 0
    for model in (PaperOrder, PaperTrade):
        for row in db.scalars(select(model).where(model.archived.isnot(True))).all():
            row.archived = True
            n += 1
    db.commit()
    return n
