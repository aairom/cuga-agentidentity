"""Shared fixtures. LLM-free: tests use the fake model so they are deterministic and offline."""

from __future__ import annotations

import os

os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["AI_LLM_MODE"] = "fake"

import pytest

from agent_identity.audit import AuditLog
from agent_identity.backend import MockVerifyBackend
from agent_identity.config import Settings
from agent_identity.lifecycle import AgentIdentityService
from agent_identity.runtime import APPROVER, DEVELOPER, Showcase


@pytest.fixture()
def backend() -> MockVerifyBackend:
    return MockVerifyBackend("https://issuer.test", token_ttl=300)


@pytest.fixture()
def service(backend: MockVerifyBackend) -> AgentIdentityService:
    return AgentIdentityService(backend, AuditLog())


@pytest.fixture()
def active_agent(service: AgentIdentityService):
    return service.onboard(DEVELOPER, APPROVER, "unit-agent", "d", {"a:read", "a:write"})


@pytest.fixture()
def showcase(tmp_path) -> Showcase:
    s = Settings(llm_mode="fake", state_dir=str(tmp_path / "state"), output_dir=str(tmp_path))
    return Showcase(settings=s)
