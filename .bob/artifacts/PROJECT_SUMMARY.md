# Project summary: Cuga + Agent Identity showcase

Artifact index for the repository. Screenshots in this folder were captured from the running UI at `http://localhost:8601`.

## What was built

| Area | Result |
|---|---|
| Identity layer | `agent_identity/`: typed config, logging, audit, mock IBM Verify backend (+ reference HTTP backend), token issue/exchange/validation, lifecycle with separation of duties, identity gateway |
| Agents | Learning Coach (on-behalf-of + approval + privileged token), Revenue Analyst (autonomous, read-only), Ops SRE Assistant (privileged restart), Workplace Supervisor (nested actor chain) |
| Cuga integration | Per-agent policy stores, intent guards, tool approval with human-in-the-loop resume, supervisor delegation, function-calling mode with local Ollama (`granite4.2:latest`) |
| Harness Kit | Scaffold and validate 11 skills (`cuga-harness-kit 0.2.0`) plus 10 conformance scenarios |
| LLM engines | `agent_identity/llm_engines.py`: pluggable Ollama / llama.cpp / cloud OpenAI-compatible backends, detection, fallback priority, runtime switch with rollback, remediation errors |
| UI | Streamlit, 9 pages (incl. LLM engine), port 8601 |
| Docs | `README.md`, `Docs/Quickstart.md`, `Docs/Architecture.md`, `Docs/AgentIdentity-Concepts.md`, `Docs/BestPractices.md` |
| Ops | `scripts/` (setup, start detached, stop, test, cleanup, container, remove_images), multi-stage `Containerfile` |

## Screenshots

| File | Page |
|---|---|
| `ui_flows.png` | How Agent Identity works (SVG sequence diagram) |
| `ui_registry.png` | Agent registry and lifecycle |
| `ui_killswitch.png` | Suspended agent |
| `ui_token_inspector.png` | Token inspector |
| `ui_audit.png` | Audit trail |
| `ui_harness.png` | Harness Kit validation (11 skills, 10/10 scenarios) |
| `ui_llm_engine_ollama.png` | LLM engine page, Ollama detected and active |
| `ui_llm_engine_error_remediation.png` | Failed connection test with "How to fix" |
| `ui_llm_engine_switched.png` | Runtime switch applied (source `ui`) |
| `ui_llm_engine_cloud_form.png` | Cloud form validation, buttons disabled |

## Validation evidence

| Check | Result |
|---|---|
| `pytest` | 118 passed (38 new for engines and switching) |
| `python -m harness` | 11 skills scaffolded, 10/10 scenarios PASS |
| `python demo.py` | all sections run offline, report written to `output/` |
| UI smoke test (Chrome DevTools) | all 9 pages exercised; live granite4.2 chat: policy block, human approval, privileged enrolment |
| Podman image | built (2.34 GB, CPU-only torch), started, health `ok`, `demo.py` ran inside; container-to-host Ollama not exercised |
| `HttpVerifyBackend` | not tested against a live IBM Verify tenant |
| Engine switching (live UI) | Ollama detect/test/switch, persistence across restart, reset to `.env`, missing-model and closed-port errors, form validation, no secrets in logs or override file |
| llama.cpp success path, cloud connect/auth | mocked HTTP only (no server or API key available) |

## Bug found and fixed during the smoke test

After an approved turn the UI still showed the approval card. Cause: `_has_approval_prompt` matched the resumed result. Fix: `Showcase.chat` sets `needs_approval = approve is None and _has_approval_prompt(result)`. Covered by `tests/test_bootstrap_demo.py::test_no_stale_approval_prompt_after_decision`, and re-verified live.

## Known limits

- IBM Agent Identity has no public SDK; the identity service is simulated locally.
- The Harness Kit scaffolds coding-assistant skills; it is not a test runner.
- `codeact` execution mode does not work with granite; `llama3.2` hung.
- Keyword intent guards are a first line only; tool-level identity checks enforce.
- Podman image was built before the engine layer; rebuild with `scripts/container.sh` to include it.
- Runtime engine swap resets private Cuga 0.4.5 attributes (`_model`, `_graph`, `_compiled_graph`); re-check on Cuga upgrades.
