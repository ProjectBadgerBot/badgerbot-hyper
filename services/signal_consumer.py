import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

from config.settings import Settings

logger = logging.getLogger("SignalConsumer")

WEBSOCKET_URL = "wss://api.badgerbot.io/signals/ws"
INITIAL_RECONNECT_DELAY_SECONDS = 1
MAX_RECONNECT_DELAY_SECONDS = 60
OFFLINE_ALERT_THRESHOLD_SECONDS = 300
OFFLINE_ALERT_COOLDOWN_SECONDS = 86400
NO_SIGNAL_REMINDER_SECONDS = 86400

SignalHandler = Callable[..., Awaitable[None]]

BATCH_WINDOW_SECONDS = 3
_signal_buffer: dict[float, list[dict]] = {}
_buffer_task: asyncio.Task | None = None

# Shared signal log for /signal command — max 50 entries
signal_log: list[dict] = []
MAX_SIGNAL_LOG = 50

# Tracks last signal timestamp for daily reminder
last_signal_at: float = 0.0


def _format_duration(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def log_signal(entry: dict) -> None:
    signal_log.append(entry)
    if len(signal_log) > MAX_SIGNAL_LOG:
        signal_log.pop(0)


def build_websocket_url(api_key: str) -> str:
    return f"{WEBSOCKET_URL}?api_key={api_key}"


def signal_matches_algorithms(signal: dict, allowed: list[str]) -> bool:
    """Returns True if the signal's display_name is in the allowed list,
    or if the signal carries no display_name at all (backward compatible)."""
    if not allowed:
        return True
    display_name = signal.get("display_name")
    if display_name is None:
        return True
    return display_name in allowed


def parse_signal(raw_message: str) -> dict | None:
    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError as error:
        logger.warning(f"Signal parse error: {error} | raw={raw_message[:200]}")
        return None

    if data.get("event") == "keepalive":
        return None

    required_fields = {"coin_symbol", "price", "tp_price", "sl_price", "mode", "dispatched_at"}
    missing = required_fields - data.keys()
    if missing:
        logger.warning(f"Signal missing fields: {missing} | raw={raw_message[:200]}")
        return None

    if not data.get("tp_price") or not data.get("sl_price"):
        logger.warning(f"Signal has null TP/SL — dropped | coin={data.get('coin_symbol')}")
        return None

    return data


def validate_signal(signal: dict, mark_price: float, settings: Settings) -> str | None:
    """Returns None if valid, or a rejection reason string."""
    dispatched_at = datetime.fromisoformat(signal["dispatched_at"]).replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - dispatched_at).total_seconds()

    if age_seconds > settings.max_signal_age_seconds:
        reason = f"stale ({age_seconds:.0f}s)"
        logger.warning(
            f"Signal dropped: stale by {age_seconds:.0f}s | coin={signal['coin_symbol']}"
        )
        return reason

    signal_price = float(signal["price"])
    tp_price = float(signal["tp_price"])
    tp_distance = abs(tp_price - signal_price)
    tp_remaining = abs(tp_price - mark_price)

    if tp_remaining < tp_distance * settings.max_price_deviation_pct:
        reason = f"TP eroded ({tp_remaining:.4f} of original {tp_distance:.4f})"
        logger.warning(
            f"Signal dropped: TP remaining {tp_remaining:.4f} < threshold"
            f" {tp_distance * settings.max_price_deviation_pct:.4f}"
            f" | coin={signal['coin_symbol']} | signal={signal_price} | mark={mark_price}"
            f" | tp={tp_price}"
        )
        return reason

    sl_price = float(signal["sl_price"])
    sl_distance = abs(sl_price - signal_price)
    sl_remaining = abs(sl_price - mark_price)

    if sl_remaining < sl_distance * settings.max_price_deviation_pct:
        reason = f"SL eroded ({sl_remaining:.4f} of original {sl_distance:.4f})"
        logger.warning(
            f"Signal dropped: SL remaining {sl_remaining:.4f} < threshold"
            f" {sl_distance * settings.max_price_deviation_pct:.4f}"
            f" | coin={signal['coin_symbol']} | signal={signal_price} | mark={mark_price}"
            f" | sl={sl_price}"
        )
        return reason

    return None


async def _flush_buffer(handler: SignalHandler) -> None:
    try:
        await asyncio.sleep(BATCH_WINDOW_SECONDS)
    except asyncio.CancelledError:
        return
    buffer_copy = dict(_signal_buffer)
    _signal_buffer.clear()
    for price, signals in buffer_copy.items():
        batch_size = len(signals)
        logger.info(f"Flushing batch | entry_price={price} | count={batch_size}")
        for signal in signals:
            await handler(signal, batch_size=batch_size)


async def _buffer_signal(signal: dict, handler: SignalHandler) -> None:
    global _buffer_task
    price = float(signal["price"])
    _signal_buffer.setdefault(price, []).append(signal)
    if _buffer_task and not _buffer_task.done():
        _buffer_task.cancel()
    _buffer_task = asyncio.create_task(_flush_buffer(handler))


async def _listen(
    websocket_url: str, signal_handler: SignalHandler, settings: Settings,
    last_message_ref: list[float],
) -> None:
    async with websockets.connect(
        websocket_url, ping_interval=60, ping_timeout=120
    ) as websocket:
        logger.info(f"Listening for signals on {WEBSOCKET_URL}")
        async for raw_message in websocket:
            last_message_ref[0] = time.monotonic()
            signal = parse_signal(raw_message)
            if signal is None:
                continue
            if not signal_matches_algorithms(signal, settings.algorithms):
                logger.info(
                    f"Signal skipped: algorithm not opted-in"
                    f" | coin={signal.get('coin_symbol')}"
                    f" | display_name={signal.get('display_name')}"
                    f" | allowed={settings.algorithms}"
                )
                continue
            global last_signal_at
            last_signal_at = time.monotonic()
            logger.info(
                f"Signal received: {signal['coin_symbol']} {signal['mode']}"
                f" @ {signal['price']} | TP: {signal['tp_price']} | SL: {signal['sl_price']}"
            )
            if settings.risk_pct is not None:
                await _buffer_signal(signal, signal_handler)
            else:
                await signal_handler(signal)


async def _no_signal_reminder(stop_event: asyncio.Event, notify) -> None:
    global last_signal_at
    last_signal_at = time.monotonic()
    while not stop_event.is_set():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return
        if stop_event.is_set():
            return
        elapsed = time.monotonic() - last_signal_at
        if elapsed >= NO_SIGNAL_REMINDER_SECONDS and notify:
            last_signal_str = _format_duration(elapsed)
            await notify(
                f"📡 No signals received in the last 24h\n\n"
                f"Connection: <code>active</code>\n"
                f"Last signal: <code>{last_signal_str} ago</code>"
            )
            last_signal_at = time.monotonic()


async def connect_and_listen(
    signal_handler: SignalHandler,
    settings: Settings,
    stop_event: asyncio.Event,
    notify=None,
) -> None:
    websocket_url = build_websocket_url(settings.badgerbot_api_key)
    reconnect_delay = INITIAL_RECONNECT_DELAY_SECONDS
    last_message_ref: list[float] = [time.monotonic()]
    last_offline_alert_at: float = 0.0

    reminder_task = asyncio.create_task(_no_signal_reminder(stop_event, notify))

    try:
        while not stop_event.is_set():
            try:
                last_message_ref[0] = time.monotonic()
                reconnect_delay = INITIAL_RECONNECT_DELAY_SECONDS
                listen_task = asyncio.create_task(
                    _listen(websocket_url, signal_handler, settings, last_message_ref)
                )
                stop_task = asyncio.create_task(stop_event.wait())
                done, pending = await asyncio.wait(
                    {listen_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                if stop_task in done:
                    break
                if not listen_task.cancelled() and listen_task.exception():
                    raise listen_task.exception()
            except ConnectionClosed as error:
                logger.warning(f"WebSocket disconnected: {error}")
            except Exception as error:
                logger.error(f"WebSocket error: {error}")

            if stop_event.is_set():
                break

            since_last_msg = time.monotonic() - last_message_ref[0]
            since_last_alert = time.monotonic() - last_offline_alert_at

            if since_last_msg >= OFFLINE_ALERT_THRESHOLD_SECONDS:
                if since_last_alert >= OFFLINE_ALERT_COOLDOWN_SECONDS or last_offline_alert_at == 0:
                    duration_str = _format_duration(since_last_msg)
                    msg = (
                        f"📡 Signal feed offline\n\n"
                        f"Last message: <code>{duration_str} ago</code>\n"
                        f"Reconnecting..."
                    )
                    logger.error(msg)
                    if notify:
                        await notify(msg)
                    last_offline_alert_at = time.monotonic()
            else:
                logger.warning(f"Reconnecting in {reconnect_delay}s...")

            # Send reconnected notice if we had previously alerted
            if last_offline_alert_at > 0:
                try:
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS)
                    continue
                except asyncio.CancelledError:
                    return

            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY_SECONDS)
    finally:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass