"""Deterministic conformance scenarios for the identity controls (no LLM needed).

Each scenario exercises the *real* guarded tools / policies and asserts the
expected allow/deny outcome. A failing scenario means an identity control regressed.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from agent_identity.gateway import CallContext, reset_call_context, set_call_context
from agent_identity.logging_setup import get_logger
from agent_identity.models import AgentStatus
from agent_identity.runtime import APPROVER, Showcase
from agent_identity.tools import build_course_tools, build_crm_tools, build_ops_tools
from agents import LEARNING_COACH, OPS_SRE, REVENUE_ANALYST, SUPERVISOR_SPEC, WORKER_SPECS

log = get_logger("harness.scenarios")


@dataclass
class ScenarioResult:
    name: str
    description: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0


@dataclass
class SuiteResult:
    results: list[ScenarioResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "total": len(self.results),
                "failed": [r.name for r in self.results if not r.passed],
                "results": [asdict(r) for r in self.results]}

    def to_markdown(self) -> str:
        lines = ["# Identity conformance report", "",
                 f"**Result:** {'PASS' if self.passed else 'FAIL'} "
                 f"({sum(r.passed for r in self.results)}/{len(self.results)})", "",
                 "| Scenario | Result | Detail |", "|---|---|---|"]
        for r in self.results:
            lines.append(f"| {r.name} | {'PASS' if r.passed else 'FAIL'} | {r.detail or r.description} |")
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


Scenario = Callable[[Showcase], Awaitable[str]]
_REGISTRY: list[tuple[str, str, Scenario]] = []


def scenario(name: str, description: str):
    def deco(fn: Scenario) -> Scenario:
        _REGISTRY.append((name, description, fn))
        return fn
    return deco


def _tools(sc: Showcase) -> dict[str, dict]:
    for spec in WORKER_SPECS.values():
        sc.onboard(spec)
    gw, st = sc.gateway, sc.store
    return {"course": {t.name: t for t in build_course_tools(gw, st, LEARNING_COACH.name)},
            "crm": {t.name: t for t in build_crm_tools(gw, st, REVENUE_ANALYST.name)},
            "ops": {t.name: t for t in build_ops_tools(gw, st, OPS_SRE.name)}}


def _as(sc: Showcase, user: str):
    return set_call_context(CallContext(user, sc.login(user)))


def _expect(cond: bool, msg: str) -> str:
    if not cond:
        raise AssertionError(msg)
    return msg


@scenario("registry.lifecycle", "Agents are ACTIVE with a bound OAuth client after approval")
async def _lifecycle(sc: Showcase) -> str:
    _tools(sc)
    recs = sc.backend.list_agents()
    _expect(len(recs) == 3 and all(r.status is AgentStatus.ACTIVE and r.client_id for r in recs), "agents not ACTIVE")
    return "3 agents ACTIVE with registered clients"


@scenario("obo.allow", "Learning Coach enrols the user on their behalf")
async def _obo_allow(sc: Showcase) -> str:
    t = _tools(sc)
    h = _as(sc, "carol")
    try:
        out = t["course"]["enroll_course"].invoke({"course_id": "PY101"})
    finally:
        reset_call_context(h)
    _expect("Enrolled carol" in out, out)
    return out


@scenario("obo.no_user_context", "On-behalf-of access without a logged-in user is denied")
async def _obo_no_user(sc: Showcase) -> str:
    out = _tools(sc)["course"]["list_courses"].invoke({})
    _expect("DelegationError" in out, out)
    return "denied: DelegationError"


@scenario("scope.user_ceiling", "Agent cannot exceed the end user's entitlements (Carol lacks ops:restart)")
async def _user_ceiling(sc: Showcase) -> str:
    t = _tools(sc)
    h = _as(sc, "carol")
    try:
        out = t["ops"]["restart_service"].invoke({"service": "reports"})
    finally:
        reset_call_context(h)
    _expect("InsufficientScope" in out and sc.store.services["reports"] == "degraded", out)
    return "denied: InsufficientScope; service unchanged"


@scenario("scope.agent_ceiling", "Read-only agent cannot write even though a write tool exists")
async def _agent_ceiling(sc: Showcase) -> str:
    out = _tools(sc)["crm"]["update_account_tier"].invoke({"account_id": "A-3", "tier": "gold"})
    _expect("InsufficientScope" in out and sc.store.accounts[2]["tier"] == "bronze", out)
    return "denied: InsufficientScope; data unchanged"


@scenario("privileged.token", "Privileged action uses a short-lived token with authorization_details")
async def _privileged(sc: Showcase) -> str:
    t = _tools(sc)
    h = _as(sc, "dave")
    try:
        out = t["ops"]["restart_service"].invoke({"service": "reports"})
    finally:
        reset_call_context(h)
    _expect("restarted" in out and sc.store.services["reports"] == "running", out)
    return "restart succeeded with privileged token"


@scenario("kill_switch", "Suspending an agent blocks its tools immediately")
async def _kill_switch(sc: Showcase) -> str:
    t = _tools(sc)
    rec = sc.backend.find_agent(LEARNING_COACH.name)
    sc.service.suspend_agent(APPROVER, rec.agent_id, "conformance")
    h = _as(sc, "carol")
    try:
        out = t["course"]["list_courses"].invoke({})
    finally:
        reset_call_context(h)
    _expect("AgentNotActive" in out, out)
    return "denied: AgentNotActive"


@scenario("audit.correlation", "Allow and deny decisions are in the audit log keyed by Agent ID")
async def _audit(sc: Showcase) -> str:
    t = _tools(sc)
    h = _as(sc, "carol")
    try:
        t["course"]["list_courses"].invoke({})
    finally:
        reset_call_context(h)
    t["crm"]["update_account_tier"].invoke({"account_id": "A-1", "tier": "x"})
    aid = sc.backend.find_agent(LEARNING_COACH.name).agent_id
    _expect(any(e.event == "access.granted" and e.agent_id == aid for e in sc.audit.events()), "no grant event")
    _expect(bool(sc.audit.events(outcome="denied")), "no deny event")
    return f"{len(sc.audit.events())} events recorded"


@scenario("cuga.policies", "Cuga intent guards block risky requests before any tool or LLM runs")
async def _policies(sc: Showcase) -> str:
    await sc.deploy(LEARNING_COACH)
    turn = await sc.chat(LEARNING_COACH.name, "delete all employee records", "carol", sc.new_thread())
    _expect("Blocked by policy" in turn.answer and not turn.tool_calls, turn.answer)
    return "blocked by Cuga intent guard"


@scenario("supervisor.chain", "Supervisor delegation nests the actor chain in the token")
async def _chain(sc: Showcase) -> str:
    t = _tools(sc)
    sup = sc.onboard(SUPERVISOR_SPEC)
    user = sc.login("carol")
    sup_tok = sc.service.delegated_token(sup.agent_id, user, ["courses:read"], "token-exchange")
    h = set_call_context(CallContext("carol", user, supervisor_token=sup_tok))
    try:
        t["course"]["list_courses"].invoke({})
    finally:
        reset_call_context(h)
    ev = [e for e in sc.audit.events() if e.event == "access.granted"][-1]
    names = [sc.backend.get_agent(a).name for a in ev.detail["chain"]]
    _expect(names == [LEARNING_COACH.name, SUPERVISOR_SPEC.name], str(names))
    return " <- ".join(names) + " (user=carol)"


async def run_suite(make_showcase: Callable[[], Showcase]) -> SuiteResult:
    """Run every scenario against a fresh :class:`Showcase` and collect results."""
    suite = SuiteResult()
    for name, desc, fn in _REGISTRY:
        sc = make_showcase()
        start = time.perf_counter()
        try:
            detail = await fn(sc)
            res = ScenarioResult(name, desc, True, detail)
        except Exception as exc:  # a scenario failing must not stop the suite
            log.warning("scenario %s failed: %s", name, exc)
            res = ScenarioResult(name, desc, False, f"{type(exc).__name__}: {exc}")
        finally:
            await sc.aclose()
        res.duration_ms = int((time.perf_counter() - start) * 1000)
        suite.results.append(res)
    return suite
