"""Embedders. FastEmbed (ONNX, no torch) is the default (decision D4: local, no data leaves the machine).
HashingEmbedder is deterministic and dependency-free, for unit tests only."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from threading import Lock


class FastEmbedder:
    def __init__(self, model_name: str, dim: int) -> None:
        self.model_name = model_name
        self.dim = dim
        self._model = None
        self._lock = Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding

                self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        def run():
            return [v.tolist() for v in self._load().embed(texts)]

        return await asyncio.to_thread(run)

    async def embed_query(self, text: str) -> list[float]:
        def run():
            return next(iter(self._load().query_embed(text))).tolist()

        return await asyncio.to_thread(run)

    def warm(self) -> None:
        self._load()


class HashingEmbedder:
    """Bag-of-words feature hashing. Similar texts -> similar vectors; good enough for plumbing tests."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 8) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)
