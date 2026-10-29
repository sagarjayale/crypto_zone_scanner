"""Scan-date check: what the GTF rules said about a stock on one date, using only the candles up to that date,
and what then happened to the entry its trade score takes, in the candles after it. No Binance call is made.
Results are in R after fees and funding (sim/costs.py).

A trade score of 7 or more takes entry type 1; a score of 5 to 6.5 takes entry types 2 and 3 (both are followed).
The entry has to come on the zone's next test: once that test is over the zone is no longer what was scored."""
from datetime import date, datetime, time

import numpy as np
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles
from ..engine import gtf
from ..engine.contact import contacts
from ..engine.core import TimeframeEngine
from ..engine.result import no_side_reason, zone_card
from ..engine import window
from ..engine.score import ema_point
from . import costs, fills

PLANS = ("type1", "type2", "type3")
ENTERED = ("target hit", "stopped", "open")


def _closed(side: str, entry: float, exit_price: float, stop: float, kind: str, reason: str, t_in, t_out, money: dict) -> dict:
    """Result in R after fees and funding, with the two cost parts shown on their own."""
    fund = money["funding"].per_unit(side, t_in, t_out, entry)
    got = costs.result_r(side, entry, exit_price, abs(entry - stop), kind, reason, money["th"], fund)
    return {"result_r": round(got["r"], 2), "fees_r": round(got["fees_r"], 2), "funding_r": round(got["funding_r"], 2)}


def _walk(eng: TimeframeEngine, start: int, side: str, entry: float, stop: float, target: float, kind: str, t_in, money: dict) -> dict:
    """Follow an open position from candle `start` to its exit, with the fill rules the backtest uses."""
    for i in range(start, len(eng)):
        hit = fills.exit_after_entry(side, stop, target, eng.o[i], eng.h[i], eng.l[i])
        if hit:
            reason = "target" if hit[1] == "win" else "stop"
            return {"status": "target hit" if hit[1] == "win" else "stopped", "exit_price": float(hit[0]), "exit_ts": eng.ts[i].isoformat(),
                    **_closed(side, entry, float(hit[0]), stop, kind, reason, t_in, eng.ts[i], money)}
    return {"status": "open", "exit_price": float(eng.c[-1]), "exit_ts": eng.ts[-1].isoformat(),
            **_closed(side, entry, float(eng.c[-1]), stop, kind, "open", t_in, eng.ts[-1], money)}


def check(db: Session, key: str, mode: str, scan_date: date, opts: dict | None = None) -> dict:
    """opts may override the fee settings (fee_maker_pct, fee_taker_pct, fee_bnb) and include_funding."""
    htf, itf, ltf = MODES[mode]
    th = settings_store.thresholds(db, ltf)
    opts = {k: v for k, v in (opts or {}).items() if v is not None}
    cost_th = {**th, **opts}
    money = {"th": cost_th, "funding": costs.Funding.load(db, key) if cost_th.get("include_funding", True) else costs.Funding([])}
    out = {"instrument_key": key, "mode": mode, "htf": htf, "itf": itf, "ltf": ltf, "scan_date": str(scan_date), "scan_ts": None, "cmp": None,
           "market_state": None, "curve": None, "htf_trend": None, "itf_trend": None, "itf_reason": None, "trade_side": "NONE",
           "demand_zone": None, "supply_zone": None, "ltf_zone": None, "zone_state": None, "contact_ltf": [], "contact_htf": [],
           "direction_rule": th.get("direction_rule") or "location",
           "signal": {"signal": "NO_TRADE", "score": 0.0, "zone_type": None, "reason": None, "entry": None, "stoploss": None, "target": None,
                      "primary_entry": None, "waiting": False, "rr": None, "type1": None, "type2": None, "type3": None},
           "outcomes": {p: {"status": "not taken", "note": "no trade on the scan date"} for p in PLANS}, "last_ts": None}
    df, h_df, i_df = load_candles(db, key, ltf), load_candles(db, key, htf), load_candles(db, key, itf)
    if df.empty or h_df.empty or i_df.empty:
        out["signal"]["reason"] = "Candles are missing for one of this mode's timeframes"
        return out
    eng = TimeframeEngine(df, ltf, th)
    day_end = datetime.combine(scan_date, time(23, 59, 59))
    seen = [n for n, t in enumerate(eng.ts) if close_time(t, ltf) <= day_end]
    if not seen:
        out["signal"]["reason"] = f"No {ltf} candle had closed by {scan_date}. Stored candles start {eng.ts[0].date()}."
        return out
    cut = seen[-1]                       # the last LTF candle closed on or before the scan date
    for i in range(cut + 1):
        eng.step(i)
    t_close = close_time(eng.ts[cut], ltf)
    h_eng = TimeframeEngine(h_df, htf, settings_store.thresholds(db, htf))
    hi = 0
    while hi < len(h_eng) and close_time(h_eng.ts[hi], htf) <= t_close:
        h_eng.step(hi)
        hi += 1
    # scanner.py's history windows, as of the scan date (all history when the zone_history setting says so)
    h_from, i_from, l_from = (window.start(mode, tf, scan_date, th) for tf in (htf, itf, ltf))
    i_ok = [close_time(t.to_pydatetime(), itf) <= t_close and (i_from is None or t.to_pydatetime() >= i_from) for t in i_df["ts"]]
    i_closes = i_df["close"].to_numpy(dtype=float)[i_ok]
    h_closes = [c for t, c in zip(h_eng.ts[:hi], h_eng.c[:hi]) if h_from is None or t >= h_from]
    price = float(eng.c[cut])
    ctx = gtf.context(window.keep(h_eng.active, h_from), np.array(h_closes, dtype=float), i_closes, price, th)
    up = gtf.next_htf(mode) if th.get("check_next_htf") else None
    if up:                                   # GTF PDF p.30 caution: the zones one timeframe above the HTF, as of the scan date
        u_df = load_candles(db, key, up)
        if not u_df.empty:
            u_th = settings_store.thresholds(db, up)
            u_eng = TimeframeEngine(u_df, up, u_th)
            for k in range(len(u_eng)):
                if close_time(u_eng.ts[k], up) > t_close:
                    break
                u_eng.step(k)
            ctx.update(above=gtf.usable_htf(u_eng.active, u_th), above_tf=up)
    th_htf = settings_store.thresholds(db, htf)
    out.update(scan_ts=eng.ts[cut].isoformat(), cmp=price, last_ts=eng.ts[-1].isoformat(), market_state=ctx["market_state"], curve=ctx["curve"],
               htf_trend=ctx["htf_trend"], itf_trend=ctx["itf_trend"], itf_reason=ctx["itf_reason"], trade_side=ctx["side"],
               demand_zone=zone_card(ctx["demand"], th_htf), supply_zone=zone_card(ctx["supply"], th_htf))
    if ctx["side"] == "NONE":
        out["signal"]["reason"] = no_side_reason(ctx)
        return out
    emas = eng.emas(cut)
    skipped: list = []
    found = gtf.candidates(window.keep(eng.active, l_from), ctx, price, emas, th, skipped)
    if not found:
        out["signal"]["reason"] = (f"The LTF zone is not traded: {skipped[0][1]}" if skipped
                                   else "No valid LTF zone between the price and the HTF zone")
        return out
    z, total, label = found[0]
    made_at = eng.ts.index(z.created_ts)
    state = gtf.zone_state(z, cut - made_at, float(eng.l[cut]), float(eng.h[cut]), price, th)
    plans = gtf.entry_plans(z, th)
    out["ltf_zone"], out["zone_state"] = zone_card(z, th, total, label), state
    out["ltf_zone"]["ema"] = ema_point(z, emas, th)
    out["contact_ltf"] = contacts(z, h_eng.active, price, th)
    if ctx["htf_zone"] is not None:
        out["contact_htf"] = contacts(ctx["htf_zone"], [], price, th, above_tf=htf)
    sig = out["signal"]
    sig.update(score=round(total, 2), zone_type=label, type1=plans["type1"], type2=plans["type2"], type3=plans["type3"])
    sig.update(stop_pct=round(gtf.stop_pct(z), 4), fees_r=gtf.fees_r(z.proximal, z.distal, cost_th, maker_entry=total >= 7))
    lead = gtf.primary_plan(plans, total, th)
    if lead is None:
        sig["reason"] = "The trade score is below 5"
        return out
    side, sign = z.side, 1 if z.side == "demand" else -1
    rr, stop = float(th["target_rr"]), float(z.distal)
    sig.update(signal=ctx["side"], primary_entry=lead["entry_type"], entry=lead["entry"], stoploss=lead["stoploss"], target=lead["target"],
               rr=lead["rr"], waiting=not lead["triggered"], watch=gtf.px(z.proximal))
    res = out["outcomes"]
    by_score = "type 1" if total >= 7 else "types 2 and 3"
    testing_on_scan = bool(z.in_visit)

    def taken(entry: float, i: int, start: int, kind: str) -> dict:
        """An entry at candle i; the exit is looked for from candle `start` on."""
        target = entry + sign * rr * abs(entry - stop)
        return {"entry": float(entry), "entry_ts": eng.ts[i].isoformat(), "stop": stop, "target": float(target),
                **_walk(eng, start, side, float(entry), stop, float(target), kind, eng.ts[i], money)}

    if total >= 7:
        for p in ("type2", "type3"):
            res[p] = {"status": "not taken", "note": f"a trade score of {total:g} takes entry type 1"}
        res["type1"] = {"status": "never entered", "note": "price did not come back to the zone",
                        "entry": float(z.proximal), "stop": stop, "target": float(z.proximal + sign * rr * abs(z.proximal - stop))}
        if testing_on_scan:
            res["type1"] = {"status": "not taken", "note": "price was already at the zone on the scan date, so the limit had been filled before it"}
    else:
        res["type1"] = {"status": "not taken", "note": f"a trade score of {total:g} takes entry type 2 or 3"}
        for p, done in (("type2", z.t2_entry), ("type3", z.t3_entry)):
            res[p] = ({"status": "not taken", "note": "this entry had already happened before the scan date"} if testing_on_scan and done is not None
                      else {"status": "never entered", "note": "price did not come back to the zone"})

    # Walk forward. The zone was scored for its next test (or the one going on), so the entry must come within that test.
    open_plans = {p for p in PLANS if res[p]["status"] == "never entered"}
    test_seen = testing_on_scan
    for i in range(cut + 1, len(eng)):
        if not open_plans:
            break
        kinds = {e["type"] for e in eng.step(i) if e["zone"] is z}
        if "touched" in kinds:
            if test_seen:                 # a later test: not the opportunity that was scored
                break
            test_seen = True
            if "type1" in open_plans:     # the limit at the proximal line is filled on this candle
                entry = float(z.proximal)
                if fills.stop_hit(side, stop, eng.h[i], eng.l[i]):       # entry and stop loss in one candle counts as a loss
                    res["type1"] = {"entry": entry, "entry_ts": eng.ts[i].isoformat(), "stop": stop, "target": float(entry + sign * rr * abs(entry - stop)),
                                    "status": "stopped", "exit_price": stop, "exit_ts": eng.ts[i].isoformat(),
                                    **_closed(side, entry, stop, stop, "TYPE_1", "stop", eng.ts[i], eng.ts[i], money)}
                else:
                    res["type1"] = taken(entry, i, i + 1, "TYPE_1")
                open_plans.discard("type1")
        if "entry2" in kinds and "type2" in open_plans:
            if abs(z.t2_entry - stop) > 0:
                res["type2"] = taken(z.t2_entry, i, i, "TYPE_2")          # entered at this candle's open: the candle itself can already exit it
            else:
                res["type2"] = {"status": "never entered", "note": "the candle opened on the stop loss line"}
            open_plans.discard("type2")
        if "entry3" in kinds and "type3" in open_plans:
            res["type3"] = taken(z.t3_entry, i, i + 1, "TYPE_3")          # entered at this candle's close
            open_plans.discard("type3")
        over = "violated" in kinds or "consumed" in kinds or (test_seen and not z.in_visit)
        if over:
            why = ("the zone was violated before the entry came" if "violated" in kinds
                   else "price tested the zone and left without the candles this entry needs")
            for p in open_plans:
                res[p] = {"status": "never entered", "note": why}
            break
    return out


def check_key(key: str, mode: str, scan_date: date, opts: dict | None = None) -> dict:
    """check() for one coin with its own database session: runs in a worker process or thread."""
    from ..db import SessionLocal
    with SessionLocal() as db:
        return {**check(db, key, mode, scan_date, opts), "symbol": key}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for p in PLANS:
        got = [r["outcomes"][p] for r in rows]
        entered = [o for o in got if o["status"] in ENTERED]
        closed = [o for o in entered if o["status"] != "open"]
        wins = [o for o in closed if o["status"] == "target hit"]
        out[p] = {"entered": len(entered), "target_hit": len(wins), "stopped": len(closed) - len(wins),
                  "open": len(entered) - len(closed), "never_entered": sum(1 for o in got if o["status"] == "never entered"),
                  "not_taken": sum(1 for o in got if o["status"] == "not taken"),
                  "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                  "total_r": round(sum(o["result_r"] for o in closed), 2),
                  "fees_r": round(sum(o.get("fees_r") or 0 for o in closed), 2),
                  "funding_r": round(sum(o.get("funding_r") or 0 for o in closed), 2)}
    return out
