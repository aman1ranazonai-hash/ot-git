# Railway Procfile
# ─────────────────────────────────────────────────────────────────────────────
# Railway only natively supports "web" (HTTP, gets PORT injected) and "worker"
# (background, no PORT).  Deploy each service separately in the Railway
# dashboard with the matching start command below.
#
#  Service 1 — Dashboard  (web, set Start Command):
web: bash start.sh
#
#  Service 2 — Trading Bot  (worker, set Start Command):
worker: python3 bot.py
#
#  Service 3 — Telegram Bot  (worker, set Start Command):
telegram: python3 telegram_bot.py
#
#  Service 4 — FastAPI Control Server  (web, set Start Command):
#  uvicorn server:app --host 0.0.0.0 --port $PORT
