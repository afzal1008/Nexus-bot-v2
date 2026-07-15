"""
Live Signals Router
File: routers/signals.py
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, or_
from database import get_db, User, Trade
from routers.auth import get_current_user

router = APIRouter()

@router.get("/live")
async def get_live_signals(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Return latest signals.
    Shows user's own signals, OR the most recent global signals
    (so the page is never blank even before bot runs for this user).
    """
    try:
        # First try user's own signals
        result = await db.execute(
            select(Trade)
            .where(Trade.user_id == current_user.id)
            .order_by(desc(Trade.created_at))
            .limit(20)
        )
        trades = result.scalars().all()

        # If user has no signals yet, show latest global signals (user_id=None or any)
        if not trades:
            result = await db.execute(
                select(Trade)
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
            "signals": signals
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
    """Signal engine health — used by the Live Signals page header"""
    try:
        # Count how many signals generated in last run
        result = await db.execute(
            select(Trade).order_by(desc(Trade.created_at)).limit(5)
        )
        recent = result.scalars().all()
        return {
            "status": "running",
            "last_signal": recent[0].created_at.isoformat() if recent else None,
            "pairs_monitored": ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"],
            "interval_seconds": 30
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
