#!/usr/bin/env python3
"""
Kronos Shared Database Layer
============================

Single place where every component (bot.py, telegram_bot.py, dashboard)
talks to the shared Supabase tables:

  trades   — one row per trade (open -> closed with exit_price/pnl)
  logs     — bot events: START, PREDICTION, TRADE, MARKET_SWITCH, ERROR,
             FORCESTOP, TARGET_REACHED, PIN_ATTEMPT, PIN_WRONG, MODE, ...
  settings — key/value store (optional mirror of .env)

Design rules:
  * NEVER raises — a database outage can never crash the trading loop.
  * Graceful fallback: failures are printed once, then writes pause for
    DB_FAILURE_BACKOFF seconds after DB_MAX_CONSECUTIVE_FAILURES.
  * Connection is created lazily and re-created if it breaks.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

from dotenv import load_dotenv

# Make sure the project root is importable no matter where we launch from
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config.market_schedule import IST, get_current_market  # re-exported for spec imports

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")

DB_TRADES_TABLE = os.getenv("DB_TRADES_TABLE", "trades")
DB_LOGS_TABLE = os.getenv("DB_LOGS_TABLE", "logs")
DB_SETTINGS_TABLE = os.getenv("DB_SETTINGS_TABLE", "settings")
DB_ALERTS_TABLE = os.getenv("DB_ALERTS_TABLE", "alerts")

DB_MAX_CONSECUTIVE_FAILURES = 3   # failures before pausing DB writes
DB_FAILURE_BACKOFF = 300          # seconds to skip DB writes after that
DB_HEALTH_INTERVAL = 60           # seconds between reachability re-probes

# ---------------------------------------------------------------------------
# Client management (lazy, shared across the process)
# ---------------------------------------------------------------------------
_client = None
_failures = 0
_skip_until = 0.0
_last_probe = 0.0
_last_ok_ts = 0.0


def _get_client():
    """Lazily create (or re-create after repeated failures) the Supabase client."""
    global _client, _last_probe
    if _client is not None and time.time() - _last_probe < DB_HEALTH_INTERVAL:
        return _client
    _last_probe = time.time()

    load_dotenv(ENV_FILE, override=True)
    url = os.getenv("SUPABASE_URL", "").strip()
    key = (os.getenv("SUPABASE_SERVICE_KEY")
           or os.getenv("SUPABASE_SECRET_KEY")
           or os.getenv("SUPABASE_KEY") or "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception as e:  # noqa: BLE001
        print(f"Database error: {e}")
        return None


def reset_db_client():
    """Force a fresh connection on the next call (used after repeated failures)."""
    global _client
    _client = None


def _allowed():
    return _skip_until <= time.time()


def _mark_ok():
    global _failures, _skip_until, _last_ok_ts
    _failures = 0
    _skip_until = 0.0
    _last_ok_ts = time.time()


def _mark_failure(what, error):
    """Print the first failure, then back off after repeated consecutive ones."""
    global _failures, _skip_until
    _failures += 1
    if _failures == 1:
        print(f"Database error ({what}): {error}")
    if _failures >= DB_MAX_CONSECUTIVE_FAILURES:
        _skip_until = time.time() + DB_FAILURE_BACKOFF
        print(f"Database writes paused for {DB_FAILURE_BACKOFF}s "
              f"after {_failures} consecutive failures")
        _failures = 0
        reset_db_client()


# ---------------------------------------------------------------------------
# Spec'd API
# ---------------------------------------------------------------------------
def insert_trade(symbol, entry_price, signal, confidence,
                 exit_price=None, pnl=None, status="open", timestamp=None):
    """
    Insert a trade row. status='open' rows are closed later via close_trade().
    Returns the new row id, or None on failure (never raises).
    """
    if not _allowed():
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        row = {
            "symbol": symbol,
            "entry_price": float(entry_price) if entry_price is not None else None,
            "exit_price": float(exit_price) if exit_price is not None else None,
            "signal": signal,
            "confidence": round(float(confidence), 4),
            "pnl": round(float(pnl), 2) if pnl is not None else None,
            "status": status,
            "timestamp": (timestamp or datetime.now(IST)).isoformat(),
        }
        res = client.table(DB_TRADES_TABLE).insert(row).execute()
        _mark_ok()
        data = getattr(res, "data", None) or []
        return data[0].get("id") if data else None
    except Exception as e:  # noqa: BLE001
        _mark_failure("trade insert", e)
        return None


def close_trade(trade_id, exit_price=None, pnl=None):
    """Close an open trade row with exit_price and pnl. Never raises."""
    if not _allowed() or trade_id is None:
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        update = {"status": "closed"}
        if exit_price is not None:
            update["exit_price"] = float(exit_price)
        if pnl is not None:
            update["pnl"] = round(float(pnl), 2)
        client.table(DB_TRADES_TABLE).update(update).eq("id", trade_id).execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure("trade close", e)
        return False


def insert_log(event, message, details=None, timestamp=None):
    """Insert an event row into the logs table. Never raises."""
    if not _allowed():
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "event": str(event),
            "message": str(message),
            "timestamp": (timestamp or datetime.now(IST)).isoformat(),
        }
        if details is not None:
            row["details"] = details if isinstance(details, str) else json.dumps(details)
        client.table(DB_LOGS_TABLE).insert(row).execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure("log insert", e)
        return False


def get_daily_pnl():
    """Sum of pnl for closed trades today (IST). Returns 0.0 on failure."""
    if not _allowed():
        return 0.0
    client = _get_client()
    if client is None:
        return 0.0
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        res = (client.table(DB_TRADES_TABLE)
               .select("pnl")
               .eq("status", "closed")
               .gte("timestamp", start.isoformat())
               .execute())
        _mark_ok()
        rows = res.data or []
        total = 0.0
        for r in rows:
            try:
                v = r.get("pnl")
                if v is not None:
                    total += float(v)
            except (TypeError, ValueError):
                pass
        return round(total, 2)
    except Exception as e:  # noqa: BLE001
        _mark_failure("daily pnl", e)
        return 0.0


def get_daily_trades():
    """All trade rows for today (IST), oldest first. Returns [] on failure."""
    if not _allowed():
        return []
    client = _get_client()
    if client is None:
        return []
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        res = (client.table(DB_TRADES_TABLE)
               .select("*")
               .gte("timestamp", start.isoformat())
               .order("timestamp", desc=False)
               .execute())
        _mark_ok()
        return res.data or []
    except Exception as e:  # noqa: BLE001
        _mark_failure("daily trades", e)
        return []


def get_win_rate():
    """(win_rate_percent, wins, losses) over closed trades today. (0.0, 0, 0) on failure."""
    try:
        rows = get_daily_trades()
        closed = [r for r in rows if r.get("status") == "closed" and r.get("pnl") is not None]
        wins = sum(1 for r in closed if float(r["pnl"]) > 0)
        losses = sum(1 for r in closed if float(r["pnl"]) < 0)
        rate = (wins / (wins + losses) * 100) if (wins + losses) else 0.0
        return round(rate, 1), wins, losses
    except Exception as e:  # noqa: BLE001
        print(f"Database error (win rate): {e}")
        return 0.0, 0, 0


def get_recent_logs(limit=100):
    """Last `limit` log events, oldest first: [{timestamp, event, message, details}]."""
    if not _allowed():
        return []
    client = _get_client()
    if client is None:
        return []
    try:
        res = (client.table(DB_LOGS_TABLE)
               .select("timestamp,event,message,details")
               .order("timestamp", desc=True)
               .limit(int(limit))
               .execute())
        _mark_ok()
        rows = res.data or []
        rows.reverse()
        return rows
    except Exception as e:  # noqa: BLE001
        _mark_failure("recent logs", e)
        return []


def get_trade_count_today():
    """Number of trade rows recorded today (IST). Returns 0 on failure."""
    if not _allowed():
        return 0
    client = _get_client()
    if client is None:
        return 0
    try:
        start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
        res = (client.table(DB_TRADES_TABLE)
               .select("id")
               .gte("timestamp", start.isoformat())
               .execute())
        _mark_ok()
        return len(res.data or [])
    except Exception as e:  # noqa: BLE001
        _mark_failure("trade count", e)
        return 0


def db_healthy():
    """
    True when Supabase is reachable and answering. Reuses the most recent
    successful operation, or actively probes when nothing succeeded recently.
    """
    if _skip_until > time.time():
        return False
    if _last_ok_ts and time.time() - _last_ok_ts < 60:
        return True
    client = _get_client()
    if client is None:
        return False
    try:
        client.table(DB_LOGS_TABLE).select("id").limit(1).execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure("health probe", e)
        return False


# ---------------------------------------------------------------------------
# Optional: settings-table mirror (used by dashboard/telegram later)
# ---------------------------------------------------------------------------
def get_setting(key, default=None):
    """Read one key from the settings table. Never raises."""
    client = _get_client()
    if client is None:
        return default
    try:
        res = (client.table(DB_SETTINGS_TABLE)
               .select("value").eq("key", key).limit(1).execute())
        _mark_ok()
        rows = res.data or []
        return rows[0].get("value") if rows else default
    except Exception as e:  # noqa: BLE001
        _mark_failure(f"get_setting({key})", e)
        return default


def set_setting(key, value):
    """Upsert one key into the settings table. Never raises."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.table(DB_SETTINGS_TABLE).upsert(
            {"key": key, "value": str(value), "updated_at": datetime.now(IST).isoformat()},
            on_conflict="key",
        ).execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure(f"set_setting({key})", e)
        return False


# ---------------------------------------------------------------------------
# Bot Status & Pause-After-Loss Control
# ---------------------------------------------------------------------------
def get_bot_status():
    """
    Get the bot operating status from the database settings.
    Returns: {"status": 'RUNNING'|'PAUSED_AFTER_LOSS'|'PAUSED_MANUAL',
              "last_loss": '<str>'|None,
              "paused_at": '<iso>'|None}
    """
    status = get_setting("bot_status", "RUNNING")
    last_loss = get_setting("last_loss_reason", None)
    paused_at = get_setting("pause_timestamp", None)
    return {
        "status": status or "RUNNING",
        "last_loss": last_loss if last_loss else None,
        "paused_at": paused_at if paused_at else None,
    }


def set_bot_status(status, reason=None):
    """
    Update the bot operating status in the database settings.
    Values: 'RUNNING', 'PAUSED_AFTER_LOSS', 'PAUSED_MANUAL'
    """
    now_iso = datetime.now(IST).isoformat()
    pause_time = now_iso if "PAUSED" in str(status) else ""

    set_setting("bot_status", str(status))
    set_setting("last_loss_reason", str(reason) if reason else "")
    set_setting("pause_timestamp", pause_time)

    # Also update column fields on a master state row if columns exist
    client = _get_client()
    if client is not None:
        try:
            update_data = {
                "key": "bot_state",
                "value": str(status),
                "bot_status": str(status),
                "last_loss_reason": str(reason) if reason else None,
                "pause_timestamp": pause_time if pause_time else None,
                "updated_at": now_iso,
            }
            client.table(DB_SETTINGS_TABLE).upsert(update_data, on_conflict="key").execute()
            _mark_ok()
        except Exception:  # noqa: BLE001
            pass
    return True


# ---------------------------------------------------------------------------
# Alerts Table Management (for Loss Detections & Manual Actions)
# ---------------------------------------------------------------------------
def insert_alert(alert_type, message, bot_status=None, trade_id=None):
    """Insert an alert event row into the alerts table. Never raises."""
    if not _allowed():
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        row = {
            "alert_type": str(alert_type),
            "message": str(message),
            "bot_status": str(bot_status or ""),
            "trade_id": int(trade_id) if trade_id is not None else None,
            "created_at": datetime.now(IST).isoformat(),
            "resolved": False,
        }
        client.table(DB_ALERTS_TABLE).insert(row).execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure("alert insert", e)
        return False


def get_alerts(resolved=False, limit=20):
    """Fetch recent alert rows."""
    if not _allowed():
        return []
    client = _get_client()
    if client is None:
        return []
    try:
        query = client.table(DB_ALERTS_TABLE).select("*")
        if resolved is not None:
            query = query.eq("resolved", bool(resolved))
        res = query.order("created_at", desc=True).limit(int(limit)).execute()
        _mark_ok()
        return res.data or []
    except Exception as e:  # noqa: BLE001
        _mark_failure("alerts fetch", e)
        return []


def resolve_alerts(alert_id=None):
    """Mark alerts as resolved."""
    if not _allowed():
        return False
    client = _get_client()
    if client is None:
        return False
    try:
        query = client.table(DB_ALERTS_TABLE).update({"resolved": True})
        if alert_id is not None:
            query = query.eq("id", alert_id)
        else:
            query = query.eq("resolved", False)
        query.execute()
        _mark_ok()
        return True
    except Exception as e:  # noqa: BLE001
        _mark_failure("resolve alerts", e)
        return False


# ---------------------------------------------------------------------------
# CLI self-test: python3 database/db.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("database/db.py self-test")
    print("=" * 60)

    ok = insert_log("START", "db.py self-test START event", {"source": "self_test"})
    print(("✅" if ok else "❌"), "insert_log ->", ok)

    tid = insert_trade("TEST", 1.0000, "BUY", 0.5, status="open")
    print(("✅" if tid else "❌"), "insert_trade -> id:", tid)

    if tid:
        closed = close_trade(tid, exit_price=1.0010, pnl=1.0)
        print(("✅" if closed else "❌"), "close_trade ->", closed)

    pnl, trades, wr, wins, losses = (
        get_daily_pnl(), len(get_daily_trades()), *get_win_rate())
    print(f"ℹ️  get_daily_pnl={pnl} | trades today={trades} | "
          f"win rate={wr}% ({wins}W/{losses}L)")

    print("Done — if any ❌ above, check SUPABASE_URL/KEY in .env and the")
    print("GRANT/RLS SQL from setup/init_database.py.")
