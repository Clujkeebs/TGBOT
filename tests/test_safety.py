"""Token filters, exposure limits, paper trading, restart cleanup."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from bot.config import WSOL_MINT, TradingDefaults
from bot.db import Database
from bot.engine import CopyEngine
from bot.filters import check_token
from bot.models import BotConfig, PendingConfirmation, Position, Trade
from bot.paper import PaperJupiter, PaperWallet
from tests.helpers import LEADER, TOKEN, TOKEN2, FakeNotifier, FakeRpc, buy_tx


def cfg(**kw) -> BotConfig:
    base = TradingDefaults().model_dump()
    base.update(kw)
    return BotConfig(**base)


SAFE_MINT = {"mintAuthority": None, "freezeAuthority": None}


# ---------------------------------------------------------------- filters
def test_authorities_block():
    assert check_token(cfg(), {"mintAuthority": "Dev", "freezeAuthority": None}, {})[1].startswith("mint authority")
    assert "freeze" in check_token(cfg(), {"mintAuthority": None, "freezeAuthority": "Dev"}, {})[1]
    assert check_token(cfg(require_mint_disabled=False), {"mintAuthority": "Dev", "freezeAuthority": None},
                       {"liquidity": 1e6})[0]
    assert not check_token(cfg(), {}, {"liquidity": 1e6})[0]  # can't verify -> block


def test_liquidity_holders_age():
    assert not check_token(cfg(min_liquidity_usd=5000), SAFE_MINT, {"liquidity": 100})[0]
    ok, note = check_token(cfg(min_liquidity_usd=5000), SAFE_MINT, {})
    assert ok and "unknown" in note  # missing Jupiter data never blocks
    assert not check_token(cfg(max_top_holders_pct=50), SAFE_MINT,
                           {"liquidity": 1e6, "audit": {"topHoldersPercentage": 80}})[0]
    young = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    assert not check_token(cfg(min_token_age_min=5), SAFE_MINT, {"liquidity": 1e6, "firstPool": {"createdAt": young}})[0]
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    assert check_token(cfg(min_token_age_min=5), SAFE_MINT, {"liquidity": 1e6, "firstPool": {"createdAt": old}})[0]


async def test_filter_blocks_copy(env):
    db, rpc, jup, notifier, engine = env
    rpc.mints[TOKEN] = {"mintAuthority": "DevWallet", "freezeAuthority": None}
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert jup.calls == [] and "mint authority" in notifier.last


# ---------------------------------------------------------------- exposure
async def test_max_open_positions_and_token_exposure(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(max_open_positions=1, max_token_exposure_sol=0.15)
    rpc.txs["a"] = buy_tx(sig="a")
    await engine.handle_leader_signature(LEADER, "a")
    assert len(jup.calls) == 1
    rpc.txs["b"] = buy_tx(sig="b", mint=TOKEN2)
    await engine.handle_leader_signature(LEADER, "b")
    assert len(jup.calls) == 1 and "max open positions" in notifier.last
    # adding to the same token is allowed, but only up to the per-token cap (0.15 - 0.1 = 0.05)
    rpc.txs["c"] = buy_tx(sig="c")
    await engine.handle_leader_signature(LEADER, "c")
    assert jup.calls[-1][2] == 50_000_000
    rpc.txs["d"] = buy_tx(sig="d")
    await engine.handle_leader_signature(LEADER, "d")
    assert len(jup.calls) == 2 and "already" in notifier.last


# ---------------------------------------------------------------- restart
async def test_stale_confirmations_expired(env):
    db, rpc, jup, notifier, engine = env
    await db.update_config(mode="confirm")
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    for t in list(engine._timeouts.values()):
        t.cancel()
    assert await engine.expire_stale_confirmations() == 1
    async with db.session() as s:
        assert all(pc.resolved for pc in (await s.execute(select(PendingConfirmation))).scalars())
        assert (await s.execute(select(Trade))).scalar_one().status == "timeout"
    assert "restarted" in notifier.last


# ---------------------------------------------------------------- paper
@pytest.fixture
async def paper(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/paper.db")
    await db.init(TradingDefaults(mode="auto", sizing_mode="fixed", sizing_value=1.0, max_trade_sol=5,
                                  daily_loss_limit_pct=0, min_liquidity_usd=0), [{"address": LEADER}], [])
    wallet = PaperWallet(db, 10.0)
    await wallet.ensure_wallet()
    rate = {"tokens_per_sol": 1000.0}

    def handler(req: httpx.Request):
        p = req.url.params
        amt = int(p.get("amount", 0))
        if req.url.path == "/swap/v1/quote":
            if p["inputMint"] == WSOL_MINT:
                out = int(amt / 1e9 * rate["tokens_per_sol"] * 1e6)
            else:
                out = int(amt / 1e6 / rate["tokens_per_sol"] * 1e9)
            return httpx.Response(200, json={"inAmount": str(amt), "outAmount": str(out), "priceImpactPct": "0"})
        if req.url.path == "/tokens/v2/search":
            return httpx.Response(200, json=[{"id": p["query"], "decimals": 6, "liquidity": 1e6}])
        if req.url.path == "/price/v3":
            return httpx.Response(200, json={WSOL_MINT: {"usdPrice": 150.0}})
        return httpx.Response(404)

    jup = PaperJupiter("https://jup.test", wallet=wallet,
                       client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rpc = FakeRpc()
    notifier = FakeNotifier()
    engine = CopyEngine(db, rpc, wallet, jup, notifier)
    yield db, rpc, wallet, rate, notifier, engine
    await engine.shutdown()
    await db.close()


async def test_paper_round_trip(paper):
    db, rpc, wallet, rate, notifier, engine = paper
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert await wallet.sol_balance() == pytest.approx(9.0)
    assert (await wallet.token_balance(TOKEN))[0] == 1_000_000_000  # 1000 tokens, 6 decimals
    assert "paper" in notifier.last and "solscan.io/tx/PAPER" not in notifier.last

    rate["tokens_per_sol"] = 500.0  # token doubles
    from tests.helpers import sell_tx
    rpc.txs["s1"] = sell_tx(pre_raw=5_000_000_000, sold_raw=5_000_000_000, sol_got=1, sig="s1")
    await engine.handle_leader_signature(LEADER, "s1")
    assert await wallet.sol_balance() == pytest.approx(11.0)
    async with db.session() as s:
        p = (await s.execute(select(Position))).scalar_one()
    assert p.realized_pnl_sol == pytest.approx(1.0) and p.qty == 0

    with pytest.raises(ValueError):
        await wallet.withdraw_sol(1, "x")
    await wallet.reset(3)
    assert await wallet.sol_balance() == 3.0 and await wallet.token_holdings() == []


async def test_paper_insufficient_balance(paper):
    db, rpc, wallet, rate, notifier, engine = paper
    await wallet.reset(0.02)
    rpc.txs["b1"] = buy_tx(sig="b1")
    await engine.handle_leader_signature(LEADER, "b1")
    assert "insufficient SOL" in notifier.last
