# ⚙️ Backend

This folder documents the **backend** (server-side Python) layer of the Kronos Trading Bot.

> **Note:** The actual source files live at their original paths in the project root to keep Python imports working without changes. This README serves as a navigation guide.

---

## Architecture overview

```
Kronos Trading Bot — Backend Components
─────────────────────────────────────────────────────────────
bot.py                  ← Trading loop (main worker process)
server.py               ← FastAPI control API (resume/pause/status)
telegram_bot.py         ← Telegram bot interface
dashboard/app.py        ← Flask web dashboard API

adapters/
  ├── olymp_adapter.py  ← Olymp Trade WebSocket (candles + trade exec)
  ├── kronos_adapter.py ← Kronos AI API (BUY/SELL/HOLD predictions)
  └── risk_adapter.py   ← Risk management gates

database/
  ├── db.py             ← All Supabase read/write helpers
  └── schema.sql        ← Supabase table definitions

config/
  └── market_schedule.py ← Trading window schedule (IST)

OT API/
  └── olymptrade_ws/    ← WebSocket client library (third-party, read-only)
```

---

## What each piece does

### `bot.py` — Trading Loop
- Runs on a configurable `LOOP_INTERVAL` (default 300 s)
- Reads market schedule → fetches candles → calls Kronos API → executes trade
- Writes all events to Supabase via `database/db.py`
- Reads `FORCE_STOP` / `DEMO_MODE` from `.env` and DB settings

### `server.py` — FastAPI Control API
- REST endpoints for pause/resume/status (used by Telegram bot & external tools)
- PIN or API-token authenticated
- Runs on `PORT` (default 8000)

### `telegram_bot.py` — Telegram Interface
- Sends trade alerts and status to a Telegram chat
- Allows `/start`, `/stop`, `/status`, `/mode` commands via Telegram

### `dashboard/app.py` — Flask Dashboard API
- Serves the web dashboard at port 5000
- Provides `/api/status`, `/api/trades`, `/api/logs`, `/api/markets`, `/api/command`

---

## Adapters

| File | Purpose |
|---|---|
| `adapters/olymp_adapter.py` | Connect to Olymp Trade via WebSocket, fetch candles, place orders |
| `adapters/kronos_adapter.py` | Call the Kronos Modal AI API for trade signals |
| `adapters/risk_adapter.py` | Daily loss limit, consecutive loss gate, min balance check |

---

## Database (`database/db.py`)

All data is stored in **Supabase** (PostgreSQL). Key tables:

| Table | Contents |
|---|---|
| `trades` | Every executed trade (symbol, direction, P&L, status) |
| `logs` | All bot events (PREDICTION, TRADE, ERROR, RESUME, etc.) |
| `settings` | Runtime settings (TRADE_AMOUNT, OLYMP_TOKEN, DEMO_MODE, …) |
| `alerts` | Loss/error alerts (resolved by resume command) |

---

## Environment variables (`.env`)

| Key | Description |
|---|---|
| `OLYMP_TOKEN` | Olymp Trade JWT access token |
| `OLYMP_ACCOUNT_ID` | Account ID (auto-detected if blank) |
| `DEMO_MODE` | `true` = demo account, `false` = real money |
| `TRADE_AMOUNT` | Stake per trade in USD |
| `TRADE_DURATION` | Option duration in seconds (default 60) |
| `KRONOS_API_URL` | Kronos Modal AI API URL |
| `LOOP_INTERVAL` | Bot cycle interval in seconds |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Chat ID for Telegram notifications |
| `PIN` | Dashboard / API PIN (default 2003) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_KEY` | Supabase anon key |

---

## Running locally

```bash
# Trading bot
python3 bot.py

# Web dashboard  (http://localhost:5000)
python3 dashboard/app.py

# FastAPI control server  (http://localhost:8000)
python3 server.py

# Telegram bot
python3 telegram_bot.py
```

## Running on Railway

See `Procfile` in the project root for Railway service definitions.
