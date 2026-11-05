#!/usr/bin/env bash
# Build and run the showcase with Podman (detached). The LLM engines run on the host: inside the
# container "localhost" is the container itself, so every engine URL points at host.containers.internal.
# Override with CONTAINER_HOST_ADDR, AI_LLM_ENGINE, AI_OLLAMA_MODEL, AI_CLOUD_* ... (exported in your shell).
source "$(dirname "$0")/_common.sh"
podman build -t cuga-agentidentity:latest -f Containerfile .
podman rm -f cuga-agentidentity >/dev/null 2>&1 || true
HOST_ADDR="${CONTAINER_HOST_ADDR:-host.containers.internal}"
podman run -d --name cuga-agentidentity -p "$PORT:8601" \
  -e AI_LLM_ENGINE="${AI_LLM_ENGINE:-}" \
  -e AI_OLLAMA_BASE_URL="${AI_OLLAMA_BASE_URL:-http://$HOST_ADDR:11434}" \
  -e AI_OLLAMA_MODEL="${AI_OLLAMA_MODEL:-granite4.2:latest}" \
  -e AI_LLAMACPP_PROBE_URLS="${AI_LLAMACPP_PROBE_URLS:-http://$HOST_ADDR:8080,http://$HOST_ADDR:9931}" \
  -e AI_CLOUD_BASE_URL="${AI_CLOUD_BASE_URL:-}" -e AI_CLOUD_MODEL="${AI_CLOUD_MODEL:-}" \
  -e AI_CLOUD_API_KEY="${AI_CLOUD_API_KEY:-}" \
  -e OPENAI_BASE_URL="${CONTAINER_OPENAI_BASE_URL:-http://$HOST_ADDR:11434/v1}" \
  -e OPENAI_API_KEY="${OPENAI_API_KEY:-ollama}" -e MODEL_NAME="${MODEL_NAME:-granite4.2:latest}" \
  cuga-agentidentity:latest >/dev/null
echo "Open: http://localhost:$PORT"
