"""
Bot Engine - Runs every 30 seconds
Uses CoinGecko API (works in UAE, no restrictions)
"""
import logging
import httpx
from datetime import datetime
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
        logger.info("✅ Bot scheduler started - 30 second interval")


scheduler_manager = SchedulerManager()


async def fetch_candles(symbol: str) -> list:
    """Fetch OHLC data from CoinGecko - works everywhere"""
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Get 30 days of daily OHLC
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
                    "volume": 1000000.0  # CoinGecko OHLC doesn't include volume
                }
                for c in raw
            ]
            logger.info(f"✅ Fetched {len(candles)} candles for {symbol}")
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
                f"https://api.coingecko.com/api/v3/simple/price",
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
                logger.info("Bot loop: no active users, generating demo signals")
                await generate_and_store_signals(None, db)
            else:
                logger.info(f"Bot loop: processing {len(users)} active users")
                for user in users:
                    await generate_and_store_signals(user, db)

            await db.commit()
            logger.info("✅ Bot loop completed")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"❌ Bot loop error: {e}", exc_info=True)


async def generate_and_store_signals(user, db):
    """Fetch candles, generate signals, save to DB"""
    from signal_engine import generate_signal
    from database import Trade, TradeStatus

    for symbol in PAIRS:
        try:
            candles = await fetch_candles(symbol)
            if len(candles) < 10:
                logger.warning(f"Not enough candles for {symbol}: {len(candles)}")
                continue

            result = generate_signal(candles)
            current_price = candles[-1]["close"]

            trade = Trade(
                user_id=user.id if user else "system",
                exchange_name="paper_trading",
                symbol=symbol,
                signal=result.signal,
                confidence=result.confidence,
                price=current_price,
                quantity=round(10.0 / current_price, 8) if current_price > 0 else 0,
                total_usdt=10.0,
                rsi=result.rsi,
                macd=result.macd,
                bb_position=result.bb_position,
                status=TradeStatus.pending,
                created_at=datetime.utcnow()
            )
            db.add(trade)

            logger.info(
                f"Signal [{symbol}]: {result.signal.upper()} "
                f"confidence={result.confidence}% "
                f"price=${current_price:,.2f} "
                f"RSI={result.rsi}"
            )

        except Exception as e:
            logger.error(f"Signal error for {symbol}: {e}", exc_info=True)
