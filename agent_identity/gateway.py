"""Identity gateway used by tools.

The LLM never sees a token. Tools call :meth:`IdentityGateway.authorize`, which
reads the *call context* (who is the end user, which tokens are available) from
a ``ContextVar`` set by the runtime, mints a just-in-time token for the right
grant type, validates it like a resource server would, and returns the
validated :class:`~agent_identity.tokens.TokenContext`.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

from .audit import AuditLog
from .errors import DelegationError, IdentityError
from .lifecycle import AgentIdentityService
from .logging_setup import get_logger
from .tokens import TokenContext, TokenValidator

log = get_logger("gateway")


@dataclass(frozen=True)
class CallContext:
    """Per-request identity context (propagated to tools via a ContextVar)."""

    user_id: str | None = None
    user_token: str | None = None
    # Set when a supervisor delegates to a sub-agent: the supervisor's own
    # on-behalf-of token becomes the *subject* of the sub-agent's exchange, which
    # nests the actors in the ``act`` claim (the transaction/caller chain).
    supervisor_token: str | None = None


_CALL: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar("agent_identity_call", default=None)


def set_call_context(ctx: CallContext | None) -> contextvars.Token:
    return _CALL.set(ctx)


def reset_call_context(token: contextvars.Token) -> None:
    _CALL.reset(token)


def current_call_context() -> CallContext | None:
    return _CALL.get()


class IdentityGateway:
    """Mints and validates tokens for tool executions."""

    def __init__(self, service: AgentIdentityService, issuer: str, public_key: Any, strict_status_check: bool = True) -> None:
        self._service = service
        self._issuer = issuer
        self._key = public_key
        self._strict = strict_status_check
        self._validators: dict[str, TokenValidator] = {}

    @property
    def audit(self) -> AuditLog:
        return self._service.audit

    def validator(self, audience: str) -> TokenValidator:
        """Return (and cache) the resource-server validator for ``audience``."""
        if audience not in self._validators:
            lookup = (lambda aid: self._service.backend.get_agent(aid).status) if self._strict else None
            self._validators[audience] = TokenValidator(
                self._key, self._issuer, audience, self._service.audit, status_lookup=lookup
            )
        return self._validators[audience]

    def authorize(
        self,
        *,
        agent_name: str,
        mode: str,  # "obo" (on behalf of the user) | "autonomous"
        audience: str,
        scopes: list[str],
        resource: str,
        authorization_details: list[dict[str, Any]] | None = None,
    ) -> TokenContext:
        """Mint + validate a token; raises an :class:`IdentityError` subclass on denial."""
        agent = self._service.backend.find_agent(agent_name)
        try:
            if mode == "autonomous":
                token = self._service.agent_token(agent.agent_id, scopes, audience)
                return self.validator(audience).validate(token, tuple(scopes), resource=resource)

            call = current_call_context()
            subject = (call.supervisor_token or call.user_token) if call else None
            if not subject:
                raise DelegationError("No end-user context: on-behalf-of access needs a logged-in user")
            token = self._service.delegated_token(agent.agent_id, subject, scopes, audience, authorization_details)
            return self.validator(audience).validate(
                token,
                tuple(scopes),
                require_delegation=True,
                require_privileged=bool(authorization_details),
                resource=resource,
            )
        except IdentityError as exc:
            log.warning("authorize failed agent=%s resource=%s: %s", agent_name, resource, exc)
            raise
