"""Showcase runtime: wires identity, registry, Cuga agents and policies together."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .backend import HttpVerifyBackend, IdentityBackend, MockVerifyBackend
from .config import Settings, get_settings
from .data import USERS, Store
from .errors import BackendError, EngineSwitchError, LLMEngineError
from .gateway import CallContext, IdentityGateway, reset_call_context, set_call_context
from .lifecycle import AgentIdentityService
from .llm_engines import ENGINES, FAKE_ENGINE, EngineConfig, EngineManager, get_engine
from .logging_setup import get_logger
from .models import AgentRecord, Entitlement, Principal
from .spec import AgentSpec

log = get_logger("runtime")

DEVELOPER = Principal("alice.dev", "Alice (Agent Developer)", frozenset({Entitlement.READ_AGENTS, Entitlement.WRITE_AGENTS}))
APPROVER = Principal("bob.approver", "Bob (Approver)", frozenset({Entitlement.READ_AGENTS, Entitlement.MANAGE_AGENT_STATUS}))


def build_backend(settings: Settings) -> IdentityBackend:
    """Select the identity backend from configuration."""
    if settings.identity_backend == "verify":
        return HttpVerifyBackend(
            settings.verify_tenant_url,
            settings.verify_admin_client_id,
            settings.verify_admin_client_secret.get_secret_value(),
        )
    return MockVerifyBackend(settings.mock_issuer, settings.token_ttl_seconds)


def build_model(settings: Settings, engine: EngineConfig | None = None) -> Any:
    """Return the chat model for the selected LLM engine (a LangChain chat model).

    ``engine`` is the resolved configuration; when omitted it is derived from ``settings``
    (``llm_mode="fake"`` -> offline fake model, otherwise the engine resolution order of
    :class:`~agent_identity.llm_engines.EngineManager`).
    """
    if engine is None:
        engine = ENGINES[FAKE_ENGINE].auto_config(settings) if settings.llm_mode == "fake" \
            else EngineManager(settings).resolve().config
    impl = get_engine(engine.engine)
    impl.apply_env(engine)  # keep env vars Cuga reads in sync with the active engine
    if engine.engine != FAKE_ENGINE:
        os.environ.setdefault("LOGURU_LEVEL", "WARNING")
    return impl.build_model(engine, settings.llm_timeout)


@dataclass
class ChatTurn:
    """Result of one chat turn, in UI-friendly form."""

    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    needs_approval: bool = False
    approval_message: str = ""
    error: str | None = None


@dataclass
class AgentRuntime:
    """A deployed agent: registry record + Cuga instance."""

    spec: AgentSpec
    record: AgentRecord
    cuga: Any  # cuga.CugaAgent or cuga.CugaSupervisor
    is_supervisor: bool = False


def _decision_dicts(result: Any) -> list[dict[str, Any]]:
    out = []
    for d in getattr(result, "policy_decisions", []) or []:
        out.append({"outcome": getattr(getattr(d, "outcome", None), "value", str(getattr(d, "outcome", ""))),
                    "policy": getattr(d, "policy_name", None) or getattr(d, "name", None) or str(d)[:80]})
    return out


def _has_approval_prompt(result: Any) -> bool:
    outcomes = {x["outcome"] for x in _decision_dicts(result)}
    return "approval_required" in outcomes or "Tool approval required" in (getattr(result, "answer", "") or "")


class Showcase:
    """Facade used by the UI, the CLI and the tests."""

    def __init__(self, settings: Settings | None = None, backend: IdentityBackend | None = None,
                 store: Store | None = None, engine: EngineConfig | None = None) -> None:
        self.settings = settings or get_settings()
        # The active LLM engine. Resolved once here (fake in offline mode) and changed at runtime
        # with :meth:`switch_engine`. Agents are deployed with the model built from it.
        self.engine_manager = EngineManager(self.settings)
        self.engine: EngineConfig = engine or (
            ENGINES[FAKE_ENGINE].auto_config(self.settings) if self.settings.llm_mode == "fake"  # type: ignore[assignment]
            else self.engine_manager.resolve().config)
        self._model: Any = None
        # Switch gate: new turns wait while a switch is running; a switch waits for running turns.
        self._ready = asyncio.Event()
        self._ready.set()
        self._idle = asyncio.Event()
        self._idle.set()
        self._inflight = 0
        self._switch_lock = asyncio.Lock()
        self.audit = AuditLog()
        self.backend = backend or build_backend(self.settings)
        self.service = AgentIdentityService(self.backend, self.audit)
        self.store = store or Store()
        self.gateway = IdentityGateway(self.service, self.backend.issuer, self.backend.verification_key(),
                                       self.settings.strict_status_check)
        self.runtimes: dict[str, AgentRuntime] = {}
        self._user_tokens: dict[str, str] = {}

    # ---- onboarding / deployment ---------------------------------------------------
    def onboard(self, spec: AgentSpec) -> AgentRecord:
        """Create -> approve -> register client in the Agent Registry (idempotent)."""
        return self.service.onboard(DEVELOPER, APPROVER, spec.name, spec.description, spec.allowed_scopes)

    async def deploy(self, spec: AgentSpec) -> AgentRuntime:
        """Onboard the agent and build its Cuga runtime with tools + policies."""
        from cuga import CugaAgent  # lazy: importing cuga is slow

        record = self.onboard(spec)
        tools = spec.tool_builder(self.gateway, self.store, spec.name) if spec.tool_builder else []
        model = self._shared_model()
        from langgraph.checkpoint.memory import MemorySaver

        # We own the checkpointer so conversation / pending-approval state survives an engine switch.
        kwargs: dict[str, Any] = dict(tools=tools, special_instructions=spec.instructions, checkpointer=MemorySaver(),
                                      model=model,
                                      cuga_folder=os.path.join(self.settings.state_dir, spec.name),
                                      reset_policy_storage=True, filesystem_sync=False, auto_load_policies=False,
                                      execution_mode=self.settings.execution_mode)
        try:
            # Cuga's default policy store is shared per process, so every agent would see every
            # other agent's policies. Give each agent its own isolated policy collection.
            from cuga.backend.cuga_graph.policy.configurable import PolicyConfigurable

            policy_system = PolicyConfigurable()
            await policy_system.initialize(collection_name=f"policies_{spec.name.replace('-', '_')}")
            kwargs["policy_system"] = policy_system
            agent = CugaAgent(**kwargs)
        except Exception as exc:
            log.exception("Cuga agent construction failed for %s", spec.name)
            raise BackendError(f"Could not build Cuga agent '{spec.name}': {exc}") from exc
        agent.description = spec.description
        for g in spec.guards:
            await agent.policies.add_intent_guard(name=g.name, keywords=list(g.keywords), response=g.response)
        for a in spec.approvals:
            await agent.policies.add_tool_approval(name=a.name, required_tools=list(a.tools), approval_message=a.message)
        rt = AgentRuntime(spec, record, agent)
        self.runtimes[spec.name] = rt
        self.audit.record("agent.deployed", plane="management", agent_id=record.agent_id, actor=DEVELOPER.user_id,
                          detail={"name": spec.name, "tools": [t.name for t in tools],
                                  "guards": len(spec.guards), "approvals": len(spec.approvals)})
        log.info("deployed %s (%s)", spec.name, record.agent_id)
        return rt

    async def deploy_supervisor(self, supervisor_spec: AgentSpec, workers: list[AgentRuntime]) -> AgentRuntime:
        """Onboard a supervisor identity and wrap the already-deployed workers in a CugaSupervisor."""
        from cuga import CugaSupervisor
        from langgraph.checkpoint.memory import MemorySaver

        record = self.onboard(supervisor_spec)
        kwargs: dict[str, Any] = {"agents": {w.spec.name.replace("-", "_"): w.cuga for w in workers},
                                  "description": supervisor_spec.description, "model": self._shared_model(),
                                  "checkpointer": MemorySaver()}
        try:
            sup = CugaSupervisor(**kwargs)
        except Exception as exc:
            log.exception("Cuga supervisor construction failed")
            raise BackendError(f"Could not build Cuga supervisor: {exc}") from exc
        rt = AgentRuntime(supervisor_spec, record, sup)
        rt.is_supervisor = True
        self.runtimes[supervisor_spec.name] = rt
        self.audit.record("agent.deployed", plane="management", agent_id=record.agent_id, actor=DEVELOPER.user_id,
                          detail={"name": supervisor_spec.name, "workers": [w.spec.name for w in workers]})
        return rt

    # ---- LLM engine -----------------------------------------------------------------
    def _shared_model(self) -> Any:
        """One chat-model object shared by every agent (built lazily from the active engine)."""
        if self._model is None:
            self._model = build_model(self.settings, self.engine)
        return self._model

    async def switch_engine(self, new: EngineConfig, drain_timeout: float = 60.0, test_timeout: float = 5.0) -> EngineConfig:
        """Switch every deployed agent to ``new`` without restarting.

        1. The connection is tested first; a failing engine is never activated.
        2. New chat turns wait, running turns finish (up to ``drain_timeout`` seconds).
        3. The model is swapped and each agent's graph is rebuilt lazily on its next turn.
           Checkpointers, the registry, tokens, store and audit log are untouched.
        4. Any failure restores the previous engine and raises :class:`EngineSwitchError`.
        """
        old_cfg = self.engine
        async with self._switch_lock:
            result = await asyncio.to_thread(self.engine_manager.test, new, test_timeout)
            if not result.ok:
                self.audit.record("llm.engine_switch_failed", plane="management", outcome="denied", actor=DEVELOPER.user_id,
                                  detail={"to": new.public_dict(), "reason": result.message})
                raise EngineSwitchError(f"Not switching to {new.engine}: {result.message}", result.remediation)
            if not new.model and result.resolved_model:
                new = new.with_updates(model=result.resolved_model)
            self._ready.clear()  # hold new turns
            old_model = self._model
            try:
                try:
                    await asyncio.wait_for(self._idle.wait(), drain_timeout)
                except asyncio.TimeoutError as exc:
                    raise EngineSwitchError(f"{self._inflight} request(s) are still running after {drain_timeout:g}s.",
                                            "Wait for them to finish and try again, or raise the drain timeout.") from exc
                try:
                    model = build_model(self.settings, new)
                    self._apply_model(model)
                except LLMEngineError:
                    self._apply_model(old_model)
                    raise
                except Exception as exc:
                    self._apply_model(old_model)
                    raise EngineSwitchError(f"Could not activate {new.engine}: {exc}",
                                            "The previous engine was restored; check the logs for details.") from exc
                self._model, self.engine = model, new
                self.audit.record("llm.engine_switched", plane="management", actor=DEVELOPER.user_id,
                                  detail={"from": old_cfg.public_dict(), "to": new.public_dict(),
                                          "latency_ms": result.latency_ms})
                log.info("LLM engine switched: %s -> %s", old_cfg.describe(), new.describe())
                return new
            except LLMEngineError as exc:
                self.engine, self._model = old_cfg, old_model
                try:
                    get_engine(old_cfg.engine).apply_env(old_cfg)
                except LLMEngineError:  # pragma: no cover - best effort
                    pass
                self.audit.record("llm.engine_switch_failed", plane="management", outcome="error", actor=DEVELOPER.user_id,
                                  detail={"to": new.public_dict(), "reason": exc.message})
                log.error("engine switch failed, kept %s: %s", old_cfg.describe(), exc)
                if isinstance(exc, EngineSwitchError):
                    raise
                raise EngineSwitchError(exc.message, exc.remediation) from exc
            finally:
                self._ready.set()

    def _apply_model(self, model: Any) -> None:
        """Point every agent at ``model`` and force its graph to be rebuilt on the next turn."""
        if model is None:
            return
        for rt in self.runtimes.values():
            for attr in ("_model", "_graph", "_compiled_graph"):
                if not hasattr(rt.cuga, attr):  # private Cuga attributes: fail loudly if a Cuga upgrade renames them
                    raise EngineSwitchError(f"Unsupported Cuga version: '{attr}' not found on {rt.spec.name}.",
                                            "Pin cuga==0.4.5 (see requirements.txt) or update agent_identity/runtime.py.")
            rt.cuga._model = model
            rt.cuga._graph = None
            rt.cuga._compiled_graph = None

    # ---- end-user session --------------------------------------------------------
    def login(self, user_id: str) -> str:
        """Mock login: returns (and caches) a user access token with the user's entitlements."""
        if user_id not in USERS:
            raise BackendError(f"Unknown user {user_id}")
        token = self.service.user_login(user_id, list(USERS[user_id]["scopes"]))  # type: ignore[arg-type]
        self._user_tokens[user_id] = token
        self.audit.record("user.login", user_id=user_id, actor=user_id)
        return token

    # ---- chat ---------------------------------------------------------------------
    async def chat(self, agent_name: str, message: str | None, user_id: str, thread_id: str,
                   approve: bool | None = None) -> ChatTurn:
        """Send a message (or resume after an approval decision) to a deployed agent.

        While an engine switch is running the turn waits (it then uses the new engine); a switch
        waits for turns already running, so no request ever straddles two engines.
        """
        await self._ready.wait()
        self._inflight += 1
        self._idle.clear()
        try:
            return await self._chat(agent_name, message, user_id, thread_id, approve)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def _chat(self, agent_name: str, message: str | None, user_id: str, thread_id: str,
                    approve: bool | None = None) -> ChatTurn:
        rt = self.runtimes.get(agent_name)
        if rt is None:
            return ChatTurn(answer="", error=f"Agent '{agent_name}' is not deployed")
        try:
            user_token = self.login(user_id)  # fresh token per turn: cached ones expire (default TTL 300 s)
        except BackendError as exc:
            return ChatTurn(answer="", error=str(exc))
        supervisor_token = None
        if rt.is_supervisor:
            # The supervisor acts on behalf of the user (sub=user, act.sub=supervisor). Its token becomes the
            # *subject* of each worker's exchange, so the worker token nests: act(worker) -> act(supervisor).
            try:
                allowed = set(USERS[user_id]["scopes"]) & set(rt.spec.allowed_scopes)  # type: ignore[arg-type]
                supervisor_token = self.service.delegated_token(rt.record.agent_id, user_token, sorted(allowed),
                                                                "token-exchange")
            except Exception as exc:
                return ChatTurn(answer="", error=f"{type(exc).__name__}: {exc}")
        ctx_token = set_call_context(CallContext(user_id=user_id, user_token=user_token,
                                                 supervisor_token=supervisor_token))
        try:
            kwargs: dict[str, Any] = {"thread_id": thread_id, "track_tool_calls": True}
            if approve is not None:
                from cuga.backend.cuga_graph.nodes.human_in_the_loop.followup_model import ActionResponse, ActionType

                kwargs["action_response"] = ActionResponse(
                    action_id="tool_approval", response_type=ActionType.CONFIRMATION, confirmed=approve,
                    timestamp=datetime.now(timezone.utc).isoformat(), user_id=user_id, session_id=thread_id)
                self.audit.record("hitl.decision", agent_id=rt.record.agent_id, actor=user_id,
                                  outcome="success" if approve else "denied", detail={"approved": approve})
                message = None
            if rt.is_supervisor:
                kwargs.pop("track_tool_calls", None)
                kwargs["user_id"] = user_id
            result = await rt.cuga.invoke(message, **kwargs)
        except Exception as exc:  # LLM down, network, etc.
            log.exception("chat failed")
            return ChatTurn(answer="", error=f"{type(exc).__name__}: {exc}")
        finally:
            reset_call_context(ctx_token)
        # After an explicit approve/deny decision the turn is final: never ask again for the same action.
        pending = approve is None and _has_approval_prompt(result)
        return ChatTurn(answer=result.answer or "", tool_calls=list(result.tool_calls or []),
                        decisions=_decision_dicts(result), needs_approval=pending,
                        approval_message=result.answer if pending else "", error=result.error)

    async def aclose(self) -> None:
        for rt in self.runtimes.values():
            try:
                await rt.cuga.aclose()
            except Exception:  # pragma: no cover - best effort
                log.debug("close failed for %s", rt.spec.name)

    @staticmethod
    def new_thread() -> str:
        return f"thread-{uuid.uuid4().hex[:8]}"
