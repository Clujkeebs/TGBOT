"""Exercise the real Jupiter + RPC clients against mocked HTTP endpoints."""
import base64
import json

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from bot.config import WSOL_MINT
from bot.jupiter import Jupiter
from bot.rpc import SolanaRpc
from tests.helpers import TOKEN, buy_tx


def unsigned_swap_tx(kp: Keypair) -> str:
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    msg = MessageV0.try_compile(kp.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction(msg, [kp])  # Jupiter returns it with an empty signature; any bytes are fine here
    return base64.b64encode(bytes(tx)).decode()


async def test_execute_quote_sign_send_confirm_measure():
    kp = Keypair()
    user = str(kp.pubkey())
    sent = []

    def jup_handler(req: httpx.Request):
        if req.url.path == "/swap/v1/quote":
            assert req.url.params["inputMint"] == WSOL_MINT
            return httpx.Response(200, json={"inAmount": "100000000", "outAmount": "500000000",
                                             "priceImpactPct": "0.01"})
        if req.url.path == "/swap/v1/swap":
            body = json.loads(req.content)
            assert body["userPublicKey"] == user and body["dynamicComputeUnitLimit"] is True
            return httpx.Response(200, json={"swapTransaction": unsigned_swap_tx(kp)})
        if req.url.path == "/price/v3":
            return httpx.Response(200, json={WSOL_MINT: {"usdPrice": 150.5}})
        return httpx.Response(404)

    def rpc_handler(req: httpx.Request):
        body = json.loads(req.content)
        m = body["method"]
        if m == "sendTransaction":
            raw = base64.b64decode(body["params"][0])
            tx = VersionedTransaction.from_bytes(raw)
            sent.append(str(tx.signatures[0]))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": sent[-1]})
        if m == "getSignatureStatuses":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "value": [{"confirmationStatus": "confirmed", "err": None}]}})
        if m == "getTransaction":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": buy_tx(sol_spent=0.1, tokens_raw=480_000_000, owner=user)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": m}})

    jup = Jupiter("https://jup.test", client=httpx.AsyncClient(transport=httpx.MockTransport(jup_handler)))
    rpc = SolanaRpc("https://rpc.test", client=httpx.AsyncClient(transport=httpx.MockTransport(rpc_handler)))

    assert await jup.sol_price_usd() == 150.5
    res = await jup.execute(rpc, kp, WSOL_MINT, TOKEN, 100_000_000, 500, 1_000_000, max_price_impact_pct=15)
    assert res.ok, res.error
    assert res.signature == sent[0]
    assert res.measured and res.token_amount == 480.0  # actual fill, not the 500 quoted
    assert abs(res.sol_amount - 0.1) < 1e-6
    assert abs(res.price_impact_pct - 1.0) < 1e-9


async def test_price_impact_gate():
    def h(req):
        return httpx.Response(200, json={"inAmount": "1", "outAmount": "1", "priceImpactPct": "0.4"})

    jup = Jupiter("https://jup.test", client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    res = await jup.execute(None, Keypair(), WSOL_MINT, TOKEN, 1, 500, 0, max_price_impact_pct=15)
    assert not res.ok and "price impact 40.0%" in res.error


async def test_rpc_application_error_not_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}})

    rpc = SolanaRpc("https://rpc.test", client=httpx.AsyncClient(transport=httpx.MockTransport(h)))
    try:
        await rpc.get_balance("x")
    except Exception as e:  # noqa: BLE001
        assert "bad" in str(e)
    assert len(calls) == 1
