# TGBOT – Solana copy-trading Telegram bot

A private Telegram bot that watches Solana "leader" wallets and copies their trades into its own hot wallet through Jupiter. You control everything from Telegram.

> ⚠️ **Real money and high risk.** Memecoin copy-trading loses money more often than it makes money. Leaders may be token devs dumping on copiers. Keep only what you can afford to lose in the hot wallet, and start in `notify` mode.

## What it does

- **Copies many wallets at once.** Follow as many leaders as you like (`/addleader` takes several at a time). Their trades are processed in parallel. Each leader can have its own mode, size weight and max SOL, and the bot tracks P/L and win rate per leader.
- **AI manager (Claude), optional.** It checks every copy buy for rug signs, reviews your leaders every few hours, and lets you manage the bot by chatting in plain English. Details [below](#ai-manager).

- **Detects leader swaps on any DEX** (Raydium, Pump.fun, Orca, Meteora, Jupiter routes, …) by comparing the wallet's SOL and token balances before and after each transaction. It doesn't depend on per-DEX decoding.
- **Paper trading.** With `PAPER_TRADING=true` the bot follows real leaders and uses real Jupiter quotes, but trades with fake SOL. Paper results go to a separate database (`data/paper.db`). Start here.
- **Rug filters.** Before any buy, the bot checks on-chain whether the token's creator can still mint more tokens or freeze yours. From Jupiter it checks minimum liquidity, and optionally how much the top holders own and how old the token is. Set these with `/filters`.
- **Exposure limits.** Caps on how many positions you hold at once and how much SOL goes into one token (`/limits`).
- **RPC watchdog.** You get a Telegram alert if the RPC stops answering for 2 minutes, and another when it recovers. Busy leaders are caught up on too (up to 100 transactions per check).
- **Three modes:** `notify` (alerts only) → `confirm` (tap ✅/❌ for each trade) → `auto`.
- **Sizing:** fixed SOL per trade, a % of your balance, or `mirror`: the same % of your SOL that the leader spent of theirs, with a multiplier. You can also give each leader a weight.
- **Sells are mirrored proportionally.** If the leader who opened the position sells 30% of a token, you sell 30% of yours. If they sell 98% or more, you exit completely. Sells by other leaders holding the same token are shown but not copied.
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
5. **Optional:** an [Anthropic API key](https://console.anthropic.com) (`ANTHROPIC_API_KEY`) to turn on the AI manager.

`setup_env.py` offers to start in **paper mode**, and that's the recommended first step. When the bot starts, it messages you. Then:

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
| `/leaders` | Every leader with P/L, win rate and overrides |
| `/addleader <addr> [label] [<addr2> [label2] …]` `/rmleader <addr\|label>` | Follow one or many wallets, or stop following one |
| `/weight <addr\|label> <pct>` | Per-leader size multiplier |
| `/leadermode <addr\|label> auto\|confirm\|notify\|default` | Per-leader mode (e.g. auto for your best wallet, notify for new ones) |
| `/leadermax <addr\|label> <SOL>` | Per-leader max SOL per buy |
| *any plain message* | Chat with the AI manager |
| `/ai` `/ai advise\|manage\|off` `/ai review` | AI status, autonomy level, run a review now |
| `/ai screen on\|off` `/ai every <h>` `/ai cap <SOL>` `/ai reset` | AI buy screening, review interval, max-size ceiling, clear chat |
| `/mode auto\|confirm\|notify` | Execution mode |
| `/size fixed <SOL>` · `percent <%>` · `mirror <%>` | Sizing |
| `/caps <min> <max>` `/slippage <%>` `/impact <%>` `/priority <SOL>` | Execution limits |
| `/tpsl <tp%> <sl%>` `/loss <%>` | Take-profit/stop-loss, daily loss limit |
| `/copysells on\|off` `/timeout <s>` | Sell mirroring, how long confirm mode waits |
| `/blacklist [add\|rm <mint> [reason]]` | Never buy these tokens |
| `/filters [liquidity <USD>\|mint on\|off\|freeze on\|off\|holders <%>\|age <min>]` | Rug filters |
| `/limits <max positions> <max SOL per token>` | Exposure limits (0 = unlimited) |
| `/paper` `/paper reset <SOL>` | Paper balance, start over (paper mode only) |
| `/positions` | Open positions with live value, unrealized and realized P/L |
| `/buy <mint> <SOL>` `/sell <mint\|all> [pct]` | Manual trades |
| `/trades` `/summary [hour\|off]` | Recent activity, daily summary |
| `/pause` `/resume` | Stop or start copying (alerts continue) |

## AI manager

Set `ANTHROPIC_API_KEY` in `.env` (and optionally `AI_MODEL`, default `claude-opus-5-5`). The AI does three things:

1. **Checks buys.** Before a copy buy executes, Claude looks at:
   - the token: liquidity, holder concentration, whether mint/freeze authority is still enabled, organic score, age
   - the leader's track record
   - your current exposure

   It answers **approve**, **reduce** (smaller size) or **reject**, and its reason appears in the trade alert. If the AI is slow or down, the normal rules decide and the trade isn't blocked. Turn it off with `/ai screen off`.
2. **Reviews leaders.** Every `review_hours` (default 6), and whenever you run `/ai review`, Claude reviews each leader's results and your open positions. It can re-weight leaders, cap or pause them, and tune TP/SL, slippage and sizing.
   - `advise` (default): the changes come to you as a proposal with an **Apply** button.
   - `manage`: it applies them itself and reports what it did.
3. **Chat.** Message the bot normally, for example:
   - "which leader is losing me money?"
   - "set TP 100% and SL 30%"
   - "only alert me for the sniper wallet"

**Hard limits, enforced in code:** the AI can never withdraw, export the key, place trades, switch the bot to auto mode, resume a pause on its own, or raise the max trade size above `ai_max_trade_sol`. Only you can change that limit, with `/ai cap`. AI requests use server-side model fallback (`fallbacks: "default"`), so if the model declines a request, it is retried on a fallback model.

**Cost:** you pay per Claude call on your Anthropic account. That's one short call per copied buy while screening is on, one longer call per review, and one per chat message. To cut cost, turn screening off or lengthen `/ai every`.

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
  filters.py      rug filters (on-chain authorities + Jupiter liquidity/holders/age)
  paper.py        paper trading wallet + simulated Jupiter fills
  ai.py           Claude AI manager: buy screen, periodic review, chat tools + guardrails
  telegram_ui.py  aiogram 3 commands (owner-only middleware)
run.py            entrypoint (python run.py --check for a pre-flight)
setup_env.py      interactive .env generator
tests/            parser, sizing, engine, multi-leader, AI, ingest and Telegram tests (pytest)
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
