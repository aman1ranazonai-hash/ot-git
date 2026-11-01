import modal
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import pandas as pd
from datetime import datetime


class PredictionRequest(BaseModel):
    candles: list


class PredictionResponse(BaseModel):
    prediction: str
    up_probability: float
    down_probability: float
    confidence: float
    timestamp: str


app = modal.App('kronos-trading-api')

image = modal.Image.debian_slim(python_version='3.11').run_commands(
    'apt-get update && apt-get install -y git',
    'git clone https://github.com/shiyu-coder/Kronos.git /kronos',
    'pip install torch==2.1.0 pandas==2.0.3 numpy==1.24.3 '
    'safetensors==0.6.2 fastapi==0.104.0 uvicorn==0.24.0 einops==0.8.1 '
    'huggingface_hub==0.33.1 tqdm==4.67.1'
)

# Cache Hugging Face model weights across cold starts
hf_cache = modal.Volume.from_name('kronos-hf-cache', create_if_missing=True)

# Load the model once per container, reuse across requests
_predictor = None


def _get_predictor():
    global _predictor
    if _predictor is None:
        # Import from /kronos (cloned from GitHub; repo root is the package root)
        import sys
        if '/kronos' not in sys.path:
            sys.path.insert(0, '/kronos')
        from model import Kronos, KronosTokenizer, KronosPredictor

        print('Loading tokenizer...')
        tokenizer = KronosTokenizer.from_pretrained('NeoQuasar/Kronos-Tokenizer-base')

        print('Loading model...')
        model = Kronos.from_pretrained('NeoQuasar/Kronos-base')

        print('Creating predictor...')
        _predictor = KronosPredictor(
            model,
            tokenizer,
            device='cuda:0',
            max_context=512
        )
    return _predictor


@app.function(
    image=image,
    gpu=['T4', 'L4', 'A10G'],
    timeout=600,
    memory=8192,
    scaledown_window=300,
    volumes={'/root/.cache/huggingface': hf_cache},
)
def predict_kronos(candles_list: list):
    '''
    Load Kronos from GitHub clone and predict.
    Input: [[open, high, low, close, volume], ...] or [{"open":..., "high":...}]
    Output: {up_probability, down_probability, confidence, signal}
    '''
    try:
        predictor = _get_predictor()

        print('Converting candles to DataFrame...')
        if not candles_list:
            raise ValueError('Empty candles_list provided')

        if isinstance(candles_list[0], dict):
            rows = []
            for c in candles_list:
                o = float(c.get('open', c.get('o', 0.0)))
                h = float(c.get('high', c.get('h', o)))
                l = float(c.get('low', c.get('l', o)))
                cl = float(c.get('close', c.get('c', o)))
                v = float(c.get('volume', c.get('vol', c.get('v', 0.0))))
                rows.append([o, h, l, cl, v])
            df = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close', 'volume'])
        else:
            df = pd.DataFrame(
                candles_list,
                columns=['open', 'high', 'low', 'close', 'volume']
            )
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)

        # Kronos max_context is 512, ensure we do not exceed 500 candles
        if len(df) > 500:
            df = df.iloc[-500:].reset_index(drop=True)

        # Kronos predicts future OHLCV candles, so timestamps are required.
        pred_len = 1
        now = pd.Timestamp.now()
        x_timestamp = pd.Series(pd.date_range(end=now, periods=len(df), freq='1min'))
        y_timestamp = pd.Series(pd.date_range(
            start=now + pd.Timedelta(minutes=1),
            periods=pred_len,
            freq='1min'
        ))

        print('Making prediction...')
        pred_df = predictor.predict(
            df,
            x_timestamp,
            y_timestamp,
            pred_len=pred_len,
            sample_count=5,
            verbose=False
        )

        # Derive up/down probabilities from the predicted next close
        last_close = float(df['close'].iloc[-1])
        next_close = float(pred_df['close'].iloc[0])
        rel_move = (next_close - last_close) / last_close if last_close != 0 else 0.0

        up_prob = 1.0 / (1.0 + 2.0 ** (-rel_move * 200))
        down_prob = 1.0 - up_prob
        conf = max(up_prob, down_prob)

        # Determine signal (trade every prediction by dominant direction)
        if up_prob >= down_prob:
            signal = 'BUY'
        else:
            signal = 'SELL'

        return {
            'up_probability': float(up_prob),
            'down_probability': float(down_prob),
            'confidence': float(conf),
            'signal': signal
        }

    except Exception as e:
        raise Exception(f'Kronos prediction failed: {str(e)}')


@app.function(
    image=image,
    timeout=60,
)
def fetch_and_predict_kronos(symbol: str, interval: str, limit: int):
    '''
    Fetch real OHLCV candles from Binance server-side, then run Kronos via remote GPU function.
    '''
    import requests as _requests

    endpoints = [
        'https://data-api.binance.vision/api/v3/klines',
        'https://api.binance.com/api/v3/klines',
        'https://api.binance.us/api/v3/klines',
    ]

    try:
        params = {'symbol': symbol.upper(), 'interval': interval, 'limit': int(limit)}
        klines = None
        errors = []
        for url in endpoints:
            try:
                resp = _requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
                klines = resp.json()
                break
            except Exception as endpoint_error:
                errors.append(f'{url}: {endpoint_error}')

        if not klines:
            raise ValueError('All Binance endpoints failed -> ' + ' | '.join(errors))

        candles_list = [
            [float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
            for k in klines
        ]

        return predict_kronos.remote(candles_list)

    except Exception as e:
        raise Exception(f'Live prediction failed: {str(e)}')


@app.function(
    image=image,
    timeout=600,
    memory=1024,
)
@modal.asgi_app()
def fastapi_app():
    web_app = FastAPI(title='Kronos Trading API')

    @web_app.get('/health')
    def health():
        return {
            'status': 'healthy',
            'service': 'Kronos Trading API',
            'timestamp': datetime.now().isoformat()
        }

    @web_app.post('/predict', response_model=PredictionResponse)
    def predict(request: PredictionRequest):
        try:
            result = predict_kronos.remote(request.candles)

            return PredictionResponse(
                prediction=result['signal'],
                up_probability=result['up_probability'],
                down_probability=result['down_probability'],
                confidence=result['confidence'],
                timestamp=datetime.now().isoformat()
            )

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @web_app.get('/predict_live', response_model=PredictionResponse)
    def predict_live(symbol: str = 'BTCUSDT', interval: str = '1m', limit: int = 100):
        try:
            result = fetch_and_predict_kronos.remote(symbol, interval, limit)

            return PredictionResponse(
                prediction=result['signal'],
                up_probability=result['up_probability'],
                down_probability=result['down_probability'],
                confidence=result['confidence'],
                timestamp=datetime.now().isoformat()
            )

        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return web_app
