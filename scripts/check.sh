#!/usr/bin/env bash
# The single source of truth for quality gates. Humans run this locally and
# .github/workflows/ci.yml invokes it verbatim, so the local and CI check
# lists cannot drift apart (they did: local runs omitted `ruff format
# --check` and CI failed on two consecutive pushes). Runs every gate in CI's
# order and exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."

ruff check .
ruff format --check .
mypy .
python -m pytest tests/ -v
