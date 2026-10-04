import asyncio

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from bot.config import TradingDefaults, WSOL_MINT
from bot.crypto import KeyVault
from bot.db import Database
from bot.engine import CopyEngine, apply_buy_caps, size_buy
from bot.models import BotConfig, Position, Trade
from bot.wallet import WalletManager
from tests.helpers import LEADER, TOKEN, FakeJupiter, FakeNotifier, FakeRpc, buy_tx, sell_tx


def cfg(**kw) -> BotConfig:
    base = TradingDefaults().model_dump()
    base.update(kw)
    return BotConfig(**base)


# ---------------------------------------------------------------- sizing
def test_size_fixed():
    d = size_buy(cfg(sizing_mode="fixed", sizing_value=0.1), 3.0, 10.0, 2.0)
    assert d.ok and d.sol_amount == pytest.approx(0.1)


def test_size_percent():
    d = size_buy(cfg(sizing_mode="percent", sizing_value=10), 3.0, 10.0, 2.0)
    assert d.sol_amount == pytest.approx(0.2)


def test_size_mirror_uses_sol_ratio_and_multiplier():
    # leader spent 1 of 10 SOL (10%) -> we spend 10% of 2 SOL, x200% = 0.4
    d = size_buy(cfg(sizing_mode="mirror", sizing_value=200), 1.0, 10.0, 2.0)
    assert d.sol_amount == pytest.approx(0.4)


def test_size_mirror_unknown_balance():
    assert not size_buy(cfg(sizing_mode="mirror", sizing_value=100), 1.0, 0.0, 2.0).ok


def test_leader_weight():
    d = size_buy(cfg(sizing_mode="fixed", sizing_value=0.1), 1, 1, 1, weight_pct=50)
    assert d.sol_amount == pytest.approx(0.05)


def test_caps():
    c = cfg(min_trade_sol=0.01, max_trade_sol=0.5)
    assert apply_buy_caps(c, 2.0, 10.0).sol_amount == 0.5
    assert not apply_buy_caps(c, 0.001, 10.0).ok
    assert apply_buy_caps(c, 0.3, 0.1).sol_amount == pytest.approx(0.08)  # keeps 0.02 fee reserve
    assert not apply_buy_caps(c, 0.3, 0.025).ok


# ---------------------------------------------------------------- engine
@pytest.fixture
async def env(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await db.init(TradingDefaults(mode="auto", sizing_mode="fixed", sizing_value=0.1, daily_loss_limit_pct=0),
                  [{"address": LEADER, "label": "whale"}], [])
    rpc = FakeRpc()
    wallet = WalletManager(db, rpc, KeyVault(Fernet.generate_key().decode()))
    await wallet.ensure_wallet()
    jup = FakeJupiter(rpc)
    notifier = FakeNotifier()
    engine = CopyEngine(db, rpc, wallet, jup, notifier)
    yield db, rpc, jup, notifier, engine
    await engine.shutdown()
    await db.close()


async def trades(db):
    async with db.session() as s:
        return list((await s.execute(select(Trade).order_by(Trade.id))).scalars().all())


async def position(db, mint=TOKEN):
    async with db.session() as s:
        return (await s.execute(select(Position).where(Position.token_mint == mint))).scalar_one_or_none()


async def test_auto_buy_then_partial_and_full_sell(env):
    db, rpc, jup, notifier, engine = env
    rpc.txs["buy1"] = buy_tx(sig="buy1")
    await engine.handle_leader_signature(LEADER, "buy1")
    assert jup.calls == [(WSOL_MINT, TOKEN, 100_000_000)]
    p = await position(db)
    assert p.qty == pytest.approx(100.0) and p.cost_basis_sol == pytest.approx(0.1)
    assert "Bought" in notifier.last

    # leader sells 50% -> we sell 50%
    rpc.txs["sell1"] = sell_tx(pre_raw=5_000_000_000, sold_raw=2_500_000_000, sol_got=1.0, sig="sell1")
    await engine.handle_leader_signature(LEADER, "sell1")
    assert jup.calls[-1] == (TOKEN, WSOL_MINT, 50_000_000)
    p = await position(db)
    assert p.qty == pytest.approx(50.0) and p.cost_basis_sol == pytest.approx(0.05)

    # leader dumps the rest -> we exit fully
    rpc.txs["sell2"] = sell_tx(pre_raw=2_500_000_000, sold_raw=2_500_000_000, sol_got=1.0, sig="sell2")
    await engine.handle_leader_signature(LEADER, "sell2")
    assert rpc.token_raw[TOKEN][0] == 0
    p = await position(db)
    assert p.qty == 0 and p.realized_pnl_sol == pytest.approx(0.0, abs=1e-6)
    assert [t.status for t in await trades(db)] == ["executed"] * 3


async def test_duplicate_signature_processed_once(env):
    db, rpc, jup, notifier, engine = env
    rpc.txs["buy1"] = buy_tx(sig="buy1")
    await asyncio.gather(engine.handle_leader_signature(LEADER, "buy1"),
                         engine.handle_leader_signature(LEADER, "buy1"))
    assert len(jup.calls) == 1


async def test_notify_mode_does_not_trade(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(mode="notify")
    rpc.txs["buy1"] = buy_tx(sig="buy1")
    await engine.handle_leader_signature(LEADER, "buy1")
    assert jup.calls == []
    assert (await trades(db))[0].status == "seen"


async def test_paused_and_blacklist(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(paused=True)
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls == [] and "paused" in notifier.last

    await db.update_config(paused=False)
    from bot.models import Blacklist
    async with db.session() as s:
        s.add(Blacklist(token_mint=TOKEN))
    rpc.txs["b2"] = buy_tx(sig="b2")
    await engine.handle_leader_signature(LEADER, "b2")
    assert jup.calls == [] and "blacklisted" in notifier.last


async def test_sell_skipped_when_not_holding(env):
    db, rpc, jup, notifier, engine = env
    rpc.txs["s1"] = sell_tx(pre_raw=10, sold_raw=5, sol_got=0.1, sig="s1")
    await engine.handle_leader_signature(LEADER, "s1")
    assert jup.calls == [] and "don't hold" in notifier.last


async def test_insufficient_balance_skips(env):
    db, rpc, jup, notifier, engine = env
    rpc.sol_lamports = 10_000_000  # 0.01 SOL
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls == [] and "insufficient" in notifier.last


async def test_failed_swap_recorded(env):
    db, rpc, jup, notifier, engine = env
    jup.fail = "quote: no route"
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    t = (await trades(db))[0]
    assert t.status == "failed" and "no route" in t.note
    assert "Buy failed" in notifier.last


async def test_confirm_mode_approve_and_skip(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(mode="confirm")
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls == []
    kb = notifier.markups[-1]
    pid = int(kb.inline_keyboard[0][0].callback_data.split(":")[-1])
    assert await engine.resolve_confirmation(pid, True) == "Executing…"
    await asyncio.sleep(0.05)
    assert len(jup.calls) == 1
    assert await engine.resolve_confirmation(pid, True) == "Already handled or expired."

    rpc.txs["b2"] = buy_tx(sig="b2")
    await engine.handle_leader_signature(LEADER, "b2")
    pid2 = int(notifier.markups[-1].inline_keyboard[0][1].callback_data.split(":")[-1])
    assert await engine.resolve_confirmation(pid2, False) == "Skipped."
    assert len(jup.calls) == 1


async def test_confirm_timeout(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(mode="confirm", confirm_timeout_s=5)
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    pid = int(notifier.markups[-1].inline_keyboard[0][0].callback_data.split(":")[-1])
    # Simulate the timer firing immediately
    engine._timeouts.pop(pid).cancel()
    await engine._confirm_timeout(pid, 0, "x")
    assert "Timed out" in notifier.last
    assert (await trades(db))[0].status == "timeout"


async def test_daily_loss_breaker_pauses(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(daily_loss_limit_pct=10)
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")  # sets baseline (2 SOL), buys
    assert len(jup.calls) == 1
    rpc.sol_lamports = 1_000_000_000  # portfolio crashes
    jup.prices[TOKEN] = 0.0
    engine._portfolio_cache = None
    rpc.txs["b2"] = buy_tx(sig="b2")
    await engine.handle_leader_signature(LEADER, "b2")
    assert len(jup.calls) == 1
    assert (await db.get_config()).paused
    assert any("Daily loss limit" in m for m in notifier.messages)


async def test_take_profit(env):
    db, rpc, jup, notifier, engine = env
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    await db.update_config(tp_pct=50)
    jup.tokens_per_sol = 500.0  # token doubled in price
    await engine._scan_tp_sl(await db.get_config())
    assert rpc.token_raw[TOKEN][0] == 0
    p = await position(db)
    assert p.realized_pnl_sol == pytest.approx(0.1, rel=1e-3)
    assert any("Take-profit" in m for m in notifier.messages)


async def test_mirror_sizing_end_to_end(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(sizing_mode="mirror", sizing_value=100, max_trade_sol=10)
    # leader spends 2 of 10 SOL (20%); we hold 2 SOL -> 0.4 SOL
    rpc.txs["b1"] = buy_tx(sol_spent=2.0, pre_sol=10.0, sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls[0][2] == pytest.approx(400_000_000, rel=1e-4)
