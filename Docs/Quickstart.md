# Quickstart

Get the showcase running in about five minutes.

## 1. Prerequisites

| Tool | Needed for | Notes |
|---|---|---|
| Python 3.12 | everything | |
| [`uv`](https://docs.astral.sh/uv/) | virtual environment | used instead of `pip` |
| An LLM engine: [Ollama](https://ollama.com) + `granite4.2:latest`, a `llama-server` (llama.cpp), or a cloud OpenAI-compatible API | live chat with the agents | optional; tests and `demo.py` work without one |
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
| `AI_LLM_ENGINE` | empty (auto) | `ollama`, `llamacpp`, `cloud`, `fake`; empty = fallback order |
| `AI_LLM_FALLBACK_ENGINES` | `ollama,llamacpp,cloud` | order tried when no engine is chosen |
| `AI_LLM_MODE` | `ollama` | `fake` = deterministic offline model (tests, harness, demo) |
| `AI_OLLAMA_BASE_URL` / `AI_OLLAMA_MODEL` | `http://localhost:11434` / `granite4.2:latest` | Ollama |
| `AI_LLAMACPP_BASE_URL` / `AI_LLAMACPP_MODEL` | empty | llama-server; empty URL = probe `AI_LLAMACPP_PROBE_URLS` (`:8080`, `:9931`) |
| `AI_CLOUD_BASE_URL` / `AI_CLOUD_MODEL` / `AI_CLOUD_API_KEY` | empty | cloud engine (all three needed for auto use; key is a secret) |
| `OPENAI_BASE_URL`, `MODEL_NAME` | Ollama `/v1`, `granite4.2:latest` | legacy; still used as Ollama defaults |
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

## 4. Choose the LLM engine

### Option A: in `.env` (persistent default)

```bash
# Ollama (default)
AI_LLM_ENGINE=ollama
AI_OLLAMA_MODEL=granite4.2:latest

# llama.cpp: start the server with chat-template support, then point the app at it
#   llama-server -m /path/model.gguf --jinja --port 8080
AI_LLM_ENGINE=llamacpp
AI_LLAMACPP_BASE_URL=http://localhost:8080

# Cloud (https is required for remote hosts)
AI_LLM_ENGINE=cloud
AI_CLOUD_BASE_URL=https://api.openai.com/v1
AI_CLOUD_MODEL=gpt-4o-mini
AI_CLOUD_API_KEY=<your key>
```

Leave `AI_LLM_ENGINE` empty to let the app choose. Without any UI choice it uses, in order: `AI_LLM_ENGINE`, then each engine in `AI_LLM_FALLBACK_ENGINES` that is configured or detected, then hard-coded Ollama (`localhost:11434`, `granite4.2:latest`), then the offline fake model.

### Option B: in the UI (runtime, no restart)

1. Open **LLM engine** in the sidebar. The green box shows the active engine, model, endpoint and where the choice came from (`ui`, `env`, `auto`, `fallback`).
2. **Detected engines** lists what is running. Press **Re-scan** after starting a server.
3. Choose an **Engine**, then fill the form. Required fields have a `*`; invalid input disables the buttons and explains why (for example `Cloud: 'Base URL' is required`, or plain `http://` to a remote host).
4. **Test connection** checks reachability, the key and that the model exists. On failure you get the cause and a *How to fix* line.
5. **Apply and activate** switches all agents. New requests wait, running ones finish, then the model is swapped. If the new engine fails, the old one stays active and the page says why.
6. Keep **Remember this choice** ticked to save engine, URL and model to `.cuga_state/llm_engine.json` (mode 600, git-ignored). **The API key is never saved.**
7. **Reset to .env defaults** deletes the saved choice and re-resolves from `.env`.

Conversations, pending approvals, tokens and the audit trail survive a switch. Each switch writes an audit event (`llm.engine_switched` or `llm.engine_switch_failed`) visible on the **Audit trail** page.

Tip: Cuga's `function_calling` mode needs a model that follows the tool protocol. `granite4.2:latest` is verified; `llama3.2:latest` hung in earlier tests.

## 5. A five-minute tour

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

## 6. Run without the UI

```bash
.venv/bin/python demo.py           # offline walk-through, writes output/demo_<timestamp>.md
.venv/bin/python demo.py --live    # also chats through Cuga + Ollama
.venv/bin/python -m harness        # Harness Kit scaffold check + 10 conformance scenarios
```

## 7. Test

```bash
scripts/test.sh                    # pytest (118 tests) + the harness
```

Tests are offline: they use a fake chat model and the mock identity service.

## 8. Podman (optional)

```bash
scripts/container.sh               # builds the multi-stage Containerfile and runs it detached
scripts/remove_images.sh           # removes the images built locally
```

The image installs the CPU-only `torch` wheel (Cuga depends on torch), giving about 2.3 GB instead of 7 GB. `scripts/container.sh` points every engine URL at `host.containers.internal` (override with `CONTAINER_HOST_ADDR`) and forwards `AI_LLM_ENGINE`, `AI_OLLAMA_*`, `AI_LLAMACPP_PROBE_URLS` and `AI_CLOUD_*` from your shell. The image was built before the engine layer was added; rebuild it with `scripts/container.sh` to include it. Validated: image builds, `/_stcore/health` returns `ok`, and `demo.py` runs inside it (with the fake model; the container-to-host Ollama path was not exercised).

## 9. Clean up

```bash
scripts/stop.sh
scripts/cleanup.sh                 # removes .venv, node_modules and __pycache__
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| Sidebar says "LLM: fake model" | No engine answered. Open **LLM engine**, read the *Detected engines* table and the warning, start a server, then **Re-scan** or **Reset to .env defaults**. |
| `Cannot connect to http://localhost:8080/v1/models` | `llama-server` is not running or uses another port (this setup may use 8080 or 9931). Start it with `--jinja`, or fix `AI_LLAMACPP_BASE_URL`. |
| `Ollama is running but model '...' is not installed` | `ollama pull <model>` or choose a model from the list shown. |
| `The provider rejected the credentials (HTTP 401/403)` | Check `AI_CLOUD_API_KEY` (or retype it in the form), its permissions and that it matches the base URL. The UI never saves the key. |
| `refusing to send an API key over plain http` | Use an `https://` URL; plain http is accepted only for local hosts (`localhost`, `127.0.0.1`, `::1`, `*.local`, `host.containers.internal`, `host.docker.internal`). |
| Switch fails, previous engine stays active | Expected: read the message on the page; details are in `.run/streamlit.log` (`agent_identity.llm`). |
| Chat error mentioning the model | `ollama list` must contain the configured model. |
| Agent hangs with `llama3.2` | Use `granite4.2:latest`; other small models may not follow the tool-calling protocol. |
| Port in use | Set `AI_UI_PORT` in `.env`. Do not use 5000 on macOS (AirDrop). |
| Odd UI errors right after editing code | Restart with `scripts/stop.sh && scripts/start.sh`; Streamlit caches objects built from old modules. |
