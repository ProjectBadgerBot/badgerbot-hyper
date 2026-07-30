import asyncio
import json
import logging
import math
import time
from pathlib import Path
from typing import Awaitable, Callable

import eth_account
from hyperliquid.api import API
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings
from services.signal_consumer import log_signal, validate_signal

logger = logging.getLogger("TradeExecutor")

Notifier = Callable[[str], Awaitable[None]]

LEVERAGE_CONFIG_PATH = Path(__file__).parent.parent / "config" / "coin_leverage.json"

_sz_decimals_cache: dict[str, int] = {}


def safe_spot_meta(api_url: str) -> dict:
    """Fetch spot metadata with out-of-bounds universe entries filtered out."""
    raw = API(api_url).post("/info", {"type": "spotMeta"})
    token_count = len(raw.get("tokens", []))
    raw["universe"] = [
        entry for entry in raw.get("universe", [])
        if all(idx < token_count for idx in entry.get("tokens", []))
    ]
    return raw


def load_leverage_config() -> dict:
    with open(LEVERAGE_CONFIG_PATH) as file:
        return json.load(file)


def build_exchange(settings: Settings) -> Exchange:
    wallet = eth_account.Account.from_key(settings.hl_api_private_key)
    # account_address required — without it Exchange uses API wallet address (empty account)
    return Exchange(wallet, constants.MAINNET_API_URL, account_address=settings.hl_account_address, spot_meta=safe_spot_meta(constants.MAINNET_API_URL))


def _fetch_sz_decimals(info: Info) -> dict[str, int]:
    meta = info.meta()
    return {asset["name"]: asset["szDecimals"] for asset in meta["universe"]}


async def ensure_sz_decimals_cached(info: Info) -> None:
    global _sz_decimals_cache
    if not _sz_decimals_cache:
        _sz_decimals_cache = await asyncio.to_thread(_fetch_sz_decimals, info)


# Perps equity at or below this counts as empty — a spot transfer leaves dust behind,
# which is enough to make a plain `> 0` check pass.
MIN_EQUITY_USD = 0.01


def has_perps_equity(equity: float) -> bool:
    """True when the perps wallet holds more than dust."""
    return equity > MIN_EQUITY_USD


# An account sitting at its margin cap rejects every incoming signal, so this rejection
# repeats at signal cadence (~every 5 min) for as long as the account stays full — which
# buries Telegram in identical alerts. Notify once per window and tally the rest. Every
# rejection is still logged and recorded in signal_log regardless.
BALANCE_NOTIFY_COOLDOWN_SECONDS = 6 * 3600
INSUFFICIENT_BALANCE_PREFIX = "insufficient balance"

_last_balance_notify_at: float | None = None
_balance_rejections_muted = 0


def is_balance_rejection(rejection: str) -> bool:
    return rejection.startswith(INSUFFICIENT_BALANCE_PREFIX)


def claim_balance_notify_slot(now: float | None = None) -> bool:
    """True at most once per BALANCE_NOTIFY_COOLDOWN_SECONDS; counts the muted ones.

    monotonic() so a system clock adjustment cannot mute alerts for hours. The None
    sentinel (rather than 0.0) makes the first rejection always notify — monotonic()
    counts from boot, so a 0.0 start would read as "just notified" and swallow it.
    """
    global _last_balance_notify_at, _balance_rejections_muted
    now = time.monotonic() if now is None else now
    if (
        _last_balance_notify_at is not None
        and now - _last_balance_notify_at < BALANCE_NOTIFY_COOLDOWN_SECONDS
    ):
        _balance_rejections_muted += 1
        return False
    _last_balance_notify_at = now
    return True


def take_muted_balance_count() -> int:
    """Muted rejections since the last notification. Resets the tally."""
    global _balance_rejections_muted
    count = _balance_rejections_muted
    _balance_rejections_muted = 0
    return count


def fetch_spot_usdc(info: Info, address: str) -> float:
    """USDC held in the spot wallet. Reported only — it cannot back a perp position."""
    spot_state = info.spot_user_state(address)
    for balance in spot_state.get("balances", []):
        if balance["coin"] == "USDC":
            return float(balance["total"])
    return 0.0


def _fetch_account_state(info: Info, address: str) -> tuple[float, float]:
    """Returns (equity, available_margin) for the PERPS wallet.

    Available margin is equity minus margin already locked by open positions — what's
    actually free to back a new entry.

    Spot and perps are separate balances on Hyperliquid: USDC sitting in spot cannot
    collateralise a perp until it is transferred across. This previously returned
    max(perps_equity, spot_usdc), which on an account holding most of its USDC in spot
    sized every trade against money the perps engine cannot touch — and subtracted
    perps margin_used from a spot balance to get "available", mixing the two pools.
    """
    user_state = info.user_state(address)
    margin = user_state.get("marginSummary", {})
    equity = float(margin.get("accountValue", 0))
    margin_used = float(margin.get("totalMarginUsed", 0))
    return equity, equity - margin_used


def _fetch_account_equity(info: Info, address: str) -> float:
    return _fetch_account_state(info, address)[0]


async def fetch_account_equity(info: Info, address: str) -> float:
    return await asyncio.to_thread(_fetch_account_equity, info, address)


async def fetch_account_state(info: Info, address: str) -> tuple[float, float]:
    return await asyncio.to_thread(_fetch_account_state, info, address)


def _fetch_position_szi(info: Info, address: str, coin: str) -> float:
    """Signed position size for a coin (>0 long, <0 short, 0 flat)."""
    user_state = info.user_state(address)
    for ap in user_state.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == coin:
            return float(pos.get("szi", 0))
    return 0.0


async def current_position_direction(info: Info, address: str, coin: str) -> str | None:
    """Returns "LONG", "SHORT", or None (flat) for the coin's current net position."""
    szi = await asyncio.to_thread(_fetch_position_szi, info, address, coin)
    if szi > 0:
        return "LONG"
    if szi < 0:
        return "SHORT"
    return None


async def fetch_mark_price(info: Info, coin: str) -> float:
    all_mids = await asyncio.to_thread(info.all_mids)
    if coin not in all_mids:
        raise ValueError(f"Coin {coin} not found in mark prices")
    return float(all_mids[coin])


def calculate_position_size(
    equity: float, mark_price: float, position_size_pct: float, sz_decimals: int
) -> float:
    notional = equity * position_size_pct
    return round(notional / mark_price, sz_decimals)


def calculate_risk_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    sl_price: float,
    batch_size: int,
    sz_decimals: int,
) -> float:
    risk_per_signal = equity * risk_pct / batch_size
    price_distance = abs(entry_price - sl_price)
    if price_distance == 0:
        return 0.0
    return round(risk_per_signal / price_distance, sz_decimals)


async def _validate_and_size(
    signal: dict,
    info: Info,
    settings: Settings,
    leverage_config: dict,
    batch_size: int = 1,
) -> tuple[float, float, float, int, str] | None:
    """Returns (mark_price, size, equity, leverage, rejection_reason) or None on fetch error.
    rejection_reason is empty string if valid."""
    coin = signal["coin_symbol"]
    leverage = leverage_config.get(coin, leverage_config.get("DEFAULT", 3))
    try:
        mark_price = await fetch_mark_price(info, coin)
    except Exception as error:
        logger.error(f"Failed to fetch mark price | coin={coin} | {error}")
        return None
    rejection = validate_signal(signal, mark_price, settings)
    if rejection:
        return mark_price, 0, 0, leverage, rejection
    is_long = signal["mode"] == "LONG"
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])
    if is_long and mark_price >= tp_price:
        rejection = f"mark price {mark_price} already at or past TP {tp_price}"
        logger.warning(f"Signal dropped: {rejection} | coin={coin}")
        return mark_price, 0, 0, leverage, rejection
    if not is_long and mark_price <= tp_price:
        rejection = f"mark price {mark_price} already at or past TP {tp_price}"
        logger.warning(f"Signal dropped: {rejection} | coin={coin}")
        return mark_price, 0, 0, leverage, rejection
    equity, available_margin = await fetch_account_state(info, settings.hl_account_address)
    if not has_perps_equity(equity):
        # Distinguish "no money" from "money in the wrong wallet" — the second is a
        # one-transfer fix, and reporting a bare "zero equity" hides that.
        spot_usdc = await asyncio.to_thread(
            fetch_spot_usdc, info, settings.hl_account_address
        )
        if spot_usdc > 0:
            rejection = "no perps collateral — USDC is in the spot wallet"
            logger.error(f"Signal dropped: {rejection} | coin={coin}")
            return mark_price, 0, 0, leverage, rejection
        logger.error(f"Account equity is zero — skipping | coin={coin}")
        return mark_price, 0, 0, leverage, "zero equity"
    await ensure_sz_decimals_cached(info)
    sz_decimals = _sz_decimals_cache.get(coin, 4)
    if settings.risk_pct is not None:
        entry_price = float(signal["price"])
        size = calculate_risk_size(
            equity, settings.risk_pct, entry_price, sl_price, batch_size, sz_decimals
        )
        logger.info(
            f"Risk sizing | coin={coin} | risk={settings.risk_pct * 100:.1f}%"
            f" | batch={batch_size} | entry={entry_price} | sl={sl_price}"
            f" | distance={abs(entry_price - sl_price):.2f} | size={size}"
        )
    elif settings.position_size_usd is not None:
        notional = settings.position_size_usd * leverage
        size = round(notional / mark_price, sz_decimals)
    else:
        size = calculate_position_size(
            equity, mark_price, settings.position_size_pct, sz_decimals
        )
    if size <= 0:
        logger.warning(f"Calculated size is zero — skipping | coin={coin}")
        return mark_price, 0, 0, leverage, "zero size"
    # Floor the lot so its smallest-notional leg clears Hyperliquid's $10 execution minimum.
    # A lot's TP and SL share its size (they close the same lot), so the binding leg is the
    # lowest-priced of entry/TP/SL — for a short that's the TP, for a long the SL, exactly the
    # legs that get rejected when too small. min() picks the right one without branching.
    binding_px = min(mark_price, tp_price, sl_price)
    floor = settings.min_close_notional_usd
    min_size = math.ceil(floor / binding_px * 10 ** sz_decimals) / 10 ** sz_decimals
    if size < min_size:
        logger.info(
            f"Lot bumped to ${floor:.2f} min-close-notional | coin={coin}"
            f" | binding_px={binding_px} | old_size={size} | new_size={min_size}"
        )
        size = min_size
    notional = size * mark_price
    required_margin = notional / leverage
    if required_margin > available_margin:
        rejection = (
            f"insufficient balance — required margin ${required_margin:.2f}"
            f" > available ${available_margin:.2f}"
        )
        logger.warning(f"Signal dropped: {rejection} | coin={coin}")
        return mark_price, 0, 0, leverage, rejection
    return mark_price, size, equity, leverage, ""


def _px_decimals(exchange: Exchange, coin: str) -> int:
    coin_name = exchange.info.name_to_coin[coin]
    asset = exchange.info.coin_to_asset[coin_name]
    return 6 - exchange.info.asset_to_sz_decimals[asset]


def _round_price(exchange: Exchange, coin: str, px: float) -> float:
    # HL requires prices with at most 5 significant figures.
    # round(mark * 1.05, 2) can produce 6-sig-fig prices (e.g. 2059.21) which are rejected
    # with "Invalid TP/SL price". Apply the same 5g rounding as the SDK's _slippage_price.
    return round(float(f"{px:.5g}"), _px_decimals(exchange, coin))


def _trigger_limit_px(
    exchange: Exchange, coin: str, is_buy: bool, trigger_px: float
) -> float:
    # limit_px must be aggressive (worse than trigger) so the order always fills when triggered.
    slippage = 0.05
    adjusted = trigger_px * (1 + slippage if is_buy else 1 - slippage)
    return round(float(f"{adjusted:.5g}"), _px_decimals(exchange, coin))


def _tpsl_status_ok(inner, label: str, coin: str) -> bool:
    """Check a single status item from a bulk_orders response."""
    # Trigger orders return the string "waitingForTrigger" on success.
    if inner == "waitingForTrigger":
        return True
    if isinstance(inner, dict):
        if "error" in inner:
            logger.error(
                f"{label} placement inner error | coin={coin} | error={inner['error']}"
            )
            return False
        if "resting" in inner or "filled" in inner:
            return True
    logger.error(f"{label} placement — unexpected status | coin={coin} | inner={inner}")
    return False


async def _open_with_tpsl(
    exchange: Exchange,
    info: Info,
    settings: Settings,
    coin: str,
    is_long: bool,
    size: float,
    leverage: int,
    tp_price: float,
    sl_price: float,
    mark_price: float,
) -> tuple[float | None, bool, bool]:
    """
    Opens a position and places per-lot TP/SL atomically via normalTpsl grouping.

    normalTpsl requires a non-trigger entry order as the first order in the batch —
    the TP and SL trigger orders are paired to that specific lot, giving each lot its
    own independent OCO pair (unlike positionTpsl, which is position-level and would
    overwrite TP/SL from other lots on the same coin).

    We do not track per-order ids: Hyperliquid is the source of truth. The position
    monitor reconciles live trigger orders against the live position size each cycle.

    Returns (fill_price, tp_ok, sl_ok). fill_price is None on entry failure.
    """
    await asyncio.to_thread(exchange.update_leverage, leverage, coin, True)
    logger.info(f"Leverage set: {coin} {leverage}x cross")

    closing_is_buy = not is_long

    # Entry: GTC limit priced just past the current mid to fill as a taker immediately.
    # 0.1% is more than enough to cross the spread on HL perps — we don't need 2% here
    # because the limit is just a ceiling, not what we actually pay (fill is at the ask).
    # MAX_PRICE_DEVIATION_PCT is the quality gate for stale/drifted signals; this slippage
    # is purely mechanical to ensure immediate fill.
    entry_slippage = 0.001
    entry_limit = _round_price(
        exchange, coin, mark_price * (1 + entry_slippage if is_long else 1 - entry_slippage)
    )

    # Round trigger prices to 5 significant figures — HL rejects 6+ sig figs.
    tp_price = _round_price(exchange, coin, tp_price)
    sl_price = _round_price(exchange, coin, sl_price)
    tp_limit = _trigger_limit_px(exchange, coin, closing_is_buy, tp_price)
    sl_limit = _trigger_limit_px(exchange, coin, closing_is_buy, sl_price)

    orders = [
        {   # Entry order — the "main" order required by normalTpsl
            "coin": coin,
            "is_buy": is_long,
            "sz": size,
            "limit_px": entry_limit,
            "order_type": {"limit": {"tif": "Gtc"}},
            "reduce_only": False,
        },
        {   # Take profit
            "coin": coin,
            "is_buy": closing_is_buy,
            "sz": size,
            "limit_px": tp_limit,
            "order_type": {"trigger": {"triggerPx": tp_price, "isMarket": True, "tpsl": "tp"}},
            "reduce_only": True,
        },
        {   # Stop loss
            "coin": coin,
            "is_buy": closing_is_buy,
            "sz": size,
            "limit_px": sl_limit,
            "order_type": {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
            "reduce_only": True,
        },
    ]

    try:
        result = await asyncio.to_thread(
            exchange.bulk_orders,
            orders,
            None,
            "normalTpsl",
        )
    except Exception as error:
        logger.error(f"Entry+TP/SL order exception | coin={coin} | {error}")
        return None, False, False

    if result.get("status") != "ok":
        logger.error(f"Entry+TP/SL placement failed | coin={coin} | result={result}")
        return None, False, False

    statuses = result.get("response", {}).get("data", {}).get("statuses", [])

    # Parse entry fill (statuses[0])
    fill_price = None
    entry_inner = statuses[0] if statuses else {}
    if isinstance(entry_inner, dict):
        if "filled" in entry_inner:
            fill_price = float(entry_inner["filled"]["avgPx"])
            logger.info(f"Entry filled @ {fill_price} | coin={coin}")
        elif "resting" in entry_inner:
            # Order is pending (unusual with aggressive pricing) — TP/SL are still paired
            fill_price = entry_limit
            logger.warning(
                f"Entry resting @ limit={entry_limit} (not filled immediately) | coin={coin}"
            )
        elif "error" in entry_inner:
            logger.error(f"Entry order error | coin={coin} | error={entry_inner['error']}")
            return None, False, False
    else:
        logger.error(f"Entry — unexpected status | coin={coin} | inner={entry_inner}")
        return None, False, False

    # Parse TP/SL statuses (statuses[1] and statuses[2])
    tp_inner = statuses[1] if len(statuses) > 1 else {}
    sl_inner = statuses[2] if len(statuses) > 2 else {}
    tp_ok = _tpsl_status_ok(tp_inner, "TP", coin)
    sl_ok = _tpsl_status_ok(sl_inner, "SL", coin)

    if tp_ok:
        logger.info(f"TP placed @ {tp_price} (limit={tp_limit}) | coin={coin}")
    if sl_ok:
        logger.info(f"SL placed @ {sl_price} (limit={sl_limit}) | coin={coin}")

    return fill_price, tp_ok, sl_ok


def _should_notify_rejection(rejection: str) -> bool:
    """Balance rejections are rate-limited; every other reason notifies immediately."""
    if not is_balance_rejection(rejection):
        return True
    return claim_balance_notify_slot()


def _format_rejection(coin: str, direction: str, rejection: str) -> str:
    message = f"⏭ {coin} {direction} skipped — <code>{rejection}</code>"
    if not is_balance_rejection(rejection):
        return message
    # Only reachable right after claiming the slot, so the tally is the muted run that
    # this message stands in for — without it the mute would hide how jammed the account is.
    muted = take_muted_balance_count()
    if muted:
        hours = BALANCE_NOTIFY_COOLDOWN_SECONDS // 3600
        message += f"\n<i>+{muted} more muted in the last {hours}h</i>"
    return message


async def execute_signal(
    signal: dict,
    info: Info,
    exchange: Exchange,
    settings: Settings,
    leverage_config: dict,
    notify: Notifier | None = None,
    batch_size: int = 1,
) -> None:
    coin = signal["coin_symbol"]
    is_long = signal["mode"] == "LONG"
    direction = "LONG" if is_long else "SHORT"
    tp_price = float(signal["tp_price"])
    sl_price = float(signal["sl_price"])

    # No direction flipping: while a coin holds opposite exposure, drop the signal so a
    # single netted position never carries TP/SL pointing the wrong way. Same-direction
    # signals add to the position; we re-enter the opposite side only once it is flat.
    open_direction = await current_position_direction(
        info, settings.hl_account_address, coin
    )
    if open_direction is not None and open_direction != direction:
        reason = f"opposite to open {open_direction} position"
        logger.info(f"Signal dropped: {reason} | coin={coin}")
        log_signal({"coin": coin, "side": direction, "outcome": "rejected", "reason": reason})
        return

    sizing = await _validate_and_size(
        signal, info, settings, leverage_config, batch_size
    )
    if sizing is None:
        log_signal(
            {
                "coin": coin,
                "side": direction,
                "outcome": "error",
                "reason": "fetch failed",
            }
        )
        return
    mark_price, size, _, leverage, rejection = sizing
    if rejection:
        log_signal(
            {
                "coin": coin,
                "side": direction,
                "outcome": "rejected",
                "reason": rejection,
            }
        )
        if notify and _should_notify_rejection(rejection):
            await notify(_format_rejection(coin, direction, rejection))
        return
    logger.info(
        f"EXECUTING: {coin} {'LONG' if is_long else 'SHORT'}"
        f" | size={size} | notional=${size * mark_price:.2f} | leverage={leverage}x"
    )

    fill_price, tp_ok, sl_ok = await _open_with_tpsl(
        exchange, info, settings, coin, is_long, size, leverage, tp_price, sl_price, mark_price
    )
    if fill_price is None:
        log_signal(
            {
                "coin": coin,
                "side": direction,
                "outcome": "error",
                "reason": "order not filled",
            }
        )
        if notify:
            await notify(
                f"⚠️ {coin} {direction} — <code>order placed but did not fill</code>"
            )
        return

    # No DB write: Hyperliquid is the source of truth. The position monitor reconciles
    # live trigger orders against the live position size every cycle.
    if not tp_ok or not sl_ok:
        logger.error(f"POSITION UNPROTECTED — TP/SL failed | coin={coin}")
        if notify:
            await notify(
                f"⚠️ UNPROTECTED: {coin} {direction} @ <code>${fill_price:,.2f}</code> — TP/SL placement failed!"
            )
        return

    log_signal(
        {
            "coin": coin,
            "side": direction,
            "outcome": "filled",
            "entry": fill_price,
            "size": size,
        }
    )
    logger.info(
        f"Trade complete | coin={coin} | entry={fill_price} | TP={tp_price} | SL={sl_price}"
    )


def make_signal_handler(
    info: Info,
    exchange: Exchange,
    settings: Settings,
    leverage_config: dict,
    notify: Notifier | None = None,
    bot_state=None,
):
    async def handler(signal: dict, batch_size: int = 1) -> None:
        if bot_state and bot_state.paused:
            logger.info(
                f"Bot paused — signal dropped | coin={signal.get('coin_symbol')}"
            )
            return
        try:
            await execute_signal(
                signal, info, exchange, settings, leverage_config, notify, batch_size
            )
        except Exception as error:
            logger.error(f"Unexpected error in execute_signal | {error}", exc_info=True)

    return handler
