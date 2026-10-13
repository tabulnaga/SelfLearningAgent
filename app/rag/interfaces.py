"""AUTHORITATIVE KNOWLEDGE boundary. The agent sees only `Retriever`; storage and models are hidden."""

from __future__ import annotations

from typing import Any, Protocol

from app.models import Evidence, RetrievalResult, RetrievalScope


class Embedder(Protocol):
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    async def rerank(self, query: str, docs: list[str]) -> list[float]: ...


class Retriever(Protocol):
    async def retrieve(
        self, query: str, scope: RetrievalScope, filters: dict[str, Any] | None = None, k: int | None = None
    ) -> RetrievalResult: ...


__all__ = ["Embedder", "Evidence", "Reranker", "Retriever", "RetrievalResult"]
