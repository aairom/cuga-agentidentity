# Multi-stage, Docker-compatible. Build: podman build -t cuga-agentidentity -f Containerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY requirements.txt .
# Cuga pulls torch in transitively. Install the CPU-only wheel first so the ~5 GB of CUDA libraries are not
# downloaded; the showcase calls an external (Ollama) LLM and needs no GPU in the container.
RUN uv venv /opt/venv \
 && uv pip install -p /opt/venv/bin/python --no-cache torch --index-url https://download.pytorch.org/whl/cpu \
 && uv pip install -p /opt/venv/bin/python --no-cache -r requirements.txt

FROM python:3.12-slim
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 AI_UI_PORT=8601 AI_STATE_DIR=/tmp/cuga_state
RUN useradd -m -u 10001 app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY agent_identity ./agent_identity
COPY agents ./agents
COPY harness ./harness
COPY app.py demo.py ./
RUN mkdir -p /app/output && chown -R app /app
USER app
EXPOSE 8601
HEALTHCHECK CMD python -c "import urllib.request as u;u.urlopen('http://localhost:8601/_stcore/health')" || exit 1
CMD ["streamlit", "run", "app.py", "--server.port=8601", "--server.address=0.0.0.0", "--server.headless=true"]
