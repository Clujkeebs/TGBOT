# TODO

Check items off as you go. Section A is for you to do. Sections B–D are code work, in priority order.

## A. Get it running (you, ~1 hour)

- [ ] **Get the keys**
  - [ ] Telegram bot token from @BotFather
  - [ ] Your Telegram user id from @userinfobot
  - [ ] Free Helius key from dashboard.helius.dev
  - [ ] Optional: Jupiter key from portal.jup.ag
  - [ ] Optional: Anthropic key from console.anthropic.com
- [ ] **Set up a server.** Use a small Ubuntu VPS (Hetzner CX22 or similar), or Docker on any always-on machine. Don't run it on a laptop that sleeps.
- [ ] **Configure:** `python setup_env.py`. Answer **y** to paper mode.
- [ ] **Back up `FERNET_KEY`** in a password manager. Without it the stored wallet can't be decrypted.
- [ ] **Pre-flight:** `python run.py --check`. Every line should show ✔.
  - If only Jupiter fails, set `JUPITER_API_KEY`.
  - If Anthropic fails, check the key or remove it.
- [ ] **Start it:** `python run.py`. Confirm the "Copy bot started" message arrives in Telegram.
- [ ] **Add 3–5 leaders:** `/addleader <addr> Name <addr2> Name2 …`
  - Find candidates on GMGN, Cielo, Birdeye "top traders", or dexscreener top wallets.
  - Pick wallets that win steadily, not ones with one lucky 100x.
- [ ] **Paper trade for 3–7 days.** Each day:
  - [ ] Open `/leaders` and note P/L and win rate per leader.
  - [ ] Open `/trades` and check the bot detected every swap you can see on Solscan for those wallets.
  - [ ] Read the skip reasons. If good trades are being filtered out, tune `/filters`.
  - [ ] Use `/rmleader` or `/leadermode … notify` on losing leaders.
- [ ] **Go live small:**
  - [ ] Set `PAPER_TRADING=false` and restart.
  - [ ] `/deposit` 0.2–0.5 SOL.
  - [ ] `/size fixed 0.02` and `/mode confirm`.
  - [ ] Watch real fills. The difference between the quote and the actual fill is your real-world slippage.
- [ ] **Scale up slowly.** Use `/mode auto` once 20+ live trades behave as expected. Raise `/caps` gradually and `/withdraw` profits regularly.

## B. Next code work (high impact)

- [ ] **Faster detection.** Add a Helius `transactionSubscribe` / `logsSubscribe` websocket feed to cut detection from ~2 s to ~400 ms. Keep polling as the fallback. (`bot/ingest.py`)
- [ ] **Jito bundles / smart priority fees.** Send buys through Jito with a tip for faster landing in hot launches. (`bot/jupiter.py`, `bot/rpc.py`)
- [ ] **Leader discovery command.** `/scout <token>` lists the earliest profitable buyers of a token that pumped, as candidate leaders. The AI can rank them.
- [ ] **Trailing stop-loss.** Add a trailing stop (sell after an X% drop from the peak) next to fixed TP/SL, plus partial take-profit ladders (e.g. sell 50% at +100%). (`bot/engine.py` `_scan_tp_sl`)
- [ ] **Backtester.** Replay a leader's last N days of swaps through the parser, filters and sizing, and estimate P/L before following them. The parser already works on any historical transaction. (`/backtest <addr> <days>`)
- [ ] **Per-leader P/L chart.** `/pnl chart` sends a PNG of cumulative P/L per leader. (matplotlib)

## C. Robustness

- [ ] **Run soak tests against mainnet** in paper mode, and save real transactions (pump.fun, Raydium CPMM, Meteora DLMM, Token-2022) as fixtures in `tests/fixtures/`.
- [ ] **Check pending buys on restart.** Re-check buys that were "sent but unconfirmed" when the bot restarted, by signature, and fix up the positions.
- [ ] **Multiple RPCs.** Accept a list in `SOLANA_RPC_URL` and switch to the next one when the current one fails.
- [ ] **Daily DB backup** of `data/` to S3 or Backblaze (cron + `sqlite3 .backup`).
- [ ] **Let the AI help with blacklisting.** Add an AI tool to propose blacklisting a token after a rug, applied through the existing Apply button.
- [ ] **Rate limits.** Stagger polling when following 20+ leaders on the free Helius plan (10 rps).

## D. Nice to have

- [ ] Inline-button menus for settings, so you don't have to type commands.
- [ ] A small web dashboard (FastAPI page) next to the webhook server.
- [ ] Multiple users, each with their own wallet. This needs per-user config and stricter security. Only do it if you really need it.

## Done

- [x] Swap detection by balance changes, works on any DEX
- [x] Jupiter swap/v1 + price/v3, impact gate, on-chain fill measurement
- [x] Modes: notify, confirm, auto. Sizing: fixed, percent, mirror. Proportional sells
- [x] TP/SL, daily loss breaker, blacklist, caps, fee reserve
- [x] Multiple leaders with per-leader mode, weight, max SOL, P/L and win rate
- [x] AI manager: buy screening, periodic reviews (advise/manage), chat, guardrails in code
- [x] Paper trading mode
- [x] Rug filters (on-chain mint/freeze authority, liquidity, holders, age)
- [x] Exposure limits (max positions, max SOL per token)
- [x] Poller catches up on bursts; RPC-down alert; stale confirmations expire on restart
- [x] Docker, systemd, CI, 63 tests
