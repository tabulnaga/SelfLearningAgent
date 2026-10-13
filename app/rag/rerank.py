"""Cross-encoder reranking (spec §5: 'avoid simply returning the top N vector chunks')."""

from __future__ import annotations

import asyncio
from threading import Lock


class FastEmbedReranker:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None
        self._lock = Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                self._model = TextCrossEncoder(model_name=self.model_name)
        return self._model

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        return await asyncio.to_thread(lambda: [float(s) for s in self._load().rerank(query, docs)])

    def warm(self) -> None:
        self._load()


class NoopReranker:
    """Keeps fused order (scores descending by position). Used when RERANK_ENABLED=false and in tests."""

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        return [float(len(docs) - i) for i in range(len(docs))]
