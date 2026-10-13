"""Hindsight adapter (LEARNED EXPERIENCE). Uses the async client methods only: the client's sync
wrappers drive their own event loop, which does not mix with an async server.

Read-side contract: recall() and knowledge_pages() never raise. A slow or failing Hindsight yields
status=degraded and the turn continues on authoritative knowledge alone (spec §17).
"""

from __future__ import annotations

import asyncio
import logging

from hindsight_client import Hindsight

from app.memory.interfaces import OperationStatus, RetainReceipt, RetainRequest
from app.models import KnowledgePage, LearnedItem, MemoryRecall, MemoryScope

log = logging.getLogger("memory.hindsight")

# Observations first: settled, multi-case beliefs outrank single raw facts (architecture §6.2).
_TYPE_ORDER = {"observation": 0, "experience": 1, "world": 2}


class HindsightMemory:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout_s: float,
        budget: str = "low",
        max_tokens: int = 2048,
        max_items: int = 8,
    ) -> None:
        # Request timeout is generous (retain can be slow); the hot path is bounded by timeout_s below.
        self.client = Hindsight(base_url=base_url, api_key=api_key, timeout=60.0)
        self.timeout_s = timeout_s
        self.budget = budget
        self.max_tokens = max_tokens
        self.max_items = max_items

    async def recall(self, query: str, scope: MemoryScope) -> MemoryRecall:
        try:
            resp = await asyncio.wait_for(
                self.client.arecall(
                    bank_id=scope.bank_id,
                    query=query,
                    types=["observation", "experience", "world"],
                    prefer_observations=True,  # only effective because observation + raw types are both requested
                    tags=scope.tags or None,
                    tags_match="all_strict" if scope.tags else "any",
                    budget=self.budget,
                    max_tokens=self.max_tokens,
                ),
                timeout=self.timeout_s,
            )
        except Exception as e:
            log.warning("hindsight recall degraded", extra={"fields": {"error": f"{type(e).__name__}: {e}"}})
            return MemoryRecall(status="degraded", error=f"{type(e).__name__}: {e}")

        items = [
            LearnedItem(
                memory_id=r.id,
                type=r.type or "unknown",
                text=r.text,
                proof_count=len(r.source_fact_ids) if r.source_fact_ids else None,
            )
            for r in resp.results
        ]
        items.sort(key=lambda i: _TYPE_ORDER.get(i.type, 9))
        items = items[: self.max_items]
        return MemoryRecall(status="ok" if items else "empty", items=items)

    async def knowledge_pages(self, scope: MemoryScope) -> list[KnowledgePage]:
        """Session-boot read of standing knowledge. DB reads on Hindsight's side; no LLM."""
        try:
            tree = await asyncio.wait_for(self.client.aget_knowledge_base_tree(bank_id=scope.bank_id),
                                          timeout=self.timeout_s)
            page_nodes = []
            stack = list(tree.roots or [])
            while stack:
                node = stack.pop()
                if node.kind == "folder":
                    stack.extend(node.children or [])
                else:
                    page_nodes.append(node)
            pages: list[KnowledgePage] = []
            for node in page_nodes:
                page = await asyncio.wait_for(
                    self.client.aget_knowledge_page(bank_id=scope.bank_id, page_id=node.id), timeout=self.timeout_s
                )
                body = (page.body or "").strip()
                if body:
                    pages.append(KnowledgePage(page_id=page.id, name=page.name, content=body))
            return pages
        except Exception as e:
            log.warning("knowledge pages unavailable", extra={"fields": {"error": f"{type(e).__name__}: {e}"}})
            return []

    async def retain_case(self, req: RetainRequest) -> RetainReceipt:
        """Raises on failure: the Memory Writer owns retries (outbox + backoff)."""
        resp = await self.client.aretain_batch(
            bank_id=req.bank_id,
            items=[{
                "content": req.content,
                "context": req.context,
                "tags": req.tags,
                "metadata": req.metadata,
                "timestamp": req.timestamp,
                "observation_scopes": req.observation_scopes,
                "update_mode": "replace",
            }],
            document_id=req.document_id,
            retain_async=req.retain_async,
            operation_id=req.operation_id if req.retain_async else None,
        )
        return RetainReceipt(accepted=bool(resp.success), operation_id=resp.operation_id,
                             is_async=bool(resp.var_async))

    async def operation_status(self, bank_id: str, operation_id: str) -> OperationStatus:
        try:
            op = await self.client.operations.get_operation_status(bank_id, operation_id)
            return OperationStatus(status=op.status, error=op.error_message)
        except Exception as e:
            if "404" in str(e):
                return OperationStatus(status="not_found", error=str(e))
            return OperationStatus(status="unknown", error=f"{type(e).__name__}: {e}")

    async def healthy(self) -> bool:
        try:
            await asyncio.wait_for(self.client.aget_version(), timeout=3)
            return True
        except Exception:
            return False


class DisabledMemory:
    """HINDSIGHT_ENABLED=false: the agent runs on authoritative knowledge only."""

    async def recall(self, query: str, scope: MemoryScope) -> MemoryRecall:
        return MemoryRecall(status="disabled")

    async def knowledge_pages(self, scope: MemoryScope) -> list[KnowledgePage]:
        return []

    async def retain_case(self, req: RetainRequest) -> RetainReceipt:
        raise RuntimeError("Hindsight disabled")

    async def operation_status(self, bank_id: str, operation_id: str) -> OperationStatus:
        return OperationStatus(status="unknown")

    async def healthy(self) -> bool:
        return False
