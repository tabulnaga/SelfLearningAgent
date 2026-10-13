"""Composition root: the only place that knows concrete implementations. Everything else depends on
interfaces, so storage, models and providers can be swapped here (spec §26.7-8)."""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.agent.orchestrator import Orchestrator
from app.config import Settings
from app.db.pool import open_pool
from app.llm import build_llm
from app.llm.interfaces import LLMClient
from app.memory.hindsight_adapter import DisabledMemory, HindsightMemory
from app.memory.interfaces import LearnedMemory
from app.rag.embed import FastEmbedder
from app.rag.ingest.pipeline import Ingestor
from app.rag.pg_store import PgKnowledgeStore
from app.rag.rerank import FastEmbedReranker, NoopReranker
from app.rag.retriever import HybridRetriever
from app.security.authorizer import Authorizer
from app.sessions.store import SessionStore
from app.tools.payments_sim import PaymentsSim
from app.tools.registry import ToolPolicy, ToolRegistry
from app.verification.verifier import Verifier


@dataclass
class Container:
    settings: Settings
    pool: AsyncConnectionPool
    store: SessionStore
    knowledge: PgKnowledgeStore
    retriever: HybridRetriever
    ingestor: Ingestor
    memory: LearnedMemory
    llm: LLMClient
    sim: PaymentsSim
    orchestrator: Orchestrator


def build_memory(settings: Settings) -> LearnedMemory:
    if not settings.hindsight_enabled:
        return DisabledMemory()
    key = settings.hindsight_api_key.get_secret_value() if settings.hindsight_api_key else None
    return HindsightMemory(settings.hindsight_url, key, settings.hindsight_timeout_s,
                           budget=settings.recall_budget, max_tokens=settings.recall_max_tokens)


async def build_container(settings: Settings, *, embedder=None, reranker=None, memory=None, llm=None) -> Container:
    pool = await open_pool(settings.database_url, settings.db_pool_max)
    store = SessionStore(pool)
    knowledge = PgKnowledgeStore(pool)
    embedder = embedder or FastEmbedder(settings.embedding_model, settings.embedding_dim)
    if reranker is None:
        reranker = FastEmbedReranker(settings.rerank_model) if settings.rerank_enabled else NoopReranker()
    retriever = HybridRetriever(knowledge, embedder, reranker, candidates=settings.rag_candidates,
                                top_k=settings.rerank_top_k,
                                min_rerank_score=settings.rag_min_rerank_score if settings.rerank_enabled else None)
    ingestor = Ingestor(knowledge, embedder, settings.chunk_tokens, settings.chunk_overlap_tokens)
    memory = memory or build_memory(settings)
    llm = llm or build_llm(settings)
    sim = PaymentsSim(pool)
    tools = ToolRegistry(sim.tools(), ToolPolicy(settings.write_tools_allowed))
    orchestrator = Orchestrator(store=store, retriever=retriever, memory=memory, llm=llm, tools=tools,
                                verifier=Verifier(), authorizer=Authorizer(settings.hindsight_bank_id),
                                max_tool_rounds=settings.max_tool_rounds)
    return Container(settings, pool, store, knowledge, retriever, ingestor, memory, llm, sim, orchestrator)
