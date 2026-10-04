"""aiogram 3 command interface (owner-only)."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, TelegramObject)
from sqlalchemy import select

from bot.config import WSOL_MINT
from bot.engine import CopyEngine
from bot.models import Blacklist, Leader, Position, Trade
from bot.notifier import esc, fmt_amount, short, token_link, tx_link
from bot.wallet import WalletManager, is_valid_pubkey

log = logging.getLogger("copybot.tg")

HELP = """<b>Solana copy-trading bot</b>

<b>Wallet</b>
/wallet – address &amp; SOL balance
/deposit – deposit QR code
/balance – SOL + token holdings
/withdraw &lt;amount|all&gt; &lt;address&gt; – send SOL out
/exportkey – reveal private key (to import in Phantom)

<b>Leaders</b>
/leaders – tracked wallets &amp; stats
/addleader &lt;address&gt; [label]
/rmleader &lt;address|label&gt;
/weight &lt;address|label&gt; &lt;pct&gt; – per-leader size multiplier

<b>Trading settings</b>
/mode auto|confirm|notify
/size fixed &lt;SOL&gt; | percent &lt;%&gt; | mirror &lt;%&gt;
/caps &lt;min SOL&gt; &lt;max SOL&gt;
/slippage &lt;%&gt;
/impact &lt;%&gt; – max price impact for buys
/priority &lt;SOL&gt; – max priority fee per tx
/tpsl &lt;tp%&gt; &lt;sl%&gt; – 0 disables
/loss &lt;%&gt; – daily loss limit (auto-pause)
/copysells on|off
/timeout &lt;seconds&gt; – confirm-mode timeout
/blacklist [add|rm &lt;mint&gt; [reason]]
/pause · /resume

<b>Positions</b>
/positions – open positions with live P/L
/buy &lt;mint&gt; &lt;SOL&gt; – manual buy
/sell &lt;mint|all&gt; [pct] – manual sell
/trades – recent activity
/summary [hour|off] – 24h summary / schedule (UTC)
/status – everything at a glance

<b>Sizing modes</b>
• <code>fixed 0.05</code> – every buy is 0.05 SOL
• <code>percent 5</code> – 5% of my SOL balance per buy
• <code>mirror 100</code> – same % of my SOL as the leader used of theirs (200 = 2×)
Sells always mirror the fraction the leader sold."""

BOT_COMMANDS = [
    ("status", "Overview"), ("wallet", "Wallet address & balance"), ("balance", "Holdings"),
    ("positions", "Open positions & P/L"), ("leaders", "Tracked leaders"), ("mode", "auto|confirm|notify"),
    ("size", "Sizing mode"), ("pause", "Stop copying"), ("resume", "Resume copying"), ("help", "All commands"),
]


class OwnerOnly(BaseMiddleware):
    def __init__(self, owner_ids: set[int]):
        self.owner_ids = owner_ids

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]], event: TelegramObject,
                       data: dict) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.id not in self.owner_ids:
            if isinstance(event, Message):
                log.warning("Ignored message from unauthorized user %s", getattr(user, "id", None))
                await event.answer("⛔ This is a private bot.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Unauthorized", show_alert=True)
            return None
        return await handler(event, data)


def _parse_float(s: str) -> float:
    return float(s.strip().rstrip("%").replace(",", "."))


def _yes_no(b: bool) -> str:
    return "on" if b else "off"


class TelegramUI:
    def __init__(self, bot: Bot, engine: CopyEngine, wallet: WalletManager, owner_ids: set[int],
                 on_leaders_changed: Callable[[], Awaitable[None]] | None = None,
                 hot_wallet_max_sol: float = 5.0, ingest_desc: str = ""):
        self.bot = bot
        self.engine = engine
        self.db = engine.db
        self.wallet = wallet
        self.on_leaders_changed = on_leaders_changed
        self.hot_wallet_max_sol = hot_wallet_max_sol
        self.ingest_desc = ingest_desc
        self._pending_actions: dict[str, tuple[float, str, dict]] = {}
        self.dp = Dispatcher()
        r = Router()
        r.message.outer_middleware(OwnerOnly(owner_ids))
        r.callback_query.outer_middleware(OwnerOnly(owner_ids))
        self._register(r)
        self.dp.include_router(r)

    def _register(self, r: Router) -> None:
        cmds = {
            "start": self.cmd_start, "help": self.cmd_help, "status": self.cmd_status,
            "wallet": self.cmd_wallet, "deposit": self.cmd_deposit, "balance": self.cmd_balance,
            "withdraw": self.cmd_withdraw, "exportkey": self.cmd_exportkey,
            "leaders": self.cmd_leaders, "addleader": self.cmd_addleader, "rmleader": self.cmd_rmleader,
            "weight": self.cmd_weight, "mode": self.cmd_mode, "size": self.cmd_size, "pct": self.cmd_size,
            "caps": self.cmd_caps, "slippage": self.cmd_slippage, "impact": self.cmd_impact,
            "priority": self.cmd_priority, "tpsl": self.cmd_tpsl, "loss": self.cmd_loss,
            "copysells": self.cmd_copysells, "timeout": self.cmd_timeout, "blacklist": self.cmd_blacklist,
            "pause": self.cmd_pause, "resume": self.cmd_resume, "positions": self.cmd_positions,
            "pnl": self.cmd_positions, "buy": self.cmd_buy, "sell": self.cmd_sell, "trades": self.cmd_trades,
            "summary": self.cmd_summary,
        }
        for name, fn in cmds.items():
            r.message.register(self._guard(fn), Command(name))
        r.callback_query.register(self.on_confirm, F.data.startswith("cf:"))
        r.callback_query.register(self.on_action, F.data.startswith("act:"))
        r.message.register(self.on_unknown)

    def _guard(self, fn):
        async def wrapped(message: Message, command: CommandObject):
            try:
                await fn(message, (command.args or "").split())
            except ValueError as e:
                await message.answer(f"⚠️ {esc(str(e))}")
            except Exception as e:  # noqa: BLE001
                log.exception("command %s failed", command.command)
                await message.answer(f"⚠️ Error: <code>{esc(str(e))[:300]}</code>")
        return wrapped

    async def set_menu(self) -> None:
        try:
            await self.bot.set_my_commands([BotCommand(command=c, description=d) for c, d in BOT_COMMANDS])
        except Exception:  # noqa: BLE001
            log.debug("set_my_commands failed", exc_info=True)

    # ---- pending inline actions (withdraw / export key / manual trades) ----
    def _stash(self, kind: str, data: dict) -> InlineKeyboardMarkup:
        now = time.time()
        for k in [k for k, v in self._pending_actions.items() if now - v[0] > 120]:
            self._pending_actions.pop(k)
        token = secrets.token_hex(6)
        self._pending_actions[token] = (now, kind, data)
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Confirm", callback_data=f"act:y:{token}"),
            InlineKeyboardButton(text="❌ Cancel", callback_data=f"act:n:{token}"),
        ]])

    async def on_action(self, call: CallbackQuery) -> None:
        _, yn, token = call.data.split(":", 2)
        item = self._pending_actions.pop(token, None)
        if item is None or time.time() - item[0] > 120:
            await call.answer("Expired - run the command again.", show_alert=True)
            return
        _, kind, data = item
        await call.message.edit_reply_markup(reply_markup=None)
        if yn != "y":
            await call.answer("Cancelled")
            await call.message.answer("Cancelled.")
            return
        await call.answer("Working…")
        try:
            if kind == "withdraw":
                sig, amount = await self.wallet.withdraw_sol(data["amount"], data["dest"])
                await self.engine.on_withdraw(amount)
                await call.message.answer(f"✅ Sent {amount:.6f} SOL to <code>{data['dest']}</code> · {tx_link(sig)}")
            elif kind == "exportkey":
                m = await call.message.answer(
                    f"🔑 <b>Private key</b> (deleting in 60s):\n<tg-spoiler><code>{self.wallet.export_secret()}</code></tg-spoiler>")
                self.engine.spawn(self._delete_later(m.chat.id, m.message_id, 60))
            elif kind == "buy":
                cfg = await self.db.get_config()
                tid = await self.engine._new_manual_trade("buy", data["mint"], "manual")
                await call.message.answer(f"⏳ Buying {data['sol']:.4f} SOL of <code>{data['mint']}</code>…")
                _, line = await self.engine.buy(data["mint"], data["sol"], cfg, trade_id=tid, origin="manual")
                await call.message.answer(line, disable_web_page_preview=True)
            elif kind == "sell":
                cfg = await self.db.get_config()
                for mint in data["mints"]:
                    tid = await self.engine._new_manual_trade("sell", mint, "manual")
                    _, line = await self.engine.sell(mint, data["fraction"], cfg, trade_id=tid, origin="manual")
                    await call.message.answer(f"<code>{short(mint)}</code>: {line}", disable_web_page_preview=True)
        except Exception as e:  # noqa: BLE001
            log.exception("action %s failed", kind)
            await call.message.answer(f"❌ {esc(str(e))[:400]}")

    async def _delete_later(self, chat_id: int, message_id: int, delay: int) -> None:
        await asyncio.sleep(delay)
        try:
            await self.bot.delete_message(chat_id, message_id)
        except Exception:  # noqa: BLE001
            pass

    async def on_confirm(self, call: CallbackQuery) -> None:
        try:
            _, yn, pid = call.data.split(":")
            text = await self.engine.resolve_confirmation(int(pid), yn == "y")
        except Exception as e:  # noqa: BLE001
            log.exception("confirm failed")
            text = f"Error: {e}"[:180]
        await call.answer(text)

    async def on_unknown(self, message: Message) -> None:
        await message.answer("Unknown command. /help lists everything.")

    # ================================================================== #
    # Commands
    # ================================================================== #
    async def cmd_start(self, m: Message, args: list[str]) -> None:
        await m.answer(f"👋 Ready. Hot wallet: <code>{self.wallet.pubkey}</code>\n\n" + HELP,
                       disable_web_page_preview=True)

    async def cmd_help(self, m: Message, args: list[str]) -> None:
        await m.answer(HELP)

    async def cmd_status(self, m: Message, args: list[str]) -> None:
        cfg = await self.db.get_config()
        leaders = await self.db.active_leaders()
        sol = await self.wallet.sol_balance()
        up = int(time.time() - self.engine.started_at)
        st = self.engine.stats
        size = {"fixed": f"fixed {cfg.sizing_value:g} SOL", "percent": f"{cfg.sizing_value:g}% of balance",
                "mirror": f"mirror ×{cfg.sizing_value:g}%"}.get(cfg.sizing_mode, cfg.sizing_mode)
        await m.answer(
            f"<b>Status</b> {'⏸ PAUSED' if cfg.paused else '▶️ running'}\n"
            f"Mode: <b>{cfg.mode}</b> · Sizing: {size}\n"
            f"Caps: {cfg.min_trade_sol:g}–{cfg.max_trade_sol:g} SOL · Slippage {cfg.slippage_bps / 100:g}%\n"
            f"Max impact {cfg.max_price_impact_pct:g}% · Priority ≤{cfg.priority_fee_max_lamports / 1e9:g} SOL\n"
            f"TP {cfg.tp_pct:g}% / SL {cfg.sl_pct:g}% · Daily loss limit {cfg.daily_loss_limit_pct:g}%\n"
            f"Copy sells: {_yes_no(cfg.copy_sells)} · Confirm timeout {cfg.confirm_timeout_s}s\n"
            f"Leaders: {len(leaders)} active · Ingest: {self.ingest_desc}\n"
            f"Wallet: <code>{self.wallet.pubkey}</code> ({sol:.4f} SOL)\n"
            f"Uptime {up // 3600}h{up % 3600 // 60:02d}m · txs {st['leader_txs']} · swaps {st['swaps_detected']} · "
            f"copies {st['copies']} · failed {st['failures']}"
        )

    async def cmd_wallet(self, m: Message, args: list[str]) -> None:
        sol = await self.wallet.sol_balance()
        warn = f"\n⚠️ Above HOT_WALLET_MAX_SOL ({self.hot_wallet_max_sol:g}). Consider withdrawing." \
            if sol > self.hot_wallet_max_sol else ""
        await m.answer(f"👛 <code>{self.wallet.pubkey}</code>\nBalance: <b>{sol:.6f} SOL</b>{warn}\n"
                       f'<a href="https://solscan.io/account/{self.wallet.pubkey}">Solscan</a>',
                       disable_web_page_preview=True)

    async def cmd_deposit(self, m: Message, args: list[str]) -> None:
        await m.answer_photo(BufferedInputFile(self.wallet.deposit_qr_png(), "deposit.png"),
                             caption=f"Send SOL to:\n<code>{self.wallet.pubkey}</code>")

    async def cmd_balance(self, m: Message, args: list[str]) -> None:
        sol = await self.wallet.sol_balance()
        holdings = await self.wallet.token_holdings()
        prices = await self.engine.jup.prices_usd([WSOL_MINT] + [h.mint for h in holdings])
        sol_usd = prices.get(WSOL_MINT, 0.0)
        lines = [f"💰 <b>{sol:.6f} SOL</b>" + (f" (${sol * sol_usd:,.2f})" if sol_usd else "")]
        total_usd = sol * sol_usd
        for h in sorted(holdings, key=lambda h: -h.amount * prices.get(h.mint, 0.0)):
            usd = h.amount * prices.get(h.mint, 0.0)
            total_usd += usd
            lines.append(f"• <code>{short(h.mint)}</code> {fmt_amount(h.amount)}" + (f" (${usd:,.2f})" if usd else ""))
        if sol_usd:
            lines.append(f"\nTotal ≈ <b>${total_usd:,.2f}</b> ({total_usd / sol_usd:.4f} SOL)")
        await m.answer("\n".join(lines[:60]))

    async def cmd_withdraw(self, m: Message, args: list[str]) -> None:
        if len(args) != 2:
            raise ValueError("Usage: /withdraw <amount|all> <destination address>")
        amount = None if args[0].lower() in ("all", "max") else _parse_float(args[0])
        if amount is not None and amount <= 0:
            raise ValueError("Amount must be positive")
        if not is_valid_pubkey(args[1]):
            raise ValueError("Invalid destination address")
        what = "ALL SOL (minus fee reserve)" if amount is None else f"{amount:g} SOL"
        await m.answer(f"Send {what} to <code>{args[1]}</code>?",
                       reply_markup=self._stash("withdraw", {"amount": amount, "dest": args[1]}))

    async def cmd_exportkey(self, m: Message, args: list[str]) -> None:
        await m.answer("⚠️ This reveals the hot wallet private key in this chat. Anyone who sees it can drain "
                       "the wallet. Continue?", reply_markup=self._stash("exportkey", {}))

    # ---- leaders -------------------------------------------------------
    async def _find_leader(self, s, ref: str) -> Leader | None:
        res = await s.execute(select(Leader).where((Leader.address == ref) | (Leader.label == ref)))
        return res.scalars().first()

    async def cmd_leaders(self, m: Message, args: list[str]) -> None:
        async with self.db.session() as s:
            leaders = list((await s.execute(select(Leader).order_by(Leader.id))).scalars().all())
        if not leaders:
            await m.answer("No leaders yet. /addleader &lt;address&gt; [label]")
            return
        lines = ["<b>Leaders</b>"]
        for ld in leaders:
            last = ld.last_trade_at.strftime("%m-%d %H:%M") if ld.last_trade_at else "never"
            w = f" · weight {ld.weight_pct:g}%" if ld.weight_pct != 100 else ""
            lines.append(f"{'🟢' if ld.is_active else '⚪'} <b>{esc(ld.label) or '—'}</b> <code>{ld.address}</code>\n"
                         f"   {ld.trades_seen} trades ({ld.buys}B/{ld.sells}S) · last {last}{w}")
        await m.answer("\n".join(lines))

    async def cmd_addleader(self, m: Message, args: list[str]) -> None:
        if not args or not is_valid_pubkey(args[0]):
            raise ValueError("Usage: /addleader <wallet address> [label]")
        addr, label = args[0], " ".join(args[1:])[:64]
        if addr == self.wallet.pubkey:
            raise ValueError("That's the bot's own wallet")
        async with self.db.session() as s:
            ld = await self._find_leader(s, addr)
            if ld:
                ld.is_active = True
                if label:
                    ld.label = label
            else:
                s.add(Leader(address=addr, label=label))
        await self._leaders_changed()
        await m.answer(f"✅ Following <code>{addr}</code> {esc(label)}\nNew trades from now on will be copied.")

    async def cmd_rmleader(self, m: Message, args: list[str]) -> None:
        if not args:
            raise ValueError("Usage: /rmleader <address|label>")
        async with self.db.session() as s:
            ld = await self._find_leader(s, " ".join(args))
            if not ld:
                raise ValueError("Leader not found")
            ld.is_active = False
            addr = ld.address
        await self._leaders_changed()
        await m.answer(f"🗑 Stopped following <code>{addr}</code>")

    async def cmd_weight(self, m: Message, args: list[str]) -> None:
        if len(args) < 2:
            raise ValueError("Usage: /weight <address|label> <pct>  (100 = normal, 50 = half size)")
        pct = _parse_float(args[-1])
        if pct < 0 or pct > 1000:
            raise ValueError("Weight must be 0–1000%")
        async with self.db.session() as s:
            ld = await self._find_leader(s, " ".join(args[:-1]))
            if not ld:
                raise ValueError("Leader not found")
            ld.weight_pct = pct
        await m.answer(f"✅ Weight set to {pct:g}%")

    async def _leaders_changed(self) -> None:
        if self.on_leaders_changed:
            try:
                await self.on_leaders_changed()
            except Exception as e:  # noqa: BLE001
                log.exception("leader sync failed")
                await self.engine.notify.send(f"⚠️ Could not update Helius webhook: <code>{esc(str(e))[:200]}</code>")

    # ---- settings ------------------------------------------------------
    async def cmd_mode(self, m: Message, args: list[str]) -> None:
        if not args or args[0].lower() not in ("auto", "confirm", "notify"):
            cfg = await self.db.get_config()
            raise ValueError(f"Current mode: {cfg.mode}. Usage: /mode auto|confirm|notify")
        mode = args[0].lower()
        await self.db.update_config(mode=mode)
        extra = {"auto": "⚠️ Trades now execute automatically with real SOL.",
                 "confirm": "Each trade will ask for ✅/❌.", "notify": "Alerts only, nothing executes."}[mode]
        await m.answer(f"✅ Mode: <b>{mode}</b>\n{extra}")

    async def cmd_size(self, m: Message, args: list[str]) -> None:
        aliases = {"override": "percent", "multiplier": "mirror", "pct": "percent", "%": "percent"}
        if len(args) == 1:  # legacy "/pct 5"
            args = ["percent", args[0]]
        if len(args) != 2:
            raise ValueError("Usage: /size fixed <SOL> | percent <%> | mirror <%>")
        mode = aliases.get(args[0].lower(), args[0].lower())
        if mode not in ("fixed", "percent", "mirror"):
            raise ValueError("Mode must be fixed, percent or mirror")
        val = _parse_float(args[1])
        if val <= 0 or (mode == "percent" and val > 100) or (mode == "mirror" and val > 1000):
            raise ValueError("Value out of range")
        await self.db.update_config(sizing_mode=mode, sizing_value=val)
        await m.answer(f"✅ Sizing: {mode} {val:g}")

    async def cmd_caps(self, m: Message, args: list[str]) -> None:
        if len(args) != 2:
            raise ValueError("Usage: /caps <min SOL> <max SOL>")
        lo, hi = _parse_float(args[0]), _parse_float(args[1])
        if lo <= 0 or hi < lo:
            raise ValueError("Need 0 < min ≤ max")
        await self.db.update_config(min_trade_sol=lo, max_trade_sol=hi)
        await m.answer(f"✅ Trade size limits: {lo:g}–{hi:g} SOL")

    async def cmd_slippage(self, m: Message, args: list[str]) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /slippage <percent>, e.g. /slippage 5")
        pct = _parse_float(args[0])
        bps = int(round(pct * 100))
        if not 10 <= bps <= 5000:
            raise ValueError("Slippage must be between 0.1% and 50%")
        await self.db.update_config(slippage_bps=bps)
        await m.answer(f"✅ Slippage: {bps / 100:g}%")

    async def cmd_impact(self, m: Message, args: list[str]) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /impact <percent> (0 disables)")
        v = _parse_float(args[0])
        if v < 0 or v > 100:
            raise ValueError("0–100")
        await self.db.update_config(max_price_impact_pct=v)
        await m.answer(f"✅ Max price impact: {v:g}%" if v else "✅ Price impact check disabled")

    async def cmd_priority(self, m: Message, args: list[str]) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /priority <max SOL per tx>, e.g. /priority 0.002")
        v = _parse_float(args[0])
        if v < 0 or v > 0.1:
            raise ValueError("0–0.1 SOL")
        await self.db.update_config(priority_fee_max_lamports=int(v * 1e9))
        await m.answer(f"✅ Max priority fee: {v:g} SOL")

    async def cmd_tpsl(self, m: Message, args: list[str]) -> None:
        if len(args) != 2:
            raise ValueError("Usage: /tpsl <take-profit %> <stop-loss %>  (0 disables), e.g. /tpsl 100 30")
        tp, sl = _parse_float(args[0]), _parse_float(args[1])
        if tp < 0 or sl < 0 or sl >= 100:
            raise ValueError("tp ≥ 0, 0 ≤ sl < 100")
        await self.db.update_config(tp_pct=tp, sl_pct=sl)
        await m.answer(f"✅ TP {tp:g}% / SL {sl:g}%")

    async def cmd_loss(self, m: Message, args: list[str]) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /loss <percent> (0 disables)")
        v = _parse_float(args[0])
        if v < 0 or v > 100:
            raise ValueError("0–100")
        await self.db.update_config(daily_loss_limit_pct=v)
        await m.answer(f"✅ Daily loss limit: {v:g}%")

    async def cmd_copysells(self, m: Message, args: list[str]) -> None:
        if not args or args[0].lower() not in ("on", "off"):
            raise ValueError("Usage: /copysells on|off")
        await self.db.update_config(copy_sells=args[0].lower() == "on")
        await m.answer(f"✅ Copy sells: {args[0].lower()}")

    async def cmd_timeout(self, m: Message, args: list[str]) -> None:
        if len(args) != 1:
            raise ValueError("Usage: /timeout <seconds>")
        v = int(_parse_float(args[0]))
        if not 5 <= v <= 600:
            raise ValueError("5–600 seconds")
        await self.db.update_config(confirm_timeout_s=v)
        await m.answer(f"✅ Confirm timeout: {v}s")

    async def cmd_blacklist(self, m: Message, args: list[str]) -> None:
        if not args:
            async with self.db.session() as s:
                rows = list((await s.execute(select(Blacklist))).scalars().all())
            body = "\n".join(f"• <code>{b.token_mint}</code> {esc(b.reason)}" for b in rows) or "empty"
            await m.answer(f"<b>Blacklist</b>\n{body}\n\n/blacklist add|rm &lt;mint&gt; [reason]")
            return
        if len(args) < 2 or args[0] not in ("add", "rm") or not is_valid_pubkey(args[1]):
            raise ValueError("Usage: /blacklist add|rm <mint> [reason]")
        async with self.db.session() as s:
            row = (await s.execute(select(Blacklist).where(Blacklist.token_mint == args[1]))).scalar_one_or_none()
            if args[0] == "add" and row is None:
                s.add(Blacklist(token_mint=args[1], reason=" ".join(args[2:])[:200]))
            elif args[0] == "rm" and row is not None:
                await s.delete(row)
        await m.answer("✅ Blacklist updated")

    async def cmd_pause(self, m: Message, args: list[str]) -> None:
        await self.db.update_config(paused=True)
        await m.answer("⏸ Paused. Leader trades are still shown but nothing is copied. /resume to continue.")

    async def cmd_resume(self, m: Message, args: list[str]) -> None:
        cfg = await self.db.update_config(paused=False)
        await m.answer(f"▶️ Resumed (mode: {cfg.mode})")

    # ---- positions / trading -----------------------------------------
    async def cmd_positions(self, m: Message, args: list[str]) -> None:
        holdings = {h.mint: h for h in await self.wallet.token_holdings()}
        async with self.db.session() as s:
            positions = {p.token_mint: p for p in (await s.execute(select(Position))).scalars().all()}
        mints = list(holdings)
        if not mints:
            realized = sum(p.realized_pnl_sol for p in positions.values())
            await m.answer(f"No open positions. Realized P/L: <b>{realized:+.4f} SOL</b>")
            return
        prices = await self.engine.jup.prices_usd([WSOL_MINT] + mints)
        sol_usd = prices.get(WSOL_MINT, 0.0)
        lines = ["<b>Open positions</b>"]
        tot_val = tot_cost = 0.0
        for mint in mints:
            h = holdings[mint]
            p = positions.get(mint)
            val = h.amount * prices.get(mint, 0.0) / sol_usd if sol_usd else 0.0
            cost = p.cost_basis_sol if p else 0.0
            tot_val += val
            tot_cost += cost
            pnl = f" · P/L {val - cost:+.4f} ({(val / cost - 1) * 100:+.1f}%)" if cost > 0 and val > 0 else ""
            lines.append(f"• <code>{mint}</code>\n   {fmt_amount(h.amount)} ≈ {val:.4f} SOL · cost {cost:.4f}{pnl}\n"
                         f"   {token_link(mint)}")
        realized = sum(p.realized_pnl_sol for p in positions.values())
        lines.append(f"\nValue ≈ <b>{tot_val:.4f} SOL</b> · cost {tot_cost:.4f} · "
                     f"unrealized {tot_val - tot_cost:+.4f} · realized {realized:+.4f}")
        await m.answer("\n".join(lines[:80]), disable_web_page_preview=True)

    async def cmd_buy(self, m: Message, args: list[str]) -> None:
        if len(args) != 2 or not is_valid_pubkey(args[0]):
            raise ValueError("Usage: /buy <token mint> <SOL amount>")
        sol = _parse_float(args[1])
        if sol <= 0:
            raise ValueError("Amount must be positive")
        await m.answer(f"Buy <code>{args[0]}</code> for {sol:g} SOL?",
                       reply_markup=self._stash("buy", {"mint": args[0], "sol": sol}))

    async def cmd_sell(self, m: Message, args: list[str]) -> None:
        if not args:
            raise ValueError("Usage: /sell <mint|all> [percent]")
        pct = _parse_float(args[1]) if len(args) > 1 else 100.0
        if not 0 < pct <= 100:
            raise ValueError("Percent must be 1–100")
        if args[0].lower() == "all":
            mints = [h.mint for h in await self.wallet.token_holdings()]
            if not mints:
                raise ValueError("No tokens held")
        elif is_valid_pubkey(args[0]):
            mints = [args[0]]
        else:
            raise ValueError("Invalid mint")
        what = f"{len(mints)} tokens" if len(mints) > 1 else f"<code>{mints[0]}</code>"
        await m.answer(f"Sell {pct:g}% of {what}?",
                       reply_markup=self._stash("sell", {"mints": mints, "fraction": pct / 100}))

    async def cmd_trades(self, m: Message, args: list[str]) -> None:
        async with self.db.session() as s:
            rows = list((await s.execute(select(Trade).order_by(Trade.id.desc()).limit(15))).scalars().all())
        if not rows:
            await m.answer("No trades yet.")
            return
        icons = {"executed": "✅", "failed": "❌", "skipped": "⏭", "timeout": "⌛", "pending": "🟡", "seen": "👁"}
        lines = ["<b>Recent trades</b>"]
        for t in rows:
            our = f" → {t.our_sol:.4f} SOL" if t.status == "executed" else ""
            note = f" <i>{esc(t.note[:60])}</i>" if t.note and t.status != "executed" else ""
            link = f" {tx_link(t.copy_tx_sig, 'tx')}" if t.copy_tx_sig else ""
            lines.append(f"{icons.get(t.status, '•')} {t.created_at:%m-%d %H:%M} {t.origin} {t.side.upper()} "
                         f"<code>{short(t.token_mint)}</code>{our}{link}{note}")
        await m.answer("\n".join(lines), disable_web_page_preview=True)

    async def cmd_summary(self, m: Message, args: list[str]) -> None:
        if args:
            if args[0].lower() == "off":
                await self.db.update_config(daily_summary_hour_utc=-1)
                await m.answer("✅ Daily summary off")
                return
            h = int(args[0])
            if not 0 <= h <= 23:
                raise ValueError("Hour must be 0–23 (UTC)")
            await self.db.update_config(daily_summary_hour_utc=h)
            await m.answer(f"✅ Daily summary at {h:02d}:00 UTC")
            return
        await m.answer(await self.engine.summary_text())
