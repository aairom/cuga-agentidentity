#!/usr/bin/env bash
# Shared helpers: resolves the project root and loads .env (never committed).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
PORT="${AI_UI_PORT:-8601}"
RUN_DIR="$ROOT/.run"
PID_FILE="$RUN_DIR/streamlit.pid"
LOG_FILE="$RUN_DIR/streamlit.log"
