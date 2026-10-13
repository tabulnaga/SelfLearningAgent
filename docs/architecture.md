# Architecture (Phase 2)

Status: **draft for approval. No code has been written yet** · 2026-10-04
Inputs: `../Requriments.md`, `reference-analysis.md`

This document covers the component diagram, data/query/memory flows, the session lifecycle,
failure paths and source-of-truth boundaries (§25 Phase 2). It also includes the memory design,
session lifecycle and harness decisions, which §22 lists as separate files; they will be split out
when they outgrow a section.

---

## 1. Source-of-truth boundaries

| | AUTHORITATIVE KNOWLEDGE | LEARNED EXPERIENCE | WORKING STATE |
|---|---|---|---|
| Answers | "What does the official documentation say?" | "What have we learned from previous cases?" | "Where is this conversation right now?" |
| Lives in | App Postgres: `rag_documents`, `rag_chunks` (pgvector + tsvector) | Hindsight bank `support-ops` (its own database) | App Postgres: `conversations`, `sessions`, `messages`, `tool_executions`, `memory_jobs`, `turn_traces` |
| Written by | Ingestion pipeline only (operator action) | **Memory Writer only**, after closure | Orchestrator and workers |
| Read by | Retriever, behind the Authorizer | Hindsight adapter (recall, knowledge pages) | Everyone, through `SessionStore` |
| Label in LLM context | `OFFICIAL KNOWLEDGE` · cite as `[D:<chunk_id>]` | `LEARNED EXPERIENCE` · cite as `[M:<memory_id>]` / `[P:<page_id>]` | Not shown as evidence. Tool results are cited as `[T:<tool_exec_id>]` |
| On conflict | **Wins** | Shown as complementary, with the conflict stated | n/a |
| If unavailable | Controlled answer: "cannot answer from documentation" | Continue without it; `memory_status=degraded` | Request fails (it is the system of record) |

The rule that prevents drift: **no component writes to more than one column of this table.**
Import-boundary tests enforce it (`rag/` and `memory/` never import each other; `agent/` imports
only interfaces).

---

## 2. Component diagram

```
                 n8n / WhatsApp / REST client                      operator
                              │ POST /v1/messages                     │ POST /v1/ingest, /sessions/{id}/resolve|escalate
                              ▼                                       ▼
┌──────────────────────────────────────────── Agent API (FastAPI) ─────────────────────────────────────────────┐
│  security/  auth → Principal ──► Authorizer → RetrievalScope (tenant, classifications)                       │
│                                                                                                              │
│  agent/Orchestrator  (fixed sequence of named steps, each traced)                                            │
│    load_session → understand → ┬ retrieve_official ─► rag/Retriever ──────────► Postgres (app db)             │
│                                └ recall_learned ───► memory/HindsightAdapter ─► Hindsight API                │
│    → build_context → reason ─► llm/LLMClient                                                                 │
│    → run_tools ─► tools/Registry + ToolPolicy ─► simulated payments env                                      │
│    → verify ─► verification/Verifier (deterministic)                                                         │
│    → render (code composes provenance sections) → persist_turn ─► sessions/SessionStore                      │
│                                                                                                              │
│  observability/  JSON logs + turn_traces                         health: /health/live, /health/ready         │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                              │ status change → memory_jobs row (same transaction)
                              ▼
┌──────────────────────────────── Worker process ─────────────────────────────┐
│  SessionMonitor (every MONITOR_INTERVAL_S): ACTIVE & idle > timeout → TIMED_OUT│
│  MemoryJobRunner (outbox, SKIP LOCKED): MemoryWriter → Hindsight retain_batch  │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                 Hindsight: facts → consolidation → observations → knowledge pages   (no code of ours)
```

### Interfaces (Python `Protocol`s, in each package's `interfaces.py`)

```python
class Retriever:        def retrieve(self, query: str, scope: RetrievalScope, filters: Filters, k: int) -> list[Evidence]
class LearnedMemory:    def recall(self, query: str, scope: MemoryScope) -> MemoryRecall          # never raises; status=degraded on failure
                        def knowledge_pages(self, scope: MemoryScope) -> list[KnowledgePage]
                        def retain_case(self, record: CaseRecord) -> RetainReceipt               # idempotent per case
class SessionStore:     def begin_turn(...) / append_message(...) / record_tool(...) / close(...) / find_idle(...)
class LLMClient:        def complete_structured(self, system: str, messages: list, schema: type[BaseModel]) -> (BaseModel, Usage)
class Tool:             name, input_model, output_model, side_effect: Literal["read","write"], def run(input) -> ToolResult
class Check:            def __call__(self, draft: DraftAnswer, ctx: TurnContext) -> CheckResult
```

Specialist agents (CRM/Billing/Network, §15) would plug in later as alternative `reason`
implementations selected by intent. The orchestrator signature does not change. This is not built in v1.

---

## 3. Query flow (one inbound message = one execution)

| # | Step | Deterministic? | On failure |
|---|---|---|---|
| 0 | **Ingress dedupe:** `(channel, external_message_id)` unique. A webhook redelivery returns the stored reply. | yes | — |
| 1 | **load_session:** take a per-session advisory lock (`pg_advisory_xact_lock`), so two messages in the same case are processed in order. Resolve the conversation (channel thread) to its open session, or create one (`OPEN`). **Write the user message before reasoning** (write-ahead), set `last_activity_at`, status → `ACTIVE`. | yes | 500, nothing half-written |
| 2 | **authorize:** Principal → `RetrievalScope` (tenant, allowed classifications) and `MemoryScope` (bank, tags). | yes | 403 |
| 3 | **understand:** LLM structured output `{intent, domain, search_query, is_smalltalk}`, with a rule-based fallback. | LLM + fallback | fallback rules |
| 4a | **retrieve_official:** hybrid search (§5) under `RetrievalScope`. | yes | `rag_status=failed` → controlled answer at step 8 |
| 4b | **recall_learned:** `recall(prefer_observations, types=[observation,experience,world], tags=domain, budget=low)` plus knowledge pages cached on the session. 4a and 4b run **concurrently**. Hard timeout `HINDSIGHT_TIMEOUT_S`. | yes | `memory_status=degraded`, continue |
| 5 | **build_context:** two separate, labelled blocks, each with stable short IDs. Retrieved text is wrapped as quoted data, never as instructions. | yes | — |
| 6 | **reason:** LLM returns a `DraftAnswer`: a list of `claims {text, kind: official\|learned\|tool\|general, cites[]}`, `conflicts[]`, `proposed_tools[]`, `proposes_resolution`. | LLM | 1 retry, then controlled answer |
| 7 | **run_tools:** validate each proposal against the tool's input model, then `ToolPolicy`: **read** tools run; **write** tools run only if policy allows (§11 D2). Results are stored in `tool_executions`. If any ran, there is one more `reason` pass with the results. Max `MAX_TOOL_ROUNDS=2`. | yes | tool error is recorded as failure; never reported as success |
| 8 | **verify:** deterministic checks (§7). On failure: one regeneration with the failure reasons attached. Second failure: **controlled fallback** (official excerpts with citations, no synthesis), `verification=failed`. | yes | fallback |
| 9 | **render:** **code** (not the LLM) arranges the verified claims into "From official documentation" and "From past cases (learned, not official policy)" sections, then conflicts, citations and suggested next step. | yes | — |
| 10 | **persist_turn:** assistant message, `turn_traces` row, counters. | yes | — |

Provenance is therefore enforced three times: by separate context blocks, by claim `kind` checked
against citation type, and by a renderer that places each claim under its section.

---

## 4. Session lifecycle

```
conversation (channel thread, e.g. one WhatsApp number) 1 ──── * session (one case)

            first message              explicit resolve (user/operator/agent+confirm)
   (new) ───────────────► OPEN ─► ACTIVE ───────────────────────────────► RESOLVED ─┐
                                    │  ▲ each message:                               │
                                    │  └ last_activity_at = now()                    │  memory job
                                    ├─ escalate (operator / policy) ──► ESCALATED ───┼─ done|skipped ─► CLOSED
                                    └─ SessionMonitor: idle > timeout ► TIMED_OUT ───┘
```

- **Challenge to §8:** the spec's six states mix *lifecycle* with *outcome*. We keep the six states
  as given, add `closure_reason` and `closed_at`, and define `CLOSED` as "the memory job reached a
  terminal state". This is what makes "is learning finished for this case?" a single query.
- **Conversation vs session.** `conversation_id` is the channel thread and lives for months.
  `session_id` is one case. A message arriving on a conversation whose session is
  RESOLVED/TIMED_OUT/CLOSED opens a **new** session. Without this split, a WhatsApp user's second
  incident would be appended to their first, closed case.
- **Closure is transactional.** The status change and the insert into `memory_jobs` (unique on
  `session_id`) commit together (transactional outbox). A crash cannot produce a closed session
  with no memory job, or a job twice.
- **No blocking waits (§10).** The SessionMonitor is a loop in the worker process. Every
  `MONITOR_INTERVAL_S` it runs `UPDATE sessions SET status='TIMED_OUT' … WHERE status='ACTIVE' AND
  last_activity_at < now() - make_interval(mins => $SESSION_TIMEOUT_MINUTES) … FOR UPDATE SKIP
  LOCKED RETURNING id` and inserts jobs. It is safe to run several replicas.
- **Resolution needs evidence.** The agent may *propose* resolution, but status moves to
  RESOLVED only on explicit confirmation from the user or operator
  (`POST /v1/sessions/{id}/resolve`), or a message the understand step classifies as a confirmation.

---

## 5. Authoritative RAG pipeline

**Ingestion** (`POST /v1/ingest` or `scripts/ingest.py`): parse (Markdown/text in v1; PDF later)
→ heading-aware chunking (target `CHUNK_TOKENS=400`, overlap 60, heading path kept as metadata)
→ metadata (`doc_id`, `title`, `source`, `classification`, `tenant_id`, `domain`, `version`,
`content_hash`) → embed → upsert. A document is replaced as a unit when its `content_hash` changes,
so re-ingestion is idempotent.

**Retrieval** `retrieve(query, scope, filters, k)`:
1. Query processing: normalise; use the `search_query` from the understand step.
2. Two arms in Postgres, **both filtered in SQL** by `tenant_id`, `classification = ANY(scope)` and
   metadata filters: pgvector cosine top-`RAG_CANDIDATES` (HNSW), and `tsvector` `ts_rank_cd`
   top-`RAG_CANDIDATES`.
3. Reciprocal Rank Fusion (k=60).
4. Cross-encoder rerank of the fused set → `RERANK_TOP_K`.
5. Score floor `RAG_MIN_RERANK_SCORE`. Below the floor the system returns *no evidence* rather than
   weak evidence, which is what lets the agent say "the documentation does not cover this".
6. Every result is an `Evidence {doc_id, doc_title, chunk_id, source, heading_path, scores{vector,
   keyword, fused, rerank}, metadata, content}`.

The `Retriever` interface hides Postgres. Swapping in OpenSearch, Qdrant or similar means one new
adapter.

---

## 6. Learned memory design (Hindsight)

### 6.1 Bank bootstrap (`scripts/bootstrap_bank.py`, idempotent, runs at deploy)

- `create_bank("support-ops", enable_observations=True, enable_text_search/graph/temporal/reranking=True)`
- `update_bank_config(...)`:
  - **retain_mission:** "Extract incident facts: symptom, affected component, environment,
    each remediation attempted and whether it worked, confirmed root cause, outcome. Ignore
    pleasantries and speculation not confirmed by tool results."
  - **observations_mission:** "Form durable beliefs about recurring incident patterns, which
    remediations repeatedly resolve them, which repeatedly fail and under what conditions, and
    environment-specific constraints. Keep the count of supporting cases."
  - **reflect_mission:** "Answer as an operations engineer. Distinguish learned experience from
    official procedure. Say when evidence is thin."
- Directives (bind reflect only): "Never present learned experience as official policy." "Cite the
  memories each claim relies on."
- Knowledge folder **Operations** with 4 pages (§12): *Known Recurring Incident Patterns*, *Proven
  Resolutions*, *Failed Remediation Attempts*, *Environment-Specific Constraints*. Each is looked up
  by name before being created, so reruns do not duplicate them.

### 6.2 Recall

`recall(query, types=["observation","experience","world"], prefer_observations=True,
tags=[f"domain:{domain}"], tags_match="any", budget=RECALL_BUDGET, max_tokens=RECALL_MAX_TOKENS)`.
Results become `LearnedItem {memory_id, type, text, proof_count?, source_count}` and **observations
are listed first**. Knowledge pages are read once per session (no LLM) and cached on the session
row with `fetched_at`. They are also LEARNED EXPERIENCE, even though "Knowledge Page" sounds official.

### 6.3 Memory Writer (the only path to `retain`)

```
memory_jobs row (pending) ─► claim (SKIP LOCKED, attempts++) ─► load session, messages, tool_executions
  ─► R1–R4 deterministic retention gate ──fail──► status=skipped(reason)  ─► session CLOSED
  ─► build CaseRecord:
        deterministic fields from tool_executions: actions_attempted[], failed_actions[], successful_action
        LLM fills only: issue, root_cause, lessons[], confidence   (structured output)
  ─► validate (R5): every action named in prose exists in tool_executions; successful_action has
        status=success; outcome consistent with closure_reason; confidence ≥ RETAIN_MIN_CONFIDENCE
  ─► retain_batch(items=[{content, context, tags, metadata, timestamp=closed_at,
                          observation_scopes=[["domain:<d>"]]}],
                  document_id="case-<session_id>", update_mode="replace",
                  retain_async=RETAIN_ASYNC, operation_id=uuid5(NS, document_id + content_hash))
  ─► store operation_id, status=submitted ─► (poll /operations or webhook) ─► done, memory_written=true ─► CLOSED
  on error: status=pending, next_attempt_at = now + backoff(attempts); after MAX_ATTEMPTS → failed (alert)
```

**Retention criteria:**

| ID | Rule |
|---|---|
| R1 | Intent is not `greeting`, `smalltalk` or `faq_only`. |
| R2 | At least one tool execution, **or** an operator-supplied outcome note. |
| R3 | `closure_reason` ∈ {RESOLVED, ESCALATED}, **or** TIMED_OUT *with* a successful remediation in the tool log. A timed-out chat with no outcome teaches nothing. |
| R4 | Not already `memory_written` for the same `content_hash`. |
| R5 | Post-extraction validation as above. |

**Why it cannot duplicate:**
(a) `document_id` per case makes Hindsight upsert, and unchanged content skips extraction
(reference-analysis §1.2); (b) `operation_id` is derived from the content, so a retried submit is
recognised; (c) `memory_jobs.session_id` is unique. Eval 5 tests this by running the job twice and
closing twice.

**Content format retained:** a short natural-language case narrative (Hindsight extracts better
from prose than from JSON), with the structured fields repeated as labelled lines.
`context = "Closed incident case summary written by the support operations agent; describes what
operators observed, what was tried, and what happened."`

### 6.4 Tag vocabulary (closed)

`source:case` · `domain:<payments|billing|network|crm>` · `outcome:<resolved|escalated|unresolved>`
· `env:<prod|staging>` · `tenant:<id>` (only while tenancy stays bank-shared).
**Never** session, case or user IDs; those go in `metadata` (reference-analysis §1.3).

---

## 7. Verification (deterministic, maker ≠ checker)

| Check | Rule | Spec §16 question |
|---|---|---|
| V1 citations-valid | Every `[D:]/[M:]/[P:]/[T:]` cited exists in this turn's context. | Are citations valid? |
| V2 grounded | Every `official` or `learned` claim has ≥1 citation. If official evidence exists, ≥1 claim cites it. | Supported by evidence? |
| V3 source-discipline | `official` claims cite only `D`. `learned` claims cite `M`/`P`. A claim citing only `M`/`P` must not contain policy language (`must`, `policy`, `required`, `official`). | Learned presented as policy? |
| V4 conflict-surfaced | If the reasoner set `conflicts[]`, the rendered answer contains the conflict note and the official position. | Contradicting docs? |
| V5 tool-truth | A `tool` claim must cite a `T` whose status matches what the claim asserts. | Did the tool succeed? |
| V6 no-false-resolution | `proposes_resolution` requires a successful **write** remediation in this session, or explicit user confirmation. | Claimed resolution when it failed? |

Checks return actionable messages (L10 harness principle), and those messages are fed back for one
regeneration. An optional LLM-judge groundedness score runs **only in evals**, never on the hot path.

---

## 8. Failure paths

| Failure | Behaviour | User impact |
|---|---|---|
| Hindsight down or slow | Timeout → `memory_status=degraded`, answer from RAG only, the answer notes that past-case memory was unavailable. | None beyond the note |
| Vector arm fails | Keyword arm alone, `rag_status=partial`. | Possibly lower quality |
| Both arms fail / DB down | Controlled answer + 503 for reads. Session writes need the DB. | Explicit error |
| No evidence above floor | "The documentation does not cover this", plus learned experience if any, clearly labelled. | Honest answer |
| LLM error or timeout | Retry once with backoff, then controlled fallback (excerpts + citations). | Degraded answer |
| Tool error | Recorded as `status=error`, cannot be claimed as success (V5), surfaced to the user. | Honest |
| Verification fails twice | Fallback answer, `verification=failed` in trace. | Degraded answer |
| Memory Writer / retain fails | Job back to pending with exponential backoff. **The user turn never waits on retain.** | None |
| Worker crash mid-job | Claimed rows' leases expire (`locked_until`) and are reclaimed. Retain is idempotent. | None |
| Duplicate webhook delivery | Ingress dedupe returns the stored reply. | None |

---

## 9. Harness mapping (§13)

| Subsystem | Implementation |
|---|---|
| Instructions | `agent/prompts.py`: role, boundaries, citation contract, source rules. Versioned; `prompt_version` is recorded in traces. |
| Tools | Typed Pydantic in/out models, `side_effect` tag, allow-list registry, `ToolPolicy` outside the LLM. |
| Environment | `.env`-driven `Settings` (pydantic-settings), Docker Compose, pinned images, `uv.lock`. |
| State | Postgres `SessionStore`. Nothing lives only in LLM context. |
| Feedback | Tool results, verification results and user/operator outcomes are persisted and feed both reasoning and the Memory Writer. |
| Verification | §7 plus evals (§19). |
| Recovery | Retries with backoff, idempotency keys at ingress, outbox and retain, leases, graceful degradation (§8), resumable jobs. |
| Observability | One JSON log line per step with `request_id`, `trace_id`, `conversation_id`, `session_id`, plus a `turn_traces` row holding the full decision path: query, intent, retrieval IDs and scores, memory IDs, tool calls and results, latencies, LLM usage, verification, outcome. `memory_jobs` holds the retain `operation_id` and the retention decision **with its reason**. PII redaction runs on log output (emails, phone numbers, card-like numbers). Secrets never reach the logger. |
| Human intervention | Escalate endpoint; write tools gated by policy; operator outcome notes. |

Security extension points (§20): `auth.py` (API key → Principal; OIDC later), `authorizer.py`
(RBAC now, ABAC hook), tenant in every RAG query and in the memory scope, document
`classification`, prompt-injection mitigations (retrieved text quoted as data; tools validated by
schema and policy, never by the model's say-so), and the audit trail through `turn_traces` and
`tool_executions`.

---

## 10. Technology choices (§21: problem · why not existing · operational cost)

| Dependency | Problem it solves | Why the existing stack can't | Operational cost |
|---|---|---|---|
| Python 3.11 + FastAPI + uvicorn | HTTP API with typed models | — (spec baseline) | Low |
| PostgreSQL 17 + pgvector | Session state, outbox, RAG vectors **and** full-text in one store | One engine covers state, queue, vector and keyword search | One service, already needed by Hindsight |
| psycopg 3 (+ pool) | DB access | — | None. Plain SQL plus numbered migration files with a 40-line runner; no ORM. |
| hindsight-client 0.10.2 | Learned memory | Mandated | Hindsight server container + its LLM spend |
| pydantic-settings | Env config | Standard for FastAPI | None |
| fastembed (ONNX) | Local embeddings (`bge-small-en-v1.5`) and cross-encoder rerank, without PyTorch | pgvector stores vectors but does not compute them | ~200 MB model cache volume; CPU inference |
| anthropic SDK | Default LLM adapter, behind `LLMClient` | — | API key. Other providers are one adapter each. |
| pytest (+ testcontainers optional) | Tests | — | Dev only |

**Rejected:**
- **LangGraph.** Our graph is a fixed, linear sequence with one bounded tool loop. State already
  persists per message in Postgres, because each webhook is a new execution, so LangGraph's
  checkpointer would be a **second** state store to keep consistent with ours. Plain code with named,
  traced steps gives the same auditability (L14) with less to learn. Revisit when routing becomes
  conditional across specialist agents.
- **Redis, Celery, Kafka for jobs.** `SELECT … FOR UPDATE SKIP LOCKED` on the outbox meets POC
  throughput with no new infrastructure.
- **A separate vector database.** pgvector + HNSW is sufficient at POC scale, and the `Retriever`
  interface keeps it swappable.
- **OpenTelemetry in v1.** See reference-analysis §2.4.

---

## 11. Challenges to the requested design, and unresolved decisions

### Where the spec is technically wrong or underspecified (§29 "challenge it")

1. **§2 diagram order.** Session state appears only *after* the response. It must be loaded and the
   user message written **first** (write-ahead), or a crash mid-turn loses the message and
   concurrent webhooks race. Fixed in §3 steps 0–1.
2. **§8 states mix lifecycle with outcome.** Resolved with `closure_reason` and `CLOSED` = memory job
   terminal (§4).
3. **§9 "mark memory_written = true" is not idempotency by itself.** There is a crash window between
   retain and the flag. The real guarantee is Hindsight's `document_id` upsert plus the outbox (§6.3).
4. **§27 "verify Hindsight produces an observation similar to …" is not deterministic.** The wording
   is generated by Hindsight's LLM. It can be evaluated only in a **live** tier, with a tolerant
   matcher (required concepts: *payment pending*, *consumer*, *restart*, *repeated*) or an LLM judge.
   An offline unit test cannot prove learning. §12 splits the eval tiers accordingly.
5. **"Retain on closure" includes TIMED_OUT,** but a timed-out chat usually has no outcome. Retaining
   it would create the noise §7 warns about. Rule R3 retains it only when there is evidence.
6. **Tag design silently controls consolidation** (reference-analysis §1.3). The spec lists tags as a
   detail, but here a tagging mistake breaks learning without any error.
7. **"Knowledge Pages" are learned, not authoritative.** Loading them at boot is good. Labelling them
   anything but LEARNED EXPERIENCE would break §4's provenance rule.
8. **Cost.** Each retained case triggers Hindsight extraction, consolidation and refreshes of affected
   pages: several LLM calls on Hindsight's side. That is acceptable because retain happens once per
   case and runs async, but it is why per-message retain would be expensive as well as noisy.

### Decisions needed from you

| # | Decision | Default I'll use if you don't choose |
|---|---|---|
| D1 | LLM provider and key for the app **and** for Hindsight extraction | Anthropic for both (app: `claude-sonnet-5-5`; Hindsight: its default `claude-haiku-4-5`). No key is set in this environment. |
| D2 | Who executes remediation (write) tools? | Agent proposes. In `SIM` mode write tools auto-run against the **simulated** payments environment. In any non-sim mode they require operator approval. |
| D3 | Tenancy | Single tenant, single bank. Tenant ID is still carried everywhere. |
| D4 | Embeddings | Local fastembed (no data leaves the machine, no key). |
| D5 | Docker daemon not running here | You start Docker Desktop. Unit and offline evals run without it. |

---

## 12. Smallest useful POC, structure and plan

### POC scope

One tenant, one bank, one domain (`payments`), a **simulated payments environment** (consumer
state, lag, pending payments) that makes tool results real and deterministic, 5 official documents,
an HTTP API (no UI; n8n or curl can drive it), the worker, and `scripts/demo_poc.py`, which plays the
§27 scenario end to end: 3 cases → wait for consolidation → new session → combined answer.

### Project structure (deviations from §22 are marked ★)

```
SelfLearningAgent/
  CLAUDE.md  feature_list.json★  claude-progress.md★      # development harness (Level B)
  pyproject.toml  uv.lock  .env.example  Dockerfile  docker-compose.yml
  docker/postgres-init.sql                                # creates app + hindsight databases
  app/
    config.py
    api/            messages, sessions, ingest, health routes
    agent/          orchestrator, steps, context builder, prompts, renderer★
    rag/            ingest/(parse, chunk), embed, pg_store, retriever, rerank, interfaces
    memory/         hindsight_adapter, bank_setup, writer, retention, interfaces
    sessions/       models, pg_store, lifecycle (state machine)
    tools/          registry, policy★, payments_sim★
    verification/   checks, verifier
    security/★      auth, authorizer, redaction
    llm/★           interfaces, anthropic, fake
    observability/  logging, trace
    db/★            pool, migrations/*.sql, migrate
  workers/          __main__ (runs both loops), session_monitor, memory_jobs
  data/docs/        official documents (runbooks, policies)
  data/cases/       scripted example cases for the demo and evals
  scripts/          init.sh★, bootstrap_bank.py, ingest.py, demo_poc.py
  tests/unit  tests/integration  tests/evals
  docs/             reference-analysis.md, architecture.md (+ split-outs later)
```

★ Rationale: `llm/`, `security/`, `db/` keep provider, policy and driver code out of business
packages (§26.7). Evaluation code lives under `tests/evals` because it is test code. `renderer` and
`policy` are where the deterministic guarantees live, so they get their own modules.

### Implementation plan (each phase ends with evidence recorded in `feature_list.json`)

| Phase | Delivers | Exit evidence |
|---|---|---|
| 3a Skeleton | Config, DB, migrations, compose (pg + hindsight + api + worker), health endpoints, logging, `init.sh` | `docker compose up` healthy; `/health/ready` checks DB and Hindsight |
| 3b RAG | Ingest + hybrid retrieval + rerank + 5 docs | Unit tests; retrieval eval (recall@5 on 10 labelled questions) |
| 3c Vertical slice | Sessions, orchestrator, Hindsight recall, LLM, verifier, renderer | Integration test: a question returns cited official answer; Hindsight down → still answers |
| 4 Learning loop | Tools + sim env, closure endpoints, monitor, outbox, Memory Writer, bank bootstrap | Closed case → one Hindsight document; rerun → no duplicate |
| 5 Reliability | Retries/backoff, leases, ingress dedupe, timeouts, failure tests | Failure-injection tests for every row of §8 |
| 6 Evaluation | Offline tier (fake LLM + fake memory: evals 1, 3, 4, 5) and live tier (real Hindsight + LLM: evals 2, 6, 7) + `demo_poc.py` | All 7 §25 evals passing, report in `docs/` |
