"""Fetch live OHLCV candles from the Binance public REST API.

Used by test_api.py (and usable anywhere else) to pull real market data:
    candles = market_data.fetch_candles('BTCUSDT', '1m')
Each candle is [open, high, low, close, volume] as floats, oldest first.
"""

import requests

# How many candles to fetch when no explicit limit is given
DEFAULT_CANDLE_COUNT = 100

BINANCE_KLINES_URL = 'https://api.binance.com/api/v3/klines'

VALID_INTERVALS = {
    '1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h',
    '1d', '3d', '1w', '1M'
}

# Binance caps klines per request at 1000
MAX_LIMIT = 1000


def fetch_candles(symbol: str = 'BTCUSDT', interval: str = '1m', limit: int = DEFAULT_CANDLE_COUNT) -> list:
    """
    Fetch OHLCV candles from Binance.

    Returns a list of [open, high, low, close, volume] float lists,
    ordered oldest -> newest.

    Raises ValueError on bad arguments and requests.HTTPError on API errors.
    """
    if interval not in VALID_INTERVALS:
        raise ValueError(f'Invalid interval {interval!r}. Valid: {sorted(VALID_INTERVALS)}')

    limit = int(limit)
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f'limit must be between 1 and {MAX_LIMIT}, got {limit}')

    response = requests.get(
        BINANCE_KLINES_URL,
        params={'symbol': symbol.upper(), 'interval': interval, 'limit': limit},
        timeout=15,
    )
    response.raise_for_status()
    klines = response.json()

    # Each kline: [open_time, open, high, low, close, volume, close_time, ...]
    candles = [
        [float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
        for k in klines
    ]

    if not candles:
        raise ValueError(f'No candles returned for {symbol} {interval}')

    return candles


if __name__ == '__main__':
    data = fetch_candles('BTCUSDT', '1m', 5)
    for c in data:
        print(c)
