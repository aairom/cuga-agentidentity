"""Registry / lifecycle governance tests."""

from __future__ import annotations

import pytest

from agent_identity.errors import (
    AgentNotActive,
    AgentNotFound,
    BackendError,
    InsufficientScope,
    PermissionDenied,
    SeparationOfDutiesError,
)
from agent_identity.models import AgentStatus, Entitlement, Principal
from agent_identity.runtime import APPROVER, DEVELOPER


def test_create_requires_write_entitlement(service):
    nobody = Principal("x", "X", frozenset({Entitlement.READ_AGENTS}))
    with pytest.raises(PermissionDenied):
        service.create_agent(nobody, "a", "d", {"s"})
    assert service.audit.events(outcome="denied")


def test_new_agent_is_default_and_cannot_register_client(service):
    rec = service.create_agent(DEVELOPER, "a", "d", {"s"})
    assert rec.status is AgentStatus.DEFAULT
    with pytest.raises(AgentNotActive):
        service.register_client(DEVELOPER, rec.agent_id)


def test_approver_activates_then_client_registered(service):
    rec = service.create_agent(DEVELOPER, "a", "d", {"s"})
    assert service.approve_agent(APPROVER, rec.agent_id).status is AgentStatus.ACTIVE
    creds = service.register_client(DEVELOPER, rec.agent_id)
    assert creds.agent_id == rec.agent_id and creds.client_secret


def test_separation_of_duties(service):
    both = Principal(DEVELOPER.user_id, "Same", frozenset({Entitlement.WRITE_AGENTS, Entitlement.MANAGE_AGENT_STATUS}))
    rec = service.create_agent(both, "a", "d", {"s"})
    with pytest.raises(SeparationOfDutiesError):
        service.approve_agent(both, rec.agent_id)


def test_developer_cannot_approve(service):
    rec = service.create_agent(DEVELOPER, "a", "d", {"s"})
    with pytest.raises(PermissionDenied):
        service.approve_agent(DEVELOPER, rec.agent_id)


def test_duplicate_name_rejected(service):
    service.create_agent(DEVELOPER, "a", "d", {"s"})
    with pytest.raises(BackendError):
        service.create_agent(DEVELOPER, "a", "d", {"s"})


def test_onboard_is_idempotent(service):
    r1 = service.onboard(DEVELOPER, APPROVER, "a", "d", {"s"})
    r2 = service.onboard(DEVELOPER, APPROVER, "a", "d", {"s"})
    assert r1.agent_id == r2.agent_id and len(service.backend.list_agents()) == 1


def test_suspend_blocks_new_tokens_and_reactivate_restores(service, active_agent):
    service.agent_token(active_agent.agent_id, ["a:read"], "aud")
    service.suspend_agent(APPROVER, active_agent.agent_id, "incident")
    with pytest.raises(AgentNotActive):
        service.agent_token(active_agent.agent_id, ["a:read"], "aud")
    service.reactivate_agent(APPROVER, active_agent.agent_id)
    assert service.agent_token(active_agent.agent_id, ["a:read"], "aud")


def test_scope_ceiling_enforced_at_issuance(service, active_agent):
    with pytest.raises(InsufficientScope):
        service.agent_token(active_agent.agent_id, ["a:admin"], "aud")


def test_unknown_agent(service):
    with pytest.raises(AgentNotFound):
        service.backend.get_agent("nope")


def test_token_without_registered_client(service):
    rec = service.create_agent(DEVELOPER, "a", "d", {"s"})
    with pytest.raises(BackendError):
        service.agent_token(rec.agent_id, ["s"], "aud")


def test_audit_correlates_by_agent_id(service, active_agent):
    service.agent_token(active_agent.agent_id, ["a:read"], "aud")
    events = {e.event for e in service.audit.events(agent_id=active_agent.agent_id)}
    assert {"agent.created", "agent.approved", "client.registered", "token.issued"} <= events
