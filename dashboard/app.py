#!/usr/bin/env python3
"""
Kronos Web Dashboard
====================

Flask dashboard for monitoring + controlling the Kronos Trading Bot.

  * GET  /               main dashboard page
  * GET  /settings       settings page (view + edit .env settings, PIN-protected)
  * GET  /api/status     live stats (market, signal, confidence, P&L, mode, ...)
  * GET  /api/trades     last 20 trades (parsed from the bot's log; DB later)
  * GET  /api/logs       last 100 log lines (color-coded client-side)
  * GET  /api/markets    market schedule with current/next info
  * POST /api/command    control commands (start/stop/demo/real/forcestop/
                         set_token/set_target) — PIN-protected

Data sources:
  * Supabase (trades, logs, settings) — live P&L, predictions, bot status
  * .env — DEMO_MODE, FORCE_STOP, TRADE_AMOUNT, OLYMP_TOKEN, ...
  * logs/kronos_bot.log — fallback when the database is unreachable

Run:  python3 dashboard/app.py   ->  http://localhost:5000
"""

import os
import re
import sys
from collections import Counter, deque
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request
import json

from dotenv import load_dotenv, set_key

# Make sure the project root is importable (config/market_schedule.py)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.market_schedule import (
    IST,
    MARKET_SCHEDULE,
    get_current_market as get_scheduled_market,
)

# --- Shared Supabase database (single source of truth) ---------------------
from database.db import (
    get_daily_trades,
    get_daily_pnl,
    get_win_rate,
    get_recent_logs,
    get_trade_count_today,
    get_current_market,
    db_healthy,
    get_bot_status,
    set_bot_status,
    resolve_alerts,
    get_setting,
    set_setting,
    insert_log,
)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
DEFAULT_PIN = "2003"

LOG_TAIL_RETURN = 100        # /api/logs returns last 100 entries
TRADES_RETURN = 20           # /api/trades returns last 20 trades

# Auto-refresh intervals surfaced to the frontend (stats 5s, trades 10s, logs 2s)
STATS_REFRESH_SECONDS = 5
TRADES_REFRESH_SECONDS = 10
LOGS_REFRESH_SECONDS = 2


def _db_print(message):
    """Console logging for DB reads/errors."""
    print(f"[dashboard] [{datetime.now(IST).strftime('%H:%M:%S')}] {message}", flush=True)

MAX_PIN_ATTEMPTS = 3         # wrong attempts before lockout
PIN_LOCKOUT_SECONDS = 300    # 5 minutes

# Settings editable from the settings page (synced to Supabase database)
KRONOS_CONFIDENCE_THRESHOLD = 0.75  # fixed: trade only when confidence > 0.75
EDITABLE_SETTINGS = [
    ("KRONOS_API_URL", "Kronos API URL", "text"),
    ("TRADE_AMOUNT", "Trade amount (Olymp fallback)", "number"),
    ("LOOP_INTERVAL", "Loop interval (seconds)", "number"),
    ("DAILY_TARGET", "Daily target ($)", "number"),
    ("TRADE_DURATION", "Trade duration (seconds)", "number"),
]
READONLY_SETTINGS = [
    ("DEMO_MODE", "Current mode"),
    ("DAILY_TARGET_ENABLED", "Daily target"),
    ("FORCE_STOP", "Force stop"),
    ("AUTO_EXECUTE_TRADES", "Auto-execute trades"),
]

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ---------------------------------------------------------------------------
# In-memory state (single-process Flask)
# ---------------------------------------------------------------------------
_pin_failures = {}    # key -> count
_locked_until = {}    # key -> timestamp
_bot_running = True   # dashboard "START/STOP" switch
_trades_cache = deque(maxlen=50)


def _save_setting(key, value):
    """Save setting to memory, Supabase DB settings table, and .env if present."""
    os.environ[key] = str(value)
    try:
        set_setting(key, str(value))
    except Exception as e:
        print(f"Database error saving setting {key}: {e}")
    if os.path.exists(ENV_FILE):
        try:
            set_key(ENV_FILE, key, str(value))
        except Exception:
            pass


def _audit(action, ok=True, note=None):
    """Console + Supabase audit log for commands and actions."""
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    ip = request.remote_addr if request else "?"
    line = f"[{ts}] ip={ip} action={action} ok={ok}" + (f" note={note}" if note else "")
    print(f"[dashboard-audit] {line}", flush=True)
    try:
        insert_log("DASHBOARD", f"{action} (ok={ok})", {"ip": ip, "note": note})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, default=0.0):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


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


def _next_market_info():
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




def _client_key():
    return request.remote_addr or "anon"


# ---------------------------------------------------------------------------
# PIN protection (3 wrong attempts = 5-minute lockout, per spec)
# ---------------------------------------------------------------------------
def verify_pin(pin):
    """Check the dashboard PIN (default '2003', overridable via PIN in .env)."""
    return str(pin or "").strip() == str(os.getenv("PIN", DEFAULT_PIN))


def _pin_locked():
    return _locked_until.get(_client_key(), 0) > datetime.now().timestamp()


def _pin_remaining_seconds():
    return max(0, int(_locked_until.get(_client_key(), 0) - datetime.now().timestamp()))


def _register_pin_result(ok):
    """Track failures per client IP; returns (allowed, attempts_left_or_none)."""
    key = _client_key()
    if ok:
        _pin_failures[key] = 0
        return True, None
    _pin_failures[key] = _pin_failures.get(key, 0) + 1
    if _pin_failures[key] >= MAX_PIN_ATTEMPTS:
        _locked_until[key] = datetime.now().timestamp() + PIN_LOCKOUT_SECONDS
        _pin_failures[key] = 0
        _audit("pin_lockout", ok=False, note=f"locked for {PIN_LOCKOUT_SECONDS}s")
        return False, 0
    return True, MAX_PIN_ATTEMPTS - _pin_failures[key]


def _parse_details(raw):
    """PREDICTION log details may be a JSON string or an already-decoded dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def get_status():
    """
    Live status. The Supabase database is the single source of truth:
      * P&L / trade count / win rate -> trades table (via database.db)
      * latest signal + confidence   -> newest PREDICTION event in logs table
      * current market               -> config/market_schedule.py
    """
    if os.path.exists(ENV_FILE):
        load_dotenv(ENV_FILE, override=True)

    market = get_current_market()
    nxt, suffix = _next_market_info()

    signal, confidence, pnl = "HOLD", 0.0, 0.0
    last_signal_time = None
    wins = losses = 0
    win_rate = 0.0
    trades_today = 0

    try:
        db_ok = db_healthy()
    except Exception as e:  # noqa: BLE001
        print(f"Database error (health): {e}")
        db_ok = False

    if db_ok:
        _db_print("reading live data from Supabase")
        try:
            pnl = get_daily_pnl()
            trades_today = get_trade_count_today()
            win_rate, wins, losses = get_win_rate()
        except Exception as e:  # noqa: BLE001
            print(f"Database error (stats): {e}")
        try:
            # newest PREDICTION event carries the last signal + confidence
            for row in reversed(get_recent_logs(30)):
                if row.get("event") == "PREDICTION":
                    d = _parse_details(row.get("details"))
                    signal = str(d.get("signal") or d.get("prediction") or "HOLD").upper()
                    try:
                        confidence = round(float(d.get("confidence") or 0) * 100, 1)
                    except (TypeError, ValueError):
                        confidence = 0.0
                    last_signal_time = str(row.get("timestamp") or "")[:19].replace("T", " ")
                    break
        except Exception as e:  # noqa: BLE001
            print(f"Database error (signal): {e}")
    else:
        _db_print("DB unavailable")

    mode = "DEMO" if _env_bool("DEMO_MODE", True) else "REAL"
    force_stop = _env_bool("FORCE_STOP", False)
    target_enabled = _env_bool("DAILY_TARGET_ENABLED", False)
    daily_target = _env_float("DAILY_TARGET", 100.0)

    # Pause state lives in the DB settings table (source of truth for the bot)
    try:
        status_info = get_bot_status()
    except Exception:  # noqa: BLE001
        status_info = {"status": "RUNNING", "last_loss": None, "paused_at": None}
    bot_status = status_info.get("status") or "RUNNING"
    paused = bot_status in ("PAUSED_AFTER_LOSS", "PAUSED_MANUAL")

    if force_stop:
        signal = "HALTED"
    elif paused:
        signal = bot_status
    elif target_enabled and pnl >= daily_target:
        signal = "TARGET_REACHED"
    elif market is None:
        signal = "NO_MARKET"

    running = _bot_running and not force_stop

    return {
        "market": market["symbol"] if market else None,
        "market_name": market["name"] if market else None,
        "market_window": f"{market['start_time']} - {market['end_time']}" if market else None,
        "market_time_remaining": _fmt_remaining(market["end_time"]) if market else None,
        "signal": signal,
        "confidence": round(confidence, 1),
        "threshold": round(KRONOS_CONFIDENCE_THRESHOLD * 100, 1),
        "trade_amount": _env_float("TRADE_AMOUNT", 70.0),
        "pnl": round(pnl, 2),
        "pnl_formatted": format_pnl(pnl),
        "trades_today": trades_today,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "mode": mode,
        "running": running,
        "force_stop": force_stop,
        "bot_status": bot_status,
        "paused": paused,
        "last_loss_reason": status_info.get("last_loss"),
        "paused_at": status_info.get("paused_at"),
        "target_enabled": target_enabled,
        "daily_target": daily_target,
        "target_reached": target_enabled and pnl >= daily_target,
        "next_switch": f"{nxt['symbol']} at {nxt['start_time']}{suffix}",
        "next_switch_countdown": (
            _fmt_remaining(market["end_time"]) if market else None
        ),
        "last_signal_time": last_signal_time,
        "db_connected": db_ok,
        "updated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _format_trade_row(r):
    """Normalize a DB trades row into the dashboard's trade dict (spec format)."""
    return {
        "timestamp": str(r.get("timestamp") or "")[:19].replace("T", " "),
        "symbol": r.get("symbol") or "?",
        "signal": str(r.get("signal") or "?").upper(),
        "confidence": r.get("confidence"),
        "entry_price": r.get("entry_price"),
        "exit_price": r.get("exit_price"),
        "pnl": r.get("pnl"),
        "status": r.get("status") or "?",
    }


def get_trades():
    """Last 20 trades from the database (today, via get_daily_trades())."""
    try:
        if db_healthy():
            rows = get_daily_trades()
            _db_print(f"read {len(rows)} trades from database")
            return [_format_trade_row(r) for r in reversed(rows[-TRADES_RETURN:])]
    except Exception as e:  # noqa: BLE001
        print(f"Database error (trades): {e}")
    return []


def get_logs():
    """Last 100 log entries from the database, oldest first (auto-scroll ready)."""
    try:
        if db_healthy():
            rows = get_recent_logs(LOG_TAIL_RETURN)
            _db_print(f"read {len(rows)} log events from database")
            return [{"timestamp": str(r.get("timestamp") or "")[:19].replace("T", " "),
                     "event": r.get("event") or "INFO",
                     "message": r.get("message") or ""}
                    for r in rows]
    except Exception as e:  # noqa: BLE001
        print(f"Database error (logs): {e}")
    return []


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def dashboard():
    return render_template("index.html")


@app.route("/settings")
def settings_page():
    return render_template("settings.html")


# ---------------------------------------------------------------------------
# API: read routes
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    try:
        return jsonify(get_status())
    except Exception as e:  # noqa: BLE001
        print(f"Database error (api_status): {e}")
        _audit("api_status", ok=False, note=str(e))
        return jsonify({"error": "Loading data..."}), 503


@app.route("/api/trades")
def api_trades():
    try:
        return jsonify(get_trades())
    except Exception as e:  # noqa: BLE001
        print(f"Database error (api_trades): {e}")
        _audit("api_trades", ok=False, note=str(e))
        return jsonify({"error": "Loading data..."}), 503


@app.route("/api/logs")
def api_logs():
    try:
        return jsonify(get_logs())
    except Exception as e:  # noqa: BLE001
        print(f"Database error (api_logs): {e}")
        _audit("api_logs", ok=False, note=str(e))
        return jsonify({"error": "Loading data..."}), 503


@app.route("/api/markets")
def api_markets():
    try:
        current = get_scheduled_market()
        now = datetime.now(IST)
        t = now.strftime("%H:%M")
        nxt, _suffix = _next_market_info()
        markets = []
        for m in MARKET_SCHEDULE:
            active = current is not None and m["name"] == current["name"]
            markets.append({
                "name": m["name"],
                "symbol": m["symbol"],
                "start": m["start_time"],
                "end": m["end_time"],
                "description": m["description"],
                "active": active,
                "is_next": (not active) and m["name"] == nxt["name"],
                "countdown": _fmt_remaining(m["end_time"]) if active else None,
            })
        return jsonify({
            "markets": markets,
            "current": current["symbol"] if current else None,
            "next": nxt["symbol"],
            "next_start": nxt["start_time"],
            "next_suffix": _suffix,
            "countdown": _fmt_remaining(current["end_time"]) if current
                         else _until_start(nxt["start_time"]),
        })
    except Exception as e:  # noqa: BLE001
        _audit("api_markets", ok=False, note=str(e))
        return jsonify({"error": str(e)}), 500


def _until_start(start_time_str):
    """Time until 'HH:MM' IST — rolls over to tomorrow when it already passed."""
    try:
        now = datetime.now(IST)
        h, m = start_time_str.split(":")
        start = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if start <= now:
            start += timedelta(days=1)  # next occurrence is tomorrow
        secs = max(0, int((start - now).total_seconds()))
        return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"
    except (ValueError, AttributeError):
        return "?"


# ---------------------------------------------------------------------------
# API: control commands (PIN-protected where sensitive)
# ---------------------------------------------------------------------------
@app.route("/api/command", methods=["POST"])
def api_command():
    """
    Body: {"command": "start|stop|demo|real|forcestop|set_token|set_target",
           "pin": "2003", ...extra args}

    Sensitive commands (demo/real/forcestop/set_token/set_target/stop) need PIN.
    Returns {"success": true/false, "message": "..."}.
    """
    global _bot_running
    try:
        data = request.get_json(silent=True) or {}
        command = str(data.get("command", "")).strip().lower()
        pin = data.get("pin")

        load_dotenv(ENV_FILE, override=True)

        sensitive = {"stop", "demo", "real", "forcestop", "set_token", "set_target"}
        if command not in {"start", "stop", "demo", "real", "forcestop",
                           "set_token", "set_target"}:
            _audit(f"command:{command}", ok=False, note="unknown command")
            return jsonify({"success": False, "message": "Unknown command"}), 400

        if command in sensitive:
            if _pin_locked():
                _audit(f"command:{command}", ok=False, note="pin locked out")
                return jsonify({
                    "success": False,
                    "locked": True,
                    "message": f"🔒 Too many attempts. Try again in {max(1, _pin_remaining_seconds() // 60)} min",
                }), 429
            if not verify_pin(pin):
                allowed, left = _register_pin_result(False)
                _audit(f"command:{command}", ok=False, note=f"invalid pin ({left} left)" if left else "locked out")
                if not allowed:
                    return jsonify({
                        "success": False,
                        "locked": True,
                        "message": "🔒 Too many attempts. Try again in 5 mins",
                    }), 429
                return jsonify({
                    "success": False,
                    "message": f"❌ Wrong PIN. ({left} attempts left)",
                }), 401
            _register_pin_result(True)

        # ---- apply the command -------------------------------------------------
        # START clears FORCE_STOP AND any DB pause (PAUSED_AFTER_LOSS /
        # PAUSED_MANUAL) so trading resumes on the bot's next cycle.
        if command == "start":
            _save_setting("FORCE_STOP", "false")
            set_bot_status("RUNNING", reason=None)
            resolve_alerts()
            _bot_running = True
        elif command in ("stop", "forcestop"):
            _save_setting("FORCE_STOP", "true")
            set_bot_status("PAUSED_MANUAL", reason="Stopped from dashboard")
            _bot_running = False
        elif command == "demo":
            _save_setting("DEMO_MODE", "true")
        elif command == "real":
            _save_setting("DEMO_MODE", "false")
        elif command == "set_token":
            token = str(data.get("token", "")).strip()
            if not token:
                return jsonify({"success": False, "message": "Missing token"}), 400
            _save_setting("OLYMP_TOKEN", token)
        elif command == "set_target":
            amount = data.get("amount")
            enabled = bool(data.get("enabled", True))
            if amount is not None:
                try:
                    _save_setting("DAILY_TARGET", str(float(amount)))
                except (TypeError, ValueError):
                    return jsonify({"success": False, "message": "Invalid amount"}), 400
            _save_setting("DAILY_TARGET_ENABLED", "true" if enabled else "false")

        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        _audit(f"command:{command}", ok=True)

        messages = {
            "start": "▶️ Bot resumed — trading RUNNING",
            "stop": "🛑 Bot stopped",
            "demo": "✅ Switched to DEMO mode",
            "real": "✅ Switched to REAL mode",
            "forcestop": "⛔ Bot stopped!",
            "set_token": "✅ Token updated!",
            "set_target": "✅ Target enabled!" if data.get("enabled", True) else "✅ Target disabled",
        }
        return jsonify({"success": True, "message": messages.get(command, "✅ Action complete!")})

    except Exception as e:  # noqa: BLE001
        _audit("command", ok=False, note=str(e))
        return jsonify({"success": False, "message": "⚠️ Error processing command"}), 500


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------
@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    if os.path.exists(ENV_FILE):
        load_dotenv(ENV_FILE, override=True)
    return jsonify({
        "editable": [
            {"key": k, "label": label, "type": typ, "value": get_setting(k, os.getenv(k, ""))}
            for k, label, typ in EDITABLE_SETTINGS
        ],
        "readonly": [
            {"key": k, "label": label, "value": get_setting(k, os.getenv(k, ""))}
            for k, label in READONLY_SETTINGS
        ],
        "pin_locked": _pin_locked(),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    try:
        data = request.get_json(silent=True) or {}
        pin = data.get("pin")
        updates = data.get("updates") or {}

        if _pin_locked():
            return jsonify({"success": False, "locked": True,
                            "message": "🔒 Too many attempts. Try again later"}), 429
        if not verify_pin(pin):
            allowed, left = _register_pin_result(False)
            _audit("settings_update", ok=False, note="invalid pin" if left else "locked out")
            if not allowed:
                return jsonify({"success": False, "locked": True,
                                "message": "🔒 Too many attempts. Try again in 5 mins"}), 429
            return jsonify({"success": False,
                            "message": f"❌ Wrong PIN. ({left} attempts left)"}), 401
        _register_pin_result(True)

        if not updates:
            return jsonify({"success": False, "message": "No updates provided"}), 400

        applied = []
        for key, value in updates.items():
            if key not in {k for k, _, _ in EDITABLE_SETTINGS}:
                continue
            _save_setting(key, str(value))
            applied.append(key)
        if os.path.exists(ENV_FILE):
            load_dotenv(ENV_FILE, override=True)
        _audit("settings_update", ok=True, note=",".join(applied) or "none")
        return jsonify({"success": True, "message": "✅ Settings saved",
                        "applied": applied})
    except Exception as e:  # noqa: BLE001
        _audit("settings_update", ok=False, note=str(e))
        return jsonify({"success": False, "message": "⚠️ Error saving settings"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if os.path.exists(ENV_FILE):
        load_dotenv(ENV_FILE)
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1")
    print("=" * 60)
    print("Kronos Web Dashboard starting")
    print(f"Port: {port} | Host: 0.0.0.0 | Debug: {debug}")
    print(f"Auto-refresh: stats {STATS_REFRESH_SECONDS}s | "
          f"trades {TRADES_REFRESH_SECONDS}s | logs {LOGS_REFRESH_SECONDS}s")
    print("=" * 60)
    app.run(debug=debug, port=port, host="0.0.0.0", use_reloader=False)
