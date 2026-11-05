"""Agent lifecycle service: create -> approve -> credential -> operate -> suspend.

Implements the IBM Agent Identity governance model:

* **Separation of duties** - the developer (``writeAgents``) creates the record;
  a *different* approver (``manageAgentStatus``) activates it.
* Only ACTIVE agents can register an OAuth client or obtain tokens.
* Every step is written to the audit log, keyed by Agent ID.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditLog
from .backend import IdentityBackend, MockVerifyBackend
from .errors import AgentNotActive, BackendError, PermissionDenied, SeparationOfDutiesError
from .logging_setup import get_logger
from .models import AgentRecord, AgentStatus, ClientCredentials, Entitlement, Principal

log = get_logger("lifecycle")


class AgentIdentityService:
    """Facade over the identity backend that enforces governance rules."""

    def __init__(self, backend: IdentityBackend, audit: AuditLog | None = None) -> None:
        self.backend = backend
        self.audit = audit or AuditLog()
        self._creds: dict[str, ClientCredentials] = {}  # agent_id -> credentials (held once)

    # ---- helpers --------------------------------------------------------------
    @staticmethod
    def _require(principal: Principal, ent: Entitlement) -> None:
        if not principal.has(ent):
            raise PermissionDenied(f"{principal.display_name} lacks entitlement '{ent.value}'")

    # ---- management plane -----------------------------------------------------
    def create_agent(self, dev: Principal, name: str, description: str, allowed_scopes: set[str] | frozenset[str]) -> AgentRecord:
        """Developer creates the agent record (status DEFAULT)."""
        try:
            self._require(dev, Entitlement.WRITE_AGENTS)
            rec = self.backend.create_agent(name, description, dev.user_id, frozenset(allowed_scopes))
        except Exception as exc:
            self.audit.record("agent.create", plane="management", outcome="denied" if isinstance(exc, PermissionDenied) else "error",
                              actor=dev.user_id, detail={"name": name, "error": str(exc)})
            raise
        self.audit.record("agent.created", plane="management", agent_id=rec.agent_id, actor=dev.user_id,
                          detail={"name": name, "scopes": sorted(rec.allowed_scopes)})
        return rec

    def approve_agent(self, approver: Principal, agent_id: str) -> AgentRecord:
        """A different person with ``manageAgentStatus`` activates the agent."""
        try:
            self._require(approver, Entitlement.MANAGE_AGENT_STATUS)
            rec = self.backend.get_agent(agent_id)
            if rec.owner == approver.user_id:
                raise SeparationOfDutiesError("Creator cannot approve their own agent")
            rec = self.backend.set_status(agent_id, AgentStatus.ACTIVE, approver.user_id)
        except Exception as exc:
            self.audit.record("agent.approve", plane="management", outcome="denied", agent_id=agent_id,
                              actor=approver.user_id, detail={"error": str(exc)})
            raise
        self.audit.record("agent.approved", plane="management", agent_id=agent_id, actor=approver.user_id)
        return rec

    def register_client(self, dev: Principal, agent_id: str) -> ClientCredentials:
        """Developer registers an OAuth client (DCR) bound to the ACTIVE agent."""
        try:
            self._require(dev, Entitlement.WRITE_AGENTS)
            creds = self.backend.register_client(agent_id)
        except Exception as exc:
            self.audit.record("client.register", plane="management", outcome="denied", agent_id=agent_id,
                              actor=dev.user_id, detail={"error": str(exc)})
            raise
        self._creds[agent_id] = creds
        self.audit.record("client.registered", plane="management", agent_id=agent_id, actor=dev.user_id,
                          detail={"client_id": creds.client_id})
        return creds

    def suspend_agent(self, approver: Principal, agent_id: str, reason: str = "") -> AgentRecord:
        """Kill-switch: blocks NEW tokens (already issued tokens remain valid until expiry)."""
        self._require(approver, Entitlement.MANAGE_AGENT_STATUS)
        rec = self.backend.set_status(agent_id, AgentStatus.SUSPENDED, approver.user_id)
        self.audit.record("agent.suspended", plane="management", agent_id=agent_id, actor=approver.user_id,
                          detail={"reason": reason})
        return rec

    def reactivate_agent(self, approver: Principal, agent_id: str) -> AgentRecord:
        self._require(approver, Entitlement.MANAGE_AGENT_STATUS)
        rec = self.backend.set_status(agent_id, AgentStatus.ACTIVE, approver.user_id)
        self.audit.record("agent.reactivated", plane="management", agent_id=agent_id, actor=approver.user_id)
        return rec

    def onboard(self, dev: Principal, approver: Principal, name: str, description: str,
                allowed_scopes: set[str] | frozenset[str]) -> AgentRecord:
        """Convenience: create -> approve -> register client in one go (idempotent by name)."""
        try:
            rec = self.backend.find_agent(name)
        except Exception:
            rec = self.create_agent(dev, name, description, allowed_scopes)
        if rec.status is not AgentStatus.ACTIVE:
            rec = self.approve_agent(approver, rec.agent_id)
        if rec.agent_id not in self._creds:
            self.register_client(dev, rec.agent_id)
        return self.backend.get_agent(rec.agent_id)

    # ---- runtime plane --------------------------------------------------------
    def agent_token(self, agent_id: str, scopes: list[str], audience: str) -> str:
        """Autonomous access: client-credentials token carrying ``agent_id``."""
        creds = self._creds.get(agent_id)
        if creds is None:
            raise BackendError(f"No client registered for agent {agent_id}")
        try:
            token = self.backend.client_credentials_token(creds.client_id, creds.client_secret, scopes, audience)
        except Exception as exc:
            self.audit.record("token.refused", outcome="denied", agent_id=agent_id, actor=agent_id,
                              detail={"grant": "client_credentials", "error": str(exc)})
            raise
        self.audit.record("token.issued", agent_id=agent_id, actor=agent_id,
                          detail={"grant": "client_credentials", "scopes": scopes, "audience": audience})
        return token

    def delegated_token(self, agent_id: str, user_token: str, scopes: list[str], audience: str,
                        authorization_details: list[dict[str, Any]] | None = None) -> str:
        """On-behalf-of: RFC 8693 exchange (subject=user, actor=agent) -> ``sub=user``, ``act.sub=agent``."""
        actor_token = self.agent_token(agent_id, scopes=[], audience="token-exchange")
        try:
            token = self.backend.exchange_token(user_token, actor_token, scopes, audience, authorization_details)
        except Exception as exc:
            self.audit.record("token.refused", outcome="denied", agent_id=agent_id, actor=agent_id,
                              detail={"grant": "token-exchange", "error": str(exc)})
            raise
        self.audit.record("token.issued", agent_id=agent_id, actor=agent_id,
                          detail={"grant": "token-exchange", "scopes": scopes, "audience": audience,
                                  "privileged": bool(authorization_details)})
        return token

    def user_login(self, user_id: str, scopes: list[str], audience: str = "token-exchange") -> str:
        """Mock end-user login (only available with the mock backend)."""
        if not isinstance(self.backend, MockVerifyBackend):
            raise BackendError("user_login is only available with the mock backend")
        return self.backend.issue_user_token(user_id, scopes, audience)

    def require_active(self, agent_id: str) -> AgentRecord:
        rec = self.backend.get_agent(agent_id)
        if rec.status is not AgentStatus.ACTIVE:
            raise AgentNotActive(f"Agent {rec.name} is {rec.status.value}")
        return rec
