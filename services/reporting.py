import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from hyperliquid.info import Info

from config.settings import Settings

logger = logging.getLogger("Reporting")

REPORT_WINDOW_SECONDS = 24 * 3600


def _seconds_until_next(hour: int, tz: ZoneInfo) -> float:
    """Seconds from now until the next HH:00 in the given timezone."""
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _account_snapshot(info: Info, address: str) -> tuple[float, float]:
    """(open position value, margin left to spend).

    Perps wallet only — spot USDC cannot back a perp position, so counting it here
    would overstate what is actually left to trade. See _fetch_account_state in
    trade_executor.
    """
    user_state = info.user_state(address)
    margin = user_state.get("marginSummary", {})
    margin_used = float(margin.get("totalMarginUsed", 0))
    account_value = float(margin.get("accountValue", 0))
    open_value = sum(
        abs(float(ap.get("position", {}).get("positionValue", 0)))
        for ap in user_state.get("assetPositions", [])
        if float(ap.get("position", {}).get("szi", 0)) != 0
    )
    return open_value, account_value - margin_used


async def build_daily_report(info: Info, settings: Settings) -> str | None:
    """Daily report covering the trailing 24h. Returns None when there were no entries
    or exits in the window (so the bot only reports on active days)."""
    address = settings.hl_account_address
    now_ms = int(datetime.now().timestamp() * 1000)
    start_ms = now_ms - REPORT_WINDOW_SECONDS * 1000

    fills = await asyncio.to_thread(info.user_fills_by_time, address, start_ms, now_ms)

    entries = sum(1 for f in fills if "Open" in f.get("dir", ""))
    exits = sum(1 for f in fills if "Close" in f.get("dir", ""))
    if entries == 0 and exits == 0:
        return None

    pnl = sum(float(f.get("closedPnl", 0)) for f in fills) - sum(
        float(f.get("fee", 0)) for f in fills
    )
    open_value, margin_left = await asyncio.to_thread(_account_snapshot, info, address)

    pnl_sign = "+" if pnl >= 0 else ""
    return (
        f"📊 Daily report — last 24h\n\n"
        f"📈 Open position value: <code>${open_value:,.2f}</code>\n"
        f"💰 Margin left to spend: <code>${margin_left:,.2f}</code>\n"
        f"🏁 Trades: <code>{entries} opened / {exits} closed</code>\n"
        f"💵 PnL (24h): <code>{pnl_sign}${pnl:,.2f}</code>"
    )


async def run_daily_report(info: Info, settings: Settings, notify, stop_event: asyncio.Event) -> None:
    tz = ZoneInfo(settings.report_tz)
    logger.info(f"Started — daily report at {settings.report_hour:02d}:00 {settings.report_tz}")
    try:
        while not stop_event.is_set():
            delay = _seconds_until_next(settings.report_hour, tz)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return  # stop_event set
            except asyncio.TimeoutError:
                pass  # time to report

            try:
                report = await build_daily_report(info, settings)
                if report:
                    await notify(report)
                    logger.info("Daily report sent")
                else:
                    logger.info("Daily report skipped — no entries/exits in last 24h")
            except Exception as error:
                logger.error(f"Daily report failed: {error}", exc_info=True)
    except asyncio.CancelledError:
        logger.warning("Daily report task cancelled")
        raise
    finally:
        logger.info("Daily report exited")
