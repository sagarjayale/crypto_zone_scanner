"""Trading costs on Binance USDT-M futures, shared by the backtest, the scan-date check and paper trading.

Fees. Every fill pays a fee on its value (price times quantity):
- maker: a limit order resting in the book. Entry type 1 (a limit at the proximal line) and an exit at the target.
- taker: an order that takes the price that is there. Entry types 2 and 3 (they enter at a candle's open or close,
  which needs a market order), a stop loss, a manual close, and a position still open at the end (valued as if closed).
Paying fees in BNB takes 10% off both rates.

Funding. At each funding time a long pays the funding rate times the position value and a short receives it (the other
way round when the rate is negative). The settlements are read from the stored funding history, so the coin's own
interval (8 hours, or 4 or 1 for some coins) is used as Binance applied it.

In R: 1 R is the planned risk, |entry - stop| per coin. Fees and funding per coin divided by that give their share in R."""
from bisect import bisect_right
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FundingRate

BNB_DISCOUNT = 0.10


def fee_rates(th: dict) -> tuple[float, float]:
    """(maker, taker) as fractions, after the BNB discount when it is switched on."""
    off = (1 - BNB_DISCOUNT) if th.get("fee_bnb") else 1.0
    maker = float(th.get("fee_maker_pct") if th.get("fee_maker_pct") is not None else 0.02) / 100.0 * off
    taker = float(th.get("fee_taker_pct") if th.get("fee_taker_pct") is not None else 0.05) / 100.0 * off
    return maker, taker


def entry_is_maker(entry_type: str | None) -> bool:
    return (entry_type or "TYPE_1") == "TYPE_1"


def exit_is_maker(reason: str | None) -> bool:
    """Only a target is a resting limit order; a stop, a manual close and an end-of-data valuation take the price."""
    return reason in ("target", "win", "target hit")


def fees_per_unit(entry: float, exit_price: float, entry_type: str | None, exit_reason: str | None, th: dict) -> float:
    maker, taker = fee_rates(th)
    return entry * (maker if entry_is_maker(entry_type) else taker) + exit_price * (maker if exit_is_maker(exit_reason) else taker)


class Funding:
    """The funding settlements of one coin, loaded once and looked up by time."""

    def __init__(self, rows: list[tuple[datetime, float, float | None]]):
        rows = sorted(rows)
        self.ts = [r[0] for r in rows]
        self.rate = [r[1] for r in rows]
        self.mark = [r[2] for r in rows]

    @classmethod
    def load(cls, db: Session, key: str) -> "Funding":
        return cls([(r.ts, r.rate, r.mark) for r in db.scalars(select(FundingRate).where(FundingRate.instrument_key == key))])

    def per_unit(self, side: str, start: datetime, end: datetime, price: float) -> float:
        """Funding received (+) or paid (-) per coin held from `start` to `end` (settlements after start, up to and
        including end). The mark price of each settlement is used, or `price` when Binance gave none."""
        if not self.ts or end <= start:
            return 0.0
        sign = 1.0 if side == "demand" else -1.0
        total = 0.0
        for k in range(bisect_right(self.ts, start), bisect_right(self.ts, end)):
            total -= sign * self.rate[k] * (self.mark[k] or price)
        return total

    def __len__(self) -> int:
        return len(self.ts)


def result_r(side: str, entry: float, exit_price: float, risk: float, entry_type: str | None, exit_reason: str | None,
             th: dict, funding_unit: float = 0.0) -> dict:
    """Result of one trade in R after fees and funding. Returns {"r", "fees_r", "funding_r", "gross_r"}."""
    gross = (exit_price - entry) if side == "demand" else (entry - exit_price)
    fees = fees_per_unit(entry, exit_price, entry_type, exit_reason, th)
    if risk <= 0:
        return {"r": 0.0, "gross_r": 0.0, "fees_r": 0.0, "funding_r": 0.0}
    return {"r": (gross - fees + funding_unit) / risk, "gross_r": gross / risk, "fees_r": -fees / risk, "funding_r": funding_unit / risk}
