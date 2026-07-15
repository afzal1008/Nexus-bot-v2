"""
Admin Router
File: src/routers/admin.py
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from database import get_db, User, Trade
from routers.auth import get_current_user

router = APIRouter()


@router.get("/stats")
async def admin_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.email not in ["afzal.1008@gmail.com", "admin@nexusbot.com"]:
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(select(func.count(User.id)))
    total_users = result.scalar() or 0

    result = await db.execute(select(func.count(Trade.id)))
    total_trades = result.scalar() or 0

    return {
        "total_users": total_users,
        "total_trades": total_trades,
    }
