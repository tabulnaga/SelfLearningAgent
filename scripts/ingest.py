"""Ingest official documents (idempotent: unchanged documents are skipped).

    python -m scripts.ingest --dir data/docs [--tenant default]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.config import get_settings
from app.db.migrate import migrate
from app.db.pool import close_pool, open_pool
from app.rag.embed import FastEmbedder
from app.rag.ingest.pipeline import Ingestor
from app.rag.pg_store import PgKnowledgeStore


async def main(directory: Path, tenant: str) -> None:
    s = get_settings()
    await migrate(s.database_url)
    pool = await open_pool(s.database_url)
    try:
        ingestor = Ingestor(PgKnowledgeStore(pool), FastEmbedder(s.embedding_model, s.embedding_dim),
                            s.chunk_tokens, s.chunk_overlap_tokens)
        for r in await ingestor.ingest_dir(directory, tenant):
            print(json.dumps(r))
    finally:
        await close_pool()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dir", type=Path, default=Path("data/docs"))
    p.add_argument("--tenant", default="default")
    a = p.parse_args()
    asyncio.run(main(a.dir, a.tenant))
