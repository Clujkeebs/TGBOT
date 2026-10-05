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


async def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE leaders (id INTEGER PRIMARY KEY, address VARCHAR(64) UNIQUE, label VARCHAR(64), "
                "is_active BOOLEAN, weight_pct FLOAT, trades_seen INTEGER, buys INTEGER, sells INTEGER, "
                "last_trade_at DATETIME, created_at DATETIME)")
    con.execute("INSERT INTO leaders (address, label, is_active, weight_pct, trades_seen, buys, sells) "
                f"VALUES ('{LEADER}', 'old', 1, 100, 0, 0, 0)")
    con.commit()
    con.close()
    db = Database(f"sqlite+aiosqlite:///{path}")
    await db.init(TradingDefaults(), [], [])
    [ld] = await db.active_leaders()
    assert ld.mode == "" and ld.max_sol == 0 and ld.realized_pnl_sol == 0
    await db.close()


async def test_poller_pages_through_bursts(tmp_path):
    db = await _db(tmp_path)
    rpc = FakeRpc()
    rpc.signatures[LEADER] = [{"signature": "old", "err": None}]
    seen = []

    async def handler(leader, sig):
        seen.append(sig)

    p = LeaderPoller(db, rpc, handler, 0.5)
    await p._poll_one(LEADER)
    burst = [{"signature": f"s{i}", "err": None} for i in range(60, 0, -1)]  # newest first
    rpc.signatures[LEADER] = burst + [{"signature": "old", "err": None}]
    await p._poll_one(LEADER)
    await asyncio.sleep(0.05)
    assert seen == [f"s{i}" for i in range(1, 61)]
    await db.close()


async def test_poller_health_alert(tmp_path):
    db = await _db(tmp_path)
    alerts = []

    async def alert(text):
        alerts.append(text)

    p = LeaderPoller(db, FakeRpc(), None, 0.5, alert=alert)
    await p._health(True, 0)
    await p._health(True, 60)
    assert alerts == []
    await p._health(True, 130)
    await p._health(True, 200)
    assert len(alerts) == 1 and "RPC unreachable" in alerts[0]
    await p._health(False, 210)
    assert len(alerts) == 2 and "back" in alerts[1]
    await db.close()
