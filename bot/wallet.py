"""Hot-wallet management: creation, encrypted storage, balances, withdrawals."""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import qrcode
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction
from sqlalchemy import select

from bot.config import LAMPORTS_PER_SOL
from bot.crypto import KeyVault
from bot.db import Database
from bot.models import Wallet
from bot.rpc import SolanaRpc

log = logging.getLogger("copybot.wallet")

FEE_RESERVE_SOL = 0.01  # always keep this much for fees/rent


@dataclass
class TokenHolding:
    mint: str
    amount: float
    amount_raw: int
    decimals: int


def is_valid_pubkey(s: str) -> bool:
    try:
        Pubkey.from_string(s.strip())
        return True
    except Exception:  # noqa: BLE001
        return False


class WalletManager:
    def __init__(self, db: Database, rpc: SolanaRpc, vault: KeyVault):
        self.db = db
        self.rpc = rpc
        self.vault = vault
        self._kp: Keypair | None = None

    async def ensure_wallet(self) -> tuple[str, bool]:
        """Load the hot wallet, creating it on first run. Returns (pubkey, created)."""
        async with self.db.session() as s:
            w = (await s.execute(select(Wallet).order_by(Wallet.id))).scalars().first()
            if w is not None:
                self._kp = Keypair.from_bytes(self.vault.decrypt(w.enc_secret))
                if str(self._kp.pubkey()) != w.pubkey:
                    raise RuntimeError("Decrypted wallet does not match its stored public key")
                return w.pubkey, False
            kp = Keypair()
            s.add(Wallet(pubkey=str(kp.pubkey()), enc_secret=self.vault.encrypt(bytes(kp))))
        self._kp = kp
        log.warning("Created new hot wallet %s - fund it with a SMALL amount of SOL.", kp.pubkey())
        return str(kp.pubkey()), True

    async def import_wallet(self, secret_b58: str) -> str:
        """Replace the hot wallet with an imported base58 secret key (Phantom/Solflare export)."""
        kp = Keypair.from_base58_string(secret_b58.strip())
        async with self.db.session() as s:
            for w in (await s.execute(select(Wallet))).scalars().all():
                await s.delete(w)
            await s.flush()
            s.add(Wallet(pubkey=str(kp.pubkey()), enc_secret=self.vault.encrypt(bytes(kp))))
        self._kp = kp
        return str(kp.pubkey())

    @property
    def keypair(self) -> Keypair:
        if self._kp is None:
            raise RuntimeError("wallet not loaded")
        return self._kp

    @property
    def pubkey(self) -> str:
        return str(self.keypair.pubkey())

    def export_secret(self) -> str:
        return str(self.keypair)  # base58 64-byte secret, importable in Phantom/Solflare

    async def sol_balance(self) -> float:
        return await self.rpc.get_balance(self.pubkey) / LAMPORTS_PER_SOL

    async def token_holdings(self) -> list[TokenHolding]:
        by_mint: dict[str, TokenHolding] = {}
        for acc in await self.rpc.get_token_accounts(self.pubkey):
            info = acc["account"]["data"]["parsed"]["info"]
            ta = info["tokenAmount"]
            raw, dec = int(ta["amount"]), int(ta["decimals"])
            if raw <= 0:
                continue
            h = by_mint.get(info["mint"])
            if h:
                h.amount_raw += raw
                h.amount = h.amount_raw / 10**dec
            else:
                by_mint[info["mint"]] = TokenHolding(info["mint"], raw / 10**dec, raw, dec)
        return list(by_mint.values())

    async def token_balance(self, mint: str) -> tuple[int, int]:
        return await self.rpc.get_token_balance(self.pubkey, mint)

    def deposit_qr_png(self) -> bytes:
        img = qrcode.make(f"solana:{self.pubkey}")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    async def withdraw_sol(self, amount_sol: float | None, destination: str) -> tuple[str, float]:
        """Send SOL to `destination`. amount_sol=None withdraws everything minus the fee reserve."""
        dest = Pubkey.from_string(destination.strip())
        bal = await self.sol_balance()
        max_out = max(0.0, bal - FEE_RESERVE_SOL)
        amount = max_out if amount_sol is None else amount_sol
        if amount <= 0:
            raise ValueError(f"Nothing to withdraw (balance {bal:.6f} SOL, {FEE_RESERVE_SOL} SOL kept for fees)")
        if amount > max_out + 1e-12:
            raise ValueError(f"Insufficient balance: have {bal:.6f} SOL, max withdrawable {max_out:.6f} SOL")
        kp = self.keypair
        ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest, lamports=int(amount * LAMPORTS_PER_SOL)))
        blockhash = await self.rpc.get_latest_blockhash()
        msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.from_string(blockhash))
        tx = VersionedTransaction(msg, [kp])
        sig = str(tx.signatures[0])
        ok, err = await self.rpc.send_and_confirm(bytes(tx), sig, skip_preflight=False)
        if not ok:
            raise RuntimeError(f"{err} (sig {sig})")
        log.info("Withdrew %.6f SOL -> %s (%s)", amount, destination, sig)
        return sig, amount
