import requests
import sys
from datetime import datetime

import market_data

# Sample candles (10 OHLCV candles)
TEST_CANDLES = [
    [100, 102, 99, 101, 1000],
    [101, 103, 100, 102, 1100],
    [102, 104, 101, 103, 1200],
    [103, 105, 102, 104, 1300],
    [104, 106, 103, 105, 1400],
    [105, 107, 104, 106, 1500],
    [106, 108, 105, 107, 1600],
    [107, 109, 106, 108, 1700],
    [108, 110, 107, 109, 1800],
    [109, 111, 108, 110, 1900],
]

def test_health(api_url):
    print('\n' + '='*60)
    print('TEST 1: /health endpoint')
    print('='*60)

    try:
        response = requests.get(f'{api_url}/health', timeout=10)

        if response.status_code == 200:
            data = response.json()
            print('✅ PASS - API is healthy')
            print(f'   Status: {data.get("status")}')
            print(f'   Timestamp: {data.get("timestamp")}')
            return True
        else:
            print(f'❌ FAIL - Status code: {response.status_code}')
            return False

    except Exception as e:
        print(f'❌ FAIL - {str(e)}')
        return False

def test_predict(api_url):
    print('\n' + '='*60)
    print('TEST 2: /predict endpoint')
    print('='*60)

    try:
        # Prefer real market candles; fall back to synthetic ones if Binance fails
        candles = TEST_CANDLES
        try:
            candles = market_data.fetch_candles('BTCUSDT', '1m')
            print(f'Using {len(candles)} REAL BTCUSDT 1m candles from Binance')
        except Exception as e:
            print(f'⚠️  Could not fetch real candles ({e}) - using synthetic ones')

        print(f'Sending {len(candles)} candles to Kronos...')

        response = requests.post(
            f'{api_url}/predict',
            json={'candles': candles},
            timeout=600  # cold start: model download + GPU load can take minutes
        )

        if response.status_code == 200:
            data = response.json()

            print('✅ PASS - Prediction received')
            print(f'   Prediction: {data.get("prediction")}')
            print(f'   UP: {data.get("up_probability"):.4f}')
            print(f'   DOWN: {data.get("down_probability"):.4f}')
            print(f'   Confidence: {data.get("confidence"):.4f}')
            print(f'   Timestamp: {data.get("timestamp")}')

            # Validate
            assert data.get('prediction') in ['BUY', 'SELL', 'HOLD'], 'Invalid signal'
            assert 0 <= data.get('up_probability') <= 1, 'Invalid UP probability'
            assert 0 <= data.get('down_probability') <= 1, 'Invalid DOWN probability'
            assert 0 <= data.get('confidence') <= 1, 'Invalid confidence'

            print('\n✅ All validations passed')
            return True

        else:
            print(f'❌ FAIL - Status code: {response.status_code}')
            print(f'   Response: {response.text}')
            return False

    except requests.Timeout:
        print('❌ FAIL - Timeout (Kronos loading?)')
        return False

    except Exception as e:
        print(f'❌ FAIL - {str(e)}')
        return False

def test_predict_live(api_url):
    print('\n' + '='*60)
    print('TEST 3: /predict_live endpoint (server-side real data)')
    print('='*60)

    try:
        print('Asking API to fetch real BTCUSDT candles and predict...')

        response = requests.get(
            f'{api_url}/predict_live',
            params={'symbol': 'BTCUSDT', 'interval': '1m', 'limit': market_data.DEFAULT_CANDLE_COUNT},
            timeout=600  # cold start: model download + GPU load can take minutes
        )

        if response.status_code == 200:
            data = response.json()

            print('✅ PASS - Live prediction received')
            print(f'   Prediction: {data.get("prediction")}')
            print(f'   UP: {data.get("up_probability"):.4f}')
            print(f'   DOWN: {data.get("down_probability"):.4f}')
            print(f'   Confidence: {data.get("confidence"):.4f}')
            print(f'   Timestamp: {data.get("timestamp")}')

            # Validate
            assert data.get('prediction') in ['BUY', 'SELL', 'HOLD'], 'Invalid signal'
            assert 0 <= data.get('up_probability') <= 1, 'Invalid UP probability'
            assert 0 <= data.get('down_probability') <= 1, 'Invalid DOWN probability'
            assert 0 <= data.get('confidence') <= 1, 'Invalid confidence'

            print('\n✅ All validations passed')
            return True

        else:
            print(f'❌ FAIL - Status code: {response.status_code}')
            print(f'   Response: {response.text}')
            return False

    except requests.Timeout:
        print('❌ FAIL - Timeout (Kronos loading?)')
        return False

    except Exception as e:
        print(f'❌ FAIL - {str(e)}')
        return False

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('USAGE: python test_api.py <MODAL_URL>')
        print('Example: python test_api.py https://your-name--kronos-trading-api.modal.run')
        sys.exit(1)

    api_url = sys.argv[1]

    print('\n' + '#'*60)
    print('# KRONOS MODAL API TEST')
    print('#'*60)
    print(f'Testing: {api_url}\n')

    health_pass = test_health(api_url)
    predict_pass = test_predict(api_url)
    live_pass = test_predict_live(api_url)

    print('\n' + '='*60)
    print('SUMMARY')
    print('='*60)
    health_status = '✅ PASS' if health_pass else '❌ FAIL'
    predict_status = '✅ PASS' if predict_pass else '❌ FAIL'
    live_status = '✅ PASS' if live_pass else '❌ FAIL'
    print(f'Health:       {health_status}')
    print(f'Predict:      {predict_status}')
    print(f'Predict-live: {live_status}')

    if health_pass and predict_pass and live_pass:
        print('\n✅ API IS WORKING!')
    else:
        print('\n❌ API HAS ISSUES')
