"""Hybrid retrieval: vector + keyword arms -> Reciprocal Rank Fusion -> cross-encoder rerank -> floor.

retrieve_knowledge(query, filters) from spec §5. Every result carries doc/chunk IDs and per-stage
scores so answers are citable and retrieval quality is measurable.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from app.models import Evidence, RetrievalResult, RetrievalScope
from app.rag.interfaces import Embedder, Reranker

log = logging.getLogger("rag.retriever")
RRF_K = 60


class SearchStore(Protocol):
    async def vector_search(self, embedding, scope, filters, limit) -> list[dict[str, Any]]: ...
    async def keyword_search(self, query, scope, filters, limit) -> list[dict[str, Any]]: ...


def rrf_fuse(*ranked_lists: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float, dict[str, int]]]:
    """Returns (row, fused_score, {arm_index: rank}) ordered by fused score."""
    acc: dict[str, list[Any]] = {}
    for arm, rows in enumerate(ranked_lists):
        for rank, row in enumerate(rows, start=1):
            entry = acc.setdefault(row["chunk_id"], [row, 0.0, {}])
            entry[1] += 1.0 / (RRF_K + rank)
            entry[2][arm] = rank
    return sorted((tuple(v) for v in acc.values()), key=lambda t: t[1], reverse=True)  # type: ignore[misc]


class HybridRetriever:
    def __init__(
        self,
        store: SearchStore,
        embedder: Embedder,
        reranker: Reranker,
        candidates: int = 20,
        top_k: int = 5,
        min_rerank_score: float | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.candidates = candidates
        self.top_k = top_k
        self.min_rerank_score = min_rerank_score

    async def _vector_arm(self, query, scope, filters):
        emb = await self.embedder.embed_query(query)
        return await self.store.vector_search(emb, scope, filters, self.candidates)

    async def retrieve(
        self, query: str, scope: RetrievalScope, filters: dict[str, Any] | None = None, k: int | None = None
    ) -> RetrievalResult:
        k = k or self.top_k
        vec_res, kw_res = await asyncio.gather(
            self._vector_arm(query, scope, filters),
            self.store.keyword_search(query, scope, filters, self.candidates),
            return_exceptions=True,
        )
        errors = [f"{name}: {r}" for name, r in (("vector", vec_res), ("keyword", kw_res))
                  if isinstance(r, BaseException)]
        vec_rows = [] if isinstance(vec_res, BaseException) else vec_res
        kw_rows = [] if isinstance(kw_res, BaseException) else kw_res
        if errors:
            log.warning("retrieval arm failed", extra={"fields": {"errors": errors}})
        if len(errors) == 2:
            return RetrievalResult(status="failed", error="; ".join(errors))

        fused = rrf_fuse(vec_rows, kw_rows)[: self.candidates]
        if not fused:
            return RetrievalResult(status="empty", error="; ".join(errors) or None)

        texts = [f"{row['heading_path']}\n{row['content']}" for row, _, _ in fused]
        try:
            rerank_scores = await self.reranker.rerank(query, texts)
        except Exception as e:  # reranker failure degrades to fused order, never to an error
            log.warning("rerank failed; using fused order", extra={"fields": {"error": str(e)}})
            errors.append(f"rerank: {e}")
            rerank_scores = [float(len(texts) - i) for i in range(len(texts))]

        vec_score = {r["chunk_id"]: float(r["score"]) for r in vec_rows}
        kw_score = {r["chunk_id"]: float(r["score"]) for r in kw_rows}
        scored = sorted(zip(fused, rerank_scores, strict=True), key=lambda t: t[1], reverse=True)

        evidence: list[Evidence] = []
        for (row, fused_score, _ranks), rr in scored:
            if self.min_rerank_score is not None and rr < self.min_rerank_score:
                continue
            evidence.append(
                Evidence(
                    doc_id=row["doc_id"],
                    doc_title=row["doc_title"],
                    chunk_id=row["chunk_id"],
                    source=row["source"],
                    heading_path=row["heading_path"],
                    content=row["content"],
                    metadata={**(row.get("metadata") or {}), "domain": row.get("domain"),
                              "classification": row.get("classification")},
                    scores={"vector": round(vec_score.get(row["chunk_id"], 0.0), 4),
                            "keyword": round(kw_score.get(row["chunk_id"], 0.0), 4),
                            "fused": round(fused_score, 5), "rerank": round(rr, 4)},
                )
            )
            if len(evidence) >= k:
                break
        status = "partial" if errors else ("ok" if evidence else "empty")
        return RetrievalResult(status=status, evidence=evidence, error="; ".join(errors) or None)
