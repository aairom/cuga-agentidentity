"""Cuga integration tests using a fake chat model (no LLM calls)."""

from __future__ import annotations

import pytest

from agents import LEARNING_COACH, OPS_SRE, REVENUE_ANALYST, WORKER_SPECS


async def test_deploy_registers_and_builds_cuga_agent(showcase):
    rt = await showcase.deploy(LEARNING_COACH)
    assert rt.record.status.value == "ACTIVE" and rt.record.client_id
    policies = await rt.cuga.policies.list()
    by_name = {p["name"]: p["type"] for p in policies}
    assert by_name["No bulk record deletion"] == "intent_guard"
    assert by_name["Approve expensive enrolments"] == "tool_approval"
    events = {e.event for e in showcase.audit.events(agent_id=rt.record.agent_id)}
    assert {"agent.created", "agent.approved", "client.registered", "agent.deployed"} <= events
    await showcase.aclose()


async def test_deploy_all_agents(showcase):
    for spec in WORKER_SPECS.values():
        await showcase.deploy(spec)
    assert set(showcase.runtimes) == {"learning-coach", "revenue-analyst", "ops-sre-assistant"}
    await showcase.aclose()


@pytest.mark.parametrize("spec,prompt", [
    (LEARNING_COACH, "Please delete all employee records"),
    (REVENUE_ANALYST, "export all customer data"),
    (OPS_SRE, "kill all services now"),
])
async def test_intent_guard_blocks_without_llm(showcase, spec, prompt):
    await showcase.deploy(spec)
    turn = await showcase.chat(spec.name, prompt, "carol", showcase.new_thread())
    assert turn.error is None
    assert "Blocked by policy" in turn.answer
    assert any(d["outcome"] == "blocked" for d in turn.decisions)
    assert not turn.tool_calls
    await showcase.aclose()


async def test_chat_unknown_agent_returns_error(showcase):
    turn = await showcase.chat("ghost", "hi", "carol", "t1")
    assert "not deployed" in turn.error


async def test_chat_unknown_user_returns_error(showcase):
    await showcase.deploy(REVENUE_ANALYST)
    turn = await showcase.chat(REVENUE_ANALYST.name, "hi", "mallory", "t1")
    assert "Unknown user" in turn.error and not turn.answer
    await showcase.aclose()


async def test_policies_are_isolated_per_agent(showcase):
    """Regression: Cuga's default policy store is process-wide; each agent must get its own."""
    a = await showcase.deploy(LEARNING_COACH)
    b = await showcase.deploy(OPS_SRE)
    names_a = {p["name"] for p in await a.cuga.policies.list()}
    names_b = {p["name"] for p in await b.cuga.policies.list()}
    assert names_a == {"No bulk record deletion", "Approve expensive enrolments"}
    assert names_b == {"No shutdown of everything", "Approve service restarts"}
    await showcase.aclose()


async def test_supervisor_deploys_and_blocks_before_delegating(showcase):
    from agents import SUPERVISOR_SPEC

    workers = [await showcase.deploy(s) for s in WORKER_SPECS.values()]
    sup = await showcase.deploy_supervisor(SUPERVISOR_SPEC, workers)
    assert sup.is_supervisor and sup.record.status.value == "ACTIVE"
    assert len(showcase.backend.list_agents()) == 4
    await showcase.aclose()


async def test_supervisor_token_nests_into_worker_chain(showcase):
    """Worker tools invoked under a supervisor see act chain: worker -> supervisor."""
    from agent_identity.gateway import CallContext, reset_call_context, set_call_context
    from agent_identity.tools import build_course_tools
    from agents import SUPERVISOR_SPEC

    showcase.onboard(LEARNING_COACH)
    sup = showcase.onboard(SUPERVISOR_SPEC)
    user_tok = showcase.login("carol")
    sup_tok = showcase.service.delegated_token(sup.agent_id, user_tok, ["courses:read"], "token-exchange")
    h = set_call_context(CallContext("carol", user_tok, supervisor_token=sup_tok))
    try:
        tool = {t.name: t for t in build_course_tools(showcase.gateway, showcase.store, LEARNING_COACH.name)}["list_courses"]
        assert "AI201" in tool.invoke({})
    finally:
        reset_call_context(h)
    ev = [e for e in showcase.audit.events() if e.event == "access.granted"][-1]
    names = [showcase.backend.get_agent(a).name for a in ev.detail["chain"]]
    assert names == ["learning-coach", "workplace-supervisor"] and ev.user_id == "carol"
