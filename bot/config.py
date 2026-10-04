"""Application configuration.

* Secrets / infrastructure come from environment variables (or `.env`).
* User-tunable trading defaults come from `config.yaml` and are copied into the
  database on first run; after that they are edited live from Telegram.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
STABLE_MINTS = {USDC_MINT, USDT_MINT}
LAMPORTS_PER_SOL = 1_000_000_000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram (required) ---
    telegram_bot_token: str
    # Comma-separated numeric Telegram user ids allowed to control the bot.
    # The first id also receives all alerts.
    telegram_chat_id: str

    # --- Solana RPC (required) ---
    solana_rpc_url: str

    # --- Encryption of the hot-wallet key at rest (required) ---
    fernet_key: str

    # --- How leader trades are detected ---
    #   poll    : poll leader wallets over RPC (no public URL needed - easiest)
    #   webhook : Helius webhooks push to our HTTPS endpoint (lowest latency)
    ingest_mode: Literal["poll", "webhook"] = "poll"
    poll_interval_s: float = 2.0

    # --- Helius (only needed for ingest_mode=webhook) ---
    helius_api_key: str = ""
    # Full public URL Helius posts to, e.g. https://bot.example.com/webhook/<random-token>
    helius_webhook_url: str = ""
    # Sent back by Helius verbatim in the Authorization header.
    helius_webhook_secret: str = ""
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8000

    # --- Jupiter ---
    # Free key from https://portal.jup.ag. Without one the keyless lite API is used.
    jupiter_api_key: str = ""
    jupiter_base_url: str = ""

    # --- Storage ---
    database_url: str = "sqlite+aiosqlite:///./data/copybot.db"

    # --- Misc ---
    config_path: str = "config.yaml"
    log_level: str = "INFO"
    # Warn (in /balance and on startup) if the hot wallet holds more than this.
    hot_wallet_max_sol: float = 5.0

    @field_validator("telegram_chat_id")
    @classmethod
    def _check_chat_ids(cls, v: str) -> str:
        ids = [x.strip() for x in v.split(",") if x.strip()]
        if not ids or not all(x.lstrip("-").isdigit() for x in ids):
            raise ValueError("TELEGRAM_CHAT_ID must be one or more numeric ids, comma separated")
        return ",".join(ids)

    @property
    def owner_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_chat_id.split(",")}

    @property
    def alert_chat_id(self) -> int:
        return int(self.telegram_chat_id.split(",")[0])

    @property
    def jupiter_url(self) -> str:
        if self.jupiter_base_url:
            return self.jupiter_base_url.rstrip("/")
        return "https://api.jup.ag" if self.jupiter_api_key else "https://lite-api.jup.ag"

    @property
    def webhook_path_token(self) -> str:
        """Last path segment of HELIUS_WEBHOOK_URL - the unguessable route token."""
        return self.helius_webhook_url.rstrip("/").rsplit("/", 1)[-1]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class TradingDefaults(BaseModel):
    """Defaults seeded into the DB on first run (see config.yaml)."""

    mode: Literal["auto", "confirm", "notify"] = "notify"
    sizing_mode: Literal["mirror", "percent", "fixed"] = "fixed"
    sizing_value: float = 0.05
    slippage_bps: int = 500
    min_trade_sol: float = 0.01
    max_trade_sol: float = 0.5
    max_price_impact_pct: float = 15.0
    priority_fee_max_lamports: int = 2_000_000
    daily_loss_limit_pct: float = 20.0
    tp_pct: float = 0.0
    sl_pct: float = 0.0
    confirm_timeout_s: int = 30
    copy_sells: bool = True
    daily_summary_hour_utc: int = -1


_SIZING_ALIASES = {"override": "percent", "multiplier": "mirror"}


def load_yaml(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def trading_defaults_from_yaml(data: dict) -> TradingDefaults:
    sizing = data.get("sizing") or {}
    raw = {k: v for k, v in data.items() if k in TradingDefaults.model_fields}
    if sizing:
        mode = str(sizing.get("mode", "fixed"))
        raw["sizing_mode"] = _SIZING_ALIASES.get(mode, mode)
        if "value" in sizing:
            raw["sizing_value"] = sizing["value"]
    notify = data.get("notify") or {}
    if "daily_summary_hour_utc" in notify:
        raw["daily_summary_hour_utc"] = notify["daily_summary_hour_utc"]
    return TradingDefaults.model_validate(raw)
