# Quickstart

Get the showcase running in about five minutes.

## 1. Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| Python 3.12 | everything | |
| [`uv`](https://docs.astral.sh/uv/) | virtual environment | used instead of `pip` |
| [Ollama](https://ollama.com) + `granite4.2:latest` | live chat with the agents | optional; tests and `demo.py` work without it |
| Podman | container run | optional |

```bash
ollama pull granite4.2:latest      # optional, for live chat
```

## 2. Set up

```bash
scripts/setup.sh                   # creates .venv, installs requirements.txt, creates .env if missing
```

Re-run `scripts/setup.sh` whenever `requirements.txt` changes.

All settings are environment variables (see `.env.example`). The important ones:

| Variable | Default | Meaning |
|---|---|---|
| `AI_IDENTITY_BACKEND` | `mock` | `mock` = local simulated IBM Verify; `verify` = real tenant (reference code, untested) |
| `AI_LLM_MODE` | `ollama` | `fake` = deterministic offline model |
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | any OpenAI-compatible endpoint |
| `MODEL_NAME` | `granite4.2:latest` | model used by the agents |
| `AI_EXECUTION_MODE` | `function_calling` | Cuga execution mode (`codeact` does not work with granite) |
| `AI_STRICT_STATUS_CHECK` | `true` | re-check agent status on every validation (instant kill switch) |
| `AI_TOKEN_TTL_SECONDS` | `300` | token lifetime |
| `AI_UI_PORT` | `8601` | UI port |

## 3. Start and stop the UI

```bash
scripts/start.sh                   # detached; prints http://localhost:8601
scripts/stop.sh                    # graceful shutdown
```

Logs are in `.run/streamlit.log` and `output/showcase_<timestamp>.log`.

## 4. A five-minute tour

1. **Overview**: four deployed agents and their use cases.
2. **How Agent Identity works**: step through six flows (onboarding, autonomous, on-behalf-of, privileged, agent chain, kill switch).
3. **Chat with agents**: pick *Learning Coach* as *Carol* and try the sample prompts:
   - "List the available courses." runs with a delegated token.
   - "Enroll me in SEC301." asks for human approval, then uses a privileged token.
   - "Delete all employee records." is blocked by a Cuga policy before any tool runs.
4. **Agent registry and lifecycle**: suspend an agent and watch its tools stop working at once; reactivate it.
5. **Token inspector**: mint a token and read its claims in plain English.
6. **Audit trail**: allow and deny decisions, keyed by Agent ID. Export to `output/`.
7. **Harness Kit validation**: scaffold the kit's skills and run the conformance scenarios.

## 5. Run without the UI

```bash
.venv/bin/python demo.py           # offline walk-through, writes output/demo_<timestamp>.md
.venv/bin/python demo.py --live    # also chats through Cuga + Ollama
.venv/bin/python -m harness        # Harness Kit scaffold check + 10 conformance scenarios
```

## 6. Test

```bash
scripts/test.sh                    # pytest (80 tests) + the harness
```

Tests are offline: they use a fake chat model and the mock identity service.

## 7. Podman (optional)

```bash
scripts/container.sh               # builds the multi-stage Containerfile and runs it detached
scripts/remove_images.sh           # removes the images built locally
```

The container reaches Ollama on the host through `host.containers.internal`. This path has not been built or tested in the repository's validation run.

## 8. Clean up

```bash
scripts/stop.sh
scripts/cleanup.sh                 # removes .venv, node_modules and __pycache__
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sidebar says "LLM: fake model" | Ollama is not reachable; start it or check `OPENAI_BASE_URL`. |
| Chat error mentioning the model | `ollama list` must contain `MODEL_NAME`. |
| Agent hangs with `llama3.2` | Use `granite4.2:latest`; other small models may not follow the tool-calling protocol. |
| Port in use | Set `AI_UI_PORT` in `.env`. Do not use 5000 on macOS (AirDrop). |
| Odd UI errors right after editing code | Restart with `scripts/stop.sh && scripts/start.sh`; Streamlit caches objects built from old modules. |
