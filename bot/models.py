"""SQLAlchemy 2.0 ORM models."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Wallet(Base):
    """The bot's hot wallet. `enc_secret` is a Fernet token of the 64-byte keypair."""

    __tablename__ = "wallets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pubkey: Mapped[str] = mapped_column(String(64), unique=True)
    enc_secret: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Leader(Base):
    __tablename__ = "leaders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-leader size multiplier in percent (100 = normal size).
    weight_pct: Mapped[float] = mapped_column(Float, default=100.0)
    # Per-leader overrides: "" = use the global setting.
    mode: Mapped[str] = mapped_column(String(16), default="")  # "" | auto | confirm | notify
    max_sol: Mapped[float] = mapped_column(Float, default=0.0)  # 0 = global max_trade_sol
    # Performance of positions this leader opened for us.
    realized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    copied_sol: Mapped[float] = mapped_column(Float, default=0.0)
    ai_note: Mapped[str] = mapped_column(Text, default="")
    trades_seen: Mapped[int] = mapped_column(Integer, default=0)
    buys: Mapped[int] = mapped_column(Integer, default=0)
    sells: Mapped[int] = mapped_column(Integer, default=0)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Trade(Base):
    """A detected leader trade and what we did about it."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    leader: Mapped[str] = mapped_column(String(64), default="", index=True)
    side: Mapped[str] = mapped_column(String(8))  # buy | sell
    token_mint: Mapped[str] = mapped_column(String(64), index=True)
    leader_sol: Mapped[float] = mapped_column(Float, default=0.0)
    leader_token_amount: Mapped[float] = mapped_column(Float, default=0.0)
    leader_tx_sig: Mapped[str] = mapped_column(String(100), default="")
    # What we did:
    our_sol: Mapped[float] = mapped_column(Float, default=0.0)  # spent (buy) / received (sell)
    our_token_amount: Mapped[float] = mapped_column(Float, default=0.0)
    copy_tx_sig: Mapped[str] = mapped_column(String(100), default="")
    # seen | pending | executed | failed | skipped | timeout
    status: Mapped[str] = mapped_column(String(16), default="seen")
    origin: Mapped[str] = mapped_column(String(16), default="copy")  # copy | manual | tp | sl
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Position(Base):
    """Our open position per token. Quantity is re-synced from chain when trading."""

    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(64), unique=True)
    leader: Mapped[str] = mapped_column(String(64), default="")  # leader whose buy opened it
    qty: Mapped[float] = mapped_column(Float, default=0.0)  # ui amount
    decimals: Mapped[int] = mapped_column(Integer, default=0)
    cost_basis_sol: Mapped[float] = mapped_column(Float, default=0.0)  # SOL in the open qty
    realized_pnl_sol: Mapped[float] = mapped_column(Float, default=0.0)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BotConfig(Base):
    """Single-row live config (seeded from config.yaml, edited via Telegram)."""

    __tablename__ = "bot_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="notify")  # auto | confirm | notify
    sizing_mode: Mapped[str] = mapped_column(String(16), default="fixed")  # mirror | percent | fixed
    sizing_value: Mapped[float] = mapped_column(Float, default=0.05)
    slippage_bps: Mapped[int] = mapped_column(Integer, default=500)
    min_trade_sol: Mapped[float] = mapped_column(Float, default=0.01)
    max_trade_sol: Mapped[float] = mapped_column(Float, default=0.5)
    max_price_impact_pct: Mapped[float] = mapped_column(Float, default=15.0)
    priority_fee_max_lamports: Mapped[int] = mapped_column(Integer, default=2_000_000)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Float, default=20.0)
    tp_pct: Mapped[float] = mapped_column(Float, default=0.0)
    sl_pct: Mapped[float] = mapped_column(Float, default=0.0)
    confirm_timeout_s: Mapped[int] = mapped_column(Integer, default=30)
    copy_sells: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_summary_hour_utc: Mapped[int] = mapped_column(Integer, default=-1)
    # Token safety filters (rule-based, run before the AI screen)
    min_liquidity_usd: Mapped[float] = mapped_column(Float, default=5000.0)
    require_mint_disabled: Mapped[bool] = mapped_column(Boolean, default=True)
    require_freeze_disabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_top_holders_pct: Mapped[float] = mapped_column(Float, default=0.0)  # 0 = off
    min_token_age_min: Mapped[float] = mapped_column(Float, default=0.0)  # 0 = off
    # Exposure limits
    max_open_positions: Mapped[int] = mapped_column(Integer, default=10)  # 0 = unlimited
    max_token_exposure_sol: Mapped[float] = mapped_column(Float, default=0.0)  # 0 = unlimited
    # AI manager
    ai_screen_buys: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_autonomy: Mapped[str] = mapped_column(String(16), default="advise")  # off | advise | manage
    ai_review_hours: Mapped[float] = mapped_column(Float, default=6.0)
    ai_max_trade_sol: Mapped[float] = mapped_column(Float, default=0.5)  # AI can never set max above this
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Blacklist(Base):
    __tablename__ = "blacklist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_mint: Mapped[str] = mapped_column(String(64), unique=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProcessedSignature(Base):
    """De-duplicates leader transactions (webhook retries, poll overlap)."""

    __tablename__ = "processed_signatures"
    signature: Mapped[str] = mapped_column(String(100), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PendingConfirmation(Base):
    __tablename__ = "pending_confirmations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)  # JSON
    chat_id: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[int] = mapped_column(Integer, default=0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaperBalance(Base):
    """Simulated balances for PAPER_TRADING=true (WSOL mint = SOL in lamports)."""

    __tablename__ = "paper_balances"
    mint: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw: Mapped[int] = mapped_column(Integer, default=0)
    decimals: Mapped[int] = mapped_column(Integer, default=0)


class DailyBaseline(Base):
    """Portfolio value (in SOL) at the first check of each UTC day - drives the loss breaker."""

    __tablename__ = "daily_baselines"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD
    portfolio_sol: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
