import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config.settings import Settings
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from services.signal_consumer import signal_log

logger = logging.getLogger("TelegramBot")


def _b(text) -> str:
    return f"<code>{text}</code>"


@dataclass
class BotState:
    paused: bool = False


class TelegramBot:
    def __init__(
        self,
        settings: Settings,
        info: Info,
        exchange: Exchange,
        bot_state: BotState,
        leverage_config: dict | None = None,
    ) -> None:
        self._settings = settings
        self._info = info
        self._exchange = exchange
        self._bot_state = bot_state
        self._leverage_config = leverage_config or {}
        self._app = Application.builder().token(settings.telegram_bot_token).build()
        self._register_handlers()

    def _register_handlers(self) -> None:
        for name, handler in [
            ("status", self._cmd_status),
            ("pause", self._cmd_pause),
            ("resume", self._cmd_resume),
            ("history", self._cmd_history),
            ("position", self._cmd_position),
            ("close", self._cmd_close),
            ("unprotected", self._cmd_unprotected),
            ("unprotected_close", self._cmd_unprotected_close),
            ("stats", self._cmd_stats),
            ("signal", self._cmd_signal),
            ("help", self._cmd_help),
        ]:
            self._app.add_handler(CommandHandler(name, handler))

    def _is_authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != self._settings.telegram_authorized_user_id:
            logger.warning(f"Unauthorized Telegram access | user_id={user_id}")
            return False
        return True

    async def send(self, text: str) -> None:
        try:
            await self._app.bot.send_message(
                chat_id=self._settings.telegram_authorized_user_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception as error:
            logger.error(f"Telegram send failed: {error}")

    async def _cmd_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        user_state = await asyncio.to_thread(
            self._info.user_state, self._settings.hl_account_address
        )
        open_positions = [
            p["position"]
            for p in user_state.get("assetPositions", [])
            if float(p.get("position", {}).get("szi", 0)) != 0
        ]

        if not open_positions:
            withdrawable = float(user_state.get("withdrawable", 0))
            if withdrawable == 0:
                spot_state = await asyncio.to_thread(
                    self._info.spot_user_state, self._settings.hl_account_address
                )
                for b in spot_state.get("balances", []):
                    if b["coin"] == "USDC":
                        withdrawable = float(b["total"])
                        break
            lev = self._leverage_config.get(
                "ETH", self._leverage_config.get("DEFAULT", 3)
            )
            if self._settings.risk_pct is not None:
                risk_label = (
                    f"{self._settings.risk_pct * 100:.1f}% risk per entry price"
                )
                await update.message.reply_text(
                    f"No open positions.\n\n"
                    f"💰 Available: {_b(f'${withdrawable:,.2f}')}\n"
                    f"⚡ Leverage: {_b(f'ETH {lev}x')}\n"
                    f"📐 Next Trade: {_b(risk_label)}",
                    parse_mode="HTML",
                )
            elif self._settings.position_size_usd is not None:
                margin = self._settings.position_size_usd
                margin_label = f"${margin:,.2f} margin (fixed)"
                await update.message.reply_text(
                    f"No open positions.\n\n"
                    f"💰 Available: {_b(f'${withdrawable:,.2f}')}\n"
                    f"⚡ Leverage: {_b(f'ETH {lev}x')}\n"
                    f"📐 Next Trade: {_b(margin_label)}",
                    parse_mode="HTML",
                )
            else:
                notional = withdrawable * self._settings.position_size_pct
                margin = notional / lev if lev > 0 else notional
                pct = (margin / withdrawable * 100) if withdrawable > 0 else 0
                margin_label = f"${margin:,.2f} margin ({pct:.1f}% of balance)"
                await update.message.reply_text(
                    f"No open positions.\n\n"
                    f"💰 Available: {_b(f'${withdrawable:,.2f}')}\n"
                    f"⚡ Leverage: {_b(f'ETH {lev}x')}\n"
                    f"📐 Next Trade: {_b(margin_label)}",
                    parse_mode="HTML",
                )
            return

        from services.trade_executor import fetch_account_equity

        equity = await fetch_account_equity(
            self._info, self._settings.hl_account_address
        )

        sections = []
        for pos in open_positions:
            coin = pos["coin"]
            szi = float(pos["szi"])
            side = "LONG" if szi > 0 else "SHORT"
            direction_emoji = "🟢" if szi > 0 else "🔴"
            size = abs(szi)
            position_value = float(pos.get("positionValue", 0))
            avg_entry = float(pos.get("entryPx", 0))
            raw_liq = pos.get("liquidationPx")
            liq_str = f"${float(raw_liq):,.2f}" if raw_liq else "N/A"
            leverage_val = pos.get("leverage", {}).get("value")
            leverage_str = f"{leverage_val}x" if leverage_val else "N/A"
            margin = (
                position_value / float(leverage_val) if leverage_val else position_value
            )
            upnl = float(pos.get("unrealizedPnl", 0))
            upnl_sign = "+" if upnl >= 0 else ""
            pnl_pct = (upnl / equity) * 100 if equity > 0 else 0
            pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
            sections.append(
                f"{direction_emoji} {coin} {side}\n\n"
                f"📐 Size: {_b(f'{size} (${position_value:,.2f})')}\n"
                f"💵 Avg Entry: {_b(f'${avg_entry:,.2f}')}\n"
                f"⚡ Leverage: {_b(f'{leverage_str} (${margin:,.2f} margin)')}\n"
                f"💀 Liq: {_b(liq_str)}\n"
                f"📈 uPnL: {_b(f'{upnl_sign}${upnl:,.2f} ({pnl_pct_str})')}"
            )
        await update.message.reply_text("\n\n".join(sections), parse_mode="HTML")

    async def _cmd_pause(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        self._bot_state.paused = True
        await update.message.reply_text("Bot paused. Signals will be ignored.")

    async def _cmd_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        self._bot_state.paused = False
        await update.message.reply_text("Bot resumed. Listening for signals.")

    async def _cmd_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        fills = await asyncio.to_thread(
            self._info.user_fills, self._settings.hl_account_address
        )
        closes = [f for f in reversed(fills) if "Close" in f.get("dir", "")][:10]
        if not closes:
            await update.message.reply_text("No closed trades yet.")
            return
        lines = []
        for f in closes:
            side = "LONG" if "Long" in f.get("dir", "") else "SHORT"
            exit_px = float(f.get("px", 0))
            pnl = float(f.get("closedPnl", 0)) - float(f.get("fee", 0))
            pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            lines.append(
                f"{f.get('coin')} {side} | Exit {_b(f'${exit_px:,.2f}')} | PnL {_b(pnl_str)}"
            )
        await update.message.reply_text(
            "Recent closes:\n" + "\n".join(lines), parse_mode="HTML"
        )

    async def _cmd_position(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return

        user_state, spot_state, raw_orders = await asyncio.gather(
            asyncio.to_thread(self._info.user_state, self._settings.hl_account_address),
            asyncio.to_thread(
                self._info.spot_user_state, self._settings.hl_account_address
            ),
            asyncio.to_thread(
                self._info.frontend_open_orders, self._settings.hl_account_address
            ),
        )

        hl_positions = {}
        for ap in user_state.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            if coin and float(pos.get("szi", 0)) != 0:
                hl_positions[coin] = pos

        if not hl_positions:
            await update.message.reply_text("No open positions.")
            return

        # Live trigger orders straight from HL — these ARE the per-lot TP/SL protection.
        triggers: dict[str, list] = {}
        for o in raw_orders:
            for item in [o, *o.get("children", [])]:
                if item.get("triggerPx") and item.get("coin"):
                    triggers.setdefault(item["coin"], []).append(item)

        margin = user_state.get("marginSummary", {})
        margin_used = float(margin.get("totalMarginUsed", 0))
        perps_equity = float(margin.get("accountValue", 0))
        spot_usdc = next(
            (
                float(b["total"])
                for b in spot_state.get("balances", [])
                if b["coin"] == "USDC"
            ),
            0.0,
        )
        account_value = max(perps_equity, spot_usdc)
        available = account_value - margin_used
        margin_pct = (margin_used / account_value * 100) if account_value > 0 else 0

        sections = []
        for coin, pos in hl_positions.items():
            szi = float(pos["szi"])
            side = "LONG" if szi > 0 else "SHORT"
            direction_emoji = "🟢" if szi > 0 else "🔴"
            total_size = abs(szi)
            position_value = float(pos.get("positionValue", 0))
            avg_entry = float(pos.get("entryPx", 0))
            raw_liq = pos.get("liquidationPx")
            liq_str = f"${float(raw_liq):,.2f}" if raw_liq else "N/A"
            leverage_val = pos.get("leverage", {}).get("value")
            leverage_str = f"{leverage_val}x" if leverage_val else "N/A"
            funding = float(pos.get("cumFunding", {}).get("sinceOpen", 0))
            funding_str = f"{'+' if funding >= 0 else ''}${funding:,.4f}"

            order_rows = []
            for o in sorted(triggers.get(coin, []), key=lambda x: float(x.get("triggerPx", 0))):
                kind = "✅ TP" if "profit" in o.get("orderType", "").lower() else "⛔ SL"
                o_sz = float(o.get("sz", 0))
                o_px = float(o.get("triggerPx", 0))
                order_rows.append(f"  {kind} {_b(f'{o_sz} @ ${o_px:,.2f}')}")
            orders_block = "\n".join(order_rows) if order_rows else "  ⚠️ no live TP/SL"

            sections.append(
                f"{direction_emoji} {coin} {side}\n\n"
                f"📐 Total Size: {_b(f'{total_size} (${position_value:,.2f})')}\n"
                f"💵 Avg Entry: {_b(f'${avg_entry:,.2f}')}\n"
                f"💀 Liq: {_b(liq_str)}\n"
                f"⚡ Leverage: {_b(leverage_str)}\n"
                f"🔛 Funding: {_b(funding_str)}\n" + orders_block
            )

        total_upnl = sum(
            float(pos.get("unrealizedPnl", 0)) for pos in hl_positions.values()
        )
        upnl_sign = "+" if total_upnl >= 0 else ""
        upnl_pct = (total_upnl / account_value * 100) if account_value > 0 else 0
        upnl_pct_str = f" ({upnl_sign}{upnl_pct:.2f}%)"

        footer = (
            f"\n\n📈 uPnL: {_b(f'{upnl_sign}${total_upnl:,.2f}{upnl_pct_str}')}\n\n"
            f"🏦 Account Value: {_b(f'${account_value:,.2f}')}\n"
            f"🎢 Margin Used: {_b(f'${margin_used:,.2f} ({margin_pct:.1f}%)')}\n"
            f"💰 Available: {_b(f'${available:,.2f}')}"
        )
        await update.message.reply_text(
            "\n\n".join(sections) + footer, parse_mode="HTML"
        )

    async def _cmd_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return

        args = context.args or []
        if len(args) != 1:
            await update.message.reply_text("Usage: /close <COIN> or /close all")
            return

        positions = await self._open_positions_map()
        if not positions:
            await update.message.reply_text("No open positions.")
            return

        if args[0].lower() == "all":
            coins = list(positions)
        else:
            coin = args[0].upper()
            if coin not in positions:
                await update.message.reply_text(f"No open {coin} position.")
                return
            coins = [coin]

        await self._close_coins(update, coins, positions)

    async def _cmd_unprotected(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return

        from services.position_monitor import find_unprotected_coins

        unprotected = await find_unprotected_coins(self._info, self._settings)
        if not unprotected:
            await update.message.reply_text(
                "✅ All open positions have full TP/SL coverage on Hyperliquid."
            )
            return

        lines = []
        for u in unprotected:
            lines.append(
                f"{u['coin']} {u['side']} — pos {_b(u['position'])}"
                f" | TP cover {_b(u['tp_sum'])}"
                f" | SL cover {_b(u['sl_sum'])}"
            )
        count = len(unprotected)
        await update.message.reply_text(
            f"⚠️ {count} position{'s' if count > 1 else ''} missing full TP/SL coverage:\n\n"
            + "\n".join(lines)
            + "\n\nUse /unprotected_close to close them.",
            parse_mode="HTML",
        )

    async def _cmd_unprotected_close(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return

        from services.position_monitor import find_unprotected_coins

        unprotected = await find_unprotected_coins(self._info, self._settings)
        if not unprotected:
            await update.message.reply_text(
                "✅ All open positions have full TP/SL coverage on Hyperliquid."
            )
            return

        positions = await self._open_positions_map()
        coins = [u["coin"] for u in unprotected if u["coin"] in positions]
        await self._close_coins(update, coins, positions)

    async def _open_positions_map(self) -> dict[str, dict]:
        """{coin: position dict} for every coin with a non-zero position on HL."""
        user_state = await asyncio.to_thread(
            self._info.user_state, self._settings.hl_account_address
        )
        positions = {}
        for ap in user_state.get("assetPositions", []):
            pos = ap.get("position", {})
            coin = pos.get("coin", "")
            if coin and float(pos.get("szi", 0)) != 0:
                positions[coin] = pos
        return positions

    async def _cancel_triggers(self, coin: str) -> None:
        """Cancel every live trigger order for a coin by its live oid."""
        try:
            orders = await asyncio.to_thread(
                self._info.frontend_open_orders, self._settings.hl_account_address
            )
        except Exception as error:
            logger.error(f"Failed to fetch open orders for {coin}: {error}")
            return
        oids = [o["oid"] for o in orders if o.get("coin") == coin and o.get("isTrigger")]
        if not oids:
            return
        try:
            result = await asyncio.to_thread(
                self._exchange.bulk_cancel, [{"coin": coin, "oid": oid} for oid in oids]
            )
            if result.get("status") == "ok":
                logger.info(f"Cancelled {len(oids)} trigger order(s) for {coin}")
            else:
                logger.error(f"bulk_cancel failed for {coin}: {result}")
        except Exception as error:
            logger.error(f"Failed to cancel orders for {coin}: {error}")

    async def _close_coins(
        self, update: Update, coins: list[str], positions: dict[str, dict]
    ) -> None:
        from services.trade_executor import fetch_account_equity

        equity = await fetch_account_equity(
            self._info, self._settings.hl_account_address
        )

        lines = []
        total_pnl = 0.0
        for coin in coins:
            pos = positions[coin]
            szi = float(pos["szi"])
            side = "LONG" if szi > 0 else "SHORT"
            size = abs(szi)
            entry_px = float(pos.get("entryPx", 0))
            try:
                result = await asyncio.to_thread(
                    self._exchange.market_close, coin, slippage=0.02
                )
                fill_px = float(
                    result["response"]["data"]["statuses"][0]["filled"]["avgPx"]
                )
            except Exception as error:
                logger.error(f"Close failed for {coin}: {error}")
                lines.append(f"⚠️ {coin} — close failed")
                continue

            # Sweep any leftover trigger orders for the now-flat coin (by live oid).
            await self._cancel_triggers(coin)

            pnl = (
                (fill_px - entry_px) * size
                if side == "LONG"
                else (entry_px - fill_px) * size
            )
            total_pnl += pnl
            direction_emoji = "🟢" if side == "LONG" else "🔴"
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_pct = (pnl / equity) * 100 if equity > 0 else 0
            pnl_pct_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
            lines.append(
                f"{direction_emoji} {coin} {side} CLOSED @ {_b(f'${fill_px:,.2f}')}\n"
                f"📐 Size: {_b(f'{size} (${size * fill_px:,.2f})')}\n"
                f"💵 Entry: {_b(f'${entry_px:,.2f}')} → Exit: {_b(f'${fill_px:,.2f}')}\n"
                f"📈 PnL: {_b(f'{pnl_sign}${pnl:,.2f} ({pnl_pct_str})')}"
            )

        total_sign = "+" if total_pnl >= 0 else ""
        total_pct = (total_pnl / equity * 100) if equity > 0 else 0
        lines.append(
            f"💰 Total PnL: {_b(f'{total_sign}${total_pnl:,.2f} ({total_sign}{total_pct:.2f}%)')}"
        )
        await update.message.reply_text("\n\n".join(lines), parse_mode="HTML")

    async def _cmd_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return

        args = context.args or []
        period = args[0].lower() if args else "week"

        if period == "week":
            days, label = 7, "Last 7 Days"
        elif period == "month":
            days, label = 30, "Last 30 Days"
        elif period == "day":
            days, label = 1, "Last 24h"
        else:
            await update.message.reply_text("Usage: /stats [day|week|month]")
            return

        address = self._settings.hl_account_address
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000
        fills = await asyncio.to_thread(
            self._info.user_fills_by_time, address, start_ms, now_ms
        )

        closes = [f for f in fills if "Close" in f.get("dir", "")]
        if not closes:
            await update.message.reply_text(f"No closed trades — {label}.")
            return

        def net(f: dict) -> float:
            return float(f.get("closedPnl", 0)) - float(f.get("fee", 0))

        total = len(closes)
        wins = [f for f in closes if net(f) >= 0]
        losses = [f for f in closes if net(f) < 0]
        win_rate = len(wins) / total * 100
        total_pnl = sum(net(f) for f in closes)
        avg_win = sum(net(f) for f in wins) / len(wins) if wins else 0
        avg_loss = sum(net(f) for f in losses) / len(losses) if losses else 0
        best = max(closes, key=net)
        worst = min(closes, key=net)

        from services.trade_executor import fetch_account_equity

        equity = await fetch_account_equity(self._info, address)
        equity_str = f"${equity:,.2f}" if equity > 0 else "N/A"
        pnl_sign = "+" if total_pnl >= 0 else ""

        def side_of(f: dict) -> str:
            return "LONG" if "Long" in f.get("dir", "") else "SHORT"

        best_label = f"+${net(best):,.2f} ({best.get('coin')} {side_of(best)})"
        worst_label = f"-${abs(net(worst)):,.2f} ({worst.get('coin')} {side_of(worst)})"

        await update.message.reply_text(
            f"📊 Performance — {_b(label)}\n\n"
            f"💰 Equity: {_b(equity_str)}\n"
            f"🏁 Closes: {_b(total)} | Win Rate: {_b(f'{win_rate:.1f}%')}\n"
            f"💵 Total PnL: {_b(f'{pnl_sign}${total_pnl:,.2f}')}\n"
            f"📈 Avg Win: {_b(f'+${avg_win:,.2f}')} | 📉 Avg Loss: {_b(f'-${abs(avg_loss):,.2f}')}\n"
            f"🏆 Best: {_b(best_label)}\n"
            f"💀 Worst: {_b(worst_label)}",
            parse_mode="HTML",
        )

    async def _cmd_signal(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        if not signal_log:
            await update.message.reply_text("No signals received since last restart.")
            return
        lines = []
        for entry in reversed(signal_log[-20:]):
            outcome = entry.get("outcome", "?")
            if outcome == "filled":
                icon = "✅"
                fill_px = entry.get("entry", 0)
                detail = f"@ {_b(f'${fill_px:,.2f}')}"
            elif outcome == "rejected":
                icon = "⏭"
                detail = _b(entry.get("reason", ""))
            else:
                icon = "❌"
                detail = _b(entry.get("reason", ""))
            lines.append(
                f"{icon} {entry.get('coin', '?')} {entry.get('side', '?')} — {detail}"
            )
        await update.message.reply_text(
            f"📡 Signal Log (last {_b(len(lines))})\n\n" + "\n".join(lines),
            parse_mode="HTML",
        )

    async def _cmd_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "📊 /status — open positions or available balance\n"
            "📋 /position — live positions with their TP/SL orders\n"
            "⏸ /pause — stop processing signals\n"
            "▶️ /resume — resume signals\n"
            "📜 /history — last 10 closed fills\n"
            "🔒 /close <COIN|all> — close a coin's position or all positions\n"
            "⚠️ /unprotected — positions missing full TP/SL coverage on HL\n"
            "🚨 /unprotected_close — close positions missing full coverage\n"
            "📈 /stats — performance (or /stats day, /stats month)\n"
            "📡 /signal — recent signal log (filled, rejected, errors)\n"
            "❓ /help — this message"
        )

    async def _send_startup_message(self) -> None:
        address = self._settings.hl_account_address
        short_address = f"{address[:6]}...{address[-4:]}"

        if self._settings.risk_pct is not None:
            sizing = f"{self._settings.risk_pct * 100:.1f}% risk per trade"
        elif self._settings.position_size_usd is not None:
            sizing = f"${self._settings.position_size_usd:,.0f} fixed margin"
        else:
            sizing = f"{self._settings.position_size_pct * 100:.0f}% equity per trade"

        try:
            user_state = await asyncio.to_thread(self._info.user_state, address)
            margin_summary = user_state.get("marginSummary", {})
            perps_equity = float(margin_summary.get("accountValue", 0))
            spot_state = await asyncio.to_thread(self._info.spot_user_state, address)
            spot_usdc = next(
                (float(b["total"]) for b in spot_state.get("balances", []) if b["coin"] == "USDC"),
                0.0,
            )
            equity = max(perps_equity, spot_usdc)
            equity_str = f"${equity:,.2f}"
        except Exception:
            equity_str = "N/A"

        algorithms_str = ", ".join(self._settings.algorithms)

        await self.send(
            f"🟢 BadgerBot Hyper started\n\n"
            f"👛 Account: {_b(short_address)}\n"
            f"💰 Equity: {_b(equity_str)}\n"
            f"📐 Sizing: {_b(sizing)}\n"
            f"🧠 Algorithms: {_b(algorithms_str)}\n"
            f"   Double-check spelling against your subscription —"
            f" send /start to @badger_trading_bot to view compatible algorithms.\n"
            f"📡 Listening for signals..."
        )

    async def run(self, stop_event: asyncio.Event) -> None:
        async with self._app:
            await self._app.bot.set_my_commands(
                [
                    BotCommand("status", "📊 Open positions or balance"),
                    BotCommand("position", "📋 Positions with live TP/SL"),
                    BotCommand("history", "📜 Last 10 closed fills"),
                    BotCommand("stats", "📈 Performance dashboard"),
                    BotCommand("signal", "📡 Recent signal log"),
                    BotCommand("close", "🔒 Close a coin or all positions"),
                    BotCommand("unprotected", "⚠️ Positions missing TP/SL coverage"),
                    BotCommand("unprotected_close", "🚨 Close positions missing coverage"),
                    BotCommand("pause", "⏸ Stop processing signals"),
                    BotCommand("resume", "▶️ Resume processing signals"),
                    BotCommand("help", "❓ List all commands"),
                ]
            )
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot polling started.")
            await self._send_startup_message()
            await stop_event.wait()
            await self._app.updater.stop()
            await self._app.stop()
        logger.info("Telegram bot stopped.")
