"""
Admin Router
File: src/routers/admin.py
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db, User, Trade, TradeStatus
from routers.auth import get_current_user

router = APIRouter()

ADMIN_EMAILS = ["afzal.1008@gmail.com", "admin@nexusbot.com"]
STARTING_BALANCE_USDT = 10000.0


@router.get("/stats")
async def admin_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(select(func.count(User.id)))
    total_users = result.scalar() or 0

    result = await db.execute(select(func.count(Trade.id)))
    total_trades = result.scalar() or 0

    return {
        "total_users": total_users,
        "total_trades": total_trades,
    }


@router.post("/reconcile-wallets")
async def reconcile_wallets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Recalculates every user's paper_balance_usdt directly from their actual trade history
    (starting_balance - reserved_in_open_positions + realized_pnl) and corrects any drift
    between that and the stored value. Use this any time a wallet number looks wrong —
    it's the source-of-truth fix, not a guess.
    """
    if current_user.email not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(select(User))
    users = result.scalars().all()

    report = []
    for user in users:
        # Sum of principal currently tied up in open (pending) positions
        reserved_result = await db.execute(
            select(func.sum(Trade.total_usdt))
            .where(Trade.user_id == user.id)
            .where(Trade.status == TradeStatus.pending)
        )
        total_reserved = float(reserved_result.scalar() or 0)

        # Sum of realized P&L across every closed trade
        pnl_result = await db.execute(
            select(func.sum(Trade.pnl_usdt))
            .where(Trade.user_id == user.id)
            .where(Trade.pnl_usdt != None)
        )
        total_pnl = float(pnl_result.scalar() or 0)

        expected_balance = round(STARTING_BALANCE_USDT - total_reserved + total_pnl, 4)
        stored_balance = float(user.paper_balance_usdt if user.paper_balance_usdt is not None else STARTING_BALANCE_USDT)
        drift = round(stored_balance - expected_balance, 4)

        if abs(drift) > 0.01:  # meaningful drift, not just floating-point noise
            user.paper_balance_usdt = expected_balance
            report.append({
                "email": user.email,
                "old_balance": stored_balance,
                "corrected_balance": expected_balance,
                "drift_fixed": drift
            })

    await db.commit()

    return {
        "status": "success",
        "users_corrected": len(report),
        "details": report
    }
