"""Spec §25 Phase 6 / §19: the seven POC evaluations, scored on separate dimensions.

Tiers (architecture §11 challenge 4 - learning cannot be proven offline):
  offline  (-m integration): real Postgres + real local embeddings/reranker; fake LLM + fake memory.
           Evals 1, 3, 4, 5.
  live     (-m live): real Hindsight + real LLM. Evals 2, 6, 7. Slow; costs tokens.

    docker compose up -d db hindsight
    uv run pytest -m integration tests/evals
    uv run pytest -m live tests/evals -s
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.container import build_container
from app.db.pool import close_pool
from app.models import LearnedItem, MemoryScope, ProposedTool
from app.rag.embed import FastEmbedder
from app.rag.rerank import FastEmbedReranker
from tests.conftest import DOCS, TEST_DSN, USER, _ensure_db  # noqa: F401 (fixtures)
from workers.memory_jobs import MemoryJobRunner

HERE = Path(__file__).parent
CASES = json.loads((HERE.parents[1] / "data" / "cases" / "poc_cases.json").read_text(encoding="utf-8"))


# ======================================================================= offline


@pytest.mark.integration
async def test_eval1_rag_retrieves_authoritative_information(c):
    """Retrieval quality with the production embedder + cross-encoder: recall@k and MRR."""
    s = c.settings
    from app.rag.retriever import HybridRetriever

    embedder = FastEmbedder(s.embedding_model, s.embedding_dim)
    from app.rag.ingest.pipeline import Ingestor

    async with c.pool.connection() as conn:
        await conn.execute("TRUNCATE rag_chunks, rag_documents CASCADE")
        await conn.commit()
    await Ingestor(c.knowledge, embedder, s.chunk_tokens, s.chunk_overlap_tokens).ingest_dir(DOCS, "default")
    retriever = HybridRetriever(c.knowledge, embedder, FastEmbedReranker(s.rerank_model), top_k=5)
    scope = c.orchestrator.authorizer.retrieval_scope(USER)

    rows, hits, rr = json.loads((HERE / "retrieval_set.json").read_text()), 0, 0.0
    report = []
    for item in rows:
        res = await retriever.retrieve(item["q"], scope)
        docs = [e.doc_id for e in res.evidence]
        rank = docs.index(item["expect"]) + 1 if item["expect"] in docs else None
        hits += rank is not None
        rr += 1 / rank if rank else 0
        report.append((item["q"][:60], item["expect"], rank))
    recall, mrr = hits / len(rows), rr / len(rows)
    print(f"\nretrieval recall@5={recall:.2f} MRR={mrr:.2f}")
    for line in report:
        print("  ", line)
    assert recall >= 0.9 and mrr >= 0.7


@pytest.mark.integration
async def test_eval3_agent_distinguishes_official_and_learned(c):
    """Source discipline: official and learned land in separate labelled sections, citations resolve to
    the right store, and learned experience never appears under the official heading."""
    c.memory.items = [LearnedItem(memory_id="obs-9", type="observation", proof_count=3,
                                  text="Stalled payment consumer caused Payment Pending; restart resolved it.")]
    res = await c.orchestrator.handle_message(principal=USER, channel="api", conversation_id="e3",
                                              text="Payments are pending, what should I check?")
    official_section = res.answer.split("From past cases")[0]
    assert "[M" not in official_section, "learned citation leaked into the official section"
    assert {s["cite"][0] for s in res.sources["official"]} == {"D"}
    assert {s["cite"][0] for s in res.sources["learned"]} <= {"M", "P"} and res.sources["learned"]
    assert res.verification["passed"]


async def _case(c, conv: str, sim: dict, messages: list[str], tools: list[dict], note: str | None):
    await c.sim.reset(**sim)
    res = None
    for m in messages:
        res = await c.orchestrator.handle_message(principal=USER, channel="api", conversation_id=conv, text=m)
    sid = uuid.UUID(res.session_id)
    for t in tools:
        await c.orchestrator.tools.execute(
            ProposedTool(name=t["name"], arguments_json=json.dumps(t["arguments"])),
            lambda **kw: c.store.record_tool(session_id=sid, request_id="operator", **kw))
    if note:
        await c.store.add_operator_message(sid, note, "operator")
    await c.store.close(sid, "RESOLVED", note=note)
    return sid


@pytest.mark.integration
async def test_eval4_resolved_cases_are_retained_and_noise_is_not(c, runner):
    sids = []
    for i, case in enumerate(CASES["cases"]):
        sids.append(await _case(c, f"e4-{i}", case["sim"], case["messages"], case["operator_tools"],
                                case["resolve_note"]))
    while await runner.run_once():
        pass
    jobs = {case["name"]: await c.store.memory_job(sid) for case, sid in zip(CASES["cases"], sids, strict=True)}
    assert all(jobs[f"case-{n}"]["status"] == "done" for n in (1, 2, 3))
    assert jobs["noise-greeting"]["status"] == "skipped"
    assert len(c.memory.documents) == 3
    # memory quality: the retained content carries the outcome, not the transcript
    for req in c.memory.documents.values():
        assert "What resolved it: restart_consumer" in req.content
        assert "Customers report" not in req.content


@pytest.mark.integration
async def test_eval5_duplicate_cases_do_not_create_duplicate_retains(c, runner):
    case = CASES["cases"][0]
    sid = await _case(c, "e5", case["sim"], case["messages"], case["operator_tools"], case["resolve_note"])
    for _ in range(3):
        await c.store.close(sid, "RESOLVED")                      # repeated closure signals
    await runner.run_once()
    async with c.pool.connection() as conn:                       # force a worker re-run of the same job
        await conn.execute("UPDATE memory_jobs SET status='pending', next_attempt_at=now() WHERE session_id=%s",
                           (sid,))
        await conn.commit()
    await runner.run_once()
    assert len(c.memory.documents) == 1, "one case must map to one Hindsight document"
    ops = {r.operation_id for r in c.memory.retain_calls}
    assert len(ops) <= 2 and len({r.content for r in c.memory.retain_calls}) == 1


# ========================================================================== live


def _concepts(text: str, *words: str) -> bool:
    t = text.lower()
    return all(re.search(w, t) for w in words)


@pytest.mark.live
async def test_live_learning_evals_2_6_7():
    """Evals 2, 6, 7 against real Hindsight + real LLM, on a fresh bank."""
    _ensure_db()
    bank = f"support-ops-eval-{int(time.time())}"
    settings = Settings(database_url=TEST_DSN, app_env="sim", hindsight_bank_id=bank, retain_async=True,
                        hindsight_url=os.getenv("HINDSIGHT_URL", "http://localhost:8888"))
    from app.db.migrate import migrate
    from app.memory.bank_setup import bootstrap_bank

    await migrate(TEST_DSN)
    c = await build_container(settings)
    try:
        assert await c.memory.healthy(), "Hindsight not reachable"
        await bootstrap_bank(c.memory.client, bank)
        async with c.pool.connection() as conn:
            await conn.execute("TRUNCATE turn_traces, memory_jobs, tool_executions, messages, inbound_messages, "
                               "sessions, conversations CASCADE")
            await conn.commit()
        await c.ingestor.ingest_dir(DOCS, "default")
        runner = MemoryJobRunner(c.pool, c.store, c.memory, c.llm, settings)

        for i, case in enumerate(CASES["cases"][:3]):
            await _case(c, f"live-{bank}-{i}", case["sim"], case["messages"], case["operator_tools"],
                        case["resolve_note"])
        deadline = time.time() + 600
        while time.time() < deadline:
            await runner.run_once()
            async with c.pool.connection() as conn:
                left = (await (await conn.execute(
                    "SELECT count(*) AS n FROM memory_jobs WHERE status NOT IN ('done','skipped','failed')"))
                    .fetchone())["n"]
            if left == 0:
                break
            await asyncio.sleep(3)
        async with c.pool.connection() as conn:
            statuses = await (await conn.execute("SELECT status, reason FROM memory_jobs")).fetchall()
        print("\nmemory jobs:", statuses)
        assert all(s["status"] == "done" for s in statuses), statuses

        scope = MemoryScope(bank_id=bank, tags=["tenant:default", "domain:payments"])

        # Eval 2 - Hindsight recalls previous experience
        recall = await c.memory.recall("payments stuck pending what fixed it", scope)
        print("recall:", [(m.type, m.text[:100]) for m in recall.items])
        assert recall.status == "ok" and any(_concepts(m.text, "consumer", "restart") for m in recall.items)

        # Eval 6 - observations emerge from multiple experiences (consolidation is async)
        obs = []
        deadline = time.time() + 600
        while time.time() < deadline:
            r = await c.memory.client.arecall(bank_id=bank, query="payment pending consumer restart",
                                              types=["observation"], tags=scope.tags, tags_match="all_strict")
            obs = [x for x in r.results if _concepts(x.text, "pending", "consumer", "restart")]
            if obs and max(len(x.source_fact_ids or []) for x in obs) >= 2:
                break
            await asyncio.sleep(10)
        print("observations:", [(x.text, len(x.source_fact_ids or [])) for x in obs])
        assert obs, "no observation linking Payment Pending, the consumer and restart"
        assert max(len(x.source_fact_ids or []) for x in obs) >= 2, "observation not supported by multiple facts"

        # Eval 7 - a new session benefits from previous sessions
        await c.sim.reset(stalled=True, pending=2)
        res = await c.orchestrator.handle_message(principal=USER, channel="api", conversation_id=f"new-{bank}",
                                                  text=CASES["new_session_question"])
        print("\nNEW SESSION ANSWER:\n" + res.answer)
        assert res.sources["official"], "official procedure missing"
        assert res.sources["learned"], "learned experience not used"
        assert "From past cases (learned experience, not official policy)" in res.answer
        assert res.verification["passed"], res.verification
    finally:
        await close_pool()
