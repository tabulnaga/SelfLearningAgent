"""WORKING STATE store (Postgres). The system of record for conversations, sessions, messages, tool
executions, the memory outbox and traces. Nothing about a session lives only in LLM context.

Concurrency model (architecture §3-4):
- One turn per conversation at a time: a session-level advisory lock on the conversation, held on a
  dedicated connection for the whole turn (`turn_lock`). Writes inside the turn use short transactions.
- Write-ahead: the user message and last_activity_at are committed BEFORE any reasoning.
- Closure + memory job are one transaction (transactional outbox), so a closed session always has
  exactly one memory job.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.models import SessionStatus, ToolRecord

CLOSURE_STATUSES = {"RESOLVED", "ESCALATED", "TIMED_OUT"}


class SessionStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # ------------------------------------------------------------- ingress dedupe

    async def find_inbound(self, channel: str, external_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM inbound_messages WHERE channel=%s AND external_message_id=%s",
                (channel, external_id),
            )
            return await cur.fetchone()

    async def claim_inbound(self, channel: str, external_id: str, request_id: str) -> bool:
        """True if this delivery is new. A redelivery (same id) returns False."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """INSERT INTO inbound_messages (channel, external_message_id, request_id)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING request_id""",
                (channel, external_id, request_id),
            )
            return await cur.fetchone() is not None

    async def complete_inbound(self, channel: str, external_id: str, session_id: str, response: dict) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """UPDATE inbound_messages SET session_id=%s, response=%s
                   WHERE channel=%s AND external_message_id=%s""",
                (session_id, Jsonb(response), channel, external_id),
            )

    # ------------------------------------------------------------------ turns

    @asynccontextmanager
    async def turn_lock(self, conversation_id: str):
        async with self.pool.connection() as conn:
            await conn.set_autocommit(True)
            try:
                await conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (conversation_id,))
                try:
                    yield
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (conversation_id,))
            finally:
                await conn.set_autocommit(False)

    async def begin_turn(
        self, *, conversation_id: str, tenant_id: str, channel: str, user_id: str | None,
        message: str, request_id: str,
    ) -> dict[str, Any]:
        """Resolve/create the live session and write the user message ahead of reasoning."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO conversations (conversation_id, tenant_id, channel, user_id)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (conversation_id) DO NOTHING""",
                (conversation_id, tenant_id, channel, user_id),
            )
            cur = await conn.execute(
                """SELECT * FROM sessions WHERE conversation_id=%s AND status IN ('OPEN','ACTIVE')
                   FOR UPDATE""",
                (conversation_id,),
            )
            session = await cur.fetchone()
            is_new = session is None
            if is_new:
                cur = await conn.execute(
                    """INSERT INTO sessions (session_id, conversation_id, tenant_id, user_id, status)
                       VALUES (%s,%s,%s,%s,'OPEN') RETURNING *""",
                    (uuid.uuid4(), conversation_id, tenant_id, user_id),
                )
                session = await cur.fetchone()
            await conn.execute(
                "INSERT INTO messages (session_id, role, content, request_id) VALUES (%s,'user',%s,%s)",
                (session["session_id"], message, request_id),
            )
            cur = await conn.execute(
                """UPDATE sessions SET status='ACTIVE', last_activity_at=now(), turn_count=turn_count+1
                   WHERE session_id=%s RETURNING *""",
                (session["session_id"],),
            )
            session = await cur.fetchone()
            session["is_new"] = is_new
            return session

    async def finish_turn(
        self, *, session_id, request_id: str, conversation_id: str, answer: str, trace: dict,
        intent: str | None, domain: str | None, answer_meta: dict,
    ) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO messages (session_id, role, content, request_id, metadata)
                   VALUES (%s,'assistant',%s,%s,%s)""",
                (session_id, answer, request_id, Jsonb(answer_meta)),
            )
            await conn.execute(
                """UPDATE sessions SET intent=COALESCE(%s, intent),
                       domain=CASE WHEN %s IS NULL OR %s='general' THEN domain ELSE %s END
                   WHERE session_id=%s""",
                (intent, domain, domain, domain, session_id),
            )
            await conn.execute(
                """INSERT INTO turn_traces (request_id, session_id, conversation_id, trace)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (request_id) DO UPDATE SET trace=EXCLUDED.trace""",
                (request_id, session_id, conversation_id, Jsonb(trace)),
            )

    async def cache_pages(self, session_id, pages: list[dict]) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE sessions SET knowledge_pages=%s, pages_fetched_at=now() WHERE session_id=%s",
                (Jsonb(pages), session_id),
            )

    # ------------------------------------------------------------------ reads

    async def get_session(self, session_id) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM sessions WHERE session_id=%s", (session_id,))
            return await cur.fetchone()

    async def messages(self, session_id, limit: int = 50) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT * FROM (SELECT * FROM messages WHERE session_id=%s ORDER BY message_id DESC LIMIT %s) m
                   ORDER BY message_id""",
                (session_id, limit),
            )
            return await cur.fetchall()

    async def tool_records(self, session_id) -> list[ToolRecord]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM tool_executions WHERE session_id=%s ORDER BY tool_exec_id", (session_id,)
            )
            return [ToolRecord.model_validate(r) for r in await cur.fetchall()]

    async def traces(self, session_id) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT request_id, created_at, trace FROM turn_traces WHERE session_id=%s ORDER BY created_at",
                (session_id,),
            )
            return await cur.fetchall()

    # ------------------------------------------------------------------ tools

    async def record_tool(
        self, *, session_id, request_id: str, tool_name: str, side_effect: str, input: dict,
        output: dict | None, status: str, error: str | None, duration_ms: int,
    ) -> ToolRecord:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """INSERT INTO tool_executions (session_id, request_id, tool_name, side_effect, input, output,
                                                status, error, duration_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (session_id, request_id, tool_name, side_effect, Jsonb(input),
                 Jsonb(output) if output is not None else None, status, error, duration_ms),
            )
            return ToolRecord.model_validate(await cur.fetchone())

    # ---------------------------------------------------------------- closure

    async def close(self, session_id, reason: str, note: str | None = None, escalation: dict | None = None) -> bool:
        """Live session -> RESOLVED/ESCALATED/TIMED_OUT, plus its memory job, atomically.
        Idempotent: closing an already-closed session returns False and changes nothing."""
        if reason not in CLOSURE_STATUSES:
            raise ValueError(f"invalid closure reason {reason}")
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE sessions SET status=%s, closure_reason=%s, closed_at=now(),
                       outcome_note=COALESCE(%s, outcome_note), escalation=COALESCE(%s, escalation)
                   WHERE session_id=%s AND status IN ('OPEN','ACTIVE') RETURNING session_id""",
                (reason, reason, note, Jsonb(escalation) if escalation else None, session_id),
            )
            if await cur.fetchone() is None:
                return False
            await conn.execute(
                "INSERT INTO memory_jobs (session_id) VALUES (%s) ON CONFLICT (session_id) DO NOTHING",
                (session_id,),
            )
            return True

    async def touch(self, session_id) -> None:
        """Operator activity keeps the case alive (SessionMonitor measures idleness from last_activity_at)."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "UPDATE sessions SET last_activity_at=now() WHERE session_id=%s AND status IN ('OPEN','ACTIVE')",
                (session_id,))

    async def add_operator_message(self, session_id, content: str, request_id: str) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO messages (session_id, role, content, request_id) VALUES (%s,'operator',%s,%s)",
                (session_id, content, request_id),
            )

    async def memory_job(self, session_id) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT job_id, status, attempts, reason, document_id, operation_id, last_error, case_record,
                          updated_at FROM memory_jobs WHERE session_id=%s""", (session_id,))
            return await cur.fetchone()

    async def time_out_idle(self, timeout_minutes: int, batch: int = 100) -> list[str]:
        """SessionMonitor sweep: ACTIVE sessions idle past the configured timeout -> TIMED_OUT + job.
        SKIP LOCKED makes concurrent monitors safe."""
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """WITH idle AS (
                       SELECT session_id FROM sessions
                       WHERE status='ACTIVE' AND last_activity_at < now() - make_interval(mins => %s)
                       ORDER BY last_activity_at LIMIT %s FOR UPDATE SKIP LOCKED),
                   upd AS (
                       UPDATE sessions s SET status='TIMED_OUT', closure_reason='TIMED_OUT', closed_at=now()
                       FROM idle WHERE s.session_id = idle.session_id RETURNING s.session_id),
                   jobs AS (
                       INSERT INTO memory_jobs (session_id) SELECT session_id FROM upd
                       ON CONFLICT (session_id) DO NOTHING RETURNING session_id)
                   SELECT session_id FROM upd""",
                (timeout_minutes, batch),
            )
            return [str(r["session_id"]) for r in await cur.fetchall()]

    async def mark_closed(self, session_id, memory_written: bool) -> None:
        """Memory job reached a terminal state -> CLOSED (architecture §4)."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """UPDATE sessions SET status='CLOSED', memory_written=%s
                   WHERE session_id=%s AND status IN ('RESOLVED','ESCALATED','TIMED_OUT')""",
                (memory_written, session_id),
            )


__all__ = ["SessionStatus", "SessionStore"]
