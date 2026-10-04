"""Minimal async Solana JSON-RPC client (httpx).

Plain dict responses keep transaction parsing identical whether a transaction
came from `getTransaction` or from a Helius raw webhook payload.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import logging
from typing import Any

import httpx

log = logging.getLogger("copybot.rpc")

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxPEb"


class RpcError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class SolanaRpc:
    def __init__(self, url: str, timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        self.url = url
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._ids = itertools.count(1)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: list | None = None, retries: int = 3) -> Any:
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = await self._client.post(self.url, json=body)
                if r.status_code == 429 or r.status_code >= 500:
                    raise RpcError(f"HTTP {r.status_code}", r.status_code)
                r.raise_for_status()
                data = r.json()
                if "error" in data and data["error"]:
                    err = data["error"]
                    # Application errors are not retried.
                    raise RpcError(f"{method}: {err.get('message')}", err.get("code"))
                return data.get("result")
            except RpcError as e:
                if e.code is not None and e.code not in (429,) and e.code < 500:
                    raise
                last = e
            except (httpx.HTTPError, ValueError) as e:
                last = e
            await asyncio.sleep(0.3 * (2**attempt))
        raise RpcError(f"{method} failed after {retries} attempts: {last}")

    # ---- reads ----------------------------------------------------------
    async def get_balance(self, pubkey: str) -> int:
        res = await self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return int(res["value"])

    async def get_signatures_for_address(self, address: str, limit: int = 20, until: str | None = None) -> list[dict]:
        opts: dict = {"limit": limit, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        return await self.call("getSignaturesForAddress", [address, opts]) or []

    async def get_transaction(self, signature: str) -> dict | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
        )

    async def get_token_accounts(self, owner: str, mint: str | None = None) -> list[dict]:
        """All SPL + Token-2022 accounts of `owner` (jsonParsed), optionally for one mint."""
        filters = [{"mint": mint}] if mint else [{"programId": TOKEN_PROGRAM}, {"programId": TOKEN_2022_PROGRAM}]
        out: list[dict] = []
        for f in filters:
            res = await self.call(
                "getTokenAccountsByOwner", [owner, f, {"encoding": "jsonParsed", "commitment": "confirmed"}]
            )
            out.extend(res.get("value") or [])
        return out

    async def get_token_balance(self, owner: str, mint: str) -> tuple[int, int]:
        """Return (raw_amount, decimals) summed over owner's accounts for mint."""
        raw, decimals = 0, 0
        for acc in await self.get_token_accounts(owner, mint):
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            raw += int(ta["amount"])
            decimals = int(ta["decimals"])
        return raw, decimals

    async def get_latest_blockhash(self) -> str:
        res = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return res["value"]["blockhash"]

    async def get_signature_status(self, signature: str) -> dict | None:
        res = await self.call("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        return (res.get("value") or [None])[0]

    # ---- writes ---------------------------------------------------------
    async def send_raw_transaction(self, raw: bytes, skip_preflight: bool = True) -> str:
        return await self.call(
            "sendTransaction",
            [
                base64.b64encode(raw).decode(),
                {"encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 0,
                 "preflightCommitment": "confirmed"},
            ],
            retries=1,
        )

    async def send_and_confirm(self, raw: bytes, signature: str, timeout_s: float = 60.0,
                               skip_preflight: bool = True) -> tuple[bool, str]:
        """Send, re-broadcast every 2s until confirmed/failed/timeout.

        Re-sending the same signed bytes is idempotent on Solana.
        Returns (confirmed, error_message).
        """
        await self.send_raw_transaction(raw, skip_preflight=skip_preflight)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last_send = loop.time()
        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            try:
                st = await self.get_signature_status(signature)
            except RpcError as e:
                log.debug("status poll failed: %s", e)
                st = None
            if st:
                if st.get("err"):
                    return False, f"transaction failed on-chain: {st['err']}"
                if st.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True, ""
            if loop.time() - last_send >= 2.0:
                last_send = loop.time()
                try:
                    await self.send_raw_transaction(raw, skip_preflight=True)
                except RpcError as e:
                    log.debug("re-broadcast failed: %s", e)
        return False, f"not confirmed within {int(timeout_s)}s (it may still land - check the explorer)"
