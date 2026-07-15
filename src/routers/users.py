"""
Users Router
File: src/routers/users.py
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db, User
from routers.auth import get_current_user

router = APIRouter()


@router.get("/me")
async def get_user(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "plan": current_user.plan.value if hasattr(current_user.plan, 'value') else str(current_user.plan),
        "bot_enabled": current_user.bot_enabled,
        "trade_amount_usdt": float(current_user.trade_amount_usdt or 10.0),
    }
