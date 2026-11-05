"""Guarded-tool tests (no LLM): the gateway + ContextVar identity propagation."""

from __future__ import annotations

import json

import pytest

from agent_identity.gateway import CallContext, reset_call_context, set_call_context
from agent_identity.models import AgentStatus
from agent_identity.runtime import APPROVER
from agent_identity.tools import build_course_tools, build_crm_tools, build_ops_tools
from agents import LEARNING_COACH, OPS_SRE, REVENUE_ANALYST


@pytest.fixture()
def tools(showcase):
    for spec in (LEARNING_COACH, REVENUE_ANALYST, OPS_SRE):
        showcase.onboard(spec)
    gw, st = showcase.gateway, showcase.store
    return {
        "course": {t.name: t for t in build_course_tools(gw, st, LEARNING_COACH.name)},
        "crm": {t.name: t for t in build_crm_tools(gw, st, REVENUE_ANALYST.name)},
        "ops": {t.name: t for t in build_ops_tools(gw, st, OPS_SRE.name)},
    }


@pytest.fixture()
def as_user(showcase):
    handles = []

    def _login(user: str):
        handles.append(set_call_context(CallContext(user_id=user, user_token=showcase.login(user))))

    yield _login
    for h in reversed(handles):
        reset_call_context(h)


def test_obo_tool_without_user_context_is_denied(tools):
    assert "ACCESS DENIED (DelegationError)" in tools["course"]["list_courses"].invoke({})


def test_list_and_enroll_on_behalf_of_user(tools, as_user, showcase):
    as_user("carol")
    assert len(json.loads(tools["course"]["list_courses"].invoke({}))) == 3
    out = tools["course"]["enroll_course"].invoke({"course_id": "py101"})
    assert "Enrolled carol in PY101" in out and "<-" not in out  # single actor in chain
    assert showcase.store.enrollments["carol"] == ["PY101"]


def test_expensive_course_redirects_and_uses_privileged_token(tools, as_user, showcase):
    as_user("carol")
    assert "enroll_expensive_course" in tools["course"]["enroll_course"].invoke({"course_id": "SEC301"})
    out = tools["course"]["enroll_expensive_course"].invoke({"course_id": "SEC301"})
    assert "privileged token" in out and "SEC301" in showcase.store.enrollments["carol"]


def test_unknown_course(tools, as_user):
    as_user("carol")
    assert "Unknown course" in tools["course"]["enroll_course"].invoke({"course_id": "ZZZ"})


def test_user_without_scope_cannot_restart_even_via_agent(tools, as_user):
    as_user("carol")  # carol has no ops:restart
    out = tools["ops"]["restart_service"].invoke({"service": "reports"})
    assert "ACCESS DENIED (InsufficientScope)" in out


def test_sre_can_restart_with_privileged_token(tools, as_user, showcase):
    as_user("dave")
    assert "restarted" in tools["ops"]["restart_service"].invoke({"service": "reports"})
    assert showcase.store.services["reports"] == "running"
    assert "Unknown service" in tools["ops"]["restart_service"].invoke({"service": "nope"})


def test_autonomous_agent_read_only(tools, showcase):
    summary = json.loads(tools["crm"]["revenue_summary"].invoke({}))
    assert summary["total"] == 3_265_000 and summary["by_region"]["EMEA"] == 1_465_000
    assert len(json.loads(tools["crm"]["list_accounts"].invoke({"region": "emea"}))) == 2
    # least privilege: the registry ceiling is crm:read only
    assert "ACCESS DENIED (InsufficientScope)" in tools["crm"]["update_account_tier"].invoke(
        {"account_id": "A-3", "tier": "gold"})
    assert showcase.store.accounts[2]["tier"] == "bronze"


def test_suspended_agent_is_denied_everywhere(tools, as_user, showcase):
    as_user("carol")
    rec = showcase.backend.find_agent(LEARNING_COACH.name)
    showcase.service.suspend_agent(APPROVER, rec.agent_id, "test")
    assert rec.status is AgentStatus.SUSPENDED
    assert "ACCESS DENIED (AgentNotActive)" in tools["course"]["list_courses"].invoke({})
    assert showcase.audit.events(outcome="denied")


def test_audit_trail_records_user_and_agent(tools, as_user, showcase):
    as_user("carol")
    tools["course"]["list_courses"].invoke({})
    ev = [e for e in showcase.audit.events() if e.event == "access.granted"][-1]
    assert ev.user_id == "carol" and ev.agent_id == showcase.backend.find_agent(LEARNING_COACH.name).agent_id
