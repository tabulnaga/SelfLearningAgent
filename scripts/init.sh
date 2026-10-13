#!/usr/bin/env bash
# Session start (harness L06): install, verify baseline, print the start path.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> uv sync"
uv sync

echo "==> unit tests + lint (no services needed)"
uv run pytest -q
uv run ruff check app workers scripts tests

if docker info >/dev/null 2>&1; then
  echo "==> integration tests (starts db)"
  docker compose up -d db
  uv run pytest -m integration -q
else
  echo "!! Docker not reachable - skipping integration tests"
fi

cat <<'EOF'

Start everything:   docker compose up -d --build && docker compose exec api python -m scripts.bootstrap_bank
Demo (spec §27):    uv run python -m scripts.demo_poc
Live learning eval: uv run pytest -m live tests/evals -s
EOF
