"""GTF candle classification and zone detection. Pure functions on arrays, so the same code
serves live scanning, backtest and replay. Two markings: `marking_rule: scanner` (the default) finds zones exactly as
scanner.py v6.4 does; `marking_rule: pdf` follows the GTF course PDF.

scanner.py v6.4 (detect_htf_zones, ltf_execution_zones):
Base candle:      body strictly below 50% of the range. Leg-out: body strictly above 50%, green for demand, red for supply.
Zone:             the candle before the base (any candle) is the leg-in, then 1 to 6 base candles, then the leg-out, which
                  must close beyond the leg-in's close. scanner.py walks a long run of base candles in blocks of six with one
                  candle between blocks, so a run of k base candles gives a base of the last k mod 7 candles (none when
                  k mod 7 is 0), and the candle before them is the leg-in.
Lines:            body to wick from the base candles only: demand proximal = highest base body, distal = lowest base wick.
An execution (LTF) zone also needs an exciting leg-in in the reversal direction; that is checked in gtf.ltf_ok.

GTF course PDF:

Base candle:      body strictly below 50% of the range.
Exciting candle:  body strictly above 50% of the range (green or red gives its direction), or a candle that opens with a
                  significant gap beyond the previous candle and holds it at the close ("gap plus base candle").
Zone:             an exciting leg-in, then 1 to 6 base candles, then an exciting leg-out that closes beyond the whole
                  leg-in: above its high for demand, below its low for supply.
Proximal line:    the highest base body for demand, the lowest base body for supply.
Distal line:      the lowest wick of the base for demand, or of the leg-out, or (reversal patterns) of the leg-in, when
                  one of those is lower: the "exceptional marking". Supply is the mirror, with the highest wick.

"""
import numpy as np

NONE, BASE, LEG_UP, LEG_DOWN = 0, 1, 2, 3
LEGS = (LEG_UP, LEG_DOWN)


def pdf_marking(th) -> bool:
    return th.get("marking_rule") == "pdf"


def body_share(o, h, l, c) -> np.ndarray:
    rng = h - l
    return np.divide(np.abs(c - o), rng, out=np.zeros_like(rng, dtype=float), where=rng > 0)


def classify(o, h, l, c, th) -> np.ndarray:
    share = body_share(o, h, l, c)
    kinds = np.full(len(o), NONE, dtype=np.int8)
    kinds[share < th["base_body_pct"] / 100.0] = BASE
    explosive = share > th["leg_body_pct"] / 100.0
    kinds[explosive & (c > o)] = LEG_UP
    kinds[explosive & (c < o)] = LEG_DOWN
    gap = float(th.get("gap_exciting_pct") or 0)
    if pdf_marking(th) and gap > 0 and len(o) > 1:
        # A significant gap plus a small candle works like an exciting candle. The gap must still be there at the close.
        small = ~explosive[1:]
        up = small & (o[1:] >= h[:-1] * (1 + gap / 100.0)) & (c[1:] > h[:-1])
        down = small & (o[1:] <= l[:-1] * (1 - gap / 100.0)) & (c[1:] < l[:-1])
        kinds[1:][up] = LEG_UP
        kinds[1:][down] = LEG_DOWN
    return kinds


def _scanner(i, j, base, o, h, l, c, kinds):
    """The marking of scanner.py v6.4."""
    up = kinds[i] == LEG_UP
    if up and not c[i] > c[j]:
        return None, "leg-out did not close above the leg-in close"
    if not up and not c[i] < c[j]:
        return None, "leg-out did not close below the leg-in close"
    if up:
        proximal, distal = float(np.maximum(o[base], c[base]).max()), float(l[base].min())
        side, pattern = "demand", "DBR" if c[j] < o[j] else "RBR"
    else:
        proximal, distal = float(np.minimum(o[base], c[base]).min()), float(h[base].max())
        side, pattern = "supply", "RBD" if c[j] > o[j] else "DBD"
    return {"side": side, "pattern": pattern, "proximal": proximal, "distal": distal, "distal_from": "base"}, None


def _pdf(i, j, base, o, h, l, c, kinds):
    """The marking of the GTF course: an exciting leg-in, the closing rule, and the exceptional distal line."""
    if kinds[j] not in LEGS:
        return None, ("more base candles than allowed" if kinds[j] == BASE else "the candle before the base is not an exciting candle")
    up, drop_in = kinds[i] == LEG_UP, kinds[j] == LEG_DOWN
    if up and not c[i] > h[j]:
        return None, "leg-out did not close above the leg-in high"
    if not up and not c[i] < l[j]:
        return None, "leg-out did not close below the leg-in low"
    if up:
        side, pattern = "demand", "DBR" if drop_in else "RBR"            # a drop into the base: reversal
        proximal = float(np.maximum(o[base], c[base]).max())
        marks = [("base", float(l[base].min())), ("leg-out", float(l[i]))] + ([("leg-in", float(l[j]))] if drop_in else [])
        distal_from, distal = min(marks, key=lambda m: m[1])            # the base wins a tie: it is listed first
    else:
        side, pattern = "supply", "DBD" if drop_in else "RBD"            # a rally into the base: reversal
        proximal = float(np.minimum(o[base], c[base]).min())
        marks = [("base", float(h[base].max())), ("leg-out", float(h[i]))] + ([] if drop_in else [("leg-in", float(h[j]))])
        distal_from, distal = max(marks, key=lambda m: m[1])
    return {"side": side, "pattern": pattern, "proximal": proximal, "distal": distal, "distal_from": distal_from}, None


def detect_at(i: int, o, h, l, c, kinds, th):
    """Check whether the closed candle i completes a zone as its leg-out.
    Returns (zone, reason): zone dict when valid; reason text when a candidate fails;
    (None, None) when candle i is not a candidate at all."""
    if kinds[i] not in LEGS:
        return None, None
    pdf = pdf_marking(th)
    j, run = i - 1, 0
    while j >= 0 and kinds[j] == BASE and (not pdf or run < th["max_base"]):
        run += 1
        j -= 1
    if not run:
        return None, None
    if not pdf:                     # scanner.py: blocks of max_base candles with one candle between blocks
        count = run % (int(th["max_base"]) + 1)
        if not count:
            return None, "more base candles than allowed"
        j = i - count - 1
    if j < 0:
        return None, "no candle before the base"
    base = list(range(j + 1, i))
    found, reason = (_pdf if pdf else _scanner)(i, j, base, o, h, l, c, kinds)
    if reason:
        return None, reason
    demand = found["side"] == "demand"
    if not (found["proximal"] > found["distal"] if demand else found["distal"] > found["proximal"]):
        return None, "zone has no height"
    rng = h[i] - l[i]
    found.update(base_start=base[0], base_count=len(base), leg_in=j,
                 has_gap=bool(o[i] > h[i - 1]) if demand else bool(o[i] < l[i - 1]),
                 legout_body=float(abs(c[i] - o[i]) / rng) if rng > 0 else 0.0, legout_close=float(c[i]),
                 legin_explosive=bool(kinds[j] in LEGS))
    return found, None
