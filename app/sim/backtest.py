"""Backtest under GTF rules: the same engine and the same fill rules as live, run over stored candles.
No Binance call is made. Each analysis mode is tested with its own HTF, ITF and LTF, and a candle on a
higher timeframe is only used once it has closed. Results are in R after Binance fees (maker or taker by order type)
and funding at every funding time a trade is open over (see sim/costs.py)."""
import math
from bisect import bisect_right
from datetime import date, datetime, time, timedelta

import numpy as np

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles, now_utc
from ..db import SessionLocal
from ..engine import gtf
from ..engine.core import TimeframeEngine
from ..engine.lifecycle import ACTIVE
from ..models import BacktestRun, BacktestTrade
from . import costs, fills, trailing

CANCEL: set[int] = set()
ENTRY_PLANS = ("rule", "type1", "type2", "type3")


def run_one(db: Session, key: str, mode: str, start: datetime, end: datetime, opts: dict, cancelled=lambda: False) -> list[dict]:
    """Backtest one instrument in one analysis mode. Returns trades as dicts.

    type1: a limit at the proximal line of a zone whose trade score is 7 or more; filled when price comes back to it.
    type2: for a score of 5 to 6.5: a candle closes inside the zone and the next opens inside it; entry at that open.
    type3: for a score of 5 to 6.5: a candle closes inside the zone and the next closes back out; entry at that close.
    rule:  type 1 for 7 or more, and for 5 to 6.5 the one of types 2 and 3 chosen in the settings.
    A zone is scored at the close of the candle before the one that enters, so nothing looks ahead."""
    htf, itf, ltf = MODES[mode]
    th = settings_store.thresholds(db, ltf)
    min_score = float(opts["min_score"]) if opts.get("min_score") is not None else float(th["min_score"])
    fees_th = {**th, **{k: opts[k] for k in ("fee_maker_pct", "fee_taker_pct", "fee_bnb") if opts.get(k) is not None}}
    use_funding = opts.get("include_funding", th.get("include_funding", True))
    funding = costs.Funding.load(db, key) if use_funding else costs.Funding([])
    entry_plan = opts.get("entry_plan") or "rule"
    two_three = "TYPE_2" if th.get("entry_23") == "TYPE_2" else "TYPE_3"
    want = {"rule": ("TYPE_1", two_three), "type1": ("TYPE_1",), "type2": ("TYPE_2",), "type3": ("TYPE_3",)}[entry_plan]
    rr = float(th["target_rr"])
    df = load_candles(db, key, ltf, until=end)
    h_df = load_candles(db, key, htf, until=end)
    i_df = load_candles(db, key, itf, until=end)
    if df.empty or h_df.empty or i_df.empty:
        return []
    eng = TimeframeEngine(df, ltf, th)
    h_eng = TimeframeEngine(h_df, htf, settings_store.thresholds(db, htf))
    h_close = [close_time(t, htf) for t in h_eng.ts]
    # GTF PDF p.30 caution: the zones one timeframe above the HTF, followed only when that setting is on
    up = gtf.next_htf(mode) if th.get("check_next_htf") else None
    u_df = load_candles(db, key, up, until=end) if up else None
    u_th = settings_store.thresholds(db, up) if up else None
    u_eng = TimeframeEngine(u_df, up, u_th) if up and not u_df.empty else None
    u_close = [close_time(t, up) for t in u_eng.ts] if u_eng is not None else []
    ui, u_at, u_usable = 0, -1, []
    i_closes = i_df["close"].to_numpy(dtype=float)
    i_close = [close_time(t.to_pydatetime(), itf) for t in i_df["ts"]]
    i_open = [t.to_pydatetime() for t in i_df["ts"]]
    i_highs, i_lows = i_df["high"].to_numpy(dtype=float), i_df["low"].to_numpy(dtype=float)
    trail = trailing.options(opts)                     # off unless the run or the settings ask for it
    trail_on, trail_itf = trailing.is_on(trail), trail["trail_tf"] == "itf"
    trail_1m = trail_on and trail["trail_tf"] == "1m"
    # lock with "move": the trade never closes at its target (the target only moves forward), from the fill on
    no_target = trail_on and trail["trail_method"] == "lock" and trail["trail_lock_target"] == "move"
    if trail_1m:      # 1-minute candles move the stop and are checked for the exit, inside each LTF candle
        m_df = load_candles(db, key, "1m", since=start - timedelta(days=1), until=end + timedelta(days=1))
        m_ts = m_df["ts"].to_numpy(dtype="datetime64[ns]") if not m_df.empty else np.array([], dtype="datetime64[ns]")
        m_o, m_h, m_l, m_c = (m_df[col].to_numpy(dtype=float) if not m_df.empty else np.array([]) for col in ("open", "high", "low", "close"))

    def minutes(ts_open: datetime) -> tuple[int, int]:
        """The 1-minute candles inside the LTF candle that opens at ts_open, as an index range (empty without data)."""
        a = int(np.searchsorted(m_ts, np.datetime64(ts_open), side="left"))
        b = int(np.searchsorted(m_ts, np.datetime64(close_time(ts_open, ltf)), side="left"))
        return a, b
    h_ts = list(h_eng.ts)
    hi = ii = 0
    usable_at, usable, itf_at, itf = -1, [], -1, None     # worked out again only when an HTF or ITF candle closes
    pending: dict[int, dict] = {}        # type 1 limits that qualified at the previous close
    scored: dict[int, tuple] = {}        # zones scored 5 to 6.5 at the previous close, waiting for entry type 2 or 3
    open_trades: list[dict] = []
    done: list[dict] = []
    traded: set[int] = set()

    def finish(tr: dict, ts: datetime, price: float, result: str):
        best = tr.pop("best")
        trailer = tr.pop("trailer", None)
        tr.pop("itf_next", None)
        tr.pop("target_live", None)
        tr.pop("stop_live", None)
        if result == "win":
            best = max(best, tr["target"]) if tr["side"] == "demand" else min(best, tr["target"])
        tr["mfe_r"] = round(abs(best - tr["entry"]) / tr["risk"], 2)
        reason = {"win": "target", "loss": "stop"}.get(result, "open")
        fund = funding.per_unit(tr["side"], tr["entry_ts"], ts, tr["entry"])
        got = costs.result_r(tr["side"], tr["entry"], float(price), tr["risk"], tr["entry_type"], reason, fees_th, fund)
        moved = trailer is not None and trailer.moves > 0
        exit_reason = "trail" if moved and result == "loss" else reason
        if exit_reason == "trail":                  # a stop that trailed into profit is a win
            result = "win" if got["r"] > 0 else "loss"
        tr.update(exit_ts=ts, exit_price=float(price), result=result, result_r=round(got["r"], 3),
                  fees_r=round(got["fees_r"], 3), funding_r=round(got["funding_r"], 3), exit_reason=exit_reason,
                  stop_moves=trailer.moves if trailer is not None else 0,
                  final_stop=float(trailer.stop) if trailer is not None else tr["stop"],
                  final_target=float(trailer.target) if trailer is not None and trailer.target is not None else tr["target"])
        done.append(tr)

    def avg_range(i: int, n: int = 14) -> float:
        lo = max(0, i - n)
        return float(np.mean(eng.h[lo:i] - eng.l[lo:i])) if i > lo else 0.0

    def facts(z, ctx, i: int) -> dict:
        """What the zone looked like when it qualified: recorded on the trade for research, never used to decide."""
        height = abs(float(z.proximal) - float(z.distal))
        risk_p = height or 1e-12
        k0 = min(bisect_right(eng.ts, z.created_ts), i)
        if z.side == "demand":
            away = (float(np.max(eng.h[k0:i + 1])) - float(z.proximal)) if i >= k0 else 0.0
        else:
            away = (float(z.proximal) - float(np.min(eng.l[k0:i + 1]))) if i >= k0 else 0.0

        def room(zones) -> float | None:
            other = "supply" if z.side == "demand" else "demand"
            gaps = [(float(o.proximal) - float(z.proximal)) if z.side == "demand" else (float(z.proximal) - float(o.proximal))
                    for o in zones if o.side == other and o.status in ACTIVE]
            gaps = [g for g in gaps if g > 0]
            return round(min(gaps) / risk_p, 2) if gaps else None
        hz = ctx.get("htf_zone")
        nested = None
        if hz is not None:
            lo1, hi1 = sorted((float(z.proximal), float(z.distal)))
            lo2, hi2 = sorted((float(hz.proximal), float(hz.distal)))
            nested = bool(lo1 <= hi2 and lo2 <= hi1)
        return {"room_ltf_r": room(eng.active), "room_htf_r": room(h_eng.active), "departure": round(away / risk_p, 2),
                "base_count": int(z.base_count or 0), "nested": nested, "zone_age": int(max(0, i - k0 + 1))}

    def start_trailing(tr: dict, i: int) -> None:
        """At the fill: the trailer of this trade, fed from the next candle of the trailing timeframe on."""
        if not trail_on:
            return
        if trail_itf:
            prior = list(zip(i_highs[max(0, ii - 20):ii], i_lows[max(0, ii - 20):ii], i_closes[max(0, ii - 20):ii]))
            tr["itf_next"] = ii
        elif trail_1m and minutes(eng.ts[i])[0] > 0:
            a = minutes(eng.ts[i])[0]
            prior = list(zip(m_h[max(0, a - 20):a], m_l[max(0, a - 20):a], m_c[max(0, a - 20):a]))
        else:
            prior = list(zip(eng.h[max(0, i - 20):i], eng.l[max(0, i - 20):i], eng.c[max(0, i - 20):i]))
        tr["trailer"] = trailing.Trailer(tr["side"], tr["entry"], tr["stop"], fees_th, trail, prior, target=tr["target"])
        tr["trail_tf_used"] = trail["trail_tf"]
        if tr["trailer"].target_removed:
            tr["target_live"] = math.inf if tr["side"] == "demand" else -math.inf

    def moved(tr: dict, new_stop) -> None:
        if new_stop is not None:
            tr["stop_live"] = new_stop
            if tr["trailer"].target_removed:
                tr["target_live"] = math.inf if tr["side"] == "demand" else -math.inf

    def trade(z, total, label, ctx, entry, stop, target, kind, ts):
        return {"instrument_key": key, "timeframe": ltf, "mode": mode, "side": z.side, "score": total, "label": label,
                "entry": float(entry), "stop": float(stop), "target": float(target), "risk": abs(entry - stop),
                "reward_risk": round(abs(target - entry) / abs(entry - stop), 2), "pattern": z.pattern,
                "zone_created_ts": z.created_ts, "zone_proximal": float(z.proximal), "zone_distal": float(z.distal),
                "curve": ctx["curve"], "trend": ctx["itf_trend"], "entry_type": kind,
                "entry_ts": ts, "best": float(entry)}

    n = len(eng)
    for i in range(n):
        if i % 2000 == 0 and cancelled():
            return []
        ts, o, h, l, c = eng.ts[i], eng.o[i], eng.h[i], eng.l[i], eng.c[i]
        in_range = start <= ts <= end
        # 1. exits for trades opened on earlier candles
        for tr in list(open_trades):
            if trail_1m and "trailer" in tr:
                a, b = minutes(ts)
                if b > a:              # minute by minute: the exit is checked, then the minute's close moves the stop
                    hit = None
                    for k in range(a, b):
                        hit = fills.exit_after_entry(tr["side"], tr.get("stop_live", tr["stop"]), tr.get("target_live", tr["target"]),
                                                     m_o[k], m_h[k], m_l[k])
                        if hit:
                            open_trades.remove(tr)
                            finish(tr, m_ts[k].astype("datetime64[us]").item(), hit[0], hit[1])
                            break
                        tr["best"] = max(tr["best"], m_h[k]) if tr["side"] == "demand" else min(tr["best"], m_l[k])
                        moved(tr, tr["trailer"].close_bar(m_h[k], m_l[k], m_c[k]))
                    continue
                tr["trail_tf_used"] = "ltf"       # no 1-minute candles for this stretch: the LTF candle stands in
            hit = fills.exit_after_entry(tr["side"], tr.get("stop_live", tr["stop"]), tr.get("target_live", tr["target"]), o, h, l)
            if hit:
                open_trades.remove(tr)
                finish(tr, ts, hit[0], hit[1])
            else:
                tr["best"] = max(tr["best"], h) if tr["side"] == "demand" else min(tr["best"], l)
                if "trailer" in tr and not trail_itf:      # the candle has closed with the trade still open
                    moved(tr, tr["trailer"].close_bar(h, l, c))
        # 2. type 1 limits that qualified at the previous close
        if in_range:
            for zid, od in list(pending.items()):
                if not fills.entry_touched(od["side"], od["entry"], h, l):
                    continue
                traded.add(zid)
                tr = dict(od, entry_ts=ts, approach=round(float((h - l) / avg_range(i)), 2) if avg_range(i) > 0 else None)
                tr.update(facts(*tr.pop("_q")))         # the zone as it stood at the last close, before this candle
                if fills.stop_hit(od["side"], od["stop"], h, l):   # entry and stop in one candle counts as a loss
                    finish(tr, ts, od["stop"], "loss")
                else:
                    start_trailing(tr, i)
                    open_trades.append(tr)
        # 3. the engines see this candle only now that it has closed
        events = eng.step(i)
        # 3a. entry types 2 and 3 that this candle completed, for zones scored at the previous close
        if in_range:
            for e in events:
                kind = {"entry2": "TYPE_2", "entry3": "TYPE_3"}.get(e["type"])
                z = e["zone"]
                if kind is None or kind not in want or z.id not in scored or z.id in traded:
                    continue
                total, label, was = scored[z.id]
                entry, sign = float(e["price"]), 1 if z.side == "demand" else -1
                if abs(entry - z.distal) <= 0:
                    continue
                traded.add(z.id)
                tr = trade(z, total, label, was, entry, z.distal, entry + sign * rr * abs(entry - z.distal), kind, ts)
                tr.update(facts(z, was, i), approach=round(float((h - l) / avg_range(i)), 2) if avg_range(i) > 0 else None)
                far = (math.inf if z.side == "demand" else -math.inf) if no_target else tr["target"]
                hit = fills.exit_after_entry(z.side, tr["stop"], far, o, h, l) if kind == "TYPE_2" else None
                if hit:                    # entered at the open: this same candle can already reach the stop or the target
                    finish(tr, ts, hit[0], hit[1])
                else:
                    start_trailing(tr, i)
                    open_trades.append(tr)
        t_close = close_time(ts, ltf)
        while hi < len(h_eng) and h_close[hi] <= t_close:
            h_eng.step(hi)
            hi += 1
        while ii < len(i_close) and i_close[ii] <= t_close:
            ii += 1
        while u_eng is not None and ui < len(u_eng) and u_close[ui] <= t_close:
            u_eng.step(ui)
            ui += 1
        if trail_on and trail_itf:            # ITF candles that closed: they move the stops of trades trailed on the ITF
            for tr in open_trades:
                if "trailer" not in tr:
                    continue
                for j in range(tr["itf_next"], ii):
                    if i_open[j] > tr["entry_ts"]:
                        moved(tr, tr["trailer"].close_bar(i_highs[j], i_lows[j], i_closes[j]))
                tr["itf_next"] = ii
        # 4. what the HTF and ITF say now, and which execution zones qualify for the next candle
        pending, scored = {}, {}
        last_bar = i + 1 >= n
        if last_bar or eng.ts[i + 1] < start:
            continue
        if usable_at != hi:
            usable_at, usable = hi, gtf.usable_htf(h_eng.active, th)
        if itf_at != ii:
            itf_at, itf = ii, gtf.itf_trend(i_closes[:ii], th)
        ctx = gtf.context(h_eng.active, h_eng.c[:hi], i_closes[:ii], float(c), th, usable, itf)
        if u_eng is not None:
            if u_at != ui:
                u_at, u_usable = ui, gtf.usable_htf(u_eng.active, u_th)
            ctx.update(above=u_usable, above_tf=up)
        if ctx["side"] == "NONE":
            continue
        for z, total, label in gtf.candidates(eng.active, ctx, float(c), eng.emas(i), th):
            if total < max(min_score, 5.0) or z.id in traded:
                continue
            if total >= 7:
                if "TYPE_1" in want and not z.in_visit and z.touches == 0:
                    sign = 1 if z.side == "demand" else -1
                    pending[z.id] = {**trade(z, total, label, ctx, z.proximal, z.distal, z.proximal + sign * rr * abs(z.proximal - z.distal), "TYPE_1", None),
                                     "_q": (z, ctx, i)}       # the zone as it qualified, for its facts once filled
            elif "TYPE_2" in want or "TYPE_3" in want:
                scored[z.id] = (total, label, ctx)
    for tr in open_trades:   # still open at the end of the data: valued at the last close, kept out of the statistics
        finish(tr, eng.ts[-1], float(eng.c[-1]), "open")
    return done


def summarize(trades: list[dict]) -> dict:
    closed = sorted((t for t in trades if t["result"] in ("win", "loss")), key=lambda t: (t["exit_ts"], t["entry_ts"]))

    def stats(rows: list[dict]) -> dict:
        if not rows:
            return {"trades": 0, "win_rate": None, "expectancy": None}
        wins = [t for t in rows if t["result"] == "win"]
        return {"trades": len(rows), "win_rate": round(100 * len(wins) / len(rows), 1),
                "expectancy": round(sum(t["result_r"] for t in rows) / len(rows), 3)}

    def by(field: str) -> dict:
        keys = sorted({t.get(field) for t in closed if t.get(field) is not None}, key=str)
        return {str(k): stats([t for t in closed if t.get(field) == k]) for k in keys}

    out = stats(closed)
    gain = sum(t["result_r"] for t in closed if t["result_r"] > 0)
    loss = -sum(t["result_r"] for t in closed if t["result_r"] < 0)
    equity, cum, peak, dd, streak, worst = [], 0.0, 0.0, 0.0, 0, 0
    for t in closed:
        cum += t["result_r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        streak = streak + 1 if t["result"] == "loss" else 0
        worst = max(worst, streak)
        equity.append([t["exit_ts"].isoformat(), round(cum, 3)])
    out.update(total_r=round(cum, 3), profit_factor=round(gain / loss, 2) if loss > 0 else None,
               max_drawdown_r=round(dd, 3), longest_losing_streak=worst,
               open_trades=sum(1 for t in trades if t["result"] == "open"), equity=equity,
               reached={str(n): sum(1 for t in closed if (t.get("mfe_r") or 0) >= n) for n in (1, 2, 3, 5, 10)},
               best_trade_r=max((t["result_r"] for t in closed), default=None),
               worst_trade_r=min((t["result_r"] for t in closed), default=None),
               by_score=by("score"), by_label=by("label"), by_mode=by("mode"), by_curve=by("curve"), by_timeframe=by("timeframe"))
    return out


def execute(run_id: int) -> None:
    """Runs in a worker thread with its own database session."""
    with SessionLocal() as db:
        run = db.get(BacktestRun, run_id)
        s = run.settings
        start = datetime.combine(date.fromisoformat(s["start"]), time.min)
        end = datetime.combine(date.fromisoformat(s["end"]), time.max).replace(microsecond=0)
        jobs = [(k, m) for k in s["symbols"] for m in s["modes"]]
        trades: list[dict] = []
        try:
            for n, (key, mode) in enumerate(jobs, 1):
                if run_id in CANCEL:
                    break
                run.progress = f"{n} of {len(jobs)}: {key} {mode}"
                db.commit()
                trades += run_one(db, key, mode, start, end, s, cancelled=lambda: run_id in CANCEL)
            if run_id in CANCEL:
                run.status = "cancelled"
            else:
                db.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run_id))
                for t in trades:
                    db.add(BacktestTrade(run_id=run_id, **{k: v for k, v in t.items() if k != "risk"}))
                run.summary, run.status, run.progress = summarize(trades), "done", f"{len(trades)} trades"
        except Exception as exc:  # shown on the page
            db.rollback()
            run = db.get(BacktestRun, run_id)
            run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            CANCEL.discard(run_id)
            run.ended_at = now_utc()
            db.commit()
