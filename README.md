# Self-Learning Agentic RAG (POC)

## What is this?

A proof-of-concept **support agent that gets better with experience, without fine-tuning**.

It answers payments-operations questions, such as "payments are stuck in Pending, what do I check
first?". It grounds each answer in official runbooks and policies, which is classic RAG. It also
remembers what actually fixed past incidents. When a case closes, the agent distils what happened
and stores it as long-term memory in [Hindsight](https://github.com/vectorize-io/hindsight).
Hindsight consolidates many cases into reusable observations. The next time a similar question
arrives, the agent shows both:

> **Official procedure** (from the runbook) *and* **what we've learned from past cases**
> (from memory), labelled separately so learned experience is never passed off as policy.

The domain is a simulated payments platform with Kafka consumers, a PSP and refunds. The
architecture is domain-agnostic and is meant to extend to other specialist agents, for example
CRM, Billing and Network.

### Three kinds of memory, kept separate

| | Source | Answers |
|---|---|---|
| **Official knowledge** | Hybrid RAG over our documents (Postgres + pgvector + full-text, cross-encoder rerank) | "What does the documentation say?" |
| **Learned experience** | Hindsight bank `support-ops`: closed cases → facts → observations → knowledge pages | "What have we learned from past cases?" |
| **Working state** | Postgres sessions, messages, tool log, outbox, traces | "Where is this conversation right now?" |

## Features

- **Hybrid RAG with provenance.** Retrieval combines vector and full-text search, then reranks
  with a cross-encoder. Every answer cites the document chunks it used.
- **Learning loop on case closure.** A session closes when it is resolved, escalated or timed out.
  The *Memory Writer* then extracts a structured outcome: issue, root cause, actions tried,
  the action that worked, lessons and confidence. That outcome is retained in Hindsight.
  Hindsight turns repeated outcomes into observations and knowledge pages.
- **Explicit retention rules (R1–R5).** The writer skips small talk, unverified claims, timeouts
  that ended without a fix, and low-confidence extractions. Every skip records its reason.
- **Idempotent memory writes.** Each closed case is retained exactly once, guarded by a
  `memory_written` flag, an idempotency key and a transactional outbox, so retries never duplicate
  memories.
- **Deterministic answer verifier (V1–V6).** Code, not the model, checks each draft answer:
  citations are valid, claims are grounded, official and learned sources are not mixed up,
  conflicts are surfaced, tool results are reported truthfully, and the agent never claims a fix
  that didn't happen.
- **Typed tools with policy.** The simulated payments platform has read tools
  (`check_consumer_status`, `list_pending_payments`, `get_payment_status`) and write tools
  (`retry_payment`, `restart_consumer`). Write tools auto-run only in `APP_ENV=sim`.
- **Security before retrieval.** API keys map to a tenant and roles (`agent_user` / `operator` /
  `admin`). Document access is filtered in SQL *before* the model sees anything, and PII is
  redacted.
- **Webhook-friendly sessions.** One inbound message is one HTTP call, so the API suits n8n,
  WhatsApp or any webhook. Nothing blocks while waiting. A background `SessionMonitor` times out
  idle sessions.
- **Graceful degradation.** If Hindsight is down, the agent keeps answering from RAG alone. A
  failed memory write is queued and retried out of band. A failed tool is never reported as a
  success.
- **Full traceability.** Each turn records a decision trace: question → retrieved chunk IDs and
  scores → memory IDs → tool calls → verification → answer → retain operation ID.
- **Evals as the definition of done.** There are seven POC evals. They cover retrieval quality,
  official-vs-learned separation, retention of real cases but not noise, duplicate-retain
  protection, and live learning across sessions.

## How it works

```
inbound message
  → load session → authorize → understand
  → RAG (official docs)  +  Hindsight recall (learned experience)
  → build context (separately labelled blocks) → reason (Claude) → tools
  → verify (V1–V6) → respond → update session state

session closed (resolved / escalated / timed out)
  → outbox → Memory Writer (R1–R5) → Hindsight retain → consolidation → observations
```

There is a single deterministic orchestrator rather than a multi-agent graph. Interfaces are left
open for specialist agents to be added later. Design details:
[`docs/architecture.md`](docs/architecture.md) · reference study:
[`docs/reference-analysis.md`](docs/reference-analysis.md) · spec: [`Requriments.md`](Requriments.md)

## Project status

All the POC code is written. **So far only the unit tests (30 passing) and lint have been
verified.** The Docker stack, the integration tests, the live learning evals and the end-to-end
demo have not been run yet. See [`claude-progress.md`](claude-progress.md) and
[`feature_list.json`](feature_list.json) for the per-feature status.

## How to use it

### Prerequisites

- Docker Desktop with **8 GB+ free memory**, because Hindsight loads local embedding and
  reranking models
- An Anthropic API key
- For local development: Python 3.11 and [uv](https://docs.astral.sh/uv/)

### 1. Configure

```bash
git clone https://github.com/tabulnaga/SelfLearningAgent.git
cd SelfLearningAgent
cp .env.example .env              # set ANTHROPIC_API_KEY; see the comments in the file for the other settings
```

### 2. Start the stack

```bash
docker compose up -d --build      # db, hindsight, api (migrates + ingests docs), worker
docker compose exec api python -m scripts.bootstrap_bank   # missions, directives, 4 knowledge pages
curl localhost:8000/health/ready
```

| Service | URL |
|---|---|
| Agent API | http://localhost:8000 |
| Hindsight API | http://localhost:8888 |
| Hindsight control plane (watch facts, observations and pages form) | http://localhost:9999 |

### 3. Run the end-to-end demo

```bash
uv sync
uv run python -m scripts.demo_poc
```

The demo plays three "Payment Pending" cases. Each one is resolved by restarting the Kafka
`payment-events` consumer. It then opens a **new** session and asks what to check first. The
answer combines the official runbook with the learned pattern, and labels which is which.

### 4. Talk to the agent

```bash
# Send a message. The session is derived from conversation_id.
curl -s localhost:8000/v1/messages -H 'content-type: application/json' \
  -d '{"conversation_id":"wa-+447700900123","text":"Payments stuck in Pending, what first?","external_message_id":"wamid.1"}'

# Inspect the session and its decision trace
curl -s localhost:8000/v1/sessions/<session_id>          # state, messages, tool log, memory job
curl -s localhost:8000/v1/sessions/<session_id>/trace    # full decision trace per turn

# Run a tool, then close the case (closing it triggers the learning loop)
curl -s -XPOST localhost:8000/v1/sessions/<id>/tools    -H 'content-type: application/json' \
  -d '{"name":"restart_consumer","arguments":{"consumer":"payment-events"}}'
curl -s -XPOST localhost:8000/v1/sessions/<id>/resolve  -H 'content-type: application/json' \
  -d '{"note":"consumer stalled; restart fixed it"}'
curl -s -XPOST localhost:8000/v1/sessions/<id>/escalate -H 'content-type: application/json' \
  -d '{"note":"...","team":"payments-l2"}'

# Reset or inspect the simulated payments platform
curl -s -XPOST localhost:8000/v1/sim/reset -H 'content-type: application/json' -d '{"stalled":true,"pending":3}'
curl -s localhost:8000/v1/sim/state
```

Other endpoints: `POST /v1/ingest` re-ingests documents, `POST /v1/memory/reflect` asks Hindsight
to reflect over learned memory, and `GET /health/live` is a liveness probe.

**Auth.** When `API_KEYS` is empty, every caller is a dev admin. That is allowed only in
`APP_ENV=sim`. Otherwise send `X-API-Key`. The key's role decides which document classifications
retrieval may return.

### 5. Add your own knowledge

Put Markdown documents in `data/docs/` and call `POST /v1/ingest`, or rebuild the `api` container.
Learned experience needs no setup: it builds up as cases close.

## Development

```bash
./scripts/init.sh                         # one shot: uv sync + unit tests + lint (+ integration if Docker is up)

uv sync                                   # Python 3.11, uv
uv run pytest                             # unit tests (no services needed)
docker compose up -d db
uv run pytest -m integration              # vertical slice, learning-loop plumbing, failure paths, offline evals
docker compose up -d db hindsight         # + ANTHROPIC_API_KEY
uv run pytest -m live tests/evals -s      # evals 2, 6, 7: real Hindsight + real LLM (slow, costs tokens)
uv run ruff check app workers scripts tests
uv run uvicorn app.api.main:app --reload  # API on :8000 (needs db; Hindsight optional)
uv run python -m workers                  # SessionMonitor + MemoryJobRunner
```

`app/container.py` is the only place that wires concrete implementations.
`tests/unit/test_architecture_boundaries.py` enforces the module boundaries as import checks.

## Layout

```
app/api            HTTP routes only
app/agent          orchestrator (fixed, traced steps), prompts, context blocks, provenance renderer
app/rag            ingest (parse, chunk), embeddings, pgvector/FTS store, hybrid retriever + rerank
app/memory         Hindsight adapter, bank bootstrap, Memory Writer (retention rules R1-R5)
app/sessions       Postgres session store: write-ahead turns, transactional closure + outbox
app/tools          typed tool registry + policy, simulated payments platform
app/verification   deterministic checks V1-V6 (checker != model)
app/security       auth, authorizer (scope before retrieval), PII redaction
app/llm            provider boundary (Anthropic, deterministic fake)
workers            SessionMonitor (idle -> TIMED_OUT), MemoryJobRunner (outbox -> retain)
data/docs          official documents · data/cases: scripted demo cases
tests              unit · integration · evals
```

## Model configuration

- App: `claude-opus-5-5` (structured outputs; server-side refusal fallback enabled).
- Hindsight retain and consolidation: `claude-opus-5-5`.
- Hindsight reflect and knowledge-page synthesis: `claude-haiku-4-5`. Hindsight's reflect agent forces
  named tool calls, which `claude-opus-5-5` rejects. Override with `HINDSIGHT_REFLECT_LLM_MODEL`.
- Set `LLM_PROVIDER=fake` to run the app with a deterministic fake model (no API key needed).
