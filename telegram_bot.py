#!/usr/bin/env python3
"""
Kronos Telegram Bot
===================

Telegram remote control for the Kronos Trading Bot (bot.py).

  * /start  /status  /demo  /real  /token  /forcestop  /target on|off
  * /pnl  /market  /help  /restart  /stop (resume helper)
  * Auto status broadcast every 5 minutes
  * Trade notifications (polls the shared `trades` table)
  * Market-switch and connection-lost notifications
  * PIN verification (PIN-protected ops, 3 wrong attempts = 5 min lockout)
  * Rate limiting (max 10 commands/minute), command logging, error handling

Communication with bot.py:
  * Settings are persisted to the shared .env (bot.py reloads it every cycle),
    so changes take effect immediately without restarting either process
  * Live status / P&L / trades are read from the shared database
    (tables: `bot_state`, `trades`, `bot_commands`) when SUPABASE_URL/KEY
    are configured; everything degrades gracefully without them.

Run:  python telegram_bot.py
"""

import asyncio
import html
import logging
import os
import re
import subprocess
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv, set_key

# Make sure the project root is importable no matter where we launch from
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from bot import is_trading_day

from config.market_schedule import (
    IST,
    MARKET_SCHEDULE,
    get_current_market as get_scheduled_market,
)

from database.db import (
    set_bot_status,
    resolve_alerts,
    insert_log,
    get_daily_pnl,
    get_daily_trades,
    get_win_rate,
    get_setting,
    set_setting,
)

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
BOT_PY_PATH = os.path.join(PROJECT_ROOT, "bot.py")

DEFAULT_PIN = "2003"

# Auto status broadcast interval (5 minutes, per spec)
STATUS_INTERVAL = int(os.getenv("TELEGRAM_STATUS_INTERVAL", "300"))
# Trade notification polling interval
TRADE_POLL_INTERVAL = int(os.getenv("TELEGRAM_TRADE_POLL", "30"))
# Market-switch watch interval
MARKET_WATCH_INTERVAL = int(os.getenv("TELEGRAM_MARKET_WATCH", "30"))
# Rate limiting: max commands per user per minute
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "10"))

MAX_PIN_ATTEMPTS = 3            # wrong attempts before lockout (per spec)
PIN_LOCKOUT_SECONDS = 300       # 5 minutes

# Shared DB table names (created/maintained alongside bot.py)
DB_STATUS_TABLE = os.getenv("DB_STATUS_TABLE", "bot_state")
DB_TRADES_TABLE = os.getenv("DB_TRADES_TABLE", "trades")
DB_COMMANDS_TABLE = os.getenv("DB_COMMANDS_TABLE", "bot_commands")

logger = logging.getLogger("kronos_telegram")


def _save_setting(key, value):
    """Save setting in-memory, Supabase settings table, and .env if present."""
    os.environ[key] = str(value)
    try:
        set_setting(key, str(value))
    except Exception as e:
        logger.error(f"Failed to persist {key} to DB: {e}")
    if os.path.exists(ENV_FILE):
        try:
            set_key(ENV_FILE, key, str(value))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def format_pnl(pnl):
    """Format P&L like '+$45' / '-$12.50' (same style as bot.py)."""
    try:
        pnl = round(float(pnl), 2)
    except (TypeError, ValueError):
        pnl = 0.0
    sign = "+" if pnl >= 0 else "-"
    if pnl == int(pnl):
        return f"{sign}${abs(int(pnl))}"
    return f"{sign}${abs(pnl):.2f}"


def _to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _fmt_remaining(end_time_str):
    """Human-readable time remaining until 'HH:MM' IST today (clamped at 0)."""
    try:
        now = datetime.now(IST)
        h, m = end_time_str.split(":")
        end = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        secs = max(0, int((end - now).total_seconds()))
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    except (ValueError, AttributeError):
        return "?"


class RateLimiter:
    """Sliding-window per-user rate limiter (commands per minute)."""

    def __init__(self, max_per_minute):
        self.max = max(1, int(max_per_minute))
        self.window = 60.0
        self._hits = defaultdict(deque)

    def allow(self, key):
        now = time.time()
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            return False
        q.append(now)
        return True


def safe_handler(fn):
    """Catch every exception so the Telegram loop never dies."""
    @wraps(fn)
    async def wrapper(self, update, context):
        try:
            await fn(self, update, context)
        except Exception as e:  # noqa: BLE001
            logger.exception("Handler %s failed: %s", fn.__name__, e)
            await self._reply(update, "⚠️ Error processing command")
    return wrapper


# ---------------------------------------------------------------------------
# Main Telegram bot
# ---------------------------------------------------------------------------
class KronosTelegramBot:
    """Telegram front-end for the Kronos Trading Bot."""

    def __init__(self, kronos=None):
        load_dotenv(ENV_FILE)

        # Shared KronosBot instance (PIN checks + .env persistence helpers).
        # Optional: pass one in when embedding into the same process as bot.py.
        if kronos is None:
            try:
                from bot import KronosBot
                kronos = KronosBot()
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not import bot.KronosBot (%s) — using .env fallback", e)
                kronos = None
        self.kronos = kronos

        # Shared database (optional)
        self.supabase = None
        url, key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
        if url and key:
            try:
                from supabase import create_client
                self.supabase = create_client(url, key)
                logger.info("Shared database connected for status/trades")
            except Exception as e:  # noqa: BLE001
                logger.warning("Supabase unavailable (%s) — status falls back to .env", e)

        # Runtime state
        self.rate_limiter = RateLimiter(RATE_LIMIT_PER_MIN)
        self._pending_pin = {}      # chat_id -> {"action": fn, "desc": str, "expires": float}
        self._pin_failures = defaultdict(int)
        self._locked_until = {}
        self._last_market_name = None
        self._last_trade_poll = datetime.now(IST)
        self._trade_poll_failures = 0
        self._conn_alerted = False
        self._bg_tasks = []

    # ------------------------------------------------------------------
    # Low-level plumbing
    # ------------------------------------------------------------------
    async def _reply(self, update, text):
        if update and update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    def _chat_id(self):
        load_dotenv(ENV_FILE, override=True)
        return os.getenv("TELEGRAM_CHAT_ID", "").strip()

    def _log_command(self, update, command, ok=True, note=None):
        """Log every command + PIN attempt to stdout + DB (audit trail)."""
        user = update.effective_user if update else None
        chat = update.effective_chat if update else None
        user_id = user.id if user else None
        username = f"@{user.username}" if user and user.username else (user.first_name if user else "?")
        ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        line = (f"[{ts}] user={user_id} ({username}) chat={chat.id if chat else '?'} "
                f"cmd={command} ok={ok}" + (f" note={note}" if note else ""))
        logger.info(line)
        print(f"[telegram-audit] {line}", flush=True)

        if self.supabase:
            try:
                self.supabase.table(DB_COMMANDS_TABLE).insert({
                    "chat_id": str(chat.id) if chat else None,
                    "user_id": str(user_id) if user_id else None,
                    "username": str(username),
                    "command": command,
                    "note": note,
                    "created_at": datetime.now(IST).isoformat(),
                }).execute()
            except Exception:  # noqa: BLE001  (table may not exist yet)
                pass

    async def _gate(self, update, command):
        """Rate limiting + PIN lockout. Returns False when the command is dropped."""
        user = update.effective_user
        key = str(user.id if user else (update.effective_chat.id if update.effective_chat else "anon"))

        if self._locked_until.get(key, 0) > time.time():
            await self._reply(update, "🔒 Too many attempts. Try again in 5 mins")
            self._log_command(update, command, ok=False, note="locked_out")
            return False
        if not self.rate_limiter.allow(key):
            await self._reply(update, f"⚠️ Rate limit: max {RATE_LIMIT_PER_MIN} commands/minute. Slow down.")
            self._log_command(update, command, ok=False, note="rate_limited")
            return False
        return True

    # ------------------------------------------------------------------
    # PIN handling
    # ------------------------------------------------------------------
    def _verify_pin(self, pin):
        if self.kronos:
            return self.kronos.verify_pin(pin)
        return str(pin) == str(os.getenv("PIN", DEFAULT_PIN))

    async def _check_pin(self, update, pin):
        """Validate a PIN; counts wrong attempts (3 = 5-minute lockout)."""
        ok = self._verify_pin(pin)
        key = str(update.effective_user.id if update.effective_user else "anon")
        if not ok:
            self._pin_failures[key] += 1
            self._log_command(update, "pin_attempt", ok=False, note=f"invalid_pin ({self._pin_failures[key]}/{MAX_PIN_ATTEMPTS})")
            if self._pin_failures[key] >= MAX_PIN_ATTEMPTS:
                self._locked_until[key] = time.time() + PIN_LOCKOUT_SECONDS
                self._pin_failures[key] = 0
                await self._reply(update, "🔒 Too many attempts. Try again in 5 mins")
            else:
                left = MAX_PIN_ATTEMPTS - self._pin_failures[key]
                await self._reply(update, f"❌ Wrong PIN. ({left} attempts left)")
        else:
            self._pin_failures[key] = 0
            self._log_command(update, "pin_attempt", ok=True, note="pin_ok")
        return ok

    async def _need_pin(self, update, desc, action):
        """No PIN in the command — store a pending action and prompt for it."""
        chat_id = update.effective_chat.id
        self._pending_pin[chat_id] = {
            "action": action,
            "desc": desc,
            "expires": time.time() + 120,
        }
        await self._reply(update, f"🔐 Enter PIN to {desc}")

    # ------------------------------------------------------------------
    # PIN-protected mutations (through KronosBot or .env fallback)
    # ------------------------------------------------------------------
    def _set_mode(self, demo, pin):
        if self.kronos:
            return self.kronos.set_demo_mode(demo, pin)
        if not self._verify_pin(pin):
            return False, "Invalid PIN"
        _save_setting("DEMO_MODE", "true" if demo else "false")
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        return True, f"Mode set to {'DEMO' if demo else 'REAL'}"

    def _set_token(self, token, pin):
        if self.kronos:
            return self.kronos.update_olymp_token(token, pin)
        if not self._verify_pin(pin):
            return False, "Invalid PIN"
        if not token or not str(token).strip():
            return False, "Empty token"
        _save_setting("OLYMP_TOKEN", str(token).strip())
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        return True, "Token updated"

    def _set_force_stop(self, stop, pin):
        if self.kronos:
            return self.kronos.set_force_stop(stop, pin)
        if not self._verify_pin(pin):
            return False, "Invalid PIN"
        _save_setting("FORCE_STOP", "true" if stop else "false")
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        # Resume must also clear the DB pause state (PAUSED_AFTER_LOSS /
        # PAUSED_MANUAL) or the bot keeps skipping cycles after a loss.
        if not stop:
            set_bot_status("RUNNING", reason=None)
            resolve_alerts()
            insert_log("RESUME_TELEGRAM", "Trading resumed via Telegram (pause cleared)")
        return True, f"Force stop {'enabled' if stop else 'cleared'}"

    def _set_target(self, enabled, amount=None, pin=None):
        if not self._verify_pin(pin):
            return False, "Invalid PIN"
        if amount is not None:
            _save_setting("DAILY_TARGET", str(amount))
        _save_setting("DAILY_TARGET_ENABLED", "true" if enabled else "false")
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        if amount is None:
            amount = _to_float(get_setting("DAILY_TARGET", os.getenv("DAILY_TARGET", "100")), 100.0)
        if self.kronos:
            try:
                self.kronos.set_daily_target_enabled(enabled, pin)
            except Exception:  # noqa: BLE001
                pass
        return True, f"Daily target {'enabled' if enabled else 'disabled'} (target: ${amount})"

    # ------------------------------------------------------------------
    # Shared state readers (DB first, .env fallback)
    # ------------------------------------------------------------------
    def _db_status_row(self):
        if not self.supabase:
            return None
        try:
            res = (self.supabase.table(DB_STATUS_TABLE)
                   .select("*").order("id", desc=True).limit(1).execute())
            return (res.data or [None])[0]
        except Exception:  # noqa: BLE001
            return None

    def _today_start(self):
        return datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)

    def trades_today(self):
        """Number of trades recorded today in the shared trades table."""
        try:
            return len(get_daily_trades())
        except Exception:  # noqa: BLE001
            return 0

    def _today_trades_rows(self):
        """All trade rows for today (for win/loss breakdown). Degrades to []."""
        try:
            return get_daily_trades()
        except Exception:  # noqa: BLE001
            return []

    def _parse_trade_ts(self, row):
        raw = row.get("timestamp") or row.get("created_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    def _new_trades_since(self, since_dt):
        if self._trade_poll_failures >= 3:
            return []
        try:
            rows = get_daily_trades()
            out = []
            for row in rows:
                ts = self._parse_trade_ts(row)
                if ts is None:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=since_dt.tzinfo)
                if ts >= since_dt:
                    out.append(row)
            self._trade_poll_failures = 0
            return out
        except Exception:  # noqa: BLE001
            self._trade_poll_failures += 1
            if self._trade_poll_failures == 3:
                logger.warning("Trades table unreachable 3x — disabling trade notifications")
            return []

    def _pnl_summary(self):
        """(pnl, trades, wins, losses) from the shared trades table."""
        try:
            pnl = get_daily_pnl()
            _, wins, losses = get_win_rate()
            trades_today = len(get_daily_trades())
            return pnl, trades_today, wins, losses
        except Exception:  # noqa: BLE001
            return 0.0, 0, 0, 0

    # ------------------------------------------------------------------
    # Market info helpers
    # ------------------------------------------------------------------
    def _next_market_info(self):
        """(next_market, suffix) — suffix notes wrap-around to tomorrow."""
        market = get_scheduled_market()
        if market:
            idx = MARKET_SCHEDULE.index(market)
            nxt = MARKET_SCHEDULE[(idx + 1) % len(MARKET_SCHEDULE)]
            return nxt, ("" if idx + 1 < len(MARKET_SCHEDULE) else " (tomorrow)")
        t = datetime.now(IST).strftime("%H:%M")
        for m in MARKET_SCHEDULE:
            if m["start_time"] > t:
                return m, ""
        return MARKET_SCHEDULE[0], " (tomorrow)"

    def build_status_text(self):
        """/status body — market, P&L, trades, next switch, mode (+ flags)."""
        load_dotenv(ENV_FILE, override=True)

        # Weekend check first — bot is paused Sat/Sun regardless of the schedule
        if not is_trading_day():
            day_name = datetime.now(IST).strftime("%A")
            return ("🔴 <b>PAUSED (WEEKEND)</b>\n"
                    f"Today: {day_name}\n\n"
                    "🛑 No trading Sat/Sun (low liquidity &amp; noise).\n"
                    "Trades resume <b>Monday 09:00 IST</b>")

        market = get_scheduled_market()
        market_str = (f"{market['symbol']} ({market['start_time']} - {market['end_time']})"
                      if market else "Closed (outside market hours)")
        pnl, trades_today, _, _ = self._pnl_summary()

        mode = ("DEMO" if _env_bool("DEMO_MODE", True) else "REAL")
        row = self._db_status_row() or {}
        mode = row.get("mode") or mode

        nxt, suffix = self._next_market_info()
        # Spec format: "Next Switch: GBPUSD at 11:00" — the next market's
        # symbol at the time trading moves to it (current window's end when
        # open; the next window's start when closed).
        switch_symbol = nxt["symbol"]
        switch_time = market["end_time"] if market else nxt["start_time"]

        lines = [
            "📊 <b>Current Market:</b> " + html.escape(market_str),
            f"💰 <b>Daily P&amp;L:</b> {format_pnl(pnl)}",
            f"🔁 <b>Trades Today:</b> {trades_today}",
            f"⏭ <b>Next Switch:</b> {html.escape(str(switch_symbol))} at {switch_time}{suffix}",
            f"🎛 <b>Mode:</b> {html.escape(str(mode))}",
        ]
        if _env_bool("FORCE_STOP", False):
            lines.append("🛑 <b>Trading halted</b> (force stop active — /stop to resume)")
        if _env_bool("DAILY_TARGET_ENABLED", False):
            target = _to_float(os.getenv("DAILY_TARGET", "100"), 100.0)
            lines.append(f"🎯 <b>Daily Target:</b> ${target:g} "
                         f"({'✅ reached' if pnl >= target else 'in progress'})")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @safe_handler
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/start"):
            return
        chat_id = update.effective_chat.id
        _save_setting("TELEGRAM_CHAT_ID", str(chat_id))
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        user = update.effective_user
        name = html.escape(user.first_name) if user else "trader"
        await self._reply(
            update,
            f"👋 Welcome, {name}!\n\n"
            "🤖 <b>Kronos Trading Bot</b> is now connected to this chat.\n"
            "You will receive auto status every 5 minutes and a notification for every trade.\n\n"
            "Type /help to see all commands.",
        )
        self._log_command(update, "/start", ok=True, note=f"chat_id={chat_id} saved")

    @safe_handler
    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop (or resume) trading — /forcestop keeps it stopped until /stop."""
        if not await self._gate(update, "/stop"):
            return
        load_dotenv(ENV_FILE, override=True)
        currently_stopped = _env_bool("FORCE_STOP", False)
        resume = currently_stopped  # /stop doubles as resume when already stopped

        async def _apply(pin):
            if not await self._check_pin(update, pin):
                return
            ok, _ = self._set_force_stop(not resume, pin)
            if ok:
                if resume:
                    await self._reply(update, "▶️ Trading <b>resumed</b>.\nThe bot will trade the scheduled markets again.")
                else:
                    await self._reply(update, "🛑 Trading <b>stopped</b>.\nNo new trades will be placed.\n"
                                              "Send /stop again to resume.")
            self._log_command(update, "/stop", ok=ok, note="resume" if resume else "stop")

        args = context.args or []
        if args:
            await _apply(args[-1].strip())
        else:
            await self._need_pin(update, "stop/resume trading", _apply)

    @safe_handler
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/status"):
            return
        await self._reply(update, self.build_status_text())
        self._log_command(update, "/status")

    @safe_handler
    async def cmd_demo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/demo"):
            return

        async def _apply(pin):
            if not await self._check_pin(update, pin):
                return
            ok, _ = self._set_mode(True, pin)
            if ok:
                await self._reply(update, "✅ Switched to <b>DEMO</b> mode\nAll trades will use the demo account")
            self._log_command(update, "/demo", ok=ok)

        args = context.args or []
        if args:
            await _apply(args[-1].strip())
        else:
            await self._need_pin(update, "switch to DEMO", _apply)

    @safe_handler
    async def cmd_real(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/real"):
            return

        async def _apply(pin):
            if not await self._check_pin(update, pin):
                return
            ok, _ = self._set_mode(False, pin)
            if ok:
                await self._reply(update, "✅ Switched to <b>REAL</b> mode\n⚠️ All trades will use real money — trade carefully!")
            self._log_command(update, "/real", ok=ok)

        args = context.args or []
        if args:
            await _apply(args[-1].strip())
        else:
            await self._need_pin(update, "switch to REAL", _apply)

    @safe_handler
    async def cmd_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/token"):
            return
        args = context.args or []
        if not args:
            await self._reply(update, "Usage: <code>/token NEWTOKEN 2003</code>")
            return
        if len(args) >= 2:
            new_token, pin = " ".join(args[:-1]), args[-1]
        else:
            new_token, pin = args[0], None

        async def _apply(p):
            if not await self._check_pin(update, p):
                return
            ok, _ = self._set_token(new_token, p)
            if ok:
                await self._reply(update, "✅ ken updated!")
                self._bg_verify_token(new_token.strip())
            self._log_command(update, "/token ***", ok=ok)  # never log the token

        if pin:
            await _apply(pin)
        else:
            await self._need_pin(update, "update the Olymp token", _apply)

    def _bg_verify_token(self, token):
        """Best-effort Olymp connectivity check after a token update (fire & forget)."""
        async def _run():
            try:
                from olymptrade_ws import OlympTradeClient
                client = OlympTradeClient(access_token=token, log_raw_messages=False)
                await asyncio.wait_for(client.start(), timeout=15)
                try:
                    candles = await asyncio.wait_for(
                        client.market.get_candles("EURUSD", size=60, count=5), timeout=15)
                finally:
                    await client.stop()
                chat_id = self._chat_id()
                if chat_id:
                    msg = ("✅ Connected to Olymp Trade" if candles
                           else "⚠️ Connected, but no candles received — check the token.")
                    await app_bot().send_message(chat_id, msg)
            except ImportError:
                pass  # olymptrade_ws not installed — skip verification silently
            except Exception as e:  # noqa: BLE001
                logger.warning("Token verification failed: %s", e)
                try:
                    chat_id = self._chat_id()
                    if chat_id:
                        await app_bot().send_message(chat_id, f"⚠️ Could not connect to Olymp Trade: {html.escape(str(e))}")
                except Exception:  # noqa: BLE001
                    pass

        task = app_task(_run())
        if task:
            self._bg_tasks.append(task)

    @safe_handler
    async def cmd_forcestop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/forcestop"):
            return

        async def _apply(pin):
            if not await self._check_pin(update, pin):
                return
            ok, _ = self._set_force_stop(True, pin)
            if ok:
                await self._reply(update, "⛔ Bot stopped!")
            self._log_command(update, "/forcestop", ok=ok)

        args = context.args or []
        if args:
            await _apply(args[-1].strip())
        else:
            await self._need_pin(update, "emergency-stop trading", _apply)

    @safe_handler
    async def cmd_target(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/target"):
            return
        args = context.args or []
        if not args or args[0].lower() not in ("on", "off"):
            await self._reply(update, "Usage:\n<code>/target on 100 2003</code>\n<code>/target off 2003</code>")
            return

        enabling = args[0].lower() == "on"
        rest = args[1:]
        pin = None
        amount = None
        if enabling:
            if not rest:
                await self._reply(update, "Usage: <code>/target on 100 2003</code>")
                return
            amount = _to_float(rest[0], None)
            if amount is None or amount <= 0:
                await self._reply(update, "⚠️ Invalid amount. Example: <code>/target on 100 2003</code>")
                return
            pin = rest[-1] if len(rest) >= 2 else None
        else:
            pin = rest[-1] if rest else None

        async def _apply(p):
            if not await self._check_pin(update, p):
                return
            ok, _ = self._set_target(enabling, amount, p)
            if ok:
                if enabling:
                    await self._reply(update, f"✅ Target enabled! Will stop at +${amount:g}")
                else:
                    await self._reply(update, "✅ Target disabled")
            self._log_command(update, f"/target {'on' if enabling else 'off'}", ok=ok)

        if pin:
            await _apply(pin)
        else:
            desc = f"{'enable' if enabling else 'disable'} the daily target"
            await self._need_pin(update, desc, _apply)

    @safe_handler
    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/pnl"):
            return
        pnl, trades, wins, losses = self._pnl_summary()
        win_rate = f"{(wins / (wins + losses) * 100):.0f}%" if (wins + losses) else "N/A"
        text = (f"💰 <b>Today's P&amp;L:</b> {format_pnl(pnl)}\n"
                f"🔁 <b>Trades:</b> {trades} ({wins} win, {losses} loss)\n"
                f"🎯 <b>Win Rate:</b> {win_rate}")
        if _env_bool("DAILY_TARGET_ENABLED", False):
            target = _to_float(os.getenv("DAILY_TARGET", "100"), 100.0)
            remaining = target - pnl
            text += f"\n🎯 <b>Target:</b> ${target:g} — " + \
                    ("✅ reached" if remaining <= 0 else f"{format_pnl(remaining)} to go")
        await self._reply(update, ("📈" if pnl >= 0 else "📉") + "\n" + text)
        self._log_command(update, "/pnl")

    @safe_handler
    async def cmd_market(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/market"):
            return
        market = get_scheduled_market()
        nxt, suffix = self._next_market_info()
        if market:
            text = (f"📊 <b>Current:</b> {html.escape(market['symbol'])} "
                    f"({market['start_time']} - {market['end_time']})\n"
                    f"⏳ <b>Remaining:</b> {_fmt_remaining(market['end_time'])}\n"
                    f"⏭ <b>Next:</b> {nxt['symbol']} at {nxt['end_time']}{suffix}")
        else:
            text = (f"🔴 <b>Market Closed</b>\n"
                    f"No scheduled market right now.\n"
                    f"⏭ <b>Next:</b> {nxt['symbol']} ({html.escape(nxt['name'])}) at {nxt['start_time']}{suffix}")
        await self._reply(update, text)
        self._log_command(update, "/market")

    @safe_handler
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/help"):
            return
        await self._reply(
            update,
            "🤖 <b>Kronos Trading Bot — Commands</b>\n\n"
            "/start — connect this chat &amp; receive updates\n"
            "/status — market, P&amp;L, trades, next switch, mode\n"
            "/pnl — today's profit/loss &amp; win rate\n"
            "/market — current market &amp; time remaining\n"
            "/help — show this message\n\n"
            "🔐 <b>PIN-protected</b> (append PIN or send it when asked):\n"
            "/demo — switch to DEMO mode\n"
            "/real — switch to REAL mode\n"
            "/token &lt;new_token&gt; &lt;pin&gt; — update Olymp token\n"
            "/forcestop — emergency stop all trading\n"
            "/target on &lt;amount&gt; &lt;pin&gt; — enable daily target\n"
            "/target off &lt;pin&gt; — disable daily target\n"
            "/stop — stop (or resume) trading\n"
            "/restart — restart the trading bot\n\n"
            "⏱ Auto status every 5 minutes; trade &amp; market-switch notifications.",
        )
        self._log_command(update, "/help")

    @safe_handler
    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._gate(update, "/restart"):
            return

        async def _apply(pin):
            if not await self._check_pin(update, pin):
                return
            await self._reply(update, "🔄 Bot restarting...")
            ok = self._restart_bot_py()
            self._log_command(update, "/restart", ok=ok,
                              note="bot.py respawned" if ok else "respawn skipped (already parent)")
            if not ok:
                await self._reply(update, "✅ Restart signal sent — bot.py will pick it up on its next cycle.")

        args = context.args or []
        if args:
            await _apply(args[-1].strip())
        else:
            await self._need_pin(update, "restart the trading bot", _apply)

    def _restart_bot_py(self):
        """
        Restart bot.py.

        * When this process IS the bot.py parent (bot.py spawned us via a
          restart wrapper), no respawn is needed — bot.py reloads .env every
          cycle, so the signal alone takes effect.
        * Otherwise spawn a fresh `python bot.py` detached with nohup so it
          survives this process. An existing bot.py process keeps running;
          kill it manually first if you want a hard restart.
        """
        try:
            if os.path.isfile(os.path.join(PROJECT_ROOT, ".restarted_by_tg")):
                return False  # we were spawned by bot.py's restart — nothing to do
            if sys.platform == "win32":
                subprocess.Popen(  # noqa: S603
                    [sys.executable, BOT_PY_PATH],
                    cwd=PROJECT_ROOT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(  # noqa: S603
                    ["nohup", sys.executable, BOT_PY_PATH],
                    cwd=PROJECT_ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Restart failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Free-text: pending PIN answers
    # ------------------------------------------------------------------
    @safe_handler
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        pending = self._pending_pin.get(chat_id)
        if not pending:
            await self._reply(update, "🤖 Unknown input. Type /help to see available commands.")
            return
        if time.time() > pending["expires"]:
            del self._pending_pin[chat_id]
            await self._reply(update, "⌛ PIN prompt expired. Run the command again.")
            return
        del self._pending_pin[chat_id]
        await pending["action"](update.message.text.strip())

    # ------------------------------------------------------------------
    # Background loops (auto status, market switch, trade notifications)
    # ------------------------------------------------------------------
    async def _auto_status_loop(self, app):
        """Send a clean status update every 5 minutes + detect connection loss."""
        await asyncio.sleep(15)  # let startup settle
        while True:
            try:
                chat_id = self._chat_id()
                if chat_id:
                    await app.bot.send_message(
                        chat_id, "⏱ <b>Auto status</b>\n" + self.build_status_text(),
                        parse_mode=ParseMode.HTML)
                    if self._conn_alerted:
                        self._conn_alerted = False
                        logger.info("Connection restored")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Auto status failed: %s", e)
                if not self._conn_alerted:
                    self._conn_alerted = True
                    try:
                        chat_id = self._chat_id()
                        if chat_id:
                            await app.bot.send_message(chat_id, "📡 Connection lost. Reconnecting...")
                    except Exception:  # noqa: BLE001
                        pass
            await asyncio.sleep(STATUS_INTERVAL)

    async def _market_switch_loop(self, app):
        """Watch the schedule and notify whenever the active market changes."""
        await asyncio.sleep(20)
        while True:
            try:
                # Weekend pause notice (once per Sat/Sun, no "Bot online" spam)
                chat_id = self._chat_id()
                if chat_id and not is_trading_day():
                    day_name = datetime.now(IST).strftime("%A")
                    if self._last_market_name != f"WEEKEND:{day_name}":
                        await app.bot.send_message(
                            chat_id,
                            f"🛑 <b>Weekend pause</b>\nToday: {day_name}\n"
                            "No trading Sat/Sun. Trades resume Monday 09:00 IST",
                            parse_mode=ParseMode.HTML)
                        self._last_market_name = f"WEEKEND:{day_name}"
                    await asyncio.sleep(MARKET_WATCH_INTERVAL)
                    continue

                market = get_scheduled_market()
                new_name = market["name"] if market else None
                if new_name != self._last_market_name:
                    if self._last_market_name is not None:  # skip the initial state
                        chat_id = self._chat_id()
                        if chat_id:
                            if market:
                                msg = (f"🔄 <b>Market switch</b>\n"
                                       f"📊 {html.escape(market['symbol'])} "
                                       f"({market['start_time']} - {market['end_time']}) — "
                                       f"{html.escape(market['description'])}")
                            else:
                                nxt, _ = self._next_market_info()
                                msg = (f"🔄 <b>Market closed</b>\n"
                                       f"No scheduled market right now.\n"
                                       f"⏭ Next: {nxt['symbol']} at {nxt['start_time']}")
                            await app.bot.send_message(chat_id, msg, parse_mode=ParseMode.HTML)
                    self._last_market_name = new_name
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Market switch check failed: %s", e)
            await asyncio.sleep(MARKET_WATCH_INTERVAL)

    async def _trade_notify_loop(self, app):
        """Poll the shared trades table and notify on each new trade."""
        await asyncio.sleep(10)
        while True:
            try:
                chat_id = self._chat_id()
                if chat_id:
                    trades = self._new_trades_since(self._last_trade_poll)
                    for t in trades:
                        await app.bot.send_message(chat_id, self._format_trade(t),
                                                   parse_mode=ParseMode.HTML)
                        self._log_command(None, "trade_notification", ok=True,
                                          note=str(t.get("id", "")))
                    if trades:
                        self._last_trade_poll = datetime.now(IST)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("Trade notification failed: %s", e)
            await asyncio.sleep(TRADE_POLL_INTERVAL)

    @staticmethod
    def _format_trade(t):
        """Format a trades-table row defensively (column names may vary)."""
        def pick(*keys, default="?"):
            for k in keys:
                if k in t and t[k] not in (None, ""):
                    return t[k]
            return default

        symbol = pick("symbol", "market")
        direction = str(pick("signal", "direction", "side", "action")).upper()
        amount = pick("amount", "stake", "investment")
        pnl = pick("pnl", "profit", "result", "pnl_amount", default=None)
        mode = pick("mode", default="")
        ts = pick("created_at", "time", "timestamp", default="")
        result = format_pnl(pnl) if pnl is not None else "pending"
        emoji = "✅" if (pnl is not None and _to_float(pnl) >= 0) else "❌"
        return (f"{emoji} <b>Trade Executed</b>\n"
                f"📊 {html.escape(str(symbol))} — <b>{html.escape(direction)}</b>\n"
                f"💵 Amount: {html.escape(str(amount))}\n"
                f"💰 Result: {result}"
                + (f"\n🎛 Mode: {html.escape(str(mode))}" if mode else "")
                + (f"\n🕐 {html.escape(str(ts))}" if ts else ""))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def _post_init(self, app):
        # Use the asyncio loop directly (Application.create_task warns when
        # called before the app is running, and the job-queue extra is not installed)
        loop = asyncio.get_running_loop()
        self._bg_tasks = [
            loop.create_task(self._auto_status_loop(app), name="auto-status"),
            loop.create_task(self._market_switch_loop(app), name="market-switch"),
            loop.create_task(self._trade_notify_loop(app), name="trade-notify"),
        ]
        chat_id = self._chat_id()
        if chat_id:
            try:
                await app.bot.send_message(chat_id, "🟢 Kronos Telegram Bot is online. Type /help.")
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not send online notice: %s", e)

    async def _post_shutdown(self, app):
        for task in self._bg_tasks:
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        logger.info("Kronos Telegram Bot shut down")

    def register_handlers(self, app):
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("demo", self.cmd_demo))
        app.add_handler(CommandHandler("real", self.cmd_real))
        app.add_handler(CommandHandler("token", self.cmd_token))
        app.add_handler(CommandHandler("forcestop", self.cmd_forcestop))
        app.add_handler(CommandHandler("target", self.cmd_target))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        app.add_handler(CommandHandler("market", self.cmd_market))
        app.add_handler(CommandHandler("restart", self.cmd_restart))
        # Free text (PIN answers) — only when no command
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))


# ---------------------------------------------------------------------------
# Event-loop helpers (used by fire-and-forget background work)
# ---------------------------------------------------------------------------
def app_bot():
    """The Telegram Application's Bot object, or None outside the loop."""
    app = getattr(app_bot, "_app", None)
    return app.bot if app else None


def app_task(coro):
    """Schedule a coroutine on the running loop; None when there is no loop."""
    try:
        return asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    load_dotenv(ENV_FILE)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN missing in .env — cannot start Telegram bot.")
        sys.exit(1)

    print("=" * 60)
    print("Kronos Telegram Bot starting (polling mode)")
    print(f"Auto status every {STATUS_INTERVAL}s | Trade poll every {TRADE_POLL_INTERVAL}s")
    print(f"Rate limit: {RATE_LIMIT_PER_MIN} cmds/min | Audit logging: stdout + Supabase")
    print("=" * 60)

    tg_bot = KronosTelegramBot()
    app = (
        Application.builder()
        .token(token)
        .post_init(tg_bot._post_init)
        .post_shutdown(tg_bot._post_shutdown)
        .build()
    )
    app_bot._app = app  # used by _bg_verify_token's fire-and-forget replies
    tg_bot.register_handlers(app)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
