"""Ingestion: parse -> chunk -> metadata -> embed -> index (spec §3A). Idempotent per document."""

from __future__ import annotations

from pathlib import Path

from app.rag.ingest.chunk import chunk_document
from app.rag.ingest.parse import ParsedDocument, parse_file
from app.rag.interfaces import Embedder
from app.rag.pg_store import PgKnowledgeStore


class Ingestor:
    def __init__(self, store: PgKnowledgeStore, embedder: Embedder, chunk_tokens: int, overlap: int) -> None:
        self.store = store
        self.embedder = embedder
        self.chunk_tokens = chunk_tokens
        self.overlap = overlap

    async def ingest(self, doc: ParsedDocument, tenant_id: str) -> dict:
        chunks = chunk_document(doc.body, self.chunk_tokens, self.overlap)
        texts = [f"{doc.title}\n{c.heading_path}\n{c.content}" for c in chunks]
        embeddings = await self.embedder.embed_documents(texts)
        result = await self.store.upsert_document(doc, tenant_id, chunks, embeddings)
        return {"doc_id": doc.doc_id, "title": doc.title, "chunks": len(chunks), "result": result}

    async def ingest_dir(self, directory: Path, tenant_id: str) -> list[dict]:
        return [await self.ingest(parse_file(p), tenant_id)
                for p in sorted(directory.glob("*.md")) + sorted(directory.glob("*.txt"))]
