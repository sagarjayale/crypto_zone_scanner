"""Reference for the tests: the zone functions of the user's scanner.py v6.4, copied unchanged (only these functions,
nothing that talks to Upstox or sends email). tests/test_scanner_rules.py runs them and the app's engine on the same
candles and checks that both find the same zones."""
# flake8: noqa
from __future__ import annotations

import numpy as np
import pandas as pd


def _color(o, c) -> str:
    if c > o: return "GREEN"
    if c < o: return "RED"
    return "DOJI"


def _body_pct(o, h, l, c) -> float:
    r = h - l
    return abs(c - o) / r if r > 0 else 0.0


def _is_base(o, h, l, c) -> bool:
    """GTF: body STRICTLY less than 50% of range."""
    return _body_pct(o, h, l, c) < 0.50


def _is_explosive(o, h, l, c) -> bool:
    """GTF: body STRICTLY more than 50% of range."""
    return _body_pct(o, h, l, c) > 0.50


def _body_ratio(o, h, l, c) -> float:
    return _body_pct(o, h, l, c)


def _pattern_label(legin_color: str, base_count: int, legout_color: str) -> str:
    """
    GTF pattern naming (pages 9-10):
      Demand: DBR (legin=RED→base→GREEN legout) = reversal
              RBR (legin=GREEN→base→GREEN legout) = continuation
      Supply: RBD (legin=GREEN→base→RED legout) = reversal
              DBD (legin=RED→base→RED legout) = continuation
    """
    if legout_color == "GREEN":
        return "DBR" if legin_color == "RED" else "RBR"
    return "RBD" if legin_color == "GREEN" else "DBD"


def _wick_tests_demand(lows: np.ndarray, proximal: float, from_idx: int) -> int:
    """Count candles after zone formation whose LOW reached the proximal.
    Excludes the live (last) candle."""
    sl = lows[from_idx:-1]   # exclude last candle
    if len(sl) == 0:
        return 0
    return int(np.sum(sl <= proximal))


def _wick_tests_supply(highs: np.ndarray, proximal: float, from_idx: int) -> int:
    """Count candles after zone formation whose HIGH reached the proximal.
    Excludes the live (last) candle."""
    sh = highs[from_idx:-1]  # exclude last candle
    if len(sh) == 0:
        return 0
    return int(np.sum(sh >= proximal))


def _is_fresh(
    closes: np.ndarray, lows: np.ndarray, highs: np.ndarray,
    from_idx: int, z_lo: float, z_hi: float
) -> bool:
    """
    Zone is fresh if no COMPLETED candle body has closed inside the zone
    since formation.

    The live (last/running) candle is EXCLUDED — it has not closed yet
    so its close price will change. Including it would cause zones to be
    incorrectly marked as consumed when price is merely touching them.

    GTF rule: zone is consumed only when a candle BODY CLOSES inside.
    A wick that enters the zone does not consume it (counted as wick_test).
    """
    # Exclude live candle: use [from_idx:-1] not [from_idx:]
    post_cl = closes[from_idx:-1]
    if len(post_cl) == 0:
        return True
    # Any close strictly inside zone = zone consumed
    return not np.any((post_cl > z_lo) & (post_cl < z_hi))


def _score_htf_zone(
    base_count: int,
    wt:         int,
    has_gap:    bool,
    lo_open:    float,
    lo_high:    float,
    lo_low:     float,
    lo_close:   float,
) -> dict:
    # ── Freshness ─────────────────────────────────────────
    if   wt == 0: f_score = 3.0; f_label = "FRESH (0 tests)"
    elif wt == 1: f_score = 1.5; f_label = "TESTED ONCE"
    else:         f_score = 0.0; f_label = "USED UP (2+ tests)"

    # ── Strength ──────────────────────────────────────────
    if has_gap:
        s_score = 2.0
        s_label = "GAP"
    else:
        body = _body_ratio(lo_open, lo_high, lo_low, lo_close)
        if body >= 0.70:
            s_score = 2.0
            s_label = f"STRONG EXPLOSIVE (body={body:.0%})"
        else:
            s_score = 1.0
            s_label = f"NORMAL EXPLOSIVE (body={body:.0%})"

    # ── Base count ────────────────────────────────────────
    if   base_count <= 3: b_score = 2.0; b_label = f"{base_count} bases (tight)"
    elif base_count <= 5: b_score = 1.0; b_label = f"{base_count} bases (medium)"
    else:                 b_score = 0.0; b_label = f"{base_count} bases (wide)"

    total = round(f_score + s_score + b_score, 2)
    label = "TYPE_1" if total >= 7 else ("TYPE_2_3" if total >= 5 else "WEAK")

    return {
        "score":           total,
        "zone_label":      label,
        "freshness_score": f_score,
        "freshness_label": f_label,
        "strength_score":  s_score,
        "strength_label":  s_label,
        "base_score":      b_score,
        "base_label":      b_label,
    }


def detect_htf_zones(df: pd.DataFrame) -> tuple[list, list]:
    """
    GTF Supply/Demand Zone Detection — 100% Accuracy spec.

    Structure per GTF PDF pages 13-15, 24:
      1. LEGIN  — any candle before the base (explosive preferred but not enforced
                   at HTF level since older bars can be normal exciting candles)
      2. BASE   — 1-6 consecutive base candles (body STRICTLY < 50% of range)
      3. LEGOUT — explosive candle (body STRICTLY > 50%) that:
                    • For DEMAND: is GREEN and closes ABOVE the legin close
                    • For SUPPLY: is RED   and closes BELOW the legin close

    Zone boundaries (Body-to-Wick marking — GTF default, pages 11-14):
      DEMAND: proximal = max body of bases (highest open or close across all bases)
              distal   = min wick of bases  (lowest low across all bases)
      SUPPLY: proximal = min body of bases (lowest open or close across all bases)
              distal   = max wick of bases  (highest high across all bases)

    Wick tests (page 33 scoring):
      Demand: count of closed candles after legout whose LOW ≤ proximal
      Supply: count of closed candles after legout whose HIGH ≥ proximal
      (Live/last candle excluded — it may be the live entry touch)

    Freshness: zone valid only if no candle BODY has closed inside the zone.
    """
    O = df["open"].values
    H = df["high"].values
    L = df["low"].values
    C = df["close"].values
    n = len(df)
    demand: list[dict] = []
    supply: list[dict] = []

    # ── CRITICAL: Exclude the live/running candle from zone detection ──────────
    # The last bar (index n-1) is the current live candle — it has not closed yet.
    # Its OHLC values change tick by tick. Using it as legin, base, or legout
    # produces incorrect zone boundaries that change every minute.
    #
    # GTF rule: zones are formed by COMPLETED candles only.
    # The live candle is only used for:
    #   1. CMP (current market price = live close)
    #   2. TOUCHING_NOW detection (is the live candle touching a completed zone?)
    #
    # scan_limit = n - 1 means:
    #   - bases can start at i=1 up to i = n-2
    #   - legout j can reach at most n-2 (second last bar)
    #   - last bar (n-1) is never legin, base, or legout
    scan_limit = n - 1   # loop: while i < scan_limit → i goes 1..n-2

    i = 1          # i = start of potential base sequence; i-1 = legin
    while i < scan_limit:
        # ── Collect 1-6 consecutive base candles (only from completed bars) ─────
        base_idxs: list[int] = []
        j = i
        while j < scan_limit and _is_base(O[j], H[j], L[j], C[j]) and len(base_idxs) < 6:
            base_idxs.append(j)
            j += 1

        if not (1 <= len(base_idxs) <= 6) or j >= n:
            i += 1
            continue

        # ── Legout must also be a completed candle (j < scan_limit) ───────────
        if j >= scan_limit:
            i += 1
            continue

        # ── Legin = candle immediately before the base ────────────────────────
        legin_idx   = i - 1
        legin_close = C[legin_idx]

        # ── Legout = first candle after the bases ─────────────────────────────
        lo_col = _color(O[j], C[j])
        lo_exp = _is_explosive(O[j], H[j], L[j], C[j])

        # ── Zone boundaries ───────────────────────────────────────────────────
        base_max_body = float(max(max(O[k], C[k]) for k in base_idxs))
        base_min_body = float(min(min(O[k], C[k]) for k in base_idxs))
        base_min_wick = float(min(L[k] for k in base_idxs))
        base_max_wick = float(max(H[k] for k in base_idxs))

        legin_col = _color(O[legin_idx], C[legin_idx])

        # ── DEMAND ZONE ───────────────────────────────────────────────────────
        # GTF page 24: legout must close ABOVE legin close
        if lo_col == "GREEN" and lo_exp and C[j] > legin_close:
            prox  = base_max_body
            dist  = base_min_wick
            z_lo  = min(prox, dist)
            z_hi  = max(prox, dist)
            if prox > dist and _is_fresh(C, L, H, j + 1, z_lo, z_hi):
                wt      = _wick_tests_demand(L, prox, j + 1)
                pat     = _pattern_label(legin_col, len(base_idxs), "GREEN")
                has_gap = bool(O[j] > H[j - 1]) if j > 0 else False
                sc      = _score_htf_zone(len(base_idxs), wt, has_gap, O[j], H[j], L[j], C[j])
                demand.append({
                    "proximal":        round(prox, 4),
                    "distal":          round(dist, 4),
                    "bases":           len(base_idxs),
                    "pattern":         pat,
                    "is_reversal":     pat in ("DBR", "RBD"),
                    "formed_at":       int(j),
                    "legin_idx":       int(legin_idx),
                    "wick_tests":      wt,
                    "has_gap":         has_gap,
                    "score":           sc["score"],
                    "zone_label":      sc["zone_label"],
                    "freshness_score": sc["freshness_score"],
                    "freshness_label": sc["freshness_label"],
                    "strength_score":  sc["strength_score"],
                    "strength_label":  sc["strength_label"],
                    "base_score":      sc["base_score"],
                    "base_label":      sc["base_label"],
                })

        # ── SUPPLY ZONE ───────────────────────────────────────────────────────
        # GTF page 24: legout must close BELOW legin close
        if lo_col == "RED" and lo_exp and C[j] < legin_close:
            prox  = base_min_body
            dist  = base_max_wick
            z_lo  = min(prox, dist)
            z_hi  = max(prox, dist)
            if dist > prox and _is_fresh(C, L, H, j + 1, z_lo, z_hi):
                wt      = _wick_tests_supply(H, prox, j + 1)
                pat     = _pattern_label(legin_col, len(base_idxs), "RED")
                has_gap = bool(O[j] < L[j - 1]) if j > 0 else False
                sc      = _score_htf_zone(len(base_idxs), wt, has_gap, O[j], H[j], L[j], C[j])
                supply.append({
                    "proximal":        round(prox, 4),
                    "distal":          round(dist, 4),
                    "bases":           len(base_idxs),
                    "pattern":         pat,
                    "is_reversal":     pat in ("DBR", "RBD"),
                    "formed_at":       int(j),
                    "legin_idx":       int(legin_idx),
                    "wick_tests":      wt,
                    "has_gap":         has_gap,
                    "score":           sc["score"],
                    "zone_label":      sc["zone_label"],
                    "freshness_score": sc["freshness_score"],
                    "freshness_label": sc["freshness_label"],
                    "strength_score":  sc["strength_score"],
                    "strength_label":  sc["strength_label"],
                    "base_score":      sc["base_score"],
                    "base_label":      sc["base_label"],
                })

        i = j + 1

    return demand, supply


def _score_zone(
    base_count: int, wt: int, has_gap: bool,
    lo_open: float, lo_high: float, lo_low: float, lo_close: float,
) -> float:
    score: float = 0.0

    # Step 7 — Freshness
    if   wt == 0: score += 3.0
    elif wt == 1: score += 1.5
    else:         return 0.0        # 2+ tests → REJECT

    # Step 8 — Strength
    if has_gap:
        score += 2.0
    else:
        body_pct = _body_ratio(lo_open, lo_high, lo_low, lo_close)
        score += 2.0 if body_pct >= 0.70 else 1.0

    # Step 9 — Base count (Time at base)
    if   base_count <= 3: score += 2.0
    elif base_count <= 5: score += 1.0
    # >5 bases → +0

    return round(score, 2)


def ema_bonus(df: pd.DataFrame, zone_low: float, zone_high: float, period: int = 20) -> int:
    closes = df["close"].values
    if len(closes) < period:
        return 0
    k   = 2 / (period + 1)
    ema = float(closes[0])
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
    return 1 if zone_low <= ema <= zone_high else 0


def classify_score(score: float) -> str:
    if score >= 7: return "TYPE_1"
    if score >= 5: return "TYPE_2_3"
    return "REJECT"


_STATE_RANK = {"TOUCHING_NOW": 0, "APPROACHING": 1, "JUST_FORMED": 2, "ACTIVE": 3}


def ltf_execution_zones(
    df: pd.DataFrame, side: str, htf_zone: dict, cmp: float
) -> list[dict]:
    """
    Find ALL valid LTF execution zones between CMP and the HTF zone interior.
    Returns list sorted by (score DESC, zone_state rank ASC).
    Each zone includes full geometry + all 3 entry plans.
    """
    if not htf_zone or side == "NONE":
        return []

    O = df["open"].values
    H = df["high"].values
    L = df["low"].values
    C = df["close"].values
    n = len(df)

    hz_prox = htf_zone["proximal"]
    hz_dist = htf_zone["distal"]
    hz_width = abs(hz_prox - hz_dist)

    # Spatial bounds: LTF zone proximal must be between CMP and HTF zone distal.
    # For higher TF modes (QUARTERLY/HALFYEARLY/YEARLY) the HTF zone is wide
    # and the LTF bars (quarterly/half-yearly) are coarse — we add a 10% buffer
    # beyond the HTF distal so zones just outside the HTF zone are also captured.
    buffer = hz_width * 0.10

    if side == "BUY":
        lo_bound = hz_dist - buffer   # slightly below distal
        hi_bound = cmp                # up to CMP
    else:
        lo_bound = cmp                # from CMP
        hi_bound = hz_dist + buffer   # slightly above distal

    candidates: list[dict] = []
    last = n - 1
    last_L = L[last]; last_H = H[last]; last_C = C[last]

    # Exclude live candle from zone formation — only completed bars form zones
    ltf_scan_limit = n - 1   # j (legout) can reach at most n-2

    i = 1
    while i < ltf_scan_limit:
        # ── Legin must be explosive ────────────────────────────────────────────
        if not _is_explosive(O[i - 1], H[i - 1], L[i - 1], C[i - 1]):
            i += 1; continue

        legin_col = _color(O[i - 1], C[i - 1])
        legin_i   = i - 1

        # Direction gate on legin colour
        if side == "BUY"  and legin_col not in ("RED",   "DOJI"): i += 1; continue
        if side == "SELL" and legin_col not in ("GREEN", "DOJI"): i += 1; continue

        # ── Collect 1-6 base candles (completed only) ─────────────────────────
        base_idxs: list[int] = []
        j = i
        while j < ltf_scan_limit and _is_base(O[j], H[j], L[j], C[j]) and len(base_idxs) < 6:
            base_idxs.append(j)
            j += 1

        if not base_idxs or j >= n:
            i += 1; continue

        # ── Legout must be a completed candle (j < ltf_scan_limit) ───────────
        if j >= ltf_scan_limit:
            i += 1; continue

        # ── Legout must be explosive and same direction as trade ───────────────
        # GTF page 24: legout close must be above legin close (demand)
        #                                or below legin close (supply)
        lo_col      = _color(O[j], C[j])
        lo_exp      = _is_explosive(O[j], H[j], L[j], C[j])
        legin_close = C[legin_i]
        valid  = (
            (side == "BUY"  and lo_col == "GREEN" and lo_exp and C[j] > legin_close) or
            (side == "SELL" and lo_col == "RED"   and lo_exp and C[j] < legin_close)
        )
        if not valid:
            i += 1; continue

        # ── Zone boundaries (Body-to-Wick marking) ────────────────────────────
        if side == "BUY":
            prox  = float(max(max(O[k], C[k]) for k in base_idxs))  # highest body
            dist  = float(min(L[k] for k in base_idxs))              # lowest wick
            ztype = "DEMAND"
        else:
            prox  = float(min(min(O[k], C[k]) for k in base_idxs))  # lowest body
            dist  = float(max(H[k] for k in base_idxs))              # highest wick
            ztype = "SUPPLY"

        if prox == dist:
            i += 1; continue

        # ── Spatial filter ─────────────────────────────────────────────────────
        if not (lo_bound <= prox <= hi_bound):
            i = j + 1; continue

        # ── LTF Proximity filter ───────────────────────────────────────────────
        # Reject LTF zones whose proximal is more than 50% away from CMP.
        # Prevents old zones (e.g. proximal=57 when CMP=335, gap=83%)
        # from generating signals — they are not actionable entry zones.
        # Example: CMP=335.6, LTF proximal=57.53 → gap=82.9% → REJECTED ✅
        #          CMP=335.6, LTF proximal=280.00 → gap=16.6% → accepted ✅
        ltf_prox_gap = abs(cmp - prox) / cmp * 100
        if ltf_prox_gap > 50.0:
            i = j + 1; continue

        # ── Freshness: no body close inside zone ──────────────────────────────
        z_lo = min(prox, dist)
        z_hi = max(prox, dist)
        if not _is_fresh(C, L, H, j + 1, z_lo, z_hi):
            i += 1; continue

        # ── Wick tests: proximal touches only (GTF page 33) ───────────────────
        # Demand: count closes (excl live) where low ≤ proximal
        # Supply: count closes (excl live) where high ≥ proximal
        if side == "BUY":
            wt = _wick_tests_demand(L, prox, j + 1)
        else:
            wt = _wick_tests_supply(H, prox, j + 1)
        if wt > 1:
            i += 1; continue

        # ── Gap check ─────────────────────────────────────────────────────────
        has_gap = False
        if j > 0:
            has_gap = bool(
                (O[j] > H[j - 1]) if side == "BUY" else (O[j] < L[j - 1])
            )

        # ── Score ──────────────────────────────────────────────────────────────
        raw_score = _score_zone(
            len(base_idxs), wt, has_gap,
            O[j], H[j], L[j], C[j]
        )
        if raw_score == 0.0:
            i += 1; continue

        e_bonus     = ema_bonus(df, z_lo, z_hi)
        total_score = round(raw_score + e_bonus, 2)
        zone_label  = classify_score(total_score)

        # ── Pattern & reversal ─────────────────────────────────────────────────
        prior_col  = _color(O[legin_i], C[legin_i])
        pat        = _pattern_label(prior_col, len(base_idxs), lo_col)
        is_reversal = pat in ("DBR", "RBD")

        # ── Zone state ─────────────────────────────────────────────────────────
        csince = n - 1 - j

        touching_now = bool(
            (side == "BUY"  and last_L <= prox and last_C >= dist) or
            (side == "SELL" and last_H >= prox and last_C <= dist)
        )
        approch = False
        if not touching_now:
            if side == "BUY":  approch = bool(prox < last_C <= prox * 1.01)
            else:              approch = bool(prox * 0.99 <= last_C < prox)

        if   csince <= 1 and not touching_now: zstate = "JUST_FORMED"
        elif touching_now:                     zstate = "TOUCHING_NOW"
        elif approch:                          zstate = "APPROACHING"
        else:                                  zstate = "ACTIVE"

        rr_zone = abs(prox - dist)

        # ── Post-legout candle slices (used by confirmation + conservative plans) ─
        post_C = C[j + 1:]
        post_L = L[j + 1:]
        post_H = H[j + 1:]

        # ══════════════════════════════════════════════════════════════════════
        # ENTRY PLAN 1 — AGGRESSIVE
        # ══════════════════════════════════════════════════════════════════════
        agg_entry  = round(float(C[j]), 2)
        agg_sl     = round(float(dist), 2)
        agg_risk   = abs(agg_entry - agg_sl)
        agg_target = round(
            agg_entry + 2 * agg_risk if side == "BUY"
            else agg_entry - 2 * agg_risk, 2
        )
        aggressive = {
            "entry_type": "AGGRESSIVE",
            "entry":      agg_entry,
            "stoploss":   agg_sl,
            "target":     agg_target,
            "rr":         "2:1",
            "triggered":  True,
            "note":       "Enter at legout close — zone just formed",
        }

        # ══════════════════════════════════════════════════════════════════════
        # ENTRY PLAN 2 — CONFIRMATION
        # ══════════════════════════════════════════════════════════════════════
        conf_entry  = round(float(prox), 2)
        conf_sl     = round(float(dist), 2)
        conf_target = round(
            prox + 2 * rr_zone if side == "BUY"
            else prox - 2 * rr_zone, 2
        )
        conf_triggered = touching_now or bool(
            np.any(post_L <= prox) if side == "BUY" else np.any(post_H >= prox)
        )
        confirmation = {
            "entry_type": "CONFIRMATION",
            "entry":      conf_entry,
            "stoploss":   conf_sl,
            "target":     conf_target,
            "rr":         "2:1",
            "triggered":  conf_triggered,
            "note": (
                "Limit at proximal — price is touching zone NOW" if touching_now
                else "Limit triggered on re-touch"              if conf_triggered
                else "Limit at proximal — waiting for first touch"
            ),
        }

        # ══════════════════════════════════════════════════════════════════════
        # ENTRY PLAN 3 — CONSERVATIVE  (wick test + rejection candle)
        # ══════════════════════════════════════════════════════════════════════
        conservative:  dict | None  = None
        wc_idx:        int | None   = None
        wc_ext:        float | None = None
        r_entry = r_sl = r_target   = None
        r_fired = False

        for pi in range(len(post_L)):
            ai        = j + 1 + pi
            wicked    = post_L[pi] <= prox and post_H[pi] >= dist
            ci_inside = (
                (dist < post_C[pi] < prox) if side == "BUY"
                else (prox < post_C[pi] < dist)
            )
            if wicked and not ci_inside and wc_idx is None:
                wc_idx = ai
                wc_ext = float(L[ai]) if side == "BUY" else float(H[ai])

            if wc_idx is not None and ai > wc_idx:
                is_rej = (
                    (side == "BUY"  and dist <= O[ai] <= prox and C[ai] > prox) or
                    (side == "SELL" and prox <= O[ai] <= dist and C[ai] < prox)
                )
                if is_rej:
                    r_entry  = round(float(C[ai]), 2)
                    r_sl     = round(float(wc_ext), 2)
                    r_risk   = abs(r_entry - r_sl)
                    r_target = round(
                        r_entry + 1.5 * r_risk if side == "BUY"
                        else r_entry - 1.5 * r_risk, 2
                    )
                    r_fired = True
                    break

        # Check if LIVE candle is the wick test
        if wc_idx is None and touching_now:
            ci2 = (dist < last_C < prox) if side == "BUY" else (prox < last_C < dist)
            if not ci2:
                wc_idx = last
                wc_ext = float(last_L) if side == "BUY" else float(last_H)

        if wc_idx is not None:
            if r_fired:
                conservative = {
                    "entry_type": "CONSERVATIVE",
                    "entry":      r_entry,
                    "stoploss":   r_sl,
                    "target":     r_target,
                    "rr":         "1.5:1",
                    "triggered":  True,
                    "note":       "Wick + rejection candle confirmed — enter at rejection close",
                }
            else:
                conservative = {
                    "entry_type":   "CONSERVATIVE",
                    "entry":        None,
                    "stoploss":     None,
                    "target":       None,
                    "rr":           "1.5:1",
                    "triggered":    False,
                    "wick_extreme": round(float(wc_ext), 2),
                    "note": (
                        "Current candle = wick test — waiting for rejection candle"
                        if wc_idx == last
                        else "Wick test seen — waiting for rejection candle"
                    ),
                }

        candidates.append({
            "type":                 ztype,
            "proximal":             round(prox, 2),
            "distal":               round(dist, 2),
            "base_candles":         len(base_idxs),
            "wick_tests":           wt,
            "has_gap":              has_gap,
            "pattern":              pat,
            "is_reversal":          is_reversal,
            "zone_width_pct":       round(abs(prox - dist) / max(abs(prox), 1) * 100, 4),
            "legin_idx":            int(legin_i),
            "legout_idx":           int(j),
            "candles_since_legout": csince,
            "zone_state":           zstate,
            "ema_bonus":            e_bonus,
            "score":                total_score,
            "zone_label":           zone_label,
            "aggressive":           aggressive,
            "confirmation":         confirmation,
            "conservative":         conservative,
        })

        i = j + 1

    # Sort: score DESC, then zone_state rank ASC (TOUCHING_NOW first)
    candidates.sort(key=lambda z: (-z["score"], _STATE_RANK.get(z["zone_state"], 9)))
    return candidates
