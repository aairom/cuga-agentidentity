"""Example agent 3 - Ops SRE Assistant.

Use case: an SRE asks the agent to inspect and repair services.
Identity pattern: **on-behalf-of + human-in-the-loop**. The user's own entitlements
bound what the agent can do (Carol has no ``ops:restart``; Dave does), and every
restart requires a human approval plus a privileged, short-lived token.
"""

from __future__ import annotations

from agent_identity.spec import AgentSpec, ApprovalSpec, IntentGuardSpec
from agent_identity.tools import build_ops_tools

SPEC = AgentSpec(
    name="ops-sre-assistant",
    title="Ops SRE Assistant",
    description="Inspects services and restarts them on behalf of an on-call engineer.",
    use_case="IT operations - privileged actions with approval",
    access_mode="obo",
    allowed_scopes=frozenset({"ops:read", "ops:restart"}),
    tool_builder=build_ops_tools,
    instructions=(
        "You are an SRE assistant. Use list_services to inspect and restart_service to repair. "
        "If a tool answers 'ACCESS DENIED', report that denial verbatim and do not retry."
    ),
    guards=(
        IntentGuardSpec(
            name="No shutdown of everything",
            keywords=("shut down everything", "kill all"),
            response="Blocked by policy: mass shutdown is not allowed.",
        ),
    ),
    approvals=(
        ApprovalSpec(
            name="Approve service restarts",
            tools=("restart_service",),
            message="Restarting a service is disruptive. Please confirm.",
        ),
    ),
    sample_prompts=(
        "Which services are degraded?",
        "Restart the reports service.",
        "Kill all services.",
    ),
)
