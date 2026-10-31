#!/usr/bin/env bash
set -e

echo "=================================================="
echo "Starting Kronos System on Railway"
echo "=================================================="

# Check if this service should run all components or just a specific role
if [ "$SERVICE_TYPE" = "bot" ]; then
    echo "Running in dedicated Bot Worker mode..."
    exec python3 bot.py
elif [ "$SERVICE_TYPE" = "telegram" ]; then
    echo "Running in dedicated Telegram Bot mode..."
    exec python3 telegram_bot.py
elif [ "$RUN_ALL_IN_ONE" = "false" ]; then
    echo "Running in Web-only mode..."
    exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120 dashboard.app:app
else
    echo "Starting background services (All-in-One mode)..."

    # Background loop for bot.py with auto-restart on exit
    (
        while true; do
            echo "[$(date -u +%T)] Starting Kronos Trading Bot (bot.py)..."
            python3 bot.py || true
            echo "[$(date -u +%T)] bot.py exited. Restarting in 5s..."
            sleep 5
        done
    ) &
    BOT_PID=$!

    # Background loop for telegram_bot.py with auto-restart on exit
    (
        while true; do
            echo "[$(date -u +%T)] Starting Kronos Telegram Bot (telegram_bot.py)..."
            python3 telegram_bot.py || true
            echo "[$(date -u +%T)] telegram_bot.py exited. Restarting in 5s..."
            sleep 5
        done
    ) &
    TG_PID=$!

    # Trap exit signals to gracefully stop background processes
    trap "kill $BOT_PID $TG_PID 2>/dev/null || true; exit 0" SIGINT SIGTERM EXIT

    echo "Starting Gunicorn Web Dashboard on port ${PORT:-5000}..."
    exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120 dashboard.app:app
fi
