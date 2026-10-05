import pytest
from cryptography.fernet import Fernet

from bot.config import TradingDefaults
from bot.crypto import KeyVault
from bot.db import Database
from bot.engine import CopyEngine
from bot.wallet import WalletManager
from tests.helpers import LEADER, FakeJupiter, FakeNotifier, FakeRpc


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
