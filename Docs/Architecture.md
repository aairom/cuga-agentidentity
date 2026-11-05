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
    CA -.-> LLM[(Ollama / OpenAI-compatible)]
```

## Agent lifecycle (state machine)

```mermaid
stateDiagram-v2
    [*] --> DEFAULT: create_agent (developer)
    DEFAULT --> ACTIVE: approve_agent (approver, never the creator)
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

## Honest limits

- `HttpVerifyBackend` is a reference implementation and has not been run against a live IBM Verify tenant.
- The Harness Kit scaffolds skills; the conformance checks are the showcase's own (`harness/scenarios.py`).
- `execution_mode="codeact"` does not work with the default local model; `function_calling` is the default.
- The Containerfile has not been built or tested in this repository's validation run.

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
