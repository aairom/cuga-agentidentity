"""Audit trail.

IBM Agent Identity correlates management-plane and runtime-plane events through
the Agent ID (``data.entity_id``). This in-memory log mimics that: every event
carries ``agent_id`` so the whole life of an agent can be queried in one place.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("audit")


@dataclass(frozen=True)
class AuditEvent:
    """A single audit record."""

    event: str  # e.g. agent.created, token.issued, access.denied
    plane: str  # "management" | "runtime"
    outcome: str  # "success" | "denied" | "error"
    agent_id: str | None = None
    actor: str | None = None  # human principal or agent doing the action
    user_id: str | None = None  # end user on whose behalf (OBO)
    detail: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))


class AuditLog:
    """Thread-safe append-only audit log with JSONL export."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(self, event: str, *, plane: str = "runtime", outcome: str = "success", **kwargs: Any) -> AuditEvent:
        detail = kwargs.pop("detail", {}) or {}
        ev = AuditEvent(event=event, plane=plane, outcome=outcome, detail=detail, **kwargs)
        with self._lock:
            self._events.append(ev)
        log.info("audit %s plane=%s outcome=%s agent=%s actor=%s", event, plane, outcome, ev.agent_id, ev.actor)
        return ev

    def events(self, agent_id: str | None = None, outcome: str | None = None) -> list[AuditEvent]:
        with self._lock:
            items = list(self._events)
        if agent_id:
            items = [e for e in items if e.agent_id == agent_id]
        if outcome:
            items = [e for e in items if e.outcome == outcome]
        return items

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def export_jsonl(self, path: Path) -> Path:
        """Write all events as JSON lines and return the path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for ev in self.events():
                fh.write(json.dumps(asdict(ev)) + "\n")
        return path
