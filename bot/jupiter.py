"""Jupiter Swap API (v1) + Price API (v3) client.

The old `quote-api.jup.ag/v6` and `price/v2` endpoints were retired; this
uses the current `/swap/v1/*` and `/price/v3` routes on either
`api.jup.ag` (with a free API key from portal.jup.ag) or `lite-api.jup.ag`.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from bot.config import WSOL_MINT
from bot.rpc import SolanaRpc
from bot.swap_parser import parse_swaps

log = logging.getLogger("copybot.jupiter")


class JupiterError(RuntimeError):
    pass


@dataclass
class SwapResult:
    ok: bool
    signature: str = ""
    error: str = ""
    in_amount_raw: int = 0
    out_amount_raw: int = 0  # actual if measured on-chain, else quoted
    price_impact_pct: float = 0.0
    sol_amount: float = 0.0  # SOL actually spent (buy) or received (sell)
    token_amount: float = 0.0  # tokens actually received (buy) or sent (sell), ui
    token_decimals: int = 0
    measured: bool = False  # True if amounts come from the confirmed tx


class Jupiter:
    def __init__(self, base_url: str, api_key: str = "", client: httpx.AsyncClient | None = None):
        self.base = base_url.rstrip("/")
        headers = {"x-api-key": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(timeout=15, headers=headers)
        self._price_cache: dict[str, tuple[float, float]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw) -> dict:
        last = ""
        for attempt in range(3):
            try:
                r = await self._client.request(method, f"{self.base}{path}", **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    last = f"HTTP {r.status_code}"
                elif r.status_code >= 400:
                    raise JupiterError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
                else:
                    return r.json()
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
            await asyncio.sleep(0.4 * (2**attempt))
        raise JupiterError(f"{path} failed: {last}")

    # ---- prices ---------------------------------------------------------
    async def prices_usd(self, mints: list[str], max_age_s: float = 10.0) -> dict[str, float]:
        now = time.monotonic()
        out: dict[str, float] = {}
        missing = []
        for m in dict.fromkeys(mints):
            hit = self._price_cache.get(m)
            if hit and now - hit[1] < max_age_s:
                out[m] = hit[0]
            else:
                missing.append(m)
        for i in range(0, len(missing), 50):
            chunk = missing[i : i + 50]
            try:
                data = await self._request("GET", "/price/v3", params={"ids": ",".join(chunk)})
            except JupiterError as e:
                log.warning("price lookup failed: %s", e)
                continue
            for m in chunk:
                p = float(((data or {}).get(m) or {}).get("usdPrice") or 0.0)
                if p > 0:
                    out[m] = p
                    self._price_cache[m] = (p, now)
        return out

    async def sol_price_usd(self) -> float:
        return (await self.prices_usd([WSOL_MINT])).get(WSOL_MINT, 0.0)

    # ---- swaps ----------------------------------------------------------
    async def quote(self, input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int) -> dict:
        params = {
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount_raw)),
            "slippageBps": int(slippage_bps), "swapMode": "ExactIn", "restrictIntermediateTokens": "true",
        }
        q = await self._request("GET", "/swap/v1/quote", params=params)
        if not q or "outAmount" not in q:
            raise JupiterError(f"no route: {str(q)[:200]}")
        return q

    async def swap_transaction(self, quote: dict, user_pubkey: str, priority_fee_max_lamports: int) -> bytes:
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": int(priority_fee_max_lamports), "priorityLevel": "veryHigh",
                }
            },
        }
        res = await self._request("POST", "/swap/v1/swap", json=body)
        if not res.get("swapTransaction"):
            raise JupiterError(f"swap build failed: {str(res)[:200]}")
        return base64.b64decode(res["swapTransaction"])

    async def execute(self, rpc: SolanaRpc, keypair: Keypair, input_mint: str, output_mint: str,
                      amount_raw: int, slippage_bps: int, priority_fee_max_lamports: int,
                      max_price_impact_pct: float = 100.0) -> SwapResult:
        """Quote -> build -> sign -> send -> confirm -> measure actual amounts."""
        try:
            q = await self.quote(input_mint, output_mint, amount_raw, slippage_bps)
        except JupiterError as e:
            return SwapResult(False, error=f"quote: {e}")
        impact = float(q.get("priceImpactPct") or 0) * 100  # API returns a fraction
        if max_price_impact_pct and impact > max_price_impact_pct:
            return SwapResult(False, error=f"price impact {impact:.1f}% > max {max_price_impact_pct:.1f}%",
                              price_impact_pct=impact)
        user = str(keypair.pubkey())
        try:
            raw_tx = await self.swap_transaction(q, user, priority_fee_max_lamports)
        except JupiterError as e:
            return SwapResult(False, error=f"build: {e}", price_impact_pct=impact)

        unsigned = VersionedTransaction.from_bytes(raw_tx)
        signed = VersionedTransaction(unsigned.message, [keypair])
        sig = str(signed.signatures[0])
        try:
            ok, err = await rpc.send_and_confirm(bytes(signed), sig)
        except Exception as e:  # noqa: BLE001
            return SwapResult(False, signature=sig, error=f"send: {e}", price_impact_pct=impact)

        result = SwapResult(ok, signature=sig, error=err, in_amount_raw=int(q["inAmount"]),
                            out_amount_raw=int(q["outAmount"]), price_impact_pct=impact)
        if ok:
            await self._measure(rpc, result, user, input_mint, output_mint)
        return result

    async def _measure(self, rpc: SolanaRpc, result: SwapResult, user: str, input_mint: str, output_mint: str):
        token_mint = output_mint if input_mint == WSOL_MINT else input_mint
        for _ in range(5):
            try:
                tx = await rpc.get_transaction(result.signature)
            except Exception as e:  # noqa: BLE001
                log.debug("measure: %s", e)
                tx = None
            if tx:
                for s in parse_swaps(tx, user):
                    if s.token_mint == token_mint:
                        result.sol_amount = s.sol_amount
                        result.token_amount = s.token_amount
                        result.token_decimals = s.token_decimals
                        result.measured = True
                        if input_mint == WSOL_MINT:
                            result.out_amount_raw = s.token_amount_raw
                        return
                return
            await asyncio.sleep(1.0)
