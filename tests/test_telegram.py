"""Drive the aiogram dispatcher with fake updates and a fake Telegram API session."""
from datetime import datetime

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from cryptography.fernet import Fernet

from bot.config import TradingDefaults
from bot.crypto import KeyVault
from bot.db import Database
from bot.engine import CopyEngine
from bot.telegram_ui import TelegramUI
from bot.wallet import WalletManager
from tests.helpers import LEADER, OTHER, TOKEN, FakeJupiter, FakeNotifier, FakeRpc

OWNER = 111
STRANGER = 999


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendPhoto", "EditMessageText"):
            return Message(message_id=len(self.sent), date=datetime.now(), chat=Chat(id=OWNER, type="private"),
                           text=getattr(method, "text", None) or "")
        return True

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def close(self):
        pass

    def texts(self) -> list[str]:
        return [getattr(m, "text", None) or getattr(m, "caption", None) or "" for m in self.sent]


@pytest.fixture
async def ui(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/tg.db")
    await db.init(TradingDefaults(), [], [])
    rpc = FakeRpc()
    wallet = WalletManager(db, rpc, KeyVault(Fernet.generate_key().decode()))
    await wallet.ensure_wallet()
    session = FakeSession()
    bot = Bot("123:abc", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    engine = CopyEngine(db, rpc, wallet, FakeJupiter(rpc), FakeNotifier())
    synced = []

    async def on_change():
        synced.append(1)

    ui = TelegramUI(bot, engine, wallet, {OWNER}, on_change)
    ui._synced = synced
    yield ui, session, db, rpc
    await engine.shutdown()
    await db.close()


_uid = 0


def msg(text: str, user: int = OWNER) -> Update:
    global _uid
    _uid += 1
    return Update(update_id=_uid, message=Message(
        message_id=_uid, date=datetime.now(), chat=Chat(id=user, type="private"),
        from_user=User(id=user, is_bot=False, first_name="u"), text=text,
        entities=[{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else None,
    ))


def cb(data: str, user: int = OWNER) -> Update:
    global _uid
    _uid += 1
    return Update(update_id=_uid, callback_query=CallbackQuery(
        id=str(_uid), chat_instance="x", data=data, from_user=User(id=user, is_bot=False, first_name="u"),
        message=Message(message_id=1, date=datetime.now(), chat=Chat(id=user, type="private"), text="q")))


async def feed(ui_, update):
    ui, session, *_ = ui_
    session.sent.clear()
    await ui.dp.feed_update(ui.bot, update)
    return session.texts()


async def test_stranger_rejected(ui):
    out = await feed(ui, msg("/status", STRANGER))
    assert out == ["⛔ This is a private bot."]


async def test_status_and_help(ui):
    out = await feed(ui, msg("/status"))
    assert "Mode: <b>notify</b>" in out[0]
    out = await feed(ui, msg("/help"))
    assert "/addleader" in out[0]


async def test_settings_commands(ui):
    _, _, db, _ = ui
    assert "auto" in (await feed(ui, msg("/mode auto")))[0]
    assert "Sizing: mirror 150" in (await feed(ui, msg("/size mirror 150")))[0]
    assert "5%" in (await feed(ui, msg("/slippage 5")))[0]
    assert "⚠️" in (await feed(ui, msg("/slippage 90")))[0]
    await feed(ui, msg("/caps 0.02 1"))
    await feed(ui, msg("/tpsl 100 30"))
    c = await db.get_config()
    assert (c.mode, c.sizing_mode, c.sizing_value, c.slippage_bps) == ("auto", "mirror", 150, 500)
    assert (c.min_trade_sol, c.max_trade_sol, c.tp_pct, c.sl_pct) == (0.02, 1, 100, 30)
    await feed(ui, msg("/pause"))
    assert (await db.get_config()).paused
    await feed(ui, msg("/resume"))
    assert not (await db.get_config()).paused


async def test_leader_management(ui):
    ui_, _, db, _ = ui
    assert "⚠️" in (await feed(ui, msg("/addleader notanaddress")))[0]
    assert "Following" in (await feed(ui, msg(f"/addleader {LEADER} Whale A")))[0]
    assert [l.address for l in await db.active_leaders()] == [LEADER]
    assert ui_._synced == [1]
    assert "Whale A" in (await feed(ui, msg("/leaders")))[0]
    await feed(ui, msg("/weight Whale A 50"))
    await feed(ui, msg("/rmleader Whale A"))
    assert await db.active_leaders() == []


async def test_withdraw_requires_confirmation(ui):
    ui_, session, *_ = ui
    out = await feed(ui, msg(f"/withdraw 0.5 {OTHER}"))
    assert "Send 0.5 SOL" in out[0]
    markup = session.sent[0].reply_markup
    cancel = markup.inline_keyboard[0][1].callback_data
    out = await feed(ui, cb(cancel))
    assert "Cancelled." in out


async def test_sell_and_positions(ui):
    _, _, _, rpc = ui
    assert "No open positions" in (await feed(ui, msg("/positions")))[0]
    rpc.token_raw[TOKEN] = (1_000_000, 6)
    out = await feed(ui, msg("/positions"))
    assert TOKEN in out[0]
    out = await feed(ui, msg(f"/sell {TOKEN} 50"))
    assert "Sell 50%" in out[0]


async def test_unknown_text(ui):
    assert "Unknown command" in (await feed(ui, msg("hello")))[0]
