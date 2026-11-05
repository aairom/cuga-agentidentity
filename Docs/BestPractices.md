# Best practices

Patterns used in this repository that you can copy into your own agents. Each one names the file it comes from.

## 1. Guard every tool with an identity check

The tool asks the gateway for a token before it touches data, and turns a denial into text the model can relay. (`agent_identity/tools.py`)

```python
@tool
def list_courses() -> str:
    """List the available training courses with ids, titles and costs (USD)."""
    try:
        gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR,
                     scopes=["courses:read"], resource="hr.list_courses")
    except IdentityError as exc:
        return _denied(exc)          # "ACCESS DENIED (InsufficientScope): ..."
    return json.dumps([{"id": k, **v} for k, v in COURSES.items()])
```

Why: authorisation lives with the data, not in the prompt. A jailbroken model still cannot read what the token does not allow.

## 2. Never give the model a credential

Tokens are minted inside the tool, per call, and never returned to the model. The signed-in user travels in a `ContextVar` (`agent_identity/gateway.py`), so tools do not take a `user_id` argument the model could forge.

```python
ctx_token = set_call_context(CallContext(user_id=user_id, user_token=user_token))
try:
    result = await rt.cuga.invoke(message, thread_id=thread_id)
finally:
    reset_call_context(ctx_token)    # always reset, even on errors
```

## 3. Effective scope is the intersection

An agent acting for a person gets only what both are allowed. A user without `ops:restart` cannot gain it through an agent that has it. The scope ceiling is fixed at approval time in the agent spec:

```python
SPEC = AgentSpec(
    name="learning-coach",
    access_mode="obo",
    allowed_scopes=frozenset({"courses:read", "courses:enroll"}),   # ceiling
    ...
)
```

## 4. Pick the access mode per use case

| Use case | Mode | Why |
|---|---|---|
| Acting for a person (HR, ops) | `obo` (token exchange) | `sub` is the user, `act` is the agent; audit shows both |
| Back-office analytics | `autonomous` (client credentials) | no person involved; `agent_id` claim identifies the agent |
| Orchestration | `obo` with nested `act` | the chain supervisor, then worker, is preserved |

## 5. Use short-lived privileged tokens for risky actions

Combine a human approval (Cuga `tool_approval` policy) with a token that names the exact action:

```python
details = [{"type": "course_enrollment", "course_id": course_id.upper(), "max_cost": course["cost"]}]
ctx = gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR,
                   scopes=["courses:enroll"], resource="hr.enroll_privileged",
                   authorization_details=details)
```

The validator then requires `authorization_details` to be present (`require_privileged=True`). A normal token cannot call this tool.

## 6. Declare policies in the agent spec

Cuga policies run before the LLM or tools, so they are cheap and deterministic. (`agents/learning_coach.py`)

```python
guards=(IntentGuardSpec(name="No bulk record deletion",
                        keywords=("delete all", "wipe", "erase all"),
                        response="Blocked by policy: this agent cannot delete records."),),
approvals=(ApprovalSpec(name="Approve expensive enrolments",
                        tools=("enroll_expensive_course",),
                        message="This enrolment exceeds 500 USD. A human must approve it."),),
```

Treat policies as a first line, not the only line: keyword guards are easy to phrase around. The identity check in the tool is what actually enforces.

## 7. Isolate policy stores per agent

Cuga's default policy store is shared per process, so policies from one agent leak into another. Give each agent its own (`agent_identity/runtime.py`):

```python
policy_system = PolicyConfigurable()
await policy_system.initialize(collection_name=f"policies_{spec.name.replace('-', '_')}")
agent = CugaAgent(policy_system=policy_system, ...)
await agent.policies.add_tool_approval(name=a.name, required_tools=list(a.tools),
                                       approval_message=a.message)
```

## 8. Check agent status at the resource server

Suspending an agent blocks new tokens, but issued tokens stay valid until expiry. Close the gap:

```python
lookup = (lambda aid: backend.get_agent(aid).status) if strict else None
TokenValidator(key, issuer, audience, audit, status_lookup=lookup)
```

Keep `AI_STRICT_STATUS_CHECK=true` and keep `AI_TOKEN_TTL_SECONDS` short.

## 9. Separate creation from approval

```python
service.onboard(DEVELOPER, APPROVER, spec.name, spec.description, spec.allowed_scopes)
```

`create_agent` and `approve_agent` demand different entitlements, and approving your own agent raises `SeparationOfDutiesError`. Test it (see `tests/test_lifecycle.py`).

## 10. Correlate everything by Agent ID

Management events (`agent.created`, `agent.approved`, `client.registered`) and runtime events (`token.issued`, `access.granted`, `access.denied`) share `agent_id`:

```python
audit.events(agent_id=record.agent_id)       # one agent, whole life
audit.events(outcome="denied")               # all refusals
```

## 11. Handle errors at the boundary

- Identity errors derive from `IdentityError` (`AgentNotActive`, `InsufficientScope`, `DelegationError`, `SeparationOfDutiesError`, ...). Catch the base class in tools; let unexpected exceptions reach the runtime.
- `Showcase.chat` never raises to the UI: failures come back as `ChatTurn(error=...)` and are logged with the traceback.
- After an explicit approve or deny, the turn is final (`needs_approval` is forced off), so the UI never shows a stale approval prompt.

## 12. Configuration and logging

- All settings come from environment variables through typed `pydantic-settings` (`AI_` prefix); secrets use `SecretStr`. Nothing is hard-coded; `.env` is git-ignored and `.env.example` is committed.
- Logging goes through `loguru` with a level from `AI_LOG_LEVEL`, written to the console and to a timestamped file `output/showcase_<timestamp>.log`. Audit events are logged as they are recorded.

## 13. Test without an LLM

Use a fake chat model and the mock identity service so tests are fast and deterministic (`tests/conftest.py`), and keep a few live checks (`demo.py --live`) for the real model. The harness scenarios (`harness/scenarios.py`) are written as plain functions that return a one-line result or raise, so they can run from the CLI, the UI and pytest.

## 14. Know your model's limits

- `function_calling` works with `granite4.2:latest`. `codeact` does not. `llama3.2` hung in our runs.
- Keep tool docstrings short and imperative; they are the model's only manual.
- Tell the model what to do on denial ("report it verbatim, do not retry") in the agent instructions.

## Production checklist

- [ ] Replace `MockVerifyBackend` with a tested real backend and real secrets from a vault.
- [ ] Validate tokens against the issuer's JWKS, with key rotation.
- [ ] Keep token lifetimes at minutes; privileged tokens at seconds.
- [ ] Persist the audit log somewhere append-only.
- [ ] Run the conformance scenarios in CI.
- [ ] Review the scope ceiling of every agent at each approval.
