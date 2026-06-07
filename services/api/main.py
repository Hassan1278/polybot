from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import polybot.alerts as alerts
from polybot.config import settings
from polybot.logging import get_logger

from services.api.routes import (
    admin,
    correlation,
    fills,
    health,
    markets,
    metrics,
    pipeline,
    pnl,
    positions,
    settings as settings_routes,
    signals,
    wallets,
    ws,
)

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    alerts.init_sentry()
    log.info("api_starting", mode=settings.trading_mode)
    yield
    log.info("api_stopping")


app = FastAPI(title="Polybot API", version="0.1.0", lifespan=lifespan)

# CORS — restrict to known origins. Wildcard "*" combined with
# allow_credentials=True is an OWASP-flagged pattern (CSRF + credential
# theft) so we list explicit origins from `settings.cors_origins`
# (env CORS_ORIGINS, comma-separated). Empty list = same-origin only,
# which the dashboard supports via the rewrite in next.config.js.
_cors_origins = [o.strip() for o in (settings.cors_origins or "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["http://localhost:3000"],
    allow_credentials=True,
    # PATCH + DELETE needed for the settings UI (adminApi.patch/delete).
    # OPTIONS auto-handled by Starlette; listing it is informational.
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    # Browser-side fetches send these headers:
    #   X-Admin-Token: every admin mutation (kill-switch, settings, wallet)
    #   X-Live-Confirm: only the paper→live mode switch (extra HMAC)
    # Plus the standard authorization + content-type for completeness.
    allow_headers=[
        "authorization", "content-type",
        "x-admin-token", "x-live-confirm",
    ],
)

app.include_router(health.router)
app.include_router(wallets.router, prefix="/wallets",         tags=["wallets"])
app.include_router(markets.router, prefix="/markets",         tags=["markets"])
app.include_router(signals.router, prefix="/signals",         tags=["signals"])
app.include_router(fills.router,   prefix="/fills",           tags=["fills"])
app.include_router(pnl.router,     prefix="/pnl",             tags=["pnl"])
app.include_router(correlation.router, prefix="/correlation", tags=["correlation"])
from fastapi import Depends
from services.api.rate_limit import admin_rate_limit
_admin_rl = [Depends(admin_rate_limit())]
app.include_router(admin.router,   prefix="/admin",           tags=["admin"], dependencies=_admin_rl)
app.include_router(settings_routes.router, prefix="/admin/settings", tags=["admin", "settings"], dependencies=_admin_rl)
app.include_router(metrics.router, prefix="/metrics",         tags=["metrics"])
app.include_router(pipeline.router, prefix="/pipeline",       tags=["pipeline"])
app.include_router(positions.router, prefix="/positions",     tags=["positions"])
app.include_router(ws.router)                                 # /ws
