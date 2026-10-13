"""Integration fixtures: real Postgres (docker compose `db`), deterministic fakes for LLM and memory.

    docker compose up -d db
    uv run pytest -m integration   (also: -m live for tests/evals)
Uses a separate database (agent_test) so tests never touch demo data.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

from app.config import Settings
from app.container import build_container
from app.db.migrate import migrate
from app.db.pool import close_pool
from app.llm.fake import FakeLLM
from app.models import Principal
from app.rag.embed import HashingEmbedder
from app.rag.rerank import NoopReranker
from tests.fakes import FakeMemory
from workers.memory_jobs import MemoryJobRunner

ADMIN_DSN = os.getenv("TEST_ADMIN_DATABASE_URL", "postgresql://agent:agent@localhost:5432/agent")
TEST_DSN = os.getenv("TEST_DATABASE_URL", "postgresql://agent:agent@localhost:5432/agent_test")
DOCS = Path(__file__).resolve().parents[2] / "data" / "docs"

TABLES = ["turn_traces", "memory_jobs", "tool_executions", "messages", "inbound_messages", "sessions",
          "conversations", "rag_chunks", "rag_documents", "sim_payments", "sim_consumers"]

USER = Principal(subject="u", tenant_id="default", roles=["agent_user"])
OPERATOR = Principal(subject="op", tenant_id="default", roles=["agent_user", "operator"])
ADMIN = Principal(subject="admin", tenant_id="default", roles=["admin"])


def _ensure_db() -> None:
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname='agent_test'").fetchone():
                conn.execute("CREATE DATABASE agent_test")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable ({e}); run `docker compose up -d db`")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_DSN, llm_provider="fake", hindsight_enabled=False, app_env="sim",
                    rerank_enabled=False, retain_async=False, api_keys="", rag_min_rerank_score=-100.0)


@pytest.fixture(scope="session")
async def env(settings):
    _ensure_db()
    await migrate(TEST_DSN)
    memory, llm = FakeMemory(), FakeLLM()
    c = await build_container(settings, embedder=HashingEmbedder(), reranker=NoopReranker(), memory=memory, llm=llm)
    yield c
    await close_pool()


@pytest.fixture
async def c(env):
    async with env.pool.connection() as conn:
        await conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        await conn.commit()
    mem: FakeMemory = env.memory
    mem.documents.clear()
    mem.retain_calls.clear()
    mem.items.clear()
    mem.pages.clear()
    mem.down = False
    llm: FakeLLM = env.llm
    llm.calls.clear()
    llm.fail_purposes.clear()
    llm.overrides.clear()
    await env.ingestor.ingest_dir(DOCS, "default")
    await env.sim.reset(stalled=True, pending=3)
    return env


@pytest.fixture
def runner(c, settings):
    return MemoryJobRunner(c.pool, c.store, c.memory, c.llm, settings)
