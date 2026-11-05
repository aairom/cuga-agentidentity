"""Token issuance, exchange (OBO) and resource-server validation tests."""

from __future__ import annotations

import time

import jwt
import pytest

from agent_identity.errors import (
    AgentNotActive,
    ApprovalRequired,
    DelegationError,
    InsufficientScope,
    TokenValidationError,
)
from agent_identity.runtime import APPROVER
from agent_identity.tokens import TokenValidator, decode_unverified

AUD = "res"


@pytest.fixture()
def validator(service, backend):
    return TokenValidator(backend.verification_key(), backend.issuer, AUD, service.audit,
                          status_lookup=lambda a: backend.get_agent(a).status)


def test_client_credentials_token_has_agent_id_claim(service, active_agent, validator):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    ctx = validator.validate(tok, ("a:read",))
    assert ctx.agent_id == active_agent.agent_id and not ctx.is_delegated


def test_obo_exchange_has_sub_user_and_act_agent(service, active_agent, validator):
    user = service.user_login("carol", ["a:read", "a:write"])
    tok = service.delegated_token(active_agent.agent_id, user, ["a:read"], AUD)
    claims = decode_unverified(tok)
    assert claims["sub"] == "carol" and claims["act"]["sub"] == active_agent.agent_id
    ctx = validator.validate(tok, ("a:read",), require_delegation=True)
    assert ctx.subject == "carol" and ctx.chain == (active_agent.agent_id,)


def test_effective_scope_is_intersection_of_user_and_agent(service, active_agent):
    user = service.user_login("carol", ["a:read"])  # user lacks a:write
    with pytest.raises(InsufficientScope):
        service.delegated_token(active_agent.agent_id, user, ["a:write"], AUD)


def test_exchange_refused_when_agent_suspended(service, active_agent):
    user = service.user_login("carol", ["a:read"])
    service.suspend_agent(APPROVER, active_agent.agent_id)
    with pytest.raises(AgentNotActive):
        service.delegated_token(active_agent.agent_id, user, ["a:read"], AUD)


def test_privileged_token_is_short_lived_and_carries_authorization_details(service, active_agent, validator):
    user = service.user_login("carol", ["a:read", "a:write"])
    details = [{"type": "x", "id": 1}]
    tok = service.delegated_token(active_agent.agent_id, user, ["a:write"], AUD, details)
    claims = decode_unverified(tok)
    assert claims["exp"] - claims["iat"] <= 60 and claims["authorization_details"] == details
    assert validator.validate(tok, ("a:write",), require_delegation=True, require_privileged=True).privileged


def test_privileged_required_but_plain_token(service, active_agent, validator):
    user = service.user_login("carol", ["a:read", "a:write"])
    tok = service.delegated_token(active_agent.agent_id, user, ["a:write"], AUD)
    with pytest.raises(ApprovalRequired):
        validator.validate(tok, ("a:write",), require_delegation=True, require_privileged=True)


def test_delegation_required_but_autonomous_token(service, active_agent, validator):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    with pytest.raises(DelegationError):
        validator.validate(tok, ("a:read",), require_delegation=True)


def test_missing_scope_denied(service, active_agent, validator):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    with pytest.raises(InsufficientScope):
        validator.validate(tok, ("a:write",))


def test_wrong_audience_and_missing_token(service, active_agent, validator):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], "other")
    with pytest.raises(TokenValidationError):
        validator.validate(tok)
    with pytest.raises(TokenValidationError):
        validator.validate(None)


def test_tampered_and_foreign_signature_rejected(service, active_agent, validator):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    with pytest.raises(TokenValidationError):
        validator.validate(tok[:-4] + "AAAA")
    forged = jwt.encode({"iss": "https://issuer.test", "aud": AUD, "sub": "x", "exp": int(time.time()) + 60},
                        "secret-secret-secret-secret-1234", algorithm="HS256")
    with pytest.raises(TokenValidationError):
        validator.validate(forged)


def test_expired_token_rejected(service, backend, active_agent, validator):
    backend.token_ttl = -10
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    with pytest.raises(TokenValidationError):
        validator.validate(tok)


def test_suspension_does_not_revoke_issued_token_unless_strict(service, backend, active_agent):
    """Mirrors IBM behaviour: issued tokens survive suspension; the strict check closes the gap."""
    lax = TokenValidator(backend.verification_key(), backend.issuer, AUD, service.audit)
    strict = TokenValidator(backend.verification_key(), backend.issuer, AUD, service.audit,
                            status_lookup=lambda a: backend.get_agent(a).status)
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    service.suspend_agent(APPROVER, active_agent.agent_id)
    assert lax.validate(tok).agent_id == active_agent.agent_id  # still accepted
    with pytest.raises(AgentNotActive):
        strict.validate(tok)


def test_nested_delegation_chain_preserved(service, active_agent, validator):
    """Supervisor -> sub-agent: the second exchange nests act claims (transaction context)."""
    sup = service.onboard(__import__("agent_identity.runtime", fromlist=["x"]).DEVELOPER, APPROVER, "sup", "d", {"a:read"})
    user = service.user_login("carol", ["a:read"])
    sup_tok = service.delegated_token(sup.agent_id, user, ["a:read"], "token-exchange")
    sub_tok = service.delegated_token(active_agent.agent_id, sup_tok, ["a:read"], AUD)
    ctx = validator.validate(sub_tok, ("a:read",), require_delegation=True)
    assert ctx.subject == "carol" and ctx.chain == (active_agent.agent_id, sup.agent_id)


def test_decode_unverified_never_validates(service, active_agent):
    tok = service.agent_token(active_agent.agent_id, ["a:read"], AUD)
    assert decode_unverified(tok)["aud"] == AUD
