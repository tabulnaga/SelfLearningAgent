"""Shared domain types that cross package boundaries. Packages depend on these, not on each other."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ security


class Principal(BaseModel):
    subject: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)


class RetrievalScope(BaseModel):
    """Computed deterministically by the Authorizer BEFORE retrieval (spec §20)."""

    tenant_id: str
    classifications: list[str]


class MemoryScope(BaseModel):
    bank_id: str
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------- AUTHORITATIVE KNOWLEDGE


class Evidence(BaseModel):
    """One authoritative chunk. Cited as [D:<cite_id>]."""

    cite_id: str = ""
    doc_id: str
    doc_title: str
    chunk_id: str
    source: str
    heading_path: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    scores: dict[str, float] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    status: Literal["ok", "partial", "empty", "failed"]
    evidence: list[Evidence] = Field(default_factory=list)
    error: str | None = None


# -------------------------------------------------------- LEARNED EXPERIENCE


class LearnedItem(BaseModel):
    """One Hindsight memory. Cited as [M:<cite_id>]."""

    cite_id: str = ""
    memory_id: str
    type: str  # observation | experience | world
    text: str
    proof_count: int | None = None


class KnowledgePage(BaseModel):
    """A Hindsight knowledge page: LEARNED, despite the name. Cited as [P:<cite_id>]."""

    cite_id: str = ""
    page_id: str
    name: str
    content: str


class MemoryRecall(BaseModel):
    status: Literal["ok", "empty", "degraded", "disabled"]
    items: list[LearnedItem] = Field(default_factory=list)
    error: str | None = None


# ------------------------------------------------------------- WORKING STATE


class SessionStatus(StrEnum):
    OPEN = "OPEN"
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"
    TIMED_OUT = "TIMED_OUT"
    CLOSED = "CLOSED"


LIVE_STATUSES = (SessionStatus.OPEN, SessionStatus.ACTIVE)


class ToolRecord(BaseModel):
    """A persisted tool execution. Cited as [T:<tool_exec_id>]."""

    tool_exec_id: int
    tool_name: str
    side_effect: Literal["read", "write"]
    input: dict[str, Any]
    output: dict[str, Any] | None = None
    status: Literal["success", "error", "denied"]
    error: str | None = None


# ------------------------------------------------------------- agent output


class Claim(BaseModel):
    text: str
    kind: Literal["official", "learned", "tool", "general"]
    cites: list[str] = Field(default_factory=list, description="e.g. ['D1','M2','T14']")


class ProposedTool(BaseModel):
    name: str
    # A JSON string, not a dict: structured outputs close free-form objects (additionalProperties=false
    # with no properties), which would make arguments impossible to express. Each tool's own input
    # model validates the parsed value in code.
    arguments_json: str = Field(default="{}", description='JSON object of tool arguments, e.g. {"name": "x"}')
    reason: str = ""


class DraftAnswer(BaseModel):
    claims: list[Claim] = Field(default_factory=list)
    conflicts: list[str] = Field(
        default_factory=list, description="Where learned experience disagrees with official knowledge."
    )
    proposed_tools: list[ProposedTool] = Field(default_factory=list)
    proposes_resolution: bool = False
    next_step: str = ""


class Understanding(BaseModel):
    intent: Literal[
        "incident", "how_to", "faq_only", "greeting", "smalltalk", "confirm_resolved", "request_escalation"
    ]
    domain: Literal["payments", "billing", "network", "crm", "general"] = "general"
    search_query: str
    is_smalltalk: bool = False


# ------------------------------------------------------------- memory writer


class CaseNarrative(BaseModel):
    """The LLM-written part of a case record. Everything else is derived from tool logs."""

    issue: str
    root_cause: str | None = None
    lessons: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class CaseRecord(BaseModel):
    """Spec §11 output, validated before retain."""

    session_id: str
    conversation_id: str
    tenant_id: str
    domain: str
    closure_reason: str
    closed_at: str
    issue: str
    root_cause: str | None = None
    actions_attempted: list[str] = Field(default_factory=list)
    failed_actions: list[str] = Field(default_factory=list)
    successful_action: str | None = None
    outcome: Literal["resolved", "escalated", "unresolved"]
    lessons: list[str] = Field(default_factory=list)
    confidence: float = 0.0
