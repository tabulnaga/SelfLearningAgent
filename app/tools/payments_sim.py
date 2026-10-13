"""Simulated payments platform (decision D2). Stands in for the real PSP/consumer/payments DB so that
tool results are real, deterministic and repeatable. State lives in Postgres (sim_* tables) so the API,
worker and demo all see the same world.

Behaviour that matters for the POC scenario (spec §27):
- a stalled `payment-events` consumer leaves payments PENDING;
- retrying a PENDING payment fails (the PSP rejects the duplicate) - matching "Retry payment: Failed";
- restarting the consumer applies the backlog and completes PENDING payments - "Resolved".
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from app.tools.registry import Tool, ToolError

KNOWN_CONSUMERS = {"payment-events", "refund-events"}


class ConsumerArgs(BaseModel):
    consumer: str = Field(default="payment-events", description="Consumer name")


class PaymentArgs(BaseModel):
    payment_id: str


class ListPendingArgs(BaseModel):
    limit: int = Field(default=10, ge=1, le=100)


class PaymentsSim:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # ------------------------------------------------------------- scenario control

    async def reset(self, *, stalled: bool, pending: int = 3, restart_fixes: bool = True, prefix: str = "PAY") -> dict:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM sim_payments")
            await conn.execute("DELETE FROM sim_consumers")
            await conn.execute(
                """INSERT INTO sim_consumers (name, state, lag, restart_fixes) VALUES
                   ('payment-events', %s, %s, %s), ('refund-events', 'running', 0, true)""",
                ("stalled" if stalled else "running", 1840 if stalled else 3, restart_fixes),
            )
            for i in range(pending):
                await conn.execute(
                    """INSERT INTO sim_payments (payment_id, status, amount, attempts, created_at)
                       VALUES (%s, 'PENDING', %s, 1, now() - make_interval(mins => %s))""",
                    (f"{prefix}-{1000 + i}", 25 + 10 * i, 12 + i),
                )
        return await self.snapshot()

    async def snapshot(self) -> dict:
        async with self.pool.connection() as conn:
            consumers = await (await conn.execute("SELECT name, state, lag, restarts FROM sim_consumers")).fetchall()
            payments = await (await conn.execute(
                "SELECT status, count(*) AS n FROM sim_payments GROUP BY status")).fetchall()
        return {"consumers": consumers, "payments": {r["status"]: r["n"] for r in payments}}

    # ------------------------------------------------------------------- tools

    async def check_consumer_status(self, args: ConsumerArgs) -> dict:
        if args.consumer not in KNOWN_CONSUMERS:
            raise ToolError(f"unknown consumer '{args.consumer}'; known: {sorted(KNOWN_CONSUMERS)}")
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT name, state, lag, restarts FROM sim_consumers WHERE name=%s", (args.consumer,))).fetchone()
        if row is None:
            raise ToolError("consumer metrics unavailable")
        return {"consumer": row["name"], "state": row["state"], "lag": row["lag"], "restarts": row["restarts"],
                "healthy": row["state"] == "running" and row["lag"] < 500}

    async def list_pending_payments(self, args: ListPendingArgs) -> dict:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                """SELECT payment_id, extract(epoch FROM now() - created_at)::int AS age_s
                   FROM sim_payments WHERE status='PENDING' ORDER BY created_at LIMIT %s""",
                (args.limit,))).fetchall()
            total = (await (await conn.execute(
                "SELECT count(*) AS n FROM sim_payments WHERE status='PENDING'")).fetchone())["n"]
        return {"pending_count": total, "oldest_age_s": rows[0]["age_s"] if rows else 0,
                "payment_ids": [r["payment_id"] for r in rows]}

    async def get_payment_status(self, args: PaymentArgs) -> dict:
        async with self.pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT payment_id, status, amount, attempts FROM sim_payments WHERE payment_id=%s",
                (args.payment_id,))).fetchone()
        if row is None:
            raise ToolError(f"payment {args.payment_id} not found")
        return {**row, "amount": float(row["amount"])}

    async def retry_payment(self, args: PaymentArgs) -> dict:
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                "SELECT status FROM sim_payments WHERE payment_id=%s FOR UPDATE", (args.payment_id,))).fetchone()
            if row is None:
                raise ToolError(f"payment {args.payment_id} not found")
            await conn.execute("UPDATE sim_payments SET attempts=attempts+1, updated_at=now() WHERE payment_id=%s",
                               (args.payment_id,))
            if row["status"] == "PENDING":
                raise ToolError("PSP rejected the retry: original payment request is still pending "
                                "(duplicate idempotency key). Payment remains PENDING.")
            if row["status"] == "COMPLETED":
                raise ToolError("payment already COMPLETED; nothing to retry")
            await conn.execute("UPDATE sim_payments SET status='COMPLETED' WHERE payment_id=%s", (args.payment_id,))
        return {"payment_id": args.payment_id, "status": "COMPLETED"}

    async def restart_consumer(self, args: ConsumerArgs) -> dict:
        if args.consumer not in KNOWN_CONSUMERS:
            raise ToolError(f"unknown consumer '{args.consumer}'")
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                "SELECT restart_fixes FROM sim_consumers WHERE name=%s FOR UPDATE", (args.consumer,))).fetchone()
            if row is None:
                raise ToolError("consumer not found")
            if not row["restart_fixes"]:
                await conn.execute("UPDATE sim_consumers SET restarts=restarts+1, state='stalled' WHERE name=%s",
                                   (args.consumer,))
                raise ToolError("consumer restarted but stalled again within 60 seconds; lag still growing")
            await conn.execute(
                "UPDATE sim_consumers SET state='running', lag=0, restarts=restarts+1, updated_at=now() WHERE name=%s",
                (args.consumer,))
            completed = 0
            if args.consumer == "payment-events":
                cur = await conn.execute(
                    "UPDATE sim_payments SET status='COMPLETED', updated_at=now() WHERE status='PENDING' RETURNING 1")
                completed = len(await cur.fetchall())
        return {"consumer": args.consumer, "state": "running", "lag": 0, "pending_payments_completed": completed}

    def tools(self) -> list[Tool]:
        return [
            Tool("check_consumer_status", "State and lag of a Kafka consumer (payment-events, refund-events).",
                 ConsumerArgs, "read", self.check_consumer_status),
            Tool("list_pending_payments", "Count and list payments currently PENDING.",
                 ListPendingArgs, "read", self.list_pending_payments),
            Tool("get_payment_status", "Status and attempt count of one payment.",
                 PaymentArgs, "read", self.get_payment_status),
            Tool("retry_payment", "Resubmit a payment to the PSP.", PaymentArgs, "write", self.retry_payment),
            Tool("restart_consumer", "Restart a Kafka consumer.", ConsumerArgs, "write", self.restart_consumer),
        ]


REMEDIATION_TOOLS = {"retry_payment", "restart_consumer"}
