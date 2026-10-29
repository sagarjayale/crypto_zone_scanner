"""Trailing stop loss, shared by the backtest and paper trading so both move the stop the same way.

Off by default: with trail_method "off" nothing here runs and every trade keeps its first stop and its target.

The rules (a long; a short is the mirror image):
- R is the distance from the entry to the first stop.
- Trailing starts once price has reached entry + trail_start_r R (1 R by default). Until then the first stop stands.
- The stop is worked out again only when a candle of the trailing timeframe closes, from closed candles alone, so a
  backtest never sees a price before it happened. Candles count from the first one that opened after the fill.
- The stop only ever moves in the trade's favour, and never below break-even once trailing has started.
- Break-even includes the fees: entry x (1 + maker + taker), so a break-even exit is about 0 R.
- Within a candle the stop is checked before it is moved (the caller does that), the cautious assumption.
- trail_keep_target false removes the fixed target once trailing has started, so the trade can run.

Methods:
- step:  stop = entry + (best R reached - 1), rounded down to 0.5 R (at +1 R break-even, +1.5 R -> +0.5 R, +2 R -> +1 R).
- swing: stop just under each new swing low: a candle whose low is below the trail_swing candles on each side,
         confirmed trail_swing candles later, less 0.1 of the average candle range.
- atr:   stop = highest price since the entry - trail_atr_mult x ATR(14) of the trailing timeframe.
- lock:  steps of trail_lock_pct % of the distance from the entry to the first target. Once the best price has passed
         a step by trail_lock_gap_pct % of one step, the stop moves to that step. Entry 100, target 200, 10 %, gap 20 %:
         one step is 10, the gap 2; at 112 the stop goes to 110, at 122 to 120, at 152 to 150. trail_start_r is not used.
         trail_lock_target "move": the trade does not close at the target; each time the best price reaches it, the
         target moves forward one step (200 -> 210 -> 220) and only the locked stop ends the trade.
         "close": the trade closes at the target as usual.
"""
import math

from . import costs

METHODS = ("off", "step", "swing", "atr", "lock")
TIMEFRAMES = ("ltf", "itf", "1m")       # the trade's own timeframe, the mode's middle one, or 1-minute candles
LOCK_TARGET = ("move", "close")        # lock: at the target, move it forward one step, or close the trade there
OPTION_KEYS = ("trail_method", "trail_start_r", "trail_keep_target", "trail_atr_mult", "trail_swing", "trail_tf",
               "trail_lock_pct", "trail_lock_gap_pct", "trail_lock_target")


def options(source: dict) -> dict:
    """The trailing options from settings (or a backtest run), with their defaults."""
    get = source.get
    return {"trail_method": get("trail_method") or "off",
            "trail_start_r": float(get("trail_start_r") if get("trail_start_r") is not None else 1.0),
            "trail_keep_target": bool(get("trail_keep_target")) if get("trail_keep_target") is not None else True,
            "trail_atr_mult": float(get("trail_atr_mult") or 2.0),
            "trail_swing": int(get("trail_swing") or 2),
            "trail_tf": get("trail_tf") or "ltf",
            "trail_lock_pct": float(get("trail_lock_pct") or 10.0),
            "trail_lock_gap_pct": float(get("trail_lock_gap_pct") if get("trail_lock_gap_pct") is not None else 20.0),
            "trail_lock_target": get("trail_lock_target") if get("trail_lock_target") in LOCK_TARGET else "move"}


def is_on(opts: dict) -> bool:
    return (opts.get("trail_method") or "off") != "off"


class Trailer:
    """The trailing stop of one open trade. Feed it the closed candles of the trailing timeframe, in order, starting
    with the first candle that opened after the fill; it returns the new stop when the stop moves.
    target is the trade's first target: the lock method measures its steps from it (other methods do not need it)."""

    def __init__(self, side: str, entry: float, stop: float, th: dict, opts: dict, prior: list | tuple = (),
                 target: float | None = None):
        self.long = side == "demand"
        self.entry, self.first_stop, self.stop = float(entry), float(stop), float(stop)
        self.risk = abs(self.entry - self.first_stop)
        self.method = opts.get("trail_method") or "off"
        self.start_r = float(opts.get("trail_start_r") or 1.0)
        self.keep_target = bool(opts.get("trail_keep_target", True))
        self.atr_mult = float(opts.get("trail_atr_mult") or 2.0)
        self.swing = max(1, int(opts.get("trail_swing") or 2))
        maker, taker = costs.fee_rates(th)
        self.break_even = self.entry * (1 + maker + taker) if self.long else self.entry * (1 - maker - taker)
        self.bars = [tuple(map(float, b)) for b in list(prior)[-20:]]     # (high, low, close), for the ATR
        self.after: list[tuple] = []                                      # candles since the fill, for the swings
        self.best = self.entry
        self.active = False
        self.moves = 0
        self.why = ""
        # lock: steps measured on the distance to the first target
        self.target = float(target) if target is not None else None
        self.first_target = self.target
        dist = abs(self.target - self.entry) if self.target is not None else 0.0
        self.lock_step = dist * float(opts.get("trail_lock_pct") or 10.0) / 100.0
        gap_pct = opts.get("trail_lock_gap_pct")
        self.lock_gap = self.lock_step * float(gap_pct if gap_pct is not None else 20.0) / 100.0
        self.lock_move = (opts.get("trail_lock_target") or "move") == "move"
        self.locked = 0                  # steps locked so far
        self.target_moves = 0

    @property
    def best_r(self) -> float:
        if self.risk <= 0:
            return 0.0
        return (self.best - self.entry) / self.risk if self.long else (self.entry - self.best) / self.risk

    @property
    def target_removed(self) -> bool:
        """True when the fixed target no longer closes the trade. Lock with "move": from the fill on (the target only
        moves forward and the locked stop ends the trade)."""
        if self.method == "lock":
            return self.lock_move and self.lock_step > 0
        return self.active and not self.keep_target

    def _lock(self) -> tuple[float | None, str]:
        """lock: the stop at the highest step the best price has passed by the gap (None before the first step)."""
        if self.lock_step <= 0:
            return None, ""
        sign = 1 if self.long else -1
        gone = (self.best - self.entry) * sign
        n = int(math.floor((gone - self.lock_gap) / self.lock_step + 1e-9))
        if n < 1:
            return None, ""
        self.locked = max(self.locked, n)
        level = self.entry + sign * self.locked * self.lock_step
        pct = self.locked * self.lock_step / abs(self.first_target - self.entry) * 100
        return level, f"lock: {pct:g}% of the target locked, stop at +{self.locked * self.lock_step / self.risk:.2f} R"

    def _move_target(self) -> None:
        """lock with "move": each time the best price reaches the target, the target moves forward one step."""
        if not (self.lock_move and self.lock_step > 0 and self.target is not None):
            return
        sign = 1 if self.long else -1
        while (self.best - self.target) * sign >= -1e-12 * max(1.0, abs(self.target)):
            self.target += sign * self.lock_step
            self.target_moves += 1

    def _better(self, a: float, b: float) -> float:
        """The more protective of two stops: the higher one for a long, the lower one for a short."""
        return max(a, b) if self.long else min(a, b)

    def _atr(self, n: int = 14) -> float:
        bars = self.bars[-(n + 1):]
        if len(bars) < 2:
            return abs(bars[-1][0] - bars[-1][1]) if bars else 0.0
        ranges = [max(h - l, abs(h - bars[k - 1][2]), abs(l - bars[k - 1][2])) for k, (h, l, _c) in enumerate(bars) if k > 0]
        return sum(ranges) / len(ranges)

    def _avg_range(self, n: int = 14) -> float:
        bars = self.bars[-n:]
        return sum(h - l for h, l, _c in bars) / len(bars) if bars else 0.0

    def _candidate(self) -> tuple[float | None, str]:
        sign = 1 if self.long else -1
        if self.method == "step":
            locked = max(0.0, math.floor((self.best_r - 1.0) / 0.5 + 1e-9) * 0.5)
            return self.entry + sign * locked * self.risk, f"step: best +{self.best_r:.2f} R, stop at +{locked:g} R"
        if self.method == "atr":
            atr = self._atr()
            return self.best - sign * self.atr_mult * atr, f"ATR: best {self.best:.8g} - {self.atr_mult:g} x ATR {atr:.8g}"
        if self.method == "swing":
            n, bars = self.swing, self.after
            k = len(bars) - 1 - n
            if k >= n:
                lows = [b[1] for b in bars] if self.long else [b[0] for b in bars]
                here, around = lows[k], lows[k - n:k] + lows[k + 1:k + n + 1]
                is_swing = all(here < x for x in around) if self.long else all(here > x for x in around)
                if is_swing:
                    pad = 0.1 * self._avg_range()
                    return here - sign * pad, f"swing {'low' if self.long else 'high'} {here:.8g}"
            return None, ""
        return None, ""

    def close_bar(self, high: float, low: float, close: float) -> float | None:
        """One closed candle of the trailing timeframe. Returns the new stop if it moved, else None."""
        bar = (float(high), float(low), float(close))
        self.bars.append(bar)
        self.bars = self.bars[-40:]
        self.after.append(bar)
        self.best = max(self.best, bar[0]) if self.long else min(self.best, bar[1])
        if self.method == "off" or self.risk <= 0:
            return None
        if self.method == "lock":
            self._move_target()
            candidate, why = self._lock()
            if candidate is None:
                return None
            self.active = True
        else:
            if not self.active:
                if self.best_r + 1e-9 < self.start_r:
                    return None
                self.active = True
            candidate, why = self._candidate()
        floor = self.break_even
        target = floor if candidate is None else self._better(candidate, floor)
        new = self._better(self.stop, target)
        if abs(new - self.stop) <= 1e-12 * max(1.0, abs(self.stop)):
            return None
        self.stop, self.moves = new, self.moves + 1
        self.why = why if candidate is not None and self._better(candidate, floor) == candidate else "break-even (entry plus fees)"
        return new
