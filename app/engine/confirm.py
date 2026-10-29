"""Confirmation score: checks the zone rules do not make, kept apart from the scanner score. All from stored data,
worked out at each scan and shown on the dashboard:

  Mode agreement   2  every higher mode that has a side gives the same side (1 when at least half do)
  HTF contact      1  the entry zone is inside, overlaps or touches a higher-timeframe zone
  BTC agreement    1  BTCUSDT gives the same side in the same mode
  Leg-out volume   1  the candle that formed the zone had confirm_volume_mult times the average volume of the 20 before it
  Strength vs BTC  1  the coin did better than BTC over the last 20 LTF candles for a long, worse for a short
  Funding          1  the crowd is not leaning hard the trade's way: for a long the current funding rate is at most
                      +confirm_funding_pct, for a short at least -confirm_funding_pct
  Open interest    1  open interest rose over the leg-out candle: new positions came in with the move that left the zone

A check that cannot be made (BTC itself, BTC not in the list, no funding or open interest stored for that time) is left
out of both the points and the maximum, so the score is always "points of what could be checked". 75% or more is
strong, 50% or more moderate. Binance serves only about 30 days of open interest history, so the app saves it as it
comes; the open interest check needs a leg-out inside the stored history."""
from bisect import bisect_left, bisect_right
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BTC, MODES
from ..data.resample import close_time
from ..data.sync import load_candles
from ..models import Instrument, ModeResult, OpenInterest

MODE_NAMES = {"15M": "15 min", "30M": "30 min", "HOURLY": "Hourly", "2HR": "2 hr", "4HR": "4 hr", "DAILY": "Daily", "WEEKLY": "Weekly",
              "MONTHLY": "Monthly", "QUARTERLY": "Quarterly", "HALFYEARLY": "Half yearly", "YEARLY": "Yearly"}


def check(key: str, label: str, points: float, most: float, note: str, applicable: bool = True) -> dict:
    return {"key": key, "label": label, "points": points if applicable else 0, "max": most if applicable else 0,
            "applicable": applicable, "passed": applicable and points >= most, "note": note}


def summary(checks: list[dict]) -> dict:
    points, most = sum(c["points"] for c in checks), sum(c["max"] for c in checks)
    pct = round(100 * points / most) if most else None
    label = None if pct is None else "strong" if pct >= 75 else "moderate" if pct >= 50 else "weak"
    return {"points": points, "max": most, "pct": pct, "label": label, "checks": checks}


def agreement(side: str, higher: list[tuple[str, str]]) -> dict:
    """higher: (mode, signal) of every higher mode. 2 points when every higher mode that has a side agrees,
    1 when at least half do, 0 otherwise. Higher modes with no trade are not counted either way."""
    sided = [(m, s) for m, s in higher if s in ("BUY", "SELL")]
    if not sided:
        return check("modes", "Mode agreement", 0, 2, "no higher mode has a side to compare with", applicable=False)
    yes = [MODE_NAMES.get(m, m) for m, s in sided if s == side]
    no = [MODE_NAMES.get(m, m) for m, s in sided if s != side]
    points = 2 if not no else 1 if len(yes) * 2 >= len(sided) else 0
    note = (f"{', '.join(yes)} agree" if yes else "no higher mode agrees") + (f"; {', '.join(no)} against" if no else "")
    return check("modes", "Mode agreement", points, 2, note)


def funding_check(side: str, rate: float | None, limit_pct: float) -> dict:
    """rate is a fraction per funding interval (0.0001 = 0.01%)."""
    if rate is None:
        return check("funding", "Funding", 0, 1, "no funding rate stored for this coin yet", applicable=False)
    pct = rate * 100
    crowded = pct > limit_pct if side == "BUY" else pct < -limit_pct
    who = "longs pay shorts" if pct > 0 else "shorts pay longs" if pct < 0 else "no one pays"
    if crowded:
        return check("funding", "Funding", 0, 1, f"funding {pct:+.4f}% ({who}): the crowd already leans hard the trade's way")
    return check("funding", "Funding", 1, 1, f"funding {pct:+.4f}% ({who}), within {limit_pct:g}% the trade's way")


def oi_change(series: list[tuple], start, end) -> tuple[float, float] | None:
    """Open interest at or before `start` and at or after `end`, from (ts, oi) rows sorted by time. None when the
    stored history does not cover both ends, or both ends fall on the same reading."""
    if not series:
        return None
    times = [t for t, _ in series]
    a = bisect_right(times, start) - 1
    b = bisect_left(times, end)
    if a < 0 or b >= len(series) or b <= a:
        return None
    return series[a][1], series[b][1]


def oi_check(db: Session, key: str, zone) -> dict:
    start = zone.created_ts
    end = close_time(start, zone.timeframe)
    for period in ("5m", "1h"):
        rows = db.execute(select(OpenInterest.ts, OpenInterest.oi).where(
            OpenInterest.instrument_key == key, OpenInterest.period == period,
            OpenInterest.ts >= start - timedelta(hours=2), OpenInterest.ts <= end + timedelta(hours=2)).order_by(OpenInterest.ts)).all()
        got = oi_change([(t, v) for t, v in rows], start, end)
        if got:
            before, after = got
            change = 100 * (after / before - 1) if before else 0.0
            return check("oi", "Open interest", 1 if after > before else 0, 1,
                         f"open interest {change:+.2f}% over the leg-out candle" + ("" if after > before else ": no new positions came in with the move"))
    return check("oi", "Open interest", 0, 1, "no open interest stored for the leg-out candle (Binance keeps about 30 days)", applicable=False)


def scanner_checks(db: Session, key: str, mode: str, side: str, zone, contact_ltf: list[dict], th: dict) -> list[dict]:
    """The seven checks, all from stored data."""
    ltf = MODES[mode][2]
    order = list(MODES)
    higher = [(m, r.signal) for m in order[order.index(mode) + 1:] if (r := db.get(ModeResult, (key, m))) is not None]
    out = [agreement(side, higher)]

    close = [c for c in contact_ltf if c["relation"] in ("inside", "overlap", "touch")]
    out.append(check("contact", "HTF contact", 1 if close else 0, 1,
                     f"{close[0]['relation']} with the {close[0]['tf']} zone" if close else "no higher zone touches the entry zone"))

    is_btc = key == BTC
    btc = None if is_btc else db.get(ModeResult, (BTC, mode))
    if is_btc:
        out.append(check("btc", "BTC agreement", 0, 1, "this is BTC", applicable=False))
    elif btc is None:
        out.append(check("btc", "BTC agreement", 0, 1, "BTCUSDT is not in your coin list", applicable=False))
    else:
        word = {"BUY": "long", "SELL": "short"}.get(btc.signal, "no trade")
        out.append(check("btc", "BTC agreement", 1 if btc.signal == side else 0, 1, f"BTC is {word} in this mode"))

    bars = load_candles(db, key, ltf, until=zone.created_ts, last=21)
    vol = bars["volume"].to_numpy(dtype=float) if not bars.empty else []
    if len(vol) < 6 or vol[:-1].mean() <= 0:
        out.append(check("volume", "Leg-out volume", 0, 1, "no volume history for this zone", applicable=False))
    else:
        ratio = float(vol[-1] / vol[:-1].mean())
        need = float(th.get("confirm_volume_mult") or 1.5)
        out.append(check("volume", "Leg-out volume", 1 if ratio >= need else 0, 1, f"{ratio:.1f} times the average of the candles before it (needs {need:g})"))

    mine = load_candles(db, key, ltf, last=21)
    theirs = None if is_btc else load_candles(db, BTC, ltf, last=21)
    if theirs is None or theirs.empty or len(mine) < 6 or len(theirs) < 6:
        out.append(check("strength", "Strength vs BTC", 0, 1, "no BTC candles on this timeframe to compare with", applicable=False))
    else:
        a = 100 * (float(mine["close"].iloc[-1]) / float(mine["close"].iloc[0]) - 1)
        b = 100 * (float(theirs["close"].iloc[-1]) / float(theirs["close"].iloc[0]) - 1)
        ok = a > b if side == "BUY" else a < b
        out.append(check("strength", "Strength vs BTC", 1 if ok else 0, 1, f"coin {a:+.1f}%, BTC {b:+.1f}% over the last {len(mine) - 1} candles"))

    inst = db.get(Instrument, key)
    out.append(funding_check(side, inst.funding_rate if inst else None, float(th.get("confirm_funding_pct") or 0.03)))
    out.append(oi_check(db, key, zone))
    return out
