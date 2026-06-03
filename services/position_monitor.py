import asyncio
import logging
import time

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from config.settings import Settings

logger = logging.getLogger("PositionMonitor")

# Monotonic timestamp of the last unprotected-trade notification (aggregate, 12h cooldown)
_last_unprotected_alert: float = 0.0
UNPROTECTED_ALERT_COOLDOWN_SECONDS = 43200  # 12 hours


def _open_positions(user_state: dict) -> dict[str, float]:
    """{coin: signed szi} for every coin with a non-zero position."""
    positions = {}
    for ap in user_state.get("assetPositions", []):
        pos = ap.get("position", {})
        coin = pos.get("coin", "")
        szi = float(pos.get("szi", 0))
        if coin and szi != 0:
            positions[coin] = szi
    return positions


def _flatten_orders(raw_orders: list) -> list:
    flat = []
    for order in raw_orders:
        flat.append(order)
        flat.extend(order.get("children", []))
    return flat


def _classify_triggers(orders: list) -> dict[str, dict[str, list[dict]]]:
    """Group reduce-only trigger orders per coin into {'tp': [...], 'sl': [...]}.

    Each entry keeps the live oid and size straight off Hyperliquid — we never rely on
    stored ids. TP vs SL is read from the order type ('Take Profit' / 'Stop')."""
    by_coin: dict[str, dict[str, list[dict]]] = {}
    for o in orders:
        if not o.get("triggerPx"):
            continue
        coin = o.get("coin", "")
        kind = o.get("orderType", "").lower()
        if "profit" in kind:
            side = "tp"
        elif "stop" in kind:
            side = "sl"
        else:
            continue
        try:
            entry = {"oid": int(o["oid"]), "sz": float(o.get("sz", 0))}
        except (TypeError, ValueError, KeyError):
            continue
        by_coin.setdefault(coin, {"tp": [], "sl": []})[side].append(entry)
    return by_coin


def _excess_to_cancel(orders: list[dict], position: float) -> list[int]:
    """Oids to cancel so the summed order size drops toward `position` without ever
    falling below it. Oldest-first (smallest oid). When position == 0 this sweeps all."""
    tol = max(position * 0.01, 1e-8)
    remaining = sum(o["sz"] for o in orders)
    to_cancel: list[int] = []
    for o in sorted(orders, key=lambda x: x["oid"]):
        if remaining - o["sz"] >= position - tol:
            to_cancel.append(o["oid"])
            remaining -= o["sz"]
    return to_cancel


async def _cancel_oids(exchange: Exchange, coin: str, oids: list[int]) -> None:
    if not oids:
        return
    try:
        result = await asyncio.to_thread(
            exchange.bulk_cancel, [{"coin": coin, "oid": oid} for oid in oids]
        )
        if result.get("status") == "ok":
            logger.info(f"Cancelled {len(oids)} leftover trigger order(s) | coin={coin} | oids={oids}")
        else:
            logger.warning(f"Leftover cancel non-ok | coin={coin} | oids={oids} | result={result}")
    except Exception as error:
        logger.warning(f"Leftover cancel failed | coin={coin} | oids={oids} | {error}")


def _is_under_covered(position: float, tp_sum: float, sl_sum: float) -> bool:
    """A live position is unprotected when either side's summed trigger size is short."""
    if position <= 0:
        return False
    tol = max(position * 0.01, 1e-8)
    return tp_sum < position - tol or sl_sum < position - tol


async def find_unprotected_coins(info: Info, settings: Settings) -> list[dict]:
    """Coins whose live position is missing full TP or SL coverage on Hyperliquid.
    Returns [{coin, position, tp_sum, sl_sum, side}]."""
    user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    try:
        raw_orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Unprotected check: failed to fetch orders | {error}")
        return []

    positions = _open_positions(user_state)
    triggers = _classify_triggers(_flatten_orders(raw_orders))

    unprotected = []
    for coin, szi in positions.items():
        pos = abs(szi)
        coin_trig = triggers.get(coin, {"tp": [], "sl": []})
        tp_sum = sum(o["sz"] for o in coin_trig["tp"])
        sl_sum = sum(o["sz"] for o in coin_trig["sl"])
        if _is_under_covered(pos, tp_sum, sl_sum):
            unprotected.append({
                "coin": coin,
                "position": pos,
                "tp_sum": tp_sum,
                "sl_sum": sl_sum,
                "side": "LONG" if szi > 0 else "SHORT",
            })
    return unprotected


async def _reconcile(info: Info, settings: Settings, exchange: Exchange, notify) -> None:
    """Compare live trigger orders to the live position per coin. Cancel leftover
    over-coverage by oid; alert on under-coverage."""
    global _last_unprotected_alert

    user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    try:
        raw_orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Reconcile: failed to fetch orders | {error}")
        return

    positions = _open_positions(user_state)
    triggers = _classify_triggers(_flatten_orders(raw_orders))

    # Every coin that has either a position or live trigger orders.
    coins = set(positions) | set(triggers)
    unprotected = []

    for coin in coins:
        pos = abs(positions.get(coin, 0.0))
        coin_trig = triggers.get(coin, {"tp": [], "sl": []})
        tp_sum = sum(o["sz"] for o in coin_trig["tp"])
        sl_sum = sum(o["sz"] for o in coin_trig["sl"])
        tol = max(pos * 0.01, 1e-8)

        # Over-coverage (incl. pos == 0 → sweep all): cancel leftover reduce-only orders
        # BY OID, never below the live position. By-oid only — the price/size heuristic is
        # what mis-cancelled a live SL on 2026-05-17.
        if tp_sum > pos + tol:
            await _cancel_oids(exchange, coin, _excess_to_cancel(coin_trig["tp"], pos))
        if sl_sum > pos + tol:
            await _cancel_oids(exchange, coin, _excess_to_cancel(coin_trig["sl"], pos))

        if _is_under_covered(pos, tp_sum, sl_sum):
            side = "LONG" if positions.get(coin, 0) > 0 else "SHORT"
            logger.warning(
                f"UNPROTECTED | coin={coin} {side} | pos={pos:.6f}"
                f" | tp_sum={tp_sum:.6f} sl_sum={sl_sum:.6f}"
            )
            unprotected.append(coin)

    if unprotected:
        now = time.monotonic()
        if now - _last_unprotected_alert >= UNPROTECTED_ALERT_COOLDOWN_SECONDS or _last_unprotected_alert == 0:
            count = len(unprotected)
            await notify(
                f"⚠️ {count} position{'s' if count > 1 else ''} missing full TP/SL coverage "
                f"on Hyperliquid: <code>{', '.join(unprotected)}</code>\n\n"
                f"Use /unprotected to inspect."
            )
            _last_unprotected_alert = now
    else:
        _last_unprotected_alert = 0.0


async def run_position_monitor(
    info, settings, notify, stop_event: asyncio.Event, exchange: Exchange = None
) -> None:
    logger.info(f"Started — polling every {settings.position_poll_interval_seconds}s")

    # Heartbeat every N cycles so silence is visible in logs (silent task death caused the
    # 6-day zombie incident in May).
    heartbeat_every = 20
    cycle = 0

    try:
        while not stop_event.is_set():
            cycle += 1
            try:
                await _reconcile(info, settings, exchange, notify)
            except Exception as error:
                logger.error(f"Poll cycle error: {error}", exc_info=True)

            if cycle % heartbeat_every == 0:
                logger.info(f"heartbeat | cycle={cycle}")

            try:
                await asyncio.sleep(settings.position_poll_interval_seconds)
            except asyncio.CancelledError:
                logger.warning("Sleep cancelled — exiting monitor loop")
                raise
    except asyncio.CancelledError:
        logger.warning("PositionMonitor task cancelled")
        raise
    except BaseException as error:
        logger.critical(f"PositionMonitor crashed: {error}", exc_info=True)
        raise
    finally:
        logger.info("PositionMonitor exited")
