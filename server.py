#!/usr/bin/env python3
"""
Kronos Trading Bot — Control API
=================================

FastAPI service for remote bot state monitoring and trade resumption.

Endpoints:
  GET  /bot-status       -> {"status": current_status, "last_loss": reason, "paused_at": timestamp}
  POST /resume-trading   -> {"status": "RUNNING", "message": "Bot resumed"}
  POST /pause-trading    -> {"status": "PAUSED_MANUAL", "message": "Bot paused manually"}
  GET  /alerts           -> List of recent alerts from Supabase

Run:
  python3 server.py
  or
  uvicorn server:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from database.db import (
    get_bot_status,
    set_bot_status,
    get_alerts,
    resolve_alerts,
    insert_log,
)

app = FastAPI(
    title="Kronos Trading Bot API",
    description="Control & Monitoring API for the Kronos AI Trading Agent",
    version="1.0.0",
)

# Enable CORS for web dashboards and local tools
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ResumeRequest(BaseModel):
    token: Optional[str] = None
    pin: Optional[str] = None


class PauseRequest(BaseModel):
    token: Optional[str] = None
    pin: Optional[str] = None
    reason: Optional[str] = "Manual pause via API"


def _validate_user(
    request: Request,
    x_api_key: Optional[str] = None,
    authorization: Optional[str] = None,
    body_token: Optional[str] = None,
    body_pin: Optional[str] = None,
) -> bool:
    """Validate user via API token, PIN, or local IP whitelist."""
    expected_token = os.getenv("RESUME_TOKEN", "").strip()
    expected_pin = os.getenv("PIN", "2003").strip()

    # Extract bearer token if Authorization header present
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:].strip()

    provided_token = x_api_key or bearer_token or body_token
    provided_pin = body_pin

    # If an explicit RESUME_TOKEN is set in .env, check it
    if expected_token:
        if provided_token == expected_token:
            return True

    # If PIN provided, check against bot PIN
    if provided_pin and expected_pin:
        if str(provided_pin).strip() == expected_pin:
            return True

    # If provided token matches PIN, accept it as well
    if provided_token and expected_pin and str(provided_token).strip() == expected_pin:
        return True

    # Allow local connections (localhost / 127.0.0.1) if no explicit RESUME_TOKEN is enforced
    client_host = request.client.host if request.client else ""
    if not expected_token and client_host in ("127.0.0.1", "localhost", "::1", "testclient"):
        return True

    return False


@app.get("/bot-status")
def bot_status():
    """
    Returns the current bot operating status, last loss details, and pause timestamp.
    Output: {"status": current_status, "last_loss": reason, "paused_at": timestamp}
    """
    info = get_bot_status()
    return {
        "status": info.get("status", "RUNNING"),
        "last_loss": info.get("last_loss"),
        "paused_at": info.get("paused_at"),
    }


@app.post("/resume-trading")
def resume_trading(
    request: Request,
    body: Optional[ResumeRequest] = None,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """
    Resumes trading by switching bot_status back to 'RUNNING'.
    Resets last_loss_reason to NULL and resolves unresolved loss alerts.
    Validates user via API token, PIN, or localhost.
    Output: {"status": "RUNNING", "message": "Bot resumed"}
    """
    b_token = body.token if body else None
    b_pin = body.pin if body else None

    if not _validate_user(request, x_api_key, authorization, b_token, b_pin):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing resume token / PIN",
        )

    # Update database status to RUNNING and clear last loss reason
    set_bot_status("RUNNING", reason=None)

    # Mark active loss alerts as resolved
    resolve_alerts()

    # Audit log
    insert_log("RESUME_API", "Trading resumed via API endpoint /resume-trading", {
        "client": request.client.host if request.client else "unknown",
        "timestamp": datetime.now().isoformat(),
    })

    return {
        "status": "RUNNING",
        "message": "Bot resumed",
    }


@app.post("/pause-trading")
def pause_trading(
    request: Request,
    body: Optional[PauseRequest] = None,
    x_api_key: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    """Manually pause the trading bot."""
    b_token = body.token if body else None
    b_pin = body.pin if body else None
    reason = body.reason if body else "Manual pause via API"

    if not _validate_user(request, x_api_key, authorization, b_token, b_pin):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing token / PIN",
        )

    set_bot_status("PAUSED_MANUAL", reason=reason)
    insert_log("PAUSE_API", f"Trading paused via API: {reason}", {
        "client": request.client.host if request.client else "unknown",
        "timestamp": datetime.now().isoformat(),
    })

    return {
        "status": "PAUSED_MANUAL",
        "message": f"Bot paused: {reason}",
    }


@app.get("/alerts")
def list_alerts(resolved: Optional[bool] = None, limit: int = 20):
    """List recent alerts from the database."""
    return get_alerts(resolved=resolved, limit=limit)


@app.get("/")
def root():
    return {
        "service": "Kronos Trading Bot API",
        "status": "online",
        "endpoints": [
            "GET  /bot-status",
            "POST /resume-trading",
            "POST /pause-trading",
            "GET  /alerts",
        ],
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", os.getenv("API_PORT", "8000")))
    print(f"🚀 Starting Kronos FastAPI Control Server on http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
