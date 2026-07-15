"""
Payments Router
File: src/routers/payments.py
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from database import get_db, User, Payment, PaymentGateway, PlanType
from routers.auth import get_current_user
from pydantic import BaseModel
from datetime import datetime, timedelta
import os

router = APIRouter()

PLAN_PRICES_INR = {"basic": 99900, "pro": 249900, "elite": 599900}
PLAN_PRICES_USD = {"basic": 12,    "pro": 30,     "elite": 72}


class PlanRequest(BaseModel):
    plan: str


@router.post("/razorpay/create-order")
async def razorpay_create_order(
    body: PlanRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if body.plan not in PLAN_PRICES_INR:
        raise HTTPException(status_code=400, detail="Invalid plan")
    return {
        "key_id": os.getenv("RAZORPAY_KEY_ID", "rzp_test_demo"),
        "order_id": f"order_demo_{body.plan}",
        "amount": PLAN_PRICES_INR[body.plan],
        "currency": "INR",
        "plan": body.plan,
    }


@router.post("/razorpay/verify")
async def razorpay_verify(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    plan = body.get("plan", "basic")
    current_user.plan = PlanType[plan]
    current_user.plan_expires_at = datetime.utcnow() + timedelta(days=30)
    await db.commit()
    return {"status": "success", "plan": plan}


@router.post("/stripe/create-session")
async def stripe_create_session(
    body: PlanRequest,
    current_user: User = Depends(get_current_user),
):
    return {
        "url": f"https://buy.stripe.com/demo_{body.plan}",
        "plan": body.plan,
    }


@router.post("/paypal/create-order")
async def paypal_create_order(
    body: PlanRequest,
    current_user: User = Depends(get_current_user),
):
    return {
        "order_id": f"paypal_demo_{body.plan}",
        "amount": PLAN_PRICES_USD.get(body.plan, 30),
        "currency": "USD",
    }
