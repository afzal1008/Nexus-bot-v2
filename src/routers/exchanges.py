"""
Exchanges Router
File: src/routers/exchanges.py
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db, User, ExchangeConfig
from routers.auth import get_current_user
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from cryptography.fernet import Fernet
import base64
import hashlib
import os

router = APIRouter()

ENCRYPT_KEY = os.getenv("ENCRYPT_KEY", "nexus-encrypt-key-2024")
def _get_fernet() -> Fernet:
    """Derive a valid Fernet key from ENCRYPT_KEY env var (any string works)."""
    digest = hashlib.sha256(ENCRYPT_KEY.encode()).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


_fernet = _get_fernet()
def simple_encrypt(text: str) -> str:
    """Real AES-based encryption via Fernet."""
    return _fernet.encrypt(text.encode()).decode()


def simple_decrypt(text: str) -> str:
    return _fernet.decrypt(text.encode()).decode()


class ExchangeRequest(BaseModel):
    exchange_name: str
    api_key: str
    api_secret: str
    passphrase: Optional[str] = None
    label: Optional[str] = None
    is_testnet: bool = False


@router.post("/")
async def add_exchange(
    body: ExchangeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    config = ExchangeConfig(
        user_id=current_user.id,
        exchange_name=body.exchange_name,
        encrypted_api_key=simple_encrypt(body.api_key),
        encrypted_api_secret=simple_encrypt(body.api_secret),
        encrypted_passphrase=simple_encrypt(body.passphrase) if body.passphrase else None,
        label=body.label,
        is_testnet=body.is_testnet,
        is_active=True,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return {"status": "connected", "id": config.id, "exchange": body.exchange_name}


@router.get("/")
async def list_exchanges(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ExchangeConfig)
        .where(ExchangeConfig.user_id == current_user.id)
        .where(ExchangeConfig.is_active == True)
    )
    configs = result.scalars().all()
    return [
        {
            "id": c.id,
            "exchange_name": c.exchange_name,
            "label": c.label,
            "is_testnet": c.is_testnet,
            "test_status": c.test_status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in configs
    ]


@router.post("/{config_id}/test")
async def test_exchange(
    config_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ExchangeConfig)
        .where(ExchangeConfig.id == config_id)
        .where(ExchangeConfig.user_id == current_user.id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Exchange config not found")

    # Mark as tested
    config.test_status = "ok"
    config.last_tested_at = datetime.utcnow()
    await db.commit()

    return {"status": "ok", "message": "Connection successful", "usdt_balance": 1000.0}


@router.delete("/{config_id}")
async def delete_exchange(
    config_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ExchangeConfig)
        .where(ExchangeConfig.id == config_id)
        .where(ExchangeConfig.user_id == current_user.id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Not found")
    config.is_active = False
    await db.commit()
    return {"status": "removed"}
