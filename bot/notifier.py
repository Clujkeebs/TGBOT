"""Telegram message formatting + delivery."""
from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import WSOL_MINT
from bot.swap_parser import ParsedSwap

log = logging.getLogger("copybot.notifier")


def tx_link(sig: str, text: str = "tx") -> str:
    return f'<a href="https://solscan.io/tx/{sig}">{text}</a>'


def token_link(mint: str) -> str:
    return (f'<a href="https://dexscreener.com/solana/{mint}">chart</a> · '
            f'<a href="https://solscan.io/token/{mint}">token</a>')


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if len(addr) > 10 else addr


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def fmt_amount(x: float) -> str:
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{x:.6g}"


def leader_trade_text(swap: ParsedSwap, label: str, sol_value: float) -> str:
    icon = "🟢" if swap.side == "buy" else "🔴"
    who = esc(label) or short(swap.wallet)
    quote = "SOL" if swap.quote_mint == WSOL_MINT else short(swap.quote_mint)
    lines = [
        f"{icon} <b>{who}</b> {swap.side.upper()} <code>{swap.token_mint}</code>",
        f"{fmt_amount(swap.token_amount)} tokens for {fmt_amount(swap.quote_amount)} {quote}"
        + (f" (≈{sol_value:.4f} SOL)" if quote != "SOL" and sol_value else ""),
    ]
    if swap.side == "sell":
        lines.append(f"Sold {swap.sell_fraction * 100:.0f}% of their position")
    elif swap.wallet_pre_sol > 0 and sol_value:
        lines.append(f"≈{sol_value / swap.wallet_pre_sol * 100:.1f}% of their SOL")
    lines.append(f"{token_link(swap.token_mint)} · {tx_link(swap.signature, 'leader tx')}")
    return "\n".join(lines)


def confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Copy", callback_data=f"cf:y:{pending_id}"),
        InlineKeyboardButton(text="❌ Skip", callback_data=f"cf:n:{pending_id}"),
    ]])


class Notifier:
    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

    async def send(self, text: str, reply_markup=None) -> int:
        try:
            m = await self.bot.send_message(self.chat_id, text, reply_markup=reply_markup,
                                            disable_web_page_preview=True)
            return m.message_id
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram send failed: %s", e)
            return 0

    async def edit(self, message_id: int, text: str) -> None:
        if not message_id:
            await self.send(text)
            return
        try:
            await self.bot.edit_message_text(text, chat_id=self.chat_id, message_id=message_id,
                                             disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.debug("edit failed; sending new message", exc_info=True)
            await self.send(text)
