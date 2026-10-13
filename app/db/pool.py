"""Async Postgres pool. One pool per process; pgvector types registered on every connection."""

from __future__ import annotations

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def _configure(conn: AsyncConnection) -> None:
    try:
        await register_vector_async(conn)
    except Exception:  # extension not yet created (first migration run)
        pass
    await conn.set_autocommit(False)


async def open_pool(dsn: str, max_size: int = 10) -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row},
            configure=_configure,
            open=False,
        )
        await _pool.open(wait=True, timeout=30)
    return _pool


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("database pool not opened")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
