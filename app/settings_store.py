"""Read and write settings. A value in the settings table overrides the default in config."""
from sqlalchemy.orm import Session
from .config import DEFAULTS
from .models import Setting


def get(db: Session, key: str, default=None):
    row = db.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, default)


def put(db: Session, key: str, value) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def thresholds(db: Session, timeframe: str) -> dict:
    """Defaults, then global overrides, then per-timeframe overrides stored under thresholds:<tf>."""
    th = {k: get(db, k) for k in DEFAULTS if k != "universe"}
    th.update(get(db, f"thresholds:{timeframe}", {}) or {})
    return th
