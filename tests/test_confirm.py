"""Confirmation score: the pieces that need no database."""
from app.engine import confirm


def test_mode_agreement_points():
    a = confirm.agreement("BUY", [("WEEKLY", "BUY"), ("MONTHLY", "BUY"), ("QUARTERLY", "NO_TRADE")])
    assert (a["points"], a["max"], a["note"]) == (2, 2, "Weekly, Monthly agree")            # a mode with no trade is not counted
    a = confirm.agreement("BUY", [("WEEKLY", "BUY"), ("MONTHLY", "SELL")])
    assert (a["points"], a["passed"]) == (1, False) and "Monthly against" in a["note"]      # half agree
    assert confirm.agreement("BUY", [("WEEKLY", "SELL"), ("MONTHLY", "SELL"), ("YEARLY", "BUY")])["points"] == 0
    none = confirm.agreement("SELL", [("WEEKLY", "NO_TRADE")])
    assert (none["applicable"], none["points"], none["max"]) == (False, 0, 0)               # nothing to compare with: left out


def test_summary_counts_only_what_could_be_checked():
    checks = [confirm.check("a", "A", 2, 2, ""), confirm.check("b", "B", 1, 1, ""), confirm.check("c", "C", 0, 1, ""),
              confirm.check("d", "D", 0, 1, "", applicable=False)]
    s = confirm.summary(checks)
    assert (s["points"], s["max"], s["pct"], s["label"]) == (3, 4, 75, "strong")
    assert confirm.summary([confirm.check("a", "A", 1, 2, "")])["label"] == "moderate"
    assert confirm.summary([confirm.check("a", "A", 0, 2, "")])["label"] == "weak"
    assert confirm.summary([confirm.check("a", "A", 0, 1, "", applicable=False)]) == {"points": 0, "max": 0, "pct": None, "label": None, "checks": [confirm.check("a", "A", 0, 1, "", applicable=False)]}


def test_funding_check_by_side():
    assert confirm.funding_check("BUY", 0.0001, 0.03)["points"] == 1                 # +0.01%: normal, longs pay a little
    hot = confirm.funding_check("BUY", 0.0005, 0.03)                                  # +0.05%: longs crowded
    assert hot["points"] == 0 and "leans hard" in hot["note"]
    assert confirm.funding_check("SELL", 0.0005, 0.03)["points"] == 1                # a short against crowded longs is fine
    assert confirm.funding_check("SELL", -0.0004, 0.03)["points"] == 0               # shorts crowded: against a short
    assert confirm.funding_check("BUY", None, 0.03)["applicable"] is False


def test_open_interest_change_over_the_leg_out():
    from datetime import datetime, timedelta
    t0 = datetime(2026, 10, 18, 10, 0)
    series = [(t0 + timedelta(minutes=5 * k), 1000.0 + 10 * k) for k in range(13)]   # 10:00 to 11:00
    assert confirm.oi_change(series, t0 + timedelta(minutes=12), t0 + timedelta(minutes=27)) == (1020.0, 1060.0)   # 10:10 and 10:30
    assert confirm.oi_change(series, t0 - timedelta(hours=1), t0) is None                 # nothing stored before the start
    assert confirm.oi_change(series, t0 + timedelta(minutes=58), t0 + timedelta(minutes=70)) is None   # nothing after the end
    assert confirm.oi_change([], t0, t0) is None
