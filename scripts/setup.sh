#!/usr/bin/env bash
# Create/refresh the virtual environment with uv (re-run after editing requirements.txt).
source "$(dirname "$0")/_common.sh"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example"; }
echo "Environment ready."
