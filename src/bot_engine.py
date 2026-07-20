"""
Bot Engine - Uses multiple free APIs with fallback
Primary: Kraken (no rate limits, works in UAE)
Fallback: CoinGecko
"""
import logging
import httpx
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]

# Kraken uses different pair names
KRAKEN_PAIRS = {
    "BTC/USDT": "XBTUSD",
    "ETH/USDT": "ETHUSD",
    "BNB/USDT": "BNBUSD",
    "SOL/USDT": "SOLUSD",
    "XRP/USDT": "XRPUSD"
}

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
            bot_main_loop, "interval", seconds=60,
            id="nexus-bot-loop", replace_existing=True
        )
        self.scheduler.add_job(
            auto_close_trades, "interval", minutes=10,
            id="nexus-auto-close", replace_existing=True
        )
        logger.info("✅ Bot scheduler started - 60 second interval")


scheduler_manager = SchedulerManager()


async def fetch_candles_kraken(symbol: str) -> list:
    """Fetch from Kraken - free, no auth needed, works everywhere"""
    kraken_pair = KRAKEN_PAIRS.get(symbol)
    if not kraken_pair:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.kraken.com/0/public/OHLC",
                params={"pair": kraken_pair, "interval": 60}
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                return []
            result = data.get("result", {})
            # Get first key that's not "last"
            pair_key = [k for k in result.keys() if k != "last"]
            if not pair_key:
                return []
            raw = result[pair_key[0]]
            candles = [
                {
                    "timestamp": int(c[0]),
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[6])
                }
                for c in raw
            ]
            logger.info(f"✅ Kraken: {len(candles)} candles for {symbol}")
            return candles
    except Exception as e:
        logger.warning(f"Kraken failed for {symbol}: {e}")
        return []


async def fetch_candles_coingecko(symbol: str) -> list:
    """Fallback: CoinGecko"""
    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        await asyncio.sleep(2)  # Rate limit delay
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": "7"}
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
        logger.warning(f"CoinGecko failed for {symbol}: {e}")
        return []


async def fetch_candles(symbol: str) -> list:
    """Try Kraken first, fallback to CoinGecko"""
    candles = await fetch_candles_kraken(symbol)
    if len(candles) >= 10:
        return candles
    logger.info(f"Falling back to CoinGecko for {symbol}")
    return await fetch_candles_coingecko(symbol)


async def fetch_current_price(symbol: str) -> float:
    """Get price from Kraken"""
    kraken_pair = KRAKEN_PAIRS.get(symbol)
    if not kraken_pair:
        return 0.0
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.kraken.com/0/public/Ticker",
                params={"pair": kraken_pair}
            )
            data = resp.json()
            result = data.get("result", {})
            pair_key = [k for k in result.keys()][0]
            return float(result[pair_key]["c"][0])
    except Exception as e:
        logger.warning(f"Price fetch failed for {symbol}: {e}")
        return 0.0


async def bot_main_loop():
    try:
        from database import get_db, User
        from sqlalchemy import select

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

            logger.info(f"Processing {len(users)} users")
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
            # Add delay between pairs to avoid rate limits
            await asyncio.sleep(1)

            candles = await fetch_candles(symbol)
            if len(candles) < 10:
                logger.warning(f"Not enough candles for {symbol}: {len(candles)}")
                continue

            result = generate_signal(candles)
            current_price = candles[-1]["close"]

            # Get signal as string
            if isinstance(result.signal, str):
                signal_str = result.signal.lower()
            else:
                signal_str = result.signal.value.lower()

            logger.info(
                f"[{symbol}] {signal_str.upper()} "
                f"conf={result.confidence}% "
                f"price=${current_price:,.2f} "
                f"RSI={result.rsi}"
            )

            # Execute if buy or sell with confidence >= 25%
            if signal_str in ["buy", "sell"] and result.confidence >= 25:
                # Check no existing pending trade for this pair
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
                    logger.info(f"Open position exists for {symbol}")
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
                    status=TradeStatus.pending,
                    executed_at=datetime.utcnow(),
                    created_at=datetime.utcnow()
                )
                db.add(trade)
                logger.info(
                    f"✅ OPENED {signal_str.upper()} "
                    f"{symbol} @ ${current_price:,.2f} "
                    f"qty={qty}"
                )

        except Exception as e:
            logger.error(f"Error for {symbol}: {e}", exc_info=True)


async def auto_close_trades():
    """Auto-close trades after 4 hours and calculate P&L"""
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
                        Trade.status == TradeStatus.pending,
                        Trade.created_at <= cutoff
                    )
                )
            )
            trades = result.scalars().all()

            if not trades:
                logger.info("No trades to close")
                return

            logger.info(f"Auto-closing {len(trades)} trades")

            for trade in trades:
                current_price = await fetch_current_price(trade.symbol)
                if current_price <= 0:
                    continue

                entry = float(trade.price or 0)
                qty = float(trade.quantity or 0)
                signal_str = trade.signal.lower() if isinstance(trade.signal, str) else trade.signal.value.lower()

                pnl = (current_price - entry) * qty if signal_str == "buy" else (entry - current_price) * qty
                trade.pnl_usdt = round(pnl, 4)
                trade.status = TradeStatus.executed
                trade.executed_at = datetime.utcnow()

                emoji = "🟢" if pnl >= 0 else "🔴"
                logger.info(f"{emoji} Closed {trade.symbol}: ${pnl:+.4f} USDT")

            await db.commit()
            logger.info(f"✅ Closed {len(trades)} trades")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Auto-close error: {e}", exc_info=True)
