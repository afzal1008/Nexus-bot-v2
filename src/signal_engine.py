"""
Live Signals Router
File: routers/signals.py
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from database import get_db, User, Trade
from routers.auth import get_current_user

router = APIRouter()

@router.get("/live")
async def get_live_signals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Return the current user's own latest signals only.
    Never falls back to showing another user's data — an empty list here means
    genuinely no signals yet for this account, not "nothing to show at all."
    """
    try:
        result = await db.execute(
            select(Trade)
            .where(Trade.user_id == current_user.id)
            .order_by(desc(Trade.created_at))
            .limit(20)
        )
        trades = result.scalars().all()

        signals = [
            {
                "id": t.id,
                "symbol": t.symbol,
                "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
                "confidence": float(t.confidence or 0),
                "entry_price": float(t.price or 0),
                "rsi": float(t.rsi or 0),
                "macd": float(t.macd or 0),
                "bb_position": t.bb_position or "—",
                "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in trades
        ]

        return {
            "status": "ok",
            "total": len(signals),
            "signals": signals,
            "message": None if signals else "No signals yet for your account. Enable the bot in Bot Settings to start generating signals."
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "signals": []
        }


@router.get("/status")
async def signal_engine_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Signal engine health — used by the Live Signals page header.
    Scoped to the current user's own trades, same as /live."""
    try:
        result = await db.execute(
            select(Trade)
            .where(Trade.user_id == current_user.id)
            .order_by(desc(Trade.created_at))
            .limit(5)
        )
        recent = result.scalars().all()
        return {
            "status": "running" if current_user.bot_enabled else "paused",
            "last_signal": recent[0].created_at.isoformat() if recent else None,
            "pairs_monitored": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
            "interval_seconds": 60
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
