"""MemoryJobRunner: drains the transactional outbox (memory_jobs) through the Memory Writer.

Job states: pending -> running -> (submitted ->) done | skipped | failed
- claim uses FOR UPDATE SKIP LOCKED + a lease, so several workers are safe and a crashed worker's
  job is reclaimed when its lease expires;
- every retry backs off exponentially; after MEMORY_JOB_MAX_ATTEMPTS the job is `failed` (alert);
- the user's request never waits on any of this (spec §17).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config import Settings
from app.llm.interfaces import LLMClient, LLMError
from app.memory import writer
from app.memory.interfaces import LearnedMemory
from app.models import CaseRecord
from app.observability.logging import bind, clear, log_event
from app.sessions.store import SessionStore
from app.tools.payments_sim import REMEDIATION_TOOLS

log = logging.getLogger("worker.memory_jobs")


def backoff_seconds(attempts: int) -> int:
    return min(600, 10 * 2 ** max(0, attempts - 1))


class MemoryJobRunner:
    def __init__(self, pool: AsyncConnectionPool, store: SessionStore, memory: LearnedMemory, llm: LLMClient,
                 settings: Settings) -> None:
        self.pool = pool
        self.store = store
        self.memory = memory
        self.llm = llm
        self.settings = settings

    # ------------------------------------------------------------------ queue ops

    async def claim(self, limit: int = 5) -> list[dict]:
        async with self.pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """UPDATE memory_jobs j SET status = CASE WHEN j.status='submitted' THEN 'submitted' ELSE 'running' END,
                       attempts = CASE WHEN j.status='submitted' THEN j.attempts ELSE j.attempts + 1 END,
                       locked_until = now() + make_interval(secs => %s), updated_at = now()
                   WHERE j.job_id IN (
                       SELECT job_id FROM memory_jobs
                       WHERE (status IN ('pending','submitted') AND next_attempt_at <= now()
                              AND (locked_until IS NULL OR locked_until < now()))
                          OR (status = 'running' AND locked_until < now())
                       ORDER BY next_attempt_at LIMIT %s FOR UPDATE SKIP LOCKED)
                   RETURNING j.*""",
                (self.settings.memory_job_lease_s, limit),
            )
            return await cur.fetchall()

    async def _update(self, job_id: int, **fields) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k} = %s")
            vals.append(Jsonb(v) if isinstance(v, dict) else v)
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                f"UPDATE memory_jobs SET {', '.join(sets)}, locked_until = NULL, updated_at = now() WHERE job_id = %s",
                (*vals, job_id),
            )

    async def _retry_later(self, job: dict, error: str) -> None:
        if job["attempts"] >= self.settings.memory_job_max_attempts:
            await self._update(job["job_id"], status="failed", last_error=error[:2000])
            log_event(log, "memory job failed permanently", logging.ERROR, job_id=job["job_id"], error=error)
            return
        delay = backoff_seconds(job["attempts"])
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """UPDATE memory_jobs SET status='pending', last_error=%s, locked_until=NULL, updated_at=now(),
                       next_attempt_at = now() + make_interval(secs => %s) WHERE job_id=%s""",
                (error[:2000], delay, job["job_id"]),
            )
        log_event(log, "memory job will retry", logging.WARNING, job_id=job["job_id"], attempts=job["attempts"],
                  retry_in_s=delay, error=error)

    # ------------------------------------------------------------------- process

    async def run_once(self) -> int:
        jobs = await self.claim()
        for job in jobs:
            clear()
            bind(session_id=str(job["session_id"]), memory_job_id=job["job_id"])
            try:
                if job["status"] == "submitted":
                    await self._poll(job)
                else:
                    await self._process(job)
            except Exception as e:
                await self._retry_later(job, f"{type(e).__name__}: {e}")
        clear()
        return len(jobs)

    async def _process(self, job: dict) -> None:
        sid = job["session_id"]
        session = await self.store.get_session(sid)
        messages = await self.store.messages(sid, limit=60)
        tools = await self.store.tool_records(sid)
        intents = [t["trace"].get("intent") for t in await self.store.traces(sid) if t["trace"].get("intent")]
        has_operator = any(m["role"] == "operator" for m in messages)

        decision = writer.evaluate_retention(session, intents, tools, has_operator, REMEDIATION_TOOLS)
        if not decision.retain:
            await self._skip(job, decision.reason)
            return

        actions = writer.derive_actions(tools, REMEDIATION_TOOLS)
        outcome = writer.outcome_for(session["closure_reason"], actions["successful_action"])
        try:
            narrative = await writer.write_narrative(self.llm, messages, actions, session.get("outcome_note"), outcome)
        except LLMError as e:
            raise RuntimeError(f"narrative extraction failed: {e}") from e

        closed_at = (session["closed_at"] or datetime.now(UTC)).isoformat()
        record = CaseRecord(
            session_id=str(sid), conversation_id=session["conversation_id"], tenant_id=session["tenant_id"],
            domain=session.get("domain") or "general", closure_reason=session["closure_reason"], closed_at=closed_at,
            issue=narrative.issue, root_cause=narrative.root_cause, lessons=narrative.lessons,
            confidence=narrative.confidence, outcome=outcome,
            actions_attempted=actions["actions_attempted"], failed_actions=actions["failed_actions"],
            successful_action=actions["successful_action"],
        )
        confirmed = session["closure_reason"] in ("RESOLVED", "ESCALATED")
        rejection = writer.validate_record(record, self.settings.retain_min_confidence, confirmed)
        if rejection:
            await self._skip(job, rejection, record)
            return

        content = writer.render_content(record, actions["diagnostics"])
        req, content_hash = writer.build_retain_request(
            record, content, self.settings.hindsight_bank_id, self.settings.app_env, self.settings.retain_async,
            attempt_salt=job["attempts"] - 1,
        )
        if job.get("content_hash") == content_hash and job.get("status") == "done":
            await self._skip(job, "R4: identical content already retained", record)
            return

        receipt = await self.memory.retain_case(req)
        if not receipt.accepted:
            raise RuntimeError(f"retain not accepted: {receipt.detail}")
        common = dict(document_id=req.document_id, operation_id=receipt.operation_id or req.operation_id,
                      content_hash=content_hash, case_record=record.model_dump(), reason="retained")
        if receipt.is_async:
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    """UPDATE memory_jobs SET status='submitted', document_id=%s, operation_id=%s, content_hash=%s,
                           case_record=%s, reason=%s, locked_until=NULL, updated_at=now(),
                           next_attempt_at = now() + interval '3 seconds' WHERE job_id=%s""",
                    (common["document_id"], common["operation_id"], content_hash, Jsonb(common["case_record"]),
                     "submitted to Hindsight", job["job_id"]),
                )
            log_event(log, "retain submitted", operation_id=common["operation_id"], document_id=req.document_id,
                      tags=req.tags)
        else:
            await self._update(job["job_id"], status="done", **common)
            await self.store.mark_closed(sid, memory_written=True)
            log_event(log, "retained (sync)", document_id=req.document_id, tags=req.tags)

    async def _poll(self, job: dict) -> None:
        op = await self.memory.operation_status(self.settings.hindsight_bank_id, job["operation_id"])
        if op.status == "completed":
            await self._update(job["job_id"], status="done", reason="retained")
            await self.store.mark_closed(job["session_id"], memory_written=True)
            log_event(log, "retain completed", operation_id=job["operation_id"], document_id=job["document_id"])
        elif op.status in ("failed", "not_found", "cancelled"):
            # Resubmit: a new attempt salts the operation_id; document_id upsert keeps it one memory.
            await self._retry_later({**job, "status": "pending"}, f"hindsight operation {op.status}: {op.error}")
        else:
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    """UPDATE memory_jobs SET locked_until=NULL, next_attempt_at = now() + interval '5 seconds',
                           updated_at=now() WHERE job_id=%s""", (job["job_id"],))

    async def _skip(self, job: dict, reason: str, record: CaseRecord | None = None) -> None:
        await self._update(job["job_id"], status="skipped", reason=reason,
                           case_record=record.model_dump() if record else None)
        await self.store.mark_closed(job["session_id"], memory_written=False)
        log_event(log, "memory job skipped", reason=reason)
