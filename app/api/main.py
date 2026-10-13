"""HTTP API. Thin: request validation, auth, and delegation. No orchestration logic lives here (§14).

n8n / WhatsApp / webhooks call POST /v1/messages once per inbound message; each call is an
independent execution and the session is resolved from conversation_id (spec §8).
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.container import Container, build_container
from app.db.pool import close_pool
from app.models import Principal, ProposedTool
from app.observability.logging import bind, clear, setup_logging
from app.rag.ingest.parse import parse_text
from app.security.auth import authenticate, require_role

DOCS_DIR = Path(__file__).resolve().parents[2] / "data" / "docs"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    app.state.c = await build_container(settings)
    yield
    await close_pool()


app = FastAPI(title="Self-Learning Agentic RAG", version="0.1.0", lifespan=lifespan)


def container(request: Request) -> Container:
    return request.app.state.c


@app.middleware("http")
async def _log_context(request: Request, call_next):
    clear()
    bind(http_path=request.url.path)
    return await call_next(request)


# ------------------------------------------------------------------- health


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(c: Container = Depends(container)):
    checks: dict[str, object] = {}
    try:
        checks["knowledge_chunks"] = await c.knowledge.count()
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {type(e).__name__}"
    checks["hindsight"] = "ok" if await c.memory.healthy() else "unavailable (degraded mode)"
    # Hindsight is not required for readiness: the agent degrades to RAG-only (spec §17).
    ok = checks["database"] == "ok"
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


# ------------------------------------------------------------------ messages


class MessageIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=8000)
    channel: str = "api"
    user_id: str | None = None
    external_message_id: str | None = Field(default=None, description="Channel message id, for redelivery dedupe")


@app.post("/v1/messages")
async def post_message(body: MessageIn, c: Container = Depends(container),
                       principal: Principal = Depends(authenticate)):
    result = await c.orchestrator.handle_message(
        principal=principal, channel=body.channel, conversation_id=body.conversation_id, text=body.text,
        user_id=body.user_id, external_message_id=body.external_message_id,
    )
    return result.__dict__


# ------------------------------------------------------------------ sessions


async def _session_or_404(c: Container, session_id: str, principal: Principal) -> dict:
    try:
        sid = uuid.UUID(session_id)
    except ValueError as e:
        raise HTTPException(404, "session not found") from e
    s = await c.store.get_session(sid)
    if s is None or s["tenant_id"] != principal.tenant_id:
        raise HTTPException(404, "session not found")
    return s


@app.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, c: Container = Depends(container),
                      principal: Principal = Depends(authenticate)):
    s = await _session_or_404(c, session_id, principal)
    return {
        "session": {k: v for k, v in s.items() if k != "knowledge_pages"},
        "messages": await c.store.messages(s["session_id"]),
        "tool_executions": [t.model_dump() for t in await c.store.tool_records(s["session_id"])],
        "memory_job": await c.store.memory_job(s["session_id"]),
    }


@app.get("/v1/sessions/{session_id}/trace")
async def get_trace(session_id: str, c: Container = Depends(container),
                    principal: Principal = Depends(authenticate)):
    require_role(principal, "operator")
    s = await _session_or_404(c, session_id, principal)
    return {"session_id": session_id, "turns": await c.store.traces(s["session_id"]),
            "memory_job": await c.store.memory_job(s["session_id"])}


class OperatorToolIn(BaseModel):
    name: str
    arguments: dict = Field(default_factory=dict)


@app.post("/v1/sessions/{session_id}/tools")
async def operator_tool(session_id: str, body: OperatorToolIn, c: Container = Depends(container),
                        principal: Principal = Depends(authenticate)):
    """A human operator runs a tool on the case (decision D2's human path). Same registry, validation,
    policy and audit log as agent-proposed calls; the result becomes citable evidence [T#]."""
    require_role(principal, "operator")
    s = await _session_or_404(c, session_id, principal)
    if s["status"] not in ("OPEN", "ACTIVE"):
        raise HTTPException(409, f"session is {s['status']}")
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    rec = await c.orchestrator.tools.execute(
        ProposedTool(name=body.name, arguments_json=json.dumps(body.arguments)),
        lambda **kw: c.store.record_tool(session_id=s["session_id"], request_id=request_id, **kw),
    )
    await c.store.touch(s["session_id"])
    return {"cite": f"T{rec.tool_exec_id}", **rec.model_dump()}


class CloseIn(BaseModel):
    note: str | None = Field(default=None, max_length=4000, description="Operator outcome note (feeds learning)")
    team: str | None = None


async def _close(c: Container, principal: Principal, session_id: str, reason: Literal["RESOLVED", "ESCALATED"],
                 body: CloseIn):
    require_role(principal, "operator")
    s = await _session_or_404(c, session_id, principal)
    request_id = f"req_{uuid.uuid4().hex[:16]}"
    if body.note and s["status"] in ("OPEN", "ACTIVE"):
        await c.store.add_operator_message(s["session_id"], body.note, request_id)
    escalation = {"team": body.team, "by": principal.subject} if reason == "ESCALATED" else None
    changed = await c.store.close(s["session_id"], reason, note=body.note, escalation=escalation)
    current = await c.store.get_session(s["session_id"])
    return {"session_id": session_id, "changed": changed, "status": current["status"]}


@app.post("/v1/sessions/{session_id}/resolve")
async def resolve(session_id: str, body: CloseIn, c: Container = Depends(container),
                  principal: Principal = Depends(authenticate)):
    return await _close(c, principal, session_id, "RESOLVED", body)


@app.post("/v1/sessions/{session_id}/escalate")
async def escalate(session_id: str, body: CloseIn, c: Container = Depends(container),
                   principal: Principal = Depends(authenticate)):
    return await _close(c, principal, session_id, "ESCALATED", body)


# ------------------------------------------------------------- knowledge admin


class IngestIn(BaseModel):
    text: str | None = None
    source: str | None = None
    doc_id: str | None = None


@app.post("/v1/ingest")
async def ingest(body: IngestIn | None = None, c: Container = Depends(container),
                 principal: Principal = Depends(authenticate)):
    require_role(principal, "admin")
    if body and body.text:
        doc = parse_text(body.text, source=body.source or "api", doc_id=body.doc_id)
        return {"ingested": [await c.ingestor.ingest(doc, principal.tenant_id)]}
    return {"ingested": await c.ingestor.ingest_dir(DOCS_DIR, principal.tenant_id)}


# -------------------------------------------------- learned memory (analysis)


class ReflectIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.post("/v1/memory/reflect")
async def reflect(body: ReflectIn, c: Container = Depends(container), principal: Principal = Depends(authenticate)):
    """Selective reasoning over accumulated experience (spec §6). Not on the hot path. The result is
    LEARNED EXPERIENCE only - it never consults official documentation."""
    require_role(principal, "operator")
    client = getattr(c.memory, "client", None)
    if client is None:
        raise HTTPException(503, "learned memory disabled")
    try:
        r = await client.areflect(bank_id=c.settings.hindsight_bank_id, query=body.question, budget="mid",
                                  tags=[f"tenant:{principal.tenant_id}"], tags_match="all_strict")
    except Exception as e:
        raise HTTPException(503, f"learned memory unavailable: {type(e).__name__}") from e
    return {"source": "LEARNED EXPERIENCE (not official policy)", "answer": r.text}


# ------------------------------------------------------- simulation control


class SimResetIn(BaseModel):
    stalled: bool = True
    pending: int = Field(default=3, ge=0, le=500)
    restart_fixes: bool = True


@app.post("/v1/sim/reset")
async def sim_reset(body: SimResetIn, c: Container = Depends(container),
                    principal: Principal = Depends(authenticate)):
    require_role(principal, "admin")
    if c.settings.app_env != "sim":
        raise HTTPException(404)
    return await c.sim.reset(stalled=body.stalled, pending=body.pending, restart_fixes=body.restart_fixes)


@app.get("/v1/sim/state")
async def sim_state(c: Container = Depends(container), principal: Principal = Depends(authenticate)):
    if c.settings.app_env != "sim":
        raise HTTPException(404)
    return await c.sim.snapshot()
