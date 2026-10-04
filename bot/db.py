"""Async database access + first-run seeding."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import TradingDefaults
from bot.models import Base, Blacklist, BotConfig, Leader, ProcessedSignature, utcnow

log = logging.getLogger("copybot.db")


class Database:
    def __init__(self, url: str):
        if url.startswith("sqlite") and ":///" in url:
            path = url.split(":///", 1)[1]
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url, future=True, pool_pre_ping=True)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def init(self, defaults: TradingDefaults, leaders: list[dict], blacklist: list[dict]) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with self.session() as s:
            if (await s.execute(select(BotConfig))).scalars().first() is None:
                s.add(BotConfig(**defaults.model_dump()))
                log.info("Seeded config from config.yaml (mode=%s)", defaults.mode)

            for entry in leaders or []:
                addr = str(entry.get("address", "")).strip()
                if not addr or addr.upper().startswith("REPLACE"):
                    continue
                exists = (await s.execute(select(Leader).where(Leader.address == addr))).scalar_one_or_none()
                if exists is None:
                    s.add(Leader(address=addr, label=str(entry.get("label", ""))[:64]))
                    log.info("Seeded leader %s", addr)

            for entry in blacklist or []:
                mint = str(entry.get("mint", "")).strip()
                if not mint or mint.upper().startswith("REPLACE"):
                    continue
                exists = (await s.execute(select(Blacklist).where(Blacklist.token_mint == mint))).scalar_one_or_none()
                if exists is None:
                    s.add(Blacklist(token_mint=mint, reason=str(entry.get("reason", ""))))

    async def get_config(self) -> BotConfig:
        async with self.session() as s:
            cfg = (await s.execute(select(BotConfig))).scalars().first()
            if cfg is None:
                cfg = BotConfig()
                s.add(cfg)
                await s.flush()
            return cfg

    async def update_config(self, **fields) -> BotConfig:
        async with self.session() as s:
            cfg = (await s.execute(select(BotConfig))).scalars().first()
            if cfg is None:
                cfg = BotConfig()
                s.add(cfg)
            for k, v in fields.items():
                if not hasattr(cfg, k):
                    raise AttributeError(k)
                setattr(cfg, k, v)
            await s.flush()
            return cfg

    async def active_leaders(self) -> list[Leader]:
        async with self.session() as s:
            res = await s.execute(select(Leader).where(Leader.is_active.is_(True)))
            return list(res.scalars().all())

    async def mark_processed(self, signature: str) -> bool:
        """Atomically record a signature. Returns False if it was already processed."""
        try:
            async with self.session() as s:
                s.add(ProcessedSignature(signature=signature))
            return True
        except IntegrityError:
            return False

    async def prune_processed(self, older_than: timedelta = timedelta(days=3)) -> None:
        async with self.session() as s:
            await s.execute(delete(ProcessedSignature).where(ProcessedSignature.created_at < utcnow() - older_than))

    async def is_blacklisted(self, mint: str) -> bool:
        async with self.session() as s:
            res = await s.execute(select(Blacklist.id).where(Blacklist.token_mint == mint))
            return res.first() is not None

    async def close(self) -> None:
        await self.engine.dispose()
