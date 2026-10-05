"""Paper trading: same engine, same leaders, same real Jupiter quotes - fake money.

Enabled with PAPER_TRADING=true. Uses its own database file, so paper results
never mix with real ones. Fills are taken at the quoted `outAmount` (real fills
can be worse by up to your slippage), and no network fees are charged.
"""
from __future__ import annotations

import logging

from solders.keypair import Keypair
from sqlalchemy import select

from bot.config import LAMPORTS_PER_SOL, WSOL_MINT
from bot.db import Database
from bot.jupiter import Jupiter, JupiterError, SwapResult
from bot.models import PaperBalance
from bot.wallet import TokenHolding

log = logging.getLogger("copybot.paper")


class PaperWallet:
    """Drop-in replacement for WalletManager backed by the paper_balances table."""

    def __init__(self, db: Database, start_sol: float):
        self.db = db
        self.start_sol = start_sol
        self._kp = Keypair()  # never funded, never used to sign anything real

    async def ensure_wallet(self) -> tuple[str, bool]:
        async with self.db.session() as s:
            if await s.get(PaperBalance, WSOL_MINT) is None:
                s.add(PaperBalance(mint=WSOL_MINT, raw=int(self.start_sol * LAMPORTS_PER_SOL), decimals=9))
                return "PAPER", True
        return "PAPER", False

    async def reset(self, start_sol: float | None = None) -> None:
        async with self.db.session() as s:
            for row in (await s.execute(select(PaperBalance))).scalars().all():
                await s.delete(row)
            await s.flush()
            s.add(PaperBalance(mint=WSOL_MINT, raw=int((start_sol or self.start_sol) * LAMPORTS_PER_SOL), decimals=9))

    @property
    def keypair(self) -> Keypair:
        return self._kp

    @property
    def pubkey(self) -> str:
        return "PAPER-WALLET"

    async def sol_balance(self) -> float:
        raw, _ = await self.token_balance(WSOL_MINT)
        return raw / LAMPORTS_PER_SOL

    async def token_balance(self, mint: str) -> tuple[int, int]:
        async with self.db.session() as s:
            row = await s.get(PaperBalance, mint)
            return (row.raw, row.decimals) if row else (0, 0)

    async def token_holdings(self) -> list[TokenHolding]:
        async with self.db.session() as s:
            rows = (await s.execute(select(PaperBalance).where(PaperBalance.mint != WSOL_MINT,
                                                               PaperBalance.raw > 0))).scalars().all()
            return [TokenHolding(r.mint, r.raw / 10**r.decimals, r.raw, r.decimals) for r in rows]

    async def adjust(self, mint: str, delta_raw: int, decimals: int) -> None:
        async with self.db.session() as s:
            row = await s.get(PaperBalance, mint)
            if row is None:
                row = PaperBalance(mint=mint, raw=0, decimals=decimals)
                s.add(row)
            row.raw = max(0, row.raw + delta_raw)
            row.decimals = decimals or row.decimals

    def deposit_qr_png(self) -> bytes:
        raise ValueError("Paper trading mode - no real deposits. /paper reset <SOL> to change the fake balance.")

    async def withdraw_sol(self, amount_sol, destination):
        raise ValueError("Paper trading mode - nothing to withdraw.")

    def export_secret(self) -> str:
        raise ValueError("Paper trading mode - there is no real wallet.")


class PaperJupiter(Jupiter):
    """Real quotes and prices; simulated execution against a PaperWallet."""

    def __init__(self, *a, wallet: PaperWallet, **kw):
        super().__init__(*a, **kw)
        self.paper = wallet
        self._n = 0

    async def execute(self, rpc, keypair, input_mint, output_mint, amount_raw, slippage_bps,
                      priority_fee_max_lamports, max_price_impact_pct=100.0) -> SwapResult:
        have, in_dec = await self.paper.token_balance(input_mint)
        if have < amount_raw:
            return SwapResult(False, error=f"paper: insufficient balance ({have} < {amount_raw})")
        try:
            q = await self.quote(input_mint, output_mint, amount_raw, slippage_bps)
        except JupiterError as e:
            return SwapResult(False, error=f"quote: {e}")
        impact = float(q.get("priceImpactPct") or 0) * 100
        if max_price_impact_pct and impact > max_price_impact_pct:
            return SwapResult(False, error=f"price impact {impact:.1f}% > max {max_price_impact_pct:.1f}%",
                              price_impact_pct=impact)
        out = int(q["outAmount"])
        token_mint = output_mint if input_mint == WSOL_MINT else input_mint
        dec = await self._decimals(token_mint, in_dec if input_mint != WSOL_MINT else None)
        out_dec = 9 if output_mint == WSOL_MINT else dec
        await self.paper.adjust(input_mint, -amount_raw, 9 if input_mint == WSOL_MINT else dec)
        await self.paper.adjust(output_mint, out, out_dec)
        self._n += 1
        if input_mint == WSOL_MINT:
            sol, tokens = amount_raw / LAMPORTS_PER_SOL, out / 10**dec
        else:
            sol, tokens = out / LAMPORTS_PER_SOL, amount_raw / 10**dec
        return SwapResult(True, signature=f"PAPER-{self._n}", in_amount_raw=amount_raw, out_amount_raw=out,
                          price_impact_pct=impact, sol_amount=sol, token_amount=tokens, token_decimals=dec,
                          measured=True)

    async def _decimals(self, mint: str, known: int | None) -> int:
        if known:
            return known
        _, dec = await self.paper.token_balance(mint)
        if dec:
            return dec
        info = await self.token_info(mint)
        return int(info.get("decimals") or 6)
