"""Test doubles for the LEARNED EXPERIENCE boundary. FakeMemory emulates the Hindsight properties the
design relies on: document_id upsert (same document -> replaced, not duplicated) and never-raising reads."""

from __future__ import annotations

from app.memory.interfaces import OperationStatus, RetainReceipt, RetainRequest
from app.models import KnowledgePage, LearnedItem, MemoryRecall, MemoryScope


class FakeMemory:
    def __init__(self) -> None:
        self.documents: dict[str, RetainRequest] = {}
        self.retain_calls: list[RetainRequest] = []
        self.items: list[LearnedItem] = []
        self.pages: list[KnowledgePage] = []
        self.down = False
        self.recall_queries: list[tuple[str, MemoryScope]] = []

    async def recall(self, query: str, scope: MemoryScope) -> MemoryRecall:
        self.recall_queries.append((query, scope))
        if self.down:
            return MemoryRecall(status="degraded", error="ConnectError: hindsight unreachable")
        items = [i.model_copy() for i in self.items]
        return MemoryRecall(status="ok" if items else "empty", items=items)

    async def knowledge_pages(self, scope: MemoryScope) -> list[KnowledgePage]:
        return [] if self.down else [p.model_copy() for p in self.pages]

    async def retain_case(self, req: RetainRequest) -> RetainReceipt:
        if self.down:
            raise ConnectionError("hindsight unreachable")
        self.retain_calls.append(req)
        self.documents[req.document_id] = req  # upsert by document_id, like Hindsight update_mode=replace
        return RetainReceipt(accepted=True, operation_id=req.operation_id, is_async=False)

    async def operation_status(self, bank_id: str, operation_id: str) -> OperationStatus:
        return OperationStatus(status="completed")

    async def healthy(self) -> bool:
        return not self.down
