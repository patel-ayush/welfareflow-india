"""
Async SQLAlchemy engine factory and session lifecycle management.

Fallback pattern (hackathon-friendly, zero-infra boot):
  - First attempt: connect to the configured PostgreSQL DSN via asyncpg.
  - If PostgreSQL is unreachable at startup, transparently fall back to an
    in-memory async SQLite database (sqlite+aiosqlite:///:memory:) using a
    StaticPool so the single in-memory schema is shared across all sessions.

The active engine / sessionmaker are module-level singletons that
`create_all_tables()` may rebind to the SQLite fallback at startup.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool

from config import get_settings

logger: logging.Logger = logging.getLogger(__name__)

settings = get_settings()

# asyncpg requires the postgresql+asyncpg:// scheme
_PG_ASYNC_URL: str = settings.database_url.replace(
    "postgresql://", "postgresql+asyncpg://", 1
)
_SQLITE_FALLBACK_URL: str = "sqlite+aiosqlite:///:memory:"


class Base(DeclarativeBase):
    pass


def _build_postgres_engine() -> AsyncEngine:
    return create_async_engine(
        _PG_ASYNC_URL,
        echo=(settings.app_env == "development"),
        pool_size=10,
        max_overflow=20,
        pool_recycle=3600,
        pool_pre_ping=True,
    )


def _build_sqlite_engine() -> AsyncEngine:
    # StaticPool + a single shared connection keeps the :memory: schema alive
    # for the lifetime of the process across every async session.
    return create_async_engine(
        _SQLITE_FALLBACK_URL,
        echo=(settings.app_env == "development"),
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


# Active singletons — may be rebound to SQLite by create_all_tables().
# Building the Postgres engine can fail at import time if the asyncpg driver is
# not installed; that must ALSO degrade to SQLite (not crash the process), so
# the zero-infra boot promise holds even without asyncpg present.
try:
    engine: AsyncEngine = _build_postgres_engine()
except Exception as _engine_exc:  # noqa: BLE001 — driver missing / bad DSN at import
    logger.info(
        "database: asyncpg driver unavailable or DATABASE_URL empty at import (%s) — "
        "deferring to SQLite until create_all_tables() runs",
        type(_engine_exc).__name__,
    )
    engine = _build_sqlite_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


def _rebind_session_factory(new_engine: AsyncEngine) -> None:
    """Point the module-level engine and session factory at a new engine."""
    global engine, AsyncSessionLocal
    engine = new_engine
    AsyncSessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a transactional session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """
    Transactional session context manager for use OUTSIDE the FastAPI request
    cycle (background tasks, agent nodes, the SLA watchdog).

    Critically, this reads the CURRENT module-level `AsyncSessionLocal` at call
    time — so callers that imported `session_scope` before the startup SQLite
    fallback rebind still bind to the correct (rebound) engine.  Code that does
    `from database import AsyncSessionLocal` would instead capture the stale
    Postgres factory; always prefer `session_scope()` in long-lived modules.
    """
    session: AsyncSession = AsyncSessionLocal()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def create_all_tables() -> None:
    """
    Called once at startup.  Attempts the PostgreSQL engine first; on ANY
    connection error falls back to an in-memory SQLite engine so the system
    boots instantly out-of-the-box.  Imports models so Base.metadata is fully
    populated before create_all.
    """
    # Import models here so Base.metadata is fully populated before create_all
    from models import AadhaarDataVault, ConsentLog, HouseholdCase, User  # noqa: F401

    try:
        async with engine.begin() as conn:
            conn: AsyncConnection
            await conn.run_sync(Base.metadata.create_all)
        logger.info(
            "database: connected via %s — all tables created / verified",
            engine.url.get_backend_name(),
        )
    except Exception as exc:
        logger.exception(
            "database: PostgreSQL unavailable (%s) — falling back to in-memory SQLite", exc
        )
        sqlite_engine: AsyncEngine = _build_sqlite_engine()
        _rebind_session_factory(sqlite_engine)
        async with sqlite_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("database: in-memory SQLite fallback active — all tables created")
