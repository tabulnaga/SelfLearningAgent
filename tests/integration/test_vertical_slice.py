"""Phase 3-5 integration tests: the vertical slice, the learning loop plumbing, and failure paths
(architecture §8). Real Postgres; deterministic LLM and memory fakes."""

import json
import uuid

import pytest

from app.models import LearnedItem, ProposedTool
from tests.conftest import ADMIN, DOCS, USER

pytestmark = pytest.mark.integration


async def ask(c, text, conv="conv-1", principal=USER, **kw):
    return await c.orchestrator.handle_message(principal=principal, channel="api", conversation_id=conv,
                                               text=text, **kw)


async def operator_tool(c, session_id, name, **args):
    return await c.orchestrator.tools.execute(
        ProposedTool(name=name, arguments_json=json.dumps(args)),
        lambda **kw: c.store.record_tool(session_id=uuid.UUID(session_id), request_id="op", **kw))


# ------------------------------------------------------------------ retrieval


async def test_ingest_is_idempotent(c):
    again = await c.ingestor.ingest_dir(DOCS, "default")
    assert {r["result"] for r in again} == {"unchanged"}


async def test_retrieval_respects_authorization(c):
    user_scope = c.orchestrator.authorizer.retrieval_scope(USER)
    admin_scope = c.orchestrator.authorizer.retrieval_scope(ADMIN)
    q = "payment pending confirmations service levels contract"
    user_docs = {e.doc_id for e in (await c.retriever.retrieve(q, user_scope)).evidence}
    admin_docs = {e.doc_id for e in (await c.retriever.retrieve(q, admin_scope)).evidence}
    assert "CON-PSP-001" not in user_docs, "confidential doc leaked to agent_user"
    assert "CON-PSP-001" in admin_docs


async def test_retrieval_finds_runbook(c):
    scope = c.orchestrator.authorizer.retrieval_scope(USER)
    r = await c.retriever.retrieve("payment pending investigate", scope, {"domain": "payments"})
    assert r.status == "ok"
    assert "RB-PAY-001" in {e.doc_id for e in r.evidence}
    assert all(e.chunk_id and e.scores for e in r.evidence)


# ---------------------------------------------------------------- vertical slice


async def test_question_returns_cited_official_answer_and_persists_state(c):
    res = await ask(c, "How do I investigate payments stuck in pending?")
    assert res.verification["passed"], res.verification
    assert "From official documentation" in res.answer and "[D1] OFFICIAL" in res.answer
    s = await c.store.get_session(uuid.UUID(res.session_id))
    assert s["status"] == "ACTIVE" and s["turn_count"] == 1
    msgs = await c.store.messages(s["session_id"])
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    (trace,) = await c.store.traces(s["session_id"])
    steps = [st["step"] for st in trace["trace"]["steps"]]
    assert steps[:4] == ["load_session", "authorize", "understand", "retrieve_and_recall"]
    assert trace["trace"]["steps"][3]["retrieval"], "retrieval IDs/scores must be traced"


async def test_learned_experience_is_labelled_separately(c):
    c.memory.items = [LearnedItem(memory_id="obs-1", type="observation", proof_count=3,
                                  text="Payment Pending incidents were repeatedly resolved by restarting "
                                       "the consumer.")]
    res = await ask(c, "Payments are pending, how do I investigate?")
    a = res.answer
    assert a.index("From official documentation") < a.index("From past cases (learned experience, not official policy)")
    assert "[M1] LEARNED - observation obs-1" in a
    assert res.sources["learned"] and res.sources["official"]


async def test_follow_up_message_stays_in_same_session(c):
    r1 = await ask(c, "Payments are pending")
    r2 = await ask(c, "What about the consumer lag?")
    assert r1.session_id == r2.session_id


async def test_agent_tool_call_is_recorded_and_cited(c):
    res = await ask(c, "Payments pending again - what is happening?")
    tools = await c.store.tool_records(uuid.UUID(res.session_id))
    assert [t.tool_name for t in tools] == ["check_consumer_status"]
    assert tools[0].status == "success" and tools[0].output["state"] == "stalled"


async def test_webhook_redelivery_is_deduplicated(c):
    r1 = await ask(c, "Payments are pending", external_message_id="wa-123")
    r2 = await ask(c, "Payments are pending", external_message_id="wa-123")
    assert r2.duplicate and r2.request_id == r1.request_id
    msgs = await c.store.messages(uuid.UUID(r1.session_id))
    assert sum(m["role"] == "user" for m in msgs) == 1


# ---------------------------------------------------------------- failure paths


async def test_memory_down_degrades_without_failing(c):
    c.memory.down = True
    res = await ask(c, "How do I investigate payments stuck in pending?")
    assert "memory:degraded" in res.degraded
    assert "Past-case memory was unavailable" in res.answer
    assert "From official documentation" in res.answer


async def test_llm_down_gives_controlled_fallback(c):
    c.llm.fail_purposes.update({"understand", "reason"})
    res = await ask(c, "How do I investigate payments stuck in pending?")
    assert res.answer.startswith("I could not produce a verified answer")
    assert "[D1]" in res.answer  # official excerpts, no synthesis
    assert not res.verification["passed"]


async def test_unverifiable_answer_falls_back(c):
    c.llm.overrides["reason"] = lambda _u: {"claims": [{"text": "Policy says restart.", "kind": "official",
                                                        "cites": ["D99"]}]}
    res = await ask(c, "How do I investigate payments stuck in pending?")
    assert "V1_citations_valid" in res.verification["failed_checks"]
    assert res.answer.startswith("I could not produce a verified answer")
    assert c.llm.calls.count("reason") == 2  # one regeneration, then fallback


async def test_failed_tool_is_never_reported_as_success(c):
    res = await ask(c, "Payments are pending")
    rec = await operator_tool(c, res.session_id, "retry_payment", payment_id="PAY-1000")
    assert rec.status == "error" and "still pending" in rec.error


# ---------------------------------------------------------------- learning loop


async def resolved_case(c, conv):
    res = await ask(c, "Payments are stuck in pending", conv=conv)
    await operator_tool(c, res.session_id, "retry_payment", payment_id="PAY-1000")
    await operator_tool(c, res.session_id, "restart_consumer", consumer="payment-events")
    sid = uuid.UUID(res.session_id)
    assert await c.store.close(sid, "RESOLVED", note="Consumer stalled; restart fixed it.")
    return sid


async def test_resolved_case_is_retained_once(c, runner):
    sid = await resolved_case(c, "conv-a")
    assert not await c.store.close(sid, "RESOLVED")  # second close is a no-op: no second job
    await runner.run_once()
    await runner.run_once()
    assert len(c.memory.retain_calls) == 1
    req = c.memory.retain_calls[0]
    assert req.document_id == f"case-{sid}"
    assert "What resolved it: restart_consumer" in req.content and "Did not work: retry_payment" in req.content
    job = await c.store.memory_job(sid)
    assert job["status"] == "done"
    assert (await c.store.get_session(sid))["status"] == "CLOSED"


async def test_retry_after_failure_does_not_duplicate(c, runner):
    sid = await resolved_case(c, "conv-b")
    c.memory.down = True
    await runner.run_once()
    job = await c.store.memory_job(sid)
    assert job["status"] == "pending" and job["attempts"] == 1 and "unreachable" in job["last_error"]
    c.memory.down = False
    async with c.pool.connection() as conn:
        await conn.execute("UPDATE memory_jobs SET next_attempt_at=now() WHERE session_id=%s", (sid,))
        await conn.commit()
    await runner.run_once()
    assert len(c.memory.documents) == 1 and (await c.store.memory_job(sid))["status"] == "done"


async def test_greeting_only_session_is_not_retained(c, runner):
    res = await ask(c, "hello", conv="conv-noise")
    sid = uuid.UUID(res.session_id)
    await c.store.close(sid, "RESOLVED")
    await runner.run_once()
    job = await c.store.memory_job(sid)
    assert job["status"] == "skipped" and job["reason"].startswith("R1")
    assert c.memory.retain_calls == []


async def test_monitor_times_out_idle_sessions_and_applies_r3(c, runner):
    from workers.session_monitor import SessionMonitor
    res = await ask(c, "Payments are stuck in pending", conv="conv-idle")
    sid = uuid.UUID(res.session_id)
    async with c.pool.connection() as conn:
        await conn.execute("UPDATE sessions SET last_activity_at = now() - interval '2 hours' WHERE session_id=%s",
                           (sid,))
        await conn.commit()
    assert await SessionMonitor(c.store, 30).run_once() == [str(sid)]
    assert await SessionMonitor(c.store, 30).run_once() == []  # idempotent sweep
    await runner.run_once()
    job = await c.store.memory_job(sid)
    assert job["status"] == "skipped" and job["reason"].startswith("R3")  # only a read tool ran


async def test_message_after_closure_opens_new_session(c):
    sid = await resolved_case(c, "conv-c")
    res = await ask(c, "New problem: refunds are slow", conv="conv-c")
    assert res.session_id != str(sid)


async def test_user_confirmation_closes_session(c):
    await ask(c, "Payments are stuck in pending", conv="conv-d")
    res = await ask(c, "Thanks, it's resolved now", conv="conv-d")
    assert res.session_status == "RESOLVED"
