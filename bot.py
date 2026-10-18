#!/usr/bin/env python3
"""
Kronos Trading Bot (Production)
================================
Scheduled multi-market trading bot that:
  * follows the market schedule defined in config/market_schedule.py
  * fetches live candles from Olymp Trade via WebSocket
  * asks the Kronos Modal API for BUY/SELL/HOLD predictions
  * tracks daily P&L, daily target and force-stop flags
  * logs all trades and events to Supabase (database/db.py)
  * exposes a live status dict for Telegram / Dashboard

All logging goes to stdout (captured by Railway). No local files.
"""

import os
import sys
import json
import time

import asyncio
import traceback
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv, set_key

from database.db import (
    insert_trade,
    insert_log,
    get_daily_pnl,
    get_daily_trades,
    close_trade,
    get_bot_status,
    set_bot_status,
    insert_alert,
    get_setting,
    set_setting,
)

import threading
from adapters.olymp_adapter import execute_trade, get_trade_amount, get_candles

# Make sure the project root is importable no matter where we launch from
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.market_schedule import (
    MARKET_SCHEDULE,
    IST,
    is_trading_day,
    get_current_market as get_scheduled_market,
    get_next_market_switch,
)

# ---------------------------------------------------------------------------
# Defaults (all overridable via .env)
# ---------------------------------------------------------------------------
DEFAULT_KRONOS_API_URL = "https://aman1-ranazonai--kronos-trading-api-fastapi-app.modal.run"
DEFAULT_PIN = "2003"
DEFAULT_MIN_CONFIDENCE = 0.65        # legacy .env knob (unused for the fixed 0.75 gate)
KRONOS_CONFIDENCE_THRESHOLD = 0.75   # fixed: trade only when confidence > 0.75
KRONOS_CANDLE_COUNT = 400            # candles sent to the Kronos API each cycle
DEFAULT_LOOP_INTERVAL = 300          # 5 minutes between cycles
DEFAULT_TRADE_AMOUNT = 70.0          # Olymp Trade app amount (fallback)
DEFAULT_DAILY_TARGET = 100.0

MAX_API_RETRIES = 3                  # retries for timeouts / connection errors
RETRY_DELAY = 5                      # seconds between retries (multiplied by attempt)
API_TIMEOUT = 30                     # seconds per HTTP request
OLYMP_CONNECT_TIMEOUT = 15           # seconds for WS connect / candle fetch

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")


_last_weekend_alert_date = None


def send_telegram_alert(message):
    """
    Fire a one-way Telegram alert from bot.py (never raises).

    * Pushes via the Telegram Bot API when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
      are set, and also records it in the alerts table (Supabase) via
      database/db.insert_alert so the Dashboard/Telegram can surface it.
    * Weekpause alerts are de-duplicated: at most one per calendar day.
    """
    global _last_weekend_alert_date
    today = datetime.now().date()
    if "Weekend pause" in message and _last_weekend_alert_date == today:
        return  # already alerted today — don't spam every cycle
    if "Weekend pause" in message:
        _last_weekend_alert_date = today

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001
            print(f"Telegram alert failed: {e}")
    try:
        insert_alert(alert_type="WEEKEND_PAUSE", message=message, bot_status="PAUSED_WEEKEND")
    except Exception as e:  # noqa: BLE001
        print(f"Database error: {e}")



def _env_bool(name, default=False):
    """Parse a boolean env var ('1', 'true', 'yes', 'on')."""
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default





class KronosBot:
    """Main trading bot: scheduling, predictions, and PIN-protected controls."""

    def __init__(self):
        # --- Load .env -----------------------------------------------------
        load_dotenv(ENV_FILE)

        # --- Settings ------------------------------------------------------
        self.pin = os.getenv("PIN", DEFAULT_PIN)
        self.kronos_api_url = os.getenv("KRONOS_API_URL", DEFAULT_KRONOS_API_URL)
        self.min_confidence = _env_float("MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
        self.loop_interval = int(_env_float("LOOP_INTERVAL", DEFAULT_LOOP_INTERVAL))
        self.trade_amount = get_trade_amount(default=DEFAULT_TRADE_AMOUNT)
        self.daily_target = _env_float("DAILY_TARGET", DEFAULT_DAILY_TARGET)

        self.demo_mode = _env_bool("DEMO_MODE", True)
        self.force_stop = _env_bool("FORCE_STOP", False)
        self.daily_target_enabled = _env_bool("DAILY_TARGET_ENABLED", False)
        self.olymp_token = os.getenv("OLYMP_TOKEN", "")

        # --- Execution Settings (Kronos-only decisioning) -------------------
        self.auto_execute_trades = _env_bool("AUTO_EXECUTE_TRADES", True)
        self.trade_duration = int(_env_float("TRADE_DURATION", 60))

        # --- Runtime state --------------------------------------------------
        self.daily_pnl = 0.0
        self.current_signal = "HOLD"
        self.current_confidence = 0.0
        self.current_market_name = None
        self.trade_date = datetime.now(IST).date()
        self.last_signal_time = None
        self.status = {}

        # Kronos-only decisioning: fixed 0.75 threshold
        self.optimal_threshold = KRONOS_CONFIDENCE_THRESHOLD

        # Pause-after-loss state (persisted in DB settings)
        initial_status_info = get_bot_status()
        self.bot_status = initial_status_info.get("status") or "RUNNING"
        self.last_loss_reason = initial_status_info.get("last_loss")
        self.pause_timestamp = initial_status_info.get("paused_at")

        # --- Shared database (Supabase via database/db.py) -------------------
        # All DB access goes through the module-level helpers, which never
        # raise and pause themselves after repeated failures.
        self.db = None  # kept for backwards compatibility (no longer used)
        self._open_trade_id = None

        self.log(None, "=" * 60)
        self.log(None, "KronosBot initialized")
        try:
            insert_log("START", f"Bot started in {'DEMO' if self.demo_mode else 'REAL'} mode",
                       {"mode": "DEMO" if self.demo_mode else "REAL"})
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
            # Continue trading - don't crash if DB fails

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, market, message):
        """
        Console + file log line in the format:
        [2026-09-13 09:05:00] [EURUSD] [DEMO] Signal: BUY | Confidence: 78% | Daily P&L: +$45 | DB: ✅
        """
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        market_str = market if market else "SYSTEM"
        mode_str = "DEMO" if self.demo_mode else "REAL"
        halted = self.force_stop or (self.daily_target_enabled
                                     and self.daily_pnl >= self.daily_target)
        db_str = "DB: ⏸" if halted else "DB: ✅"
        line = f"[{ts}] [{market_str}] [{mode_str}] {message} | {db_str}"
        print(line, flush=True)

    @staticmethod
    def format_pnl(pnl):
        """Format P&L like '+$45' / '-$12.50' (decimals only when needed)."""
        pnl = round(pnl, 2)
        sign = "+" if pnl >= 0 else "-"
        if pnl == int(pnl):
            return f"{sign}${abs(int(pnl))}"
        return f"{sign}${abs(pnl):.2f}"

    # ------------------------------------------------------------------
    # PIN protection
    # ------------------------------------------------------------------
    def verify_pin(self, entered_pin):
        """Check the entered PIN. Required for mode switch / token update / force stop."""
        ok = str(entered_pin) == str(self.pin)
        try:
            if ok:
                insert_log("PIN_ATTEMPT", "PIN verified",
                           {"result": "ok", "user": "control"})
            else:
                insert_log("PIN_WRONG", "Invalid PIN attempt", {"attempts_left": 2})
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
            # Continue trading - don't crash if DB fails
        if not ok:
            self.log(None, "PIN verification FAILED")
        return ok

    # ------------------------------------------------------------------
    # PIN-protected controls (callable later from Telegram / Dashboard)
    # ------------------------------------------------------------------
    def _persist_env(self, key, value):
        """Write a value to Supabase settings and .env (if present) so it survives restarts."""
        try:
            os.environ[key] = str(value)
            set_setting(key, str(value))
            if os.path.exists(ENV_FILE):
                set_key(ENV_FILE, key, str(value))
        except Exception as e:
            self.log(None, f"ERROR persisting {key} to DB/.env: {e}")

    # ------------------------------------------------------------------
    # Shared database event helpers (delegating to database/db.py)
    # ------------------------------------------------------------------
    def log_event(self, event, message, details=None):
        """Record a bot event to the shared logs table (never raises)."""
        try:
            insert_log(event, message, details)
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
            # Continue trading - don't crash if DB fails

    def record_trade(self, symbol, signal, confidence, entry_price):
        """Record a prepared trade (status='open') to the shared trades table.

        Returns the row id so the result can close it later; None when the
        write failed or the DB is unavailable.
        """
        try:
            return insert_trade(symbol=symbol, entry_price=entry_price,
                                signal=signal, confidence=confidence,
                                exit_price=None, pnl=None, status="open")
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
            # Continue trading - don't crash if DB fails
            return None

    def set_demo_mode(self, demo, entered_pin):
        """PIN-protected DEMO/REAL switch."""
        if not self.verify_pin(entered_pin):
            return False, "Invalid PIN"
        self.demo_mode = bool(demo)
        self._persist_env("DEMO_MODE", "true" if self.demo_mode else "false")
        self.log(None, f"Mode switched to {'DEMO' if self.demo_mode else 'REAL'}")
        self.log_event("PIN_ATTEMPT", "PIN verified for mode switch", {"action": "switch_mode"})
        self.log_event("MODE", f"Mode switched to {'DEMO' if self.demo_mode else 'REAL'}")
        return True, f"Mode set to {'DEMO' if self.demo_mode else 'REAL'}"

    def update_olymp_token(self, new_token, entered_pin):
        """PIN-protected token update — reloads without restarting the bot."""
        if not self.verify_pin(entered_pin):
            return False, "Invalid PIN"
        if not new_token or not str(new_token).strip():
            return False, "Empty token"
        self.olymp_token = str(new_token).strip()
        self._persist_env("OLYMP_TOKEN", self.olymp_token)
        self.log(None, "Olymp token updated and reloaded (no restart needed)")
        self.log_event("PIN_ATTEMPT", "PIN verified for token update", {"action": "update_token"})
        self.log_event("TOKEN", "Olymp token updated and reloaded (no restart needed)")
        return True, "Token updated"

    def set_force_stop(self, stop, entered_pin):
        """PIN-protected force stop / resume."""
        if not self.verify_pin(entered_pin):
            return False, "Invalid PIN"
        self.force_stop = bool(stop)
        self._persist_env("FORCE_STOP", "true" if self.force_stop else "false")
        self.log(None, f"FORCE_STOP set to {self.force_stop}")
        self.log_event("PIN_ATTEMPT", "PIN verified for force stop", {"action": "force_stop"})
        if self.force_stop:
            self.bot_status = "PAUSED_MANUAL"
            set_bot_status("PAUSED_MANUAL", reason="Force stop by user")
            self.log_event("FORCESTOP", "Trading stopped by user", {})
        else:
            self.bot_status = "RUNNING"
            self.last_loss_reason = None
            set_bot_status("RUNNING", reason=None)
            self.log_event("START", "Trading resumed by user", {})
        return True, f"Force stop {'enabled' if self.force_stop else 'cleared'}"

    def set_daily_target_enabled(self, enabled, entered_pin):
        """PIN-protected daily target toggle."""
        if not self.verify_pin(entered_pin):
            return False, "Invalid PIN"
        self.daily_target_enabled = bool(enabled)
        self._persist_env("DAILY_TARGET_ENABLED", "true" if self.daily_target_enabled else "false")
        self.log(None, f"Daily target {'ENABLED' if self.daily_target_enabled else 'DISABLED'} (target: ${self.daily_target})")
        self.log_event("PIN_ATTEMPT", "PIN verified for target toggle", {"action": "set_target"})
        self.log_event("TARGET", f"Daily target {'ENABLED' if self.daily_target_enabled else 'DISABLED'} "
                                 f"(target: ${self.daily_target})")
        return True, f"Daily target {'enabled' if self.daily_target_enabled else 'disabled'}"

    # ------------------------------------------------------------------
    # Settings reload (picks up .env edits from Telegram/Dashboard)
    # ------------------------------------------------------------------
    def reload_settings(self):
        """Re-read .env + DB settings each cycle so external changes take effect without restart."""
        old = (self.demo_mode, self.force_stop, self.daily_target_enabled,
               self.olymp_token, self.daily_target, self.auto_execute_trades)
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)

        # Database is the primary shared persistent store across containers/processes
        db_demo = get_setting("DEMO_MODE")
        if db_demo is not None:
            self.demo_mode = db_demo.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.demo_mode = _env_bool("DEMO_MODE", self.demo_mode)

        db_stop = get_setting("FORCE_STOP")
        if db_stop is not None:
            self.force_stop = db_stop.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.force_stop = _env_bool("FORCE_STOP", self.force_stop)

        db_target_enabled = get_setting("DAILY_TARGET_ENABLED")
        if db_target_enabled is not None:
            self.daily_target_enabled = db_target_enabled.strip().lower() in ("1", "true", "yes", "on")
        else:
            self.daily_target_enabled = _env_bool("DAILY_TARGET_ENABLED", self.daily_target_enabled)

        db_token = get_setting("OLYMP_TOKEN")
        if db_token:
            self.olymp_token = db_token
        else:
            self.olymp_token = os.getenv("OLYMP_TOKEN", self.olymp_token)

        db_target = get_setting("DAILY_TARGET")
        if db_target:
            try:
                self.daily_target = float(db_target)
            except ValueError:
                pass
        else:
            self.daily_target = _env_float("DAILY_TARGET", self.daily_target)

        self.min_confidence = _env_float("MIN_CONFIDENCE", self.min_confidence)
        self.auto_execute_trades = _env_bool("AUTO_EXECUTE_TRADES", getattr(self, "auto_execute_trades", True))
        new = (self.demo_mode, self.force_stop, self.daily_target_enabled,
               self.olymp_token, self.daily_target, self.auto_execute_trades)
        if old != new:
            self.log(None, "Settings reloaded (change detected from DB/env)")

    # ------------------------------------------------------------------
    # Market schedule
    # ------------------------------------------------------------------
    def get_current_market(self):
        """Active market from config/market_schedule.py (None = outside hours)."""
        return get_scheduled_market()

    def detect_market_switch(self, market):
        """Log a line whenever the active market changes."""
        new_name = market["name"] if market else None
        if new_name != self.current_market_name:
            old_name = self.current_market_name
            if old_name is None and new_name is not None:
                self.log(market["symbol"], f"Market window OPEN: {market['name']} "
                                           f"({market['start_time']}-{market['end_time']} IST) — {market['description']}")
                self.log_event("MARKET_SWITCH", f"Switched from none to {market['symbol']}",
                               {"from": None, "to": market["symbol"]})
            elif new_name is None:
                self.log(None, f"Market window CLOSED: {old_name} ended. "
                               f"Next window starts {get_next_market_switch()} IST")
                self.log_event("MARKET_SWITCH", f"Switched from {old_name} to closed",
                               {"from": old_name, "to": None})
            else:
                self.log(market["symbol"], f"Market switch: {old_name} -> {new_name}")
                self.log_event("MARKET_SWITCH", f"Switched from {old_name} to {market['symbol']}",
                               {"from": old_name, "to": market["symbol"]})
            self.current_market_name = new_name

    # ------------------------------------------------------------------
    # Candle data
    # ------------------------------------------------------------------
    def _fetch_candles_olymp(self, symbol, count=60, size=300):
        """Fetch real candles via the olymptrade_ws client (async).

        size is the candle length in SECONDS: 300 == 5-minute candles only.
        """
        res = get_candles(symbol, lookback=count, size=size)
        if isinstance(res, dict) and "error" in res:
            raise RuntimeError(res["error"])
        return res

    def fetch_candles(self, symbol, count=60, size=300):
        """Fetch live candles from Olymp Trade. Returns None on failure (never raises)."""
        if not self.olymp_token:
            self.log(symbol, "ERROR: OLYMP_TOKEN not set — cannot fetch candles")
            return None
        try:
            candles = self._fetch_candles_olymp(symbol, count=count, size=size)
            if candles:
                self.log(symbol, f"Fetched {len(candles)} live candles from Olymp")
                return candles
            self.log(symbol, "Olymp returned no candles — skipping cycle")
            return None
        except Exception as e:
            self.log(symbol, f"Olymp candle fetch failed ({e}) — skipping cycle")
            return None

    # ------------------------------------------------------------------
    # Kronos Modal API
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_prediction(data):
        """Extract signal + confidence from the Kronos response, tolerating key variants."""
        if not isinstance(data, dict):
            return None
        raw_signal = (data.get("signal") or data.get("prediction")
                      or data.get("action") or data.get("direction") or "hold")
        sig = str(raw_signal).strip().lower()
        if sig in ("buy", "up", "long", "call"):
            signal = "BUY"
        elif sig in ("sell", "down", "short", "put"):
            signal = "SELL"
        else:
            signal = "HOLD"

        conf = data.get("confidence", data.get("probability", data.get("prob", 0)))
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            conf = 0.0
        if conf > 1.0:  # normalize 0-100 responses to 0-1
            conf = conf / 100.0
        return {"signal": signal, "confidence": conf, "raw": data}

    def call_kronos_api(self, candles, symbol):
        """POST candles to the Kronos Modal API /predict endpoint. Retries on network errors."""
        url = self.kronos_api_url.rstrip("/") + "/predict"
        sanitized = []
        for c in (candles or []):
            if not isinstance(c, dict):
                continue
            t = c.get("time") or c.get("t") or c.get("timestamp")
            o = float(c.get("open") or c.get("o") or 0.0)
            h = float(c.get("high") or c.get("h") or o)
            l = float(c.get("low") or c.get("l") or o)
            cl = float(c.get("close") or c.get("c") or o)
            v = float(c.get("volume") or c.get("vol") or c.get("v") or 100.0)
            sanitized.append({
                "time": int(t) if t else int(time.time()),
                "open": o, "high": h, "low": l, "close": cl, "volume": v,
            })
        payload = {"symbol": symbol, "timeframe": "5m", "candles": sanitized}
        headers = {"Content-Type": "application/json"}

        for attempt in range(1, MAX_API_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=API_TIMEOUT)
                resp.raise_for_status()
                parsed = self._parse_prediction(resp.json())
                if parsed is None:
                    self.log(symbol, f"Kronos API returned unexpected payload: {str(resp.text)[:200]}")
                    return None
                return parsed
            except (requests.Timeout, requests.ConnectionError) as e:
                self.log(symbol, f"Kronos API network error (attempt {attempt}/{MAX_API_RETRIES}): {e}")
            except requests.HTTPError as e:
                self.log(symbol, f"Kronos API HTTP error (attempt {attempt}/{MAX_API_RETRIES}): "
                                 f"{e.response.status_code} {e.response.text[:200]}")
            except json.JSONDecodeError as e:
                self.log(symbol, f"Kronos API returned invalid JSON: {e}")
                return None
            except Exception as e:
                self.log(symbol, f"Kronos API unexpected error (attempt {attempt}/{MAX_API_RETRIES}): {e}")
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY * attempt)

        self.log(symbol, f"Kronos API FAILED after {MAX_API_RETRIES} attempts")
        return None

    # ------------------------------------------------------------------
    # Daily target / P&L
    # ------------------------------------------------------------------
    def check_daily_target(self):
        """True when the daily target feature is on and the target is reached."""
        if not self.daily_target_enabled:
            return False
        return self.daily_pnl >= self.daily_target

    def record_trade_result(self, pnl, trade_id=None, exit_price=None, symbol=None):
        """Record a trade result: close its open row in the trades table and update P&L."""
        pnl = float(pnl)
        self.daily_pnl += pnl
        self.log(symbol, f"Trade result recorded: {self.format_pnl(pnl)} "
                         f"(Daily P&L: {self.format_pnl(self.daily_pnl)})")
        if trade_id is None:
            trade_id = getattr(self, "_open_trade_id", None)
        try:
            close_trade(trade_id, exit_price=exit_price, pnl=pnl)
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
        self.log_event("TRADE", f"Trade closed with result {self.format_pnl(pnl)}",
                       {"pnl": round(pnl, 2), "exit_price": exit_price,
                        "daily_pnl": round(self.daily_pnl, 2), "trade_id": trade_id})

        # --- PAUSE AFTER LOSS ---
        if pnl < 0:
            loss_reason = f"Trade {trade_id or ''} closed with loss of {self.format_pnl(pnl)}"
            self.bot_status = "PAUSED_AFTER_LOSS"
            self.last_loss_reason = loss_reason
            self.pause_timestamp = datetime.now(IST).isoformat()
            set_bot_status("PAUSED_AFTER_LOSS", reason=loss_reason)
            self.log(symbol, "Trade loss detected. Bot paused. Awaiting manual approval.")
            self.log_event("LOSS_DETECTED", "Trade loss detected. Bot paused. Awaiting manual approval.",
                           {"trade_id": trade_id, "pnl": pnl, "reason": loss_reason})
            insert_alert(
                alert_type="LOSS_DETECTED",
                message=f"Trade loss detected ({self.format_pnl(pnl)} on {symbol or 'market'}). Bot paused. Awaiting manual approval.",
                bot_status="PAUSED_AFTER_LOSS",
                trade_id=trade_id,
            )
        else:
            self.log(symbol, f"Trade ended in profit/breakeven ({self.format_pnl(pnl)}). Continuing to next cycle.")

        self._open_trade_id = None

    def resume_trading(self, entered_pin=None):
        """Resume trading from PAUSED state and set bot_status to RUNNING."""
        if entered_pin is not None and not self.verify_pin(entered_pin):
            return False, "Invalid PIN"
        self.bot_status = "RUNNING"
        self.last_loss_reason = None
        self.pause_timestamp = None
        self.force_stop = False
        self._persist_env("FORCE_STOP", "false")
        set_bot_status("RUNNING", reason=None)
        self.log(None, "Trading resumed manually. Status set to RUNNING.")
        self.log_event("RESUME", "Trading resumed manually. Status set to RUNNING.")
        return True, "Bot resumed successfully"

    def reset_daily_pnl_if_new_day(self):
        """Daily P&L and trade counts come from the DB per IST day — nothing to reset."""
        return

    def _track_trade_outcome(self, trade_id, ext_id, symbol, signal, entry_price, amount, duration):
        """Track digital option trade outcome after expiry in a daemon thread."""
        def _waiter():
            time.sleep(max(5, duration + 2))
            try:
                candles = self.fetch_candles(symbol, count=5)
                exit_price = candles[-1].get("close") if candles else entry_price
                win = False
                if entry_price and exit_price:
                    if signal == "BUY" and exit_price > entry_price:
                        win = True
                    elif signal == "SELL" and exit_price < entry_price:
                        win = True
                pnl = round(amount * 0.82 if win else -amount, 2)
                self.record_trade_result(pnl=pnl, trade_id=trade_id, exit_price=exit_price, symbol=symbol)
            except Exception as e:  # noqa: BLE001
                self.log(symbol, f"Error tracking trade outcome: {e}")

        t = threading.Thread(target=_waiter, name=f"trade-track-{trade_id}", daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Status for Telegram / Dashboard
    # ------------------------------------------------------------------
    def get_status(self):
        return self.status

    def update_status(self, market):
        # Live trade stats from the shared DB (0s when DB unavailable)
        try:
            trades_today_rows = get_daily_trades()
            wins, losses = 0, 0
            for r in trades_today_rows:
                if r.get("status") == "closed" and r.get("pnl") is not None:
                    if float(r["pnl"]) > 0:
                        wins += 1
                    elif float(r["pnl"]) < 0:
                        losses += 1
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
            trades_today_rows, wins, losses = [], 0, 0

        self.status = {
            "current_market": self.current_market_name,
            "symbol": market["symbol"] if market else None,
            "signal": self.current_signal,
            "confidence": round(self.current_confidence, 4),
            "optimal_threshold": round(getattr(self, "optimal_threshold", 0.75), 4),
            "daily_pnl": round(self.daily_pnl, 2),
            "trades_today": len(trades_today_rows),
            "wins": wins,
            "losses": losses,
            "win_rate": (round(wins / (wins + losses) * 100, 1)
                         if (wins + losses) else 0.0),
            "mode": "DEMO" if self.demo_mode else "REAL",
            "bot_status": getattr(self, "bot_status", "RUNNING"),
            "last_loss_reason": getattr(self, "last_loss_reason", None),
            "pause_timestamp": getattr(self, "pause_timestamp", None),
            "force_stop": self.force_stop,
            "daily_target_enabled": self.daily_target_enabled,
            "daily_target": self.daily_target,
            "target_reached": self.check_daily_target(),
            "next_market_switch": get_next_market_switch(),
            "last_signal_time": self.last_signal_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_signal_time else None,
            "token_set": bool(self.olymp_token),
            "updated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
        }
        return self.status

    # ------------------------------------------------------------------
    # Startup banner
    # ------------------------------------------------------------------
    def print_startup_banner(self):
        market = self.get_current_market()
        self.log(None, "=" * 60)
        self.log(None, "BOT NAME : Kronos Trading Bot")
        self.log(None, f"MODE     : {'DEMO' if self.demo_mode else 'REAL'}")
        self.log(None, f"API URL  : {self.kronos_api_url}")
        self.log(None, "MARKETS  :")
        for m in MARKET_SCHEDULE:
            self.log(None, f"  - {m['name']:<22} {m['symbol']:<7} {m['start_time']}-{m['end_time']} IST ({m['description']})")
        if market:
            self.log(None, f"CURRENT  : {market['name']} ({market['symbol']}) — active now")
        else:
            self.log(None, f"CURRENT  : none — outside market hours, next window {get_next_market_switch()} IST")
        self.log(None, f"Daily target: {'ENABLED ($%s)' % self.daily_target if self.daily_target_enabled else 'disabled — trading all day'}")
        self.log(None, f"Force stop  : {self.force_stop} | Olymp token: {'set' if self.olymp_token else 'not set'}")
        self.log(None, f"AI Agent    : disabled — Kronos-only decisioning (fixed threshold {KRONOS_CONFIDENCE_THRESHOLD:.2f})")
        self.log(None, f"Execution   : {'LIVE AUTO-EXECUTE (Olymp)' if getattr(self, 'auto_execute_trades', True) else 'SIMULATION ONLY'}")
        self.log(None, "=" * 60)

    # ------------------------------------------------------------------
    # Trading cycle
    # ------------------------------------------------------------------
    def run_cycle(self, pnl_hint=None, trade_count_hint=None):
        """One trading cycle. Never raises — all errors are caught and logged."""
        self.reload_settings()

        # Prefer DB-derived daily stats (from main_loop) over in-memory ones
        if pnl_hint is not None:
            self.daily_pnl = float(pnl_hint)
        if trade_count_hint is not None:
            self.trade_count = int(trade_count_hint)

        market = self.get_current_market()
        self.detect_market_switch(market)

        # --- Pause-after-loss & manual pause check -----------------------
        status_info = get_bot_status()
        current_status = status_info.get("status") or getattr(self, "bot_status", "RUNNING")
        if current_status == "PAUSED_AFTER_LOSS":
            self.bot_status = "PAUSED_AFTER_LOSS"
            self.current_signal = "PAUSED_AFTER_LOSS"
            self.last_loss_reason = status_info.get("last_loss") or getattr(self, "last_loss_reason", "trade loss detected")
            self.log(None, f"Bot is PAUSED_AFTER_LOSS ({self.last_loss_reason}). Skipping cycle. Awaiting manual approval.")
            self.update_status(market)
            return
        elif current_status == "PAUSED_MANUAL":
            self.bot_status = "PAUSED_MANUAL"
            self.current_signal = "PAUSED_MANUAL"
            self.log(None, "Bot is PAUSED_MANUAL. Skipping cycle. Awaiting manual approval.")
            self.update_status(market)
            return
        else:
            self.bot_status = "RUNNING"

        # --- Force stop check --------------------------------------------
        if self.force_stop:
            self.current_signal = "HALTED"
            self.log(None, "FORCE STOP is active — all trading halted")
            self.update_status(market)
            return

        # --- Daily target check --------------------------------------------
        if self.check_daily_target():
            self.current_signal = "TARGET_REACHED"
            self.log(None, f"Daily target reached ({self.format_pnl(self.daily_pnl)} "
                           f">= ${self.daily_target}) — trading paused for today")
            self.log_event("TARGET_REACHED", f"Daily target reached! P&L: ${self.daily_pnl}",
                           {"pnl": round(self.daily_pnl, 2)})
            self.update_status(market)
            return

        # --- Outside market hours ------------------------------------------
        if market is None:
            self.current_signal = "NO_MARKET"
            self.log(None, f"Outside scheduled market hours — next window {get_next_market_switch()} IST")
            self.update_status(market)
            return

        symbol = market["symbol"]

        # --- Fetch 5m candles (spec: 400 x 5-minute candles every cycle) ------
        current_time = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        self.log(symbol, f"Fetching 5m candle for {symbol} at {current_time}")
        try:
            candles = self.fetch_candles(symbol, count=KRONOS_CANDLE_COUNT)
        except Exception as e:
            self.log(symbol, f"ERROR fetching candles: {e}")
            print(traceback.format_exc(), flush=True)
            self.update_status(market)
            return
        if not candles:
            self.log(symbol, "No candle data available this cycle")
            self.update_status(market)
            return

        # --- Kronos prediction (UP/DOWN + 0-1 confidence) ---------------------
        prediction = self.call_kronos_api(candles, symbol)
        if prediction is None:
            error_msg = getattr(self, "_last_api_error", None) or "Kronos API failed after retries"
            self.log_event("ERROR", f"Failed to call Kronos API: {error_msg}",
                           {"error": str(error_msg), "symbol": symbol})
            self.update_status(market)
            return

        signal = prediction["signal"]          # BUY/SELL/HOLD (UP/DOWN mapped)
        confidence = prediction["confidence"]  # 0.0 - 1.0
        self.current_confidence = confidence

        entry_price = candles[-1].get("close") if candles else None

        # --- Gates: force stop / target ---------------------------------------
        if self.force_stop:
            self.current_signal = "HALTED"
            self.log(symbol, f"Signal {signal} suppressed — FORCE STOP became active")
            self.log_event("PREDICTION", f"Prediction {signal} skipped — force stop",
                           {"symbol": symbol, "signal": signal,
                            "confidence": round(confidence, 4), "trade_placed": False})
            self.update_status(market)
            return
        if self.check_daily_target():
            self.current_signal = "TARGET_REACHED"
            self.log(symbol, f"Signal {signal} suppressed — daily target reached")
            self.log_event("PREDICTION", f"Prediction {signal} skipped — daily target reached",
                           {"symbol": symbol, "signal": signal,
                            "confidence": round(confidence, 4), "trade_placed": False})
            self.update_status(market)
            return

        # --- Kronos-only decision gate: confidence > 0.75 ------------------------
        if confidence <= KRONOS_CONFIDENCE_THRESHOLD or signal == "HOLD":
            self.current_signal = "HOLD"
            self.log(symbol, f"Prediction skipped: confidence {confidence:.2f} below "
                             f"{KRONOS_CONFIDENCE_THRESHOLD:.2f} threshold")
            self.log(symbol, f"Signal: {signal} | Confidence: {confidence * 100:.0f}% "
                             f"(not above {KRONOS_CONFIDENCE_THRESHOLD * 100:.0f}%) — no trade")
            self.log_event("PREDICTION", f"Prediction {signal} for {symbol} — confidence below threshold",
                           {"symbol": symbol, "signal": signal, "prediction": signal,
                            "confidence": round(confidence, 4), "trade_placed": False,
                            "entry_price": entry_price})
            self.update_status(market)
            return

        # --- Trade amount from Olymp Trade settings (fresh, before every trade) ---
        trade_amount = get_trade_amount(default=DEFAULT_TRADE_AMOUNT)
        self.trade_amount = trade_amount

        # --- Confirmed Trade Signal & Execution --------------------------------
        self.current_signal = signal
        self.last_signal_time = datetime.now(IST)
        pnl_str = self.format_pnl(self.daily_pnl)
        self.log(symbol, f"Signal: {signal} | Conf: {confidence * 100:.0f}% | "
                         f"Threshold: {KRONOS_CONFIDENCE_THRESHOLD * 100:.0f}% | Amount: ${trade_amount:g} | P&L: {pnl_str}")
        self.log_event("PREDICTION", f"Confirmed {signal} for {symbol}",
                       {"symbol": symbol, "signal": signal, "prediction": signal,
                        "confidence": round(confidence, 4), "trade_placed": True,
                        "amount": trade_amount, "entry_price": entry_price})

        if getattr(self, "auto_execute_trades", True):
            exec_res = execute_trade(signal, symbol, size=trade_amount)
            if exec_res.get("ok"):
                trade_info = exec_res.get("trade", {})
                ext_id = trade_info.get("id") or trade_info.get("trade_id")
                self.log(symbol, f"🔥 TRADE EXECUTED on Olymp: {signal} {symbol} amount=${trade_amount:g} mode={'DEMO' if self.demo_mode else 'REAL'} (id={ext_id})")
                self._open_trade_id = self.record_trade(symbol, signal, confidence, entry_price)
                self.log_event("TRADE", f"Trade executed: {symbol} {signal} amount=${trade_amount:g} mode={'DEMO' if self.demo_mode else 'REAL'}",
                               {"symbol": symbol, "signal": signal, "confidence": round(confidence, 4),
                                "amount": trade_amount, "entry_price": entry_price, "ext_id": ext_id})
                self._track_trade_outcome(self._open_trade_id, ext_id, symbol, signal, entry_price, trade_amount, self.trade_duration)
            else:
                err = exec_res.get("error", "unknown error")
                self.log(symbol, f"Olymp trade execution failed: {err} — recording trade attempt")
                self._open_trade_id = self.record_trade(symbol, signal, confidence, entry_price)
                self.log_event("TRADE_FAILED", f"Execution failed: {err}", {"symbol": symbol, "error": err})
        else:
            self._open_trade_id = self.record_trade(symbol, signal, confidence, entry_price)
            self.log(symbol, f"Trade prepared (simulated): {symbol} {signal} amount=${trade_amount:g} mode={'DEMO' if self.demo_mode else 'REAL'}")
            self.log_event("TRADE", f"Trade prepared: {symbol} {signal} amount=${trade_amount:g}",
                           {"symbol": symbol, "signal": signal, "confidence": round(confidence, 4),
                            "entry_price": entry_price})

        if self._open_trade_id is None:
            self.log(symbol, "WARNING: trade not saved to trades table (DB unavailable or offline)")

        # --- Status -------------------------------------------------------------
        self.update_status(market)
        self.log(symbol, f"Status: Daily P&L {self.format_pnl(self.daily_pnl)} | "
                         f"Mode {'DEMO' if self.demo_mode else 'REAL'} | "
                         f"Next switch {get_next_market_switch()} IST")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def main_loop(self):
        self.print_startup_banner()
        self.log(None, f"Trading loop started (every {self.loop_interval}s). Press Ctrl+C to stop.")
        self.log(None, f"Weekend filter: {'ACTIVE' if is_trading_day() else 'today is Sat/Sun — trading paused'}")
        while True:
            # --- Weekend check: skip Sat/Sun entirely (low liquidity & noise) ---
            if not is_trading_day():
                day_name = datetime.now(IST).strftime("%A")
                self.log(None, f"Weekend detected - Bot paused. No trading today ({day_name}).")
                self.current_signal = "WEEKEND_PAUSE"
                send_telegram_alert(f"🛑 Weekend pause: No trading Sat/Sun (today: {day_name}). Trades resume Monday 09:00 IST.")
                time.sleep(300)
                continue

            try:
                # Live daily stats from the shared DB (used in status/logging)
                try:
                    current_pnl = get_daily_pnl()
                    trade_count = len(get_daily_trades())
                    self.daily_pnl = current_pnl
                    self.trade_count = trade_count
                except Exception as e:  # noqa: BLE001
                    print(f"Database error: {e}")
                    # Continue trading - don't crash if DB fails
                    current_pnl, trade_count = self.daily_pnl, getattr(self, "trade_count", 0)
                self.run_cycle(pnl_hint=current_pnl, trade_count_hint=trade_count)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                self.log(None, f"ERROR in trading cycle: {e} — continuing")
                print(traceback.format_exc(), flush=True)
                self.log_event("ERROR", f"ERROR in trading cycle: {e}")
                time.sleep(300)
                continue
            time.sleep(self.loop_interval)


if __name__ == "__main__":
    bot = KronosBot()
    try:
        bot.main_loop()
    except KeyboardInterrupt:
        bot.log(None, "Shutdown requested (Ctrl+C) — Kronos Trading Bot stopped")
        try:
            bot.log_event("STOP", "Bot shutdown (Ctrl+C)", {})
        except Exception as e:  # noqa: BLE001
            print(f"Database error: {e}")
