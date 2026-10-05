"""Builders for synthetic Solana transactions + fakes for RPC/Jupiter/Telegram."""
from __future__ import annotations

from solders.keypair import Keypair

from bot.config import WSOL_MINT
from bot.jupiter import SwapResult

LEADER = str(Keypair().pubkey())
OTHER = str(Keypair().pubkey())
POOL = str(Keypair().pubkey())
TOKEN = str(Keypair().pubkey())
TOKEN2 = str(Keypair().pubkey())


def tb(idx: int, mint: str, owner: str, raw: int, dec: int = 6) -> dict:
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"amount": str(raw), "decimals": dec, "uiAmount": raw / 10**dec}}


def make_tx(signer: str = LEADER, pre_sol: float = 10.0, post_sol: float = 9.0, fee: int = 5000,
            pre_tokens=(), post_tokens=(), err=None, sig: str = "sig1", parsed_keys: bool = True,
            extra_keys=(POOL,)) -> dict:
    keys = [signer, *extra_keys]
    if parsed_keys:
        account_keys = [{"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate(keys)]
    else:
        account_keys = keys
    pre = [int(pre_sol * 1e9)] + [1_000_000_000] * len(extra_keys)
    post = [int(post_sol * 1e9) - fee] + [1_000_000_000] * len(extra_keys)
    return {
        "slot": 1, "blockTime": 1700000000,
        "meta": {"err": err, "fee": fee, "preBalances": pre, "postBalances": post,
                 "preTokenBalances": list(pre_tokens), "postTokenBalances": list(post_tokens),
                 "loadedAddresses": {"writable": [], "readonly": []}},
        "transaction": {"signatures": [sig], "message": {"accountKeys": account_keys,
                                                          "header": {"numRequiredSignatures": 1}}},
    }


def buy_tx(sol_spent: float = 1.0, tokens_raw: int = 5_000_000_000, pre_sol: float = 10.0, sig: str = "buy1",
           new_account: bool = True, owner: str = LEADER, mint: str = TOKEN) -> dict:
    rent = 0.00203928 if new_account else 0.0
    pre_tokens = [] if new_account else [tb(2, mint, owner, 0)]
    return make_tx(signer=owner, pre_sol=pre_sol, post_sol=pre_sol - sol_spent - rent, sig=sig,
                   pre_tokens=pre_tokens, post_tokens=[tb(2, mint, owner, tokens_raw)])


def sell_tx(pre_raw: int, sold_raw: int, sol_got: float, sig: str = "sell1", owner: str = LEADER,
            mint: str = TOKEN) -> dict:
    return make_tx(signer=owner, pre_sol=5.0, post_sol=5.0 + sol_got, sig=sig,
                   pre_tokens=[tb(2, mint, owner, pre_raw)], post_tokens=[tb(2, mint, owner, pre_raw - sold_raw)])


class FakeRpc:
    def __init__(self):
        self.txs: dict[str, dict] = {}
        self.sol_lamports = 2_000_000_000
        self.token_raw: dict[str, tuple[int, int]] = {}
        self.signatures: dict[str, list[dict]] = {}
        self.mints: dict[str, dict] = {}

    async def get_mint_info(self, mint):
        return self.mints.get(mint, {"mintAuthority": None, "freezeAuthority": None, "decimals": 6})

    async def get_transaction(self, sig):
        return self.txs.get(sig)

    async def get_balance(self, pubkey):
        return self.sol_lamports

    async def get_token_balance(self, owner, mint):
        return self.token_raw.get(mint, (0, 6))

    async def get_token_accounts(self, owner, mint=None):
        out = []
        for m, (raw, dec) in self.token_raw.items():
            if mint and m != mint:
                continue
            out.append({"account": {"data": {"parsed": {"info": {
                "mint": m, "tokenAmount": {"amount": str(raw), "decimals": dec}}}}}})
        return out

    async def get_signatures_for_address(self, address, limit=20, until=None, before=None):
        sigs = self.signatures.get(address, [])
        if before:
            idx = [s["signature"] for s in sigs].index(before)
            sigs = sigs[idx + 1:]
        out = []
        for s in sigs:
            if s["signature"] == until:
                break
            out.append(s)
        return out[:limit]


class FakeJupiter:
    """Simulates fills at a fixed price: 1 SOL = `tokens_per_sol` tokens (6 decimals)."""

    def __init__(self, rpc: FakeRpc, tokens_per_sol: float = 1000.0, fail: str = ""):
        self.rpc = rpc
        self.tokens_per_sol = tokens_per_sol
        self.fail = fail
        self.calls: list[tuple] = []
        self.prices = {WSOL_MINT: 150.0}
        self.tokens: dict[str, dict] = {}

    async def token_info(self, mint):
        return self.tokens.get(mint, {"liquidity": 250_000.0})

    async def prices_usd(self, mints, max_age_s=10.0):
        return {m: self.prices[m] for m in mints if m in self.prices}

    async def sol_price_usd(self):
        return self.prices[WSOL_MINT]

    async def quote(self, input_mint, output_mint, amount_raw, slippage_bps):
        if input_mint == WSOL_MINT:
            out = int(amount_raw / 1e9 * self.tokens_per_sol * 1e6)
        else:
            out = int(amount_raw / 1e6 / self.tokens_per_sol * 1e9)
        return {"inAmount": str(amount_raw), "outAmount": str(out), "priceImpactPct": "0.001"}

    async def execute(self, rpc, keypair, input_mint, output_mint, amount_raw, slippage_bps,
                      priority_fee_max_lamports, max_price_impact_pct=100.0):
        self.calls.append((input_mint, output_mint, amount_raw))
        if self.fail:
            return SwapResult(False, error=self.fail)
        q = await self.quote(input_mint, output_mint, amount_raw, slippage_bps)
        out = int(q["outAmount"])
        if input_mint == WSOL_MINT:
            self.rpc.sol_lamports -= amount_raw
            raw, dec = self.rpc.token_raw.get(output_mint, (0, 6))
            self.rpc.token_raw[output_mint] = (raw + out, 6)
            return SwapResult(True, signature=f"our{len(self.calls)}", in_amount_raw=amount_raw, out_amount_raw=out,
                              sol_amount=amount_raw / 1e9, token_amount=out / 1e6, token_decimals=6, measured=True)
        raw, dec = self.rpc.token_raw[input_mint]
        self.rpc.token_raw[input_mint] = (raw - amount_raw, dec)
        self.rpc.sol_lamports += out
        return SwapResult(True, signature=f"our{len(self.calls)}", in_amount_raw=amount_raw, out_amount_raw=out,
                          sol_amount=out / 1e9, token_amount=amount_raw / 1e6, token_decimals=6, measured=True)


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []
        self.markups: list = []
        self._id = 0

    async def send(self, text, reply_markup=None):
        self._id += 1
        self.messages.append(text)
        self.markups.append(reply_markup)
        return self._id

    async def edit(self, message_id, text):
        self.messages.append(text)

    @property
    def last(self) -> str:
        return self.messages[-1] if self.messages else ""
