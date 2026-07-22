"""
Dashboard Router - All stats in last 24 hours
File: src/routers/dashboard.py
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db, User, Trade, TradeStatus
from routers.auth import get_current_user
from datetime import datetime, timedelta

router = APIRouter()


@router.get("/summary")
async def dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    last_24h = datetime.utcnow() - timedelta(hours=24)

    # Total signals in last 24 hours
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.created_at >= last_24h)
    )
    total_signals = result.scalar() or 0

    # Executed trades in last 24 hours
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.executed)
        .where(Trade.created_at >= last_24h)
    )
    executed_trades = result.scalar() or 0

    # Total P&L (all time)
    result = await db.execute(
        select(func.sum(Trade.pnl_usdt))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt != None)
    )
    total_pnl = float(result.scalar() or 0)

    # P&L last 24h
    result = await db.execute(
        select(func.sum(Trade.pnl_usdt))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt != None)
        .where(Trade.created_at >= last_24h)
    )
    pnl_24h = float(result.scalar() or 0)

    # Win rate last 24h
    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt > 0)
        .where(Trade.created_at >= last_24h)
    )
    winning_24h = result.scalar() or 0

    result = await db.execute(
        select(func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
        .where(Trade.pnl_usdt != None)
        .where(Trade.created_at >= last_24h)
    )
    closed_24h = result.scalar() or 0

    win_rate = round((winning_24h / closed_24h * 100) if closed_24h > 0 else 0, 1)

    # Paper balance = $1000 starting + all time P&L
    paper_balance = round(1000 + total_pnl, 2)

    # Recent signals (latest per coin - no duplicates)
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id)
        .order_by(Trade.created_at.desc())
        .limit(50)
    )
    all_recent = result.scalars().all()

    # Deduplicate by symbol - keep latest
    seen = set()
    recent_unique = []
    for t in all_recent:
        if t.symbol not in seen:
            seen.add(t.symbol)
            recent_unique.append(t)
        if len(recent_unique) >= 5:
            break

    recent_signals = [
        {
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "confidence": float(t.confidence or 0),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in recent_unique
    ]

    return {
        "total_signals": total_signals,
        "executed_trades": executed_trades,
        "total_pnl_usdt": round(total_pnl, 4),
        "pnl_24h": round(pnl_24h, 4),
        "win_rate_pct": win_rate,
        "paper_balance": paper_balance,
        "bot_enabled": current_user.bot_enabled,
        "plan": current_user.plan.value if hasattr(current_user.plan, 'value') else str(current_user.plan),
        "paper_balance_usdt": float(current_user.paper_balance_usdt if current_user.paper_balance_usdt is not None else 10000.0),
        "recent_signals": recent_signals,
    }
