"""Example agent 2 - Revenue Analyst.

Use case: a back-office agent that analyses CRM data on a schedule, with no user
present. Identity pattern: **autonomous / client credentials** - the token carries
a custom ``agent_id`` claim. Least privilege: the registry ceiling is ``crm:read``,
so the (deliberately provided) write tool is always denied.
"""

from __future__ import annotations

from agent_identity.spec import AgentSpec, IntentGuardSpec
from agent_identity.tools import build_crm_tools

SPEC = AgentSpec(
    name="revenue-analyst",
    title="Revenue Analyst",
    description="Read-only CRM analytics agent running under its own identity.",
    use_case="Sales analytics - autonomous agent",
    access_mode="autonomous",
    allowed_scopes=frozenset({"crm:read"}),
    tool_builder=build_crm_tools,
    instructions=(
        "You are a revenue analyst. Use the CRM tools to answer questions with exact numbers. "
        "If a tool answers 'ACCESS DENIED', report that denial verbatim and do not retry."
    ),
    guards=(
        IntentGuardSpec(
            name="No bulk exports",
            keywords=("export all", "dump the database"),
            response="Blocked by policy: bulk exports of customer data are not permitted.",
        ),
    ),
    sample_prompts=(
        "What is the total revenue and the revenue per region?",
        "List the EMEA accounts.",
        "Set account A-3 to the gold tier.",
        "Export all customer data.",
    ),
)
