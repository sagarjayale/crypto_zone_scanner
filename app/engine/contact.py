"""HTF contact: how a zone sits against the same-side zones of higher timeframes.

inside:   the zone lies completely within the higher zone.
overlap:  the two price ranges share some prices.
touch:    they do not overlap, and the gap is at most contact_touch_pct of price.
near:     the gap is larger, but at most contact_near_pct of price.
One entry per higher timeframe (its strongest contact), nearest timeframe first."""
from ..config import TIMEFRAMES
from .lifecycle import ACTIVE

RANK = {"inside": 0, "overlap": 1, "touch": 2, "near": 3}


def relate(z, h, price: float, th: dict) -> dict | None:
    zlo, zhi = sorted((z.proximal, z.distal))
    hlo, hhi = sorted((h.proximal, h.distal))
    lo, hi = max(zlo, hlo), min(zhi, hhi)
    base = {"tf": h.timeframe, "zone_id": getattr(h, "id", None), "proximal": h.proximal, "distal": h.distal}
    if hi > lo:
        if zlo >= hlo and zhi <= hhi:
            return {**base, "relation": "inside", "gap": 0.0, "pct": 0.0, "low": hlo, "high": hhi}
        return {**base, "relation": "overlap", "gap": 0.0, "pct": 0.0, "low": lo, "high": hi}
    gap = lo - hi                                      # zero when the edges meet exactly
    pct = 100.0 * gap / price if price else 0.0
    where = "above" if hlo >= zhi else "below"
    if pct <= float(th["contact_touch_pct"]):
        return {**base, "relation": "touch", "gap": float(f"{gap:.6g}"), "pct": round(pct, 3), "at": float(f"{(lo + hi) / 2:.8g}"), "where": where}
    if pct <= float(th["contact_near_pct"]):
        return {**base, "relation": "near", "gap": float(f"{gap:.6g}"), "pct": round(pct, 2), "where": where}
    return None


def contacts(z, zones: list, price: float, th: dict, above_tf: str | None = None) -> list[dict]:
    """Contacts of zone z with active same-side zones on timeframes higher than above_tf (default: z's own)."""
    floor = TIMEFRAMES.index(above_tf or z.timeframe)
    best: dict[str, dict] = {}
    for h in zones:
        if h is z or h.side != z.side or h.status not in ACTIVE or TIMEFRAMES.index(h.timeframe) >= floor:
            continue
        r = relate(z, h, price, th)
        if r is None:
            continue
        cur = best.get(h.timeframe)
        if cur is None or (RANK[r["relation"]], r["gap"]) < (RANK[cur["relation"]], cur["gap"]):
            best[h.timeframe] = r
    return sorted(best.values(), key=lambda r: TIMEFRAMES.index(r["tf"]), reverse=True)
