# Progress

## Current state (2026-10-04)

The whole POC is written: API, orchestrator, RAG, Hindsight adapter, Memory Writer, workers, tools
and simulation, verifier, tests, evals, demo and Docker files. **Only unit tests have run (30 passing,
ruff clean).** Nothing that needs Postgres or Hindsight has run yet, because the Docker Desktop engine
stopped responding mid-session.

## Verified

- `uv run pytest` → 30 passed (RAG units, verifier V1–V6, Memory Writer rules and idempotency keys,
  tool policy, authorizer, redaction, architecture import boundaries).
- `uv run ruff check app workers scripts tests` → clean.
- All entry points import (`app.api.main`, `workers`, `scripts.*`).
- Hindsight 0.10.2 behaviour the design relies on was checked in source: `document_id` upsert,
  unchanged-content LLM skip, `operation_id` dedupe, tag-scoped consolidation, reflect's forced
  tool calls (the reason reflect runs on Haiku).

## Not yet verified

1. `docker compose up -d --build`. The schema (`0001_init.sql`) and the Postgres init script have
   never run.
2. `uv run pytest -m integration` (18 vertical-slice tests and offline evals 1, 3, 4, 5).
3. `python -m scripts.bootstrap_bank` against live Hindsight. The knowledge-tree node `kind`
   values ("folder") are assumed from the generated client, not observed.
4. `uv run pytest -m live tests/evals -s` (evals 2, 6, 7) and `python -m scripts.demo_poc`. These
   need `ANTHROPIC_API_KEY`.
5. Hindsight with `claude-opus-5-5` for retain and consolidation (non-forced JSON path), still untested live.

## Next steps

1. Restart Docker Desktop. Then `docker compose up -d db` → `uv run pytest -m integration`, and fix what fails.
2. `docker compose up -d --build`, bootstrap the bank, then run the live evals and the demo.
3. Record the evidence in `feature_list.json`.
