#!/usr/bin/env bash
# Remove node_modules, .venv, __pycache__ and runtime state (for the end of the project).
source "$(dirname "$0")/_common.sh"
"$ROOT/scripts/stop.sh" || true
find . -name node_modules -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name .venv -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf .pytest_cache .run .cuga_state
echo "Cleaned."
