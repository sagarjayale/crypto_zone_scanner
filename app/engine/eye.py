"""GTF EYE and Premium pages: where each coin's price stands against its HTF demand and supply zones.

For one analysis mode (Daily = 1 day zones, Weekly = 1 week zones, ... Yearly = 1 year zones), every coin gets, per side:
  in_zone      the price is between the proximal and distal lines of an active zone of that side
  reacting     an active zone of that side was touched by one of the last REACT_CANDLES closed HTF candles,
               and the price is now back outside it, moving away (above a demand zone, below a supply zone),
               but not yet further from it than REACT_MAX_R times the zone height (so the move is still going on)
  approaching  the nearest zone of that side is within the approach % of the settings (outside the zone)
  None         none of these (the nearest zone and its distance are still given)
The order of preference is in_zone, reacting, approaching. Active zones are fresh or tested, never violated or used up,
and tested no more often than the max_wick_tests setting allows (as for trading): the test going on, or for reacting the
test that just ended, is not counted.

Volatility: ATR % = the average true range of the last 14 closed daily candles, as a % of the last close.
Crypto perpetuals have no implied volatility (IV comes from options), so this stands in for "low IV / high IV".

Everything is read from the database (zones, candles, the saved scan results); nothing is downloaded. The heavy part
(zones and candles of every coin) is kept for CACHE_SECONDS; the live price is applied on every call."""
import time as _time
from datetime import datetime

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import MODES
from ..data.resample import close_time
from ..data.sync import load_candles, now_utc
from ..models import ModeResult, Zone
from . import window
from .lifecycle import ACTIVE
from .score import htf_score

REACT_CANDLES = 3
REACT_MAX_R = 2.0        # reacting: still within 2 zone heights of the proximal line
ATR_DAYS = 14
CACHE_SECONDS = 60
EYE_MODES = ("15M", "30M", "HOURLY", "2HR", "4HR", "DAILY", "WEEKLY", "MONTHLY", "QUARTERLY", "HALFYEARLY", "YEARLY")
_cache: dict[tuple, tuple[float, dict]] = {}


def _closed(df, tf: str, now: datetime):
    """Only the candles that have closed (a derived quarter, half year or year can include the one still forming)."""
    if df.empty:
        return df
    keep = [close_time(t.to_pydatetime(), tf) <= now for t in df["ts"]]
    return df[keep]


def atr_pct(daily) -> float | None:
    """Average true range of the last ATR_DAYS closed daily candles, as a % of the last close."""
    if daily is None or len(daily) < 2:
        return None
    h, l, c = (daily[k].to_numpy(dtype=float) for k in ("high", "low", "close"))
    prev = c[:-1]
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
    tr = tr[-ATR_DAYS:]
    return round(float(np.mean(tr)) / float(c[-1]) * 100, 3) if c[-1] > 0 and len(tr) else None


def _card(z, price: float, state: str | None, th: dict | None = None) -> dict:
    lo, hi = sorted((float(z.proximal), float(z.distal)))
    if lo <= price <= hi:
        dist = 0.0
    elif z.side == "demand":
        dist = (price - float(z.proximal)) / price * 100
    else:
        dist = (float(z.proximal) - price) / price * 100
    # the score as the app gives it for trading (htf_score: the test going on is not counted under the GTF PDF rules)
    score, label = htf_score(z, th) if th else (float(z.score or 0), z.label)
    return {"zone_id": z.id, "side": z.side, "proximal": float(z.proximal), "distal": float(z.distal), "pattern": z.pattern,
            "score": float(score), "label": label, "status": z.status, "touches": int(z.touches or 0),
            "created_ts": z.created_ts.isoformat() if z.created_ts else None, "state": state, "distance_pct": round(dist, 3)}


def _tests_before(z) -> int:
    """Tests the zone had before the one going on now (or before the one that just ended, for a zone being left)."""
    return max(int(z.touches or 0) - (1 if getattr(z, "in_visit", False) else 0), 0)


def side_view(zones: list, side: str, price: float, candles: list[tuple[float, float, float]], approach_pct: float,
              max_tests: int | None = None, th: dict | None = None) -> dict | None:
    """Where the price stands against the zones of one side. candles: (high, low, close) of the last closed HTF candles,
    oldest first, optionally with the candle's open time as a 4th item. th: the HTF thresholds, for the zone score. max_tests: zones tested more often than this are left out (None: no limit).
    Returns the zone card with its state, or None when the coin has no usable zone of that side."""
    zs = [z for z in zones if z.side == side]
    if not zs or not price:
        return None
    if max_tests is not None:
        # a zone that a recent candle touched counts that touch as the reaction, not as an earlier test
        zs = [z for z in zs if _tests_before(z) <= max_tests or (int(z.touches or 0) - 1 <= max_tests and not getattr(z, "in_visit", False))]
        if not zs:
            return None
    demand = side == "demand"
    inside = [z for z in zs if min(z.proximal, z.distal) <= price <= max(z.proximal, z.distal)]
    if inside:
        return _card(max(inside, key=lambda z: (float(z.score or 0), z.created_ts or datetime.min)), price, "in_zone", th)
    # zones on the trading side of the price: below it for demand, above it for supply; nearest first
    out = sorted((z for z in zs if (z.proximal < price if demand else z.proximal > price)),
                 key=lambda z: abs(price - float(z.proximal)))
    if not out:
        return None
    recent = candles[-REACT_CANDLES:]
    if recent:
        for z in out:
            # only candles after the leg-out count: the zone's own base candles are inside it by definition
            after = [c for c in recent if len(c) < 4 or z.created_ts is None or c[3] > z.created_ts]
            touched = any((c[1] <= z.proximal) if demand else (c[0] >= z.proximal) for c in after)
            height = abs(float(z.proximal) - float(z.distal))
            if touched and height > 0 and abs(price - float(z.proximal)) <= REACT_MAX_R * height:
                card = _card(z, price, "reacting", th)
                card["reaction_r"] = round(abs(price - float(z.proximal)) / height, 2)
                return card
    if max_tests is not None:        # approaching: the test that would begin must still be allowed
        out = [z for z in out if _tests_before(z) <= max_tests]
        if not out:
            return None
    near = out[0]
    card = _card(near, price, None, th)
    if card["distance_pct"] <= float(approach_pct or 0):
        card["state"] = "approaching"
    return card


def _heavy(db: Session, keys: list[str], mode: str, max_tests: int | None, th: dict | None = None) -> dict:
    """Zones, last HTF candles, ATR % and the saved scan result of every coin: the part that is kept for a minute.
    Zones are those the live scanner reads: inside the history window of the timeframe (engine/window.py, e.g. 30 days
    for 15 min zones), unless the zone_history setting says all."""
    hit = _cache.get((mode, tuple(keys), max_tests, (th or {}).get("zone_history")))
    if hit and _time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    htf = MODES[mode][0]
    now = now_utc()
    zones: dict[str, list] = {k: [] for k in keys}
    if keys:
        since = window.start(mode, htf, now, th or {})
        for z in window.keep(db.scalars(select(Zone).where(Zone.instrument_key.in_(keys), Zone.timeframe == htf,
                                                         Zone.status.in_(ACTIVE))).all(), since):
            zones[z.instrument_key].append(z)
        for z in (z for zl in zones.values() for z in zl):
            db.expunge(z)                     # kept after the session closes, read only
    saved = {r.instrument_key: r.data or {} for r in db.scalars(select(ModeResult).where(ModeResult.mode == mode, ModeResult.instrument_key.in_(keys)))} if keys else {}
    out = {}
    for k in keys:
        bars = _closed(load_candles(db, k, htf, last=REACT_CANDLES + 2), htf, now)
        daily = _closed(load_candles(db, k, "1d", last=ATR_DAYS + 2), "1d", now)
        data = saved.get(k) or {}
        sig = data.get("signal") or {}
        last_close = float(daily["close"].iloc[-1]) if not daily.empty else None
        out[k] = {"zones": zones[k], "candles": [(float(h), float(l), float(c), t.to_pydatetime()) for h, l, c, t in
                              zip(bars["high"], bars["low"], bars["close"], bars["ts"])][-REACT_CANDLES:],
                  "atr_pct": atr_pct(daily), "scan_price": data.get("cmp") or last_close,
                  "signal": sig.get("signal") or "NO_TRADE", "signal_score": sig.get("score"), "entry": sig.get("entry"),
                  "stoploss": sig.get("stoploss"), "target": sig.get("target"), "trade_side": data.get("trade_side")}
    _cache[(mode, tuple(keys), max_tests, (th or {}).get("zone_history"))] = (_time.time(), out)
    return out


def scan(db: Session, keys: list[str], mode: str, prices: dict, approach_pct: float, th: dict | None = None) -> list[dict]:
    """One row per coin with its demand and supply view, ATR % and the GTF signal of the saved scan.
    th: the thresholds of the mode's HTF (zone score, and max_wick_tests for which zones count)."""
    max_tests = int(th["max_wick_tests"]) if th and th.get("max_wick_tests") is not None else None
    rows = []
    for k, h in _heavy(db, keys, mode, max_tests, th).items():
        price = prices.get(k) or h["scan_price"]
        if not price:
            continue
        price = float(price)
        rows.append({"instrument_key": k, "symbol": k, "price": price, "atr_pct": h["atr_pct"], "signal": h["signal"],
                     "signal_score": h["signal_score"], "entry": h["entry"], "stoploss": h["stoploss"], "target": h["target"],
                     "demand": side_view(h["zones"], "demand", price, h["candles"], approach_pct, max_tests, th),
                     "supply": side_view(h["zones"], "supply", price, h["candles"], approach_pct, max_tests, th)})
    return rows
