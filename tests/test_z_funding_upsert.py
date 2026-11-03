"""Funding rows with and without a mark price are saved one after the other (old Binance records have no mark price):
each set of columns gets its own statement, and rows in one batch always share the same columns."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects import mysql

from app.data.market import _upsert
from app.data.sync import upsert_statement
from app.db import SessionLocal
from app.models import FundingRate

KEY = "FUNDTESTUSDT"


def test_mysql_statement_matches_the_columns_given():
    with_mark = upsert_statement("mysql", FundingRate, ("instrument_key", "ts"), ["rate", "mark"])
    without = upsert_statement("mysql", FundingRate, ("instrument_key", "ts"), ["rate"])
    assert with_mark is not without
    sql = str(without.compile(dialect=mysql.dialect(), column_keys=["instrument_key", "ts", "rate"]))
    assert "mark" not in sql


def test_rows_with_and_without_a_mark_price():
    with SessionLocal() as db:
        _upsert(db, FundingRate, [{"instrument_key": KEY, "ts": datetime(2026, 10, 1, 8), "rate": 0.0001, "mark": 100.0}], ("instrument_key", "ts"))
        _upsert(db, FundingRate, [{"instrument_key": KEY, "ts": datetime(2020, 1, 16, 8), "rate": 0.0002}], ("instrument_key", "ts"))
        _upsert(db, FundingRate, [{"instrument_key": KEY, "ts": datetime(2020, 1, 17, 8), "rate": 0.0003},          # mixed batch
                                  {"instrument_key": KEY, "ts": datetime(2026, 10, 2, 8), "rate": 0.0004, "mark": 101.0}], ("instrument_key", "ts"))
        got = {r.ts: (r.rate, r.mark) for r in db.scalars(select(FundingRate).where(FundingRate.instrument_key == KEY))}
    assert got[datetime(2026, 10, 1, 8)] == (0.0001, 100.0)
    assert got[datetime(2020, 1, 16, 8)] == (0.0002, None)
    assert got[datetime(2020, 1, 17, 8)] == (0.0003, None) and got[datetime(2026, 10, 2, 8)] == (0.0004, 101.0)


def test_every_insert_names_the_columns_its_update_uses():
    """SQLAlchemy leaves a column that is empty (None) in the first row out of the INSERT. MySQL refuses an update that
    names a column the INSERT does not have, so the update must only name columns that hold values (SQLite lets it pass,
    so the statements are checked here)."""
    import re
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session
    from app.models import Base
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    sent = []
    event.listen(eng, "before_cursor_execute", lambda conn, cur, stmt, *a: sent.append(stmt))
    with Session(eng) as db:
        _upsert(db, FundingRate, [{"instrument_key": KEY, "ts": datetime(2020, 1, 16, h), "rate": 0.0001, "mark": None} for h in range(3)],
                ("instrument_key", "ts"))
        _upsert(db, FundingRate, [{"instrument_key": KEY, "ts": datetime(2026, 10, 1, 8), "rate": 0.0002, "mark": 100.0},
                                  {"instrument_key": KEY, "ts": datetime(2020, 1, 16, 0), "rate": 0.0003, "mark": None}], ("instrument_key", "ts"))
        got = {r.ts: (r.rate, r.mark) for r in db.scalars(select(FundingRate))}
    inserts = [s for s in sent if s.startswith("INSERT")]
    assert len(inserts) == 3
    for s in inserts:
        cols = set(re.search(r"\(([^)]*)\) VALUES", s).group(1).replace(" ", "").split(","))
        updated = set(re.findall(r"(\w+) = excluded", s))
        assert updated <= cols, s
    assert got[datetime(2026, 10, 1, 8)] == (0.0002, 100.0) and got[datetime(2020, 1, 16, 0)] == (0.0003, None)
