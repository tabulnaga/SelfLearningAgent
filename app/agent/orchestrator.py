"""Deterministic orchestrator (spec §4, §14; architecture §3). A fixed sequence of named steps:

  load_session -> authorize -> understand -> [retrieve_official || recall_learned] -> build_context
  -> reason -> (run_tools -> reason)* -> verify (-> regenerate once) -> render -> persist_turn

Every step is traced. Memory failures degrade, never fail the turn. Session closure and memory
writing happen elsewhere (closure endpoints, SessionMonitor, MemoryWriter) - never inline here.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.agent import prompts
from app.agent.context import assign_ids, render_history, render_learned, render_official, render_tools
from app.agent.renderer import render_answer, render_fallback
from app.llm.interfaces import LLMClient, LLMError
from app.memory.interfaces import LearnedMemory
from app.models import (
    DraftAnswer,
    KnowledgePage,
    MemoryRecall,
    Principal,
    RetrievalResult,
    Understanding,
)
from app.observability.logging import bind
from app.observability.trace import TurnTrace
from app.rag.interfaces import Retriever
from app.security.authorizer import Authorizer
from app.sessions.store import SessionStore
from app.tools.registry import ToolRegistry
from app.verification.checks import TurnContext
from app.verification.verifier import Verifier

log = logging.getLogger("agent.orchestrator")

_SMALLTALK = {"greeting", "smalltalk"}


@dataclass
class TurnResult:
    request_id: str
    conversation_id: str
    session_id: str
    session_status: str
    answer: str
    intent: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    duplicate: bool = False


class Orchestrator:
    def __init__(
        self,
        *,
        store: SessionStore,
        retriever: Retriever,
        memory: LearnedMemory,
        llm: LLMClient,
        tools: ToolRegistry,
        verifier: Verifier,
        authorizer: Authorizer,
        max_tool_rounds: int = 2,
    ) -> None:
        self.store = store
        self.retriever = retriever
        self.memory = memory
        self.llm = llm
        self.tools = tools
        self.verifier = verifier
        self.authorizer = authorizer
        self.max_tool_rounds = max_tool_rounds

    # ---------------------------------------------------------------- entry point

    async def handle_message(
        self,
        *,
        principal: Principal,
        channel: str,
        conversation_id: str,
        text: str,
        user_id: str | None = None,
        external_message_id: str | None = None,
    ) -> TurnResult:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        bind(request_id=request_id, conversation_id=conversation_id, tenant_id=principal.tenant_id)

        # step 0: ingress dedupe - a webhook redelivery gets the stored reply, no second execution
        if external_message_id:
            if not await self.store.claim_inbound(channel, external_message_id, request_id):
                prior = await self.store.find_inbound(channel, external_message_id)
                if prior and prior.get("response"):
                    return TurnResult(**{**prior["response"], "duplicate": True})
                return TurnResult(request_id=prior["request_id"] if prior else request_id,
                                  conversation_id=conversation_id, session_id="", session_status="PROCESSING",
                                  answer="This message is already being processed.", duplicate=True)

        async with self.store.turn_lock(conversation_id):
            result = await self._turn(request_id, principal, channel, conversation_id, text, user_id)
        if external_message_id:
            await self.store.complete_inbound(channel, external_message_id, result.session_id,
                                              result.__dict__)
        return result

    # --------------------------------------------------------------------- turn

    async def _turn(self, request_id, principal, channel, conversation_id, text, user_id) -> TurnResult:
        trace = TurnTrace(request_id)
        trace.set(conversation_id=conversation_id, query=text, prompt_version=prompts.PROMPT_VERSION,
                  user_id=user_id, tenant_id=principal.tenant_id)
        notes: list[str] = []
        degraded: list[str] = []

        # 1. load_session (write-ahead: user message committed before any reasoning)
        with trace.step("load_session") as s:
            session = await self.store.begin_turn(conversation_id=conversation_id, tenant_id=principal.tenant_id,
                                                  channel=channel, user_id=user_id, message=text,
                                                  request_id=request_id)
            session_id = session["session_id"]
            bind(session_id=str(session_id))
            s.update(session_id=str(session_id), new_session=session["is_new"], turn=session["turn_count"])
        trace.set(session_id=str(session_id))
        history = await self.store.messages(session_id, limit=12)
        prior_tools = await self.store.tool_records(session_id)

        # 2. authorize - deterministic, before anything reaches the model
        with trace.step("authorize") as s:
            r_scope = self.authorizer.retrieval_scope(principal)
            s.update(classifications=r_scope.classifications)

        # 3. understand
        with trace.step("understand") as s:
            understanding = await self._understand(text, history, trace)
            s.update(intent=understanding.intent, domain=understanding.domain,
                     search_query=understanding.search_query)
        domain = understanding.domain if understanding.domain != "general" else (session.get("domain") or "general")
        m_scope = self.authorizer.memory_scope(principal, domain)

        if understanding.intent in _SMALLTALK and session["turn_count"] <= 1:
            answer = ("Hello. Describe the issue you are investigating and I will check the documentation "
                      "and past cases.")
            return await self._finish(trace, session, conversation_id, answer, understanding, {}, {}, degraded)

        # 4. retrieve_official || recall_learned (concurrently; each fails independently)
        query = understanding.search_query or text
        filters = {"domain": domain} if domain in ("payments", "billing") else None
        with trace.step("retrieve_and_recall") as s:
            retrieval, recall, pages = await asyncio.gather(
                self._retrieve(query, r_scope, filters),
                self.memory.recall(query, m_scope),
                self._pages(session, m_scope),
            )
            s.update(
                rag_status=retrieval.status,
                retrieval=[{"chunk_id": e.chunk_id, "doc_id": e.doc_id, "scores": e.scores}
                           for e in retrieval.evidence],
                memory_status=recall.status,
                memory=[{"memory_id": m.memory_id, "type": m.type, "support": m.proof_count}
                        for m in recall.items],
                pages=[p.name for p in pages],
            )
        if retrieval.status in ("failed", "partial"):
            degraded.append(f"rag:{retrieval.status}")
        if retrieval.status == "failed":
            notes.append("The documentation search is unavailable right now; this answer is not based on it.")
        elif retrieval.status == "empty":
            notes.append("The official documentation does not appear to cover this question.")
        if recall.status == "degraded":
            degraded.append("memory:degraded")
            notes.append("Past-case memory was unavailable; this answer uses official documentation only.")

        # 5. build_context
        ctx = assign_ids(retrieval.evidence, recall.items, pages, prior_tools)
        ctx.user_confirmed_resolution = understanding.intent == "confirm_resolved"
        trace.set(intent=understanding.intent, domain=domain)

        # 6-8. reason -> tools -> verify (bounded)
        draft, verdict = await self._reason_tools_verify(
            trace, text, history, ctx, retrieval.status, recall.status, session_id, request_id)

        # 9. render (code arranges claims into provenance sections)
        with trace.step("render") as s:
            if draft is None:
                answer = render_fallback(ctx, "the reasoning service failed", notes)
                s.update(mode="fallback")
            elif not verdict["passed"]:
                answer = render_fallback(ctx, "verification failed", notes)
                s.update(mode="fallback")
            else:
                answer = render_answer(draft, ctx, notes)
                s.update(mode="verified")

        # Explicit closure signals from the user (architecture §4). The actual close is the store's
        # transactional close + outbox; memory writing happens later in the worker.
        closed = None
        if understanding.intent == "confirm_resolved" and session["turn_count"] > 1:
            if await self.store.close(session_id, "RESOLVED", note="User confirmed the issue is resolved."):
                closed = "RESOLVED"
        elif understanding.intent == "request_escalation":
            if await self.store.close(session_id, "ESCALATED", note="User requested escalation.",
                                      escalation={"requested_by": "user", "request_id": request_id}):
                closed = "ESCALATED"
        if closed:
            answer += f"\n\nThis case is now marked {closed}."
            trace.set(closed=closed)

        used = {c for claim in (draft.claims if draft else []) for c in claim.cites}
        sources = {
            "official": [{"cite": k, "doc_id": v.doc_id, "chunk_id": v.chunk_id, "title": v.doc_title}
                         for k, v in ctx.official.items() if k in used],
            "learned": [{"cite": k, "memory_id": v.memory_id, "type": v.type}
                        for k, v in ctx.learned.items() if k in used]
            + [{"cite": k, "page": v.name} for k, v in ctx.pages.items() if k in used],
            "tools": [{"cite": k, "tool": v.tool_name, "status": v.status}
                      for k, v in ctx.tools.items() if k in used],
        }
        return await self._finish(trace, session, conversation_id, answer, understanding, verdict, sources,
                                  degraded, status=closed)

    # ------------------------------------------------------------------- steps

    async def _understand(self, text: str, history: list[dict], trace: TurnTrace) -> Understanding:
        user = f"{render_history(history[:-1])}\n<message>{text}</message>"
        try:
            out, usage = await self.llm.complete_structured(system=prompts.UNDERSTAND_SYSTEM, user=user,
                                                            schema=Understanding, purpose="understand")
            trace.add_usage(usage.input_tokens, usage.output_tokens)
            return out
        except LLMError as e:
            log.warning("understand failed; rule fallback", extra={"fields": {"error": str(e)}})
            low = text.lower()
            domain = "payments" if any(w in low for w in ("payment", "pending", "consumer", "psp")) else "general"
            return Understanding(intent="incident" if "pending" in low else "how_to", domain=domain,
                                 search_query=text)

    async def _retrieve(self, query, scope, filters) -> RetrievalResult:
        try:
            return await self.retriever.retrieve(query, scope, filters)
        except Exception as e:  # defensive: the retriever already degrades per arm
            return RetrievalResult(status="failed", error=f"{type(e).__name__}: {e}")

    async def _pages(self, session: dict, scope) -> list[KnowledgePage]:
        """Knowledge pages are read once per session (boot) and cached on the session row."""
        cached = session.get("knowledge_pages")
        if cached is not None:
            return [KnowledgePage.model_validate(p) for p in cached]
        pages = await self.memory.knowledge_pages(scope)
        await self.store.cache_pages(session["session_id"], [p.model_dump() for p in pages])
        return pages

    async def _reason(self, trace, text, history, ctx: TurnContext, rag_status, mem_status,
                      allow_tools: bool, feedback: list[str] | None) -> DraftAnswer:
        blocks = [
            render_history(history[:-1]),
            f"<message>{text}</message>",
            render_official(ctx, rag_status),
            render_learned(ctx, mem_status),
            render_tools(ctx),
        ]
        if allow_tools:
            blocks.append(f"<tools_available>\n{self.tools.describe()}\n</tools_available>")
        else:
            blocks.append("<tools_available>(no further tool calls this turn)</tools_available>")
        if feedback:
            blocks.append("<verification_feedback>\nYour previous answer failed verification. Fix:\n- "
                          + "\n- ".join(feedback) + "\n</verification_feedback>")
        out, usage = await self.llm.complete_structured(system=prompts.REASON_SYSTEM, user="\n\n".join(blocks),
                                                        schema=DraftAnswer, purpose="reason")
        trace.add_usage(usage.input_tokens, usage.output_tokens)
        return out

    async def _reason_tools_verify(self, trace, text, history, ctx, rag_status, mem_status, session_id,
                                   request_id) -> tuple[DraftAnswer | None, dict]:
        draft: DraftAnswer | None = None
        rounds = 0
        try:
            with trace.step("reason") as s:
                draft = await self._reason(trace, text, history, ctx, rag_status, mem_status,
                                           allow_tools=self.max_tool_rounds > 0, feedback=None)
                s.update(claims=len(draft.claims), proposed_tools=[p.name for p in draft.proposed_tools])
            while draft.proposed_tools and rounds < self.max_tool_rounds:
                rounds += 1
                with trace.step("run_tools", round=rounds) as s:
                    executed = []
                    for proposal in draft.proposed_tools[:3]:
                        rec = await self.tools.execute(
                            proposal,
                            lambda **kw: self.store.record_tool(session_id=session_id, request_id=request_id, **kw),
                        )
                        ctx.tools[f"T{rec.tool_exec_id}"] = rec
                        executed.append({"cite": f"T{rec.tool_exec_id}", "tool": rec.tool_name,
                                         "input": rec.input, "status": rec.status, "error": rec.error})
                    s.update(executed=executed)
                with trace.step("reason", after_tools=rounds) as s:
                    draft = await self._reason(trace, text, history, ctx, rag_status, mem_status,
                                               allow_tools=rounds < self.max_tool_rounds, feedback=None)
                    s.update(claims=len(draft.claims), proposed_tools=[p.name for p in draft.proposed_tools])
            if draft.proposed_tools:
                trace.set(ignored_tool_proposals=[p.name for p in draft.proposed_tools])
                draft.proposed_tools = []

            with trace.step("verify") as s:
                verdict = self.verifier.verify(draft, ctx)
                s.update(**verdict.summary())
            if not verdict.passed:
                with trace.step("regenerate") as s:
                    draft = await self._reason(trace, text, history, ctx, rag_status, mem_status,
                                               allow_tools=False, feedback=verdict.problems)
                    draft.proposed_tools = []
                with trace.step("verify", attempt=2) as s:
                    verdict = self.verifier.verify(draft, ctx)
                    s.update(**verdict.summary())
            summary = verdict.summary()
            trace.set(verification=summary)
            return draft, summary
        except LLMError as e:
            trace.set(verification={"passed": False, "failed_checks": ["llm_error"], "problems": [str(e)]})
            return None, {"passed": False, "failed_checks": ["llm_error"], "problems": [str(e)]}

    async def _finish(self, trace, session, conversation_id, answer, understanding, verdict, sources, degraded,
                      status: str | None = None) -> TurnResult:
        trace.set(final_outcome=status or "answered", degraded=degraded, sources=sources)
        with trace.step("persist_turn"):
            await self.store.finish_turn(
                session_id=session["session_id"], request_id=trace.request_id, conversation_id=conversation_id,
                answer=answer, trace=trace.to_json(), intent=understanding.intent, domain=understanding.domain,
                answer_meta={"verification": verdict, "sources": sources, "degraded": degraded},
            )
        return TurnResult(
            request_id=trace.request_id, conversation_id=conversation_id, session_id=str(session["session_id"]),
            session_status=status or "ACTIVE", answer=answer, intent=understanding.intent,
            verification=verdict, sources=sources, degraded=degraded,
        )


__all__ = ["MemoryRecall", "Orchestrator", "TurnResult"]
