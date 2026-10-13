# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Start here

The POC is implemented. Read `claude-progress.md` first: it lists what has been verified and what
has not. Then read `feature_list.json`, where one feature is active at a time and "passing" needs
recorded evidence. The design is in `docs/architecture.md`. Spec: `Requriments.md` (misspelled;
leave it). It is **not** the same document as `../Complaint_Management/Requriments.md`.

```bash
uv sync && uv run pytest                    # unit tests, no services
uv run pytest tests/unit/test_verification.py::test_good_answer_passes   # single test
docker compose up -d db && uv run pytest -m integration                   # needs Postgres
uv run pytest -m live tests/evals -s        # real Hindsight + ANTHROPIC_API_KEY; evals 2, 6, 7
uv run ruff check app workers scripts tests
docker compose up -d --build && docker compose exec api python -m scripts.bootstrap_bank
uv run python -m scripts.demo_poc           # spec §27 end-to-end against the running stack
```

Code map: `app/container.py` is the only place that wires concrete implementations.
`tests/unit/test_architecture_boundaries.py` enforces the separation rules below as import checks.
Update that file when a boundary changes, rather than working around it.

## Mandated working order (spec §25, §29)

The spec gates implementation behind written analysis. Follow the phases in order:

1. **Before any code:** inspect the workspace, study both reference repos, report findings,
   propose the architecture, list unresolved decisions, recommend the smallest POC, and show the
   project structure and plan. **Then stop and ask for approval.** Do not generate the full
   application in one pass.
2. Deliverables are `docs/reference-analysis.md` (Phase 1) and then `docs/architecture.md`
   (Phase 2), followed by the vertical slice, the learning loop, reliability, and evals.
3. The spec explicitly invites pushback: "If something in my requested architecture is
   technically wrong, challenge it." Don't simply accept the suggested layout (§22), the stack
   (§21), or LangGraph. §21 also requires a stated justification for every new dependency: what
   problem it solves, why the existing stack can't, and what operational cost it adds.

## Reference repos are already local; use them instead of fetching

| Spec reference | Local copy |
|---|---|
| `vectorize-io/hindsight` | `../SelfLearnMemory/hindsight-main/` (v0.10.2 snapshot) |
| `walkinglabs/learn-harness-engineering` | `../learn-harness-engineering-main/` |

- The Hindsight snapshot has its own authoritative `CLAUDE.md`. Read it first. It also contains
  a full duplicate nested at `hindsight-main/hindsight-main/`, so ignore the inner copy.
- The Python client to integrate against is `hindsight-client`
  (`../SelfLearnMemory/hindsight-main/hindsight-clients/python/`). Docker Compose variants for
  running Hindsight live in `.../docker/docker-compose/`, including `external-pg`, which is relevant
  if the app's Postgres is shared with Hindsight (§24 says not to duplicate infrastructure).
- Hindsight is a dependency **to call, not to reimplement or modify**. Facts → observations →
  mental models/knowledge pages is Hindsight's own consolidation. Configure it through bank
  missions, directives, and tags. Don't rebuild it outside Hindsight.

## Architectural invariants the spec treats as hard rules

These rules span many components, so a change to any one component can break them:

- **Three memory types stay separate:** authoritative RAG (docs/policies, with a pluggable
  retriever and hybrid search + rerank), learned experience (Hindsight), and session/working
  state (a swappable state store). Hindsight is never the RAG vector store, and session state is
  never written to Hindsight.
- **Provenance is preserved end to end.** RAG results and Hindsight recall reach the LLM as
  separately labelled blocks (`OFFICIAL KNOWLEDGE` / `LEARNED EXPERIENCE`) and are never merged.
  When they conflict, authoritative docs win, and learned experience can't be presented as policy.
  The verifier checks this deterministically where it can.
- **Retain only through the Memory Writer, and only on session closure.** Never call `retain()`
  for each message. Closure comes from explicit resolution, escalation, or an inactivity timeout.
  The writer produces a structured outcome (issue, root cause, actions attempted/failed,
  successful action, outcome, lessons, confidence), applies explicit retention criteria, and then
  retains. Retain must be **idempotent per closed case**, guarded by a `memory_written` flag plus
  an idempotency key, so a retry can't duplicate memories.
- **No blocking waits for timeouts.** Each message is treated as a separate execution
  (n8n/WhatsApp/webhook). The app persists `last_activity_at` and status, and a separate session
  monitor worker closes stale `ACTIVE` sessions. The timeout comes from config
  (`SESSION_TIMEOUT_MINUTES`) and is never hard-coded. Session states: OPEN, ACTIVE, RESOLVED,
  ESCALATED, CLOSED, TIMED_OUT.
- **Memory failures never fail the user request.** If Hindsight is down, continue with RAG only.
  If the Memory Writer fails, queue and retry it out of band. If a tool fails, never report
  success.
- **Authorization is deterministic and runs before retrieval.** It happens before any context
  reaches the model and never inside LLM reasoning.
- **Start with a single deterministic agent.** The orchestrator graph is load session →
  understand → RAG + recall → build context → reason → tools → verify → respond → update state.
  Leave interfaces open for later specialist agents (CRM/Billing/Network), but don't build
  multi-agent support now. Core logic lives in the app. n8n may call the app's API, but the app
  must not depend on n8n.
- **Observability:** every request is traceable through structured logs from question →
  retrieval IDs/scores → memory IDs → tool calls → verification → answer → retain operation ID.
  Logs must never contain secrets, and PII needs careful handling.

## Acceptance target

The POC scenario (§27) is the end-to-end proof. It runs three "Payment Pending" cases that are
resolved by restarting the Kafka payment consumer. After them, a Hindsight observation should
capture that pattern. A **new** session asking what to investigate first must then answer by
combining official procedure (RAG) with learned experience (Hindsight), with provenance kept
visible. The §25 Phase 6 eval list (7 items, including "duplicate cases do not create duplicate
retains") is the definition of done for evaluation. Evals are scored on separate dimensions
(§19), not only on final-answer quality.
