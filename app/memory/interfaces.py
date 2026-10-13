"""LEARNED EXPERIENCE boundary. Read side (recall, pages) never raises; write side (retain_case) is
used only by the Memory Writer and is idempotent per case."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.models import KnowledgePage, MemoryRecall, MemoryScope


class RetainRequest(BaseModel):
    bank_id: str
    document_id: str
    content: str
    context: str
    tags: list[str]
    metadata: dict[str, str]
    timestamp: str
    observation_scopes: list[list[str]]
    operation_id: str
    retain_async: bool = True


class RetainReceipt(BaseModel):
    accepted: bool
    operation_id: str | None = None
    is_async: bool = True
    detail: str = ""


class OperationStatus(BaseModel):
    status: str  # pending | processing | completed | failed | not_found | unknown
    error: str | None = None


class LearnedMemory(Protocol):
    async def recall(self, query: str, scope: MemoryScope) -> MemoryRecall: ...
    async def knowledge_pages(self, scope: MemoryScope) -> list[KnowledgePage]: ...
    async def retain_case(self, req: RetainRequest) -> RetainReceipt: ...
    async def operation_status(self, bank_id: str, operation_id: str) -> OperationStatus: ...
    async def healthy(self) -> bool: ...
