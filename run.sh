#!/bin/bash
# run.sh — Starts BTC Terminal perpetually with auto-restart on crash.
#
# Usage:
#   chmod +x run.sh
#   ./run.sh
#
# Both processes restart automatically if they crash.
# Logs saved to btc.log and bot.log

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv/bin/activate"

# Load .env
if [ -f "$DIR/.env" ]; then
  export $(grep -v '^#' "$DIR/.env" | xargs)
fi

cd "$DIR"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  BTC Terminal — starting all processes"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Logs: btc.log | bot.log"
echo "  Stop: Ctrl+C"
echo ""

# Trap Ctrl+C to kill both child processes
trap 'echo ""; echo "Stopping..."; kill $PID_BTC $PID_BOT 2>/dev/null; exit 0' INT TERM

run_btc() {
  while true; do
    echo "[$(date '+%H:%M:%S')] BTC.py starting..."
    source "$VENV" && python "$DIR/BTC.py" >> "$DIR/btc.log" 2>&1
    echo "[$(date '+%H:%M:%S')] BTC.py crashed — restarting in 5s..."
    sleep 5
  done
}

run_bot() {
  while true; do
    echo "[$(date '+%H:%M:%S')] telegram_bot.py starting..."
    source "$VENV" && python "$DIR/telegram_bot.py" >> "$DIR/bot.log" 2>&1
    echo "[$(date '+%H:%M:%S')] telegram_bot.py crashed — restarting in 5s..."
    sleep 5
  done
}

# Start both in background
run_btc &
PID_BTC=$!

sleep 2  # Stagger startup

run_bot &
PID_BOT=$!

echo "  BTC.py PID:          $PID_BTC"
echo "  telegram_bot.py PID: $PID_BOT"
echo ""

# Tail both logs to terminal
tail -f "$DIR/btc.log" "$DIR/bot.log" &
PID_TAIL=$!

# Wait for both processes
wait $PID_BTC $PID_BOT
kill $PID_TAIL 2>/dev/null
