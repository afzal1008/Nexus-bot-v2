"""
Nexus Bot - Main FastAPI Application
"""
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from bot_engine import scheduler_manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Creating database tables...")
    try:
        from database import init_db
        await init_db()
        logger.info("Database tables ready")
    except Exception as e:
        logger.error(f"DB init error: {e}")
    logger.info("Starting Nexus Bot...")
    scheduler = AsyncIOScheduler()
    scheduler_manager.start(scheduler)
    scheduler.start()
    logger.info("Bot running every 60 seconds")
    yield
    scheduler.shutdown()
    logger.info("Bot stopped")

app = FastAPI(title="Nexus Bot API", version="2.0", lifespan=lifespan)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Max-Age": "86400",
}

# Handle ALL OPTIONS requests first
@app.options("/{path:path}")
async def options_handler(path: str):
    return Response(status_code=200, headers=CORS_HEADERS)

# Add CORS headers to every single response
@app.middleware("http")
async def cors_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return Response(status_code=200, headers=CORS_HEADERS)
    try:
        response = await call_next(request)
    except Exception as e:
        response = JSONResponse(
            status_code=500,
            content={"detail": str(e)}
        )
    for key, value in CORS_HEADERS.items():
        response.headers[key] = value
    return response

# Also add FastAPI CORS middleware as backup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import auth, dashboard, exchanges, payments, users, admin, signals, trades

app.include_router(auth.router,      prefix="/api/auth",      tags=["auth"])
app.include_router(dashboard.router, prefix="/api/dashboard", tags=["dashboard"])
app.include_router(exchanges.router, prefix="/api/exchanges", tags=["exchanges"])
app.include_router(payments.router,  prefix="/api/payments",  tags=["payments"])
app.include_router(users.router,     prefix="/api/users",     tags=["users"])
app.include_router(admin.router,     prefix="/api/admin",     tags=["admin"])
app.include_router(signals.router,   prefix="/api/signals",   tags=["signals"])
app.include_router(trades.router,    prefix="/api/trades",    tags=["trades"])

from pydantic import BaseModel
from database import get_db, User, Trade, TradeStatus
from routers.auth import get_current_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

class BotSettingsRequest(BaseModel):
    bot_enabled: bool
    trade_amount_usdt: float = 500.0

@app.post("/api/bot/settings")
async def bot_settings(
    body: BotSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    current_user.bot_enabled = body.bot_enabled
    current_user.trade_amount_usdt = max(500.0, min(body.trade_amount_usdt, 10000.0))
    await db.commit()
    return {
        "status": "success",
        "bot_enabled": current_user.bot_enabled,
        "trade_amount_usdt": current_user.trade_amount_usdt
    }

@app.get("/api/bot/status")
async def bot_status(current_user: User = Depends(get_current_user)):
    return {
        "bot_enabled": current_user.bot_enabled,
        "plan": current_user.plan.value if hasattr(current_user.plan, 'value') else str(current_user.plan),
        "trade_amount_usdt": float(current_user.trade_amount_usdt or 10.0)
    }

@app.get("/api/bot/history")
async def bot_history(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id)
        .order_by(Trade.created_at.desc())
        .limit(100)
    )
    trades = result.scalars().all()
    return [
        {
            "id": t.id,
            "exchange": t.exchange_name,
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "confidence": float(t.confidence or 0),
            "price": float(t.price or 0),              # buy/entry price
            "entry_price": float(t.price or 0),         # kept for backward compatibility
            "exit_price": float(t.exit_price) if t.exit_price is not None else None,  # sell price, once closed
            "quantity": float(t.quantity or 0),
            "total_usdt": float(t.total_usdt or 0),
            "pnl_usdt": float(t.pnl_usdt) if t.pnl_usdt is not None else None,
            "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
            "created_at": t.created_at.isoformat() if t.created_at else None
        }
        for t in trades
    ]

@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexus-bot-v2"}

@app.get("/")
async def root():
    return {"message": "Nexus Bot API v2.0"}
