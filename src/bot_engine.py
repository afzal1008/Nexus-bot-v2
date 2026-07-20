"""
Bot Engine - Runs every 30 seconds
Uses CoinGecko API (works in UAE and all countries)
"""
import logging
import httpx
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

COINGECKO_IDS = {
    "BTC/USDT": "bitcoin",
    "ETH/USDT": "ethereum",
    "BNB/USDT": "binancecoin",
    "SOL/USDT": "solana",
    "XRP/USDT": "ripple"
}


class SchedulerManager:
    def __init__(self):
        self.scheduler = None

    def start(self, scheduler: AsyncIOScheduler):
        self.scheduler = scheduler
        self.scheduler.add_job(
            bot_main_loop,
            "interval",
            seconds=30,
            id="nexus-bot-loop",
            replace_existing=True
        )
        self.scheduler.add_job(
            auto_close_trades,
            "interval",
            minutes=5,
            id="nexus-auto-close",
            replace_existing=True
        )
        logger.info("✅ Bot scheduler started - 30 second interval")


scheduler_manager = SchedulerManager()


async def fetch_candles(symbol: str) -> list:
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": "30"}
            )
            resp.raise_for_status()
            raw = resp.json()
            return [
                {
                    "timestamp": c[0],
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": 1000000.0
                }
                for c in raw
            ]
    except Exception as e:
        logger.warning(f"CoinGecko fetch failed for {symbol}: {e}")
        return []


async def fetch_current_price(symbol: str) -> float:
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"}
            )
            resp.raise_for_status()
            return float(resp.json()[coin_id]["usd"])
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
        return 0.0


async def bot_main_loop():
    try:
        from database import get_db, User, Trade, TradeStatus
        from sqlalchemy import select
        from signal_engine import generate_signal

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            result = await db.execute(
                select(User).where(User.bot_enabled == True)
            )
            users = result.scalars().all()

            if not users:
                logger.info("No active users with bot enabled")
                return

            for user in users:
                await process_user(user, db)

            await db.commit()
            logger.info("✅ Bot loop completed")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"❌ Bot loop error: {e}", exc_info=True)


async def process_user(user, db):
    from signal_engine import generate_signal
    from database import Trade, TradeStatus
    from sqlalchemy import select, and_

    for symbol in PAIRS:
        try:
            candles = await fetch_candles(symbol)
            if len(candles) < 10:
                logger.warning(f"Not enough candles for {symbol}")
                continue

            result = generate_signal(candles)
            current_price = candles[-1]["close"]
            signal_str = result.signal.lower() if isinstance(result.signal, str) else result.signal.value.lower()

            logger.info(
                f"Signal [{symbol}]: {signal_str.upper()} "
                f"confidence={result.confidence}% "
                f"price=${current_price:,.2f} RSI={result.rsi}"
            )

            # Only execute on buy/sell with enough confidence
            if signal_str in ["buy", "sell"] and result.confidence >= 25:

                # Check no open position for this pair
                existing = await db.execute(
                    select(Trade).where(
                        and_(
                            Trade.user_id == user.id,
                            Trade.symbol == symbol,
                            Trade.status == TradeStatus.pending
                        )
                    )
                )
                if existing.scalar_one_or_none():
                    logger.info(f"Position already open for {symbol} - skipping")
                    continue

                trade_amount = float(user.trade_amount_usdt or 10.0)
                qty = round(trade_amount / current_price, 8) if current_price > 0 else 0

                trade = Trade(
                    user_id=user.id,
                    exchange_name="paper_trading",
                    symbol=symbol,
                    signal=signal_str,
                    confidence=result.confidence,
                    price=current_price,
                    quantity=qty,
                    total_usdt=trade_amount,
                    rsi=result.rsi,
                    macd=result.macd,
                    bb_position=result.bb_position,
                    status=TradeStatus.executed,
                    executed_at=datetime.utcnow(),
                    created_at=datetime.utcnow()
                )
                db.add(trade)
                logger.info(
                    f"✅ EXECUTED {signal_str.upper()} {symbol} "
                    f"@ ${current_price:,.2f} qty={qty}"
                )

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}", exc_info=True)


async def auto_close_trades():
    """Auto-close trades after 4 hours"""
    try:
        from database import get_db, Trade, TradeStatus
        from sqlalchemy import select, and_

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            cutoff = datetime.utcnow() - timedelta(hours=4)
            result = await db.execute(
                select(Trade).where(
                    and_(
                        Trade.status == TradeStatus.executed,
                        Trade.executed_at <= cutoff,
                        Trade.pnl_usdt == None
                    )
                )
            )
            trades = result.scalars().all()

            for trade in trades:
                current_price = await fetch_current_price(trade.symbol)
                if current_price <= 0:
                    continue

                entry = float(trade.price or 0)
                qty = float(trade.quantity or 0)
                signal_str = trade.signal.lower() if isinstance(trade.signal, str) else trade.signal.value.lower()

                if signal_str == "buy":
                    pnl = (current_price - entry) * qty
                else:
                    pnl = (entry - current_price) * qty

                trade.pnl_usdt = round(pnl, 4)
                logger.info(
                    f"Auto-closed {trade.symbol}: "
                    f"{'🟢' if pnl >= 0 else '🔴'} ${pnl:+.4f} USDT"
                )

            if trades:
                await db.commit()
                logger.info(f"✅ Auto-closed {len(trades)} trades")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Auto-close error: {e}", exc_info=True)
