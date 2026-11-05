"""Showcase runtime: wires identity, registry, Cuga agents and policies together."""

from __future__ import annotations

import itertools
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .audit import AuditLog
from .backend import HttpVerifyBackend, IdentityBackend, MockVerifyBackend
from .config import Settings, apply_llm_env, get_settings
from .data import USERS, Store
from .errors import BackendError
from .gateway import CallContext, IdentityGateway, reset_call_context, set_call_context
from .lifecycle import AgentIdentityService
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


def build_model(settings: Settings) -> Any:
    """Return the chat model: real (Ollama / OpenAI-compatible, picked up by Cuga) or a fake."""
    if settings.llm_mode == "fake":
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        os.environ.setdefault("OPENAI_API_KEY", "fake")  # Cuga builds an LLM client eagerly
        return GenericFakeChatModel(messages=itertools.repeat(AIMessage(content="(fake model: no LLM configured)")))
    apply_llm_env()
    return None  # Cuga resolves the model from env/settings


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
                 store: Store | None = None) -> None:
        self.settings = settings or get_settings()
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
        model = build_model(self.settings)
        kwargs: dict[str, Any] = dict(tools=tools, special_instructions=spec.instructions,
                                      cuga_folder=os.path.join(self.settings.state_dir, spec.name),
                                      reset_policy_storage=True, filesystem_sync=False, auto_load_policies=False,
                                      execution_mode=self.settings.execution_mode)
        if model is not None:
            kwargs["model"] = model
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

        record = self.onboard(supervisor_spec)
        kwargs: dict[str, Any] = {"agents": {w.spec.name.replace("-", "_"): w.cuga for w in workers},
                                  "description": supervisor_spec.description}
        model = build_model(self.settings)
        if model is not None:
            kwargs["model"] = model
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
        """Send a message (or resume after an approval decision) to a deployed agent."""
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
