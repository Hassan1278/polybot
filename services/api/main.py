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
    pipeline,
    pnl,
    positions,
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(wallets.router, prefix="/wallets",         tags=["wallets"])
app.include_router(markets.router, prefix="/markets",         tags=["markets"])
app.include_router(signals.router, prefix="/signals",         tags=["signals"])
app.include_router(fills.router,   prefix="/fills",           tags=["fills"])
app.include_router(pnl.router,     prefix="/pnl",             tags=["pnl"])
app.include_router(correlation.router, prefix="/correlation", tags=["correlation"])
app.include_router(admin.router,   prefix="/admin",           tags=["admin"])
app.include_router(pipeline.router, prefix="/pipeline",       tags=["pipeline"])
app.include_router(positions.router, prefix="/positions",     tags=["positions"])
app.include_router(ws.router)                                 # /ws
