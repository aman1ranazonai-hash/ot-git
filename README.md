# Kronos AI Trading Bot (Production Ready)

An autonomous, multi-market algorithmic trading system for **Olymp Trade Fixed Time Trading**. The system uses a dedicated **Kronos foundation time-series AI model** (hosted on Modal) for directional decisioning (`UP`/`DOWN`). When confidence exceeds the production threshold (**> 70%**), the bot automatically executes a trade via Olymp Trade WebSocket and synchronizes state, trades, and logs to a shared **Supabase** cloud database.

The system includes:
- **Autonomous Trading Engine (`bot.py`)**: Follows active market hours (IST schedule), fetches live candles, queries Kronos API, executes orders, and pauses safely on losses.
- **Telegram Remote Controller (`telegram_bot.py`)**: Real-time notifications, status monitoring, and PIN-secured controls.
- **Real-Time Web Dashboard (`dashboard/app.py`)**: Visualizes live market status, P&L, recent trades, and audit logs.
- **REST Control API (`server.py`)**: FastAPI service for remote health checks and bot state manipulation.

---

## Decision & Execution Flow

Every **5 minutes** during active market sessions:

1. **Market Verification**: Confirms current time falls within active IST market windows (European EURUSD/GBPUSD, US USDJPY, or Asian AUDUSD).
2. **Live Candle Ingestion**: Fetches 400 real OHLCV candles directly from the Olymp Trade WebSocket client. (No synthetic/dummy candle fallbacks — cycles skip cleanly if live data is unavailable).
3. **Kronos Inference**: Sends recent candle data to the Kronos Modal API endpoint (`https://aman1-ranazonai--kronos-trading-api-fastapi-app.modal.run`).
4. **Confidence Evaluation**:
   - **Confidence > 0.70**: Checks risk controls, determines trade size from Olymp Trade settings (defaulting to 70), executes the order on Olymp Trade, and records the open trade in Supabase.
   - **Confidence ≤ 0.70**: Skips execution and logs prediction event to Supabase.
5. **Trade Resolution**: Tracks trade outcome. If a loss occurs, the bot safely pauses and awaits operator confirmation via Telegram or Dashboard.

---

## Cloud Architecture & Database Persistence

All runtime state, trades, command audits, and events are stored **directly in Supabase**, not on the local filesystem. This ensures complete reliability and persistence across container restarts (e.g., on Railway).

```
                      ┌────────────────────────────────────┐
                      │    Market Scheduler (IST Hours)    │
                      └─────────────────┬──────────────────┘
                                        │
                                        ▼
                      ┌────────────────────────────────────┐
                      │    Olymp Trade WebSocket Client    │
                      │       400 Live OHLCV Candles       │
                      └─────────────────┬──────────────────┘
                                        │
                                        ▼
                      ┌────────────────────────────────────┐
                      │    Kronos Foundation Model API     │
                      │         (Hosted on Modal)          │
                      └─────────────────┬──────────────────┘
                                        │
                             Confidence > 70% ?
                            /                  \
                          YES                   NO
                           │                     │
                           ▼                     ▼
                 Execute Olymp Trade       Skip Execution
                           │                     │
                           └──────────┬──────────┘
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │  Shared Supabase Database │
                        │  (trades, logs, settings) │
                        └─────────────┬─────────────┘
                                      │
               ┌──────────────────────┼──────────────────────┐
               ▼                      ▼                      ▼
        Web Dashboard            Telegram Bot           FastAPI API
```

---

## Repository Structure

```
.
├── bot.py                  # Core trading engine & loop
├── telegram_bot.py         # Telegram bot with PIN controls & alert broadcasts
├── server.py               # FastAPI service for status & resume/pause endpoints
├── Procfile                # Railway process definitions (worker, web, telegram, api)
├── runtime.txt             # Railway Python version specification (3.11.9)
├── requirements.txt        # Production Python dependencies (lean, no torch/transformers)
├── .env.example            # Template for environment variables
├── adapters/
│   ├── kronos_adapter.py   # Client for Modal Kronos API
│   ├── olymp_adapter.py    # Live candle fetching & order execution
│   └── risk_adapter.py     # Risk checks & guardrails
├── config/
│   └── market_schedule.py  # IST market operating windows
├── dashboard/
│   ├── app.py              # Flask real-time web dashboard
│   ├── static/             # Dashboard styles and scripts
│   └── templates/          # HTML templates
├── database/
│   ├── db.py               # Supabase database interface layer
│   └── schema.sql          # Complete Supabase schema (trades, logs, settings, alerts)
└── OT API/                 # Local Olymp Trade WebSocket client library
```

---

## Database Setup (Supabase)

The bot requires 4 tables in Supabase: `trades`, `logs`, `settings`, and `alerts`.

1. Go to your [Supabase Dashboard](https://supabase.com/dashboard).
2. Open the **SQL Editor**.
3. Copy and run the entire [`database/schema.sql`](database/schema.sql) file.
4. Obtain your `SUPABASE_URL` and `SUPABASE_KEY` (service role or anon key) from **Project Settings → API**.

---

## Configuration & Environment Variables

Create a `.env` file locally or set these variables in your hosting provider:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `PIN` | Security PIN for mode switches & critical controls | `2003` |
| `DEMO_MODE` | Trade on Demo (`true`) or Real account (`false`) | `true` |
| `FORCE_STOP` | Emergency kill switch | `false` |
| `DAILY_TARGET_ENABLED`| Automatically pause after reaching daily profit | `false` |
| `DAILY_TARGET` | Daily profit target in USD | `100.0` |
| `AUTO_EXECUTE_TRADES` | Place trades automatically on signal | `true` |
| `TRADE_DURATION` | Digital option expiry duration in seconds | `60` |
| `TRADE_AMOUNT` | Trade stake amount in USD (fallback) | `70` |
| `LOOP_INTERVAL` | Seconds between trading cycles | `300` (5 min) |
| `KRONOS_API_URL` | Modal API URL | `https://aman1-ranazonai--kronos-trading-api-fastapi-app.modal.run` |
| `OLYMP_TOKEN` | Olymp Trade session JWT token | `...` |
| `SUPABASE_URL` | Supabase Project URL | `https://<id>.supabase.co` |
| `SUPABASE_KEY` | Supabase API Key | `...` |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token from @BotFather | `...` |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID (auto-saved on `/start`) | `...` |
| `PORT` | Web port for Dashboard or FastAPI | `5000` |

---

## Deployment on Railway

The repository is configured for immediate deployment on [Railway](https://railway.app/).

### Step 1: Push Code to GitHub
Ensure your repository is initialized and pushed to GitHub:
```bash
git init
git add .
git commit -m "Production ready Kronos trading bot"
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

### Step 2: Create a Project in Railway
1. Open [Railway Dashboard](https://railway.app/).
2. Click **New Project** → **Deploy from GitHub repo**.
3. Select this repository.

### Step 3: Configure Environment Variables
In your Railway project settings, go to the **Variables** tab and add the variables listed above (`OLYMP_TOKEN`, `SUPABASE_URL`, `SUPABASE_KEY`, `TELEGRAM_BOT_TOKEN`, `PIN`, etc.).

### Step 4: Configure Services (Procfile)
Railway detects the `Procfile` and allows running distinct services from the same codebase:
- **Web Service (`web`)**: Runs the Dashboard `python3 dashboard/app.py`. Under service settings, generate a public domain to access your dashboard from anywhere.
- **Worker Service (`worker`)**: In Railway, create another service from the same repo and set its **Custom Start Command** to `python3 bot.py`.
- **Telegram Service (`telegram`)**: (Optional) Add a service with **Custom Start Command** `python3 telegram_bot.py`.

---

## Local Development & Execution

To run locally:

1. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Start the Trading Bot**:
   ```bash
   python3 bot.py
   ```

3. **Start the Web Dashboard**:
   ```bash
   python3 dashboard/app.py
   ```
   Open `http://localhost:5000` in your browser.

4. **Start the Telegram Bot**:
   ```bash
   python3 telegram_bot.py
   ```

---

## Telegram Bot Commands

Sensitive commands require the configured `PIN` (default: `2003`).

| Command | Action | PIN Required |
| :--- | :--- | :---: |
| `/start` | Subscribe chat to bot alerts & notifications | No |
| `/status` | View current market, signal, P&L, and state | No |
| `/pnl` | View today's total P&L, win rate, and trade count | No |
| `/market` | View current trading session and countdown to next | No |
| `/demo` | Switch execution to DEMO account | Yes |
| `/real` | Switch execution to REAL account | Yes |
| `/token <JWT>` | Update Olymp Trade session token without restart | Yes |
| `/forcestop` | Immediately halt automated trading | Yes |
| `/stop` | Pause or resume automated execution | Yes |
| `/target on/off`| Enable or disable daily profit target limit | Yes |
| `/help` | List available commands | No |

---

## Safety & Risk Controls

1. **Pause-After-Loss**: If a trade closes in a loss, the bot automatically enters `PAUSED_AFTER_LOSS` and alerts the operator. Trading does not resume until manually approved via Telegram or Dashboard.
2. **Fixed 70% Confidence Gate**: Trades are placed only when the Kronos AI model confidence strictly exceeds 0.70.
3. **Session Windows**: Trading only occurs during active market hours to prevent low-volatility slippage.
4. **PIN Protection**: 3 incorrect PIN attempts trigger a 5-minute lockout to protect against unauthorized command execution.
# ot-git
