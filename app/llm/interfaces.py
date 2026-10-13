"""LLM provider boundary. Callers get a validated Pydantic object or an LLMError, never raw text."""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class LLMError(Exception):
    """Provider failure after the SDK's own retries (timeouts, 5xx, refusal, unparseable output)."""


class LLMClient(Protocol):
    async def complete_structured(
        self, *, system: str, user: str, schema: type[T], purpose: str
    ) -> tuple[T, Usage]: ...
