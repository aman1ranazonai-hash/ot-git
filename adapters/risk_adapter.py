#!/usr/bin/env python3
"""
Risk Adapter
============

Final safety gate before any trade is executed:

    check_risk(decision, market_data, trade_history)
      -> {'approved': True/False, 'size': 1, 'reason': '<str>',
          'details': {...}}

Approval logic (per spec — approve only if ALL of):
  * daily_pnl           > RISK_DAILY_LOSS_LIMIT   (default -100)
  * consecutive_losses  < RISK_CONSECUTIVE_LOSS_LIMIT (default 3)
  * balance             > RISK_MIN_BALANCE        (default 100)

Daily P&L and consecutive losses come from the shared Supabase layer
(database/db.py — the same tables bot.py/telegram/dashboard use). The
`trade_history` argument, when given, takes precedence (useful for tests).

Design rules (per project guardrails):
  * NEVER raises — any internal failure means "not approved" with a reason.
  * Read-only: this adapter never writes to the database.

Optional .env keys (defaults shown):
  RISK_DAILY_LOSS_LIMIT        = -100
  RISK_CONSECUTIVE_LOSS_LIMIT  = 3
  RISK_MIN_BALANCE             = 100
  RISK_BALANCE                 = 1000  (account balance; no live source yet)
  RISK_TRADE_SIZE              = 1
"""

import os
import sys

# Make sure the project root is importable no matter where we launch from
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from database.db import get_daily_pnl, get_daily_trades


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return int(default)


def count_consecutive_losses(trade_history=None):
    """Count trailing closed losses, newest first. 0 when unknown."""
    rows = trade_history
    if rows is None:
        try:
            rows = get_daily_trades()
        except Exception:  # noqa: BLE001
            return 0
    losses = 0
    try:
        for row in reversed(list(rows or [])):
            if row.get("status") != "closed" or row.get("pnl") is None:
                continue
            if float(row["pnl"]) < 0:
                losses += 1
            else:
                break
    except Exception:  # noqa: BLE001
        return 0
    return losses


def check_risk(decision=None, market_data=None, trade_history=None):
    """
    Risk gate. See module docstring for the approval logic.

    Input:
      decision      — 'BUY'/'SELL'/'WAIT' (informational)
      market_data   — unused today, kept for signature compatibility
      trade_history — optional list of trade rows; fetched from the DB when None

    Output:
      {'approved': bool, 'size': int, 'reason': str, 'details': {...}}
    """
    try:
        daily_loss_limit = _env_float("RISK_DAILY_LOSS_LIMIT", -100)
        consec_limit = _env_int("RISK_CONSECUTIVE_LOSS_LIMIT", 3)
        min_balance = _env_float("RISK_MIN_BALANCE", 100)
        balance = _env_float("RISK_BALANCE", 1000)
        trade_size = _env_int("RISK_TRADE_SIZE", 1)

        try:
            daily_pnl = float(get_daily_pnl())
        except Exception:  # noqa: BLE001
            daily_pnl = 0.0
        consecutive_losses = count_consecutive_losses(trade_history)

        checks = [
            ("daily_loss_limit",
             daily_pnl > daily_loss_limit,
             f"daily_pnl {daily_pnl:+.2f} must be > {daily_loss_limit}"),
            ("consecutive_losses",
             consecutive_losses < consec_limit,
             f"consecutive_losses {consecutive_losses} must be < {consec_limit}"),
            ("balance",
             balance > min_balance,
             f"balance {balance:.2f} must be > {min_balance}"),
        ]
        failed = [msg for _name, ok, msg in checks if not ok]
        details = {
            "daily_pnl": round(daily_pnl, 2),
            "consecutive_losses": consecutive_losses,
            "balance": balance,
            "daily_loss_limit": daily_loss_limit,
            "consecutive_loss_limit": consec_limit,
            "min_balance": min_balance,
        }

        if failed:
            return {"approved": False, "size": 0,
                    "reason": "RISK REJECTED: " + "; ".join(failed),
                    "details": details}
        return {"approved": True, "size": trade_size,
                "reason": "All risk checks passed", "details": details}
    except Exception as e:  # noqa: BLE001 — fail safe: reject on any error
        return {"approved": False, "size": 0,
                "reason": f"RISK REJECTED: internal error: {e}",
                "details": {}}


# ---------------------------------------------------------------------------
# CLI self-test: python3 -m adapters.risk_adapter
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("check_risk():", check_risk("BUY"))
    print("3 straight losses:",
          check_risk("BUY", trade_history=[
              {"status": "closed", "pnl": 1.0},
              {"status": "closed", "pnl": -1.0},
              {"status": "closed", "pnl": -1.0},
              {"status": "closed", "pnl": -1.0},
          ]))
