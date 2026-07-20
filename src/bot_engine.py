"""
Bot Engine - Runs every 30 seconds
Uses CoinGecko API (works in UAE and all countries)
Auto-executes trades based on signals
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
        # Auto-close profitable trades every 5 minutes
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
    """Fetch OHLC from CoinGecko"""
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": "30"}
            )
            resp.raise_for_status()
            raw = resp.json()
            candles = [
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
            return candles
    except Exception as e:
        logger.warning(f"CoinGecko fetch failed for {symbol}: {e}")
        return []


async def fetch_current_price(symbol: str) -> float:
    """Get current price from CoinGecko"""
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"}
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data[coin_id]["usd"])
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
        return 0.0


async def bot_main_loop():
    """Main bot loop - runs every 30 seconds"""
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
                logger.info("No active users")
                return

            logger.info(f"Processing {len(users)} active users")
            for user in users:
                await generate_and_store_signals(user, db)

            await db.commit()
            logger.info("✅ Bot loop completed")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"❌ Bot loop error: {e}", exc_info=True)


async def generate_and_store_signals(user, db):
    """Fetch candles, generate signals, auto-execute trades"""
    from signal_engine import generate_signal
    from database import Trade, TradeStatus, TradeSignal
    from sqlalchemy import select

    for symbol in PAIRS:
        try:
            candles = await fetch_candles(symbol)
            if len(candles) < 10:
                continue

            result = generate_signal(candles)
            current_price = candles[-1]["close"]

            # Only act on BUY or SELL signals with confidence > 30%
            if result.signal in ["buy", "sell"] and result.confidence >= 30:

                # Check if we already have an open position for this pair
                existing = await db.execute(
                    select(Trade).where(
                        Trade.user_id == user.id,
                        Trade.symbol == symbol,
                        Trade.status == TradeStatus.pending
                    )
                )
                open_trade = existing.scalar_one_or_none()

                if not open_trade:
                    # Open new trade
                    trade = Trade(
                        user_id=user.id,
                        exchange_name="paper_trading",
                        symbol=symbol,
                        signal=result.signal,
                        confidence=result.confidence,
                        price=current_price,
                        quantity=round(
                            float(user.trade_amount_usdt) / current_price, 8
                        ) if current_price > 0 else 0,
                        total_usdt=float(user.trade_amount_usdt),
                        rsi=result.rsi,
                        macd=result.macd,
                        bb_position=result.bb_position,
                        status=TradeStatus.executed,
                        executed_at=datetime.utcnow(),
                        created_at=datetime.utcnow()
                    )
                    db.add(trade)
                    logger.info(
                        f"✅ AUTO-EXECUTED {result.signal.upper()} "
                        f"{symbol} @ ${current_price:,.2f} "
                        f"confidence={result.confidence}%"
                    )
                else:
                    logger.info(
                        f"Signal [{symbol}]: {result.signal.upper()} "
                        f"confidence={result.confidence}% — position already open"
                    )
            else:
                logger.info(
                    f"Signal [{symbol}]: {result.signal.upper()} "
                    f"confidence={result.confidence}% RSI={result.rsi}"
                )

        except Exception as e:
            logger.error(f"Signal error for {symbol}: {e}", exc_info=True)


async def auto_close_trades():
    """Auto-close trades after 4 hours and calculate P&L"""
    try:
        from database import get_db, Trade, TradeStatus, TradeSignal
        from sqlalchemy import select

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            cutoff = datetime.utcnow() - timedelta(hours=4)
            result = await db.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.executed,
                    Trade.executed_at <= cutoff,
                    Trade.pnl_usdt == None
                )
            )
            trades = result.scalars().all()

            if not trades:
                return

            logger.info(f"Auto-closing {len(trades)} trades...")

            for trade in trades:
                current_price = await fetch_current_price(trade.symbol)
                if current_price <= 0:
                    continue

                entry = float(trade.price or 0)
                qty = float(trade.quantity or 0)

                if trade.signal == TradeSignal.buy:
                    pnl = (current_price - entry) * qty
                else:
                    pnl = (entry - current_price) * qty

                trade.pnl_usdt = round(pnl, 4)
                trade.status = TradeStatus.executed
                trade.executed_at = datetime.utcnow()

                logger.info(
                    f"Closed {trade.symbol}: "
                    f"{'🟢 +' if pnl >= 0 else '🔴 '}${pnl:.4f} USDT"
                )

            await db.commit()
            logger.info("✅ Auto-close completed")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Auto-close error: {e}", exc_info=True)
