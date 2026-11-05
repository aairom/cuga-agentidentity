"""Declarative description of an example agent.

An :class:`AgentSpec` is everything that is needed to (1) onboard the agent in
the Agent Registry, (2) build its Cuga runtime and (3) document it in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .tools import ToolBuilder


@dataclass(frozen=True)
class IntentGuardSpec:
    name: str
    keywords: tuple[str, ...]
    response: str


@dataclass(frozen=True)
class ApprovalSpec:
    name: str
    tools: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class AgentSpec:
    """Static definition of an example agent."""

    name: str  # registry name (unique)
    title: str
    description: str
    use_case: str
    access_mode: Literal["obo", "autonomous"]
    allowed_scopes: frozenset[str]  # ceiling configured in the registry
    tool_builder: ToolBuilder | None
    instructions: str
    guards: tuple[IntentGuardSpec, ...] = field(default_factory=tuple)
    approvals: tuple[ApprovalSpec, ...] = field(default_factory=tuple)
    sample_prompts: tuple[str, ...] = field(default_factory=tuple)
