# Reference Analysis (Phase 1)

Status: draft for approval · 2026-10-04 · spec: `../Requriments.md` §1, §25 Phase 1

Both reference repositories were studied from the local copies in this workspace, not fetched:

| Spec reference | Local copy studied |
|---|---|
| `vectorize-io/hindsight` | `../../SelfLearnMemory/hindsight-main/` (v0.10.2; outer copy only) |
| `walkinglabs/learn-harness-engineering` | `../../learn-harness-engineering-main/` |

Every Hindsight claim below was checked against source, not only against the workspace guide
`SelfLearnMemory/BUILDING_A_SELF_LEARNING_AGENT.md`. Where the guide and the source disagree, the
source wins, and the disagreement is noted.

---

## 1. Hindsight — what it is and how we will use it

Hindsight is a memory **server** (FastAPI + PostgreSQL/pgvector) with its own LLM, embedding model
and cross-encoder. We call it over HTTP through the `hindsight-client` Python package
(`hindsight-clients/python/hindsight_client/hindsight_client.py`). We do not embed, fork or patch it.

### 1.1 Features relevant to this project

| Concept | What the source actually does | Verified at |
|---|---|---|
| **Bank** | Isolated memory store. There are no cross-bank queries; a second bank means a second call and a merge we write ourselves. `create_bank` is **create-or-update**, so a bootstrap script can run it repeatedly. | client `create_bank` docstring |
| **retain()** | Extracts facts with an LLM. Takes `content`, `context`, `tags`, `metadata`, `timestamp`, `document_id`, `update_mode`, `retain_async`, `operation_id`. | client L457–517 |
| **Facts** | Two raw types: `world` (about others) and `experience` (the agent's own). Which type a fact gets depends on **who is speaking**, and the `context` string sets that. | guide §1.3, recall `types` param |
| **Observations** | Built **automatically** after retain by background consolidation. Each one cites its source facts and carries a proof count. New evidence refines an observation instead of overwriting it. `enable_observations=True` is the switch for the whole learning layer. | `memory_engine.submit_async_consolidation`, `create_bank(enable_observations)` |
| **Mental models / Knowledge pages** | A standing question (`source_query`) whose answer Hindsight keeps up to date incrementally. A knowledge page is a mental model with defaults chosen and a folder location. | client `create_mental_model`, `create_knowledge_page` |
| **recall()** | Four retrieval arms (semantic, BM25, graph, temporal), fused with RRF and reranked by a cross-encoder. Supports `types`, `tags` + `tags_match`, `budget`, `min_scores`, `trace`. | client L618–700 |
| **prefer_observations** | Drops raw facts that a returned observation was built from. **Has no effect unless `types` includes both `observation` and a raw type.** The guide omits this condition. | client L663 docstring |
| **reflect()** | Agentic loop of up to 10 iterations over mental models → observations → facts. Applies disposition and directives, and validates the IDs it cites. Expensive. | guide §6, client `reflect` |
| **Missions** | `retain_mission` (what to extract), `observations_mission` (what to consolidate; **replaces** the built-in rules), `reflect_mission`. `mission` and `disposition_*` on `create_bank` are **deprecated**; use `update_bank_config`. The guide still shows the deprecated form. | client L858–900 |
| **Directives** | Binding rules for reflect, optionally scoped by tag. | client `create_directive` |
| **Tags** | Scope recall, reflect, mental models and directives, **and consolidation** (see §1.3). | http.py `observation_scopes` |
| **Context** | Free text that tells extraction who is speaking. It decides world vs experience. | guide §1.3 |
| **Async retain** | `retain_async=True` returns at once with an operation ID that we can poll at `/operations/{id}`. | http.py L8032 |
| **Webhooks** | Events `retain.completed`, `consolidation.completed` (with created/updated/deleted counts), `memory_defense.triggered`. | `webhooks/models.py` |
| **Health** | `/health`, `/health/ready`, `/health/live`, `/version`. | http.py L5474–5527 |
| **Providers** | LLM: `openai` (default), `anthropic`, `gemini`, `ollama`, … and **`mock`**. Embeddings and reranker default to `local` (`BAAI/bge-small-en-v1.5`, `ms-marco-MiniLM-L-6-v2`). | config.py L1091–1275 |
| **Database** | Its own Postgres, with schema set by `HINDSIGHT_API_DATABASE_SCHEMA`. A compose variant for an external PG exists. Hindsight runs its own Alembic migrations. | `docker/docker-compose/external-pg` |

### 1.2 Idempotency — what Hindsight gives us (key finding)

The spec requires that "the same closed case must not create duplicate learned memories" (§7, §9).
Hindsight already provides most of what that needs:

1. **`document_id` makes retain an upsert.** With the default `update_mode="replace"`, re-retaining
   the same `document_id` deletes the old facts and re-processes. If the content is **unchanged**,
   chunk-level delta retain **skips the LLM entirely** (`tests/test_delta_retain.py::
   test_delta_retain_unchanged_content_skips_llm`). So one case maps to one document
   (`document_id = "case-<session_id>"`), and a duplicate retain is a no-op, not a duplicate.
2. **`operation_id` deduplicates async submissions.** A caller-supplied UUID makes an async retain
   retry-safe. Reusing it for a *different* payload returns HTTP 409
   (`RetainOperationConflictError`). It is ignored for synchronous retain.

So the application's `memory_written` flag is a **progress marker**, not the safety mechanism.
That matters, because a flag alone has a crash window: a retain can succeed and the process can
die before the flag is written. The design in `architecture.md` §6 relies on (1) for correctness
and on (2) plus an outbox table for retries.

### 1.3 Tags decide what consolidates together (key finding — a silent POC killer)

`observation_scopes` defaults to `"combined"`, which means **one consolidation pass per exact tag
set**. If each case carried a per-case tag (`case:123`), the three Payment Pending cases in §27
would never consolidate into a shared observation, and nothing would report an error. Rules that
follow from this:

- Per-case identifiers go in `metadata` and `document_id`, **never in tags**.
- The tag vocabulary is small, closed and stable (`architecture.md` §6.4).
- The Memory Writer passes `observation_scopes` explicitly (`[["domain:payments"]]`-style), through
  `retain_batch` items, so consolidation grouping is a decision we make rather than a side effect of
  tagging. The `retain()` wrapper does not expose this parameter; `retain_batch` items do.

### 1.4 What we will adopt from Hindsight

| Adopt | Why |
|---|---|
| Hindsight server in Docker, pinned to `0.10.2`, sharing our Postgres **server** but in its own **database** | §24 says not to duplicate infrastructure. A separate database keeps Hindsight's migrations away from ours. |
| One bank for the POC, `support-ops`, with tags for scoping | No real isolation need yet (guide §1.1). The bank ID comes from config, so per-tenant banks are a later configuration change. |
| `retain_batch` with `document_id`, `update_mode="replace"`, `retain_async=True`, deterministic `operation_id`, explicit `observation_scopes` | Idempotency (§1.2) and controlled consolidation (§1.3). |
| `recall(types=["observation","experience","world"], prefer_observations=True, budget="low")` on the hot path | Prefers settled belief over raw facts at low latency. |
| Explicit `retain_mission`, `observations_mission`, `reflect_mission` written in a bootstrap script | The spec asks for these (§12), and accepting defaults silently is how banks drift. |
| Four knowledge pages in one folder, matching the spec's §12 examples | The spec says to start small. They are rebuilt by Hindsight when in-scope knowledge consolidates. |
| Reading knowledge pages at session start, labelled LEARNED EXPERIENCE, with staleness shown | A cheap DB read with no LLM, and settled knowledge is present on turn 1. |
| `mock` LLM provider for Hindsight in CI-style integration tests | Lets us test the plumbing (retain → operation → document upsert) without paying for extraction. |
| Health and operations endpoints | Health checks (§25 Phase 5) and retain confirmation. |

### 1.5 What we will NOT adopt from Hindsight, and why

| Not adopted | Why |
|---|---|
| Hindsight as the RAG store for official documents | Forbidden by §3 and §26.1. Official documents go through our own pipeline and index. |
| `reflect()` on the hot path | It runs its own agentic loop and writes its own synthesized answer, which would blur our OFFICIAL vs LEARNED provenance. v1 uses recall + our LLM. `reflect` is exposed only on a separate analysis endpoint (§6 "use selectively"). |
| Memory-as-tools (guide Pattern A) | It is non-deterministic: models under-call memory tools. The spec wants recall to be infrastructure (§4, §14). |
| Per-turn retain (guide Pattern B's retain node) | Explicitly forbidden by §7. Retain happens only in the Memory Writer, after closure. |
| The framework integrations (`hindsight-langgraph`, etc.) | We use the plain client; see the LangGraph decision in `architecture.md` §10. |
| Recreating consolidation or synthesis externally | Forbidden by §12. We configure missions and pages and let Hindsight do the work. |
| Hindsight's MCP server, CLI, control plane as dependencies | Not needed by the application. The control plane UI (`:9999`) is kept as an **operator** tool for watching observations form. |

---

## 2. Learn Harness Engineering — what it is and how we will use it

### 2.1 What the repository actually is

It is a 14-lecture course plus 8 projects about **the environment around a coding agent** (Claude
Code, Codex): repository-as-system-of-record, `AGENTS.md`, feature lists, progress files,
initialization scripts, verification gates, observability, clean handoffs, loops and graphs. Its
project code is an Electron knowledge-base app whose Q&A service is a **mock** (no real LLM).

That has a direct consequence. **The course is not a runtime framework for a production agent, and
there is nothing to import.** Its principles apply at two levels, and we use both:

- **Level A — runtime harness:** the operational environment around *our support agent*
  (§13 of the spec). Five subsystems from Lecture 02: instructions, tools, environment, state,
  feedback. Verification from Lectures 09 and 10, observability from Lecture 11, external state and
  maker/checker separation from Lecture 13, nodes/edges/routing from Lecture 14.
- **Level B — development harness:** the environment around *the coding agent building this repo*.
  Here the course's templates apply almost directly: a short `CLAUDE.md` map, `feature_list.json`,
  `claude-progress.md`, an `init` script, a clean-state checklist.

### 2.2 Principles relevant to this project

| Principle (source) | Meaning in the course | How it maps to our runtime |
|---|---|---|
| Five-subsystem harness (L02) | Instructions, tools, environment, state, feedback, each with clear responsibility | The spec's §13 lists these same headings. Each one becomes a component (`architecture.md` §9). |
| External state / system of record (L03, L05, L13) | "Models forget everything between runs; memory must live on disk." | Session state lives in Postgres, never in the LLM context. Each webhook message rebuilds context from the store. |
| Clean-state / session integrity (L12) | A session either commits fully or rolls back to a consistent state | Each turn's write is transactional. Closure and its outbox job are written in one transaction. |
| Idempotent cleanup (L12) | Operations give the same result however many times they run | Retain, session closure and the bank bootstrap are all idempotent. |
| Premature completion and calibration bias (L09) | Agents are systematically overconfident about being done | The verifier refuses resolution claims that tool evidence does not back. The Memory Writer refuses `outcome=resolved` without a successful tool record. |
| Three-layer termination check (L09) | Static → runtime → system-level verification | Per-answer deterministic checks (citations exist, sources labelled) → tool-result checks → system-level evals (§19). |
| Maker/checker separation (L09, L13) | "A model grading its own work is untrustworthy" | The verifier is deterministic code, not the same LLM call. An optional LLM judge is used only in evals. |
| Agent-oriented error messages (L10) | Failures should say how to fix | When verification fails, the reasons are fed back for at most one bounded regeneration, then the agent falls back to a controlled answer. |
| Executable architectural rules (L10) | Turn boundary rules into automated checks | Import-boundary tests: `agent/` must not import storage drivers; `rag/` and `memory/` must not import each other. |
| Layered observability, task trace (L11) | Runtime signals plus process artifacts; replayable decision path | Structured JSON logs plus a per-request `trace` record from question → retrieval IDs → memory IDs → tools → verification → answer → retain op. |
| Evaluator rubric (L11) | Fixed scoring categories, not taste | The seven eval dimensions in §19 each get their own scorer and threshold. |
| Graph: nodes, edges, state, routing; failure/retry edges (L14) | Declare structure up front for auditability | The orchestrator is an explicit, fixed sequence of named steps with declared failure edges, written as plain code (§10). |
| Time- and event-driven loops (L13) | Scheduled heartbeat vs event triggers | The session monitor is a time-based loop. Memory jobs are an event-driven outbox. The user turn is a webhook. |
| "Add the smallest artifact that fixes the observed failure" (method map) | Minimalism | Supports §14's "simple deterministic workflow first". |

### 2.3 What we will adopt from Learn Harness Engineering

- **Runtime:** everything in the table above, implemented as code and tests rather than as prompt text.
- **Development harness (Level B):** `CLAUDE.md` as a short map, `feature_list.json` with one active
  feature and evidence required to pass, `claude-progress.md` for handoffs, an `init` script that
  installs, verifies and prints the start command, and a clean-state checklist (build, tests,
  progress, no stale artifacts, startup path). The implementation phases in `architecture.md` §12
  are tracked as features in that file.

### 2.4 What we will NOT adopt from Learn Harness Engineering, and why

| Not adopted | Why |
|---|---|
| The Electron/TypeScript app and its services | Wrong stack and a mock Q&A. We take patterns only, as §1 requires. |
| Planner → generator → evaluator **multi-agent** runtime | §14 and §26.6 rule out multi-agent complexity in v1. Maker/checker is achieved with a deterministic verifier. |
| Autonomous `/goal`-style loops inside the runtime | Our agent answers one turn per message. Long loops belong to the development harness, not the product. |
| Worktree isolation, skills, connectors (L13 primitives) | They concern the coding agent's workflow, not a support runtime. |
| OpenTelemetry in v1 (L11 recommends it) | Structured logs plus a trace table meet §18 with no new infrastructure. Log fields use OTel-compatible names (`trace_id`, `span`) so an exporter can be added later. |
| Quality document per module (L12) | Low value for a POC. Revisit once the codebase grows. |

---

## 3. Gaps neither repository answers

These need a decision from us. They are carried into `architecture.md` §11 (Unresolved decisions):

1. The LLM provider and key, for **both** our app and Hindsight's extraction. No API key is set in
   this environment.
2. Who executes remediation actions (restart consumer): the agent through gated tools, or a human
   operator who reports results.
3. The tenancy model and whether a bank per tenant is needed.
4. Local embeddings vs a provider embedding API for the RAG index.
5. Docker is installed on this machine but its daemon was **not running** at the time of study.
