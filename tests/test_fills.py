from app.sim import fills
from app.sim.backtest import summarize
from datetime import datetime


def test_entry_and_stop_rules():
    assert fills.entry_touched("demand", 521, 534, 519) and not fills.entry_touched("demand", 521, 534, 522)
    assert fills.entry_touched("supply", 912, 913, 900) and not fills.entry_touched("supply", 912, 911, 900)
    assert fills.stop_hit("demand", 512, 530, 511) and not fills.stop_hit("demand", 512, 530, 513)


def test_exit_rules_stop_first_and_gaps():
    assert fills.exit_after_entry("demand", 512, 557, 530, 560, 510) == (512, "loss")     # both reached: stop is taken
    assert fills.exit_after_entry("demand", 512, 557, 505, 520, 500) == (505, "loss")     # gap below stop: exit at open
    assert fills.exit_after_entry("demand", 512, 557, 560, 565, 558) == (557, "win")      # gap above target: target price
    assert fills.exit_after_entry("demand", 512, 557, 530, 540, 520) is None
    assert fills.exit_after_entry("supply", 928, 870, 915, 930, 860) == (928, "loss")
    assert fills.exit_after_entry("supply", 928, 870, 900, 905, 869) == (870, "win")


def test_result_in_r_after_costs():
    assert round(fills.result_r("demand", 521, 557, 9, 0.0), 6) == 4.0
    assert round(fills.result_r("demand", 521, 512, 9, 0.0), 6) == -1.0
    assert round(fills.result_r("supply", 912, 870, 16, 0.0), 6) == 2.625
    assert round(fills.result_r("demand", 521, 557, 9, 0.05), 4) == round(4.0 - 2 * 0.0005 * 521 / 9, 4)


def test_summary_metrics():
    def t(day, result, r, score=8):
        return {"result": result, "result_r": r, "score": score, "timeframe": "1d",
                "entry_ts": datetime(2026, 1, day), "exit_ts": datetime(2026, 1, day + 1)}
    s = summarize([t(1, "win", 3.0), t(3, "loss", -1.0), t(5, "loss", -1.0, 7), t(7, "win", 2.0, 7), t(9, "open", 0.4)])
    assert (s["trades"], s["win_rate"], s["expectancy"], s["total_r"]) == (4, 50.0, 0.75, 3.0)
    assert (s["profit_factor"], s["max_drawdown_r"], s["longest_losing_streak"], s["open_trades"]) == (2.5, 2.0, 2, 1)
    assert s["by_score"]["7"] == {"trades": 2, "win_rate": 50.0, "expectancy": 0.5}
    assert [p[1] for p in s["equity"]] == [3.0, 2.0, 1.0, 3.0]
    assert (s["best_trade_r"], s["worst_trade_r"]) == (3.0, -1.0)
    assert summarize([])["trades"] == 0
