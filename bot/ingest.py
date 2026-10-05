"""Leader-trade ingestion.

* `LeaderPoller`  - polls getSignaturesForAddress for every active leader.
                    Needs nothing but an RPC URL. Latency ~ poll interval.
* `build_webhook_app` - FastAPI receiver for Helius webhooks (lowest latency).

Both only hand (leader, signature) to the engine. The engine re-fetches the
transaction from OUR RPC, so a forged webhook body can never fabricate a
trade - at worst it makes us look up a real signature.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from bot.crypto import constant_time_eq
from bot.db import Database
from bot.rpc import SolanaRpc

log = logging.getLogger("copybot.ingest")

Handler = Callable[[str, str], Awaitable[None]]

_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _background.add(t)
    t.add_done_callback(_background.discard)


class LeaderPoller:
    PAGE = 25
    MAX_PAGES = 4  # catch up on up to 100 txs per leader per cycle
    ALERT_AFTER_S = 120

    def __init__(self, db: Database, rpc: SolanaRpc, handler: Handler, interval_s: float = 2.0,
                 alert: Callable[[str], Awaitable[None]] | None = None):
        self.db = db
        self.rpc = rpc
        self.handler = handler
        self.interval_s = max(0.5, interval_s)
        self.alert = alert
        self._cursor: dict[str, str] = {}  # leader -> newest signature already seen
        self._failing_since: float | None = None
        self._alerted = False

    async def run(self, stop: asyncio.Event) -> None:
        log.info("Polling leaders every %.1fs", self.interval_s)
        loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                leaders = [ld.address for ld in await self.db.active_leaders()]
                for addr in list(self._cursor):
                    if addr not in leaders:
                        self._cursor.pop(addr)
                results = await asyncio.gather(*(self._poll_one(a) for a in leaders))
                await self._health(bool(leaders) and not any(results), loop.time())
            except Exception:  # noqa: BLE001
                log.exception("poll cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def _health(self, all_failed: bool, now: float) -> None:
        if all_failed:
            if self._failing_since is None:
                self._failing_since = now
            elif not self._alerted and now - self._failing_since >= self.ALERT_AFTER_S:
                self._alerted = True
                if self.alert:
                    await self.alert("🚨 <b>RPC unreachable</b> for 2+ minutes - leader trades are NOT being "
                                     "detected. Check SOLANA_RPC_URL / your RPC provider.")
        else:
            if self._alerted and self.alert:
                await self.alert("✅ RPC is back - watching leaders again.")
            self._failing_since, self._alerted = None, False

    async def _poll_one(self, leader: str) -> bool:
        first_sight = leader not in self._cursor
        until = self._cursor.get(leader) or None
        try:
            sigs = await self.rpc.get_signatures_for_address(leader, limit=1 if first_sight else self.PAGE,
                                                             until=until)
            if first_sight:
                # Start from "now" - don't replay history. "" marks a wallet with no history yet.
                self._cursor[leader] = sigs[0]["signature"] if sigs else ""
                return True
            pages = 1
            while len(sigs) == self.PAGE * pages and pages < self.MAX_PAGES:
                more = await self.rpc.get_signatures_for_address(leader, limit=self.PAGE, until=until,
                                                                 before=sigs[-1]["signature"])
                if not more:
                    break
                sigs += more
                pages += 1
        except Exception as e:  # noqa: BLE001
            log.warning("poll %s failed: %s", leader[:6], e)
            return False
        if not sigs:
            return True
        self._cursor[leader] = sigs[0]["signature"]
        for entry in reversed(sigs):  # oldest first
            if entry.get("err") is not None:
                continue
            _spawn(self._safe(leader, entry["signature"]))
        return True

    async def _safe(self, leader: str, sig: str) -> None:
        try:
            await self.handler(leader, sig)
        except Exception:  # noqa: BLE001
            log.exception("handler failed for %s", sig)


def _leaders_in(tx: dict, leaders: set[str]) -> set[str]:
    """Which tracked leaders appear in a webhook tx item (raw or enhanced format)."""
    found: set[str] = set()
    if tx.get("feePayer") in leaders:  # enhanced format
        found.add(tx["feePayer"])
    for ad in tx.get("accountData") or []:
        if ad.get("account") in leaders:
            found.add(ad["account"])
    msg = (tx.get("transaction") or {}).get("message") or {}  # raw format
    for k in msg.get("accountKeys") or []:
        key = k.get("pubkey") if isinstance(k, dict) else k
        if key in leaders:
            found.add(key)
    return found


def _signature_of(tx: dict) -> str:
    if tx.get("signature"):
        return tx["signature"]
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    return sigs[0] if sigs else ""


def build_webhook_app(db: Database, handler: Handler, path_token: str, secret: str) -> FastAPI:
    app = FastAPI(title="copybot webhook", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/webhook/{token}")
    async def receive(token: str, request: Request, authorization: str | None = Header(default=None)):
        if not constant_time_eq(token, path_token):
            raise HTTPException(status_code=404)
        provided = (authorization or "").removeprefix("Bearer ").strip()
        if not constant_time_eq(provided, secret):
            log.warning("webhook rejected: bad Authorization header")
            raise HTTPException(status_code=401)
        try:
            payload = json.loads(await request.body())
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid json")
        items = payload if isinstance(payload, list) else [payload]
        leaders = {ld.address for ld in await db.active_leaders()}
        queued = 0
        for tx in items:
            if not isinstance(tx, dict):
                continue
            sig = _signature_of(tx)
            if not sig:
                continue
            for leader in _leaders_in(tx, leaders):
                _spawn(handler(leader, sig))
                queued += 1
        return {"ok": True, "queued": queued}

    return app


class HeliusWebhooks:
    """Create/update the Helius webhook so it tracks exactly the active leaders."""

    API = "https://api-mainnet.helius-rpc.com/v0/webhooks"

    def __init__(self, api_key: str, webhook_url: str, secret: str):
        self.api_key = api_key
        self.webhook_url = webhook_url
        self.secret = secret

    async def sync(self, addresses: list[str]) -> str:
        params = {"api-key": self.api_key}
        body = {
            "webhookURL": self.webhook_url,
            "transactionTypes": ["ANY"],
            "accountAddresses": addresses or ["11111111111111111111111111111111"],
            "webhookType": "raw",
            "authHeader": self.secret,
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(self.API, params=params)
            r.raise_for_status()
            existing = [w for w in r.json() if w.get("webhookURL") == self.webhook_url]
            if existing:
                wid = existing[0]["webhookID"]
                r = await c.put(f"{self.API}/{wid}", params=params, json=body)
            else:
                r = await c.post(self.API, params=params, json=body)
            r.raise_for_status()
            wid = r.json().get("webhookID", "")
        log.info("Helius webhook %s now tracks %d leader(s)", wid, len(addresses))
        return wid
