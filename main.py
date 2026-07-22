import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings, load_settings
from services.position_monitor import run_position_monitor
from services.reporting import run_daily_report
from services.signal_consumer import connect_and_listen
from services.updater import run_updater
from services.telegram_bot import BotState, TelegramBot
from services.trade_executor import build_exchange, has_perps_equity, load_leverage_config, make_signal_handler, safe_spot_meta

LOGS_DIR = Path(__file__).parent / "logs"


def configure_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / "hyperbot.log"

    file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(name)s] %(message)s")
    )

    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler], force=True)


def connect_to_hyperliquid(settings: Settings) -> Info:
    return Info(constants.MAINNET_API_URL, skip_ws=True, spot_meta=safe_spot_meta(constants.MAINNET_API_URL))


async def create_info(settings: Settings, logger: logging.Logger) -> Info:
    try:
        return await asyncio.to_thread(connect_to_hyperliquid, settings)
    except Exception as error:
        logger.error(f"Failed to connect to Hyperliquid: {error}")
        sys.exit(1)


def fetch_spot_usdc_balance(info: Info, address: str) -> float:
    spot_state = info.spot_user_state(address)
    for balance in spot_state.get("balances", []):
        if balance["coin"] == "USDC":
            return float(balance["total"])
    return 0.0


def format_startup_equity(perps_equity: float, spot_usdc: float) -> str:
    """Startup equity line. Perps equity is what trades are sized against; spot USDC is
    called out separately because it cannot collateralise a perp until transferred."""
    line = f"${perps_equity:,.2f} (perps)"
    if spot_usdc > 0:
        line += f" | ${spot_usdc:,.2f} USDC in spot — NOT tradeable until transferred"
    return line


def format_open_positions(asset_positions: list) -> str:
    open_positions = [
        p for p in asset_positions
        if "position" in p and float(p["position"]["szi"]) != 0
    ]
    if not open_positions:
        return "0 open positions"
    lines = [
        f"  {p['position']['coin']} | size={p['position']['szi']} | entry={p['position'].get('entryPx', 'N/A')}"
        for p in open_positions
    ]
    return f"{len(lines)} open position(s):\n" + "\n".join(lines)


async def run_startup_check(settings: Settings, info: Info, logger: logging.Logger) -> None:
    logger.info(f"Connecting to Hyperliquid MAINNET ({constants.MAINNET_API_URL})")

    try:
        user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    except Exception as error:
        logger.error(f"Failed to connect to Hyperliquid: {error}")
        sys.exit(1)

    margin_summary = user_state.get("marginSummary", {})
    perps_equity = float(margin_summary.get("accountValue", 0))
    spot_usdc = await asyncio.to_thread(
        fetch_spot_usdc_balance, info, settings.hl_account_address
    )
    asset_positions = user_state.get("assetPositions", [])

    logger.info("Connected to Hyperliquid MAINNET")
    logger.info(
        f"Account: {settings.hl_account_address}"
        f" | Equity: {format_startup_equity(perps_equity, spot_usdc)}"
    )
    if not has_perps_equity(perps_equity) and spot_usdc > 0:
        logger.warning(
            f"No perps collateral — ${spot_usdc:,.2f} USDC is in the spot wallet."
            f" Trades cannot open."
        )
    logger.info(format_open_positions(asset_positions))
    logger.info("All services ready. Starting loop...")


async def main() -> None:
    configure_logging()
    logger = logging.getLogger("Main")

    settings = load_settings()
    info = await create_info(settings, logger)
    await run_startup_check(settings, info, logger)

    exchange = await asyncio.to_thread(build_exchange, settings)
    leverage_config = load_leverage_config()
    bot_state = BotState()
    telegram_bot = TelegramBot(settings, info, exchange, bot_state, leverage_config)
    signal_handler = make_signal_handler(
        info, exchange, settings, leverage_config, telegram_bot.send, bot_state
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("Starting signal consumer, position monitor, and Telegram bot...")

    # Named tasks let us identify which critical service died. Anything in this list is
    # load-bearing — if any of them exit (cleanly or otherwise) while the bot is supposed
    # to be running, we treat it as a crash, alert the user, cancel siblings, and exit
    # non-zero so systemd restarts the unit. The previous gather(return_exceptions=True)
    # swallowed exceptions and kept the bot half-alive, which is what caused 6 days of
    # zombie trades after position_monitor died silently on a 502.
    named_tasks: list[tuple[str, asyncio.Task]] = [
        ("signal_consumer", asyncio.create_task(
            connect_and_listen(signal_handler, settings, stop_event, notify=telegram_bot.send),
            name="signal_consumer",
        )),
        ("telegram_bot", asyncio.create_task(
            telegram_bot.run(stop_event), name="telegram_bot",
        )),
        ("position_monitor", asyncio.create_task(
            run_position_monitor(info, settings, telegram_bot.send, stop_event, exchange),
            name="position_monitor",
        )),
        ("daily_report", asyncio.create_task(
            run_daily_report(info, settings, telegram_bot.send, stop_event),
            name="daily_report",
        )),
    ]

    if settings.auto_update_enabled:
        named_tasks.append(("updater", asyncio.create_task(
            run_updater(settings.auto_update_interval_hours, stop_event, notify=telegram_bot.send),
            name="updater",
        )))

    tasks = [t for _, t in named_tasks]
    name_by_task = {t: n for n, t in named_tasks}

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    fatal_exception = None
    for finished in done:
        name = name_by_task[finished]
        if finished.cancelled():
            logger.warning(f"Service '{name}' was cancelled")
            continue
        exc = finished.exception()
        if exc is not None:
            logger.critical(f"Service '{name}' crashed: {exc}", exc_info=exc)
            try:
                await telegram_bot.send(
                    f"🚨 Service <code>{name}</code> crashed: <code>{exc}</code>\n"
                    f"Bot is shutting down for restart."
                )
            except Exception as notify_error:
                logger.error(f"Failed to send crash notification: {notify_error}")
            fatal_exception = exc
        else:
            # A critical task returned normally while stop_event was NOT set — also a crash.
            if not stop_event.is_set():
                logger.critical(f"Service '{name}' exited unexpectedly (clean return)")
                try:
                    await telegram_bot.send(
                        f"🚨 Service <code>{name}</code> exited unexpectedly — bot shutting down for restart."
                    )
                except Exception as notify_error:
                    logger.error(f"Failed to send crash notification: {notify_error}")
                fatal_exception = RuntimeError(f"{name} exited unexpectedly")

    stop_event.set()
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    if fatal_exception is not None:
        logger.error("Bot stopped due to service crash — exiting non-zero for systemd restart")
        sys.exit(1)

    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())