set dotenv-load := true

default:
    @just --list

# Install deps, git hooks, and a local .env
setup:
    uv sync
    just fix-pth
    uv run pre-commit install
    test -f .env || cp .env.example .env

# macOS (iCloud-synced folders) can mark the editable-install .pth as hidden, which Python 3.12+ skips
fix-pth:
    -chflags nohidden .venv/lib/python3.*/site-packages/*.pth 2>/dev/null

test:
    uv run pytest

lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy

fmt:
    uv run ruff check --fix .
    uv run ruff format .

ingest: fix-pth
    uv run scout ingest

summarize: fix-pth
    uv run scout summarize

rank: fix-pth
    uv run scout rank

brief: fix-pth
    uv run scout brief

label: fix-pth
    uv run scout label

run-daily: fix-pth
    uv run scout run-daily

serve: fix-pth
    uv run scout serve

eval: fix-pth
    uv run scout eval
