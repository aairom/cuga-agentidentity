"""Tests for bootstrap.deploy_all, the stale-approval fix and the demo CLI."""

from __future__ import annotations

from types import SimpleNamespace

import demo
from agent_identity.bootstrap import deploy_all
from agent_identity.config import Settings
from agent_identity.runtime import AgentRuntime


async def test_deploy_all_deploys_workers_and_supervisor(showcase):
    runtimes = await deploy_all(showcase)
    assert {"learning-coach", "revenue-analyst", "ops-sre-assistant"} <= set(runtimes)
    assert len(runtimes) == 4
    assert any(rt.is_supervisor for rt in runtimes.values())
    assert all(rt.record.status.value == "ACTIVE" for rt in runtimes.values())
    await showcase.aclose()


class _FakeCuga:
    """Always answers with an approval prompt, like a Cuga run that is waiting for the user."""

    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    async def invoke(self, message, **kwargs):
        self.kwargs.append(kwargs)
        return SimpleNamespace(answer="Tool approval required", tool_calls=[], policy_decisions=[], error=None)


async def test_no_stale_approval_prompt_after_decision(showcase):
    rt = await showcase.deploy(__import__("agents").LEARNING_COACH)
    fake = _FakeCuga()
    showcase.runtimes[rt.spec.name] = AgentRuntime(rt.spec, rt.record, fake)
    thread = showcase.new_thread()

    first = await showcase.chat(rt.spec.name, "Enroll me in SEC301", "carol", thread)
    assert first.needs_approval and first.approval_message

    decided = await showcase.chat(rt.spec.name, None, "carol", thread, approve=True)
    assert not decided.needs_approval and decided.approval_message == ""
    assert "action_response" in fake.kwargs[-1]

    denied = await showcase.chat(rt.spec.name, None, "carol", thread, approve=False)
    assert not denied.needs_approval
    outcomes = [e.outcome for e in showcase.audit.events() if e.event == "hitl.decision"]
    assert outcomes == ["success", "denied"]


async def test_chat_unknown_agent_and_user(showcase):
    assert "not deployed" in (await showcase.chat("ghost", "hi", "carol", "t")).error
    await showcase.deploy(__import__("agents").LEARNING_COACH)
    assert "Unknown user" in (await showcase.chat("learning-coach", "hi", "mallory", "t")).error


def test_demo_main_runs_offline(tmp_path, monkeypatch):
    s = Settings(llm_mode="fake", state_dir=str(tmp_path / "state"), output_dir=str(tmp_path / "out"))
    monkeypatch.setattr(demo, "get_settings", lambda: s)
    assert demo.main([]) == 0
    reports = list((tmp_path / "out").glob("demo_*.md"))
    assert len(reports) == 1
    assert "Agent Identity demo" in reports[0].read_text(encoding="utf-8")
