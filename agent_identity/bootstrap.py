"""Bootstrap: deploy the example agents + supervisor into a :class:`Showcase`."""

from __future__ import annotations

from agents import SUPERVISOR_SPEC, WORKER_SPECS

from .runtime import AgentRuntime, Showcase


async def deploy_all(showcase: Showcase) -> dict[str, AgentRuntime]:
    """Onboard and deploy every worker agent and the supervisor. Returns runtimes by name."""
    workers = [await showcase.deploy(spec) for spec in WORKER_SPECS.values()]
    await showcase.deploy_supervisor(SUPERVISOR_SPEC, workers)
    return dict(showcase.runtimes)
