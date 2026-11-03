# Trading Schedule: Monday-Friday, IST timezone
# Weekends (Sat/Sun) are automatically skipped by is_trading_day() check in bot.py
# All trades use 2-minute candles only ("timeframe": "2m")
from datetime import datetime
import pytz

# India timezone
IST = pytz.timezone('Asia/Kolkata')

# Candles used everywhere: 2-minute charts (Olymp websocket size = 120 seconds)
TIMEFRAME = "2m"

MARKET_SCHEDULE = [
    {
        'name': 'EURUSD Morning',
        'symbol': 'EURUSD_OTC',   # Olymp requires _OTC suffix for forex digital options
        'start_time': '09:00',
        'end_time': '11:00',
        'timeframe': '2m',              # 2-min candles only
        'min_confidence': 0.65,
        'description': 'European open'
    },
    {
        'name': 'GBPUSD Late Morning',
        'symbol': 'GBPUSD_OTC',
        'start_time': '11:00',
        'end_time': '14:00',
        'timeframe': '2m',
        'min_confidence': 0.65,
        'description': 'UK market active'
    },
    {
        'name': 'USDJPY Afternoon',
        'symbol': 'USDJPY_OTC',
        'start_time': '14:00',
        'end_time': '17:00',
        'timeframe': '2m',
        'min_confidence': 0.65,
        'description': 'Tokyo/London overlap'
    },
    {
        'name': 'AUDUSD Evening',
        'symbol': 'AUDUSD_OTC',
        'start_time': '17:00',
        'end_time': '21:00',
        'timeframe': '2m',
        'min_confidence': 0.65,
        'description': 'Asian session'
    }
]

import os

def is_trading_day():
    """
    Checks if today is a valid trading day.
    If ALLOW_WEEKEND_TRADING is True (default for OTC markets), trading is active all 7 days.
    If False, skips Saturday and Sunday.
    """
    val = os.getenv("ALLOW_WEEKEND_TRADING")
    if val is None:
        try:
            from database.db import get_setting
            val = get_setting("ALLOW_WEEKEND_TRADING")
        except Exception:
            pass
    if val is not None:
        allow_weekend = str(val).strip().lower() in ("1", "true", "yes", "on")
    else:
        allow_weekend = True  # Default to True for OTC 24/7 markets
    if allow_weekend:
        return True
    return datetime.now(IST).weekday() not in (5, 6)

def get_current_market():
    """Get which market should trade now (Saturday/Sunday still return a
    scheduled window, but bot.py pauses the whole cycle via is_trading_day)."""
    now = datetime.now(IST)
    current_time = now.strftime('%H:%M')

    for market in MARKET_SCHEDULE:
        if market['start_time'] <= current_time < market['end_time']:
            return market

    return None

def get_next_market_switch():
    """Get time until next market switch"""
    now = datetime.now(IST)
    current_time = now.strftime('%H:%M')

    for market in MARKET_SCHEDULE:
        if current_time < market['end_time']:
            return market['end_time']

    return MARKET_SCHEDULE[0]['start_time']
