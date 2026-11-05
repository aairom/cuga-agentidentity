"""Example agent 1 - Learning Coach.

Use case: an employee asks an agent to browse and enrol in training courses.
Identity pattern: **on-behalf-of (RFC 8693)** - tokens carry ``sub=<employee>`` and
``act.sub=<agent>``. Expensive courses need a human approval (Cuga tool-approval
policy) *and* a short-lived privileged token (``authorization_details``).
"""

from __future__ import annotations

from agent_identity.spec import AgentSpec, ApprovalSpec, IntentGuardSpec
from agent_identity.tools import build_course_tools

SPEC = AgentSpec(
    name="learning-coach",
    title="Learning Coach",
    description="Helps employees discover and enrol in training courses on their behalf.",
    use_case="HR / learning - delegated user actions",
    access_mode="obo",
    allowed_scopes=frozenset({"courses:read", "courses:enroll"}),
    tool_builder=build_course_tools,
    instructions=(
        "You are the Learning Coach. Use the provided tools to list courses and enrol the current user. "
        "Courses above 500 USD must be enrolled with enroll_expensive_course. "
        "If a tool answers 'ACCESS DENIED', report that denial verbatim and do not retry."
    ),
    guards=(
        IntentGuardSpec(
            name="No bulk record deletion",
            keywords=("delete all", "wipe", "erase all"),
            response="Blocked by policy: this agent cannot delete records.",
        ),
    ),
    approvals=(
        ApprovalSpec(
            name="Approve expensive enrolments",
            tools=("enroll_expensive_course",),
            message="This enrolment exceeds 500 USD. A human must approve it.",
        ),
    ),
    sample_prompts=(
        "List the available courses.",
        "Enroll me in PY101.",
        "Enroll me in SEC301.",
        "Delete all employee records.",
    ),
)
