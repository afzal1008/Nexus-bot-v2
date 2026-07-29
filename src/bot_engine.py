"""
Bot Engine - Uses multiple free APIs with fallback
Primary: Kraken (no rate limits, works in UAE)
Fallback: CoinGecko

SPOT-ONLY TRADING MODE:
- BUY opens a new paper position (only if one isn't already open for that symbol)
- SELL only closes an existing open position and realizes its P&L
- There is no shorting — a SELL signal with nothing open is simply ignored

RISK MANAGEMENT (v3):
- Each trade gets its own ATR-based stop-loss/take-profit, sized to that coin's
  actual recent volatility, capped by the user's configured max % as a safety ceiling.
- A 60s background check force-closes anything that hits its stop/target,
  independent of the technical signal.
- 4-hour force-close remains as a final backstop.

SIGNAL QUALITY (v3):
- Confidence threshold raised to 40% (was 25%) — requires broader indicator agreement.
- ADX trend-strength filter discounts MACD/EMA signals in choppy/ranging markets.
- CoinGecko fallback lookback extended to 30 days (was 7) for more stable indicators.
"""
import logging
import httpx
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PAIRS = ["BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT"]
BASE_PAIRS = list(PAIRS)

GAINER_THRESHOLD_PCT = 2.5   # lower bar so more coins qualify
MAX_GAINERS = 5              # allow more extra coins per loop

MIN_TRADE_USDT = 500.0            # minimum paper allocation per trade
STARTING_BALANCE_USDT = 10000.0   # starting/reference paper wallet size
MIN_CONFIDENCE_PCT = 40           # minimum signal confidence to act on (raised from 25)

DEFAULT_STOP_LOSS_PCT = 8.0     # fallback cap if a user has no value set — 8% loss
DEFAULT_TAKE_PROFIT_PCT = 15.0  # fallback cap if a user has no value set — 15% gain

# ATR-based stop/target multipliers — the actual per-trade levels, capped by the user's settings
ATR_STOP_MULTIPLIER = 1.5     # stop-loss = 1.5x ATR% away from entry
ATR_TARGET_MULTIPLIER = 3.0   # take-profit = 3x ATR% away from entry (≈2:1 reward:risk vs the stop)
MIN_STOP_PCT = 2.0             # never set a stop tighter than this, even on very calm coins
MIN_TARGET_PCT = 3.0           # never set a target tighter than this

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

_gainers_cache = {"data": [], "fetched_at": None}
GAINERS_CACHE_SECONDS = 300

_candles_cache = {}  # symbol -> {"data": [...], "fetched_at": datetime}
CANDLES_CACHE_SECONDS = 300  # 5 minutes — avoids re-fetching full OHLC every 60s loop

# Postgres advisory lock IDs — prevent the same scheduled job from running twice at once
# across multiple app instances (e.g. during a Render deploy overlap window).
LOCK_BOT_MAIN_LOOP = 987001
LOCK_RISK_CHECK = 987002
LOCK_AUTO_CLOSE = 987003


async def try_acquire_xact_lock(conn, lock_id: int) -> bool:
    """Try to grab a Postgres TRANSACTION-scoped advisory lock on an already-open
    connection/transaction. Unlike a session-scoped lock, this is guaranteed by Postgres
    to auto-release the instant the transaction ends (commit or rollback) — no explicit
    unlock call needed, and no risk of a connection-pooling edge case leaving it stuck
    forever (which is what a session-scoped lock is vulnerable to)."""
    from sqlalchemy import text
    result = await conn.execute(text("SELECT pg_try_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
    return bool(result.scalar())


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
            check_stop_loss_take_profit, "interval", seconds=60,
            id="nexus-risk-check", replace_existing=True
        )
        self.scheduler.add_job(
            auto_close_trades, "interval", minutes=10,
            id="nexus-auto-close", replace_existing=True
        )
        logger.info("✅ Bot scheduler started - signal loop + risk check every 60s")


scheduler_manager = SchedulerManager()


async def fetch_candles_kraken(symbol: str) -> list:
    """Fetch from Kraken - free, no auth needed, works everywhere.
    interval=60 (60-minute candles) with Kraken's default ~720-candle return
    already gives ~30 days of history."""
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
    """Fallback: CoinGecko — 30 days (was 7) for more stable MACD/EMA/ADX readings.
    Cached for 5 minutes per symbol to avoid hammering CoinGecko's free-tier rate limit
    every 60-second loop — indicators don't meaningfully change minute-to-minute anyway."""
    now = datetime.utcnow()
    cached = _candles_cache.get(symbol)
    if cached and (now - cached["fetched_at"]).total_seconds() < CANDLES_CACHE_SECONDS:
        return cached["data"]

    coin_id = COINGECKO_IDS.get(symbol, "bitcoin")
    try:
        await asyncio.sleep(2)  # Rate limit delay
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
            _candles_cache[symbol] = {"data": candles, "fetched_at": now}
            return candles
    except Exception as e:
        logger.warning(f"CoinGecko failed for {symbol}: {e}")
        if cached:
            logger.info(f"Using stale cached candles for {symbol} (rate limited)")
            return cached["data"]
        return []


async def fetch_candles(symbol: str) -> list:
    """Try Kraken first, fallback to CoinGecko"""
    candles = await fetch_candles_kraken(symbol)
    if len(candles) >= 10:
        return candles
    logger.info(f"Falling back to CoinGecko for {symbol}")
    return await fetch_candles_coingecko(symbol)


async def fetch_current_price(symbol: str) -> float:
    """Get price from Kraken only (base pairs)"""
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


async def resolve_coingecko_id(symbol: str) -> str:
    """Last-resort lookup: find a coin's CoinGecko id via live search.
    Needed because COINGECKO_IDS is in-memory only and resets on every restart/redeploy —
    this recovers price lookups for a symbol whose id was lost from memory."""
    base = symbol.split("/")[0]
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/search",
                params={"query": base}
            )
            resp.raise_for_status()
            data = resp.json()
            coins = data.get("coins", [])
            for c in coins:
                if c.get("symbol", "").upper() == base.upper():
                    COINGECKO_IDS[symbol] = c.get("id")
                    return c.get("id")
            if coins:
                coin_id = coins[0].get("id")
                COINGECKO_IDS[symbol] = coin_id
                return coin_id
    except Exception as e:
        logger.warning(f"CoinGecko search failed for {symbol}: {e}")
    return None


async def get_price_any(symbol: str, coingecko_id: str = None) -> float:
    """Get current price for ANY monitored symbol — Kraken primary, CoinGecko fallback.
    Pass coingecko_id (saved on the Trade row) so this survives the in-memory cache resetting."""
    price = await fetch_current_price(symbol)
    if price > 0:
        return price

    coin_id = coingecko_id or COINGECKO_IDS.get(symbol)
    if not coin_id:
        coin_id = await resolve_coingecko_id(symbol)

    if coin_id:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": coin_id, "vs_currencies": "usd"}
                )
                resp.raise_for_status()
                data = resp.json()
                p = data.get(coin_id, {}).get("usd")
                if p:
                    return float(p)
        except Exception as e:
            logger.warning(f"CoinGecko price fetch failed for {symbol}: {e}")

    return 0.0


async def fetch_top_gainers() -> list:
    """Get top gaining coins from CoinGecko to add to monitoring — cached to avoid rate limits"""
    now = datetime.utcnow()
    if _gainers_cache["fetched_at"] and (now - _gainers_cache["fetched_at"]).total_seconds() < GAINERS_CACHE_SECONDS:
        return _gainers_cache["data"]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "price_change_percentage_24h_desc",
                    "per_page": 20,
                    "page": 1,
                    "price_change_percentage": "24h"
                }
            )
            resp.raise_for_status()
            data = resp.json()
            gainers = []
            for coin in data:
                symbol = coin["symbol"].upper() + "/USDT"
                if symbol not in BASE_PAIRS and coin.get("price_change_percentage_24h", 0) > GAINER_THRESHOLD_PCT:
                    gainers.append(symbol)
                    COINGECKO_IDS[symbol] = coin["id"]
                    if len(gainers) >= MAX_GAINERS:
                        break
            if not gainers:
                logger.info(f"No coins currently exceed +{GAINER_THRESHOLD_PCT}% (24h) — using base pairs only")
            _gainers_cache["data"] = gainers
            _gainers_cache["fetched_at"] = now
            return gainers
    except Exception as e:
        logger.warning(f"Top gainers fetch failed: {e}")
        return _gainers_cache["data"]


async def bot_main_loop():
    try:
        from database import get_db, User
        from sqlalchemy import select

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            if not await try_acquire_xact_lock(db, LOCK_BOT_MAIN_LOOP):
                logger.info("⏭️ bot_main_loop skipped — another instance is already running it (deploy overlap protection)")
                return

            result = await db.execute(
                select(User).where(User.bot_enabled == True)
            )
            users = result.scalars().all()

            if not users:
                logger.info("No active users")
                return

            global PAIRS
            top_gainers = await fetch_top_gainers()
            PAIRS = BASE_PAIRS + top_gainers
            if top_gainers:
                logger.info(f"Adding top gainers to monitoring: {top_gainers}")
            else:
                logger.info("No qualifying gainers this cycle — monitoring base pairs only")

            logger.info(f"Processing {len(users)} users — monitoring {len(PAIRS)} pairs")
            for user in users:
                await process_user(user, db)

            await db.commit()
            logger.info("✅ Bot loop completed")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"❌ Bot loop error: {e}", exc_info=True)


def compute_atr_levels(entry_price: float, atr_pct: float, user_stop_cap_pct: float, user_target_cap_pct: float):
    """Turn a coin's ATR% into an actual stop-loss/take-profit price, sized to that
    coin's own volatility but capped by the user's configured safety limits."""
    stop_pct = max(MIN_STOP_PCT, min(atr_pct * ATR_STOP_MULTIPLIER, user_stop_cap_pct))
    target_pct = max(MIN_TARGET_PCT, min(atr_pct * ATR_TARGET_MULTIPLIER, user_target_cap_pct * 1.5))
    stop_loss_price = round(entry_price * (1 - stop_pct / 100.0), 8)
    take_profit_price = round(entry_price * (1 + target_pct / 100.0), 8)
    return stop_loss_price, take_profit_price, stop_pct, target_pct


async def process_user(user, db):
    """Spot-only logic: BUY opens a position, SELL only closes an existing one. No shorting."""
    from signal_engine import generate_signal
    from database import Trade, TradeStatus
    from sqlalchemy import select, and_

    stop_cap = float(user.stop_loss_pct) if getattr(user, "stop_loss_pct", None) is not None else DEFAULT_STOP_LOSS_PCT
    target_cap = float(user.take_profit_pct) if getattr(user, "take_profit_pct", None) is not None else DEFAULT_TAKE_PROFIT_PCT

    for symbol in PAIRS:
        try:
            await asyncio.sleep(1)  # avoid hammering rate limits between pairs

            candles = await fetch_candles(symbol)
            if len(candles) < 10:
                logger.warning(f"Not enough candles for {symbol}: {len(candles)}")
                continue

            result = generate_signal(candles)
            candle_price = candles[-1]["close"]  # used for RSI/MACD/EMA/ADX/ATR calculation — can lag by hours for thin coins

            if isinstance(result.signal, str):
                signal_str = result.signal.lower()
            else:
                signal_str = result.signal.value.lower()

            logger.info(
                f"[{symbol}] {signal_str.upper()} "
                f"conf={result.confidence}% "
                f"price=${candle_price:,.2f} "
                f"RSI={result.rsi} ADX={result.adx} ATR%={result.atr_pct}"
            )

            if signal_str == "hold" or result.confidence < MIN_CONFIDENCE_PCT:
                continue

            existing = await db.execute(
                select(Trade).where(
                    and_(
                        Trade.user_id == user.id,
                        Trade.symbol == symbol,
                        Trade.status == TradeStatus.pending
                    )
                ).limit(1)
            )
            open_trade = existing.scalars().first()

            if signal_str == "buy":
                if open_trade:
                    logger.info(f"Already holding {symbol} — skipping new buy signal")
                    continue

                trade_amount = max(MIN_TRADE_USDT, float(user.trade_amount_usdt or MIN_TRADE_USDT))
                current_balance = float(user.paper_balance_usdt if user.paper_balance_usdt is not None else STARTING_BALANCE_USDT)

                if current_balance < trade_amount:
                    logger.info(
                        f"Skipping {symbol} for {user.email} — insufficient paper balance "
                        f"(${current_balance:.2f} < ${trade_amount:.2f})"
                    )
                    continue

                coingecko_id = COINGECKO_IDS.get(symbol)
                live_price = await get_price_any(symbol, coingecko_id)
                entry_price = live_price if live_price > 0 else candle_price

                qty = round(trade_amount / entry_price, 8) if entry_price > 0 else 0

                sl_price, tp_price, sl_pct, tp_pct = compute_atr_levels(
                    entry_price, result.atr_pct, stop_cap, target_cap
                )

                trade = Trade(
                    user_id=user.id,
                    exchange_name="paper_trading",
                    symbol=symbol,
                    signal="buy",
                    confidence=result.confidence,
                    price=entry_price,
                    quantity=qty,
                    total_usdt=trade_amount,
                    rsi=result.rsi,
                    macd=result.macd,
                    bb_position=result.bb_position,
                    coingecko_id=coingecko_id,
                    stop_loss_price=sl_price,
                    take_profit_price=tp_price,
                    status=TradeStatus.pending,
                    created_at=datetime.utcnow()
                )
                db.add(trade)
                user.paper_balance_usdt = current_balance - trade_amount

                logger.info(
                    f"✅ BOUGHT {symbol} @ ${entry_price:,.2f} (live) "
                    f"[candle close was ${candle_price:,.2f}] "
                    f"qty={qty} amount=${trade_amount:.2f} "
                    f"ATR-stop=${sl_price:,.4f} ({sl_pct:.1f}%) ATR-target=${tp_price:,.4f} ({tp_pct:.1f}%) "
                    f"(balance now ${user.paper_balance_usdt:.2f})"
                )

            elif signal_str == "sell":
                if not open_trade:
                    logger.info(f"Sell signal for {symbol} ignored — no open position to close (spot-only, no shorting)")
                    continue

                coingecko_id = open_trade.coingecko_id or COINGECKO_IDS.get(symbol)
                live_price = await get_price_any(symbol, coingecko_id)
                exit_price = live_price if live_price > 0 else candle_price

                entry = float(open_trade.price or 0)
                qty = float(open_trade.quantity or 0)
                pnl = (exit_price - entry) * qty

                open_trade.exit_price = exit_price
                open_trade.pnl_usdt = round(pnl, 4)
                open_trade.status = TradeStatus.executed
                open_trade.executed_at = datetime.utcnow()
                open_trade.close_reason = "signal"

                principal = float(open_trade.total_usdt or 0)
                user.paper_balance_usdt = float(user.paper_balance_usdt or 0) + principal + pnl

                emoji = "🟢" if pnl >= 0 else "🔴"
                logger.info(
                    f"{emoji} SOLD {symbol} @ ${exit_price:,.2f} (live) — closed position (signal), "
                    f"P&L=${pnl:+.4f} (balance now ${user.paper_balance_usdt:.2f})"
                )

        except Exception as e:
            logger.error(f"Error for {symbol}: {e}", exc_info=True)


async def check_stop_loss_take_profit():
    """Runs every 60s for ALL open positions across ALL users, independent of the technical
    signal. Uses each trade's own ATR-based stop/target price if set; falls back to the
    user's fixed percentage for any older trade that predates the ATR feature."""
    try:
        from database import get_db, Trade, TradeStatus, User
        from sqlalchemy import select

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            if not await try_acquire_xact_lock(db, LOCK_RISK_CHECK):
                logger.info("⏭️ check_stop_loss_take_profit skipped — another instance is already running it")
                return

            result = await db.execute(select(Trade).where(Trade.status == TradeStatus.pending))
            open_trades = result.scalars().all()
            if not open_trades:
                return

            user_cache = {}
            closed_count = 0

            for trade in open_trades:
                entry = float(trade.price or 0)
                if entry <= 0:
                    continue

                current_price = await get_price_any(trade.symbol, trade.coingecko_id)
                if current_price <= 0:
                    continue

                user = user_cache.get(trade.user_id)
                if user is None:
                    user_result = await db.execute(select(User).where(User.id == trade.user_id))
                    user = user_result.scalar_one_or_none()
                    user_cache[trade.user_id] = user

                if trade.stop_loss_price is not None and trade.take_profit_price is not None:
                    hit_stop = current_price <= trade.stop_loss_price
                    hit_target = current_price >= trade.take_profit_price
                else:
                    # Legacy fallback for trades opened before ATR-based levels existed
                    stop_loss_pct = float(user.stop_loss_pct) if user and user.stop_loss_pct is not None else DEFAULT_STOP_LOSS_PCT
                    take_profit_pct = float(user.take_profit_pct) if user and user.take_profit_pct is not None else DEFAULT_TAKE_PROFIT_PCT
                    change_pct = (current_price - entry) / entry
                    hit_stop = change_pct <= -(stop_loss_pct / 100.0)
                    hit_target = change_pct >= (take_profit_pct / 100.0)

                if not (hit_stop or hit_target):
                    continue

                qty = float(trade.quantity or 0)
                pnl = (current_price - entry) * qty

                trade.exit_price = current_price
                trade.pnl_usdt = round(pnl, 4)
                trade.status = TradeStatus.executed
                trade.executed_at = datetime.utcnow()
                trade.close_reason = "stop_loss" if hit_stop else "take_profit"

                if user:
                    principal = float(trade.total_usdt or 0)
                    user.paper_balance_usdt = float(user.paper_balance_usdt or 0) + principal + pnl

                change_pct_display = ((current_price - entry) / entry) * 100
                reason = "STOP-LOSS" if hit_stop else "TAKE-PROFIT"
                emoji = "🛑" if hit_stop else "🎯"
                logger.info(
                    f"{emoji} {reason} on {trade.symbol}: {change_pct_display:+.2f}% "
                    f"— closed @ ${current_price:,.2f}, P&L=${pnl:+.4f}"
                )
                closed_count += 1

            if closed_count:
                await db.commit()
                logger.info(f"Risk check closed {closed_count} position(s)")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Stop-loss/take-profit check error: {e}", exc_info=True)


async def auto_close_trades():
    """Final safety net: force-close any position still open after 4 hours, settle wallet."""
    try:
        from database import get_db, Trade, TradeStatus, User
        from sqlalchemy import select, and_

        async_gen = get_db()
        db = await async_gen.__anext__()

        try:
            if not await try_acquire_xact_lock(db, LOCK_AUTO_CLOSE):
                logger.info("⏭️ auto_close_trades skipped — another instance is already running it")
                return

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

            logger.info(f"Auto-closing {len(trades)} trades (held over 4 hours)")

            for trade in trades:
                current_price = await get_price_any(trade.symbol, trade.coingecko_id)
                if current_price <= 0:
                    continue

                entry = float(trade.price or 0)
                qty = float(trade.quantity or 0)
                pnl = (current_price - entry) * qty

                trade.exit_price = current_price
                trade.pnl_usdt = round(pnl, 4)
                trade.status = TradeStatus.executed
                trade.executed_at = datetime.utcnow()
                trade.close_reason = "timeout_4h"

                user_result = await db.execute(select(User).where(User.id == trade.user_id))
                user = user_result.scalar_one_or_none()
                if user:
                    principal = float(trade.total_usdt or 0)
                    user.paper_balance_usdt = float(user.paper_balance_usdt or 0) + principal + pnl

                emoji = "🟢" if pnl >= 0 else "🔴"
                logger.info(f"{emoji} Auto-closed {trade.symbol}: ${pnl:+.4f} USDT")

            await db.commit()
            logger.info(f"✅ Closed {len(trades)} trades")

        finally:
            await db.close()

    except Exception as e:
        logger.error(f"Auto-close error: {e}", exc_info=True)
