# Crypto zone scanner

Demand and supply zone scanner for Binance USDT-M perpetual futures, using GTF rules: the HTF gives location and the
curve, the ITF gives the trend, and the LTF gives the execution zone. It includes data sync, chart with replay, a
scan-date backtest, a live dashboard, paper trading with fees, funding and leverage, Telegram alerts and settings.
Database: MySQL. It is the crypto version of the stock scanner, with the same engine and the same zone rules.

Binance market data is public: **no API key is used and no order is ever sent to Binance.**

The rules have not been tested on real crypto data. Nothing here promises a win rate; the Backtest page exists to
measure what the rules do, after fees and funding, on the coins and dates you choose.

## Set up on Linux

```
sudo apt install python3-venv            # once, if venv is missing
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                     # then edit .env
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Run it as one process, as shown: the schedule and the live price feed live inside the app, so do not add
`--workers`. Crypto trades around the clock, so leave it running; whatever it missed while stopped is downloaded at
the next start.

In `.env`, fill in the MySQL values (no Binance key is needed):

```
DB_HOST=localhost
DB_PORT=3306
DB_USER=your_mysql_user
DB_PASSWORD=your_password        # written as it is; @ and other symbols need no escaping
DB_NAME=crypto_scanner_db
```

The database and its tables are created on first start if they do not exist. The MySQL user needs the right to create
that database, or create it yourself first: `CREATE DATABASE crypto_scanner_db CHARACTER SET utf8mb4;`
It is a separate database from the stock scanner's; the two apps can run side by side on different ports
(`--port 8001` for this one, for example).

Leave the `DB_` values empty to use a local SQLite file instead.

Open http://127.0.0.1:8000 for the pages, or http://127.0.0.1:8000/docs to call every endpoint by hand.

### Keep it running (recommended)

The downloads of missing candles, the scans and paper trading all run on the app's own schedule, around the clock, with
no cron job and no button to press, but only while the app is running. To start it with the computer and restart it by
itself if it ever stops, install it as a service (the file has the paths of /var/www/html/scan/advance_app; change them
if the app lives elsewhere):

```bash
sudo cp deploy/crypto-scanner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-scanner
journalctl -u crypto-scanner -f            # the log
sudo systemctl restart crypto-scanner      # after unzipping an update
```

Stop any copy started by hand first (Ctrl+C), since both would want port 8001. When the app was off for a while, the
start-up catch-up downloads exactly the candles missed, then the every-minute schedule carries on.

## First use, in order

1. Start the app. On the very first start it downloads the list of USDT perpetuals from Binance and makes the top 30
   by 24 hour volume your coin list ("Top N" on the Settings page), then downloads and scans them in the background.
2. Data page: watch the coins become ready. A coin listed years ago needs a few hundred requests the first time; the
   app keeps to half of Binance's request weight, so 30 coins take roughly half an hour to an hour. Each coin appears on
   the other pages as soon as it is done.
3. Settings page: change the coin list if you like: find a coin, show the top N, or paste symbols.
4. Dashboard: setups appear here. Chart and Paper trade are on each row.

## Score filters

The dashboard and the backtest report share four score filters. Each starts on "any".

| Filter | Judges | Highest score |
| --- | --- | --- |
| HTF demand score | The nearest HTF demand zone | 7 |
| HTF supply score | The nearest HTF supply zone | 7 |
| LTF demand zone score | The LTF entry zone of a buy setup | 8 (with the EMA point) |
| LTF supply zone score | The LTF entry zone of a sell setup | 8 (with the EMA point) |

- Every filter that is set must pass. A coin with no zone of that kind fails that filter.
- With either LTF filter set, a coin with no LTF entry zone is hidden, so "no trade" rows only show when both are on "any".
- Every filter starts on "any", so every coin shows; "Clear score filters" sets all four back to "any". A line above the table says how many coins pass and how many are hidden.
- The choices are remembered in the browser and shared by both pages.
- On the backtest report the totals per plan are recalculated for the coins that pass. A saved check keeps every
  coin, so you can reopen it and try other filters without running it again. The report also shows the HTF demand
  and supply zones with their scores.
- On the chart, every drawn zone carries its score and type, and the result panel shows the breakdown (freshness,
  strength, base, and the EMA point for the LTF zone). The "Zone scores" switch hides the labels.

## Confirmation score

A second score beside the scanner score, made of checks the zone rules do not make. The scanner score is unchanged.

| Check | Points | Passes when | Needs |
| --- | --- | --- | --- |
| Mode agreement | 2 | Every higher mode that has a side agrees (1 point when at least half do) | Stored results |
| HTF contact | 1 | The entry zone is inside, overlaps or touches a higher-timeframe zone | Stored zones |
| BTC agreement | 1 | BTCUSDT gives the same side in the same mode | BTCUSDT in the coin list |
| Leg-out volume | 1 | The candle that formed the zone had 1.5 times the average USDT volume of the 20 before it | Volume |
| Strength vs BTC | 1 | The coin did better than BTC over the last 20 LTF candles for a long, worse for a short | BTC candles |
| Funding | 1 | The crowd is not leaning hard the trade's way: for a long the current funding rate is at most +0.03% per interval, for a short at least -0.03% | Funding rate |
| Open interest | 1 | Open interest rose over the leg-out candle: new positions came in with the move that left the zone | Open interest history for that candle |

- A check that cannot be made is left out of both the points and the maximum, so the score always reads "points of
  what could be checked". 75% or more is strong, 50% or more moderate, less is weak.
- Binance serves only about 30 days of open interest history. The app saves it every 15 minutes (5 minute and 1 hour
  readings), so the check covers zones formed in the last month at first and more as the stored history grows.
- Dashboard: a Confirmation column, the list of checks in the row details, and a filter: any, moderate or better, strong.
- The two limits are settings ("Confirmation score" on the Settings page).

## Coins become available one by one

Every coin in the list goes through the same steps in the background: waiting, downloading, scanning, ready.

- A coin appears on the dashboard, chart, paper and backtest pages as soon as its own download and scan are done.
  The others carry on; nothing waits for the whole list.
- Up to four coins download side by side, under one request-weight limit. Scans run one at a time in a worker thread, so the pages keep answering.
- A coin that has been ready once stays visible while it is being updated, and also if a later update fails; the
  failure and its reason are shown on the Data page.
- The Data page shows where every coin stands and refreshes itself while work is in progress.
- "Download and scan all" on the Data page starts the work for the whole list. Adding a coin on the Settings page
  starts it for that coin.

## The result of a mode

For each ready coin and each mode the app saves one result at every scan, shaped like the scan result of the
original scanner: price, nearest HTF demand and supply with the freshness, strength and base scores, market state,
curve, HTF trend, ITF trend with its reason, trade side, the best LTF entry zone with its score (including the EMA
bonus) and type, its state, the signal, the primary entry with stop loss and target, and all three entry plans.

- An execution zone may have been tested once before; a zone tested twice is used up.
- A zone the price is inside right now still counts as the entry zone, so "touching now" shows with live prices.
- The trade score picks the entry: 7 or more takes entry type 1, 5 to 6.5 takes entry type 2 or 3 (your choice in
  Settings), below 5 is no trade with the reason "The trade score is below 5".
- Entry types 2 and 3 have no price until their candles come. Until then the result shows the proximal line as the
  level to watch (`signal.watch`), the stop loss, and `waiting: true`; `signal.entry` and `signal.target` are empty.
- With live prices the dashboard works out the state itself; between scans the rest of the result is as of the last scan.

## Live mode, around the clock

Once the app is running it works on its own, every day of the week, in UTC:

| When (UTC) | What runs |
| --- | --- |
| Every minute, 8 seconds past | Candle close: download the candles that just closed (1 min when a mode uses it; 5 min, 15 min, 30 min, 1 hr, 2 hr, 4 hr, day, week and month when theirs closed) for 8 coins at a time, rescan only the coins and timeframes that got one, check paper orders of coins held on paper. Measured with 50 coins: 8 to 9 seconds a minute, 25 seconds when 5 minute candles close too |
| Every second | Live mark prices of every coin from the Binance WebSocket; alerts when price comes within 1% of a setup's proximal line or reaches it; paper fills, stops and targets |
| Every 5 minutes | Current funding rate and mark price of every coin (one call) |
| Every 15 minutes | Open interest history of the coin list |
| Every hour, 2 minutes past | Funding settlements of the coin list (some coins settle every 1 or 4 hours, most every 8) |
| 00:10 | Coin list refresh: filters, listing dates, funding intervals, 24 hour volumes |
| 00:20 | Gap repair for coins with missing candles; old used-up zones and log rows cleared |
| At start-up | The coin list on the first start (the top 50 by volume), then a catch-up download and scan. Later starts refresh the coin list, download what closed while the app was stopped, and rescan only what changed. If Binance cannot be reached, the first start is tried again every 5 minutes |
| Every minute, 30 seconds past | The live price feed is started again if it has stopped for any reason |

- Live prices come from one WebSocket connection to the all-market mark price stream,
  `wss://fstream.binance.com/market/ws/!markPrice@arr@1s`, so the number of coins does not matter. Binance moved
  futures market streams to the `/market` address in 2026; the old `/ws` address still connects but has sent no mark
  prices since 23 April 2026.
- A connection that sends nothing for 15 seconds is closed and opened again. It reconnects on its own with a growing
  pause, and Binance closes every connection after 24 hours, which is handled the same way. While it is down or
  silent, mark prices are fetched over REST every 5 seconds, and the badge says "Prices by REST".
- A watchdog checks every minute that the feed is running and starts it again if it has stopped for any reason.
- Prices are mark prices, which Binance also uses for liquidation. They differ slightly from the last traded price.
- A coin still on its first full download is left to that job: the candle-close job only updates coins that are ready.
- Each job is recorded in the jobs table and shown on the Settings page. A failed job is recorded; it never stops the app.
- Each alert fires once per zone: approaching, then entry reached. Alerts are stored, pushed to the dashboard and sent to Telegram.
- Set `SCHEDULER_ENABLED=0` in `.env` to run the pages and APIs with no schedule and no live prices.

## Candles: what is downloaded and how

- Every timeframe Binance has is downloaded as Binance builds it (1 min, 5 min, 15 min, 30 min, 1 hr, 2 hr, 4 hr, day,
  week, month). Quarter, half year and year are built from closed monthly candles, because Binance has no such
  intervals. Downloading Binance's own intervals is both more accurate (the candles are exactly the ones Binance shows)
  and far cheaper in request weight than building them all from 5 minute candles.
- All candles are in UTC, as Binance builds them: a day runs 00:00 to 24:00 UTC, a week starts Monday 00:00 UTC, a
  month on the 1st at 00:00 UTC, quarters in January, April, July and October. The pages show times in the time zone
  set under Settings (Asia/Riyadh to begin with).
- A candle is stored only once it has closed. The running candle is never used to find or test a zone.
- History kept (and downloaded the first time): 1 min 14 days, 5 min 90 days, 15 and 30 min 180 days, 1 hr 365 days,
  2 hr and above from the coin's listing date. These are settings ("History kept" on the Settings page): they reach well
  past what the zone rules search, so raise them only for longer backtests. Older candles of the short timeframes are
  removed as new ones arrive. After the first download only the candles that closed since the last stored one are fetched.
- Incremental: each sync asks only for the candles after the last stored one, and asks for no more than can exist
  (a request for 1 to 99 candles costs weight 1, up to 1,000 candles weight 5).
- Gaps: missing candles in the last 30 days are counted per coin and timeframe (Data page) and filled by the 00:20 job
  or by Repair gaps.
- Volume is stored in USDT (Binance's quote volume).
- Schedule (UTC, inside the app): new candles every minute at second 8; holes in the last 30 days at 00:20; the coin
  list at 00:10; funding every 5 minutes and open interest every 15; funding history hourly; the live feed is checked
  every 30 seconds. Nothing needs cron.

## More than one LTF zone

Several LTF zones can qualify between the price and one HTF zone. Every one of them gets its own plan and is drawn on
the chart. The dashboard row shows the best (highest score; on a tie, the first the engine lists) with "+N more LTF
zones" under it. "Other LTF zones" on the dashboard (off, score 5+, 6+, 7+ or 8) lists the others at or above that
score under the row, each with its own entry, stop, target, Chart and Paper trade buttons (remembered in this browser).
Only zones that can be traded (score 5 or more) count as other zones. Alerts and automatic paper
orders use only the best zone of each coin and mode, unless "Alerts and automatic paper orders on every LTF zone" is
ticked on the Settings page. A plan saved by an earlier version counts as the best until its timeframe is scanned again.
On the Chart page, "LTF zone" draws the best zone and "Other LTF zones" (off, score 5+, 6+, 7+ or 8) draws the other
qualifying ones at or above that score, dashed, each with its score and entry type; the HTF zones are drawn as before. ("All zones" is different: every zone stored on the timeframe, qualifying or not.)

## Minimum stop distance and minimum base candles (Settings, off by default)

Two filters on which LTF zones are traded. They never change how zones are found or scored, and at 0 the app works
exactly as before.
- Minimum stop distance, % of price: a zone whose stop (the distal line) is closer to its entry (the proximal line)
  than this is not traded. Fees are a share of the position while R is the distance to the stop, so on a thin zone
  the fees alone can cost more than 1 R: at a 0.05% stop, maker in and taker out cost about 1.4 R.
- Minimum base candles: a zone with fewer base candles is not traded.
A zone left out this way is still drawn on the chart (marked "not traded" with the reason when LTF zone is ticked); the
next qualifying zone, if any, becomes the dashboard's zone; with none left the row says why. Alerts, paper trading, the
backtest and the scan-date checks all follow the same settings. "Save and rescan" applies them to every coin.
The dashboard, the checks and the Enter now box also show the fees as R ("fees ≈ 0.5 R on a loss, 0.3 R on a win"):
information only.

## Trailing stop loss (Settings, Trade management: off by default)

Off, every trade keeps its first stop and its target, exactly as before. On, once price has gone `Start trailing at` R
(1 by default) in the trade's favour, the stop moves to break-even (the entry plus the maker and taker fees, so a
break-even exit is about 0 R) and then follows the trade:
- Step: the stop sits at the best R reached less 1, rounded down to 0.5 R (+1 R: break-even; +1.5 R: +0.5 R; +2 R: +1 R).
- Swing: the stop goes just under each new swing low (a candle with lower lows than the `Swing size` candles on each
  side, confirmed that many candles later), less 0.1 of the average candle; above each swing high for a short.
- ATR: the stop stays `ATR multiple` x ATR(14) behind the best price since the entry.
The stop only tightens, never goes below break-even once started, and moves only when a candle of the chosen timeframe
closes (LTF, the trade's own, or ITF, the mode's middle one). Within a candle the stop is checked before it moves.
Timeframe `1 minute` moves the stop at every 1-minute close: a fast move up and back down inside one 5-minute candle
is caught. In the backtest the exit is then checked minute by minute inside each LTF candle too; where no 1-minute
candles are stored the LTF candle stands in (the trade's `trail_tf_used` says ltf). Only 14 days of 1-minute candles
are kept by default: raise History kept, 1 min, then download the older ones once with
`POST /api/data/backfill {"symbols": "all", "timeframes": ["1m"]}` (the regular sync only adds candles after the last).
`Keep the fixed target` off removes the target once trailing has started, so the trade can run. A trailing exit pays
the taker fee and shows as "Trailing stop". Paper positions take the settings in force when they fill and keep them;
each stop move is written in the trade's history with the reason. The backtest uses the settings, or the values a run
gives (`trail_method`, `trail_start_r`, `trail_keep_target`, `trail_atr_mult`, `trail_swing`, `trail_tf` in the run
request), so one method can be compared with another on the same dates without changing the settings.

Every backtest trade also records, for research only: exit reason (target, stop, trail, open), stop moves and final
stop, room to the nearest opposite LTF and HTF zone (in R), departure (how far price went from the zone, in zone
heights), base candles, whether the zone overlaps the mode's HTF zone, zone age (LTF candles), and approach (the
filling candle's range over the average of the 14 before). None of these change a decision.

## Times on the dashboard and the paper page

- Dashboard, Time column: "Signal since" is the close of the candle on which the current buy, sell or no-trade first
  appeared; it stays put while the signal holds and moves when it changes. "Based on" is the close of the last LTF
  candle the result uses. "Zone formed" is when the LTF zone was made. Hover for when the coin was last checked.
- Paper: pending orders show when they were placed and when their signal appeared. Open positions show the entry
  time, the entry price and how long they have been open; closed trades the entry time, exit time and duration.
  Export CSV downloads the closed trades with these times (in UTC).
- All times on the pages are in the time zone set under Settings.

## Binance request limits

- Binance counts request weight per IP per minute. The limit is read from `exchangeInfo` at start-up, not written into
  the code (2,400 per minute when this was built). The app keeps to half of it.
- Every Binance answer carries the weight used so far this minute; the app keeps that number and waits for the next
  minute before a request would pass its share.
- HTTP 429 (too many requests): the app waits as long as Binance's Retry-After says, then tries again.
- HTTP 418 (the IP is banned): every download stops until the ban ends, and the Data page and the badge at the top of
  every page say so.
- The open interest history endpoint has its own limit at Binance; the app keeps to 300 calls in 5 minutes.
- The Data page shows the weight used this minute, the limit, the last 429 and any ban.

## GTF rules as built

**Zone marking: two choices (Settings, "Zone marking").** The default is **scanner.py v6.4**: zones are found, tested,
used up and scored exactly as the user's scanner.py does. A test runs scanner.py's own zone functions
(tests/reference_scanner_v64.py, copied unchanged) next to the app's engine on 24 series of random candles and
checks that both find the same 201 HTF zones and 58 LTF zones, with the same lines, tests and scores. The other choice
is the **GTF course PDF** marking, described after this list.

scanner.py v6.4 marking:

- Base candle: body below 50% of the range. Leg-out: body above 50%, green for demand, red for supply.
- The leg-in is the candle before the base and can be any candle on the HTF. 1 to 6 base candles. A longer run of base
  candles is cut as scanner.py walks it, in blocks of six with one candle between: 7 base candles give no zone,
  8 give a base of 1.
- The leg-out must close beyond the leg-in's close (above it for demand, below it for supply).
- Lines from the base candles only: demand proximal = highest base body, distal = lowest base wick (supply mirrored).
- Execution (LTF) zone: also an exciting leg-in, red for a buy (drop-base-rally), green for a sell (rally-base-drop).
- Test: every closed candle whose wick reaches the proximal line, also several in a row.
- Used up: a closed candle closes strictly inside the zone. The zone disappears one candle later, so that the next
  candle can still give entry type 2 or 3 (scanner.py removes it at once; it has no entry types 2 and 3).
- A close beyond the distal line does not end the zone. The zone is ignored while price is beyond it.
- Score: freshness 3 / 1.5 / 0 for 0 / 1 / 2 or more tests; strength 2 for a gap or a leg-out body of 70% or more,
  otherwise 1; base 2 / 1 / 0; execution zones +1 when the 20 EMA is inside. HTF zones with 2 or more tests stay (for
  the curve) with freshness 0; execution zones with 2 or more tests are not used.
- History (Settings, "History searched for zones"). scanner.py read each timeframe from a start date (for stocks:
  monthly and up from 2000, daily five years, intraday from the start of the quarter). Coins trade around the clock and
  have a few years of history at most, so the crypto windows are:

| Timeframe | Zones searched |
| --- | --- |
| 1 min | the last 14 days |
| 5 min, 15 min | the last 30 days |
| 30 min, 2 hr, 4 hr | the last 90 days |
| 1 hr | the last year |
| Day and above | everything stored, from the coin's listing date |

  Zones whose base starts before the window are left out. "All stored history" uses every zone instead.

GTF course PDF marking (choose "GTF course PDF" in Settings):

**Candles.** Base: body below 50% of the range. Exciting (explosive): body above 50%, green or red. Exactly 50% is neither.

**Gap candle (course page 25).** A candle with a small body that opens at least 0.25% of the price beyond the previous
candle's high (or low) and still closes beyond it counts as an exciting candle in the direction of the gap. The size is
the setting "Gap candle counts as exciting"; 0 switches the rule off. The course only says "significant gap", so 0.25%
is a starting value, not a course number. **Crypto hardly ever gaps**: a perpetual never closes, so each candle opens
where the last one closed. The rule is kept so the course marking stays complete, but it will almost never fire, and
neither will the 2 points for a gap in the scanner.py score (a leg-out body of 70% or more earns them instead).

**Zone.** An exciting leg-in, then 1 to 6 base candles, then an exciting leg-out. More than 6 base candles in a row give
no zone.

| Pattern | Leg-in | Leg-out | Kind |
| --- | --- | --- | --- |
| DBR, drop-base-rally | Down | Up | Demand, reversal |
| RBR, rally-base-rally | Up | Up | Demand, continuation |
| RBD, rally-base-drop | Up | Down | Supply, reversal |
| DBD, drop-base-drop | Down | Down | Supply, continuation |

**Closing rule (course page 24).** The leg-out must close beyond the whole leg-in candle: above its high for demand,
below its low for supply. Closing exactly on the high or low is not enough.

**Lines (course pages 14 to 20).** Body to wick. The proximal line is the highest base body for demand and the lowest
base body for supply. The distal line is the lowest base wick for demand, moved to the leg-out wick, or for a reversal
pattern the leg-in wick, when that wick is lower: the course's exceptional marking. Supply is the mirror with the
highest wick. For continuation patterns the leg-in wick is not used, because price came from beyond the zone.
The chart's zone details say which candle gave the distal line.

| Pattern | Distal line |
| --- | --- |
| DBR | Lowest low of leg-in, base and leg-out |
| RBR | Lowest low of base and leg-out |
| RBD | Highest high of leg-in, base and leg-out |
| DBD | Highest high of base and leg-out |

**Choosing.** The Backtest page reads both settings at once, so the same scan date can be checked under each; the
dashboard follows after "Save and rebuild all zones". The example the course marks as wrong on page 24 is a valid zone
under the scanner.py marking.

**Tests and freshness (PDF marking).** Checked on every closed candle after the zone forms:

| What happens | Result |
| --- | --- |
| A candle closes beyond the distal line | Violated |
| A candle reaches the proximal line | A test begins. Candles that keep reaching the zone belong to the same test |
| A candle no longer reaches the proximal line | The test is over |
| A second test is over | Used up ("tested twice") |

A candle that closes inside the zone no longer ends the zone: entry types 2 and 3 need exactly that candle.

**Trade score (PDF marking).** From the GTF course, out of 8:

| Part | Points |
| --- | --- |
| Freshness | Fresh 3, tested once 1.5, tested twice 0 |
| Strength | Price left with a gap 2, with two exciting candles 2, with one exciting candle and no gap 1 |
| Time at base | 1 to 3 base candles 2, 4 to 5 candles 1, more 0 |
| EMA | 1 when the 20 or the 50 EMA runs through the zone (execution zones only) |

- 7 or more takes entry type 1. 5 to 6.5 takes entry type 2 or 3. Below 5 there is no trade.
- The test that is going on is not counted against freshness: the zone is scored for the test it is in. Once that
  test is over, the zone is "tested once" and its score drops by 1.5.
- "Two exciting candles" means the candle straight after the leg-out is also exciting, in the same direction. It is
  known one candle after the zone forms, so a zone's score can rise from 6 to 7 on that candle.

**Modes.** Each mode names an HTF, an ITF and an LTF:

| Mode | HTF | ITF | LTF |
| --- | --- | --- | --- |
| 15 min | 15 min | 5 min | 1 min |
| 30 min | 30 min | 15 min | 5 min |
| Hourly | 1 hr | 30 min | 5 min |
| 2 hr | 2 hr | 30 min | 5 min |
| 4 hr | 4 hr | 1 hr | 15 min |
| Daily | Day | 1 hr | 30 min |
| Weekly | Week | Day | 1 hr |
| Monthly | Month | Week | Day |
| Quarterly | Quarter | Month | Day |
| Half yearly | Half year | Quarter | Week |
| Yearly | Year | Half year | Month |

Every timeframe up to the month is Binance's own; quarter, half year and year are built from months. The 15 min mode
uses 1 minute candles for the LTF: 5 minutes would make the LTF the same timeframe as the ITF and lose the step down.
Only 14 days of 1 minute candles are kept, which is plenty for zones that close within hours. Yearly and half-yearly
modes see very few candles (BTC has about seven yearly candles, most coins one to three), so they often show "no
zones" rather than a forced signal.

**HTF and curve.** From the current price, the nearest HTF demand below it and the nearest HTF supply above it are
found: demand zones whose distal line is below the price (the highest proximal line wins), supply zones whose distal
line is above it (the lowest proximal line wins). Price inside a zone still counts. **Only zones that have never been
tested are used** (GTF PDF page 31, "nearest fresh supply and demand"): a zone price has come back to is skipped and
the next untested one further away is used. A candle that is still forming has not been counted as a test, so a zone
stays usable during the HTF candle that first reaches it. Settings, "Nearest HTF demand and supply for the curve",
can switch back to "any live zone", as scanner.py does. Tested zones stay on the chart either way.
The curve places price between the two proximal lines: very low, low, equilibrium, high, very high.
With only a demand zone left the state is bullish expansion, with only a supply zone bearish expansion, and the curve
is taken as equilibrium.

**Trends.** HTF: two or more of the last 20 closes above the supply is an uptrend, below the demand a downtrend.
ITF: slope of the 50 SMA over 7 bars (a shorter SMA, then the raw 7-bar slope, when there are fewer candles). The slope
must pass a threshold to count as up or down. scanner.py used 0.05% of price, which is too sensitive for coins that
move several percent in an hour. Settings, "ITF trend", offers two rules:

| Rule | Threshold (50 SMA / shorter SMA / raw slope) |
| --- | --- |
| Average move (the default) | 0.1 / 0.5 / 1.5 times the coin's average move per candle over the last 14 candles (the mean close-to-close change, a close-only stand-in for the ATR) |
| Share of price | 0.15% / 1.5% / 6% of price (scanner.py: 0.05%, with the same 10 and 40 times widening) |

The average-move rule adapts to each coin and each timeframe by itself. Both numbers are settings; they are starting
values, not tested ones.

**Direction: location beats trend.** The curve decides the side at the edges, and the ITF only in the middle.

| Curve | Where the price is | ITF up | ITF down | ITF sideways |
| --- | --- | --- | --- | --- |
| Very low | Inside the HTF demand zone | Long | Long | Long |
| Low | Lowest third above the demand proximal | Long | Long | Long |
| Equilibrium | Middle third | Long | Short | No trade |
| High | Top third below the supply proximal | Short | Short | Short |
| Very high | Inside the HTF supply zone | Short | Short | Short |

A buy is a long position and a sell a short one: the API keeps the words BUY and SELL, the pages say long and short.

- When only one HTF zone is left (an expansion) there is no curve: buy only with the ITF up and the HTF trend not
  down; sell only with the ITF down and the HTF trend not up.
- The earlier rule is still available under "Trade side rule" in Settings: the ITF decides (up buys, down sells,
  sideways is no trade) and the curve only blocks very high for buys and very low for sells. Saving the rule rescans
  every coin. With both rules you can run the same scan-date check twice and compare.
- The dashboard and the chart say what decided the side, and the chart labels the curve bands "low: buy",
  "equilibrium: ITF decides" and "high: sell".
- Under location beats trend a buy can fire while the ITF is falling, and a sell while it is rising.

**Execution zone.** On the LTF, a reversal pattern (drop-base-rally for a buy, rally-base-drop for a sell) with an
explosive leg-in, at most one earlier test, lying between price and the HTF zone (up to 10% of the HTF zone's width beyond
its distal line), and no more than 50% away from price.

**Entry types.** The stop loss is the distal line and the target is 2 times the distance from entry to stop loss
(setting "Target") for all three.

| Entry type | Used for a score of | Entry |
| --- | --- | --- |
| 1 | 7 or more | A limit at the proximal line, set and forget. Filled when price comes back to the zone |
| 2 | 5 to 6.5 | A candle closes inside the zone and the next candle opens inside it: entry at that open |
| 3 | 5 to 6.5 | A candle closes inside the zone and the next candle closes back beyond the proximal line: entry at that close |

- For a score of 5 to 6.5 you choose in Settings which of types 2 and 3 the plans, the alerts and paper trading use
  ("A score of 5 to 6.5 enters with"). Entry type 3 is the default. All three are always shown in the row details.
- "Inside the zone" means from the proximal line to the distal line, both included.
- Entries of types 2 and 3 are found by the scan when the candle closes, not by the live price. A type 2 entry is
  therefore known only after its candle has closed, although its price is that candle's open.
- An entry of type 2 or 3 belongs to one test. When the test ends without it, the plan waits for the next test, by
  which time the zone is "tested once" and is scored again.
- Until the entry happens, a type 2 or 3 plan holds the proximal line as its entry and a target measured from it.
  Both are replaced by the real entry and target when the entry happens. The chart labels such lines
  "waiting (proximal line)".

**Live states and alerts.** Approaching: price within 1% of the proximal line. Touching now: price at or inside the zone.
One alert for each, once per zone.

**Where this differs from the original scanner.py.**

- With the PDF marking a close beyond the distal line retires the zone; with the scanner.py marking it does not.
- Zones are stored and updated candle by candle, so tests and freshness are a history, not a recount on each scan.
- Prices are kept to 8 significant digits instead of scanner.py's 2 decimals, so coins priced in fractions of a cent
  keep their lines.
- Price alerts (approaching, touching) are raised for untested zones only. A zone tested once can still be a setup
  when its score is 5 or more.
- The live candle is never used to form a zone or to count a test; live prices only drive the alerts.
- With the scanner.py marking, a zone that closed inside is removed one candle later than scanner.py removes it.
- Entries are entry types 1, 2 and 3 from the course, not scanner.py's aggressive, confirmation and conservative plans.

**Things worth knowing about these rules.**

- With no size test on exciting candles, many zones are found, most of them soon used up. The course marking finds
  fewer than the scanner.py one.
- Under the scanner.py marking a very thin zone can be touched many times without a close strictly inside it, so it
  stays (with freshness 0) and can still be the nearest HTF zone for the curve. "Smallest score for an HTF location
  zone" in Settings (0, as in scanner.py) can be raised to 5 to skip such zones.
- Where the distal line moves to a leg-in or leg-out wick the zone is wider: the stop loss is further away, the
  quantity for the same risk is smaller, and the target (2 times the risk) is further away too.
- Entry type 3 enters above the proximal line of a demand zone (below it for supply), so its risk is larger than the
  zone and its target is further away than entry type 1's. Entry type 2 enters inside the zone, so its risk is
  smaller than the zone and one wick to the distal line stops it.
- The course text for types 2 and 3 speaks of a first and a second candle. As built, any candle of the test that
  closes inside the zone can be the first one, so a type 3 entry can come several candles into a test.
- Not built: the stop loss with room beyond the distal line, the credibility filter and the trailing stop.
- **Fees take a large share on thin zones.** A zone 0.1% of price wide, entered and left with limit orders, pays
  0.04% in fees: a fifth of a 2 R target is gone. A zone 0.05% wide with a market entry and a stop pays more than
  half of 1 R on top of the loss. In the tests on generated data, one target hit was worth +0.57 R instead of +2 R. The
  backtest shows fees and funding in their own columns so you can see this. A minimum zone width is not built yet; tell
  me if you want one.
- A sudden move can run through the stop: the backtest exits at the open of the candle that gaps through it, which can
  lose more than 1 R. On a leveraged position that can also mean liquidation (see paper trading).

## HTF contact

For every setup, two zones are checked against the same-side zones of higher timeframes: the setup's own (LTF) zone,
against every timeframe above it, and its mode's HTF zone, against every timeframe above that.

| Result | Rule | What is shown |
| --- | --- | --- |
| Inside | The zone lies completely within the higher zone | The timeframe |
| Overlap | The two price ranges share some prices | The shared range |
| Touch | No overlap, and the gap is at most 0.1% of price | The price where the edges meet |
| Near | The gap is at most 1% of price | The gap in percent, above or below |

- Only active zones count, one entry per higher timeframe (its strongest contact), nearest timeframe first.
- Scanner: an "HTF contact" column, the full list for both zones in the row details, and two filters: the execution
  zone's timeframe, and contact with any timeframe or with one chosen timeframe.
- Chart: selecting a zone draws its mode's HTF zone and every contacted higher zone, dashed and labelled, with one
  switch per timeframe.
- The two limits are under Zone rules in Settings. The field is information only: it does not change the score.
- Contacts are worked out when a timeframe is scanned. Press Scan all once after upgrading to fill them in.

## Speed

Measured on this version with 50 coins, MariaDB, the every-minute job and the live feed running (a fake Binance on the
same machine, 2 CPU cores):

| Request | Typical | Slowest 5% |
| --- | --- | --- |
| Dashboard data (`/api/results`) | 0.02 s | 0.03 s |
| Chart: candles, zones and result in one request (`/api/chart`) | 0.05 s | 0.07 s |
| Live status (`/api/live/status`) | 0.01 s | 0.02 s |

What keeps it fast:

- **The web server never waits for background work.** Live price checks (alerts, paper fills) run in a worker thread
  once a second, and are skipped when there is nothing to check; a round still at work never holds up the next price
  push. Coin list, funding and open interest writes run in worker threads.
- **Scans run in separate worker processes** on MySQL (`SCAN_WORKERS` in `.env`; the CPU cores less one, at most 4), so
  several coins are scanned at once and a long scan never slows the pages. On SQLite scans run in a thread. One coin's
  full first scan takes about 12 seconds; later scans only read the candles that are new.
- **The every-minute job is light:** small downloads for 8 coins at a time, rescans only where a candle closed, no
  status or job rows written for a normal run (a failure is recorded as a job). The Data page and the dashboard footer
  show how long the last one took.
- **"Touched again" events are not stored.** Under the scanner.py marking a zone stays while price sits beyond it, and
  every candle there reaches the proximal line again. Earlier versions wrote one row per candle for that: millions of
  rows on 1 minute candles, which made the alert list and every scan slow. The zone's own test count keeps the number.
  On the first start of this version a background job ("tidy zone events") deletes the old rows in batches (6.3
  million in 51 seconds in the test) and then indexes the alert list.
- **Inserts use one prepared statement per table,** run with up to 2,000 rows at a time.
- **Counts are kept, not recounted:** candles stored and gaps per coin and timeframe are updated as candles arrive and
  counted again from scratch by the nightly job, so the Data page never counts the whole candle table.
- **The dashboard reads the results saved at each scan in one query**, and the BTC and ETH line from the same results.
- On a catch-up (the first scan of a coin), zones that were used up long ago are not stored at all. Violated and used-up
  zones are kept 3 days on 1 min, 10 on 5 min, 15 on 15 min, 30 on 30 min, 45 on 1 hr, 60 on 2 hr, 90 on 4 hr and 120
  on daily and above, then removed by the 00:20 job. Active zones are never removed.

## Coin list

- Automatic to begin with: the top 50 USDT perpetuals by 24 hour volume (setting "Top N"), checked again every day at
  00:10 UTC; coins that come into the top 50 are downloaded, coins that leave keep their candles and zones.
- The Dashboard, Chart and Backtest pages have a **Coins** choice: **Top 20 by volume** (the default), **Top 50 by
  volume**, or **My whole list**. All three read the coin list, most traded first; the choice is remembered in the browser.
- Settings page: find a coin by symbol; show the top N by volume and tick the ones you want ("Add selected" adds them,
  "Use only the selected" replaces the list); or paste symbols (BTC, ETHUSDT and SOL all work). Choosing coins by hand
  switches the automatic list off; "Keep my list as the top N automatically" switches it on again.
- Only coins with contract type PERPETUAL, quote asset USDT and status TRADING are offered. A coin that stops trading
  is kept with its history, marked as not trading.
- Each coin keeps Binance's filters: tick size, step size, minimum quantity and minimum order value, used by paper trading.
- Adding a coin starts its download and scan at once, in the background. Removing it keeps its candles.

## Paper trading rules

- An order is created from a zone's current plan (Paper trade on the dashboard), or automatically for every watched
  setup at or above a score you choose. One order or position per zone.
- Balance: in USDT, starting from 10,000 (setting). Closed profit and loss is added to it.
- Size: the quantity that loses your chosen share of the current balance (default 1%) at the stop loss, rounded down to
  the coin's step size. Refused when it is under the coin's minimum quantity or minimum order value.
- Leverage: set on the Paper page or in Settings (default 5x), or per order through the API. The margin set aside is
  the position value divided by the leverage.
- Liquidation: an estimate for isolated margin, entry x (1 - 1/leverage + maintenance margin rate) for a long and
  entry x (1 + 1/leverage - maintenance margin rate) for a short, with the rate a setting (0.5%). Binance's real rate
  depends on the position size and its tiers need a signed API call, so this is an estimate. **An order whose estimated
  liquidation price comes before its stop loss is refused.**
- Fill, entry type 1: when the live mark price reaches the entry, at the entry price, paying the maker fee.
- Fill, entry types 2 and 3: when the scan records the entry, at the recorded price and candle time, paying the taker
  fee. The quantity is worked out again from the real entry. Price reaching the proximal line does not fill these orders.
- A pending order follows its plan if the plan changes, and is cancelled if the zone is violated or used up.
- Exit: stop at the live price seen when it is reached (taker fee), target at the target price (maker fee), or Close
  now at the live price less the slippage setting (taker fee).
- Funding: paid or received at each funding time the position is open over, from the stored funding history, at the
  coin's own interval. The open position shows it as it builds up.
- Profit and loss is after fees and funding; the result in R is that divided by the planned risk.
- Safety net: after every candle close the stored 1 minute candles (5 minute for a coin without them) are replayed
  against each order and position with the backtest's rules, so a fill or stop between two price updates is not missed.

- Enter now (Paper page, pending orders): enters the order at once at the live mark price, as a market order (taker
  fee and the slippage setting), with the order's own stop and target. The size is worked out again from that price,
  so the risk stays at your share of the balance. A confirm box shows the price, size, risk and reward-to-risk first.
  Refused when price is already past the stop or the target, when the size is under the coin's minimum, or when the
  liquidation would come before the stop; under 1 to 1 it warns and enters only if you confirm. The trade is marked
  "Entered by you at market" (entry_type MANUAL in the CSV).
- Chart (Paper page, every pending order, open position and closed trade): a chart panel on the page with the candles
  of the trade's timeframe, the zone, entry, stop, target (and liquidation while open), the signal, entry and exit
  times, the live price while open or pending, and a detail box: times, prices, reward to risk, size, fees, funding,
  result, the zone score with its breakdown, and any stop or target changes. "Open full chart" opens the Chart page on
  that coin, timeframe and mode with the zone selected.

## Costs

Binance USDT-M futures, regular account (checked when this was built; change them in Settings if your tier differs):

| | Rate | Used for |
| --- | --- | --- |
| Maker | 0.02% | A limit order resting in the book: entry type 1, an exit at the target |
| Taker | 0.05% | An order that takes the price: entry types 2 and 3, a stop loss, a manual close, a trade valued at the end of the data |
| BNB | 10% off both | When "Fees paid in BNB" is ticked |

Funding is taken from the stored settlements of each coin, so a coin that settles every 4 hours pays every 4 hours.
The backtest, the scan-date check and paper trading all use the same cost code (app/sim/costs.py).

## Telegram

Create a bot with BotFather in Telegram and enter its token and your chat id on the Settings page (or set
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`). Send test alert confirms it works. Zone alerts and paper
fills are then sent to that chat. The token is stored in the database and never returned by any API.

## Backtest: the scan-date check

The Backtest page has one form and one date. You pick coins, a mode and a scan date; there is no start or end date.

- **Coins:** every ready coin is ticked to begin with. The "All coins" box ticks or clears them all, and shows a
  partial mark when only some are ticked.
- **As of the scan date:** the engine is run on the candles up to that date only, so the nearest HTF zones, the
  curve, the ITF trend, the LTF entry zone and its score are what the rules would have said that day.
- **After the scan date:** the entry type the trade score takes is followed through the later LTF candles with the
  usual fill rules.

| Score on the scan date | Followed | Shown as "not taken" |
| --- | --- | --- |
| 7 or more | Entry type 1: the first later candle that reaches the proximal line | Entry types 2 and 3 |
| 5 to 6.5 | Entry types 2 and 3, both, each from its own candle | Entry type 1 |
| Below 5 | Nothing: no trade on that date | All three |

- The entry has to come on the zone's next test (or the one going on at the scan date). After that test the zone is
  no longer what was scored, so the entry is "never entered".
- A type 2 entry is checked for its exit from the entry candle itself, because it enters at the open. A type 1 limit
  that is filled and stopped in the same candle counts as a loss.
- Each entry ends as target hit, stopped, still open at the last stored candle, never entered (with the reason), or not taken.
- The report shows totals per entry type and one row per coin, with a "Chart and replay" link that opens the chart on that scan date.
- **Past checks:** every check is saved and listed under the form, newest first, with its totals. Click one to open its
  report again exactly as it was; Delete removes it. They are kept in the backtest runs table.
- Results are in R after fees and funding; the totals show how much of them the fees and the funding were. The fee
  rates and the funding switch are on the form (they start from the settings) and are saved with each check.
- The scan date is a UTC day: the last LTF candle that closed by 24:00 UTC of that date.
- One date is one sample. It shows how the rules read a coin on a day you choose, not a win rate.

## Chart: live price, scan date and replay

- **Live:** the candle that is still forming is drawn faintly and follows the live mark price every second, and the
  price axis shows the live price. The forming candle comes from Binance once (one request of weight 1) and is only
  drawn: it is never stored and never used to find or test a zone. When a scan brings new closed candles for the coin,
  the chart refreshes by itself, keeping the zone you selected and the part of the chart you are looking at.
  Quarter, half year and year have no forming candle (Binance has no such intervals).
- The candles, zones and result arrive in one request (`/api/chart`).

- Choosing a scan date above the chart shows the result as of that date, draws a vertical line there (switch:
  "Scan date line") and marks each entry type's entry and exit. "Back to today" returns to the live result.
- **Replay starts from the scan date and does not redraw the picture.** The nearest HTF demand and supply, the curve,
  the LTF zone, the entry, the stop loss and the target stay as they were on the scan date, and only the items ticked
  under "Show on the chart" are drawn. Unticking one during replay hides it at once.
- Candles after the scan date appear one at a time. When the replay reaches them, each entry is marked with an
  arrow, its target with a green marker and its stop loss with a red marker, and the event list says what happened
  and the result in R. At the end, entries that never happened are listed with the reason.
- Play, pause, step, speed and "Jump to next event" (the next entry or exit) work as before.
- With "All zones" ticked before starting, the older replay runs instead: the engine recalculates every zone of that
  one timeframe at each bar.

The date-range backtest (From and To) is no longer on the page. Its API and its saved runs are still in the database.

- Times on the chart are in the time zone set under Settings. The chart library has no time zones, so each candle's
  UTC time is shifted by that zone's offset before it is drawn.
- Prices on the axis keep as many decimals as the coin needs.

## How the backtest works

- The same engine and fill rules as live, on stored candles. No Binance calls.
- Each chosen mode is run with its own HTF, ITF and LTF. A candle on a higher timeframe is used only after it has
  closed; a test checks that cutting the data off earlier leaves every earlier trade unchanged.
- Results are in R: 1 R is the distance from entry to stop. Fees (maker or taker by order type) and funding are taken
  off every trade and shown in their own columns (fees_r, funding_r).
- Entry type 1: the limit exists only while the zone scores 7 or more and the mode allows the trade at the close of
  the previous candle; the candle that reaches the proximal line fills it. Entry types 2 and 3: the zone must score
  5 to 6.5 at the close of the previous candle, and the trade opens on the candle that completes the entry.
- A zone is traded once per run. `entry_plan` can be `rule` (the score decides, the default), `type1`, `type2` or `type3`.
- Entry and stop reached in the same candle counts as a full loss. Stop and target reached in the same later candle
  counts as the stop. A gap through the stop exits at the open.
- Trades still open at the end of the data are valued at the last close and kept out of the statistics.
- Breakdowns by zone type, mode, curve at entry and score show where the results come from.

## Tests

```
python -m pytest tests -q
```

153 tests. They run on a throwaway SQLite file and never touch your database, even with `DB_` values in `.env`.
To run them on MySQL, use an empty throwaway database (it is wiped at the start of the run):

```
TEST_DATABASE_URL='mysql+pymysql://user:pass@localhost:3306/crypto_scanner_test' python -m pytest tests -q
```

In this URL, special characters in the password must be encoded (`@` becomes `%40`).

- `tests/test_scanner_rules.py` runs scanner.py v6.4's own zone functions (`tests/reference_scanner_v64.py`, copied
  unchanged) next to the app's engine on random candles and checks that both find the same zones, lines, tests and
  scores. The engine is the stock scanner's, unchanged apart from price rounding and the ITF threshold.
- The sync, scan, live, paper and backtest tests use a fake Binance client (`tests/fake_binance.py`) with generated
  candles that are fixed by their open time. That data exists only in the tests; the application contains no made-up data.
- `tests/test_binance_client.py` checks the weight limit read from exchangeInfo, the weight header, the wait on 429 and
  the stop on 418 against a mock transport.

The suite passes on SQLite. It has not been run on MySQL in this version; nothing in the code needs MySQL 8 (JSON is
stored as text, no window functions), but tell me if MySQL reports an error.

## Not yet verified against live Binance

The environment this was built in cannot reach Binance, so these have not been run against the real API:

- The REST answers (klines, exchangeInfo, fundingInfo, 24 hour tickers, premiumIndex, fundingRate,
  openInterestHist). They follow Binance's documented formats and the official connector.
- The WebSocket stream `!markPrice@arr@1s` and its message format.
- Sending to Telegram (the message format and the failure handling are tested; the real send is not).

Check during your first run: that the Data page shows candles up to the last closed one, that the weight figure moves
and stays under the budget, and that the badge at the top says "Live prices".

## Security

The app has no login of its own. Keep it bound to `127.0.0.1` as shown, or put it behind your own protection
before exposing it on a network: anyone who can open the pages can change settings and see your setups.
Keep `.env` out of version control.

## Layout

```
app/config.py            database selection, timeframes, GTF modes, default rules and costs
app/db.py                engine, sessions, database creation, in-place schema upgrade
app/models.py            all tables, including funding rates and open interest
app/binance/client.py    Binance USDT-M REST client: weight limit, 429 and 418 handling, klines, funding, open interest
app/data/sync.py         candle download (incremental, gaps, keep windows), all in UTC
app/data/resample.py     candle boundaries, quarter, half year and year from months
app/data/market.py       coin list and filters, top N by volume, funding and open interest storage
app/engine/detect.py     candle types and zone detection
app/engine/lifecycle.py  tests, freshness, violated, used up, and the entries of types 2 and 3
app/engine/score.py      GTF score and zone type
app/engine/core.py       the engine, free of database code; scan, replay and backtest all drive it
app/engine/gtf.py        nearest HTF zones, curve, trends, direction, execution zones, entry plans
app/engine/window.py     history windows per timeframe
app/engine/contact.py    HTF contact: inside, overlap, touch, near
app/engine/confirm.py    confirmation score: mode and BTC agreement, contact, volume, strength vs BTC, funding, open interest
app/engine/result.py     the full result of one coin in one mode, saved at each scan
app/engine/plan.py       plans per zone and mode, from stored data
app/engine/scanner.py    scan service: drives the engine and saves the results
app/sim/                 fills (shared fill rules), costs (fees and funding), backtest, scan-date check, replay, paper
app/status.py            where each coin stands: waiting, downloading, scanning, ready, failed
app/alerts/telegram.py   Telegram sender and message text
app/live.py              Binance WebSocket mark prices, REST fallback, alerts, paper fills, the page WebSocket hub
app/jobs/                scheduler (the jobs, in UTC, every day), the scan worker processes and the job runner
app/web/                 six pages; vendored Lightweight Charts 5.2.1 (Apache 2.0)
app/main.py              48 endpoints, two WebSockets and the pages
tests/                   153 tests, the fake Binance client and scanner.py v6.4's zone functions
```
