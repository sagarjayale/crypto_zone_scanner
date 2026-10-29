"""Fill rules shared by the backtest and (later) paper trading, so both mean the same thing.
All functions are pure: prices in, decision out."""


def entry_touched(side: str, entry: float, high: float, low: float) -> bool:
    """A limit order at the proximal line fills when price trades at it."""
    return low <= entry if side == "demand" else high >= entry


def stop_hit(side: str, stop: float, high: float, low: float) -> bool:
    return low <= stop if side == "demand" else high >= stop


def exit_after_entry(side: str, stop: float, target: float, o: float, h: float, l: float):
    """Exit check on a candle after the entry candle. Returns (price, 'loss' | 'win') or None.
    If one candle reaches both stop and target, the stop is taken: the order within a candle is unknown.
    A gap beyond the stop exits at the open (worse than the stop). A gap beyond the target exits at the target."""
    if side == "demand":
        if o <= stop:
            return o, "loss"
        if l <= stop:
            return stop, "loss"
        if h >= target:
            return target, "win"
    else:
        if o >= stop:
            return o, "loss"
        if h >= stop:
            return stop, "loss"
        if l <= target:
            return target, "win"
    return None


def result_r(side: str, entry: float, exit_price: float, risk: float, cost_pct: float) -> float:
    """Result in multiples of the planned risk, after costs. cost_pct is charged on each side."""
    gross = (exit_price - entry) / risk if side == "demand" else (entry - exit_price) / risk
    return gross - 2 * (cost_pct / 100.0) * entry / risk
