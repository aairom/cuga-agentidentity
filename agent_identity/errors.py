"""Exception hierarchy for the Agent Identity showcase.

Every error raised by the identity layer derives from :class:`IdentityError`
so callers (tools, UI, tests) can handle them uniformly.
"""

from __future__ import annotations


class IdentityError(Exception):
    """Base class for all identity-layer errors."""


class BackendError(IdentityError):
    """The identity backend (mock or IBM Verify) rejected or failed a request."""


class AgentNotFound(IdentityError):
    """No agent with the requested id / name exists in the registry."""


class AgentNotActive(IdentityError):
    """The agent is not in the ACTIVE state, so it cannot receive credentials/tokens."""


class PermissionDenied(IdentityError):
    """A management principal lacks the entitlement required for the operation."""


class SeparationOfDutiesError(PermissionDenied):
    """The same person tried to both create and approve an agent."""


class TokenValidationError(IdentityError):
    """A presented token is malformed, expired, mis-signed or has a wrong audience/issuer."""


class InsufficientScope(TokenValidationError):
    """The token is valid but does not carry the scope(s) the operation needs."""


class DelegationError(TokenValidationError):
    """The token lacks (or has an unacceptable) on-behalf-of / delegation chain."""


class ApprovalRequired(IdentityError):
    """A privileged action was attempted without a human approver (HITL / CIBA)."""


class LLMEngineError(Exception):
    """Base class for LLM-engine problems. Always carries a human-readable ``remediation``."""

    def __init__(self, message: str, remediation: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remediation = remediation

    def __str__(self) -> str:  # message + hint, so one line in logs / UI is enough
        return f"{self.message} Hint: {self.remediation}" if self.remediation else self.message


class EngineMisconfigured(LLMEngineError):
    """The engine configuration is invalid or incomplete (bad URL, missing key or model...)."""


class EngineUnavailable(LLMEngineError):
    """The engine is not installed, not running or does not know the requested model."""


class EngineConnectionError(LLMEngineError):
    """The engine endpoint exists but the connection or authentication failed."""


class EngineSwitchError(LLMEngineError):
    """A runtime engine switch could not be completed (the previous engine was restored)."""
