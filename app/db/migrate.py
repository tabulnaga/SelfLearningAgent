"""Minimal migration runner: applies db/migrations/NNNN_*.sql in order, once each, under an advisory lock.

Usage: python -m app.db.migrate
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_LOCK_KEY = 7_420_001


async def migrate(dsn: str) -> list[str]:
    applied: list[str] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            cur = await conn.execute("SELECT version FROM schema_migrations")
            done = {r[0] for r in await cur.fetchall()}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.stem in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text(encoding="utf-8"))
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
                applied.append(path.stem)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied


def main() -> None:
    applied = asyncio.run(migrate(get_settings().database_url))
    print(f"applied: {applied or 'none (up to date)'}")


if __name__ == "__main__":
    main()
