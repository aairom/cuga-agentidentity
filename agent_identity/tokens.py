"""Resource-server side token validation.

A tool (resource server) must never trust a token blindly. It checks signature,
issuer, audience, expiry, scopes, the delegation (``act``) chain and - as
defence in depth - the *current* agent status, because suspending an agent in
IBM Agent Identity does not revoke tokens that were already issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import jwt

from .audit import AuditLog
from .errors import (
    AgentNotActive,
    AgentNotFound,
    ApprovalRequired,
    DelegationError,
    InsufficientScope,
    TokenValidationError,
)
from .logging_setup import get_logger
from .models import AgentStatus

log = get_logger("tokens")


@dataclass(frozen=True)
class TokenContext:
    """Validated, structured view of an access token."""

    subject: str  # user id (OBO) or OAuth client id (autonomous)
    scopes: frozenset[str]
    agent_id: str | None  # acting agent (act.sub or agent_id claim)
    chain: tuple[str, ...]  # delegation chain, outermost actor first
    privileged: bool
    authorization_details: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def is_delegated(self) -> bool:
        return bool(self.chain)


def decode_unverified(token: str) -> dict[str, Any]:
    """Decode WITHOUT verification - for display in the UI inspector only."""
    return jwt.decode(token, options={"verify_signature": False})


def _chain(act: dict[str, Any] | None) -> tuple[str, ...]:
    out: list[str] = []
    while act:
        out.append(act["sub"])
        act = act.get("act")
    return tuple(out)


class TokenValidator:
    """Validates tokens for one resource server (identified by ``audience``)."""

    def __init__(
        self,
        public_key: Any,
        issuer: str,
        audience: str,
        audit: AuditLog,
        status_lookup: Callable[[str], AgentStatus] | None = None,
        algorithms: tuple[str, ...] = ("RS256",),
    ) -> None:
        self._key = public_key
        self._issuer = issuer
        self._audience = audience
        self._audit = audit
        self._status_lookup = status_lookup
        self._algorithms = list(algorithms)

    def validate(
        self,
        token: str | None,
        required_scopes: tuple[str, ...] = (),
        *,
        require_delegation: bool = False,
        require_privileged: bool = False,
        resource: str = "",
    ) -> TokenContext:
        """Return a :class:`TokenContext` or raise a :class:`TokenValidationError` subclass."""
        if not token:
            self._deny(None, resource, "missing token")
            raise TokenValidationError("No access token presented")
        try:
            claims = jwt.decode(
                token, self._key, algorithms=self._algorithms, audience=self._audience, issuer=self._issuer
            )
        except jwt.PyJWTError as exc:
            self._deny(None, resource, f"invalid token: {exc}")
            raise TokenValidationError(f"Invalid token: {exc}") from exc

        chain = _chain(claims.get("act"))
        agent_id = chain[0] if chain else claims.get("agent_id")
        ctx = TokenContext(
            subject=claims["sub"],
            scopes=frozenset(claims.get("scope", "").split()),
            agent_id=agent_id,
            chain=chain,
            privileged=bool(claims.get("privileged")),
            authorization_details=tuple(claims.get("authorization_details", ())),
            claims=claims,
        )

        try:
            self._check(ctx, required_scopes, require_delegation, require_privileged)
        except Exception as exc:
            self._deny(ctx, resource, str(exc))
            raise
        self._audit.record(
            "access.granted",
            agent_id=ctx.agent_id,
            actor=ctx.agent_id,
            user_id=ctx.subject if ctx.is_delegated else None,
            detail={"resource": resource, "scopes": sorted(ctx.scopes), "chain": list(ctx.chain)},
        )
        return ctx

    def _check(self, ctx: TokenContext, required: tuple[str, ...], delegation: bool, privileged: bool) -> None:
        missing = set(required) - ctx.scopes
        if missing:
            raise InsufficientScope(f"Missing scope(s): {sorted(missing)}")
        if delegation and not ctx.is_delegated:
            raise DelegationError("Token has no 'act' claim: this operation needs on-behalf-of delegation")
        if privileged and not (ctx.privileged and ctx.authorization_details):
            raise ApprovalRequired("Privileged token (authorization_details) required for this action")
        if self._status_lookup and ctx.agent_id:
            try:
                status = self._status_lookup(ctx.agent_id)
            except AgentNotFound as exc:
                raise TokenValidationError(f"Unknown agent in token: {ctx.agent_id}") from exc
            if status is not AgentStatus.ACTIVE:
                raise AgentNotActive(f"Agent {ctx.agent_id} is {status.value}; refusing already-issued token")

    def _deny(self, ctx: TokenContext | None, resource: str, reason: str) -> None:
        self._audit.record(
            "access.denied",
            outcome="denied",
            agent_id=ctx.agent_id if ctx else None,
            user_id=ctx.subject if ctx and ctx.is_delegated else None,
            detail={"resource": resource, "reason": reason},
        )
        log.warning("access denied resource=%s reason=%s", resource, reason)
