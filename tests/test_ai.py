"""AI manager + multi-leader tests with a scripted fake Claude client."""
import json
from types import SimpleNamespace as NS

import pytest
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from sqlalchemy import select

from bot.ai import FALLBACK_BETA, AIManager
from bot.config import TradingDefaults
from bot.crypto import KeyVault
from bot.db import Database
from bot.engine import CopyEngine
from bot.models import Leader
from bot.wallet import WalletManager
from tests.helpers import LEADER, FakeJupiter, FakeNotifier, FakeRpc, buy_tx, sell_tx

LEADER2 = str(Keypair().pubkey())


def text(t):
    return NS(type="text", text=t)


def tool_use(name, args, id_="t1"):
    return NS(type="tool_use", name=name, input=args, id=id_)


def reply(*blocks, stop="end_turn"):
    return NS(content=list(blocks), stop_reason=stop)


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        item = self.script.pop(0)
        return item(kw) if callable(item) else item


@pytest.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/ai.db")
    await db.init(TradingDefaults(mode="auto", sizing_mode="fixed", sizing_value=0.1, daily_loss_limit_pct=0,
                                  ai_max_trade_sol=0.5),
                  [{"address": LEADER, "label": "whale"}, {"address": LEADER2, "label": "sniper"}], [])
    rpc = FakeRpc()
    wallet = WalletManager(db, rpc, KeyVault(Fernet.generate_key().decode()))
    await wallet.ensure_wallet()
    jup = FakeJupiter(rpc)

    async def token_info(mint):
        return {"id": mint, "symbol": "MEME", "liquidity": 123456.0, "audit": {"mintAuthorityDisabled": False}}

    jup.token_info = token_info
    notifier = FakeNotifier()
    engine = CopyEngine(db, rpc, wallet, jup, notifier)
    yield db, rpc, jup, notifier, engine
    await engine.shutdown()
    await db.close()


def attach(engine, script):
    claude = FakeClaude(script)
    engine.ai = AIManager(claude, "claude-opus-5-5", engine)
    return claude


def verdict(decision, mult=1.0, reason="r"):
    return reply(text(json.dumps({"decision": decision, "size_multiplier": mult, "reason": reason})))


async def leader_row(db, addr):
    async with db.session() as s:
        return (await s.execute(select(Leader).where(Leader.address == addr))).scalar_one()


# ------------------------------------------------------------ buy screening
async def test_screen_reject_blocks_buy(env):
    db, rpc, jup, notifier, engine = env
    claude = attach(engine, [verdict("reject", reason="mint authority enabled")])
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls == []
    assert "mint authority enabled" in notifier.last and "rejected by AI" in notifier.last
    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    ctx = json.loads(call["messages"][0]["content"])
    assert ctx["token"]["symbol"] == "MEME" and ctx["token"]["liquidity"] == 123456.0 and ctx["leader"]["label"] == "whale"


async def test_screen_reduce_scales_size(env):
    db, rpc, jup, notifier, engine = env
    attach(engine, [verdict("reduce", 0.5)])
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls[0][2] == 50_000_000


async def test_screen_failure_falls_back_to_rules(env):
    db, rpc, jup, notifier, engine = env

    def boom(kw):
        raise RuntimeError("api down")

    attach(engine, [boom])
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls[0][2] == 100_000_000
    assert engine.ai.stats["errors"] == 1


async def test_screen_disabled(env):
    db, rpc, jup, notifier, engine = env
    claude = attach(engine, [])
    await db.update_config(ai_screen_buys=False)
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert len(jup.calls) == 1 and claude.calls == []


# ------------------------------------------------------------ multi-leader
async def test_per_leader_mode_max_and_attribution(env):
    db, rpc, jup, notifier, engine = env
    async with db.session() as s:
        (await s.execute(select(Leader).where(Leader.address == LEADER2))).scalar_one().mode = "notify"
        (await s.execute(select(Leader).where(Leader.address == LEADER))).scalar_one().max_sol = 0.05
    rpc.txs["a"] = buy_tx(sig="a")
    rpc.txs["b"] = buy_tx(sig="b", owner=LEADER2)
    await engine.handle_leader_signature(LEADER, "a")
    await engine.handle_leader_signature(LEADER2, "b")
    assert len(jup.calls) == 1 and jup.calls[0][2] == 50_000_000  # whale capped at 0.05, sniper notify-only

    # sniper selling the token doesn't trigger our sell - whale opened the position
    async with db.session() as s:
        (await s.execute(select(Leader).where(Leader.address == LEADER2))).scalar_one().mode = ""
    rpc.txs["s2"] = sell_tx(pre_raw=5_000_000_000, sold_raw=5_000_000_000, sol_got=1, sig="s2", owner=LEADER2)
    await engine.handle_leader_signature(LEADER2, "s2")
    assert len(jup.calls) == 1 and "opened by whale" in notifier.last

    jup.tokens_per_sol = 500.0  # price doubled
    rpc.txs["s1"] = sell_tx(pre_raw=5_000_000_000, sold_raw=5_000_000_000, sol_got=1, sig="s1")
    await engine.handle_leader_signature(LEADER, "s1")
    whale = await leader_row(db, LEADER)
    assert whale.wins == 1 and whale.realized_pnl_sol == pytest.approx(0.05, rel=1e-3)
    assert whale.copied_sol == pytest.approx(0.05)


# ------------------------------------------------------------ chat / review
async def test_chat_runs_tools_and_respects_ceiling(env):
    db, rpc, jup, notifier, engine = env
    claude = attach(engine, [
        reply(tool_use("get_leaders", {}, "a"), stop="tool_use"),
        reply(tool_use("update_leader", {"leader": "sniper", "weight_pct": 50, "active": None, "mode": None,
                                         "max_sol": None, "note": "losing"}, "b"),
              tool_use("update_settings", {"sizing_mode": None, "sizing_value": None, "min_trade_sol": None,
                                           "max_trade_sol": 5.0, "slippage_pct": None, "tp_pct": None,
                                           "sl_pct": None, "daily_loss_limit_pct": None, "copy_sells": None}, "c"),
              stop="tool_use"),
        reply(text("Halved sniper.")),
    ])
    out = await engine.ai.chat("cut sniper in half and raise max to 5")
    assert out == "Halved sniper."
    assert (await leader_row(db, LEADER2)).weight_pct == 50
    assert (await db.get_config()).max_trade_sol == 0.5  # capped by ai_max_trade_sol
    # tool results went back in one user message, with leader data in the first one
    results = claude.calls[1]["messages"][-1]["content"]
    assert results[0]["tool_use_id"] == "a" and "whale" in results[0]["content"]
    assert [r["tool_use_id"] for r in claude.calls[2]["messages"][-1]["content"]] == ["b", "c"]
    assert "AI ceiling" in claude.calls[2]["messages"][-1]["content"][1]["content"]
    assert len(engine.ai._chat) == 6  # append-only history kept for follow-ups


async def test_review_advise_creates_proposal_then_apply(env):
    db, rpc, jup, notifier, engine = env
    attach(engine, [
        reply(tool_use("update_leader", {"leader": "whale", "weight_pct": None, "active": False, "mode": None,
                                         "max_sol": None, "note": "10 straight losses"}), stop="tool_use"),
        reply(text("Whale keeps losing; propose pausing it.")),
    ])
    await engine.ai.review()
    assert (await leader_row(db, LEADER)).is_active  # not applied yet
    assert "Proposed" in notifier.last and "10 straight losses" in notifier.last
    token = notifier.markups[-1].inline_keyboard[0][0].callback_data.split(":")[-1]
    assert await engine.ai.apply_proposal(token, True) == "Applied."
    whale = await leader_row(db, LEADER)
    assert not whale.is_active and whale.ai_note == "10 straight losses"
    assert await engine.ai.apply_proposal(token, True) == "This proposal expired."


async def test_review_manage_applies_and_blocks_chat_only_tools(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(ai_autonomy="manage", paused=True)
    attach(engine, [
        reply(tool_use("update_settings", {"sizing_mode": None, "sizing_value": None, "min_trade_sol": None,
                                           "max_trade_sol": None, "slippage_pct": None, "tp_pct": 80,
                                           "sl_pct": 30, "daily_loss_limit_pct": None, "copy_sells": None}, "a"),
              tool_use("resume_copying", {}, "b"), stop="tool_use"),
        reply(text("Set TP/SL.")),
    ])
    await engine.ai.review()
    cfg = await db.get_config()
    assert (cfg.tp_pct, cfg.sl_pct) == (80, 30)
    assert cfg.paused  # resume is chat-only


async def test_refusal_handled(env):
    db, rpc, jup, notifier, engine = env
    attach(engine, [reply(stop="refusal")])
    assert "declined" in await engine.ai.chat("hi")
