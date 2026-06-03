"""
Simulate trade signals by injecting them directly into the trade pipeline.

By default, fires one ETH LONG and one ETH SHORT (one minute apart) at the current mark
price with a small TP/SL offset. Order size is always the minimum allowed for the coin
(10^(-szDecimals)), bypassing equity-based sizing so the order always triggers regardless
of account balance.

WARNING: This places REAL orders on Hyperliquid mainnet.

Run from the project directory:
    .venv/bin/python simulate_signals.py            # one LONG + one SHORT (default)
    .venv/bin/python simulate_signals.py --mode long
    .venv/bin/python simulate_signals.py --mode short
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import load_settings
from services.telegram_bot import BotState, TelegramBot
from services.trade_executor import (
    _open_with_tpsl,
    build_exchange,
    load_leverage_config,
    safe_spot_meta,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(name)s] %(message)s",
    force=True,
)
logger = logging.getLogger("Simulator")


def _b(text) -> str:
    return f"<code>{text}</code>"


COIN = "ETH"
MINIMUM_NOTIONAL_USD = 11.0  # HL enforces $10 minimum notional; pad to $11 for safety

SIGNAL_TEMPLATES = {
    "long": {"mode": "LONG", "tp_offset": 0.05, "sl_offset": -0.03},
    "short": {"mode": "SHORT", "tp_offset": -0.05, "sl_offset": 0.03},
}
DELAY_BETWEEN_SIGNALS_SECONDS = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate BadgerBot trade signals")
    parser.add_argument(
        "--mode",
        choices=["long", "short", "both"],
        default="both",
        help="Which direction(s) to simulate (default: both)",
    )
    return parser.parse_args()


async def fetch_min_size(info: Info, coin: str) -> float:
    meta = await asyncio.to_thread(info.meta)
    for asset in meta["universe"]:
        if asset["name"] == coin:
            sz_decimals = asset["szDecimals"]
            all_mids = await asyncio.to_thread(info.all_mids)
            mark_price = float(all_mids[coin])
            # Use the larger of precision minimum and notional minimum.
            # HL enforces a $10 minimum notional — szDecimals alone is not enough.
            precision_min = round(10 ** (-sz_decimals), sz_decimals)
            notional_min = round(MINIMUM_NOTIONAL_USD / mark_price, sz_decimals)
            min_size = max(precision_min, notional_min)
            logger.info(
                f"Min order size for {coin}: {min_size}"
                f" (szDecimals={sz_decimals}, mark={mark_price:.2f},"
                f" notional=${min_size * mark_price:.2f})"
            )
            return min_size
    raise ValueError(f"Coin {coin} not found in meta")


async def build_signal(info: Info, template: dict) -> dict:
    all_mids = await asyncio.to_thread(info.all_mids)
    mark_price = float(all_mids[COIN])
    return {
        "coin_symbol": COIN,
        "price": mark_price,
        "tp_price": mark_price * (1 + template["tp_offset"]),
        "sl_price": mark_price * (1 + template["sl_offset"]),
        "mode": template["mode"],
        "dispatched_at": datetime.now(timezone.utc).isoformat(),
    }


async def execute_with_fixed_size(
    signal: dict, size: float, info, exchange, settings, leverage_config: dict, notify
) -> bool:
    coin = signal["coin_symbol"]
    is_long = signal["mode"] == "LONG"
    direction = signal["mode"]
    direction_emoji = "🟢" if is_long else "🔴"
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])
    leverage = leverage_config.get(coin, leverage_config.get("DEFAULT", 3))

    fill_price, tp_ok, sl_ok = await _open_with_tpsl(
        exchange, info, settings, coin, is_long, size, leverage,
        tp_price, sl_price, float(signal["price"]),
    )
    if fill_price is None:
        logger.error(f"Entry failed | coin={coin}")
        return False

    if not tp_ok or not sl_ok:
        logger.error(f"POSITION UNPROTECTED | coin={coin}")
        await notify(f"⚠️ UNPROTECTED: {coin} {direction} @ ${fill_price:,.2f} — TP/SL failed!")
    else:
        logger.info(f"Trade complete | entry={fill_price} | TP={tp_price:.2f} | SL={sl_price:.2f}")
        notional = size * fill_price
        await notify(
            f"{direction_emoji} {coin} {direction} OPENED\n\n"
            f"📐 Size: {size} (${notional:,.2f})\n"
            f"💵 Entry: ${fill_price:,.2f}\n"
            f"🎯 TP: ${tp_price:,.2f}\n"
            f"⛔ SL: ${sl_price:,.2f}\n"
            f"⚡ Leverage: {leverage}x"
        )
    return True


async def run_simulation(mode: str) -> None:
    settings = load_settings()
    logger.info("Network: MAINNET")

    if mode == "both":
        templates = [SIGNAL_TEMPLATES["long"], SIGNAL_TEMPLATES["short"]]
    else:
        templates = [SIGNAL_TEMPLATES[mode]]

    directions = [t["mode"] for t in templates]
    logger.info(f"Simulating {len(templates)} signal(s): {directions}, {DELAY_BETWEEN_SIGNALS_SECONDS}s apart")

    info = Info(constants.MAINNET_API_URL, skip_ws=True, spot_meta=safe_spot_meta(constants.MAINNET_API_URL))
    exchange = build_exchange(settings)
    leverage_config = load_leverage_config()

    bot_state = BotState()
    telegram_bot = TelegramBot(settings, info, exchange, bot_state)

    min_size = await fetch_min_size(info, COIN)

    async with telegram_bot._app:
        directions = " + ".join(t["mode"] for t in templates)
        await telegram_bot.send(
            f"🧪 Simulation started\n\n"
            f"📊 Directions: {_b(directions)}\n"
            f"🪙 Coin: {_b(COIN)}\n"
            f"🌐 Network: {_b('MAINNET')}"
        )

        opened_coins = set()
        for index, template in enumerate(templates):
            signal = await build_signal(info, template)
            logger.info(
                f"Signal {index + 1}/{len(templates)}:"
                f" {COIN} {template['mode']} @ {signal['price']:.2f}"
                f" | TP: {signal['tp_price']:.2f}"
                f" | SL: {signal['sl_price']:.2f}"
                f" | size: {min_size}"
            )

            if await execute_with_fixed_size(
                signal, min_size, info, exchange, settings, leverage_config, telegram_bot.send
            ):
                opened_coins.add(COIN)

            if index < len(templates) - 1:
                logger.info(f"Waiting {DELAY_BETWEEN_SIGNALS_SECONDS}s before next signal...")
                await asyncio.sleep(DELAY_BETWEEN_SIGNALS_SECONDS)

        await close_simulation_trades(
            info, exchange, settings.hl_account_address, telegram_bot.send, opened_coins
        )

    logger.info("Simulation complete.")


async def close_simulation_trades(info, exchange, address: str, notify, coins: set[str]) -> None:
    """Market-close the simulated coins and cancel any leftover trigger orders by live oid."""
    if not coins:
        logger.info("No simulation trades were opened — nothing to close.")
        return

    logger.info(f"Auto-closing simulation trades | coins={sorted(coins)}")

    try:
        all_orders = await asyncio.to_thread(info.frontend_open_orders, address)
    except Exception as error:
        logger.error(f"Failed to fetch open orders for cleanup: {error}")
        all_orders = []

    lines = []
    for coin in sorted(coins):
        fill_px = None
        try:
            result = await asyncio.to_thread(exchange.market_close, coin, slippage=0.02)
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            filled = statuses[0].get("filled") if statuses else None
            if filled:
                fill_px = float(filled["avgPx"])
        except Exception as error:
            logger.warning(f"market_close for {coin} returned no fill: {error}")

        oids = [o["oid"] for o in all_orders if o.get("coin") == coin and o.get("isTrigger")]
        if oids:
            try:
                await asyncio.to_thread(
                    exchange.bulk_cancel,
                    [{"coin": coin, "oid": oid} for oid in oids],
                )
                logger.info(f"Cancelled {len(oids)} TP/SL order(s) for {coin}")
            except Exception as error:
                logger.error(f"Failed to cancel TP/SL for {coin}: {error}")

        if fill_px:
            lines.append(f"🧹 {coin} closed @ ${fill_px:,.2f}")
        else:
            lines.append(f"🧹 {coin} netted to zero — TP/SL cancelled")

    await notify("🧹 Simulation complete — trades closed\n\n" + "\n".join(lines))
    logger.info("Simulation cleanup complete")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run_simulation(args.mode))
