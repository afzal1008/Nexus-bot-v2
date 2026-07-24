"""
Database - PostgreSQL with SQLAlchemy async
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, mapped_column, Mapped, relationship
from sqlalchemy import String, Boolean, DateTime, Float, Text, ForeignKey, Enum as SAEnum, text
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
from typing import Optional, List
import uuid
import enum
import os

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:password@localhost/nexusbot")
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    pass
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

# ─── Enums ────────────────────────────────────────────────────────────────────

class PlanType(str, enum.Enum):
    free = "free"
    basic = "basic"
    pro = "pro"
    elite = "elite"

class TradeSignal(str, enum.Enum):
    buy = "buy"
    sell = "sell"
    hold = "hold"

class TradeStatus(str, enum.Enum):
    pending = "pending"
    executed = "executed"
    failed = "failed"
    cancelled = "cancelled"

class PaymentGateway(str, enum.Enum):
    razorpay = "razorpay"
    stripe = "stripe"
    paypal = "paypal"

# ─── Models ───────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    hashed_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    google_id: Mapped[Optional[str]] = mapped_column(String, nullable=True, unique=True)
    avatar_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    plan: Mapped[PlanType] = mapped_column(SAEnum(PlanType), default=PlanType.free)
    plan_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # Bot settings
    bot_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    trade_amount_usdt: Mapped[float] = mapped_column(Float, default=500.0)      # per trade, paper money (min $500)
    paper_balance_usdt: Mapped[float] = mapped_column(Float, default=10000.0)   # total virtual wallet

    # Risk management — stored as positive percentages (e.g. 8.0 = 8% loss, 15.0 = 15% gain)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=8.0)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=15.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    exchange_configs: Mapped[List["ExchangeConfig"]] = relationship(back_populates="user", cascade="all, delete")
    trades: Mapped[List["Trade"]] = relationship(back_populates="user", cascade="all, delete")
    payments: Mapped[List["Payment"]] = relationship(back_populates="user", cascade="all, delete")


class ExchangeConfig(Base):
    __tablename__ = "exchange_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    exchange_name: Mapped[str] = mapped_column(String, nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_secret: Mapped[Text] = mapped_column(Text, nullable=False)
    encrypted_passphrase: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    last_tested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    test_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="exchange_configs")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    exchange_name: Mapped[str] = mapped_column(String, nullable=False)
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    signal: Mapped[TradeSignal] = mapped_column(SAEnum(TradeSignal), nullable=False)
    status: Mapped[TradeStatus] = mapped_column(SAEnum(TradeStatus), default=TradeStatus.pending)

    order_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    order_type: Mapped[str] = mapped_column(String, default="market")
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    rsi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    macd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ema_signal: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    bb_position: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    pnl_usdt: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="trades")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    gateway: Mapped[PaymentGateway] = mapped_column(SAEnum(PaymentGateway), nullable=False)
    gateway_payment_id: Mapped[str] = mapped_column(String, nullable=False)
    gateway_order_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gateway_subscription_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    plan: Mapped[PlanType] = mapped_column(SAEnum(PlanType), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    currency: Mapped[str] = mapped_column(String, default="INR")
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="payments")


# ─── DB Init ──────────────────────────────────────────────────────────────────

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations()


async def run_migrations():
    """Lightweight, safe migrations for columns added after initial deploy."""
    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS paper_balance_usdt DOUBLE PRECISION DEFAULT 10000"
        ))
        await conn.execute(text(
            "UPDATE users SET paper_balance_usdt = 10000 WHERE paper_balance_usdt IS NULL"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stop_loss_pct DOUBLE PRECISION DEFAULT 8.0"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS take_profit_pct DOUBLE PRECISION DEFAULT 15.0"
        ))
        await conn.execute(text(
            "UPDATE users SET stop_loss_pct = 8.0 WHERE stop_loss_pct IS NULL"
        ))
        await conn.execute(text(
            "UPDATE users SET take_profit_pct = 15.0 WHERE take_profit_pct IS NULL"
        ))

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
