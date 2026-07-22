"""
Manual Trade Router
File: routers/trades.py
Handles manual buy/sell execution in paper trading mode
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from database import get_db, User, Trade, TradeStatus, TradeSignal
from routers.auth import get_current_user
from bot_engine import KRAKEN_PAIRS, COINGECKO_IDS
from pydantic import BaseModel
from datetime import datetime
import httpx
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


class ManualTradeRequest(BaseModel):
    symbol: str          # e.g. "BTC/USDT"
    action: str          # "buy" or "sell"
    amount_usdt: float   # how much USDT to use


async def get_current_price(symbol: str) -> float:
    """Fetch live price - Kraken primary, CoinGecko fallback.
    (Binance blocks many hosting regions with HTTP 451, so we don't use it here.)"""
    kraken_pair = KRAKEN_PAIRS.get(symbol)
    if kraken_pair:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://api.kraken.com/0/public/Ticker",
                    params={"pair": kraken_pair}
                )
                resp.raise_for_status()
                data = resp.json()
                result = data.get("result", {})
                if result and not data.get("error"):
                    pair_key = list(result.keys())[0]
                    return float(result[pair_key]["c"][0])
        except Exception as e:
            logger.warning(f"Kraken price fetch failed for {symbol}: {e}")

    # Fallback: CoinGecko simple price (works for base pairs + top gainers alike)
    coin_id = COINGECKO_IDS.get(symbol)
    if coin_id:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"}
                )
                resp.raise_for_status()
                data = resp.json()
                price = data.get(coin_id, {}).get("usd")
                if price:
                    return float(price)
        except Exception as e:
            logger.warning(f"CoinGecko price fetch failed for {symbol}: {e}")

    raise HTTPException(status_code=503, detail=f"Could not fetch price for {symbol} from any source")


@router.post("/manual")
async def manual_trade(
    body: ManualTradeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Execute a manual paper trade (buy or sell)"""

    # Validate
    action = body.action.lower()
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'sell'")

    if body.amount_usdt < 1:
        raise HTTPException(status_code=400, detail="Minimum trade amount is $1 USDT")

    # Get live price
    price = await get_current_price(body.symbol)
    quantity = round(body.amount_usdt / price, 8)

    # Save trade record
    trade = Trade(
        user_id=current_user.id,
        exchange_name="paper_trading",
        symbol=body.symbol,
        signal=TradeSignal.buy if action == "buy" else TradeSignal.sell,
        confidence=100.0,       # manual = 100% confidence
        price=price,
        quantity=quantity,
        total_usdt=body.amount_usdt,
        status=TradeStatus.executed,   # paper trade = instantly executed
        executed_at=datetime.utcnow(),
        created_at=datetime.utcnow()
    )
    db.add(trade)
    await db.commit()

    logger.info(f"Manual {action.upper()} by {current_user.email}: {body.symbol} qty={quantity} @ ${price}")

    return {
        "status": "executed",
        "action": action,
        "symbol": body.symbol,
        "price": price,
        "quantity": quantity,
        "total_usdt": body.amount_usdt,
        "message": f"✅ Paper {action.upper()} executed: {quantity:.6f} {body.symbol.split('/')[0]} @ ${price:,.2f}"
    }


@router.get("/open")
async def get_open_trades(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get open/pending positions for the user"""
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.pending)
        .order_by(desc(Trade.created_at))
        .limit(50)
    )
    trades = result.scalars().all()
    return [
        {
            "id": t.id,
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "price": float(t.price or 0),
            "quantity": float(t.quantity or 0),
            "total_usdt": float(t.total_usdt or 0),
            "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in trades
    ]


@router.post("/close/{trade_id}")
async def close_trade(
    trade_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Close/exit an open position — calculates P&L and stores the exit price"""
    result = await db.execute(
        select(Trade)
        .where(Trade.id == trade_id)
        .where(Trade.user_id == current_user.id)
    )
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    # Get current price to calculate P&L
    current_price = await get_current_price(trade.symbol)
    entry_price = float(trade.price or 0)
    quantity = float(trade.quantity or 0)

    if trade.signal == TradeSignal.buy:
        pnl = (current_price - entry_price) * quantity
    else:
        pnl = (entry_price - current_price) * quantity

    trade.exit_price = current_price
    trade.status = TradeStatus.executed
    trade.pnl_usdt = round(pnl, 4)
    trade.executed_at = datetime.utcnow()
    await db.commit()

    return {
        "status": "closed",
        "symbol": trade.symbol,
        "entry_price": entry_price,
        "exit_price": current_price,
        "quantity": quantity,
        "pnl_usdt": round(pnl, 4),
        "message": f"{'🟢 Profit' if pnl >= 0 else '🔴 Loss'}: ${pnl:+.4f} USDT"
    }
