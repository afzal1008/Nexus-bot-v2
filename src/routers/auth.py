"""
Auth Router
File: src/routers/auth.py
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db, User, PlanType
from pydantic import BaseModel
from datetime import datetime, timedelta
from jose import JWTError, jwt
from passlib.hash import bcrypt
import os
import logging

logger = logging.getLogger(__name__)
router = APIRouter()

SECRET_KEY = os.getenv("SECRET_KEY", "nexus-bot-secret-key-2024")
ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def hash_password(password: str) -> str:
    return bcrypt.hash(password[:72])

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.verify(plain[:72], hashed)

def create_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(days=30)
    return jwt.encode({"sub": user_id, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str

class LoginRequest(BaseModel):
    email: str
    password: str

def user_to_dict(user: User, token: str = None):
    data = {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "plan": user.plan.value if hasattr(user.plan, 'value') else str(user.plan),
        "bot_enabled": user.bot_enabled,
        "trade_amount_usdt": float(user.trade_amount_usdt or 10.0),
        "is_active": user.is_active,
    }
    if token:
        data["access_token"] = token
        data["token_type"] = "bearer"
        data["user"] = {k: v for k, v in data.items() if k not in ["access_token","token_type"]}
    return data

@router.post("/register")
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(User).where(User.email == body.email))
        if result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already registered")
        user = User(
            email=body.email,
            full_name=body.full_name,
            hashed_password=hash_password(body.password),
            plan=PlanType.pro,
            is_active=True,
            is_verified=True,
            bot_enabled=False,
            trade_amount_usdt=10.0,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        token = create_token(user.id)
        logger.info(f"Registered: {user.email}")
        return user_to_dict(user, token)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Register error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(User).where(User.email == body.email))
        user = result.scalar_one_or_none()
        if not user or not verify_password(body.password, user.hashed_password):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        token = create_token(user.id)
        logger.info(f"Login: {user.email}")
        return user_to_dict(user, token)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return user_to_dict(current_user)

@router.put("/me")
async def update_me(
    body: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if "full_name" in body:
        current_user.full_name = body["full_name"]
    if "password" in body and body["password"]:
        current_user.hashed_password = hash_password(body["password"])
    await db.commit()
    return user_to_dict(current_user)
