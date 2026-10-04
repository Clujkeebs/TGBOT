"""Interactive .env generator: asks for the few required values, generates secrets.

    python setup_env.py
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ENV = Path(".env")


def ask(prompt: str, default: str = "", required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        v = input(f"{prompt}{suffix}: ").strip() or default
        if v or not required:
            return v
        print("  (required)")


def main() -> None:
    if ENV.exists() and input(".env exists - overwrite? [y/N] ").lower() != "y":
        sys.exit("Aborted.")
    print("\n1) Create a bot with @BotFather (/newbot) and paste the token.")
    token = ask("TELEGRAM_BOT_TOKEN")
    print("\n2) Message @userinfobot to get your numeric user id.")
    chat = ask("TELEGRAM_CHAT_ID")
    print("\n3) Solana RPC URL. Free Helius key: https://dashboard.helius.dev")
    helius_key = ask("Helius API key (leave empty to paste a full RPC URL instead)", required=False)
    rpc = f"https://mainnet.helius-rpc.com/?api-key={helius_key}" if helius_key else ask("SOLANA_RPC_URL")
    print("\n4) Detection mode: 'poll' needs nothing else; 'webhook' needs a public HTTPS domain.")
    mode = ask("INGEST_MODE (poll/webhook)", "poll")
    webhook_url = webhook_secret = ""
    if mode == "webhook":
        if not helius_key:
            helius_key = ask("HELIUS_API_KEY")
        domain = ask("Public base URL, e.g. https://bot.example.com").rstrip("/")
        webhook_url = f"{domain}/webhook/{secrets.token_urlsafe(24)}"
        webhook_secret = secrets.token_urlsafe(32)
    jup = ask("\n5) Jupiter API key from portal.jup.ag (optional)", required=False)
    fernet = Fernet.generate_key().decode()

    ENV.write_text(
        f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\nSOLANA_RPC_URL={rpc}\nFERNET_KEY={fernet}\n"
        f"INGEST_MODE={mode}\nPOLL_INTERVAL_S=2\nHELIUS_API_KEY={helius_key}\nHELIUS_WEBHOOK_URL={webhook_url}\n"
        f"HELIUS_WEBHOOK_SECRET={webhook_secret}\nJUPITER_API_KEY={jup}\n"
        f"DATABASE_URL=sqlite+aiosqlite:///./data/copybot.db\nLOG_LEVEL=INFO\nHOT_WALLET_MAX_SOL=5\n",
        encoding="utf-8",
    )
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    print("\n✔ Wrote .env (permissions 600).")
    print("⚠️  Back up FERNET_KEY somewhere safe - without it the stored wallet can't be decrypted.")
    if webhook_url:
        print(f"   Point your reverse proxy at port 8000. Helius will POST to:\n   {webhook_url}")
    print("\nNext:  python run.py --check   then   python run.py")


if __name__ == "__main__":
    main()
