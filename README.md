# Self-Learning Agentic RAG (POC)

A payments-operations support agent that combines three kinds of memory and keeps them separate:

| | Source | Answers |
|---|---|---|
| **Official knowledge** | Hybrid RAG over our documents (Postgres + pgvector + full-text, cross-encoder rerank) | "What does the documentation say?" |
| **Learned experience** | [Hindsight](../SelfLearnMemory/hindsight-main) bank `support-ops`: closed cases → facts → observations → knowledge pages | "What have we learned from past cases?" |
| **Working state** | Postgres sessions, messages, tool log, outbox, traces | "Where is this conversation right now?" |

The agent learns across sessions without fine-tuning. Closed cases are summarised by the
Memory Writer and retained in Hindsight, and Hindsight consolidates them into observations. New
sessions recall those observations next to the official procedure, and code labels each part with
its source.

Design: [`docs/architecture.md`](docs/architecture.md) · reference study: [`docs/reference-analysis.md`](docs/reference-analysis.md) · spec: [`Requriments.md`](Requriments.md)

## Quick start

Prerequisites: Docker Desktop (8 GB+ free memory recommended: Hindsight loads local embedding and
reranking models), and an Anthropic API key.

```bash
cp .env.example .env              # set ANTHROPIC_API_KEY
docker compose up -d --build      # db, hindsight, api (migrates + ingests docs), worker
docker compose exec api python -m scripts.bootstrap_bank   # missions, directives, 4 knowledge pages
curl localhost:8000/health/ready
python -m scripts.demo_poc        # spec §27 end-to-end (needs: uv sync, run on the host)
```

Hindsight's control plane on http://localhost:9999 shows facts, observations and pages as they form.

## Talking to the agent

```bash
# one inbound message = one call (n8n / WhatsApp / webhook friendly); the session comes from conversation_id
curl -s localhost:8000/v1/messages -H 'content-type: application/json' \
  -d '{"conversation_id":"wa-+447700900123","text":"Payments stuck in Pending, what first?","external_message_id":"wamid.1"}'

curl -s localhost:8000/v1/sessions/<session_id>                     # state, messages, tool log, memory job
curl -s localhost:8000/v1/sessions/<session_id>/trace               # full decision trace per turn
curl -s -XPOST localhost:8000/v1/sessions/<id>/tools   -d '{"name":"restart_consumer","arguments":{"consumer":"payment-events"}}' -H 'content-type: application/json'
curl -s -XPOST localhost:8000/v1/sessions/<id>/resolve -d '{"note":"consumer stalled; restart fixed it"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8000/v1/sessions/<id>/escalate -d '{"note":"...","team":"payments-l2"}' -H 'content-type: application/json'
curl -s -XPOST localhost:8000/v1/sim/reset -d '{"stalled":true,"pending":3}' -H 'content-type: application/json'
```

Auth: with `API_KEYS` empty (allowed only in `APP_ENV=sim`) every caller is a dev admin. Otherwise
send `X-API-Key`; roles `agent_user` / `operator` / `admin` decide which document classifications
retrieval may return. That decision is made in SQL, before the model sees anything.

## Development

```bash
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
data/docs          official documents · data/cases: scripted §27 cases
```

## Model configuration

- App: `claude-opus-5-5` (structured outputs; server-side refusal fallback enabled).
- Hindsight retain and consolidation: `claude-opus-5-5`.
- Hindsight reflect and knowledge-page synthesis: `claude-haiku-4-5`. Hindsight's reflect agent forces
  named tool calls, which `claude-opus-5-5` rejects. Override with `HINDSIGHT_REFLECT_LLM_MODEL`.
