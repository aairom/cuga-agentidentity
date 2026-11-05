"""Pure, UI-framework-free helpers used by the Streamlit app and the CLI demo.

Keeping this logic out of ``app.py`` makes it unit-testable. It contains:

* declarative descriptions of the Agent Identity flows plus a local SVG sequence-diagram
  renderer (no CDN / cloud dependency, so the docs work fully offline);
* token-claims presentation for the JWT inspector;
* flattening helpers for registry / audit tables;
* a small Ollama health probe.
"""

from __future__ import annotations

import html
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from .audit import AuditEvent
from .models import AgentRecord
from .tokens import decode_unverified

# --------------------------------------------------------------------------------------
# Flow descriptions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One arrow of a sequence diagram."""

    src: str
    dst: str
    label: str
    dashed: bool = False


@dataclass(frozen=True)
class Flow:
    """A documented identity flow."""

    key: str
    title: str
    summary: str
    participants: tuple[str, ...]
    steps: tuple[Step, ...]
    takeaways: tuple[str, ...]


FLOWS: tuple[Flow, ...] = (
    Flow(
        key="onboarding",
        title="1. Onboarding (create, approve, register)",
        summary="An agent gets a first-class identity before it can obtain a single token. "
                "Creation and activation are done by two different people (separation of duties).",
        participants=("Developer", "Agent Registry", "Approver", "OAuth server"),
        steps=(
            Step("Developer", "Agent Registry", "create agent (writeAgents)"),
            Step("Agent Registry", "Developer", "Agent ID, status DEFAULT", True),
            Step("Approver", "Agent Registry", "set status ACTIVE (manageAgentStatus)"),
            Step("Developer", "OAuth server", "dynamic client registration"),
            Step("OAuth server", "Developer", "client_id + secret (shown once)", True),
        ),
        takeaways=(
            "The creator can never approve their own agent.",
            "Only ACTIVE agents can register a client or get tokens.",
            "Every step is audited under the same Agent ID.",
        ),
    ),
    Flow(
        key="autonomous",
        title="2. Autonomous access (client credentials)",
        summary="The agent acts as itself. The token carries an agent_id claim so the resource "
                "server knows exactly which agent called.",
        participants=("Agent", "OAuth server", "Tool / API"),
        steps=(
            Step("Agent", "OAuth server", "client_credentials (client_id, secret)"),
            Step("OAuth server", "Agent", "access token {agent_id, scope}", True),
            Step("Agent", "Tool / API", "call with Bearer token"),
            Step("Tool / API", "Tool / API", "verify signature, aud, exp, scope, agent status"),
            Step("Tool / API", "Agent", "result or 403", True),
        ),
        takeaways=(
            "Scopes are limited by the ceiling configured in the registry.",
            "Use for batch / back-office work with no human in the loop.",
        ),
    ),
    Flow(
        key="obo",
        title="3. On-behalf-of (RFC 8693 token exchange)",
        summary="The agent acts for a user. The exchanged token has sub=user and act.sub=agent, "
                "so the agent can never do more than the user is allowed to.",
        participants=("User", "Agent", "OAuth server", "Tool / API"),
        steps=(
            Step("User", "Agent", "request + user access token"),
            Step("Agent", "OAuth server", "token-exchange: subject=user, actor=agent"),
            Step("OAuth server", "Agent", "token {sub=user, act.sub=agent}", True),
            Step("Agent", "Tool / API", "call with delegated token"),
            Step("Tool / API", "Tool / API", "scopes = user entitlements AND agent ceiling"),
            Step("Tool / API", "Agent", "result or 403", True),
        ),
        takeaways=(
            "Effective permission = user entitlements intersected with the agent ceiling.",
            "Carol cannot restart a service through the agent because Carol herself cannot.",
        ),
    ),
    Flow(
        key="privileged",
        title="4. Privileged action (human-in-the-loop)",
        summary="Sensitive tools pause for approval (Cuga tool-approval policy) and then use a "
                "short-lived token with authorization_details scoped to one action.",
        participants=("User", "Cuga agent", "OAuth server", "Tool / API"),
        steps=(
            Step("User", "Cuga agent", "enroll me in SEC301 (1800 USD)"),
            Step("Cuga agent", "User", "approval required", True),
            Step("User", "Cuga agent", "approve"),
            Step("Cuga agent", "OAuth server", "exchange + authorization_details"),
            Step("OAuth server", "Cuga agent", "privileged token (very short TTL)", True),
            Step("Cuga agent", "Tool / API", "enroll_expensive_course"),
            Step("Tool / API", "Cuga agent", "enrolled", True),
        ),
        takeaways=(
            "Approval is enforced by the runtime policy, not by the prompt.",
            "The privileged token is bound to the specific action and expires quickly.",
        ),
    ),
    Flow(
        key="chain",
        title="5. Multi-agent delegation (transaction context)",
        summary="A supervisor delegates to a worker. The worker token nests the chain "
                "act(worker) -> act(supervisor), giving a complete audit trail.",
        participants=("User", "Supervisor", "Worker agent", "Tool / API"),
        steps=(
            Step("User", "Supervisor", "request"),
            Step("Supervisor", "Supervisor", "exchange: sub=user, act=supervisor"),
            Step("Supervisor", "Worker agent", "delegate task + supervisor token"),
            Step("Worker agent", "Worker agent", "exchange: subject=supervisor token, actor=worker"),
            Step("Worker agent", "Tool / API", "token {sub=user, act=worker -> supervisor}"),
            Step("Tool / API", "Worker agent", "result", True),
        ),
        takeaways=(
            "The resource server sees who asked, and through which agents.",
            "Audit event 'access.granted' records the whole chain.",
        ),
    ),
    Flow(
        key="killswitch",
        title="6. Kill switch (suspend)",
        summary="Suspending an agent stops it from obtaining NEW tokens. Already-issued tokens stay "
                "valid until they expire, so resource servers should also check the agent status.",
        participants=("Approver", "Agent Registry", "Agent", "Tool / API"),
        steps=(
            Step("Approver", "Agent Registry", "suspend agent"),
            Step("Agent", "Agent Registry", "request new token"),
            Step("Agent Registry", "Agent", "refused (not ACTIVE)", True),
            Step("Agent", "Tool / API", "reuse old, still-valid token"),
            Step("Tool / API", "Agent Registry", "defence in depth: check status"),
            Step("Tool / API", "Agent", "403 AgentNotActive", True),
        ),
        takeaways=(
            "Keep token TTLs short.",
            "This showcase enables the strict status check by default (AI_STRICT_STATUS_CHECK).",
        ),
    ),
)


def flow_by_key(key: str) -> Flow:
    """Return the flow with ``key`` (raises ``KeyError`` if unknown)."""
    for f in FLOWS:
        if f.key == key:
            return f
    raise KeyError(key)


def flow_to_svg(flow: Flow, col_width: int = 190, row_height: int = 46) -> str:
    """Render a flow as a standalone SVG sequence diagram (dark/light neutral colours)."""
    parts = list(flow.participants)
    xs = {p: 20 + col_width // 2 + i * col_width for i, p in enumerate(parts)}
    width = 40 + col_width * len(parts)
    top = 56
    height = top + row_height * (len(flow.steps) + 1)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="100%" '
        f'role="img" aria-label="{html.escape(flow.title)}" font-family="IBM Plex Sans, Arial, sans-serif">',
        '<defs><marker id="ah" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto">'
        '<path d="M0,0 L9,4.5 L0,9 z" fill="#0f62fe"/></marker></defs>',
    ]
    for p in parts:
        x = xs[p]
        out.append(f'<rect x="{x - 70}" y="8" width="140" height="30" rx="4" fill="#0f62fe"/>')
        out.append(f'<text x="{x}" y="28" text-anchor="middle" font-size="12" fill="#fff">{html.escape(p)}</text>')
        out.append(f'<line x1="{x}" y1="38" x2="{x}" y2="{height - 8}" stroke="#8d8d8d" stroke-dasharray="4 4"/>')
    for i, s in enumerate(flow.steps, start=1):
        y = top + i * row_height - 10
        x1, x2 = xs[s.src], xs[s.dst]
        dash = ' stroke-dasharray="6 4"' if s.dashed else ""
        label = html.escape(f"{i}. {s.label}")
        if x1 == x2:  # self-call: small loop
            out.append(f'<path d="M{x1},{y - 8} h34 v18 h-34" fill="none" stroke="#0f62fe"{dash} marker-end="url(#ah)"/>')
            out.append(f'<text x="{x1 + 40}" y="{y + 2}" font-size="11" fill="#a8a8a8">{label}</text>')
        else:
            out.append(f'<line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" stroke="#0f62fe" stroke-width="1.6"{dash} '
                       f'marker-end="url(#ah)"/>')
            out.append(f'<text x="{(x1 + x2) / 2}" y="{y - 6}" text-anchor="middle" font-size="11" '
                       f'fill="#a8a8a8">{label}</text>')
    out.append("</svg>")
    return "".join(out)


def flow_to_mermaid(flow: Flow) -> str:
    """Return the same flow as Mermaid ``sequenceDiagram`` text (used by the docs generator)."""
    lines = ["sequenceDiagram"]
    lines += [f"    participant {p.replace(' ', '_').replace('/', '_')} as {p}" for p in flow.participants]
    for s in flow.steps:
        a, b = (x.replace(" ", "_").replace("/", "_") for x in (s.src, s.dst))
        lines.append(f"    {a}{'-->>' if s.dashed else '->>'}{b}: {s.label}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Token inspector
# --------------------------------------------------------------------------------------


def _chain(act: dict[str, Any] | None) -> list[str]:
    out: list[str] = []
    while act:
        out.append(str(act.get("sub")))
        act = act.get("act")
    return out


def inspect_token(token: str, now: float | None = None) -> dict[str, Any]:
    """Return a presentation-friendly summary of a JWT (no signature verification!)."""
    claims = decode_unverified(token)
    now = time.time() if now is None else now
    exp = claims.get("exp")
    scope = claims.get("scope", "")
    scopes = scope.split() if isinstance(scope, str) else list(scope)
    return {
        "subject": claims.get("sub"),
        "issuer": claims.get("iss"),
        "audience": claims.get("aud"),
        "agent_id": claims.get("agent_id"),
        "actor_chain": _chain(claims.get("act")),
        "scopes": scopes,
        "privileged": bool(claims.get("authorization_details")),
        "authorization_details": claims.get("authorization_details", []),
        "expires_in_s": None if exp is None else int(exp - now),
        "raw_claims": claims,
    }


def explain_token(summary: dict[str, Any]) -> list[str]:
    """Plain-English bullets describing what a token allows."""
    lines: list[str] = []
    if summary["actor_chain"]:
        chain = " -> ".join(summary["actor_chain"])
        lines.append(f"Delegated: acts for **{summary['subject']}** via agent chain `{chain}`.")
    elif summary["agent_id"]:
        lines.append(f"Autonomous: the agent `{summary['agent_id']}` acts as itself.")
    else:
        lines.append(f"End-user token for **{summary['subject']}** (no agent involved).")
    lines.append("Scopes: " + (", ".join(f"`{s}`" for s in summary["scopes"]) or "none"))
    if summary["privileged"]:
        lines.append("Privileged: bound to specific actions via `authorization_details`.")
    exp = summary["expires_in_s"]
    if exp is not None:
        lines.append("Expired." if exp <= 0 else f"Expires in {exp} s.")
    return lines


# --------------------------------------------------------------------------------------
# Table helpers
# --------------------------------------------------------------------------------------


def registry_rows(records: Iterable[AgentRecord]) -> list[dict[str, Any]]:
    """Flatten registry records for a table."""
    return [
        {
            "name": r.name,
            "agent_id": r.agent_id,
            "status": r.status.value,
            "owner": r.owner,
            "approved_by": r.approved_by or "",
            "client_id": r.client_id or "",
            "scope ceiling": ", ".join(sorted(r.allowed_scopes)),
        }
        for r in records
    ]


def audit_rows(events: Iterable[AuditEvent], names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Flatten audit events for a table; ``names`` maps agent_id -> friendly name."""
    names = names or {}
    return [
        {
            "time": e.ts[11:23],
            "event": e.event,
            "plane": e.plane,
            "outcome": e.outcome,
            "agent": names.get(e.agent_id or "", e.agent_id or ""),
            "actor": e.actor or "",
            "user": e.user_id or "",
            "detail": json.dumps(e.detail, default=str)[:160],
        }
        for e in events
    ]


# --------------------------------------------------------------------------------------
# Local LLM probe
# --------------------------------------------------------------------------------------


def probe_ollama(base_url: str, timeout: float = 1.5) -> tuple[bool, list[str]]:
    """Return ``(reachable, model_names)`` for an Ollama / OpenAI-compatible endpoint."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        with urllib.request.urlopen(f"{root}/api/tags", timeout=timeout) as resp:  # noqa: S310 - local URL from config
            data = json.loads(resp.read().decode("utf-8"))
        return True, [m.get("name", "") for m in data.get("models", [])]
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False, []
