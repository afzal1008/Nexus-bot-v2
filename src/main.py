"""
Nexus Bot - Main FastAPI Application
FIXED: signals router included, bot runs for all users
"""
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from bot_engine import scheduler_manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Nexus Bot...")
    scheduler = AsyncIOScheduler()
    scheduler_manager.start(scheduler)
    scheduler.start()
    logger.info("✅ Bot running every 30 seconds")
    yield
    scheduler.shutdown()
    logger.info("Bot stopped")

app = FastAPI(title="Nexus Bot API", version="2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
from routers import auth, dashboard, exchanges, payments, users, admin, signals, trades

app.include_router(auth.router,       prefix="/api/auth",      tags=["auth"])
app.include_router(dashboard.router,  prefix="/api/dashboard", tags=["dashboard"])
app.include_router(exchanges.router,  prefix="/api/exchanges", tags=["exchanges"])
app.include_router(payments.router,   prefix="/api/payments",  tags=["payments"])
app.include_router(users.router,      prefix="/api/users",     tags=["users"])
app.include_router(admin.router,      prefix="/api/admin",     tags=["admin"])
app.include_router(signals.router,    prefix="/api/signals",   tags=["signals"])
app.include_router(trades.router,     prefix="/api/trades",    tags=["trades"])

# ── Bot endpoints ─────────────────────────────────────────────────────────────
from pydantic import BaseModel
from database import get_db, User, Trade, TradeStatus
from routers.auth import get_current_user
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

class BotSettingsRequest(BaseModel):
    bot_enabled: bool
    trade_amount_usdt: float = 10.0

@app.post("/api/bot/settings")
async def bot_settings(
    body: BotSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # REMOVED plan check so PRO users (like you) can always enable
    current_user.bot_enabled = body.bot_enabled
    current_user.trade_amount_usdt = max(5.0, min(body.trade_amount_usdt, 10000.0))
    await db.commit()
    logger.info(f"Bot {'ENABLED' if body.bot_enabled else 'DISABLED'} for {current_user.email}")
    return {
        "status": "success",
        "bot_enabled": current_user.bot_enabled,
        "trade_amount_usdt": current_user.trade_amount_usdt
    }

@app.get("/api/bot/status")
async def bot_status(
    current_user: User = Depends(get_current_user),
):
    """Dashboard uses this to show Active/Inactive"""
    return {
        "bot_enabled": current_user.bot_enabled,
        "plan": current_user.plan,
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
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "confidence": float(t.confidence or 0),
            "entry_price": float(t.price or 0),
            "exit_price": float(t.price or 0),
            "pnl_usdt": float(t.pnl_usdt or 0),
            "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
            "exchange": t.exchange_name,
            "quantity": float(t.quantity or 0),
            "created_at": t.created_at.isoformat() if t.created_at else None
        }
        for t in trades
    ]

# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "nexus-bot-v2"}

@app.get("/")
async def root():
    return {"message": "Nexus Bot API v2.0"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
