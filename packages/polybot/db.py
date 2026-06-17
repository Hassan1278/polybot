"""SQLAlchemy 2.x async engine + session helpers.

Resilience model:
  - pool_recycle=1800 → connections refreshed every 30 min, prevents
    "MySQL server has gone away"-style staleness after PostgreSQL restart.
  - pool_timeout=10 → instead of indefinitely blocking when pool is
    exhausted, raise TimeoutError after 10 s so the caller can react.
  - pool_pre_ping=True (default) → re-validate connection before use.
  - session_scope() retries on OperationalError up to 3 attempts with
    exponential backoff + jitter (mirroring the pattern in
    packages/polybot/clients/_http.py). DBAPI errors that are clearly
    *not* transient (constraint violation, syntax, etc.) re-raise
    immediately without retry.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from polybot.config import settings
from polybot.logging import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    pass


# psycopg v3 is detected automatically by create_async_engine when the URL uses
# the `+psycopg` driver — no extra dialect prefix needed.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    pool_recycle=1800,
    pool_timeout=10,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def _is_db_transient(exc: BaseException) -> bool:
    """OperationalError / DBAPI 'connection_invalidated' = transient.

    Constraint violations, integrity errors, programming errors are NOT
    retryable — re-raise immediately so business logic can react.
    """
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, DBAPIError):
        return bool(getattr(exc, "connection_invalidated", False))
    return False


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """`async with session_scope() as s:` — auto-commits on success, rolls back on exception.

    Acquires a session with retry on transient OperationalError (PostgreSQL
    restart, brief network hiccup, etc.). Once we have a live session, the
    inner body is NOT retried — that would risk partial side effects (Redis
    publishes, alerts) re-firing.

    Note: SessionLocal() itself is sync and lazy — it doesn't open a
    connection. To actually trigger the OperationalError inside the retry
    block we eagerly acquire a connection via `await s.connection()` and
    retry on THAT. Without this the retry was a no-op (an earlier audit
    flagged this — operational errors only fired on the first execute(),
    outside the retry, so transient blips were never recovered).
    """
    retrying = AsyncRetrying(
        retry=retry_if_exception(_is_db_transient),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.3, max=4.0),
        reraise=True,
    )
    s: AsyncSession | None = None
    async for attempt in retrying:
        with attempt:
            s = SessionLocal()
            try:
                # Force an eager connect so OperationalError surfaces here,
                # inside the retry. SessionLocal alone is lazy.
                await s.connection()
            except Exception:
                await s.close()
                raise
    assert s is not None  # retrying re-raises on exhaustion, so we got here = success
    try:
        yield s
        await s.commit()
    except Exception:
        await s.rollback()
        raise
    finally:
        await s.close()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency."""
    async with SessionLocal() as s:
        yield s
