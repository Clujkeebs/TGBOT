"""Entrypoint: wires everything together and runs it on one asyncio loop.

    python run.py            # run the bot
    python run.py --check    # validate config + connectivity, then exit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pydantic import ValidationError

from bot.config import get_settings, load_yaml, trading_defaults_from_yaml
from bot.crypto import KeyVault
from bot.db import Database
from bot.engine import CopyEngine
from bot.ingest import HeliusWebhooks, LeaderPoller, build_webhook_app
from bot.jupiter import Jupiter
from bot.notifier import Notifier
from bot.rpc import SolanaRpc
from bot.telegram_ui import TelegramUI
from bot.wallet import WalletManager

log = logging.getLogger("copybot")


def load_settings():
    try:
        return get_settings()
    except ValidationError as e:
        missing = [".".join(str(p) for p in err["loc"]).upper() for err in e.errors()]
        print("Configuration error - fix these in .env (run `python setup_env.py` for a guided setup):")
        for err, name in zip(e.errors(), missing):
            print(f"  - {name}: {err['msg']}")
        sys.exit(2)


async def check(settings) -> int:
    """Pre-flight: Telegram token, RPC, Jupiter."""
    ok = True
    bot = Bot(settings.telegram_bot_token)
    try:
        me = await bot.get_me()
        print(f"✔ Telegram bot @{me.username}")
    except Exception as e:  # noqa: BLE001
        print(f"✘ Telegram: {e}")
        ok = False
    finally:
        await bot.session.close()
    rpc = SolanaRpc(settings.solana_rpc_url)
    try:
        bh = await rpc.get_latest_blockhash()
        print(f"✔ Solana RPC (blockhash {bh[:8]}…)")
    except Exception as e:  # noqa: BLE001
        print(f"✘ Solana RPC: {e}")
        ok = False
    finally:
        await rpc.close()
    jup = Jupiter(settings.jupiter_url, settings.jupiter_api_key)
    try:
        p = await jup.sol_price_usd()
        print(f"✔ Jupiter ({settings.jupiter_url}) SOL=${p:.2f}" if p else "✘ Jupiter: no SOL price returned")
        ok = ok and p > 0
    except Exception as e:  # noqa: BLE001
        print(f"✘ Jupiter: {e}")
        ok = False
    finally:
        await jup.close()
    try:
        KeyVault(settings.fernet_key)
        print("✔ FERNET_KEY valid")
    except ValueError as e:
        print(f"✘ {e}")
        ok = False
    if settings.ingest_mode == "webhook" and not (settings.helius_api_key and settings.helius_webhook_url
                                                  and settings.helius_webhook_secret):
        print("✘ INGEST_MODE=webhook needs HELIUS_API_KEY, HELIUS_WEBHOOK_URL and HELIUS_WEBHOOK_SECRET")
        ok = False
    return 0 if ok else 1


async def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    yaml_cfg = load_yaml(settings.config_path)
    db = Database(settings.database_url)
    await db.init(trading_defaults_from_yaml(yaml_cfg), yaml_cfg.get("leaders") or [], yaml_cfg.get("blacklist") or [])

    rpc = SolanaRpc(settings.solana_rpc_url)
    vault = KeyVault(settings.fernet_key)
    wallet = WalletManager(db, rpc, vault)
    pubkey, created = await wallet.ensure_wallet()

    bot = Bot(settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = Notifier(bot, settings.alert_chat_id)
    jupiter = Jupiter(settings.jupiter_url, settings.jupiter_api_key)
    engine = CopyEngine(db, rpc, wallet, jupiter, notifier)

    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    on_leaders_changed = None

    if settings.ingest_mode == "webhook":
        if not (settings.helius_api_key and settings.helius_webhook_url and settings.helius_webhook_secret):
            raise SystemExit("INGEST_MODE=webhook needs HELIUS_API_KEY, HELIUS_WEBHOOK_URL, HELIUS_WEBHOOK_SECRET")
        import uvicorn

        helius = HeliusWebhooks(settings.helius_api_key, settings.helius_webhook_url, settings.helius_webhook_secret)

        async def on_leaders_changed() -> None:  # noqa: F811
            await helius.sync([ld.address for ld in await db.active_leaders()])

        app = build_webhook_app(db, engine.handle_leader_signature, settings.webhook_path_token,
                                settings.helius_webhook_secret)
        server = uvicorn.Server(uvicorn.Config(app, host=settings.webhook_host, port=settings.webhook_port,
                                               log_level="warning", access_log=False))
        tasks.append(asyncio.create_task(server.serve(), name="webhook"))
        try:
            await on_leaders_changed()
        except Exception as e:  # noqa: BLE001
            log.error("Helius webhook sync failed: %s", e)
        ingest_desc = f"Helius webhook (:{settings.webhook_port})"
    else:
        poller = LeaderPoller(db, rpc, engine.handle_leader_signature, settings.poll_interval_s)
        tasks.append(asyncio.create_task(poller.run(stop), name="poller"))
        ingest_desc = f"RPC polling every {settings.poll_interval_s:g}s"

    ui = TelegramUI(bot, engine, wallet, settings.owner_ids, on_leaders_changed,
                    settings.hot_wallet_max_sol, ingest_desc)
    await ui.set_menu()
    tasks.append(asyncio.create_task(engine.tp_sl_loop(stop), name="tpsl"))
    tasks.append(asyncio.create_task(engine.daily_summary_loop(stop), name="summary"))

    cfg = await db.get_config()
    leaders = await db.active_leaders()
    try:
        sol = await wallet.sol_balance()
        bal = f"{sol:.4f} SOL"
    except Exception as e:  # noqa: BLE001
        bal = f"unknown ({e})"
    hello = (f"🤖 <b>Copy bot started</b>\nWallet: <code>{pubkey}</code>{' (NEW - fund it!)' if created else ''}\n"
             f"Balance: {bal}\nMode: <b>{cfg.mode}</b>{' (paused)' if cfg.paused else ''} · "
             f"{len(leaders)} leader(s) · {ingest_desc}\n/help for commands")
    await notifier.send(hello)
    log.info("Started. wallet=%s mode=%s leaders=%d ingest=%s", pubkey, cfg.mode, len(leaders), ingest_desc)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    polling = asyncio.create_task(ui.dp.start_polling(bot, handle_signals=False), name="telegram")
    for t in tasks + [polling]:
        t.add_done_callback(lambda t: (not t.cancelled() and t.exception() and log.error(
            "task %s crashed: %r", t.get_name(), t.exception())) or stop.set())
    try:
        await stop.wait()
    finally:
        log.info("Shutting down…")
        try:
            await ui.dp.stop_polling()
        except RuntimeError:
            pass
        for t in tasks + [polling]:
            t.cancel()
        await asyncio.gather(*tasks, polling, return_exceptions=True)
        await engine.shutdown()
        await jupiter.close()
        await rpc.close()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate config and connectivity, then exit")
    a = ap.parse_args()
    if a.check:
        sys.exit(asyncio.run(check(load_settings())))
    asyncio.run(main())
