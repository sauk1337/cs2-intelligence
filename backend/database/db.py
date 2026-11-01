"""Database engine, sessions and KV helpers."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import KV, SCHEMA_VERSION, Base

log = logging.getLogger("cs2.db")


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        kwargs: dict = {"pool_pre_ping": True}
        if self.is_sqlite:
            kwargs = {"connect_args": {"timeout": 30}}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if self.is_sqlite:
            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self.kv_set("schema_version", str(SCHEMA_VERSION))
        log.info("Database ready (%s)", self.url.split("///")[-1] if self.is_sqlite else self.url.split("@")[-1])

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessionmaker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        async with self.session() as s:
            row = await s.get(KV, key)
            return row.value if row else default

    async def kv_set(self, key: str, value: str) -> None:
        async with self.session() as s:
            row = await s.get(KV, key)
            if row:
                row.value = value
            else:
                s.add(KV(key=key, value=value))

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.session() as s:
                await s.execute(select(1))
            return True
        except Exception:
            return False


def now() -> float:
    return time.time()
