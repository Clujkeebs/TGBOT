"""Copy-trading engine: leader tx -> parse -> size -> risk -> execute -> record.

Also owns the background jobs: TP/SL monitor, daily summary, and the
daily-loss circuit breaker.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from bot.config import LAMPORTS_PER_SOL, WSOL_MINT
from bot.db import Database
from bot.jupiter import Jupiter, SwapResult
from bot.models import BotConfig, DailyBaseline, Leader, PendingConfirmation, Position, Trade, utcnow
from bot.notifier import Notifier, confirm_keyboard, esc, fmt_amount, leader_trade_text, short, tx_link
from bot.rpc import SolanaRpc
from bot.swap_parser import ParsedSwap, parse_swaps
from bot.wallet import WalletManager

log = logging.getLogger("copybot.engine")

BUY_FEE_RESERVE_SOL = 0.02  # never spend the last bit of SOL - it pays fees and rent
FULL_EXIT_FRACTION = 0.98  # leader sold >= 98% -> we sell 100%


@dataclass
class Decision:
    ok: bool
    sol_amount: float = 0.0
    reason: str = ""


def size_buy(cfg: BotConfig, leader_sol_value: float, leader_pre_sol: float, my_sol: float,
             weight_pct: float = 100.0) -> Decision:
    """Pure sizing math (unit-tested). Returns the SOL amount *before* risk caps."""
    weight = max(0.0, weight_pct) / 100.0
    if cfg.sizing_mode == "fixed":
        amt = cfg.sizing_value
        why = f"fixed {cfg.sizing_value:g} SOL"
    elif cfg.sizing_mode == "percent":
        amt = my_sol * cfg.sizing_value / 100.0
        why = f"{cfg.sizing_value:g}% of my {my_sol:.4f} SOL"
    elif cfg.sizing_mode == "mirror":
        if leader_pre_sol <= 0 or leader_sol_value <= 0:
            return Decision(False, 0.0, "mirror: leader SOL balance unknown")
        pct = leader_sol_value / leader_pre_sol
        amt = my_sol * pct * cfg.sizing_value / 100.0
        why = f"leader used {pct * 100:.2f}% of their SOL × {cfg.sizing_value:g}%"
    else:
        return Decision(False, 0.0, f"unknown sizing mode {cfg.sizing_mode}")
    if weight != 1.0:
        amt *= weight
        why += f" × leader weight {weight_pct:g}%"
    return Decision(True, amt, why)


def apply_buy_caps(cfg: BotConfig, amount: float, my_sol: float) -> Decision:
    """Clamp a buy to max/balance and reject it if it ends up below the minimum."""
    if amount < cfg.min_trade_sol:
        return Decision(False, 0.0, f"size {amount:.4f} SOL below min {cfg.min_trade_sol:g}")
    capped = min(amount, cfg.max_trade_sol, max(0.0, my_sol - BUY_FEE_RESERVE_SOL))
    if capped < cfg.min_trade_sol:
        return Decision(False, 0.0, f"insufficient SOL: have {my_sol:.4f}, need {cfg.min_trade_sol:g} + fees")
    note = f"capped to {capped:.4f} SOL" if capped < amount - 1e-9 else ""
    return Decision(True, capped, note)


class CopyEngine:
    def __init__(self, db: Database, rpc: SolanaRpc, wallet: WalletManager, jupiter: Jupiter, notifier: Notifier):
        self.db = db
        self.rpc = rpc
        self.wallet = wallet
        self.jup = jupiter
        self.notify = notifier
        self._mint_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._timeouts: dict[int, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._portfolio_cache: tuple[float, float] | None = None
        self._breaker_alerted_day = ""
        self._summary_sent_day = ""
        self.started_at = time.time()
        self.stats = {"leader_txs": 0, "swaps_detected": 0, "copies": 0, "failures": 0}

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ================================================================== #
    # Ingestion entry point
    # ================================================================== #
    async def handle_leader_signature(self, leader: str, signature: str, tx: dict | None = None) -> None:
        """Process one leader transaction exactly once."""
        if not await self.db.mark_processed(signature):
            return
        self.stats["leader_txs"] += 1
        if tx is None:
            for attempt in range(4):
                try:
                    tx = await self.rpc.get_transaction(signature)
                except Exception as e:  # noqa: BLE001
                    log.warning("getTransaction %s failed: %s", signature, e)
                if tx:
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        if not tx:
            log.warning("tx %s not found", signature)
            return
        swaps = parse_swaps(tx, leader)
        if not swaps:
            return
        self.stats["swaps_detected"] += len(swaps)
        for swap in swaps:
            try:
                await self.handle_swap(swap)
            except Exception as e:  # noqa: BLE001
                log.exception("handle_swap failed for %s", signature)
                await self.notify.send(f"⚠️ Error handling leader trade {tx_link(signature)}:\n<code>{esc(str(e))[:300]}</code>")

    async def handle_swap(self, swap: ParsedSwap) -> None:
        cfg = await self.db.get_config()
        leader = await self._leader(swap.wallet)
        label = leader.label if leader else ""
        weight = leader.weight_pct if leader else 100.0
        sol_value = await self._sol_value(swap)
        await self._record_leader_stats(swap.wallet, swap.side)

        trade_id = await self._new_trade(swap, sol_value)
        header = leader_trade_text(swap, label, sol_value)

        # ---- decide -------------------------------------------------------
        if cfg.mode == "notify":
            return await self._finish_skip(trade_id, header, "notify mode - not copied", "seen")
        if cfg.paused:
            return await self._finish_skip(trade_id, header, "bot paused (/resume to copy again)")
        if swap.side == "sell" and not cfg.copy_sells:
            return await self._finish_skip(trade_id, header, "copy_sells is off")

        if swap.side == "buy":
            plan = await self.plan_buy(cfg, swap, sol_value, weight)
            if not plan.ok:
                return await self._finish_skip(trade_id, header, plan.reason)
            payload = {"swap": swap.to_dict(), "sol_amount": plan.sol_amount}
            action = f"Copy: BUY {plan.sol_amount:.4f} SOL" + (f" ({esc(plan.reason)})" if plan.reason else "")
        else:
            raw, _ = await self.wallet.token_balance(swap.token_mint)
            if raw <= 0:
                return await self._finish_skip(trade_id, header, "we don't hold this token")
            frac = 1.0 if swap.sell_fraction >= FULL_EXIT_FRACTION else swap.sell_fraction
            if frac <= 0:
                return await self._finish_skip(trade_id, header, "could not determine sell fraction")
            payload = {"swap": swap.to_dict(), "sell_fraction": frac}
            action = f"Copy: SELL {frac * 100:.0f}% of our position"

        if cfg.mode == "confirm":
            await self._request_confirmation(trade_id, payload, f"{header}\n\n🟡 <b>{action}?</b>", cfg)
            return

        msg_id = await self.notify.send(f"{header}\n\n⏳ {action}…")
        await self._execute(trade_id, payload, msg_id, header)

    async def plan_buy(self, cfg: BotConfig, swap: ParsedSwap, sol_value: float, weight: float) -> Decision:
        if await self.db.is_blacklisted(swap.token_mint):
            return Decision(False, 0.0, "token is blacklisted")
        if not await self.check_daily_loss(cfg):
            return Decision(False, 0.0, "daily loss limit hit - bot paused")
        my_sol = await self.wallet.sol_balance()
        sized = size_buy(cfg, sol_value, swap.wallet_pre_sol, my_sol, weight)
        if not sized.ok:
            return sized
        capped = apply_buy_caps(cfg, sized.sol_amount, my_sol)
        if not capped.ok:
            return Decision(False, 0.0, f"{capped.reason} ({sized.reason})")
        return Decision(True, capped.sol_amount, "; ".join(x for x in (sized.reason, capped.reason) if x))

    # ================================================================== #
    # Execution
    # ================================================================== #
    async def _execute(self, trade_id: int, payload: dict, msg_id: int, header: str) -> None:
        swap = ParsedSwap.from_dict(payload["swap"])
        cfg = await self.db.get_config()
        if swap.side == "buy":
            res, line = await self.buy(swap.token_mint, payload["sol_amount"], cfg, swap.token_decimals,
                                       trade_id=trade_id)
        else:
            res, line = await self.sell(swap.token_mint, payload["sell_fraction"], cfg, trade_id=trade_id)
        await self.notify.edit(msg_id, f"{header}\n\n{line}")

    async def buy(self, mint: str, sol_amount: float, cfg: BotConfig, decimals_hint: int = 0,
                  trade_id: int | None = None, origin: str = "copy") -> tuple[SwapResult, str]:
        async with self._mint_locks[mint]:
            res = await self.jup.execute(
                self.rpc, self.wallet.keypair, WSOL_MINT, mint, int(sol_amount * LAMPORTS_PER_SOL),
                cfg.slippage_bps, cfg.priority_fee_max_lamports, cfg.max_price_impact_pct,
            )
            if res.ok:
                spent = res.sol_amount if res.measured else sol_amount
                dec = res.token_decimals if res.measured else decimals_hint
                got = res.token_amount if res.measured else res.out_amount_raw / 10**dec
                await self._position_add(mint, got, dec, spent)
                await self._finish_trade(trade_id, "executed", res.signature, spent, got, origin=origin)
                self.stats["copies"] += 1
                line = (f"✅ Bought {fmt_amount(got)} tokens for {spent:.4f} SOL "
                        f"(impact {res.price_impact_pct:.2f}%) · {tx_link(res.signature, 'our tx')}")
            else:
                await self._finish_trade(trade_id, "failed", res.signature, note=res.error, origin=origin)
                self.stats["failures"] += 1
                line = f"❌ Buy failed: <code>{esc(res.error)[:300]}</code>"
                if res.signature:
                    line += f" · {tx_link(res.signature)}"
            return res, line

    async def sell(self, mint: str, fraction: float, cfg: BotConfig, trade_id: int | None = None,
                   origin: str = "copy", max_impact: float | None = None) -> tuple[SwapResult, str]:
        async with self._mint_locks[mint]:
            raw, dec = await self.wallet.token_balance(mint)
            amount_raw = raw if fraction >= 0.999 else int(raw * fraction)
            if amount_raw <= 0:
                await self._finish_trade(trade_id, "skipped", note="no balance", origin=origin)
                return SwapResult(False, error="no balance"), "⏭ Nothing to sell (zero balance)"
            # Exits ignore the price-impact limit unless told otherwise - getting out matters more.
            res = await self.jup.execute(
                self.rpc, self.wallet.keypair, mint, WSOL_MINT, amount_raw, cfg.slippage_bps,
                cfg.priority_fee_max_lamports, max_impact if max_impact is not None else 0,
            )
            if res.ok:
                sold = res.token_amount if res.measured else amount_raw / 10**dec
                got = res.sol_amount if res.measured else res.out_amount_raw / LAMPORTS_PER_SOL
                pnl = await self._position_remove(mint, sold, got, raw / 10**dec if dec >= 0 else 0, dec)
                await self._finish_trade(trade_id, "executed", res.signature, got, sold, origin=origin)
                self.stats["copies"] += 1
                line = (f"✅ Sold {fmt_amount(sold)} tokens for {got:.4f} SOL "
                        f"(P/L {pnl:+.4f} SOL) · {tx_link(res.signature, 'our tx')}")
            else:
                await self._finish_trade(trade_id, "failed", res.signature, note=res.error, origin=origin)
                self.stats["failures"] += 1
                line = f"❌ Sell failed: <code>{esc(res.error)[:300]}</code>"
                if res.signature:
                    line += f" · {tx_link(res.signature)}"
            return res, line

    # ================================================================== #
    # Confirm mode
    # ================================================================== #
    async def _request_confirmation(self, trade_id: int, payload: dict, text: str, cfg: BotConfig) -> None:
        async with self.db.session() as s:
            pc = PendingConfirmation(trade_id=trade_id, payload=json.dumps(payload))
            s.add(pc)
            await s.flush()
            pid = pc.id
        await self._set_trade_status(trade_id, "pending")
        timeout = max(5, cfg.confirm_timeout_s)
        msg_id = await self.notify.send(f"{text}\n<i>Expires in {timeout}s</i>", reply_markup=confirm_keyboard(pid))
        async with self.db.session() as s:
            pc = await s.get(PendingConfirmation, pid)
            pc.message_id = msg_id
        self._timeouts[pid] = self.spawn(self._confirm_timeout(pid, timeout, text))

    async def _claim_pending(self, pid: int) -> PendingConfirmation | None:
        async with self.db.session() as s:
            pc = await s.get(PendingConfirmation, pid)
            if pc is None or pc.resolved:
                return None
            pc.resolved = True
            return pc

    async def _confirm_timeout(self, pid: int, timeout: int, text: str) -> None:
        await asyncio.sleep(timeout)
        self._timeouts.pop(pid, None)
        pc = await self._claim_pending(pid)
        if pc is None:
            return
        await self._set_trade_status(pc.trade_id, "timeout")
        await self.notify.edit(pc.message_id, f"{text}\n\n⌛ Timed out - skipped")

    async def resolve_confirmation(self, pid: int, approve: bool) -> str:
        pc = await self._claim_pending(pid)
        if pc is None:
            return "Already handled or expired."
        t = self._timeouts.pop(pid, None)
        if t:
            t.cancel()
        payload = json.loads(pc.payload)
        swap = ParsedSwap.from_dict(payload["swap"])
        leader = await self._leader(swap.wallet)
        header = leader_trade_text(swap, leader.label if leader else "", swap.sol_amount)
        if not approve:
            await self._set_trade_status(pc.trade_id, "skipped", "skipped by user")
            await self.notify.edit(pc.message_id, f"{header}\n\n⏭ Skipped")
            return "Skipped."
        cfg = await self.db.get_config()
        if cfg.paused:
            await self.notify.edit(pc.message_id, f"{header}\n\n⏸ Bot is paused - not executed")
            return "Bot is paused."
        if swap.side == "buy":  # re-check caps against the current balance
            capped = apply_buy_caps(cfg, payload["sol_amount"], await self.wallet.sol_balance())
            if not capped.ok:
                await self._set_trade_status(pc.trade_id, "skipped", capped.reason)
                await self.notify.edit(pc.message_id, f"{header}\n\n⏭ {esc(capped.reason)}")
                return capped.reason
            payload["sol_amount"] = capped.sol_amount
        await self.notify.edit(pc.message_id, f"{header}\n\n⏳ Executing…")
        self.spawn(self._execute(pc.trade_id, payload, pc.message_id, header))
        return "Executing…"

    # ================================================================== #
    # Risk: daily loss breaker
    # ================================================================== #
    async def portfolio_sol(self, max_age_s: float = 20.0) -> float:
        now = time.monotonic()
        if self._portfolio_cache and now - self._portfolio_cache[1] < max_age_s:
            return self._portfolio_cache[0]
        sol = await self.wallet.sol_balance()
        holdings = await self.wallet.token_holdings()
        if holdings:
            prices = await self.jup.prices_usd([WSOL_MINT] + [h.mint for h in holdings])
            sol_usd = prices.get(WSOL_MINT, 0.0)
            if sol_usd > 0:
                sol += sum(h.amount * prices.get(h.mint, 0.0) for h in holdings) / sol_usd
        self._portfolio_cache = (sol, now)
        return sol

    async def check_daily_loss(self, cfg: BotConfig) -> bool:
        if cfg.daily_loss_limit_pct <= 0:
            return True
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            current = await self.portfolio_sol()
        except Exception as e:  # noqa: BLE001
            log.warning("portfolio valuation failed (%s) - allowing trade", e)
            return True
        async with self.db.session() as s:
            base = await s.get(DailyBaseline, day)
            if base is None:
                s.add(DailyBaseline(day=day, portfolio_sol=current))
                return True
            baseline = base.portfolio_sol
        if baseline <= 0:
            return True
        drawdown = (baseline - current) / baseline * 100
        if drawdown < cfg.daily_loss_limit_pct:
            return True
        await self.db.update_config(paused=True)
        if self._breaker_alerted_day != day:
            self._breaker_alerted_day = day
            await self.notify.send(
                f"🛑 <b>Daily loss limit hit</b>: portfolio down {drawdown:.1f}% today "
                f"({baseline:.4f} → {current:.4f} SOL). Bot paused. /resume to continue."
            )
        return False

    async def on_withdraw(self, amount_sol: float) -> None:
        """Withdrawals aren't losses - lower today's baseline accordingly."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._portfolio_cache = None
        async with self.db.session() as s:
            base = await s.get(DailyBaseline, day)
            if base:
                base.portfolio_sol = max(0.0, base.portfolio_sol - amount_sol)

    # ================================================================== #
    # Background loops
    # ================================================================== #
    async def tp_sl_loop(self, stop: asyncio.Event, interval_s: float = 20.0) -> None:
        log.info("TP/SL monitor started")
        while not stop.is_set():
            try:
                cfg = await self.db.get_config()
                if (cfg.tp_pct > 0 or cfg.sl_pct > 0) and not cfg.paused:
                    await self._scan_tp_sl(cfg)
            except Exception:  # noqa: BLE001
                log.exception("TP/SL scan failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass

    async def _scan_tp_sl(self, cfg: BotConfig) -> None:
        async with self.db.session() as s:
            positions = list((await s.execute(select(Position).where(Position.qty > 0))).scalars().all())
        for p in positions:
            if p.cost_basis_sol <= 0:
                continue
            raw, _ = await self.wallet.token_balance(p.token_mint)
            if raw <= 0:
                await self._zero_position(p.token_mint)
                continue
            try:
                q = await self.jup.quote(p.token_mint, WSOL_MINT, raw, cfg.slippage_bps)
            except Exception as e:  # noqa: BLE001
                log.debug("tp/sl quote %s: %s", p.token_mint, e)
                continue
            value = int(q["outAmount"]) / LAMPORTS_PER_SOL
            change = (value / p.cost_basis_sol - 1) * 100
            kind = ""
            if cfg.tp_pct > 0 and change >= cfg.tp_pct:
                kind = "tp"
            elif cfg.sl_pct > 0 and change <= -cfg.sl_pct:
                kind = "sl"
            if not kind:
                continue
            label = "🎯 Take-profit" if kind == "tp" else "🛡 Stop-loss"
            msg_id = await self.notify.send(
                f"{label} on <code>{p.token_mint}</code>: {change:+.1f}% "
                f"({p.cost_basis_sol:.4f} → {value:.4f} SOL). Selling…")
            trade_id = await self._new_manual_trade("sell", p.token_mint, kind)
            _, line = await self.sell(p.token_mint, 1.0, cfg, trade_id=trade_id, origin=kind)
            await self.notify.edit(msg_id, f"{label} on <code>{p.token_mint}</code> ({change:+.1f}%)\n{line}")

    async def daily_summary_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                cfg = await self.db.get_config()
                now = datetime.now(timezone.utc)
                day = now.strftime("%Y-%m-%d")
                if cfg.daily_summary_hour_utc == now.hour and self._summary_sent_day != day:
                    self._summary_sent_day = day
                    await self.notify.send(await self.summary_text())
                await self.db.prune_processed()
            except Exception:  # noqa: BLE001
                log.exception("daily summary failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def summary_text(self, hours: int = 24) -> str:
        since = utcnow() - timedelta(hours=hours)
        async with self.db.session() as s:
            trades = list((await s.execute(select(Trade).where(Trade.created_at >= since))).scalars().all())
            positions = list((await s.execute(select(Position))).scalars().all())
        execd = [t for t in trades if t.status == "executed"]
        buys = [t for t in execd if t.side == "buy"]
        sells = [t for t in execd if t.side == "sell"]
        failed = sum(1 for t in trades if t.status == "failed")
        realized = sum(p.realized_pnl_sol for p in positions)
        open_pos = sum(1 for p in positions if p.qty > 0)
        return (
            f"📊 <b>Summary (last {hours}h)</b>\n"
            f"Leader trades seen: {len(trades)}\n"
            f"Copied: {len(buys)} buys ({sum(t.our_sol for t in buys):.4f} SOL) / "
            f"{len(sells)} sells ({sum(t.our_sol for t in sells):.4f} SOL)\n"
            f"Failed: {failed}\n"
            f"Open positions: {open_pos}\n"
            f"Realized P/L (all time): <b>{realized:+.4f} SOL</b>"
        )

    # ================================================================== #
    # DB helpers
    # ================================================================== #
    async def _leader(self, address: str) -> Leader | None:
        async with self.db.session() as s:
            return (await s.execute(select(Leader).where(Leader.address == address))).scalar_one_or_none()

    async def _sol_value(self, swap: ParsedSwap) -> float:
        if swap.quote_mint == WSOL_MINT:
            return swap.sol_amount
        prices = await self.jup.prices_usd([swap.quote_mint, WSOL_MINT])
        q, s = prices.get(swap.quote_mint, 0.0), prices.get(WSOL_MINT, 0.0)
        value = swap.quote_amount * q / s if q > 0 and s > 0 else 0.0
        swap.sol_amount = value
        return value

    async def _record_leader_stats(self, address: str, side: str) -> None:
        async with self.db.session() as s:
            ld = (await s.execute(select(Leader).where(Leader.address == address))).scalar_one_or_none()
            if ld:
                ld.trades_seen += 1
                if side == "buy":
                    ld.buys += 1
                else:
                    ld.sells += 1
                ld.last_trade_at = utcnow()

    async def _new_trade(self, swap: ParsedSwap, sol_value: float) -> int:
        async with self.db.session() as s:
            t = Trade(leader=swap.wallet, side=swap.side, token_mint=swap.token_mint, leader_sol=sol_value,
                      leader_token_amount=swap.token_amount, leader_tx_sig=swap.signature)
            s.add(t)
            await s.flush()
            return t.id

    async def _new_manual_trade(self, side: str, mint: str, origin: str) -> int:
        async with self.db.session() as s:
            t = Trade(side=side, token_mint=mint, status="pending", origin=origin)
            s.add(t)
            await s.flush()
            return t.id

    async def _set_trade_status(self, trade_id: int | None, status: str, note: str = "") -> None:
        if trade_id is None:
            return
        async with self.db.session() as s:
            t = await s.get(Trade, trade_id)
            if t:
                t.status = status
                if note:
                    t.note = note

    async def _finish_trade(self, trade_id: int | None, status: str, sig: str = "", our_sol: float = 0.0,
                            our_tokens: float = 0.0, note: str = "", origin: str = "copy") -> None:
        if trade_id is None:
            return
        async with self.db.session() as s:
            t = await s.get(Trade, trade_id)
            if t:
                t.status, t.copy_tx_sig, t.our_sol, t.our_token_amount, t.origin = status, sig, our_sol, our_tokens, origin
                if note:
                    t.note = note[:500]

    async def _finish_skip(self, trade_id: int, header: str, reason: str, status: str = "skipped") -> None:
        await self._set_trade_status(trade_id, status, reason)
        await self.notify.send(f"{header}\n\n⏭ {esc(reason)}")

    async def _position_add(self, mint: str, qty: float, decimals: int, sol_spent: float) -> None:
        async with self.db.session() as s:
            p = (await s.execute(select(Position).where(Position.token_mint == mint))).scalar_one_or_none()
            if p is None:
                p = Position(token_mint=mint, qty=0.0, cost_basis_sol=0.0, realized_pnl_sol=0.0, decimals=decimals)
                s.add(p)
            if p.qty <= 0:
                p.opened_at = utcnow()
                p.cost_basis_sol = 0.0
            p.qty += qty
            p.decimals = decimals or p.decimals
            p.cost_basis_sol += sol_spent

    async def _position_remove(self, mint: str, sold: float, sol_received: float, qty_before: float,
                               decimals: int) -> float:
        """Reduce a position after a sell; returns realized P/L for this sale."""
        async with self.db.session() as s:
            p = (await s.execute(select(Position).where(Position.token_mint == mint))).scalar_one_or_none()
            if p is None:  # tokens we never bought through the bot - no cost basis
                p = Position(token_mint=mint, qty=qty_before, cost_basis_sol=0.0, realized_pnl_sol=0.0,
                             decimals=decimals)
                s.add(p)
            held = qty_before if qty_before > 0 else p.qty
            frac = min(1.0, sold / held) if held > 0 else 1.0
            cost_out = p.cost_basis_sol * frac
            pnl = sol_received - cost_out
            p.realized_pnl_sol += pnl
            p.cost_basis_sol -= cost_out
            p.qty = max(0.0, held - sold)
            if p.qty <= 1e-12 or frac >= 0.999:
                p.qty, p.cost_basis_sol = 0.0, 0.0
            return pnl

    async def _zero_position(self, mint: str) -> None:
        async with self.db.session() as s:
            p = (await s.execute(select(Position).where(Position.token_mint == mint))).scalar_one_or_none()
            if p:
                p.qty, p.cost_basis_sol = 0.0, 0.0

    async def shutdown(self) -> None:
        for t in list(self._tasks):
            t.cancel()


__all__ = ["CopyEngine", "Decision", "size_buy", "apply_buy_caps", "short"]
