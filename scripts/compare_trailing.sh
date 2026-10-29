#!/usr/bin/env bash
# Runs the same backtest four times, with the trailing stop off, step, swing and ATR, and zips the four CSV files.
# The settings are not changed: each run names its own trailing method. Run it from the crypto_zone_scanner folder
# while the app is running. Each run takes about as long as one normal backtest (20 to 40 minutes with 50 coins).
#   bash scripts/compare_trailing.sh            (defaults below)
#   TF=itf bash scripts/compare_trailing.sh     (move the stop on the ITF instead of the LTF)
set -u
URL=${URL:-http://127.0.0.1:8001}
START=${START:-2026-07-25}
END=${END:-2026-10-15}
MODES=${MODES:-'["2HR","4HR","DAILY"]'}
SYMBOLS=${SYMBOLS:-'"all"'}
TF=${TF:-ltf}
OUT=trailing_$(date +%Y%m%d_%H%M)
mkdir -p "$OUT"
for METHOD in off step swing atr; do
  RUN=$(curl -s -X POST "$URL/api/backtest/run" -H "Content-Type: application/json" \
    -d "{\"symbols\": $SYMBOLS, \"modes\": $MODES, \"start\": \"$START\", \"end\": \"$END\", \"entry_plan\": \"type1\", \"trail_method\": \"$METHOD\", \"trail_tf\": \"$TF\"}" \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('run_id') or d)")
  echo "$METHOD: backtest run $RUN started"
  while true; do
    S=$(curl -s "$URL/api/backtest/$RUN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['status'], '|', d['progress'])")
    echo "  $(date +%H:%M) $S"
    case "$S" in running*) sleep 60;; *) break;; esac
  done
  curl -s "$URL/api/backtest/$RUN/export" -o "$OUT/backtest_${METHOD}_${TF}_run$RUN.csv"
done
ls -la "$OUT"
zip -qr "$OUT.zip" "$OUT" && echo "send me: $OUT.zip"
