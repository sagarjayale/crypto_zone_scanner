"""The GTF trade score. Under the scanner.py marking (the default) freshness counts every candle that reached the
proximal line, strength is 2 for a gap or a leg-out body of 70% or more, and only the 20 EMA gives the extra point,
as in scanner.py v6.4. Under the PDF marking it is as follows.

Freshness:  fresh 3, tested once 1.5, tested twice 0. A test that is still going on is not counted yet.
Strength:   price left the zone with a gap 2, with two exciting candles 2, with one exciting candle and no gap 1.
Time at base: 1 to 3 base candles 2, 4 to 5 candles 1, more 0.
EMA:        +1 when the 20 or the 50 EMA runs through the zone (execution timeframe only).
Entry:      7 or more takes entry type 1, 5 to 6.5 takes entry type 2 or 3, below 5 is not traded.
Label:      7 or more TYPE_1, 5 or more TYPE_2_3, below that WEAK (location zone) or REJECT (execution zone)."""
from .lifecycle import counted_tests, scanner_rules


def parts(z, th: dict) -> dict:
    tests = counted_tests(z, th)
    fresh = 3.0 if tests <= 0 else 1.5 if tests == 1 else 0.0
    base = 2.0 if z.base_count <= 3 else 1.0 if z.base_count <= 5 else 0.0
    if scanner_rules(th):
        body = float(getattr(z, "legout_body", None) or 0.0)
        strong = bool(z.has_gap) or body >= 0.70
        label = "gap" if z.has_gap else f"strong leg-out, body {round(body * 100)}%" if strong else f"normal leg-out, body {round(body * 100)}%"
    else:
        two = int(getattr(z, "legout_count", None) or 1) >= 2
        strong = bool(z.has_gap) or two
        label = "gap" if z.has_gap else "two exciting candles" if two else "one exciting candle"
    return {"freshness": fresh, "strength": 2.0 if strong else 1.0, "base": base, "tests": tests, "strength_label": label}


def ema_inside(z, emas) -> bool:
    """True when any of the given EMA values lies inside the zone. Accepts one number, several, or None."""
    if emas is None:
        return False
    lo, hi = sorted((z.proximal, z.distal))
    values = emas if isinstance(emas, (tuple, list)) else (emas,)
    return any(v is not None and lo <= v <= hi for v in values)


def ema_point(z, emas, th: dict) -> bool:
    """The EMA point of an execution zone: scanner.py looks at the 20 EMA only, the course at the 20 and the 50 EMA."""
    if scanner_rules(th) and isinstance(emas, (tuple, list)):
        emas = emas[0]
    return ema_inside(z, emas)


def htf_score(z, th: dict) -> tuple[float, str]:
    """Score of a zone used for location. Never rejected: a tested zone still marks the curve."""
    p = parts(z, th)
    total = p["freshness"] + p["strength"] + p["base"]
    return total, "TYPE_1" if total >= 7 else "TYPE_2_3" if total >= 5 else "WEAK"


def ltf_score(z, emas, th: dict) -> tuple[float, str]:
    """Score of a zone used for execution. More earlier tests than allowed rejects it outright."""
    if counted_tests(z, th) > th["max_wick_tests"]:
        return 0.0, "REJECT"
    p = parts(z, th)
    total = p["freshness"] + p["strength"] + p["base"] + (1.0 if ema_point(z, emas, th) else 0.0)
    return total, "TYPE_1" if total >= 7 else "TYPE_2_3" if total >= 5 else "REJECT"


def entry_kind(total: float, th: dict) -> str | None:
    """The entry type a trade score takes: 7 or more type 1, 5 to 6.5 the chosen one of types 2 and 3, less none."""
    if total >= 7:
        return "TYPE_1"
    if total >= 5:
        return "TYPE_2" if th.get("entry_23") == "TYPE_2" else "TYPE_3"
    return None
