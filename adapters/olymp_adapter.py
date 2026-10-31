#!/usr/bin/env python3
"""
Olymp Trade Adapter
===================

Wraps the local olymptrade_ws repo (./OT API/olymptrade_ws/) — the same client
the existing bot.py uses — for candle fetching and trade execution:

    get_candles(symbol, lookback=400, size=60)
      -> list of OHLCV candle dicts (as returned by the Olymp websocket)

    execute_trade(decision, symbol, size=1, direction='BUY')
      -> {'ok': True, 'trade': {...}} on success
         {'error': '<message>'} on any failure

Design rules (per project guardrails):
  * NEVER raises — every failure returns {'error': '<message>'}.
  * Websocket connect / fetch / place are each capped at 10 seconds.
  * Trades go to the DEMO account unless DEMO_MODE=false in .env.
  * The olymptrade_ws repo itself is NEVER modified — we only import from it.

Optional .env keys (defaults shown):
  OLYMP_TOKEN        = (no default — real candles/trades need it)
  OLYMP_ACCOUNT_ID   = (auto-detected from the websocket session when omitted)
  DEMO_MODE          = true   -> trades placed with group='demo'
  TRADE_DURATION     = 60     (seconds; digital options expiry)
"""

import os
import sys
import asyncio

from dotenv import load_dotenv

# Make sure the project root is importable no matter where we launch from
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Load the project .env so the adapter works standalone (never overrides
# variables the caller already exported — override=False)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)

# The olymptrade_ws package lives inside the "OT API" folder
OT_API_DIR = os.path.join(PROJECT_ROOT, "OT API")
if OT_API_DIR not in sys.path:
    sys.path.insert(0, OT_API_DIR)

OLYMP_TIMEOUT = 10


def _client_class():
    from olymptrade_ws import OlympTradeClient
    return OlympTradeClient


def _clean_token(token):
    """Sanitize raw token input from web devtools, stripping quotes, Bearer, etc."""
    if not token:
        return ""
    t = str(token).strip().strip('"').strip("'")
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if t.lower().startswith("access_token="):
        t = t[len("access_token="):].strip()
    return t.strip().strip('"').strip("'")


def get_olymp_token():
    """
    Retrieve sanitized Olymp Trade token from os.environ, .env, or DB settings.
    Never raises.
    """
    if os.path.exists(os.path.join(PROJECT_ROOT, ".env")):
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

    token = _clean_token(os.getenv("OLYMP_TOKEN", ""))
    if token:
        return token

    # Fallback to Supabase database settings table
    try:
        from database.db import get_setting
        db_token = get_setting("OLYMP_TOKEN")
        if db_token:
            token = _clean_token(db_token)
            if token:
                os.environ["OLYMP_TOKEN"] = token
                return token
    except Exception:
        pass
    return ""


def get_candles(symbol, lookback=400, size=60):
    """
    Fetch the last `lookback` OHLCV candles for `symbol` via the
    olymptrade_ws websocket.

    Output (success): list of candle dicts (timestamp, open, high, low, close, volume)
    Output (failure): {'error': '<message>'}
    """
    token = get_olymp_token()
    if not token:
        return {"error": "OLYMP_TOKEN not set in .env or database — cannot fetch live candles"}

    async def _run():
        import time
        OlympTradeClient = _client_class()
        client = OlympTradeClient(access_token=token, log_raw_messages=False)
        candles_future = asyncio.get_running_loop().create_future()

        async def _on_candles(msg):
            if not candles_future.done():
                candles_future.set_result(msg)

        client.register_callback(10, _on_candles)
        client.register_callback(1003, _on_candles)
        await asyncio.wait_for(client.start(), timeout=OLYMP_TIMEOUT)
        try:
            to_ts = int(time.time())
            data = [{"pair": symbol, "size": size, "to": to_ts, "solid": True}]
            await client.send_request(10, data, requires_response=False)
            msg = await asyncio.wait_for(candles_future, timeout=OLYMP_TIMEOUT)
            d = msg.get("d", [])
            candles = []
            if isinstance(d, list) and d and isinstance(d[0], dict) and "candles" in d[0]:
                candles = d[0]["candles"]
            elif isinstance(d, list):
                candles = d
            return candles[:lookback] if lookback else candles
        finally:
            await client.stop()

    try:
        candles = asyncio.run(_run())
    except asyncio.TimeoutError:
        return {"error": f"Olymp websocket timeout after {OLYMP_TIMEOUT}s"}
    except ImportError as e:
        return {"error": f"olymptrade_ws package not importable: {e}"}
    except Exception as e:  # noqa: BLE001 — connection/API errors
        return {"error": f"Olymp candle fetch failed: {e}"}

    if not candles or not isinstance(candles, list):
        return {"error": "Olymp returned no candles for " + str(symbol)}
    return list(candles)


def get_trade_amount(default=None):
    """
    Return the current trade amount from Olymp Trade settings (e.g. 20, 30, 40).

    Checks Supabase database settings first, then .env / os.environ,
    and falls back to 70.0 (or default) when unavailable.
    NEVER raises.
    """
    FALLBACK = 70.0

    if os.path.exists(os.path.join(PROJECT_ROOT, ".env")):
        load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=True)

    # 1) Database settings table (synced from dashboard/telegram)
    try:
        from database.db import get_setting
        db_val = get_setting("TRADE_AMOUNT")
        if db_val is not None:
            val = float(db_val)
            if val > 0:
                os.environ["TRADE_AMOUNT"] = str(val)
                return val
    except Exception:
        pass

    # 2) .env TRADE_AMOUNT (last known/stored amount)
    try:
        val = float(os.getenv("TRADE_AMOUNT", "") or "")
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass

    # 3) explicit caller default
    try:
        if default is not None and float(default) > 0:
            return float(default)
    except (TypeError, ValueError):
        pass

    # 4) spec fallback
    return FALLBACK


def execute_trade(decision, symbol, size=None, direction=None):
    """
    Execute a trade via the olymptrade_ws websocket (also used by force_trade).

    Input:
      decision  — 'BUY'/'SELL' (used when `direction` is not given)
      symbol    — e.g. 'EURUSD'
      size      — trade amount in account currency (defaults to get_trade_amount())
      direction — optional explicit 'BUY'/'SELL' override

    Output (success): {'ok': True, 'trade': {...}, 'group': 'demo'|'real'}
    Output (failure): {'error': '<message>'}
    """
    chosen = str(direction or decision or "").strip().upper()
    if chosen in ("BUY", "UP", "LONG", "CALL"):
        olymp_dir = "up"
    elif chosen in ("SELL", "DOWN", "SHORT", "PUT"):
        olymp_dir = "down"
    else:
        return {"error": f"Cannot execute trade for decision '{decision}' "
                         f"(need BUY or SELL)"}

    token = get_olymp_token()
    if not token:
        return {"error": "OLYMP_TOKEN not set in .env or database — cannot execute trade"}

    demo_mode = os.getenv("DEMO_MODE", "true").strip().lower() \
        not in ("0", "false", "no", "off")
    group = "demo" if demo_mode else "real"
    duration = int(float(os.getenv("TRADE_DURATION", "60")))
    amount = float(size) if size is not None and float(size) > 0 else float(get_trade_amount())

    async def _run():
        OlympTradeClient = _client_class()
        client = OlympTradeClient(access_token=token, log_raw_messages=False)
        await asyncio.wait_for(client.start(), timeout=OLYMP_TIMEOUT)
        try:
            account_id = int(float(os.getenv("OLYMP_ACCOUNT_ID", "0") or 0))
            if not account_id:
                # Subscribe to balance updates (event 55 arrives quickly)
                await client.send_request(98, [2223, 2301, 55, 150, 152, 151, 126, 602, 601], requires_response=False)
                for _ in range(10):
                    bal = client.current_balance
                    if bal and "d" in bal and isinstance(bal["d"], list):
                        for acc in bal["d"]:
                            if acc.get("group") == group:
                                account_id = acc.get("account_id")
                                break
                    if account_id:
                        break
                    await asyncio.sleep(0.3)

            account_id = account_id or client.account_id
            if not account_id:
                raise RuntimeError("could not determine Olymp account_id from the live session")

            return await asyncio.wait_for(
                client.trade.place_order(
                    pair=symbol, amount=amount, direction=olymp_dir,
                    duration=duration, account_id=account_id, group=group,
                ),
                timeout=OLYMP_TIMEOUT,
            )
        finally:
            await client.stop()

    try:
        result = asyncio.run(_run())
    except asyncio.TimeoutError:
        return {"error": f"Olymp websocket timeout after {OLYMP_TIMEOUT}s"}
    except ImportError as e:
        return {"error": f"olymptrade_ws package not importable: {e}"}
    except Exception as e:  # noqa: BLE001 — connection/API errors
        return {"error": f"Olymp trade execution failed: {e}"}

    if not result:
        return {"error": "Olymp did not confirm the order"}
    return {"ok": True, "trade": result, "group": group}


def force_trade(symbol=None, direction="BUY", amount=None):
    """
    Manually place one test/forced trade, bypassing the Kronos confidence
    gate and market-hours check — used by the Dashboard 'FORCE TRADE' button
    and the Telegram /forcetrade command to verify a freshly pasted Olymp
    token actually works and is connected.

    * Respects DEMO_MODE (places on the demo account unless DEMO_MODE=false).
    * Amount defaults to the configured TRADE_AMOUNT / Olymp Trade setting.
    * Symbol defaults to the currently active scheduled market, falling back
      to 'EURUSD_OTC' outside market hours.

    Output (success): {'ok': True, 'symbol': ..., 'direction': ..., 'amount': ..., 'group': ...}
    Output (failure): {'ok': False, 'symbol': ..., 'direction': ..., 'error': '<message>'}
    Never raises.
    """
    if not symbol:
        try:
            from config.market_schedule import get_current_market
            m = get_current_market()
            symbol = m["symbol"] if m else "EURUSD_OTC"
        except Exception:  # noqa: BLE001
            symbol = "EURUSD_OTC"

    direction = str(direction or "BUY").strip().upper()
    try:
        amt = float(amount) if amount is not None and float(amount) > 0 else get_trade_amount(default=70.0)
    except (TypeError, ValueError):
        amt = get_trade_amount(default=70.0)

    result = execute_trade(direction, symbol, size=amt)

    out = {"symbol": symbol, "direction": direction, "amount": amt}
    if isinstance(result, dict) and result.get("ok"):
        out.update({"ok": True, "group": result.get("group"), "trade": result.get("trade")})
        try:
            from database.db import insert_log
            insert_log("FORCE_TRADE", f"Force trade placed: {symbol} {direction} ${amt:g}",
                       {"symbol": symbol, "direction": direction, "amount": amt,
                        "group": out.get("group")})
        except Exception:  # noqa: BLE001
            pass
    else:
        err = (result.get("error") if isinstance(result, dict) else str(result)) or "unknown error"
        out.update({"ok": False, "error": err})
        try:
            from database.db import insert_log
            insert_log("FORCE_TRADE", f"Force trade failed: {err}", {"symbol": symbol})
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------------------
# Live check: python3 -m adapters.olymp_adapter
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    token = get_olymp_token()
    print("token:", "set" if token else "MISSING")
    result = get_candles("EURUSD_OTC", lookback=10, size=300)
    if isinstance(result, dict) and "error" in result:
        print(f"candles: error ({result['error']})")
    else:
        print(f"candles: {len(result)} live bars")
    print("get_trade_amount:", get_trade_amount())
