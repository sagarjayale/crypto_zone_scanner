"""History windows. scanner.py v6.4 read each timeframe only from a start date (for Indian stocks: monthly and up from
2000, daily five years, 30 min to 4 hr from the start of the quarter, 5 and 15 min one month), so zones older than that
were never seen. The crypto version keeps the same idea with windows suited to coins that trade around the clock and
have a few years of history at most:

  1 min                   14 days back
  5 and 15 min            30 days back
  30 min, 2 hr, 4 hr      90 days back
  1 hr                    one year back
  day and above           everything stored (from the coin's listing date)

With the setting zone_history = "scanner" (the default) zones whose base starts before the window are left out.
zone_history = "all" uses every stored zone."""
from datetime import date, datetime, time, timedelta

DAYS_BACK = {"1m": 14, "5m": 30, "15m": 30, "30m": 90, "1h": 365, "2h": 90, "4h": 90}


def start(mode: str, tf: str, end, th: dict) -> datetime | None:
    """First moment of the window read for timeframe tf, or None to use all history. `mode` is kept for the callers;
    the crypto windows depend on the timeframe only."""
    if (th.get("zone_history") or "scanner") == "all" or end is None or tf not in DAYS_BACK:
        return None
    if isinstance(end, datetime):
        stop = end
    elif isinstance(end, date):
        stop = datetime.combine(end, time(23, 59, 59))
    else:
        stop = end
    return stop - timedelta(days=DAYS_BACK[tf])


def keep(zones: list, since: datetime | None) -> list:
    """The zones whose base starts inside the window (so their leg-in is inside it too)."""
    if since is None:
        return list(zones)
    return [z for z in zones if z.base_start_ts is not None and z.base_start_ts > since]
