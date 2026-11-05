"""CLI demo: walks through the Agent Identity flows end to end.

    python demo.py            # deterministic, no LLM needed (calls the agents' tools directly)
    python demo.py --live     # additionally chats with the agents through Cuga + local Ollama

Results are also written to ``output/demo_<timestamp>.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from agent_identity.bootstrap import deploy_all
from agent_identity.config import get_settings
from agent_identity.gateway import CallContext, reset_call_context, set_call_context
from agent_identity.logging_setup import setup_logging
from agent_identity.runtime import APPROVER, Showcase
from agent_identity.tokens import decode_unverified
from agent_identity.ui_helpers import explain_token, inspect_token
from agents import LEARNING_COACH, OPS_SRE, REVENUE_ANALYST

REPORT: list[str] = []


def say(text: str = "") -> None:
    """Print and remember a line for the markdown report."""
    print(text)
    REPORT.append(text)


def section(title: str) -> None:
    say(f"\n## {title}")


def call_tool(sc: Showcase, agent: str, tool_name: str, args: dict, user: str | None) -> str:
    """Invoke one agent tool as ``user`` (None = no end-user context)."""
    builders = {LEARNING_COACH.name: LEARNING_COACH, REVENUE_ANALYST.name: REVENUE_ANALYST, OPS_SRE.name: OPS_SRE}
    tools = {t.name: t for t in builders[agent].tool_builder(sc.gateway, sc.store, agent)}  # type: ignore[misc]
    handle = set_call_context(CallContext(user, sc.login(user))) if user else None
    try:
        return tools[tool_name].invoke(args)
    finally:
        if handle is not None:
            reset_call_context(handle)


async def run_deterministic(sc: Showcase) -> None:
    section("1. Onboarding: create -> approve -> register client")
    for spec in (LEARNING_COACH, REVENUE_ANALYST, OPS_SRE):
        rec = sc.onboard(spec)
        say(f"- {rec.name}: {rec.status.value}, agent_id={rec.agent_id}, approved_by={rec.approved_by}")

    section("2. On-behalf-of (Carol enrols herself through the Learning Coach)")
    say("  " + call_tool(sc, LEARNING_COACH.name, "enroll_course", {"course_id": "PY101"}, "carol"))

    section("3. Autonomous (Revenue Analyst reads CRM as itself)")
    say("  " + call_tool(sc, REVENUE_ANALYST.name, "revenue_summary", {}, None)[:200])

    section("4. Denials")
    say("  agent ceiling : " + call_tool(sc, REVENUE_ANALYST.name, "update_account_tier",
                                         {"account_id": "A-3", "tier": "gold"}, None))
    say("  user ceiling  : " + call_tool(sc, OPS_SRE.name, "restart_service", {"service": "reports"}, "carol"))
    say("  no user       : " + call_tool(sc, LEARNING_COACH.name, "list_courses", {}, None))

    section("5. Privileged token (Dave restarts a service)")
    say("  " + call_tool(sc, OPS_SRE.name, "restart_service", {"service": "reports"}, "dave"))

    section("6. Token inspector (Carol -> Learning Coach exchange)")
    rec = sc.backend.find_agent(LEARNING_COACH.name)
    tok = sc.service.delegated_token(rec.agent_id, sc.login("carol"), ["courses:read"], "hr-api")
    for line in explain_token(inspect_token(tok)):
        say(f"- {line}")
    say(f"  raw claims: {sorted(decode_unverified(tok))}")

    section("7. Kill switch")
    sc.service.suspend_agent(APPROVER, rec.agent_id, "demo")
    say("  after suspend : " + call_tool(sc, LEARNING_COACH.name, "list_courses", {}, "carol"))
    sc.service.reactivate_agent(APPROVER, rec.agent_id)
    say("  after reactivate: " + call_tool(sc, LEARNING_COACH.name, "list_courses", {}, "carol")[:80] + "...")

    section("8. Audit trail")
    for e in sc.audit.events()[-8:]:
        say(f"- {e.ts[11:23]} {e.event:<16} {e.outcome:<8} agent={e.agent_id} actor={e.actor}")


async def run_live(sc: Showcase) -> None:
    section("9. Live chat through Cuga (local LLM)")
    await deploy_all(sc)
    thread = sc.new_thread()
    turn = await sc.chat(LEARNING_COACH.name, "Enroll me in SEC301.", "carol", thread)
    say(f"  Carol: Enroll me in SEC301.\n  -> needs_approval={turn.needs_approval} error={turn.error}")
    if turn.needs_approval:
        turn = await sc.chat(LEARNING_COACH.name, None, "carol", thread, approve=True)
        say(f"  After approval: {turn.answer[:200]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="also chat with the agents through Cuga + Ollama")
    args = ap.parse_args(argv)

    settings = get_settings()
    if not args.live:
        settings = settings.model_copy(update={"llm_mode": "fake"})
    setup_logging(settings.log_level, settings.output_dir)
    sc = Showcase(settings=settings)

    async def _go() -> None:
        try:
            await run_deterministic(sc)
            if args.live:
                await run_live(sc)
        finally:
            await sc.aclose()

    try:
        asyncio.run(_go())
    except Exception as exc:  # keep the CLI friendly
        print(f"demo failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    out = Path(settings.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"demo_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text("# Agent Identity demo\n" + "\n".join(REPORT) + "\n", encoding="utf-8")
    print(f"\nReport: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
