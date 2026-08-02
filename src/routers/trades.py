"""
Manual Trade Router
File: routers/trades.py
Handles manual buy/sell in paper trading mode — SPOT ONLY, no shorting.
"Buy" opens a new position. "Sell" only closes an existing open position for that symbol.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from database import get_db, User, Trade, TradeStatus, TradeSignal
from routers.auth import get_current_user
from bot_engine import (
    KRAKEN_PAIRS, COINGECKO_IDS, MIN_TRADE_USDT, get_price_any, resolve_coingecko_id,
    fetch_candles, compute_atr_levels, DEFAULT_STOP_LOSS_PCT, DEFAULT_TAKE_PROFIT_PCT,
    adjust_balance, try_close_trade
)
from signal_engine import calculate_atr
from pydantic import BaseModel
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


class ManualTradeRequest(BaseModel):
    symbol: str          # e.g. "BTC/USDT"
    action: str          # "buy" or "sell"
    amount_usdt: float   # how much paper USDT to use (ignored for "sell" — uses the open position's size)


async def get_current_price(symbol: str, coingecko_id: str = None) -> float:
    """Fetch live price - Kraken primary, CoinGecko fallback (via bot_engine's shared,
    restart-resilient lookup). Raises if no source has a price."""
    price = await get_price_any(symbol, coingecko_id)
    if price > 0:
        return price
    raise HTTPException(status_code=503, detail=f"Could not fetch price for {symbol} from any source")


@router.post("/manual")
async def manual_trade(
    body: ManualTradeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Spot-only manual trade: 'buy' opens a position, 'sell' closes an existing one."""

    action = body.action.lower()
    if action not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="action must be 'buy' or 'sell'")

    # Find any existing open position for this symbol
    existing = await db.execute(
        select(Trade).where(
            and_(
                Trade.user_id == current_user.id,
                Trade.symbol == body.symbol,
                Trade.status == TradeStatus.pending
            )
        ).limit(1)
    )
    open_trade = existing.scalars().first()

    if action == "buy":
        if open_trade:
            raise HTTPException(
                status_code=400,
                detail=f"You already have an open position in {body.symbol}. Sell it before buying again."
            )

        if body.amount_usdt < MIN_TRADE_USDT:
            raise HTTPException(status_code=400, detail=f"Minimum trade amount is ${MIN_TRADE_USDT:.0f} USDT")

        current_balance = float(current_user.paper_balance_usdt or 0)
        if current_balance < body.amount_usdt:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient paper balance: ${current_balance:.2f} available, ${body.amount_usdt:.2f} requested"
            )

        coin_id = COINGECKO_IDS.get(body.symbol)
        if not coin_id:
            coin_id = await resolve_coingecko_id(body.symbol)

        price = await get_current_price(body.symbol, coin_id)
        quantity = round(body.amount_usdt / price, 8)

        # Compute the same ATR-based stop-loss/take-profit used by the automated bot,
        # so manual trades get the same volatility-aware protection
        stop_cap = float(current_user.stop_loss_pct) if current_user.stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT
        target_cap = float(current_user.take_profit_pct) if current_user.take_profit_pct is not None else DEFAULT_TAKE_PROFIT_PCT
        try:
            candles = await fetch_candles(body.symbol)
            atr = calculate_atr(candles) if len(candles) >= 10 else price * 0.02
            atr_pct = (atr / price) * 100 if price > 0 else 2.0
        except Exception:
            atr_pct = 2.0  # conservative fallback if candle fetch fails
        sl_price, tp_price, _, _ = compute_atr_levels(price, atr_pct, stop_cap, target_cap)

        trade = Trade(
            user_id=current_user.id,
            exchange_name="paper_trading",
            symbol=body.symbol,
            signal=TradeSignal.buy,
            confidence=100.0,
            price=price,
            quantity=quantity,
            total_usdt=body.amount_usdt,
            coingecko_id=coin_id,
            stop_loss_price=sl_price,
            take_profit_price=tp_price,
            status=TradeStatus.pending,
            created_at=datetime.utcnow()
        )
        db.add(trade)
        await adjust_balance(db, current_user.id, -body.amount_usdt)
        current_user.paper_balance_usdt = current_balance - body.amount_usdt  # local estimate for the response below
        await db.commit()

        logger.info(f"Manual BUY by {current_user.email}: {body.symbol} qty={quantity} @ ${price}")

        return {
            "status": "executed",
            "action": "buy",
            "symbol": body.symbol,
            "price": price,
            "quantity": quantity,
            "total_usdt": body.amount_usdt,
            "paper_balance_usdt": current_user.paper_balance_usdt,
            "message": f"✅ Bought {quantity:.6f} {body.symbol.split('/')[0]} @ ${price:,.2f}"
        }

    else:  # action == "sell" — only closes an existing open position, never opens a short
        if not open_trade:
            raise HTTPException(
                status_code=400,
                detail=f"No open position in {body.symbol} to sell. Buy first, then sell to close."
            )

        current_price = await get_current_price(body.symbol, open_trade.coingecko_id)
        entry_price = float(open_trade.price or 0)
        quantity = float(open_trade.quantity or 0)
        pnl = (current_price - entry_price) * quantity

        won = await try_close_trade(db, open_trade.id, current_price, pnl, "manual")
        if not won:
            await db.commit()
            raise HTTPException(status_code=409, detail="This position was already closed by another process a moment ago.")

        principal = float(open_trade.total_usdt or 0)
        await adjust_balance(db, current_user.id, principal + pnl)
        current_user.paper_balance_usdt = float(current_user.paper_balance_usdt or 0) + principal + pnl  # local estimate for the response below
        await db.commit()

        return {
            "status": "closed",
            "action": "sell",
            "symbol": body.symbol,
            "entry_price": entry_price,
            "exit_price": current_price,
            "quantity": quantity,
            "pnl_usdt": round(pnl, 4),
            "paper_balance_usdt": current_user.paper_balance_usdt,
            "message": f"{'🟢 Profit' if pnl >= 0 else '🔴 Loss'}: ${pnl:+.4f} USDT"
        }


@router.get("/open")
async def get_open_trades(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Get open/pending positions for the user, including their stop-loss/take-profit price levels"""
    result = await db.execute(
        select(Trade)
        .where(Trade.user_id == current_user.id)
        .where(Trade.status == TradeStatus.pending)
        .order_by(desc(Trade.created_at))
        .limit(50)
    )
    trades = result.scalars().all()

    stop_loss_pct = float(current_user.stop_loss_pct if current_user.stop_loss_pct is not None else 8.0)
    take_profit_pct = float(current_user.take_profit_pct if current_user.take_profit_pct is not None else 15.0)

    output = []
    for t in trades:
        entry = float(t.price or 0)
        # Prefer the ATR-based levels saved on the trade itself; fall back to the
        # flat percentage calc only for trades opened before this feature existed
        if t.stop_loss_price is not None and t.take_profit_price is not None:
            stop_loss_price = t.stop_loss_price
            take_profit_price = t.take_profit_price
        else:
            stop_loss_price = round(entry * (1 - stop_loss_pct / 100.0), 6) if entry > 0 else None
            take_profit_price = round(entry * (1 + take_profit_pct / 100.0), 6) if entry > 0 else None
        output.append({
            "id": t.id,
            "symbol": t.symbol,
            "signal": t.signal.value if hasattr(t.signal, 'value') else str(t.signal),
            "price": entry,
            "quantity": float(t.quantity or 0),
            "total_usdt": float(t.total_usdt or 0),
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "status": t.status.value if hasattr(t.status, 'value') else str(t.status),
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return output


@router.post("/close/{trade_id}")
async def close_trade(
    trade_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Close/exit an open position by ID — calculates P&L, stores exit price, settles the wallet"""
    result = await db.execute(
        select(Trade)
        .where(Trade.id == trade_id)
        .where(Trade.user_id == current_user.id)
    )
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    current_price = await get_current_price(trade.symbol, trade.coingecko_id)
    entry_price = float(trade.price or 0)
    quantity = float(trade.quantity or 0)
    pnl = (current_price - entry_price) * quantity  # spot-only: always long

    won = await try_close_trade(db, trade.id, current_price, pnl, "manual")
    if not won:
        await db.commit()
        raise HTTPException(status_code=409, detail="This position was already closed by another process a moment ago.")

    principal = float(trade.total_usdt or 0)
    await adjust_balance(db, current_user.id, principal + pnl)
    current_user.paper_balance_usdt = float(current_user.paper_balance_usdt or 0) + principal + pnl  # local estimate for the response below

    await db.commit()

    return {
        "status": "closed",
        "symbol": trade.symbol,
        "entry_price": entry_price,
        "exit_price": current_price,
        "quantity": quantity,
        "pnl_usdt": round(pnl, 4),
        "paper_balance_usdt": current_user.paper_balance_usdt,
        "message": f"{'🟢 Profit' if pnl >= 0 else '🔴 Loss'}: ${pnl:+.4f} USDT"
    }
