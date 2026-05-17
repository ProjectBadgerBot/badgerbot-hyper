import asyncio
import logging
from datetime import datetime, timezone

from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from config.settings import Settings
from storage.trade_log import close_trade, fetch_open_trades, net_pnl, update_entry_fee

logger = logging.getLogger("PositionMonitor")

_alerted_orphan_coins: set[str] = set()

# {coin: {"count": int, "next_log_at": int}} — tracks fill retry backoff per coin
_fill_retry_state: dict[str, dict] = {}

import time as _time

# Monotonic timestamp of the last unprotected-trade notification (aggregate, 12h cooldown)
_last_unprotected_alert: float = 0.0
UNPROTECTED_ALERT_COOLDOWN_SECONDS = 43200  # 12 hours


async def _cancel_counterpart_order(
    exchange: Exchange,
    info: Info,
    settings: Settings,
    trade: dict,
    close_status: str,
) -> None:
    """Cancel the orphaned TP or SL order after its counterpart was hit."""
    # If TP was hit, cancel the SL. If SL was hit, cancel the TP.
    if close_status == "TP":
        target_px = float(trade["sl_px"])
        target_oid = trade.get("sl_oid")
        label = "SL"
    else:
        target_px = float(trade["tp_px"])
        target_oid = trade.get("tp_oid")
        label = "TP"

    coin = trade["coin"]
    size = float(trade["size"])

    # Primary path: cancel directly by stored OID — unambiguous even when tight/wide
    # variants share an identical size under capital_insert_pct sizing.
    if target_oid:
        try:
            result = await asyncio.to_thread(exchange.cancel, coin, int(target_oid))
            if result.get("status") == "ok":
                logger.info(
                    f"Cancelled orphaned {label} by oid | coin={coin} | oid={target_oid}"
                )
                return
            logger.warning(
                f"Cancel {label} by oid non-ok | coin={coin} | oid={target_oid} | result={result}"
            )
        except Exception as error:
            logger.warning(
                f"Cancel {label} by oid failed | coin={coin} | oid={target_oid} | {error}"
            )
        # Heuristic fallback intentionally disabled even when the by-oid path fails:
        # if the OID is stale, the order was likely already cancelled or filled — no
        # action needed. The heuristic risks cancelling another trade's protection.
        return

    # SAFETY GUARD (2026-05-17 incident prevention): never fall back to the price+size
    # heuristic. The heuristic can match a *different* trade's live TP/SL when DB rows
    # share similar sizes (e.g. 0.0047 recurring lots), causing the bot to cancel
    # legitimate protection. If we don't have an OID, accept the orphan and log loudly.
    logger.warning(
        f"Cannot cancel {label} — no stored OID and heuristic fallback disabled"
        f" | coin={coin} | target_px={target_px} | trade_id={trade.get('id')}"
    )


def _extract_open_positions(user_state: dict) -> dict[str, float]:
    """Returns {coin: abs_size} for all coins with a non-zero open position."""
    positions = {}
    for asset_position in user_state.get("assetPositions", []):
        position = asset_position.get("position", {})
        coin = position.get("coin", "")
        size = float(position.get("szi", "0"))
        if coin and abs(size) > 0:
            positions[coin] = abs(size)
    return positions


def _get_close_fills(fills: list, coin: str, side: str, since_ms: float) -> list:
    """Returns close fills for a coin after since_ms, sorted oldest-first."""
    expected_dir = "Close Long" if side == "LONG" else "Close Short"
    matching = [
        f
        for f in fills
        if f.get("coin") == coin
        and f.get("time", 0) > since_ms
        and expected_dir in f.get("dir", "")
    ]
    return sorted(matching, key=lambda f: f["time"])


def _find_trade_by_fill_oid(
    open_trades: list[dict], fill_oid
) -> tuple[dict, str] | tuple[None, None]:
    """Match a close fill to a DB trade by the fill's order id against the stored
    tp_oid / sl_oid. Returns (trade, "TP"|"SL") on hit, (None, None) otherwise.
    Unambiguous for tight/wide variants that share an identical size.
    """
    if fill_oid is None:
        return None, None
    try:
        fill_oid_int = int(fill_oid)
    except (TypeError, ValueError):
        return None, None
    for trade in open_trades:
        tp_oid = trade.get("tp_oid")
        sl_oid = trade.get("sl_oid")
        try:
            if tp_oid is not None and int(tp_oid) == fill_oid_int:
                return trade, "TP"
            if sl_oid is not None and int(sl_oid) == fill_oid_int:
                return trade, "SL"
        except (TypeError, ValueError):
            continue
    return None, None


def _find_matching_trade(
    open_trades: list[dict], fill_px: float, fill_sz: float,
    fill_time_ms: float | None = None,
) -> dict | None:
    """Match fill to trade by size first (within 1%), then TP/SL price proximity.

    SAFETY GUARD: when fill_time_ms is provided, only trades opened *before* the fill
    are eligible. Without this, an old close fill (size-matching a recurring lot size
    like 0.0047) could be attributed to a freshly-opened trade and trigger
    cancellation of its live TP/SL — exactly the 2026-05-17 13:18 incident."""
    if fill_time_ms is not None:
        eligible = []
        for t in open_trades:
            try:
                opened_ms = (
                    datetime.fromisoformat(t["opened_at"])
                    .replace(tzinfo=timezone.utc)
                    .timestamp() * 1000
                )
            except (TypeError, ValueError):
                continue
            if opened_ms < fill_time_ms:
                eligible.append(t)
    else:
        eligible = open_trades

    # Phase 1: exact size match narrows candidates
    size_matched = [
        t
        for t in eligible
        if float(t["size"]) > 0
        and abs(float(t["size"]) - fill_sz) / float(t["size"]) < 0.01
    ]
    candidates = size_matched if size_matched else eligible

    # Phase 2: closest TP/SL price as tiebreaker
    best_trade = None
    best_distance = float("inf")
    for trade in candidates:
        distance = min(
            abs(float(trade["tp_px"]) - fill_px),
            abs(float(trade["sl_px"]) - fill_px),
        )
        if distance < best_distance:
            best_distance = distance
            best_trade = trade
    return best_trade


def _find_entry_fee(fills: list, coin: str, trade: dict) -> float | None:
    """Find the entry fill fee for a trade by matching coin, direction, and time."""
    expected_dir = "Open Long" if trade["side"] == "LONG" else "Open Short"
    opened_ms = (
        datetime.fromisoformat(trade["opened_at"])
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )
    entry_px = float(trade["entry_px"])
    for fill in fills:
        if fill.get("coin") != coin or expected_dir not in fill.get("dir", ""):
            continue
        if abs(fill["time"] - opened_ms) > 60_000:
            continue
        if abs(float(fill["px"]) - entry_px) / entry_px > 0.01:
            continue
        return float(fill.get("fee", 0))
    return None


def _determine_close_status(trade: dict, close_px: float) -> str:
    tp_px = float(trade["tp_px"])
    sl_px = float(trade["sl_px"])
    midpoint = (tp_px + sl_px) / 2

    # Trigger orders fill at market after the trigger price is reached, so the actual
    # fill can land slightly beyond the trigger (e.g. a LONG TP sell that triggers at
    # $2059 may fill at $2056). Strict threshold comparison mislabels these as MANUAL.
    # Use the TP/SL midpoint as the dividing line instead.
    if trade["side"] == "LONG":
        return "TP" if close_px >= midpoint else "SL"
    else:
        return "TP" if close_px <= midpoint else "SL"


def _format_batched_close_notification(
    coin: str,
    side: str,
    items: list[dict],
    equity: float,
) -> str:
    """Format one summary message covering all closures processed this cycle.

    Per-trade PnL attribution is unreliable because HL computes closedPnl against
    the position-average entry, not per-trade. We report aggregates instead.
    """
    count = len(items)
    total_size = sum(i["fill_sz"] for i in items)
    total_notional = sum(i["fill_sz"] * i["fill_px"] for i in items)
    avg_exit = total_notional / total_size if total_size > 0 else 0
    total_pnl = sum(i["pnl"] for i in items)
    total_fees = sum(i["entry_fee"] + i["close_fee"] for i in items)
    net = total_pnl - total_fees
    pnl_sign = "+" if net >= 0 else ""
    pnl_pct = (net / equity) * 100 if equity > 0 else 0
    pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"

    tp_count = sum(1 for i in items if i["status"] == "TP")
    sl_count = sum(1 for i in items if i["status"] == "SL")
    if tp_count and sl_count:
        status_label = f"{tp_count} TP / {sl_count} SL"
        header_emoji = "🔀"
    elif tp_count:
        status_label = f"{tp_count} TP" if count > 1 else "TP"
        header_emoji = "🟢" if side == "LONG" else "🔴"
    else:
        status_label = f"{sl_count} SL" if count > 1 else "SL"
        header_emoji = "⛔"

    entry_prices = [float(i["trade"]["entry_px"]) for i in items]
    entry_low, entry_high = min(entry_prices), max(entry_prices)
    entry_str = (
        f"${entry_low:,.2f}"
        if entry_low == entry_high
        else f"${entry_low:,.2f}–${entry_high:,.2f}"
    )

    header_suffix = f" ×{count}" if count > 1 else ""

    return (
        f"{header_emoji} {coin} {side} CLOSED{header_suffix} — {status_label} @ <code>${avg_exit:,.2f}</code>\n\n"
        f"📐 Size: <code>{total_size} (${total_notional:,.2f})</code>\n"
        f"💵 Entry: <code>{entry_str}</code> → Exit: <code>${avg_exit:,.2f}</code>\n"
        f"📈 PnL: <code>{pnl_sign}${net:,.2f} ({pnl_pct_str})</code>"
    )


async def _process_coin_closures(
    coin: str,
    db_trades: list[dict],
    fills: list,
    notify,
    equity: float,
    exchange: Exchange,
    info: Info,
    settings: Settings,
) -> None:
    """Match close fills to DB trades and close each one individually."""
    side = db_trades[0]["side"]
    since_ms = min(
        datetime.fromisoformat(t["opened_at"]).replace(tzinfo=timezone.utc).timestamp()
        * 1000
        for t in db_trades
    )

    close_fills = _get_close_fills(fills, coin, side, since_ms)

    if not close_fills:
        state = _fill_retry_state.setdefault(coin, {"count": 0, "next_log_at": 0})
        state["count"] += 1
        if state["count"] >= state["next_log_at"]:
            logger.warning(
                f"{coin}: closure detected but no matching fills in user_fills"
                f" — retry #{state['count']}, backing off"
            )
            # Log at: 0, 1, 2, 4, 8, then every ~112 cycles (~30min at 16s poll)
            state["next_log_at"] = state["count"] + min(2 ** state["count"], 112)
        return

    _fill_retry_state.pop(coin, None)

    pending_trades = list(db_trades)
    closed_items: list[dict] = []

    for fill in close_fills:
        if not pending_trades:
            break

        fill_px = float(fill["px"])
        fill_sz = float(fill.get("sz", 0))
        pnl = float(fill.get("closedPnl", 0))
        close_fee = float(fill.get("fee", 0))

        # Primary match: fill's order id vs stored tp_oid/sl_oid on each trade.
        # Disambiguates tight and wide variants that share an identical size.
        trade, oid_status = _find_trade_by_fill_oid(pending_trades, fill.get("oid"))
        if trade is None:
            # SAFETY: pass the fill's timestamp so the heuristic cannot attribute
            # this fill to a trade opened AFTER it (2026-05-17 incident).
            fill_time_ms = float(fill.get("time", 0)) or None
            trade = _find_matching_trade(
                pending_trades, fill_px, fill_sz, fill_time_ms=fill_time_ms
            )
        if trade is None:
            break

        entry_fee = float(trade.get("entry_fee") or 0)
        if trade.get("entry_fee") is None:
            found_fee = _find_entry_fee(fills, coin, trade)
            if found_fee is not None:
                entry_fee = found_fee
                await update_entry_fee(trade["id"], found_fee)

        status = oid_status or _determine_close_status(trade, fill_px)
        await close_trade(trade["id"], pnl, status, close_fee=close_fee)
        pending_trades.remove(trade)

        net = pnl - entry_fee - close_fee
        logger.info(
            f"{coin} {side} closed | status={status}"
            f" | entry={trade['entry_px']} exit={fill_px} pnl={pnl:+.2f} net={net:+.2f}"
        )
        closed_items.append(
            {
                "trade": trade,
                "fill_px": fill_px,
                "fill_sz": fill_sz,
                "pnl": pnl,
                "entry_fee": entry_fee,
                "close_fee": close_fee,
                "status": status,
            }
        )
        await _cancel_counterpart_order(exchange, info, settings, trade, status)

    if closed_items:
        await notify(
            _format_batched_close_notification(coin, side, closed_items, equity)
        )


async def _sweep_cancel_orders(
    exchange: Exchange,
    info: Info,
    settings: Settings,
    coin: str,
) -> None:
    """Cancel all remaining trigger orders for a coin after position is fully closed."""
    try:
        orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Sweep cancel: failed to fetch orders | coin={coin} | {error}")
        return

    coin_orders = [o for o in orders if o.get("coin") == coin and o.get("triggerPx")]
    if not coin_orders:
        return

    cancel_list = [{"coin": coin, "oid": o["oid"]} for o in coin_orders]
    try:
        result = await asyncio.to_thread(exchange.bulk_cancel, cancel_list)
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        cancelled = sum(1 for s in statuses if s == "success")
        logger.info(
            f"Sweep cancelled {cancelled}/{len(cancel_list)} orphaned orders | coin={coin}"
        )
    except Exception as error:
        logger.warning(f"Sweep cancel failed | coin={coin} | {error}")


async def _close_residual_position(
    exchange: Exchange,
    info: Info,
    settings: Settings,
    coin: str,
    residual_size: float,
    notify,
) -> None:
    """Auto-close a residual position left by rounding across multiple TP/SL fills."""
    logger.info(f"Closing residual position | coin={coin} | size={residual_size}")
    try:
        result = await asyncio.to_thread(exchange.market_close, coin, slippage=0.02)
        if result.get("status") != "ok":
            logger.error(f"Residual close failed | coin={coin} | result={result}")
            await notify(
                f"⚠️ Residual {coin} position (<code>{residual_size}</code>) "
                f"could not be closed — close manually"
            )
            return
        fill_info = result.get("response", {}).get("data", {}).get("statuses", [{}])[0]
        close_px = float(fill_info.get("filled", {}).get("avgPx", 0))
        notional = residual_size * close_px
        logger.info(
            f"Residual closed | coin={coin} | size={residual_size}"
            f" | exit={close_px} | notional=${notional:.2f}"
        )
        await notify(
            f"🧹 {coin} residual closed @ <code>${close_px:,.2f}</code>\n"
            f"📐 Size: <code>{residual_size} (${notional:,.2f})</code>\n"
            f"ℹ️ Rounding remainder after all TP/SL fills"
        )
        # Sweep-cancel any orphaned trigger orders for this coin.
        await _sweep_cancel_orders(exchange, info, settings, coin)
    except Exception as error:
        logger.error(f"Residual close exception | coin={coin} | {error}")
        await notify(
            f"⚠️ Residual {coin} position (<code>{residual_size}</code>) "
            f"close failed — <code>{error}</code>"
        )


async def _check_closed_positions(info, settings, notify, exchange) -> None:
    open_trades = await fetch_open_trades()

    user_state = await asyncio.to_thread(info.user_state, settings.hl_account_address)
    hl_positions = _extract_open_positions(user_state)

    if not open_trades and not hl_positions:
        return

    spot_state = await asyncio.to_thread(
        info.spot_user_state, settings.hl_account_address
    )
    perps_equity = float(user_state.get("marginSummary", {}).get("accountValue", 0))
    spot_usdc = next(
        (
            float(b["total"])
            for b in spot_state.get("balances", [])
            if b["coin"] == "USDC"
        ),
        0.0,
    )
    equity = max(perps_equity, spot_usdc)

    trades_by_coin: dict[str, list[dict]] = {}
    for trade in open_trades:
        trades_by_coin.setdefault(trade["coin"], []).append(trade)

    for coin, db_trades in trades_by_coin.items():
        total_db_size = sum(float(t["size"]) for t in db_trades)
        current_hl_size = hl_positions.get(coin, 0.0)
        closed_amount = total_db_size - current_hl_size

        if closed_amount < 1e-6:
            continue

        logger.info(
            f"Closure detected: {coin} | db_total={total_db_size:.6f}"
            f" hl_size={current_hl_size:.6f} closed={closed_amount:.6f}"
        )
        fills = await asyncio.to_thread(info.user_fills, settings.hl_account_address)
        await _process_coin_closures(
            coin, db_trades, fills, notify, equity, exchange, info, settings
        )

    # Close residual positions left by rounding mismatches across multiple TP/SL fills.
    # Re-fetch open trades since _process_coin_closures may have closed some.
    remaining_trades = await fetch_open_trades()
    remaining_coins = {t["coin"] for t in remaining_trades}

    try:
        all_open_orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Residual check: failed to fetch open orders | {error}")
        all_open_orders = []

    for coin, hl_size in hl_positions.items():
        if coin in remaining_coins:
            continue
        if coin in trades_by_coin:
            # All DB trades for this coin just closed, but HL position persists.
            # Only close as residual if no active TP/SL orders remain — if orders exist,
            # the position is protected and will close naturally when price hits them.
            has_trigger_orders = any(
                o.get("coin") == coin and o.get("triggerPx")
                for o in all_open_orders
            )
            if has_trigger_orders:
                logger.info(
                    f"Residual skipped — active TP/SL orders still live | coin={coin} | size={hl_size}"
                )
                continue
            await _close_residual_position(
                exchange, info, settings, coin, hl_size, notify
            )
        elif coin not in _alerted_orphan_coins:
            _alerted_orphan_coins.add(coin)
            msg = f"Untracked position: {coin} size=<code>{hl_size}</code> — no DB record. Close manually or wait for next signal."
            logger.warning(msg)
            await notify(msg)


async def find_unprotected_trades(
    info: Info, settings: Settings, open_trades: list[dict]
) -> list[dict]:
    """Return open trades that are missing a matching live TP or SL trigger order on HL."""
    if not open_trades:
        return []
    try:
        orders = await asyncio.to_thread(
            info.frontend_open_orders, settings.hl_account_address
        )
    except Exception as error:
        logger.warning(f"Unprotected check: failed to fetch orders | {error}")
        return []

    trigger_orders: dict[str, list[dict]] = {}
    live_oids: set[int] = set()
    for o in orders:
        if o.get("triggerPx"):
            trigger_orders.setdefault(o["coin"], []).append(o)
            try:
                live_oids.add(int(o["oid"]))
            except (TypeError, ValueError, KeyError):
                pass

    unprotected = []
    for trade in open_trades:
        coin = trade["coin"]
        tp_px = float(trade["tp_px"])
        sl_px = float(trade["sl_px"])
        size = float(trade["size"])
        tp_oid = trade.get("tp_oid")
        sl_oid = trade.get("sl_oid")
        coin_orders = trigger_orders.get(coin, [])

        # Prefer OID membership when stored; fallback to price+size heuristic otherwise.
        if tp_oid is not None:
            try:
                has_tp = int(tp_oid) in live_oids
            except (TypeError, ValueError):
                has_tp = False
        else:
            has_tp = any(
                abs(float(o.get("triggerPx", 0)) - tp_px) / tp_px < 0.001
                and abs(float(o.get("sz", 0)) - size) / size < 0.01
                for o in coin_orders
            )
        if sl_oid is not None:
            try:
                has_sl = int(sl_oid) in live_oids
            except (TypeError, ValueError):
                has_sl = False
        else:
            has_sl = any(
                abs(float(o.get("triggerPx", 0)) - sl_px) / sl_px < 0.001
                and abs(float(o.get("sz", 0)) - size) / size < 0.01
                for o in coin_orders
            )

        if not has_tp or not has_sl:
            unprotected.append(trade)

    return unprotected


async def _check_unprotected_trades(info: Info, settings: Settings, notify) -> None:
    """Send an aggregate alert when any open DB trades are missing live TP/SL orders on HL."""
    global _last_unprotected_alert

    open_trades = await fetch_open_trades()
    unprotected = await find_unprotected_trades(info, settings, open_trades)

    if not unprotected:
        _last_unprotected_alert = 0.0
        return

    now = _time.monotonic()
    if now - _last_unprotected_alert < UNPROTECTED_ALERT_COOLDOWN_SECONDS:
        return

    count = len(unprotected)
    logger.warning(f"{count} open trade(s) missing live TP/SL orders on HL")
    await notify(
        f"⚠️ {count} open position{'s' if count > 1 else ''} "
        f"{'are' if count > 1 else 'is'} missing live TP/SL orders on Hyperliquid.\n\n"
        f"Use /unprotected to view them, or /unprotected_close to close them."
    )
    _last_unprotected_alert = now


async def run_position_monitor(
    info, settings, notify, stop_event: asyncio.Event, exchange: Exchange = None
) -> None:
    logger.info(f"Started — polling every {settings.position_poll_interval_seconds}s")

    # Heartbeat every N cycles so silence is visible in logs. At a 16s poll interval,
    # every 20 cycles is ≈ 5 minutes — enough to detect a dead task without log spam.
    heartbeat_every = 20
    cycle = 0

    try:
        while not stop_event.is_set():
            cycle += 1
            try:
                await _check_closed_positions(info, settings, notify, exchange)
                await _check_unprotected_trades(info, settings, notify)
            except Exception as error:
                logger.error(f"Poll cycle error: {error}", exc_info=True)

            if cycle % heartbeat_every == 0:
                open_trades = await fetch_open_trades()
                logger.info(
                    f"heartbeat | cycle={cycle} | open_trades={len(open_trades)}"
                )

            try:
                await asyncio.sleep(settings.position_poll_interval_seconds)
            except asyncio.CancelledError:
                logger.warning("Sleep cancelled — exiting monitor loop")
                raise
    except asyncio.CancelledError:
        logger.warning("PositionMonitor task cancelled")
        raise
    except BaseException as error:
        # Anything that escapes the per-cycle handler (a BaseException, an error from
        # the heartbeat path, etc.) must be logged loudly — silent task death is what
        # caused the 6-day zombie incident in May.
        logger.critical(f"PositionMonitor crashed: {error}", exc_info=True)
        raise
    finally:
        logger.info("PositionMonitor exited")
