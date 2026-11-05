"""Identity-guarded tools for the example agents.

Every tool obtains a token through the :class:`IdentityGateway` *before* touching
data. Denials are returned as ``ACCESS DENIED: ...`` text so the model can relay
them faithfully, and are also written to the audit log.
"""

from __future__ import annotations

import json
from typing import Callable

from langchain_core.tools import BaseTool, tool

from .data import COURSES, PRIVILEGED_COST_THRESHOLD, USERS, Store
from .errors import IdentityError
from .gateway import IdentityGateway
from .logging_setup import get_logger

log = get_logger("tools")

AUD_HR = "hr-api"
AUD_CRM = "crm-api"
AUD_OPS = "ops-api"


def _denied(exc: IdentityError) -> str:
    return f"ACCESS DENIED ({type(exc).__name__}): {exc}"


def build_course_tools(gw: IdentityGateway, store: Store, agent_name: str) -> list[BaseTool]:
    """Tools for the learning agent. Mode: on-behalf-of the logged-in employee."""

    @tool
    def list_courses() -> str:
        """List the available training courses with ids, titles and costs (USD)."""
        try:
            gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR, scopes=["courses:read"],
                         resource="hr.list_courses")
        except IdentityError as exc:
            return _denied(exc)
        return json.dumps([{"id": k, **v} for k, v in COURSES.items()])

    @tool
    def enroll_course(course_id: str) -> str:
        """Enroll the current user in a course costing up to 500 USD. For dearer courses use enroll_expensive_course."""
        course = COURSES.get(course_id.upper())
        if not course:
            return f"Unknown course '{course_id}'."
        if int(course["cost"]) > PRIVILEGED_COST_THRESHOLD:  # type: ignore[arg-type]
            return (f"Course {course_id} costs {course['cost']} USD (> {PRIVILEGED_COST_THRESHOLD}). "
                    "Use enroll_expensive_course, which needs human approval.")
        try:
            ctx = gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR, scopes=["courses:enroll"],
                               resource="hr.enroll")
        except IdentityError as exc:
            return _denied(exc)
        with store.lock:
            store.enrollments.setdefault(ctx.subject, []).append(course_id.upper())
        return f"Enrolled {ctx.subject} in {course_id.upper()} (acting agent chain: {' <- '.join(ctx.chain)})."

    @tool
    def enroll_expensive_course(course_id: str) -> str:
        """Enroll the current user in a course costing MORE than 500 USD. Requires human approval and a privileged token."""
        course = COURSES.get(course_id.upper())
        if not course:
            return f"Unknown course '{course_id}'."
        details = [{"type": "course_enrollment", "course_id": course_id.upper(), "max_cost": course["cost"]}]
        try:
            ctx = gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR, scopes=["courses:enroll"],
                               resource="hr.enroll_privileged", authorization_details=details)
        except IdentityError as exc:
            return _denied(exc)
        with store.lock:
            store.enrollments.setdefault(ctx.subject, []).append(course_id.upper())
        return (f"Enrolled {ctx.subject} in {course_id.upper()} using a short-lived privileged token "
                f"(authorization_details={ctx.authorization_details[0]}).")

    @tool
    def my_enrollments() -> str:
        """Show the courses the current user is enrolled in."""
        try:
            ctx = gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_HR, scopes=["courses:read"],
                               resource="hr.my_enrollments")
        except IdentityError as exc:
            return _denied(exc)
        return json.dumps(store.enrollments.get(ctx.subject, []))

    return [list_courses, enroll_course, enroll_expensive_course, my_enrollments]


def build_crm_tools(gw: IdentityGateway, store: Store, agent_name: str) -> list[BaseTool]:
    """Tools for the revenue analyst. Mode: autonomous (client credentials), read-only."""

    @tool
    def list_accounts(region: str = "") -> str:
        """List CRM accounts, optionally filtered by region (EMEA, AMER, APAC)."""
        try:
            gw.authorize(agent_name=agent_name, mode="autonomous", audience=AUD_CRM, scopes=["crm:read"],
                         resource="crm.list_accounts")
        except IdentityError as exc:
            return _denied(exc)
        rows = [a for a in store.accounts if not region or a["region"].lower() == region.lower()]  # type: ignore[union-attr]
        return json.dumps(rows)

    @tool
    def revenue_summary() -> str:
        """Total revenue overall and per region."""
        try:
            gw.authorize(agent_name=agent_name, mode="autonomous", audience=AUD_CRM, scopes=["crm:read"],
                         resource="crm.revenue_summary")
        except IdentityError as exc:
            return _denied(exc)
        by_region: dict[str, int] = {}
        for a in store.accounts:
            by_region[a["region"]] = by_region.get(a["region"], 0) + int(a["revenue"])  # type: ignore[arg-type,index]
        return json.dumps({"total": sum(by_region.values()), "by_region": by_region})

    @tool
    def update_account_tier(account_id: str, tier: str) -> str:
        """Change an account tier (gold/silver/bronze). This agent is read-only, so this is expected to be denied."""
        try:
            gw.authorize(agent_name=agent_name, mode="autonomous", audience=AUD_CRM, scopes=["crm:write"],
                         resource="crm.update_account_tier")
        except IdentityError as exc:
            return _denied(exc)
        for a in store.accounts:
            if a["id"] == account_id:
                a["tier"] = tier
                return f"Updated {account_id} to {tier}"
        return "Account not found"

    return [list_accounts, revenue_summary, update_account_tier]


def build_ops_tools(gw: IdentityGateway, store: Store, agent_name: str) -> list[BaseTool]:
    """Tools for the IT-ops agent. Mode: on-behalf-of the SRE; restart needs approval."""

    @tool
    def list_services() -> str:
        """List infrastructure services and their status."""
        try:
            gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_OPS, scopes=["ops:read"],
                         resource="ops.list_services")
        except IdentityError as exc:
            return _denied(exc)
        return json.dumps(store.services)

    @tool
    def restart_service(service: str) -> str:
        """Restart a service. Requires the user to hold ops:restart AND a human approval."""
        if service not in store.services:
            return f"Unknown service '{service}'."
        details = [{"type": "service_restart", "service": service}]
        try:
            gw.authorize(agent_name=agent_name, mode="obo", audience=AUD_OPS, scopes=["ops:restart"],
                         resource="ops.restart_service", authorization_details=details)
        except IdentityError as exc:
            return _denied(exc)
        with store.lock:
            store.services[service] = "running"
        return f"Service '{service}' restarted and is now running."

    return [list_services, restart_service]


ToolBuilder = Callable[[IdentityGateway, Store, str], list[BaseTool]]


def user_exists(user_id: str) -> bool:
    return user_id in USERS
