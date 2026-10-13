"""Postgres/pgvector storage for AUTHORITATIVE KNOWLEDGE. Both search arms push the authorization
scope (tenant + classification) into SQL, so unauthorized chunks never leave the database."""

from __future__ import annotations

import re
from typing import Any

import numpy as np
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.models import RetrievalScope
from app.rag.ingest.chunk import Chunk
from app.rag.ingest.parse import ParsedDocument

_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "to", "of", "and", "or", "in", "on", "for", "with",
    "what", "which", "how", "do", "does", "we", "i", "you", "it", "this", "that", "should", "first", "have",
    "has", "another", "can", "my", "our", "me", "at", "by", "as", "from", "there", "any", "about",
}
_ALLOWED_FILTERS = {"domain", "doc_id"}


def or_tsquery(text: str) -> str | None:
    """Natural-language question -> OR tsquery. websearch_to_tsquery ANDs all terms, which returns
    nothing for most full questions; OR + ts_rank_cd ranks by how many terms match instead."""
    terms = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1]
    return " | ".join(dict.fromkeys(terms)) or None


class PgKnowledgeStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def upsert_document(
        self, doc: ParsedDocument, tenant_id: str, chunks: list[Chunk], embeddings: list[list[float]]
    ) -> str:
        """Idempotent: unchanged content_hash -> no-op; changed -> replace all chunks atomically."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT content_hash FROM rag_documents WHERE doc_id = %s FOR UPDATE", (doc.doc_id,)
            )
            row = await cur.fetchone()
            if row and row["content_hash"] == doc.content_hash:
                return "unchanged"
            await conn.execute("DELETE FROM rag_documents WHERE doc_id = %s", (doc.doc_id,))
            await conn.execute(
                """INSERT INTO rag_documents (doc_id, tenant_id, title, source, classification, domain,
                                              version, content_hash, metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (doc.doc_id, tenant_id, doc.title, doc.source, doc.classification, doc.domain,
                 doc.version, doc.content_hash, Jsonb(doc.metadata)),
            )
            async with conn.cursor() as cur2:
                await cur2.executemany(
                    """INSERT INTO rag_chunks (chunk_id, doc_id, tenant_id, classification, domain, ord,
                                               heading_path, content, metadata, embedding)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [
                        (f"{doc.doc_id}#{c.ord}", doc.doc_id, tenant_id, doc.classification, doc.domain,
                         c.ord, c.heading_path, c.content, Jsonb({"version": doc.version}),
                         np.asarray(e, dtype=np.float32))
                        for c, e in zip(chunks, embeddings, strict=True)
                    ],
                )
            return "replaced" if row else "inserted"

    @staticmethod
    def _where(scope: RetrievalScope, filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
        clauses = ["c.tenant_id = %s", "c.classification = ANY(%s)"]
        params: list[Any] = [scope.tenant_id, scope.classifications]
        for key, value in (filters or {}).items():
            if key not in _ALLOWED_FILTERS or value is None:
                continue
            clauses.append(f"c.{key} = %s")
            params.append(value)
        return " AND ".join(clauses), params

    _SELECT = """c.chunk_id, c.doc_id, d.title AS doc_title, d.source, c.heading_path, c.content,
                 c.metadata, c.domain, c.classification"""

    async def vector_search(
        self, embedding: list[float], scope: RetrievalScope, filters: dict[str, Any] | None, limit: int
    ) -> list[dict[str, Any]]:
        where, params = self._where(scope, filters)
        vec = np.asarray(embedding, dtype=np.float32)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT {self._SELECT}, 1 - (c.embedding <=> %s) AS score
                    FROM rag_chunks c JOIN rag_documents d USING (doc_id)
                    WHERE {where} ORDER BY c.embedding <=> %s LIMIT %s""",
                [vec, *params, vec, limit],
            )
            return await cur.fetchall()

    async def keyword_search(
        self, query: str, scope: RetrievalScope, filters: dict[str, Any] | None, limit: int
    ) -> list[dict[str, Any]]:
        tsq = or_tsquery(query)
        if not tsq:
            return []
        where, params = self._where(scope, filters)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""SELECT {self._SELECT}, ts_rank_cd(c.tsv, q) AS score
                    FROM rag_chunks c JOIN rag_documents d USING (doc_id),
                         to_tsquery('english', %s) q
                    WHERE c.tsv @@ q AND {where} ORDER BY score DESC LIMIT %s""",
                [tsq, *params, limit],
            )
            return await cur.fetchall()

    async def count(self) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) AS n FROM rag_chunks")
            return (await cur.fetchone())["n"]
