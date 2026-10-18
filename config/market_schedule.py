# Trading Schedule: Monday-Friday, IST timezone
# Weekends (Sat/Sun) are automatically skipped by is_trading_day() check in bot.py
# All trades use 5-minute candles only ("timeframe": "5m")
from datetime import datetime
import pytz

# India timezone
IST = pytz.timezone('Asia/Kolkata')

# Candles used everywhere: 5-minute charts (Olymp websocket size = 300 seconds)
TIMEFRAME = "5m"

MARKET_SCHEDULE = [
    {
        'name': 'EURUSD Morning',
        'symbol': 'EURUSD_OTC',   # Olymp requires _OTC suffix for forex digital options
        'start_time': '09:00',
        'end_time': '11:00',
        'timeframe': '5m',              # 5-min candles only — no 1m/15m anywhere
        'min_confidence': 0.65,
        'description': 'European open'
    },
    {
        'name': 'GBPUSD Late Morning',
        'symbol': 'GBPUSD_OTC',
        'start_time': '11:00',
        'end_time': '14:00',
        'timeframe': '5m',
        'min_confidence': 0.65,
        'description': 'UK market active'
    },
    {
        'name': 'USDJPY Afternoon',
        'symbol': 'USDJPY_OTC',
        'start_time': '14:00',
        'end_time': '17:00',
        'timeframe': '5m',
        'min_confidence': 0.65,
        'description': 'Tokyo/London overlap'
    },
    {
        'name': 'AUDUSD Evening',
        'symbol': 'AUDUSD_OTC',
        'start_time': '17:00',
        'end_time': '21:00',
        'timeframe': '5m',
        'min_confidence': 0.65,
        'description': 'Asian session'
    }
]

def is_trading_day():
    """
    Single source of truth for the weekday filter (Mon-Fri only).
    Saturday (5) and Sunday (6) are skipped — low liquidity and noise.
    bot.py imports this so bot logic and the schedule never disagree.
    """
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
