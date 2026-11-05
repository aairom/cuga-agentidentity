"""Domain models shared by the identity layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    """Lifecycle states of an agent in the IBM Agent Registry."""

    DEFAULT = "DEFAULT"  # created by the developer, not yet approved
    ACTIVE = "ACTIVE"  # approved: may register clients and obtain tokens
    SUSPENDED = "SUSPENDED"  # blocks NEW token issuance (issued tokens stay valid!)
    UNDER_REVIEW = "UNDER_REVIEW"


class Entitlement(str, Enum):
    """Management-plane entitlements (names follow the IBM docs)."""

    READ_AGENTS = "readAgents"
    WRITE_AGENTS = "writeAgents"
    MANAGE_AGENT_STATUS = "manageAgentStatus"


@dataclass(frozen=True)
class Principal:
    """A human management user (developer or approver)."""

    user_id: str
    display_name: str
    entitlements: frozenset[Entitlement]

    def has(self, ent: Entitlement) -> bool:
        return ent in self.entitlements


@dataclass
class AgentRecord:
    """An entry of the Agent Registry."""

    agent_id: str
    name: str
    description: str
    owner: str  # user_id of the creating developer
    status: AgentStatus = AgentStatus.DEFAULT
    allowed_scopes: frozenset[str] = frozenset()
    approved_by: str | None = None
    client_id: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClientCredentials:
    """OAuth client bound to an agent. The secret is returned only once."""

    client_id: str
    client_secret: str
    agent_id: str
