"""Runtime engine switching: swap, rollback, in-flight draining and state preservation (no network)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from agent_identity import llm_engines as le
from agent_identity.bootstrap import deploy_all
from agent_identity.config import Settings
from agent_identity.errors import EngineConnectionError, EngineSwitchError
from agent_identity.llm_engines import EngineConfig
from agent_identity.runtime import Showcase, build_model


@pytest.fixture()
def sc(tmp_path, monkeypatch):
    monkeypatch.setattr(le, "_ORIGINAL_ENV", {})
    s = Settings(_env_file=None, llm_mode="fake", state_dir=str(tmp_path / "state"), output_dir=str(tmp_path))
    return Showcase(settings=s)


def ollama_ok(monkeypatch, models=("granite4.2:latest",)):
    monkeypatch.setattr(le, "_http_get", lambda url, headers=None, timeout=5.0: (200, {"models": [{"name": m} for m in models]}))


def llama_cfg(): return EngineConfig("llamacpp", "http://localhost:8080", "m", source="ui")
def ollama_cfg(): return EngineConfig("ollama", "http://localhost:11434", "granite4.2:latest", source="ui")


def test_fake_mode_starts_with_fake_engine(sc):
    assert sc.engine.engine == "fake" and sc.engine.source == "fake"
    assert build_model(sc.settings).__class__.__name__ == "GenericFakeChatModel"


def test_build_model_for_real_engine():
    s = Settings(_env_file=None, llm_mode="ollama")
    m = build_model(s, ollama_cfg())
    assert m.model_name == "granite4.2:latest" and str(m.openai_api_base).endswith("/v1")


async def test_switch_replaces_model_and_resets_graphs(sc, monkeypatch):
    await deploy_all(sc)
    first = sc._model
    assert all(rt.cuga._model is first for rt in sc.runtimes.values())  # one shared model
    ollama_ok(monkeypatch)
    new = await sc.switch_engine(ollama_cfg())
    assert sc.engine.engine == "ollama" and new.model == "granite4.2:latest" and sc._model is not first
    for rt in sc.runtimes.values():
        assert rt.cuga._model is sc._model and rt.cuga._graph is None and rt.cuga._compiled_graph is None
    ev = [e for e in sc.audit.events() if e.event == "llm.engine_switched"]
    assert ev and ev[0].detail["from"]["engine"] == "fake" and ev[0].detail["to"]["engine"] == "ollama"
    assert "api_key" in ev[0].detail["to"] and ev[0].detail["to"]["api_key"] in ("(set)", "(not set)")


async def test_failed_test_keeps_previous_engine(sc, monkeypatch):
    await deploy_all(sc)
    before, model = sc.engine, sc._model

    def down(url, headers=None, timeout=5.0):
        raise EngineConnectionError(f"Cannot connect to {url}", "x")
    monkeypatch.setattr(le, "_http_get", down)
    with pytest.raises(EngineSwitchError) as e:
        await sc.switch_engine(llama_cfg())
    assert "Not switching to llamacpp" in e.value.message and "llama-server" in e.value.remediation
    assert sc.engine is before and sc._model is model
    assert all(rt.cuga._model is model for rt in sc.runtimes.values())
    failed = [x for x in sc.audit.events() if x.event == "llm.engine_switch_failed"]
    assert failed and failed[0].outcome == "denied"


async def test_build_failure_rolls_back(sc, monkeypatch):
    await deploy_all(sc)
    before, model = sc.engine, sc._model
    ollama_ok(monkeypatch)
    monkeypatch.setattr("agent_identity.runtime.build_model", lambda settings, engine=None: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(EngineSwitchError) as e:
        await sc.switch_engine(ollama_cfg())
    assert "boom" in e.value.message and "previous engine was restored" in e.value.remediation
    assert sc.engine is before and sc._model is model
    assert all(rt.cuga._model is model for rt in sc.runtimes.values())
    assert sc._ready.is_set()  # the gate is always reopened


async def test_inflight_turn_finishes_before_swap_and_new_turns_wait(sc, monkeypatch):
    await deploy_all(sc)
    ollama_ok(monkeypatch)
    release, started, seen = asyncio.Event(), asyncio.Event(), []

    async def slow_chat(self, agent, message, user, thread, approve=None):
        seen.append(("start", self.engine.engine))
        started.set()
        await release.wait()
        seen.append(("end", self.engine.engine))
        from agent_identity.runtime import ChatTurn
        return ChatTurn(answer="ok")

    monkeypatch.setattr(Showcase, "_chat", slow_chat)
    turn = asyncio.create_task(sc.chat("learning-coach", "hi", "carol", "t1"))
    await started.wait()
    switch = asyncio.create_task(sc.switch_engine(ollama_cfg()))
    await asyncio.sleep(0.1)
    assert not switch.done() and sc.engine.engine == "fake"  # waiting for the running turn
    late = asyncio.create_task(sc.chat("learning-coach", "later", "carol", "t2"))
    await asyncio.sleep(0.05)
    assert len(seen) == 1  # the new turn is held back while the switch is pending
    release.set()
    assert (await turn).answer == "ok"
    await switch
    assert (await late).answer == "ok"
    assert seen[1] == ("end", "fake")  # the in-flight turn ended on the OLD engine
    assert seen[2] == ("start", "ollama")  # the held turn started on the NEW engine


async def test_drain_timeout_aborts_switch(sc, monkeypatch):
    await deploy_all(sc)
    ollama_ok(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def stuck(self, *a, **k):
        started.set()
        await release.wait()

    monkeypatch.setattr(Showcase, "_chat", stuck)
    turn = asyncio.create_task(sc.chat("learning-coach", "hi", "carol", "t"))
    await started.wait()
    with pytest.raises(EngineSwitchError) as e:
        await sc.switch_engine(ollama_cfg(), drain_timeout=0.1)
    assert "still running" in e.value.message
    assert sc.engine.engine == "fake" and sc._ready.is_set()
    release.set()
    await turn


async def test_state_survives_switch(sc, monkeypatch):
    await deploy_all(sc)
    ids = {r.name: r.agent_id for r in sc.backend.list_agents()}
    audit_before = len(sc.audit.events())
    checkpointers = {n: rt.cuga._checkpointer for n, rt in sc.runtimes.items()}
    ollama_ok(monkeypatch)
    await sc.switch_engine(ollama_cfg())
    assert {r.name: r.agent_id for r in sc.backend.list_agents()} == ids
    assert len(sc.audit.events()) == audit_before + 1
    assert {n: rt.cuga._checkpointer for n, rt in sc.runtimes.items()} == checkpointers
    assert all(cp is not None for cp in checkpointers.values())


async def test_switch_back_and_forth_and_secret_not_in_audit(sc, monkeypatch):
    await deploy_all(sc)
    monkeypatch.setattr(le, "_http_get", lambda url, headers=None, timeout=5.0: (200, {"data": [{"id": "m"}]}))
    cloud = EngineConfig("cloud", "https://api.example.com/v1", "m", SecretStr("sk-very-secret"), "ui")  # pragma: allowlist secret
    await sc.switch_engine(cloud)
    assert sc.engine.engine == "cloud"
    ollama_ok(monkeypatch)
    await sc.switch_engine(ollama_cfg())
    assert "sk-very-secret" not in str([e.detail for e in sc.audit.events()])  # pragma: allowlist secret


async def test_chat_still_works_after_switch(sc, monkeypatch):
    """The graph is rebuilt lazily: an intent guard (answered without the LLM) must still fire after a switch."""
    await deploy_all(sc)
    guard = next(g for g in sc.runtimes["learning-coach"].spec.guards)
    before = await sc.chat("learning-coach", f"please {guard.keywords[0]} now", "carol", "t1")
    assert before.error is None and guard.response in before.answer
    await sc.switch_engine(sc.engine.with_updates())  # fake -> fake: exercises the rebuild path offline
    assert sc.runtimes["learning-coach"].cuga._graph is None  # really reset
    after = await sc.chat("learning-coach", f"please {guard.keywords[0]} now", "carol", "t2")
    assert after.error is None and guard.response in after.answer
    assert sc.runtimes["learning-coach"].cuga._graph is not None  # rebuilt on demand
