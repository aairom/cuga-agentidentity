"""Example agents. Each module exposes a ``SPEC`` (:class:`AgentSpec`)."""

from __future__ import annotations

from agent_identity.spec import AgentSpec

from .learning_coach import SPEC as LEARNING_COACH
from .ops_sre_assistant import SPEC as OPS_SRE
from .revenue_analyst import SPEC as REVENUE_ANALYST

#: Specs of the worker agents, keyed by registry name.
WORKER_SPECS: dict[str, AgentSpec] = {s.name: s for s in (LEARNING_COACH, REVENUE_ANALYST, OPS_SRE)}

SUPERVISOR_NAME = "workplace-supervisor"
#: Scopes the supervisor may delegate (union of the user-facing worker scopes).
SUPERVISOR_SCOPES = frozenset(
    {"courses:read", "courses:enroll", "ops:read", "ops:restart", "crm:read"}
)

#: The supervisor has no tools of its own; it delegates to the workers (A2A-style).
SUPERVISOR_SPEC = AgentSpec(
    name=SUPERVISOR_NAME,
    title="Workplace Supervisor",
    description="Routes a request to the right specialist agent and keeps the caller chain in the token.",
    use_case="Multi-agent orchestration - nested delegation (transaction context)",
    access_mode="obo",
    allowed_scopes=SUPERVISOR_SCOPES,
    tool_builder=None,
    instructions="Delegate each request to the most suitable specialist agent. Relay ACCESS DENIED messages verbatim.",
    sample_prompts=("What is the total revenue?", "List the available courses.", "Which services are degraded?"),
)

__all__ = ["WORKER_SPECS", "SUPERVISOR_NAME", "SUPERVISOR_SCOPES", "SUPERVISOR_SPEC", "LEARNING_COACH", "REVENUE_ANALYST", "OPS_SRE"]
