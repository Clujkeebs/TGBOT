import asyncio

from fastapi.testclient import TestClient

from bot.config import TradingDefaults
from bot.db import Database
from bot.ingest import LeaderPoller, build_webhook_app
from tests.helpers import LEADER, OTHER, FakeRpc, buy_tx


async def _db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/w.db")
    await db.init(TradingDefaults(), [{"address": LEADER}], [])
    return db


async def test_webhook_auth_and_dispatch(tmp_path):
    db = await _db(tmp_path)
    seen = []

    async def handler(leader, sig):
        seen.append((leader, sig))

    app = build_webhook_app(db, handler, "tok123", "s3cret")
    tx = buy_tx(sig="abc")
    unrelated = buy_tx(sig="zzz", owner=OTHER)

    def call():
        with TestClient(app) as c:
            assert c.post("/webhook/wrong", json=[tx], headers={"Authorization": "s3cret"}).status_code == 404
            assert c.post("/webhook/tok123", json=[tx], headers={"Authorization": "bad"}).status_code == 401
            assert c.post("/webhook/tok123", json=[tx]).status_code == 401
            r = c.post("/webhook/tok123", json=[tx, unrelated], headers={"Authorization": "s3cret"})
            assert r.status_code == 200 and r.json()["queued"] == 1
            assert c.get("/healthz").json() == {"ok": True}

    await asyncio.to_thread(call)
    await asyncio.sleep(0.05)
    assert seen == [(LEADER, "abc")]
    await db.close()


async def test_poller_skips_history_then_picks_up_new(tmp_path):
    db = await _db(tmp_path)
    rpc = FakeRpc()
    rpc.signatures[LEADER] = [{"signature": "old", "err": None}]
    seen = []

    async def handler(leader, sig):
        seen.append(sig)

    p = LeaderPoller(db, rpc, handler, 0.5)
    await p._poll_one(LEADER)
    assert seen == []
    rpc.signatures[LEADER] = [{"signature": "n2", "err": None}, {"signature": "bad", "err": {"x": 1}},
                              {"signature": "n1", "err": None}, {"signature": "old", "err": None}]
    await p._poll_one(LEADER)
    await asyncio.sleep(0.05)
    assert seen == ["n1", "n2"]
    await db.close()


async def test_poller_wallet_without_history(tmp_path):
    db = await _db(tmp_path)
    rpc = FakeRpc()
    seen = []

    async def handler(leader, sig):
        seen.append(sig)

    p = LeaderPoller(db, rpc, handler, 0.5)
    await p._poll_one(LEADER)  # no history at all
    rpc.signatures[LEADER] = [{"signature": "first", "err": None}]
    await p._poll_one(LEADER)
    await asyncio.sleep(0.05)
    assert seen == ["first"]
    await db.close()
