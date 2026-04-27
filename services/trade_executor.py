import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

import eth_account
from hyperliquid.api import API
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

from config.settings import Settings
from services.signal_consumer import log_signal, validate_signal
from storage.trade_log import insert_trade, update_entry_fee, update_trade_status

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


def _fetch_account_state(info: Info, address: str) -> tuple[float, float]:
    # Returns (equity, available_margin). Available margin is equity minus margin already
    # locked by open positions — what's actually free to back a new entry.
    # Unified accounts: spot USDC is the total collateral pool; perps equity is only the
    # margin portion. Standard accounts: perps equity is the full pool; spot USDC is 0.
    # max() returns the correct total for both account types.
    user_state = info.user_state(address)
    margin = user_state.get("marginSummary", {})
    perps_equity = float(margin.get("accountValue", 0))
    margin_used = float(margin.get("totalMarginUsed", 0))
    spot_state = info.spot_user_state(address)
    spot_usdc = 0.0
    for balance in spot_state.get("balances", []):
        if balance["coin"] == "USDC":
            spot_usdc = float(balance["total"])
            break
    equity = max(perps_equity, spot_usdc)
    return equity, equity - margin_used


def _fetch_account_equity(info: Info, address: str) -> float:
    return _fetch_account_state(info, address)[0]


async def fetch_account_equity(info: Info, address: str) -> float:
    return await asyncio.to_thread(_fetch_account_equity, info, address)


async def fetch_account_state(info: Info, address: str) -> tuple[float, float]:
    return await asyncio.to_thread(_fetch_account_state, info, address)


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
    if is_long and mark_price >= tp_price:
        rejection = f"mark price {mark_price} already at or past TP {tp_price}"
        logger.warning(f"Signal dropped: {rejection} | coin={coin}")
        return mark_price, 0, 0, leverage, rejection
    if not is_long and mark_price <= tp_price:
        rejection = f"mark price {mark_price} already at or past TP {tp_price}"
        logger.warning(f"Signal dropped: {rejection} | coin={coin}")
        return mark_price, 0, 0, leverage, rejection
    equity, available_margin = await fetch_account_state(info, settings.hl_account_address)
    if equity <= 0:
        logger.error(f"Account equity is zero — skipping | coin={coin}")
        return mark_price, 0, 0, leverage, "zero equity"
    await ensure_sz_decimals_cached(info)
    sz_decimals = _sz_decimals_cache.get(coin, 4)
    if settings.risk_pct is not None:
        entry_price = float(signal["price"])
        sl_price = float(signal["sl_price"])
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
    notional = size * mark_price
    if notional < 10.0:
        min_size = round(10.0 / mark_price + 10 ** (-sz_decimals), sz_decimals)
        logger.info(
            f"Position bumped to $10 min | coin={coin}"
            f" | original=${notional:.2f} | new_size={min_size}"
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


def _fetch_post_trade_state(info: Info, address: str, coin: str) -> dict:
    user_state = info.user_state(address)
    spot_state = info.spot_user_state(address)
    margin = user_state.get("marginSummary", {})
    margin_used = float(margin.get("totalMarginUsed", 0))
    spot_usdc = next(
        (
            float(b["total"])
            for b in spot_state.get("balances", [])
            if b["coin"] == "USDC"
        ),
        0.0,
    )
    perps_equity = float(margin.get("accountValue", 0))
    account_value = max(perps_equity, spot_usdc)
    available = account_value - margin_used
    liq_px = None
    for ap in user_state.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == coin and float(pos.get("szi", 0)) != 0:
            raw_liq = pos.get("liquidationPx")
            if raw_liq:
                liq_px = float(raw_liq)
            break
    return {
        "account_value": account_value,
        "margin_used": margin_used,
        "withdrawable": available,
        "liq_px": liq_px,
    }


def _extract_trigger_oid(inner) -> str:
    """Pull the order id from a TP/SL status item. Returns "" when HL only returned
    the bare "waitingForTrigger" string (no oid available in that path)."""
    if isinstance(inner, dict):
        for key in ("resting", "filled"):
            oid = inner.get(key, {}).get("oid") if isinstance(inner.get(key), dict) else None
            if oid is not None:
                return str(oid)
    return ""


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
    coin: str,
    is_long: bool,
    size: float,
    leverage: int,
    tp_price: float,
    sl_price: float,
    mark_price: float,
) -> tuple[float | None, bool, bool, str, str]:
    """
    Opens a position and places per-lot TP/SL atomically via normalTpsl grouping.

    normalTpsl requires a non-trigger entry order as the first order in the batch —
    the TP and SL trigger orders are paired to that specific lot, giving each lot its
    own independent OCO pair (unlike positionTpsl, which is position-level and would
    overwrite TP/SL from other lots on the same coin).

    Returns (fill_price, tp_ok, sl_ok, tp_oid, sl_oid). fill_price is None on entry
    failure; OIDs are empty strings when unavailable.
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
        return None, False, False, "", ""

    if result.get("status") != "ok":
        logger.error(f"Entry+TP/SL placement failed | coin={coin} | result={result}")
        return None, False, False, "", ""

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
            return None, False, False, "", ""
    else:
        logger.error(f"Entry — unexpected status | coin={coin} | inner={entry_inner}")
        return None, False, False, "", ""

    # Parse TP/SL statuses (statuses[1] and statuses[2])
    tp_inner = statuses[1] if len(statuses) > 1 else {}
    sl_inner = statuses[2] if len(statuses) > 2 else {}
    tp_ok = _tpsl_status_ok(tp_inner, "TP", coin)
    sl_ok = _tpsl_status_ok(sl_inner, "SL", coin)

    tp_oid = _extract_trigger_oid(tp_inner) if tp_ok else ""
    sl_oid = _extract_trigger_oid(sl_inner) if sl_ok else ""

    if tp_ok:
        logger.info(f"TP placed @ {tp_price} (limit={tp_limit}) | oid={tp_oid} | coin={coin}")
    if sl_ok:
        logger.info(f"SL placed @ {sl_price} (limit={sl_limit}) | oid={sl_oid} | coin={coin}")

    return fill_price, tp_ok, sl_ok, tp_oid, sl_oid


async def _capture_entry_fee(
    info: Info, settings: Settings, trade_id: int, coin: str, entry_px: float
) -> None:
    """Best-effort: fetch the entry fill fee and store it in the DB."""
    try:
        fills = await asyncio.to_thread(info.user_fills, settings.hl_account_address)
        for fill in fills:
            if fill.get("coin") != coin or "Open" not in fill.get("dir", ""):
                continue
            if abs(float(fill["px"]) - entry_px) / entry_px < 0.01:
                await update_entry_fee(trade_id, float(fill.get("fee", 0)))
                logger.info(
                    f"Entry fee captured | trade={trade_id} | fee={fill['fee']}"
                )
                return
        logger.warning(f"Entry fill not found for fee capture | trade={trade_id}")
    except Exception as error:
        logger.warning(f"Entry fee capture failed | trade={trade_id} | {error}")


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
        if notify:
            await notify(f"⏭ {coin} {direction} skipped — <code>{rejection}</code>")
        return
    logger.info(
        f"EXECUTING: {coin} {'LONG' if is_long else 'SHORT'}"
        f" | size={size} | notional=${size * mark_price:.2f} | leverage={leverage}x"
    )

    fill_price, tp_ok, sl_ok, tp_oid, sl_oid = await _open_with_tpsl(
        exchange, coin, is_long, size, leverage, tp_price, sl_price, mark_price
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

    # Write trade record immediately after entry — Financial Safety Rule #4
    trade_id = await insert_trade(
        coin, direction, size, fill_price, tp_price, sl_price,
        tp_order_id=tp_oid, sl_order_id=sl_oid,
    )

    direction_emoji = "🟢" if is_long else "🔴"

    if not tp_ok or not sl_ok:
        await update_trade_status(trade_id, "UNPROTECTED")
        logger.error(
            f"POSITION UNPROTECTED — TP/SL failed | coin={coin} | trade_id={trade_id}"
        )
        if notify:
            await notify(
                f"⚠️ UNPROTECTED: {coin} {direction} @ <code>${fill_price:,.2f}</code> — TP/SL placement failed!"
            )
        return

    asyncio.create_task(_capture_entry_fee(info, settings, trade_id, coin, fill_price))
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
    if notify:
        post = await asyncio.to_thread(
            _fetch_post_trade_state, info, settings.hl_account_address, coin
        )
        notional = size * fill_price
        liq_str = f"${post['liq_px']:,.2f}" if post["liq_px"] else "N/A"
        margin_pct = (
            (post["margin_used"] / post["account_value"] * 100)
            if post["account_value"] > 0
            else 0
        )
        await notify(
            f"{direction_emoji} {coin} {direction} OPENED\n\n"
            f"📐 Size: <code>{size} (${notional:,.2f})</code>\n"
            f"💵 Entry: <code>${fill_price:,.2f}</code>\n"
            f"✅ TP: <code>${tp_price:,.2f}</code>\n"
            f"⛔ SL: <code>${sl_price:,.2f}</code>\n"
            f"⚡ Leverage: <code>{leverage}x</code>\n"
            f"💀 Liq: <code>{liq_str}</code>\n\n"
            f"🏦 Account Value: <code>${post['account_value']:,.2f}</code>\n"
            f"🎢 Margin Used: <code>${post['margin_used']:,.2f} ({margin_pct:.1f}%)</code>\n"
            f"💰 Available: <code>${post['withdrawable']:,.2f}</code>"
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
