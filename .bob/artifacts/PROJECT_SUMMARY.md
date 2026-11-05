# Project summary: Cuga + Agent Identity showcase

Artifact index for the repository. Screenshots in this folder were captured from the running UI at `http://localhost:8601`.

## What was built

| Area | Result |
|---|---|
| Identity layer | `agent_identity/`: typed config, logging, audit, mock IBM Verify backend (+ reference HTTP backend), token issue/exchange/validation, lifecycle with separation of duties, identity gateway |
| Agents | Learning Coach (on-behalf-of + approval + privileged token), Revenue Analyst (autonomous, read-only), Ops SRE Assistant (privileged restart), Workplace Supervisor (nested actor chain) |
| Cuga integration | Per-agent policy stores, intent guards, tool approval with human-in-the-loop resume, supervisor delegation, function-calling mode with local Ollama (`granite4.2:latest`) |
| Harness Kit | Scaffold and validate 11 skills (`cuga-harness-kit 0.2.0`) plus 10 conformance scenarios |
| UI | Streamlit, 8 pages, port 8601 |
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

## Validation evidence

| Check | Result |
|---|---|
| `pytest` | 80 passed |
| `python -m harness` | 11 skills scaffolded, 10/10 scenarios PASS |
| `python demo.py` | all sections run offline, report written to `output/` |
| UI smoke test (Chrome DevTools) | all 8 pages exercised; live granite4.2 chat: policy block, human approval, privileged enrolment |
| Podman image | not built or tested |
| `HttpVerifyBackend` | not tested against a live IBM Verify tenant |

## Bug found and fixed during the smoke test

After an approved turn the UI still showed the approval card. Cause: `_has_approval_prompt` matched the resumed result. Fix: `Showcase.chat` sets `needs_approval = approve is None and _has_approval_prompt(result)`. Covered by `tests/test_bootstrap_demo.py::test_no_stale_approval_prompt_after_decision`, and re-verified live.

## Known limits

- IBM Agent Identity has no public SDK; the identity service is simulated locally.
- The Harness Kit scaffolds coding-assistant skills; it is not a test runner.
- `codeact` execution mode does not work with granite; `llama3.2` hung.
- Keyword intent guards are a first line only; tool-level identity checks enforce.
