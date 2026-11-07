"""Check the trailing stop of every open paper position: works the lock out again from the stored candles, exactly as the
app does each minute, and prints each step of the way so a stop that does not move can be explained.
Read only: nothing is saved or changed.

Run from the crypto_zone_scanner folder with the app's virtual environment:
    python scripts/check_trailing.py"""
import os
import sys
import traceback
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from app import settings_store                              # noqa: E402
from app.data.resample import close_time                    # noqa: E402
from app.data.sync import load_candles, now_utc             # noqa: E402
from app.db import SessionLocal                             # noqa: E402
from app.models import SyncState                            # noqa: E402
from app.sim import paper, trailing                         # noqa: E402


def main() -> None:
    now = now_utc()
    with SessionLocal() as db:
        trades = paper.open_trades(db)
        print(f"{len(trades)} open positions, now {now:%Y-%m-%d %H:%M:%S} UTC\n")
        for t in trades:
            st = t.trail or {}
            print(f"#{t.id} {t.instrument_key} {'LONG' if t.side == 'demand' else 'SHORT'} entry {t.fill_price:g} "
                  f"filled {t.fill_ts:%H:%M:%S} UTC, stop now {t.stop:g}")
            if not st:
                print("   trailing: OFF for this trade (it was filled while trailing was off)\n")
                continue
            tf = st.get("tf") or t.timeframe or "1h"
            print(f"   method {st.get('trail_method')}, lock {st.get('trail_lock_pct')}%, gap {st.get('trail_lock_gap_pct')}%, "
                  f"candles {tf}, first stop {st.get('first_stop')}, first target {st.get('first_target')}, moves {st.get('moves')}")
            sync = db.get(SyncState, (t.instrument_key, tf))
            last = sync.last_candle_ts if sync else None
            print(f"   newest stored {tf} candle: {last:%H:%M} UTC (closed {close_time(last, tf):%H:%M})" if last
                  else f"   newest stored {tf} candle: NONE — the stop cannot move without {tf} candles")
            bars = load_candles(db, t.instrument_key, tf, since=t.fill_ts + timedelta(seconds=1))
            print(f"   {tf} candles after the fill: {len(bars)}")
            if bars.empty:
                print("   -> nothing to trail on yet\n")
                continue
            best = bars["high"].max() if t.side == "demand" else bars["low"].min()
            k = paper.lock_view(t, settings_store.thresholds(db, t.timeframe or "1d"))
            print(f"   best price in those candles: {best:g}; step 1 needs {k.get('next_trigger', 'n/a') if not k.get('steps') else '(already locked)'}")
            try:
                th = settings_store.thresholds(db, t.timeframe or "1d")
                tr = trailing.Trailer(t.side, t.fill_price, st["first_stop"], th, st, [], target=st.get("first_target", t.target))
                for b in bars.itertuples():
                    tr.close_bar(b.high, b.low, b.close)
                print(f"   worked out now: stop {tr.stop:g}, steps locked {tr.locked}, moves {tr.moves} ({tr.why or 'no move'})")
                tighter = (tr.stop > t.stop) if t.side == "demand" else (tr.stop < t.stop)
                if tr.moves and tighter:
                    print("   -> THE STORED STOP IS BEHIND: the minute job has not applied this. Send this output.")
                elif tr.moves:
                    print("   -> stored stop already at (or tighter than) this: OK")
                else:
                    print("   -> no step reached on the stored candles: the lock is correctly waiting")
            except Exception:
                print("   ERROR while working it out:")
                traceback.print_exc()
            print()


if __name__ == "__main__":
    main()
