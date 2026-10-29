"""Settings, timeframes and default thresholds. Thresholds can be overridden in the settings table.
Every candle is stored and built in UTC, as Binance builds it. Times are shown in the time zone set under Settings."""
import os
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

BINANCE_REST = os.getenv("BINANCE_REST", "https://fapi.binance.com").rstrip("/")
BINANCE_WS = os.getenv("BINANCE_WS", "wss://fstream.binance.com").rstrip("/")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def database_url():
    """DATABASE_URL wins if set. Otherwise the DB_* values select MySQL. With neither, a local SQLite file.
    The MySQL URL is built field by field, so a password with @ : / or # needs no escaping in .env."""
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    if os.getenv("DB_NAME"):
        return URL.create("mysql+pymysql", username=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
                          host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", "3306")),
                          database=os.getenv("DB_NAME"), query={"charset": "utf8mb4"})
    return "sqlite:///./crypto_scanner.db"


DATABASE_URL = database_url()

# Ordered highest to lowest.
TIMEFRAMES = ["1y", "6mo", "3mo", "1mo", "1w", "1d", "4h", "2h", "1h", "30m", "15m", "5m", "1m"]
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440, "1w": 10080}

# Downloaded from Binance as its own intervals. keep_days: candles older than this are not downloaded and are removed;
# None keeps everything from the coin's listing date.
FETCHED = {
    "1m": {"interval": "1m", "keep_days": 14},
    "5m": {"interval": "5m", "keep_days": 180},
    "15m": {"interval": "15m", "keep_days": 365},
    "30m": {"interval": "30m", "keep_days": 365},
    "1h": {"interval": "1h", "keep_days": 730},
    "2h": {"interval": "2h", "keep_days": None},
    "4h": {"interval": "4h", "keep_days": None},
    "1d": {"interval": "1d", "keep_days": None},
    "1w": {"interval": "1w", "keep_days": None},
    "1mo": {"interval": "1M", "keep_days": None},
}
# Built from closed monthly candles (Binance has no such intervals): derived timeframe -> source timeframe.
DERIVED = {"3mo": "1mo", "6mo": "1mo", "1y": "1mo"}

# GTF analysis modes: (HTF for location and curve, ITF for trend, LTF for the execution zone). Lowest first.
MODES = {
    "15M": ("15m", "5m", "1m"),
    "30M": ("30m", "15m", "5m"), "HOURLY": ("1h", "30m", "5m"), "2HR": ("2h", "30m", "5m"), "4HR": ("4h", "1h", "15m"),
    "DAILY": ("1d", "1h", "30m"), "WEEKLY": ("1w", "1d", "1h"), "MONTHLY": ("1mo", "1w", "1d"),
    "QUARTERLY": ("3mo", "1mo", "1d"), "HALFYEARLY": ("6mo", "3mo", "1w"), "YEARLY": ("1y", "6mo", "1mo"),
}

BTC = "BTCUSDT"     # the coin the BTC agreement and relative strength checks compare with

DEFAULTS = {
    # GTF zone rules
    "base_body_pct": 50.0,       # base candle: body strictly below this share of the range
    "leg_body_pct": 50.0,        # explosive candle: body strictly above this share of the range
    "max_base": 6,
    "marking_rule": "scanner",   # scanner: zones found, tested and scored as scanner.py v6.4 does; pdf: as the GTF course PDF marks them
    "zone_history": "scanner",   # scanner: only zones inside the history window of each timeframe (see engine/window.py); all: every stored zone
    "htf_zone_pick": "fresh",    # fresh: the nearest HTF demand and supply must never have been tested (GTF PDF page 31); any: any live zone (scanner.py)
    "gap_exciting_pct": 0.25,    # PDF marking: a candle that opens at least this far (% of price) beyond the previous high or low counts as exciting
    "max_wick_tests": 1,         # earlier tests an execution zone may have; one more test uses the zone up
    "min_score": 5.0,            # 7 = entry type 1 only, 5 = entry types 2 and 3 as well
    "min_htf_score": 0.0,
    "ema_period": 20,            # an EMA of either period running through the zone adds 1 point
    "ema_period_2": 50,
    "entry_23": "TYPE_3",        # which entry a score of 5 to 6.5 takes: TYPE_2 (next candle opens inside) or TYPE_3 (next candle leaves)
    "approach_pct": 1.0,         # "approaching" when price is within this percentage of the proximal line
    "all_ltf_zones": False,      # alerts and automatic paper orders on every qualifying LTF zone; off: only the best one per coin and mode
    "min_stop_pct": 0.0,         # an execution zone whose stop is closer than this % of price is not traded (0 = off). Fees eat thin zones.
    "min_bases": 0,              # an execution zone with fewer base candles than this is not traded (0 = off)
    # GTF PDF filters for a strong execution zone (all off by default; they only decide which zones are traded)
    "max_bases": 0,              # PDF p.22: more base candles than this is traded only with a strong leg-out (gap or 2 exciting candles); 0 = off
    "skip_reaction_zones": False,  # PDF p.35-36 credibility: a zone formed in the reaction of an earlier zone is not traded, unless its leg-out is strong
    "check_next_htf": False,     # PDF p.30: no trade when a fresh opposite zone one timeframe above the HTF lies between the entry and the target
    "ltf_max_gap_pct": 50.0,     # an execution zone further than this from price is ignored
    "htf_buffer_pct": 10.0,      # execution zones may sit this far beyond the HTF distal line, as a share of the HTF zone width
    "contact_touch_pct": 0.1,    # HTF contact: zones this close (as a percentage of price) are touching
    "contact_near_pct": 1.0,     # HTF contact: zones this close are near
    "direction_rule": "location",  # location: the curve decides, the ITF only in equilibrium; trend: the ITF decides
    "target_rr": 2.0,            # target as a multiple of entry to stop loss, for every entry type
    # trailing stop loss (sim/trailing.py), for paper trading and the backtest. Off: every trade keeps its first stop.
    "trail_method": "off",       # off | step | swing | atr | lock
    "trail_start_r": 1.0,        # trailing starts once price has gone this many R in the trade's favour
    "trail_keep_target": True,   # keep the fixed target while trailing (false: remove it and let the trade run)
    "trail_atr_mult": 2.0,       # atr: the stop stays this many ATRs behind the best price
    "trail_swing": 2,            # swing: candles on each side of a swing low (or high)
    "trail_tf": "ltf",           # the timeframe whose closed candles move the stop: ltf (the trade's own) or itf (the mode's middle one)
    "trail_lock_pct": 10.0,      # lock: one step is this % of the distance from the entry to the first target
    "trail_lock_gap_pct": 20.0,  # lock: price must pass a step by this % of one step before the stop moves to it
    "trail_lock_target": "move", # lock: at the target, "move" it forward one step (the trade runs) or "close" there
    # ITF trend: slope of the 50 SMA over 7 candles must pass a threshold
    "itf_rule": "atr",           # atr: a share of the average move per candle; pct: a share of price (scanner.py used 0.05%)
    "itf_atr_mult": 0.1,         # atr rule: the slope must pass this many times the average move per candle (last 14)
    "itf_slope_pct": 0.15,       # pct rule: the slope must pass this percentage of price
    # confirmation score
    "confirm_volume_mult": 1.5,  # leg-out volume at least this many times the average before it
    "confirm_funding_pct": 0.03,  # funding: the crowd leans hard the trade's way when funding is beyond this (% per interval)
    "modes": list(MODES),        # analysis modes the scanner runs
    "universe": [],              # the coin list; filled with the top coins by volume on the first start
    "top_n": 50,                 # an automatic coin list is the top N by 24 hour volume (the dashboard offers top 20 and top 50)
    "universe_auto": True,       # the list follows the top N every day until coins are chosen by hand on the Settings page
    # days of candles kept (and downloaded the first time) per timeframe; the others are kept from the listing date.
    # They reach well past the history the zone rules search (engine/window.py); raise them for longer backtests.
    "history_days": {"1m": 14, "5m": 90, "15m": 180, "30m": 180, "1h": 365},
    "display_tz": "Asia/Riyadh",
    # costs, for the backtest, the scan-date check and paper trading (Binance USDT-M regular user rates)
    "fee_maker_pct": 0.02, "fee_taker_pct": 0.05, "fee_bnb": False,   # paying fees in BNB takes 10% off
    "include_funding": True,
    # paper trading
    "paper_capital": 10000.0, "paper_risk_pct": 1.0, "paper_slippage_pct": 0.05,
    "paper_leverage": 5.0, "paper_mmr_pct": 0.5,      # maintenance margin rate used for the estimated liquidation price
    "paper_auto": False, "paper_auto_min_score": 8,
    # alerts
    "telegram_alerts": True,
}
