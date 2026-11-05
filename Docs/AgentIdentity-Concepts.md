# Agent Identity concepts

This page explains the identity model the showcase implements. It is based on the public product description of [IBM Agent Identity](https://www.ibm.com/products/agent-identity) and on standard OAuth 2.0 mechanisms. The showcase simulates the identity service locally (see the README for what is real and what is simulated).

## Why agents need their own identity

An agent that borrows a shared service account or the user's password is invisible: nobody can tell which agent did what, scopes cannot be narrowed, and a misbehaving agent cannot be switched off without breaking everything else. Giving each agent its own identity solves four problems:

| Problem | What an agent identity provides |
|---|---|
| Accountability | Every call carries the Agent ID; the audit log tells one story per agent. |
| Least privilege | The agent has a scope ceiling set at approval time. |
| Delegation | When acting for a person, the token says so (`sub` = person, `act` = agent). |
| Control | Suspending the agent stops it, immediately. |

## The building blocks

| Concept | In this showcase |
|---|---|
| **Agent record** | Name, description, owner, allowed scopes, status (`DEFAULT`, `ACTIVE`, `SUSPENDED`, `UNDER_REVIEW`). `agent_identity/models.py` |
| **Separation of duties** | A developer creates the agent; a different approver activates it. The creator cannot approve their own agent. |
| **OAuth client** | Registered after approval; the secret is shown once. |
| **Scope ceiling** | `allowed_scopes` on the record. Tokens can never exceed it. |
| **Autonomous token** | `client_credentials` grant. Claims include `agent_id`. |
| **Delegated token** | RFC 8693 token exchange. `sub` = user, `act.sub` = agent. Effective scope is the intersection of user and agent scopes. |
| **Privileged token** | A delegated token that also carries `authorization_details` naming the exact action; very short lived. |
| **Actor chain** | Nested `act` claims: supervisor then worker. |
| **Kill switch** | Suspending the agent. With the strict status check, even unexpired tokens are refused. |

## The six flows

Each flow has a sequence diagram in [`Architecture.md`](Architecture.md) and an interactive version on the "How Agent Identity works" page.

1. **Onboarding**: create, approve, register a client.
2. **Autonomous access**: the agent acts as itself (Revenue Analyst).
3. **On-behalf-of**: the agent acts for the signed-in user (Learning Coach).
4. **Privileged action**: human approval plus a narrow, short-lived token (expensive enrolment, service restart).
5. **Agent chain**: a supervisor delegates to a worker and the chain is preserved in the token.
6. **Kill switch**: suspend and watch calls fail.

## Reading a delegated token

```json
{
  "iss": "https://mock-verify.local",
  "sub": "carol",
  "aud": "hr-api",
  "scope": "courses:enroll",
  "act": { "sub": "agt-a2decf6be6e1" },
  "authorization_details": [ { "type": "course_enrollment", "course_id": "SEC301", "max_cost": 1800 } ],
  "exp": 1793873257
}
```

Read it as: *the agent `agt-a2decf6be6e1` is acting for Carol, may only enroll, only for SEC301, and only until `exp`.* (Illustrative claims; open the Token inspector for real ones.)

## Two layers of control

| Layer | Owner | Examples | When it runs |
|---|---|---|---|
| Cuga policies | agent framework | intent guard, tool approval | before the tool is called |
| Identity checks | resource server | signature, audience, expiry, scope, agent status | inside every tool |

Neither replaces the other. Policies shape what the agent attempts; identity checks decide what is allowed.

## Known behaviour worth remembering

- In the identity service, suspending an agent blocks **new** token issuance only. Tokens already issued stay valid until they expire. The showcase closes this gap with `AI_STRICT_STATUS_CHECK=true`, which looks up the agent status on every validation.
- Short token lifetimes are the second line of defence for the same reason.
