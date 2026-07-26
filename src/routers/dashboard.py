"""
Dashboard Router
File: src/routers/dashboard.py
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db, User, Trade, TradeStatus, TradeSignal
from routers.auth import get_current_user
from datetime import datetime, timedelta

router = APIRouter()

STARTING_BALANCE_USDT = 10000.0


@router.get("/summary")
async def dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Dashboard summary stats"""
    thirty_days_ago = datetime.utcnow() - timedelta(hours=24)

    # Total signals in last 24 hours
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.created_at >= thirty_days_ago)
    )
    total_signals = result.scalar() or 0

    # Executed (closed) trades
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.executed)
    )
    executed_trades = result.scalar() or 0

    # Total realized PnL across all closed trades
    result = await db.execute(
        select(func.sum(Trade.pnl_usdt))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt != None)
    )
    total_pnl = float(result.scalar() or 0)

    # Win rate
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt > 0)
    )
    winning_trades = result.scalar() or 0
    win_rate = round((winning_trades / executed_trades * 100) if executed_trades > 0 else 0, 1)

    # Total USDT currently reserved in OPEN (pending) positions
    result = await db.execute(
        select(func.sum(Trade.total_usdt))
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.pending)
    )
    total_reserved = float(result.scalar() or 0)

    # Count of open positions
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.pending)
    )
    open_positions_count = result.scalar() or 0

    # Recent signals
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id)
        .order_by(Trade.created_at.desc())
        .limit(5)
    )
    recent = result.scalars().all()
    recent_signals = [
        {
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "confidence": float(t.confidence or 0),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in recent
    ]

    actual_balance = float(current_user.paper_balance_usdt if current_user.paper_balance_usdt is not None else STARTING_BALANCE_USDT)
    # What the balance SHOULD be, computed independently from trade history —
    # lets you spot any drift between this and the stored value.
    expected_balance = round(STARTING_BALANCE_USDT - total_reserved + total_pnl, 4)

    return {
        "total_signals": total_signals,
        "executed_trades": executed_trades,
        "total_pnl_usdt": round(total_pnl, 4),
        "win_rate_pct": win_rate,
        "bot_enabled": current_user.bot_enabled,
        "plan": current_user.plan.value if hasattr(current_user.plan, 'value') else str(current_user.plan),
        "paper_balance_usdt": actual_balance,
        "wallet_breakdown": {
            "starting_balance": STARTING_BALANCE_USDT,
            "open_positions_count": open_positions_count,
            "total_reserved_in_open_positions": round(total_reserved, 4),
            "total_realized_pnl": round(total_pnl, 4),
            "expected_balance": expected_balance,
            "stored_balance": actual_balance,
            "drift": round(actual_balance - expected_balance, 4),
        },
        "recent_signals": recent_signals,
    }
