-- =============================================================================
-- Kronos AI Trading Agent — Complete Supabase Database Schema
-- Run this in: Supabase Dashboard -> SQL Editor -> New query -> Run
-- =============================================================================

-- 1. Trades table (holds open and closed trades)
CREATE TABLE IF NOT EXISTS public.trades (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol      text,                        -- EURUSD, GBPUSD, USDJPY, AUDUSD
    entry_price double precision,            -- entry price
    exit_price  double precision,            -- exit price (null while open)
    signal      text,                        -- BUY or SELL
    confidence  double precision,            -- 0-1 (0.78 = 78%)
    pnl         double precision,            -- profit/loss in dollars (null until closed)
    status      text DEFAULT 'open',         -- 'open' or 'closed'
    "timestamp" timestamptz,                 -- when the trade was made
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON public.trades ("timestamp" DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON public.trades (symbol);

-- 2. Logs table (audit trail for bot events)
CREATE TABLE IF NOT EXISTS public.logs (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event       text,                        -- TRADE, SWITCH, STOP, ERROR, PIN, START, ...
    message     text,                        -- detailed message
    details     text,                        -- additional JSON data (nullable)
    "timestamp" timestamptz,                 -- when the event occurred
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_logs_event ON public.logs (event);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON public.logs ("timestamp" DESC);

-- 3. Settings table (persistent key-value store + bot state)
CREATE TABLE IF NOT EXISTS public.settings (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    key              text NOT NULL UNIQUE,         -- OLYMP_TOKEN, DEMO_MODE, DAILY_TARGET_ENABLED, ...
    value            text,                         -- the setting value
    bot_status       text DEFAULT 'RUNNING',       -- 'RUNNING', 'PAUSED_AFTER_LOSS', 'PAUSED_MANUAL'
    last_loss_reason text,
    pause_timestamp  timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- If settings table already exists without the new columns, add them safely:
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'settings' AND column_name = 'bot_status') THEN
        ALTER TABLE public.settings ADD COLUMN bot_status text DEFAULT 'RUNNING';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'settings' AND column_name = 'last_loss_reason') THEN
        ALTER TABLE public.settings ADD COLUMN last_loss_reason text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'settings' AND column_name = 'pause_timestamp') THEN
        ALTER TABLE public.settings ADD COLUMN pause_timestamp timestamptz;
    END IF;
END $$;

-- 4. Alerts table (for loss notifications, pause events, and manual approvals)
CREATE TABLE IF NOT EXISTS public.alerts (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  timestamptz NOT NULL DEFAULT now(),
    alert_type  text,                        -- 'LOSS_DETECTED', 'MANUAL_ACTION_NEEDED'
    message     text,
    bot_status  text,                        -- 'PAUSED_AFTER_LOSS', etc.
    trade_id    bigint,                      -- reference to trades table id
    resolved    boolean DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_alerts_resolved ON public.alerts (resolved);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON public.alerts (created_at DESC);

-- =============================================================================
-- Privileges & Row Level Security (RLS) Grants
-- Grants access to both anon and service_role keys
-- =============================================================================

GRANT ALL PRIVILEGES ON public.trades TO anon, authenticated, service_role;
GRANT ALL PRIVILEGES ON public.logs TO anon, authenticated, service_role;
GRANT ALL PRIVILEGES ON public.settings TO anon, authenticated, service_role;
GRANT ALL PRIVILEGES ON public.alerts TO anon, authenticated, service_role;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO anon, authenticated, service_role;

ALTER TABLE public.trades   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.logs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alerts   ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bot_all_trades   ON public.trades;
DROP POLICY IF EXISTS bot_all_logs     ON public.logs;
DROP POLICY IF EXISTS bot_all_settings ON public.settings;
DROP POLICY IF EXISTS bot_all_alerts   ON public.alerts;

CREATE POLICY bot_all_trades   ON public.trades   FOR ALL TO anon, authenticated, service_role USING (true) WITH CHECK (true);
CREATE POLICY bot_all_logs     ON public.logs     FOR ALL TO anon, authenticated, service_role USING (true) WITH CHECK (true);
CREATE POLICY bot_all_settings ON public.settings FOR ALL TO anon, authenticated, service_role USING (true) WITH CHECK (true);
CREATE POLICY bot_all_alerts   ON public.alerts   FOR ALL TO anon, authenticated, service_role USING (true) WITH CHECK (true);
