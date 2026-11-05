#!/usr/bin/env bash
# Build and run the showcase with Podman (detached). Ollama on the host is used for the LLM.
source "$(dirname "$0")/_common.sh"
podman build -t cuga-agentidentity:latest -f Containerfile .
podman rm -f cuga-agentidentity >/dev/null 2>&1 || true
podman run -d --name cuga-agentidentity -p "$PORT:8601" \
  -e OPENAI_BASE_URL="${CONTAINER_OPENAI_BASE_URL:-http://host.containers.internal:11434/v1}" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}" -e MODEL_NAME="${MODEL_NAME:-granite4.2:latest}" \
  cuga-agentidentity:latest >/dev/null
echo "Open: http://localhost:$PORT"
