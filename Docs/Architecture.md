# Architecture

This page describes the components of the showcase and the six identity flows. The sequence diagrams below are generated from the same `FLOWS` data (`agent_identity/ui_helpers.py`) that powers the "How Agent Identity works" page in the UI, so the two stay consistent.

## Component view

```mermaid
flowchart TB
    subgraph Presentation
        UI[Streamlit UI - app.py]
        CLI[CLI demo - demo.py]
        HRN[Harness - python -m harness]
    end
    subgraph Application
        SC[Showcase facade - runtime.py]
        BOOT[bootstrap.deploy_all]
        AG[Agent specs - agents/]
        TL[Guarded tools - tools.py]
    end
    subgraph Identity layer
        GW[IdentityGateway - gateway.py]
        SVC[AgentIdentityService - lifecycle.py]
        TV[TokenValidator - tokens.py]
        AUD[AuditLog - audit.py]
        BE{{IdentityBackend}}
    end
    subgraph Cuga
        CA[CugaAgent / CugaSupervisor]
        POL[Policies: intent guard, tool approval]
    end
    subgraph LLM engine layer
        EM[EngineManager - llm_engines.py]
        EO[OllamaEngine]
        EL[LlamaCppEngine]
        EC[CloudEngine]
        EF[FakeEngine]
    end
    UI --> SC
    CLI --> SC
    HRN --> SC
    BOOT --> SC
    AG --> SC
    SC --> CA
    CA --> POL
    CA --> TL
    TL --> GW
    GW --> SVC
    GW --> TV
    SVC --> BE
    SVC --> AUD
    TV --> AUD
    BE --> M[MockVerifyBackend]
    BE --> H[HttpVerifyBackend]
    SC --> EM
    EM --> EO
    EM --> EL
    EM --> EC
    EM --> EF
    CA -.-> MODEL[(shared chat model)]
    EM -.-> MODEL
```

## Agent lifecycle (state machine)

```mermaid
stateDiagram-v2
    state "DEFAULT" as Pending
    [*] --> Pending: create_agent (developer)
    Pending --> ACTIVE: approve_agent (approver, never the creator)
    ACTIVE --> ACTIVE: register_client, issue tokens
    ACTIVE --> SUSPENDED: suspend_agent (kill switch)
    SUSPENDED --> ACTIVE: reactivate_agent
```

## Request path of one tool call

```mermaid
flowchart TD
    A[User message] --> B{Cuga intent guard}
    B -->|match| X[Blocked before any LLM or tool call]
    B -->|pass| C[LLM chooses a tool]
    C --> D{Tool approval policy}
    D -->|needed| E[Human approves or denies]
    E -->|deny| Y[Stop]
    E -->|approve| F
    D -->|not needed| F[Tool runs]
    F --> G[Gateway reads call context - user and tokens via ContextVar]
    G --> H[Mint token: client_credentials or token exchange]
    H --> I{Validate: signature, iss, aud, exp, scope, agent status}
    I -->|fail| Z[ACCESS DENIED + audit event]
    I -->|ok| J[Business logic runs]
    J --> K[Result + audit event]
```

## LLM engine layer

`agent_identity/llm_engines.py` hides the model backend behind one interface. Agents never know which engine they use; they receive one shared LangChain chat model from `Showcase`.

```mermaid
classDiagram
    class LLMEngine {
        <<abstract>>
        +engine_id
        +fields: ConfigField[]
        +detect(settings) Detection
        +auto_config(settings) EngineConfig
        +test_connection(cfg) TestResult
        +build_model(cfg) ChatModel
        +apply_env(cfg)
    }
    LLMEngine <|-- OllamaEngine
    LLMEngine <|-- LlamaCppEngine
    LLMEngine <|-- CloudEngine
    LLMEngine <|-- FakeEngine
    class EngineManager {
        +detect_all() Detection[]
        +resolve() Resolution
        +test(cfg) TestResult
        +save_override(cfg)
        +load_override()
        +clear_override()
    }
    EngineManager --> LLMEngine : registry ENGINES
    class Showcase {
        +engine: EngineConfig
        +switch_engine(cfg)
        -_shared_model()
    }
    Showcase --> EngineManager
```

| Piece | Role |
|---|---|
| `EngineConfig` | Immutable description of the active engine: id, base URL, model, `SecretStr` key, source (`ui`, `env`, `auto`, `fallback`, `fake`). `public_dict()` never includes the key. |
| `ConfigField` | Declares the form fields of an engine, so the UI renders any engine without engine-specific code. |
| `Detection` | Result of probing: `running`, `installed`, `not detected`, endpoint, models. |
| `EngineManager` | Detection, resolution, connection tests and the persisted UI choice (`.cuga_state/llm_engine.json`, mode 600, no key). |
| `LLMEngineError` | Message plus remediation. Subclasses: `EngineMisconfigured`, `EngineUnavailable`, `EngineConnectionError`, `EngineSwitchError`. |

### Switching with a consistent state

`Showcase.switch_engine` (see the sequence diagram in the [README](../README.md#llm-engines)) is built around three objects: a `_ready` gate that makes new turns wait while a switch runs, an `_idle` event that is set when no turn is in flight, and a per-agent `MemorySaver` that outlives the model.

```mermaid
stateDiagram-v2
    [*] --> Serving
    Serving --> Testing: switch_engine(new)
    Testing --> Serving: test fails (EngineSwitchError, nothing changed)
    Testing --> Draining: test ok, gate closed
    Draining --> Swapping: in-flight turns finished
    Draining --> Restoring: drain timeout
    Swapping --> Serving: model replaced, graphs rebuilt, audit llm.engine_switched
    Swapping --> Restoring: build fails
    Restoring --> Serving: old engine back, audit llm.engine_switch_failed
```

- A turn that started before the switch finishes on the old model. A turn that arrives during the switch waits at the gate and then runs on the new model.
- Cuga graphs are rebuilt lazily on the next turn by resetting `agent._graph` and `agent._compiled_graph`; this relies on Cuga 0.4.5 internals and should be re-checked when Cuga is upgraded.
- The checkpointer is ours, so chat history and pending approvals are not lost.
- Tokens, the agent registry and the audit log do not depend on the model at all.

### Error handling and logging

Every failure on the engine path becomes an `LLMEngineError` with a remediation. The UI prints both. The logger `agent_identity.llm` records the same text (never a key) and the audit log records switches. The fallback decisions made at start-up (`attempts` and `warnings` of `Resolution`) are logged and shown on the page when the app had to fall back.

## Design decisions

| Decision | Reason |
|---|---|
| Identity backend behind an interface | The showcase runs offline with a mock; a real tenant plugs in through `HttpVerifyBackend`. |
| Tokens are minted per tool call | Short lifetimes, narrow scopes, and the LLM never holds a credential. |
| User context travels in a `ContextVar` | Tools stay free of user-id parameters the LLM could forge. |
| Effective scope = user scopes intersected with agent ceiling | An agent can never exceed the user it acts for, nor its own approval. |
| Strict status check on every validation | Suspending an agent takes effect immediately, even for unexpired tokens. |
| One Cuga policy store per agent | Cuga's default store is shared per process; isolating it stops policies leaking between agents. |
| Audit events keyed by Agent ID | Management and runtime events can be read as one story. |
| One interface per LLM engine, data-driven form fields | A new engine is one class plus a registry entry; the UI needs no change. |
| Test the new engine before touching the old one | A bad URL or key can never leave the app without a working model. |
| UI choice never stores the API key | The key stays in `.env` or memory; the saved file can be read or shared without leaking it. |
| Plain http to a remote host is refused for the cloud engine | A key must not cross the network unencrypted. |

## Honest limits

- `HttpVerifyBackend` is a reference implementation and has not been run against a live IBM Verify tenant.
- The Harness Kit scaffolds skills; the conformance checks are the showcase's own (`harness/scenarios.py`).
- `execution_mode="codeact"` does not work with the default local model; `function_calling` is the default.
- The container image builds and starts (health check OK, `demo.py` runs inside it). Chatting with Ollama from inside the container was not exercised, and the image was built before the engine layer existed.
- LLM engines: Ollama detection, connection test, switching and persistence were exercised against a real local Ollama. **llama.cpp and cloud were tested with mocked HTTP only**: no `llama-server` was running and no cloud key was available.
- The runtime graph reset relies on private Cuga attributes (0.4.5).
- Small local models may not follow Cuga's tool protocol (`llama3.2:latest` hung in an earlier test); switching to them works but chat quality depends on the model.

## Identity flows

### 1. Onboarding (create, approve, register)

An agent gets a first-class identity before it can obtain a single token. Creation and activation are done by two different people (separation of duties).

```mermaid
sequenceDiagram
    participant Developer as Developer
    participant Agent_Registry as Agent Registry
    participant Approver as Approver
    participant OAuth_server as OAuth server
    Developer->>Agent_Registry: create agent (writeAgents)
    Agent_Registry-->>Developer: Agent ID, status DEFAULT
    Approver->>Agent_Registry: set status ACTIVE (manageAgentStatus)
    Developer->>OAuth_server: dynamic client registration
    OAuth_server-->>Developer: client_id + secret (shown once)
```

Key takeaways:

- The creator can never approve their own agent.
- Only ACTIVE agents can register a client or get tokens.
- Every step is audited under the same Agent ID.

### 2. Autonomous access (client credentials)

The agent acts as itself. The token carries an agent_id claim so the resource server knows exactly which agent called.

```mermaid
sequenceDiagram
    participant Agent as Agent
    participant OAuth_server as OAuth server
    participant Tool___API as Tool / API
    Agent->>OAuth_server: client_credentials (client_id, secret)
    OAuth_server-->>Agent: access token {agent_id, scope}
    Agent->>Tool___API: call with Bearer token
    Tool___API->>Tool___API: verify signature, aud, exp, scope, agent status
    Tool___API-->>Agent: result or 403
```

Key takeaways:

- Scopes are limited by the ceiling configured in the registry.
- Use for batch / back-office work with no human in the loop.

### 3. On-behalf-of (RFC 8693 token exchange)

The agent acts for a user. The exchanged token has sub=user and act.sub=agent, so the agent can never do more than the user is allowed to.

```mermaid
sequenceDiagram
    participant User as User
    participant Agent as Agent
    participant OAuth_server as OAuth server
    participant Tool___API as Tool / API
    User->>Agent: request + user access token
    Agent->>OAuth_server: token-exchange: subject=user, actor=agent
    OAuth_server-->>Agent: token {sub=user, act.sub=agent}
    Agent->>Tool___API: call with delegated token
    Tool___API->>Tool___API: scopes = user entitlements AND agent ceiling
    Tool___API-->>Agent: result or 403
```

Key takeaways:

- Effective permission = user entitlements intersected with the agent ceiling.
- Carol cannot restart a service through the agent because Carol herself cannot.

### 4. Privileged action (human-in-the-loop)

Sensitive tools pause for approval (Cuga tool-approval policy) and then use a short-lived token with authorization_details scoped to one action.

```mermaid
sequenceDiagram
    participant User as User
    participant Cuga_agent as Cuga agent
    participant OAuth_server as OAuth server
    participant Tool___API as Tool / API
    User->>Cuga_agent: enroll me in SEC301 (1800 USD)
    Cuga_agent-->>User: approval required
    User->>Cuga_agent: approve
    Cuga_agent->>OAuth_server: exchange + authorization_details
    OAuth_server-->>Cuga_agent: privileged token (very short TTL)
    Cuga_agent->>Tool___API: enroll_expensive_course
    Tool___API-->>Cuga_agent: enrolled
```

Key takeaways:

- Approval is enforced by the runtime policy, not by the prompt.
- The privileged token is bound to the specific action and expires quickly.

### 5. Multi-agent delegation (transaction context)

A supervisor delegates to a worker. The worker token nests the chain act(worker) -> act(supervisor), giving a complete audit trail.

```mermaid
sequenceDiagram
    participant User as User
    participant Supervisor as Supervisor
    participant Worker_agent as Worker agent
    participant Tool___API as Tool / API
    User->>Supervisor: request
    Supervisor->>Supervisor: exchange: sub=user, act=supervisor
    Supervisor->>Worker_agent: delegate task + supervisor token
    Worker_agent->>Worker_agent: exchange: subject=supervisor token, actor=worker
    Worker_agent->>Tool___API: token {sub=user, act=worker -> supervisor}
    Tool___API-->>Worker_agent: result
```

Key takeaways:

- The resource server sees who asked, and through which agents.
- Audit event 'access.granted' records the whole chain.

### 6. Kill switch (suspend)

Suspending an agent stops it from obtaining NEW tokens. Already-issued tokens stay valid until they expire, so resource servers should also check the agent status.

```mermaid
sequenceDiagram
    participant Approver as Approver
    participant Agent_Registry as Agent Registry
    participant Agent as Agent
    participant Tool___API as Tool / API
    Approver->>Agent_Registry: suspend agent
    Agent->>Agent_Registry: request new token
    Agent_Registry-->>Agent: refused (not ACTIVE)
    Agent->>Tool___API: reuse old, still-valid token
    Tool___API->>Agent_Registry: defence in depth: check status
    Tool___API-->>Agent: 403 AgentNotActive
```

Key takeaways:

- Keep token TTLs short.
- This showcase enables the strict status check by default (AI_STRICT_STATUS_CHECK).
