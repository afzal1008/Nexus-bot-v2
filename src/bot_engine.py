"""
Bot Engine - Runs every 30 seconds
Fetches real candle data from Binance public API, generates signals, saves to DB
"""
import logging
import httpx
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Trading pairs to monitor
PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
BINANCE_URL = "https://api.binance.com/api/v3/klines"


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


async def fetch_candles(symbol: str, limit: int = 50) -> list:
    """Fetch OHLCV candles from Binance public API - no auth needed"""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(BINANCE_URL, params={
                "symbol": symbol,
                "interval": "1h",
                "limit": limit
            })
            resp.raise_for_status()
            raw = resp.json()
            candles = [
                {
                    "timestamp": c[0],
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                }
                for c in raw
            ]
            return candles
    except Exception as e:
        logger.warning(f"Binance fetch failed for {symbol}: {e}")
        return []


async def bot_main_loop():
    """Main bot loop - runs every 30 seconds"""
    try:
        from database import get_db, User, Trade, TradeStatus
        from sqlalchemy import select
        from signal_engine import SignalEngine, generate_signal

        # Get DB session
        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            # Check for users with bot enabled
            result = await db.execute(
                select(User).where(User.bot_enabled == True)
            )
            users = result.scalars().all()

            if not users:
                logger.info("Bot loop: no active users, generating demo signals anyway")
                # Still generate + store signals so Live Signals page works
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
            if len(candles) < 30:
                logger.warning(f"Not enough candles for {symbol}: {len(candles)}")
                continue

            # generate_signal is sync - call directly
            result = generate_signal(candles)

            # Always store signal regardless of buy/sell/hold so UI has data
            current_price = candles[-1]["close"]
            trade = Trade(
                user_id=user.id if user else "system",
                exchange_name="paper_trading",
                symbol=symbol.replace("USDT", "/USDT"),  # BTC/USDT format for display
                signal=result.signal,
                confidence=result.confidence,
                price=current_price,
                quantity=round(10.0 / current_price, 6),  # $10 paper trade
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
                f"RSI={result.rsi} | {result.reasoning[:60]}"
            )

        except Exception as e:
            logger.error(f"Signal error for {symbol}: {e}", exc_info=True)
