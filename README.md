# TGBOT – Solana copy-trading Telegram bot

A private Telegram bot that watches Solana "leader" wallets and copies their trades into its own hot wallet through Jupiter. You control everything from Telegram.

> ⚠️ **Real money and high risk.** Memecoin copy-trading loses money more often than it makes money. Leaders may be token devs dumping on copiers. Keep only what you can afford to lose in the hot wallet, and start in `notify` mode.

## What it does

- **Detects leader swaps on any DEX** (Raydium, Pump.fun, Orca, Meteora, Jupiter routes, …) by comparing the wallet's SOL and token balances before and after each transaction. It doesn't depend on per-DEX decoding.
- **Three modes:** `notify` (alerts only) → `confirm` (tap ✅/❌ for each trade) → `auto`.
- **Sizing:** fixed SOL per trade, a % of your balance, or `mirror`: the same % of your SOL that the leader spent of theirs, with a multiplier. You can also give each leader a weight.
- **Sells are mirrored proportionally.** If the leader sells 30% of a token, you sell 30% of yours. If they sell 98% or more, you exit completely.
- **Risk controls:** min/max trade size, a 0.02 SOL fee reserve, a max price-impact check, a token blacklist, take-profit/stop-loss, and a daily loss limit that pauses the bot automatically.
- **Wallet:** the wallet is created on first run and its private key is encrypted at rest with Fernet. The bot also has deposit QR codes, `/withdraw`, and `/exportkey` for importing into Phantom.
- **Two ways to detect trades:**
  - `poll` (the default): needs only an RPC URL. No domain or server setup. About 2 s latency.
  - `webhook`: Helius pushes transactions to you. Lowest latency, but needs a public HTTPS URL. The bot creates and updates the Helius webhook itself.

## Quick start

```bash
git clone https://github.com/clujkeebs/tgbot.git && cd tgbot
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python setup_env.py        # asks for bot token, your Telegram id, Helius key; generates secrets
python run.py --check      # verifies Telegram, RPC, Jupiter, keys
python run.py              # start
```

You need:

1. **A Telegram bot token.** Get one from [@BotFather](https://t.me/BotFather) with `/newbot`.
2. **Your Telegram user id.** Ask [@userinfobot](https://t.me/userinfobot). Only this id can control the bot.
3. **A Solana RPC URL.** A free [Helius](https://dashboard.helius.dev) key is fine. The public `api.mainnet-beta.solana.com` endpoint is too rate-limited for polling.
4. **Optional:** a free [Jupiter API key](https://portal.jup.ag) for higher swap and price rate limits.

When the bot starts, it messages you its wallet address. Then:

```
/deposit                      send a small amount of SOL (e.g. 0.1)
/addleader <wallet> Whale A   follow a wallet
                              …watch the alerts for a while in notify mode…
/size fixed 0.02              tiny fixed size to start
/mode confirm                 approve each trade by tapping ✅
/mode auto                    once you trust it
```

### Docker

```bash
python setup_env.py          # or copy .env.example to .env and fill it in
docker compose up -d --build
docker compose logs -f
```

### systemd (VPS)

```bash
sudo adduser --disabled-password copybot && sudo -iu copybot
git clone https://github.com/clujkeebs/tgbot.git TGBOT && cd TGBOT
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python setup_env.py && mkdir -p data
exit
sudo cp /home/copybot/TGBOT/solana-copy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now solana-copy-bot
journalctl -u solana-copy-bot -f
```

### Webhook mode (optional, fastest)

Set `INGEST_MODE=webhook`, `HELIUS_API_KEY`, `HELIUS_WEBHOOK_URL=https://your.domain/webhook/<long-random-token>` and `HELIUS_WEBHOOK_SECRET`. `setup_env.py` generates the token and secret for you. Put a TLS proxy in front of port 8000, for example with Caddy:

```
your.domain {
    reverse_proxy 127.0.0.1:8000
}
```

On startup, and whenever you `/addleader` or `/rmleader`, the bot creates or updates the Helius webhook.

The webhook is protected in three ways:

1. A secret path token.
2. A constant-time check of the `Authorization` header (Helius sends back the `authHeader` the bot registered).
3. The webhook body is never trusted. The bot takes only the signature from it, fetches that transaction from your own RPC, and checks that the leader signed it.

## Commands

| | |
|---|---|
| `/status` | Mode, sizing, limits, leaders, balance, counters |
| `/wallet` `/deposit` `/balance` | Address, QR code, holdings with USD values |
| `/withdraw <amount\|all> <address>` | Send SOL out (asks you to confirm) |
| `/exportkey` | Shows the private key in a spoiler and deletes it after 60 s |
| `/leaders` `/addleader <addr> [label]` `/rmleader <addr\|label>` | Manage leaders |
| `/weight <addr\|label> <pct>` | Per-leader size multiplier |
| `/mode auto\|confirm\|notify` | Execution mode |
| `/size fixed <SOL>` · `percent <%>` · `mirror <%>` | Sizing |
| `/caps <min> <max>` `/slippage <%>` `/impact <%>` `/priority <SOL>` | Execution limits |
| `/tpsl <tp%> <sl%>` `/loss <%>` | Take-profit/stop-loss, daily loss limit |
| `/copysells on\|off` `/timeout <s>` | Sell mirroring, how long confirm mode waits |
| `/blacklist [add\|rm <mint> [reason]]` | Never buy these tokens |
| `/positions` | Open positions with live value, unrealized and realized P/L |
| `/buy <mint> <SOL>` `/sell <mint\|all> [pct]` | Manual trades |
| `/trades` `/summary [hour\|off]` | Recent activity, daily summary |
| `/pause` `/resume` | Stop or start copying (alerts continue) |

## Project layout

```
bot/
  config.py       env settings + config.yaml defaults
  models.py db.py SQLAlchemy models, async DB, first-run seeding
  crypto.py       Fernet key vault, constant-time compare
  rpc.py          minimal async Solana JSON-RPC (send + rebroadcast + confirm)
  swap_parser.py  balance-delta swap detection (leaders and our own fills)
  jupiter.py      Jupiter swap/v1 + price/v3, impact gate, on-chain fill measurement
  wallet.py       hot wallet: create/import/export, balances, withdraw, QR
  engine.py       sizing, risk, execution, positions, confirm flow, TP/SL, loss breaker
  ingest.py       RPC poller, Helius webhook receiver + webhook sync
  notifier.py     message formatting
  telegram_ui.py  aiogram 3 commands (owner-only middleware)
run.py            entrypoint (python run.py --check for a pre-flight)
setup_env.py      interactive .env generator
tests/            parser, sizing, engine, ingest and Telegram tests (pytest)
```

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
```

## Security notes

- **The hot wallet's key lives on the server.** It is encrypted in the database, but the bot decrypts it in memory to sign trades. Anyone with shell access to the box, or with both `.env` and the DB, can drain the wallet. Keep the balance small and withdraw profits regularly.
- **Back up `FERNET_KEY`.** Without it the stored wallet cannot be decrypted. `/exportkey` is the other way to back up the wallet.
- **Leave the firewall closed in poll mode.** Poll mode needs no open ports. In webhook mode, open only 443, served by the TLS proxy.
- **The bot is owner-only.** Only the user ids listed in `TELEGRAM_CHAT_ID` can use it. Everyone else is refused.
